"""Локальный прогон набора вопросов по двум демо-учёткам."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from collections import defaultdict, deque
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import TextIO
from urllib.parse import urlsplit

import httpx
import yaml
from dotenv import dotenv_values
from langgraph_sdk.errors import APIConnectionError
from pydantic import SecretStr

from scripts.eval.checks import evaluate
from scripts.eval.client import Login, StandError, StandSession, check_health
from scripts.eval.questions import (
    Account,
    Corpus,
    Question,
    account_levels,
    load_questions,
    read_corpus,
)
from scripts.eval.report import CaseResult, RunHeader, encode_record, write_report

ROOT = Path(__file__).resolve().parents[2]
LOCAL_STAND = "http://localhost:8080"
ENV_PLACEHOLDER = "replace-me"
SECRET_ENV_PARTS = ("PASSWORD", "TOKEN", "API_KEY", "SECRET")


@dataclass(frozen=True)
class StandConfig:
    base: str
    issuer: str
    audience: str
    assistant: str | None
    model: str
    passwords: dict[Account, SecretStr]
    deadline: float
    account_limit: int
    secrets: set[str]


def _origin(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or (
            parts.scheme != "https"
            and (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"})
        )
    ):
        raise ValueError("Укажите HTTPS-адрес стенда или HTTP-адрес на loopback")
    return parts.scheme, parts.netloc


def read_config(path: Path, stand: str | None, deadline: float) -> StandConfig:
    if not path.is_file():
        raise ValueError(f"Файл окружения {path} отсутствует. Укажите --env-file для стенда.")
    env = dotenv_values(path, interpolate=False)
    env_base = env.get("PUBLIC_BASE_URL") or LOCAL_STAND
    if not stand and urlsplit(env_base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(
            "По умолчанию используется локальный стенд. Укажите его адрес через --stand."
        )
    base = (stand or env_base).rstrip("/")
    issuer = (env.get("KEYCLOAK_ISSUER") or f"{base}/auth/realms/regagent").rstrip("/")
    if _origin(base) != _origin(issuer) or urlsplit(base).path:
        raise ValueError("Адрес стенда должен совпадать с origin KEYCLOAK_ISSUER из .env")
    if not isfinite(deadline) or deadline <= 0:
        raise ValueError("--timeout должен быть положительным числом секунд")
    try:
        limit = int(env.get("RATE_LIMIT_PER_USER_PER_MIN") or "20")
    except ValueError:
        raise ValueError("RATE_LIMIT_PER_USER_PER_MIN должен быть целым числом") from None
    if limit <= 0:
        raise ValueError("RATE_LIMIT_PER_USER_PER_MIN должен быть больше нуля для прогона")

    passwords: dict[Account, SecretStr] = {}
    for user in ("employee_public", "employee_fin"):
        key = f"KEYCLOAK_{user.upper()}_PASSWORD"
        password = env.get(key)
        if not password or password == ENV_PLACEHOLDER:
            raise ValueError(f"В {path} отсутствует пароль {key}")
        passwords[user] = SecretStr(password)
    return StandConfig(
        base=base,
        issuer=issuer,
        audience=env.get("KEYCLOAK_AUDIENCE") or "regagent-web",
        assistant=env.get("VITE_ASSISTANT_ID") or None,
        model=env.get("LLM_AGENT_MODEL") or env.get("LLM_MODEL") or "не указана",
        passwords=passwords,
        deadline=deadline,
        account_limit=limit,
        secrets={
            value
            for key, value in env.items()
            if value and any(part in key.upper() for part in SECRET_ENV_PARTS)
        },
    )


async def _case(
    question: Question,
    corpus: Corpus,
    session: StandSession,
    config: StandConfig,
    threads: dict[tuple[Account, str], str],
    starts: deque[float],
) -> CaseResult:
    if question.reset_memory:
        await session.reset_memory()
    key = (question.user, question.scenario or question.id)
    if not question.scenario or question.new_thread or key not in threads:
        threads[key] = await session.new_thread()
    starts.append(monotonic())
    answer = await session.ask(threads[key], question.question, config.deadline)
    return CaseResult(question=question, answer=answer, checks=evaluate(question, answer, corpus))


async def run_cases(
    questions: list[Question], corpus: Corpus, config: StandConfig, raw: TextIO
) -> int:
    threads: dict[tuple[Account, str], str] = {}
    starts: dict[Account, deque[float]] = defaultdict(deque)
    status = 0
    async with AsyncExitStack() as stack:
        sessions = {
            user: StandSession(
                await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=f"{config.base}/api/",
                        timeout=httpx.Timeout(15, read=config.deadline),
                    )
                ),
                config.assistant,
            )
            for user in config.passwords
        }
        for index, question in enumerate(questions, 1):
            print(f"[{index}/{len(questions)}] {question.id}", flush=True)
            session = sessions[question.user]
            recent = starts[question.user]
            if len(recent) == config.account_limit:
                await asyncio.sleep(max(0, recent.popleft() + 61 - monotonic()))
            try:
                await session.sign_in(
                    Login(
                        config.base,
                        config.issuer,
                        config.audience,
                        question.user,
                        config.passwords[question.user],
                    )
                )
            except (httpx.HTTPError, StandError):
                raise StandError(f"Не удалось начать сессию под {question.user}") from None
            try:
                case = await _case(question, corpus, session, config, threads, recent)
            except (httpx.TransportError, APIConnectionError):
                raise StandError("Связь со стендом потеряна во время прогона") from None
            except httpx.HTTPStatusError as exc:
                case = CaseResult(
                    question=question, error=f"Запрос завершился с HTTP {exc.response.status_code}"
                )
            except StandError as exc:
                case = CaseResult(question=question, error=str(exc), error_run_id=exc.run_id)

            leak = bool(case.checks and case.checks.leaks)
            if leak:
                print(
                    f"УТЕЧКА: {question.id} под {question.user}. Прогон остановлен.",
                    file=sys.stderr,
                    flush=True,
                )
            tokens = {token for active in sessions.values() for token in active.tokens}
            raw.write(encode_record(case, config.secrets | tokens))
            raw.flush()
            if leak:
                return 3
            if not case.passed:
                status = 1
    return status


def _git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "main"],  # noqa: S607
        cwd=ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--stand", help="origin стенда, по умолчанию локальный")
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/docs/manifest.yaml")
    parser.add_argument("--questions", type=Path, default=ROOT / "eval/questions.yaml")
    parser.add_argument("--reports-dir", type=Path, default=ROOT / "eval/reports")
    parser.add_argument(
        "--timeout", type=float, default=180, help="предел времени одного ответа в секундах"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="сверить вопросы с корпусом")
    mode.add_argument("--report-from", type=Path, help="пересобрать отчёт из JSONL после разметки")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    config = None if args.validate_only else read_config(args.env_file, args.stand, args.timeout)
    if config:
        await check_health(f"{config.base}/api")
    access, accounts = account_levels(ROOT)
    corpus = read_corpus(args.manifest, access, accounts)
    all_questions = load_questions(args.questions, corpus)
    questions = [question for question in all_questions if question.category != "revision"]
    if not questions:
        raise ValueError("В наборе нет активных кейсов для прогона")
    if not config:
        print(f"Набор валиден. Кейсов для прогона: {len(questions)} из {len(all_questions)}.")
        return 0
    if (
        any(question.id == "margin-before" for question in questions)
        and corpus.versions.get("deal-margin-control") != 1
    ):
        raise ValueError("Кейс margin-before ожидает первую редакцию deal-margin-control в корпусе")

    run = RunHeader(
        sha=_git_sha(),
        model=config.model,
        stand=config.base,
        started=datetime.now().astimezone(),
        expected=len(questions),
    )
    raw_dir = args.reports_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = raw_dir / f"{run.started.date()}.jsonl"
    try:
        stream = raw.open("x", encoding="utf-8")
    except FileExistsError:
        raw = raw.with_stem(f"{raw.stem}-{run.started.strftime('%H%M%S%f')}")
        stream = raw.open("x", encoding="utf-8")

    report = args.reports_dir / f"{raw.stem}.md"
    with stream:
        stream.write(encode_record(run, config.secrets))
        stream.flush()
        try:
            return await run_cases(questions, corpus, config, stream)
        finally:
            write_report(raw, report)
            print(f"Отчёт {report}\nСырые ответы {raw}", flush=True)


def main() -> int:
    args = _args()
    os.umask(0o077)
    try:
        if args.report_from:
            args.reports_dir.mkdir(parents=True, exist_ok=True)
            report = args.reports_dir / f"{args.report_from.stem}.md"
            write_report(args.report_from, report)
            print(f"Отчёт {report}")
            return 0
        return asyncio.run(_run(args))
    except (StandError, ValueError, OSError, yaml.YAMLError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError:
        print("Не удалось определить SHA ветки main", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
