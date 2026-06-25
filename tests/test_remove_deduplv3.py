"""Tests for remove-deduplv3.py — clean error contract (rdv3-robust-01)."""
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "remove-deduplv3.py"
_spec = importlib.util.spec_from_file_location("remove_deduplv3", _PATH)
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


def test_missing_input_clean_error_exit2(monkeypatch, tmp_path, capsys):
    """A missing input file must produce `error:` + exit 2 (the documented
    contract), not an uncaught FileNotFoundError traceback (rdv3-robust-01)."""
    missing = tmp_path / "does-not-exist.txt"
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(missing)])
    with pytest.raises(SystemExit) as exc:
        rd.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith("error:")


def test_unreadable_input_clean_error_exit2(monkeypatch, tmp_path, capsys):
    """A path that exists but cannot be opened (a directory) also routes
    through the clean error path rather than crashing in detect_encoding."""
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        rd.main()
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith("error:")


def _run(monkeypatch, tmp_path, text):
    f = tmp_path / "hashes.txt"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(f)])
    out = []
    monkeypatch.setattr("sys.stdout.write", lambda s: out.append(s) or len(s))
    rd.main()
    return "".join(out)


def test_identical_path_collapsed_not_removed(monkeypatch, tmp_path):
    """rdv3-rel-01: a path duplicated in a group must not be both kept and
    emitted for removal."""
    out = _run(monkeypatch, tmp_path, "h /a\nh /a\n")
    # Only one unique file in the group -> nothing to remove.
    assert "rm -f" not in out
