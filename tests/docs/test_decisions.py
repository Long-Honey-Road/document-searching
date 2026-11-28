"""Номера решений и техдолга в коде описаны в документации.

Код ссылается на решения `D-xx` (приложение «Решения» в docs/LLD.md) и на техдолг
`TECH-DEBT TD-xx` (docs/TECH-DEBT.md). Ссылка без записи — мёртвая.
"""

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LLD = ROOT / "docs" / "LLD.md"
TECH_DEBT = ROOT / "docs" / "TECH-DEBT.md"
CODE_DIRS = ("packages", "services", "deploy", "scripts", "web/src", "eval", "tests", "config")
SUFFIXES = {".py", ".ts", ".tsx", ".yaml", ".yml", ".sql", ".md", ".json"}
DOCS = ("README.md", "docs/HLD.md", "docs/LLD.md", "docs/TECH-DEBT.md")


def _rows(path: Path, prefix: str) -> list[str]:
    return re.findall(rf"^\| ({prefix}-\d+) \|", path.read_text(encoding="utf-8"), re.M)


def _tech_debt() -> list[str]:
    text = TECH_DEBT.read_text(encoding="utf-8")
    return _rows(TECH_DEBT, "TD") + re.findall(r"^### (TD-\d+)\.", text, re.M)


def _code_files() -> list[Path]:
    files = [ROOT / "deploy" / "caddy" / "Caddyfile", ROOT / ".env.example"]
    for d in CODE_DIRS:
        for path in (ROOT / d).rglob("*"):
            if path.suffix in SUFFIXES and not {".venv", "node_modules"} & set(path.parts):
                files.append(path)
    return files


def test_decision_and_tech_debt_numbers_are_unique() -> None:
    for numbers in (_rows(LLD, "D"), _tech_debt()):
        dup = [n for n, c in Counter(numbers).items() if c > 1]
        assert not dup, f"повторяются номера: {dup}"


def test_decisions_referenced_in_code_and_docs_are_described() -> None:
    known = set(_rows(LLD, "D"))
    paths = _code_files() + [ROOT / doc for doc in DOCS]
    found = {
        (number, str(path.relative_to(ROOT)))
        for path in paths
        for number in re.findall(r"\bD-\d+\b", path.read_text(encoding="utf-8"))
    }
    missing = sorted(item for item in found if item[0] not in known)
    assert not missing, f"решения без записи в docs/LLD.md: {missing}"


def test_tech_debt_markers_are_described() -> None:
    known = set(_tech_debt())
    pattern = re.compile(r"\bTD-\d+\b")
    found = {
        number
        for path in _code_files() + [ROOT / doc for doc in DOCS]
        for number in pattern.findall(path.read_text(encoding="utf-8"))
    }
    assert found <= known, f"техдолг без записи в docs/TECH-DEBT.md: {sorted(found - known)}"
