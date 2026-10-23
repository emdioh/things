import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from dashboard import ssh
from dashboard.app import create_app
from dashboard.config import Config, Host, Service
from dashboard.deploy import build_script, validate_ref


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A clone of an origin with main (2 commits), a tag on the first, and a `next` branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "f").write_text("one")
    git(origin, "add", "f")
    git(origin, "commit", "-qm", "one")
    git(origin, "tag", "v1")
    (origin / "f").write_text("two")
    git(origin, "commit", "-qam", "two")
    git(origin, "checkout", "-qb", "next")
    (origin / "f").write_text("next")
    git(origin, "commit", "-qam", "next")
    git(origin, "checkout", "-q", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    return origin, clone


def run_script(service, ref):
    return subprocess.run(
        ["bash", "-s", "--", service.repo_dir, ref],
        input=build_script(service), capture_output=True, text=True,
    )


@pytest.mark.parametrize("ref, expected", [("", "two"), ("main", "two"), ("v1", "one"), ("next", "next")])
def test_checkout_refs(repo, ref, expected):
    origin, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])
    r = run_script(service, ref)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (clone / "out").read_text() == expected


def test_checkout_by_hash_and_new_commits(repo):
    origin, clone = repo
    first = git(origin, "rev-parse", "v1")
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])
    assert run_script(service, first[:8]).returncode == 0
    assert (clone / "out").read_text() == "one"

    (origin / "f").write_text("three")
    git(origin, "commit", "-qam", "three")
    assert run_script(service, "").returncode == 0
    assert (clone / "out").read_text() == "three"


def test_failing_step_stops_deploy(repo):
    _, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["false", "touch never"])
    r = run_script(service, "")
    assert r.returncode != 0
    assert not (clone / "never").exists()


def test_step_reading_stdin_does_not_eat_script(repo):
    _, clone = repo
    service = Service(name="s", host="h", repo_dir=str(clone), steps=["cat > /dev/null", "touch after"])
    assert run_script(service, "").returncode == 0
    assert (clone / "after").exists()


def test_unknown_ref_fails(repo):
    _, clone = repo
    r = run_script(Service(name="s", host="h", repo_dir=str(clone)), "nope")
    assert r.returncode != 0


@pytest.mark.parametrize("ref", ["main", "v1.2.3", "feature/x", "abc123f", " main "])
def test_valid_refs(ref):
    assert validate_ref(ref) == ref.strip()


@pytest.mark.parametrize("ref", ["-rf", "a b", "x;rm", "$(id)", "a..b", "`x`"])
def test_invalid_refs(ref):
    with pytest.raises(ValueError):
        validate_ref(ref)


def test_authorize_snippet_is_valid_shell():
    host = Host(name="h", address="x", ctid=101)
    cmd = ssh.authorize_command("ssh-ed25519 AAAA dashboard", host)
    assert cmd.startswith("pct exec 101 -- sh -c ")
    subprocess.run(["sh", "-n", "-c", cmd], check=True)


def test_web_deploy_end_to_end(repo, tmp_path, monkeypatch):
    _, clone = repo
    # Run the "remote" command locally instead of over SSH.
    monkeypatch.setattr(ssh, "ssh_argv", lambda cfg, host, remote: ["sh", "-c", remote])
    monkeypatch.setattr(ssh, "ensure_key", lambda cfg: False)
    cfg = Config(
        data_dir=tmp_path / "data",
        hosts={"h": Host(name="h", address="localhost")},
        services={"s": Service(name="s", host="h", repo_dir=str(clone), steps=["cat f > out"])},
    )
    with TestClient(create_app(cfg)) as client:
        assert "Redeploy" in client.get("/").text
        r = client.post("/services/s/deploy", data={"ref": "v1"})
        job_url = r.headers["HX-Redirect"]
        for _ in range(100):
            if "running" not in client.get(job_url + "/log").text:
                break
            time.sleep(0.05)
        page = client.get(job_url).text
        assert "==&gt; done" in page and "status ok" in page
        assert (clone / "out").read_text() == "one"
        assert "v1" in client.get("/history").text

        bad = client.post("/services/s/deploy", data={"ref": "$(id)"})
        assert "invalid git ref" in bad.text
