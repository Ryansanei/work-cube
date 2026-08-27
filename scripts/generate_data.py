"""Generates a synthetic rate schedule and sample documents for a template.
One generic engine, driven entirely by a template's field definitions plus
a per-template "rate recipe" (the actual dollar figures, which are business
knowledge no engine can invent) — used for both shipped templates
(transport_levy, utility_bill) as the proof that the engine is genuinely
template-agnostic, not just architecturally claimed to be.

Two schedule versions tell a deliberate story per template (mirroring the
injected-story approach from the other two projects, rather than pure
random noise): a rate increase plus a new exemption introduced in v2 — this
is what gives the temporal-mismatch and exemption-error document categories
something real to be about, not an arbitrary label.
"""

import json
import random
from decimal import Decimal
from pathlib import Path

from app.calculator.calculator import ExemptionRule, RateRule, compute_charge
from app.template_defs import ALL_TEMPLATES, TemplateDef

DATA_DIR = Path(__file__).parent.parent / "data"
SEED = 20260818
V2_INCREASE = Decimal("1.08")


def _rule_lookup(schedule: dict, version_key: str, dimension_values: dict) -> RateRule:
    version_index = {v["key"]: i + 1 for i, v in enumerate(schedule["versions"])}
    row = next(
        r for r in schedule["rate_rules"][version_key]
        if r["dimension_key"] == dimension_values
    )
    return RateRule(
        id=0, schedule_version_id=version_index[version_key],
        dimension_key=row["dimension_key"], base_fee=Decimal(row["base_fee"]),
        rate_terms=row["rate_terms"], surcharge_pct=Decimal(row["surcharge_pct"]),
    )


def _exemption_lookup(schedule: dict, version_key: str, code: str) -> ExemptionRule | None:
    for row in schedule["exemption_rules"][version_key]:
        if row["exemption_code"] == code:
            return ExemptionRule(
                exemption_code=row["exemption_code"], discount_pct=Decimal(row["discount_pct"]),
                applies_to=row["applies_to"],
            )
    return None


def build_rate_schedule(
    template: TemplateDef, authority_name: str, boundary_date: str,
    rate_recipe: dict[tuple, dict], universal_exemption: dict, dimension_exemption: dict,
    v2_only_exemption: dict,
) -> dict:
    """rate_recipe: {dimension_value_tuple (in template.dimension_field_names
    order): {"base_fee": str, "rate_terms": [{"field","rate","free_threshold"}],
    "surcharge_pct": str}}"""
    versions = [
        {"key": "v1", "effective_from": "2024-01-01", "effective_to": _day_before(boundary_date),
         "published_by": authority_name, "notes": "Original rate schedule."},
        {"key": "v2", "effective_from": boundary_date, "effective_to": None,
         "published_by": authority_name,
         "notes": f"Revision effective {boundary_date} — ~8% increase, adds {v2_only_exemption['exemption_code']}."},
    ]

    dim_names = template.dimension_field_names
    rate_rules = {"v1": [], "v2": []}
    for dim_values, recipe in rate_recipe.items():
        dimension_key = dict(zip(dim_names, dim_values, strict=True))
        v1_terms = recipe["rate_terms"]
        v2_terms = [
            {**t, "rate": str((Decimal(t["rate"]) * V2_INCREASE).quantize(Decimal("0.0001")))}
            for t in v1_terms
        ]
        rate_rules["v1"].append({
            "dimension_key": dimension_key, "base_fee": recipe["base_fee"],
            "rate_terms": v1_terms, "surcharge_pct": recipe["surcharge_pct"],
        })
        rate_rules["v2"].append({
            "dimension_key": dimension_key,
            "base_fee": str((Decimal(recipe["base_fee"]) * V2_INCREASE).quantize(Decimal("0.01"))),
            "rate_terms": v2_terms, "surcharge_pct": recipe["surcharge_pct"],
        })

    exemption_rules = {"v1": [universal_exemption, dimension_exemption]}
    exemption_rules["v2"] = exemption_rules["v1"] + [v2_only_exemption]

    return {"versions": versions, "rate_rules": rate_rules, "exemption_rules": exemption_rules}


def _day_before(iso_date: str) -> str:
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso_date.split("-"))
    return (date(y, m, d) - timedelta(days=1)).isoformat()


def build_documents(
    template: TemplateDef, schedule: dict, rng: random.Random, boundary_date: str,
    random_record: "callable",
) -> list[dict]:
    """random_record(rng, date_str=None) -> dict of {dim_field: value, quantity_field: Decimal, ..., 'date_str': ...}
    covering every dimension and quantity field this template defines."""
    documents = []
    doc_id = 1
    dim_names = template.dimension_field_names
    date_field = template.date_field_name
    amount_field = template.amount_field_name

    def new_doc(record: dict, exemption_code, authority_claimed_amount, category, correct_total, notes,
                omit_field=None) -> dict:
        nonlocal doc_id
        doc = {
            "id": doc_id, "filename": f"doc_{doc_id:03d}.pdf", "language": "en",
            "exemption_code": exemption_code, amount_field: authority_claimed_amount,
            "ground_truth_category": category, "ground_truth_correct_total": correct_total, "notes": notes,
        }
        doc.update(record)
        if omit_field:
            doc["omit_field"] = omit_field
        doc_id += 1
        documents.append(doc)
        return doc

    def version_key_for(date_str: str) -> str:
        return "v1" if date_str < boundary_date else "v2"

    def dim_key_of(record: dict) -> dict:
        return {f: record[f] for f in dim_names}

    # --- clean_match: 15 ---
    for _ in range(15):
        record = random_record(rng)
        date_str = record[date_field]
        version_key = version_key_for(date_str)
        exemption_code = None
        if rng.random() < 0.35:
            candidates = [
                e["exemption_code"] for e in schedule["exemption_rules"][version_key]
                if _exemption_matches_dims(e, dim_key_of(record))
            ]
            if candidates:
                exemption_code = rng.choice(candidates)
        rule = _rule_lookup(schedule, version_key, dim_key_of(record))
        exemption = _exemption_lookup(schedule, version_key, exemption_code) if exemption_code else None
        result = compute_charge(rule, {**record, date_field: date_str}, exemption)
        new_doc(record, exemption_code, str(result.total), "clean_match", str(result.total),
                "Authority's figure matches an independent recomputation exactly.")

    # --- classification_error: 6 (wrong dimension value used) ---
    for _ in range(6):
        record = random_record(rng)
        date_str = record[date_field]
        version_key = version_key_for(date_str)
        wrong_dim_field = rng.choice(dim_names)
        wrong_options = [o["value"] for o in template.field(wrong_dim_field).enum_options
                         if o["value"] != record[wrong_dim_field]]
        wrong_value = rng.choice(wrong_options)
        wrong_dims = {**dim_key_of(record), wrong_dim_field: wrong_value}
        correct_rule = _rule_lookup(schedule, version_key, dim_key_of(record))
        wrong_rule = _rule_lookup(schedule, version_key, wrong_dims)
        correct = compute_charge(correct_rule, record, None)
        claimed = compute_charge(wrong_rule, {**record, **wrong_dims}, None)
        new_doc(record, None, str(claimed.total), "classification_error", str(correct.total),
                f"Document correctly declares {wrong_dim_field}={record[wrong_dim_field]!r}, but the "
                f"authority's figure was computed using {wrong_dim_field}={wrong_value!r} instead.")

    # --- exemption_error: 6 ---
    for _ in range(6):
        record = random_record(rng)
        date_str = record[date_field]
        version_key = version_key_for(date_str)
        candidates = [e["exemption_code"] for e in schedule["exemption_rules"][version_key]
                     if _exemption_matches_dims(e, dim_key_of(record))]
        exemption_code = rng.choice(candidates)
        rule = _rule_lookup(schedule, version_key, dim_key_of(record))
        exemption = _exemption_lookup(schedule, version_key, exemption_code)
        correct = compute_charge(rule, record, exemption)
        claimed = compute_charge(rule, record, None)
        new_doc(record, exemption_code, str(claimed.total), "exemption_error", str(correct.total),
                f"Document declares valid exemption {exemption_code!r}, but the authority's figure "
                f"does not reflect the discount.")

    # --- temporal_mismatch: 6 ---
    for i in range(6):
        record = random_record(rng)
        if i % 2 == 0:
            y, m, d = (int(x) for x in boundary_date.split("-"))
            date_str, correct_key, wrong_key = f"{y}-{m:02d}-{min(d + 15, 28):02d}", "v2", "v1"
        else:
            prev = _day_before(boundary_date)
            y, m, _ = (int(x) for x in prev.split("-"))
            date_str, correct_key, wrong_key = f"{y}-{m:02d}-10", "v1", "v2"
        record = {**record, date_field: date_str}
        correct_rule = _rule_lookup(schedule, correct_key, dim_key_of(record))
        wrong_rule = _rule_lookup(schedule, wrong_key, dim_key_of(record))
        correct = compute_charge(correct_rule, record, None)
        claimed = compute_charge(wrong_rule, record, None)
        new_doc(record, None, str(claimed.total), "temporal_mismatch", str(correct.total),
                f"Record dated {date_str} falls under schedule {correct_key}, but the authority's "
                f"figure was computed using schedule {wrong_key}'s rates.")

    # --- arithmetic_only: 4 ---
    for _ in range(4):
        record = random_record(rng)
        date_str = record[date_field]
        version_key = version_key_for(date_str)
        rule = _rule_lookup(schedule, version_key, dim_key_of(record))
        correct = compute_charge(rule, record, None)
        delta = Decimal(rng.choice([-1, 1]) * rng.randint(5, 40))
        claimed_total = (correct.total + delta).quantize(Decimal("0.01"))
        new_doc(record, None, str(claimed_total), "arithmetic_only", str(correct.total),
                "Classification, exemption, and schedule version are all correct — the authority's "
                "figure is simply off by a flat amount.")

    # --- needs_review: 3 (a required field is missing from the record) ---
    omit_candidates = template.quantity_field_names + dim_names[:1]
    for i in range(3):
        missing = omit_candidates[i % len(omit_candidates)]
        record = random_record(rng)
        new_doc(record, None, None, "needs_review", None,
                f"Document does not state {missing} — cannot be recomputed without it.",
                omit_field=missing)

    rng.shuffle(documents)
    for i, doc in enumerate(documents, start=1):
        doc["id"] = i
        doc["filename"] = f"doc_{i:03d}.pdf"
    return documents


def _exemption_matches_dims(exemption_row: dict, dim_values: dict) -> bool:
    applies_to = exemption_row.get("applies_to")
    if not applies_to:
        return True
    return all(dim_values.get(k) == v for k, v in applies_to.items())


# ============================== transport_levy recipe ==============================

def _transport_levy_recipe():
    from app.template_defs import TRANSPORT_LEVY as T
    cargo_classes = [o["value"] for o in T.field("cargo_class").enum_options]
    vehicle_classes = [o["value"] for o in T.field("vehicle_class").enum_options]
    weight_range = {"light_truck": (500, 4000), "heavy_truck": (2000, 12000), "articulated": (5000, 25000)}
    v1_rates = {
        ("general_freight", "light_truck"): ("40.00", "0.35", "0.008", "2000", "0"),
        ("general_freight", "heavy_truck"): ("65.00", "0.55", "0.006", "5000", "0"),
        ("general_freight", "articulated"): ("95.00", "0.75", "0.005", "10000", "0"),
        ("perishable", "light_truck"): ("45.00", "0.40", "0.009", "1500", "0"),
        ("perishable", "heavy_truck"): ("72.00", "0.60", "0.007", "4000", "0"),
        ("perishable", "articulated"): ("105.00", "0.80", "0.006", "9000", "0"),
        ("hazardous", "light_truck"): ("80.00", "0.70", "0.012", "1000", "12.5"),
        ("hazardous", "heavy_truck"): ("130.00", "0.95", "0.010", "3000", "12.5"),
        ("hazardous", "articulated"): ("190.00", "1.20", "0.009", "8000", "15.0"),
        ("oversized", "light_truck"): ("70.00", "0.60", "0.010", "1500", "5.0"),
        ("oversized", "heavy_truck"): ("115.00", "0.85", "0.008", "4000", "5.0"),
        ("oversized", "articulated"): ("160.00", "1.05", "0.007", "9000", "7.5"),
    }
    rate_recipe = {
        (cargo, vehicle): {
            "base_fee": base, "surcharge_pct": surcharge,
            "rate_terms": [
                {"field": "distance_km", "rate": per_km, "free_threshold": "0"},
                {"field": "weight_kg", "rate": per_kg, "free_threshold": threshold},
            ],
        }
        for (cargo, vehicle), (base, per_km, per_kg, threshold, surcharge) in v1_rates.items()
    }

    def random_record(rng, date_str=None):
        cargo = rng.choice(cargo_classes)
        vehicle = rng.choice(vehicle_classes)
        lo, hi = weight_range[vehicle]
        if date_str is None:
            year = rng.choice([2024, 2025, 2026])
            date_str = f"{year:04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        return {
            "cargo_class": cargo, "vehicle_class": vehicle,
            "weight_kg": str(rng.randint(lo, hi)), "distance_km": str(rng.randint(50, 800)),
            "shipment_date": date_str,
        }

    return dict(
        template=T, authority_name=T.authority_name, boundary_date="2025-07-01",
        rate_recipe=rate_recipe,
        universal_exemption={"exemption_code": "DIPLOMATIC", "applies_to": None, "discount_pct": "100",
                              "description": "Diplomatic cargo movements — full levy waiver, all cargo classes."},
        dimension_exemption={"exemption_code": "AGRI-SUBSIDY", "applies_to": {"cargo_class": "general_freight"},
                              "discount_pct": "25",
                              "description": "Agricultural subsidy program — general freight only."},
        v2_only_exemption={"exemption_code": "COLD-CHAIN", "applies_to": {"cargo_class": "perishable"},
                           "discount_pct": "15",
                           "description": "Cold-chain perishable transport incentive, introduced "
                                          "2025-07 — perishable cargo only."},
        random_record=random_record,
    )


# ============================== utility_bill recipe ==============================

def _utility_bill_recipe():
    from app.template_defs import UTILITY_BILL as U
    customer_classes = [o["value"] for o in U.field("customer_class").enum_options]
    kwh_range = {"residential": (150, 1200), "commercial": (1000, 8000), "industrial": (5000, 40000)}
    v1_rates = {
        "residential": ("12.00", "0.14", "50", "0"),
        "commercial": ("35.00", "0.11", "200", "0"),
        "industrial": ("120.00", "0.08", "1000", "3.0"),
    }
    rate_recipe = {
        (customer,): {
            "base_fee": base, "surcharge_pct": surcharge,
            "rate_terms": [{"field": "kwh_used", "rate": rate, "free_threshold": threshold}],
        }
        for customer, (base, rate, threshold, surcharge) in v1_rates.items()
    }

    def random_record(rng, date_str=None):
        customer = rng.choice(customer_classes)
        lo, hi = kwh_range[customer]
        if date_str is None:
            year = rng.choice([2024, 2025, 2026])
            date_str = f"{year:04d}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
        return {"customer_class": customer, "kwh_used": str(rng.randint(lo, hi)), "billing_period_end": date_str}

    return dict(
        template=U, authority_name=U.authority_name, boundary_date="2025-07-01",
        rate_recipe=rate_recipe,
        universal_exemption={"exemption_code": "LOW-INCOME", "applies_to": None, "discount_pct": "20",
                              "description": "Low-income household discount program, all customer classes."},
        dimension_exemption={
            "exemption_code": "COMMERCIAL-EFFICIENCY", "applies_to": {"customer_class": "commercial"},
            "discount_pct": "10",
            "description": "Verified energy-efficiency upgrade rebate — commercial accounts only.",
        },
        v2_only_exemption={"exemption_code": "SOLAR-CREDIT", "applies_to": {"customer_class": "residential"},
                           "discount_pct": "12",
                           "description": "Rooftop solar generation credit, introduced "
                                          "2025-07 — residential accounts only."},
        random_record=random_record,
    )


RECIPES = {"transport_levy": _transport_levy_recipe, "utility_bill": _utility_bill_recipe}


def main() -> None:
    for tmpl in ALL_TEMPLATES:
        recipe = RECIPES[tmpl.key]()
        rng = random.Random(SEED)
        schedule = build_rate_schedule(
            recipe["template"], recipe["authority_name"], recipe["boundary_date"],
            recipe["rate_recipe"], recipe["universal_exemption"], recipe["dimension_exemption"],
            recipe["v2_only_exemption"],
        )
        documents = build_documents(recipe["template"], schedule, rng, recipe["boundary_date"], recipe["random_record"])

        out_dir = DATA_DIR / tmpl.key
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "rate_schedule.json").write_text(json.dumps(schedule, indent=2))
        (out_dir / "documents_manifest.json").write_text(json.dumps(documents, indent=2))

        by_category = {}
        for doc in documents:
            by_category[doc["ground_truth_category"]] = by_category.get(doc["ground_truth_category"], 0) + 1
        print(f"[{tmpl.key}] wrote {len(documents)} documents: {by_category}")
        print(f"[{tmpl.key}] wrote rate schedule: {len(schedule['versions'])} versions")


if __name__ == "__main__":
    main()
