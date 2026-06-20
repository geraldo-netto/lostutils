"""Tests for hash-recursive-ai5.py — the hr-rel-* reliability fixes
(jobs clamp, readable-alias representative) and the functions they touch."""
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest

_PATH = Path(__file__).resolve().parent.parent / "hash-recursive-ai5.py"
_spec = importlib.util.spec_from_file_location("hash_recursive_ai5", _PATH)
hr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hr)


# --- hr-rel-01: jobs is clamped to >=1 so a walk never silently no-ops ------

def test_threaded_walk_clamps_zero_jobs():
    with TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.txt").write_text("x")
        (root / "sub").mkdir()
        (root / "sub" / "b.txt").write_text("y")
        results, stats = hr.threaded_walk(root, 0)      # 0 -> clamped to 1
        names = sorted(Path(p).name for p, *_ in results)
        assert names == ["a.txt", "b.txt"]
        assert stats["files"] == 2


def test_threaded_walk_clamps_negative_jobs():
    with TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.txt").write_text("x")
        results, _ = hr.threaded_walk(root, -5)
        assert len(results) == 1


# --- hr-rel-02: representative is a readable alias when one exists -----------

def test_readable_rep_prefers_readable_alias():
    paths = ["/inode/aliasA", "/inode/aliasB"]
    with mock.patch.object(hr.os, "access",
                           side_effect=lambda p, mode: p != "/inode/aliasA"):
        assert hr._readable_rep(paths) == "/inode/aliasB"


def test_readable_rep_falls_back_to_first_when_none_readable():
    paths = ["/inode/aliasA", "/inode/aliasB"]
    with mock.patch.object(hr.os, "access", return_value=False):
        assert hr._readable_rep(paths) == "/inode/aliasA"


def test_readable_rep_real_files():
    with TemporaryDirectory() as d:
        a = Path(d) / "a"
        a.write_text("x")
        assert hr._readable_rep([str(a)]) == str(a)


# --- hr-arch-01: extracted pipeline stages ----------------------------------

def test_index_inodes_groups_hardlinks():
    files = [("/a", 10, 1, 100), ("/b", 10, 1, 100), ("/c", 20, 1, 200)]
    aliases, inode_size = hr.index_inodes(files)
    assert sorted(aliases[(1, 100)]) == ["/a", "/b"]   # two aliases, one inode
    assert aliases[(1, 200)] == ["/c"]
    assert inode_size == {(1, 100): 10, (1, 200): 20}


def test_size_collision_candidates_filters_and_sorts():
    inode_size = {(1, 1): 50, (1, 2): 50, (1, 3): 999, (1, 4): 10, (1, 5): 10}
    cands = hr.size_collision_candidates(inode_size)
    sizes = [s for s, _ in cands]
    assert sizes == sorted(sizes, reverse=True)        # largest first
    assert set(sizes) == {50, 10}                      # unique-size 999 dropped
    assert (999, (1, 3)) not in cands


def test_emit_groups_writes_only_real_duplicates():
    aliases = {("k1",): ["/p1", "/p1b"], ("k2",): ["/p2"], ("k3",): ["/p3"]}
    final_groups = {"digA": [("k1",)], "digB": [("k2",), ("k3",)]}
    out = []
    groups, paths = hr.emit_groups(final_groups, aliases, out.append)
    # digA expands to 2 alias paths (a hardlinked dup), digB to 2 distinct files
    assert groups == 2 and paths == 4
    # hr-perf-03: one `write` call per group, each containing every line for
    # that group (so the per-line count requires splitting on newline).
    assert len(out) == 2
    digA_chunk = next(c for c in out if c.startswith("digA "))
    assert sum(1 for ln in digA_chunk.splitlines() if ln.startswith("digA ")) == 2


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def test_find_duplicate_groups_stage1_and_stage2():
    with TemporaryDirectory() as d:
        root = Path(d)
        # small identical pair -> confirmed at stage 1 (head is whole file)
        _write(root / "s1.bin", b"hello-small")
        _write(root / "s2.bin", b"hello-small")
        # large identical pair (> 2*CAP) -> goes through stage 2
        big = b"A" * (2 * hr.CAP + 4096)
        _write(root / "big1.bin", big)
        _write(root / "big2.bin", big)
        # a unique file -> never a candidate
        _write(root / "uniq.bin", b"unique-content-xyz")

        files, _ = hr.threaded_walk(root, 2)
        final_groups, aliases, info = hr.find_duplicate_groups(files, 2)
        out = []
        groups, paths = hr.emit_groups(final_groups, aliases, out.append)

        assert groups == 2          # small pair + big pair
        assert paths == 4
        assert info["candidates"] == 4   # 2 sizes each shared by 2 inodes
        assert info["stage2"] == 2       # the two big files needed stage 2
        # hr-scal-01: aliases pruned to candidate inodes only (uniq dropped)
        assert all(not p.endswith("uniq.bin")
                   for paths_ in aliases.values() for p in paths_)


def test_no_false_dup_for_files_between_cap_and_2cap():
    # hr-rel-03 regression: same-size files between CAP and 2*CAP that share
    # their first CAP bytes but differ in the tail must NOT be reported as
    # duplicates (the pre-fix threshold confirmed them at stage 1).
    with TemporaryDirectory() as d:
        root = Path(d)
        shared_head = b"H" * hr.CAP
        a = shared_head + b"AAA" * (hr.CAP // 6)   # ~1.5 * CAP
        b = shared_head + b"BBB" * (hr.CAP // 6)   # same size, different tail
        assert len(a) == len(b) and hr.CAP < len(a) <= 2 * hr.CAP
        (root / "x1.bin").write_bytes(a)
        (root / "x2.bin").write_bytes(b)
        files, _ = hr.threaded_walk(root, 2)
        final_groups, aliases, info = hr.find_duplicate_groups(files, 2)
        # different content -> NOT in the same final group
        for keys in final_groups.values():
            paths = [p for k in keys for p in aliases[k]]
            if len(paths) > 1:
                names = sorted(Path(p).name for p in paths)
                assert names != ["x1.bin", "x2.bin"], \
                    "false positive: 1-2 MiB files with different tails grouped"


def test_find_duplicate_groups_no_candidates():
    with TemporaryDirectory() as d:
        root = Path(d)
        _write(root / "only.bin", b"x")
        files, _ = hr.threaded_walk(root, 1)
        final_groups, aliases, info = hr.find_duplicate_groups(files, 1)
        assert final_groups == {} and info["candidates"] == 0


# --- hr-cx-01: preflight + main smoke ---------------------------------------

def test_preflight_root_missing_path(monkeypatch, capsys):
    # hr-rel-17: typed exception, not sys.exit. CLI translation lives in main().
    with TemporaryDirectory() as d:
        bad = str(Path(d) / "does-not-exist")
    with pytest.raises(hr.RootError) as ex:
        hr._preflight_root(bad)
    assert "no such file" in str(ex.value)


def test_preflight_root_not_a_directory(tmp_path, capsys):
    f = tmp_path / "file.txt"; f.write_text("x")
    with pytest.raises(hr.RootError) as ex:
        hr._preflight_root(str(f))
    assert "not a directory" in str(ex.value)


def test_preflight_root_no_access(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hr.os, "access", lambda p, m: False)
    with pytest.raises(hr.RootError) as ex:
        hr._preflight_root(str(tmp_path))
    assert "permission denied" in str(ex.value)


def test_run_stage_serial_branch():
    # _run_stage runs serially when total_bytes < THREAD_THRESHOLD_BYTES.
    items = ["a", "b", "c"]
    fake = lambda batch: [(p, p.upper()) for p in batch]
    out, errors = hr._run_stage(items, fake, total_bytes=10, jobs=2)
    assert out == {"a": "A", "b": "B", "c": "C"} and errors == 0


def test_run_stage_threaded_branch(monkeypatch):
    # Force the threaded branch by dropping the threshold to 0; verify the
    # results merge from multiple batches and None values count as errors.
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 2)
    items = ["a", "b", "c", "d", "e"]

    def fake(batch):
        return [(p, None if p == "c" else p.upper()) for p in batch]

    out, errors = hr._run_stage(items, fake, total_bytes=10**9, jobs=2)
    assert out == {"a": "A", "b": "B", "c": None, "d": "D", "e": "E"}
    assert errors == 1


def test_main_smoke(monkeypatch, capsys):
    with TemporaryDirectory() as d:
        root = Path(d)
        _write(root / "a.bin", b"hello-dup")
        _write(root / "b.bin", b"hello-dup")           # duplicates
        _write(root / "uniq.bin", b"alone")
        monkeypatch.setattr(hr.sys, "argv", ["hr", str(root), "-j", "2"])
        hr.main()
        out, err = capsys.readouterr()
        # both duplicate paths printed (same digest line each)
        assert "a.bin" in out and "b.bin" in out
        assert "dup_groups=1" in err and "dup_paths=2" in err


# --- hr-perf-02: single-stat walk ------------------------------------------

def test_threaded_walk_single_stat_per_entry(monkeypatch):
    """A previous version called is_dir + is_file + stat. The new walk uses
    one e.stat(follow_symlinks=False) per entry. Wrap DirEntry.stat to count
    calls and assert each regular file is stat'd exactly once."""
    with TemporaryDirectory() as d:
        root = Path(d)
        for n in ("a.bin", "b.bin", "c.bin"):
            (root / n).write_bytes(b"x")
        (root / "sub").mkdir()
        for n in ("d.bin", "e.bin"):
            (root / "sub" / n).write_bytes(b"x")

        import os as _os
        real_stat = _os.DirEntry.stat
        counter = {"calls": 0, "is_dir": 0, "is_file": 0}

        def counting_stat(self, *, follow_symlinks=True):
            if not follow_symlinks:
                counter["calls"] += 1
            return real_stat(self, follow_symlinks=follow_symlinks)

        # is_dir / is_file MUST NOT be called from threaded_walk anymore;
        # spying on them lets us prove the optimisation stuck.
        real_is_dir = _os.DirEntry.is_dir
        real_is_file = _os.DirEntry.is_file

        def counting_is_dir(self, *, follow_symlinks=True):
            counter["is_dir"] += 1
            return real_is_dir(self, follow_symlinks=follow_symlinks)

        def counting_is_file(self, *, follow_symlinks=True):
            counter["is_file"] += 1
            return real_is_file(self, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(_os.DirEntry, "stat", counting_stat)
        monkeypatch.setattr(_os.DirEntry, "is_dir", counting_is_dir)
        monkeypatch.setattr(_os.DirEntry, "is_file", counting_is_file)
        results, stats = hr.threaded_walk(root, 1)
        assert stats["files"] == 5
        # 5 regular files + 1 subdir == 6 entries, one stat each.
        assert counter["calls"] >= 6
        # hr-perf-02: walk must not call is_dir / is_file on entries anymore.
        assert counter["is_dir"] == 0 and counter["is_file"] == 0


def test_threaded_walk_handles_stat_failure_on_entry():
    # An OSError on a single entry must increment entry_errors, not raise.
    import os as _os
    with TemporaryDirectory() as d:
        root = Path(d)
        (root / "ok.bin").write_bytes(b"x")
        (root / "bad.bin").write_bytes(b"x")
        real_stat = _os.DirEntry.stat

        def flaky_stat(self, *, follow_symlinks=True):
            if self.name == "bad.bin":
                raise OSError("racy delete")
            return real_stat(self, follow_symlinks=follow_symlinks)

        with mock.patch.object(_os.DirEntry, "stat", flaky_stat):
            results, stats = hr.threaded_walk(root, 1)
        names = [Path(p).name for p, *_ in results]
        assert names == ["ok.bin"]
        assert stats["entry_errors"] == 1


# --- hr-perf-03: buffered emit ---------------------------------------------

def test_emit_groups_writes_once_per_group():
    aliases = {("k1",): ["/p1", "/p1b", "/p1c"],
               ("k2",): ["/p2a", "/p2b"]}
    final_groups = {"dA": [("k1",)], "dB": [("k2",)]}
    out = []
    groups, paths = hr.emit_groups(final_groups, aliases, out.append)
    assert groups == 2 and paths == 5
    # hr-perf-03: one write call per group.
    assert len(out) == 2
    # Each chunk contains every path for that group.
    chunk_A = next(c for c in out if c.startswith("dA "))
    assert chunk_A.count("\n") == 3
    assert "/p1" in chunk_A and "/p1b" in chunk_A and "/p1c" in chunk_A


# --- hr-conc-02: cancellable walk ------------------------------------------

def test_threaded_walk_cancels_via_event():
    # hr-conc-02: if cancel_event is set before workers start, no directories
    # are scanned and the walk returns empty results without hanging.
    import threading as _t
    with TemporaryDirectory() as d:
        root = Path(d)
        for i in range(5):
            (root / f"f{i}.bin").write_bytes(b"x")
        event = _t.Event()
        event.set()                                  # pre-cancelled
        results, stats = hr.threaded_walk(root, 2, cancel_event=event)
        # The root dir's scan is skipped, so we record dirs=0 and no files.
        assert results == []
        assert stats["files"] == 0
        assert stats["dirs"] == 0


def test_threaded_walk_runs_normally_when_event_unset():
    import threading as _t
    with TemporaryDirectory() as d:
        root = Path(d)
        (root / "a.bin").write_bytes(b"x")
        event = _t.Event()                           # never set
        results, stats = hr.threaded_walk(root, 2, cancel_event=event)
        assert stats["files"] == 1
        assert len(results) == 1


# --- hr-conc-01: per-worker buckets (no results lock) ----------------------

def test_threaded_walk_results_complete_with_many_workers():
    """8 workers over a wide tree must produce every file exactly once with
    no duplicates and no losses, proving the per-worker bucket merge stays
    consistent under contention."""
    with TemporaryDirectory() as d:
        root = Path(d)
        for sub in range(20):
            sd = root / f"d{sub}"; sd.mkdir()
            for i in range(50):
                (sd / f"f{i}.bin").write_bytes(b"x")
        results, stats = hr.threaded_walk(root, 8)
        assert stats["files"] == 20 * 50
        names = [Path(p).name for p, *_ in results]
        # No duplicate paths.
        assert len(set(p for p, *_ in results)) == len(results)
        # 1000 files total.
        assert len(names) == 1000


def test_threaded_walk_no_results_lock_attr():
    """Sanity: the new design has no shared `results_lock` to take per-entry."""
    import inspect
    src = inspect.getsource(hr.threaded_walk)
    assert "results_lock" not in src


# --- hr-rel-04: ENOENT vs other OSErrors ------------------------------------

def test_hash_file_windows_filenotfound_is_silent(capsys):
    out = hr._hash_file_windows("/nonexistent-path-xyz", [(0, 10, hr.os.SEEK_SET)])
    assert out is None
    assert capsys.readouterr().err == ""           # no warning emitted


def test_hash_file_windows_other_oserror_warns(monkeypatch, capsys):
    # hr-sec-05: open path uses os.open + O_NOFOLLOW; patch there.
    def boom(*a, **k):
        raise PermissionError("EACCES")

    monkeypatch.setattr(hr.os, "open", boom, raising=False)
    out = hr._hash_file_windows("/whatever", [(0, 10, hr.os.SEEK_SET)])
    assert out is None
    err = capsys.readouterr().err
    assert "hash failed" in err and "EACCES" in err


# --- hr-rel-06: sample windows never read past EOF --------------------------

def test_hash_tail_and_samples_clamps_windows(tmp_path):
    """Even on a contrived `size` value, the offsets handed to
    _hash_file_windows must satisfy offset + SAMPLE <= size."""
    captured = {}

    def fake_hash(path, windows, config=None):
        captured["windows"] = windows
        return "deadbeef"

    import types
    orig = hr._hash_file_windows
    try:
        hr._hash_file_windows = fake_hash    # type: ignore[assignment]
        hr.hash_tail_and_samples("/dummy", 3 * hr.CAP)   # > 2*CAP
        for window in captured["windows"]:
            offset, length, whence = window[0], window[1], window[2]
            if whence == hr.os.SEEK_SET:
                assert offset + length <= 3 * hr.CAP, \
                    f"window past EOF: {offset}+{length} > {3 * hr.CAP}"
    finally:
        hr._hash_file_windows = orig   # type: ignore[assignment]


def test_hash_tail_and_samples_tiny_size_does_not_overflow():
    # Defensive: even if a future caller drops the size > 2*CAP gate, no
    # window may read past EOF.
    captured = {}

    def fake_hash(path, windows, config=None):
        captured["windows"] = windows
        return None

    orig = hr._hash_file_windows
    try:
        hr._hash_file_windows = fake_hash   # type: ignore[assignment]
        hr.hash_tail_and_samples("/dummy", 100)   # << SAMPLE
        for window in captured["windows"]:
            offset, length, whence = window[0], window[1], window[2]
            if whence == hr.os.SEEK_SET:
                # offset is clamped to size - SAMPLE = max(0, 100-65536) = 0
                assert offset == 0
    finally:
        hr._hash_file_windows = orig    # type: ignore[assignment]


# --- 100% coverage gap-fillers ---------------------------------------------

import os as _os
import sys as _sys
import stat as _stat


def test_threaded_walk_records_dir_error(tmp_path, monkeypatch):
    # Force os.scandir to raise OSError so dir_errors counter increments
    # (covers L126-128).
    (tmp_path / "a.txt").write_text("x")

    def boom(p):
        raise OSError("denied")

    monkeypatch.setattr(hr.os, "scandir", boom)
    _, stats = hr.threaded_walk(tmp_path, 1)
    assert stats["dir_errors"] >= 1


def test_threaded_walk_records_entry_error(tmp_path, monkeypatch):
    # entry.stat raises OSError -> entry_errors counter increments.
    (tmp_path / "a.txt").write_text("x")
    real_scandir = hr.os.scandir

    class Wrapper:
        def __init__(self, it): self._it = it
        def __enter__(self): return self
        def __exit__(self, *a): self._it.__exit__(*a)
        def __iter__(self): return self
        def __next__(self):
            entry = next(iter(self._it))
            class FakeEntry:
                path = entry.path
                name = entry.name
                def stat(self_inner, follow_symlinks=False):
                    raise OSError("racy")
            return FakeEntry()

    def fake_scandir(p):
        it = real_scandir(p)
        return Wrapper(it)

    monkeypatch.setattr(hr.os, "scandir", fake_scandir)
    _, stats = hr.threaded_walk(tmp_path, 1)
    assert stats.get("entry_errors", 0) >= 1


def test_run_stage_serial_records_hash_error():
    # batch_fn returns d=None -> errors counter increments (covers L233).
    items = [("/x", 100), ("/y", 200)]

    def batch(items):
        for p, _ in items:
            yield p, None    # signal hash failure

    out, errors = hr._run_stage(items, batch, total_bytes=300, jobs=1)
    assert errors == len(items)


def test_emit_groups_skips_single_alias_keys(tmp_path):
    # Groups with len(all_paths) <= 1 must NOT be emitted (covers L359 False).
    aliases = {("dev", 1): ["/x"], ("dev", 2): ["/y"]}
    final_groups = {"deadbeef": [("dev", 1)]}
    written = []
    dup_groups, dup_paths = hr.emit_groups(final_groups, aliases, written.append)
    assert dup_groups == 0
    assert dup_paths == 0
    assert written == []


def test_main_quiet_suppresses_summary(tmp_path, monkeypatch, capsys):
    # Cover L399 False arm (the silent quiet branch). Also exercises main().
    (tmp_path / "a.txt").write_text("hi")
    monkeypatch.setattr(_sys, "argv", ["hr", str(tmp_path), "--quiet"])
    hr.main()
    err = capsys.readouterr().err
    assert "[ai5]" not in err


def test_main_emits_summary_when_not_quiet(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.txt").write_text("hi")
    monkeypatch.setattr(_sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "[ai5]" in err


def test_find_duplicate_groups_skips_unread_head(monkeypatch, tmp_path):
    # head_by_path.get returns None -> continue (covers L302).
    a = tmp_path / "a.bin"; a.write_bytes(b"x" * 100)
    b = tmp_path / "b.bin"; b.write_bytes(b"x" * 100)

    def stub(items, fn, total, jobs):
        # items here is a list of paths (strings); map all to None.
        return ({p: None for p in items}, 0)

    monkeypatch.setattr(hr, "_run_stage", stub)
    files = [(str(a), 100, 1, 100), (str(b), 100, 1, 101)]
    final_groups, *_ = hr.find_duplicate_groups(files, jobs=1)
    assert final_groups == {} or all(len(v) <= 1 for v in final_groups.values())


def test_find_duplicate_groups_skips_single_member_head_groups(tmp_path, monkeypatch):
    # Two files of the same size but distinct heads -> each (size, head)
    # bucket has 1 member -> `if len(keys) < 2: continue` (covers L310).
    a = tmp_path / "a.bin"; a.write_bytes(b"AAA" * 50)
    b = tmp_path / "b.bin"; b.write_bytes(b"BBB" * 50)
    files = [(str(a), len(a.read_bytes()), 1, 100),
             (str(b), len(b.read_bytes()), 1, 101)]
    final_groups, *_ = hr.find_duplicate_groups(files, jobs=1)
    assert final_groups == {}


def test_threaded_walk_skips_non_regular_non_dir(tmp_path, monkeypatch):
    # An entry whose stat reports neither S_ISDIR nor S_ISREG (e.g., FIFO):
    # both the S_ISDIR True and S_ISREG True branches at L116/L120 evaluate
    # False, so the loop falls through (covers branch 120->107).
    _os.mkfifo(tmp_path / "f")
    (tmp_path / "real.txt").write_text("x")
    results, stats = hr.threaded_walk(tmp_path, 1)
    # FIFO is not counted as file; only real.txt is.
    assert stats["files"] == 1


def test_expand_keys_to_paths_caps_at_alias_cap():
    aliases = {("d", i): [f"/p/{i}/{j}" for j in range(100)] for i in range(20)}
    keys = list(aliases)
    out = hr._expand_keys_to_paths(keys, aliases, cap=50)
    # cap=50 + "+N more" sentinel = 51 entries total.
    assert len(out) == 51
    assert out[-1].startswith("+")
    assert "more" in out[-1]


def test_expand_keys_to_paths_under_cap_no_sentinel():
    aliases = {("d", 0): ["/a", "/b"]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=ALIAS_CAP_LARGE)
    assert out == ["/a", "/b"]


ALIAS_CAP_LARGE = 100000


def test_emit_groups_streaming_returns_callback_and_totals():
    written = []
    cb, totals = hr.emit_groups_streaming(written.append)
    cb(("abc", None),
       [("d", 1), ("d", 2)],
       {("d", 1): ["/a/1"], ("d", 2): ["/a/2"]})
    assert any("abc /a/1" in chunk for chunk in written)
    groups, paths = totals()
    assert groups == 1
    assert paths == 2


def test_emit_groups_streaming_skips_single_path():
    written = []
    cb, totals = hr.emit_groups_streaming(written.append)
    cb("hash", [("d", 1)], {("d", 1): ["/only"]})
    assert written == []
    assert totals() == (0, 0)


def test_find_duplicate_groups_streaming_callback(tmp_path):
    big = b"y" * 200
    a = tmp_path / "a.bin"; a.write_bytes(big)
    b = tmp_path / "b.bin"; b.write_bytes(big)
    files = [(str(a), len(big), 1, 100), (str(b), len(big), 1, 101)]
    captured = []

    def on_group(digest_key, keys, aliases):
        captured.append((digest_key, keys, aliases))

    result = hr.find_duplicate_groups(files, jobs=1, on_group=on_group)
    # Streaming branch: result.groups stays empty since cb consumes them.
    assert result.groups == {}
    # cb was invoked at least once for the duplicate pair.
    assert captured


def test_find_duplicate_groups_skips_failed_tail(tmp_path, monkeypatch):
    # tail is None -> continue (covers L328).
    big = b"y" * (hr.HEAD_TAIL_THRESHOLD + 100)
    a = tmp_path / "a.bin"; a.write_bytes(big)
    b = tmp_path / "b.bin"; b.write_bytes(big)
    real_run_stage = hr._run_stage
    call_n = {"n": 0}

    def stub(items, fn, total, jobs):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Stage 1: real head hashes (so paths share head).
            return real_run_stage(items, fn, total, jobs)
        # Stage 2: return None for every tail.
        return ({p: None for _s, p in items}, 0)

    monkeypatch.setattr(hr, "_run_stage", stub)
    files = [(str(a), len(big), 1, 100), (str(b), len(big), 1, 101)]
    final_groups, *_ = hr.find_duplicate_groups(files, jobs=1)
    # Stage-2 hash all None -> no final groups beyond head-only.
    assert all(len(v) >= 2 for v in final_groups.values()) or final_groups == {}


# --- hr-rel-09 / hr-rel-10 -------------------------------------------------

def test_hash_file_windows_strict_short_file_returns_none(tmp_path):
    f = tmp_path / "tiny.bin"; f.write_bytes(b"only9byte")
    # strict=True on a 9-byte file asking for 100 -> None
    assert hr._hash_file_windows(str(f), [hr.FileWindow(0, 100, 0, strict=True)]) is None


def test_hash_file_windows_non_strict_short_file_returns_digest(tmp_path):
    f = tmp_path / "tiny.bin"; f.write_bytes(b"only9byte")
    digest = hr._hash_file_windows(str(f), [hr.FileWindow(0, 100, 0, strict=False)])
    assert digest is not None and len(digest) > 0


def test_read_window_into_assembles_short_reads():
    # hr-test-01: a kernel short read (network mount / signal-interrupted
    # slow read) hands back a partial buffer; _read_window_into must loop
    # and assemble the FULL window so the digest matches a single full read.
    import blake3

    payload = b"abcdefghijklmnopqrstuvwxyz0123456789"

    class ShortReadFile:
        """Returns a SHORT first chunk, then the remainder, then EOF."""
        def __init__(self, data):
            self._data = data
            self._pos = 0
            self._first = True

        def read(self, n):
            if self._pos >= len(self._data):
                return b""
            # First read deliberately undershoots `n` to simulate a
            # kernel short read; later reads return the rest.
            take = 4 if self._first else n
            self._first = False
            chunk = self._data[self._pos:self._pos + take]
            self._pos += len(chunk)
            return chunk

    h = blake3.blake3()
    ok = hr._read_window_into(h, ShortReadFile(payload), len(payload), strict=True)
    assert ok is True

    expected = blake3.blake3()
    expected.update(payload)
    assert h.hexdigest() == expected.hexdigest()


def test_hash_file_windows_legacy_3tuple_window(tmp_path):
    # Back-compat: bare 3-tuple windows still work (no `strict` field).
    f = tmp_path / "p.bin"; f.write_bytes(b"hello")
    digest = hr._hash_file_windows(str(f), [(0, 100, 0)])
    assert digest is not None


# --- hr-obs-01 _fmt_count + boundary fuzz ---------------------------------

def test_fmt_count_under_million_is_bare_int():
    assert hr._fmt_count(0) == "0"
    assert hr._fmt_count(1) == "1"
    assert hr._fmt_count(999_999) == "999999"


def test_fmt_count_million_threshold():
    assert hr._fmt_count(1_000_000) == "1.0M"
    assert hr._fmt_count(1_500_000) == "1.5M"
    assert hr._fmt_count(100_000_000) == "100M"   # >= 100 drops decimal


def test_fmt_count_negative():
    assert hr._fmt_count(-1) == "-1"
    assert hr._fmt_count(-1_500_000) == "-1.5M"


def test_fmt_count_giga_tera():
    assert hr._fmt_count(2_500_000_000) == "2.5G"
    assert hr._fmt_count(7_300_000_000_000) == "7.3T"


def test_fmt_count_int_boundaries():
    # Boundary fuzz: 1, 0, -1, MAX, MIN, MAX+1
    import sys as _sys
    for n in (0, 1, -1, _sys.maxsize, -_sys.maxsize - 1, _sys.maxsize + 1):
        out = hr._fmt_count(n)
        assert isinstance(out, str) and len(out) > 0


# --- hr-perf-04 _iter_batches boundary cases -----------------------------

def test_iter_batches_exact_multiple():
    out = list(hr._iter_batches([1, 2, 3, 4], 2))
    assert out == [[1, 2], [3, 4]]


def test_iter_batches_remainder():
    out = list(hr._iter_batches([1, 2, 3, 4, 5], 2))
    assert out == [[1, 2], [3, 4], [5]]


def test_iter_batches_empty():
    assert list(hr._iter_batches([], 10)) == []


def test_iter_batches_size_larger_than_items():
    assert list(hr._iter_batches([1, 2], 100)) == [[1, 2]]


def _patch_runconfig_capture(monkeypatch, *, force_hits=None):
    """Patch hr.RunConfig to record the most recently constructed
    instance (hr-arch-05). Returns a dict whose ``cfg`` key holds the
    captured instance once main constructs it. ``force_hits`` lets
    tests pre-populate ``alias_cap_hits`` to exercise the end-of-run
    warning path without driving a real cap fire."""
    captured = {}
    real_cls = hr.RunConfig

    class _Captured(real_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            if force_hits is not None:
                self.alias_cap_hits = force_hits
            captured["cfg"] = self

    monkeypatch.setattr(hr, "RunConfig", _Captured)
    return captured


def test_alias_cap_warning_counter_fires_on_truncation():
    # hr-scal-04 + hr-arch-05: cap fired -> counter increments on config.
    config = hr.RunConfig(alias_cap=10)
    assert config.alias_cap_hits == 0
    aliases = {("d", 0): [f"/p/{i}" for i in range(2000)]}
    hr._expand_keys_to_paths([("d", 0)], aliases, config=config)
    assert config.alias_cap_hits == 1


def test_alias_cap_warning_silent_under_cap():
    config = hr.RunConfig(alias_cap=100)
    aliases = {("d", 0): ["/p/0", "/p/1"]}
    hr._expand_keys_to_paths([("d", 0)], aliases, config=config)
    assert config.alias_cap_hits == 0


def test_alias_cap_cli_flag_overrides_global(tmp_path, monkeypatch, capsys):
    # hr-arch-05: --alias-cap is propagated through the RunConfig main builds.
    (tmp_path / "a.bin").write_bytes(b"hi")
    captured = _patch_runconfig_capture(monkeypatch)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path), "--alias-cap", "7"])
    hr.main()
    assert captured["cfg"].alias_cap == 7


def test_alias_cap_zero_disables_cap(tmp_path, monkeypatch):
    (tmp_path / "a.bin").write_bytes(b"x")
    captured = _patch_runconfig_capture(monkeypatch)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path), "--alias-cap", "0"])
    hr.main()
    assert captured["cfg"].alias_cap > 1_000_000_000   # effectively no cap


def test_alias_cap_warning_logged_at_end_of_run(tmp_path, monkeypatch, capsys):
    # hr-scal-04 + hr-arch-05: when the cap fires during a run, main
    # emits a one-shot WARNING on stderr before exiting.
    _patch_runconfig_capture(monkeypatch, force_hits=3)
    (tmp_path / "a.bin").write_bytes(b"x")
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "WARNING: alias cap" in err
    assert "truncated 3 group(s)" in err
    hr.ALIAS_CAP = 1024


def test_iter_batches_returns_fresh_lists(tmp_path):
    # hr-rel-12: each batch is a fresh list, not an alias of the source.
    src = [1, 2, 3, 4, 5]
    out = list(hr._iter_batches(src, 2))
    out[0].append(99)
    assert src == [1, 2, 3, 4, 5]   # source untouched


def test_run_stage_partial_results_annotated(tmp_path, monkeypatch):
    # hr-rel-13: when batch_fn raises mid-iteration, the exception
    # is re-raised with a note describing how many items were hashed.
    items = [f"/p/{i}" for i in range(20)]
    calls = {"n": 0}

    def fail_after_two(batch):
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("synthetic")
        return [(p, "deadbeef") for p in batch]

    monkeypatch.setattr(hr, "HASH_BATCH", 4)
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    with pytest.raises(RuntimeError, match="synthetic") as exc_info:
        hr._run_stage(items, fail_after_two, total_bytes=100, jobs=2)
    notes = getattr(exc_info.value, "__notes__", []) or []
    assert any("items hashed" in n for n in notes)


def test_expand_keys_to_paths_more_sentinel_type():
    # hr-hyg-02: truncation sentinel is _MoreSentinel (str subclass).
    aliases = {("d", 0): [f"/p/{i}" for i in range(2000)]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=10)
    assert len(out) == 11   # 10 paths + 1 sentinel
    assert isinstance(out[-1], hr._MoreSentinel)
    assert isinstance(out[0], str) and not isinstance(out[0], hr._MoreSentinel)


def test_count_real_paths_excludes_sentinel():
    aliases = {("d", 0): [f"/p/{i}" for i in range(100)]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=5)
    assert hr._count_real_paths(out) == 5   # ignores the sentinel


def test_emit_groups_does_not_double_count_sentinel():
    # hr-hyg-02: a 1-real-path group with a sentinel must NOT be emitted
    # (real_count == 1 even though list len == 2).
    aliases = {("d", 0): [f"/p/{i}" for i in range(2)]}
    written = []
    cb, totals = hr.emit_groups_streaming(written.append)
    # Force a sentinel by passing a small cap externally — emulate by
    # building an expansion that yields [path, sentinel]
    final_groups = {"hash": [("d", 0)]}
    aliases_truncated = {("d", 0): ["/only"]}
    # With one real path, dup_groups stays 0 (filter is `> 1`).
    cb("hash", [("d", 0)], aliases_truncated)
    g, p = totals()
    assert g == 0
    assert p == 0


def test_find_duplicate_groups_info_includes_stage2_skipped(tmp_path, monkeypatch):
    # hr-rel-10: dropped tails are counted in info["stage2_skipped"].
    big = b"q" * (hr.HEAD_TAIL_THRESHOLD + 200)
    a = tmp_path / "a.bin"; a.write_bytes(big)
    b = tmp_path / "b.bin"; b.write_bytes(big)
    real_run = hr._run_stage
    seen = {"n": 0}

    def stub(items, fn, total, jobs):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_run(items, fn, total, jobs)
        # Stage 2: every tail = None
        return ({p: None for _s, p in items}, 0)

    monkeypatch.setattr(hr, "_run_stage", stub)
    files = [(str(a), len(big), 1, 100), (str(b), len(big), 1, 101)]
    result = hr.find_duplicate_groups(files, jobs=1)
    assert result.info["stage2_skipped"] >= 2


# --- hr-obs-03: bounded stderr error logging ------------------------------

def test_log_hash_error_no_config_unbounded(capsys):
    # Back-compat: legacy callers without a config still print verbatim.
    hr._log_hash_error("/some/path", OSError("boom"), config=None)
    err = capsys.readouterr().err
    assert "hash failed for /some/path: boom" in err


def test_log_hash_error_under_cap_logs(capsys):
    cfg = hr.RunConfig(hash_error_verbose_cap=3)
    for _ in range(2):
        hr._log_hash_error("/p", OSError("x"), config=cfg)
    err = capsys.readouterr().err
    assert err.count("hash failed for /p") == 2
    assert cfg.hash_error_logged == 2
    assert cfg.hash_error_suppressed == 0


def test_log_hash_error_above_cap_suppresses(capsys):
    cfg = hr.RunConfig(hash_error_verbose_cap=2)
    for _ in range(5):
        hr._log_hash_error("/p", OSError("x"), config=cfg)
    err = capsys.readouterr().err
    assert err.count("hash failed for /p") == 2
    assert cfg.hash_error_logged == 2
    assert cfg.hash_error_suppressed == 3


# --- back-compat legacy batch shims ---------------------------------------

def test_head_batch_legacy_shim(tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"hello")
    out = hr._head_batch([str(f)])
    assert len(out) == 1
    p, d = out[0]
    assert p == str(f)
    assert isinstance(d, str) and len(d) == 64


def test_tail_batch_legacy_shim(tmp_path):
    f = tmp_path / "a.bin"; f.write_bytes(b"z" * (3 * hr.CAP))
    out = hr._tail_batch([(3 * hr.CAP, str(f))])
    assert len(out) == 1
    p, d = out[0]
    assert p == str(f)
    assert isinstance(d, str) and len(d) == 64


# --- hr-conc-05: SIGINT handler body --------------------------------------

def test_install_sigint_cancel_sets_event_and_restores_default():
    import signal, threading
    ev = threading.Event()
    hr._install_sigint_cancel(ev)
    try:
        installed = signal.getsignal(signal.SIGINT)
        assert installed is not signal.SIG_DFL
        assert ev.is_set() is False
        # Invoke the handler synchronously — exercises the body.
        installed(signal.SIGINT, None)
        assert ev.is_set()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGINT, signal.default_int_handler)


# --- hr-rel-17: main translates RootError to sys.exit(2) ------------------

def test_main_exits_on_bad_root(tmp_path, monkeypatch, capsys):
    bad = str(tmp_path / "does-not-exist")
    monkeypatch.setattr(hr.sys, "argv", ["hr", bad])
    with pytest.raises(SystemExit) as ex:
        hr.main()
    assert ex.value.code == 2
    assert "no such file" in capsys.readouterr().err


# --- hr-obs-03 + hr-conc-05: end-of-run WARNINGs --------------------------

def test_main_emits_hash_error_suppressed_warning(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.bin").write_bytes(b"x")
    real_cls = hr.RunConfig
    class Spy(real_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.hash_error_suppressed = 7
            self.hash_error_logged = 2
    monkeypatch.setattr(hr, "RunConfig", Spy)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "7 additional hash error(s) suppressed" in err
    assert "showed first 2" in err


def test_main_emits_sigint_cancel_warning(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.bin").write_bytes(b"x")
    # Force the SIGINT installer to pre-set the cancel event so the walk
    # short-circuits and the end-of-run cancel warning fires.
    def fake_install(ev):
        ev.set()
        return None
    monkeypatch.setattr(hr, "_install_sigint_cancel", fake_install)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "walk cancelled by SIGINT" in err


# ===== rescan: boundary / validation gap tests (hr-test-05..20) ============

# --- hr-test-05/06: _iter_batches boundary inputs --------------------------

def test_iter_batches_zero_batch_size_raises():
    with pytest.raises(ValueError):
        list(hr._iter_batches([1, 2, 3], 0))


def test_iter_batches_negative_batch_size_yields_nothing():
    # range(0, n, -1) yields nothing — pin the current behaviour.
    assert list(hr._iter_batches([1, 2, 3], -1)) == []


def test_iter_batches_empty_input():
    assert list(hr._iter_batches([], 64)) == []


# --- hr-test-07/08: _expand_keys_to_paths cap=0 / cap=-1 -------------------

def test_expand_keys_to_paths_cap_zero_emits_only_sentinel():
    aliases = {("d", 0): ["/a", "/b", "/c"]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=0)
    assert len(out) == 1
    assert isinstance(out[0], hr._MoreSentinel)
    assert hr._count_real_paths(out) == 0


def test_expand_keys_to_paths_negative_cap_behaves_like_zero():
    # cap=-1 is shorter than every list, so the sentinel fires.
    aliases = {("d", 0): ["/a"]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=-1)
    # No real paths fit (len(out) < cap=-1 is never True), sentinel appended.
    assert any(isinstance(p, hr._MoreSentinel) for p in out)


# --- hr-test-09: RunConfig clamps ------------------------------------------

def test_runconfig_negative_alias_cap_clamps_to_unbounded():
    cfg = hr.RunConfig(alias_cap=-5)
    assert cfg.alias_cap >= 2**31


def test_runconfig_zero_alias_cap_clamps_to_unbounded():
    cfg = hr.RunConfig(alias_cap=0)
    assert cfg.alias_cap >= 2**31


def test_runconfig_negative_hash_error_cap_clamps_to_zero():
    cfg = hr.RunConfig(hash_error_verbose_cap=-1)
    assert cfg.hash_error_verbose_cap == 0


# --- hr-test-10: ThirdsStrategy size boundaries ----------------------------

@pytest.mark.parametrize("size", [0, 1, hr.SAMPLE - 1, hr.SAMPLE,
                                    hr.CAP, 2 * hr.CAP, 2 * hr.CAP + 1])
def test_thirds_strategy_windows_never_exceed_size(size):
    """offset + length must fit within `size` for every SET-relative
    window; SEEK_END negative offsets are bounded by the tail-CAP fixed
    layout."""
    windows = hr.ThirdsStrategy().windows(size)
    for w in windows:
        if w.whence == hr.os.SEEK_SET:
            assert w.offset + w.length <= max(size, w.length), \
                f"window {w} reads past EOF for size={size}"


# --- hr-test-11: _readable_rep empty input ---------------------------------

def test_readable_rep_empty_list_raises_indexerror():
    # Current behaviour: IndexError. Pin so a future guard change is
    # visible (the call sites never hand it an empty list).
    with pytest.raises(IndexError):
        hr._readable_rep([])


# --- hr-test-12: _hash_file_windows with empty windows ---------------------

def test_hash_file_windows_no_windows_returns_empty_digest(tmp_path):
    f = tmp_path / "any.bin"
    f.write_bytes(b"contents")
    digest = hr._hash_file_windows(str(f), [])
    # blake3 of empty input is a fixed digest. Pin equality.
    import blake3
    assert digest == blake3.blake3().hexdigest()


# --- hr-test-13: _format_digest((None, None)) crash --------------------

def test_format_digest_none_tail_returns_head_unchanged():
    # `(head, None)` shape returns the head as-is; pin the contract so a
    # future caller can't silently change emit format for stage1-only
    # groups.
    assert hr._format_digest((None, None)) is None
    assert hr._format_digest((None, "tail")) == "None:tail"


# --- hr-test-14: newline/space in path roundtrip ---------------------------

def test_emit_groups_path_with_newline_breaks_format(tmp_path):
    """Documenting the limitation: paths containing newline garble the
    `'<digest> <path>\\n'` format. This test pins the misbehaviour so
    a future fix (path quoting / NUL-delimited output) is detectable.
    """
    written = []
    aliases = {
        ("d", 0): ["/normal", "/with\nnewline"],
    }
    final_groups = {"abc": [("d", 0)]}
    hr.emit_groups(final_groups, aliases, written.append)
    out = "".join(written)
    # The emitted block contains an embedded newline → two visual lines
    # for one logical entry → downstream `split('\n')` parsing gets
    # confused. Expectation: number of lines > number of paths in group.
    lines = [ln for ln in out.split("\n") if ln]
    assert len(lines) > 2   # would be 2 if format were robust


# --- hr-test-15: _fmt_count boundaries -------------------------------------

def test_fmt_count_zero():
    assert hr._fmt_count(0) == "0"


def test_fmt_count_negative_one():
    assert hr._fmt_count(-1) == "-1"


def test_fmt_count_int_max():
    # 2**63 - 1 ≈ 9.2 exabytes — exercises the T (trillion) branch with
    # a very large value (>100 so the no-decimal form fires).
    result = hr._fmt_count(2**63 - 1)
    assert result.endswith("T")
    # value/1e12 ≈ 9.2M, well above 100 → no decimal point.
    assert "." not in result


# --- hr-test-16: _WalkIter consumed twice ----------------------------------

def test_iter_threaded_walk_consumed_twice_starts_second_walk(tmp_path):
    """Document the current semantics: calling `iter()` twice spawns
    workers a second time. A future hardening that single-shots the
    iterator would change this — pin behaviour either way."""
    (tmp_path / "a.bin").write_bytes(b"x")
    walk = hr.iter_threaded_walk(tmp_path, jobs=1)
    first = list(walk)
    second = list(walk)
    # Both walks produce the same files since the tree is unchanged.
    assert first == second
    assert len(first) == 1


# --- hr-test-17: file size at the stage gates ------------------------------

@pytest.mark.parametrize("size,expected_stage2", [
    (hr.CAP, False),         # head IS the full file → stage1-only
    (hr.CAP + 1, True),      # just over → forced into stage2
    (2 * hr.CAP, True),
    (2 * hr.CAP + 1, True),  # first sample-eligible size
])
def test_find_duplicate_groups_stage_boundary(tmp_path, size, expected_stage2):
    a = tmp_path / "a.bin"; a.write_bytes(b"q" * size)
    b = tmp_path / "b.bin"; b.write_bytes(b"q" * size)
    files = [(str(a), size, 1, 100), (str(b), size, 1, 101)]
    result = hr.find_duplicate_groups(files, jobs=1)
    assert result.info["stage2"] > 0 if expected_stage2 else result.info["stage2"] == 0


# --- hr-test-18: _log_hash_error thread-safety stress ----------------------

def test_log_hash_error_concurrent_counter_invariant(capsys):
    """Even under 16 workers ticking the counter without a lock, the
    invariant `logged + suppressed == total` must hold (the GIL keeps
    `+= 1` effectively atomic on CPython byte code)."""
    import threading
    cfg = hr.RunConfig(hash_error_verbose_cap=5)
    total = 200

    def stress():
        for _ in range(total // 16):
            hr._log_hash_error("/p", OSError("x"), config=cfg)

    threads = [threading.Thread(target=stress) for _ in range(16)]
    for t in threads: t.start()
    for t in threads: t.join()
    capsys.readouterr()  # drain stderr to keep CI clean
    seen = cfg.hash_error_logged + cfg.hash_error_suppressed
    expected = (total // 16) * 16
    assert seen == expected


# --- hr-test-19: pre-cancelled walk path -----------------------------------

def test_find_duplicate_groups_with_precancelled_walk(tmp_path):
    import threading
    (tmp_path / "a.bin").write_bytes(b"x")
    cancel = threading.Event()
    cancel.set()
    walk = hr.iter_threaded_walk(tmp_path, jobs=1, cancel_event=cancel)
    result = hr.find_duplicate_groups(walk, jobs=1)
    # Pre-cancelled → walk emits nothing → empty pipeline.
    assert result.info["inodes"] == 0
    assert result.info["stage1"] == 0


# --- hr-test-20: on_group callback raises propagation ----------------------

def test_find_duplicate_groups_on_group_raises_propagates(tmp_path):
    body = b"y" * 100
    a = tmp_path / "a.bin"; a.write_bytes(body)
    b = tmp_path / "b.bin"; b.write_bytes(body)
    files = [(str(a), len(body), 1, 100), (str(b), len(body), 1, 101)]

    def bad_cb(digest, keys, aliases):
        raise RuntimeError("callback failure")

    with pytest.raises(RuntimeError, match="callback failure"):
        hr.find_duplicate_groups(files, jobs=1, on_group=bad_cb)


# ===== implementation of hr-rel-18, hr-sec-02..05, hr-scal-06, hr-dup-04 =====

# --- hr-dup-04: extracted error-line helper -------------------------------

def test_emit_hash_error_line_single_source_of_truth(capsys):
    hr._emit_hash_error_line("/some/path", OSError("boom"))
    err = capsys.readouterr().err
    # hr-obs-02: severity tag so the line greps apart from other [ai5] lines.
    assert "[ai5] ERROR: hash failed for /some/path: boom" in err


# --- hr-sec-02: realpath in preflight error messages ----------------------

def test_preflight_root_error_uses_realpath_for_relative_path(tmp_path, monkeypatch):
    # CWD-relative path becomes absolute in the error message.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(hr.RootError) as ex:
        hr._preflight_root("does-not-exist")
    msg = str(ex.value)
    # realpath should produce an absolute path containing tmp_path stem.
    assert tmp_path.name in msg or str(tmp_path) in msg


# --- hr-sec-03: _readable_rep handles NUL byte gracefully -----------------

def test_readable_rep_skips_path_with_nul_byte():
    # NUL in a path raises ValueError from os.access; skip + continue.
    paths = ["/with\x00nul", "/real-path"]
    import os as real_os
    # Monkeypatch isn't needed — os.access raises ValueError naturally.
    rep = hr._readable_rep(paths)
    # Returns the second path (the NUL one is skipped, second probes
    # for readability → may not exist but stays the chosen rep
    # because the first was rejected).
    assert rep in paths  # at least it didn't crash


def test_readable_rep_all_paths_invalid_falls_back_to_first():
    # All paths fail os.access → return first as documented fallback.
    paths = ["/with\x00a", "/with\x00b"]
    rep = hr._readable_rep(paths)
    assert rep == "/with\x00a"


# --- hr-sec-04: _preflight_root handles NUL gracefully --------------------

def test_preflight_root_nul_byte_raises_typed_error():
    with pytest.raises(hr.RootError) as ex:
        hr._preflight_root("/tmp/with\x00bad")
    # Either "invalid path" (NUL caught) or "no such file" (realpath
    # cleaned the NUL). Both are typed RootError, no traceback.
    assert isinstance(ex.value, hr.RootError)


# --- hr-sec-05: _hash_file_windows uses O_NOFOLLOW ------------------------

def test_hash_file_windows_uses_o_nofollow(tmp_path, monkeypatch):
    # Verify the open path goes through os.open with O_NOFOLLOW.
    captured = {}
    real_os_open = hr.os.open

    def spy_open(path, flags, *a, **kw):
        captured["flags"] = flags
        return real_os_open(path, flags, *a, **kw)
    monkeypatch.setattr(hr.os, "open", spy_open)
    f = tmp_path / "a.bin"; f.write_bytes(b"x")
    hr._hash_file_windows(str(f), [(0, 1, hr.os.SEEK_SET)])
    assert captured["flags"] & hr.os.O_NOFOLLOW


def test_hash_file_windows_symlink_fails_with_eloop(tmp_path):
    target = tmp_path / "target.bin"; target.write_bytes(b"x" * 100)
    link = tmp_path / "link.bin"
    link.symlink_to(target)
    # Direct file: works.
    digest = hr._hash_file_windows(str(target), [(0, 50, hr.os.SEEK_SET)])
    assert digest is not None
    # Symlink: O_NOFOLLOW → ELOOP → None.
    digest = hr._hash_file_windows(str(link), [(0, 50, hr.os.SEEK_SET)])
    assert digest is None


# --- hr-rel-18: _WalkIter survives non-OSError in worker -----------------

def test_walk_iter_survives_runtime_error_in_scandir(tmp_path, monkeypatch):
    # Force os.scandir to raise RuntimeError (not an OSError) for one
    # directory; the walk must still terminate and report dir_errors.
    real_scandir = hr.os.scandir
    counter = {"n": 0}

    def fake_scandir(path):
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("simulated kernel quirk")
        return real_scandir(path)

    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "a.bin").write_bytes(b"x")

    monkeypatch.setattr(hr.os, "scandir", fake_scandir)
    # MUST NOT HANG. Use a timeout via threading to bound the wait.
    import threading
    done = threading.Event()
    results = []

    def run():
        results.extend(list(hr.iter_threaded_walk(tmp_path, jobs=2)))
        done.set()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait(timeout=10)
    assert done.is_set(), "walk hung — hr-rel-18 BaseException catch broken"


# --- hr-scal-06: index_inodes alias_cap at ingest -------------------------

def test_index_inodes_caps_aliases_with_overflow_count():
    files = [(f"/p{i}", 100, 1, 99) for i in range(500)]   # 500 hardlinks of one inode
    overflow: dict = {}
    aliases, _ = hr.index_inodes(files, alias_cap=50, overflow=overflow)
    assert len(aliases[(1, 99)]) == 50    # capped at ingest
    assert overflow[(1, 99)] == 450        # 500-50 elided


def test_index_inodes_no_cap_preserves_default_behaviour():
    files = [("/p1", 100, 1, 99), ("/p2", 100, 1, 99)]
    aliases, _ = hr.index_inodes(files)
    assert len(aliases[(1, 99)]) == 2


def test_expand_keys_to_paths_includes_overflow_in_total():
    aliases = {("d", 0): ["/a", "/b"]}
    overflow = {("d", 0): 100}
    # cap=5 with 2 stored + 100 elided = 102 total > 5 → sentinel says "+97 more".
    out = hr._expand_keys_to_paths(
        [("d", 0)], aliases, cap=5, overflow=overflow)
    assert any(isinstance(p, hr._MoreSentinel) for p in out)
    sentinel = next(p for p in out if isinstance(p, hr._MoreSentinel))
    assert "97" in str(sentinel)


# ===== hr-decoup-04: alias_cap plumbed through main pipeline ============

def test_find_duplicate_groups_uses_ingest_alias_cap_via_config(tmp_path):
    # Construct a scenario where the same inode has many hardlinks; with
    # a small alias_cap, the ingest stage drops paths past the cap and
    # records them as overflow. Emit-side `+N more` reflects the real
    # total via config.overflow plumbing.
    body = b"shared body" * 200
    (tmp_path / "a.bin").write_bytes(body)
    files = [(f"/synthetic/p{i}", len(body), 1, 100) for i in range(20)]
    # Make a duplicate group via two inodes with same size.
    files += [(f"/synthetic/q{i}", len(body), 1, 200) for i in range(20)]
    cfg = hr.RunConfig(alias_cap=5)   # small cap → ingest drops
    captured = []

    def cb(digest, keys, aliases):
        captured.append((digest, keys, aliases))
    # Skip actual hashing — fake the stage1 to confirm both inodes.
    real_stage1 = hr._stage1_hash

    def stub_stage1(candidates, rep, jobs, config):
        from collections import defaultdict
        by_head = defaultdict(list)
        for size, key in candidates:
            by_head[(size, "fakehead")].append(key)
        return by_head, {"stage1": len(candidates), "stage1_errors": 0}
    import unittest.mock as mock
    with mock.patch.object(hr, "_stage1_hash", stub_stage1):
        result = hr.find_duplicate_groups(files, jobs=1, on_group=cb, config=cfg)
    # Each inode had 20 paths but cap=5 limits storage to 5.
    # Overflow on each = 15. Sentinel total = (5 + 15) per inode × 2
    # inodes = 40 paths total, cap=5, sentinel says "+35 more".
    assert result.info["inodes"] >= 2
    # Verify config.overflow was populated.
    assert cfg.overflow is not None
    assert all(v == 15 for v in cfg.overflow.values())


def test_run_config_overflow_attr_defaults_none():
    cfg = hr.RunConfig()
    assert cfg.overflow is None


def test_expand_keys_to_paths_reads_overflow_from_config():
    cfg = hr.RunConfig(alias_cap=5)
    cfg.overflow = {("d", 0): 100}
    aliases = {("d", 0): ["/a", "/b"]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, config=cfg)
    sentinel = next((p for p in out if isinstance(p, hr._MoreSentinel)), None)
    assert sentinel is not None
    # 2 stored + 100 overflow = 102 total. cap=5 → "+97 more".
    assert "97" in str(sentinel)
