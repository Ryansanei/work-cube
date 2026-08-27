.PHONY: install db-schema data ingest process run eval test test-db-setup lint

TEST_DATABASE_URL ?= postgresql+psycopg://localhost/work_cube_test

install:
	python3.12 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

db-schema:
	psql "$(subst +psycopg,,$(DATABASE_URL))" -f db/schema.sql

# One-time (or after a schema change): a dedicated database for `make test`,
# separate from the dev/demo database so test runs never leave
# api_test_upload.pdf / lifecycle_test.xlsx / etc. rows in what the app's
# own dashboard shows.
test-db-setup:
	createdb work_cube_test || true
	psql work_cube_test -f db/schema.sql
	DATABASE_URL=$(TEST_DATABASE_URL) .venv/bin/python -m scripts.ingest

data:
	.venv/bin/python -m scripts.generate_data

ingest:
	.venv/bin/python -m scripts.ingest

process:
	.venv/bin/python -c "from app.db import SessionLocal; from app.pipeline import process_pending_documents; \
	db = SessionLocal(); n = process_pending_documents(db); print(f'processed {n} documents'); db.close()"

run:
	.venv/bin/uvicorn app.api.main:app --reload --port 8010

eval:
	.venv/bin/python -m eval.run_eval

test:
	DATABASE_URL=$(TEST_DATABASE_URL) .venv/bin/python -m pytest tests/ -v

lint:
	.venv/bin/ruff check .
