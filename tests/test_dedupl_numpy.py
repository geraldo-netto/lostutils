"""Focused tests for the legacy NumPy duplicate extractor."""

import builtins
import importlib.util
import subprocess
import sys
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

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


def test_main_supports_help(monkeypatch, capsys):
    monkeypatch.setattr(dedupl_numpy.sys, "argv", ["dedupl_numpy.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 0
    assert "hash_file" in capsys.readouterr().out


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

    captured = capfd.readouterr()
    assert "first.bin" in captured.out
    assert "second.bin" in captured.out
    assert "unique.bin" not in captured.out
    assert "equal files:" not in captured.out
    assert "equal files: 1 / 3" in captured.err


def test_group_duplicates_is_pure():
    digest = b"a" * 32
    raw = digest + b" /one\n" + digest + b" /two\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


@pytest.mark.parametrize("hash_width", [40, 64])
def test_group_duplicates_derives_hash_width(hash_width):
    digest = b"a" * hash_width
    raw = digest + b" /one\n" + digest + b" /two\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


@pytest.mark.parametrize(
    "raw,error",
    [
        (b"short\n", "separator"),
        (b"\n", "separator"),
        (b"a" * 32 + b" /one\n" + b"b" * 31 + b" /two\n", "same hash width"),
    ],
)
def test_group_duplicates_rejects_invalid_record_layout(raw, error):
    data = dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8)
    with pytest.raises(ValueError, match=error):
        dedupl_numpy.group_duplicates(data)


def test_main_reports_invalid_record_layout(monkeypatch, tmp_path, capfd):
    source = tmp_path / "hashes.txt"
    source.write_bytes(b"short\n")
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 1
    assert "error:" in capfd.readouterr().err


def test_group_duplicates_includes_final_unterminated_record():
    digest = b"a" * 32
    raw = digest + b" /one\n" + digest + b" /two"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


def test_group_duplicates_strips_crlf_record_terminator():
    digest = b"a" * 32
    raw = digest + b" /one\r\n" + digest + b" /two\r\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


def test_main_closes_mmap(monkeypatch, tmp_path, capfd):
    source = tmp_path / "hashes.txt"
    source.write_bytes(b"a" * 32 + b" /one\n")
    real_mmap = dedupl_numpy.mmap.mmap
    mappings = []
    mmap_calls = []

    def tracked_mmap(*args, **kwargs):
        mmap_calls.append(kwargs)
        mapping = real_mmap(*args, **kwargs)
        mappings.append(mapping)
        return mapping

    monkeypatch.setattr(dedupl_numpy.mmap, "mmap", tracked_mmap)
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    dedupl_numpy.main()

    capfd.readouterr()
    assert mappings[0].closed
    assert mmap_calls == [{"access": dedupl_numpy.mmap.ACCESS_READ}]


def test_main_accepts_empty_input(monkeypatch, tmp_path, capfd):
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    dedupl_numpy.main()

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_main_reports_unreadable_input(monkeypatch, tmp_path, capfd, kind):
    source = tmp_path / kind
    if kind == "directory":
        source.mkdir()
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert f"error: cannot read {source}" in captured.err


def test_group_duplicates_does_not_make_contiguous_gather_copy(monkeypatch):
    digest = b"a" * 32
    raw = digest + b" /one\n" + digest + b" /two\n"

    def fail_ascontiguousarray(*_args, **_kwargs):
        raise AssertionError("unexpected full contiguous copy")

    monkeypatch.setattr(
        dedupl_numpy.np,
        "ascontiguousarray",
        fail_ascontiguousarray,
    )

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


def test_group_duplicates_gathers_hashes_in_bounded_chunks(monkeypatch):
    digests = [b"a" * 32, b"a" * 32, b"b" * 32, b"c" * 32, b"c" * 32]
    raw = b"".join(digest + f" /path/{index}\n".encode() for index, digest in enumerate(digests))
    real_take = dedupl_numpy.np.take
    gathered_shapes = []

    def tracked_take(data, indices, out):
        gathered_shapes.append(indices.shape)
        return real_take(data, indices, out=out)

    monkeypatch.setattr(dedupl_numpy, "HASH_GATHER_CHUNK_RECORDS", 2)
    monkeypatch.setattr(dedupl_numpy.np, "take", tracked_take)

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/path/0", b"/path/1", b"/path/3", b"/path/4"}
    assert equal_files == 2
    assert record_count == 5
    assert gathered_shapes == [(2, 32), (2, 32), (1, 32)]


def test_write_paths_handles_broken_pipe():
    class ClosedPipe:
        def write(self, _data):
            raise BrokenPipeError

        def flush(self):
            raise AssertionError("flush must not follow a failed write")

    assert not dedupl_numpy._write_paths({b"/one"}, ClosedPipe())


def test_broken_pipe_silences_stdout(monkeypatch, tmp_path):
    """dnp-rob-50: a handled BrokenPipeError must also redirect fd 1."""
    f = tmp_path / "hashes.txt"
    f.write_text("aaa /x\naaa /y\n", encoding="utf-8")
    monkeypatch.setattr(dedupl_numpy.sys, "argv", ["dedupl_numpy.py", str(f)])
    monkeypatch.setattr(dedupl_numpy, "_write_paths", lambda *_args: False)
    silenced = []
    monkeypatch.setattr(
        dedupl_numpy, "_silence_stdout_after_broken_pipe",
        lambda: silenced.append(True))

    dedupl_numpy.main()

    assert silenced == [True]


def test_silence_stdout_after_broken_pipe_redirects_fd(monkeypatch):
    class FakeStdout:
        def fileno(self):
            return 7

    replacement = object()
    opened, duped, closed = [], [], []
    monkeypatch.setattr(dedupl_numpy.sys, "stdout", FakeStdout())
    monkeypatch.setattr(
        dedupl_numpy.os, "open", lambda path, flags: opened.append((path, flags)) or 99)
    monkeypatch.setattr(dedupl_numpy.os, "dup2", lambda src, dst: duped.append((src, dst)))
    monkeypatch.setattr(dedupl_numpy.os, "close", closed.append)
    monkeypatch.setattr(builtins, "open", lambda *a, **k: replacement)

    dedupl_numpy._silence_stdout_after_broken_pipe()

    assert opened == [(dedupl_numpy.os.devnull, dedupl_numpy.os.O_WRONLY)]
    assert duped == [(99, 7)]
    assert closed == [99]
    assert dedupl_numpy.sys.stdout is replacement


def test_silence_stdout_tolerates_a_devnull_open_failure(monkeypatch):
    def refuse(_path, _flags):
        raise OSError("no devnull")

    monkeypatch.setattr(dedupl_numpy.os, "open", refuse)
    dedupl_numpy._silence_stdout_after_broken_pipe()   # must not raise


def test_piping_into_a_short_reader_exits_cleanly(tmp_path):
    """dnp-rob-50 end to end: no 'Exception ignored' noise, no exit 120."""
    hashes = tmp_path / "big.txt"
    hashes.write_text(
        "".join(f"{i % 5000:064x} /path/{i}\n" for i in range(40000)),
        encoding="utf-8",
    )
    producer = subprocess.Popen(
        [sys.executable, str(SCRIPT), str(hashes)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert producer.stdout is not None
    producer.stdout.readline()
    producer.stdout.close()
    stderr = producer.stderr.read() if producer.stderr else b""
    if producer.stderr is not None:
        producer.stderr.close()

    assert producer.wait(timeout=60) == 0
    assert b"BrokenPipeError" not in stderr
    assert b"Exception ignored" not in stderr


def test_silence_stdout_survives_a_bad_stdout_descriptor(monkeypatch):
    """dup2 onto an invalid descriptor is tolerated: fd 1 is already gone."""
    class BadFileno:
        def fileno(self):
            return -1        # real os.dup2 raises EBADF

    monkeypatch.setattr(dedupl_numpy.sys, "stdout", BadFileno())

    dedupl_numpy._silence_stdout_after_broken_pipe()   # must not raise

    assert not isinstance(dedupl_numpy.sys.stdout, BadFileno)   # devnull took over


def test_silence_stdout_survives_a_devnull_open_failure(monkeypatch, tmp_path):
    """Every step is best-effort; a devnull that will not open is not fatal."""
    os_mod = dedupl_numpy.os
    spare = os_mod.open(str(tmp_path / "spare"), os_mod.O_WRONLY | os_mod.O_CREAT)

    class SpareStdout:
        def fileno(self):
            return spare     # dup2 lands on our own fd, not pytest's

    monkeypatch.setattr(dedupl_numpy.sys, "stdout", SpareStdout())
    monkeypatch.setattr(
        builtins, "open",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no devnull")))
    try:
        dedupl_numpy._silence_stdout_after_broken_pipe()   # must not raise
    finally:
        monkeypatch.undo()
        os_mod.close(spare)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"/tmp/plain", b"/tmp/plain"),
        (b'@lostutils-json:"  /tmp/edged  "', b"  /tmp/edged  "),
        (b'@lostutils-json:"/tmp/a\\nb"', b"/tmp/a\nb"),
        (b'@lostutils-json:""', b""),
        (b'@lostutils-json:"/tmp/caf\\u00e9"', "/tmp/café".encode()),
        (b'@lostutils-json:"\\udcff"', b"\xff"),          # non-UTF-8 filename
        (b"@lostutils-json:not-json", None),
        (b'@lostutils-json:42', None),                     # not a string
        (b'@lostutils-json:"\xff"', None),                 # payload not UTF-8
    ],
)
def test_decode_record_path_round_trips_every_escape(raw, expected):
    """dnp-api-50: the escaped form is a path, not a literal token."""
    assert dedupl_numpy._decode_record_path(raw) == expected


def test_decode_record_path_matches_the_producer(monkeypatch):
    """The decoder is the inverse of hash-recursive-ai5.py's encoder."""
    hr_path = Path(__file__).resolve().parent.parent / "hash-recursive-ai5.py"
    spec = importlib.util.spec_from_file_location("hr_for_dnp", hr_path)
    hr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hr)

    for original in ["/tmp/plain", "  /tmp/edged  ", "/tmp/a\nb", "/tmp/café", ""]:
        encoded = hr._encode_record_path(original).encode()
        assert dedupl_numpy._decode_record_path(encoded) == original.encode()


def test_decode_paths_holds_back_newlines_in_line_mode():
    escaped = b'@lostutils-json:"/tmp/a\\nb"'
    paths, unreadable, unprintable = dedupl_numpy._decode_paths(
        {escaped, b'@lostutils-json:"  x  "', b"@lostutils-json:junk"}, True)

    assert paths == {escaped, b"  x  "}    # the newline path stays escaped
    assert (unreadable, unprintable) == (1, 1)


def test_decode_paths_emits_newlines_when_not_line_oriented():
    paths, unreadable, unprintable = dedupl_numpy._decode_paths(
        {b'@lostutils-json:"/tmp/a\\nb"'}, False)

    assert paths == {b"/tmp/a\nb"}
    assert (unreadable, unprintable) == (0, 0)


@pytest.mark.parametrize(
    "unreadable, unprintable, expected",
    [
        (0, 0, []),
        (2, 0, ["could not be decoded"]),
        (0, 3, ["--print0"]),
        (1, 1, ["could not be decoded", "--print0"]),
    ],
)
def test_report_decode_warnings(capsys, unreadable, unprintable, expected):
    dedupl_numpy._report_decode_warnings(unreadable, unprintable)

    err = capsys.readouterr().err
    assert [fragment for fragment in expected if fragment in err] == expected
    assert bool(err.strip()) == bool(expected)


def test_write_paths_honours_a_nul_separator():
    class Sink:
        def __init__(self):
            self.chunks = []

        def write(self, data):
            self.chunks.append(data)

        def flush(self):
            pass

    sink = Sink()
    assert dedupl_numpy._write_paths({b"/b", b"/a"}, sink, b"\0")
    assert sink.chunks == [b"/a\0", b"/b\0"]


def _hash_file(tmp_path):
    f = tmp_path / "hashes.txt"
    f.write_bytes(
        b'a' * 32 + b' @lostutils-json:"/tmp/a\\nb"\n'
        + b'a' * 32 + b' @lostutils-json:"  edged  "\n'
    )
    return f


def test_main_decodes_escaped_paths(monkeypatch, tmp_path, capsysbinary):
    monkeypatch.setattr(
        dedupl_numpy.sys, "argv", ["dedupl_numpy.py", str(_hash_file(tmp_path))])

    dedupl_numpy.main()

    captured = capsysbinary.readouterr()
    assert b"  edged  \n" in captured.out                    # decoded
    assert b'@lostutils-json:"/tmp/a\\nb"\n' in captured.out  # held back
    assert b"--print0" in captured.err


def test_main_print0_emits_newline_paths_literally(
        monkeypatch, tmp_path, capsysbinary):
    monkeypatch.setattr(
        dedupl_numpy.sys, "argv",
        ["dedupl_numpy.py", "--print0", str(_hash_file(tmp_path))])

    dedupl_numpy.main()

    captured = capsysbinary.readouterr()
    assert captured.out == b"  edged  \0/tmp/a\nb\0"
    assert b"--print0" not in captured.err
