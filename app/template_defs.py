"""Static definitions of the templates shipped with this repo — used both
to seed the `templates`/`template_fields` tables (scripts/ingest.py) and by
the synthetic data generator, which needs to know field names, dimensions,
and enum values before any database exists yet. Adding a new template for a
new use case means adding one of these, not writing a new calculator or
extractor.
"""

from dataclasses import dataclass, field


@dataclass
class FieldDef:
    name: str
    label: str
    field_type: str  # text | number | date | enum
    required: bool = True
    is_dimension: bool = False
    is_quantity: bool = False
    is_amount: bool = False
    is_date: bool = False
    enum_options: list[dict] | None = None
    label_aliases: list[str] = field(default_factory=list)


@dataclass
class TemplateDef:
    key: str
    name: str
    description: str
    authority_name: str
    document_label: str
    fields: list[FieldDef]

    def field(self, name: str) -> FieldDef:
        return next(f for f in self.fields if f.name == name)

    @property
    def dimension_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.is_dimension]

    @property
    def quantity_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.is_quantity]

    @property
    def amount_field_name(self) -> str:
        return next(f.name for f in self.fields if f.is_amount)

    @property
    def date_field_name(self) -> str:
        return next(f.name for f in self.fields if f.is_date)


TRANSPORT_LEVY = TemplateDef(
    key="transport_levy",
    name="Transport Levy Reconciliation",
    description="Verify a transport levy assessment against the effective rate schedule.",
    authority_name="National Transport Levy Authority (NTLA), Veridia",
    document_label="Transport Levy Assessment",
    fields=[
        FieldDef("shipment_date", "Shipment Date", "date", is_date=True,
                  label_aliases=["Shipment Date"]),
        FieldDef("cargo_class", "Cargo Classification", "enum", is_dimension=True,
                  enum_options=[
                      {"value": "general_freight", "label": "General Freight"},
                      {"value": "perishable", "label": "Perishable Goods"},
                      {"value": "hazardous", "label": "Hazardous Materials"},
                      {"value": "oversized", "label": "Oversized Loads"},
                  ],
                  label_aliases=["Cargo Classification"]),
        FieldDef("vehicle_class", "Vehicle Classification", "enum", is_dimension=True,
                  enum_options=[
                      {"value": "light_truck", "label": "Light Truck"},
                      {"value": "heavy_truck", "label": "Heavy Truck"},
                      {"value": "articulated", "label": "Articulated Vehicle"},
                  ],
                  label_aliases=["Vehicle Classification"]),
        FieldDef("weight_kg", "Declared Weight (kg)", "number", is_quantity=True,
                  label_aliases=["Declared Weight (kg)"]),
        FieldDef("distance_km", "Declared Distance (km)", "number", is_quantity=True,
                  label_aliases=["Declared Distance (km)"]),
        FieldDef("exemption_code", "Exemption Code Claimed", "text", required=False,
                  label_aliases=["Exemption Code Claimed"]),
        FieldDef("authority_claimed_amount", "Total Levy Assessed", "number", is_amount=True,
                  label_aliases=["Total Levy Assessed"]),
    ],
)

UTILITY_BILL = TemplateDef(
    key="utility_bill",
    name="Utility Bill Reconciliation",
    description="Verify a utility bill against the effective municipal rate schedule.",
    authority_name="Veridia Municipal Power & Water Authority",
    document_label="Utility Bill",
    fields=[
        FieldDef("billing_period_end", "Billing Period End", "date", is_date=True,
                  label_aliases=["Billing Period End"]),
        FieldDef("customer_class", "Customer Class", "enum", is_dimension=True,
                  enum_options=[
                      {"value": "residential", "label": "Residential"},
                      {"value": "commercial", "label": "Commercial"},
                      {"value": "industrial", "label": "Industrial"},
                  ],
                  label_aliases=["Customer Class"]),
        FieldDef("kwh_used", "Energy Used (kWh)", "number", is_quantity=True,
                  label_aliases=["Energy Used (kWh)"]),
        FieldDef("exemption_code", "Discount Code Claimed", "text", required=False,
                  label_aliases=["Discount Code Claimed"]),
        FieldDef("authority_claimed_amount", "Total Amount Billed", "number", is_amount=True,
                  label_aliases=["Total Amount Billed"]),
    ],
)

ALL_TEMPLATES = [TRANSPORT_LEVY, UTILITY_BILL]
