from __future__ import annotations

from app.model_config import DEFAULT_BASE_URL, DEFAULT_MODEL, load_model_config


def test_model_config_defaults_to_empty_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    config = load_model_config()
    assert config.api_key == ""
    assert config.base_url == DEFAULT_BASE_URL
    assert config.model == DEFAULT_MODEL
    assert config.enabled is False
    assert config.authorization_header == {}
    assert config.chat_completions_url == f"{DEFAULT_BASE_URL}/chat/completions"


def test_model_config_reads_live_values_and_strips_slash(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    config = load_model_config()
    assert config.enabled is True
    assert config.base_url == "https://example.test/v1"
    assert config.model == "test-model"
    assert config.chat_completions_url == "https://example.test/v1/chat/completions"
    assert config.authorization_header == {"Authorization": "Bearer test-key"}


def test_pytest_database_is_isolated_from_development_db():
    from app import main

    assert str(main.DB_PATH) != ""
    assert main.DB_PATH.name != "personal_ai_os.db" or "paos-pytest" in str(main.DB_PATH)
