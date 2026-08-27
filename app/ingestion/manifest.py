"""Parses a bulk Excel manifest — a real freight-audit pattern: reconcile
many records from one spreadsheet instead of one document at a time.
Bypasses Docling entirely, since there's no source image to extract from or
cite; a manifest row's provenance is "row N of this spreadsheet," not a
bounding box. Expected columns come from the active template's fields, not
a hardcoded list.
"""

from datetime import date, datetime
from decimal import Decimal

import openpyxl

from app.templates import Template


class ManifestParseError(Exception):
    pass


def parse_manifest(file_obj, template: Template) -> list[dict[str, str]]:
    try:
        wb = openpyxl.load_workbook(file_obj, data_only=True)
    except Exception as exc:
        raise ManifestParseError(f"could not read file as an Excel workbook: {exc}") from exc

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ManifestParseError("manifest is empty")

    required_columns = template.required_field_names
    all_columns = [f.name for f in template.fields]

    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    col_index = {name: header.index(name) for name in all_columns if name in header}
    missing = [c for c in required_columns if c not in col_index]
    if missing:
        raise ManifestParseError(
            f"manifest is missing required column(s): {', '.join(missing)}. "
            f"Expected a header row with: {', '.join(required_columns)}"
        )

    records = []
    for row in rows[1:]:
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        record = {
            name: _normalize_cell(template.field(name), row[idx] if idx < len(row) else None)
            for name, idx in col_index.items()
        }
        records.append(record)

    if not records:
        raise ManifestParseError("manifest has a header row but no data rows")
    return records


def _normalize_cell(field, value) -> str:
    if value is None:
        return ""
    if field.field_type == "date":
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value).strip()
    if field.field_type == "number":
        if isinstance(value, int | float | Decimal):
            return str(Decimal(str(value)))
        return str(value).strip()
    return str(value).strip()
