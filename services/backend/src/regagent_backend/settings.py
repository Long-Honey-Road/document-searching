"""Настройки backend из `.env` (dev_docs/06-contracts.md#переменные-env)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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

    @field_validator("AREA_CODES", mode="before")
    @classmethod
    def _split_codes(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value
