"""Автоматическая выкладка origin/main на Mac."""

from __future__ import annotations

import fcntl
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

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


def init_env(source: Path, destination: Path, base: str) -> None:
    """Первичный .env стенда из исходного; существующий не трогается."""
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
    lines = [
        line
        for line in source.read_text().splitlines()
        if line.partition("=")[0].strip() not in overrides
    ]
    lines += [f"{key}={value}" for key, value in overrides.items()]
    destination.write_text("\n".join(lines) + "\n")


# Локальный адрес самого стенда (значение по умолчанию из .env.example). Общий поиск
# localhost не годится: библиотеки в сборке содержат свои http://localhost:3000, :8123 и т.п.
# Что релиз проверяет через прокси на loopback. Админка Keycloak должна быть закрыта (D-47).
PROBE_PATHS: tuple[tuple[str, set[int]], ...] = (
    ("/", {200}),
    ("/api/health", {200}),
    ("/api/threads", {401, 403}),
    ("/auth/realms/regagent/.well-known/openid-configuration", {200}),
    ("/auth/admin/", {404}),
    ("/auth/realms/master/.well-known/openid-configuration", {404}),
)
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


def recover(path: Path, start: Callable[[str], bool], stop: Callable[[], None]) -> None:
    state = read_state(path)
    pending = state.get("pending")
    if not pending:
        return
    good = state.get("good")
    log.warning("Откат %s → %s", pending, good or "нет принятой версии")
    if good:
        if not start(good):
            raise RuntimeError(f"откат на {good} не прошёл проверку")
    else:
        stop()
    write_state(path, {**({"good": good} if good else {}), "rejected": pending})


def activate(
    path: Path, candidate: str, start: Callable[[str], bool], stop: Callable[[], None]
) -> bool:
    state = read_state(path)
    write_state(path, {**state, "pending": candidate})
    try:
        passed = start(candidate)
    except Exception:
        log.exception("Ошибка запуска кандидата %s", candidate)
        passed = False
    if passed:
        write_state(path, {"good": candidate})
        log.info("Принят %s", candidate)
        return True
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
                if path.endswith("openid-configuration"):
                    issuer = json.loads(response.read())["issuer"]
                    if issuer != f"{base}/auth/realms/regagent":
                        raise RuntimeError("неверный OIDC issuer")
            finally:
                connection.close()
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
