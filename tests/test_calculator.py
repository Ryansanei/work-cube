from datetime import date
from decimal import Decimal

import pytest

from app.calculator.calculator import (
    CalculationInput,
    ExemptionRule,
    NoApplicableScheduleError,
    RateRule,
    calculate,
    compute_charge,
    find_schedule_version_id,
)


def make_rule(**overrides) -> RateRule:
    defaults = dict(
        id=1, schedule_version_id=1, dimension_key={"cargo_class": "general_freight"},
        base_fee=Decimal("40.00"),
        rate_terms=[
            {"field": "distance_km", "rate": Decimal("0.35"), "free_threshold": Decimal("0")},
            {"field": "weight_kg", "rate": Decimal("0.008"), "free_threshold": Decimal("2000")},
        ],
        surcharge_pct=Decimal("0"),
    )
    defaults.update(overrides)
    return RateRule(**defaults)


def test_compute_charge_weight_under_threshold_has_no_weight_charge():
    rule = make_rule()
    result = compute_charge(rule, {"distance_km": "100", "weight_kg": "1500"}, None)
    weight_charge = next(c for c in result.charges if c.field == "weight_kg")
    assert weight_charge.amount == Decimal("0.00")
    assert result.total == Decimal("75.00")  # 40 base + 100*0.35


def test_compute_charge_weight_over_threshold_is_charged_on_excess_only():
    rule = make_rule()
    result = compute_charge(rule, {"distance_km": "200", "weight_kg": "3500"}, None)
    # base 40 + 200*0.35=70 + (3500-2000)*0.008=12 -> 122
    assert result.subtotal == Decimal("122.00")
    assert result.total == Decimal("122.00")


def test_compute_charge_surcharge_applies_to_pre_exemption_subtotal():
    rule = make_rule(surcharge_pct=Decimal("12.5"))
    result = compute_charge(rule, {"distance_km": "100", "weight_kg": "1000"}, None)
    # base 40 + 100*0.35=35 -> 75, surcharge 12.5% of 75 = 9.375 -> 84.375 -> 84.38
    assert result.surcharge_amount == Decimal("9.38")
    assert result.total == Decimal("84.38")


def test_compute_charge_exemption_reduces_full_subtotal_including_surcharge():
    rule = make_rule(surcharge_pct=Decimal("10"))
    exemption = ExemptionRule(exemption_code="TEST", discount_pct=Decimal("50"), applies_to=None)
    result = compute_charge(rule, {"distance_km": "100", "weight_kg": "1000"}, exemption)
    # subtotal with surcharge = 75 * 1.10 = 82.50; 50% off -> 41.25
    assert result.subtotal == Decimal("82.50")
    assert result.exemption_discount == Decimal("41.25")
    assert result.total == Decimal("41.25")


def test_compute_charge_exemption_ignored_when_dimension_does_not_match():
    rule = make_rule(dimension_key={"cargo_class": "perishable"})
    exemption = ExemptionRule(
        exemption_code="AGRI-SUBSIDY", discount_pct=Decimal("25"),
        applies_to={"cargo_class": "general_freight"},
    )
    result = compute_charge(rule, {"distance_km": "100", "weight_kg": "1500"}, exemption)
    assert result.exemption_applied is None
    assert result.exemption_discount == Decimal("0.00")


def test_compute_charge_single_rate_term_works():
    rule = make_rule(dimension_key={"customer_class": "residential"}, base_fee=Decimal("12.00"),
                      rate_terms=[{"field": "kwh_used", "rate": Decimal("0.14"), "free_threshold": Decimal("50")}])
    result = compute_charge(rule, {"kwh_used": "620"}, None)
    # base 12 + (620-50)*0.14 = 12 + 79.80 = 91.80
    assert result.total == Decimal("91.80")
    assert len(result.charges) == 1


def test_find_schedule_version_id_real_db(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    version_id = find_schedule_version_id(db, tmpl.id, date(2024, 6, 1))
    assert version_id is not None


def test_find_schedule_version_id_raises_outside_any_version(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    with pytest.raises(NoApplicableScheduleError):
        find_schedule_version_id(db, tmpl.id, date(1999, 1, 1))


def test_calculate_matches_real_ingested_rate_rule(db):
    from app.templates import load_template
    tmpl = load_template(db, "transport_levy")
    result = calculate(db, CalculationInput(
        template_id=tmpl.id,
        field_values={"cargo_class": "general_freight", "vehicle_class": "light_truck",
                      "weight_kg": "2500", "distance_km": "100"},
        calc_date=date(2024, 6, 1),
    ), tmpl.dimension_field_names)
    # v1 general_freight/light_truck: base 40, 0.35/km, 0.008/kg over 2000
    # 40 + 100*0.35=35 + 500*0.008=4 -> 79.00
    assert result.total == Decimal("79.00")


def test_calculate_utility_bill_template(db):
    from app.templates import load_template
    tmpl = load_template(db, "utility_bill")
    result = calculate(db, CalculationInput(
        template_id=tmpl.id,
        field_values={"customer_class": "residential", "kwh_used": "620"},
        calc_date=date(2024, 6, 1),
    ), tmpl.dimension_field_names)
    # v1 residential: base 12.00, 0.14/kwh over 50 free
    # 12 + (620-50)*0.14 = 12 + 79.80 = 91.80
    assert result.total == Decimal("91.80")
