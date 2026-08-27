# Work Cube

A local-first, general-purpose, open-source implementation of the
**shadow-calculation pattern**: instead of asking an LLM to answer a
question from scratch, the system independently *recomputes* a third
party's calculation and reports where it disagrees, with every claim
traceable back to a specific record, regulation, or region of the source
document.

Work Cube is **config-driven, not hardcoded to one domain**: a use case is
a *template* — a set of fields plus a rate-formula shape — not a code
change. Adding a new use case means adding a template definition, not
writing a new calculator, extractor, or UI. The repo ships two templates
end-to-end as proof this genuinely generalizes, not just architecturally
claims to: **transport levy assessments** (the original worked example —
a fictional authority, the National Transport Levy Authority (NTLA),
Veridia, assessing shipments by cargo class, vehicle class, weight, and
distance) and **utility bills** (a municipal power/water authority billing
by customer class and usage) — structurally different templates (different
field counts, different dimension/rate-term shapes) that both run through
the exact same pipeline, calculator, and UI. See "Templates" below.

An official **Dark Cube** product (alongside [Cuby](https://github.com/DarkCube-team/cuby-demo)) —
provider-agnostic and runnable entirely on a local machine, on purpose:
no cloud project, no billing account, no API key required to try it.
Native desktop packaging for Windows and macOS is planned; see "What's not
here" for current status.

## Why this exists

Started as a third portfolio piece, deliberately distinct from the other two:

- **Support Copilot** — single-agent OpenAI tool-calling, hybrid RAG + Postgres.
- **Cloud Cost Advisor** — Google ADK multi-agent orchestration on Vertex AI.
- **Work Cube** — no framework-provided orchestration yet (Phase 1, see
  below), a genuinely different problem shape (verify-a-third-party's-math,
  not answer-a-question), real multimodal document extraction with
  citation provenance, and — unlike the other two — **fully runnable and
  verifiable by anyone**, with no access-gated cloud dependency at all.

It has since grown past that: the first pass was scoped narrowly to
transport levies, but the underlying pattern — extract, recompute, compare,
cite — doesn't actually care what the numbers are called. It's now built
and shipped as a general-purpose Dark Cube product, with the transport levy
use case as one template among several rather than the whole point.

## From demo to app

The first pass at this UI was a read-only dashboard over 40 pre-seeded
synthetic documents — a single screen, no way to bring your own document in.
A user tried it and called it correctly: "it's only a demo look-alike... it
has no such thing as inputs as PDF, Word, Excel... it's just a one tab
screen." That's a fair description of what existed at that point, and it
prompted an actual rearchitecture rather than a defensive response.

Before touching code, this went through real competitive research — freight
audit platforms, AP automation tools (Rossum, AppZen), document-intake AI UX
guides — looking at both what real products' architectures do and how
reviewers actually use them day to day. The gap wasn't a rendering bug, it
was scope: no real intake, no way to act on a flagged document, and one flat
view standing in for what real tools split into distinct sections. What's
described below — the Inbox, the editable/actionable Review Queue, the Rate
Schedule view — is the result of closing that gap, not the original design.

## Real-world use cases

Transport levies and utility bills are two worked examples of a pattern
that shows up anywhere a business receives a bill, assessment, or statement
from a third party whose calculation logic is public or contractually
defined, but whose arithmetic isn't independently checked because doing so
by hand is slow and error-prone at any real volume:

- **Freight & logistics accessorial billing audits** — the problem this
  project was actually seeded by: a carrier invoices fuel surcharges,
  detention, and accessorial fees against a contracted rate card with many
  variables (weight, distance, fuel index, equipment type), and a shipper's
  ops team re-derives every invoice by hand to catch overcharges.
- **Customs & import duty verification** — an importer receives a broker's
  or customs authority's duty calculation from a published tariff schedule
  (HS code, country of origin, declared value) and wants to verify the
  classification and rate applied before paying or disputing it.
- **Utility bill auditing** — a business on a tiered or seasonal commercial
  electricity/water/telecom rate plan checks the vendor's monthly bill
  against the published rate schedule, catching tier misapplication or
  stale-rate errors.
- **Payroll tax withholding verification** — an employer or employee
  verifies a payroll processor's computed withholding against published tax
  brackets and exemptions for a given pay period and jurisdiction.
- **Insurance premium or claim payout verification** — a policyholder
  verifies an insurer's premium calculation or claim settlement against the
  policy's own rate tables and coverage terms.

In every case, the shape is the same: extract the variables a third party's
document claims to have used, recompute independently from the actual
published rule set, and produce a citation-backed explanation of any
disagreement — which is exactly Work Cube's four layers, just pointed at a
different rate schedule and document format.

## Templates: config-driven, not hardcoded

A **template** (`templates` + `template_fields` in Postgres, defined
statically per shipped use case in `app/template_defs.py`) is what makes a
use case addable as data:

- **Fields** — name, label, type (text/number/date/enum), whether it's
  required, and role flags: `is_dimension` (keys into the rate table, e.g.
  cargo class), `is_quantity` (a rated quantity, e.g. weight), `is_amount`
  (the third party's claimed total), `is_date` (which rate schedule version
  applies), plus `label_aliases` the extractor matches against in the
  source document.
- **A generic rate formula shape** — `total = (base_fee + Σ(rate ×
  max(0, quantity − free_threshold))) × (1 + surcharge_pct/100) × (1 −
  exemption_pct/100 if applicable)`, with rate rules keyed by whatever
  combination of dimension fields the template defines. Deliberately not
  Turing-complete — no arbitrary code execution risk from a new template.

Every layer — schema, calculator, PDF/manifest extraction, document
rendering, temporal RAG, API, and the UI itself (including the Farsi
translation) — reads field/dimension/rate structure from these tables
rather than hardcoded field names. The `utility_bill` template exists
specifically to prove this: it has a different field count, one dimension
instead of two, and one rate term instead of two, and produces identical
correctness behavior through the identical code path (`tests/` exercises
both templates end to end; `eval/run_eval.py` grades both).

Authoring a template today means adding a `TemplateDef` in
`app/template_defs.py` plus a rate "recipe" for the synthetic data
generator — a template-authoring UI (so a new use case doesn't require
touching Python at all) is planned but not yet built.

## Phase 1 (built) vs. Phase 2 (not yet built)

Scoped deliberately in two phases to avoid the scope bloat that's the
obvious risk in a project with this many moving parts:

- **Phase 1** (this repo, today): ingest → extract → deterministically
  recompute → flag match/mismatch → persist to a triage table. No LLM
  anywhere in this loop — see "No LLM in Phase 1" below.
- **Phase 2** (not built): when a discrepancy is found, a LangGraph
  investigation graph would delegate to specialist nodes (classification
  check, exemption check, temporal check) that each append a structured
  finding to a shared evidence ledger, and a synthesis step would produce a
  cited discrepancy memo constrained to only that ledger. This is where an
  LLM actually enters the system, and where a probabilistic eval harness
  (routing accuracy, citation grounding) like the other two projects' would
  become relevant — Phase 1's eval, described below, is a different kind of
  thing.

## Architecture

```
   authority PDF/image           bulk Excel manifest
        │                              │
        ▼                              ▼
  Docling extraction            openpyxl row parser
  (bounding-box provenance)     (no source image → no provenance)
        │                              │
        └──────────────┬───────────────┘
                        ▼
         reconcile_from_field_values()   ◄──  templates / template_fields, and per-template
                        │                       rate_schedule_versions / rate_rules /
                        │                       exemption_rules (Postgres, effective-dated)
                        ▼
         compare vs. authority's claimed amount
                        │
                        ▼
         reconciliation_runs (Postgres)
                        │
        ┌───────────────┼────────────────────┐
        ▼                                     ▼
  Review Queue UI                   reviewer corrects a field
  (view, filter, resolve)                     │
        ▲                                     ▼
        └──────────────  recompute + activity_log  ◄─┘
```

One Postgres instance holds everything: the structured rate schedule, the
pgvector-backed regulation text (`regulation_chunks`), the extracted
document fields with provenance (`authority_documents.extracted_fields`),
and the triage table (`reconciliation_runs`) — now including a resolution
workflow (`resolution_status`, `resolution_notes`, `activity_log`) on top of
the original match/mismatch determination.

Both intake paths — Docling extraction and manifest parsing — and a manual
field correction all funnel into the exact same
`app/pipeline.py::reconcile_from_field_values` function, so "is this a
match" is never implemented three different ways.

### No LLM in Phase 1's extraction or calculation

Phase 1's synthetic documents are consistently labeled ("Cargo
Classification: ...", "Total Levy Assessed: $..."), so extraction is
deterministic label-matching over Docling's structured text output — not an
LLM call. The levy itself is computed by a plain, versioned Python
calculator reading the rate schedule, never by an LLM doing arithmetic. This
means the entire Phase 1 pipeline runs with **zero model dependency, not
even Ollama** — genuinely local-first, and genuinely free to run.
LLM-assisted extraction for messier, inconsistently formatted real-world
documents is a natural future direction, not built here.

### Temporal RAG (wired, not yet consumed)

`app/rag/temporal_rag.py` searches `regulation_chunks` with a SQL
`valid_from`/`valid_to` pre-filter *before* the pgvector similarity search —
so retrieval resolves which schedule version's rules applied on a given
document's date deterministically, rather than leaving two dated versions of
the same clause to compete on embedding similarity. Verified directly:
querying with an as-of date in the original 2024 schedule's window never
returns the 2025-07 revision's rate chunks, and vice versa (`tests/test_temporal_rag.py`).

This is currently a standalone, tested capability — nothing in Phase 1 calls
it yet, since Phase 1 doesn't investigate *why* a discrepancy occurred, only
*that* one exists. It's Phase 2's regulation-citation tool.

### Model backend

Provider-agnostic via [LiteLLM](https://github.com/BerriAI/litellm), default
to a **local Ollama** model (`llama3.2:1b` for generation, `nomic-embed-text`
for embeddings) — no API key needed. Override `WORK_CUBE_LLM_MODEL` /
`WORK_CUBE_EMBEDDING_MODEL` to any LiteLLM-supported model string (e.g.
`anthropic/claude-3-5-sonnet-20241022`) plus the matching provider API key to
use a hosted model instead. Nothing in Phase 1 actually calls the LLM model
(see above) — only regulation-chunk embedding at ingest time uses Ollama
today.

### Why synthetic data

`scripts/generate_data.py` (fixed seed `20260818`) is one generic engine
driven per-template by a "rate recipe" (the actual dollar figures — business
knowledge no engine can invent), run once per template in
`app/template_defs.py`. For each template it generates two schedule
"editions" that tell a deliberate story, not random noise: an original 2024
schedule, and a 2025-07-01 revision that raises rates ~8% and introduces a
new exemption that didn't exist before. 40 sample documents per template (80
total across the two shipped templates) are generated across 6 categories:
clean matches, classification errors (document correctly declares a
dimension value, e.g. cargo class, but the authority's figure used a
different value's rate table), exemption errors (a valid exemption is
declared but not reflected in the total), temporal mismatches (dated near
the schedule boundary, computed with the wrong version's rates), arithmetic-
only discrepancies (everything else correct, the total is just off), and
needs-review cases (a required field is missing from the document entirely).

Regulation text (`data/regulations/<template_key>/*.md`) is hand-written,
chunked by H2 section, and effective-dated to match each template's schedule
story — never generated from or tied to any real project's data.

## Setup

### Option A — Docker

```bash
docker compose up --build
```

Postgres+pgvector runs in the container; the app expects **Ollama running on
the host** at `http://host.docker.internal:11434`. Verified directly, not
assumed: **Docker Desktop resolves `host.docker.internal` correctly, but
Ollama's default bind is `127.0.0.1` only** (confirmed via `lsof` — `ollama
serve` listens on `localhost:11434`, not `0.0.0.0:11434`), so a default
Ollama install is still unreachable from the container even with DNS/routing
working. Fix by running Ollama with `OLLAMA_HOST=0.0.0.0:11434 ollama serve`
(or the equivalent for how you start it) so it accepts connections beyond
loopback. On Linux, `host.docker.internal` additionally needs
`extra_hosts: ["host.docker.internal:host-gateway"]` on the `app` service in
a `docker-compose.override.yml`.

If Ollama isn't reachable, ingestion proceeds without regulation-chunk
embeddings rather than failing outright — this is the actual, observed
behavior of a `docker compose up` run on this machine's default Ollama
setup, not a hypothetical fallback path.

First run downloads Docling's layout/table/OCR models from Hugging Face —
needs internet, and can take a few minutes.

### Option B — local Python

```bash
brew install postgresql@17 pgvector ollama
ollama pull llama3.2:1b && ollama pull nomic-embed-text

make install
createdb work_cube
make db-schema DATABASE_URL=postgresql://localhost/work_cube

cp .env.example .env

make data      # writes data/<template_key>/*.json for every shipped template (fixed seed — reproducible)
make ingest    # loads Postgres, embeds regulation chunks, renders documents to PDF, for every template
make process   # runs Docling extraction + recomputation over every document, every template

make run       # http://localhost:8010
```

### Try it

Open **`http://localhost:8010/`** — a real three-section app, not a single
screen. A **template selector** in the header switches the entire app
(Inbox uploads, Review Queue, Rate Schedule) between the shipped templates —
the same UI, driven by whichever template's fields/dimensions are active,
not a separate build per use case.

**Inbox** — drag-and-drop (or click to browse) PDF/image files for real
Docling extraction, or upload a bulk Excel manifest (`.xlsx`) listing many
shipments at once — a genuine freight-audit pattern: reconcile a whole
batch from one spreadsheet instead of one document at a time. Manifest rows
have no source image, so they carry no bounding-box provenance — the UI
says so explicitly rather than pretending otherwise. Every upload result
links straight into the Review Queue.

**Review Queue** — the original triage dashboard, now actionable, sorted by
dollar-impact discrepancy and filterable by status. Click a row to see the
source document with bounding-box overlays on every extracted field, the
comparison against the authority's claimed amount, and the full calculation
trace. Below that: **editable fields** (correct a misread or missing value
and hit *Save & Recompute* — it re-runs the exact same reconciliation logic,
it doesn't fake the outcome), **resolution actions** (Accept / Dispute /
Mark Resolved, with notes), and a chronological **activity log** of every
correction, recompute, and resolution on that document — the audit trail
real freight-audit tools have and the original read-only viewer didn't.

**Rate Schedule** — a read-only view of the versioned rules the calculator
actually uses (both schedule editions, every rate rule, every exemption).
Makes the system legible instead of a black box: if a number looks wrong,
this is where you check whether the *rule* was wrong, not just the *math*.

The globe icon in the header toggles the UI between English and Farsi (RTL
layout included, all three sections) — added because the real users of a
tool like this (freight/logistics ops staff) often aren't English speakers,
and a portfolio demo that only works in English doesn't actually prove the
tool is usable. This translates the interface chrome and known field-value
labels (cargo class, vehicle class); it does **not** translate the source
document itself — that stays English, since Farsi document *extraction* is
a separately-scoped, harder problem (see "What's not here" below).

## Evaluation

```bash
make eval
```

Unlike the other two projects, **this eval harness has actually been run to
completion, and its numbers are real** — Phase 1 has no access-gated model
call in its loop, so there's nothing blocking a live run. `eval/run_eval.py`
loops over every shipped template, checking each one's pipeline output
against its own `data/<template_key>/documents_manifest.json` ground truth
(known exactly, since the data is generated) — a single combined report
plus a per-template breakdown:

| Metric | transport_levy | utility_bill | Combined |
|---|---|---|---|
| Status classification accuracy (clean / discrepancy / needs-review) | **100%** (40/40) | **100%** (40/40) | **100%** (80/80) |
| Recomputed amount accuracy | **100%** | **100%** | **100%** |
| Field extraction accuracy | **100%** | **100%** | **100%** |
| HITL routing accuracy (missing-field documents correctly flagged) | **100%** (3/3) | **100%** (3/3) | **100%** (6/6) |

This is deliberately **not** shaped like Support Copilot's or Cloud Cost
Advisor's eval harnesses. Those grade a probabilistic system — did the LLM
route to the right specialist, did it cite accurately. Phase 1 has no LLM in
its decision path, so there's no routing or citation-grounding question to
grade yet; this is closer to a deterministic acceptance test than a model
eval. A Phase 2 eval harness, once the investigation graph exists, will look
like the other two projects' — because that's where an LLM actually enters
the system.

Results are archived the same way as the other two projects:
`eval/results/latest.json` (gitignored scratch), `eval/results/history.jsonl`
and `eval/results/runs/<timestamp>.json` (git-tracked — not "evidence" until
committed).

The eval query filters to `source_type = 'synthetic_seed'` **and** the
document's `template_id` deliberately — once real upload existed, the
golden-set documents no longer had the database to themselves, and once a
second template existed, an unfiltered-by-template query would have
compared one template's manifest against the other template's rows too.
Both were caught and fixed while building the respective features, not
before.

## Testing

```bash
make test
```

58 tests, real mechanics tested for real — including real Docling extraction,
real Postgres/pgvector, and real HTTP requests against the actual FastAPI app
(`tests/test_api.py`, via `TestClient`) for the upload, correction, and
resolution endpoints. `embed_query` is mocked only in `test_temporal_rag.py`,
so the temporal SQL filter is verified without requiring Ollama to be
running during tests.

Every layer that touches template structure (calculator, extraction,
manifest parsing, pipeline, temporal RAG, the API) has a dedicated test
proving it also works correctly for `utility_bill`, not just
`transport_levy` — the generalization is tested, not just architecturally
asserted.

Three real bugs were caught this way, not by inspection:
- Docling merges visually-close lines into a single text block rather than
  one item per line — a naive whole-line regex parser would have silently
  dropped most fields. Fixed by scanning each block for every known label's
  *position* and slicing values between them.
- SQLAlchemy's `text()` silently fails to substitute a bind parameter
  immediately followed by a `::vector` cast — fixed with `CAST(... AS vector)`.
- The pipeline computed per-field bounding-box provenance but only ever
  persisted flat string values, discarding it before the UI could ever use
  it — caught before the UI was even built, by checking what
  `process_pending_documents` actually wrote to the database.

A fourth, more serious bug shipped and was caught by an actual user, not by
this test suite: CSS grid items default to `min-width: auto`, so `.layout`'s
two-column split (list + detail panel) refused to shrink below the table's
content width on any viewport narrower than ~1400px — pushing the entire
detail panel off-screen with no visual indication anything was wrong. It
read as "some tabs aren't clickable" and "I don't see any classifications,"
because the panel that would answer both was rendered, just invisible off
to the right. Fixed with `min-width: 0` on the grid items and an
`overflow-x: auto` scroll container around the table itself. A reminder that
"verified in a real browser" is only as good as the viewport width it was
verified at — this was tested at 1400px and broke at 721px.

Two more caught while building the upload/correction/resolution rearchitecture:
- `POST /upload/pdf` extracts real bounding-box provenance from Docling but
  originally never backfilled `authority_documents.document_date` from the
  extracted `shipment_date` — uploaded documents would have shown "no date"
  in the Review Queue forever. Caught by checking the actual API response,
  not assumed from the code.
- `POST /runs/{id}/recompute` updated `reconciliation_runs.status` but left
  `authority_documents.extraction_status` stuck on `needs_review` even after
  a successful correction — a document that was genuinely fixed would have
  kept showing as unresolved. Caught the same way: checking the real
  response after the real action, not trusting the code to be right.

## What was and wasn't verified

**Verified for real:** schema application, the calculator against real
ingested rate rules, the synthetic data generator's determinism and category
correctness, Docling extraction (including bounding-box provenance) against
real rendered PDFs, the temporal RAG filter against real pgvector, the full
Phase 1 pipeline run end-to-end against all 80 documents across both shipped
templates with **zero mismatches** against ground truth, every API endpoint (list/detail, upload,
field correction, recompute, resolve, rate schedule) against live data via
both direct HTTP calls and `TestClient`, and — in a real browser — all three
UI sections: Inbox uploads (PDF and manifest), the Review Queue's filtering,
row selection, editable-field correction, and resolution actions end to
end, and the Rate Schedule view. The correct → recompute → resolve lifecycle
was driven through the actual UI, not just the API, and its activity log
was checked against what really happened, not what the code intends to do.

**One real gap in how this was verified:** the Inbox's drag-and-drop and
file-picker interactions could not be driven through an actual OS file
dialog in this sandboxed browser environment. The upload *code path* is
fully verified — real files, real Docling extraction, real database writes,
proven both via direct API calls and via `tests/test_api.py` — but the
literal "drag a file from your desktop" gesture wasn't. Said plainly instead
of quietly assumed.

**Not yet built, not yet verified:** anything Phase 2 (LangGraph
investigation, evidence ledger, citation-backed memos, root-cause
classification), a live hosted-model run (only local Ollama has been
exercised), and the Farsi/RTL document extraction module (deferred — see
below).

## What's not here (known limitations)

- **No native desktop packaging yet.** Windows and macOS builds are planned
  (this currently runs as a local web app via `make run` / Docker Compose
  only) but packaging work hasn't started.
- **No local-vs-hosted-model hardware detection yet.** The idea — check the
  device's available RAM/GPU on request and suggest whether local inference
  (Ollama) is realistic or a hosted API key is needed instead — is planned
  but not built; today the choice is manual (`WORK_CUBE_LLM_MODEL` env var,
  see "Model backend" above).
- **No template-authoring UI.** Adding a new use case today means adding a
  `TemplateDef` in `app/template_defs.py` plus a synthetic-data "rate
  recipe" — both require touching Python, not just configuration through
  the app itself.
- **Phase 2 (investigation graph) is not built.** `root_cause_category`,
  `evidence_ledger`, and `memo` columns exist in the schema and are read by
  the API, but nothing populates them yet — every discrepancy in this repo
  is flagged, never explained.
- **Farsi/RTL is a UI-level toggle, not document-level support.** The
  interface itself (labels, filters, table, detail panel) is fully
  translated and RTL-aware. The source *documents* are still English-only —
  a genuine Farsi document-extraction pipeline needs local OCR script-support
  verification (the bundled `rapidocr` models are not confirmed to handle
  Persian script) that wasn't done in this pass. Flagging that distinction
  explicitly rather than letting the UI toggle imply more than it does.
- **Bounding-box provenance is per text-block, not per-field.** When Docling
  merges several labeled fields into one visual block (common when they're
  vertically close together), all of those fields share that block's
  bounding box rather than a pixel-tight box around just one field — visible
  in the UI as stacked overlapping labels. An accepted Phase 1 approximation.
- **No deployment beyond Docker Compose.** Local-first is the point; no
  cloud hosting target is planned.
- **No auth or per-user identity.** `actor`/`resolved_by` on corrections and
  resolutions are freeform text fields the caller supplies (defaulting to
  `"reviewer"`), not an authenticated user — fine for a single-reviewer
  portfolio demo, not fine for a real multi-user deployment.
- **Corrections aren't validated against the rate schedule's own vocabulary.**
  A reviewer can type any string into `cargo_class`; a value that doesn't
  match a real `rate_rules` row just surfaces as a `NoApplicableRateRuleError`
  on recompute rather than being caught earlier as an obviously-invalid
  input. Works, but the error surfaces later than it ideally would.

## Stack

FastAPI · Docling · LangGraph *(Phase 2)* · LiteLLM (Ollama default) ·
`nomic-embed-text` · PostgreSQL + pgvector · SQLAlchemy · PyMuPDF ·
ReportLab · openpyxl · Docker · pytest · ruff
