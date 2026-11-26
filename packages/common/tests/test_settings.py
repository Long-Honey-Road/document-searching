import pytest
from pydantic import ValidationError
from regagent_common.settings import CommonSettings

REQUIRED = {
    "LLM_BASE_URL": "http://llm:8000/v1",
    "LLM_MODEL": "m",
    "EMBEDDINGS_BASE_URL": "http://emb:80/v1",
    "EMBEDDINGS_MODEL": "bge-m3",
    "EMBEDDINGS_DIM": "1024",
    "RERANK_BASE_URL": "http://rerank:80",
    "RERANK_MODEL": "bge-reranker-v2-m3",
}


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key, value in REQUIRED.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def test_model_for_falls_back_to_default(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("LLM_MODEL", "m")
    base_env.setenv("LLM_GUARD_MODEL", "")
    assert CommonSettings(_env_file=None).model_for("guard") == "m"


def test_model_for_uses_role_override(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("LLM_EXTRACT_MODEL", "big")
    s = CommonSettings(_env_file=None)
    assert s.model_for("extract") == "big"
    assert s.model_for("agent") == "m"


def test_extra_body_json_parsed(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("LLM_EXTRA_BODY_JSON", '{"chat_template_kwargs":{"enable_thinking":false}}')
    assert CommonSettings(_env_file=None).llm_extra_body_json == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_extra_body_json_empty_means_no_extra(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("LLM_EXTRA_BODY_JSON", "")
    assert CommonSettings(_env_file=None).llm_extra_body_json == {}


def test_api_key_not_leaked_in_repr(base_env: pytest.MonkeyPatch) -> None:
    base_env.setenv("LLM_API_KEY", "super-secret")
    s = CommonSettings(_env_file=None)
    assert "super-secret" not in repr(s)
    assert s.llm_api_key.get_secret_value() == "super-secret"


def test_missing_llm_address_fails_fast(base_env: pytest.MonkeyPatch) -> None:
    base_env.delenv("LLM_BASE_URL")
    with pytest.raises(ValidationError):
        CommonSettings(_env_file=None)


def test_defaults_match_env_example(base_env: pytest.MonkeyPatch) -> None:
    s = CommonSettings(_env_file=None)
    assert s.llm_timeout_s == 120
    assert s.llm_max_concurrency == 8
    assert s.rerank_min_score == 0.2
    assert s.embeddings_dim == 1024
