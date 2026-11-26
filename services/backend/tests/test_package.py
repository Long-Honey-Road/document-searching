import regagent_backend


def test_package_imports() -> None:
    assert regagent_backend.__version__ == "0.1.0"
