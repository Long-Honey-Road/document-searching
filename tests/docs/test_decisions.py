"""Журнал решений: номера уникальны, техдолг из кода записан в таблицу.

Повод — мерж 27.11: два агента независимо заняли D-17…D-24 (dev_docs/merge-log-2026-11-27.md).
"""

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ROOT / "dev_docs" / "09-decisions.md"
CODE_DIRS = ("packages", "services", "deploy", "scripts", "web/src")


def _numbers(prefix: str) -> list[str]:
    return re.findall(rf"^\| ({prefix}-\d+) \|", DECISIONS.read_text(encoding="utf-8"), re.M)


def test_decision_numbers_are_unique_and_sequential() -> None:
    numbers = _numbers("D")
    dup = [n for n, c in Counter(numbers).items() if c > 1]
    assert not dup, f"повторяются номера решений: {dup}"
    assert numbers == [f"D-{i:02d}" for i in range(1, len(numbers) + 1)]


def test_tech_debt_numbers_are_unique() -> None:
    numbers = _numbers("TD")
    assert len(numbers) == len(set(numbers))


def test_tech_debt_markers_in_code_are_in_the_table() -> None:
    known = set(_numbers("TD"))
    found: set[str] = set()
    for d in CODE_DIRS:
        for path in (ROOT / d).rglob("*"):
            if path.suffix in {".py", ".ts", ".tsx", ".yaml", ".yml"} and ".venv" not in path.parts:
                found |= set(re.findall(r"TECH-DEBT (TD-\d+)", path.read_text(encoding="utf-8")))
    assert found <= known, f"маркеры без записи в 09-decisions.md: {sorted(found - known)}"
