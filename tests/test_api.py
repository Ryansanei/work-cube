"""Real end-to-end tests against the actual FastAPI app + real Postgres +
real Docling — using FastAPI's TestClient (no mocking of the pipeline).
Slower than a mocked test, deliberately: these are the paths a real upload
or a real reviewer action would take.
"""

import io

import openpyxl
from fastapi.testclient import TestClient

from app.api.main import app
from app.ingestion.render import render_document
from app.templates import load_template

client = TestClient(app)

HEADER = ["cargo_class", "vehicle_class", "weight_kg", "distance_km", "shipment_date",
          "exemption_code", "authority_claimed_amount"]


def _make_xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_upload_pdf_creates_a_real_run(tmp_path, db):
    tmpl = load_template(db, "transport_levy")
    doc = {
        "id": 9001, "filename": "api_test_upload.pdf", "language": "en",
        "shipment_date": "2024-06-01", "cargo_class": "general_freight",
        "vehicle_class": "light_truck", "weight_kg": "2500", "distance_km": "100",
        "exemption_code": None, "authority_claimed_amount": "79.00",
    }
    pdf_path = render_document(doc, tmpl, tmp_path)

    with open(pdf_path, "rb") as f:
        res = client.post(
            "/api/upload/pdf", data={"template": "transport_levy"},
            files={"files": ("api_test_upload.pdf", f, "application/pdf")},
        )

    assert res.status_code == 200
    results = res.json()
    assert len(results) == 1
    assert results[0]["status"] == "clean_match"
    assert "run_id" in results[0]

    detail = client.get(f"/api/runs/{results[0]['run_id']}").json()
    assert detail["source_type"] == "pdf_upload"
    assert detail["template_key"] == "transport_levy"
    assert detail["has_page_image"] is True
    assert detail["extracted_fields"]["cargo_class"]["value"] == "general_freight"


def test_upload_pdf_rejects_unsupported_file_type():
    res = client.post(
        "/api/upload/pdf", data={"template": "transport_levy"},
        files={"files": ("not_a_pdf.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 200
    assert "error" in res.json()[0]


def test_upload_pdf_rejects_unknown_template():
    res = client.post(
        "/api/upload/pdf", data={"template": "not_a_real_template"},
        files={"files": ("x.pdf", b"hello", "application/pdf")},
    )
    assert res.status_code == 404


def test_upload_manifest_creates_runs_with_no_page_image():
    content = _make_xlsx_bytes([
        HEADER,
        ["general_freight", "light_truck", 2500, 100, "2024-06-01", None, 79.00],
    ])
    res = client.post(
        "/api/upload/manifest", data={"template": "transport_levy"},
        files={"file": ("api_test_manifest.xlsx", content,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["rows_processed"] == 1
    run_id = body["results"][0]["run_id"]

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["source_type"] == "excel_row"
    assert detail["has_page_image"] is False
    assert detail["extracted_fields"]["cargo_class"]["css_box"] is None


def test_upload_manifest_missing_columns_returns_400():
    content = _make_xlsx_bytes([["cargo_class", "vehicle_class"], ["general_freight", "light_truck"]])
    res = client.post(
        "/api/upload/manifest", data={"template": "transport_levy"},
        files={"file": ("bad_manifest.xlsx", content,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 400


def test_upload_manifest_for_second_template():
    """Proves the upload endpoint is genuinely template-driven — a
    utility_bill manifest has a completely different column set."""
    content = _make_xlsx_bytes([
        ["customer_class", "kwh_used", "billing_period_end", "exemption_code", "authority_claimed_amount"],
        ["residential", 620, "2024-06-01", None, "91.80"],
    ])
    res = client.post(
        "/api/upload/manifest", data={"template": "utility_bill"},
        files={"file": ("util_manifest.xlsx", content,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["results"][0]["status"] == "clean_match"
    detail = client.get(f"/api/runs/{body['results'][0]['run_id']}").json()
    assert detail["template_key"] == "utility_bill"


def test_correct_fields_recompute_and_resolve_full_lifecycle():
    # A manifest row missing weight_kg -> needs_review, then corrected and resolved.
    content = _make_xlsx_bytes([
        HEADER,
        ["perishable", "light_truck", None, 250, "2025-09-01", None, 150.00],
    ])
    upload_res = client.post(
        "/api/upload/manifest", data={"template": "transport_levy"},
        files={"file": ("lifecycle_test.xlsx", content,
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ).json()
    run_id = upload_res["results"][0]["run_id"]
    assert upload_res["results"][0]["status"] == "needs_review"

    correct_res = client.patch(
        f"/api/runs/{run_id}/fields", json={"fields": {"weight_kg": "1800"}, "actor": "test"}
    )
    assert correct_res.status_code == 200

    recompute_res = client.post(f"/api/runs/{run_id}/recompute", params={"actor": "test"})
    assert recompute_res.status_code == 200
    assert recompute_res.json()["status"] in ("clean_match", "discrepancy_found")

    resolve_res = client.post(f"/api/runs/{run_id}/resolve", json={
        "resolution_status": "resolved", "notes": "fixed and verified", "resolved_by": "test",
    })
    assert resolve_res.status_code == 200

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["resolution_status"] == "resolved"
    assert detail["resolution_notes"] == "fixed and verified"
    assert detail["extraction_status"] == "extracted"
    actions = [a["action"] for a in detail["activity_log"]]
    assert actions == ["uploaded", "corrected", "recomputed", "resolved"]


def test_resolve_run_rejects_invalid_status():
    res = client.post("/api/runs/1/resolve", json={"resolution_status": "not_a_real_status"})
    assert res.status_code == 400


def test_resolve_nonexistent_run_returns_404():
    res = client.post("/api/runs/999999/resolve", json={"resolution_status": "accepted"})
    assert res.status_code == 404


def test_rate_schedule_endpoint_returns_versions_with_rules():
    res = client.get("/api/rate-schedule", params={"template": "transport_levy"})
    assert res.status_code == 200
    versions = res.json()
    assert len(versions) == 2
    assert len(versions[0]["rate_rules"]) == 12
    assert any(e["exemption_code"] == "COLD-CHAIN" for e in versions[1]["exemption_rules"])
    assert not any(e["exemption_code"] == "COLD-CHAIN" for e in versions[0]["exemption_rules"])


def test_rate_schedule_endpoint_for_second_template():
    res = client.get("/api/rate-schedule", params={"template": "utility_bill"})
    assert res.status_code == 200
    versions = res.json()
    assert len(versions[0]["rate_rules"]) == 3
    assert len(versions[0]["rate_rules"][0]["rate_terms"]) == 1


def test_rate_schedule_endpoint_rejects_unknown_template():
    res = client.get("/api/rate-schedule", params={"template": "not_a_real_template"})
    assert res.status_code == 404


def test_templates_endpoint_lists_both_templates_with_fields():
    res = client.get("/api/templates")
    assert res.status_code == 200
    keys = {t["key"] for t in res.json()}
    assert keys == {"transport_levy", "utility_bill"}
    transport = next(t for t in res.json() if t["key"] == "transport_levy")
    assert any(f["name"] == "cargo_class" and f["is_dimension"] for f in transport["fields"])


def test_runs_endpoint_filters_by_template():
    all_transport = client.get("/api/runs", params={"template": "transport_levy"}).json()
    all_utility = client.get("/api/runs", params={"template": "utility_bill"}).json()
    assert len(all_transport) >= 40
    assert len(all_utility) >= 40
    assert all(r["template_key"] == "transport_levy" for r in all_transport)
    assert all(r["template_key"] == "utility_bill" for r in all_utility)
