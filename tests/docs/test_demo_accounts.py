"""Демо-учётки в документах и кейсах прогона есть в realm Keycloak.

Повод — 27.11: чек-лист демо ссылался на `tester_public`/`tester_fin`, которых нет в realm,
а realm, прогон качества и e2e используют `employee_public`/`employee_fin`.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REALM = ROOT / "deploy" / "keycloak" / "regagent-realm.json"
SOURCES = ("docs", "deploy", "eval", "scripts/eval", "tests/e2e")
# Имена в духе демо-учёток: employee_public, tester_fin … (qc_* и user_* — это таблицы и поля)
ACCOUNT = re.compile(r"\b(?:employee|tester|demo)_(?!corpus\b)[a-z]+\b")


def _realm_users() -> set[str]:
    realm = json.loads(REALM.read_text(encoding="utf-8"))
    return {user["username"] for user in realm["users"]}


def test_demo_accounts_in_docs_exist_in_realm() -> None:
    known = _realm_users()
    unknown: dict[str, set[str]] = {}
    paths = [ROOT / "README.md"] + [p for source in SOURCES for p in (ROOT / source).rglob("*")]
    for path in paths:
        if path.suffix not in {".md", ".yaml", ".yml", ".py"} or ".venv" in path.parts:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        names = set(ACCOUNT.findall(path.read_text(encoding="utf-8"))) - known
        if names:
            unknown[str(path.relative_to(ROOT))] = names
    assert not unknown, f"учёток нет в realm {REALM.name}: {unknown}"
