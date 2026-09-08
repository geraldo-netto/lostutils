"""Source filesystem rename capability must be checked before copying."""

import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


@pytest.mark.parametrize("error", [
    OSError(errno.EINVAL, "unsupported flags"),
    OSError(errno.EOPNOTSUPP, "unsupported filesystem"),
    RuntimeError("rename unavailable"),
])
def test_source_probe_failure_prevents_copy(tmp_path, monkeypatch, error):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "destination" / "target"
    original_rename = rf._rename_noreplace

    def reject_source_filesystem(before, after):
        if before.parent == tmp_path:
            assert before != source
            raise error
        original_rename(before, after)

    def forbid_copy(*_args, **_kwargs):
        pytest.fail("copy began before source rename capability was checked")

    monkeypatch.setattr(rf, "_rename_noreplace", reject_source_filesystem)
    monkeypatch.setattr(rf, "_copy_and_verify", forbid_copy)
    with pytest.raises(RuntimeError, match="source filesystem.*before copying"):
        rf.execute(rf.Plan(source=source, target=target, force=True))

    assert (source / "payload").read_bytes() == b"original"
    assert not target.parent.exists()
    assert list(tmp_path.iterdir()) == [source]


def test_successful_probe_uses_private_directory_and_cleans_it(tmp_path, monkeypatch):
    original_rename = rf._rename_noreplace
    calls = []

    def record_rename(before, after):
        assert before.parent == after.parent == tmp_path
        assert before.stat().st_mode & 0o777 == 0o700
        assert list(before.iterdir()) == []
        calls.append((before, after))
        original_rename(before, after)

    monkeypatch.setattr(rf, "_rename_noreplace", record_rename)
    rf._probe_source_rename(tmp_path)
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_probe_cleans_after_rename_then_interrupt(tmp_path, monkeypatch):
    original_rename = rf._rename_noreplace

    def interrupted_rename(before, after):
        original_rename(before, after)
        raise KeyboardInterrupt

    monkeypatch.setattr(rf, "_rename_noreplace", interrupted_rename)
    with pytest.raises(KeyboardInterrupt):
        rf._probe_source_rename(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_probe_preserves_colliding_directory(tmp_path, monkeypatch):
    original_rename = rf._rename_noreplace
    foreign = []

    def collision(before, after):
        after.mkdir()
        (after / "payload").write_bytes(b"unrelated")
        foreign.append(after)
        original_rename(before, after)

    monkeypatch.setattr(rf, "_rename_noreplace", collision)
    with pytest.raises(RuntimeError, match="source filesystem rename preflight"):
        rf._probe_source_rename(tmp_path)
    assert list(tmp_path.iterdir()) == foreign
    assert (foreign[0] / "payload").read_bytes() == b"unrelated"


def test_probe_preserves_unexpected_content_and_warns(tmp_path, monkeypatch, caplog):
    original_rename = rf._rename_noreplace

    def add_content(before, after):
        original_rename(before, after)
        (after / "payload").write_bytes(b"unrelated")

    monkeypatch.setattr(rf, "_rename_noreplace", add_content)
    rf._probe_source_rename(tmp_path)
    remaining, = tmp_path.iterdir()
    assert (remaining / "payload").read_bytes() == b"unrelated"
    assert "could not remove rename probe" in caplog.text


def test_probe_setup_failure_is_actionable(tmp_path, monkeypatch):
    def denied(**_kwargs):
        raise PermissionError("parent not writable")

    monkeypatch.setattr(rf.tempfile, "mkdtemp", denied)
    with pytest.raises(RuntimeError, match="Check parent permissions"):
        rf._probe_source_rename(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_unidentified_probe_is_preserved(tmp_path, monkeypatch, caplog):
    original_lstat = Path.lstat

    def denied_probe(path):
        if path.name.startswith(".relocate-probe-"):
            raise PermissionError("probe stat denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_probe)
    with pytest.raises(RuntimeError, match="probe stat denied"):
        rf._probe_source_rename(tmp_path)
    remaining, = tmp_path.iterdir()
    assert remaining.is_dir()
    assert "could not identify rename probe; inspect" in caplog.text


def test_dry_run_never_probes_source_filesystem(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()

    def forbidden(*_args, **_kwargs):
        pytest.fail("dry-run mutated the source filesystem")

    monkeypatch.setattr(rf.tempfile, "mkdtemp", forbidden)
    plan = rf.Plan(source=source, target=tmp_path / "target", force=True, dry_run=True)
    assert rf.execute(plan).startswith("dry-run:")
    assert list(tmp_path.iterdir()) == [source]
