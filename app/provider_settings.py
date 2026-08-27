"""Storage and connection-testing for hosted-provider credentials (the
Settings tab). Deliberately not wired into Phase 1's pipeline — Phase 1 is
deterministic by design and has no LLM in its path. This is groundwork for
Phase 2 and for regenerating regulation-chunk embeddings with a hosted
model instead of local Ollama.
"""

import litellm
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.crypto import decrypt, encrypt

PROVIDERS = ["openai", "anthropic", "gemini", "mistral", "deepseek"]

# LiteLLM model strings are provider-prefixed; the UI only asks for the
# bare model name (e.g. "gpt-4o"), this maps it to what LiteLLM expects.
LITELLM_PREFIX = {
    "openai": "openai", "anthropic": "anthropic", "gemini": "gemini",
    "mistral": "mistral", "deepseek": "deepseek",
}

DEFAULT_MODEL_HINT = {
    "openai": "gpt-4o", "anthropic": "claude-sonnet-5", "gemini": "gemini-2.5-pro",
    "mistral": "mistral-large-latest", "deepseek": "deepseek-chat",
}


class UnknownProviderError(Exception):
    pass


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise UnknownProviderError(f"unknown provider {provider!r} — expected one of {PROVIDERS}")


def list_providers(db: Session) -> list[dict]:
    rows = db.execute(
        text("SELECT provider, model, api_key, updated_at FROM provider_settings")
    ).mappings().fetchall()
    configured = {row["provider"]: row for row in rows}
    return [
        {
            "provider": p,
            "configured": p in configured and bool(configured[p]["api_key"]),
            "model": configured[p]["model"] if p in configured else None,
            "model_hint": DEFAULT_MODEL_HINT[p],
            "updated_at": configured[p]["updated_at"].isoformat() if p in configured else None,
        }
        for p in PROVIDERS
    ]


def upsert_provider(db: Session, provider: str, api_key: str, model: str) -> None:
    _check_provider(provider)
    db.execute(
        text("""
            INSERT INTO provider_settings (provider, api_key, model, updated_at)
            VALUES (:provider, :api_key, :model, now())
            ON CONFLICT (provider) DO UPDATE
            SET api_key = EXCLUDED.api_key, model = EXCLUDED.model, updated_at = now()
        """),
        {"provider": provider, "api_key": encrypt(api_key), "model": model},
    )
    db.commit()


def delete_provider(db: Session, provider: str) -> bool:
    _check_provider(provider)
    result = db.execute(
        text("DELETE FROM provider_settings WHERE provider = :provider"), {"provider": provider}
    )
    db.commit()
    return result.rowcount > 0


def get_provider_credentials(db: Session, provider: str) -> tuple[str, str] | None:
    _check_provider(provider)
    row = db.execute(
        text("SELECT api_key, model FROM provider_settings WHERE provider = :provider"),
        {"provider": provider},
    ).fetchone()
    if row is None or not row.api_key:
        return None
    return decrypt(row.api_key), (row.model or DEFAULT_MODEL_HINT[provider])


def test_connection(db: Session, provider: str) -> dict:
    """Makes one minimal real completion call with the stored credentials.
    Returns {ok, message} — never raises for a bad key/model, that's a
    normal 'ok: false' result, not a server error."""
    _check_provider(provider)
    creds = get_provider_credentials(db, provider)
    if creds is None:
        return {"ok": False, "message": "no API key saved for this provider"}
    api_key, model = creds
    litellm_model = f"{LITELLM_PREFIX[provider]}/{model}"
    try:
        litellm.completion(
            model=litellm_model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            api_key=api_key,
            max_tokens=5,
            timeout=15,
        )
        return {"ok": True, "message": f"connected — {litellm_model} responded"}
    except litellm.AuthenticationError:
        return {"ok": False, "message": "authentication failed — check the API key"}
    except litellm.NotFoundError:
        return {"ok": False, "message": f"model not found: {model}"}
    except litellm.RateLimitError:
        return {"ok": False, "message": "rate limited by the provider — try again shortly"}
    except litellm.Timeout:
        return {"ok": False, "message": "request timed out"}
    except Exception:
        # Deliberately generic — the underlying exception can echo request
        # details (sometimes including fragments of the key, depending on
        # the provider SDK), and that's not safe to forward to the browser.
        return {"ok": False, "message": "connection failed — could not reach the provider"}
