"""Phase 1 acceptance eval: checks the deterministic pipeline's output
against each template's synthetic data generator's ground-truth manifest.

This is deliberately not shaped like Support Copilot's or Cloud Cost
Advisor's eval harnesses — Phase 1 has no LLM in its loop (extraction is
label-matching over Docling's structured output, recomputation is a plain
calculator), so there is no probabilistic routing or citation-grounding
question to grade. What's checked here is straightforward correctness
against known-correct synthetic data, and — unlike the other two projects
— it can actually be run to completion and its results committed, since
nothing here depends on a paid or access-gated model call. A comparable
eval for the Phase 2 investigation graph (once built) will look more like
the other two projects', because Phase 2 does put an LLM in the loop.

Runs against every template in app/template_defs.py, not just
transport_levy — the whole point of the second template was to prove the
engine generalizes, and an eval harness that only ever checked one template
wouldn't actually prove that.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal
from app.template_defs import ALL_TEMPLATES, TemplateDef
from app.templates import load_template

ROOT = Path(__file__).parent.parent
RESULTS_DIR = ROOT / "eval" / "results"
RUNS_DIR = RESULTS_DIR / "runs"

EXPECTED_STATUS_BY_CATEGORY = {
    "clean_match": "clean_match",
    "classification_error": "discrepancy_found",
    "exemption_error": "discrepancy_found",
    "temporal_mismatch": "discrepancy_found",
    "arithmetic_only": "discrepancy_found",
    "needs_review": "needs_review",
}


def load_manifest(template_key: str) -> dict[str, dict]:
    docs = json.loads((ROOT / "data" / template_key / "documents_manifest.json").read_text())
    return {d["filename"]: d for d in docs}


def load_pipeline_output(db, template_id: int) -> dict[str, dict]:
    # Filtered to source_type='synthetic_seed' deliberately — the app now
    # accepts real uploads too (PDF and Excel manifest), which coexist in
    # the same tables as the golden-set seed data. Without this filter, any
    # real upload would break the manifest/DB set-equality check below.
    rows = db.execute(text("""
        SELECT ad.filename, ad.extracted_fields, rr.status, rr.recomputed_amount
        FROM authority_documents ad
        JOIN reconciliation_runs rr ON rr.document_id = ad.id
        WHERE ad.source_type = 'synthetic_seed' AND ad.template_id = :tid
    """), {"tid": template_id}).mappings().fetchall()
    return {row["filename"]: dict(row) for row in rows}


def evaluate_template(template_def: TemplateDef) -> list[dict]:
    manifest = load_manifest(template_def.key)
    with SessionLocal() as db:
        template = load_template(db, template_def.key)
        outputs = load_pipeline_output(db, template.id)

    if set(manifest) != set(outputs):
        missing = set(manifest) - set(outputs)
        raise RuntimeError(
            f"[{template_def.key}] pipeline output is missing {len(missing)} document(s) — "
            f"run `make ingest` then process pending documents before evaluating."
        )

    # Dimension + quantity + date fields only — excludes the amount field
    # (compared separately, see amount_correct) and exemption_code (optional,
    # None-heavy, and not a value the extractor is meant to be graded on).
    comparable_fields = [f.name for f in template.fields if f.is_dimension or f.is_quantity or f.is_date]
    per_document = []
    for filename, doc in manifest.items():
        output = outputs[filename]
        expected_status = EXPECTED_STATUS_BY_CATEGORY[doc["ground_truth_category"]]
        status_correct = output["status"] == expected_status

        amount_correct = None
        if doc["ground_truth_correct_total"] is not None and output["recomputed_amount"] is not None:
            amount_correct = abs(
                float(doc["ground_truth_correct_total"]) - float(output["recomputed_amount"])
            ) <= 0.01

        raw_fields = output["extracted_fields"]
        extracted = json.loads(raw_fields) if isinstance(raw_fields, str) else (raw_fields or {})
        omit = doc.get("omit_field")
        field_results = {}
        for field in comparable_fields:
            if field == omit or field not in doc:
                continue
            expected_value = str(doc[field])
            actual = extracted.get(field)
            actual_value = actual["value"] if isinstance(actual, dict) else actual
            field_results[field] = actual_value == expected_value
        field_accuracy = sum(field_results.values()) / len(field_results) if field_results else None

        per_document.append({
            "template": template_def.key,
            "filename": filename,
            "ground_truth_category": doc["ground_truth_category"],
            "expected_status": expected_status,
            "actual_status": output["status"],
            "status_correct": status_correct,
            "amount_correct": amount_correct,
            "field_accuracy": field_accuracy,
            "field_results": field_results,
        })
    return per_document


def summarize(per_document: list[dict]) -> dict:
    n = len(per_document)
    status_accuracy = sum(d["status_correct"] for d in per_document) / n
    amount_checks = [d["amount_correct"] for d in per_document if d["amount_correct"] is not None]
    amount_accuracy = sum(amount_checks) / len(amount_checks) if amount_checks else None
    field_checks = [d["field_accuracy"] for d in per_document if d["field_accuracy"] is not None]
    mean_field_accuracy = sum(field_checks) / len(field_checks) if field_checks else None

    hitl_docs = [d for d in per_document if d["ground_truth_category"] == "needs_review"]
    hitl_accuracy = (
        sum(d["actual_status"] == "needs_review" for d in hitl_docs) / len(hitl_docs)
        if hitl_docs else None
    )

    by_category = {}
    for d in per_document:
        cat = d["ground_truth_category"]
        by_category.setdefault(cat, {"n": 0, "status_correct": 0})
        by_category[cat]["n"] += 1
        by_category[cat]["status_correct"] += int(d["status_correct"])

    return {
        "n_documents": n,
        "status_accuracy": round(status_accuracy, 4),
        "amount_accuracy": round(amount_accuracy, 4) if amount_accuracy is not None else None,
        "mean_field_extraction_accuracy": (
            round(mean_field_accuracy, 4) if mean_field_accuracy is not None else None
        ),
        "hitl_routing_accuracy": round(hitl_accuracy, 4) if hitl_accuracy is not None else None,
        "by_category": by_category,
    }


def evaluate() -> dict:
    per_document_all = []
    per_template = {}
    for template_def in ALL_TEMPLATES:
        docs = evaluate_template(template_def)
        per_document_all.extend(docs)
        per_template[template_def.key] = summarize(docs)

    overall = summarize(per_document_all)
    return {
        "run_at": datetime.now(UTC).isoformat(),
        **overall,
        "per_template": per_template,
        "per_document": per_document_all,
    }


def main() -> None:
    result = evaluate()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(result, indent=2))

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (RUNS_DIR / f"{timestamp}.json").write_text(json.dumps(result, indent=2))

    summary = {k: v for k, v in result.items() if k != "per_document"}
    with open(RESULTS_DIR / "history.jsonl", "a") as f:
        f.write(json.dumps(summary) + "\n")

    print(f"Overall status accuracy:        {result['status_accuracy']:.1%} ({result['n_documents']} documents)")
    print(f"Overall amount accuracy:        {result['amount_accuracy']:.1%}")
    print(f"Overall field extraction acc.:  {result['mean_field_extraction_accuracy']:.1%}")
    print(f"Overall HITL routing accuracy:  {result['hitl_routing_accuracy']:.1%}")
    for key, summ in result["per_template"].items():
        print(f"\n[{key}] status={summ['status_accuracy']:.1%} amount={summ['amount_accuracy']:.1%} "
              f"fields={summ['mean_field_extraction_accuracy']:.1%} hitl={summ['hitl_routing_accuracy']:.1%}")
        print(f"[{key}] by category: {json.dumps(summ['by_category'])}")
    print(f"\nWrote {RESULTS_DIR / 'latest.json'} and {RUNS_DIR / f'{timestamp}.json'}")
    print("Commit eval/results/runs/*.json and the history.jsonl line for this to count as a real result.")


if __name__ == "__main__":
    main()
