import os

import pytest

from audiopub.env import load_env


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for var in ("AP_TEST_A", "AP_TEST_B", "AP_TEST_C"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
    for var in ("AP_TEST_A", "AP_TEST_B", "AP_TEST_C"):
        os.environ.pop(var, None)


def test_precedence(isolated, monkeypatch):
    user = isolated / "config" / "audiopub" / ".env"
    user.parent.mkdir(parents=True)
    user.write_text("AP_TEST_A=user\nAP_TEST_B=user\nAP_TEST_C=user\n")
    (isolated / ".env").write_text("AP_TEST_A=local\nAP_TEST_B=local\n")
    explicit = isolated / "custom.env"
    explicit.write_text("AP_TEST_A=explicit\n")
    monkeypatch.setenv("AP_TEST_C", "shell")

    loaded = load_env(explicit)

    assert loaded == [explicit, isolated / ".env", user]
    assert os.environ["AP_TEST_A"] == "explicit"
    assert os.environ["AP_TEST_B"] == "local"
    assert os.environ["AP_TEST_C"] == "shell"  # real environment always wins


def test_missing_files(isolated):
    assert load_env() == []
    with pytest.raises(SystemExit):
        load_env(isolated / "nope.env")
