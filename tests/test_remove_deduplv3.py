"""Tests for remove-deduplv3.py — clean error contract (rdv3-robust-01)."""
import builtins
import io
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "remove-deduplv3.py"
_spec = importlib.util.spec_from_file_location("remove_deduplv3", _PATH)
rd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rd)


@pytest.mark.parametrize("record", ['h @lostutils-json:"/d/ab\\u0000"\n', "h /d/ab\x00\n"])
def test_rdv3_val_70_nul_paths_are_skipped(record):
    groups, skipped = rd._read_groups([record, "h /safe\n"])
    assert groups == {"h": ["/safe"]}
    assert skipped == 1


@pytest.mark.parametrize("comment_kind", ["saving", "case_conflict"])
def test_rdv3_sec_60_comments_cannot_execute_shell_commands(tmp_path, comment_kind):
    if shutil.which("sh") is None:
        pytest.skip("POSIX shell unavailable")
    payload = "long-survivor-name\nprintf AUDIT_MARKER\n#"
    paths = [payload, payload.upper()] if comment_kind == "case_conflict" else [payload, "x"]
    out = []
    rd._emit_remove_commands({"h": paths}, out.append)
    result = subprocess.run(
        ["sh"], input="rm() { :; }\n" + "".join(out), text=True,
        capture_output=True, cwd=tmp_path, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert "\\n" in "".join(out)


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
    assert rd.detect_encoding(bom + b"h /a\n") == expected


def test_load_groups_peeks_without_reopening_or_losing_prefix(monkeypatch):
    class PeekableBytes(io.BytesIO):
        def peek(self, size):
            start = self.tell()
            return self.getvalue()[start:start + size]

    source = PeekableBytes(b"h /first\nh /second-longer\n")
    opened = []

    def open_once(path, mode):
        opened.append((path, mode))
        return source

    monkeypatch.setattr(builtins, "open", open_once)

    encoding, groups, skipped = rd._load_groups(
        "stream", None, "surrogateescape"
    )

    assert opened == [("stream", "rb")]
    assert encoding == "utf-8"
    assert groups == {"h": ["/first", "/second-longer"]}
    assert skipped == 0


def test_read_groups_decodes_tagged_path_and_skips_invalid_payload():
    groups, skipped = rd._read_groups(
        [
            'h @lostutils-json:"/with\\nnewline"\n',
            "h @lostutils-json:not-json\n",
        ]
    )

    assert groups == {"h": ["/with\nnewline"]}
    assert skipped == 1


def test_help_documents_exit_codes(capsys):
    with pytest.raises(SystemExit) as exc:
        rd.parse_args(["--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Exit codes:" in out
    assert "2 command-line/input/output setup error" in out
    assert "3 encoding/decode error" in out


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
        targets = ln[len(rd.RM_COMMAND_PREFIX) + 1:].split()
        assert len(targets) == len(set(targets)), f"duplicate path in: {ln}"


def test_case_variant_paths_are_never_nominated_for_removal(monkeypatch, tmp_path):
    """rdv3-plat-03: on NTFS/APFS these name one file, so removing either
    deletes the copy the `# saving:` line just promised to keep."""
    out = _run(monkeypatch, tmp_path, "h /photos/x.jpg\nh /Photos/x.jpg\n")

    assert [ln for ln in out.splitlines() if ln.startswith("rm -f")] == []
    assert "SKIPPED" in out
    assert "/photos/x.jpg" in out
    assert "/Photos/x.jpg" in out


def test_case_variants_do_not_block_the_rest_of_a_group(monkeypatch, tmp_path):
    """Only the ambiguous spellings are withheld, not every path sharing the
    hash — but a survivor is still never chosen from among them."""
    out = _run(
        monkeypatch, tmp_path,
        "h /a/x.jpg\nh /A/x.jpg\nh /b/longer-name.jpg\nh /c/y.jpg\n")
    removals = [ln for ln in out.splitlines() if ln.startswith("rm -f")]

    assert removals == ["rm -f -- /c/y.jpg"]
    assert "# saving: /b/longer-name.jpg" in out
    assert "SKIPPED" in out


def test_case_variant_detection_ignores_distinct_names(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "h /a/one.jpg\nh /a/two-longer.jpg\n")

    assert "SKIPPED" not in out
    assert "rm -f -- /a/one.jpg" in out


def test_survivor_splits_on_slash_regardless_of_host_separators(monkeypatch):
    """rdv3-plat-02: the records and the emitted script are POSIX-dialect, so
    the same input must nominate the same survivor on every OS."""
    paths = ["/a/very/long/directory/a.txt", "/b/longer-name.txt"]
    expected = rd._survivor(paths)

    monkeypatch.setattr(rd.os, "sep", "\\")
    monkeypatch.setattr(rd.os, "altsep", "/")
    assert rd._survivor(paths) == expected == "/b/longer-name.txt"


def test_survivor_keeps_a_backslash_in_a_name_out_of_the_basename(monkeypatch):
    """A backslash is a legal filename character on Linux; treating it as a
    separator on Windows would pick a different file to delete."""
    paths = [r"/data/weird\name.txt", "/data/short.txt"]
    expected = rd._survivor(paths)

    monkeypatch.setattr(rd.os, "sep", "\\")
    monkeypatch.setattr(rd.os, "altsep", None)
    assert rd._survivor(paths) == expected == r"/data/weird\name.txt"


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
    assert f"{rd.RM_COMMAND_PREFIX} " in text


def test_emit_remove_commands_terminates_rm_options():
    out = []

    rd._emit_remove_commands(
        {"h": ["-r", "keep-this-longer-survivor-name"]},
        out.append,
    )

    rm_line = next(
        line for line in "".join(out).splitlines()
        if line.startswith(rd.RM_COMMAND_PREFIX)
    )
    assert rd.shlex.split(rm_line) == ["rm", "-f", "--", "-r"]


def test_emit_remove_commands_chunks_large_group():
    """rdv3-scal-01: a group whose removals exceed the per-command byte
    budget is split across multiple `rm -f` lines, none over the limit."""
    survivor = "/tmp/keep-" + "s" * 120 + ".txt"
    paths = [survivor] + [f"/tmp/dup-{i:04d}-" + "x" * 90 + ".txt" for i in range(2000)]
    out = []

    groups, removed = rd._emit_remove_commands({"h": paths}, out.append)

    assert groups == 1
    assert removed == 2000
    rm_lines = [
        line for line in "".join(out).splitlines()
        if line.startswith(f"{rd.RM_COMMAND_PREFIX} ")
    ]
    assert len(rm_lines) > 1
    for ln in rm_lines:
        assert len(ln.encode("utf-8")) <= rd.RM_ARGV_BYTE_LIMIT
    targets = [
        target
        for line in rm_lines
        for target in line[len(rd.RM_COMMAND_PREFIX) + 1:].split()
    ]
    assert len(targets) == 2000
    assert survivor not in targets


def test_emit_remove_commands_uses_plain_messages():
    out = []

    rd._emit_remove_commands({"h": ["/tmp/remove", "/tmp/keep-longer-name"]}, out.append)

    text = "".join(out)
    assert text.startswith(rd.SAFETY_BANNER_TEMPLATE)
    assert "# duplicates: h\n# saving: /tmp/keep-longer-name\n" in text
    assert f"{rd.RM_COMMAND_PREFIX} /tmp/remove" in text


def test_summary_and_error_use_plain_messages(capsys):
    assert rd._format_summary(3, 2, 1, 4) == (
        "summary: 3 hash group(s), 2 with duplicates, "
        "1 file(s) queued for removal, 4 skipped line(s)"
    )
    with pytest.raises(SystemExit) as exc:
        rd._fail("boom", 7)

    assert exc.value.code == 7
    assert capsys.readouterr().err == "error: boom\n"


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


def test_configure_stdout_falls_back_after_reconfigure_failure(monkeypatch):
    class RefusingStream(io.TextIOWrapper):
        def reconfigure(self, **_kwargs):
            raise ValueError("cannot reconfigure")

    stream = RefusingStream(io.BytesIO())
    monkeypatch.setattr(rd.sys, "stdout", stream)

    rd._configure_stdout_errors("surrogateescape")

    assert rd.sys.stdout is not stream
    assert rd.sys.stdout.errors == "surrogateescape"
    rd.sys.stdout.detach()


def test_silence_stdout_handles_open_and_cleanup_failures(monkeypatch):
    class OpenFailure:
        devnull = "/dev/null"
        O_WRONLY = 1

        @staticmethod
        def open(*_args):
            raise OSError("open failed")

    monkeypatch.setattr(rd, "os", OpenFailure())
    rd._silence_stdout_after_broken_pipe()

    class CleanupFailure:
        devnull = "/dev/null"
        O_WRONLY = 1

        @staticmethod
        def open(*_args):
            return 99

        @staticmethod
        def dup2(*_args):
            raise OSError("dup failed")

        @staticmethod
        def close(*_args):
            raise OSError("close failed")

    monkeypatch.setattr(rd, "os", CleanupFailure())
    monkeypatch.setattr(rd.sys, "stdout", object())
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )

    rd._silence_stdout_after_broken_pipe()
