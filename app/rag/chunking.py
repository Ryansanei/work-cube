from pathlib import Path


def chunk_markdown(path: Path) -> list[dict]:
    """Splits a markdown file into one chunk per H2 section, dropping the H1
    title line. Mirrors the chunking approach used for Cloud Cost Advisor's
    best-practice docs, applied here to regulation text instead."""
    lines = path.read_text().splitlines()
    chunks = []
    heading = None
    body: list[str] = []
    chunk_index = 0

    def flush():
        nonlocal heading, body, chunk_index
        if heading is not None and body:
            chunks.append({
                "chunk_index": chunk_index,
                "heading": heading,
                "content": "\n".join(body).strip(),
            })
            chunk_index += 1
        body = []

    for line in lines:
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
        elif line.startswith("# "):
            continue
        else:
            body.append(line)
    flush()
    return chunks
