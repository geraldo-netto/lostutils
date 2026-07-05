#!/usr/bin/env python3
"""
Recursive duplicate finder. Same output shape as the legacy ai4 script:
one line per file in any duplicate group, "<digest> <path>".

Pipeline:
  1. Threaded walk — collect (path, size, dev, ino) for every regular file.
  2. Hardlink dedup — paths sharing (dev, ino) are aliases of one inode;
     hash the inode once, emit every alias at the end.
  3. Size pre-filter — inodes whose size is unique can't have duplicates.
  4. Stage 1 hash — BLAKE3 of the first 4 MiB of each surviving inode.
     Group by (size, head_digest).
  5. Stage 2 hash — for groups with size > 8 MiB and ≥2 members, also
     hash the last 4 MiB, a contiguous 4 MiB block at the file's center,
     plus two 64 KiB samples at size/3 and 2*size/3. Files that match on
     all windows are reported as duplicates; the rest fall out (head
     matched but content differed past the head).

Why head + center + tail + samples? A first-window-only hash treats files
as duplicate when they only share a container header — common false
positive for MKV/MP4 files with the same intro, ISOs of related distros,
tar backups of similar trees, DB dumps with the same schema. Hashing three
4 MiB blocks (head, center, tail) plus two point samples makes accidental
collision essentially impossible for real-world content while staying
bounded (max ~12.13 MiB read per file, regardless of size).
"""
from __future__ import annotations

import argparse
import errno
import io
import os
import queue
import signal
import stat
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from typing import NamedTuple

import blake3

CAP = 4 * 1024 * 1024          # 4 MiB head/tail window
SAMPLE = 64 * 1024             # mid-file sample window
# hr-mem-01: cap each read() at this many bytes so a worker's transient
# buffer is bounded by READ_CHUNK, not by CAP. Without it a 4 MiB window
# read in one call held ~4 MiB per concurrent hash (~jobs*4 MiB peak).
READ_CHUNK = 1024 * 1024       # 1 MiB per read syscall
HEAD_TAIL_THRESHOLD = CAP      # at-or-below this, the head IS the full file
# (CAP < size <= 2*CAP MUST go through stage 2; otherwise files that share
# their first CAP bytes but differ in the tail would be reported as duplicates
# falsely — hr-rel-03.)
HASH_BATCH = 64
# hr-scal-02: the threaded stage keeps at most ``jobs * SUBMIT_WINDOW``
# batch futures in flight at once (a sliding window) instead of submitting
# every batch up front. 2 keeps every worker fed (one running + one queued)
# while bounding peak memory to ~2*jobs batch lists + futures regardless of
# how many candidates the stage holds.
SUBMIT_WINDOW = 2
# BLAKE3 is ~3 GB/s/thread on modern x86. Below this much candidate
# work, ThreadPoolExecutor setup + per-task overhead exceeds the gain.
THREAD_THRESHOLD_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_JOBS = 8
WALK_OUT_QUEUE_PER_JOB = 4096

DEFAULT_ALIAS_CAP = 1024
DEFAULT_HASH_ERROR_VERBOSE_CAP = 20
# hr-log-01 / hr-log-05: emit a timestamped progress line to stderr every N
# files/items, bracketed by start and done lines. Suppressed by --quiet
# alongside the end-of-run summary. The same cadence drives stage-1/stage-2
# hash progress.
LOG_EVERY_N_FILES = 50
# hr-log-02: default path for the dump of every hashed file
# (``<digest> <path>`` per alias). Appended to each run.
DEFAULT_HASHES_FILE = "hashes.txt"
# hr-cmplx-01: the alias cap is stored as Optional[int] — `None` means
# "no cap". The disabled state is detected via `alias_cap is not None`
# (see `alias_cap_active`), so a legitimate positive cap of any size
# (including exactly 2**31) is honored verbatim.
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
        # hr-obs-01 / hr-obs-02: count benign vanished-file (ENOENT/ESTALE)
        # skips separately from real EACCES/EIO/... hash errors. The
        # per-stage `*_errors` totals count every None digest and so
        # include these; the summary line subtracts this counter (and
        # hash_skipped_shrank) from the printed `hash_errors` so the
        # printed figure is the real-failure count, while the raw skip
        # subset is still reported alongside as `hash_skipped`.
        "hash_skipped_vanished",
        # hr-rel-01: a strict-window short read (the file was truncated
        # between walk and hash) yields None just like a real hash error.
        # Counting it separately here lets the summary subtract it from the
        # printed hash_errors so a mid-run shrink doesn't masquerade as an
        # EACCES/EIO failure worth investigating, and the operator still
        # sees that a file changed under them.
        "hash_skipped_shrank",
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
        self.hash_skipped_shrank = 0
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


def threaded_walk(root, jobs, cancel_event=None, skip_ino=None):
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
    it = iter_threaded_walk(
        root, jobs, cancel_event=cancel_event, skip_ino=skip_ino)
    results = list(it)
    return results, it.stats


def iter_threaded_walk(root, jobs, cancel_event=None, skip_ino=None):
    """Stream entries from a threaded walk (hr-scal-05).

    Returns a :class:`_WalkIter`; iterate it to receive
    ``(path, size, dev, ino)`` tuples as workers produce them. Read
    ``.stats`` AFTER iteration completes for the
    ``{dirs, files, dir_errors, entry_errors}`` totals.

    Unlike :func:`threaded_walk`, the full file list is never
    materialised — RAM tracks "currently buffered" entries, not "every
    file ever seen". On a 50M-file tree this drops peak RSS from
    multi-GB to whatever the consumer keeps in flight.

    `skip_ino` (hr-log-04): an optional ``(st_dev, st_ino)`` tuple whose
    matching regular file is excluded from the stream and the counts —
    used to keep the run's own ``hashes.txt`` dump (opened before the
    walk) from being discovered, hashed, and self-listed. Identity by
    inode covers every hardlink/alias of the dump file, not just one
    path spelling."""
    return _WalkIter(root, jobs, cancel_event, skip_ino)


class _WalkState(NamedTuple):
    """Shared mutable state passed explicitly to :func:`_walk_worker`
    (hr-cx-02).

    Promoting the worker out of :meth:`_WalkIter.__iter__` means its
    closed-over queues, lock and counters become named fields here
    instead of free variables — the worker is now testable / readable in
    isolation. ``inflight`` is a one-element list used as a mutable int
    box guarded by ``lock``."""

    pending: queue.SimpleQueue
    out_q: queue.Queue
    lock: threading.Lock
    inflight: list
    per_worker_stats: list
    jobs: int
    sentinel: object
    cancel_event: "threading.Event | None"
    # hr-log-04: (dev, ino) of the run's own hashes dump to exclude, or None.
    skip_ino: "tuple | None"


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
            # hr-conc-01: re-enqueue `d` (under the same lock that guards
            # inflight) BEFORE re-raising so the dying worker doesn't drop
            # the directory's not-yet-scanned subtree — a surviving worker
            # retries it. The +1 here balances the unconditional -1 in the
            # finally, so inflight nets the re-enqueued directory.
            scanned = False
            with state.lock:
                state.inflight[0] += 1
                state.pending.put(d)
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
                    # hr-log-04: drop the run's own hashes dump (matched by
                    # inode) so it is never counted, hashed, or self-listed.
                    if (state.skip_ino is not None
                            and (st.st_dev, st.st_ino) == state.skip_ino):
                        continue
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

    def __init__(self, root, jobs, cancel_event, skip_ino=None):
        self._root = root
        # jobs=0/negative would spawn no workers → silent empty result
        # (hr-rel-01).
        self._jobs = max(1, jobs)
        self._cancel = cancel_event
        self._skip_ino = skip_ino
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
            out_q=queue.Queue(maxsize=max(1, jobs) * WALK_OUT_QUEUE_PER_JOB),
            lock=threading.Lock(),
            inflight=[1],
            per_worker_stats=per_worker_stats,
            jobs=jobs,
            sentinel=object(),     # signals worker termination
            cancel_event=self._cancel,
            skip_ino=self._skip_ino,
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


def _log_prefix(now=None) -> str:
    """Local timestamp prefix for operational stderr lines (hr-log-05)."""
    stamp = now if now is not None else datetime.now().astimezone()
    return stamp.strftime("[%Y-%m-%d %H:%M:%S %z]")


def _fmt_elapsed(seconds: float) -> str:
    """Compact monotonic elapsed-time renderer for progress lines."""
    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:d}:{secs:02d}"


def _fmt_rate(done: int, elapsed: float) -> str:
    """Items/sec for progress logs; avoids divide-by-zero at startup."""
    if elapsed <= 0:
        return "n/a"
    return _fmt_count(int(done / elapsed))


def _default_jobs() -> int:
    """Conservative default for mixed disk I/O; users can still override."""
    return max(1, min(DEFAULT_MAX_JOBS, os.cpu_count() or 1))


def _emit_hash_error_line(path, exc) -> None:
    """Single source of truth for the per-file hash error stderr format
    (hr-dup-04). Both branches of :func:`_log_hash_error` previously
    duplicated this f-string; a future change to the format (e.g. add a
    timestamp prefix or switch to structured JSON) now lands in one
    place."""
    print(f"{_log_prefix()} ERROR: hash failed for {path}: {exc}",
          file=sys.stderr)


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


def _tick_shrank(config) -> None:
    """Count one strict-window short-read (truncated-mid-run) skip
    (hr-rel-01).

    No-op without a config (legacy callers / tests that hash directly).
    hr-conc-06: guarded by ``config._counter_lock`` for deterministic
    counts on a free-threaded build."""
    if config is not None:
        with config._counter_lock:
            config.hash_skipped_shrank += 1


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
        # hr-mem-01: bound the per-read allocation to READ_CHUNK so a wide
        # CAP window doesn't pin CAP bytes per concurrent hash.
        chunk = f.read(min(remaining, READ_CHUNK))
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
        try:
            f = os.fdopen(fd, "rb", buffering=0)
        except BaseException:
            # hr-rob-02: fdopen never took ownership of the raw fd, so the
            # `with` below never runs to close it; close it here so a fdopen
            # failure can't leak one fd per file across a long run.
            os.close(fd)
            raise
        with f:
            h = blake3.blake3()
            for window in windows:
                f.seek(window.offset, window.whence)
                if not _read_window_into(
                        h, f, window.length, window.strict):
                    # File shrank / partial read in a strict window — abort
                    # the hash so a truncated window can't silently produce
                    # a different digest from a full re-read (hr-rel-09).
                    # hr-rel-01: tick the dedicated shrank counter so this
                    # None isn't silently lumped into hash_errors with no
                    # trace — a file truncated mid-run is observable apart
                    # from a permission / I/O failure.
                    _tick_shrank(config)
                    return None
            return h.hexdigest()
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
    tail-CAP, a center CAP-wide block at the file midpoint, plus two
    SAMPLE-byte windows at `size // 3` and `2 * size // 3` (see
    `ThirdsStrategy` for the exact layout). Splitting this out from `hash_tail_and_samples` lets
    tests plug a deterministic strategy (e.g. fixed offsets) and lets a
    future caller experiment with denser sampling for very large files
    without touching the dedup pipeline."""

    def windows(self, size: int) -> "list[FileWindow]":
        raise NotImplementedError  # pragma: no cover


class ThirdsStrategy(SamplingStrategy):
    """Default sampler: tail CAP + center CAP block + middle thirds
    (hr-rel-05 / hr-rel-06).

    Combined with the stage-1 head CAP, a confirmed duplicate has had its
    first 4 MiB, last 4 MiB AND a contiguous 4 MiB block at the file's
    center hashed, plus two 64 KiB point samples at size/3 and 2*size/3.
    The center block is the dominant collision-reducer: a 4 MiB contiguous
    region covers far more than the 128 KiB of point samples, so two large
    files that differ only somewhere in their middle are now caught.

    Offsets are clamped so no window can read past EOF (hr-rel-06): given
    the stage-2 gate of `size > 2*CAP`, every window's end-point already
    fits, but the clamps keep the function safe if a future caller relaxes
    the gate, if `SAMPLE` grows, or for the center block on small files."""

    def windows(self, size: int) -> "list[FileWindow]":
        a = size // 3
        b = 2 * size // 3
        last_sample_start = max(0, size - SAMPLE)
        a = min(a, last_sample_start)
        b = min(b, last_sample_start)
        # Center 4 MiB block: start at the file midpoint minus half a CAP so
        # the window is centered, clamped so a CAP-wide read never runs past
        # EOF (covers small files if the size > 2*CAP gate is ever relaxed).
        last_cap_start = max(0, size - CAP)
        mid = min(max(0, size // 2 - CAP // 2), last_cap_start)
        # All stage-2 windows are gated by `size > 2*CAP`, so they MUST
        # yield their full length. strict=True triggers hr-rel-09 short-read
        # detection.
        return [
            FileWindow(-CAP, CAP, os.SEEK_END, strict=True),    # tail 4 MiB
            FileWindow(mid, CAP, os.SEEK_SET, strict=True),     # center 4 MiB
            FileWindow(a, SAMPLE, os.SEEK_SET, strict=True),
            FileWindow(b, SAMPLE, os.SEEK_SET, strict=True),
        ]


_DEFAULT_SAMPLING: SamplingStrategy = ThirdsStrategy()


def hash_tail_and_samples(path, size, strategy=None, config=None):
    """Stage 2 (only when size > 2*CAP): BLAKE3 of the last CAP bytes, a
    center CAP block at the file midpoint, plus two SAMPLE-byte windows
    around size/3 and 2*size/3 (hr-rel-05). Catches files that share a
    container header but differ anywhere in the body.

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


def _make_head_candidate_batch(rep, config):
    """Build a stage-1 batch closure keyed by inode, not path (hr-scal-07)."""
    def _head_batch(items):
        return [(key, hash_head(rep[key], config)) for _size, key in items]
    return _head_batch


def _make_tail_batch(config):
    """Build a tail-batch closure that binds `config` for error logging
    (hr-arch-05). Closure shape: ``[(size, path), ...] -> [(path,
    digest), ...]``."""
    def _tail_batch(items):
        return [(p, hash_tail_and_samples(p, s, config=config))
                for s, p in items]
    return _tail_batch


def _make_tail_stage2_batch(config):
    """Build a stage-2 batch closure without projecting a second item list."""
    def _tail_batch(items):
        return [
            (item, hash_tail_and_samples(item[1], item[0], config=config))
            for item in items
        ]
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


def _collect_batch(batch, out) -> "tuple[int, int]":
    """Fold one ``[(path, digest), ...]`` batch into ``out``; return the
    ``(error_count, item_count)`` in it (hr-scal-02 / hr-log-05)."""
    errors = 0
    count = 0
    for p, d in batch:
        out[p] = d
        count += 1
        if d is None:
            errors += 1
    return errors, count


def _notify_stage_progress(on_progress, done: int, total: int) -> None:
    """Invoke a hash-stage progress callback when one is supplied."""
    if on_progress is not None:
        on_progress(done, total)


def _run_stage_serial(items, batch_fn, out, cancel_event,
                      on_progress=None) -> int:
    """Serial hash dispatch in HASH_BATCH chunks (hr-scal-07).

    The old serial branch handed the entire item list to one batch call.
    That was cheap for a handful of files, but pathological for hundreds
    of thousands of tiny same-size files: the worker allocated one huge
    result list and emitted no progress until the whole stage finished."""
    errors = 0
    done = 0
    total = len(items)
    for batch in _iter_batches(items, HASH_BATCH):
        if _cancelled(cancel_event):
            break
        batch_errors, count = _collect_batch(batch_fn(batch), out)
        errors += batch_errors
        done += count
        _notify_stage_progress(on_progress, done, total)
    return errors


def _fill_window(ex, batch_fn, batches, inflight, window, cancel_event) -> bool:
    """Submit batches until ``inflight`` reaches ``window`` or the batch
    iterator is exhausted (hr-scal-02). Returns True when no more batches
    will ever be submitted — either the iterator drained or the cancel
    event fired (hr-scal-03), so the caller stops replenishing."""
    while len(inflight) < window:
        if _cancelled(cancel_event):
            return True
        try:
            inflight.add(ex.submit(batch_fn, next(batches)))
        except StopIteration:
            return True
    return False


def _run_stage_windowed(items, batch_fn, jobs, out, cancel_event,
                        on_progress=None) -> int:
    """Threaded hash dispatch with BOUNDED submission (hr-scal-02).

    At most ``jobs * SUBMIT_WINDOW`` batch futures are kept in flight at
    once via a sliding window: submit until the window is full, drain the
    completed ones into ``out``, then replenish. Batch lists and future
    objects are therefore bounded by the window — a 10M-candidate stage no
    longer pins every batch + future up front, so the streaming claim
    holds.

    hr-scal-03: replenishment stops the instant ``cancel_event`` is set, so
    a Ctrl-C schedules no new work past the current batch boundary; only
    the ≤ window in-flight batches finish before the pool drains. Returns
    the running hash-failure count."""
    errors = 0
    done_count = 0
    total = len(items)
    window = max(1, jobs) * SUBMIT_WINDOW
    batches = _iter_batches(items, HASH_BATCH)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        inflight: set = set()
        exhausted = _fill_window(
            ex, batch_fn, batches, inflight, window, cancel_event)
        while inflight:
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                batch_errors, count = _collect_batch(fut.result(), out)
                errors += batch_errors
                done_count += count
                _notify_stage_progress(on_progress, done_count, total)
            if not exhausted:
                exhausted = _fill_window(
                    ex, batch_fn, batches, inflight, window, cancel_event)
    return errors


def _run_stage(items, batch_fn, total_bytes, jobs, cancel_event=None,
               on_progress=None):
    """Dispatch a hash stage either serially or via ThreadPoolExecutor,
    depending on total work. Returns (digest_by_item, error_count).

    Stage 2 on huge trees streams batches lazily through `_iter_batches`
    (hr-perf-04) and the threaded path bounds submission to a sliding
    window of ``jobs * SUBMIT_WINDOW`` futures (hr-scal-02) so peak memory
    tracks the window, not the whole candidate list.

    hr-conc-01 / hr-scal-03: `cancel_event` (the SIGINT cooperative-cancel
    flag) is checked between batches in BOTH the serial path and the
    windowed submit loop. Once set, no further batches are dispatched —
    only the ≤ window already-running batches finish — and the partial
    ``out`` collected so far is returned, so a Ctrl-C during a long hash
    phase takes effect at the next batch boundary instead of waiting for
    every queued batch."""
    out = {}
    errors = 0
    if total_bytes < THREAD_THRESHOLD_BYTES:
        return out, _run_stage_serial(
            items, batch_fn, out, cancel_event, on_progress)
    # hr-rel-13: wrap the pool loop in try/finally so a mid-iteration
    # exception (e.g. `batch_fn` raises) doesn't return a silently
    # partial `out` dict to the caller.
    try:
        errors = _run_stage_windowed(
            items, batch_fn, jobs, out, cancel_event, on_progress)
    except Exception as exc:
        # hr-rel-14: surface partial progress on EVERY Python (3.10 and
        # 3.11+). `__partial__` is set unconditionally so 3.10 callers
        # who can't read `__notes__` still see the count via attribute
        # lookup; `add_note` is also called when available so the
        # default traceback formatter prints the note inline.
        progress = {"hashed": len(out), "total": len(items)}
        setattr(exc, "__partial__", progress)
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


def _retry_head_alias(key, tried, aliases, config):
    """hr-rel-02: re-hash the head of the next readable alias of `key`
    after its representative produced a None digest.

    A multi-alias (hardlinked) inode whose representative was deleted or
    became unreadable between walk and hash — while a sibling alias is
    still readable — was silently dropped from its group. Probe the
    remaining aliases (skipping the already-tried `tried` path) and return
    the first non-None head digest, or None when no sibling is readable."""
    for alias in aliases.get(key, ()):
        if alias == tried:
            continue
        head = hash_head(alias, config)
        if head is not None:
            return head
    return None


def _retry_tail_alias(key, tried, size, aliases, config):
    """hr-rel-30: re-hash the stage-2 tail of the next readable alias of `key`
    after its representative produced a None tail, mirroring
    :func:`_retry_head_alias`. A multi-alias (hardlinked) inode whose stage-2
    representative vanished or lost read access between stages — while a sibling
    alias stays readable — was silently dropped from its confirmed group. Returns
    the first non-None tail digest, or None when no sibling is readable."""
    for alias in aliases.get(key, ()):
        if alias == tried:
            continue
        tail = hash_tail_and_samples(alias, size, config=config)
        if tail is not None:
            return tail
    return None


def _stage1_hash(candidates, rep, jobs, config, cancel_event=None,
                 aliases=None, on_hashed=None, on_progress=None):
    """Run Stage 1 (head hash) and bucket candidates by ``(size, head)``
    (hr-cx-05).

    Returns ``(by_head, info)``. ``info`` carries ``stage1`` and
    ``stage1_errors`` for the run summary. `cancel_event` (hr-conc-01) is
    forwarded to :func:`_run_stage` so a Ctrl-C aborts the head-hash
    phase between batches.

    hr-rel-02: when a multi-alias inode's representative head is None and
    `aliases` is supplied, the next readable alias is retried via
    :func:`_retry_head_alias` so a deleted/unreadable rep doesn't silently
    exclude an inode whose siblings are still readable.

    `on_hashed(done, total, head, key, aliases)` (hr-log-02): invoked once
    per candidate inode on the main thread after its head digest is known
    (``head`` is None when it failed). Lets :func:`main` dump every hashed
    file without the pipeline doing I/O. Live progress is driven by
    ``on_progress`` as batches complete."""
    if not candidates:
        return defaultdict(list), {"stage1": 0, "stage1_errors": 0}
    stage1_bytes = _capped_byte_total(min(size, CAP) for size, _key in candidates)
    run_kwargs = {"cancel_event": cancel_event}
    if on_progress is not None:
        run_kwargs["on_progress"] = on_progress
    head_by_key, errors = _run_stage(
        candidates, _make_head_candidate_batch(rep, config), stage1_bytes,
        jobs, **run_kwargs)
    by_head = _bucket_stage1_heads(
        candidates, head_by_key, rep, aliases, config, on_hashed)
    return by_head, {"stage1": len(head_by_key), "stage1_errors": errors}


def _bucket_stage1_heads(candidates, head_by_key, rep, aliases, config, on_hashed):
    """Bucket hashed candidates by ``(size, head)`` (hr-cmplx-03).

    A None head on a multi-alias inode is retried on the next readable sibling
    (hr-rel-02); ``on_hashed(done, total, head, key, aliases)`` is invoked once
    per candidate on the main thread (hr-log-02). Keys absent from
    ``head_by_key`` or still None after retry are dropped from the buckets."""
    by_head: dict = defaultdict(list)
    total = len(candidates)
    for done, (size, key) in enumerate(candidates, 1):
        if key not in head_by_key:
            continue
        head = head_by_key[key]
        if head is None and aliases is not None and len(aliases.get(key, ())) > 1:
            head = _retry_head_alias(key, rep[key], aliases, config)
        if on_hashed is not None:
            on_hashed(done, total, head, key, aliases)
        if head is None:
            continue
        by_head[(size, head)].append(key)
    return by_head


def _stage2_hash(stage2_items, jobs, config, cancel_event=None,
                 on_progress=None, aliases=None):
    """Run Stage 2 (tail + center + middle samples) and regroup by
    ``(head, tail)`` (hr-cx-05).

    Returns ``(regrouped, info)``. ``regrouped`` maps ``(head, tail) ->
    [keys]`` for digest-confirmed duplicate groups; ``info`` carries
    ``stage2`` (items hashed) and ``stage2_errors`` (None tails).
    `cancel_event` (hr-conc-01) is forwarded to :func:`_run_stage` so a
    Ctrl-C aborts the tail-hash phase between batches.

    hr-obs-01: a None tail has a SINGLE source of truth — it is counted
    once in ``stage2_errors`` (the count :func:`_run_stage` returns) and
    then skipped from the regroup below as pure control flow, never
    re-counted. The benign subset (vanished / truncated) is tracked apart
    on the :class:`RunConfig` and subtracted by :func:`main` to derive the
    real-failure figure. The former ``stage2_skipped`` field duplicated
    ``stage2_errors`` (identical except under cancellation) with no
    reconciliation, so it has been removed."""
    if not stage2_items:
        return {}, {"stage2": 0, "stage2_errors": 0}
    # Stage 2 reads up to tail CAP + center CAP + two SAMPLE windows.
    stage2_bytes = _capped_byte_total(
        min(s, 2 * CAP + 2 * SAMPLE) for s, _p, _h, _k in stage2_items)
    run_kwargs = {"cancel_event": cancel_event}
    if on_progress is not None:
        run_kwargs["on_progress"] = on_progress
    tail_by_item, errors = _run_stage(
        stage2_items, _make_tail_stage2_batch(config), stage2_bytes, jobs,
        **run_kwargs)
    regrouped: dict = {}
    for item in stage2_items:
        _s, _path, head, key = item
        tail = tail_by_item.get(item)
        # hr-rel-30: retry the tail on a readable sibling alias before dropping a
        # multi-alias inode whose representative lost read access between stages
        # (mirrors the stage-1 head retry).
        if tail is None and aliases is not None and len(aliases.get(key, ())) > 1:
            tail = _retry_tail_alias(key, _path, _s, aliases, config)
        if tail is None:
            continue   # failed/unhashed tail — already in stage2_errors
        regrouped.setdefault((head, tail), []).append(key)
    return regrouped, {"stage2": len(tail_by_item), "stage2_errors": errors}


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


def _stage_progress_cb(on_stage_progress, stage):
    """Bind a stage label onto ``on_stage_progress`` (hr-log-05), returning a
    ``(done, total)`` callback or None when no callback was supplied."""
    if on_stage_progress is None:
        return None

    def _cb(done, total):
        on_stage_progress(stage, done, total)
    return _cb


def _emit_stage2_groups(regrouped, on_composite, accept_group):
    """Emit confirmed stage-2 ``(head, tail)`` groups (hr-cmplx-04).

    hr-obs-02: reports the composite ``head:tail`` digest per key so the hashes
    dump records the true dup-grouping identity (two files sharing a head but
    differing past it get DISTINCT dump digests). Groups with <2 members are
    dropped."""
    for combined, keys in regrouped.items():
        if on_composite is not None:
            head, tail = combined
            for key in keys:
                on_composite(key, f"{head}:{tail}")
        if len(keys) >= 2:
            accept_group(combined, keys)


def find_duplicate_groups(files, jobs, on_group=None, config=None,
                          on_walk_done=None, cancel_event=None,
                          on_hashed=None, on_stage_progress=None,
                          on_composite=None):
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

    `on_hashed(done, total, head, key, aliases)` (hr-log-02): forwarded to
    :func:`_stage1_hash` and fired once per candidate inode as its head
    digest is resolved — drives the per-file ``hashes.txt`` dump.

    `on_stage_progress(stage, done, total)` (hr-log-05): fired as stage
    batches are collected, not after the whole stage returns. This keeps
    long stage-1 / stage-2 hashing runs visibly alive even when the walk
    has already finished.

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
    # encapsulates the disabled-state test).
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
        candidates, rep, jobs, config, cancel_event, aliases=aliases,
        on_hashed=on_hashed,
        on_progress=_stage_progress_cb(on_stage_progress, "stage1"))
    stage2_items = _split_stage1_buckets(by_head, rep, _accept_group)

    # ---- Stage 2: tail + center + middle samples for size+head collisions ----
    regrouped, stage2_info = _stage2_hash(
        stage2_items, jobs, config, cancel_event,
        on_progress=_stage_progress_cb(on_stage_progress, "stage2"),
        aliases=aliases)
    _emit_stage2_groups(regrouped, on_composite, _accept_group)

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


def _log_line(msg, quiet) -> None:
    """Emit one timestamped operational log line to stderr (hr-log-05).
    No-op when `quiet` is set, matching the summary's --quiet behaviour."""
    if not quiet:
        print(f"{_log_prefix()} {msg}", file=sys.stderr)


_DUMP_COMPOSITE_DIGEST_WIDTH = len(("00" * 32) + ":" + ("00" * 32))


def _dump_digest_field(digest: str) -> str:
    return digest.ljust(_DUMP_COMPOSITE_DIGEST_WIDTH)


class HashDumpWriter:
    """Append hash dump lines early and patch digest fields in place."""

    def __init__(self, handle):
        self._handle = handle
        self._offsets: dict = {}
        self._digests: dict = {}

    def write_head(self, key, digest: str, paths) -> None:
        offsets = []
        for path in paths:
            offsets.append(self._write_line(digest, path))
        self._offsets[key] = offsets
        self._digests[key] = digest
        self._handle.flush()

    def patch_composite(self, key, digest: str) -> None:
        offsets = self._offsets.get(key)
        if not offsets:
            return
        current = self._handle.tell()
        try:
            for offset in offsets:
                self._handle.seek(offset)
                self._handle.write(_dump_digest_field(digest))
            self._handle.flush()
            self._digests[key] = digest
        finally:
            self._handle.seek(current)

    def write_overflow(self, overflow) -> None:
        self._handle.seek(0, os.SEEK_END)
        for key, elided in overflow.items():
            if elided > 0 and key in self._digests:
                self._write_line(
                    self._digests[key], f"+{elided} more (alias-cap)")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def fileno(self) -> int:
        return self._handle.fileno()

    def _write_line(self, digest: str, path: str) -> int:
        offset = self._handle.tell()
        self._handle.write(f"{_dump_digest_field(digest)} {path}\n")
        return offset


def _open_hash_dump(path: str):
    try:
        fh = open(path, "r+", buffering=1, encoding="utf-8",
                  errors="surrogateescape")
    except FileNotFoundError:
        fh = open(path, "w+", buffering=1, encoding="utf-8",
                  errors="surrogateescape")
    fh.seek(0, os.SEEK_END)
    return fh


def _configure_stdio_encoding() -> None:
    """Keep path output writable for Unicode and surrogate-escaped names."""
    for stream in (sys.stdout, sys.stderr):
        if not isinstance(stream, io.TextIOWrapper):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="surrogateescape")
        except (AttributeError, TypeError, ValueError, OSError):
            continue


def _progress_walk(walk_iter, quiet, every=LOG_EVERY_N_FILES):
    """Pass-through generator over the walk that logs a progress line every
    ``every`` files scanned (hr-log-01).

    Wrapping the walk here keeps `index_inodes` consuming the stream exactly
    once; closing this generator (which `index_inodes` does in its finally)
    propagates GeneratorExit into the underlying walk so its coordinator is
    still joined and ``.stats`` finalised. `main` reads ``walk_iter.stats``
    from the real iterator, not this wrapper."""
    n = 0
    started = time.monotonic()
    for entry in walk_iter:
        n += 1
        if n % every == 0:
            elapsed = time.monotonic() - started
            _log_line(
                f"progress: {n} files scanned "
                f"elapsed={_fmt_elapsed(elapsed)} "
                f"rate={_fmt_rate(n, elapsed)}/s",
                quiet,
            )
        yield entry


def _configure_windows(block_size: int, sample_size: int) -> None:
    """Override the hash window sizes from the CLI (hr-adapt-01).

    CAP (head / tail / center block) and SAMPLE (mid-file point samples)
    are read as module globals at hash time, so reassigning them here
    before the pipeline runs is sufficient — the shared
    :data:`_DEFAULT_SAMPLING` instance reads the new values too, no rebuild
    needed. HEAD_TAIL_THRESHOLD tracks CAP so the 'head IS the whole file'
    gate stays consistent with the chosen block size."""
    global CAP, SAMPLE, HEAD_TAIL_THRESHOLD
    CAP = block_size
    SAMPLE = sample_size
    HEAD_TAIL_THRESHOLD = CAP


def main():
    _configure_stdio_encoding()
    ap = argparse.ArgumentParser(
        description="Duplicate finder (head + tail + center + mid-samples, "
                    "hardlink-aware, two-stage hash).")
    ap.add_argument("directory")
    ap.add_argument(
        "-j", "--jobs", type=int, default=_default_jobs(),
        help=("Walk + hash worker threads "
              f"(default: min(cpu_count, {DEFAULT_MAX_JOBS}); override for "
              "fast SSDs or slower disks)."))
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="Suppress the end-of-run summary on stderr.")
    ap.add_argument("--alias-cap", type=int, default=DEFAULT_ALIAS_CAP,
                    help=("Max paths printed per inode group "
                          f"(default: {DEFAULT_ALIAS_CAP}, <= 0 = no cap). "
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
    ap.add_argument(
        "--block-size", type=int, default=CAP,
        help=(f"Head/tail/center hash-window size in bytes (default: {CAP} "
              "= 4 MiB). Larger windows hash more of each file, lowering "
              "false-positive collision odds at the cost of more I/O "
              "(hr-adapt-01)."))
    ap.add_argument(
        "--sample-size", type=int, default=SAMPLE,
        help=(f"Mid-file point-sample window in bytes (default: {SAMPLE} "
              "= 64 KiB) (hr-adapt-01)."))
    ap.add_argument(
        "--hashes-file", default=DEFAULT_HASHES_FILE,
        help=(f"Dump '<digest> <path>' for every hashed file to this path, "
              f"appending to it (default: {DEFAULT_HASHES_FILE}) (hr-log-02)."))
    args = ap.parse_args()
    # hr-rel-21: clamp jobs to >= 1. The walk already clamps via max(1, jobs)
    # but the hash path passes jobs straight to ThreadPoolExecutor, which
    # raises `max_workers must be greater than 0` once a stage crosses
    # THREAD_THRESHOLD_BYTES — so `-j 0` aborted big trees while silently
    # working on small ones.
    args.jobs = max(1, args.jobs)
    # hr-adapt-01: validate then apply the window-size overrides before the
    # pipeline reads CAP/SAMPLE.
    if args.block_size < 1 or args.sample_size < 1:
        print("error: --block-size and --sample-size must be >= 1",
              file=sys.stderr)
        sys.exit(2)
    _configure_windows(args.block_size, args.sample_size)
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

    hashes_state: dict = {"writer": None}
    # hr-obs-10: per-inode count of aliases elided AT INGEST by the alias cap, so
    # the dump can append a `+N more` marker instead of silently omitting
    # hardlinks past the cap.
    dump_overflow: dict = {}

    try:
        # hr-log-04: open the hashes dump BEFORE the walk so it always exists
        # even on a Ctrl-C during the walk. hr-rob-02: write each stage-1 head
        # line immediately, then patch the fixed-width digest field if stage 2
        # upgrades it to a composite head:tail digest.
        skip_ino = None
        try:
            writer = HashDumpWriter(_open_hash_dump(args.hashes_file))
            hashes_state["writer"] = writer
            dump_stat = os.fstat(writer.fileno())
            skip_ino = (dump_stat.st_dev, dump_stat.st_ino)
        except OSError as exc:
            _log_line(f"WARNING: cannot write {args.hashes_file}: {exc}", False)
        # hr-obs-02 + hr-scal-05: stream the walk so we never materialise
        # the full `files` list. `on_walk_done` snaps the walk/hash
        # boundary so the per-stage durations remain meaningful.
        # hr-log-01: start marker before any scanning begins.
        _log_line(f"start: scanning {root} (jobs={args.jobs})", args.quiet)
        walk_iter = iter_threaded_walk(
            root, args.jobs, cancel_event=cancel_event, skip_ino=skip_ino)
        t_start = time.perf_counter()
        walk_boundary: list[float | None] = [None]

        def _mark_walk_done():
            walk_boundary[0] = time.perf_counter()

        def _disable_hash_dump(exc):
            writer = hashes_state["writer"]
            hashes_state["writer"] = None
            _log_line(f"WARNING: writing {args.hashes_file} failed: {exc}", False)
            if writer is not None:
                try:
                    writer.close()
                except OSError:
                    pass

        # hr-scal-02: stream groups straight to stdout so the result set
        # never buffers in memory. Pipeline-internal `final_groups` stays
        # empty because the callback consumes every group inline.
        on_group, totals = emit_groups_streaming(sys.stdout.write, config=config)
        progress_state: dict = {}

        def _on_hashed(done, total, head, key, aliases):
            # hr-log-02: record every hashed file for the dump. Live progress is
            # emitted by `_on_stage_progress` as each hash batch completes.
            writer = hashes_state["writer"]
            if head is not None and writer is not None:
                try:
                    writer.write_head(key, head, aliases.get(key, ()))
                except OSError as exc:
                    _disable_hash_dump(exc)

        def _on_composite(key, composite):
            # hr-obs-02: upgrade the stage-1 head digest to the composite
            # head:tail once stage 2 has resolved the tail for this key.
            writer = hashes_state["writer"]
            if writer is not None:
                try:
                    writer.patch_composite(key, composite)
                except OSError as exc:
                    _disable_hash_dump(exc)

        def _on_stage_progress(stage, done, total):
            state = progress_state.setdefault(
                stage, {"started": time.monotonic(), "last": 0})
            if done != total and done - state["last"] < LOG_EVERY_N_FILES:
                return
            state["last"] = done
            elapsed = time.monotonic() - state["started"]
            pct = (done * 100) // total if total else 100
            _log_line(
                f"hashing {stage} {pct}% ({done}/{total}) "
                f"elapsed={_fmt_elapsed(elapsed)} "
                f"rate={_fmt_rate(done, elapsed)}/s",
                args.quiet,
            )

        # hr-log-01: wrap the walk so a progress line prints every N files;
        # find_duplicate_groups still consumes the stream exactly once.
        result = find_duplicate_groups(
            _progress_walk(walk_iter, args.quiet), args.jobs,
            on_group=on_group, config=config, on_walk_done=_mark_walk_done,
            cancel_event=cancel_event, on_hashed=_on_hashed,
            on_stage_progress=_on_stage_progress, on_composite=_on_composite,
        )
        if result.overflow:
            dump_overflow.update(result.overflow)
        t_end = time.perf_counter()
        walk_stats = walk_iter.stats
        info = result.info
        dup_groups, dup_paths = totals()
        # hr-log-01: done marker once the pipeline has finished.
        _log_line(
            f"done: {walk_stats['files']} files scanned, "
            f"{dup_groups} duplicate group(s)", args.quiet)
        # hr-rel-03: `_mark_walk_done` is invoked unconditionally by
        # `find_duplicate_groups` the moment `index_inodes` has consumed
        # the walk — even an immediate-cancel walk still returns an empty
        # iterator that index_inodes drains. So once this line is reached
        # (a successful return), `walk_boundary[0]` is always set; the old
        # `is None` fallback could only have fired had the function raised
        # before returning, in which case this code never runs at all. The
        # dead guard + its misleading comment are gone.
        walk_end = walk_boundary[0]
        assert walk_end is not None  # set by _mark_walk_done before any return
        walk_seconds = walk_end - t_start
        hash_seconds = t_end - walk_end

        # hr-scal-04: one-shot warning so the operator knows printed alias
        # lists may be partial.
        if config.alias_cap_hits > 0 and not args.quiet:
            _log_line(
                f"WARNING: alias cap ({config.alias_cap}) truncated "
                f"{config.alias_cap_hits} group(s); rerun with "
                f"--alias-cap=0 to print every hardlink.",
                False,
            )

        # hr-obs-03: one-shot summary of suppressed hash errors instead of
        # per-file stderr spam.
        if config.hash_error_suppressed > 0 and not args.quiet:
            _log_line(
                f"WARNING: {config.hash_error_suppressed} additional "
                f"hash error(s) suppressed (showed first "
                f"{config.hash_error_logged}); rerun with a larger "
                "--hash-error-verbose-cap to see more.",
                False,
            )

        # hr-conc-05: surface partial results when the user hit Ctrl-C.
        if cancel_event.is_set() and not args.quiet:
            _log_line("WARNING: cancelled by SIGINT; results are partial.",
                      False)

        # ---- Summary on stderr (the user's safety net) ----
        if not args.quiet:
            # hr-obs-01: abbreviate big counts so the line stays readable.
            f = _fmt_count
            # hr-obs-02: the per-stage `*_errors` totals count EVERY None
            # digest, including benign vanished (ENOENT/ESTALE) and shrank
            # (truncated mid-run) skips. Subtract those subsets so the
            # printed `hash_errors` is the count of REAL failures worth
            # investigating (EACCES/EIO/...), matching the counter's
            # documented contract. Clamp at 0 in case of any miscount.
            total_hash_errors = info["stage1_errors"] + info["stage2_errors"]
            real_hash_errors = max(
                0,
                total_hash_errors
                - config.hash_skipped_vanished
                - config.hash_skipped_shrank,
            )
            _log_line(
                f"dirs={f(walk_stats['dirs'])} "
                f"files={f(walk_stats['files'])} "
                f"inodes={f(info['inodes'])} "
                f"size_collision_inodes={f(info['candidates'])} "
                f"hashed_stage1={f(info['stage1'])} "
                f"hashed_stage2={f(info['stage2'])} "
                f"dup_groups={f(dup_groups)} "
                f"dup_paths={f(dup_paths)} "
                f"walk_errors={f(walk_stats['dir_errors'])}+"
                f"{f(walk_stats['entry_errors'])} "
                f"hash_errors={f(real_hash_errors)} "
                f"hash_skipped={f(config.hash_skipped_vanished)} "
                f"hash_shrank={f(config.hash_skipped_shrank)} "
                f"walk_s={walk_seconds:.2f} hash_s={hash_seconds:.2f}",
                False,
            )
    finally:
        # hr-log-02 / hr-log-03: flush + close the hashes dump if it was
        # opened. This finally runs on a normal return AND on a Ctrl-C
        # KeyboardInterrupt (a second Ctrl-C, after the cooperative cancel),
        # so the file is always closed. The close is guarded so a flush
        # error can't skip the SIGINT-handler restore below.
        if hashes_state["writer"] is not None:
            try:
                # hr-obs-10: surface hardlinks elided at ingest by the alias
                # cap instead of silently omitting them from the dump.
                hashes_state["writer"].write_overflow(dump_overflow)
            except OSError as exc:
                _log_line(f"WARNING: writing {args.hashes_file} failed: {exc}",
                          False)
            finally:
                # hr-rob-01: a second Ctrl-C landing inside the write loop above
                # raises KeyboardInterrupt (not OSError); without this finally
                # the close would be skipped and the fd leaked. Close here so
                # the fd is released on every exit path.
                try:
                    hashes_state["writer"].close()
                except OSError as exc:
                    _log_line(
                        f"WARNING: closing {args.hashes_file} failed: {exc}",
                        False)
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
