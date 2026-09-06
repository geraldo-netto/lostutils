"""Destination validation for descriptor-pinned directory relocation."""

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
