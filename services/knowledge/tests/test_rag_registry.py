from pathlib import Path
from typing import Any

import pytest
from regagent_knowledge.rag import (
    FRAGMENT_SEPARATOR,
    SENTINEL_WORKSPACE,
    RagError,
    RagRegistry,
    UnknownLevelError,
    WorkspaceOverrideError,
    assert_no_workspace_override,
)


class FakeDocStatus:
    def __init__(self) -> None:
        self.status: dict[str, str] = {}

    async def get_by_id(self, doc_id: str) -> dict[str, Any] | None:
        if doc_id not in self.status:
            return None
        return {"status": self.status[doc_id], "error_msg": "boom"}


class FakeRag:
    def __init__(self, workspace: str, log: list[tuple[Any, ...]]) -> None:
        self.workspace = workspace
        self.log = log
        self.doc_status = FakeDocStatus()
        self.insert_status = "processed"
        self.query_result: dict[str, Any] = {
            "status": "success",
            "data": {
                "entities": [{"entity_name": "РП", "description": "без заголовка"}],
                "relationships": [],
                "chunks": [{"content": "⟦doc=a v=1 clause=1 area=ДИБ level=public⟧ текст"}],
            },
        }

    async def initialize_storages(self) -> None:
        self.log.append(("init", self.workspace))

    async def finalize_storages(self) -> None:
        self.log.append(("finalize", self.workspace))

    async def ainsert(self, input: str, **kwargs: Any) -> str:
        self.log.append(("insert", self.workspace, input, kwargs))
        self.doc_status.status[kwargs["ids"]] = self.insert_status
        return "track"

    async def adelete_by_doc_id(self, doc_id: str) -> Any:
        self.log.append(("delete", self.workspace, doc_id))
        self.doc_status.status.pop(doc_id, None)

    async def aquery_data(self, query: str, param: Any) -> dict[str, Any]:
        self.log.append(("query", self.workspace, query, param))
        return self.query_result


@pytest.fixture
def setup() -> tuple[RagRegistry, list[tuple[Any, ...]], dict[str, FakeRag]]:
    log: list[tuple[Any, ...]] = []
    made: dict[str, FakeRag] = {}

    def factory(ws: str) -> FakeRag:
        made[ws] = FakeRag(ws, log)
        log.append(("factory", ws))
        return made[ws]

    return RagRegistry(["public", "closed_fin"], factory, mode="mix"), log, made


async def test_sentinel_initialized_first(setup: Any) -> None:
    reg, log, _ = setup
    await reg.start()
    assert log[0] == ("factory", SENTINEL_WORKSPACE)
    assert log[1] == ("init", SENTINEL_WORKSPACE)
    assert [e[1] for e in log if e[0] == "init"] == [
        SENTINEL_WORKSPACE,
        "lvl_public",
        "lvl_closed_fin",
    ]


async def test_unknown_level_raises(setup: Any) -> None:
    reg, _, _ = setup
    await reg.start()
    with pytest.raises(UnknownLevelError):
        await reg.query("nope", "вопрос", 5)
    with pytest.raises(UnknownLevelError):
        reg.workspace("_sentinel")


def test_workspace_naming(setup: Any) -> None:
    reg, _, _ = setup
    assert reg.workspace("closed_fin") == "lvl_closed_fin"


async def test_insert_replaces_and_splits_by_our_separator(setup: Any) -> None:
    reg, log, _ = setup
    await reg.start()
    await reg.insert("public", "x@v4", ["⟦a⟧ раз", "⟦b⟧ два"])
    ops = [e for e in log if e[0] in ("delete", "insert")]
    assert ops[0] == ("delete", "lvl_public", "x@v4")
    _, ws, text, kwargs = ops[1]
    assert ws == "lvl_public"
    assert text == "⟦a⟧ раз" + FRAGMENT_SEPARATOR + "⟦b⟧ два"
    assert kwargs["split_by_character"] == FRAGMENT_SEPARATOR
    assert kwargs["split_by_character_only"] is True
    assert kwargs["ids"] == "x@v4"


@pytest.mark.parametrize("chunks", [[], ["ok", ""], ["ok", "внутри" + FRAGMENT_SEPARATOR + "x"]])
async def test_insert_rejects_bad_chunks(setup: Any, chunks: list[str]) -> None:
    reg, log, _ = setup
    await reg.start()
    with pytest.raises(ValueError):
        await reg.insert("public", "x@v1", chunks)
    assert not [e for e in log if e[0] == "insert"]


async def test_insert_raises_when_lightrag_marks_document_failed(setup: Any) -> None:
    reg, _, made = setup
    await reg.start()
    made["lvl_public"].insert_status = "failed"
    with pytest.raises(RagError, match="boom"):
        await reg.insert("public", "x@v1", ["текст"])


async def test_delete_goes_to_own_workspace(setup: Any) -> None:
    reg, log, _ = setup
    await reg.start()
    await reg.delete("closed_fin", "x@v1")
    assert ("delete", "lvl_closed_fin", "x@v1") in log
    assert not [e for e in log if e[0] == "delete" and e[1] != "lvl_closed_fin"]


async def test_query_returns_only_chunk_texts(setup: Any) -> None:
    reg, log, _ = setup
    await reg.start()
    texts = await reg.query("public", "кто назначает РП", 5, keywords=["РП"])
    assert texts == ["⟦doc=a v=1 clause=1 area=ДИБ level=public⟧ текст"]
    param = next(e for e in log if e[0] == "query")[3]
    assert param.mode == "mix"
    assert param.chunk_top_k == 5
    assert param.hl_keywords == ["РП"] and param.ll_keywords == ["РП"]
    assert param.enable_rerank is False


async def test_query_without_keywords_uses_query_as_keyword(setup: Any) -> None:
    # Ключевые слова всегда снаружи: LightRAG не должен звать LLM на их извлечение (D-29).
    reg, log, _ = setup
    await reg.start()
    await reg.query("public", "кто назначает РП", 5)
    param = next(e for e in log if e[0] == "query")[3]
    assert param.hl_keywords == ["кто назначает РП"]


async def test_query_no_results_is_empty(setup: Any) -> None:
    # aquery_data отвечает status=failure только когда ничего не нашлось; ошибки — исключения.
    reg, _, made = setup
    await reg.start()
    made["lvl_public"].query_result = {
        "status": "failure",
        "message": "Query returned no results",
        "data": {},
    }
    assert await reg.query("public", "вопрос", 5) == []


async def test_query_unknown_status_raises(setup: Any) -> None:
    reg, _, made = setup
    await reg.start()
    made["lvl_public"].query_result = {"status": "weird", "message": "?"}
    with pytest.raises(RagError):
        await reg.query("public", "вопрос", 5)


async def test_operations_before_start_raise(setup: Any) -> None:
    reg, _, _ = setup
    with pytest.raises(RagError):
        await reg.query("public", "вопрос", 5)


async def test_stop_finalizes_everything(setup: Any) -> None:
    reg, log, _ = setup
    await reg.start()
    await reg.stop()
    assert {e[1] for e in log if e[0] == "finalize"} == {
        SENTINEL_WORKSPACE,
        "lvl_public",
        "lvl_closed_fin",
    }


async def test_start_refuses_workspace_override(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg, log, _ = setup
    monkeypatch.setenv("POSTGRES_WORKSPACE", "shared")
    with pytest.raises(WorkspaceOverrideError):
        await reg.start()
    assert log == []


def test_override_check_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_WORKSPACE", raising=False)
    assert_no_workspace_override()
    monkeypatch.setenv("POSTGRES_WORKSPACE", "")
    with pytest.raises(WorkspaceOverrideError):
        assert_no_workspace_override()


def test_override_check_config_ini(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("POSTGRES_WORKSPACE", raising=False)
    (tmp_path / "config.ini").write_text("[other]\nx=1\n", encoding="utf-8")
    assert_no_workspace_override()
    (tmp_path / "config.ini").write_text("[postgres]\nworkspace = shared\n", encoding="utf-8")
    with pytest.raises(WorkspaceOverrideError):
        assert_no_workspace_override()
