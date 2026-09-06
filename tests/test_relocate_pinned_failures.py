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
