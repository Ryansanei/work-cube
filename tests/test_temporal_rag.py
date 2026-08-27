from datetime import date
from unittest.mock import patch

from app.rag.temporal_rag import search_regulations
from app.templates import load_template

FIXED_VECTOR = [0.1] * 768


def _transport_levy_id(db):
    return load_template(db, "transport_levy").id


def test_v1_period_query_never_returns_v2_only_chunks(db):
    tid = _transport_levy_id(db)
    with patch("app.rag.temporal_rag.embed_query", return_value=FIXED_VECTOR):
        results = search_regulations(db, "rate table", date(2025, 3, 1), tid, top_k=20)
    doc_paths = {r["doc_path"] for r in results}
    assert "transport_levy/levy_rates_2025_revision.md" not in doc_paths
    assert "transport_levy/exemptions_cold_chain_2025.md" not in doc_paths


def test_v2_period_query_never_returns_v1_only_chunks(db):
    tid = _transport_levy_id(db)
    with patch("app.rag.temporal_rag.embed_query", return_value=FIXED_VECTOR):
        results = search_regulations(db, "rate table", date(2025, 9, 1), tid, top_k=20)
    doc_paths = {r["doc_path"] for r in results}
    assert "transport_levy/levy_rates_2024.md" not in doc_paths


def test_evergreen_chunks_appear_on_both_sides_of_the_boundary(db):
    tid = _transport_levy_id(db)
    with patch("app.rag.temporal_rag.embed_query", return_value=FIXED_VECTOR):
        before = search_regulations(db, "cargo classification", date(2024, 6, 1), tid, top_k=20)
        after = search_regulations(db, "cargo classification", date(2026, 1, 1), tid, top_k=20)
    before_paths = {r["doc_path"] for r in before}
    after_paths = {r["doc_path"] for r in after}
    assert "transport_levy/cargo_classification_guide.md" in before_paths
    assert "transport_levy/cargo_classification_guide.md" in after_paths


def test_citation_id_format(db):
    tid = _transport_levy_id(db)
    with patch("app.rag.temporal_rag.embed_query", return_value=FIXED_VECTOR):
        results = search_regulations(db, "exemptions", date(2025, 9, 1), tid, top_k=3)
    for r in results:
        assert r["citation_id"] == f"{r['doc_path']}#{r['chunk_index']}"


def test_search_is_scoped_to_the_given_template(db):
    """A utility_bill query must never return transport_levy chunks, even
    when both templates' regulation text happens to be valid on the same
    date — the template_id filter, not just the date filter, is what keeps
    a Phase 2 investigation from citing the wrong domain's rules."""
    utility_id = load_template(db, "utility_bill").id
    with patch("app.rag.temporal_rag.embed_query", return_value=FIXED_VECTOR):
        results = search_regulations(db, "classification", date(2024, 6, 1), utility_id, top_k=50)
    doc_paths = {r["doc_path"] for r in results}
    assert all(p.startswith("utility_bill/") for p in doc_paths)
