"""Настройки backend из `.env` (dev_docs/06-contracts.md#переменные-env)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    # OIDC: iss проверяем по внешнему адресу, ключи берём по внутреннему (D-17)
    KEYCLOAK_ISSUER: str
    KEYCLOAK_AUDIENCE: str
    KEYCLOAK_JWKS_URL: str
    JWKS_CACHE_TTL_S: float = 300
    JWKS_MIN_REFRESH_S: float = 30
    JWT_LEEWAY_S: float = 30

    ACCESS_LEVELS_FILE: Path = Path("config/access_levels.yaml")
    # Справочник кодов областей на этап 1 (D-20)
    AREA_CODES: Annotated[list[str], NoDecode] = ["ДИБ", "ДБРЦТ", "общий"]

    KNOWLEDGE_URL: str = "http://knowledge:8000"
    KNOWLEDGE_INTERNAL_TOKEN: SecretStr = SecretStr("")
    KNOWLEDGE_TIMEOUT_S: float = Field(default=30, gt=0)
    AGENT_MAX_STEPS: int = Field(default=12, ge=2)
    AGENT_MAX_SEARCHES: int = Field(default=3, ge=1)
    TOOL_RESULT_MAX_CHARS: int = Field(default=12000, ge=1)
    QUESTION_MAX_CHARS: int = Field(default=2000, ge=1)
    RATE_LIMIT_PER_USER_PER_MIN: int = Field(default=20, ge=1)
    MEMORY_MAX_ACTIVE_AREAS: int = Field(default=3, ge=1)
    MEMORY_AREA_TTL_DAYS: int = Field(default=14, ge=1)
    # D-55: прямых указаний области до записи в пустую память и подряд — до смены области
    MEMORY_AREA_SIGNALS_TO_REMEMBER: int = Field(default=2, ge=1)
    MEMORY_AREA_SIGNALS_TO_SWITCH: int = Field(default=3, ge=1)
    MEMORY_AREA_SIGNAL_WINDOW_DAYS: int = Field(default=14, ge=1)
    RABBITMQ_URL: SecretStr = SecretStr("")
    AUDIT_RETENTION_DAYS: int = Field(default=90, ge=1, le=3650)
    GUARD_MODE: Literal["json_schema", "tool"] = "json_schema"

    @field_validator("AREA_CODES", mode="before")
    @classmethod
    def _split_codes(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value
