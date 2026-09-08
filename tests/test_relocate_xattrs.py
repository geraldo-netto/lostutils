"""Extended metadata must survive a pinned relocation or retain its source."""

import os
import struct
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(not hasattr(os, "setxattr"), reason="native xattr tests need Linux")


@pytest.fixture
def source_tree(tmp_path):
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (nested / "payload").write_bytes(b"original")
    try:
        os.setxattr(source, "user.auditdir", b"root directory")
        os.setxattr(nested, "user.auditdir", b"nested directory")
        os.setxattr(nested / "payload", "user.audit", b"file\x00metadata")
    except OSError as exc:
        pytest.skip(f"test filesystem cannot store user xattrs: {exc}")
    return source


def test_execute_preserves_file_nested_and_root_xattrs(source_tree, tmp_path):
    destination = tmp_path / "destination"

    assert rf.main([str(source_tree), str(destination), "--force", "--verify-ownership"]) == 0

    target = destination / source_tree.name
    assert os.getxattr(target, "user.auditdir") == b"root directory"
    assert os.getxattr(target / "nested", "user.auditdir") == b"nested directory"
    assert os.getxattr(target / "nested" / "payload", "user.audit") == b"file\x00metadata"


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("failure", ["denied", "discarded"])
def test_copy_retains_source_when_xattrs_cannot_be_reproduced(
        source_tree, tmp_path, monkeypatch, caplog, verify, failure):
    destination = tmp_path / "destination"

    def fail_set(_descriptor, _name, _value):
        if failure == "denied":
            raise PermissionError("xattr write denied")

    monkeypatch.setattr(rf.os, "setxattr", fail_set)
    options = [] if verify else ["--no-verify"]

    assert rf.main([str(source_tree), str(destination), "--force", *options]) == 1
    assert not source_tree.is_symlink()
    assert (source_tree / "nested" / "payload").read_bytes() == b"original"
    assert os.getxattr(source_tree / "nested" / "payload", "user.audit") == b"file\x00metadata"
    assert not (destination / source_tree.name).exists()
    assert "xattr" in caplog.text.lower()


def test_execute_preserves_acl_attributes(source_tree, tmp_path):
    payload = source_tree / "nested" / "payload"
    entries = [(1, 6, 0xFFFFFFFF), (2, 4, os.getuid() + 1), (4, 4, 0xFFFFFFFF),
               (16, 4, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(payload, "system.posix_acl_access", acl)
        os.setxattr(source_tree, "system.posix_acl_default", acl)
    except OSError as exc:
        pytest.skip(f"test filesystem cannot store POSIX ACLs: {exc}")
    destination = tmp_path / "destination"

    assert rf.main([str(source_tree), str(destination), "--force", "--verify-ownership"]) == 0
    target = destination / source_tree.name
    assert os.getxattr(target / "nested" / "payload", "system.posix_acl_access") == acl
    assert os.getxattr(target, "system.posix_acl_default") == acl


def test_verify_detects_xattr_corruption_before_swap(source_tree, tmp_path, monkeypatch):
    target = tmp_path / "destination" / source_tree.name
    original_copy = rf.copy_tree

    def corrupt_xattr(*args, **kwargs):
        result = original_copy(*args, **kwargs)
        os.setxattr(target / "nested" / "payload", "user.audit", b"tampered")
        return result

    monkeypatch.setattr(rf, "copy_tree", corrupt_xattr)
    assert rf.main([str(source_tree), str(target.parent), "--force"]) == 1
    assert not source_tree.is_symlink()
    assert (source_tree / "nested" / "payload").read_bytes() == b"original"
    assert not target.exists()


def test_sync_xattrs_removes_inherited_values_and_preserves_matching_values(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(b"payload")
    os.setxattr(target, "user.extra", b"inherited")
    os.setxattr(target, "user.keep", b"matching")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        monkeypatch.setattr(rf.os, "setxattr", mock.Mock(side_effect=AssertionError("unneeded write")))
        rf._sync_xattrs(descriptor, {"user.keep": b"matching"})
        assert rf._read_xattrs(descriptor) == {"user.keep": b"matching"}
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("error", [rf.errno.ENOTSUP, rf.errno.EACCES])
def test_read_xattrs_distinguishes_unsupported_filesystem_from_failed_access(
        tmp_path, monkeypatch, error):
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(rf.os, "listxattr", mock.Mock(side_effect=OSError(error, "xattr failed")))
        if error == rf.errno.ENOTSUP:
            assert rf._read_xattrs(descriptor) == {}
        else:
            with pytest.raises(OSError) as raised:
                rf._read_xattrs(descriptor)
            assert raised.value.errno == error
    finally:
        os.close(descriptor)


def test_missing_xattr_backend_fails_closed(monkeypatch):
    monkeypatch.setattr(rf.sys, "platform", "unknown-posix")
    monkeypatch.delattr(rf.os, "listxattr")
    with pytest.raises(RuntimeError, match="source metadata cannot be preserved"):
        rf._xattr_backend()


def test_darwin_xattrs_use_native_fd_apis_and_preserve_binary_values(monkeypatch):
    values = {}

    def read_value(value, buffer, size):
        if buffer is not None:
            assert size >= len(value)
            rf.ctypes.memmove(buffer, value, len(value))
        return len(value)

    def list_names(descriptor, buffer, size, options):
        assert descriptor == 17 and options == 0
        return read_value(b"\0".join(values) + (b"\0" if values else b""), buffer, size)

    def get_value(descriptor, name, buffer, size, position, options):
        assert (descriptor, position, options) == (17, 0, 0)
        return read_value(values[name], buffer, size)

    def set_value(descriptor, name, value, size, position, options):
        assert (descriptor, position, options) == (17, 0, 0)
        values[name] = value[:size]
        return 0

    def remove_value(descriptor, name, options):
        assert (descriptor, options) == (17, 0)
        del values[name]
        return 0

    native = SimpleNamespace(
        flistxattr=mock.Mock(side_effect=list_names),
        fgetxattr=mock.Mock(side_effect=get_value),
        fsetxattr=mock.Mock(side_effect=set_value),
        fremovexattr=mock.Mock(side_effect=remove_value),
    )
    monkeypatch.setattr(rf.sys, "platform", "darwin")
    monkeypatch.setattr(rf.ctypes, "CDLL", lambda *_args, **_kwargs: native)
    backend = rf._xattr_backend()

    assert backend.listxattr(17) == []
    backend.setxattr(17, "com.example.audit", b"binary\0value")
    assert backend.listxattr(17) == ["com.example.audit"]
    assert backend.getxattr(17, "com.example.audit") == b"binary\0value"
    backend.removexattr(17, "com.example.audit")
    assert backend.listxattr(17) == []
    assert native.fgetxattr.restype is rf.ctypes.c_ssize_t
    assert native.fsetxattr.restype is rf.ctypes.c_int


def test_native_xattr_failure_propagates_errno():
    original_errno = rf.ctypes.get_errno()
    try:
        rf.ctypes.set_errno(rf.errno.ERANGE)
        with pytest.raises(OSError) as raised:
            rf._checked_xattr_call(lambda: -1)
        assert raised.value.errno == rf.errno.ERANGE
    finally:
        rf.ctypes.set_errno(original_errno)


def test_xattrs_follow_pinned_source_after_path_replacement(source_tree, tmp_path, monkeypatch):
    descriptor, _identity = rf._source_identity_fd(source_tree)
    parked = tmp_path / "parked"
    target = tmp_path / "target"
    list_xattrs = rf.os.listxattr
    observed = []

    def require_descriptor(value):
        assert isinstance(value, int)
        observed.append(value)
        return list_xattrs(value)

    try:
        source_tree.rename(parked)
        source_tree.mkdir()
        os.setxattr(source_tree, "user.auditdir", b"replacement")
        monkeypatch.setattr(rf.os, "listxattr", require_descriptor)
        rf.copy_tree(source_tree, target, source_fd=descriptor)
        rf.verify_copy(source_tree, target, source_fd=descriptor, checksum=True)
    finally:
        os.close(descriptor)

    assert observed
    assert os.getxattr(target, "user.auditdir") == b"root directory"
    assert os.getxattr(target / "nested" / "payload", "user.audit") == b"file\x00metadata"
    assert os.getxattr(source_tree, "user.auditdir") == b"replacement"
    assert os.getxattr(parked, "user.auditdir") == b"root directory"


@pytest.mark.parametrize("phase", ["open", "read"])
def test_pinned_directory_metadata_rejects_changes_and_closes_descriptor(
        tmp_path, monkeypatch, phase):
    child = tmp_path / "child"
    child.mkdir()
    expected = child.stat()
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    opened = []
    real_open = os.open

    def track_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    if phase == "open":
        child.rename(tmp_path / "parked")
        child.mkdir()
    monkeypatch.setattr(rf.os, "open", track_open)
    try:
        with pytest.raises(RuntimeError, match="source directory changed"):
            with rf._open_pinned_directory(parent_fd, child.name, expected):
                os.utime(child, ns=(expected.st_atime_ns, expected.st_mtime_ns + 1_000_000_000))
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])
    finally:
        os.close(parent_fd)


def test_pinned_copy_propagates_walker_error_and_preserves_source(source_tree, tmp_path, monkeypatch):
    target = tmp_path / "target"
    descriptor, _identity = rf._source_identity_fd(source_tree)
    failure = PermissionError("walker cannot enumerate source")

    def failed_walk(*_args, **kwargs):
        assert kwargs["dir_fd"] == descriptor
        kwargs["onerror"](failure)
        yield from ()

    monkeypatch.setattr(rf.os, "fwalk", failed_walk)
    try:
        with pytest.raises(PermissionError) as raised:
            rf.copy_tree(source_tree, target, source_fd=descriptor, check_space=False)
        assert raised.value is failure
    finally:
        os.close(descriptor)

    assert not target.exists()
    assert (source_tree / "nested" / "payload").read_bytes() == b"original"
