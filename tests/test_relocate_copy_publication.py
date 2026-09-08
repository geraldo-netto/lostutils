"""Incomplete copies remain private and never occupy the final destination."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


def _wait_for_marker(process, marker):
    deadline = time.monotonic() + 5
    while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "migration did not reach its controlled interruption point"


def test_sigkill_during_copy_leaves_private_artifact_and_allows_retry(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    payload = b"original data" * 4096
    (source / "payload").write_bytes(payload)
    destination = tmp_path / "destination"
    target = destination / source.name
    marker = tmp_path / "copy-started"
    code = textwrap.dedent("""\
        import sys
        import threading
        from pathlib import Path
        import relocate_folder as rf

        source, destination, marker = map(Path, sys.argv[1:])
        def interrupted_copy(reader, writer):
            writer.write(reader.read(32))
            writer.flush()
            marker.write_text("ready")
            threading.Event().wait(30)
        rf.shutil.copyfileobj = interrupted_copy
        raise SystemExit(rf.main([str(source), str(destination), "--force", "--no-space-check"]))
    """)
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(source), str(destination), str(marker)],
        cwd=Path(rf.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(process, marker)
        assert not target.exists()
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    assert process.returncode < 0
    artifacts = list(destination.glob(".relocate-copy-*"))
    assert len(artifacts) == 1
    assert artifacts[0].stat().st_mode & 0o777 == 0o700
    assert (artifacts[0] / "target" / "payload").read_bytes() == payload[:32]
    assert (source / "payload").read_bytes() == payload

    assert rf.main([str(source), str(destination), "--force"]) == 0
    assert (target / "payload").read_bytes() == payload
    assert artifacts[0].exists()


def test_sigkill_after_backup_rename_is_recoverable_by_fresh_cli(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    payload = b"recover the original bytes" * 4096
    original_file = nested / "payload"
    original_file.write_bytes(payload)
    original_file.chmod(0o640)
    (source / "link").symlink_to("nested/payload")
    original_identity = source.stat().st_ino
    destination = tmp_path / "destination"
    target = destination / source.name
    backup = source.with_name(source.name + rf.BACKUP_SUFFIX)
    marker = tmp_path / "backup-renamed"
    code = textwrap.dedent("""\
        import sys
        import threading
        from pathlib import Path
        import relocate_folder as rf

        source, destination, marker = map(Path, sys.argv[1:])
        original_rename = rf._rename_noreplace
        def interrupted_rename(before, after):
            original_rename(before, after)
            if before == source and after == source.with_name(source.name + rf.BACKUP_SUFFIX):
                marker.write_text("ready")
                threading.Event().wait(30)
        rf._rename_noreplace = interrupted_rename
        raise SystemExit(rf.main([str(source), str(destination), "--force", "--verify-ownership"]))
    """)
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(source), str(destination), str(marker)],
        cwd=Path(rf.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(process, marker)
        assert not source.exists()
        assert backup.stat().st_ino == original_identity
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)

    assert process.returncode < 0
    assert (backup / "nested" / "payload").read_bytes() == payload
    assert (target / "nested" / "payload").read_bytes() == payload
    assert not list(destination.glob(".relocate-copy-*"))
    command = [sys.executable, rf.__file__, str(source), "--recover", "--force"]
    preview = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, timeout=10)
    assert preview.returncode == 0, preview.stderr
    assert "dry-run: would recover" in preview.stderr
    assert not source.exists()
    assert backup.stat().st_ino == original_identity

    restored = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert restored.returncode == 0, restored.stderr
    assert source.stat().st_ino == original_identity
    assert not source.is_symlink()
    assert not backup.exists()
    assert (source / "link").read_bytes() == payload
    assert (source / "nested" / "payload").stat().st_mode & 0o777 == 0o640
    assert (target / "link").read_bytes() == payload


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_copy_preserves_final_destination_created_during_population(
        tmp_path, monkeypatch, replacement):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "target"
    foreign = tmp_path / "foreign" if replacement == "symlink" else target
    populate = rf._populate_pinned_copy

    def competing_destination(source_label, descriptor, staged, *args):
        assert not target.exists()
        assert staged.parent.name.startswith(".relocate-copy-")
        foreign.mkdir()
        (foreign / "payload").write_bytes(b"unrelated")
        if replacement == "symlink":
            target.symlink_to(foreign, target_is_directory=True)
        return populate(source_label, descriptor, staged, *args)

    monkeypatch.setattr(rf, "_populate_pinned_copy", competing_destination)
    descriptor, identity = rf._source_identity_fd(source)
    try:
        with pytest.raises(FileExistsError):
            rf.copy_tree(source, target, source_fd=descriptor)
        assert os.fstat(descriptor).st_ino == identity[1]
    finally:
        os.close(descriptor)

    assert (foreign / "payload").read_bytes() == b"unrelated"
    assert (source / "payload").read_bytes() == b"original"
    assert not list(tmp_path.glob(".relocate-copy-*"))


@pytest.mark.parametrize("phase", ["before", "during"])
def test_publication_preserves_substituted_copy_and_original(tmp_path, monkeypatch, phase):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "target"
    parked = tmp_path / "parked-copy"
    populate = rf._populate_pinned_copy
    rename = rf._rename_noreplace
    replacements = []

    def substitute(staged):
        staged.rename(parked)
        staged.mkdir()
        (staged / "replacement").write_bytes(b"unrelated")
        replacements.append(staged)

    def finish_copy(source_label, descriptor, staged, *args):
        result = populate(source_label, descriptor, staged, *args)
        if phase == "before":
            substitute(staged)
        return result

    def raced_rename(old, new):
        if phase == "during" and new == target:
            substitute(old)
        return rename(old, new)

    monkeypatch.setattr(rf, "_populate_pinned_copy", finish_copy)
    monkeypatch.setattr(rf, "_rename_noreplace", raced_rename)
    descriptor, _identity = rf._source_identity_fd(source)
    try:
        with pytest.raises(RuntimeError, match="copy directory was substituted"):
            rf.copy_tree(source, target, source_fd=descriptor)
    finally:
        os.close(descriptor)

    preserved = replacements[0] if phase == "before" else target
    assert (preserved / "replacement").read_bytes() == b"unrelated"
    assert (parked / "payload").read_bytes() == b"original"
    assert (source / "payload").read_bytes() == b"original"


def test_private_copy_cleanup_after_descriptor_open_failure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "target"
    original_open = os.open

    def denied(path, *args, **kwargs):
        if isinstance(path, Path) and path.parent.name.startswith(".relocate-copy-"):
            raise PermissionError("cannot open private copy")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(rf.os, "open", denied)
    descriptor, _identity = rf._source_identity_fd(source)
    try:
        with pytest.raises(PermissionError, match="cannot open private copy"):
            rf.copy_tree(source, target, source_fd=descriptor)
    finally:
        os.close(descriptor)

    assert not target.exists()
    assert not list(tmp_path.glob(".relocate-copy-*"))
    assert (source / "payload").read_bytes() == b"original"


def test_copy_probes_destination_rename_support_before_population(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "target"
    calls = []

    def unsupported(old, new):
        calls.append((old, new))
        raise OSError(rf.errno.EINVAL, "no-replace rename unsupported")

    def must_not_copy(*_args, **_kwargs):
        pytest.fail("rename capability must be checked before copying")

    monkeypatch.setattr(rf, "_rename_noreplace", unsupported)
    monkeypatch.setattr(rf, "_populate_pinned_copy", must_not_copy)
    descriptor, _identity = rf._source_identity_fd(source)
    try:
        with pytest.raises(OSError, match="no-replace rename unsupported"):
            rf.copy_tree(source, target, source_fd=descriptor)
    finally:
        os.close(descriptor)

    assert len(calls) == 1
    assert not target.exists()
    assert not list(tmp_path.glob(".relocate-copy-*"))
    assert (source / "payload").read_bytes() == b"original"


@pytest.mark.parametrize("phase", ["slot", "publication"])
def test_interruption_after_rename_cleans_the_owned_copy(tmp_path, monkeypatch, phase):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"original")
    target = tmp_path / "target"
    original_rename = rf._rename_noreplace

    def interrupted(old, new):
        result = original_rename(old, new)
        if phase == "publication" and new == target:
            raise KeyboardInterrupt
        if phase == "slot" and old.name == "pending":
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(rf, "_rename_noreplace", interrupted)
    descriptor, _identity = rf._source_identity_fd(source)
    try:
        with pytest.raises(KeyboardInterrupt):
            rf.copy_tree(source, target, source_fd=descriptor)
    finally:
        os.close(descriptor)

    assert not target.exists()
    assert not list(tmp_path.glob(".relocate-copy-*"))
    assert (source / "payload").read_bytes() == b"original"
