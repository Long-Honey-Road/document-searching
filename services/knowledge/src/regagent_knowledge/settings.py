"""Настройки knowledge (dev_docs/06-contracts.md#переменные-env)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from regagent_common.settings import CommonSettings

SearchMode = Literal["naive", "local", "global", "hybrid", "mix"]


class KnowledgeSettings(CommonSettings):
    postgres_host: str
    postgres_port: int = 5432
    knowledge_db: str
    knowledge_db_user: str
    knowledge_db_password: SecretStr

    chunk_max_tokens: int = 1200
    search_top_k: int = 8
    search_mode: SearchMode = "mix"

    lightrag_working_dir: Path = Path("/data/lightrag")
    lightrag_extract_gleaning: int = 1


def configure_postgres_env(settings: KnowledgeSettings) -> None:
    """LightRAG читает подключение к Postgres из ``POSTGRES_*`` процесса (D-32)."""
    os.environ["POSTGRES_HOST"] = settings.postgres_host
    os.environ["POSTGRES_PORT"] = str(settings.postgres_port)
    os.environ["POSTGRES_USER"] = settings.knowledge_db_user
    os.environ["POSTGRES_PASSWORD"] = settings.knowledge_db_password.get_secret_value()
    os.environ["POSTGRES_DATABASE"] = settings.knowledge_db
