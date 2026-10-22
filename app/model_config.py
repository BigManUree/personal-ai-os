"""OpenAI-compatible model configuration for Personal AI OS.

The module contains configuration only. It never performs a network request and
never logs or persists the API key. Callers read configuration at request time so
tests can safely monkeypatch environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


@dataclass(frozen=True)
class ModelConfig:
    api_key: str
    base_url: str
    model: str

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def load_model_config() -> ModelConfig:
    """Read current OpenAI-compatible environment variables.

    Values are intentionally read on every call so process lifetime is not tied
    to environment changes made by tests or by a future configuration command.
    """
    return ModelConfig(
        api_key=os.getenv("LLM_API_KEY", "").strip(),
        base_url=os.getenv("LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        model=os.getenv("LLM_MODEL", DEFAULT_MODEL),
    )
