"""Tests for deduplicate-by-namev3.py — dnv3-rel-* reliability fixes
(threshold clamp against uint8 overflow, word-boundary cleanup)."""
import io
import csv
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rapidfuzz")
from hypothesis import given, strategies as st

_PATH = Path(__file__).resolve().parent.parent / "deduplicate-by-namev3.py"
_spec = importlib.util.spec_from_file_location("deduplicate_by_namev3", _PATH)
dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dn)

TEST_WORD_TOKENS = ("xxx", "monography")


# --- dnv3-rel-01: threshold clamped to <= MAX_THRESHOLD (uint8 safety) -------

def test_clamp_threshold_passthrough_default():
    assert dn.clamp_threshold(dn.DEFAULT_THRESHOLD) == dn.DEFAULT_THRESHOLD


def test_clamp_threshold_at_limit_unchanged():
    assert dn.clamp_threshold(dn.MAX_THRESHOLD) == dn.MAX_THRESHOLD


def test_clamp_threshold_over_limit_is_clamped(capsys):
    assert dn.clamp_threshold(300) == dn.MAX_THRESHOLD
    err = capsys.readouterr().err
    assert "clamp" in err.lower()
    assert str(dn.MAX_THRESHOLD) in err


def test_clamp_threshold_no_warning_within_limit(capsys):
    dn.clamp_threshold(0)
    assert capsys.readouterr().err == ""


@given(st.integers(min_value=-10, max_value=10_000))
def test_clamp_threshold_never_exceeds_limit(t):
    assert dn.clamp_threshold(t) <= dn.MAX_THRESHOLD


@given(st.integers(min_value=-10, max_value=dn.MAX_THRESHOLD))
def test_clamp_threshold_identity_within_range(t):
    assert dn.clamp_threshold(t) == t


# --- end-to-end: large -t does not wrap / corrupt grouping ------------------

def _run_main(monkeypatch, tmp_path, lines, threshold):
    f = tmp_path / "in.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        dn.sys, "argv",
        ["prog", str(f), "-t", str(threshold), "-w", "1"],
    )
    out = []
    monkeypatch.setattr(dn.sys.stdout, "write", lambda s: out.append(s) or len(s))
    dn.main()
    return "".join(out)


def _cleanup_default(text):
    return dn.cleanup(text, dn.REPLACEMENTS, dn.compile_word_re(dn.WORD_TOKENS))


def _cleanup_with_tokens(text, tokens=TEST_WORD_TOKENS):
    return dn.cleanup(text, dn.REPLACEMENTS, dn.compile_word_re(tokens))


def test_main_large_threshold_clamped_not_wrapped(monkeypatch, tmp_path):
    # Two far-apart strings; an unclamped uint8 wrap could falsely match them.
    lines = ["a", "z" * 50]
    out = _run_main(monkeypatch, tmp_path, lines, 300)
    # distance is 50, well under clamp(254); no pair should be reported as 0-ish wrap
    for ln in out.splitlines():
        parts = ln.split(";")
        assert int(parts[2]) <= dn.MAX_THRESHOLD


def test_main_reports_self_collision(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, ["dup", "dup"], 7)
    assert "dup;dup;0" in out


def test_main_reports_near_pair(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, ["hello", "hallo"], 7)
    assert any(line.endswith(";1") for line in out.splitlines())


def test_main_empty_file_noop(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, [""], 7)
    assert out == ""


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_main_reports_input_open_error(monkeypatch, tmp_path, capsys, kind):
    source = tmp_path / kind
    if kind == "directory":
        source.mkdir()
    monkeypatch.setattr(dn.sys, "argv", ["prog", str(source)])

    with pytest.raises(SystemExit) as exc:
        dn.main()

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"error: cannot read {source}" in captured.err


def test_main_reports_cleaned_empty_lines(monkeypatch, tmp_path, capsys):
    f = tmp_path / "in.txt"
    f.write_text("\nxxx\nalpha\n", encoding="utf-8")
    monkeypatch.setattr(
        dn.sys,
        "argv",
        ["prog", str(f), "--word-tokens", "xxx", "-w", "1"],
    )

    dn.main()

    assert "dropped 2 empty cleaned line(s)" in capsys.readouterr().err


def test_main_reports_all_cleaned_empty_before_noop(monkeypatch, tmp_path, capsys):
    f = tmp_path / "in.txt"
    f.write_text("\nxxx\n", encoding="utf-8")
    monkeypatch.setattr(
        dn.sys,
        "argv",
        ["prog", str(f), "--word-tokens", "xxx", "-w", "1"],
    )

    dn.main()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dropped 2 empty cleaned line(s)" in captured.err


def test_load_cleaned_lines_keeps_line_numbers_as_source_of_truth(tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("same\nsame\nother\n", encoding="utf-8")
    line_nums, dropped = dn._load_cleaned_lines(
        f,
        dn.REPLACEMENTS,
        dn.compile_word_re(dn.WORD_TOKENS),
    )

    assert line_nums == {"same": [1, 2], "other": [3]}
    assert dropped == 0


# --- dnv3-rel-02: cleanup strips only standalone "xxx"/"monography" ---------

def test_cleanup_strips_standalone_tokens():
    assert _cleanup_with_tokens("xxx monography") == ""


def test_cleanup_keeps_words_containing_tokens():
    assert _cleanup_with_tokens("xxxl") == "xxxl"
    assert _cleanup_with_tokens("monographymania") == "monographymania"
    assert _cleanup_with_tokens("amonography") == "amonography"


def test_cleanup_strips_token_among_words():
    assert _cleanup_with_tokens("my xxx file") == "my file"


def test_cleanup_basic_replacements_and_case():
    assert _cleanup_default("  A,[B] ") == "ab"


def test_cleanup_accepts_custom_replacements_and_word_tokens():
    word_re = dn.compile_word_re(("skip",))

    assert dn.cleanup("Foo# skip", ("#",), word_re) == "foo"


def test_parse_word_tokens_trims_and_drops_empty_items():
    assert dn.parse_word_tokens(" alpha, ,Beta ") == ("alpha", "beta")


def test_configure_stdout_forces_utf8_and_surrogateescape(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(dn.sys, "stdout", stream)

    dn.configure_stdout()

    assert stream.encoding.lower().replace("_", "-") == "utf-8"
    assert stream.errors == "surrogateescape"
    stream.detach()


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12))
def test_cleanup_never_strips_token_inside_longer_word(word):
    # A pure-alpha word longer than a token and containing it as a substring,
    # but not equal to it, must survive intact (no boundary).
    for tok in TEST_WORD_TOKENS:
        if tok in word and word != tok:
            assert _cleanup_with_tokens(word) == word


@given(st.lists(st.sampled_from(["xxx", "monography", "alpha", "beta"]),
                min_size=1, max_size=6))
def test_cleanup_drops_all_standalone_tokens(words):
    result = _cleanup_with_tokens(" ".join(words))
    for tok in TEST_WORD_TOKENS:
        assert tok not in result.split()


def test_default_word_tokens_empty():
    args = dn._build_parser().parse_args(["names.txt"])

    assert args.word_tokens == ()
    assert _cleanup_default("xxx monography") == "xxx monography"


# --- dnv3-rel-01: matrix-mask vs self-collision semantics (no double-count) --

def _parse_rows(out):
    rows = []
    for ln in out.splitlines():
        if not ln or ln.startswith("#"):  # skip blank + `# source lines:` comments
            continue
        a, b, d = next(csv.reader([ln], delimiter=";"))
        rows.append((a, b, int(d)))
    return rows


def test_self_collision_not_double_counted_with_cross_pair(monkeypatch, tmp_path):
    # "dup" appears twice (self-collision) and is also near "dap" (cross-pair).
    # The dup;dup;0 self report must appear exactly once and never be emitted
    # as an off-diagonal pair.
    out = _run_main(monkeypatch, tmp_path, ["dup", "dup", "dap"], 7)
    rows = _parse_rows(out)
    assert rows.count(("dup", "dup", 0)) == 1
    # No distance-0 row between two *distinct* cleaned strings.
    assert all(d != 0 for a, b, d in rows if a != b)


def test_no_zero_distance_between_distinct_strings(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, ["abc", "abd", "xyz"], 7)
    for a, b, d in _parse_rows(out):
        if a != b:
            assert d > 0


def test_distinct_buckets_never_distance_zero(monkeypatch, tmp_path):
    # Three raw forms collapsing to two distinct cleaned buckets; the shared
    # bucket self-reports, the distinct buckets are never a 0-distance pair.
    out = _run_main(monkeypatch, tmp_path, ["same", "same", "other"], 7)
    rows = _parse_rows(out)
    assert ("same", "same", 0) in rows
    assert all(not (a != b and d == 0) for a, b, d in rows)


def test_threshold_at_cap_excludes_clipped_cells(monkeypatch, tmp_path):
    # At the 254 cap, an above-threshold cell is clipped to 255; with strings
    # whose distance exceeds 254 no pair must be reported (no uint8 wrap to 0).
    lines = ["a" * 1, "z" * 260]
    out = _run_main(monkeypatch, tmp_path, lines, dn.MAX_THRESHOLD)
    cross = [(a, b, d) for a, b, d in _parse_rows(out) if a != b]
    assert cross == []


def test_pair_emitted_only_in_upper_triangle(monkeypatch, tmp_path):
    # A near pair must be reported exactly once (upper triangle, k=1), not
    # mirrored as both (i,j) and (j,i).
    out = _run_main(monkeypatch, tmp_path, ["hello", "hallo"], 7)
    cross = [(a, b) for a, b, d in _parse_rows(out) if a != b]
    assert len(cross) == 1
    a, b = cross[0]
    assert (b, a) not in cross


def _run_main_isolated(tmp_path_factory, lines, threshold):
    import contextlib
    import io

    d = tmp_path_factory.mktemp("dn")
    f = d / "in.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    argv = ["prog", str(f), "-t", str(threshold), "-w", "1"]
    raw_output = io.BytesIO()
    buf = io.TextIOWrapper(raw_output, encoding="utf-8")
    old_argv = dn.sys.argv
    dn.sys.argv = argv
    try:
        with contextlib.redirect_stdout(buf):
            dn.main()
            buf.flush()
    finally:
        dn.sys.argv = old_argv
    output = raw_output.getvalue().decode("utf-8")
    buf.detach()
    return output


@given(st.lists(st.text(alphabet="abcde", min_size=1, max_size=6),
                min_size=1, max_size=8),
       st.integers(min_value=0, max_value=dn.MAX_THRESHOLD))
def test_matrix_no_zero_distance_off_diagonal(tmp_path_factory, lines, t):
    # Property: across arbitrary inputs/thresholds, distinct cleaned strings
    # are never reported at distance 0, and clipped cells never wrap below the
    # threshold (every reported distance is within the threshold).
    out = _run_main_isolated(tmp_path_factory, lines, t)
    for a, b, d in _parse_rows(out):
        assert d <= t
        if a != b:
            assert d > 0


# --- dnv3-perf-01: row-blocked matrix produces identical output -------------

def _emit(cleaned_strs, threshold, workers=1):
    out = []
    dn.emit_pairs(cleaned_strs, threshold, workers, lambda s: out.append(s))
    return set(out)


def test_emit_pairs_blocked_matches_single_call(monkeypatch):
    strs = ["hello", "hallo", "help", "world", "word", "abc", "abd", "zzzz"]
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 10 ** 9)
    single = _emit(strs, 3)
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(dn, "BLOCK_ROWS", 3)
    blocked = _emit(strs, 3)
    assert single == blocked


def test_emit_pairs_block_size_one(monkeypatch):
    strs = ["aaa", "aab", "abb", "bbb", "ccc"]
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 10 ** 9)
    single = _emit(strs, 2)
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(dn, "BLOCK_ROWS", 1)
    assert _emit(strs, 2) == single


def test_emit_pairs_upper_triangle_only(monkeypatch):
    strs = ["cat", "car", "can"]
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(dn, "BLOCK_ROWS", 1)
    pairs = [tuple(s.rstrip("\n").split(";")[:2]) for s in _emit(strs, 5)]
    seen = set()
    for a, b in pairs:
        assert (b, a) not in seen  # no mirrored duplicate
        assert a != b  # no diagonal
        seen.add((a, b))


def test_main_large_n_blocked_path(monkeypatch, tmp_path):
    # Force the blocked path through main() and confirm a known near pair shows.
    monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 0)
    monkeypatch.setattr(dn, "BLOCK_ROWS", 2)
    lines = ["alpha", "alpht", "gamma", "delta", "delts"]
    out = _run_main(monkeypatch, tmp_path, lines, 2)
    rows = _parse_rows(out)
    cross = {(a, b) for a, b, d in rows if a != b}
    assert ("alpha", "alpht") in cross
    assert ("delta", "delts") in cross


@given(st.lists(st.text(alphabet="abc", min_size=1, max_size=5),
                min_size=1, max_size=10),
       st.integers(min_value=0, max_value=6),
       st.integers(min_value=1, max_value=4))
def test_emit_pairs_blocking_invariant(lines, t, block_rows):
    # Property: for any block size the row-blocked emission equals the
    # single-call emission, with no double-counting.
    cleaned = []
    seen = set()
    for ln in lines:
        c = _cleanup_default(ln)
        if c and c not in seen:
            seen.add(c)
            cleaned.append(c)
    if not cleaned:
        return

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 10 ** 9)
        single = _emit(cleaned, t)
        monkeypatch.setattr(dn, "BLOCK_THRESHOLD", 0)
        monkeypatch.setattr(dn, "BLOCK_ROWS", block_rows)
        blocked = _emit(cleaned, t)
    assert single == blocked
    assert len(blocked) == len(set(blocked))


def test_valid_workers_accepts_all_cores_and_positive(monkeypatch):
    monkeypatch.setattr(dn.os, "cpu_count", lambda: 8)
    assert dn.valid_workers("-1") == -1
    assert dn.valid_workers("4") == 4


def test_valid_workers_clamps_above_available_cores(monkeypatch, capsys):
    monkeypatch.setattr(dn.os, "cpu_count", lambda: 4)

    assert dn.valid_workers("99") == 4
    assert "clamping to 4" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["-5", "0", "-2", "x", "1.5"])
def test_valid_workers_rejects_out_of_range(bad):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        dn.valid_workers(bad)


def test_distinct_undecodable_bytes_not_collapsed(tmp_path):
    """Two lines with DIFFERENT undecodable bytes must stay distinct, not
    collapse to one U+FFFD key reported as a distance-0 self-collision
    (dnv3-di-01). Run as a subprocess so stdout surrogateescape round-trips."""
    import subprocess
    import sys as _sys
    p = tmp_path / "names.txt"
    p.write_bytes(b"cafe\xe9\ncafe\xe8\n")  # two distinct invalid trailing bytes
    script = str(Path(__file__).resolve().parent.parent / "deduplicate-by-namev3.py")
    out = subprocess.run([_sys.executable, script, str(p)],
                         capture_output=True).stdout
    # Under errors="replace" both lines collapse and emit a ";0" self-collision.
    # Under surrogateescape they stay distinct -> no distance-0 line.
    assert b";0\n" not in out


def test_self_collision_reports_source_line_numbers(monkeypatch, tmp_path, capsys):
    """A cleaned form produced by multiple input lines emits a `# source
    lines:` comment naming those 1-based line numbers (dnv3-rel-02)."""
    f = tmp_path / "names.txt"
    f.write_text("alpha\nbeta\nalpha\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["deduplicate-by-namev3.py", str(f)])
    dn.main()
    out = capsys.readouterr().out
    assert "# source lines: 1,3" in out
    assert "alpha;alpha;0" in out


def test_valid_threshold_accepts_zero_and_positive():
    assert dn.valid_threshold("0") == 0
    assert dn.valid_threshold("7") == 7


@pytest.mark.parametrize("bad", ["-1", "-5", "x", "1.5"])
def test_valid_threshold_rejects_negative_and_garbage(bad):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        dn.valid_threshold(bad)


def test_block_knobs_parse_from_cli():
    args = dn._build_parser().parse_args([
        "names.txt",
        "--block-threshold",
        "12",
        "--block-rows",
        "3",
    ])

    assert args.block_threshold == 12
    assert args.block_rows == 3


def test_help_documents_output_record_and_comment_contract():
    help_text = " ".join(dn._build_parser().format_help().split())

    assert "VALUE;VALUE;DISTANCE" in help_text
    assert "ignore lines beginning with '#'" in help_text


@pytest.mark.parametrize("bad", ["-1", "x", "1.5"])
def test_valid_block_threshold_rejects_negative_and_garbage(bad):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        dn.valid_block_threshold(bad)


@pytest.mark.parametrize("bad", ["0", "-1", "x", "1.5"])
def test_valid_block_rows_rejects_nonpositive_and_garbage(bad):
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        dn.valid_block_rows(bad)


def test_emit_pairs_threshold_zero_short_circuits(monkeypatch):
    """dnv3-cli-02: threshold 0 emits no cross pairs and does not walk the
    matrix (cdist must not be called)."""
    called = []
    monkeypatch.setattr(dn, "cdist", lambda *a, **k: called.append(1))
    out = []
    dn.emit_pairs(["abc", "abd", "xyz"], 0, 1, out.append)
    assert out == []
    assert called == []   # no matrix computed


def test_cleanup_strips_semicolon_delimiter():
    """dnv3-cli-03: ';' is removed so a cleaned value can't break the ;-delimited
    output format."""
    assert ";" not in _cleanup_default("foo;bar")
    assert _cleanup_default("a;b") == "ab"


def test_strip_chars_override_still_strips_output_delimiter():
    replacements = dn.effective_replacements("#")

    assert replacements == ("#", ";")
    assert dn.cleanup("a;b#c", replacements, None) == "abc"


def test_emitters_use_configured_output_delimiter(monkeypatch):
    monkeypatch.setattr(dn, "OUTPUT_DELIMITER", "|")
    output = []

    dn._emit_self_collisions({"same": [1, 2]}, output.append)
    dn.emit_pairs(["cat", "car"], 1, 1, output.append)

    text = "".join(output)
    assert "same|same|0" in text
    assert "cat|car|1" in text


def test_emit_results_handles_broken_pipe():
    args = dn._build_parser().parse_args(["names.txt", "-w", "1"])

    def closed_pipe(_text):
        raise BrokenPipeError

    assert not dn._emit_results(
        {"same": [1, 2]},
        ["same"],
        1,
        args,
        closed_pipe,
        lambda: None,
    )


def test_main_cleanup_flags_override_defaults(monkeypatch, tmp_path, capsys):
    f = tmp_path / "names.txt"
    f.write_text("A# skip\nA\n", encoding="utf-8")
    monkeypatch.setattr(
        dn.sys,
        "argv",
        ["prog", str(f), "--strip-chars", "#", "--word-tokens", "skip", "-w", "1"],
    )

    dn.main()

    out = capsys.readouterr().out
    assert "a;a;0" in out


def test_configure_stdout_wraps_buffer_after_reconfigure_error(monkeypatch):
    class RefusingStream(io.TextIOWrapper):
        def reconfigure(self, **_kwargs):
            raise ValueError("already detached")

    stream = RefusingStream(io.BytesIO())
    monkeypatch.setattr(dn.sys, "stdout", stream)

    dn.configure_stdout()

    assert dn.sys.stdout.encoding == "utf-8"
    assert dn.sys.stdout.errors == "surrogateescape"
    dn.sys.stdout.detach()


def test_configure_stdout_rejects_stream_without_binary_buffer(monkeypatch):
    monkeypatch.setattr(dn.sys, "stdout", io.StringIO())

    with pytest.raises(RuntimeError, match="binary buffer"):
        dn.configure_stdout()


@pytest.mark.parametrize('prefix', [b'\xef\xbb\xbf', b''])
def test_dnv3_rel_70_bom_does_not_change_collision_identity(tmp_path, prefix):
    source = tmp_path / 'names.txt'
    source.write_bytes(prefix + b'abc\nabc\n\xffname\n')
    lines, dropped = dn._load_cleaned_lines(source, dn.REPLACEMENTS, None)
    assert lines == {'abc': [1, 2], '\udcffname': [3]}
    assert dropped == 0
    output = []
    dn._emit_self_collisions(lines, output.append)
    assert ''.join(output) == '# source lines: 1,2\nabc;abc;0\n'


@pytest.mark.parametrize("value", ["#abc", "# source lines: 1,2", '#say "hello"'])
def test_dnv3_api_60_comment_like_values_remain_data(value):
    output = []
    dn._emit_self_collisions({value: [1, 2]}, output.append)
    dn.emit_pairs([value, value + "d"], 1, 1, output.append)
    assert _parse_rows("".join(output)) == [(value, value, 0), (value, value + "d", 1)]
    assert sum(line.startswith("#") for line in "".join(output).splitlines()) == 1


@given(st.text(alphabet=st.characters(blacklist_categories=('Cs',), blacklist_characters='\r\n'), min_size=1, max_size=100))
def test_dnv3_api_60_record_fields_round_trip(value):
    output = []
    dn._write_pair(value, value + 'x', 1, output.append)
    assert not output[0].startswith('#')
    assert next(csv.reader(output, delimiter=';')) == [value, value + 'x', '1']


def test_dnv3_rob_60_early_pipe_close_exits_without_flush_traceback(tmp_path):
    import os
    import subprocess
    import sys
    source = tmp_path / 'dense.txt'
    source.write_text(''.join(f'{i:04d}\n' for i in range(200)))
    proc = subprocess.Popen([sys.executable, str(_PATH), str(source), '-w', '1'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert os.read(proc.stdout.fileno(), 1)
        proc.stdout.close()
        assert proc.wait(timeout=10) == 0
        assert proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stderr.close()


def test_dnv3_rob_60_final_buffer_flush_is_guarded(monkeypatch, tmp_path):
    source = tmp_path / 'names.txt'
    source.write_text('abc\nabc\n')
    class ClosedReader(io.StringIO):
        def flush(self):
            raise BrokenPipeError('reader closed after buffered write')
    stream = ClosedReader()
    monkeypatch.setattr(dn, 'configure_stdout', lambda: None)
    monkeypatch.setattr(dn.sys, 'stdout', stream)
    monkeypatch.setattr(dn.sys, 'argv', ['prog', str(source), '-w', '1'])
    dn.main()
    assert dn.sys.stdout is not stream
    dn.sys.stdout.flush()
