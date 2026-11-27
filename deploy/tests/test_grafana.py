"""Мониторинг: Grafana + Loki + Alloy + свой туннель, отдельный compose (D-65)."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1] / "grafana"


def services() -> dict[str, Any]:
    found: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    return found


def test_only_grafana_is_reachable_and_only_on_loopback() -> None:
    found = services()
    assert set(found) == {"grafana", "loki", "alloy", "tunnel"}
    for name in ("loki", "alloy", "tunnel"):
        assert not found[name].get("ports"), name
    assert all(str(port).startswith("127.0.0.1:") for port in found["grafana"]["ports"])


def test_images_are_pinned() -> None:
    for name, service in services().items():
        image = str(service["image"])
        assert ":" in image and not image.endswith(":latest"), name


def test_secrets_come_from_env_and_are_required() -> None:
    found = services()
    grafana = found["grafana"]["environment"]
    assert grafana["GF_SECURITY_ADMIN_PASSWORD"].startswith("${GRAFANA_ADMIN_PASSWORD:?")
    assert grafana["GF_SERVER_ROOT_URL"] == "https://grafana.honey-road.tech"
    assert grafana["GF_USERS_ALLOW_SIGN_UP"] == "false"
    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert found["tunnel"]["environment"]["TUNNEL_TOKEN"].startswith("${TUNNEL_TOKEN:?")
    example = (ROOT / ".env.example").read_text()
    assert "TUNNEL_TOKEN=" in example and "eyJ" not in example


def test_collector_reads_docker_and_deploy_events_read_only() -> None:
    mounts = [str(m) for m in services()["alloy"]["volumes"]]
    assert any(m.startswith("/var/run/docker.sock:") and m.endswith(":ro") for m in mounts)
    assert any(m.endswith(":/var/log/regagent:ro") for m in mounts)
    config = (ROOT / "alloy" / "config.alloy").read_text()
    assert "com.docker.compose.project=" in config
    assert "/var/log/regagent/events.jsonl" in config
    assert "http://loki:3100/loki/api/v1/push" in config


def test_dashboard_queries_use_provisioned_loki_and_known_events() -> None:
    source = yaml.safe_load((ROOT / "provisioning" / "datasources" / "loki.yaml").read_text())
    [loki] = source["datasources"]
    assert (loki["uid"], loki["url"]) == ("loki", "http://loki:3100")
    dashboard = json.loads((ROOT / "dashboards" / "regagent.json").read_text())
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert exprs
    events = {e for expr in exprs for e in re.findall(r'event="([a-z_.]+)"', expr)}
    assert events >= {"turn.result", "llm.usage", "index.job"}
    for panel in dashboard["panels"]:
        assert panel["datasource"]["uid"] in {"loki", "-- Grafana --"}
