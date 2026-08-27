CREATE EXTENSION IF NOT EXISTS vector;

-- A "template" is one reconciliation use case (transport levy, utility
-- bill, insurance premium, ...). The engine is generic; templates are data,
-- not code — a new use case is a new template row + its fields + its rate
-- table, not a new calculator module.
CREATE TABLE IF NOT EXISTS templates (
    id SERIAL PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    authority_name TEXT NOT NULL,
    document_label TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every field a template's documents can carry. `is_dimension` fields key
-- into rate_rules (e.g. cargo_class + vehicle_class together select a rate
-- row); `is_quantity` fields are multiplied by a rate_terms entry (e.g.
-- distance_km, weight_kg). A field can be neither (shipment_date,
-- authority_claimed_amount) or, in principle, both is not meaningful.
-- `label_aliases` are the strings Docling extraction searches a document's
-- text for when looking for this field — generalizes what used to be a
-- hardcoded label list in the extractor.
CREATE TABLE IF NOT EXISTS template_fields (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    name TEXT NOT NULL,
    label TEXT NOT NULL,
    field_type TEXT NOT NULL CHECK (field_type IN ('text', 'number', 'date', 'enum')),
    required BOOLEAN NOT NULL DEFAULT true,
    is_dimension BOOLEAN NOT NULL DEFAULT false,
    is_quantity BOOLEAN NOT NULL DEFAULT false,
    -- Exactly one field per template should set each of these: is_amount
    -- marks the field being independently verified (named per-domain --
    -- "authority_claimed_amount" for a levy, "billed_amount" for a utility
    -- bill); is_date marks the field used to pick the schedule version.
    is_amount BOOLEAN NOT NULL DEFAULT false,
    is_date BOOLEAN NOT NULL DEFAULT false,
    enum_options JSONB,
    label_aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (template_id, name)
);

-- One template publishes a whole new rate schedule "edition" periodically,
-- rather than versioning each line item independently — this is what the
-- temporal RAG and the calculator both key off of to resolve "which rules
-- applied on this document's date," per template.
CREATE TABLE IF NOT EXISTS rate_schedule_versions (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    effective_from DATE NOT NULL,
    effective_to DATE,
    published_by TEXT NOT NULL,
    notes TEXT
);

-- dimension_key selects a rule (e.g. cargo_class=general_freight plus
-- vehicle_class=light_truck together); rate_terms is the list of quantity
-- charges, each a field name, a rate, and a free-tier threshold below
-- which that field isn't charged — this shape covers per-unit-with-a-
-- free-tier pricing generally: transport levies, utility tiers, insurance
-- premiums, shipping, without arbitrary code. SQLAlchemy's text() scans
-- this whole file for bind params even inside comments, so avoid writing
-- inline JSON-style examples here with a colon directly before a digit or
-- bare word above this line -- that reads as a bind parameter reference
-- and breaks schema application (confirmed directly, not assumed).
CREATE TABLE IF NOT EXISTS rate_rules (
    id SERIAL PRIMARY KEY,
    schedule_version_id INTEGER NOT NULL REFERENCES rate_schedule_versions(id),
    dimension_key JSONB NOT NULL,
    base_fee NUMERIC(12, 2) NOT NULL,
    rate_terms JSONB NOT NULL DEFAULT '[]'::jsonb,
    surcharge_pct NUMERIC(6, 3) NOT NULL DEFAULT 0,
    UNIQUE (schedule_version_id, dimension_key)
);

-- applies_to is a dimension filter (e.g. {"cargo_class":"general_freight"})
-- or NULL for a universal exemption (matches any dimension values).
CREATE TABLE IF NOT EXISTS exemption_rules (
    id SERIAL PRIMARY KEY,
    schedule_version_id INTEGER NOT NULL REFERENCES rate_schedule_versions(id),
    exemption_code TEXT NOT NULL,
    description TEXT NOT NULL,
    discount_pct NUMERIC(6, 3) NOT NULL,
    applies_to JSONB,
    UNIQUE (schedule_version_id, exemption_code)
);

-- Human-readable regulation text backing the structured rate_rules above —
-- what an investigation agent actually cites when explaining *why* a rate
-- applied. Effective-dated the same way as rate_schedule_versions so a
-- retrieval can be pre-filtered to the rule text valid on a document's date
-- before the vector search runs, not left to embedding similarity to guess.
CREATE TABLE IF NOT EXISTS regulation_chunks (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    doc_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    valid_from DATE NOT NULL,
    valid_to DATE,
    embedding vector(768),
    UNIQUE (doc_path, chunk_index)
);

CREATE INDEX IF NOT EXISTS regulation_chunks_embedding_idx
    ON regulation_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS authority_documents (
    id SERIAL PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id),
    filename TEXT NOT NULL,
    -- NULL for excel_row documents — a spreadsheet-sourced row has no
    -- source image, so it has no bounding-box provenance either.
    source_path TEXT,
    source_type TEXT NOT NULL DEFAULT 'synthetic_seed'
        CHECK (source_type IN ('pdf_upload', 'excel_row', 'synthetic_seed')),
    language TEXT NOT NULL DEFAULT 'en',
    document_date DATE,
    extracted_fields JSONB,
    extraction_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (extraction_status IN ('pending', 'extracted', 'needs_review')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES authority_documents(id),
    authority_amount NUMERIC(12, 2),
    recomputed_amount NUMERIC(12, 2),
    discrepancy_amount NUMERIC(12, 2),
    discrepancy_pct NUMERIC(8, 3),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'clean_match', 'discrepancy_found', 'needs_review', 'error')),
    root_cause_category TEXT
        CHECK (root_cause_category IN
            ('classification_error', 'exemption_error', 'temporal_mismatch', 'arithmetic_only', NULL)),
    calculation_trace JSONB,
    evidence_ledger JSONB,
    memo TEXT,
    -- Resolution workflow: a reviewer's disposition of a flagged run, distinct
    -- from `status` (which is the system's own match/mismatch determination
    -- and shouldn't be overwritten by a human decision about what to do next).
    resolution_status TEXT NOT NULL DEFAULT 'open'
        CHECK (resolution_status IN ('open', 'corrected', 'accepted', 'disputed', 'resolved')),
    resolution_notes TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    -- Append-only audit trail: [{"at", "action", "actor", "details"}, ...]
    activity_log JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reconciliation_runs_discrepancy_idx
    ON reconciliation_runs (discrepancy_amount DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS reconciliation_runs_status_idx
    ON reconciliation_runs (status);

-- Hosted-provider credentials for the Settings tab. api_key is encrypted at
-- rest (app/crypto.py, Fernet, key in .secret_key or WORK_CUBE_SECRET_KEY)
-- and write-only from the API's perspective (never returned by GET) — this
-- is a local-first, single-operator tool with no auth system, so this is
-- the right bar for that trust boundary, not a multi-tenant secrets store.
-- Configuring a provider here does not change Phase 1's behavior
-- (deterministic, no LLM in its path by design) — it's groundwork for
-- Phase 2 and for regenerating regulation-chunk embeddings.
CREATE TABLE IF NOT EXISTS provider_settings (
    provider TEXT PRIMARY KEY
        CHECK (provider IN ('openai', 'anthropic', 'gemini', 'mistral', 'deepseek')),
    api_key TEXT,
    model TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
