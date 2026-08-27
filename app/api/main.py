import io
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.ingestion.manifest import ManifestParseError, parse_manifest
from app.ingestion.page_image import bbox_to_css_pixels, render_page_png
from app.pipeline import (
    persist_document_and_run,
    reconcile_document,
    reconcile_from_field_values,
)
from app.provider_settings import (
    UnknownProviderError,
    delete_provider,
    list_providers,
    test_connection,
    upsert_provider,
)
from app.templates import Template, UnknownTemplateError, list_templates, load_template, load_template_by_id

app = FastAPI(title="Work Cube — Reconciliation Workspace")

STATIC_DIR = Path(__file__).parent.parent / "static"
UPLOADED_PDF_DIR = Path(__file__).parent.parent.parent / "data" / "documents" / "uploaded"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

VALID_STATUSES = {"pending", "clean_match", "discrepancy_found", "needs_review", "error"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _serialize_template(t: Template) -> dict:
    return {
        "id": t.id, "key": t.key, "name": t.name, "description": t.description,
        "authority_name": t.authority_name, "document_label": t.document_label,
        "fields": [
            {
                "name": f.name, "label": f.label, "field_type": f.field_type, "required": f.required,
                "is_dimension": f.is_dimension, "is_quantity": f.is_quantity,
                "is_amount": f.is_amount, "is_date": f.is_date, "enum_options": f.enum_options,
            }
            for f in t.fields
        ],
    }


@app.get("/api/templates")
def get_templates() -> list[dict]:
    with SessionLocal() as db:
        return [_serialize_template(t) for t in list_templates(db)]


@app.get("/api/runs")
def list_runs(status: str | None = Query(default=None), template: str | None = Query(default=None)) -> list[dict]:
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status {status!r}")

    query = """
        SELECT rr.id AS run_id, ad.id AS document_id, ad.filename, ad.document_date,
               ad.source_type, t.key AS template_key, t.name AS template_name,
               rr.status, rr.authority_amount, rr.recomputed_amount,
               rr.discrepancy_amount, rr.discrepancy_pct, rr.root_cause_category,
               rr.resolution_status
        FROM reconciliation_runs rr
        JOIN authority_documents ad ON ad.id = rr.document_id
        JOIN templates t ON t.id = ad.template_id
        WHERE 1=1
    """
    params = {}
    if status:
        query += " AND rr.status = :status"
        params["status"] = status
    if template:
        query += " AND t.key = :template"
        params["template"] = template
    query += " ORDER BY ABS(COALESCE(rr.discrepancy_amount, 0)) DESC, rr.id"

    with SessionLocal() as db:
        rows = db.execute(text(query), params).mappings().fetchall()

    return [_serialize_run_row(row) for row in rows]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    with SessionLocal() as db:
        row = db.execute(
            text("""
                SELECT rr.id AS run_id, rr.status, rr.authority_amount, rr.recomputed_amount,
                       rr.discrepancy_amount, rr.discrepancy_pct, rr.root_cause_category,
                       rr.calculation_trace, rr.evidence_ledger, rr.memo,
                       rr.resolution_status, rr.resolution_notes, rr.resolved_by, rr.resolved_at,
                       rr.activity_log,
                       ad.id AS document_id, ad.filename, ad.document_date, ad.language,
                       ad.source_type, ad.template_id, t.key AS template_key, t.name AS template_name,
                       ad.extracted_fields, ad.extraction_status
                FROM reconciliation_runs rr
                JOIN authority_documents ad ON ad.id = rr.document_id
                JOIN templates t ON t.id = ad.template_id
                WHERE rr.id = :run_id
            """),
            {"run_id": run_id},
        ).mappings().fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="run not found")

        template = load_template_by_id(db, row["template_id"])

    result = _serialize_run_row(row)
    result["calculation_trace"] = row["calculation_trace"]
    result["evidence_ledger"] = row["evidence_ledger"]
    result["memo"] = row["memo"]
    result["extraction_status"] = row["extraction_status"]
    result["resolution_status"] = row["resolution_status"]
    result["resolution_notes"] = row["resolution_notes"]
    result["resolved_by"] = row["resolved_by"]
    result["resolved_at"] = row["resolved_at"].isoformat() if row["resolved_at"] else None
    result["activity_log"] = row["activity_log"] or []
    result["has_page_image"] = row["source_type"] != "excel_row"
    result["template"] = _serialize_template(template)

    # Every template field gets an entry, even if extraction found nothing for
    # it — a fully-failed extraction should still let a reviewer fill in every
    # field by hand, not just the ones that happened to be found.
    extracted = row["extracted_fields"] or {}
    fields_with_overlay = {}
    for f in template.fields:
        stored = extracted.get(f.name, {})
        bbox = stored.get("bbox")
        fields_with_overlay[f.name] = {
            "value": stored.get("value", ""),
            "page_no": stored.get("page_no"),
            "css_box": bbox_to_css_pixels(tuple(bbox)) if bbox else None,
        }
    result["extracted_fields"] = fields_with_overlay
    return result


@app.get("/api/rate-schedule")
def get_rate_schedule(template: str = Query(...)) -> list[dict]:
    with SessionLocal() as db:
        try:
            tmpl = load_template(db, template)
        except UnknownTemplateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        versions = db.execute(
            text("""
                SELECT id, effective_from, effective_to, published_by, notes
                FROM rate_schedule_versions WHERE template_id = :tid ORDER BY effective_from
            """),
            {"tid": tmpl.id},
        ).mappings().fetchall()

        result = []
        for v in versions:
            rules = db.execute(
                text("""
                    SELECT dimension_key, base_fee, rate_terms, surcharge_pct
                    FROM rate_rules WHERE schedule_version_id = :vid ORDER BY dimension_key
                """),
                {"vid": v["id"]},
            ).mappings().fetchall()
            exemptions = db.execute(
                text("""
                    SELECT exemption_code, description, discount_pct, applies_to
                    FROM exemption_rules WHERE schedule_version_id = :vid ORDER BY exemption_code
                """),
                {"vid": v["id"]},
            ).mappings().fetchall()

            result.append({
                "id": v["id"],
                "effective_from": v["effective_from"].isoformat(),
                "effective_to": v["effective_to"].isoformat() if v["effective_to"] else None,
                "published_by": v["published_by"],
                "notes": v["notes"],
                "rate_rules": [
                    {
                        "dimension_key": r["dimension_key"], "base_fee": float(r["base_fee"]),
                        "rate_terms": [
                            {"field": t["field"], "rate": float(t["rate"]),
                             "free_threshold": float(t.get("free_threshold", 0))}
                            for t in r["rate_terms"]
                        ],
                        "surcharge_pct": float(r["surcharge_pct"]),
                    }
                    for r in rules
                ],
                "exemption_rules": [
                    {
                        "exemption_code": e["exemption_code"], "description": e["description"],
                        "discount_pct": float(e["discount_pct"]), "applies_to": e["applies_to"],
                    }
                    for e in exemptions
                ],
            })
    return result


@app.get("/api/documents/{document_id}/page-image")
def get_document_page_image(document_id: int) -> Response:
    with SessionLocal() as db:
        row = db.execute(
            text("SELECT source_path FROM authority_documents WHERE id = :id"),
            {"id": document_id},
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="document not found")
    if row.source_path is None:
        raise HTTPException(status_code=404, detail="this document has no source image (manifest row)")

    png_bytes = render_page_png(row.source_path)
    return Response(content=png_bytes, media_type="image/png")


class ProviderCredentials(BaseModel):
    api_key: str
    model: str


@app.get("/api/settings/providers")
def get_provider_settings() -> list[dict]:
    with SessionLocal() as db:
        return list_providers(db)


@app.put("/api/settings/providers/{provider}")
def put_provider_settings(provider: str, body: ProviderCredentials) -> dict:
    with SessionLocal() as db:
        try:
            upsert_provider(db, provider, body.api_key, body.model)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return next(p for p in list_providers(db) if p["provider"] == provider)


@app.delete("/api/settings/providers/{provider}")
def delete_provider_settings(provider: str) -> dict:
    with SessionLocal() as db:
        try:
            removed = delete_provider(db, provider)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not removed:
            raise HTTPException(status_code=404, detail="provider not configured")
        return {"provider": provider, "configured": False}


@app.post("/api/settings/providers/{provider}/test")
def test_provider_settings(provider: str) -> dict:
    with SessionLocal() as db:
        try:
            return test_connection(db, provider)
        except UnknownProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


class FieldCorrection(BaseModel):
    fields: dict[str, str]
    actor: str = "reviewer"


class ResolveRequest(BaseModel):
    resolution_status: str
    notes: str = ""
    resolved_by: str = "reviewer"


VALID_RESOLUTION_STATUSES = {"open", "corrected", "accepted", "disputed", "resolved"}


@app.patch("/api/runs/{run_id}/fields")
def correct_fields(run_id: int, body: FieldCorrection) -> dict:
    with SessionLocal() as db:
        doc_row = db.execute(
            text("""
                SELECT ad.id AS document_id, ad.extracted_fields
                FROM reconciliation_runs rr JOIN authority_documents ad ON ad.id = rr.document_id
                WHERE rr.id = :run_id
            """),
            {"run_id": run_id},
        ).mappings().fetchone()
        if doc_row is None:
            raise HTTPException(status_code=404, detail="run not found")

        current_fields = dict(doc_row["extracted_fields"] or {})
        changes = {}
        for name, new_value in body.fields.items():
            old_value = current_fields.get(name, {}).get("value")
            changes[name] = {"old": old_value, "new": new_value}
            existing = current_fields.get(name, {"page_no": None, "bbox": None})
            current_fields[name] = {**existing, "value": new_value}

        db.execute(
            text("UPDATE authority_documents SET extracted_fields = :fields WHERE id = :id"),
            {"fields": _to_json(current_fields), "id": doc_row["document_id"]},
        )
        _append_activity(db, run_id, "corrected", body.actor, {"changes": changes})
        db.commit()

    return {"run_id": run_id, "corrected_fields": list(body.fields.keys())}


@app.post("/api/runs/{run_id}/recompute")
def recompute_run(run_id: int, actor: str = "reviewer") -> dict:
    with SessionLocal() as db:
        doc_row = db.execute(
            text("""
                SELECT ad.id AS document_id, ad.template_id, ad.extracted_fields,
                       rr.status AS old_status, rr.recomputed_amount AS old_amount
                FROM reconciliation_runs rr JOIN authority_documents ad ON ad.id = rr.document_id
                WHERE rr.id = :run_id
            """),
            {"run_id": run_id},
        ).mappings().fetchone()
        if doc_row is None:
            raise HTTPException(status_code=404, detail="run not found")

        template = load_template_by_id(db, doc_row["template_id"])
        field_values = {name: f["value"] for name, f in (doc_row["extracted_fields"] or {}).items()}
        outcome = reconcile_from_field_values(field_values, template)

        db.execute(
            text("""
                UPDATE reconciliation_runs
                SET authority_amount = :aa, recomputed_amount = :ra, discrepancy_amount = :da,
                    discrepancy_pct = :dp, status = :status, calculation_trace = :trace,
                    resolution_status = 'corrected'
                WHERE id = :run_id
            """),
            {
                "aa": outcome.authority_amount, "ra": outcome.recomputed_amount,
                "da": outcome.discrepancy_amount, "dp": outcome.discrepancy_pct,
                "status": outcome.status, "trace": _to_json(outcome.calculation_trace),
                "run_id": run_id,
            },
        )
        new_extraction_status = "needs_review" if outcome.status in ("needs_review", "error") else "extracted"
        db.execute(
            text("UPDATE authority_documents SET extraction_status = :s WHERE id = :id"),
            {"s": new_extraction_status, "id": doc_row["document_id"]},
        )
        _append_activity(db, run_id, "recomputed", actor, {
            "old_status": doc_row["old_status"], "new_status": outcome.status,
            "old_recomputed_amount": _opt_float(doc_row["old_amount"]),
            "new_recomputed_amount": _opt_float(outcome.recomputed_amount),
        })
        db.commit()

    return {"run_id": run_id, "status": outcome.status}


@app.post("/api/runs/{run_id}/resolve")
def resolve_run(run_id: int, body: ResolveRequest) -> dict:
    if body.resolution_status not in VALID_RESOLUTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid resolution_status {body.resolution_status!r}")

    with SessionLocal() as db:
        result = db.execute(
            text("""
                UPDATE reconciliation_runs
                SET resolution_status = :status, resolution_notes = :notes,
                    resolved_by = :resolved_by, resolved_at = now()
                WHERE id = :run_id
            """),
            {
                "status": body.resolution_status, "notes": body.notes,
                "resolved_by": body.resolved_by, "run_id": run_id,
            },
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="run not found")
        _append_activity(db, run_id, "resolved", body.resolved_by, {
            "resolution_status": body.resolution_status, "notes": body.notes,
        })
        db.commit()

    return {"run_id": run_id, "resolution_status": body.resolution_status}


def _append_activity(db: Session, run_id: int, action: str, actor: str, details: dict) -> None:
    entry = {"at": _now_iso(), "action": action, "actor": actor, "details": details}
    db.execute(
        text("""
            UPDATE reconciliation_runs SET activity_log = activity_log || CAST(:entry AS jsonb)
            WHERE id = :run_id
        """),
        {"entry": _to_json([entry]), "run_id": run_id},
    )


def _to_json(value) -> str | None:
    return json.dumps(value) if value is not None else None


@app.post("/api/upload/pdf")
async def upload_pdf(template: str = Form(...), files: list[UploadFile] = File(...)) -> list[dict]:
    UPLOADED_PDF_DIR.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        try:
            tmpl = load_template(db, template)
        except UnknownTemplateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    results = []
    with SessionLocal() as db:
        for upload in files:
            if not upload.filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
                results.append({
                    "filename": upload.filename,
                    "error": "unsupported file type — expected PDF or image",
                })
                continue

            dest_path = UPLOADED_PDF_DIR / f"{uuid.uuid4().hex[:8]}_{upload.filename}"
            dest_path.write_bytes(await upload.read())

            doc_id = _insert_pending_document(db, tmpl.id, upload.filename, str(dest_path), "pdf_upload")
            try:
                extraction, outcome = reconcile_document(dest_path, tmpl)
            except Exception as exc:
                results.append({"filename": upload.filename, "document_id": doc_id, "error": str(exc)})
                continue

            fields_for_storage = {
                name: {"value": f.value, "page_no": f.page_no, "bbox": list(f.bbox)}
                for name, f in extraction.fields.items()
            }
            date_field = extraction.fields.get(tmpl.date_field_name)
            if date_field:
                db.execute(
                    text("UPDATE authority_documents SET document_date = :d WHERE id = :id"),
                    {"d": date_field.value, "id": doc_id},
                )
            run_id = persist_document_and_run(
                db, doc_id, fields_for_storage, outcome,
                activity={
                    "at": _now_iso(), "action": "uploaded", "actor": "user",
                    "details": {"source": "pdf_upload"},
                },
            )
            results.append({
                "filename": upload.filename, "document_id": doc_id,
                "run_id": run_id, "status": outcome.status,
            })
        db.commit()
    return results


@app.post("/api/upload/manifest")
async def upload_manifest(template: str = Form(...), file: UploadFile = File(...)) -> dict:
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="expected an .xlsx file")

    with SessionLocal() as db:
        try:
            tmpl = load_template(db, template)
        except UnknownTemplateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    content = await file.read()
    try:
        records = parse_manifest(io.BytesIO(content), tmpl)
    except ManifestParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    results = []
    with SessionLocal() as db:
        for i, record in enumerate(records, start=1):
            row_label = f"{file.filename} — row {i + 1}"
            doc_id = _insert_pending_document(
                db, tmpl.id, row_label, None, "excel_row",
                document_date=record.get(tmpl.date_field_name) or None,
            )

            outcome = reconcile_from_field_values(record, tmpl)
            fields_for_storage = {
                name: {"value": value, "page_no": None, "bbox": None} for name, value in record.items()
            }
            run_id = persist_document_and_run(
                db, doc_id, fields_for_storage, outcome,
                activity={
                    "at": _now_iso(), "action": "uploaded", "actor": "user",
                    "details": {"source": "excel_manifest", "manifest": file.filename, "row": i + 1},
                },
            )
            results.append({
                "filename": row_label, "document_id": doc_id,
                "run_id": run_id, "status": outcome.status,
            })
        db.commit()
    return {"manifest": file.filename, "rows_processed": len(results), "results": results}


def _insert_pending_document(
    db: Session, template_id: int, filename: str, source_path: str | None, source_type: str,
    document_date: str | None = None,
) -> int:
    return db.execute(
        text("""
            INSERT INTO authority_documents
                (template_id, filename, source_path, source_type, extraction_status, document_date)
            VALUES (:tid, :fn, :sp, :st, 'pending', :dd)
            RETURNING id
        """),
        {"tid": template_id, "fn": filename, "sp": source_path, "st": source_type, "dd": document_date},
    ).scalar_one()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _opt_float(value) -> float | None:
    return float(value) if value is not None else None


def _serialize_run_row(row) -> dict:
    return {
        "run_id": row["run_id"],
        "document_id": row["document_id"],
        "filename": row["filename"],
        "document_date": row["document_date"].isoformat() if row["document_date"] else None,
        "source_type": row["source_type"],
        "template_key": row["template_key"],
        "template_name": row["template_name"],
        "status": row["status"],
        "authority_amount": _opt_float(row["authority_amount"]),
        "recomputed_amount": _opt_float(row["recomputed_amount"]),
        "discrepancy_amount": _opt_float(row["discrepancy_amount"]),
        "discrepancy_pct": _opt_float(row["discrepancy_pct"]),
        "root_cause_category": row["root_cause_category"],
        "resolution_status": row["resolution_status"],
    }
