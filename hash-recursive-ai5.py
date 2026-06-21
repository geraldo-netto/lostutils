#!/usr/bin/env python3
"""
Recursive duplicate finder. Same output shape as the legacy ai4 script:
one line per file in any duplicate group, "<digest> <path>".

Pipeline:
  1. Threaded walk — collect (path, size, dev, ino) for every regular file.
  2. Hardlink dedup — paths sharing (dev, ino) are aliases of one inode;
     hash the inode once, emit every alias at the end.
  3. Size pre-filter — inodes whose size is unique can't have duplicates.
  4. Stage 1 hash — BLAKE3 of the first 1 MiB of each surviving inode.
     Group by (size, head_digest).
  5. Stage 2 hash — for groups with size > 2 MiB and ≥2 members, also
     hash the last MiB plus two 64 KiB samples at size/3 and 2*size/3.
     Files that sample-match are reported as duplicates; the rest fall
     out (head matched but content differed past the head).

Why head + tail + middle samples? A first-MiB-only hash treats files as
duplicate when they only share a container header — common false positive
for MKV/MP4 files with the same intro, ISOs of related distros, tar
backups of similar trees, DB dumps with the same schema. Head+tail+samples
makes accidental collision essentially impossible for real-world content
while staying bounded (max ~2.13 MiB read per file, regardless of size).
"""
from __future__ import annotations

import argparse
import errno
import os
import queue
import signal
import stat
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import blake3

CAP = 1024 * 1024              # 1 MiB head/tail window
SAMPLE = 64 * 1024             # mid-file sample window
HEAD_TAIL_THRESHOLD = CAP      # at-or-below this, the head IS the full file
# (CAP < size <= 2*CAP MUST go through stage 2; otherwise files that share
# their first CAP bytes but differ in the tail would be reported as duplicates
# falsely — hr-rel-03.)
HASH_BATCH = 64
# BLAKE3 is ~3 GB/s/thread on modern x86. Below this much candidate
# work, ThreadPoolExecutor setup + per-task overhead exceeds the gain.
THREAD_THRESHOLD_BYTES = 32 * 1024 * 1024
# hr-conc-01: each worker accumulates this many entries before grabbing
# results_lock. The walk previously flushed once per directory — on trees
# where most dirs hold one or two files, that meant per-entry lock contention
# at 8+ workers. 1024 keeps the flush cheap (one small extend per dir on a
# typical tree) without unbounded per-worker memory.
WALK_FLUSH_THRESHOLD = 1024

DEFAULT_ALIAS_CAP = 1024
DEFAULT_HASH_ERROR_VERBOSE_CAP = 20
# hr-cmplx-01: the alias cap is stored as Optional[int] — `None` means
# "no cap". `NO_CAP` is retained only as a back-compat alias for callers
# that imported the old sentinel; it is no longer used to detect the
# disabled state (that overloaded a legitimate cap of exactly 2**31).
NO_CAP = None
# hr-rel-02: cap the os.access probe in `_readable_rep` — a hardlink-heavy
# inode with 100k aliases otherwise costs 100k stat syscalls just to pick
# a representative. The first few aliases are almost always representative
# of readability; fall back to the first alias if none in the window passes.
_REP_PROBE_LIMIT = 8
# hr-conc-02: errno values that mean "the file recorded at walk time is
# gone" — ENOENT (deleted) and ESTALE (NFS handle invalidated). Raised
# both on `os.open` (as FileNotFoundError, errno ENOENT) and mid-read
# when a sibling thread / another process removes the file after open.
# These are benign racy-delete skips, counted apart from real I/O errors.
_VANISHED_ERRNOS = frozenset({errno.ENOENT, errno.ESTALE})


class RunConfig:
    """Per-run configuration + counters (hr-arch-05 / hr-decoup-02).

    Replaces the module-level mutable globals that used to hold
    `ALIAS_CAP`, the cap-hit counter, and the per-file hash-error
    bookkeeping. One instance is created in :func:`main` and threaded
    through the dedup pipeline and emit layer. Tests construct their
    own config — no cross-test reset dance, no order-dependent state.
    """

    __slots__ = (
        "alias_cap",
        "hash_error_verbose_cap",
        "alias_cap_hits",
        "hash_error_logged",
        "hash_error_suppressed",
        # hr-obs-01: count benign vanished-file (ENOENT) skips separately
        # from real EACCES/EIO/... hash errors. The per-stage `*_errors`
        # totals still include these (back-compat `hash_errors`), but the
        # summary subtracts this counter so the operator can tell a racy
        # delete apart from a permission / I/O failure worth investigating.
        "hash_skipped_vanished",
        # hr-conc-06: lock for the cross-thread `+= 1` counters above.
        # CPython's GIL makes the bytecode effectively atomic today but
        # py3.13 free-threaded builds drop the GIL and the increments
        # race. The lock is held only for the increment, never around
        # I/O, so contention is negligible.
        "_counter_lock",
    )

    def __init__(
        self,
        alias_cap: int = DEFAULT_ALIAS_CAP,
        hash_error_verbose_cap: int = DEFAULT_HASH_ERROR_VERBOSE_CAP,
    ) -> None:
        # hr-cmplx-01: `alias_cap <= 0` means "no cap", stored as None so
        # a legitimate positive cap (including exactly 2**31) is honored
        # verbatim instead of colliding with a magic sentinel.
        self.alias_cap = alias_cap if alias_cap > 0 else None
        self.hash_error_verbose_cap = max(0, hash_error_verbose_cap)
        self.alias_cap_hits = 0
        self.hash_error_logged = 0
        self.hash_error_suppressed = 0
        self.hash_skipped_vanished = 0
        self._counter_lock = threading.Lock()

    @property
    def alias_cap_active(self) -> bool:
        """True when the alias cap is a real bound, False when disabled
        (hr-cmplx-01). ``alias_cap`` is ``None`` exactly when disabled, so
        the check is a single ``is not None`` — no magic value, and a
        legitimate cap of any positive size (including 2**31) is honored."""
        return self.alias_cap is not None


class RootError(Exception):
    """Raised by :func:`_preflight_root` for a bad root path (hr-rel-17).

    Callers catch this to render a typed error; :func:`main` translates
    it to ``sys.exit(2)`` at the CLI boundary only. Library / test
    consumers no longer get an unrecoverable ``SystemExit`` when they
    call the preflight helper to validate a user-supplied directory."""


def threaded_walk(root, jobs, cancel_event=None):
    """Back-compat wrapper: drain :func:`iter_threaded_walk` into a list
    and return ``(entries, stats)`` (hr-scal-05).

    Prefer the iterator form when memory matters — this wrapper still
    materialises every entry into a list.

    Returns
    -------
    results : list of ``(path, size, dev, ino)`` tuples.
    stats : dict with keys ``dirs``, ``files``, ``dir_errors``,
        ``entry_errors``.
    """
    it = iter_threaded_walk(root, jobs, cancel_event=cancel_event)
    results = list(it)
    return results, it.stats


def iter_threaded_walk(root, jobs, cancel_event=None):
    """Stream entries from a threaded walk (hr-scal-05).

    Returns a :class:`_WalkIter`; iterate it to receive
    ``(path, size, dev, ino)`` tuples as workers produce them. Read
    ``.stats`` AFTER iteration completes for the
    ``{dirs, files, dir_errors, entry_errors}`` totals.

    Unlike :func:`threaded_walk`, the full file list is never
    materialised — RAM tracks "currently buffered" entries, not "every
    file ever seen". On a 50M-file tree this drops peak RSS from
    multi-GB to whatever the consumer keeps in flight."""
    return _WalkIter(root, jobs, cancel_event)


class _WalkState(NamedTuple):
    """Shared mutable state passed explicitly to :func:`_walk_worker`
    (hr-cx-02).

    Promoting the worker out of :meth:`_WalkIter.__iter__` means its
    closed-over queues, lock and counters become named fields here
    instead of free variables — the worker is now testable / readable in
    isolation. ``inflight`` is a one-element list used as a mutable int
    box guarded by ``lock``."""

    pending: queue.SimpleQueue
    out_q: queue.SimpleQueue
    lock: threading.Lock
    inflight: list
    per_worker_stats: list
    jobs: int
    sentinel: object
    cancel_event: object


def _walk_worker(idx, state: "_WalkState") -> None:
    """One walk worker: pop a directory, scandir it, push subdirs back
    and regular-file entries to the output queue (hr-cx-02).

    Behaviour-identical to the former nested closure — same cooperative
    cancel (hr-conc-02), single-lstat classification (hr-perf-02),
    per-entry error counting (hr-sec-03), and BaseException safety net
    (hr-rel-18)."""
    wstats = state.per_worker_stats[idx]
    while True:
        d = state.pending.get()
        if d is state.sentinel:
            return
        scanned = True
        try:
            if state.cancel_event is not None and state.cancel_event.is_set():
                scanned = False
                continue
            _scan_dir(d, state, wstats)
        except (OSError, ValueError):
            wstats["dir_errors"] += 1
            scanned = False
        except BaseException:
            # hr-rel-18: any non-OSError/ValueError escape would leave the
            # consumer hanging on out_q.get(); count + re-raise so the
            # finally still decrements inflight and the coordinator joins.
            wstats["dir_errors"] += 1
            scanned = False
            raise
        finally:
            if scanned:
                wstats["dirs"] += 1
            with state.lock:
                state.inflight[0] -= 1
                if state.inflight[0] == 0:
                    for _ in range(state.jobs):
                        state.pending.put(state.sentinel)


def _scan_dir(d, state: "_WalkState", wstats) -> None:
    """Scan one directory: enqueue subdirs, emit regular files
    (hr-cx-02). Per-entry errors are counted, never raised."""
    with os.scandir(d) as it:
        for e in it:
            try:
                # hr-perf-02: single lstat covers classification AND
                # (size, dev, ino).
                st = e.stat(follow_symlinks=False)
                mode = st.st_mode
                if stat.S_ISDIR(mode):
                    with state.lock:
                        state.inflight[0] += 1
                    state.pending.put(e.path)
                elif stat.S_ISREG(mode):
                    state.out_q.put(
                        (e.path, st.st_size, st.st_dev, st.st_ino))
                    wstats["files"] += 1
            except (OSError, ValueError):
                # ValueError: NUL byte in entry name (hr-sec-03 family).
                wstats["entry_errors"] += 1


class _WalkIter:
    """Iterator implementation behind :func:`iter_threaded_walk`
    (hr-scal-05).

    Same cooperative cancel (hr-conc-02), same per-worker stats merge
    (hr-conc-01), same 0/negative-jobs guard (hr-rel-01) as the original
    `threaded_walk` — only the materialisation strategy changed. Workers
    push entries into an output queue; a coordinator thread joins the
    workers and posts an end-of-stream sentinel so the consumer can
    unblock from ``out_q.get()`` and then merge stats safely."""

    def __init__(self, root, jobs, cancel_event):
        self._root = root
        # jobs=0/negative would spawn no workers → silent empty result
        # (hr-rel-01).
        self._jobs = max(1, jobs)
        self._cancel = cancel_event
        self.stats = {"dirs": 0, "files": 0, "dir_errors": 0, "entry_errors": 0}

    def __iter__(self):
        jobs = self._jobs
        SENTINEL_OUT = object()    # signals end of output stream
        per_worker_stats: "list[dict]" = [
            {"dirs": 0, "files": 0, "dir_errors": 0, "entry_errors": 0}
            for _ in range(jobs)
        ]
        state = _WalkState(
            pending=queue.SimpleQueue(),
            out_q=queue.SimpleQueue(),
            lock=threading.Lock(),
            inflight=[1],
            per_worker_stats=per_worker_stats,
            jobs=jobs,
            sentinel=object(),     # signals worker termination
            cancel_event=self._cancel,
        )
        state.pending.put(self._root)

        threads = [threading.Thread(target=_walk_worker, args=(i, state),
                                    daemon=True)
                   for i in range(jobs)]
        for t in threads:
            t.start()

        # Coordinator joins workers and posts the end-of-stream sentinel
        # so the consumer can unblock from `out_q.get()` once production
        # is done.
        def coordinator():
            for t in threads:
                t.join()
            state.out_q.put(SENTINEL_OUT)

        coord_thread = threading.Thread(target=coordinator, daemon=True)
        coord_thread.start()

        stream_ended = False
        try:
            while True:
                item = state.out_q.get()
                if item is SENTINEL_OUT:
                    stream_ended = True
                    break
                yield item
        finally:
            # hr-rel-02: a consumer (e.g. `index_inodes` driving the hash
            # pipeline) that abandons this generator mid-walk — because
            # hashing raised — triggers GeneratorExit here. Without this
            # finally the coordinator was never joined, the daemon walk
            # workers kept scandir-ing the rest of the tree, and
            # `self.stats` stayed zeroed so the caller's except path read
            # stale counters. Drive the walk to completion (workers
            # self-terminate once `pending` drains, posting SENTINEL_OUT),
            # join the coordinator, then finalise stats — always.
            if not stream_ended:
                while state.out_q.get() is not SENTINEL_OUT:
                    pass
            coord_thread.join()
            for k in self.stats:
                self.stats[k] = sum(w[k] for w in per_worker_stats)


def _emit_hash_error_line(path, exc) -> None:
    """Single source of truth for the per-file hash error stderr format
    (hr-dup-04). Both branches of :func:`_log_hash_error` previously
    duplicated this f-string; a future change to the format (e.g. add a
    timestamp prefix or switch to structured JSON) now lands in one
    place."""
    print(f"[ai5] ERROR: hash failed for {path}: {exc}", file=sys.stderr)


def _log_hash_error(path, exc, config):
    """Bounded stderr logging for per-file hash errors (hr-obs-03).

    Up to ``config.hash_error_verbose_cap`` errors print verbosely; the
    rest only tick ``hash_error_suppressed``. :func:`main` prints a
    single summary line at end-of-run when any were suppressed.

    Without a config (legacy callers, tests calling :func:`hash_head`
    directly) the original unbounded behaviour is preserved so existing
    assertions hold.

    Threading: invoked from inside ``ThreadPoolExecutor`` workers.
    hr-conc-06: counters guarded by ``config._counter_lock`` so a
    py3.13 free-threaded build (no GIL) sees deterministic counts
    instead of torn `+= 1` races. The lock spans the
    check-then-increment so we never print more than
    ``hash_error_verbose_cap`` lines."""
    if config is None:
        _emit_hash_error_line(path, exc)
        return
    with config._counter_lock:
        verbose = config.hash_error_logged < config.hash_error_verbose_cap
        if verbose:
            config.hash_error_logged += 1
        else:
            config.hash_error_suppressed += 1
    if verbose:
        _emit_hash_error_line(path, exc)


def _tick_vanished(config) -> None:
    """Count one benign vanished-file (ENOENT/ESTALE) skip (hr-conc-02).

    No-op without a config (legacy callers). hr-conc-06: guarded by
    ``config._counter_lock`` so the `+= 1` is deterministic on a
    free-threaded build."""
    if config is not None:
        with config._counter_lock:
            config.hash_skipped_vanished += 1


def _read_window_into(h, f, length: int, strict: bool) -> bool:
    """Read up to `length` bytes from `f` and feed them to hasher `h`
    (hr-rel-09).

    Loops on the read so a kernel that hands back a partial buffer
    (network mounts, signal-interrupted slow reads) eventually yields the
    full window instead of silently truncating the hash. `strict=True`
    means the caller expects exactly `length` bytes — if the file ends
    early, return False so the digest is discarded. `strict=False` (e.g.
    `hash_head` on a file shorter than `CAP`) treats early EOF as the
    natural end of the window."""
    remaining = length
    while remaining > 0:
        chunk = f.read(remaining)
        if not chunk:
            return not strict
        h.update(chunk)
        remaining -= len(chunk)
    return True


class FileWindow(NamedTuple):
    """A (offset, length, whence, strict) slice of a file (hr-arch-03 /
    hr-rel-09).

    `whence` follows the `os.SEEK_*` convention: SEEK_SET=0, SEEK_END=2.
    Negative `offset` paired with SEEK_END addresses bytes-from-EOF.

    `strict=False` (default — back-compat) means partial reads at EOF are
    accepted (e.g. `hash_head` of a 9-byte file with `length=65536` reads
    9 bytes and that's fine). `strict=True` means the window MUST yield
    exactly `length` bytes; if EOF arrives early, the digest is discarded
    (hr-rel-09) — set this on windows whose offsets are guaranteed by the
    caller's size gate."""

    offset: int
    length: int
    whence: int
    strict: bool = False


def _hash_file_windows(path, windows, config=None):
    """BLAKE3 of one or more (offset, length, whence) windows of a file
    (hr-dup-01). Returns the hex digest or None on any OSError. Shared
    scaffold for hash_head and hash_tail_and_samples.

    `windows` accepts either bare tuples (legacy callers, tests) or
    `FileWindow` named-tuples (hr-arch-03).

    `config` (hr-obs-03 / hr-arch-05): when supplied, OSError logging is
    routed through :func:`_log_hash_error` which caps stderr output at
    ``config.hash_error_verbose_cap`` lines per run. Without a config,
    legacy unbounded stderr logging is used.

    hr-rel-04 / hr-conc-02: a vanished-after-walk file is benign and
    emitted silently as None — counted on ``hash_skipped_vanished``. This
    covers ENOENT on the open (the file deleted between scandir and open)
    AND ENOENT/ESTALE raised mid-read (a sibling thread or another process
    deleted the file, or an NFS handle went stale, after the open
    succeeded) — the read-path case used to be miscounted as a real hash
    error. Every other OSError — EACCES, EIO, ENOSPC, EMFILE, … — is data
    loss in disguise and is surfaced (verbosely or via the suppression
    counter) so the user can investigate. The return value is None in all
    cases so the caller's "skip" semantics still hold.

    hr-sec-05: the open uses ``O_NOFOLLOW`` so an attacker who swapped
    the regular file recorded at walk time for a symlink between walk
    and hash gets ``ELOOP`` instead of having the symlink target
    silently hashed. Same skip semantics — None on any OSError — but
    the operator sees the ELOOP via the bounded stderr stream.

    hr-sec-06: ``O_CLOEXEC`` so any subprocess spawned from another
    thread (signal handler, daemon helper) does not inherit the hash
    fd."""
    # hr-arch-03: normalise every window to FileWindow at the boundary
    # once. `FileWindow(*window)` accepts both raw 3-tuples (strict
    # defaults to False) and 4-tuples, so the read loop never arity-sniffs.
    windows = [w if isinstance(w, FileWindow) else FileWindow(*w)
               for w in windows]
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb", buffering=0) as f:
            h = blake3.blake3()
            try:
                for window in windows:
                    f.seek(window.offset, window.whence)
                    if not _read_window_into(
                            h, f, window.length, window.strict):
                        # File shrank / partial read in a strict window
                        # — abort the hash so a truncated window can't
                        # silently produce a different digest from a
                        # full re-read (hr-rel-09).
                        return None
                return h.hexdigest()
            finally:
                # hr-rel-11: discard the hasher explicitly so partial
                # state from a mid-loop exception can't survive into
                # later code that re-hashes the same buffer ref.
                del h
    except OSError as exc:
        # hr-obs-01 / hr-conc-02: a vanished-after-walk file is a benign
        # skip, not a real error. ENOENT on `os.open` (FileNotFoundError)
        # AND ENOENT/ESTALE raised mid-read (a sibling thread or another
        # process deleted the file, or an NFS handle went stale, after the
        # open succeeded) all mean "the file is gone" — route every one of
        # them to the dedicated vanished counter so the summary separates
        # racy deletes from EACCES/EIO failures worth investigating.
        if exc.errno in _VANISHED_ERRNOS:
            _tick_vanished(config)
        else:
            _log_hash_error(path, exc, config)
        return None


def hash_head(path, config=None):
    """Stage 1: BLAKE3 of the first CAP bytes (or whole file if smaller).

    `config` is optional for back-compat with legacy callers (tests)."""
    return _hash_file_windows(path, [FileWindow(0, CAP, os.SEEK_SET)], config)


class SamplingStrategy:
    """Encapsulates the stage-2 window layout (hr-decoup-01).

    Subclasses override `windows(size)` to return the list of
    `FileWindow`s to hash. The default (`ThirdsStrategy`) picks the
    tail-CAP plus two SAMPLE-byte windows at `size // 3` and
    `2 * size // 3`. Splitting this out from `hash_tail_and_samples` lets
    tests plug a deterministic strategy (e.g. fixed offsets) and lets a
    future caller experiment with denser sampling for very large files
    without touching the dedup pipeline."""

    def windows(self, size: int) -> "list[FileWindow]":
        raise NotImplementedError  # pragma: no cover


class ThirdsStrategy(SamplingStrategy):
    """Default sampler: tail + middle thirds (hr-rel-05 / hr-rel-06).

    Offsets are clamped so the second SAMPLE window can never read past EOF
    (hr-rel-06): given the stage-2 gate of `size > 2*CAP`, the
    `floor(2*size/3)+SAMPLE` end-point already fits, but the clamp keeps the
    function safe if a future caller relaxes the gate or if `SAMPLE` grows."""

    def windows(self, size: int) -> "list[FileWindow]":
        a = size // 3
        b = 2 * size // 3
        last_window_end = max(0, size - SAMPLE)
        a = min(a, last_window_end)
        b = min(b, last_window_end)
        # All three stage-2 windows are gated by `size > 2*CAP`, so they
        # MUST yield their full length. strict=True triggers hr-rel-09
        # short-read detection.
        return [
            FileWindow(-CAP, CAP, os.SEEK_END, strict=True),
            FileWindow(a, SAMPLE, os.SEEK_SET, strict=True),
            FileWindow(b, SAMPLE, os.SEEK_SET, strict=True),
        ]


_DEFAULT_SAMPLING: SamplingStrategy = ThirdsStrategy()


def hash_tail_and_samples(path, size, strategy=None, config=None):
    """Stage 2 (only when size > 2*CAP): BLAKE3 of last CAP bytes plus two
    SAMPLE-byte windows around size/3 and 2*size/3 (hr-rel-05). Catches files
    that share a container header but differ in body.

    `strategy` (hr-decoup-01) defaults to :class:`ThirdsStrategy`. Pass a
    custom :class:`SamplingStrategy` to override the window layout.

    `config` (hr-arch-05) is forwarded to :func:`_hash_file_windows` so
    per-file errors are bounded by the active :class:`RunConfig`."""
    sampler = strategy if strategy is not None else _DEFAULT_SAMPLING
    return _hash_file_windows(path, sampler.windows(size), config)


def _readable_rep(paths):
    """Pick the first read-accessible alias of an inode, falling back to
    the first path if none probes readable — the hash open still handles
    failure.

    hr-sec-03: `os.access` raises `ValueError: embedded null byte` if
    `p` contains NUL; treat that path as unreadable and continue rather
    than letting the bare ValueError escape the dedup pipeline. NUL in
    paths can arrive from filesystems that allow weird encodings or
    from corrupted state files.

    hr-rel-02: only the first ``_REP_PROBE_LIMIT`` aliases are probed so a
    hardlink-heavy inode doesn't trigger a stat per alias just to pick a
    representative; the hash open still handles a dead first alias."""
    for p in paths[:_REP_PROBE_LIMIT]:
        try:
            if os.access(p, os.R_OK):
                return p
        except (ValueError, OSError):
            continue
    return paths[0]


def _make_head_batch(config):
    """Build a head-batch closure that binds `config` for error logging
    (hr-arch-05). The closure shape ``paths -> [(path, digest), ...]``
    is what :func:`_run_stage` expects."""
    def _head_batch(paths):
        return [(p, hash_head(p, config)) for p in paths]
    return _head_batch


def _make_tail_batch(config):
    """Build a tail-batch closure that binds `config` for error logging
    (hr-arch-05). Closure shape: ``[(size, path), ...] -> [(path,
    digest), ...]``."""
    def _tail_batch(items):
        return [(p, hash_tail_and_samples(p, s, config=config))
                for s, p in items]
    return _tail_batch


# Back-compat shims for callers (tests, embedders) that import these
# bare-name helpers. The pipeline itself uses the `_make_*_batch`
# closure factories above so each run can bind its own RunConfig.
# `config=None` reproduces the legacy unbound-config behaviour (hr-dup-01).
_head_batch = _make_head_batch(None)
_tail_batch = _make_tail_batch(None)


def _iter_batches(items, batch_size):
    """Yield consecutive `batch_size`-sized chunks of `items`
    (hr-perf-04). Lazy generator — never materialises the full slice
    list, so a 10M-candidate stage doesn't pin 156k+ slice objects
    before the first hash starts.

    hr-rel-12: each yielded chunk is a fresh list — Python list slicing
    already returns a new list, so a consumer that mutates a batch in
    place (sort, append) cannot corrupt subsequent batches via aliased
    storage. hr-perf-05: the previous ``list(items[i:...])`` wrapper
    was a redundant second copy of the same slice; dropped."""
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def _capped_byte_total(sizes) -> int:
    """Sum `sizes` but stop once the running total reaches
    ``THREAD_THRESHOLD_BYTES`` (hr-perf-02).

    :func:`_run_stage` only needs to know whether the work crosses the
    serial-vs-threaded threshold, not the exact byte count. On a tree
    with millions of candidates the full O(N) sum is pure waste once the
    answer is already decided, so this short-circuits."""
    total = 0
    for size in sizes:
        total += size
        if total >= THREAD_THRESHOLD_BYTES:
            return total
    return total


def _cancelled(cancel_event) -> bool:
    """True when `cancel_event` is a set :class:`threading.Event`
    (hr-conc-01). ``None`` (no cancel wired) is never cancelled."""
    return cancel_event is not None and cancel_event.is_set()


def _run_stage(items, batch_fn, total_bytes, jobs, cancel_event=None):
    """Dispatch a hash stage either serially or via ThreadPoolExecutor,
    depending on total work. Returns (digest_by_path, error_count).

    Stage 2 on huge trees streams batches lazily through `_iter_batches`
    (hr-perf-04) so the executor consumes them as workers free up
    instead of seeing the full slice list upfront.

    hr-conc-01: `cancel_event` (the SIGINT cooperative-cancel flag) is
    checked between batches. Once set, no further batches are dispatched
    and the partial ``out`` collected so far is returned — so a Ctrl-C
    during a long hash phase takes effect at the next batch boundary
    instead of being ignored until a second Ctrl-C."""
    out = {}
    errors = 0
    if total_bytes < THREAD_THRESHOLD_BYTES:
        if _cancelled(cancel_event):
            return out, errors
        for p, d in batch_fn(items):
            out[p] = d
            if d is None:
                errors += 1
        return out, errors
    # hr-rel-13: wrap the pool loop in try/finally so a mid-iteration
    # exception (e.g. `batch_fn` raises) doesn't return a silently
    # partial `out` dict to the caller.
    try:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for batch in ex.map(batch_fn, _iter_batches(items, HASH_BATCH)):
                for p, d in batch:
                    out[p] = d
                    if d is None:
                        errors += 1
                # hr-conc-01: stop pulling new batches once cancelled.
                if _cancelled(cancel_event):
                    break
    except Exception as exc:
        # hr-rel-14: surface partial progress on EVERY Python (3.10 and
        # 3.11+). `__partial__` is set unconditionally so 3.10 callers
        # who can't read `__notes__` still see the count via attribute
        # lookup; `add_note` is also called when available so the
        # default traceback formatter prints the note inline.
        progress = {"hashed": len(out), "total": len(items)}
        exc.__partial__ = progress
        note = (
            f"_run_stage failed mid-iteration: {progress['hashed']}/"
            f"{progress['total']} items hashed before the error"
        )
        if hasattr(exc, "add_note"):   # pragma: no branch - 3.11+ always has it
            exc.add_note(note)
        raise
    return out, errors


def _group_by(items, key_fn, value_fn=None):
    """Group `items` into a dict keyed by `key_fn(item) -> hashable`
    (hr-dup-02 / hr-cx-04).

    When `value_fn` is provided, the bucket value is `value_fn(item)`
    instead of the item itself. Lets callers project to the field they
    actually need (e.g. `(key, size)` pairs grouped by size where the
    bucket should hold just `key`) without a post-loop reshape."""
    grouped: dict = {}
    project = value_fn if value_fn is not None else (lambda x: x)
    for item in items:
        grouped.setdefault(key_fn(item), []).append(project(item))
    return grouped


def index_inodes(files, alias_cap=None, overflow=None):
    """Group walk results by inode: returns (aliases[(dev,ino)] -> [paths],
    inode_size[(dev,ino)] -> size). Hardlinks share one inode entry.

    hr-rel-01: when several aliases of one inode report DIFFERENT sizes
    (a stat race during the walk), the FIRST observed size wins — the
    map is stable regardless of walk ordering instead of silently
    inheriting whichever alias the walk saw last.

    `files` may be any iterable of ``(path, size, dev, ino)`` tuples,
    including the streaming iterator from :func:`iter_threaded_walk`
    (hr-scal-05). The iterable is consumed exactly once.

    hr-scal-06: when ``alias_cap`` is supplied, each per-inode path
    list is capped at that length AT INGEST TIME — a single hardlink-
    heavy inode (100k aliases) no longer materialises all 100k path
    strings in RAM during the walk. Elided counts are recorded into
    ``overflow[(dev,ino)]`` (a caller-supplied dict) so the emit-side
    sentinel can still report ``+N more`` honestly. ``alias_cap=None``
    preserves the original unbounded behaviour for callers that don't
    care about the worst-case alias count."""
    aliases = defaultdict(list)
    inode_size = {}
    # hr-rel-02: consume via an explicit iterator and close it in the
    # finally. If a later pipeline stage raises, the caller still holds a
    # reference to `files`, so the generator would NOT be GC-collected
    # promptly — its `__iter__` finally (which joins the walk coordinator
    # and finalises `.stats`) would never run, leaving daemon walk threads
    # scandir-ing and `.stats` zeroed. Closing here makes that
    # finalisation deterministic the moment ingest unwinds.
    it = iter(files)
    try:
        _ingest(it, aliases, inode_size, alias_cap, overflow)
    finally:
        close = getattr(it, "close", None)
        if close is not None:
            close()
    return aliases, inode_size


def _ingest(it, aliases, inode_size, alias_cap, overflow) -> None:
    """Drain walk entries into `aliases`/`inode_size`/`overflow`
    (hr-rel-01 / hr-rel-02). Extracted so :func:`index_inodes` can wrap
    the consume in a try/finally that closes the source iterator."""
    for path, size, dev, ino in it:
        key = (dev, ino)
        bucket = aliases[key]
        if alias_cap is None or len(bucket) < alias_cap:
            bucket.append(path)
        elif overflow is not None:
            overflow[key] = overflow.get(key, 0) + 1
        # hr-rel-01: first-writer-wins via setdefault. Two walk entries
        # for the same (dev, ino) should report the same size, but a stat
        # race (file written between the two scandir hits) can disagree.
        # Last-writer-wins silently let the inode inherit whichever size
        # the walk happened to see last, which then drives the CAP / 2*CAP
        # stage gating and the stage-2 sample offsets. Pinning the first
        # observed size makes the choice deterministic regardless of walk
        # ordering.
        inode_size.setdefault(key, size)


def size_collision_candidates(inode_size):
    """Size pre-filter: return [(size, inode_key), ...] for inodes whose size
    is shared by at least one other inode, sorted largest-first."""
    # hr-cx-04: project to bucket value (key) so post-loop reshape isn't needed.
    by_size = _group_by(
        inode_size.items(),
        key_fn=lambda kv: kv[1],
        value_fn=lambda kv: kv[0],
    )
    candidates = []
    for size, keys in by_size.items():
        if len(keys) >= 2:
            for key in keys:
                candidates.append((size, key))
    candidates.sort(key=lambda sk: sk[0], reverse=True)
    return candidates


class DedupResult(NamedTuple):
    """Outcome of `find_duplicate_groups` (hr-arch-02 / hr-arch-01).

    Provides named attribute access and a typed repr. `aliases` stays on
    the result for `emit_groups` to expand inode keys back to paths.

    `overflow` (hr-arch-01): the per-inode ingest-elided alias counts
    (``{(dev, ino): n_elided}``), or ``None`` when the alias cap was
    disabled. It is returned EXPLICITLY here instead of being smuggled on
    `config`; a batched caller forwards it to :func:`emit_groups` so the
    ``+N more`` sentinel still reports the true total.
    """

    groups: dict
    aliases: dict
    info: dict
    overflow: dict | None = None


class _MoreSentinel(str):
    """`str` subclass that wraps a `+N more` truncation marker
    (hr-hyg-02). Inherits all `str` behaviour so existing callers that
    write it to a stream still work, but `isinstance(p, _MoreSentinel)`
    lets emit-side filters distinguish it from a real path."""

    __slots__ = ()


def _expand_keys_to_paths(keys, aliases, config=None, *, cap=None,
                          overflow=None) -> list:
    """Expand inode `keys` to their alias paths (hr-arch-04), truncating
    at the alias cap with a ``+N more`` sentinel (hr-scal-03 / hr-hyg-02
    / hr-arch-05).

    A single inode with millions of hardlinks otherwise materialises the
    full list both here and via :func:`emit_groups`. Capping the output
    keeps the printed group bounded; the sentinel records how many paths
    were elided so the user knows the count is not the truth.

    Cap resolution order: explicit ``cap=`` kwarg > ``config.alias_cap``
    > :data:`DEFAULT_ALIAS_CAP`. The kwarg form preserves back-compat
    with tests / external callers that pre-date :class:`RunConfig`. When
    a config is supplied, ``alias_cap_hits`` is incremented on it so
    :func:`main` can emit a one-shot WARNING.

    hr-scal-06 / hr-arch-01: when ``overflow`` is supplied (explicitly by
    the caller — never read off ``config``), the per-inode counts of
    paths elided AT INGEST (by :func:`index_inodes` with `alias_cap`)
    are added to the displayed ``+N more`` so the sentinel still
    reports the real total even though the alias list itself was
    bounded at scan time.

    The sentinel is a typed :class:`_MoreSentinel` instance so emit-side
    :func:`_count_real_paths` can exclude it from the duplicate-group
    filter — a pair like ``[path0, "+1 more"]`` is NOT actually 2
    duplicates."""
    if cap is None:
        cap = config.alias_cap if config is not None else DEFAULT_ALIAS_CAP
    # hr-cmplx-01: a resolved cap of None means the cap is disabled —
    # return every path with no truncation and no sentinel.
    if cap is None:
        out: list = []
        for key in keys:
            out.extend(aliases.get(key, ()))
        return out
    # hr-arch-01: `overflow` is now passed explicitly by the emit layer
    # (`_emit_one_group` threads it from `on_group`/`emit_groups`). It is
    # no longer smuggled on `config`, so emit correctness no longer
    # silently depends on `_prepare_candidates` having mutated the config.
    out = []
    total = 0
    for key in keys:
        paths = aliases.get(key, ())
        total += len(paths)
        if overflow is not None:
            total += overflow.get(key, 0)
        # hr-scal-01: extend only up to the remaining room under `cap` so a
        # single inode whose bucket far exceeds `cap` never materialises the
        # whole list before the final slice.
        room = cap - len(out)
        if room > 0:
            out.extend(paths[:room])
    if total > cap:
        if config is not None:
            # hr-conc-06: guard against torn `+= 1` on free-threaded py3.13.
            with config._counter_lock:
                config.alias_cap_hits += 1
        return out[:cap] + [_MoreSentinel(f"+{total - cap} more")]
    return out


def _count_real_paths(expanded) -> int:
    """Number of REAL paths in an `_expand_keys_to_paths` result —
    excludes any trailing `_MoreSentinel` (hr-hyg-02)."""
    return sum(1 for p in expanded if not isinstance(p, _MoreSentinel))


def _emit_one_group(digest_key, keys, aliases, write, config, overflow=None):
    """Render a single duplicate group (hr-dup-03).

    Shared core of :func:`emit_groups` and the callback returned by
    :func:`emit_groups_streaming`. Expands keys via
    :func:`_expand_keys_to_paths`, skips groups whose real path count
    is ≤ 1, then writes ``"<label> <path>\\n"`` lines joined into one
    ``write()`` call (hr-perf-03).

    `overflow` (hr-arch-01): per-inode ingest-elided counts, passed
    explicitly from the emit caller so the ``+N more`` sentinel reports
    the true total without reading it off ``config``.

    Returns ``(groups_delta, paths_delta)`` — ``(0, 0)`` for skipped
    groups, ``(1, real_count)`` for emitted ones."""
    all_paths = _expand_keys_to_paths(keys, aliases, config, overflow=overflow)
    real_count = _count_real_paths(all_paths)
    if real_count <= 1:
        return 0, 0
    label = _format_digest(digest_key)
    write("".join(f"{label} {p}\n" for p in all_paths))
    return 1, real_count


def _stage1_hash(candidates, rep, jobs, config, cancel_event=None):
    """Run Stage 1 (head hash) and bucket candidates by ``(size, head)``
    (hr-cx-05).

    Returns ``(by_head, info)``. ``info`` carries ``stage1`` and
    ``stage1_errors`` for the run summary. `cancel_event` (hr-conc-01) is
    forwarded to :func:`_run_stage` so a Ctrl-C aborts the head-hash
    phase between batches."""
    if not candidates:
        return defaultdict(list), {"stage1": 0, "stage1_errors": 0}
    # hr-perf-01: build the path list and (capped) byte total in one pass
    # over candidates instead of two — non-surviving keys are never probed.
    stage1_paths = []
    stage1_bytes = 0
    for size, key in candidates:
        stage1_paths.append(rep[key])
        if stage1_bytes < THREAD_THRESHOLD_BYTES:
            stage1_bytes += min(size, CAP)
    head_by_path, errors = _run_stage(
        stage1_paths, _make_head_batch(config), stage1_bytes, jobs,
        cancel_event=cancel_event)
    by_head: dict = defaultdict(list)
    for size, key in candidates:
        head = head_by_path.get(rep[key])
        if head is None:
            continue
        by_head[(size, head)].append(key)
    return by_head, {"stage1": len(stage1_paths), "stage1_errors": errors}


def _stage2_hash(stage2_items, jobs, config, cancel_event=None):
    """Run Stage 2 (tail + middle samples) and regroup by
    ``(head, tail)`` (hr-cx-05).

    Returns ``(regrouped, info)``. ``regrouped`` maps ``(head, tail) ->
    [keys]`` for digest-confirmed duplicate groups; ``info`` carries
    ``stage2``, ``stage2_errors``, and ``stage2_skipped`` (hr-rel-10).
    `cancel_event` (hr-conc-01) is forwarded to :func:`_run_stage` so a
    Ctrl-C aborts the tail-hash phase between batches."""
    if not stage2_items:
        return {}, {"stage2": 0, "stage2_errors": 0, "stage2_skipped": 0}
    stage2_bytes = _capped_byte_total(
        min(s, CAP + 2 * SAMPLE) for s, _p, _h, _k in stage2_items)
    # `_run_stage` consumes the legacy (size, path) shape; project for it.
    tail_by_path, errors = _run_stage(
        [(s, p) for s, p, _h, _k in stage2_items],
        _make_tail_batch(config), stage2_bytes, jobs,
        cancel_event=cancel_event)
    regrouped: dict = {}
    skipped = 0
    for _s, path, head, key in stage2_items:
        tail = tail_by_path.get(path)
        if tail is None:
            skipped += 1
            continue
        regrouped.setdefault((head, tail), []).append(key)
    return regrouped, {
        "stage2": len(stage2_items),
        "stage2_errors": errors,
        "stage2_skipped": skipped,
    }


def _prepare_candidates(aliases, inode_size, overflow):
    """Select size-collision candidates and build the per-inode
    representative map (hr-cx-01).

    Drops the alias / overflow buckets of non-candidate inodes so peak
    memory tracks candidates rather than the whole tree (hr-scal-01 /
    hr-scal-02), then picks a readable representative per candidate
    inode (hr-rel-02). Returns ``(candidates, rep)``; `aliases` and
    `overflow` are mutated in place to retain only candidate keys.

    hr-arch-01: `overflow` is no longer stashed on `config` — the caller
    threads it explicitly into the emit layer."""
    candidates = size_collision_candidates(inode_size)
    cand_keys = {key for _, key in candidates}
    # hr-scal-01: drop non-candidate buckets in place so we never briefly
    # hold a second full-size copy of the alias dict.
    for key in [k for k in aliases if k not in cand_keys]:
        del aliases[key]
    if overflow is not None:
        for key in [k for k in overflow if k not in cand_keys]:
            del overflow[key]
    rep = {key: _readable_rep(aliases[key]) for _, key in candidates}
    return candidates, rep


def _split_stage1_buckets(by_head, rep, accept_group):
    """Triage stage-1 ``(size, head)`` buckets (hr-cx-01).

    Buckets with a single member can't be duplicates and are dropped.
    Buckets whose size means the head IS the whole file are confirmed
    immediately via ``accept_group`` (hr-cx-03 composite key). The rest
    become ``(size, path, head, key)`` stage-2 items — self-describing so
    the follow-up join needs no side lookup (hr-cx-02). Returns the
    stage-2 item list."""
    stage2_items: list = []
    for (size, head), keys in by_head.items():
        if len(keys) < 2:
            continue
        if size <= HEAD_TAIL_THRESHOLD:
            accept_group((head, None), keys)
        else:
            for key in keys:
                stage2_items.append((size, rep[key], head, key))
    return stage2_items


def find_duplicate_groups(files, jobs, on_group=None, config=None,
                          on_walk_done=None, cancel_event=None):
    """Run the dedup pipeline on walk results (hr-arch-01).

    `files` may be a list OR an iterator (hr-scal-05). When called with
    the iterator from :func:`iter_threaded_walk`, the full walk list is
    never materialised — :func:`index_inodes` consumes it exactly once.

    Returns a :class:`DedupResult` (`.groups` maps digest -> [inode_key,
    ...]; `.aliases` is the per-key path list; `.info` holds the
    per-stage counters for the summary; `.overflow` holds the per-inode
    ingest-elided alias counts, or ``None`` when the cap was disabled —
    forward it to :func:`emit_groups` for an accurate ``+N more``).

    Hashing is split into helper functions :func:`_stage1_hash` and
    :func:`_stage2_hash` (hr-cx-05) so each piece stays under the
    AGENTS.md cyclomatic-complexity ceiling.

    `on_group(digest_key, keys, aliases)` (hr-scal-02): when supplied,
    each confirmed group is handed to the callback the instant the
    pipeline knows it is final, instead of being accumulated in
    `groups`. Stage-1-only groups stream out as soon as the head bucket
    is checked; stage-2 groups stream out per-(head,tail).

    `config` (hr-arch-05 / hr-decoup-02): per-run :class:`RunConfig`.
    Carries alias-cap, error-cap thresholds, and per-run counters that
    used to live in module globals. Defaults to a fresh instance.

    `on_walk_done()` (hr-scal-05): optional callback invoked the instant
    the inode index has been built — lets :func:`main` split walk-time
    from hash-time without holding a `files` list across both phases.

    `cancel_event` (hr-conc-01): the SIGINT cooperative-cancel flag, the
    same one the walk workers consult. Forwarded into both hash stages so
    a Ctrl-C during a long stage-1/stage-2 phase stops dispatching new
    batches at the next batch boundary instead of being ignored until a
    second Ctrl-C. Already-confirmed groups stay in the partial result.

    Ingest-time alias cap (hr-decoup-04): when ``config.alias_cap`` is
    set to a meaningful (non-sentinel) value, the per-inode path list
    is bounded at scan time (hr-scal-06) so a single hardlink-heavy
    inode no longer materialises every alias in RAM. The elided count
    per inode is collected into ``overflow`` and threaded into
    :func:`_expand_keys_to_paths` so the emit-side ``+N more``
    sentinel still reports the real total."""
    if config is None:
        config = RunConfig()
    # hr-decoup-04: thread the alias cap from config into index_inodes
    # and the per-inode overflow counts into the emit stage. Skip the
    # cap when the user disabled it (hr-cx-01: `alias_cap_active`
    # encapsulates the NO_CAP sentinel test).
    ingest_cap = config.alias_cap if config.alias_cap_active else None
    overflow: "dict[tuple, int] | None" = {} if ingest_cap is not None else None
    aliases, inode_size = index_inodes(
        files, alias_cap=ingest_cap, overflow=overflow)
    if on_walk_done is not None:
        on_walk_done()
    n_inodes = len(aliases)
    candidates, rep = _prepare_candidates(aliases, inode_size, overflow)
    # hr-scal-02: inode_size is no longer needed once candidates are
    # selected — drop it so it isn't live alongside aliases/rep at peak.
    del inode_size

    final_groups: dict = {}   # digest_key -> [inode_key, ...]

    def _accept_group(digest_key, keys) -> None:
        """Route a confirmed group either to the streaming callback
        (hr-scal-02) or into the accumulator buffer (back-compat).

        hr-decoup-04 / hr-arch-01: the streaming callback receives the
        alias dict and the overflow dict EXPLICITLY (4th arg) — when the
        ingest cap is active emit-side includes the ingest-time elisions
        in the `+N more` sentinel without the pipeline mutating
        ``config``."""
        if on_group is not None:
            on_group(digest_key, list(keys), aliases, overflow)
        else:
            final_groups.setdefault(digest_key, []).extend(keys)

    # ---- Stage 1: head hash ----
    by_head, stage1_info = _stage1_hash(
        candidates, rep, jobs, config, cancel_event)
    stage2_items = _split_stage1_buckets(by_head, rep, _accept_group)

    # ---- Stage 2: tail + middle samples for size+head collisions ----
    regrouped, stage2_info = _stage2_hash(
        stage2_items, jobs, config, cancel_event)
    for combined, keys in regrouped.items():
        if len(keys) >= 2:
            _accept_group(combined, keys)

    info = {
        "inodes": n_inodes,
        "candidates": len(candidates),
        **stage1_info,
        **stage2_info,
    }
    return DedupResult(
        groups=final_groups, aliases=aliases, info=info, overflow=overflow)


def _format_digest(digest) -> str:
    """Render a composite digest key for output (hr-cx-03).

    `(head, None)` -> ``head`` (head IS the whole file).
    `(head, tail)` -> ``head:tail`` (matches the pre-refactor visual form).
    Bare strings (back-compat for legacy callers that bypass the pipeline)
    are returned unchanged."""
    if isinstance(digest, tuple):
        head, tail = digest
        return head if tail is None else f"{head}:{tail}"
    return digest


def emit_groups(final_groups, aliases, write, config=None, overflow=None):
    """Expand each duplicate inode group back to all its alias paths and
    write '<digest> <path>' lines. Returns ``(dup_groups, dup_paths)``.

    Per-group rendering is delegated to :func:`_emit_one_group`
    (hr-dup-03) so this function and :func:`emit_groups_streaming` share
    the same shape — a bug fix to the emit format lands in one place.

    `config` (hr-arch-05): provides the alias cap and accumulates
    ``alias_cap_hits``. Defaults to a fresh :class:`RunConfig`.

    `overflow` (hr-arch-01): per-inode ingest-elided counts from
    :attr:`DedupResult.overflow`, passed explicitly so the ``+N more``
    sentinel reflects the true total. ``None`` (default) means the alias
    cap was disabled / no paths were elided at ingest."""
    if config is None:
        config = RunConfig()
    dup_groups = 0
    dup_paths = 0
    for digest, keys in final_groups.items():
        g, p = _emit_one_group(digest, keys, aliases, write, config, overflow)
        dup_groups += g
        dup_paths += p
    return dup_groups, dup_paths


def emit_groups_streaming(write, config=None):
    """Build an ``on_group(digest_key, keys, aliases, overflow)`` callback
    that writes each group immediately (hr-scal-02). Pair with
    ``find_duplicate_groups(..., on_group=cb)`` for the streaming
    pipeline.

    hr-arch-01: the callback's 4th parameter ``overflow`` carries the
    per-inode ingest-elided counts explicitly from the pipeline, so the
    ``+N more`` sentinel reports the true total without the pipeline
    smuggling the overflow dict onto ``config``.

    Returns ``(cb, totals_fn)`` where ``totals_fn()`` yields
    ``(dup_groups, dup_paths)`` after the pipeline drained — matching
    the return tuple of the batched :func:`emit_groups`.

    Per-group rendering goes through :func:`_emit_one_group`
    (hr-dup-03), shared with :func:`emit_groups`.

    `config` (hr-arch-05): same role as in :func:`emit_groups`.

    Threading contract (hr-conc-04)
    -------------------------------
    The callback is invoked SEQUENTIALLY by `find_duplicate_groups` on
    the main pipeline thread — never concurrently. The internal `state`
    dict is therefore unguarded by a lock. A future caller that fans
    the callback out across threads MUST wrap state updates in a
    `threading.Lock` (or pass its own thread-safe collector); the cb
    object returned here is NOT re-entrant."""
    if config is None:
        config = RunConfig()
    state = {"groups": 0, "paths": 0}

    def cb(digest_key, keys, aliases, overflow=None) -> None:
        g, p = _emit_one_group(
            digest_key, keys, aliases, write, config, overflow)
        state["groups"] += g
        state["paths"] += p

    def totals():
        return state["groups"], state["paths"]

    return cb, totals


def _preflight_root(root):
    """Validate `root` for the walk; raise :class:`RootError` on bad
    input (hr-rel-17).

    Replaces the older ``sys.exit(2)``-on-bad-root behaviour so
    library / test consumers can catch the typed error and recover.
    :func:`main` translates it back into an exit code at the CLI
    boundary.

    hr-sec-02: error messages use ``os.path.realpath`` so a typo
    relative to cwd surfaces as the user's intended path family
    (instead of leaking a server-side absolute prefix). Falls back to
    the raw input when realpath itself fails (e.g. NUL byte).

    hr-sec-04: each ``os`` call is guarded against ``ValueError`` from
    NUL bytes in the root string — the graceful ``RootError`` fires
    instead of a bare traceback."""
    try:
        display = os.path.realpath(root)
    except (ValueError, OSError):
        display = root
    try:
        exists = os.path.exists(root)
        is_dir = os.path.isdir(root) if exists else False
        accessible = (os.access(root, os.R_OK | os.X_OK)
                      if (exists and is_dir) else False)
    except (ValueError, OSError) as exc:
        raise RootError(f"{display}: invalid path ({exc})") from exc
    if not exists:
        raise RootError(f"{display}: no such file or directory")
    if not is_dir:
        raise RootError(f"{display}: not a directory")
    if not accessible:
        raise RootError(f"{display}: permission denied")


def _install_sigint_cancel(cancel_event):
    """Wire SIGINT to set `cancel_event` (hr-conc-05).

    First Ctrl-C: cooperative — workers stop pulling new directories
    and the walk returns whatever it has so far (hr-conc-02). Second
    Ctrl-C: default handler is restored so the user gets the usual
    ``KeyboardInterrupt`` if the cooperative cancel hangs (e.g. mid-
    hash). Returns the previous handler so embedders can restore it.

    Must be called from the main thread of the main interpreter —
    ``signal.signal`` raises ``ValueError`` otherwise."""
    previous = signal.getsignal(signal.SIGINT)

    def _handle(signum, frame):
        cancel_event.set()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _handle)
    return previous


def main():
    ap = argparse.ArgumentParser(
        description="Duplicate finder (head + tail + mid-samples, "
                    "hardlink-aware, two-stage hash).")
    ap.add_argument("directory")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="Walk + hash worker threads (default: cpu_count).")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="Suppress the end-of-run summary on stderr.")
    ap.add_argument("--alias-cap", type=int, default=DEFAULT_ALIAS_CAP,
                    help=("Max paths printed per inode group "
                          f"(default: {DEFAULT_ALIAS_CAP}, 0 = no cap). "
                          "When the cap fires, a `+N more` sentinel is "
                          "appended and a WARNING surfaces once at end "
                          "of run (hr-scal-04)."))
    ap.add_argument(
        "--hash-error-verbose-cap", type=int,
        default=DEFAULT_HASH_ERROR_VERBOSE_CAP,
        help=("Max per-file hash-error lines printed verbosely on "
              f"stderr (default: {DEFAULT_HASH_ERROR_VERBOSE_CAP}); "
              "extras are counted and summarised once at end of run "
              "(hr-obs-03)."))
    args = ap.parse_args()
    root = os.path.abspath(args.directory)

    # hr-rel-17: translate typed RootError to a CLI exit code here, at
    # the CLI boundary. `_preflight_root` no longer calls `sys.exit`.
    try:
        _preflight_root(root)
    except RootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)

    # hr-arch-05 / hr-decoup-02: per-run config — no module-global
    # mutation, no cross-run state leakage in tests.
    config = RunConfig(
        alias_cap=args.alias_cap,
        hash_error_verbose_cap=args.hash_error_verbose_cap,
    )

    # hr-conc-05: cooperative cancel on Ctrl-C — the walker checks the
    # event between directories and returns whatever it has so far.
    cancel_event = threading.Event()
    # hr-rel-20: capture the previous handler so we can restore it in
    # the finally block — without this the custom handler leaks past
    # the run (e.g. when main() is invoked from a long-lived host).
    previous_sigint = _install_sigint_cancel(cancel_event)

    try:
        # hr-obs-02 + hr-scal-05: stream the walk so we never materialise
        # the full `files` list. `on_walk_done` snaps the walk/hash
        # boundary so the per-stage durations remain meaningful.
        walk_iter = iter_threaded_walk(root, args.jobs, cancel_event=cancel_event)
        t_start = time.perf_counter()
        walk_boundary = [None]

        def _mark_walk_done():
            walk_boundary[0] = time.perf_counter()

        # hr-scal-02: stream groups straight to stdout so the result set
        # never buffers in memory. Pipeline-internal `final_groups` stays
        # empty because the callback consumes every group inline.
        on_group, totals = emit_groups_streaming(sys.stdout.write, config=config)
        result = find_duplicate_groups(
            walk_iter, args.jobs,
            on_group=on_group, config=config, on_walk_done=_mark_walk_done,
            cancel_event=cancel_event,
        )
        t_end = time.perf_counter()
        walk_stats = walk_iter.stats
        info = result.info
        dup_groups, dup_paths = totals()
        # hr-rel-03: `_mark_walk_done` is invoked unconditionally by
        # `find_duplicate_groups` the moment `index_inodes` has consumed
        # the walk — even an immediate-cancel walk still returns an empty
        # iterator that index_inodes drains. So once this line is reached
        # (a successful return), `walk_boundary[0]` is always set; the old
        # `is None` fallback could only have fired had the function raised
        # before returning, in which case this code never runs at all. The
        # dead guard + its misleading comment are gone.
        walk_seconds = walk_boundary[0] - t_start
        hash_seconds = t_end - walk_boundary[0]

        # hr-scal-04: one-shot warning so the operator knows printed alias
        # lists may be partial.
        if config.alias_cap_hits > 0 and not args.quiet:
            print(
                f"[ai5] WARNING: alias cap ({config.alias_cap}) truncated "
                f"{config.alias_cap_hits} group(s); rerun with "
                f"--alias-cap=0 to print every hardlink.",
                file=sys.stderr,
            )

        # hr-obs-03: one-shot summary of suppressed hash errors instead of
        # per-file stderr spam.
        if config.hash_error_suppressed > 0 and not args.quiet:
            print(
                f"[ai5] WARNING: {config.hash_error_suppressed} additional "
                f"hash error(s) suppressed (showed first "
                f"{config.hash_error_logged}); rerun with a larger "
                "--hash-error-verbose-cap to see more.",
                file=sys.stderr,
            )

        # hr-conc-05: surface partial results when the user hit Ctrl-C.
        if cancel_event.is_set() and not args.quiet:
            print("[ai5] WARNING: walk cancelled by SIGINT — results are partial.",
                  file=sys.stderr)

        # ---- Summary on stderr (the user's safety net) ----
        if not args.quiet:
            # hr-obs-01: abbreviate big counts so the line stays readable.
            f = _fmt_count
            print(
                f"[ai5] dirs={f(walk_stats['dirs'])} "
                f"files={f(walk_stats['files'])} "
                f"inodes={f(info['inodes'])} "
                f"size_collision_inodes={f(info['candidates'])} "
                f"hashed_stage1={f(info['stage1'])} "
                f"hashed_stage2={f(info['stage2'])} "
                f"dup_groups={f(dup_groups)} "
                f"dup_paths={f(dup_paths)} "
                f"walk_errors={f(walk_stats['dir_errors'])}+"
                f"{f(walk_stats['entry_errors'])} "
                f"hash_errors={f(info['stage1_errors'])}+{f(info['stage2_errors'])} "
                f"hash_skipped={f(config.hash_skipped_vanished)} "
                f"walk_s={walk_seconds:.2f} hash_s={hash_seconds:.2f}",
                file=sys.stderr,
            )
    finally:
        # hr-rel-20: always restore the previous SIGINT handler so a
        # second run (or a host that imports and calls main()) gets a
        # clean signal stack. `signal.getsignal` returns None when the
        # previous handler was installed from C (or wasn't installed at
        # all); `signal.signal` rejects None, so fall back to SIG_DFL.
        signal.signal(
            signal.SIGINT,
            previous_sigint if previous_sigint is not None else signal.SIG_DFL,
        )


def _fmt_count(n: int) -> str:
    """Human-friendly count (hr-obs-01 / hr-hyg-01): bare integer up to
    1M, then abbreviated as 1.2M / 3.4G / 5.6T. Negative inputs are
    signed via a single negate-and-prefix instead of recursion.

    Used by the summary line and any progress emitter that reports large
    counts. Pure integer math + format string — no locale dependency."""
    sign = "-" if n < 0 else ""
    magnitude = -n if n < 0 else n
    if magnitude < 1_000_000:
        return f"{sign}{magnitude}"
    for unit, scale in (("T", 1_000_000_000_000),
                        ("G", 1_000_000_000),
                        ("M", 1_000_000)):
        if magnitude >= scale:
            value = magnitude / scale
            body = f"{int(value)}{unit}" if value >= 100 else f"{value:.1f}{unit}"
            return f"{sign}{body}"
    return f"{sign}{magnitude}"  # pragma: no cover - unreachable: 1M floor matches loop


if __name__ == "__main__":  # pragma: no cover
    main()
