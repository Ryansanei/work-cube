"""Real Postgres for storage, litellm.completion mocked for test_connection —
same boundary as the rest of the suite: only the external API call is mocked.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.provider_settings import (
    UnknownProviderError,
    delete_provider,
    get_provider_credentials,
    list_providers,
    upsert_provider,
)
from app.provider_settings import test_connection as check_connection


@pytest.fixture(autouse=True)
def clean_provider_settings(db):
    db.execute(text("DELETE FROM provider_settings WHERE provider = 'anthropic'"))
    db.commit()
    yield
    db.execute(text("DELETE FROM provider_settings WHERE provider = 'anthropic'"))
    db.commit()


def test_list_providers_includes_all_five_unconfigured_by_default(db):
    providers = {p["provider"]: p for p in list_providers(db)}
    assert set(providers) == {"openai", "anthropic", "gemini", "mistral", "deepseek"}
    assert providers["anthropic"]["configured"] is False
    assert providers["anthropic"]["model_hint"] == "claude-sonnet-5"


def test_upsert_then_list_shows_configured_without_exposing_the_key(db):
    upsert_provider(db, "anthropic", "sk-ant-real-secret", "claude-sonnet-5")
    providers = {p["provider"]: p for p in list_providers(db)}
    assert providers["anthropic"]["configured"] is True
    assert providers["anthropic"]["model"] == "claude-sonnet-5"
    assert "api_key" not in providers["anthropic"]
    assert "sk-ant-real-secret" not in str(providers["anthropic"])


def test_api_key_is_encrypted_at_rest_not_plaintext(db):
    upsert_provider(db, "anthropic", "sk-ant-real-secret", "claude-sonnet-5")
    raw = db.execute(
        text("SELECT api_key FROM provider_settings WHERE provider = 'anthropic'")
    ).scalar_one()
    assert raw != "sk-ant-real-secret"
    assert "sk-ant-real-secret" not in raw
    # and it still round-trips back to the real key through the app's own API
    assert get_provider_credentials(db, "anthropic")[0] == "sk-ant-real-secret"


def test_upsert_is_idempotent_and_updates_in_place(db):
    upsert_provider(db, "anthropic", "sk-ant-first", "claude-sonnet-5")
    upsert_provider(db, "anthropic", "sk-ant-second", "claude-opus-5")
    creds = get_provider_credentials(db, "anthropic")
    assert creds == ("sk-ant-second", "claude-opus-5")


def test_delete_provider_removes_it(db):
    upsert_provider(db, "anthropic", "sk-ant-x", "claude-sonnet-5")
    assert delete_provider(db, "anthropic") is True
    assert get_provider_credentials(db, "anthropic") is None
    assert delete_provider(db, "anthropic") is False


def test_unknown_provider_rejected_by_every_public_function(db):
    with pytest.raises(UnknownProviderError):
        upsert_provider(db, "not_a_real_provider", "key", "model")
    with pytest.raises(UnknownProviderError):
        delete_provider(db, "not_a_real_provider")
    with pytest.raises(UnknownProviderError):
        get_provider_credentials(db, "not_a_real_provider")


def test_connection_with_no_saved_key_fails_without_calling_litellm(db):
    with patch("app.provider_settings.litellm.completion") as mock_completion:
        result = check_connection(db, "anthropic")
    assert result["ok"] is False
    assert "no api key" in result["message"].lower()
    mock_completion.assert_not_called()


def test_connection_success_is_reported_ok(db):
    upsert_provider(db, "anthropic", "sk-ant-x", "claude-sonnet-5")
    with patch("app.provider_settings.litellm.completion") as mock_completion:
        mock_completion.return_value = object()
        result = check_connection(db, "anthropic")
    assert result["ok"] is True
    assert "anthropic/claude-sonnet-5" in result["message"]
    mock_completion.assert_called_once()
    assert mock_completion.call_args.kwargs["api_key"] == "sk-ant-x"


def test_connection_failure_is_reported_gracefully_not_raised(db):
    upsert_provider(db, "anthropic", "sk-ant-bad", "claude-sonnet-5")
    with patch("app.provider_settings.litellm.completion", side_effect=RuntimeError("bad key")):
        result = check_connection(db, "anthropic")
    assert result["ok"] is False
    # The raw exception must never reach the caller — it can echo request
    # details, sometimes including key fragments, from the provider SDK.
    assert "bad key" not in result["message"]
    assert result["message"] == "connection failed — could not reach the provider"
