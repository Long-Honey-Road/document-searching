import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from regagent_knowledge.lightrag_factory import build_lightrag
from regagent_knowledge.settings import KnowledgeSettings, configure_postgres_env


def test_settings_defaults(knowledge_env: pytest.MonkeyPatch) -> None:
    s = KnowledgeSettings(_env_file=None)
    assert s.chunk_max_tokens == 1200
    assert s.search_top_k == 8
    assert s.search_mode == "mix"
    assert s.knowledge_db_password.get_secret_value() == "secret"


def test_knowledge_db_is_required(knowledge_env: pytest.MonkeyPatch) -> None:
    knowledge_env.delenv("KNOWLEDGE_DB_USER")
    with pytest.raises(ValidationError):
        KnowledgeSettings(_env_file=None)


def test_unknown_search_mode_rejected(knowledge_env: pytest.MonkeyPatch) -> None:
    knowledge_env.setenv("SEARCH_MODE", "bypass")
    with pytest.raises(ValidationError):
        KnowledgeSettings(_env_file=None)


def test_postgres_env_for_lightrag(knowledge_env: pytest.MonkeyPatch) -> None:
    configure_postgres_env(KnowledgeSettings(_env_file=None))
    assert os.environ["POSTGRES_USER"] == "knowledge"
    assert os.environ["POSTGRES_PASSWORD"] == "secret"  # noqa: S105
    assert os.environ["POSTGRES_DATABASE"] == "knowledge"
    assert os.environ["POSTGRES_HOST"] == "postgres"
    assert "POSTGRES_WORKSPACE" not in os.environ


def test_factory_wires_lightrag(
    knowledge_env: pytest.MonkeyPatch,
    tmp_path: Path,
    llm_func: Any,
    embedding_func: Any,
    tokenizer: Any,
) -> None:
    knowledge_env.setenv("LIGHTRAG_WORKING_DIR", str(tmp_path))
    knowledge_env.setenv("LIGHTRAG_EXTRACT_GLEANING", "0")
    s = KnowledgeSettings(_env_file=None)
    rag = build_lightrag(
        "lvl_public",
        s,
        llm_func=llm_func,
        embedding_func=embedding_func,
        tokenizer=tokenizer,
    )
    assert rag.workspace == "lvl_public"
    assert rag.kv_storage == "PGKVStorage"
    assert rag.vector_storage == "PGVectorStorage"
    assert rag.graph_storage == "PGGraphStorage"
    assert rag.doc_status_storage == "PGDocStatusStorage"
    assert rag.entity_extract_max_gleaning == 0
    assert rag.llm_model_max_async == s.llm_max_concurrency
    # LightRAG не дорезает наши фрагменты: его предел не меньше нашего (D-28).
    assert rag.chunk_token_size is not None and rag.chunk_token_size >= s.chunk_max_tokens
