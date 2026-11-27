"""CI запускает тесты на утечки на том же образе Postgres, что и стенд (хотфикс 27.11)."""

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def test_ci_runs_integration_and_leak_tests_on_stand_postgres_image() -> None:
    job = yaml.safe_load(CI.read_text())["jobs"]["integration"]
    steps = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "docker build -t regagent-postgres deploy/postgres" in steps
    assert 'pytest -m "(integration or leak) and not e2e" services packages' in steps
    # тесты ходят пользователем knowledge, как сервис на стенде, а не суперпользователем
    assert job["env"]["TEST_POSTGRES_USER"] == "knowledge"
    assert "TEST_LIGHTRAG_GRAPH_STORAGE" not in job["env"]  # граф — в AGE, как на стенде
    assert job["env"]["TEST_RABBITMQ_URL"].startswith("amqp://")
