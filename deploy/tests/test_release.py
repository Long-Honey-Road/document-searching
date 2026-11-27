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
