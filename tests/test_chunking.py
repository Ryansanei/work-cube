from pathlib import Path

from app.rag.chunking import chunk_markdown

REGULATIONS_DIR = Path(__file__).parent.parent / "data" / "regulations" / "transport_levy"


def test_chunk_markdown_splits_by_h2_and_drops_h1():
    chunks = chunk_markdown(REGULATIONS_DIR / "cargo_classification_guide.md")
    headings = [c["heading"] for c in chunks]
    assert headings == [
        "General Freight", "Perishable Goods", "Hazardous Materials", "Oversized Loads",
    ]
    for i, chunk in enumerate(chunks):
        assert chunk["chunk_index"] == i
        assert "#" not in chunk["content"]


def test_chunk_content_does_not_include_heading_line():
    chunks = chunk_markdown(REGULATIONS_DIR / "exemptions.md")
    diplomatic = next(c for c in chunks if c["heading"] == "DIPLOMATIC")
    assert "100%" in diplomatic["content"]
    assert "## DIPLOMATIC" not in diplomatic["content"]
