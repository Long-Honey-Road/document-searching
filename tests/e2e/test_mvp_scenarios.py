import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import pytest
import yaml
from dotenv import dotenv_values
from langgraph_sdk import get_sync_client
from langgraph_sdk.client import SyncLangGraphClient
from langgraph_sdk.schema import Input

from scripts.synth.generate import build_corpus

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]


class LoginForm(HTMLParser):
    action = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "kc-form-login":
            self.action = attributes.get("action") or ""


def sign_in(issuer: str, audience: str, base: str, username: str, password: str) -> str:
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    state = secrets.token_urlsafe(16)
    with httpx.Client(timeout=15) as http:
        response = http.get(issuer + "/.well-known/openid-configuration")
        response.raise_for_status()
        oidc = response.json()
        assert oidc["issuer"] == issuer
        login = http.get(
            oidc["authorization_endpoint"],
            params={
                "client_id": audience,
                "redirect_uri": base + "/",
                "response_type": "code",
                "scope": "openid profile",
                "code_challenge_method": "S256",
                "code_challenge": challenge.rstrip("="),
                "state": state,
            },
        )
        login.raise_for_status()
        form = LoginForm()
        form.feed(login.text)
        assert form.action, "No Keycloak sign-in form"
        login_url = urljoin(str(login.url), form.action)
        endpoint = urlsplit(login_url)
        declared_issuer = urlsplit(issuer)
        assert (endpoint.scheme, endpoint.netloc) == (
            declared_issuer.scheme,
            declared_issuer.netloc,
        )

        headers = {}
        if endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1"}:
            # Browsers treat loopback as a secure context; the HTTP cookie jar does not.
            headers["Cookie"] = "; ".join(
                f"{cookie.name}={cookie.value}" for cookie in http.cookies.jar
            )
        callback = http.post(
            login_url, data={"username": username, "password": password}, headers=headers
        )
        assert callback.status_code == 302, "Keycloak did not redirect after sign-in"
        target = urlsplit(callback.headers["location"])
        assert target.geturl().split("?")[0] == base + "/"
        params = parse_qs(target.query)
        assert params.get("state") == [state]
        token_url = urlsplit(oidc["token_endpoint"])
        assert (token_url.scheme, token_url.netloc) == (
            declared_issuer.scheme,
            declared_issuer.netloc,
        )
        token = http.post(
            token_url.geturl(),
            data={
                "grant_type": "authorization_code",
                "client_id": audience,
                "redirect_uri": base + "/",
                "code": params["code"][0],
                "code_verifier": verifier,
            },
        )
        token.raise_for_status()
        return str(token.json()["access_token"])


@dataclass(frozen=True)
class Answer:
    content: str
    meta: dict[str, Any]

    def includes(self, doc_id: str, clause: str, version: int | None = None) -> bool:
        return any(
            source.get("fragment_id", "").startswith(doc_id + "@")
            and source.get("clause") == clause
            and (version is None or source.get("version") == version)
            for source in self.meta.get("sources", [])
        )


@dataclass(frozen=True)
class Employee:
    client: SyncLangGraphClient
    assistant: str

    def ask(self, text: str, thread: str | None = None) -> Answer:
        if thread is None:
            thread = self.client.threads.create()["thread_id"]
        payload = cast(Input, {"messages": [{"type": "human", "content": text}]})
        result = self.client.runs.wait(thread, self.assistant, input=payload)
        assert isinstance(result, dict) and isinstance(result.get("messages"), list)
        message = result["messages"][-1]
        assert message["type"] == "ai"
        extra = message.get("additional_kwargs", {})
        meta = extra.get("answer_meta", extra)
        assert meta.get("verified") is True
        assert isinstance(message["content"], str)
        return Answer(message["content"], meta)


@pytest.fixture(scope="module")
def stand_config() -> tuple[dict[str, str | None], str]:
    config_file = Path(os.getenv("REGAGENT_E2E_ENV_FILE", str(ROOT / ".env")))
    config = {**dotenv_values(config_file), **os.environ}
    base = config.get("PUBLIC_BASE_URL") or "http://localhost:8080"
    return config, base.rstrip("/")


def test_loopback_alias_redirects_before_oidc(
    stand_config: tuple[dict[str, str | None], str],
) -> None:
    _, base = stand_config
    origin = urlsplit(base)
    if origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1"}:
        pytest.skip("This check requires an HTTP loopback deployment")
    alias = "127.0.0.1" if origin.hostname == "localhost" else "localhost"
    authority = f"{alias}:{origin.port}" if origin.port else alias
    response = httpx.get(f"http://{authority}/?regagent=1", follow_redirects=False, timeout=10)
    assert response.status_code == 302
    assert response.headers["location"] == base + "/?regagent=1"


@pytest.fixture(scope="module")
def employees(stand_config: tuple[dict[str, str | None], str]) -> dict[str, Employee]:
    config, base = stand_config
    issuer = config.get("KEYCLOAK_ISSUER") or base + "/auth/realms/regagent"
    audience = config.get("KEYCLOAK_AUDIENCE") or "regagent-web"
    api = base + "/api"
    health = httpx.get(api + "/health", timeout=10)
    assert health.is_success, f"Aegra is not ready ({health.status_code}) on {api}"

    results = {}
    for name in ("employee_public", "employee_fin"):
        password = config.get(f"KEYCLOAK_{name.upper()}_PASSWORD")
        assert password, f"Missing password for {name} in the local test environment"
        token = sign_in(issuer, audience, base, name, password)
        client = get_sync_client(
            url=api, api_key=None, headers={"Authorization": f"Bearer {token}"}, timeout=60
        )
        assistant = config.get("VITE_ASSISTANT_ID")
        if not assistant:
            assistants = client.assistants.search(limit=2)
            assert isinstance(assistants, list) and len(assistants) == 1
            assistant = assistants[0]["assistant_id"]
        results[name] = Employee(client, assistant)
    return results


@pytest.fixture(scope="module")
def questions() -> dict[str, str]:
    entries = yaml.safe_load((ROOT / "eval/questions.yaml").read_text())
    return {entry["id"]: entry["question"] for entry in entries}


def test_clarifies_area_and_remembers_it_in_a_new_thread(
    employees: dict[str, Employee], questions: dict[str, str]
) -> None:
    employee = employees["employee_public"]
    thread = employee.client.threads.create()["thread_id"]
    first = employee.ask(questions["clarify-area"], thread)
    assert not first.meta["sources"]
    assert re.search(r"уточн|какой област|выберите област", first.content, re.I)
    second = employee.ask(questions["confirm-dib"], thread)
    assert second.meta["area_used"] == "ДИБ"
    assert second.includes("dib-project-activity", "2.7")
    remembered = employee.ask(questions["remember-dib"])
    assert remembered.meta["area_used"] == "ДИБ"
    assert remembered.meta["area_source"] == "memory"
    assert remembered.includes("dib-project-activity", "2.7")


def test_answers_with_two_regulations(
    employees: dict[str, Employee], questions: dict[str, str]
) -> None:
    answer = employees["employee_public"].ask(questions["multi-doc"])
    assert answer.meta["outcome"] == "answer"
    assert answer.includes("dib-presale-activity", "2.4")
    assert answer.includes("dib-project-activity", "2.1")


def test_refuses_to_compute_a_specific_deals_margin(
    employees: dict[str, Employee], questions: dict[str, str]
) -> None:
    answer = employees["employee_public"].ask(questions["refuse-margin"])
    assert answer.meta["outcome"] == "refusal"


@pytest.mark.leak
def test_finance_answer_requires_the_finance_group(
    employees: dict[str, Employee], questions: dict[str, str]
) -> None:
    denied = employees["employee_public"].ask(questions["denied-finance"])
    assert denied.meta["outcome"] == "not_found"
    assert not denied.meta["sources"]
    assert "семь рабочих дней" not in denied.content.lower()
    assert "7 рабочих дней" not in denied.content.lower()
    allowed = employees["employee_fin"].ask(questions["allowed-finance"])
    assert allowed.meta["outcome"] == "answer"
    assert allowed.includes("finance-documents", "1.2")


def test_switching_manifest_revision_changes_the_answer(
    employees: dict[str, Employee], questions: dict[str, str]
) -> None:
    employee = employees["employee_public"]
    before = employee.ask(questions["margin-before"])
    assert before.includes("deal-margin-control", "1.2", version=1)
    samples_dir = Path(os.getenv("REGAGENT_E2E_SAMPLES_DIR", str(ROOT / "data/samples")))
    docs_dir = Path(os.getenv("REGAGENT_E2E_DOCS_DIR", str(ROOT / "data/docs")))
    build_corpus(samples_dir, docs_dir, revision=2)
    deadline = monotonic() + 90
    while monotonic() < deadline:
        after = employee.ask(questions["margin-after"])
        if after.includes("deal-margin-control", "1.2", version=2):
            return
        sleep(5)
    pytest.fail("Knowledge did not return the new source revision within the test budget")
