"""Общие настройки из .env (dev_docs/06-contracts.md#переменные-env).

Адреса и модели обязательны: забытый адрес должен ронять сервис при старте, а не уводить
запросы на значение по умолчанию. Пустая переменная равна отсутствующей.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelRole = Literal["agent", "guard", "extract", "keyword", "judge"]


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    env: Literal["dev", "stand"] = "dev"
    log_level: str = "INFO"

    llm_base_url: str
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str
    llm_agent_model: str | None = None
    llm_guard_model: str | None = None
    llm_extract_model: str | None = None
    llm_keyword_model: str | None = None
    llm_judge_model: str | None = None
    llm_timeout_s: float = 120
    llm_max_concurrency: int = 8
    llm_extra_body_json: dict[str, Any] = {}

    embeddings_base_url: str
    embeddings_model: str
    embeddings_dim: int

    rerank_base_url: str
    rerank_model: str
    rerank_min_score: float = 0.2

    def model_for(self, role: ModelRole) -> str:
        override: str | None = getattr(self, f"llm_{role}_model")
        if override and override.strip():
            return override.strip()
        return self.llm_model
