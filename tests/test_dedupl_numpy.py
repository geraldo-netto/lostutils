"""Focused tests for the legacy NumPy duplicate extractor."""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("numpy")

SCRIPT = Path(__file__).resolve().parent.parent / "dedupl_numpy.py"
SPEC = importlib.util.spec_from_file_location("dedupl_numpy", SCRIPT)
dedupl_numpy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dedupl_numpy)


def test_main_requires_hash_file(monkeypatch, capsys):
    monkeypatch.setattr(dedupl_numpy.sys, "argv", ["dedupl_numpy.py"])

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 1
    assert "Usage:" in capsys.readouterr().out


def test_main_groups_duplicate_hashes(monkeypatch, tmp_path, capfd):
    digest = b"a" * 32
    source = tmp_path / "hashes.txt"
    source.write_bytes(
        digest + b" /data/first.bin\n"
        + digest + b" /data/second.bin\n"
        + b"b" * 32 + b" /data/unique.bin\n"
    )
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    dedupl_numpy.main()

    output = capfd.readouterr().out
    assert "first.bin" in output
    assert "second.bin" in output
    assert "unique.bin" not in output
    assert "equal files: 1 / 3" in output
