"""Loads generated data into Postgres: schema, templates, rate schedules,
regulation chunks (embedded), and rendered authority documents — for every
template in app/template_defs.py. Idempotent — safe to re-run; clears and
reloads per-template tables each time, but skips re-rendering documents that
already exist on disk.
"""

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal, engine
from app.embeddings import embed_texts
from app.ingestion.render import render_document
from app.rag.chunking import chunk_markdown
from app.template_defs import ALL_TEMPLATES, TemplateDef

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DOCUMENTS_DIR = DATA_DIR / "documents" / "generated"
SCHEMA_PATH = ROOT / "db" / "schema.sql"

# (filename, valid_from, valid_to) per template key.
REGULATION_METADATA = {
    "transport_levy": [
        ("levy_formula_overview.md", "2024-01-01", None),
        ("cargo_classification_guide.md", "2024-01-01", None),
        ("vehicle_classification_guide.md", "2024-01-01", None),
        ("levy_rates_2024.md", "2024-01-01", "2025-06-30"),
        ("levy_rates_2025_revision.md", "2025-07-01", None),
        ("exemptions.md", "2024-01-01", None),
        ("exemptions_cold_chain_2025.md", "2025-07-01", None),
    ],
    "utility_bill": [
        ("rate_overview.md", "2024-01-01", None),
        ("customer_classification_guide.md", "2024-01-01", None),
        ("rates_2025_revision.md", "2025-07-01", None),
    ],
}


def apply_schema():
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_PATH.read_text()))


def seed_templates() -> dict[str, int]:
    """Inserts/updates every template + its fields. Returns {key: template_id}."""
    template_ids = {}
    with SessionLocal() as db:
        for tmpl in ALL_TEMPLATES:
            row = db.execute(
                text("""
                    INSERT INTO templates (key, name, description, authority_name, document_label)
                    VALUES (:key, :name, :desc, :authority, :doclabel)
                    ON CONFLICT (key) DO UPDATE
                    SET name = EXCLUDED.name, description = EXCLUDED.description,
                        authority_name = EXCLUDED.authority_name, document_label = EXCLUDED.document_label
                    RETURNING id
                """),
                {"key": tmpl.key, "name": tmpl.name, "desc": tmpl.description,
                 "authority": tmpl.authority_name, "doclabel": tmpl.document_label},
            ).scalar_one()
            template_ids[tmpl.key] = row

            db.execute(text("DELETE FROM template_fields WHERE template_id = :tid"), {"tid": row})
            for i, f in enumerate(tmpl.fields):
                db.execute(
                    text("""
                        INSERT INTO template_fields
                            (template_id, name, label, field_type, required, is_dimension, is_quantity,
                             is_amount, is_date, enum_options, label_aliases, sort_order)
                        VALUES (:tid, :name, :label, :ftype, :req, :dim, :qty, :amt, :dt, :enum, :aliases, :sort)
                    """),
                    {
                        "tid": row, "name": f.name, "label": f.label, "ftype": f.field_type,
                        "req": f.required, "dim": f.is_dimension, "qty": f.is_quantity,
                        "amt": f.is_amount, "dt": f.is_date,
                        "enum": json.dumps(f.enum_options) if f.enum_options else None,
                        "aliases": json.dumps(f.label_aliases), "sort": i,
                    },
                )
        db.commit()
    print(f"Seeded {len(template_ids)} templates: {list(template_ids.keys())}")
    return template_ids


def load_rate_schedule(tmpl: TemplateDef, template_id: int):
    schedule = json.loads((DATA_DIR / tmpl.key / "rate_schedule.json").read_text())
    with SessionLocal() as db:
        version_ids = {}
        for v in schedule["versions"]:
            row = db.execute(
                text("""
                    INSERT INTO rate_schedule_versions (template_id, effective_from, effective_to, published_by, notes)
                    VALUES (:tid, :ef, :et, :pb, :notes) RETURNING id
                """),
                {"tid": template_id, "ef": v["effective_from"], "et": v["effective_to"],
                 "pb": v["published_by"], "notes": v["notes"]},
            ).fetchone()
            version_ids[v["key"]] = row.id

        for key, rules in schedule["rate_rules"].items():
            for r in rules:
                db.execute(
                    text("""
                        INSERT INTO rate_rules (schedule_version_id, dimension_key, base_fee, rate_terms, surcharge_pct)
                        VALUES (:sid, CAST(:dk AS jsonb), :bf, CAST(:rt AS jsonb), :sc)
                    """),
                    {"sid": version_ids[key], "dk": json.dumps(r["dimension_key"], sort_keys=True),
                     "bf": r["base_fee"], "rt": json.dumps(r["rate_terms"]), "sc": r["surcharge_pct"]},
                )

        for key, exemptions in schedule["exemption_rules"].items():
            for e in exemptions:
                db.execute(
                    text("""
                        INSERT INTO exemption_rules
                            (schedule_version_id, exemption_code, description, discount_pct, applies_to)
                        VALUES (:sid, :code, :desc, :pct, CAST(:at AS jsonb))
                    """),
                    {"sid": version_ids[key], "code": e["exemption_code"], "desc": e["description"],
                     "pct": e["discount_pct"], "at": json.dumps(e["applies_to"]) if e["applies_to"] else None},
                )
        db.commit()
    print(f"[{tmpl.key}] loaded rate schedule: {len(schedule['versions'])} versions, "
          f"{sum(len(r) for r in schedule['rate_rules'].values())} rate rules")


def load_regulation_chunks(tmpl: TemplateDef, template_id: int, skip_embeddings: bool = False):
    regs_dir = DATA_DIR / "regulations" / tmpl.key
    metadata = REGULATION_METADATA[tmpl.key]
    with SessionLocal() as db:
        for filename, valid_from, valid_to in metadata:
            path = regs_dir / filename
            chunks = chunk_markdown(path)
            embeddings = None if skip_embeddings else embed_texts([c["content"] for c in chunks])
            for i, chunk in enumerate(chunks):
                embedding = embeddings[i] if embeddings else None
                db.execute(
                    text("""
                        INSERT INTO regulation_chunks
                            (template_id, doc_path, chunk_index, heading, content, valid_from, valid_to, embedding)
                        VALUES (:tid, :dp, :ci, :h, :c, :vf, :vt, :emb)
                    """),
                    {"tid": template_id, "dp": f"{tmpl.key}/{filename}", "ci": chunk["chunk_index"],
                     "h": chunk["heading"], "c": chunk["content"], "vf": valid_from, "vt": valid_to,
                     "emb": str(embedding) if embedding else None},
                )
        db.commit()
    print(f"[{tmpl.key}] loaded regulation chunks from {len(metadata)} docs "
          f"({'skipped embeddings' if skip_embeddings else 'embedded via ollama'})")


def load_documents(tmpl: TemplateDef, template_id: int):
    documents = json.loads((DATA_DIR / tmpl.key / "documents_manifest.json").read_text())
    out_dir = DOCUMENTS_DIR / tmpl.key
    with SessionLocal() as db:
        for doc in documents:
            pdf_path = render_document(doc, load_template_defs_wrapper(tmpl), out_dir)
            db.execute(
                text("""
                    INSERT INTO authority_documents
                        (template_id, filename, source_path, source_type, language, document_date, extraction_status)
                    VALUES (:tid, :fn, :sp, 'synthetic_seed', :lang, :dd, 'pending')
                """),
                {"tid": template_id, "fn": doc["filename"], "sp": str(pdf_path), "lang": doc["language"],
                 "dd": doc[tmpl.date_field_name]},
            )
        db.commit()
    print(f"[{tmpl.key}] rendered and loaded {len(documents)} authority documents")


def load_template_defs_wrapper(tmpl: TemplateDef):
    """render_document expects app.templates.Template (DB-shaped), not
    app.template_defs.TemplateDef (static definition) — they're structurally
    compatible for rendering purposes, so adapt one to the other rather than
    querying the DB mid-ingest for something we already have in memory."""
    from app.templates import Template, TemplateField
    fields = [
        TemplateField(
            name=f.name, label=f.label, field_type=f.field_type, required=f.required,
            is_dimension=f.is_dimension, is_quantity=f.is_quantity, is_amount=f.is_amount,
            is_date=f.is_date, enum_options=f.enum_options, label_aliases=f.label_aliases,
        )
        for f in tmpl.fields
    ]
    return Template(id=0, key=tmpl.key, name=tmpl.name, description=tmpl.description,
                     authority_name=tmpl.authority_name, document_label=tmpl.document_label, fields=fields)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-embeddings", action="store_true")
    args = parser.parse_args()

    apply_schema()
    template_ids = seed_templates()

    with SessionLocal() as db:
        db.execute(text("TRUNCATE reconciliation_runs, authority_documents RESTART IDENTITY CASCADE"))
        db.execute(text("TRUNCATE regulation_chunks RESTART IDENTITY"))
        db.execute(text("TRUNCATE rate_schedule_versions, rate_rules, exemption_rules RESTART IDENTITY CASCADE"))
        db.commit()

    for tmpl in ALL_TEMPLATES:
        template_id = template_ids[tmpl.key]
        load_rate_schedule(tmpl, template_id)
        load_regulation_chunks(tmpl, template_id, skip_embeddings=args.skip_embeddings)
        load_documents(tmpl, template_id)


if __name__ == "__main__":
    main()
