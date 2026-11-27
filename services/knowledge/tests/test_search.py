from __future__ import annotations

import pytest
from regagent_common.contracts import SearchRequest, SearchResponse
from regagent_knowledge.normalize import Clause, split_clauses
from regagent_knowledge.registry import DocumentRecord
from regagent_knowledge.search import search


def chunk(doc: str, area: str = "общий", level: str = "public") -> str:
    return f"⟦doc={doc} v=1 clause=2.1 area={area} level={level}⟧ Title — 2.1 Rule\n{doc} text"


class FakeRag:
    def __init__(self, hits: list[str]) -> None:
        self.levels = ["public", "closed_fin"]
        self.hits = hits
        self.queried: list[tuple[str, int]] = []

    async def query(self, level: str, query: str, top_k: int) -> list[str]:
        self.queried.append((level, top_k))
        return self.hits if level == "public" else []


class FakeReranker:
    def __init__(self, scores: list[float] | None = None) -> None:
        self.scores = scores
        self.texts: list[str] = []

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        self.texts.extend(texts)
        return self.scores if self.scores is not None else [0.75] * len(texts)


class SearchCase:
    def __init__(
        self,
        hits: list[str],
        *,
        levels: dict[str, str] | None = None,
        scores: list[float] | None = None,
    ) -> None:
        self.registry = levels or {}
        self.rag = FakeRag(hits)
        self.reranker = FakeReranker(scores)

    async def run(
        self,
        *,
        areas: list[str] | None = None,
        levels: list[str] | None = None,
        top_k: int | None = None,
    ) -> SearchResponse:
        async def lookup(doc_id: str) -> str | None:
            return self.registry.get(doc_id)

        return await search(
            SearchRequest(
                query="who approves",
                levels=["public"] if levels is None else levels,
                areas=areas or [],
                top_k=top_k,
            ),
            self.rag,
            lookup,
            self.reranker,
            0.2,
        )


@pytest.mark.leak
async def test_queries_only_requested_levels() -> None:
    case = SearchCase([])
    await case.run(levels=["public", "unknown", "public"], top_k=3)
    assert case.rag.queried == [("public", 3)]


@pytest.mark.leak
@pytest.mark.parametrize(
    ("hit", "registry"),
    [
        (chunk("foreign", level="closed_fin"), {"foreign": "closed_fin"}),
        (chunk("foreign"), {"foreign": "closed_fin"}),
        (chunk("foreign", level="closed_fin"), {"foreign": "public"}),
        (chunk("foreign"), {}),
        ("foreign text without a header", {}),
    ],
    ids=["foreign-workspace", "registry-mismatch", "header-mismatch", "unknown-doc", "no-header"],
)
async def test_foreign_or_invalid_fragment_is_dropped_before_rerank(
    hit: str, registry: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    case = SearchCase([hit], levels=registry)
    result = await case.run(levels=["public", "closed_fin"])
    assert result.fragments == []
    assert case.reranker.texts == []
    assert "security" in caplog.text
    assert "foreign text" not in caplog.text


async def test_area_filter_keeps_common() -> None:
    areas = {"dib": "ДИБ", "dbrct": "ДБРЦТ", "general": "общий"}
    case = SearchCase(
        [chunk(doc, area) for doc, area in areas.items()],
        levels=dict.fromkeys(areas, "public"),
    )
    response = await case.run(areas=["ДИБ"])
    assert [f.doc_id for f in response.fragments] == ["dib", "general"]
    assert response.areas_found == ["ДИБ", "общий"]
    assert not response.areas_diverge


async def test_areas_diverge_when_multiple_specific_areas_match() -> None:
    case = SearchCase(
        [chunk("dib", "ДИБ"), chunk("dbrct", "ДБРЦТ")],
        levels={"dib": "public", "dbrct": "public"},
    )
    response = await case.run(top_k=1)
    assert len(response.fragments) == 1
    assert response.areas_found == ["ДБРЦТ", "ДИБ"]
    assert response.areas_diverge


async def test_low_confidence_when_all_below_threshold() -> None:
    case = SearchCase([chunk("a")], levels={"a": "public"}, scores=[0.1])
    response = await case.run()
    assert response.fragments == []
    assert response.low_confidence


async def test_returns_best_unique_fragment_and_preserves_title_and_text() -> None:
    case = SearchCase(
        [chunk("a"), chunk("a"), chunk("b")],
        levels={"a": "public", "b": "public"},
        scores=[0.25, 0.9],
    )
    response = await case.run(top_k=1)
    (fragment,) = response.fragments
    assert (fragment.fragment_id, fragment.title, fragment.text, fragment.score) == (
        "b@1#2.1",
        "Title",
        "2.1 Rule\nb text",
        0.9,
    )


async def test_search_preserves_clause_heading_for_rerank_and_answer() -> None:
    case = SearchCase(
        [
            "⟦doc=a v=1 clause=2.1 area=общий level=public⟧"
            " Approval Rules — 2.1 Finance approves the request\nWithin two days."
        ],
        levels={"a": "public"},
    )
    response = await case.run()
    (fragment,) = response.fragments
    assert fragment.title == "Approval Rules"
    assert fragment.text == "2.1 Finance approves the request\nWithin two days."
    assert case.reranker.texts == [fragment.text]


@pytest.mark.leak
async def test_access_revoked_during_rerank_is_not_returned() -> None:
    levels = iter(["public", None])

    async def lookup(doc_id: str) -> str | None:
        return next(levels)

    response = await search(
        SearchRequest(query="who approves", levels=["public"]),
        FakeRag([chunk("revoked", "ДИБ")]),
        lookup,
        FakeReranker(),
        0.2,
    )
    assert response.fragments == []
    assert response.areas_found == []


async def test_search_offers_exact_subclause_from_authorized_section() -> None:
    async def lookup(doc_id: str) -> str | None:
        return "public"

    rag = FakeRag(
        [
            "⟦doc=rules v=1 clause=2 area=ДИБ level=public⟧ Регламент — 2 Проекты\n\n"
            "2.1. Подрядчика выбирает комиссия.\n\n"
            "2.7. Координатор назначает руководителя проекта за рабочий день."
        ]
    )
    response = await search(
        SearchRequest(query="Кто назначает руководителя проекта?", levels=["public"], top_k=2),
        rag,
        lookup,
        FakeReranker(),
        0.2,
    )
    source = response.fragments[0]
    assert source.fragment_id == "rules@1#2.7"
    assert source.area == "ДИБ"
    assert "рабочий день" in source.text
    assert "подрядчик" not in source.text.casefold()


async def test_search_finds_exact_clause_outside_recalled_section() -> None:
    record = DocumentRecord("rules", 1, "test", "public", ["ДИБ"], "Регламент", "indexed", None)

    async def lookup(doc_id: str) -> DocumentRecord:
        return record

    async def load_clauses(indexed: DocumentRecord) -> list[Clause]:
        assert indexed == record
        return split_clauses(
            "2.4. SS запрашивает спецификацию на ПО/HW.\n\n"
            "6.5. Архитектор организует подготовку расчёта."
        )

    rag = FakeRag(
        [
            "⟦doc=rules v=1 clause=6 area=ДИБ level=public⟧ Регламент — 6 Взаимодействие\n\n"
            "6.5. Архитектор организует подготовку расчёта."
        ]
    )
    response = await search(
        SearchRequest(query="Кто запрашивает спецификацию на ПО?", levels=["public"]),
        rag,
        lookup,
        FakeReranker(),
        0.2,
        indexed_clauses=load_clauses,
    )
    assert response.fragments[0].fragment_id == "rules@1#2.4"


async def test_search_keeps_regulation_acronyms_when_finding_subclauses() -> None:
    async def lookup(doc_id: str) -> str | None:
        return "public"

    rag = FakeRag(
        [
            "⟦doc=rules v=1 clause=2 area=ДИБ level=public⟧ Регламент — 2 Проекты\n\n"
            "2.1. Логист готовит поставку.\n\n2.7. РП готовит поставку."
        ]
    )
    response = await search(
        SearchRequest(query="Что делает РП?", levels=["public"]),
        rag,
        lookup,
        FakeReranker(),
        0.2,
    )
    assert response.fragments[0].fragment_id == "rules@1#2.7"


async def test_rerank_uses_relevant_excerpt_but_returns_the_full_clause() -> None:
    async def lookup(doc_id: str) -> str | None:
        return "public"

    text = "Вводная часть. " * 100 + "Координатор назначает руководителя проекта."
    ranker = FakeReranker()
    response = await search(
        SearchRequest(query="Кто назначает руководителя проекта?", levels=["public"]),
        FakeRag(
            ["⟦doc=rules v=1 clause=2.7 area=ДИБ level=public⟧ Регламент — 2.7 Правило\n" + text]
        ),
        lookup,
        ranker,
        0.2,
        rerank_text_max_chars=150,
    )
    assert len(ranker.texts[0]) <= 150
    assert "Координатор назначает" in ranker.texts[0]
    assert response.fragments[0].text.endswith(text)


async def test_search_returns_verified_practice_with_its_visible_regulation() -> None:
    from regagent_common.contracts import PracticeBinding, PracticeSource
    from regagent_common.fragments import FragmentHeader

    raw = (
        "⟦practice=p1 doc=example v=1 clause=2.1 mode=overrides area=общий level=public⟧"
        " Новое правило"
    )
    practice = PracticeSource(
        mode="overrides", binds=[PracticeBinding(doc_id="example", version=1, clause="2.1")]
    )
    row = DocumentRecord("example", 1, "hash", "public", ["общий"], "Правила", "indexed", None)

    async def lookup(_: str) -> DocumentRecord:
        return row

    async def clauses(_: DocumentRecord) -> list[Clause]:
        return split_clauses("2.1. Базовое правило.")

    async def verify(_: FragmentHeader, chunk_text: str) -> PracticeSource | None:
        return practice if chunk_text == raw else None

    result = await search(
        SearchRequest(query="правило", levels=["public"], top_k=2),
        FakeRag([raw, raw + " подмена"]),
        lookup,
        FakeReranker(),
        0.2,
        indexed_clauses=clauses,
        practice_lookup=verify,
    )
    assert [fragment.fragment_id for fragment in result.fragments] == [
        "practice:p1",
        "example@1#2.1",
    ]
    assert result.fragments[0].practice == practice
    assert result.fragments[1].practice is None
