import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


CHECKER = Path(__file__).resolve().parents[1] / ".github" / "check_governance.py"
SPEC = importlib.util.spec_from_file_location("repository_governance", CHECKER)
assert SPEC is not None and SPEC.loader is not None
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


@pytest.fixture
def repository(tmp_path):
    _git(tmp_path, "init", "--quiet")
    return tmp_path


def _track(repo: Path, name: str, data: str | bytes) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    _git(repo, "add", "--", name)
    return path


def _allow(repo: Path, name: str, rule: str, value: str) -> None:
    entry = {"path": name, "rule": rule, "sha256": check.fingerprint(value), "reason": "Synthetic parser fixture."}
    _track(repo, check.ALLOWLIST, json.dumps([entry]))


@pytest.mark.parametrize("sample,rule", [
    ("/" + "home/private-user/project", "private-home-path"),
    ("/" + "Users/Private User/project", "private-home-path"),
    ("C:" + "\\Users\\Private User\\project", "private-home-path"),
    ("C:" + "\\\\Users\\\\Private User\\\\project", "private-home-path"),
    ("-----BEGIN " + "RSA PRIVATE KEY-----", "private-key"),
    ("-----BEGIN " + "OPENSSH PRIVATE KEY-----", "private-key"),
    ("ghp_" + "a" * 36, "provider-token"),
    ("github_pat_" + "b" * 70, "provider-token"),
    ("sk-proj-" + "c" * 30, "provider-token"),
    ("AKIA" + "A" * 16, "provider-token"),
    ("xoxb-" + "1" * 24, "provider-token"),
    ('api_' + 'key = "synthetic-value"', "credential-literal"),
    ('"client_' + 'secret": "synthetic-value"', "credential-literal"),
    ("export API_" + "KEY=synthetic-value", "credential-literal"),
])
def test_sensitive_literals_detected_without_returning_values(sample, rule):
    findings = check.scan_text("source.py", "safe\n" + sample, set(), set())
    assert findings == [("source.py", 2, rule)]
    assert sample not in repr(findings)


def test_tracked_tests_are_scanned_but_untracked_cache_is_not(repository):
    value = "ghp_" + "a" * 36
    _track(repository, "tests/fixture.py", value)
    cache = repository / "cache"
    cache.mkdir()
    (cache / "download.txt").write_text(value)
    assert check.scan_repository(repository) == [("tests/fixture.py", 1, "provider-token")]


def test_explicit_exception_binds_exact_match_file_and_rule(repository):
    value = "ghp_" + "a" * 36
    _track(repository, "tests/fixture.txt", value)
    _allow(repository, "tests/fixture.txt", "provider-token", value)
    assert check.scan_repository(repository) == []
    _track(repository, "source.py", value)
    assert check.scan_repository(repository) == [("source.py", 1, "provider-token")]
    (repository / "tests/fixture.txt").write_text(value + "b")
    with pytest.raises(check.CheckError, match="unused governance exception"):
        check.scan_repository(repository)


@pytest.mark.parametrize("entry", [
    None, {}, {"path": "file.py", "rule": "provider-token", "sha256": "a" * 64, "reason": ""},
    {"path": "../file.py", "rule": "provider-token", "sha256": "a" * 64, "reason": "fixture"},
    {"path": "/file.py", "rule": "provider-token", "sha256": "a" * 64, "reason": "fixture"},
    {"path": "file.py", "rule": "unknown", "sha256": "a" * 64, "reason": "fixture"},
    {"path": "file.py", "rule": "provider-token", "sha256": "not-a-digest", "reason": "fixture"},
])
def test_invalid_exception_fails_closed(entry):
    with pytest.raises(check.CheckError):
        check.exception_key(entry)


@pytest.mark.parametrize("contents", ["broken json", "{}", b"\xff"])
def test_invalid_exception_file_fails_closed(repository, contents):
    _track(repository, check.ALLOWLIST, contents)
    with pytest.raises(check.CheckError):
        check.load_exceptions(repository)


def test_duplicate_exception_fails_closed(repository):
    entry = {"path": "file.py", "rule": "provider-token", "sha256": "a" * 64, "reason": "fixture"}
    _track(repository, check.ALLOWLIST, json.dumps([entry, entry]))
    with pytest.raises(check.CheckError, match="duplicate"):
        check.load_exceptions(repository)


def test_binary_assets_skipped_but_invalid_source_encoding_rejected(repository):
    _track(repository, "image.png", b"\xff\x00")
    assert check.scan_repository(repository) == []
    path = _track(repository, "source.py", b"\xff")
    with pytest.raises(check.CheckError, match="valid UTF-8"):
        check.scan_repository(repository)
    path.write_bytes(b"a\x00b")
    with pytest.raises(check.CheckError, match="valid UTF-8"):
        check.scan_repository(repository)


def test_deleted_tracked_file_is_reported_without_file_contents(repository):
    path = _track(repository, "source.py", "safe")
    path.unlink()
    with pytest.raises(check.CheckError, match="cannot read tracked file"):
        check.scan_repository(repository)


def test_tracked_symlink_scans_link_target_without_reading_external_file(repository, tmp_path):
    external = tmp_path / "outside.txt"
    external.write_text("ghp_" + "a" * 36)
    link = repository / "link.txt"
    try:
        link.symlink_to(external.name)
    except OSError:
        pytest.skip("symlink creation unavailable")
    _git(repository, "add", "--", "link.txt")
    assert check.scan_repository(repository) == []


def test_git_failure_does_not_echo_subprocess_output(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "git", stderr=b"sensitive failure detail")

    monkeypatch.setattr(check.subprocess, "run", fail)
    with pytest.raises(check.CheckError) as caught:
        check.tracked_paths(tmp_path)
    assert "sensitive failure detail" not in str(caught.value)


def test_main_exit_status_and_redacted_output(repository, capsys):
    assert check.main(["--repo", str(repository)]) == 0
    assert "passed" in capsys.readouterr().out
    value = "sk-proj-" + "z" * 30
    _track(repository, "source.py", value)
    assert check.main(["--repo", str(repository)]) == 1
    output = capsys.readouterr().err
    assert '"source.py":1: provider-token' in output
    assert value not in output
    _track(repository, check.ALLOWLIST, "invalid json")
    assert check.main(["--repo", str(repository)]) == 2


def test_checker_cli_runs_without_third_party_imports(repository):
    result = subprocess.run(
        [sys.executable, "-S", str(CHECKER), "--repo", str(repository)],
        check=False, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout
