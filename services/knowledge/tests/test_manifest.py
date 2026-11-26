from datetime import date
from pathlib import Path

import pytest
from regagent_common.access import AccessLevels
from regagent_knowledge.manifest import load_manifest

AREAS = """
areas:
  - code: ДИБ
    title: Дирекция ИБ
    synonyms: [Дирекция информационной безопасности]
  - code: ДБРЦТ
    title: Департамент бизнес-решений и цифровой трансформации
  - code: общий
    title: Общий
"""


def entry(doc_id: str, **over: object) -> str:
    fields: dict[str, object] = {
        "file": f"docs/{doc_id}.docx",
        "doc_id": doc_id,
        "title": f"Документ {doc_id}",
        "areas": "[ДИБ]",
        "access": "public",
        "version": 1,
        "effective_from": "2026-09-01",
    }
    fields.update(over)
    lines = [f"    {k}: {v}" for k, v in fields.items() if v is not None]
    lines[0] = "  - " + lines[0].lstrip()
    return "\n".join(lines)


def write(tmp_path: Path, *entries: str, areas: str = AREAS) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(areas + "documents:\n" + "\n".join(entries) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def levels() -> AccessLevels:
    return AccessLevels({"public": ["*"], "closed_fin": ["/levels/closed_fin"]})


def test_valid_manifest_loads(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(
        write(tmp_path, entry("a"), entry("b", access="closed_fin", version=3)), levels
    )
    assert len(res.valid) == 2
    assert res.errors == []
    b = res.valid[1]
    assert (b.doc_id, b.access, b.version, b.effective_from) == (
        "b",
        "closed_fin",
        3,
        date(2026, 9, 1),
    )
    assert [a.code for a in res.areas] == ["ДИБ", "ДБРЦТ", "общий"]
    assert res.areas[0].synonyms == ["Дирекция информационной безопасности"]


def test_unknown_access_level_is_error(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(write(tmp_path, entry("a", access="secret")), levels)
    assert res.valid == []
    assert res.errors[0].doc_id == "a"
    assert "secret" in res.errors[0].message


def test_unknown_area_is_error(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(write(tmp_path, entry("a", areas="[ДИБ, Марс]")), levels)
    assert res.valid == []
    assert "Марс" in res.errors[0].message


def test_bad_doc_id_is_error(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(write(tmp_path, entry('"Dib Project"', file="docs/x.docx")), levels)
    assert res.valid == []
    assert res.errors[0].file == "docs/x.docx"


def test_duplicate_doc_id_marks_both_invalid(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(
        write(tmp_path, entry("x", file="one.docx"), entry("x", file="two.docx"), entry("y")),
        levels,
    )
    assert [e.doc_id for e in res.valid] == ["y"]
    assert len(res.errors) == 2
    assert {e.file for e in res.errors} == {"one.docx", "two.docx"}


def test_same_file_twice_marks_both_invalid(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(
        write(tmp_path, entry("x", file="a.docx"), entry("y", file="a.docx")), levels
    )
    assert res.valid == []
    assert len(res.errors) == 2


@pytest.mark.parametrize("bad", ["../secret.docx", "/etc/passwd", "a/../../b.docx", ""])
def test_file_outside_docs_dir_is_error(tmp_path: Path, levels: AccessLevels, bad: str) -> None:
    res = load_manifest(write(tmp_path, entry("a", file=f'"{bad}"')), levels)
    assert res.valid == []


@pytest.mark.parametrize(
    "over",
    [
        {"version": 0},
        {"version": "три"},
        {"effective_from": "вчера"},
        {"areas": "[]"},
        {"title": None},
        {"supersedes": "dib@x"},
        {"unexpected": 1},
    ],
)
def test_invalid_fields_are_errors(
    tmp_path: Path, levels: AccessLevels, over: dict[str, object]
) -> None:
    res = load_manifest(write(tmp_path, entry("a", **over), entry("ok")), levels)
    assert [e.doc_id for e in res.valid] == ["ok"]
    assert len(res.errors) == 1


def test_supersedes_parsed(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(write(tmp_path, entry("a", version=3, supersedes="a@2")), levels)
    assert res.valid[0].supersedes == "a@2"


def test_area_code_must_fit_fragment_header(tmp_path: Path, levels: AccessLevels) -> None:
    areas = "areas:\n  - code: Дирекция ИБ\n    title: с пробелом\n"
    res = load_manifest(write(tmp_path, entry("a"), areas=areas), levels)
    assert res.valid == []
    assert any("Дирекция ИБ" in e.message for e in res.errors)


def test_broken_yaml_does_not_raise(tmp_path: Path, levels: AccessLevels) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("documents: [\n", encoding="utf-8")
    res = load_manifest(path, levels)
    assert res.valid == [] and res.areas == []
    assert res.errors and res.errors[0].doc_id is None


def test_missing_manifest_is_error_not_exception(tmp_path: Path, levels: AccessLevels) -> None:
    res = load_manifest(tmp_path / "nope.yaml", levels)
    assert res.valid == []
    assert len(res.errors) == 1
