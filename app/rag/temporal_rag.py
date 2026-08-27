"""Search over regulation_chunks with a temporal pre-filter: only chunks
valid on the given date are candidates for the vector search at all, so
"which rule version applied on this date" is resolved deterministically by
SQL, not left for embedding similarity to guess between two dated versions
of the same clause.
"""

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.embeddings import embed_query


def search_regulations(db: Session, query: str, as_of_date: date, template_id: int, top_k: int = 5) -> list[dict]:
    query_embedding = embed_query(query)
    rows = db.execute(
        text("""
            SELECT doc_path, chunk_index, heading, content, valid_from, valid_to,
                   1 - (embedding <=> CAST(:query_embedding AS vector)) AS similarity
            FROM regulation_chunks
            WHERE template_id = :template_id
              AND valid_from <= :as_of_date
              AND (valid_to IS NULL OR valid_to >= :as_of_date)
              AND embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:query_embedding AS vector)
            LIMIT :top_k
        """),
        {"query_embedding": str(query_embedding), "as_of_date": as_of_date,
         "template_id": template_id, "top_k": top_k},
    ).mappings().fetchall()
    return [
        {
            "citation_id": f"{row['doc_path']}#{row['chunk_index']}",
            "doc_path": row["doc_path"],
            "chunk_index": row["chunk_index"],
            "heading": row["heading"],
            "content": row["content"],
            "valid_from": row["valid_from"].isoformat(),
            "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
            "similarity": round(float(row["similarity"]), 4),
        }
        for row in rows
    ]
