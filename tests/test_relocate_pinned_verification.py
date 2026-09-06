"""Destination validation for descriptor-pinned directory relocation."""

import hashlib
import os
from pathlib import Path

import pytest

import relocate_folder as rf


@pytest.mark.parametrize("kind", ["missing", "regular file"])
def test_pinned_directory_rejects_missing_or_wrong_kind(tmp_path, kind):
    source = tmp_path / "source" / "nested"
    destination = tmp_path / "copy"
    if kind == "regular file":
        destination.write_bytes(b"not a directory")

    with pytest.raises(RuntimeError, match="missing directory in copy: nested") as error:
        rf._verify_pinned_directory(source, destination, Path("nested"))

    assert str(source) in str(error.value)


def test_pinned_directory_accepts_existing_directory(tmp_path):
    destination = tmp_path / "copy"
    destination.mkdir()

    rf._verify_pinned_directory(tmp_path / "source", destination, Path("nested"))


@pytest.mark.parametrize("kind, payload, message", [
    ("missing", None, "missing file in copy: payload.bin"),
    ("directory", None, "missing file in copy: payload.bin"),
    ("file", b"short", "size mismatch for payload.bin"),
    ("file", b"modified", "hash mismatch for payload.bin"),
])
def test_pinned_file_rejects_incomplete_or_changed_copy(tmp_path, kind, payload, message):
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    destination = tmp_path / "copy.bin"
    if kind == "directory":
        destination.mkdir()
    elif payload is not None:
        destination.write_bytes(payload)
    expected_digest = hashlib.sha256(b"original").hexdigest()

    with pytest.raises(RuntimeError, match=message) as error:
        rf._verify_pinned_file(
            source, destination, Path("payload.bin"), source.stat(), expected_digest)

    assert str(source) in str(error.value)
    assert source.read_bytes() == b"original"


def test_pinned_file_accepts_matching_content(tmp_path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "copy.bin"
    source.write_bytes(b"original")
    destination.write_bytes(b"original")

    rf._verify_pinned_file(
        source, destination, Path("payload.bin"), source.stat(),
        hashlib.sha256(b"original").hexdigest())


@pytest.mark.parametrize("kind", ["missing", "regular file"])
def test_pinned_symlink_rejects_missing_or_wrong_kind(tmp_path, kind):
    source = tmp_path / "source-link"
    destination = tmp_path / "copy-link"
    if kind == "regular file":
        destination.write_bytes(b"not a symlink")

    with pytest.raises(RuntimeError, match="missing symlink in copy: link") as error:
        rf._verify_pinned_symlink(source, "../target", destination, Path("link"))

    assert str(source) in str(error.value)


@pytest.mark.skipif(os.name != "posix", reason="relocation requires POSIX symlink support")
def test_pinned_symlink_rejects_changed_target(tmp_path):
    source = tmp_path / "source-link"
    destination = tmp_path / "copy-link"
    destination.symlink_to("../changed-target")

    with pytest.raises(RuntimeError, match="symlink target mismatch for link") as error:
        rf._verify_pinned_symlink(source, "../expected-target", destination, Path("link"))

    assert "'../expected-target' != '../changed-target'" in str(error.value)
    assert str(source) in str(error.value)
    assert os.readlink(destination) == "../changed-target"


@pytest.mark.skipif(os.name != "posix", reason="relocation requires POSIX symlink support")
def test_pinned_symlink_accepts_matching_relative_target_without_dereferencing(tmp_path):
    destination = tmp_path / "copy-link"
    destination.symlink_to("../absent-target")
    assert not destination.exists()

    rf._verify_pinned_symlink(
        tmp_path / "source-link", "../absent-target", destination, Path("link"))
