#!/bin/bash
set -e

# psql/pg_isready don't understand SQLAlchemy's "+psycopg" driver suffix —
# strip it for the CLI calls below, keep $DATABASE_URL as-is for the app.
PSQL_URL="${DATABASE_URL/+psycopg/}"

echo "Waiting for Postgres..."
until pg_isready -d "$PSQL_URL" -q; do sleep 1; done

echo "Ensuring schema exists (idempotent — CREATE TABLE/EXTENSION IF NOT EXISTS)..."
psql "$PSQL_URL" -f db/schema.sql > /dev/null

DOC_COUNT=$(psql "$PSQL_URL" -tAc "SELECT COUNT(*) FROM authority_documents" 2>/dev/null || echo 0)

if [ "$DOC_COUNT" = "0" ]; then
  echo "No documents loaded yet — running full setup."

  if [ ! -f data/rate_schedule.json ] || [ ! -f data/documents_manifest.json ]; then
    echo "Generating synthetic data (fixed seed)..."
    python3 -m scripts.generate_data
  fi

  SKIP_EMBEDDINGS_FLAG=""
  if ! curl -sf "${OLLAMA_API_BASE:-http://localhost:11434}/api/version" > /dev/null 2>&1; then
    echo "Ollama not reachable at ${OLLAMA_API_BASE:-http://localhost:11434} — ingesting without regulation-chunk embeddings."
    echo "(On Linux, pass OLLAMA_API_BASE=http://host.docker.internal:11434 with --add-host=host.docker.internal:host-gateway.)"
    SKIP_EMBEDDINGS_FLAG="--skip-embeddings"
  fi

  echo "Loading rate schedule + regulation chunks, rendering documents..."
  python3 -m scripts.ingest $SKIP_EMBEDDINGS_FLAG

  echo "Running Phase 1 reconciliation (first run also downloads Docling's layout/OCR models — needs internet, may take a few minutes)..."
  python3 -c "
from app.db import SessionLocal
from app.pipeline import process_pending_documents
with SessionLocal() as db:
    n = process_pending_documents(db)
    print(f'processed {n} documents')
"
else
  echo "$DOC_COUNT documents already loaded — skipping setup."
fi

echo "Starting API on :8010..."
exec uvicorn app.api.main:app --host 0.0.0.0 --port 8010
