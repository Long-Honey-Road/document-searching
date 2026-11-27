import io
import json
from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from scripts.eval import run
from scripts.eval.checks import TimedAnswer
from scripts.eval.questions import Corpus, Question


@pytest.fixture
def config(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "PUBLIC_BASE_URL=http://localhost:8080\n"
        "LLM_MODEL=project-model\n"
        "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD=public-demo-value\n"
        "KEYCLOAK_EMPLOYEE_FIN_PASSWORD=fin-demo-value\n"
    )
    return run.read_config(env, None, 2)


@pytest.fixture
def corpus():
    return Corpus(
        clauses={"closed": frozenset({"1.1"})},
        versions={"closed": 1},
        levels={"closed": "closed_fin"},
        areas=frozenset(),
        accounts={
            "employee_public": frozenset({"public"}),
            "employee_fin": frozenset({"public", "closed_fin"}),
        },
    )


@pytest.fixture
def calls(monkeypatch):
    calls = []

    class Session:
        def __init__(self, http, assistant):
            self.tokens = {"test-session-value"}

        async def sign_in(self, login):
            self.user = login.user

        async def reset_memory(self):
            calls.append((self.user, "reset"))

        async def new_thread(self):
            calls.append((self.user, "new"))
            return str(len(calls))

        async def ask(self, thread, question, deadline):
            calls.append((self.user, thread))
            sources = (
                [{"fragment_id": "closed@1#1.1", "clause": "1.1"}] if question == "closed" else []
            )
            return TimedAnswer(
                text="public-demo-value test-session-value",
                meta={"verified": True, "outcome": "answer", "sources": sources},
                first_words_s=1,
                total_s=2,
            )

    monkeypatch.setattr(run, "StandSession", Session)
    return calls


def question(case_id, **values):
    return Question.model_validate(
        {
            "id": case_id,
            "question": case_id,
            "user": "employee_public",
            "outcome": "answer",
            "areas": [],
            "expected_clauses": [],
            "reference_answer": "Ответ",
            **values,
        }
    )


@pytest.mark.asyncio
async def test_uses_both_accounts_and_the_same_thread_until_new_thread(config, corpus, calls):
    cases = [
        question("start", scenario="dialog", reset_memory=True),
        question("followup", scenario="dialog"),
        question("next", scenario="dialog", new_thread=True),
        question("closed", user="employee_fin", expected_clauses=["closed#1.1"]),
    ]
    raw = io.StringIO()
    assert await run.run_cases(cases, corpus, config, raw) == 0
    assert calls == [
        ("employee_public", "reset"),
        ("employee_public", "new"),
        ("employee_public", "2"),
        ("employee_public", "2"),
        ("employee_public", "new"),
        ("employee_public", "5"),
        ("employee_fin", "new"),
        ("employee_fin", "7"),
    ]
    assert len(raw.getvalue().splitlines()) == 4
    assert "public-demo-value" not in raw.getvalue() and "test-session-value" not in raw.getvalue()


@pytest.mark.asyncio
async def test_stops_at_leak_before_asking_next_question(config, corpus, calls, capsys):
    raw = io.StringIO()
    assert await run.run_cases([question("closed"), question("later")], corpus, config, raw) == 3
    assert "УТЕЧКА" in capsys.readouterr().err
    assert json.loads(raw.getvalue())["checks"]["leaks"] == ["closed@1#1.1"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_run_writes_both_artifacts(config, corpus, calls, tmp_path, monkeypatch):
    monkeypatch.setattr(run, "read_config", lambda *_: config)
    monkeypatch.setattr(run, "check_health", AsyncMock())
    monkeypatch.setattr(run, "account_levels", lambda *_: (None, None))
    monkeypatch.setattr(run, "read_corpus", lambda *_: corpus)
    monkeypatch.setattr(run, "load_questions", lambda *_: [question("first")])
    args = Namespace(
        env_file=None,
        stand=None,
        timeout=2,
        validate_only=False,
        manifest=None,
        questions=None,
        reports_dir=tmp_path,
    )
    assert await run._run(args) == 0
    [report] = list(tmp_path.glob("*.md"))
    [raw] = list((tmp_path / "raw").glob("*.jsonl"))
    assert raw.stem == report.stem
    assert "1 из 1" in report.read_text()
    assert [json.loads(line)["type"] for line in raw.read_text().splitlines()] == ["run", "case"]


def test_remote_address_in_env_is_not_used_by_default(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PUBLIC_BASE_URL=https://example.invalid\n")
    with pytest.raises(ValueError, match="локальный стенд"):
        run.read_config(env, None, 2)
