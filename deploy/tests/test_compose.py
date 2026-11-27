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
