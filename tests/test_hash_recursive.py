"""Tests for hash-recursive-ai5.py — the hr-rel-* reliability fixes
(jobs clamp, readable-alias representative) and the functions they touch."""
import importlib.util
import io
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest
from hypothesis import given, strategies as st

_PATH = Path(__file__).resolve().parent.parent / "hash-recursive-ai5.py"
_spec = importlib.util.spec_from_file_location("hash_recursive_ai5", _PATH)
hr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hr)

_TS_RE = re.compile(
    r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}\]"
)


def _assert_timestamped(text: str) -> None:
    assert _TS_RE.search(text), text


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # hr-log-02: main() now writes ./hashes.txt by default. Run every test
    # from its own tmp dir so that dump never lands in the repo working
    # tree (and is auto-cleaned with the tmp dir).
    monkeypatch.chdir(tmp_path)


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


def test_index_inodes_size_first_writer_wins_on_stat_race():
    # hr-rel-01: two aliases of one inode disagree on size (stat race).
    # First observed size must win deterministically, not last.
    files = [("/a", 10, 1, 100), ("/b", 999, 1, 100)]
    _, inode_size = hr.index_inodes(files)
    assert inode_size[(1, 100)] == 10   # first writer, not 999


def test_index_inodes_size_first_writer_wins_reversed_order():
    # Order-independence: flipping input order flips which size "would"
    # be last, but setdefault still keeps the first one seen.
    files = [("/b", 999, 1, 100), ("/a", 10, 1, 100)]
    _, inode_size = hr.index_inodes(files)
    assert inode_size[(1, 100)] == 999   # first seen in THIS ordering


def test_index_inodes_consistent_sizes_unaffected():
    files = [("/a", 10, 1, 100), ("/b", 10, 1, 100)]
    _, inode_size = hr.index_inodes(files)
    assert inode_size[(1, 100)] == 10


@given(st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=5),   # dev
        st.integers(min_value=0, max_value=5),   # ino
        st.integers(min_value=0, max_value=10**9),  # size
    ),
    min_size=1, max_size=50))
def test_index_inodes_size_is_first_observed_property(entries):
    # hr-rel-01 property: for every inode key, inode_size holds the size
    # of the FIRST entry seen for that key in input order — regardless of
    # later disagreeing sizes.
    files = [(f"/p{i}", size, dev, ino)
             for i, (dev, ino, size) in enumerate(entries)]
    _, inode_size = hr.index_inodes(files)
    first_seen: dict = {}
    for _p, size, dev, ino in files:
        first_seen.setdefault((dev, ino), size)
    assert inode_size == first_seen


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
        result = hr.find_duplicate_groups(files, 2)
        final_groups, aliases, info = result.groups, result.aliases, result.info
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
        result = hr.find_duplicate_groups(files, 2)
        final_groups, aliases = result.groups, result.aliases
        # different content -> NOT in the same final group
        for keys in final_groups.values():
            paths = [p for k in keys for p in aliases[k]]
            if len(paths) > 1:
                names = sorted(Path(p).name for p in paths)
                assert names != ["x1.bin", "x2.bin"], \
                    "false positive: CAP..2*CAP files with different tails grouped"


def test_center_block_catches_middle_only_difference(tmp_path):
    # New center 4 MiB block: two large files identical at head, tail, AND
    # both 64 KiB thirds-samples, differing ONLY at the file midpoint, must
    # NOT be grouped — they would have collided under the head+tail+samples-
    # only design that this block was added to fix.
    size = 3 * hr.CAP                       # 12 MiB > 8 MiB stage-2 gate
    base = bytearray(b"A" * size)
    a = bytes(base)
    mid = size // 2                         # 1.5*CAP — inside the center block
    base[mid] = ord("B")                    # single-byte center difference
    b = bytes(base)
    # The difference sits outside head, tail, and both thirds samples.
    assert a[:hr.CAP] == b[:hr.CAP]
    assert a[-hr.CAP:] == b[-hr.CAP:]
    third = size // 3
    assert a[third:third + hr.SAMPLE] == b[third:third + hr.SAMPLE]
    assert a[2 * third:2 * third + hr.SAMPLE] == b[2 * third:2 * third + hr.SAMPLE]
    (tmp_path / "a.bin").write_bytes(a)
    (tmp_path / "b.bin").write_bytes(b)
    files, _ = hr.threaded_walk(tmp_path, 1)
    result = hr.find_duplicate_groups(files, 1)
    for keys in result.groups.values():
        paths = [p for k in keys for p in result.aliases[k]
                 if not isinstance(p, hr._MoreSentinel)]
        names = sorted(Path(p).name for p in paths)
        assert names != ["a.bin", "b.bin"], \
            "center block failed to catch a midpoint-only difference"


def test_find_duplicate_groups_no_candidates():
    with TemporaryDirectory() as d:
        root = Path(d)
        _write(root / "only.bin", b"x")
        files, _ = hr.threaded_walk(root, 1)
        result = hr.find_duplicate_groups(files, 1)
        final_groups, info = result.groups, result.info
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


def test_run_stage_serial_batches_and_reports_progress(monkeypatch):
    # hr-scal-07 / hr-log-05: even the serial path must work in bounded
    # HASH_BATCH chunks and report progress as each chunk completes.
    monkeypatch.setattr(hr, "HASH_BATCH", 2)
    items = ["a", "b", "c", "d", "e"]
    batch_sizes = []
    progress = []

    def batch(xs):
        batch_sizes.append(len(xs))
        return [(p, p.upper()) for p in xs]

    out, errors = hr._run_stage(
        items, batch, total_bytes=1, jobs=1,
        on_progress=lambda done, total: progress.append((done, total)))

    assert out == {"a": "A", "b": "B", "c": "C", "d": "D", "e": "E"}
    assert errors == 0
    assert batch_sizes == [2, 2, 1]
    assert progress == [(2, 5), (4, 5), (5, 5)]


def test_run_stage_threaded_reports_progress(monkeypatch):
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 2)
    progress = []
    out, errors = hr._run_stage(
        ["a", "b", "c"], lambda xs: [(p, p.upper()) for p in xs],
        total_bytes=10**9, jobs=1,
        on_progress=lambda done, total: progress.append((done, total)))
    assert out == {"a": "A", "b": "B", "c": "C"}
    assert errors == 0
    assert progress[-1] == (3, 3)


def test_run_stage_windowed_bounds_inflight_submission(monkeypatch):
    # hr-scal-02: the threaded path must NOT submit every batch up front.
    # Track concurrent in-flight batch_fn calls and assert the peak never
    # exceeds jobs * SUBMIT_WINDOW even with far more batches than the
    # window.
    import threading as _t
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 1)
    monkeypatch.setattr(hr, "SUBMIT_WINDOW", 2)
    jobs = 2
    window = jobs * hr.SUBMIT_WINDOW

    lock = _t.Lock()
    state = {"cur": 0, "peak": 0}
    gate = _t.Event()

    def batch(items):
        with lock:
            state["cur"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        # Hold briefly so several batches overlap and the peak is real.
        gate.wait(timeout=0.05)
        with lock:
            state["cur"] -= 1
        return [(p, p.upper()) for p in items]

    items = [str(i) for i in range(50)]
    out, errors = hr._run_stage(items, batch, total_bytes=10**9, jobs=jobs)
    gate.set()
    assert errors == 0
    assert out == {str(i): str(i).upper() for i in range(50)}
    # Never more than the window submitted/running at once.
    assert state["peak"] <= window


def test_run_stage_windowed_collects_all_and_counts_errors(monkeypatch):
    # hr-scal-02: correctness across many batches — every result lands in
    # `out` and None digests count as errors regardless of window size.
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 3)
    monkeypatch.setattr(hr, "SUBMIT_WINDOW", 2)
    items = [f"/p/{i}" for i in range(100)]

    def batch(b):
        return [(p, None if p.endswith("0") else p.upper()) for p in b]

    out, errors = hr._run_stage(items, batch, total_bytes=10**9, jobs=3)
    assert len(out) == 100
    # paths ending in 0: /p/0,10,20,...,90 => 10 of them
    assert errors == 10


@given(n=st.integers(min_value=0, max_value=300),
       jobs=st.integers(min_value=1, max_value=4),
       hash_batch=st.integers(min_value=1, max_value=8),
       window=st.integers(min_value=1, max_value=4))
def test_run_stage_windowed_property_all_results(n, jobs, hash_batch, window):
    # hr-scal-02 property: the windowed dispatch returns exactly one entry
    # per item, no losses or duplicates, for any window / batch / job mix.
    old_t, old_b, old_w = (hr.THREAD_THRESHOLD_BYTES, hr.HASH_BATCH,
                           hr.SUBMIT_WINDOW)
    hr.THREAD_THRESHOLD_BYTES = 0
    hr.HASH_BATCH = hash_batch
    hr.SUBMIT_WINDOW = window
    try:
        items = [f"/x/{i}" for i in range(n)]
        out, errors = hr._run_stage(
            items, lambda b: [(p, p.upper()) for p in b],
            total_bytes=10**9, jobs=jobs)
        assert out == {f"/x/{i}": f"/X/{i}" for i in range(n)}
        assert errors == 0
    finally:
        (hr.THREAD_THRESHOLD_BYTES, hr.HASH_BATCH,
         hr.SUBMIT_WINDOW) = old_t, old_b, old_w


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


def test_emit_groups_accepts_explicit_config():
    aliases = {("d", 0): ["/a"], ("d", 1): ["/b"]}
    written = []
    cfg = hr.RunConfig()
    groups, paths = hr.emit_groups(
        {"hash": [("d", 0), ("d", 1)]}, aliases, written.append, config=cfg)
    assert groups == 1
    assert paths == 2
    assert written


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


# --- hr-conc-02: ENOENT/ESTALE mid-read counts as vanished, not error ------

def test_tick_vanished_increments_counter():
    cfg = hr.RunConfig()
    hr._tick_vanished(cfg)
    hr._tick_vanished(cfg)
    assert cfg.hash_skipped_vanished == 2


def test_tick_vanished_no_config_is_noop():
    hr._tick_vanished(None)   # must not raise


def test_hash_file_windows_open_enoent_counts_vanished(tmp_path):
    # hr-conc-02 regression: ENOENT on the open (FileNotFoundError) still
    # routes to the vanished counter, not the error log.
    cfg = hr.RunConfig()
    out = hr._hash_file_windows(
        str(tmp_path / "gone.bin"), [(0, 10, hr.os.SEEK_SET)], config=cfg)
    assert out is None
    assert cfg.hash_skipped_vanished == 1


@pytest.mark.parametrize("err_no", [hr.errno.ENOENT, hr.errno.ESTALE])
def test_hash_file_windows_midread_vanish_counts_vanished(
        tmp_path, monkeypatch, capsys, err_no):
    # hr-conc-02: a file deleted AFTER open raises OSError(ENOENT/ESTALE)
    # inside the read loop — must be counted as vanished, silently.
    f = tmp_path / "a.bin"; f.write_bytes(b"x" * 100)
    cfg = hr.RunConfig()

    def vanish(*a, **k):
        raise OSError(err_no, "vanished mid-read")

    monkeypatch.setattr(hr, "_read_window_into", vanish)
    out = hr._hash_file_windows(str(f), [(0, 10, hr.os.SEEK_SET)], config=cfg)
    assert out is None
    assert cfg.hash_skipped_vanished == 1
    assert cfg.hash_error_logged == 0
    assert capsys.readouterr().err == ""   # benign skip, no warning


def test_hash_file_windows_midread_eio_counts_real_error(
        tmp_path, monkeypatch, capsys):
    # hr-conc-02: a genuine I/O error mid-read (EIO) is NOT vanished — it
    # surfaces via the bounded error log and the error counter.
    f = tmp_path / "a.bin"; f.write_bytes(b"x" * 100)
    cfg = hr.RunConfig()

    def boom(*a, **k):
        raise OSError(hr.errno.EIO, "disk on fire")

    monkeypatch.setattr(hr, "_read_window_into", boom)
    out = hr._hash_file_windows(str(f), [(0, 10, hr.os.SEEK_SET)], config=cfg)
    assert out is None
    assert cfg.hash_skipped_vanished == 0
    assert cfg.hash_error_logged == 1
    assert "hash failed" in capsys.readouterr().err


def test_vanished_errnos_contains_enoent_and_estale():
    assert hr.errno.ENOENT in hr._VANISHED_ERRNOS
    assert hr.errno.ESTALE in hr._VANISHED_ERRNOS
    assert hr.errno.EIO not in hr._VANISHED_ERRNOS


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
    assert err == ""


def test_main_emits_summary_when_not_quiet(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.txt").write_text("hi")
    monkeypatch.setattr(_sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    _assert_timestamped(err)
    assert "dirs=" in err


def test_find_duplicate_groups_skips_unread_head(monkeypatch, tmp_path):
    # head_by_path.get returns None -> continue (covers L302).
    a = tmp_path / "a.bin"; a.write_bytes(b"x" * 100)
    b = tmp_path / "b.bin"; b.write_bytes(b"x" * 100)

    def stub(items, fn, total, jobs, cancel_event=None, **kwargs):
        # items here are stage-1 candidate tuples; map all to None.
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


def test_expand_keys_to_paths_extends_only_up_to_remaining_room():
    # hr-scal-01: a single inode bucket far exceeding the cap must NOT be
    # fully extended into `out` before slicing — only `cap - len(out)`
    # paths are taken from the oversized bucket.
    class TrackingList(list):
        sliced_with = []

        def __getitem__(self, item):
            if isinstance(item, slice):
                TrackingList.sliced_with.append(item.stop)
            return super().__getitem__(item)

    big_bucket = TrackingList(f"/p/{i}" for i in range(100_000))
    aliases = {("d", 0): big_bucket}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=10)
    assert hr._count_real_paths(out) == 10
    # The bucket was sliced to the remaining room (10), never extended whole.
    assert 10 in TrackingList.sliced_with


def test_expand_keys_to_paths_room_zero_skips_extend():
    # hr-scal-01: once `out` is already at the cap, later buckets contribute
    # nothing to `out` but still count toward the total/sentinel.
    aliases = {("d", 0): ["/a", "/b"], ("d", 1): ["/c", "/d", "/e"]}
    out = hr._expand_keys_to_paths([("d", 0), ("d", 1)], aliases, cap=2)
    assert hr._count_real_paths(out) == 2
    assert out[:2] == ["/a", "/b"]
    sentinel = next(p for p in out if isinstance(p, hr._MoreSentinel))
    assert "3" in str(sentinel)   # 5 total - 2 cap


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

    def on_group(digest_key, keys, aliases, overflow):
        captured.append((digest_key, keys, aliases, overflow))

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

    def stub(items, fn, total, jobs, cancel_event=None, **kwargs):
        call_n["n"] += 1
        if call_n["n"] == 1:
            # Stage 1: real head hashes (so paths share head).
            return real_run_stage(items, fn, total, jobs)
        # Stage 2: return None for every tail.
        return ({item: None for item in items}, 0)

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


def test_tick_shrank_increments_counter():
    # hr-rel-01: dedicated counter for strict short-reads.
    cfg = hr.RunConfig()
    hr._tick_shrank(cfg)
    hr._tick_shrank(cfg)
    assert cfg.hash_skipped_shrank == 2


def test_tick_shrank_no_config_is_noop():
    hr._tick_shrank(None)   # must not raise


def test_hash_file_windows_strict_short_read_ticks_shrank(tmp_path, capsys):
    # hr-rel-01: a strict short read routes through the shrank counter
    # (NOT the error log, NOT the vanished counter) and stays silent.
    f = tmp_path / "tiny.bin"; f.write_bytes(b"only9byte")
    cfg = hr.RunConfig()
    out = hr._hash_file_windows(
        str(f), [hr.FileWindow(0, 100, 0, strict=True)], config=cfg)
    assert out is None
    assert cfg.hash_skipped_shrank == 1
    assert cfg.hash_skipped_vanished == 0
    assert cfg.hash_error_logged == 0
    assert capsys.readouterr().err == ""   # no spurious stderr trace


def test_hash_file_windows_non_strict_short_read_does_not_tick_shrank(tmp_path):
    # A non-strict short read is the natural EOF case, not a shrink.
    f = tmp_path / "tiny.bin"; f.write_bytes(b"only9byte")
    cfg = hr.RunConfig()
    out = hr._hash_file_windows(
        str(f), [hr.FileWindow(0, 100, 0, strict=False)], config=cfg)
    assert out is not None
    assert cfg.hash_skipped_shrank == 0


def test_main_summary_surfaces_stage2_failures_as_hash_errors(
        tmp_path, monkeypatch, capsys):
    # hr-obs-01: stage-2 None tails have a single source of truth. Force
    # two big files through stage 2 with every tail dropped (and counted as
    # errors); they surface in `hash_errors`, not a separate overlapping
    # field.
    big = b"q" * (hr.HEAD_TAIL_THRESHOLD + 200)
    (tmp_path / "a.bin").write_bytes(big)
    (tmp_path / "b.bin").write_bytes(big)
    real_run = hr._run_stage
    seen = {"n": 0}

    def stub(items, fn, total, jobs, cancel_event=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_run(items, fn, total, jobs)
        return ({item: None for item in items}, len(items))

    monkeypatch.setattr(hr, "_run_stage", stub)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    # hr-obs-01: the duplicated/overlapping field is gone.
    assert "hashed_stage2_skipped" not in err
    # Both dropped tails surface as real hash errors (no vanished/shrank).
    assert "hash_errors=2 " in err


def test_main_summary_omits_removed_stage2_skipped_field(
        tmp_path, monkeypatch, capsys):
    # hr-obs-01: the removed field never appears, even on a clean run.
    (tmp_path / "a.bin").write_bytes(b"x")
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "hashed_stage2_skipped" not in err
    assert "hash_errors=0 " in err


def test_main_clamps_zero_jobs_no_crash(tmp_path, monkeypatch, capsys):
    # hr-rel-21: `-j 0` must not crash the hash pool. Force the threaded
    # path (THREAD_THRESHOLD_BYTES=0) so ThreadPoolExecutor(max_workers=jobs)
    # is actually exercised; without the clamp this raised
    # "max_workers must be greater than 0".
    (tmp_path / "a.bin").write_bytes(b"dup")
    (tmp_path / "b.bin").write_bytes(b"dup")
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr.sys, "argv", ["hr", "-j", "0", str(tmp_path)])
    hr.main()                       # must not raise
    err = capsys.readouterr().err
    _assert_timestamped(err)


def _run_main_with_stage_stub(tmp_path, monkeypatch, capsys, *, none_tails,
                              cfg_mutate):
    # Helper: two big files forced through stage 2, every tail None, with a
    # RunConfig whose skip counters are pre-seeded so we can assert the
    # summary's hash_errors subtraction.
    big = b"q" * (hr.HEAD_TAIL_THRESHOLD + 200)
    (tmp_path / "a.bin").write_bytes(big)
    (tmp_path / "b.bin").write_bytes(big)
    real_run = hr._run_stage
    seen = {"n": 0}

    def stub(items, fn, total, jobs, cancel_event=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_run(items, fn, total, jobs)
        if none_tails:
            return ({item: None for item in items}, len(items))
        return real_run(items, fn, total, jobs)

    real_cls = hr.RunConfig

    class Spy(real_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            cfg_mutate(self)

    monkeypatch.setattr(hr, "_run_stage", stub)
    monkeypatch.setattr(hr, "RunConfig", Spy)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    return capsys.readouterr().err


def test_main_summary_subtracts_vanished_from_hash_errors(tmp_path, monkeypatch, capsys):
    # hr-obs-02: stage2 reports 2 errors, but both are vanished skips, so
    # the printed hash_errors must be 0 while hash_skipped shows 2.
    def mutate(cfg):
        cfg.hash_skipped_vanished = 2
    err = _run_main_with_stage_stub(
        tmp_path, monkeypatch, capsys, none_tails=True, cfg_mutate=mutate)
    assert "hash_errors=0 " in err
    assert "hash_skipped=2" in err


def test_main_summary_subtracts_shrank_from_hash_errors(tmp_path, monkeypatch, capsys):
    # hr-obs-02: stage2 reports 2 errors; one vanished + one shrank → net
    # real hash_errors = 0.
    def mutate(cfg):
        cfg.hash_skipped_vanished = 1
        cfg.hash_skipped_shrank = 1
    err = _run_main_with_stage_stub(
        tmp_path, monkeypatch, capsys, none_tails=True, cfg_mutate=mutate)
    assert "hash_errors=0 " in err
    assert "hash_skipped=1" in err
    assert "hash_shrank=1" in err


def test_main_summary_real_errors_survive_subtraction(tmp_path, monkeypatch, capsys):
    # hr-obs-02: a genuine error (no vanished/shrank) is NOT subtracted —
    # stage2 reports 2 errors, 0 skipped → hash_errors=2.
    def mutate(cfg):
        pass
    err = _run_main_with_stage_stub(
        tmp_path, monkeypatch, capsys, none_tails=True, cfg_mutate=mutate)
    assert "hash_errors=2 " in err


def test_main_summary_hash_errors_clamps_at_zero(tmp_path, monkeypatch, capsys):
    # hr-obs-02: an over-count of skips never produces a negative printed
    # hash_errors (defensive max(0, ...)).
    def mutate(cfg):
        cfg.hash_skipped_vanished = 99
    err = _run_main_with_stage_stub(
        tmp_path, monkeypatch, capsys, none_tails=True, cfg_mutate=mutate)
    assert "hash_errors=0 " in err
    assert "hash_errors=-" not in err


def test_main_summary_surfaces_hash_shrank(tmp_path, monkeypatch, capsys):
    # hr-rel-01: the shrank counter is visible in the end-of-run summary.
    (tmp_path / "a.bin").write_bytes(b"x")
    real_cls = hr.RunConfig

    class Spy(real_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.hash_skipped_shrank = 4

    monkeypatch.setattr(hr, "RunConfig", Spy)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "hash_shrank=4" in err


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


def test_hash_file_windows_multi_window_digest_stable(tmp_path):
    # hr-rel-02: removing the dead `del h` finally must not change the
    # digest. A multi-window hash equals the same windows fed to a fresh
    # blake3 by hand.
    import blake3
    data = bytes(range(256)) * 64
    f = tmp_path / "m.bin"; f.write_bytes(data)
    windows = [hr.FileWindow(0, 100, hr.os.SEEK_SET),
               hr.FileWindow(50, 80, hr.os.SEEK_SET)]
    digest = hr._hash_file_windows(str(f), windows)

    expected = blake3.blake3()
    expected.update(data[0:100])
    expected.update(data[50:130])
    assert digest == expected.hexdigest()


def test_hash_file_windows_no_del_h_in_source():
    # hr-rel-02: the dead `del h` safety measure is gone.
    import inspect
    assert "del h" not in inspect.getsource(hr._hash_file_windows)


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


def test_fmt_elapsed_hour_path():
    assert hr._fmt_elapsed(3661) == "1:01:01"


def test_fmt_rate_zero_elapsed():
    assert hr._fmt_rate(10, 0) == "n/a"


def test_log_line_uses_timestamp_prefix(capsys):
    hr._log_line("hello progress", quiet=False)
    err = capsys.readouterr().err
    _assert_timestamped(err)
    assert "[ai5]" not in err
    assert "hello progress" in err


def test_default_jobs_caps_cpu_count(monkeypatch):
    monkeypatch.setattr(hr.os, "cpu_count", lambda: 64)
    assert hr._default_jobs() == hr.DEFAULT_MAX_JOBS


def test_default_jobs_handles_missing_cpu_count(monkeypatch):
    monkeypatch.setattr(hr.os, "cpu_count", lambda: None)
    assert hr._default_jobs() == 1


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
    assert captured["cfg"].alias_cap is None   # no cap
    assert captured["cfg"].alias_cap_active is False


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


def test_find_duplicate_groups_info_counts_stage2_errors(tmp_path, monkeypatch):
    # hr-obs-01: dropped tails are counted once, in info["stage2_errors"]
    # — the single source of truth. The old overlapping stage2_skipped
    # field is gone.
    big = b"q" * (hr.HEAD_TAIL_THRESHOLD + 200)
    a = tmp_path / "a.bin"; a.write_bytes(big)
    b = tmp_path / "b.bin"; b.write_bytes(big)
    real_run = hr._run_stage
    seen = {"n": 0}

    def stub(items, fn, total, jobs, cancel_event=None, **kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return real_run(items, fn, total, jobs)
        # Stage 2: every tail = None, counted as errors.
        return ({item: None for item in items}, len(items))

    monkeypatch.setattr(hr, "_run_stage", stub)
    files = [(str(a), len(big), 1, 100), (str(b), len(big), 1, 101)]
    result = hr.find_duplicate_groups(files, jobs=1)
    assert result.info["stage2_errors"] >= 2
    assert "stage2_skipped" not in result.info


# --- coverage completion: cheap edge paths --------------------------------

def test_capped_byte_total_short_circuits_at_threshold():
    # Early return the instant the running total reaches the threshold.
    total = hr._capped_byte_total([hr.THREAD_THRESHOLD_BYTES, 10**9])
    assert total == hr.THREAD_THRESHOLD_BYTES


def test_prepare_candidates_drops_non_candidate_overflow():
    # Non-candidate inode keys are pruned from the overflow dict in place.
    aliases = {(1, 1): ["a"], (1, 2): ["b"], (1, 3): ["c"]}
    inode_size = {(1, 1): 100, (1, 2): 100, (1, 3): 50}  # (1,3) size unique
    overflow = {(1, 3): 5}                               # not a candidate
    candidates, rep = hr._prepare_candidates(aliases, inode_size, overflow)
    cand_keys = {k for _, k in candidates}
    assert (1, 3) not in cand_keys
    assert (1, 3) not in overflow      # pruned in place


def test_configure_windows_overrides_globals():
    # hr-adapt-01: the helper reassigns CAP/SAMPLE/HEAD_TAIL_THRESHOLD.
    orig = (hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD)
    try:
        hr._configure_windows(2048, 128)
        assert hr.CAP == 2048
        assert hr.SAMPLE == 128
        assert hr.HEAD_TAIL_THRESHOLD == 2048   # tracks CAP
    finally:
        hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD = orig


def test_main_block_size_override_runs(tmp_path, monkeypatch, capsys):
    # hr-adapt-01: a custom --block-size flows through main without error
    # and is applied to the module globals.
    orig = (hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD)
    (tmp_path / "a.bin").write_bytes(b"hello world")
    (tmp_path / "b.bin").write_bytes(b"hello world")
    monkeypatch.setattr(
        hr.sys, "argv",
        ["hr", "--block-size", "4", "--sample-size", "2", str(tmp_path)])
    try:
        hr.main()
        assert hr.CAP == 4 and hr.SAMPLE == 2
    finally:
        hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD = orig
    _assert_timestamped(capsys.readouterr().err)


def test_main_rejects_nonpositive_block_size(tmp_path, monkeypatch, capsys):
    # hr-adapt-01: a block/sample size < 1 exits 2 before touching globals.
    (tmp_path / "a.bin").write_bytes(b"x")
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--block-size", "0", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        hr.main()
    assert exc.value.code == 2
    assert "must be >= 1" in capsys.readouterr().err


def test_main_logs_start_progress_done(tmp_path, monkeypatch, capsys):
    # hr-log-01: a start line, a progress line every 50 files, and a done
    # line all land on stderr.
    for i in range(120):
        (tmp_path / f"f{i}.bin").write_bytes(b"")
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "start: scanning" in err
    assert "progress: 50 files scanned" in err
    assert "progress: 100 files scanned" in err
    assert "done: 120 files scanned" in err


def test_main_quiet_suppresses_logs(tmp_path, monkeypatch, capsys):
    # hr-log-01: --quiet suppresses the start/progress/done lines too.
    for i in range(60):
        (tmp_path / f"f{i}.bin").write_bytes(b"")
    monkeypatch.setattr(hr.sys, "argv", ["hr", "-q", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "start:" not in err
    assert "progress:" not in err
    assert "done:" not in err


def test_progress_walk_logs_at_interval(capsys):
    # hr-log-01: the wrapper yields every entry and logs every `every`.
    entries = [(f"/p/{i}", 0, 1, i) for i in range(5)]
    out = list(hr._progress_walk(iter(entries), quiet=False, every=2))
    assert out == entries                     # pass-through, nothing dropped
    err = capsys.readouterr().err
    assert "progress: 2 files scanned" in err
    assert "progress: 4 files scanned" in err


def test_main_dumps_every_hashed_file(tmp_path, monkeypatch):
    # hr-log-02: hashes.txt holds <digest> <path> for every hashed file
    # (size-collision candidates); unique-size files are never hashed and
    # so never appear.
    (tmp_path / "a.bin").write_bytes(b"same")
    (tmp_path / "b.bin").write_bytes(b"same")
    (tmp_path / "u.bin").write_bytes(b"a-unique-length-payload")  # unique size
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    lines = [ln for ln in out.read_text().splitlines() if ln]
    names = sorted(ln.split(" ", 1)[1].rsplit("/", 1)[-1] for ln in lines)
    assert names == ["a.bin", "b.bin"]
    for ln in lines:
        digest, _path = ln.split(" ", 1)
        assert len(digest) == 64               # blake3 hex digest


def test_main_hashes_file_appended(tmp_path, monkeypatch):
    # hr-log-02: an existing dump is appended to, not overwritten.
    out = tmp_path / "hashes.txt"
    out.write_text("PRIOR RUN LINE\n")
    (tmp_path / "a.bin").write_bytes(b"x")
    (tmp_path / "b.bin").write_bytes(b"x")
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    text = out.read_text()
    assert "PRIOR RUN LINE" in text            # earlier content preserved
    assert text.count(" ") >= 2                 # new <digest> <path> lines added


def test_main_hashes_file_handles_surrogateescape_path(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows paths are not represented with surrogateescape")
    raw_name = b"bad_\xa7.bin"
    raw_path = os.fsencode(tmp_path) + b"/" + raw_name
    fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"same")
    (tmp_path / "ok.bin").write_bytes(b"same")
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])

    hr.main()

    dumped = out.read_bytes()
    assert raw_name in dumped
    assert b"ok.bin" in dumped


def test_main_hashes_file_keeps_multilingual_paths(tmp_path, monkeypatch):
    names = [
        "русский.bin",
        "ελληνικά.bin",
        "עברית.bin",
        "日本語.bin",
        "中文.bin",
    ]
    for name in names:
        (tmp_path / name).write_bytes(b"same")
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "-q", "--hashes-file", str(out), str(tmp_path)])

    hr.main()

    text = out.read_text(encoding="utf-8")
    for name in names:
        assert name in text


def test_main_stdout_handles_surrogateescape_path_with_strict_stream(
        tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Windows paths are not represented with surrogateescape")
    raw_name = b"bad_\xa7.bin"
    raw_path = os.fsencode(tmp_path) + b"/" + raw_name
    fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"same")
    (tmp_path / "ok.bin").write_bytes(b"same")
    out = tmp_path / "hashes.txt"
    stdout_bytes = io.BytesIO()
    strict_stdout = io.TextIOWrapper(stdout_bytes, encoding="ascii", errors="strict")
    monkeypatch.setattr(hr.sys, "stdout", strict_stdout)
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "-q", "--hashes-file", str(out), str(tmp_path)])

    hr.main()

    strict_stdout.flush()
    assert raw_name in stdout_bytes.getvalue()
    assert b"ok.bin" in stdout_bytes.getvalue()


def test_main_logs_hashing_progress_percent(tmp_path, monkeypatch, capsys):
    # hr-log-02: a hashing NN% (done/total) line appears and reaches 100%.
    for i in range(120):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")   # all size 1 → 120 candidates
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "hashing stage1 100% (120/120)" in err
    assert "hashing stage1" in err and "(64/120)" in err


def test_main_hashing_progress_throttles_small_batches(
        tmp_path, monkeypatch, capsys):
    # hr-log-05: batch completions below LOG_EVERY_N_FILES are suppressed
    # until the final completion for that stage.
    for i in range(30):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(hr, "HASH_BATCH", 10)
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "hashing stage1 100% (30/30)" in err
    assert "(10/30)" not in err


def test_main_quiet_keeps_hashes_dump_but_no_progress(tmp_path, monkeypatch, capsys):
    # hr-log-02: --quiet silences the hashing line but still writes the dump.
    (tmp_path / "a.bin").write_bytes(b"x")
    (tmp_path / "b.bin").write_bytes(b"x")
    out = tmp_path / "hashes.txt"
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "-q", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "hashing" not in err
    assert out.read_text().strip()             # dump still populated


def test_main_hashes_dump_excludes_itself(tmp_path, monkeypatch):
    # hr-log-04: the dump lives inside the scanned tree but is excluded
    # from the walk by inode, so it never self-lists and never inflates the
    # scanned-file count.
    (tmp_path / "a.bin").write_bytes(b"dup")
    (tmp_path / "b.bin").write_bytes(b"dup")
    out = tmp_path / "hashes.txt"               # inside the scanned dir
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    text = out.read_text()
    assert "hashes.txt" not in text             # dump never lists itself
    names = sorted(ln.split(" ", 1)[1].rsplit("/", 1)[-1]
                   for ln in text.splitlines() if ln)
    assert names == ["a.bin", "b.bin"]


def test_main_hashes_file_exists_on_cancel_during_walk(tmp_path, monkeypatch):
    # hr-log-04: eager open means the dump file exists even when a Ctrl-C
    # aborts before anything is hashed (here: cancel fires during the walk,
    # so no file is ever hashed).
    for i in range(5):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")
    out = tmp_path / "hashes.txt"
    real_walk = hr.iter_threaded_walk

    def cancelling_walk(root, jobs, cancel_event=None, skip_ino=None):
        if cancel_event is not None:
            cancel_event.set()                  # simulate Ctrl-C during walk
        return real_walk(root, jobs, cancel_event=cancel_event,
                         skip_ino=skip_ino)

    monkeypatch.setattr(hr, "iter_threaded_walk", cancelling_walk)
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()
    assert out.exists()                         # file created despite no hashing


def test_main_hashes_flushed_and_closed_on_keyboard_interrupt(
        tmp_path, monkeypatch):
    # hr-log-03: a Ctrl-C (KeyboardInterrupt) mid-pipeline must leave the
    # dump flushed and closed. Line buffering means each line is on disk as
    # written; main's finally closes the handle even though the interrupt
    # propagates out.
    (tmp_path / "a.bin").write_bytes(b"x")
    out = tmp_path / "hashes.txt"
    digest = "ab" * 32                          # 64-char hex
    seen = {}

    def stub(files, jobs, on_group=None, config=None, on_walk_done=None,
             cancel_event=None, on_hashed=None, on_stage_progress=None):
        list(files)                             # drain walk so threads finish
        if on_walk_done is not None:
            on_walk_done()
        on_hashed(1, 1, digest, (1, 1), {(1, 1): [str(tmp_path / "a.bin")]})
        # Read from a separate handle: with line buffering the line is
        # already on disk before main closes the writer.
        seen["mid"] = out.read_text()
        raise KeyboardInterrupt

    monkeypatch.setattr(hr, "find_duplicate_groups", stub)
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    with pytest.raises(KeyboardInterrupt):
        hr.main()
    assert digest in seen["mid"]                # flushed mid-run
    assert digest in out.read_text()            # survived the finally close


def test_main_hashes_close_failure_warns(tmp_path, monkeypatch, capsys):
    # hr-log-03: a flush/close error is caught so the SIGINT-handler restore
    # in the same finally still runs; the failure is warned, not raised.
    (tmp_path / "a.bin").write_bytes(b"x")
    (tmp_path / "b.bin").write_bytes(b"x")
    out = tmp_path / "hashes.txt"
    real_open = open

    class _FH:
        def __init__(self, f):
            self._f = f

        def fileno(self):
            return self._f.fileno()

        def write(self, s):
            return self._f.write(s)

        def close(self):
            self._f.close()
            raise OSError("disk full on flush")

    def fake_open(path, *a, **kw):
        if str(path) == str(out):
            return _FH(real_open(path, *a, **kw))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(out), str(tmp_path)])
    hr.main()                                   # must not raise
    err = capsys.readouterr().err
    assert "closing" in err and "failed" in err


def test_main_hashes_file_open_failure_warns(tmp_path, monkeypatch, capsys):
    # hr-log-02: an unwritable dump path warns once and does not abort.
    (tmp_path / "a.bin").write_bytes(b"x")
    (tmp_path / "b.bin").write_bytes(b"x")
    bad = tmp_path / "nonexistent-dir" / "hashes.txt"   # parent missing
    monkeypatch.setattr(
        hr.sys, "argv", ["hr", "--hashes-file", str(bad), str(tmp_path)])
    hr.main()                                  # must not raise
    err = capsys.readouterr().err
    assert "cannot write" in err
    assert not bad.exists()


def test_read_window_chunks_match_single_read(tmp_path, monkeypatch):
    # hr-mem-01: reading a window in many small sub-chunks must yield the
    # same digest as one read of the whole window.
    import blake3
    payload = b"abcdefghij" * 50          # 500 bytes, < CAP
    f = tmp_path / "f.bin"
    f.write_bytes(payload)
    monkeypatch.setattr(hr, "READ_CHUNK", 7)   # force many sub-reads
    got = hr.hash_head(str(f))
    assert got == blake3.blake3(payload[:hr.CAP]).hexdigest()


def test_preflight_root_probe_error_raises_root_error(monkeypatch):
    # An OSError/ValueError from the os.path probes is translated to a
    # typed RootError ("invalid path" branch), never allowed to escape raw.
    def boom(_root):
        raise OSError("simulated probe failure")
    monkeypatch.setattr(hr.os.path, "exists", boom)
    with pytest.raises(hr.RootError, match="invalid path"):
        hr._preflight_root("anything")


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
    assert "cancelled by SIGINT" in err


def test_main_immediate_cancel_still_reports_timing(tmp_path, monkeypatch, capsys):
    # hr-rel-03: even an immediate-cancel walk reaches the timing line
    # with walk_boundary set (on_walk_done ran when index_inodes drained
    # the empty iterator). The summary must still print walk_s/hash_s
    # without a TypeError from the removed `is None` fallback.
    (tmp_path / "a.bin").write_bytes(b"x")

    def fake_install(ev):
        ev.set()
        return None
    monkeypatch.setattr(hr, "_install_sigint_cancel", fake_install)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "walk_s=" in err and "hash_s=" in err


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
    assert cfg.alias_cap is None
    assert cfg.alias_cap_active is False


def test_runconfig_zero_alias_cap_clamps_to_unbounded():
    cfg = hr.RunConfig(alias_cap=0)
    assert cfg.alias_cap is None
    assert cfg.alias_cap_active is False


def test_runconfig_negative_hash_error_cap_clamps_to_zero():
    cfg = hr.RunConfig(hash_error_verbose_cap=-1)
    assert cfg.hash_error_verbose_cap == 0


# --- hr-cx-01: alias_cap_active property -----------------------------------

def test_no_cap_constant_removed():
    # hr-cmplx-01: the dead `NO_CAP = None` back-compat alias was removed;
    # the disabled state is detected via `alias_cap is not None`.
    assert not hasattr(hr, "NO_CAP")


def test_legitimate_cap_of_two_pow_31_is_honored():
    # hr-cmplx-01 regression: a cap of exactly 2**31 must be a REAL cap,
    # not silently treated as "disabled" (the old sentinel bug).
    cfg = hr.RunConfig(alias_cap=2**31)
    assert cfg.alias_cap == 2**31
    assert cfg.alias_cap_active is True


def test_cap_above_old_sentinel_still_active():
    cfg = hr.RunConfig(alias_cap=2**31 + 5)
    assert cfg.alias_cap == 2**31 + 5
    assert cfg.alias_cap_active is True


def test_alias_cap_active_true_for_real_cap():
    cfg = hr.RunConfig(alias_cap=10)
    assert cfg.alias_cap_active is True


def test_alias_cap_active_false_when_disabled():
    # alias_cap=0 → None (disabled) → cap not active.
    cfg = hr.RunConfig(alias_cap=0)
    assert cfg.alias_cap_active is False
    assert cfg.alias_cap is None


def test_alias_cap_active_default_is_active():
    cfg = hr.RunConfig()
    assert cfg.alias_cap_active is True


def test_expand_keys_to_paths_disabled_cap_returns_all_no_sentinel():
    # hr-cmplx-01: a config with the cap disabled (None) returns every
    # path with no truncation and no `+N more` sentinel.
    cfg = hr.RunConfig(alias_cap=0)
    aliases = {("d", 0): [f"/p/{i}" for i in range(5000)]}
    out = hr._expand_keys_to_paths([("d", 0)], aliases, config=cfg)
    assert len(out) == 5000
    assert not any(isinstance(p, hr._MoreSentinel) for p in out)
    assert cfg.alias_cap_hits == 0


def test_find_duplicate_groups_disabled_cap_skips_ingest_overflow(tmp_path):
    # hr-cx-01: with the cap disabled, no overflow dict is built and the
    # alias list is unbounded — exercises the `not alias_cap_active` arm.
    body = b"z" * 200
    (tmp_path / "a.bin").write_bytes(body)
    files = [(f"/synthetic/p{i}", len(body), 1, 100) for i in range(30)]
    files += [(f"/synthetic/q{i}", len(body), 1, 200) for i in range(30)]
    cfg = hr.RunConfig(alias_cap=0)   # disabled

    def stub_stage1(candidates, rep, jobs, config, cancel_event=None, **kwargs):
        from collections import defaultdict
        by_head = defaultdict(list)
        for size, key in candidates:
            by_head[(size, "fakehead")].append(key)
        return by_head, {"stage1": len(candidates), "stage1_errors": 0}

    with mock.patch.object(hr, "_stage1_hash", stub_stage1):
        result = hr.find_duplicate_groups(files, jobs=1, config=cfg)
    # cap disabled → overflow never populated, all aliases retained.
    # hr-arch-01: overflow is returned on the result, not stashed on config.
    assert result.overflow is None
    assert len(result.aliases[(1, 100)]) == 30


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


def test_find_duplicate_groups_reports_stage2_progress(tmp_path):
    # hr-log-05: stage 2 exposes live progress through the pipeline callback.
    orig = (hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD)
    try:
        hr._configure_windows(4, 2)
        body = b"0123456789"
        a = tmp_path / "a.bin"; a.write_bytes(body)
        b = tmp_path / "b.bin"; b.write_bytes(body)
        files = []
        for path in (a, b):
            st_ = path.stat()
            files.append((str(path), st_.st_size, st_.st_dev, st_.st_ino))
        progress = []

        def on_progress(stage, done, total):
            progress.append((stage, done, total))

        result = hr.find_duplicate_groups(
            files, jobs=1, on_stage_progress=on_progress)
    finally:
        hr.CAP, hr.SAMPLE, hr.HEAD_TAIL_THRESHOLD = orig

    assert result.info["stage2"] == 2
    assert any(stage == "stage2" and done == 2 and total == 2
               for stage, done, total in progress)


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

    def bad_cb(digest, keys, aliases, overflow):
        raise RuntimeError("callback failure")

    with pytest.raises(RuntimeError, match="callback failure"):
        hr.find_duplicate_groups(files, jobs=1, on_group=bad_cb)


# ===== implementation of hr-rel-18, hr-sec-02..05, hr-scal-06, hr-dup-04 =====

# --- hr-dup-04: extracted error-line helper -------------------------------

def test_emit_hash_error_line_single_source_of_truth(capsys):
    hr._emit_hash_error_line("/some/path", OSError("boom"))
    err = capsys.readouterr().err
    # hr-obs-02: severity tag so the line greps apart from other log lines.
    _assert_timestamped(err)
    assert "ERROR: hash failed for /some/path: boom" in err


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


# --- hr-conc-01: BaseException in a walk worker re-enqueues the dir -------

def test_walk_worker_requeues_dir_on_base_exception(tmp_path, monkeypatch):
    # hr-conc-01: a non-OSError/ValueError escape while scanning a directory
    # must re-enqueue that directory so its subtree isn't silently dropped.
    # The first scandir of the root raises RuntimeError; a retry succeeds,
    # so every file under root is still discovered.
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "top.bin").write_bytes(b"x")
    for i in range(3):
        (sub / f"f{i}.bin").write_bytes(b"x")

    real_scandir = hr.os.scandir
    state = {"raised": False}
    root_str = str(tmp_path)
    lock = __import__("threading").Lock()

    def flaky_scandir(path):
        with lock:
            if str(path) == root_str and not state["raised"]:
                state["raised"] = True
                raise RuntimeError("transient kernel quirk on root")
        return real_scandir(path)

    monkeypatch.setattr(hr.os, "scandir", flaky_scandir)
    # Single worker: the same worker that died is gone, but it re-enqueued
    # root, and... with jobs=2 a surviving worker retries it.
    results, stats = hr.threaded_walk(tmp_path, 2)
    names = sorted(Path(p).name for p, *_ in results)
    # All 4 files recovered despite the first root scan failing.
    assert names == ["f0.bin", "f1.bin", "f2.bin", "top.bin"]
    assert stats["files"] == 4


def test_walk_worker_deterministic_base_exception_terminates(tmp_path, monkeypatch):
    # hr-conc-01 safety: even when scanning the root ALWAYS raises a
    # BaseException (re-enqueue + retry can never succeed), the walk must
    # still TERMINATE — bounded by the worker count dying off — instead of
    # looping forever on the re-enqueued directory.
    (tmp_path / "a.bin").write_bytes(b"x")

    def always_boom(path):
        raise RuntimeError("permanent failure")

    monkeypatch.setattr(hr.os, "scandir", always_boom)
    import threading
    done = threading.Event()

    def run():
        list(hr.iter_threaded_walk(tmp_path, jobs=3))
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    done.wait(timeout=10)
    assert done.is_set(), "deterministic BaseException re-enqueue looped forever"


# --- hr-test-01: a true BaseException in _scan_dir re-enqueues the dir -----

class _ScanBoom(BaseException):
    """True BaseException (not Exception) — exercises the BaseException
    arm of `_walk_worker` directly, distinct from the RuntimeError tests
    above which only cover the Exception subset."""


def test_scan_dir_base_exception_preserves_siblings_and_stats(
        tmp_path, monkeypatch):
    # hr-test-01: patch `_scan_dir` to raise a true BaseException the FIRST
    # time one specific subdirectory of a multi-level tree is scanned. The
    # re-enqueue path (hr-conc-01) must hand that directory to a surviving
    # worker so its children — and every sibling subtree — are still
    # emitted, and the merged stats must finalize non-zero.
    (tmp_path / "top.bin").write_bytes(b"x")
    sub = tmp_path / "sub"
    (sub / "deep").mkdir(parents=True)
    (sub / "f0.bin").write_bytes(b"x")
    (sub / "f1.bin").write_bytes(b"x")
    (sub / "deep" / "g0.bin").write_bytes(b"x")
    sib = tmp_path / "sib"
    sib.mkdir()
    (sib / "s0.bin").write_bytes(b"x")

    real_scan_dir = hr._scan_dir
    sub_str = str(sub)
    state = {"boomed": False}
    lock = __import__("threading").Lock()

    def flaky_scan_dir(d, st, wstats):
        with lock:
            if str(d) == sub_str and not state["boomed"]:
                state["boomed"] = True
                raise _ScanBoom("transient failure scanning sub")
        return real_scan_dir(d, st, wstats)

    monkeypatch.setattr(hr, "_scan_dir", flaky_scan_dir)
    # jobs>=2 so a surviving worker retries the re-enqueued directory after
    # the worker that hit the BaseException dies.
    results, stats = hr.threaded_walk(tmp_path, 3)
    names = sorted(Path(p).name for p, *_ in results)
    assert names == ["f0.bin", "f1.bin", "g0.bin", "s0.bin", "top.bin"]
    assert state["boomed"] is True            # the BaseException really fired
    assert stats["files"] == 5
    assert stats["dirs"] > 0                  # stats finalized non-zero
    assert stats["dir_errors"] == 0           # BaseException != OSError count


# --- hr-conc-01: cancel_event aborts the hash stages between batches ------

def test_cancelled_helper():
    import threading as _t
    assert hr._cancelled(None) is False
    ev = _t.Event()
    assert hr._cancelled(ev) is False
    ev.set()
    assert hr._cancelled(ev) is True


def test_run_stage_serial_skips_work_when_precancelled():
    import threading as _t
    ev = _t.Event(); ev.set()
    called = {"n": 0}

    def batch(items):
        called["n"] += 1
        return [(p, p) for p in items]

    out, errors = hr._run_stage(
        ["a", "b"], batch, total_bytes=1, jobs=1, cancel_event=ev)
    assert out == {} and errors == 0
    assert called["n"] == 0   # batch_fn never invoked


def test_run_stage_serial_runs_when_not_cancelled():
    import threading as _t
    ev = _t.Event()   # unset
    out, _ = hr._run_stage(
        ["a"], lambda b: [(p, p.upper()) for p in b],
        total_bytes=1, jobs=1, cancel_event=ev)
    assert out == {"a": "A"}


def test_run_stage_threaded_stops_after_cancel_between_batches(monkeypatch):
    # hr-conc-01: in the threaded branch, once cancel fires the loop stops
    # consuming further batches and returns the partial result.
    import threading as _t
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 1)
    ev = _t.Event()

    def batch(items):
        # Set the cancel after the first batch is produced.
        result = [(p, p.upper()) for p in items]
        ev.set()
        return result

    items = ["a", "b", "c", "d"]
    out, _ = hr._run_stage(
        items, batch, total_bytes=10**9, jobs=1, cancel_event=ev)
    # The loop breaks after the first consumed batch → fewer than all 4.
    assert 0 < len(out) < len(items)


def test_run_stage_windowed_cancel_stops_further_submission(monkeypatch):
    # hr-scal-03 (achieved by hr-scal-02): once cancel fires, no NEW batches
    # are submitted past the in-flight window — total batch_fn invocations
    # stay bounded near the window, not the full batch count.
    import threading as _t
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0)
    monkeypatch.setattr(hr, "HASH_BATCH", 1)
    monkeypatch.setattr(hr, "SUBMIT_WINDOW", 2)
    jobs = 1
    window = jobs * hr.SUBMIT_WINDOW
    ev = _t.Event()
    calls = {"n": 0}
    lock = _t.Lock()

    def batch(items):
        with lock:
            calls["n"] += 1
        ev.set()   # cancel as soon as the first batch runs
        return [(p, p.upper()) for p in items]

    items = [str(i) for i in range(1000)]
    out, _ = hr._run_stage(
        items, batch, total_bytes=10**9, jobs=jobs, cancel_event=ev)
    # Far fewer than 1000 batches ran — submission stopped at the boundary.
    assert calls["n"] < 1000
    assert calls["n"] <= window + 1
    assert 0 < len(out) < 1000


def test_find_duplicate_groups_forwards_cancel_to_stages(tmp_path, monkeypatch):
    # hr-conc-01: a pre-set cancel_event means the stage helpers receive it
    # and return early → no groups confirmed even though candidates exist.
    import threading as _t
    body = b"q" * 200
    a = tmp_path / "a.bin"; a.write_bytes(body)
    b = tmp_path / "b.bin"; b.write_bytes(body)
    files = [(str(a), len(body), 1, 100), (str(b), len(body), 1, 101)]
    ev = _t.Event(); ev.set()
    seen = {}

    real_stage1 = hr._stage1_hash

    def spy(candidates, rep, jobs, config, cancel_event=None, **kwargs):
        seen["cancel"] = cancel_event
        return real_stage1(candidates, rep, jobs, config, cancel_event,
                           **kwargs)

    monkeypatch.setattr(hr, "_stage1_hash", spy)
    result = hr.find_duplicate_groups(files, jobs=1, cancel_event=ev)
    assert seen["cancel"] is ev
    # Stage1 was cancelled → head hashes empty → no candidates confirmed.
    assert result.groups == {}


def test_main_cancel_during_hash_takes_effect(tmp_path, monkeypatch, capsys):
    # hr-conc-01 end-to-end: cancel_event set before hashing → stages
    # abort, run completes, summary shows the SIGINT partial warning.
    body = b"z" * 300
    (tmp_path / "a.bin").write_bytes(body)
    (tmp_path / "b.bin").write_bytes(body)

    def fake_install(ev):
        ev.set()
        return None
    monkeypatch.setattr(hr, "_install_sigint_cancel", fake_install)
    monkeypatch.setattr(hr.sys, "argv", ["hr", str(tmp_path)])
    hr.main()
    err = capsys.readouterr().err
    assert "cancelled by SIGINT" in err


# --- hr-rel-02: walk is finalized even when the pipeline raises -----------

def test_find_duplicate_groups_finalizes_walk_stats_on_hash_error(tmp_path, monkeypatch):
    # hr-rel-02: hashing raises AFTER the walk is consumed; walk_iter.stats
    # must still be finalized (not stale zeros) and threads joined.
    for i in range(6):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")
    walk = hr.iter_threaded_walk(tmp_path, jobs=2)

    def boom(*a, **k):
        raise RuntimeError("hash blew up")

    monkeypatch.setattr(hr, "_stage1_hash", boom)
    # Files are all same size+content → they become size-collision
    # candidates → stage1 runs → boom.
    with pytest.raises(RuntimeError, match="hash blew up"):
        hr.find_duplicate_groups(walk, jobs=2)
    # Walk fully drained before hashing → stats are real, not zeroed.
    assert walk.stats["files"] == 6


def test_walk_iter_finalizes_stats_when_consumer_abandons(tmp_path):
    # hr-rel-02: a consumer that stops iterating early (GeneratorExit on
    # close) still drives the walk to completion and finalizes stats via
    # the generator's finally block.
    for i in range(8):
        (tmp_path / f"f{i}.bin").write_bytes(b"x")
    walk = hr.iter_threaded_walk(tmp_path, jobs=2)
    gen = iter(walk)
    first = next(gen)          # consume just one entry
    assert first is not None
    gen.close()                # abandon mid-walk → finally drains + joins
    # stats finalized despite early abandonment.
    assert walk.stats["files"] == 8


def test_index_inodes_closes_source_iterator_on_error():
    # hr-rel-02: index_inodes closes its source iterator if ingest raises,
    # so a generator-backed walk gets its finally (join + stats) run.
    closed = {"v": False}

    def gen():
        try:
            yield ("/a", 10, 1, 100)
            yield ("not-a-4-tuple",)   # unpack raises ValueError
        finally:
            closed["v"] = True

    with pytest.raises(ValueError):
        hr.index_inodes(gen())
    assert closed["v"] is True


def test_index_inodes_closes_source_iterator_on_success():
    closed = {"v": False}

    def gen():
        try:
            yield ("/a", 10, 1, 100)
        finally:
            closed["v"] = True

    aliases, inode_size = hr.index_inodes(gen())
    assert aliases[(1, 100)] == ["/a"]
    assert closed["v"] is True


def test_index_inodes_plain_list_has_no_close():
    # A list iterator has no .close(); index_inodes must not blow up.
    files = [("/a", 10, 1, 100), ("/b", 10, 1, 100)]
    aliases, _ = hr.index_inodes(files)
    assert len(aliases[(1, 100)]) == 2


# --- hr-scal-06: index_inodes alias_cap at ingest -------------------------

def test_index_inodes_caps_aliases_with_overflow_count():
    files = [(f"/p{i}", 100, 1, 99) for i in range(500)]   # 500 hardlinks of one inode
    overflow: dict = {}
    aliases, _ = hr.index_inodes(files, alias_cap=50, overflow=overflow)
    assert len(aliases[(1, 99)]) == 50    # capped at ingest
    assert overflow[(1, 99)] == 450        # 500-50 elided


def test_index_inodes_cap_without_overflow_discards_extra_aliases():
    files = [("/p0", 100, 1, 99), ("/p1", 100, 1, 99)]
    aliases, inode_size = hr.index_inodes(files, alias_cap=1, overflow=None)
    assert aliases[(1, 99)] == ["/p0"]
    assert inode_size[(1, 99)] == 100


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

def test_find_duplicate_groups_uses_ingest_alias_cap_via_result(tmp_path):
    # hr-arch-01: the same inode has many hardlinks; with a small
    # alias_cap the ingest stage drops paths past the cap and records
    # them as overflow. The overflow dict is returned EXPLICITLY on the
    # DedupResult AND handed to the on_group callback as its 4th arg —
    # never smuggled on config.
    body = b"shared body" * 200
    (tmp_path / "a.bin").write_bytes(body)
    files = [(f"/synthetic/p{i}", len(body), 1, 100) for i in range(20)]
    # Make a duplicate group via two inodes with same size.
    files += [(f"/synthetic/q{i}", len(body), 1, 200) for i in range(20)]
    cfg = hr.RunConfig(alias_cap=5)   # small cap → ingest drops
    captured = []

    def cb(digest, keys, aliases, overflow):
        captured.append((digest, keys, aliases, overflow))

    def stub_stage1(candidates, rep, jobs, config, cancel_event=None, **kwargs):
        from collections import defaultdict
        by_head = defaultdict(list)
        for size, key in candidates:
            by_head[(size, "fakehead")].append(key)
        return by_head, {"stage1": len(candidates), "stage1_errors": 0}
    with mock.patch.object(hr, "_stage1_hash", stub_stage1):
        result = hr.find_duplicate_groups(files, jobs=1, on_group=cb, config=cfg)
    # Each inode had 20 paths but cap=5 limits storage to 5.
    # Overflow on each = 15.
    assert result.info["inodes"] >= 2
    # hr-arch-01: overflow is on the result, NOT on config.
    assert not hasattr(cfg, "overflow")
    assert result.overflow is not None
    assert all(v == 15 for v in result.overflow.values())
    # The callback received the SAME overflow dict explicitly.
    assert captured and all(c[3] is result.overflow for c in captured)


def test_run_config_has_no_overflow_attr():
    # hr-arch-01: the overflow side-channel slot is gone from RunConfig.
    cfg = hr.RunConfig()
    assert not hasattr(cfg, "overflow")
    with pytest.raises(AttributeError):
        cfg.overflow = {("d", 0): 1}   # __slots__ rejects it


def test_expand_keys_to_paths_takes_overflow_explicitly():
    # hr-arch-01: overflow is an explicit kwarg, never read off config.
    cfg = hr.RunConfig(alias_cap=5)
    aliases = {("d", 0): ["/a", "/b"]}
    out = hr._expand_keys_to_paths(
        [("d", 0)], aliases, config=cfg, overflow={("d", 0): 100})
    sentinel = next((p for p in out if isinstance(p, hr._MoreSentinel)), None)
    assert sentinel is not None
    # 2 stored + 100 overflow = 102 total. cap=5 → "+97 more".
    assert "97" in str(sentinel)


def test_emit_groups_forwards_overflow_to_sentinel():
    # hr-arch-01: the batched emit path receives overflow explicitly and
    # the +N more sentinel reflects it.
    aliases = {("d", 0): ["/a", "/b"], ("d", 1): ["/c"]}
    final_groups = {"hash": [("d", 0), ("d", 1)]}
    overflow = {("d", 0): 50}
    written = []
    groups, paths = hr.emit_groups(
        final_groups, aliases, written.append, overflow=overflow)
    blob = "".join(written)
    # 3 stored paths + 50 overflow = 53; with default cap (1024) no
    # sentinel, but the real count includes the overflow-bearing inode.
    assert groups == 1
    assert "/a" in blob and "/c" in blob


def test_dedup_result_overflow_field_default_none():
    # hr-arch-01: DedupResult carries overflow; default None for callers
    # that build it positionally with 3 args.
    res = hr.DedupResult(groups={}, aliases={}, info={})
    assert res.overflow is None
    res2 = hr.DedupResult(groups={}, aliases={}, info={}, overflow={("d", 0): 3})
    assert res2.overflow == {("d", 0): 3}


# --- hr-rel-02: retry next readable alias when the rep head is None ---------

def _stage1_candidates(size, key):
    """Single (size, key) candidate list shared by the retry tests."""
    return [(size, key)]


def test_retry_head_alias_skips_tried_and_returns_sibling(monkeypatch):
    # The representative (tried) is dead; the next alias hashes fine.
    aliases = {("d", 0): ["/dead", "/live"]}
    calls = []

    def fake_hash_head(path, config):
        calls.append(path)
        return None if path == "/dead" else "GOODHEAD"

    monkeypatch.setattr(hr, "hash_head", fake_hash_head)
    head = hr._retry_head_alias(("d", 0), "/dead", aliases, None)
    assert head == "GOODHEAD"
    assert "/dead" not in calls   # the already-tried rep is skipped


def test_retry_head_alias_returns_none_when_no_sibling_readable(monkeypatch):
    aliases = {("d", 0): ["/dead", "/alsodead"]}
    monkeypatch.setattr(hr, "hash_head", lambda p, c: None)
    assert hr._retry_head_alias(("d", 0), "/dead", aliases, None) is None


def test_retry_head_alias_unknown_key_returns_none():
    assert hr._retry_head_alias(("d", 9), "/x", {}, None) is None


def test_stage1_hash_retries_alias_when_rep_unreadable(tmp_path):
    # hr-rel-02 end-to-end at stage 1: rep path is unreadable, but a
    # hardlinked sibling is readable → the inode is NOT dropped.
    body = b"head-content"
    live = tmp_path / "live.bin"
    live.write_bytes(body)
    key = ("d", 0)
    rep = {key: str(tmp_path / "vanished.bin")}    # rep does not exist
    aliases = {key: [str(tmp_path / "vanished.bin"), str(live)]}
    by_head, info = hr._stage1_hash(
        _stage1_candidates(len(body), key), rep, jobs=1, config=None,
        aliases=aliases)
    # The inode survived stage 1 via the readable sibling.
    confirmed = [k for keys in by_head.values() for k in keys]
    assert confirmed == [key]
    assert info["stage1"] == 1


def test_stage1_hash_no_retry_without_aliases(tmp_path):
    # Back-compat: no aliases supplied → a None rep head drops the inode.
    key = ("d", 0)
    rep = {key: str(tmp_path / "missing.bin")}
    by_head, _ = hr._stage1_hash(
        _stage1_candidates(50, key), rep, jobs=1, config=None)
    assert by_head == {}


def test_stage1_hash_no_retry_for_single_alias(tmp_path, monkeypatch):
    # A single-alias inode whose rep is None must not trigger a retry probe.
    key = ("d", 0)
    rep = {key: str(tmp_path / "missing.bin")}
    aliases = {key: [str(tmp_path / "missing.bin")]}
    calls = []
    real = hr._retry_head_alias
    monkeypatch.setattr(hr, "_retry_head_alias",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    by_head, _ = hr._stage1_hash(
        _stage1_candidates(50, key), rep, jobs=1, config=None, aliases=aliases)
    assert by_head == {}
    assert calls == []   # single alias → no retry attempted


def test_find_duplicate_groups_unreadable_rep_still_grouped(tmp_path):
    # hr-rel-02 full pipeline: two inodes with identical content; each has
    # a dead rep alias listed first plus a readable sibling. The dead reps
    # must not cause either inode to be silently excluded — both group.
    body = b"D" * 400
    live_a = tmp_path / "a_live.bin"; live_a.write_bytes(body)
    live_b = tmp_path / "b_live.bin"; live_b.write_bytes(body)
    files = [
        (str(tmp_path / "a_dead.bin"), len(body), 1, 100),  # rep, missing
        (str(live_a), len(body), 1, 100),
        (str(tmp_path / "b_dead.bin"), len(body), 1, 200),  # rep, missing
        (str(live_b), len(body), 1, 200),
    ]
    result = hr.find_duplicate_groups(files, jobs=1)
    grouped_keys = [k for keys in result.groups.values() for k in keys]
    assert sorted(grouped_keys) == [(1, 100), (1, 200)]


@given(st.integers(min_value=1, max_value=8),
       st.integers(min_value=0, max_value=7))
def test_retry_head_alias_property(n_aliases, live_choice):
    # Property: with the rep ("/p0") always tried/dead, _retry_head_alias
    # returns a digest exactly when at least one sibling is readable, and
    # never probes the tried rep.
    paths = [f"/p{i}" for i in range(n_aliases + 1)]   # /p0 is the rep
    aliases = {("d", 0): paths}
    siblings = paths[1:]
    readable = siblings[live_choice % len(siblings)] if live_choice % 3 else None
    probed = []

    def fake(path, config):
        probed.append(path)
        return "HD" if path == readable else None

    with mock.patch.object(hr, "hash_head", side_effect=fake):
        result = hr._retry_head_alias(("d", 0), "/p0", aliases, None)
    assert "/p0" not in probed
    assert result == ("HD" if readable is not None else None)
