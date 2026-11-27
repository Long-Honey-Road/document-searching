import pytest
from pydantic import ValidationError
from regagent_common.contracts import (
    AdminJob,
    AdminReview,
    AnswerMeta,
    Area,
    ClauseRequest,
    ClauseResponse,
    DocumentRef,
    Fragment,
    IndexJob,
    RefsRequest,
    RefsResponse,
    SearchRequest,
    SearchResponse,
)

# Примеры — из dev_docs/06-contracts.md
FRAGMENT = {
    "fragment_id": "dib-project-activity@3#2.7",
    "doc_id": "dib-project-activity",
    "version": 3,
    "title": "Проектная деятельность Дирекции ИБ",
    "clause": "2.7",
    "area": "ДИБ",
    "kind": "regulation",
    "text": "...",
    "score": 0.83,
}


def test_search_request_example_parses() -> None:
    req = SearchRequest.model_validate(
        {"query": "кто назначает РП", "levels": ["public"], "areas": ["ДИБ"], "top_k": 8}
    )
    assert req.areas == ["ДИБ"]


def test_search_request_defaults() -> None:
    req = SearchRequest(query="кто назначает РП", levels=["public"])
    assert req.areas == []
    assert req.top_k is None


def test_search_request_rejects_empty_query() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="", levels=["public"])


def test_search_response_example_roundtrip() -> None:
    body = {
        "fragments": [FRAGMENT],
        "areas_found": ["ДИБ", "ДБРЦТ", "общий"],
        "areas_diverge": True,
        "low_confidence": False,
    }
    resp = SearchResponse.model_validate(body)
    assert resp.fragments[0] == Fragment.model_validate(FRAGMENT)
    assert resp.model_dump() == body


def test_practice_fragment_may_have_no_version() -> None:
    f = Fragment.model_validate(
        {**FRAGMENT, "fragment_id": "practice:14", "version": None, "kind": "practice"}
    )
    assert f.version is None


def test_unknown_fragment_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        Fragment.model_validate({**FRAGMENT, "kind": "rumor"})


def test_clause_request_and_response() -> None:
    req = ClauseRequest.model_validate(
        {"doc_id": "dib-project-activity", "clause": "2.7", "version": None, "levels": ["public"]}
    )
    assert req.version is None
    resp = ClauseResponse.model_validate({"fragment": FRAGMENT, "neighbors": [FRAGMENT]})
    assert resp.neighbors[0].clause == "2.7"


def test_index_job_upsert_requires_path_and_sha() -> None:
    with pytest.raises(ValidationError):
        IndexJob(op="upsert", doc_id="x", version=1)


def test_index_job_delete_without_file() -> None:
    job = IndexJob(op="delete", doc_id="x", version=1)
    assert job.path is None


def test_index_job_envelope_fields() -> None:
    job = IndexJob(op="upsert", doc_id="x", version=1, path="a/x.docx", sha256="ab")
    dumped = job.model_dump(mode="json")
    assert dumped["type"] == "index.jobs"
    assert dumped["message_id"] and dumped["created_at"] and dumped["job_id"]
    assert IndexJob.model_validate(dumped) == job


def test_answer_meta_example_parses() -> None:
    meta = AnswerMeta.model_validate(
        {
            "verified": True,
            "area_used": "ДИБ",
            "area_source": "memory",
            "sources": [
                {
                    "n": 1,
                    "fragment_id": "dib-project-activity@3#2.7",
                    "title": "Проектная деятельность Дирекции ИБ",
                    "clause": "2.7",
                    "version": 3,
                    "kind": "regulation",
                }
            ],
            "outcome": "answer",
        }
    )
    assert meta.sources[0].n == 1


def test_answer_meta_rejects_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        AnswerMeta.model_validate(
            {
                "verified": False,
                "area_used": None,
                "area_source": "none",
                "sources": [],
                "outcome": "maybe",
            }
        )


def test_area_defaults() -> None:
    assert Area(code="общий", title="Общий").synonyms == []


def test_answer_meta_accepts_area_clarification() -> None:
    meta = AnswerMeta(
        verified=True, area_used=None, area_source="none", sources=[], outcome="ask_area"
    )
    assert meta.outcome == "ask_area"


def test_refs_request_and_response() -> None:
    req = RefsRequest.model_validate({"doc_id": "subcontract", "levels": ["public"]})
    assert req.levels == ["public"]
    with pytest.raises(ValidationError):
        RefsRequest.model_validate({"doc_id": "x", "levels": [], "user": "someone"})
    ref = {"doc_id": "margin", "title": "Регламент контроля маржинальности", "clause": "2.3"}
    resp = RefsResponse.model_validate(
        {"outgoing": [{**ref, "via": "регламентом контроля маржинальности"}], "incoming": []}
    )
    assert resp.outgoing == [DocumentRef(**ref, via="регламентом контроля маржинальности")]


def test_admin_job_and_review_contracts() -> None:
    job = AdminJob.model_validate(
        {
            "job_id": "j",
            "op": "upsert",
            "doc_id": "a",
            "version": 3,
            "status": "failed",
            "attempts": 4,
            "error": "превышено время задачи (шаг index)",
            "current_step": None,
            "steps": {"parse": 1.5},
            "created_at": "2026-11-27T10:00:00+07:00",
            "updated_at": "2026-11-27T10:30:00+07:00",
        }
    )
    assert job.steps == {"parse": 1.5}
    review = AdminReview.model_validate(
        {
            "items": [
                {
                    "doc_id": "a",
                    "version": 3,
                    "title": "Регламент",
                    "clause": "2.5",
                    "via": "регламентом подбора",
                    "kind": "ambiguous",
                    "candidates": ["b", "c"],
                }
            ]
        }
    )
    assert review.items[0].kind == "ambiguous"
    with pytest.raises(ValidationError):
        AdminReview.model_validate({"items": [{**review.items[0].model_dump(), "kind": "x"}]})


def test_index_job_force_flag_is_part_of_the_contract() -> None:
    """Переиндексация (D-45) ставит force: флаг — часть общего контракта index.jobs."""
    plain = IndexJob(op="upsert", doc_id="a", version=1, path="a.docx", sha256="s")
    assert plain.force is False
    forced = IndexJob.model_validate_json(
        plain.model_copy(update={"force": True}).model_dump_json()
    )
    assert forced.force is True


def test_turn_report_contract() -> None:
    from regagent_common.contracts import TurnReport

    report = TurnReport.model_validate(
        {
            "run_id": "r-1",
            "thread_id": "t-1",
            "user_sub": "u-1",
            "at": "2026-11-28T01:30:00Z",
            "question": "Кто назначает РП?",
            "area": "ДИБ",
            "area_source": "question",
            "outcome": "answered",
            "reason": None,
            "fragments": ["dib@1#2.7"],
            "cited": ["dib@1#2.7"],
            "practice_used": False,
            "searches": 1,
        }
    )
    assert report.outcome == "answered"
    with pytest.raises(ValidationError):
        TurnReport.model_validate({**report.model_dump(), "outcome": "access_denied"})
