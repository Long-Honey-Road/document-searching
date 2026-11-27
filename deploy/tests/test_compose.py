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
