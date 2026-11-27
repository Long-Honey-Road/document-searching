from pathlib import Path

import pytest
from regagent_common.access import AccessLevels

LEVELS_YAML = """
levels:
  public:
    groups: ["*"]
  closed_fin:
    groups: ["/levels/closed_fin"]
"""


def write_levels(tmp_path: Path, content: str = LEVELS_YAML) -> Path:
    path = tmp_path / "access_levels.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_levels_for_groups(tmp_path: Path) -> None:
    lv = AccessLevels.from_yaml(write_levels(tmp_path))
    assert lv.levels_for([]) == {"public"}
    assert lv.levels_for(["/levels/closed_fin"]) == {"public", "closed_fin"}
    assert not lv.known("nope")


def test_unrelated_groups_do_not_grant_levels(tmp_path: Path) -> None:
    lv = AccessLevels.from_yaml(write_levels(tmp_path))
    assert lv.levels_for(["/levels/closed_fin_extra", "closed_fin"]) == {"public"}


def test_all_and_known(tmp_path: Path) -> None:
    lv = AccessLevels.from_yaml(write_levels(tmp_path))
    assert lv.all() == ["public", "closed_fin"]
    assert lv.known("closed_fin")


def test_repository_config_loads() -> None:
    repo_config = Path(__file__).parents[3] / "config" / "access_levels.yaml"
    lv = AccessLevels.from_yaml(repo_config)
    assert set(lv.all()) == {"public", "closed_fin"}


@pytest.mark.parametrize(
    "content",
    [
        "levels: {}\n",
        "nothing: 1\n",
        "levels:\n  public:\n    groups: []\n",
        "levels:\n  public: {}\n",
        # имя уровня идёт в имя workspace lvl_<level>; '_sentinel' занят служебным
        "levels:\n  _sentinel:\n    groups: ['*']\n",
        "levels:\n  Closed-Fin:\n    groups: ['*']\n",
        "levels:\n  public:\n    groups: [1]\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValueError):
        AccessLevels.from_yaml(write_levels(tmp_path, content))
