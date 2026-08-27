import io

import openpyxl
import pytest

from app.ingestion.manifest import ManifestParseError, parse_manifest


def _make_xlsx(rows: list[list]) -> io.BytesIO:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


HEADER = ["cargo_class", "vehicle_class", "weight_kg", "distance_km", "shipment_date",
          "exemption_code", "authority_claimed_amount"]


def test_parse_manifest_happy_path(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([
        HEADER,
        ["general_freight", "light_truck", 2500, 100, "2024-06-01", None, 79.00],
    ])
    records = parse_manifest(file_obj, tmpl)
    assert len(records) == 1
    assert records[0]["cargo_class"] == "general_freight"
    assert records[0]["weight_kg"] == "2500"
    assert records[0]["shipment_date"] == "2024-06-01"


def test_parse_manifest_skips_blank_rows(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([
        HEADER,
        ["general_freight", "light_truck", 2500, 100, "2024-06-01", None, 79.00],
        [None, None, None, None, None, None, None],
        ["perishable", "heavy_truck", 4000, 200, "2025-08-01", "COLD-CHAIN", 150.00],
    ])
    records = parse_manifest(file_obj, tmpl)
    assert len(records) == 2


def test_parse_manifest_missing_required_column_raises(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([
        ["cargo_class", "vehicle_class", "weight_kg"],  # missing distance_km, shipment_date, amount
        ["general_freight", "light_truck", 2500],
    ])
    with pytest.raises(ManifestParseError, match="missing required column"):
        parse_manifest(file_obj, tmpl)


def test_parse_manifest_empty_file_raises(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([])
    with pytest.raises(ManifestParseError):
        parse_manifest(file_obj, tmpl)


def test_parse_manifest_no_data_rows_raises(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([HEADER])
    with pytest.raises(ManifestParseError, match="no data rows"):
        parse_manifest(file_obj, tmpl)


def test_parse_manifest_excel_native_date_and_number_types(db):
    from datetime import date

    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    file_obj = _make_xlsx([
        HEADER,
        ["hazardous", "heavy_truck", 5000, 300, date(2024, 8, 15), None, 200],
    ])
    records = parse_manifest(file_obj, tmpl)
    assert records[0]["shipment_date"] == "2024-08-15"
    assert records[0]["weight_kg"] == "5000"


def test_parse_manifest_works_for_a_second_template_with_different_fields(db):
    """Proves the parser is genuinely template-driven, not just accepting
    an extra parameter it ignores — a utility_bill manifest has a
    completely different column set than transport_levy's."""
    from app.templates import load_template
    tmpl = load_template(db, "utility_bill")
    file_obj = _make_xlsx([
        ["customer_class", "kwh_used", "billing_period_end", "exemption_code", "authority_claimed_amount"],
        ["residential", 620, "2026-02-15", None, 99.80],
    ])
    records = parse_manifest(file_obj, tmpl)
    assert len(records) == 1
    assert records[0]["customer_class"] == "residential"
    assert records[0]["kwh_used"] == "620"
