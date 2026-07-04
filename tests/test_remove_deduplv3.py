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
    assert not [ln for ln in out.splitlines() if ln.startswith("rm -f")]


def test_no_duplicate_path_in_rm_line(monkeypatch, tmp_path):
    """rdv3-rel-02: a path repeated under one hash must not produce
    `rm -f /a /a`; the per-group dedup (rdv3-rel-01) prevents it."""
    out = _run(monkeypatch, tmp_path, "h /a\nh /a\nh /b\n")
    rm_lines = [ln for ln in out.splitlines() if ln.startswith("rm -f")]
    assert rm_lines
    for ln in rm_lines:
        targets = ln[len("rm -f "):].split()
        assert len(targets) == len(set(targets)), f"duplicate path in: {ln}"


def test_output_starts_with_destructive_command_warning(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "h /a\nh /bb\n")

    assert out.startswith("# WARNING: generated destructive rm -f commands.\n")
    assert "Review this file before piping it to sh" in out.splitlines()[1]


def test_emits_stderr_summary(monkeypatch, tmp_path, capsys):
    """rdv3-obs-01: a stderr summary reports group + removal counts."""
    f = tmp_path / "hashes.txt"
    f.write_text("h1 /a\nh1 /bb\nh2 /c\n", encoding="utf-8")  # h1 dup, h2 single
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(f)])
    rd.main()
    err = capsys.readouterr().err
    assert "summary:" in err
    assert "2 hash group(s)" in err
    assert "1 with duplicates" in err
    assert "1 file(s) queued for removal" in err
