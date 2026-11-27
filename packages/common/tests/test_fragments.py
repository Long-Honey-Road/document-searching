import pytest
from regagent_common.fragments import FragmentHeader, format_header, fragment_id, parse_header


def test_header_roundtrip() -> None:
    h = FragmentHeader("dib-project-activity", 3, "2.7", "ДИБ", "public")
    text = format_header(h) + " Проектная деятельность — 2.7 Назначение РП\nтекст"
    assert format_header(h) == "⟦doc=dib-project-activity v=3 clause=2.7 area=ДИБ level=public⟧"
    assert parse_header(text) == h
    assert fragment_id(h) == "dib-project-activity@3#2.7"


def test_practice_fragment_id() -> None:
    h = FragmentHeader("travel", 2, "4.1", "общий", "public", practice_id="14", mode="overrides")
    assert fragment_id(h) == "practice:14"


def test_parse_header_rejects_garbage() -> None:
    assert parse_header("просто текст без заголовка") is None
    assert parse_header("⟦doc=x v=abc clause=1 area=a level=public⟧") is None


def test_practice_header_format_from_spec_without_version() -> None:
    # Формат из 03-knowledge-service.md: у практики нет v=.
    text = "⟦practice=14 doc=travel clause=4.1 mode=overrides area=общий level=public⟧ текст"
    assert parse_header(text) == FragmentHeader(
        "travel", None, "4.1", "общий", "public", practice_id="14", mode="overrides"
    )


def test_practice_header_roundtrip_with_and_without_version() -> None:
    with_v = FragmentHeader(
        "travel", 2, "4.1", "общий", "public", practice_id="14", mode="clarifies"
    )
    without_v = FragmentHeader(
        "travel", None, "4.1", "общий", "public", practice_id="14", mode="clarifies"
    )
    assert format_header(without_v) == (
        "⟦practice=14 doc=travel clause=4.1 mode=clarifies area=общий level=public⟧"
    )
    assert parse_header(format_header(with_v)) == with_v
    assert parse_header(format_header(without_v)) == without_v


def test_practice_header_with_unknown_mode_is_rejected() -> None:
    assert parse_header("⟦practice=14 doc=t clause=4.1 mode=maybe area=общий level=public⟧") is None


def test_header_must_be_at_start_of_text() -> None:
    h = FragmentHeader("x", 1, "1", "ДИБ", "public")
    assert parse_header("обрывок " + format_header(h)) is None
    assert parse_header("\n  " + format_header(h) + " текст") == h


def test_regulation_header_without_version_is_rejected() -> None:
    assert parse_header("⟦doc=x clause=1 area=ДИБ level=public⟧") is None
    with pytest.raises(ValueError):
        format_header(FragmentHeader("x", None, "1", "ДИБ", "public"))
    with pytest.raises(ValueError):
        fragment_id(FragmentHeader("x", None, "1", "ДИБ", "public"))


def test_practice_header_requires_mode() -> None:
    with pytest.raises(ValueError):
        format_header(FragmentHeader("t", None, "4.1", "общий", "public", practice_id="14"))


@pytest.mark.parametrize("bad", ["два слова", "a=b", "a⟧b", "", "x\ny"])
def test_values_that_would_break_header_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        format_header(FragmentHeader("x", 1, "1", bad, "public"))


def test_unnumbered_clause_ids_are_allowed() -> None:
    h = FragmentHeader("memo", 1, "h2", "общий", "public")
    assert parse_header(format_header(h)) == h
    assert fragment_id(h) == "memo@1#h2"


def test_is_header_value() -> None:
    from regagent_common.fragments import is_header_value

    assert is_header_value("ДИБ") and is_header_value("общий") and is_header_value("2.1.3")
    assert not is_header_value("Дирекция ИБ")
    assert not is_header_value("") and not is_header_value("a=b") and not is_header_value("⟧")
