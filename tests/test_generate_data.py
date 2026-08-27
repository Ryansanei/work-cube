import random
from decimal import Decimal

from scripts.generate_data import RECIPES, build_documents, build_rate_schedule


def _generate(key: str):
    recipe = RECIPES[key]()
    rng = random.Random(20260818)
    schedule = build_rate_schedule(
        recipe["template"], recipe["authority_name"], recipe["boundary_date"],
        recipe["rate_recipe"], recipe["universal_exemption"], recipe["dimension_exemption"],
        recipe["v2_only_exemption"],
    )
    documents = build_documents(recipe["template"], schedule, rng, recipe["boundary_date"], recipe["random_record"])
    return schedule, documents


def test_generation_is_deterministic_for_a_fixed_seed():
    _, docs_a = _generate("transport_levy")
    _, docs_b = _generate("transport_levy")
    assert docs_a == docs_b


def test_v2_rates_are_roughly_eight_percent_above_v1():
    schedule, _ = _generate("transport_levy")
    v1 = {tuple(sorted(r["dimension_key"].items())): r for r in schedule["rate_rules"]["v1"]}
    v2 = {tuple(sorted(r["dimension_key"].items())): r for r in schedule["rate_rules"]["v2"]}
    for key, v1_row in v1.items():
        ratio = Decimal(v2[key]["base_fee"]) / Decimal(v1_row["base_fee"])
        assert Decimal("1.07") < ratio < Decimal("1.09")


def test_cold_chain_exemption_only_exists_in_v2():
    schedule, _ = _generate("transport_levy")
    v1_codes = {e["exemption_code"] for e in schedule["exemption_rules"]["v1"]}
    v2_codes = {e["exemption_code"] for e in schedule["exemption_rules"]["v2"]}
    assert "COLD-CHAIN" not in v1_codes
    assert "COLD-CHAIN" in v2_codes


def test_document_categories_have_expected_counts():
    _, docs = _generate("transport_levy")
    by_category = {}
    for doc in docs:
        by_category[doc["ground_truth_category"]] = by_category.get(doc["ground_truth_category"], 0) + 1
    assert by_category == {
        "clean_match": 15, "classification_error": 6, "exemption_error": 6,
        "temporal_mismatch": 6, "arithmetic_only": 4, "needs_review": 3,
    }


def test_classification_error_docs_declare_correct_class_but_claim_wrong_amount():
    _, docs = _generate("transport_levy")
    error_docs = [d for d in docs if d["ground_truth_category"] == "classification_error"]
    assert len(error_docs) == 6
    for doc in error_docs:
        assert doc["authority_claimed_amount"] != doc["ground_truth_correct_total"]


def test_needs_review_docs_have_no_claimed_amount():
    _, docs = _generate("transport_levy")
    for doc in docs:
        if doc["ground_truth_category"] == "needs_review":
            assert doc["authority_claimed_amount"] is None
            assert "omit_field" in doc


def test_utility_bill_template_also_generates_correctly():
    """The second template proves the generator is genuinely generic, not
    just parameterized in name — different dimension count (1, not 2),
    different rate-term count (1, not 2), different exemption codes."""
    schedule, docs = _generate("utility_bill")
    assert len(docs) == 40
    by_category = {}
    for doc in docs:
        by_category[doc["ground_truth_category"]] = by_category.get(doc["ground_truth_category"], 0) + 1
    assert by_category == {
        "clean_match": 15, "classification_error": 6, "exemption_error": 6,
        "temporal_mismatch": 6, "arithmetic_only": 4, "needs_review": 3,
    }
    v2_codes = {e["exemption_code"] for e in schedule["exemption_rules"]["v2"]}
    assert "SOLAR-CREDIT" in v2_codes
    assert all(len(r["rate_terms"]) == 1 for r in schedule["rate_rules"]["v1"])
