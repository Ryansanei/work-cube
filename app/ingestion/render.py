"""Renders a synthetic document-manifest entry into an actual PDF —
standing in for the "authority's document" a real user would upload. Native
PDF text (not a scanned image) is the honest Phase 1 scope: it exercises
Docling's real layout/text extraction and provenance end-to-end without
also taking on OCR-quality risk in the same pass. Degraded/scanned-image
input is a documented future enhancement, not built here.

Driven entirely by the template's field definitions — the label printed for
each field is exactly what app/ingestion/extract.py's label_aliases parses
back out, so a template's own data is the only thing keeping the two in
sync, not two hardcoded copies of the same strings.
"""

from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

from app.templates import Template


def _display_value(field, raw_value) -> str:
    if raw_value is None or raw_value == "":
        return "(none)"
    if field.field_type == "enum" and field.enum_options:
        match = next((o for o in field.enum_options if o["value"] == raw_value), None)
        return match["label"] if match else str(raw_value)
    return str(raw_value)


def render_document(doc: dict, template: Template, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / doc["filename"]
    c = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    x = inch
    y = height - 1.1 * inch
    line_height = 0.3 * inch
    omit = doc.get("omit_field")

    def line(text: str, size: int = 11, bold: bool = False) -> None:
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, text)
        y -= line_height

    line(template.authority_name.upper(), size=13, bold=True)
    line(f"{template.document_label} Notice", size=11, bold=True)
    y -= line_height * 0.5
    line(f"Document ID: doc_{doc['id']:03d}")

    amount_field = template.amount_field_name
    for field in template.fields:
        if field.name == amount_field:
            continue  # printed last, bold, below
        if field.name == omit:
            continue
        alias = field.label_aliases[0] if field.label_aliases else field.label
        value = doc.get(field.name)
        display = _display_value(field, value)
        line(f"{alias}: {display}")

    y -= line_height * 0.5
    amount_value = doc.get(amount_field)
    amount_label = template.field(amount_field).label_aliases[0]
    if amount_value is not None:
        line(f"{amount_label}: ${amount_value}", size=12, bold=True)
    else:
        line(f"{amount_label}: (pending verification — form incomplete)", size=11, bold=True)

    c.save()
    return path
