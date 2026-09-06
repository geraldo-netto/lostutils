"""Fault-path regressions for descriptor-pinned relocation."""

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


def test_pinned_metadata_reports_chown_failure_and_preserves_other_metadata(
        tmp_path, monkeypatch, caplog):
    source = tmp_path / "source"
    destination = tmp_path / "copy"
    source.write_bytes(b"original")
    destination.write_bytes(b"original")
    source.chmod(0o640)
    os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
    expected = source.stat()

    def denied(*_args, **_kwargs):
        raise PermissionError("ownership denied")

    monkeypatch.setattr(rf.os, "chown", denied)
    with caplog.at_level(logging.WARNING, logger="relocate"):
        rf._apply_pinned_metadata(destination, expected, rf._ChownWarningLimiter())

    actual = destination.stat()
    assert actual.st_mode & 0o777 == 0o640
    assert actual.st_mtime_ns == expected.st_mtime_ns
    assert "ownership denied" in caplog.text
    assert str(destination) in caplog.text
    assert destination.read_bytes() == b"original"


def test_pinned_metadata_permission_failure_propagates_before_timestamp_changes(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "copy"
    source.write_bytes(b"original")
    destination.write_bytes(b"original")
    os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_001_000_000_000))
    before = destination.stat()
    failure = PermissionError("mode denied")

    def denied(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(rf.os, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rf.os, "chmod", denied)
    with pytest.raises(PermissionError) as raised:
        rf._apply_pinned_metadata(destination, source.stat(), rf._ChownWarningLimiter())

    assert raised.value is failure
    assert destination.stat().st_mtime_ns == before.st_mtime_ns
    assert destination.stat().st_mode == before.st_mode


@pytest.fixture
def pinned_directory(tmp_path):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield tmp_path, descriptor
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("replacement", ["file", "directory"])
def test_pinned_open_rejects_replacement_and_closes_descriptor(
        pinned_directory, monkeypatch, replacement):
    root, directory_fd = pinned_directory
    payload = root / "payload"
    payload.write_bytes(b"original")
    expected = payload.stat()
    payload.rename(root / "original")
    if replacement == "file":
        payload.write_bytes(b"replaced")
    else:
        payload.mkdir()
    opened = []
    real_open = os.open

    def tracking_open(name, flags, *args, **kwargs):
        descriptor = real_open(name, flags, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(rf.os, "open", tracking_open)
    with pytest.raises(RuntimeError, match="source entry changed while opening"):
        with rf._open_pinned_file(directory_fd, payload.name, expected):
            pytest.fail("replacement must not become a trusted source stream")

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert (root / "original").read_bytes() == b"original"
    assert os.fstat(directory_fd).st_ino == root.stat().st_ino


def test_pinned_read_detects_same_size_mutation_and_closes_stream(pinned_directory):
    root, directory_fd = pinned_directory
    payload = root / "payload"
    payload.write_bytes(b"original")
    expected = payload.stat()

    with pytest.raises(RuntimeError, match="source entry changed while reading"):
        with rf._open_pinned_file(directory_fd, payload.name, expected) as stream:
            descriptor = stream.fileno()
            assert stream.read() == b"original"
            payload.write_bytes(b"modified")
            os.utime(payload, ns=(expected.st_atime_ns, expected.st_mtime_ns + 1_000_000_000))

    assert stream.closed
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert payload.read_bytes() == b"modified"


def test_pinned_copy_surfaces_directory_walk_error_and_removes_owned_target(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    blocked = source / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "valuable.txt").write_bytes(b"untouched")
    target = tmp_path / "target"
    source_inode = source.stat().st_ino
    failure = PermissionError("access denied during source walk")
    real_open = os.open

    def denied(name, flags, *args, **kwargs):
        directory_fd = kwargs.get("dir_fd")
        if (name == "blocked" and directory_fd is not None
                and os.fstat(directory_fd).st_ino == source_inode):
            raise failure
        return real_open(name, flags, *args, **kwargs)

    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(rf.os, "open", denied)
        with pytest.raises(PermissionError) as raised:
            rf.copy_tree(source, target, source_fd=source_fd, check_space=False)
        assert raised.value is failure
        assert os.fstat(source_fd).st_ino == source_inode
    finally:
        os.close(source_fd)

    assert (blocked / "valuable.txt").read_bytes() == b"untouched"
    assert not target.exists()
    assert not list(tmp_path.glob(".relocate-*"))


def test_strict_pinned_copy_rejects_fifo_before_publication_and_closes_source(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "regular.txt").write_bytes(b"preserved")
    pipe = source / "pipe"
    os.mkfifo(pipe)
    target = tmp_path / "target"
    opened, publications = [], []
    real_source_open = rf._source_identity_fd
    real_rename = rf._rename_noreplace

    def track_source(path):
        descriptor, identity = real_source_open(path)
        opened.append(descriptor)
        return descriptor, identity

    def track_publication(src, dst):
        publications.append(dst)
        return real_rename(src, dst)

    monkeypatch.setattr(rf, "_source_identity_fd", track_source)
    monkeypatch.setattr(rf, "_rename_noreplace", track_publication)
    plan = rf.Plan(source=source, target=target, strict=True, force=True, check_space=False)
    with pytest.raises(RuntimeError, match="strict mode: refusing to migrate, 1 special file"):
        rf.execute(plan)

    assert target not in publications
    assert not target.exists()
    assert (source / "regular.txt").read_bytes() == b"preserved"
    assert pipe.exists()
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_pinned_special_preflight_allows_regular_files_directories_and_symlinks(
        pinned_directory):
    root, directory_fd = pinned_directory
    (root / "directory").mkdir()
    (root / "regular.txt").write_bytes(b"ordinary")
    (root / "link").symlink_to("regular.txt")
    cache = {}

    rf._refuse_pinned_specials(directory_fd, root, cache)

    assert cache == {}
    assert (root / "regular.txt").read_bytes() == b"ordinary"
    assert (root / "link").is_symlink()
