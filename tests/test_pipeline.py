"""Exercises the real Docling extraction + calculator against freshly
rendered PDFs — slower than the rest of the suite (real model inference,
not mocked), kept to a small number of cases deliberately."""

from pathlib import Path

from app.pipeline import reconcile_document
from app.templates import load_template

BASE_DOC = {
    "id": 901, "filename": "test_doc.pdf", "language": "en",
    "shipment_date": "2024-06-01", "cargo_class": "general_freight",
    "vehicle_class": "light_truck", "weight_kg": "2500", "distance_km": "100",
    "exemption_code": None,
}


def _render(tmp_path: Path, template, **overrides) -> Path:
    from app.ingestion.render import render_document
    doc = {**BASE_DOC, **overrides}
    return render_document(doc, template, tmp_path)


def test_reconcile_clean_match(tmp_path, db):
    tmpl = load_template(db, "transport_levy")
    path = _render(tmp_path, tmpl, authority_claimed_amount="79.00")  # matches real calculation
    extraction, outcome = reconcile_document(path, tmpl)
    assert outcome.status == "clean_match"
    assert outcome.discrepancy_amount == 0
    assert extraction.fields["cargo_class"].value == "general_freight"
    assert extraction.fields["cargo_class"].bbox != (0, 0, 0, 0)


def test_reconcile_discrepancy_found(tmp_path, db):
    tmpl = load_template(db, "transport_levy")
    path = _render(tmp_path, tmpl, authority_claimed_amount="50.00")  # deliberately wrong
    extraction, outcome = reconcile_document(path, tmpl)
    assert outcome.status == "discrepancy_found"
    assert outcome.recomputed_amount is not None
    assert abs(outcome.discrepancy_amount) > 0


def test_reconcile_needs_review_on_missing_field(tmp_path, db):
    tmpl = load_template(db, "transport_levy")
    path = _render(tmp_path, tmpl, authority_claimed_amount="79.00", omit_field="cargo_class")
    extraction, outcome = reconcile_document(path, tmpl)
    assert outcome.status == "needs_review"
    assert "cargo_class" in outcome.missing_fields
    assert "cargo_class" not in extraction.fields


def test_reconcile_second_template_end_to_end(tmp_path, db):
    """Proves the pipeline itself is template-agnostic, not just the
    calculator — real Docling extraction against a document rendered for a
    completely different template's field set."""
    tmpl = load_template(db, "utility_bill")
    doc = {
        "id": 902, "filename": "util_test.pdf",
        "billing_period_end": "2024-06-01", "customer_class": "residential",
        "kwh_used": "620", "exemption_code": None, "authority_claimed_amount": "91.80",
    }
    from app.ingestion.render import render_document
    path = render_document(doc, tmpl, tmp_path)
    extraction, outcome = reconcile_document(path, tmpl)
    assert outcome.status == "clean_match"
    assert extraction.fields["customer_class"].value == "residential"
