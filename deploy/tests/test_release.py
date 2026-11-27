from pathlib import Path
from runpy import run_path

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
    import pytest

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
