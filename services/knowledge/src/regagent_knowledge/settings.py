"""Настройки knowledge (dev_docs/06-contracts.md#переменные-env)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from regagent_common.settings import CommonSettings

SearchMode = Literal["naive", "local", "global", "hybrid", "mix"]
DEFAULT_SEARCH_TOP_K = 8
DEFAULT_RERANK_CANDIDATE_LIMIT = 24
DEFAULT_RERANK_TEXT_MAX_CHARS = 384


class KnowledgeSettings(CommonSettings):
    postgres_host: str
    postgres_port: int = 5432
    knowledge_db: str
    knowledge_db_user: str
    knowledge_db_password: SecretStr

    chunk_max_tokens: int = 1200
    search_top_k: int = Field(default=DEFAULT_SEARCH_TOP_K, ge=1)
    rerank_mode: Literal["embeddings", "tei"] = "embeddings"
    rerank_candidate_limit: int = Field(default=DEFAULT_RERANK_CANDIDATE_LIMIT, ge=1)
    rerank_text_max_chars: int = Field(default=DEFAULT_RERANK_TEXT_MAX_CHARS, ge=128)
    search_mode: SearchMode = "mix"

    lightrag_working_dir: Path = Path("/data/lightrag")
    lightrag_extract_gleaning: int = 1
    lightrag_language: str = Field(default="Russian", min_length=1)
    lightrag_extraction_json: bool = False

    docs_dir: Path = Path("/data/docs")
    okf_dir: Path = Path("/data/okf")
    access_levels_file: Path = Path("config/access_levels.yaml")
    watch_debounce_s: float = Field(default=3, ge=0)
    index_max_retries: int = Field(default=3, ge=0)
    index_job_timeout_s: float = Field(default=1800, gt=0, allow_inf_nan=False)


class RuntimeSettings(KnowledgeSettings):
    rabbitmq_url: SecretStr
    knowledge_internal_token: SecretStr


def configure_postgres_env(settings: KnowledgeSettings) -> None:
    """LightRAG читает подключение к Postgres из ``POSTGRES_*`` процесса (D-32)."""
    os.environ["POSTGRES_HOST"] = settings.postgres_host
    os.environ["POSTGRES_PORT"] = str(settings.postgres_port)
    os.environ["POSTGRES_USER"] = settings.knowledge_db_user
    os.environ["POSTGRES_PASSWORD"] = settings.knowledge_db_password.get_secret_value()
    os.environ["POSTGRES_DATABASE"] = settings.knowledge_db
