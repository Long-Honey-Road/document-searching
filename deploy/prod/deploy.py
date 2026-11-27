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


def run(*args: str, cwd: Path, capture: bool = False) -> str:
    log.debug("Выполняю %s %s", args[0], args[1])
    result = subprocess.run(  # noqa: S603
        args, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE if capture else None
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
        if locked:
            command.extend(["-f", "images.json"])
        return run(*command, *args, cwd=path, capture=capture)

    def prepare(self, release: str) -> None:
        path = self.releases / release
        path.mkdir(parents=True, exist_ok=True)
        (self.root / "configs").mkdir(exist_ok=True)
        shutil.copyfile(self.root / ".env", self.root / "configs" / f"{release}.env")
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
        config = json.loads(
            self.compose(release, "config", "--format", "json", locked=False, capture=True)
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

    def probe(self, release: str) -> None:
        config = json.loads(self.compose(release, "config", "--format", "json", capture=True))
        proxy = config["services"]["proxy"]
        base = proxy["environment"]["PUBLIC_BASE_URL"]
        host = urlsplit(base).netloc
        port = int(proxy["ports"][0]["published"])
        for path, expected in (
            ("/", {200}),
            ("/api/health", {200}),
            ("/api/threads", {401, 403}),
            ("/auth/realms/regagent/.well-known/openid-configuration", {200}),
        ):
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
