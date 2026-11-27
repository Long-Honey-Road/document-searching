from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def test_app_services_have_internal_ports_and_separate_database_roles() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert set(services) >= {"knowledge", "backend", "web", "proxy"}
    for service in ("knowledge", "backend", "web"):
        assert not services[service].get("ports")
    assert services["backend"]["environment"]["POSTGRES_USER"] == "${APP_DB_USER}"
    assert services["knowledge"]["environment"]["KNOWLEDGE_DB_USER"] == "${KNOWLEDGE_DB_USER}"
    assert "POSTGRES_SUPERUSER_PASSWORD" not in services["backend"]["environment"]
    assert "POSTGRES_SUPERUSER_PASSWORD" not in services["knowledge"]["environment"]
    assert services["backend"]["environment"]["AUTH_TYPE"] == "custom"


def test_knowledge_reads_documents_and_writes_okf() -> None:
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    mounts = services["knowledge"]["volumes"]
    assert matches_mount(mounts, "../data/docs", read_only=True)
    assert matches_mount(mounts, "../data/okf", read_only=False)


def matches_mount(mounts: list[dict[str, object]], source: str, *, read_only: bool) -> bool:
    return any(
        entry.get("source") == source and entry.get("read_only", False) == read_only
        for entry in mounts
    )


def test_knowledge_has_no_workspace_override() -> None:
    # POSTGRES_WORKSPACE сводит все уровни доступа в один workspace LightRAG (D-30).
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    assert "POSTGRES_WORKSPACE" not in services["knowledge"]["environment"]


def test_knowledge_keeps_lightrag_working_dir_on_volume() -> None:
    # Якорь конфигурации хранилищ LightRAG должен переживать пересоздание контейнера (D-33).
    compose = yaml.safe_load(COMPOSE.read_text())
    knowledge = compose["services"]["knowledge"]
    workdir = knowledge["environment"]["LIGHTRAG_WORKING_DIR"]
    assert "LIGHTRAG_EXTRACT_GLEANING" in knowledge["environment"]
    target = workdir.split(":-")[-1].rstrip("}") if workdir.startswith("${") else workdir
    named = [m for m in knowledge["volumes"] if m.get("type") == "volume"]
    assert any(m["target"] == target and m["source"] in compose["volumes"] for m in named)


def test_services_reach_host_models_on_linux() -> None:
    # Ollama на хосте: на Linux host.docker.internal сам не резолвится.
    services = yaml.safe_load(COMPOSE.read_text())["services"]
    for service in ("knowledge", "backend"):
        assert "host.docker.internal:host-gateway" in services[service].get("extra_hosts", [])


def test_backend_gets_agent_limits_with_defaults() -> None:
    # Параметры backend из .env (D-24, D-20, JWKS): пустая подстановка уронила бы настройки.
    env = yaml.safe_load(COMPOSE.read_text())["services"]["backend"]["environment"]
    for name in (
        "AREA_CODES",
        "KNOWLEDGE_TIMEOUT_S",
        "AGENT_MAX_SEARCHES",
        "TOOL_RESULT_MAX_CHARS",
        "JWKS_CACHE_TTL_S",
        "JWKS_MIN_REFRESH_S",
        "JWT_LEEWAY_S",
    ):
        assert env[name].startswith(f"${{{name}:-"), name


def test_knowledge_image_bakes_tiktoken_cache() -> None:
    # Без кэша tiktoken knowledge при старте лезет в интернет за словарём (D-31).
    dockerfile = (COMPOSE.parents[1] / "services" / "knowledge" / "Dockerfile").read_text()
    assert "lightrag-download-cache" in dockerfile
    assert "TIKTOKEN_CACHE_DIR" in dockerfile.split("AS runtime", 1)[1]


def test_knowledge_receives_every_setting() -> None:
    """Compose передаёт knowledge переменные явным списком: поле настроек без строки в
    compose молча работает на значении по умолчанию, и на стенде его не поменять.
    Повод — 27.11: INDEX_JOB_TIMEOUT_S, LIGHTRAG_LANGUAGE, LIGHTRAG_EXTRACTION_JSON."""
    from regagent_knowledge.settings import RuntimeSettings

    env = yaml.safe_load(COMPOSE.read_text())["services"]["knowledge"]["environment"]
    missing = sorted({name.upper() for name in RuntimeSettings.model_fields} - env.keys())
    assert not missing, f"нет в environment knowledge: {missing}"


def test_new_knowledge_settings_have_compose_defaults() -> None:
    env = yaml.safe_load(COMPOSE.read_text())["services"]["knowledge"]["environment"]
    assert env["INDEX_JOB_TIMEOUT_S"] == "${INDEX_JOB_TIMEOUT_S:-1800}"
    assert env["LIGHTRAG_LANGUAGE"] == "${LIGHTRAG_LANGUAGE:-Russian}"
    assert env["LIGHTRAG_EXTRACTION_JSON"] == "${LIGHTRAG_EXTRACTION_JSON:-false}"


CADDYFILE = Path(__file__).resolve().parents[1] / "caddy" / "Caddyfile"
KEYCLOAK_ADMIN_PATHS = (
    "/auth/admin",
    "/auth/admin/*",
    "/auth/realms/master",
    "/auth/realms/master/*",
)


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for index in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[index], 0)
        if depth == 0:
            return text[start : index + 1]
    raise AssertionError(f"блок {header!r} не закрыт")


def test_proxy_hides_keycloak_admin_console() -> None:
    """Консоль администратора Keycloak и master realm снаружи не отдаются (D-47): после
    Bypass в Cloudflare стенд открыт всем, админку ведут через kcadm.sh в контейнере."""
    auth = _block(CADDYFILE.read_text(), "handle /auth/* {")
    matcher = next(line.split() for line in auth.splitlines() if "@keycloakAdmin path" in line)
    assert tuple(matcher[2:]) == KEYCLOAK_ADMIN_PATHS
    lines = [line.strip() for line in auth.splitlines()]
    assert "respond @keycloakAdmin 404" in lines
    assert lines.index("respond @keycloakAdmin 404") < lines.index("reverse_proxy keycloak:8080")


def test_backend_receives_every_setting() -> None:
    """Как и для knowledge: настройка без строки в compose на стенде не меняется."""
    from regagent_backend.settings import BackendSettings

    env = yaml.safe_load(COMPOSE.read_text())["services"]["backend"]["environment"]
    missing = sorted({name.upper() for name in BackendSettings.model_fields} - env.keys())
    assert not missing, f"нет в environment backend: {missing}"
