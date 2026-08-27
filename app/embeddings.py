import litellm

from app.config import settings


def embed_texts(texts: list[str]) -> list[list[float]]:
    response = litellm.embedding(
        model=settings.work_cube_embedding_model,
        input=texts,
        api_base=settings.ollama_api_base,
    )
    return [row["embedding"] for row in response.data]


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
