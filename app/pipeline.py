"""Phase 1 reconciliation pipeline: extract -> recompute -> compare ->
persist. No investigation yet — a discrepancy is flagged and stored with its
calculation trace, but root-cause attribution is Phase 2 (LangGraph), not
built here.

`reconcile_from_field_values` is the single recompute/compare code path
shared by every entry point that can produce a field-values dict: Docling
PDF extraction, a parsed Excel manifest row, and a reviewer's manual
correction. They must never drift into three different implementations of
"is this a match." It's also template-agnostic — every domain-specific
detail (which fields exist, which one is the amount, which is the date,
which are dimensions) comes from the `Template` passed in, not from
anything hardcoded here.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.calculator.calculator import (
    CalculationInput,
    NoApplicableRateRuleError,
    NoApplicableScheduleError,
    calculate,
)
from app.ingestion.extract import ExtractionResult, extract_document
from app.templates import Template, load_template_by_id

MATCH_TOLERANCE = Decimal("0.01")


@dataclass
class ReconciliationOutcome:
    status: str  # clean_match | discrepancy_found | needs_review | error
    authority_amount: Decimal | None
    recomputed_amount: Decimal | None
    discrepancy_amount: Decimal | None
    discrepancy_pct: float | None
    calculation_trace: dict | None
    missing_fields: list[str]
    error_message: str | None = None


def reconcile_from_field_values(field_values: dict[str, str], template: Template) -> ReconciliationOutcome:
    """The shared recompute/compare logic — takes a flat field-name-to-string
    dict from any source and produces a ReconciliationOutcome, entirely
    driven by the given template's field roles."""
    missing = [f for f in template.required_field_names if not field_values.get(f)]
    if missing:
        return ReconciliationOutcome(
            status="needs_review", authority_amount=None, recomputed_amount=None,
            discrepancy_amount=None, discrepancy_pct=None, calculation_trace=None,
            missing_fields=missing,
        )

    try:
        calc_date = date.fromisoformat(field_values[template.date_field_name])
        calc_input = CalculationInput(
            template_id=template.id,
            field_values=field_values,
            calc_date=calc_date,
            exemption_code=field_values.get("exemption_code") or None,
        )
        authority_amount = Decimal(field_values[template.amount_field_name])
    except (ValueError, InvalidOperation, KeyError) as exc:
        return ReconciliationOutcome(
            status="error", authority_amount=None, recomputed_amount=None,
            discrepancy_amount=None, discrepancy_pct=None, calculation_trace=None,
            missing_fields=[], error_message=f"could not parse field values: {exc}",
        )

    return _recompute_and_compare(calc_input, authority_amount, template)


def _recompute_and_compare(
    calc_input: CalculationInput, authority_amount: Decimal, template: Template
) -> ReconciliationOutcome:
    from app.db import SessionLocal  # local import avoids a module-load-time DB dependency

    with SessionLocal() as db:
        try:
            result = calculate(db, calc_input, template.dimension_field_names)
        except (NoApplicableScheduleError, NoApplicableRateRuleError) as exc:
            return ReconciliationOutcome(
                status="error", authority_amount=authority_amount, recomputed_amount=None,
                discrepancy_amount=None, discrepancy_pct=None, calculation_trace=None,
                missing_fields=[], error_message=str(exc),
            )

    discrepancy = result.total - authority_amount
    discrepancy_pct = float(discrepancy / authority_amount * 100) if authority_amount != 0 else None
    status = "clean_match" if abs(discrepancy) <= MATCH_TOLERANCE else "discrepancy_found"

    return ReconciliationOutcome(
        status=status,
        authority_amount=authority_amount,
        recomputed_amount=result.total,
        discrepancy_amount=discrepancy,
        discrepancy_pct=discrepancy_pct,
        calculation_trace=result.to_dict(),
        missing_fields=[],
    )


def reconcile_document(pdf_path: Path, template: Template) -> tuple[ExtractionResult, ReconciliationOutcome]:
    extraction = extract_document(pdf_path, template)
    field_values = {name: f.value for name, f in extraction.fields.items()}
    outcome = reconcile_from_field_values(field_values, template)
    return extraction, outcome


def persist_document_and_run(
    db: Session,
    document_id: int,
    field_values_for_storage: dict,
    outcome: ReconciliationOutcome,
    activity: dict | None = None,
) -> int:
    """Writes extracted_fields + extraction_status to an existing
    authority_documents row and inserts its reconciliation_runs row. Returns
    the new run id. `field_values_for_storage` uses the same
    {field: {value, page_no, bbox}} shape the UI's overlay expects — bbox is
    None for fields with no source-image provenance (manifest rows)."""
    extraction_status = "needs_review" if outcome.status in ("needs_review", "error") else "extracted"
    db.execute(
        text("""
            UPDATE authority_documents
            SET extracted_fields = :fields, extraction_status = :status
            WHERE id = :id
        """),
        {"fields": _to_json(field_values_for_storage), "status": extraction_status, "id": document_id},
    )
    activity_log = [activity] if activity else []
    run_id = db.execute(
        text("""
            INSERT INTO reconciliation_runs
                (document_id, authority_amount, recomputed_amount, discrepancy_amount,
                 discrepancy_pct, status, calculation_trace, activity_log, created_at)
            VALUES (:doc_id, :aa, :ra, :da, :dp, :status, :trace, :log, :created_at)
            RETURNING id
        """),
        {
            "doc_id": document_id,
            "aa": outcome.authority_amount, "ra": outcome.recomputed_amount,
            "da": outcome.discrepancy_amount, "dp": outcome.discrepancy_pct,
            "status": outcome.status, "trace": _to_json(outcome.calculation_trace),
            "log": _to_json(activity_log), "created_at": datetime.now(UTC),
        },
    ).scalar_one()
    return run_id


def process_pending_documents(db: Session) -> int:
    """Runs the Phase 1 pipeline over every pending authority_documents row
    with source_type='pdf_upload' or 'synthetic_seed' (i.e. anything that
    needs real Docling extraction). Excel-manifest rows are reconciled
    directly at upload time instead, since there's no document to extract.
    Returns the number of documents processed."""
    rows = db.execute(
        text("""
            SELECT id, source_path, template_id FROM authority_documents
            WHERE extraction_status = 'pending' AND source_type IN ('pdf_upload', 'synthetic_seed')
        """)
    ).fetchall()

    for row in rows:
        template = load_template_by_id(db, row.template_id)
        extraction, outcome = reconcile_document(Path(row.source_path), template)
        fields_for_storage = {
            name: {"value": f.value, "page_no": f.page_no, "bbox": list(f.bbox)}
            for name, f in extraction.fields.items()
        }
        persist_document_and_run(db, row.id, fields_for_storage, outcome)
    db.commit()
    return len(rows)


def _to_json(value) -> str | None:
    return json.dumps(value) if value is not None else None
