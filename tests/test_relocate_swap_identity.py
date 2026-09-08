"""Keep original and replacement data safe across relocation's destructive swap."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


@pytest.mark.parametrize("phase", ["fsync", "rename"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_execute_preserves_source_replaced_after_verification(
        tmp_path, monkeypatch, phase, replacement):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_bytes(b"original contents")
    parked = tmp_path / "parked-original"
    target = tmp_path / "destination" / "source"
    descriptors = []
    open_source = rf._source_identity_fd
    rename = rf._rename_noreplace
    fsync = rf._fsync_tree

    def track_source(path):
        descriptor, identity = open_source(path)
        descriptors.append(descriptor)
        return descriptor, identity

    def replace_source():
        assert os.fstat(descriptors[0]).st_ino == source.stat().st_ino
        source.rename(parked)
        if replacement == "directory":
            source.mkdir()
        else:
            foreign = tmp_path / "foreign"
            foreign.mkdir()
            source.symlink_to(foreign, target_is_directory=True)
        (source / "replacement").write_bytes(b"new data must survive")

    def raced_fsync(path):
        fsync(path)
        if phase == "fsync":
            replace_source()

    def raced_rename(old, new):
        if phase == "rename" and old == source:
            replace_source()
        return rename(old, new)

    monkeypatch.setattr(rf, "_source_identity_fd", track_source)
    monkeypatch.setattr(rf, "_fsync_tree", raced_fsync)
    monkeypatch.setattr(rf, "_rename_noreplace", raced_rename)

    assert rf.main([str(source), str(target.parent), "--force", "--no-space-check"]) == 1
    assert (source / "replacement").read_bytes() == b"new data must survive"
    assert (parked / "original").read_bytes() == b"original contents"
    assert (target / "original").read_bytes() == b"original contents"
    assert source.is_symlink() == (replacement == "symlink")
    assert not source.with_name(source.name + rf.BACKUP_SUFFIX).exists()
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


@pytest.mark.parametrize("restore_error", [
    OSError("denied"), RuntimeError("unsupported rename"), FileExistsError("source occupied"),
])
def test_swap_mismatch_preserves_backup_when_restoration_fails(
        tmp_path, monkeypatch, caplog, restore_error):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_bytes(b"original")
    parked = tmp_path / "parked-original"
    target = tmp_path / "destination" / "source"
    backup = source.with_name(source.name + rf.BACKUP_SUFFIX)
    rename = rf._rename_noreplace

    def raced_rename(old, new):
        if old == source:
            source.rename(parked)
            source.mkdir()
            (source / "replacement").write_bytes(b"preserved")
        if old == backup and new == source:
            if isinstance(restore_error, FileExistsError):
                source.mkdir()
                (source / "latest").write_bytes(b"latest occupant")
            raise restore_error
        return rename(old, new)

    monkeypatch.setattr(rf, "_rename_noreplace", raced_rename)

    assert rf.main([str(source), str(target.parent), "--force", "--no-space-check"]) == 1
    assert (backup / "replacement").read_bytes() == b"preserved"
    assert (parked / "original").read_bytes() == b"original"
    assert (target / "original").read_bytes() == b"original"
    if isinstance(restore_error, FileExistsError):
        assert (source / "latest").read_bytes() == b"latest occupant"
    else:
        assert not source.exists()
    assert "rollback failed" in caplog.text
    assert str(backup) in caplog.text
    assert "is untouched" not in caplog.text
    assert "rm -rf" not in caplog.text


@pytest.mark.parametrize("restore_blocked", [False, True])
def test_backup_deletion_quarantine_preserves_raced_replacement(
        tmp_path, monkeypatch, caplog, restore_blocked):
    backup = tmp_path / "source.relocate-backup"
    backup.mkdir()
    (backup / "original").write_bytes(b"original")
    original_stat = backup.stat()
    expected = (original_stat.st_dev, original_stat.st_ino)
    parked = tmp_path / "parked-original"
    rename = rf._rename_noreplace

    def raced_rename(old, new):
        if old == backup:
            backup.rename(parked)
            backup.mkdir()
            (backup / "replacement").write_bytes(b"unrelated replacement")
        if restore_blocked and new == backup:
            backup.mkdir()
            (backup / "latest").write_bytes(b"latest occupant")
        return rename(old, new)

    monkeypatch.setattr(rf, "_rename_noreplace", raced_rename)

    rf._remove_source_backup(backup, expected)

    assert (parked / "original").read_bytes() == b"original"
    preserved = (
        list(tmp_path.glob(".relocate-cleanup-*/backup/replacement"))
        if restore_blocked else [backup / "replacement"]
    )
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == b"unrelated replacement"
    assert "was substituted" in caplog.text
    if restore_blocked:
        assert (backup / "latest").read_bytes() == b"latest occupant"
        assert "rollback failed" in caplog.text
    else:
        assert not list(tmp_path.glob(".relocate-cleanup-*"))


def test_backup_identity_read_failure_preserves_data_and_names_backup(
        tmp_path, monkeypatch, caplog):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_bytes(b"original")
    target = tmp_path / "destination" / "source"
    backup = source.with_name(source.name + rf.BACKUP_SUFFIX)
    lstat = rf.os.lstat

    def denied(path, *args, **kwargs):
        if path == backup:
            raise PermissionError("backup identity unavailable")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(rf.os, "lstat", denied)

    assert rf.main([str(source), str(target.parent), "--force", "--no-space-check"]) == 1
    assert (backup / "original").read_bytes() == b"original"
    assert (target / "original").read_bytes() == b"original"
    assert "cannot read backup identity; preserving" in caplog.text
    assert str(backup) in caplog.text


def test_restore_fsync_failure_reports_restored_path(tmp_path, monkeypatch, caplog):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "original").write_bytes(b"original")
    expected_stat = backup.stat()
    source = tmp_path / "source"

    def denied(_path):
        raise OSError("fsync unavailable")

    monkeypatch.setattr(rf, "_fsync_directory", denied)
    rf._restore_backup(backup, source, (expected_stat.st_dev, expected_stat.st_ino))

    assert (source / "original").read_bytes() == b"original"
    assert not backup.exists()
    assert "sync restored source" in caplog.text
    assert str(source) in caplog.text
    assert "rollback failed" not in caplog.text


def test_backup_removal_stays_anchored_if_private_parent_is_replaced(tmp_path, monkeypatch):
    backup = tmp_path / "source.relocate-backup"
    backup.mkdir()
    (backup / "original").write_bytes(b"original")
    original_stat = backup.stat()
    parked = tmp_path / "parked-private"
    rmtree = rf.shutil.rmtree
    replacements = []

    def replace_private(path, *args, **kwargs):
        private = next(tmp_path.glob(".relocate-cleanup-*"))
        private.rename(parked)
        private.mkdir(mode=0o700)
        replacement = private / "backup"
        replacement.mkdir()
        (replacement / "valuable").write_bytes(b"unrelated")
        replacements.append(replacement)
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(rf.shutil, "rmtree", replace_private)
    rf._remove_source_backup(backup, (original_stat.st_dev, original_stat.st_ino))

    assert not (parked / "backup").exists()
    assert len(replacements) == 1
    assert (replacements[0] / "valuable").read_bytes() == b"unrelated"
