"""Автоматическая выкладка origin/main на Mac."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

log = logging.getLogger("deploy")


def read_state(path: Path) -> dict[str, str]:
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(path: Path, state: dict[str, str]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


# --- Конфигурация стенда (хотфикс 27.11: web собрался с VITE_API_URL=http://localhost:8080/api) ---

TUNNEL_IMAGE = "cloudflare/cloudflared:2026.10.7"
_LOCAL_HOSTS = ("localhost", "127.0.0.1")
_PLACEHOLDERS = ("change-me", "replace-me")
_LOCAL_URL = re.compile(r"https?://(localhost|127\.0\.0\.1)")


def parse_env(text: str) -> dict[str, str]:
    """Простой разбор .env: KEY=VALUE, комментарии, кавычки, ``export``."""
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "") in _LOCAL_HOSTS


def stand_env_problems(env: dict[str, str]) -> list[str]:
    """Нарушения конфигурации релиза. Сообщения называют переменную, но не значение."""
    problems: list[str] = []
    if "POSTGRES_WORKSPACE" in env:
        problems.append("POSTGRES_WORKSPACE задан: все уровни доступа сольются в один (D-30)")
    for key, value in env.items():
        if any(marker in value for marker in _PLACEHOLDERS):
            problems.append(f"{key}: осталось значение-заглушка из .env.example")
    if env.get("ENV") != "stand":
        return problems
    base = env.get("PUBLIC_BASE_URL", "")
    if not base.startswith("https://") or _is_local(base):
        problems.append("PUBLIC_BASE_URL: нужен публичный https-адрес стенда")
    issuer = base.rstrip("/") + "/auth/realms/regagent"
    if env.get("KEYCLOAK_ISSUER") != issuer:
        problems.append("KEYCLOAK_ISSUER: должен быть PUBLIC_BASE_URL + /auth/realms/regagent")
    if env.get("VITE_OIDC_AUTHORITY") != env.get("KEYCLOAK_ISSUER"):
        problems.append("VITE_OIDC_AUTHORITY: должен совпадать с KEYCLOAK_ISSUER")
    api = env.get("VITE_API_URL", "")
    if api and not api.startswith("/"):
        problems.append(
            "VITE_API_URL: нужен относительный путь (/api), иначе браузер уйдёт не туда"
        )
    for key, value in env.items():
        if key.startswith("VITE_") and any(host in value for host in _LOCAL_HOSTS):
            problems.append(f"{key}: локальный адрес в сборке web для стенда")
    return sorted(set(problems))


# Секреты, которые стенд создаёт себе сам. Внешние ключи (LLM_API_KEY) вписывает человек.
_GENERATED_SECRET = re.compile(r"[A-Z0-9_]+_(?:PASSWORD|TOKEN)")
# Переменные, в которые вшит пароль другой переменной
_DERIVED_SECRETS = {"RABBITMQ_URL": "RABBITMQ_PASSWORD"}


def _fill_secrets(env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    filled: dict[str, str] = {}
    for key, value in env.items():
        if value in _PLACEHOLDERS and _GENERATED_SECRET.fullmatch(key):
            filled[key] = secrets.token_urlsafe(32)
    for key, origin in _DERIVED_SECRETS.items():
        if origin in filled and key in env:
            value = env[key]
            for placeholder in _PLACEHOLDERS:
                value = value.replace(f":{placeholder}@", f":{filled[origin]}@")
            if value != env[key]:
                filled[key] = value
    left = [key for key, value in env.items() if value in _PLACEHOLDERS and key not in filled]
    return filled, left


def init_env(source: Path, destination: Path, base: str) -> None:
    """Первичный .env стенда из исходного; существующий не трогается. Заглушки паролей и
    внутренних токенов заменяются случайными значениями, значения в вывод не попадают."""
    if destination.exists():
        return
    issuer = base + "/auth/realms/regagent"
    overrides = {
        "ENV": "stand",
        "PUBLIC_BASE_URL": base,
        "KEYCLOAK_ISSUER": issuer,
        "VITE_OIDC_AUTHORITY": issuer,
        "VITE_API_URL": "/api",
        "RERANK_MODE": "embeddings",
        "LIGHTRAG_EXTRACT_GLEANING": "0",
    }
    text = source.read_text()
    secrets_filled, left = _fill_secrets(parse_env(text))
    replaced = {**secrets_filled, **overrides}
    lines = [
        line
        for line in text.splitlines()
        if line.partition("=")[0].removeprefix("export ").strip() not in replaced
    ]
    lines += [f"{key}={value}" for key, value in replaced.items()]
    destination.touch(mode=0o600)
    destination.write_text("\n".join(lines) + "\n")
    if secrets_filled:
        print("Сгенерированы секреты: " + ", ".join(sorted(secrets_filled)))
    if left:
        print("Впишите вручную, иначе релиз не пройдёт: " + ", ".join(sorted(left)))


# Что релиз проверяет через прокси на loopback. Админка Keycloak должна быть закрыта (D-47).
PROBE_PATHS: tuple[tuple[str, set[int]], ...] = (
    ("/", {200}),
    ("/api/health", {200}),
    ("/api/threads", {401, 403}),
    ("/auth/realms/regagent/.well-known/openid-configuration", {200}),
    ("/auth/admin/", {404}),
    ("/auth/realms/master/.well-known/openid-configuration", {404}),
)
# Локальный адрес самого стенда (значение по умолчанию из .env.example). Общий поиск
# localhost не годится: библиотеки в сборке содержат свои http://localhost:3000, :8123 и т.п.
LOCAL_STAND_URLS = ("http://localhost:8080", "http://127.0.0.1:8080")


def web_bundle_problems(*, local_files: str, authority_found: bool, base: str) -> list[str]:
    """``local_files`` — файлы сборки web с LOCAL_STAND_URLS; ``authority_found`` — есть ли
    в сборке VITE_OIDC_AUTHORITY из конфигурации релиза (сборка шла с нужным .env)."""
    if _is_local(base):
        return []
    problems = [
        f"сборка web обращается к локальному адресу стенда: {name}"
        for name in local_files.splitlines()
        if name.strip()
    ]
    if not authority_found:
        problems.append("в сборке web нет VITE_OIDC_AUTHORITY из конфигурации релиза")
    return problems


def tunnel_override(cloudflare_env: Path) -> dict[str, object] | None:
    """Сервис коннектора Cloudflare для релиза, если в cloudflare.env есть токен."""
    if not cloudflare_env.is_file():
        return None
    if not parse_env(cloudflare_env.read_text()).get("TUNNEL_TOKEN", "").startswith("eyJ"):
        return None
    return {
        "services": {
            "tunnel": {
                "image": TUNNEL_IMAGE,
                "profiles": ["tunnel"],
                "command": ["tunnel", "--no-autoupdate", "run"],
                "env_file": [str(cloudflare_env)],
                "restart": "unless-stopped",
                "depends_on": ["proxy"],
                "logging": {"options": {"max-size": "10m", "max-file": "3"}},
            }
        }
    }


# --- Вход под демо-учёткой после релиза (stdlib: deploy.py работает на системном python3) ---

Send = Callable[
    [str, str, "dict[str, str]", "bytes | None"], "tuple[int, list[tuple[str, str]], bytes]"
]
PROBE_USER = "employee_public"
PROBE_PASSWORD_VAR = "KEYCLOAK_EMPLOYEE_PUBLIC_PASSWORD"  # noqa: S105 — имя переменной


class LoginRejected(RuntimeError):
    """Keycloak не принял пароль. Не повторяем: защита от подбора заблокирует учётку."""


class LoopbackHttp:
    """HTTP к прокси стенда через loopback. Публичные адреса стенда (их отдаёт Keycloak)
    переписываются на 127.0.0.1 с заголовком Host, наружу запросы не уходят."""

    def __init__(self, port: int, base: str, send: Send | None = None) -> None:
        self.port = port
        self.host = urlsplit(base).netloc
        self.cookies: dict[str, str] = {}
        self._send = send or self._loopback

    def _loopback(
        self, method: str, target: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            connection.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        payload: object = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(url)
        if parts.netloc and parts.netloc != self.host:
            raise RuntimeError("вход: Keycloak отправил на другой хост")
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = {"Host": self.host}
        body = None
        if form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        status, raw_headers, data = self._send(method, target, headers, body)
        for name, value in raw_headers:
            if name.lower() == "set-cookie":
                for morsel in SimpleCookie(value).values():
                    self.cookies[morsel.key] = morsel.value
        return status, {name.lower(): value for name, value in raw_headers}, data


class _LoginForm(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("id") == "kc-form-login":
            self.action = values.get("action") or ""


def _sign_in(
    http: LoopbackHttp, issuer: str, client_id: str, base: str, user: str, password: str
) -> str:
    status, _, data = http.request("GET", issuer + "/.well-known/openid-configuration")
    if status != 200:
        raise RuntimeError(f"вход: OIDC discovery → {status}")
    oidc = json.loads(data)
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    redirect = base + "/"
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect,
            "response_type": "code",
            "scope": "openid profile",
            "code_challenge_method": "S256",
            "code_challenge": challenge.decode().rstrip("="),
            "state": state,
        }
    )
    status, _, page = http.request("GET", oidc["authorization_endpoint"] + "?" + query)
    form = _LoginForm()
    form.feed(page.decode("utf-8", "replace"))
    if status != 200 or not form.action:
        raise RuntimeError(f"вход: нет формы Keycloak ({status})")
    action = urljoin(oidc["authorization_endpoint"], form.action)
    status, headers, _ = http.request("POST", action, form={"username": user, "password": password})
    location = headers.get("location", "")
    if status == 200:
        # Keycloak снова показал форму: учётные данные не приняты
        raise LoginRejected(f"вход под {user} не прошёл: пароль в .env не совпадает с Keycloak?")
    if status != 302 or not location.startswith(redirect):
        raise RuntimeError(f"вход под {user}: Keycloak ответил {status}")
    params = parse_qs(urlsplit(location).query)
    if params.get("state") != [state] or not params.get("code"):
        raise RuntimeError("вход: Keycloak вернул неверный state или без code")
    status, _, data = http.request(
        "POST",
        oidc["token_endpoint"],
        form={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "code": params["code"][0],
            "code_verifier": verifier,
        },
    )
    if status != 200:
        raise RuntimeError(f"вход: обмен кода на токен → {status}")
    token: str = json.loads(data)["access_token"]
    return token


def sign_in_probe_enabled(env: dict[str, str]) -> bool:
    """Аварийный выключатель проверки входа: ``RELEASE_SIGN_IN_PROBE=off`` в .env."""
    return env.get("RELEASE_SIGN_IN_PROBE", "on").strip().lower() not in {"off", "0", "false"}


def check_signed_in(
    http: LoopbackHttp, issuer: str, client_id: str, base: str, user: str, password: str
) -> None:
    """То, что делает браузер после входа: список диалогов и ровно один ассистент."""
    token = _sign_in(http, issuer, client_id, base, user, password)
    status, _, _ = http.request("POST", "/api/threads/search", payload={"limit": 1}, token=token)
    if status != 200:
        raise RuntimeError(f"после входа /api/threads/search → {status}")
    status, _, data = http.request("POST", "/api/assistants/search", payload={}, token=token)
    if status != 200:
        raise RuntimeError(f"после входа /api/assistants/search → {status}")
    found = len(json.loads(data))
    if found != 1:
        raise RuntimeError(f"после входа ассистентов {found}, нужен ровно один")


def emit_event(root: Path, event: str, status: str, **fields: object) -> None:
    """Событие выкатки (D-52) в ``logs/events.jsonl``: одна JSON-строка, только SHA, коды и
    числа. Формат — как у ``regagent_common.events``: deploy.py работает на системном
    python3 без пакетов проекта. Сбой записи не мешает выкатке."""
    line = {
        # datetime.UTC — только с 3.11, а deploy.py работает на системном python3 3.9
        "ts": datetime.now(timezone.utc)  # noqa: UP017
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "service": "deploy",
        "event": event,
        "status": status,
        **{key: value for key, value in fields.items() if value is not None},
    }
    try:
        (root / "logs").mkdir(parents=True, exist_ok=True)
        with (root / "logs" / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        log.warning("Не удалось записать событие выкатки: %s", type(exc).__name__)


def recover(path: Path, start: Callable[[str], bool], stop: Callable[[], None]) -> None:
    state = read_state(path)
    pending = state.get("pending")
    if not pending:
        return
    good = state.get("good")
    log.warning("Откат %s → %s", pending, good or "нет принятой версии")
    started = time.monotonic()
    if good:
        if not start(good):
            emit_event(path.parent, "deploy.rollback", "error", candidate=pending, previous=good)
            raise RuntimeError(f"откат на {good} не прошёл проверку")
    else:
        stop()
    write_state(path, {**({"good": good} if good else {}), "rejected": pending})
    emit_event(
        path.parent,
        "deploy.rollback",
        "ok" if good else "stopped",
        duration_ms=round((time.monotonic() - started) * 1000),
        candidate=pending,
        previous=good,
    )


def activate(
    path: Path, candidate: str, start: Callable[[str], bool], stop: Callable[[], None]
) -> bool:
    state = read_state(path)
    write_state(path, {**state, "pending": candidate})
    started = time.monotonic()
    error_code = None
    try:
        passed = start(candidate)
    except Exception as exc:
        log.exception("Ошибка запуска кандидата %s", candidate)
        error_code = type(exc).__name__
        passed = False
    decision = {
        "duration_ms": round((time.monotonic() - started) * 1000),
        "candidate": candidate,
        "previous": state.get("good"),
        "error_code": error_code,
    }
    if passed:
        write_state(path, {"good": candidate})
        log.info("Принят %s", candidate)
        emit_event(path.parent, "deploy.decision", "accepted", **decision)
        return True
    emit_event(path.parent, "deploy.decision", "rejected", **decision)
    recover(path, start, stop)
    return False


def run(*args: str, cwd: Path, capture: bool = False, check: bool = True) -> str:
    log.debug("Выполняю %s %s", args[0], args[1])
    result = subprocess.run(  # noqa: S603
        args, cwd=cwd, check=check, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout or ""


def make_source_readable(path: Path) -> None:
    for directory, _, files in os.walk(path):
        Path(directory).chmod(0o755)
        for name in files:
            file = Path(directory) / name
            if not file.is_symlink():
                mode = 0o755 if file.stat().st_mode & 0o100 else 0o644
                file.chmod(mode)


class Deployment:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state.json"
        self.source = root / "source"
        self.releases = root / "releases"

    def compose(self, release: str, *args: str, locked: bool = True, capture: bool = False) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", release):
            raise ValueError("некорректный SHA релиза")
        path = self.releases / release
        command = [
            "docker",
            "compose",
            "-p",
            "regagent-prod",
            "--env-file",
            str(self.root / "configs" / f"{release}.env"),
            "-f",
            "deploy/compose.yaml",
            "-f",
            "deploy/prod/compose.yaml",
            "-f",
            "storage.json",
        ]
        if (path / "tunnel.json").exists():
            command.extend(["-f", "tunnel.json"])
        if locked:
            command.extend(["-f", "images.json"])
        return run(*command, *args, cwd=path, capture=capture)

    def prepare(self, release: str) -> None:
        path = self.releases / release
        path.mkdir(parents=True, exist_ok=True)
        (self.root / "configs").mkdir(exist_ok=True)
        config_file = self.root / "configs" / f"{release}.env"
        shutil.copyfile(self.root / ".env", config_file)
        problems = stand_env_problems(parse_env(config_file.read_text()))
        if problems:
            for problem in problems:
                log.error("Конфигурация релиза: %s", problem)
            raise RuntimeError("конфигурация релиза не прошла проверку, см. ошибки выше")
        archive = self.releases / f"{release}.tar"
        run("git", "archive", "--output", str(archive), release, cwd=self.source)
        run("tar", "-xf", str(archive), "-C", str(path), cwd=self.root)
        archive.unlink()
        make_source_readable(path)

        for command in (
            ("uv", "lock", "--check"),
            ("uv", "sync", "--all-packages", "--frozen"),
            ("uv", "run", "--no-sync", "ruff", "check", "."),
            ("uv", "run", "--no-sync", "ruff", "format", "--check", "."),
            ("uv", "run", "--no-sync", "mypy", "packages", "services"),
            ("uv", "run", "--no-sync", "pytest", "-q"),
        ):
            run(*command, cwd=path)
        for args in (("ci",), ("test",), ("run", "lint"), ("run", "build")):
            run("npm", *args, cwd=path / "web")
        data = self.root / "data"
        if not (data / "docs" / "manifest.yaml").exists() and not any((data / "docs").iterdir()):
            run(
                "uv",
                "run",
                "--no-sync",
                "python",
                "scripts/synth/generate.py",
                "--docs-dir",
                str(data / "docs"),
                cwd=path,
            )
        (path / "storage.json").write_text(
            json.dumps(
                {
                    "services": {
                        "knowledge": {
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(data / "docs"),
                                    "target": "${DOCS_DIR}",
                                    "read_only": True,
                                },
                                {
                                    "type": "bind",
                                    "source": str(data / "okf"),
                                    "target": "${OKF_DIR}",
                                },
                            ]
                        }
                    }
                }
            )
        )
        tunnel = tunnel_override(self.root / "cloudflare.env")
        if tunnel is not None:
            (path / "tunnel.json").write_text(json.dumps(tunnel))
        else:
            log.warning("TUNNEL_TOKEN в cloudflare.env не задан: стенд не будет опубликован")
        profiles = ("--profile", "tunnel") if tunnel is not None else ()
        config = json.loads(
            self.compose(
                release, *profiles, "config", "--format", "json", locked=False, capture=True
            )
        )
        services = config["services"]
        if config.get("name") != "regagent-prod":
            raise RuntimeError("неверный Compose project")
        images = {}
        build = []
        for name, service in services.items():
            tag = f"regagent-prod/{name}:{release}"
            images[name] = {"image": tag}
            if "build" in service:
                build.append(name)
            else:
                original = service["image"]
                run("docker", "pull", original, cwd=path)
                run("docker", "tag", original, tag, cwd=path)
        (path / "images.json").write_text(json.dumps({"services": images}))
        self.compose(release, "build", *build)
        self.check_web_bundle(release)

    def check_web_bundle(self, release: str) -> None:
        env = parse_env((self.root / "configs" / f"{release}.env").read_text())

        def files_with(*needles: str) -> str:
            patterns = [arg for needle in needles for arg in ("-e", needle)]
            return run(
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "grep",
                f"regagent-prod/web:{release}",
                "-rlF",
                *patterns,
                "/srv",
                cwd=self.releases / release,
                capture=True,
                check=False,
            )

        authority = env.get("VITE_OIDC_AUTHORITY", "")
        problems = web_bundle_problems(
            local_files=files_with(*LOCAL_STAND_URLS),
            authority_found=bool(authority) and bool(files_with(authority).strip()),
            base=env.get("PUBLIC_BASE_URL", ""),
        )
        if problems:
            for problem in problems:
                log.error("Сборка web: %s", problem)
            raise RuntimeError("сборка web не соответствует конфигурации релиза")

    def start_tunnel(self, release: str) -> None:
        """Коннектор Cloudflare не блокирует релиз: сбой туннеля — предупреждение в логе."""
        if not (self.releases / release / "tunnel.json").exists():
            return
        try:
            self.compose(
                release,
                "--profile",
                "tunnel",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--pull",
                "never",
                "tunnel",
            )
        except subprocess.CalledProcessError:
            log.warning("Коннектор Cloudflare не запустился, стенд снаружи недоступен")

    def probe(self, release: str) -> None:
        config = json.loads(self.compose(release, "config", "--format", "json", capture=True))
        proxy = config["services"]["proxy"]
        base = proxy["environment"]["PUBLIC_BASE_URL"]
        host = urlsplit(base).netloc
        port = int(proxy["ports"][0]["published"])
        for path, expected in PROBE_PATHS:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                method = "POST" if path == "/api/threads" else "GET"
                connection.request(method, path, headers={"Host": host})
                response = connection.getresponse()
                if response.status not in expected:
                    raise RuntimeError(f"{path} → {response.status}")
                if response.status == 200 and path.endswith("openid-configuration"):
                    issuer = json.loads(response.read())["issuer"]
                    if issuer != f"{base}/auth/realms/regagent":
                        raise RuntimeError("неверный OIDC issuer")
            finally:
                connection.close()
        env = parse_env((self.root / "configs" / f"{release}.env").read_text())
        if not sign_in_probe_enabled(env):
            log.warning("Проверка входа выключена (RELEASE_SIGN_IN_PROBE=off)")
        else:
            check_signed_in(
                LoopbackHttp(port, base),
                f"{base}/auth/realms/regagent",
                env.get("VITE_OIDC_CLIENT_ID") or "regagent-web",
                base,
                PROBE_USER,
                env.get(PROBE_PASSWORD_VAR, ""),
            )
        indexed = self.compose(
            release,
            "exec",
            "-T",
            "postgres",
            "sh",
            "-ec",
            'exec psql -X -At -U "$POSTGRES_USER" -d "$KNOWLEDGE_DB" -c "$1"',
            "check-index",
            "SELECT doc_id || chr(9) || version FROM documents WHERE status = 'indexed'",
            capture=True,
        )
        expected = run(
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            'import sys,yaml; docs=yaml.safe_load(open(sys.argv[1]))["documents"]; '
            'print(*[str(d["doc_id"])+chr(9)+str(d["version"]) for d in docs], sep=chr(10))',
            str(self.root / "data/docs/manifest.yaml"),
            cwd=self.releases / release,
            capture=True,
        )
        required = set(expected.splitlines())
        if not required or not required.issubset(indexed.splitlines()):
            raise RuntimeError("корпус ещё не проиндексирован")
        hook = self.root / "verify-release"
        if hook.is_file():
            run(str(hook), release, cwd=self.root)

    def start(self, release: str) -> bool:
        log.info("Запускаю %s", release)
        try:
            self.compose(
                release,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "180",
                "--no-build",
                "--pull",
                "never",
                "--remove-orphans",
            )
        except subprocess.CalledProcessError:
            log.exception("Compose не запустил %s", release)
            self.status(release)
            return False
        for attempt in range(61):
            try:
                self.probe(release)
                self.start_tunnel(release)
                return True
            except LoginRejected as exc:
                log.error("Проверка %s: %s. Повторять не буду.", release, exc)
                break
            except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
                log.info("Проверка %s (%d/61): %s", release, attempt + 1, exc)
                if attempt < 60:
                    time.sleep(10)
            except subprocess.CalledProcessError:
                log.exception("Проверка релиза не прошла")
                break
        self.status(release)
        return False

    def status(self, release: str) -> None:
        try:
            self.compose(release, "ps", "-a")
            self.compose(release, "logs", "--no-color", "--tail", "30")
        except subprocess.CalledProcessError:
            log.warning("Статус контейнеров недоступен")

    def stop_pending(self) -> None:
        self.compose(read_state(self.state)["pending"], "down")

    def deploy(self) -> None:
        recover(self.state, self.start, self.stop_pending)
        run("git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main", cwd=self.source)
        candidate = run("git", "rev-parse", "origin/main", cwd=self.source, capture=True).strip()
        state = read_state(self.state)
        if candidate in {state.get("good"), state.get("rejected")}:
            log.debug("Для %s решение уже принято", candidate)
            return
        files = run(
            "git",
            "ls-tree",
            "--name-only",
            candidate,
            "deploy/prod/compose.yaml",
            cwd=self.source,
            capture=True,
        )
        if not files.strip():
            log.info("Ожидаю релиз с конфигурацией выкладки в origin/main")
            return
        log.info("Проверяю кандидата %s", candidate)
        try:
            self.prepare(candidate)
        except subprocess.CalledProcessError:
            log.exception(
                "Кандидат %s не прошёл подготовку. Активная версия не менялась.", candidate
            )
            raise
        if not activate(self.state, candidate, self.start, self.stop_pending):
            raise RuntimeError(f"релиз {candidate} отвергнут")


def main() -> None:
    os.umask(0o077)
    if sys.argv[1] == "init-env":
        init_env(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        return
    root = Path(sys.argv[1]).resolve()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with (root / "deploy.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        Deployment(root).deploy()


if __name__ == "__main__":
    main()
