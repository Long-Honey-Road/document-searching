import base64
import hashlib
import json
import re
from pathlib import Path
from runpy import run_path
from urllib.parse import parse_qs, urlsplit

deploy = run_path(str(Path(__file__).parents[1] / "prod/deploy.py"))


def test_rejected_releases_never_become_rollback_targets(tmp_path):
    state = tmp_path / "state.json"
    switch = deploy["activate"]
    calls = []

    def start(release):
        calls.append(release)
        return release == "A"

    assert switch(state, "A", start, lambda: None)
    assert not switch(state, "B", start, lambda: None)
    assert not switch(state, "C", start, lambda: None)
    assert calls == ["A", "B", "A", "C", "A"]
    assert deploy["read_state"](state)["good"] == "A"


def test_recovers_interrupted_candidate_before_next_release(tmp_path):
    state = tmp_path / "state.json"
    deploy["write_state"](state, {"good": "A", "pending": "B"})
    calls = []
    deploy["recover"](state, lambda release: not calls.append(release), lambda: None)
    assert calls == ["A"]
    assert deploy["read_state"](state) == {"good": "A", "rejected": "B"}


def test_failed_rollback_preserves_pending_for_restart(tmp_path):
    state = tmp_path / "state.json"
    deploy["write_state"](state, {"good": "A", "pending": "B"})
    with pytest.raises(RuntimeError, match="откат"):
        deploy["recover"](state, lambda _: False, lambda: None)
    assert deploy["read_state"](state) == {"good": "A", "pending": "B"}


def test_first_failed_release_is_stopped_without_rollback_target(tmp_path):
    state = tmp_path / "state.json"
    stopped = []
    assert not deploy["activate"](state, "A", lambda _: False, lambda: stopped.append(True))
    assert stopped == [True]
    assert deploy["read_state"](state) == {"rejected": "A"}


def test_release_source_is_readable_by_service_users(tmp_path):
    source = tmp_path / "release"
    source.mkdir(mode=0o700)
    child = source / "config"
    child.mkdir(mode=0o700)
    config = child / "realm.json"
    config.write_text("{}")
    config.chmod(0o600)
    deploy["make_source_readable"](source)
    assert source.stat().st_mode & 0o777 == 0o755
    assert child.stat().st_mode & 0o777 == 0o755
    assert config.stat().st_mode & 0o777 == 0o644


# --- хотфикс 27.11: конфигурация стенда, сборка web, туннель ---

STAND = {
    "ENV": "stand",
    "PUBLIC_BASE_URL": "https://regagent.example.tech",
    "KEYCLOAK_ISSUER": "https://regagent.example.tech/auth/realms/regagent",
    "VITE_OIDC_AUTHORITY": "https://regagent.example.tech/auth/realms/regagent",
    "VITE_API_URL": "/api",
    "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD": "s3cret-value",
}


def test_valid_stand_env_has_no_problems():
    assert deploy["stand_env_problems"](STAND) == []


def test_absolute_api_url_is_rejected_without_leaking_values():
    env = {**STAND, "VITE_API_URL": "http://localhost:8080/api"}
    problems = deploy["stand_env_problems"](env)
    assert any("VITE_API_URL" in p for p in problems)
    assert not any("localhost:8080" in p for p in problems)


def test_empty_api_url_falls_back_to_relative_default():
    assert deploy["stand_env_problems"]({**STAND, "VITE_API_URL": ""}) == []


import pytest  # noqa: E402


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PUBLIC_BASE_URL", "http://regagent.example.tech"),
        ("PUBLIC_BASE_URL", "https://localhost:8080"),
        ("KEYCLOAK_ISSUER", "http://localhost:8080/auth/realms/regagent"),
        ("VITE_OIDC_AUTHORITY", "https://other.example/auth/realms/regagent"),
        ("VITE_ASSISTANT_ID", "http://127.0.0.1:8080"),
        ("POSTGRES_WORKSPACE", "shared"),
        ("RABBITMQ_URL", "amqp://regagent:change-me@rabbitmq:5672/"),
        ("KEYCLOAK_QC_EXPERT_PASSWORD", "replace-me"),
    ],
)
def test_stand_env_problems_name_the_variable(name, value):
    problems = deploy["stand_env_problems"]({**STAND, name: value})
    assert any(name in p for p in problems), problems
    assert not any(value in p for p in problems if value not in {"shared"})


def test_dev_env_skips_public_url_rules_but_keeps_safety_rules():
    dev = {"ENV": "dev", "PUBLIC_BASE_URL": "http://localhost:8080", "VITE_API_URL": "/api"}
    assert deploy["stand_env_problems"](dev) == []
    assert deploy["stand_env_problems"]({**dev, "POSTGRES_WORKSPACE": "x"})


def test_parse_env_handles_comments_quotes_and_equals():
    text = "# комментарий\nA=1\n\nB=\"два = три\"\nexport C='c'\nD=\n"
    assert deploy["parse_env"](text) == {"A": "1", "B": "два = три", "C": "c", "D": ""}


def test_init_env_forces_public_settings(tmp_path):
    source = tmp_path / "source.env"
    source.write_text(
        "ENV=dev\nPUBLIC_BASE_URL=http://localhost:8080\n"
        "VITE_API_URL=http://localhost:8080/api\nLLM_MODEL=m\n"
    )
    target = tmp_path / ".env"
    deploy["init_env"](source, target, "https://regagent.example.tech")
    env = deploy["parse_env"](target.read_text())
    assert env["VITE_API_URL"] == "/api"
    assert env["ENV"] == "stand"
    assert env["LLM_MODEL"] == "m"
    assert deploy["stand_env_problems"](env) == []


def test_init_env_never_overwrites_existing_env(tmp_path):
    source = tmp_path / "source.env"
    source.write_text("ENV=dev\n")
    target = tmp_path / ".env"
    target.write_text("ENV=stand\nKEEP=1\n")
    deploy["init_env"](source, target, "https://regagent.example.tech")
    assert target.read_text() == "ENV=stand\nKEEP=1\n"


SECRETS_SOURCE = (
    "LLM_API_KEY=change-me\n"
    "POSTGRES_SUPERUSER_PASSWORD=change-me\n"
    "RABBITMQ_PASSWORD=change-me\n"
    "RABBITMQ_URL=amqp://regagent:change-me@rabbitmq:5672/\n"
    "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD=replace-me\n"
    "KNOWLEDGE_INTERNAL_TOKEN=change-me\n"
    "KEYCLOAK_ADMIN=admin\n"
    "APP_DB_PASSWORD=уже-задан\n"
)


def test_init_env_generates_local_secrets_but_not_external_keys(tmp_path, capsys):
    source = tmp_path / "source.env"
    source.write_text(SECRETS_SOURCE)
    target = tmp_path / ".env"
    deploy["init_env"](source, target, "https://regagent.example.tech")
    env = deploy["parse_env"](target.read_text())
    generated = [
        "POSTGRES_SUPERUSER_PASSWORD",
        "RABBITMQ_PASSWORD",
        "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD",
        "KNOWLEDGE_INTERNAL_TOKEN",
    ]
    for name in generated:
        assert env[name] not in {"change-me", "replace-me"}
        assert len(env[name]) >= 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", env[name]), "значение годится для URL и .env"
    assert len({env[name] for name in generated}) == len(generated)
    assert env["RABBITMQ_URL"] == f"amqp://regagent:{env['RABBITMQ_PASSWORD']}@rabbitmq:5672/"
    # Внешний ключ модели генерировать бессмысленно: его вписывает человек, релиз ждёт
    assert env["LLM_API_KEY"] == "change-me"
    assert deploy["stand_env_problems"](env) == [
        "LLM_API_KEY: осталось значение-заглушка из .env.example"
    ]
    assert env["APP_DB_PASSWORD"] == "уже-задан"  # noqa: S105
    assert env["KEYCLOAK_ADMIN"] == "admin"
    out = capsys.readouterr().out
    assert "RABBITMQ_PASSWORD" in out and "LLM_API_KEY" in out
    assert all(env[name] not in out for name in generated)


def test_init_env_secrets_differ_between_installs(tmp_path):
    source = tmp_path / "source.env"
    source.write_text(SECRETS_SOURCE)
    values = []
    for name in ("a.env", "b.env"):
        deploy["init_env"](source, tmp_path / name, "https://regagent.example.tech")
        values.append(deploy["parse_env"]((tmp_path / name).read_text())["RABBITMQ_PASSWORD"])
    assert values[0] != values[1]


def test_web_bundle_with_local_stand_address_is_rejected():
    # Библиотеки в сборке сами содержат http://localhost:3000, :8123 и т.п. — их не трогаем.
    check = deploy["web_bundle_problems"]
    base = "https://regagent.example.tech"
    assert check(local_files="/srv/assets/Chat-x.js\n", authority_found=True, base=base)
    assert check(local_files="", authority_found=False, base=base)
    assert check(local_files="", authority_found=True, base=base) == []
    assert (
        check(local_files="/srv/a.js\n", authority_found=False, base="http://localhost:8080") == []
    )


def test_tunnel_override_only_with_token(tmp_path):
    env = tmp_path / "cloudflare.env"
    assert deploy["tunnel_override"](env) is None
    env.write_text("TUNNEL_TOKEN=\n")
    assert deploy["tunnel_override"](env) is None
    env.write_text("TUNNEL_TOKEN=eyJabc\n")
    service = deploy["tunnel_override"](env)["services"]["tunnel"]
    assert service["profiles"] == ["tunnel"]
    assert service["env_file"] == [str(env)]
    assert service["command"] == ["tunnel", "--no-autoupdate", "run"]
    assert "eyJabc" not in str(service)


def test_tunnel_failure_does_not_fail_release(tmp_path):
    calls = []

    def fake_compose(release, *args, **kwargs):
        calls.append(args)
        raise deploy["subprocess"].CalledProcessError(1, "docker")

    d = deploy["Deployment"](tmp_path)
    release = "a" * 40
    (tmp_path / "releases" / release).mkdir(parents=True)
    (tmp_path / "releases" / release / "tunnel.json").write_text("{}")
    d.compose = fake_compose
    d.start_tunnel(release)  # не бросает
    assert calls and calls[0][:3] == ("--profile", "tunnel", "up")


def test_no_tunnel_file_means_no_tunnel_start(tmp_path):
    d = deploy["Deployment"](tmp_path)
    called = []
    d.compose = lambda *a, **k: called.append(a)
    release = "b" * 40
    (tmp_path / "releases" / release).mkdir(parents=True)
    d.start_tunnel(release)
    assert called == []


def test_probe_expects_keycloak_admin_hidden() -> None:
    """Релиз проверяет на живом Caddy, что админка Keycloak за прокси отдаёт 404 (D-47)."""
    paths = dict(deploy["PROBE_PATHS"])
    assert paths["/auth/admin/"] == {404}
    assert paths["/auth/realms/master/.well-known/openid-configuration"] == {404}
    assert paths["/auth/realms/regagent/.well-known/openid-configuration"] == {200}


# --- вход под демо-учёткой после релиза ---------------------------------------------

BASE = "https://regagent.example.test"
ISSUER = BASE + "/auth/realms/regagent"
LOGIN_PAGE = (
    '<html><form id="kc-form-login" method="post" '
    f'action="{ISSUER}/login-actions/authenticate?session_code=s&amp;tab_id=t">'
    '<input name="username"><input name="password" type="password"></form></html>'
)


class FakeStand:
    """Keycloak и API за прокси: ровно то, что видит probe через loopback."""

    def __init__(
        self,
        *,
        password: str = "секрет",  # noqa: S107
        assistants: int = 1,
        threads: int = 200,
        login_status: int | None = None,
    ):
        self.login_status = login_status
        self.password = password
        self.assistants = assistants
        self.threads = threads
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.state = ""
        self.challenge = ""

    def send(self, method: str, target: str, headers: dict[str, str], body: bytes | None):
        self.requests.append((method, target, dict(headers)))
        path, query = urlsplit(target).path, parse_qs(urlsplit(target).query)
        form = parse_qs((body or b"").decode())
        json_ok = [("Content-Type", "application/json")]
        if path == "/auth/realms/regagent/.well-known/openid-configuration":
            oidc = {
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/protocol/openid-connect/auth",
                "token_endpoint": ISSUER + "/protocol/openid-connect/token",
            }
            return 200, json_ok, json.dumps(oidc).encode()
        if path.endswith("/openid-connect/auth"):
            assert query["client_id"] == ["regagent-web"]
            assert query["redirect_uri"] == [BASE + "/"]
            assert query["code_challenge_method"] == ["S256"]
            self.state, self.challenge = query["state"][0], query["code_challenge"][0]
            cookie = [("Set-Cookie", "AUTH_SESSION_ID=abc; Path=/auth/realms/regagent/; Secure")]
            return 200, cookie, LOGIN_PAGE.encode()
        if path.endswith("/login-actions/authenticate"):
            assert "AUTH_SESSION_ID=abc" in headers.get("Cookie", "")
            assert query == {"session_code": ["s"], "tab_id": ["t"]}
            if self.login_status is not None:
                return self.login_status, [], b""
            if form.get("password") != [self.password]:
                return 200, [], b"<span id='input-error'>Invalid username or password.</span>"
            location = f"{BASE}/?state={self.state}&session_state=x&code=the-code"
            return 302, [("Location", location)], b""
        if path.endswith("/openid-connect/token"):
            verifier = form["code_verifier"][0]
            digest = hashlib.sha256(verifier.encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge
            assert form["code"] == ["the-code"] and form["grant_type"] == ["authorization_code"]
            return 200, json_ok, json.dumps({"access_token": "user-token"}).encode()
        if path in {"/api/threads/search", "/api/assistants/search"}:
            if headers.get("Authorization") != "Bearer user-token":
                return 401, [], b""
            if path == "/api/threads/search":
                return self.threads, json_ok, b"[]"
            return (
                200,
                json_ok,
                json.dumps([{"assistant_id": str(i)} for i in range(self.assistants)]).encode(),
            )
        return 404, [], b""


def _login(stand: FakeStand) -> None:
    http = deploy["LoopbackHttp"](18082, BASE, send=stand.send)
    deploy["check_signed_in"](http, ISSUER, "regagent-web", BASE, "employee_public", "секрет")


def test_signed_in_probe_walks_keycloak_and_api_through_loopback(caplog) -> None:
    stand = FakeStand()
    with caplog.at_level("DEBUG"):
        _login(stand)
    paths = [target.split("?")[0] for _, target, _ in stand.requests]
    assert paths[-2:] == ["/api/threads/search", "/api/assistants/search"]
    # Всё идёт на прокси стенда: путь без схемы и хоста, Host — публичный
    assert all(not target.startswith("http") for _, target, _ in stand.requests)
    assert {headers["Host"] for _, _, headers in stand.requests} == {"regagent.example.test"}
    assert "секрет" not in caplog.text


def test_signed_in_probe_rejects_wrong_password_without_echoing_it() -> None:
    with pytest.raises(deploy["LoginRejected"]) as error:
        _login(FakeStand(password="другой"))  # noqa: S106
    assert "employee_public" in str(error.value)
    assert "секрет" not in str(error.value)


def test_signed_in_probe_rejects_401_from_api() -> None:
    with pytest.raises(RuntimeError, match="threads/search → 401"):
        _login(FakeStand(threads=401))


def test_signed_in_probe_requires_exactly_one_assistant() -> None:
    with pytest.raises(RuntimeError, match="ассистент"):
        _login(FakeStand(assistants=2))


def test_loopback_http_never_leaves_the_stand_host() -> None:
    http = deploy["LoopbackHttp"](18082, BASE, send=FakeStand().send)
    with pytest.raises(RuntimeError, match="другой хост"):
        http.request("GET", "https://evil.example/steal")


def test_deploy_script_stays_python39_compatible() -> None:
    """LaunchAgent запускает deploy.py системным /usr/bin/python3 macOS (3.9)."""
    import ast

    source = (Path(__file__).parents[1] / "prod/deploy.py").read_text()
    ast.parse(source, feature_version=(3, 9))
    assert source.splitlines()[2] == "from __future__ import annotations"


def _start_with_probe(tmp_path: Path, monkeypatch, failure: Exception) -> tuple[bool, int]:
    d = deploy["Deployment"](tmp_path)
    attempts = []

    def probe(release: str) -> None:
        attempts.append(release)
        raise failure

    monkeypatch.setattr(d, "compose", lambda *args, **kwargs: "")
    monkeypatch.setattr(d, "probe", probe)
    monkeypatch.setattr(d, "status", lambda release: None)
    sleeper = type("NoSleep", (), {"sleep": staticmethod(lambda seconds: None)})
    monkeypatch.setitem(deploy["Deployment"].start.__globals__, "time", sleeper)
    return d.start("a" * 40), len(attempts)


def test_rejected_sign_in_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    """Повторы с неверным паролем заблокировали бы employee_public защитой от подбора."""
    rejected = deploy["LoginRejected"]("вход под employee_public не прошёл")
    assert _start_with_probe(tmp_path, monkeypatch, rejected) == (False, 1)


def test_transient_probe_failures_are_retried(tmp_path: Path, monkeypatch) -> None:
    assert _start_with_probe(tmp_path, monkeypatch, OSError("connection refused")) == (False, 61)


def test_keycloak_hiccup_on_sign_in_is_retried_not_rejected() -> None:
    """503 от стартующего Keycloak — повод повторить, а не отклонить релиз."""
    with pytest.raises(RuntimeError) as error:
        _login(FakeStand(login_status=503))
    assert not isinstance(error.value, deploy["LoginRejected"])


def test_signed_in_probe_can_be_switched_off(tmp_path: Path, caplog) -> None:
    env = {"RELEASE_SIGN_IN_PROBE": "off", "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD": "x"}
    assert deploy["sign_in_probe_enabled"](env) is False
    assert deploy["sign_in_probe_enabled"]({}) is True
    assert deploy["sign_in_probe_enabled"]({"RELEASE_SIGN_IN_PROBE": "on"}) is True


def _deploy_events(root: Path) -> list[dict[str, object]]:
    path = root / "logs" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_accepted_release_is_a_deploy_event(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"good": "a" * 40}))
    assert deploy["activate"](state, "b" * 40, lambda sha: True, lambda: None)
    [event] = _deploy_events(tmp_path)
    assert (event["service"], event["event"], event["status"]) == (
        "deploy",
        "deploy.decision",
        "accepted",
    )
    assert (event["candidate"], event["previous"]) == ("b" * 40, "a" * 40)
    assert isinstance(event["duration_ms"], int) and event["ts"].endswith("Z")


def test_rejected_release_and_rollback_are_deploy_events(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"good": "a" * 40}))
    assert not deploy["activate"](state, "b" * 40, lambda sha: sha == "a" * 40, lambda: None)
    statuses = [(event["event"], event["status"]) for event in _deploy_events(tmp_path)]
    assert statuses == [("deploy.decision", "rejected"), ("deploy.rollback", "ok")]
