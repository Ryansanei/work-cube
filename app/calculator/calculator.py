"""Deterministic recomputation — the codified calculator that stands in for
LLM arithmetic, generic across templates (transport levy, utility bill,
whatever use case a template defines). The agent's job is only to decide
which inputs to feed this; it never computes the number itself.

The formula shape is fixed and deliberately not Turing-complete:
    total = (base_fee + sum(rate * max(0, quantity - free_threshold)))
            * (1 + surcharge_pct/100)
            * (1 - exemption_pct/100)   [if an exemption applies]
This covers per-unit-with-a-free-tier pricing generally — transport levies,
utility tiers, insurance premiums, shipping — without running arbitrary
code per template. `compute_charge` is pure (no DB access) so the synthetic
data generator can call it directly; `calculate` is the DB-backed wrapper
the running app uses.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

TWO_PLACES = Decimal("0.01")


class NoApplicableScheduleError(Exception):
    pass


class NoApplicableRateRuleError(Exception):
    pass


@dataclass
class RateRule:
    id: int
    schedule_version_id: int
    dimension_key: dict[str, str]
    base_fee: Decimal
    rate_terms: list[dict]  # [{"field": str, "rate": Decimal, "free_threshold": Decimal}, ...]
    surcharge_pct: Decimal


@dataclass
class ExemptionRule:
    exemption_code: str
    discount_pct: Decimal
    applies_to: dict[str, str] | None


@dataclass
class CalculationInput:
    template_id: int
    field_values: dict[str, str]  # all field values, keyed by template_fields.name, as strings
    calc_date: date
    exemption_code: str | None = None


@dataclass
class ChargeLine:
    field: str
    rate: Decimal
    quantity: Decimal
    free_threshold: Decimal
    amount: Decimal

    def to_dict(self) -> dict:
        return {
            "field": self.field, "rate": float(self.rate), "quantity": float(self.quantity),
            "free_threshold": float(self.free_threshold), "amount": float(self.amount),
        }


@dataclass
class CalculationResult:
    schedule_version_id: int
    rate_rule_id: int
    base_fee: Decimal
    charges: list[ChargeLine] = field(default_factory=list)
    surcharge_amount: Decimal = Decimal(0)
    subtotal: Decimal = Decimal(0)
    exemption_applied: str | None = None
    exemption_discount: Decimal = Decimal(0)
    total: Decimal = Decimal(0)

    def to_dict(self) -> dict:
        return {
            "schedule_version_id": self.schedule_version_id,
            "rate_rule_id": self.rate_rule_id,
            "base_fee": float(self.base_fee),
            "charges": [c.to_dict() for c in self.charges],
            "surcharge_amount": float(self.surcharge_amount),
            "subtotal": float(self.subtotal),
            "exemption_applied": self.exemption_applied,
            "exemption_discount": float(self.exemption_discount),
            "total": float(self.total),
        }


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _exemption_matches(exemption: ExemptionRule, field_values: dict[str, str]) -> bool:
    if not exemption.applies_to:
        return True
    return all(str(field_values.get(k)) == str(v) for k, v in exemption.applies_to.items())


def compute_charge(
    rule: RateRule,
    field_values: dict[str, str],
    exemption: ExemptionRule | None,
) -> CalculationResult:
    running = rule.base_fee
    charges = []
    for term in rule.rate_terms:
        rate = Decimal(str(term["rate"]))
        threshold = Decimal(str(term.get("free_threshold", 0)))
        quantity = Decimal(str(field_values[term["field"]]))
        billable = max(Decimal(0), quantity - threshold)
        amount = rate * billable
        charges.append(ChargeLine(field=term["field"], rate=rate, quantity=quantity,
                                   free_threshold=threshold, amount=_round(amount)))
        running += amount

    surcharge_amount = running * (rule.surcharge_pct / Decimal(100))
    subtotal = running + surcharge_amount

    exemption_applied = None
    exemption_discount = Decimal(0)
    if exemption is not None and _exemption_matches(exemption, field_values):
        exemption_applied = exemption.exemption_code
        exemption_discount = subtotal * (exemption.discount_pct / Decimal(100))

    total = subtotal - exemption_discount

    return CalculationResult(
        schedule_version_id=rule.schedule_version_id,
        rate_rule_id=rule.id,
        base_fee=_round(rule.base_fee),
        charges=charges,
        surcharge_amount=_round(surcharge_amount),
        subtotal=_round(subtotal),
        exemption_applied=exemption_applied,
        exemption_discount=_round(exemption_discount),
        total=_round(total),
    )


def find_schedule_version_id(db: Session, template_id: int, on_date: date) -> int:
    row = db.execute(
        text("""
            SELECT id FROM rate_schedule_versions
            WHERE template_id = :tid AND effective_from <= :d AND (effective_to IS NULL OR effective_to >= :d)
        """),
        {"tid": template_id, "d": on_date},
    ).fetchone()
    if row is None:
        raise NoApplicableScheduleError(f"no rate schedule version covers {on_date} for template {template_id}")
    return row.id


def _fetch_rate_rule(db: Session, schedule_version_id: int, dimension_values: dict[str, str]) -> RateRule:
    import json
    row = db.execute(
        text("""
            SELECT id, dimension_key, base_fee, rate_terms, surcharge_pct
            FROM rate_rules
            WHERE schedule_version_id = :sid AND dimension_key = CAST(:dk AS jsonb)
        """),
        {"sid": schedule_version_id, "dk": json.dumps(dimension_values, sort_keys=True)},
    ).fetchone()
    if row is None:
        raise NoApplicableRateRuleError(
            f"no rate rule for dimensions={dimension_values!r} in schedule version {schedule_version_id}"
        )
    return RateRule(
        id=row.id,
        schedule_version_id=schedule_version_id,
        dimension_key=row.dimension_key,
        base_fee=Decimal(str(row.base_fee)),
        rate_terms=row.rate_terms,
        surcharge_pct=Decimal(str(row.surcharge_pct)),
    )


def _fetch_exemption_rule(db: Session, schedule_version_id: int, exemption_code: str) -> ExemptionRule | None:
    row = db.execute(
        text("""
            SELECT exemption_code, discount_pct, applies_to
            FROM exemption_rules
            WHERE schedule_version_id = :sid AND exemption_code = :code
        """),
        {"sid": schedule_version_id, "code": exemption_code},
    ).fetchone()
    if row is None:
        return None
    return ExemptionRule(
        exemption_code=row.exemption_code,
        discount_pct=Decimal(str(row.discount_pct)),
        applies_to=row.applies_to,
    )


def calculate(db: Session, calc_input: CalculationInput, dimension_fields: list[str]) -> CalculationResult:
    """`dimension_fields` — the names of this template's is_dimension fields,
    in any order; used to build the dimension_key lookup from field_values."""
    schedule_version_id = find_schedule_version_id(db, calc_input.template_id, calc_input.calc_date)
    dimension_values = {f: calc_input.field_values[f] for f in dimension_fields}
    rule = _fetch_rate_rule(db, schedule_version_id, dimension_values)
    exemption = (
        _fetch_exemption_rule(db, schedule_version_id, calc_input.exemption_code)
        if calc_input.exemption_code
        else None
    )
    return compute_charge(rule=rule, field_values=calc_input.field_values, exemption=exemption)
