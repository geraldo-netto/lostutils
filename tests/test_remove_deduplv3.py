"""Tests for remove-deduplv3.py — clean error contract (rdv3-robust-01)."""
import builtins
import io
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


@pytest.mark.parametrize(
    ("bom", "expected"),
    [
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"abcd", "utf-8"),
    ],
)
def test_detect_encoding_from_bom(tmp_path, bom, expected):
    f = tmp_path / "hashes.txt"
    f.write_bytes(bom + b"h /a\n")

    assert rd.detect_encoding(f) == expected


def test_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        rd.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Exit codes:" in out
    assert "2 input file error" in out
    assert "3 decode error" in out


def test_invalid_encoding_clean_error_exit3(monkeypatch, tmp_path, capsys):
    f = tmp_path / "hashes.txt"
    f.write_text("h /a\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", "--encoding", "bogus", str(f)])

    with pytest.raises(SystemExit) as exc:
        rd.main()

    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "unknown encoding" in err


def test_strict_decode_error_exits3(monkeypatch, tmp_path, capsys):
    f = tmp_path / "hashes.txt"
    f.write_bytes(b"h /a\xff\n")
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", "--strict", str(f)])

    with pytest.raises(SystemExit) as exc:
        rd.main()

    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "decode error" in err
    assert "try --encoding" in err


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


def test_survivor_uses_platform_separators(monkeypatch):
    monkeypatch.setattr(rd.os, "sep", "\\")
    monkeypatch.setattr(rd.os, "altsep", "/")

    keep = rd._survivor([
        r"C:\very\long\directory\a.txt",
        r"C:\b\longer-name.txt",
    ])

    assert keep == r"C:\b\longer-name.txt"


def test_survivor_tiebreaks_by_lexicographic_path():
    keep = rd._survivor(["/z/aa", "/a/bb", "/m/cc"])

    assert keep == "/z/aa"


def test_emit_remove_commands_shell_quotes_paths():
    out = []

    groups, removed = rd._emit_remove_commands(
        {
            "h": [
                "/tmp/keep-this-longest-survivor-name.txt",
                "/tmp/has spaces.txt",
                "/tmp/quote'and;$dollar.txt",
            ]
        },
        out.append,
    )

    text = "".join(out)
    assert groups == 1
    assert removed == 2
    assert rd.shlex.quote("/tmp/has spaces.txt") in text
    assert rd.shlex.quote("/tmp/quote'and;$dollar.txt") in text
    assert "rm -f " in text


def test_emit_remove_commands_chunks_large_group():
    """rdv3-scal-01: a group whose removals exceed the per-command byte
    budget is split across multiple `rm -f` lines, none over the limit."""
    survivor = "/tmp/keep-" + "s" * 120 + ".txt"
    paths = [survivor] + [f"/tmp/dup-{i:04d}-" + "x" * 90 + ".txt" for i in range(2000)]
    out = []

    groups, removed = rd._emit_remove_commands({"h": paths}, out.append)

    assert groups == 1
    assert removed == 2000
    rm_lines = [ln for ln in "".join(out).splitlines() if ln.startswith("rm -f ")]
    assert len(rm_lines) > 1
    for ln in rm_lines:
        assert len(ln.encode("utf-8")) <= rd.RM_ARGV_BYTE_LIMIT + len("rm -f ")
    targets = [t for ln in rm_lines for t in ln[len("rm -f "):].split()]
    assert len(targets) == 2000
    assert survivor not in targets


def test_emit_remove_commands_uses_translation_hook(monkeypatch):
    def translate(message):
        if message == rd.SAFETY_BANNER_TEMPLATE:
            return "BANNER\n"
        if message == "# duplicates: {hash}\n# saving: {path}\n":
            return "DUP {hash}\nKEEP {path}\n"
        return message

    out = []
    monkeypatch.setattr(rd, "_", translate)

    rd._emit_remove_commands({"h": ["/tmp/remove", "/tmp/keep-longer-name"]}, out.append)

    text = "".join(out)
    assert text.startswith("BANNER\n")
    assert "DUP h\nKEEP /tmp/keep-longer-name\n" in text
    assert "rm -f /tmp/remove" in text


def test_summary_and_error_use_translation_hook(monkeypatch, capsys):
    def translate(message):
        if message.startswith("summary:"):
            return "SUM {group_count}/{dup_count}/{remove_count}"
        if message.startswith(","):
            return " SKIP {skipped_count}"
        if message == "error: {message}":
            return "ERR {message}"
        return message

    monkeypatch.setattr(rd, "_", translate)

    assert rd._format_summary(3, 2, 1, 4) == "SUM 3/2/1 SKIP 4"
    with pytest.raises(SystemExit) as exc:
        rd._fail("boom", 7)

    assert exc.value.code == 7
    assert capsys.readouterr().err == "ERR boom\n"


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


def test_summary_reports_skipped_malformed_lines(monkeypatch, tmp_path, capsys):
    f = tmp_path / "hashes.txt"
    f.write_text("\nmissing-path\nh1 /a\nh1 /bb\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(f)])

    rd.main()

    assert "2 skipped line(s)" in capsys.readouterr().err


def test_broken_pipe_exits_cleanly(monkeypatch, tmp_path):
    f = tmp_path / "hashes.txt"
    f.write_text("h /a\nh /bb\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["remove-deduplv3.py", str(f)])
    monkeypatch.setattr("sys.stdout.write", lambda _s: (_ for _ in ()).throw(BrokenPipeError()))
    silenced = []
    monkeypatch.setattr(rd, "_silence_stdout_after_broken_pipe", lambda: silenced.append(True))

    rd.main()

    assert silenced == [True]


def test_silence_stdout_after_broken_pipe_redirects_fd(monkeypatch):
    class FakeStdout:
        def fileno(self):
            return 7

    class FakeDevnull:
        pass

    opened = []
    duped = []
    closed = []
    replacement = FakeDevnull()
    monkeypatch.setattr(rd.sys, "stdout", FakeStdout())
    monkeypatch.setattr(rd.os, "open", lambda path, flags: opened.append((path, flags)) or 99)
    monkeypatch.setattr(rd.os, "dup2", lambda src, dst: duped.append((src, dst)))
    monkeypatch.setattr(rd.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: replacement)

    rd._silence_stdout_after_broken_pipe()

    assert opened == [(rd.os.devnull, rd.os.O_WRONLY)]
    assert duped == [(99, 7)]
    assert closed == [99]
    assert rd.sys.stdout is replacement


def test_configure_stdout_forces_utf8_and_error_mode(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(rd.sys, "stdout", stream)

    rd._configure_stdout_errors("surrogateescape")

    assert stream.encoding.lower().replace("_", "-") == "utf-8"
    assert stream.errors == "surrogateescape"
    stream.detach()


def test_configure_stdout_wraps_binary_buffer(monkeypatch):
    raw = io.BytesIO()

    class StdoutProxy:
        buffer = raw

    monkeypatch.setattr(rd.sys, "stdout", StdoutProxy())

    rd._configure_stdout_errors("surrogateescape")
    rd.sys.stdout.write("\udcff\n")
    rd.sys.stdout.flush()

    assert raw.getvalue() == b"\xff\n"
    rd.sys.stdout.detach()
