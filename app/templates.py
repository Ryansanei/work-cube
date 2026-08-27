"""Loads a template's definition (its fields, and which of them play which
role in the calculator/extraction) from the database. Every other module
that needs to know "what fields does this use case have, and which one is
the amount / the date / a dimension / a quantity" goes through this — the
calculator, the extractor, the manifest parser, and the API all read the
same definition, so they can't silently drift from each other.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


class UnknownTemplateError(Exception):
    pass


@dataclass
class TemplateField:
    name: str
    label: str
    field_type: str
    required: bool
    is_dimension: bool
    is_quantity: bool
    is_amount: bool
    is_date: bool
    enum_options: list[dict] | None
    label_aliases: list[str]


@dataclass
class Template:
    id: int
    key: str
    name: str
    description: str
    authority_name: str
    document_label: str
    fields: list[TemplateField]

    @property
    def required_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    @property
    def dimension_field_names(self) -> list[str]:
        return [f.name for f in self.fields if f.is_dimension]

    @property
    def amount_field_name(self) -> str:
        return next(f.name for f in self.fields if f.is_amount)

    @property
    def date_field_name(self) -> str:
        return next(f.name for f in self.fields if f.is_date)

    def field(self, name: str) -> TemplateField | None:
        return next((f for f in self.fields if f.name == name), None)


def _row_to_field(row) -> TemplateField:
    return TemplateField(
        name=row.name, label=row.label, field_type=row.field_type, required=row.required,
        is_dimension=row.is_dimension, is_quantity=row.is_quantity,
        is_amount=row.is_amount, is_date=row.is_date,
        enum_options=row.enum_options, label_aliases=row.label_aliases or [],
    )


def load_template(db: Session, key: str) -> Template:
    row = db.execute(
        text("""
            SELECT id, key, name, description, authority_name, document_label
            FROM templates WHERE key = :key AND active
        """),
        {"key": key},
    ).fetchone()
    if row is None:
        raise UnknownTemplateError(f"no active template with key {key!r}")
    return _build_template(db, row)


def load_template_by_id(db: Session, template_id: int) -> Template:
    row = db.execute(
        text("""
            SELECT id, key, name, description, authority_name, document_label
            FROM templates WHERE id = :id
        """),
        {"id": template_id},
    ).fetchone()
    if row is None:
        raise UnknownTemplateError(f"no template with id {template_id!r}")
    return _build_template(db, row)


def _build_template(db: Session, row) -> Template:
    field_rows = db.execute(
        text("""
            SELECT name, label, field_type, required, is_dimension, is_quantity,
                   is_amount, is_date, enum_options, label_aliases
            FROM template_fields WHERE template_id = :tid ORDER BY sort_order, id
        """),
        {"tid": row.id},
    ).fetchall()
    return Template(
        id=row.id, key=row.key, name=row.name, description=row.description,
        authority_name=row.authority_name, document_label=row.document_label,
        fields=[_row_to_field(r) for r in field_rows],
    )


def list_templates(db: Session) -> list[Template]:
    rows = db.execute(text("SELECT key FROM templates WHERE active ORDER BY id")).fetchall()
    return [load_template(db, r.key) for r in rows]
