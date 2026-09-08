#!/usr/bin/env python3
"""Relocate a (hidden) directory to another filesystem and replace it with a symlink.

Typical use: move heavyweight cache/state directories off the home volume.

Example:
    sudo python relocate_folder.py ~/.cache /backups/disk1/apps/profile
    # result: ~/.cache -> /backups/disk1/apps/profile/.cache (symlink)

What it does
------------
1. If <source> is already a symlink to <dest_root>/<basename>, exit (idempotent).
2. Copy <source> recursively to <dest_root>/<basename> preserving mode, times,
   symlinks, and ownership (when run as root).
3. Verify the copy (file/dir/symlink set, sizes, and per-file SHA-256 by
   default — the source is deleted afterwards; --no-checksum for size-only).
4. Atomically rename <source> aside, create symlink <source> -> <target>,
   delete the renamed-aside original.

Environment
-----------
- RELOCATE_STALE_PID_WARN_AT: stale-PID count at which the open-file precheck
  warns that its snapshot is not authoritative (default 1, i.e. warn on any).
  Raise it on noisy hosts where short-lived processes churn.
- RELOCATE_DISK_SPACE_HEADROOM: how much destination free space the pre-flight
  check demands relative to the payload (default 1.05). Below 1.0 clamps to 1.0.
- RELOCATE_SHA256_RETRY_ATTEMPTS: verify-hash attempts per file when the read
  hits a transient truncation (default 2). Clamped to at least 1.
An unparseable value falls back to the default in all three cases.

Caveats
-------
- POSIX-only, by design: ownership preservation (os.chown), the symlink swap,
  the O_NOFOLLOW|O_DIRECTORY anti-race open, and the /proc open-file precheck
  have no Windows equivalent. On a host without them the script refuses to run
  and exits 2 rather than failing partway through a migration.
- Processes that already have files open from <source> keep using the old
  inodes via their open file handles (standard unix semantics). New opens
  use the new location through the symlink. Stop the affected programs
  first if you want a fully clean handover.
- Cross-filesystem ownership preservation requires CAP_CHOWN (typically root).
- Non-regular files (Unix sockets, FIFOs, block/char devices) are skipped by
  default — they're kernel objects, not data, and copying them is impossible
  (and meaningless). Use --strict to fail instead of skipping.
"""
from __future__ import annotations

import argparse
import enum
import errno
import hashlib
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
import contextvars
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, Sequence

LOG = logging.getLogger("relocate")

# rf-decl-01: contextvar-backed logger injection so unit tests can capture
# logs without monkeypatching the module-global. Defaults to LOG; override
# inside `with_logger(...)`.
_log_ctx: ContextVar[logging.Logger] = ContextVar("relocate_log", default=LOG)


def _log() -> logging.Logger:
    return _log_ctx.get()


@contextmanager
def with_logger(log: logging.Logger) -> Iterator[logging.Logger]:
    """Temporarily replace the logger seen by every module function."""
    token = _log_ctx.set(log)
    try:
        yield log
    finally:
        _log_ctx.reset(token)


# rf-test-01: contextvar-backed destination hash injection. `_verify_pinned_file` calls
# `_hash()` (the active hash function) rather than `_sha256` directly so a
# test can substitute a hash that blocks a worker mid-stream and exercise the
# verify-pool race/leak paths (e.g. proving rmtree waits for a still-running
# hash). Defaults to `_sha256`; override inside `with_hash_fn(...)`.
_hash_ctx: "ContextVar[Callable[[Path], str]]" = ContextVar("relocate_hash")


def _hash() -> "Callable[[Path], str]":
    return _hash_ctx.get(_sha256)


@contextmanager
def with_hash_fn(fn: "Callable[[Path], str]") -> "Iterator[Callable[[Path], str]]":
    """Temporarily replace the hash function used by content verification."""
    token = _hash_ctx.set(fn)
    try:
        yield fn
    finally:
        _hash_ctx.reset(token)


BACKUP_SUFFIX = ".relocate-backup"
FAILED_MESSAGE = "FAILED: %s"
STAGING_PREFIX = ".relocate-stage-"
STAGING_PID_FILE = ".owner-pid"
_CHOWN_WARNING_LIMIT = 10
_STALL_WARN_SECONDS = 60.0


# rf-ddd-01: MigrationState makes the implicit lifecycle explicit. The
# orchestrator hands one of these back so callers (logs, tests, recovery
# tooling) can branch on a stable enum instead of parsing log strings.
class MigrationState(enum.Enum):
    ALREADY_MIGRATED = "already_migrated"
    DRY_RUN = "dry_run"
    COPIED = "copied"
    VERIFIED = "verified"
    SWAPPED = "swapped"
    FAILED = "failed"


# --- small shared helpers ---------------------------------------------------

def _path_taken(p: Path) -> bool:
    """True if `p` exists in any form (regular, dir, dangling symlink) (rf-dup-02).

    `path.exists()` returns False for dangling symlinks, so a separate
    `is_symlink()` arm is needed. One lstat is also cheaper than two stat
    syscalls when the path is unlikely to exist."""
    try:
        os.lstat(p)
        return True
    except OSError:
        return False


class _OperationStallWatchdog:
    """Warn when a filesystem operation goes idle for too long."""

    def __init__(
        self,
        operation: str,
        *,
        warning_after: float = _STALL_WARN_SECONDS,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._operation = operation
        self._warning_after = warning_after
        self._now = now_fn
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_progress = self._now()
        self._last_warning = self._last_progress
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_OperationStallWatchdog":
        self.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        self.touch()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def touch(self, operation: str | None = None) -> None:
        now = self._now()
        with self._lock:
            if operation is not None:
                self._operation = operation
            self._last_progress = now
            self._last_warning = now

    def _run(self) -> None:
        interval = max(1.0, min(self._warning_after / 4.0, 10.0))
        while not self._stop.wait(interval):
            self._maybe_warn()

    def _maybe_warn(self) -> None:
        now = self._now()
        with self._lock:
            idle = now - self._last_progress
            recently_warned = now - self._last_warning < self._warning_after
            if idle < self._warning_after or recently_warned:
                return
            self._last_warning = now
            operation = self._operation
        _log().warning(
            "%s stalled: no progress for %.0fs; filesystem I/O may be blocked",
            operation,
            idle,
        )


def _swallow_or_warn(label: str, fn: Callable, *args, **kwargs):
    """Best-effort wrapper (rf-dup-03 / rf-arch-08): run `fn(*args, **kwargs)`;
    on `PermissionError` / `OSError` log a warning and return None. Use
    ONLY for cleanup-style operations (rmtree on rollback, unlink of a
    staging file) where swallowing the error is the right policy.

    The name encodes the contract — failure is logged and absorbed. For
    operations whose failure should abort the migration (chown / chmod
    during destination setup, where a botched perm silently leaves the
    dest with the wrong owner and the user only finds out at first
    access), use :func:`_raise_or_fail` instead so the error propagates
    with a clear message (rf-rel-09)."""
    try:
        return fn(*args, **kwargs)
    except OSError as exc:
        _log().warning("could not %s: %s", label, exc)
        return None


def _raise_or_fail(label: str, fn: Callable, *args, **kwargs):
    """Required-success wrapper (rf-rel-09 / rf-arch-08): run
    `fn(*args, **kwargs)` and re-raise any `PermissionError` / `OSError`
    as `RuntimeError` with a typed label so the caller's stack trace
    points at the failed setup step instead of the bare syscall site.

    Companion to :func:`_swallow_or_warn`. The verb-based names make the
    contract visible at every call site — a future contributor can no
    longer reach for "safe" thinking it means "doesn't crash"."""
    try:
        return fn(*args, **kwargs)
    except OSError as exc:
        raise RuntimeError(f"required {label} failed: {exc}") from exc


@dataclass(frozen=True)
class Plan:
    source: Path
    target: Path
    dry_run: bool = False
    verify: bool = True
    checksum: bool = True  # content-verify by default; the source is deleted after (rf-rel-01)
    strict: bool = False  # if True, fail on special files instead of skipping
    force: bool = False  # if True, skip the open-files pre-flight check
    verify_ownership: bool = False  # rf-rel-04: also compare mode/uid/gid in verify_copy
    strict_cross_device: bool = False  # rf-sec-03: refuse same-fs migrations (default warn)
    jobs: int | None = None  # rf-scal-02: pool width override; None = _default_worker_count()
    check_space: bool = True  # rf-perf-02: pre-copy disk-space walk; False skips it
    progress: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Plan":
        """Build (and validate) a Plan from parsed CLI args (rf-arch-01/02).

        Path normalisation lives here rather than in `parse_args` so that
        unit tests can drive `Plan.from_args` with a synthesised Namespace
        without invoking argparse."""
        # absolute() (not resolve()) for source: we must NOT follow the symlink
        # if we already migrated, otherwise the idempotency check breaks.
        src = Path(args.source).expanduser().absolute()
        dst_root = Path(args.dest_root).expanduser().resolve()
        # rf-rel-30: a root-shaped source ("/" or a trailing-slash root) has an
        # empty basename, so `dst_root / src.name == dst_root` and the src==target
        # guard below would not fire — the run would proceed on a root path.
        if not src.name:
            raise ValueError(f"source has no basename to relocate: {src}")
        target = dst_root / src.name
        canonical_source = _canonical_path_location(src)
        canonical_target = _canonical_path_location(target)
        if canonical_source == canonical_target:
            raise ValueError(f"source equals computed target: {src}")
        if _paths_overlap(canonical_source, canonical_target):
            raise ValueError(
                "source and computed target overlap: "
                f"{canonical_source} <-> {canonical_target}"
            )
        return cls(
            source=src,
            target=target,
            dry_run=getattr(args, "dry_run", False),
            verify=not getattr(args, "no_verify", False),
            checksum=not getattr(args, "no_checksum", False),
            strict=getattr(args, "strict", False),
            force=getattr(args, "force", False),
            verify_ownership=getattr(args, "verify_ownership", False),
            strict_cross_device=getattr(args, "strict_cross_device", False),
            jobs=getattr(args, "jobs", None),
            check_space=not getattr(args, "no_space_check", False),
            progress=getattr(args, "progress", False),
        )


def _canonical_path_location(path: Path) -> Path:
    """Resolve parent aliases without following the final path component."""
    return path.parent.resolve(strict=False) / path.name


def _paths_overlap(first: Path, second: Path) -> bool:
    return first in second.parents or second in first.parents


# --- validation -------------------------------------------------------------

def validate_source(source: Path) -> None:
    if source.is_symlink():
        raise ValueError(
            f"source is already a symlink: {source} -> {os.readlink(source)}"
        )
    if not source.exists():
        raise FileNotFoundError(f"source does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"source is not a directory: {source}")


def _source_identity_fd(source: Path) -> tuple[int, tuple[int, int]]:
    """Open ``source`` as a directory WITHOUT following a final symlink
    (rf-sec-01), returning ``(fd, (st_dev, st_ino))``.

    ``O_NOFOLLOW`` makes the open fail (ELOOP) if ``source`` was swapped for a
    symlink, and ``O_DIRECTORY`` (where available) fails (ENOTDIR) if it is no
    longer a directory —
    closing the swap-for-symlink TOCTOU at open time. The caller holds the fd
    open across the migration (pinning the inode) and re-checks the path's
    identity against this fstat right before copying via
    :func:`_assert_source_identity`."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise
    return fd, (st.st_dev, st.st_ino)


def _assert_source_identity(source: Path, expected: tuple[int, int]) -> None:
    """Raise when ``source``'s current lstat identity differs from ``expected``
    (rf-sec-01) — i.e. the path was swapped since it was opened."""
    st = os.lstat(source)
    if (st.st_dev, st.st_ino) != expected:
        raise RuntimeError(
            f"source {source} was replaced during the migration "
            f"(inode/device changed); aborting to avoid acting on a swapped path")


def ensure_dest_root(dest_root: Path, source: Path) -> list[Path]:
    """Create dest_root if missing; new dirs get source's uid/gid and mode 0o755.

    Existing path components are NEVER modified. If any existing ancestor would
    block the source's owner from traversing it, a warning is logged. Returns
    the list of directories actually created (outermost-first) so a failed
    migration can unwind them (rf-state-01).
    """
    if dest_root.exists() and not dest_root.is_dir():
        raise NotADirectoryError(
            f"dest root exists but is not a directory: {dest_root}"
        )
    created = _create_missing_dirs(dest_root, source)
    _warn_if_not_traversable(dest_root, source)
    return created


def _cleanup_created_dirs(created: list[Path]) -> None:
    """rf-state-01: remove the destination ancestor dirs created by
    ensure_dest_root when a migration fails after they were made, so a failed
    run doesn't leave empty dirs stranded on the dest volume. Innermost-first;
    stop at the first dir that won't rmdir (non-empty or already gone)."""
    for path in reversed(created):
        try:
            os.rmdir(path)
        except OSError:
            break


def _create_missing_dirs(dest_root: Path, source: Path) -> list[Path]:
    """mkdir dest_root and any missing ancestors with a secure sequence
    (rf-sec-02): mkdir restrictive -> chown to source owner -> chmod relax.

    The TOCTOU window between mkdir(0o755) and chown that older code had on a
    world-writable parent is closed: mkdir(0o700) means no other user can
    enter or write the new directory before chown completes."""
    missing: list[Path] = []
    cur = dest_root
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    created = list(reversed(missing))
    if not created:
        return []
    st = source.stat()
    made: list[Path] = []
    try:
        for path in created:
            os.mkdir(path, mode=0o700)
            made.append(path)
            # rf-rel-09: ownership and permissions are required setup steps.
            _raise_or_fail(
                f"chown {path} to uid={st.st_uid} gid={st.st_gid}",
                os.chown, path, st.st_uid, st.st_gid,
            )
            _raise_or_fail(f"chmod {path} to 0o755", os.chmod, path, 0o755)
    except BaseException:
        _cleanup_created_dirs(made)
        raise
    return created


def _warn_if_not_traversable(dest_root: Path, source: Path) -> None:
    # rf-robust-05: this is an advisory check; a stat that fails (source/ancestor
    # removed or permission revoked mid-walk) must never abort the migration, so
    # every syscall here is guarded and any failure just ends the advisory walk.
    try:
        st = source.stat()
    except OSError:
        return
    cur = dest_root
    while cur != cur.parent:
        try:
            traversable = _can_traverse(cur, st.st_uid, st.st_gid)
        except OSError:
            return
        if not traversable:
            try:
                cur_st = cur.stat()
            except OSError:
                return
            _log().warning(
                "path %s may not be traversable by uid=%d "
                "(mode=%s, owner_uid=%d); note: supplementary-group "
                "membership is not checked, so this may be a false positive",
                cur, st.st_uid, oct(cur_st.st_mode & 0o777), cur_st.st_uid,
            )
            return
        cur = cur.parent


def _can_traverse(path: Path, uid: int, gid: int) -> bool:
    """Best-effort traversability check for ``uid``/``gid`` over ``path``.

    rf-ux-01: this only considers the source's PRIMARY gid. A uid whose execute
    access actually comes from a supplementary group (group-exec on a group the
    uid belongs to but which isn't its primary gid) is reported as
    non-traversable, producing a false warning. We don't resolve the uid's full
    group list (its membership on this host may differ from the source host), so
    the caller's warning is explicitly advisory — see its message."""
    if uid == 0:
        return True
    st = path.stat()
    mode = st.st_mode
    if uid == st.st_uid:
        return bool(mode & 0o100)
    if gid == st.st_gid:
        return bool(mode & 0o010)
    return bool(mode & 0o001)


def _resolves_to(source: Path, target: Path) -> bool:
    """True when `source` is a symlink whose target (relative links resolved
    against the link's parent) resolves to the same path as `target`.

    rf-dup-04: shared preamble for `already_migrated` and
    `_symlink_points_at_empty_target` so the two symlink probes can't drift.
    `resolve()` may raise OSError/PermissionError/RuntimeError (symlink loops,
    EACCES); the caller wraps this so it can react to each case."""
    if not source.is_symlink():
        return False
    link_target = Path(os.readlink(source))
    if not link_target.is_absolute():
        link_target = source.parent / link_target
    return link_target.resolve(strict=False) == target.resolve(strict=False)


def already_migrated(source: Path, target: Path) -> bool:
    try:
        if not _resolves_to(source, target):
            return False
        # rf-rel-04: a stale or hand-made symlink can resolve equal to the
        # computed target while the target itself is missing or empty. Treating
        # that as "migrated" would skip the copy and silently strand the source
        # data. Only declare migration done when the target exists and holds
        # content.
        has_content = _target_has_content(target)
        if has_content:
            # rf-rel-03: the skip is decided on content PRESENCE, not a
            # source-vs-target comparison — a partial/divergent prior target
            # still counts as migrated. Log the decision so an operator can
            # audit a skip that may have stranded or diverged data.
            _log().info(
                "already migrated: %s -> %s (target non-empty; copy skipped on "
                "content presence, not a content comparison)", source, target)
        return has_content
    except PermissionError as exc:
        # rf-rel-10: silently returning False on EACCES would let a
        # misconfigured (chmod 0o000) target look like "not migrated"
        # and re-trigger the whole copy. Warn so the operator notices
        # the access problem before the copy retries.
        _log().warning(
            "already_migrated probe: cannot resolve %s or %s (%s); "
            "treating as not-migrated but the target may be inaccessible",
            source, target, exc,
        )
        return False
    except (OSError, RuntimeError):
        # `Path.resolve` raises RuntimeError on symlink loops (rf-test-02);
        # treat the loop as "not migrated" so the caller can validate the
        # source and fail with a typed error instead of crashing.
        return False


def _symlink_points_at_empty_target(source: Path, target: Path) -> bool:
    """True when `source` is a symlink that resolves to the computed `target`
    and `target` is an existing-but-empty directory (rf-rel-01).

    This is the regression shape from rf-rel-04: a prior migration completed,
    the symlink is correct, but the target was later emptied. `already_migrated`
    returns False for it (no content), and the old code then crashed in
    `validate_source` with "source is already a symlink". The orchestrator uses
    this predicate to emit a distinct "already migrated (empty target)" skip
    instead of crashing on a legitimately-migrated directory."""
    try:
        if not _resolves_to(source, target):
            return False
        if not target.is_dir() or target.is_symlink():
            return False
        with os.scandir(target) as it:
            return not any(True for _ in it)
    except (OSError, RuntimeError):
        return False


def _target_has_content(target: Path) -> bool:
    """True iff `target` is an existing directory with at least one entry
    (rf-rel-04).

    A non-existent target, a non-directory, or an empty directory all mean the
    symlink is stale/hand-made rather than the product of a real migration, so
    the copy must still run."""
    try:
        if not target.is_dir() or target.is_symlink():
            return False
        with os.scandir(target) as it:
            return any(True for _ in it)
    except OSError:
        return False


# --- pre-flight: detect processes holding files open under source -----------

@dataclass(frozen=True)
class OpenFileSnapshot:
    """Result of :func:`find_open_file_holders` (rf-rel-15 / rf-arch-09
    / rf-rel-18).

    `holders` is a TUPLE of `(pid, comm, paths_tuple)` triples seen
    during the scan. `stale_pids` counts the number of `/proc/<pid>`
    entries that disappeared mid-iteration — when > 0 the snapshot is
    partial and the caller should treat the result as a soft warning
    instead of an authoritative all-clear.

    rf-arch-09: this used to be a `NamedTuple` with `__iter__` /
    `__len__` / `__bool__` overrides that aliased the iterator
    semantics to the inner `holders` list. That was convenient for
    ``for h in snap`` / ``if snap`` callers but broke the NamedTuple
    expectation that iteration yields the field tuple. The dataclass
    form makes the access surface unambiguous: ``snap.holders`` for
    the sequence, ``snap.stale_pids`` for the count.

    rf-rel-18: `holders` is now a `tuple` (was `list`) and the inner
    per-process `paths` is also a `tuple`. Frozen-only at the dataclass
    level wasn't enough — a caller could `snap.holders.append(...)` and
    mutate the snapshot post-hoc, defeating both the audit guarantee
    and the auto-generated `__hash__` / `__eq__`. Tuples make the
    snapshot fully immutable so it's safe to memoise or compare across
    repeat scans. :func:`find_open_file_holders` does the conversion
    at construction time."""

    holders: tuple[tuple[int, str, tuple[Path, ...]], ...]
    stale_pids: int


def find_open_file_holders(source: Path) -> OpenFileSnapshot:
    """Return :class:`OpenFileSnapshot` for processes holding a
    regular-file FD inside `source`. Linux only; returns an empty
    snapshot elsewhere.

    Without root, only the current user's processes are visible — that's
    exactly what matters for ~/.cache anyway.

    LIMITATION (rf-rel-06): this is a SNAPSHOT at call time. A process that
    starts after this scan and opens a file before `atomic_swap` runs is
    invisible to the precheck — its open FDs will point at the old (deleted)
    inode after the swap and continue working via unix semantics, but it
    won't see future writes that route through the new symlink. If you're
    migrating a directory of an actively-used service, stop the service
    first or accept that newly-spawned processes during the copy may need
    a restart afterwards.

    rf-rel-15: each `/proc/<pid>` entry that disappears between
    ``proc_dir.iterdir()`` and the per-pid read increments
    ``stale_pids``. The result shape now surfaces this so callers can
    warn the operator instead of trusting a possibly-partial snapshot.
    """
    with _OperationStallWatchdog("open-file precheck") as watchdog:
        proc_dir = Path("/proc")
        if not proc_dir.is_dir():
            return OpenFileSnapshot(holders=(), stale_pids=0)
        src_resolved = source.resolve()
        watchdog.touch("open-file precheck resolve")
        results: list[tuple[int, str, tuple[Path, ...]]] = []
        stale = 0
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            files = _process_open_files_in(entry, src_resolved)
            watchdog.touch("open-file precheck scan")
            if files is None:
                # rf-rel-15: process exited mid-scan — snapshot is partial.
                stale += 1
                continue
            if files:
                # rf-rel-18: tuple-of-tuples so the snapshot is fully
                # immutable. Caller can still iterate / index normally;
                # mutation via `snap.holders.append(...)` is no longer
                # possible.
                results.append((pid, _read_comm(pid), tuple(files)))
        return OpenFileSnapshot(holders=tuple(results), stale_pids=stale)


def _process_open_files_in(proc_entry: Path, root: Path) -> "list[Path] | None":
    """Return open paths under `root`, or None if the process exited
    mid-scan (rf-rel-15).

    `None` is reserved for the "vanished" signal so the caller can
    tally `stale_pids` and warn on a partial snapshot; the empty list
    means "process is alive but has no matching FDs"."""
    out: list[Path] = []
    try:
        fds = list((proc_entry / "fd").iterdir())
    except FileNotFoundError:
        return None
    except PermissionError:
        return out
    for fd in fds:
        target = _read_fd_target(fd)
        if target is not None and _is_under(target, root):
            out.append(target)
    return out


def _read_fd_target(fd: Path) -> Path | None:
    """Read an /proc/<pid>/fd/N symlink. Filter sockets/pipes/deleted files."""
    try:
        target_str = os.readlink(fd)
    except OSError:
        return None
    if target_str.endswith(" (deleted)"):
        return None
    if not target_str.startswith("/"):
        return None  # e.g. socket:[12345], pipe:[678] — not real files
    return Path(target_str)


def _is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _read_comm(pid: int) -> str:
    # rf-plat-02: `comm` is arbitrary bytes — any process can set its own with
    # prctl(PR_SET_NAME) — so a strict locale decode raises UnicodeDecodeError,
    # a ValueError this OSError-only guard does not catch. It would escape into
    # _check_no_open_files and abort an otherwise-valid migration because some
    # unrelated process has an odd name.
    try:
        raw = Path(f"/proc/{pid}/comm").read_bytes()
    except OSError:
        return "?"
    return raw.decode("utf-8", "replace").strip()


def _stale_pid_warn_threshold() -> int:
    """Return the stale_pids threshold for the open-file precheck
    warning (rf-obs-02).

    Read from ``RELOCATE_STALE_PID_WARN_AT`` (env var). Default ``1``
    preserves the original "warn on any" behaviour. Operators on noisy
    hosts (CI runners, churning short-lived processes) can set
    ``RELOCATE_STALE_PID_WARN_AT=10`` (or higher) to mute the warning
    until the count crosses a meaningful boundary. Invalid values fall
    back to the default."""
    raw = os.environ.get("RELOCATE_STALE_PID_WARN_AT", "1")
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def _check_no_open_files(source: Path) -> None:
    snapshot = find_open_file_holders(source)
    if snapshot.stale_pids >= _stale_pid_warn_threshold():
        # rf-rel-15 / rf-obs-02: snapshot is partial — warn so the
        # operator knows the precheck isn't authoritative even if it
        # reports zero holders. Don't refuse on stale_pids alone; a
        # noisy host would otherwise block every migration. The
        # threshold is env-tunable so chatty hosts can mute the noise
        # without losing the signal when something interesting happens.
        _log().warning(
            "open-file precheck saw %d process(es) exit mid-scan; "
            "result may be incomplete", snapshot.stale_pids,
        )
    if not snapshot.holders:
        return
    raise RuntimeError(_format_open_file_warning(snapshot.holders, source))


def _format_open_file_warning(
    holders: "Sequence[tuple[int, str, Sequence[Path]]]", source: Path,
) -> str:
    lines = [
        f"refusing to migrate: {len(holders)} process(es) have files "
        f"open under {source}:"
    ]
    for pid, comm, files in holders[:10]:
        lines.append(f"  pid={pid:<7} {comm:<20} ({len(files)} file(s) open)")
    remaining = len(holders) - 10
    if remaining > 0:
        lines.append(f"  ... and {remaining} more process(es)")
    lines.append(
        "Close these applications first (especially browsers / Electron apps), "
        "or pass --force to proceed anyway. Without --force this check exists "
        "to spare you a long copy that fails at verification."
    )
    return "\n".join(lines)


# --- copy + verify ----------------------------------------------------------

def _rmtree_logging(path: Path, context: str) -> None:
    """Best-effort `rmtree(path)` that collects per-entry OSError failures and
    logs them instead of silently dropping them (rf-obs-02). `context` names the
    failing operation for the log lines. The caller re-raises the original
    exception; this only cleans up and records what it could not remove.

    rf-dup-05: shared by the copy_tree and _copy_and_verify cleanup paths so the
    collector + capped warnings live in one place."""
    failures: list[tuple[str, OSError]] = []

    def _record(_fn, entry, excinfo):
        exc = excinfo[1] if isinstance(excinfo, tuple) else excinfo
        if isinstance(exc, OSError):   # pragma: no branch - rmtree passes OSError-shaped excinfo
            failures.append((str(entry), exc))

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_record)
    else:
        shutil.rmtree(path, onerror=_record)
    for entry, exc in failures[:10]:
        _log().warning("cleanup after %s: could not remove %s: %s", context, entry, exc)
    if len(failures) > 10:
        _log().warning(
            "cleanup: %d more removal errors suppressed; manual cleanup of %s "
            "may be needed", len(failures) - 10, path)
    elif failures:
        _log().warning(
            "partial target %s may still exist after a failed cleanup; manual "
            "removal may be needed before re-running", path)


@dataclass
class _CopyTargetOwner:
    path: Path
    descriptor: int = -1
    identity: tuple[int, int] | None = None

    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".relocate-copy-", dir=self.path.parent))
        try:
            # Pin the privately created inode before publishing its name.
            self.pin(staging)
            _rename_noreplace(staging, self.path)
        finally:
            if staging.exists():
                _swallow_or_warn(f"remove empty copy staging {staging}", staging.rmdir)

    def pin(self, path: Path) -> None:
        self.descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(self.descriptor)
        self.identity = (opened.st_dev, opened.st_ino)

    def matches(self, path: Path) -> bool:
        return self.identity is not None and _backup_identity_ok(path, self.identity)

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def cleanup(self, context: str) -> None:
        if not self.matches(self.path):
            return
        _log().warning("removing target %s after %s", self.path, context)
        quarantine = None
        try:
            quarantine = Path(tempfile.mkdtemp(
                prefix=".relocate-cleanup-", dir=self.path.parent))
            held = quarantine / "target"
            _rename_noreplace(self.path, held)
            _dispose_quarantined_copy(self, held, context)
        except (OSError, RuntimeError) as exc:
            _log().warning("cleanup after %s failed: %s", context, exc)
        finally:
            if quarantine is not None:
                _swallow_or_warn(f"remove cleanup staging {quarantine}", quarantine.rmdir)


def _dispose_quarantined_copy(owner: _CopyTargetOwner, held: Path, context: str) -> None:
    if owner.matches(held):
        _rmtree_logging(held, context)
        return
    # A replacement can win between the precheck and rename. Restore it;
    # an occupied original name leaves the replacement safely quarantined.
    try:
        _rename_noreplace(held, owner.path)
    except (OSError, RuntimeError) as exc:
        _log().error("replacement preserved at %s; restore to %s failed: %s",
                     held, owner.path, exc)
    else:
        _log().warning("replacement restored at %s; refusing to remove it", owner.path)


@contextmanager
def _copy_target_owner(path: Path) -> Iterator[_CopyTargetOwner]:
    owner = _CopyTargetOwner(path)
    try:
        yield owner
    finally:
        owner.close()


PinnedEntry = tuple[Path, int, str, os.stat_result]


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def _walk_pinned_entries(source_fd: int) -> Iterator[PinnedEntry]:
    """Walk below ``source_fd`` without resolving the source pathname."""
    for root, dir_names, names, directory_fd in os.fwalk(
        ".", topdown=True, onerror=_raise_walk_error,
        follow_symlinks=False, dir_fd=source_fd,
    ):
        relative_root = Path(root).relative_to(".")
        for name in (*dir_names, *names):
            entry_stat = os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False)
            yield relative_root / name, directory_fd, name, entry_stat


def _pinned_stat_snapshot(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_size,
        st.st_mtime_ns, st.st_ctime_ns,
    )


@contextmanager
def _open_pinned_file(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> Iterator[BinaryIO]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or _pinned_stat_snapshot(opened) != _pinned_stat_snapshot(expected)):
            raise RuntimeError(f"source entry changed while opening: {name}")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            yield stream
            final = os.fstat(stream.fileno())
            if _pinned_stat_snapshot(final) != _pinned_stat_snapshot(expected):
                raise RuntimeError(f"source entry changed while reading: {name}")
    finally:
        if fd >= 0:
            os.close(fd)


def _read_pinned_symlink(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> str:
    target = os.readlink(name, dir_fd=directory_fd)
    final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if _pinned_stat_snapshot(final) != _pinned_stat_snapshot(expected):
        raise RuntimeError(f"source symlink changed while reading: {name}")
    return target


def _apply_pinned_metadata(
    destination: Path,
    source_stat: os.stat_result,
    warning_limiter: "_ChownWarningLimiter",
    *,
    symlink: bool = False,
) -> None:
    try:
        os.chown(
            destination, source_stat.st_uid, source_stat.st_gid,
            follow_symlinks=False,
        )
    except OSError as exc:
        warning_limiter.warn(destination, exc)
    if not symlink or os.chmod in os.supports_follow_symlinks:
        os.chmod(
            destination, stat.S_IMODE(source_stat.st_mode),
            follow_symlinks=not symlink,
        )
    if not symlink or os.utime in os.supports_follow_symlinks:
        os.utime(
            destination,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=not symlink,
        )


def _copy_pinned_regular(
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
    destination: Path,
    warning_limiter: "_ChownWarningLimiter",
) -> None:
    with _open_pinned_file(directory_fd, name, source_stat) as source_stream:
        with open(destination, "xb") as target_stream:
            shutil.copyfileobj(source_stream, target_stream)
    _apply_pinned_metadata(destination, source_stat, warning_limiter)


def _pinned_size_totals(source_fd: int) -> tuple[int, int]:
    apparent = 0
    allocated = 0
    for _rel, _directory_fd, _name, entry_stat in _walk_pinned_entries(source_fd):
        if stat.S_ISREG(entry_stat.st_mode):
            apparent += entry_stat.st_size
            allocated += entry_stat.st_blocks * 512
    return apparent, allocated


def _refuse_pinned_specials(
    source_fd: int,
    source_label: Path,
    mode_cache: dict[Path, int],
) -> None:
    specials: list[Path] = []
    for rel, _directory_fd, _name, entry_stat in _walk_pinned_entries(source_fd):
        if _is_special_file(entry_stat.st_mode):
            display_path = source_label / rel
            mode_cache[display_path] = entry_stat.st_mode
            specials.append(display_path)
    _report_skipped(specials, strict=True, mode_cache=mode_cache)


def _copy_pinned_entry(
    source_label: Path,
    destination: Path,
    entry: PinnedEntry,
    directories: list[tuple[Path, os.stat_result]],
    skipped: list[Path],
    mode_cache: dict[Path, int],
    warning_limiter: "_ChownWarningLimiter",
) -> int | None:
    rel, directory_fd, name, entry_stat = entry
    target = destination / rel
    mode = entry_stat.st_mode
    if stat.S_ISDIR(mode):
        target.mkdir(mode=0o700)
        directories.append((target, entry_stat))
    elif stat.S_ISREG(mode):
        _copy_pinned_regular(
            directory_fd, name, entry_stat, target, warning_limiter)
        return entry_stat.st_size
    elif stat.S_ISLNK(mode):
        os.symlink(_read_pinned_symlink(
            directory_fd, name, entry_stat), target)
        _apply_pinned_metadata(
            target, entry_stat, warning_limiter, symlink=True)
    else:
        display_path = source_label / rel
        skipped.append(display_path)
        mode_cache[display_path] = mode
    return None


def _copy_tree_pinned(
    source_label: Path,
    source_fd: int,
    destination: Path,
    *,
    mode_cache: dict[Path, int],
    progress_cb: "Callable[[int, int], None] | None",
    check_space: bool,
    owner: _CopyTargetOwner,
) -> list[Path]:
    if _path_taken(destination):
        raise FileExistsError(
            f"target already exists: {destination}; if a previous run was killed "
            "during copy, this may be a stale partial target. Inspect and "
            "remove it manually before re-running."
        )
    apparent = allocated = 0
    if check_space or progress_cb is not None:
        apparent, allocated = _pinned_size_totals(source_fd)
    if check_space:
        _check_disk_space(source_label, destination, total_bytes=allocated)
    watchdog = _OperationStallWatchdog("copytree")
    try:
        owner.create()
        with watchdog:
            return _populate_pinned_copy(
                source_label, source_fd, destination, mode_cache,
                progress_cb, apparent, watchdog,
            )
    except BaseException:
        owner.cleanup("failed descriptor-pinned copy")
        raise


def _populate_pinned_copy(
    source_label: Path, source_fd: int, destination: Path,
    mode_cache: dict[Path, int], progress_cb: Callable[[int, int], None] | None,
    apparent: int, watchdog: _OperationStallWatchdog,
) -> list[Path]:
    skipped: list[Path] = []
    warning_limiter = _ChownWarningLimiter()
    directories = [(destination, os.fstat(source_fd))]
    copied = 0
    for entry in _walk_pinned_entries(source_fd):
        copied_size = _copy_pinned_entry(
            source_label, destination, entry, directories, skipped,
            mode_cache, warning_limiter,
        )
        if copied_size is not None:
            copied += copied_size
            if progress_cb is not None:
                progress_cb(copied, apparent)
        watchdog.touch("copytree")
    for target, entry_stat in reversed(directories):
        _apply_pinned_metadata(target, entry_stat, warning_limiter)
    warning_limiter.summarize()
    return skipped


def copy_tree(src: Path, dst: Path, *,
              source_fd: int,
              mode_cache: dict[Path, int] | None = None,
              progress_cb: Callable[[int, int], None] | None = None,
              check_space: bool = True,
              _target_owner: _CopyTargetOwner | None = None,
              ) -> list[Path]:
    """Copy through an open source directory descriptor, skipping special files.

    The caller owns ``source_fd`` and keeps it open until this call returns.
    ``src`` labels diagnostics and skipped entries; it is never reopened.
    ``mode_cache`` records skipped entry modes for reporting. Optional progress
    receives completed and total apparent file bytes; disabling both progress
    and the space precheck avoids the preliminary size walk.
    """
    ownership = (
        _copy_target_owner(dst) if _target_owner is None else nullcontext(_target_owner)
    )
    with ownership as owner:
        return _copy_tree_pinned(
            src, source_fd, dst,
            mode_cache=mode_cache if mode_cache is not None else {},
            progress_cb=progress_cb, check_space=check_space, owner=owner,
        )


# Headroom factor: filesystems need a little slack for metadata, journals, and
# block-size rounding. 5% over the raw payload is conservative enough to catch
# the "destination is exactly the right size minus a few KiB" trap.
_DISK_SPACE_HEADROOM = 1.05


def _disk_space_headroom() -> float:
    """Return the disk-space headroom factor (rf-adapt-01).

    Read from ``RELOCATE_DISK_SPACE_HEADROOM`` (env var). Default
    ``_DISK_SPACE_HEADROOM``. Values below 1.0 would demand LESS space
    than the payload itself, so they clamp to 1.0; invalid values fall
    back to the default."""
    raw = os.environ.get("RELOCATE_DISK_SPACE_HEADROOM", str(_DISK_SPACE_HEADROOM))
    try:
        value = float(raw)
    except ValueError:
        return _DISK_SPACE_HEADROOM
    return max(1.0, value)


def _check_disk_space(src: Path, dst: Path, *, total_bytes: int) -> None:
    """Check destination headroom using the caller's pinned source size walk.

    This is best effort: the source can grow after the precheck, and a copy
    that runs out of space must still clean up its owned target.
    """
    needed = total_bytes
    probe = dst
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return  # can't check; let the copy try
    required = int(needed * _disk_space_headroom())
    if free < required:
        raise RuntimeError(
            f"insufficient space on destination filesystem at {probe}: "
            f"need ~{required} bytes (incl. headroom for ~{needed} of payload), "
            f"have {free} free"
        )


def _is_special_file(mode: int) -> bool:
    # Single source of truth for the S_IS* classification (rf-dup-01): a
    # special file is any kind _kind_of names.
    return _kind_of(mode) is not None


def _kind_of(mode: int) -> "str | None":
    """The short name of the special-file kind for `mode`, or None for regular
    files / dirs / symlinks. Used both for the "is special?" predicate and
    the per-kind summary (rf-dup-01)."""
    for predicate, name in (
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISFIFO, "fifo"),
        (stat.S_ISBLK, "block-device"),
        (stat.S_ISCHR, "char-device"),
    ):
        if predicate(mode):
            return name
    return None


def _default_worker_count() -> int:
    """Default thread-pool width (rf-scal-02): `min(32, cpu_count + 4)`.

    Mirrors `ThreadPoolExecutor`'s own default heuristic but is exposed as
    a module-level helper so callers (the CLI `--jobs` knob, tests) can
    override via `Plan.jobs` and so verification uses
    one source of truth (rf-arch-06 unified the loop; this unifies the
    sizing)."""
    return min(32, (os.cpu_count() or 1) + 4)


def _positive_jobs(value: str) -> int:
    """argparse `type=` validator (rf-cli-01): reject `--jobs <= 0` with a clear
    CLI error instead of silently falling back to the default in _resolved_jobs
    (which let `-j 0`/`-j -4` run at the default while the user believed
    concurrency was constrained)."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--jobs must be an integer, got {value!r}")
    if iv < 1:
        raise argparse.ArgumentTypeError(f"--jobs must be >= 1, got {iv}")
    return iv


def _resolved_jobs(jobs: int | None) -> int:
    """Resolve a CLI/plan `jobs` value to a positive pool width (rf-scal-02)."""
    if jobs is not None and jobs > 0:
        return jobs
    return _default_worker_count()


def _inflight_cap(workers: int) -> int:
    """Bound on queued+running futures for a given pool width (rf-scal-03)."""
    return workers * 4


def _run_streamed(
    submit: Callable[..., "Future"],
    tasks: Iterable,
    max_inflight: int,
    on_done: Callable[["Future"], bool],
) -> None:
    """Drive `tasks` through `submit(task)` with a bounded inflight set
    (rf-arch-06). Used by `_run_verify_pool`.

    `on_done(fut)` is invoked for every completed Future and returns True
    to short-circuit the submission loop on the first verify error.
    Streaming preserves the
    millions-of-entries-without-OOM property (rf-scal-03).

    In-flight futures on abort (rf-conc-02)
    ---------------------------------------
    When `on_done` signals abort, the submission loop stops queueing NEW
    tasks immediately, but the trailing `for fut in inflight` drain still
    calls `on_done(fut)` — and `on_done` blocks on `fut.exception()` — for
    every future already submitted. A `Future` that is *running* (not just
    queued) cannot be cancelled in Python, so abort waits for up to
    `max_inflight` (≈ `workers`) of these in-flight tasks to finish.

    This blocking drain is DELIBERATE, not an oversight: the verify pool
    relies on it (together with `shutdown(wait=True)`) to guarantee no
    SHA-256 worker is still reading a file when the caller deletes the
    target tree (rf-conc-01 / rf-rel-03). The cost is bounded: at most
    `max_inflight` already-started tasks, never the unsubmitted tail. The
    only way to avoid the wait would be to ignore the in-flight results,
    which would re-introduce the rmtree-vs-read race; we accept the bounded
    latency instead."""
    inflight: set = set()
    abort = False
    for task in tasks:
        if abort:
            break
        if len(inflight) >= max_inflight:
            inflight, abort = _consume_completed(inflight, on_done)
        inflight.add(submit(task))
    # rf-conc-02: drains (and thus joins) every still-inflight future —
    # including running ones — so callers that delete shared state after
    # this returns never race a worker still touching it. `on_done` is
    # invoked for each drained future, so a first-and-only error that only
    # surfaces here (every task was still inflight when submission ended,
    # none completed during the loop) is still recorded by the caller's
    # `on_done` — even though the drain ignores its abort return.
    for fut in inflight:
        on_done(fut)


def _consume_completed(
    inflight: set,
    on_done: Callable[["Future"], bool],
) -> tuple[set, bool]:
    done, pending = wait(inflight, return_when=FIRST_COMPLETED)
    abort = False
    for future in done:
        abort = on_done(future) or abort
    return pending, abort


class _ChownWarningLimiter:
    def __init__(self, limit: int | None = None) -> None:
        self.limit = _CHOWN_WARNING_LIMIT if limit is None else limit
        self.suppressed = 0
        self._emitted = 0
        self._lock = threading.Lock()

    def warn(self, dst_path: Path, exc: BaseException, *, invalid: bool = False) -> None:
        with self._lock:
            should_emit = self._emitted < self.limit
            if should_emit:
                self._emitted += 1
            else:
                self.suppressed += 1
        if not should_emit:
            return
        if invalid:
            _log().warning("could not chown %s (invalid path): %s", dst_path, exc)
        else:
            _log().warning("could not chown %s: %s", dst_path, exc)

    def summarize(self) -> None:
        if self.suppressed:
            _log().warning(
                "ownership replication: suppressed %d additional chown warning(s)",
                self.suppressed,
            )


def _digest_pinned_file(
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
) -> str:
    digest = hashlib.sha256()
    with _open_pinned_file(directory_fd, name, source_stat) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_pinned_file(
    source_label: Path,
    destination: Path,
    rel: Path,
    source_stat: os.stat_result,
    expected_digest: str | None,
) -> None:
    try:
        destination_stat = destination.lstat()
    except OSError:
        raise RuntimeError(
            f"missing file in copy: {rel} (src={source_label})") from None
    if not stat.S_ISREG(destination_stat.st_mode):
        raise RuntimeError(f"missing file in copy: {rel} (src={source_label})")
    if source_stat.st_size != destination_stat.st_size:
        raise RuntimeError(
            f"size mismatch for {rel} (src={source_label}): "
            f"{source_stat.st_size} != {destination_stat.st_size}"
        )
    if expected_digest is not None and expected_digest != _hash()(destination):
        raise RuntimeError(f"hash mismatch for {rel} (src={source_label})")


def _verify_pinned_directory(
    source_label: Path,
    destination: Path,
    rel: Path,
) -> None:
    try:
        destination_mode = destination.lstat().st_mode
    except OSError:
        destination_mode = 0
    if not stat.S_ISDIR(destination_mode):
        raise RuntimeError(
            f"missing directory in copy: {rel} (src={source_label})")


def _verify_pinned_symlink(
    source_label: Path,
    source_target: str,
    destination: Path,
    rel: Path,
) -> None:
    if not destination.is_symlink():
        raise RuntimeError(
            f"missing symlink in copy: {rel} (src={source_label})")
    destination_target = os.readlink(destination)
    if source_target != destination_target:
        raise RuntimeError(
            f"symlink target mismatch for {rel} (src={source_label}): "
            f"{source_target!r} != {destination_target!r}"
        )


def _pinned_kind_verify_task(
    source_path: Path,
    target: Path,
    rel: Path,
    directory_fd: int,
    name: str,
    source_stat: os.stat_result,
    checksum: bool,
) -> Callable[[], None] | None:
    # Capture source data before the walker advances and closes directory_fd.
    mode = source_stat.st_mode
    if stat.S_ISREG(mode):
        digest = _digest_pinned_file(directory_fd, name, source_stat) if checksum else None
        return partial(_verify_pinned_file, source_path, target, rel, source_stat, digest)
    if stat.S_ISDIR(mode):
        return partial(_verify_pinned_directory, source_path, target, rel)
    if stat.S_ISLNK(mode):
        return partial(
            _verify_pinned_symlink, source_path,
            _read_pinned_symlink(directory_fd, name, source_stat), target, rel,
        )
    return None


def _iter_pinned_verify_tasks(
    source_fd: int,
    source_label: Path,
    destination: Path,
    checksum: bool,
    verify_ownership: bool,
) -> Iterator[Callable[[], None]]:
    for rel, directory_fd, name, source_stat in _walk_pinned_entries(source_fd):
        source_path = source_label / rel
        target = destination / rel
        task = _pinned_kind_verify_task(
            source_path, target, rel, directory_fd, name, source_stat, checksum)
        if task is None:
            continue
        yield task
        if verify_ownership:
            yield partial(
                _verify_ownership, source_path, target, rel, source_stat)


def verify_copy(src: Path, dst: Path, checksum: bool = False,
                verify_ownership: bool = False,
                *, source_fd: int, jobs: int | None = None) -> None:
    """Verify contents and inventory through the caller's open source descriptor.

    ``src`` labels diagnostics only. The caller keeps ``source_fd`` open until
    verification finishes. Checksum tasks stream through a bounded worker pool;
    size-only checks run sequentially. Optional ownership checks compare mode,
    uid and gid for every copied entry.
    """
    tasks = _iter_pinned_verify_tasks(
        source_fd, src, dst, checksum, verify_ownership)
    with _OperationStallWatchdog("verify_copy") as watchdog:
        if not checksum:
            # cheap stat-only / readlink ops: sequential is fast and keeps the
            # error path deterministic for tests.
            for task in tasks:
                task()
                watchdog.touch("verify_copy")
        else:
            _run_verify_pool(tasks, jobs=jobs, watchdog=watchdog)
        _assert_complete_inventory_match(src, dst, source_fd=source_fd)
        watchdog.touch("complete inventory comparison")


def _inventory_kind(mode: int) -> int:
    if stat.S_ISDIR(mode):
        return 1
    if stat.S_ISREG(mode):
        return 2
    if stat.S_ISLNK(mode):
        return 3
    return 4


def _load_inventory(
    database: sqlite3.Connection, table: str, root: Path
) -> None:
    for path in _walk_entries(root):
        mode = os.lstat(path).st_mode
        kind = _inventory_kind(mode)
        database.execute(
            f"INSERT INTO {table} VALUES (?, ?)",
            (os.fsencode(path.relative_to(root)), kind),
        )


def _load_pinned_inventory(
    database: sqlite3.Connection,
    source_fd: int,
) -> None:
    for rel, _directory_fd, _name, entry_stat in _walk_pinned_entries(source_fd):
        kind = _inventory_kind(entry_stat.st_mode)
        if kind == 4:
            continue
        database.execute(
            "INSERT INTO source_entries VALUES (?, ?)",
            (os.fsencode(rel), kind),
        )


# rf-rel-50: two separate EXCEPTs, deliberately NOT one compound statement.
# `A EXCEPT B UNION ALL B EXCEPT A` reads like a symmetric difference, but
# SQLite evaluates compound operators strictly left to right, so it collapses
# to `((A EXCEPT B) UNION ALL B) EXCEPT A` == `B \ A` — target-only extras
# only, which `copy_tree` already makes impossible by refusing a pre-existing
# target. The missing-entry case this check exists for was never detected.
_INVENTORY_MISSING_SQL = (
    "SELECT path, kind FROM source_entries "
    "EXCEPT SELECT path, kind FROM target_entries LIMIT 1"
)
_INVENTORY_EXTRA_SQL = (
    "SELECT path, kind FROM target_entries "
    "EXCEPT SELECT path, kind FROM source_entries LIMIT 1"
)


class InventorySpoolError(RuntimeError):
    """The temporary inventory database could not be used (rf-dep-50)."""


_INVENTORY_SPOOL_HINT = (
    "set TMPDIR (or SQLITE_TMPDIR) to a writable filesystem with free space"
)


@contextmanager
def _inventory_db() -> Iterator[sqlite3.Connection]:
    """Open the temporary inventory database (rf-dep-50).

    ``sqlite3.connect("")`` opens a private temporary database ON DISK under
    TMPDIR / SQLITE_TMPDIR — which is exactly what keeps a multi-million-entry
    inventory off the heap — so a full or read-only temp filesystem is a real
    failure mode. It bites at the very END of verify, where a raw
    ``OperationalError`` turned a fully-copied, fully-verified migration into a
    traceback. A typed error lets ``main`` report it as a plain FAILED line
    (and ``_copy_and_verify`` still removes the unswapped target, leaving the
    source intact for a retry).
    """
    try:
        database = sqlite3.connect("")
    except sqlite3.Error as exc:
        raise InventorySpoolError(
            f"could not open the temporary inventory database ({exc}); "
            f"{_INVENTORY_SPOOL_HINT}"
        ) from exc
    try:
        yield database
    except sqlite3.Error as exc:
        raise InventorySpoolError(
            f"the temporary inventory database failed ({exc}); "
            f"{_INVENTORY_SPOOL_HINT}"
        ) from exc
    finally:
        database.close()


def _assert_complete_inventory_match(
    src: Path,
    dst: Path,
    *,
    source_fd: int,
) -> None:
    """Entry-for-entry comparison of the two trees immediately before the swap.

    This is the last gate before the source is deleted, so it must catch an
    entry that never made it into the copy (rf-rel-50)."""
    with _inventory_db() as database:
        database.execute("CREATE TABLE source_entries (path BLOB, kind INTEGER)")
        database.execute("CREATE TABLE target_entries (path BLOB, kind INTEGER)")
        _load_pinned_inventory(database, source_fd)
        _load_inventory(database, "target_entries", dst)
        missing = database.execute(_INVENTORY_MISSING_SQL).fetchone()
        extra = database.execute(_INVENTORY_EXTRA_SQL).fetchone()
    _raise_on_inventory_divergence(missing, extra)


def _raise_on_inventory_divergence(missing, extra) -> None:
    """Report WHICH side diverged so the operator can act without re-walking
    both trees by hand (rf-rel-50)."""
    if missing is None and extra is None:
        return
    detail = (
        f"missing from the copy: {os.fsdecode(missing[0])!r}"
        if missing is not None
        else f"present only in the copy: {os.fsdecode(extra[0])!r}"
    )
    raise RuntimeError(
        f"source/destination inventory mismatch immediately before swap "
        f"({detail})"
    )


def _run_verify_pool(
    tasks: Iterator[Callable[[], None]],
    *,
    jobs: int | None = None,
    watchdog: _OperationStallWatchdog | None = None,
) -> None:
    """Drive `tasks` through `_run_streamed` over a bounded
    ThreadPoolExecutor (rf-perf-02 / rf-arch-06).

    Error-path join (rf-rel-02): submission aborts on the first failing
    future, but the function does NOT return while hash work is still
    running. Two independent mechanisms cooperate:

      * `_run_streamed` invokes `on_done` over the inflight set, which
        blocks on `fut.exception()` for each currently-tracked future;
      * the `finally` then calls `ex.shutdown(wait=True, cancel_futures=…)`,
        which cancels still-QUEUED tasks (so a TB-scale tree's first
        mismatch surfaces immediately) yet BLOCKS until the already-RUNNING
        worker threads finish.

    The earlier docstring claimed inflight futures were "drained" while the
    shutdown was actually `wait=False`, leaving running SHA-256 threads
    detached past the return; the wording and the call now match (rf-rel-02
    / rf-conc-01).

    Secondary errors are counted, not silently dropped (rf-rel-08): the
    re-raise is annotated with `verify pool raised N errors total;
    re-raising first` so an operator who fixes the first divergence
    knows more are pending."""
    first_error: list[BaseException] = []
    dropped = [0]

    def on_done(fut: "Future") -> bool:
        if watchdog is not None:
            watchdog.touch("verify_copy")
        exc = fut.exception()
        if exc is None:
            return False
        if not first_error:
            first_error.append(exc)
        else:
            dropped[0] += 1
        return True   # short-circuit submission

    workers = _resolved_jobs(jobs)
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        # rf-test-01: run each task under a COPY of the submitting context so
        # worker threads see the active `_hash` / `_log` contextvars. Without
        # this, ThreadPoolExecutor workers start with an empty context and the
        # injectable hash seam (and logger override) would silently fall back
        # to the module defaults inside the pool.
        _run_streamed(partial(_submit_in_context, ex), tasks,
                      _inflight_cap(workers), on_done)
    finally:
        # rf-perf-04: on failure, cancel still-QUEUED futures instead of
        # draining them so the first hash mismatch is visible immediately
        # instead of waiting seconds for the rest of a TB-scale tree.
        # rf-conc-01: but wait=True so the executor joins the already-RUNNING
        # worker threads deterministically before returning — cancel_futures
        # only drops queued work, the in-flight hashes are not left detached
        # past this function. The previous wait=False leaked those threads on
        # the abort path.
        ex.shutdown(wait=True, cancel_futures=bool(first_error))
    if first_error:
        if dropped[0]:
            _log().warning(
                "verify pool raised %d errors total; re-raising first",
                dropped[0] + 1,
            )
        raise first_error[0]


def _submit_in_context(ex: ThreadPoolExecutor, task: Callable[[], None]
                       ) -> "Future":
    """Submit `task` to `ex`, running it under a copy of the current context
    (rf-test-01) so worker threads inherit the active `_hash` / `_log`
    contextvars instead of the empty default context."""
    ctx = contextvars.copy_context()
    return ex.submit(ctx.run, task)


def _verify_ownership(
    src_path: Path,
    dst_path: Path,
    rel: Path,
    src_stat: os.stat_result | None = None,
) -> None:
    """Fail if uid/gid/mode on `dst_path` don't match `src_path` (rf-rel-04).
    Uses lstat so the comparison covers symlinks themselves, not their targets."""
    try:
        s = src_stat if src_stat is not None else src_path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"ownership stat failed for {rel} (src={src_path}): {exc}"
        ) from exc
    if not os.path.lexists(dst_path):
        raise RuntimeError(
            f"missing copied entry before ownership check: {rel} (src={src_path})"
        )
    try:
        d = dst_path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"ownership stat failed for {rel} (src={src_path}): {exc}"
        ) from exc
    if (s.st_uid, s.st_gid) != (d.st_uid, d.st_gid):
        raise RuntimeError(
            f"ownership mismatch for {rel} (src={src_path}): "
            f"src uid/gid={s.st_uid}/{s.st_gid} dst={d.st_uid}/{d.st_gid}"
        )
    # mode comparison: only the permission bits (the file-type bits would
    # always match because the entry kinds were already validated above).
    if (s.st_mode & 0o7777) != (d.st_mode & 0o7777):
        raise RuntimeError(
            f"mode mismatch for {rel} (src={src_path}): "
            f"src={oct(s.st_mode & 0o7777)} dst={oct(d.st_mode & 0o7777)}"
        )


def _walk_entries(root: Path) -> Iterable[Path]:
    """Yield every entry (dirs + files) under root, not following symlinks."""
    for path, dir_names, names in os.walk(root, followlinks=False):
        base = Path(path)
        for n in (*dir_names, *names):
            yield base / n


class _HashTruncatedError(RuntimeError):
    """Raised by :func:`_hash_once_strict` when the file shrank
    mid-hash (rf-rel-19). A typed subclass so :func:`_sha256` retries
    ONLY this case instead of swallowing every unrelated
    ``RuntimeError`` (e.g. a future hashlib backend error)."""


class _HashVanishedError(RuntimeError):
    """Raised by :func:`_hash_once_strict` when the file disappeared
    between open-and-read and the final stat (rf-rel-19)."""


def _sha256(path: Path) -> str:
    """Stream-hash `path` with SHA-256, returning the hex digest.

    rf-rel-16: records the byte count it actually read and re-stats the
    file on close. A length mismatch between "bytes hashed" and "final
    size" means the file was truncated mid-stream — that's data loss
    in disguise, not a clean copy.

    rf-rel-17 / rf-rel-19: a single transient truncation no longer
    aborts the whole verify pool. ``_hash_once_strict`` is retried up
    to ``_sha256_retry_attempts()`` times (env-tunable via
    ``RELOCATE_SHA256_RETRY_ATTEMPTS``, rf-adapt-01) for the typed
    ``_HashTruncatedError`` / ``_HashVanishedError`` cases ONLY. Any
    other exception escapes immediately so a hashlib backend failure
    (or any future error shape) doesn't masquerade as a transient and
    burn a retry on a non-retryable condition.

    rf-perf-30: the per-file stall watchdog is gated on file size — see
    :func:`_sha256_watchdog`."""
    last_exc: BaseException | None = None
    attempts = _sha256_retry_attempts()
    for _ in range(attempts):
        try:
            with _sha256_watchdog(path):
                return _hash_once_strict(path)
        except (_HashTruncatedError, _HashVanishedError) as exc:
            last_exc = exc
            continue
    # rf-rel-20: was `assert last_exc is not None` — stripped under `python -O`,
    # so a misconfigured attempt count of 0 silently returned None and
    # downstream digest compares blew up far from the cause. A real `raise`
    # survives optimization and names the misconfiguration.
    if last_exc is None:
        raise RuntimeError(
            f"_sha256({path}): no attempts ran; "
            f"retry attempts={attempts}"
        )
    raise last_exc


_SHA256_RETRY_ATTEMPTS = 2

# rf-perf-30: below this size a healthy hash finishes in well under the
# _STALL_WARN_SECONDS window even on slow media, so a dedicated watchdog
# thread per file (created inside the retry loop, twice per file for
# src+dst) is pure overhead on many-small-file trees. A genuine I/O stall
# on a small file is still surfaced by the outer verify_copy watchdog,
# which only loses the per-file label. One lstat to read the size is far
# cheaper than a thread create/join cycle.
_SHA256_WATCHDOG_MIN_BYTES = 256 * 1024 * 1024


def _sha256_watchdog(path: Path):
    """Per-file stall watchdog for :func:`_sha256`, or a no-op context
    when `path` is too small for a >60s healthy hash (rf-perf-30)."""
    try:
        size = os.lstat(path).st_size
    except OSError:
        size = 0
    if size < _SHA256_WATCHDOG_MIN_BYTES:
        return nullcontext(None)
    return _OperationStallWatchdog(f"sha256 {path}")


def _sha256_retry_attempts() -> int:
    """Return the hash retry-attempt count (rf-adapt-01).

    Read from ``RELOCATE_SHA256_RETRY_ATTEMPTS`` (env var). Default
    ``_SHA256_RETRY_ATTEMPTS``. Clamped to at least 1 so a zero or
    negative value can't disable hashing outright (rf-rel-20); invalid
    values fall back to the default."""
    raw = os.environ.get(
        "RELOCATE_SHA256_RETRY_ATTEMPTS", str(_SHA256_RETRY_ATTEMPTS))
    try:
        value = int(raw)
    except ValueError:
        return _SHA256_RETRY_ATTEMPTS
    return max(1, value)


def _hash_once_strict(path: Path) -> str:
    """One stream-hash attempt with the rf-rel-16 truncation guard.

    Raises :class:`_HashVanishedError` if the file disappeared between
    the read loop and the final stat, or :class:`_HashTruncatedError`
    if the final size is shorter than the bytes actually hashed. Both
    are retryable by :func:`_sha256`'s loop; any other exception
    escapes the function unmolested (rf-rel-19)."""
    h = hashlib.sha256()
    bytes_hashed = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            bytes_hashed += len(chunk)
    try:
        final_size = path.lstat().st_size
    except OSError:
        raise _HashVanishedError(
            f"file vanished during hash: {path}") from None
    if final_size != bytes_hashed:
        raise _HashTruncatedError(
            f"file truncated during hash: {path} "
            f"(hashed {bytes_hashed} bytes, final size {final_size})"
        )
    return h.hexdigest()


# --- atomic swap ------------------------------------------------------------

def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
            raise
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    """Best-effort fsync of every regular file and directory under `root`.

    rf-rob-50: this is durability hardening, NOT correctness — by the time it
    runs the bytes are written AND verified. Letting a single os.open/os.fsync
    error propagate out of `atomic_swap` stranded that complete, verified
    target: `_copy_and_verify`'s cleanup had already returned, and
    `_execute_migration`'s unwind only rmdirs the (now non-empty) destination
    ancestors. The next run then died in `copy_tree` calling it "a stale
    partial target", which is the opposite of the truth. Failures are warned
    (capped) and the swap proceeds.
    """
    failures: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False):
        directory = Path(dirpath)
        for name in filenames:
            _fsync_regular_file(directory / name, failures)
        _fsync_directory_best_effort(directory, failures)
    _fsync_directory_best_effort(root.parent, failures)
    _warn_fsync_failures(root, failures)


def _fsync_regular_file(path: Path, failures: "list[str]") -> None:
    try:
        if path.is_symlink() or not path.is_file():
            return
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        failures.append(f"{path}: {exc}")
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        failures.append(f"{path}: {exc}")
    finally:
        os.close(fd)


def _fsync_directory_best_effort(path: Path, failures: "list[str]") -> None:
    try:
        _fsync_directory(path)
    except OSError as exc:
        failures.append(f"{path}: {exc}")


def _warn_fsync_failures(root: Path, failures: "list[str]") -> None:
    if not failures:
        return
    for detail in failures[:5]:
        _log().warning("could not fsync %s", detail)
    _log().warning(
        "%d entr(ies) under %s could not be fsynced; the copy is verified but "
        "may not survive an immediate power loss", len(failures), root,
    )

@contextmanager
def _backup_target(target: Path, expected: tuple[int, int]) -> Iterator[Path]:
    """Quarantine the pinned source; restore on error and remove only that inode."""
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    try:
        _rename_noreplace(target, backup)
    except FileExistsError as exc:
        raise FileExistsError(
            f"stale backup exists, refusing to overwrite: {backup}"
        ) from exc
    _validate_source_backup(backup, target, expected)
    try:
        _fsync_directory(target.parent)
        yield backup
    except BaseException:
        _restore_backup(backup, target, expected)
        raise
    _remove_source_backup(backup, expected)
    if not _path_taken(backup):
        _fsync_directory(target.parent)


def _restore_backup(backup: Path, target: Path, expected: tuple[int, int]) -> None:
    if not _backup_identity_ok(backup, expected):
        _log().error(
            "backup at %s was substituted; refusing to restore it onto %s. "
            "Inspect both paths manually.", backup, target,
        )
        return
    try:
        _rename_noreplace(backup, target)
    except (OSError, RuntimeError) as exc:
        _log().error(
            "rollback failed: backup preserved at %s; restore to %s failed: %s. "
            "Inspect both paths before using mv to restore it manually.",
            backup, target, exc,
        )
        return
    _swallow_or_warn(f"sync restored source {target}", _fsync_directory, target.parent)


def _validate_source_backup(
    backup: Path, target: Path, expected: tuple[int, int],
) -> None:
    try:
        actual = os.lstat(backup)
    except OSError:
        _log().error("cannot read backup identity; preserving %s for inspection", backup)
        raise
    actual_id = (actual.st_dev, actual.st_ino)
    if actual_id != expected:
        # This directory was never copied. Put it back without replacing any
        # newer source occupant, and never enter the symlink/delete lifecycle.
        _restore_backup(backup, target, actual_id)
        raise RuntimeError(
            f"source changed before swap: {target}; replacement preserved at "
            f"the source or backup {backup}; inspect both paths"
        )


def _remove_source_backup(backup: Path, expected: tuple[int, int]) -> None:
    if not _backup_identity_ok(backup, expected):
        _log().warning("backup at %s was substituted; leaving it in place", backup)
        return
    private = Path(tempfile.mkdtemp(prefix=".relocate-cleanup-", dir=backup.parent))
    held = private / "backup"
    descriptor = os.open(private, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o700:
            raise RuntimeError(f"backup cleanup directory was substituted: {private}")
        # Checking inside a private parent closes the public backup-name race
        # between the identity check and recursive deletion.
        _rename_noreplace(backup, held)
        _dispose_source_backup(held, backup, expected, descriptor)
    finally:
        os.close(descriptor)
        _swallow_or_warn(f"remove backup cleanup staging {private}", private.rmdir)


def _dispose_source_backup(
    held: Path, backup: Path, expected: tuple[int, int], directory_fd: int,
) -> None:
    actual = os.stat(held.name, dir_fd=directory_fd, follow_symlinks=False)
    actual_id = (actual.st_dev, actual.st_ino)
    if actual_id != expected:
        _log().warning("backup at %s was substituted; preserving replacement", backup)
        _restore_backup(held, backup, actual_id)
        return
    _swallow_or_warn(
        f"remove backup {held}", shutil.rmtree, held.name, dir_fd=directory_fd)
    if _path_taken(held):
        _restore_backup(held, backup, expected)


def _backup_identity_ok(backup: Path, expected: tuple[int, int]) -> bool:
    """True when `backup`'s current (st_dev, st_ino) still matches `expected`
    (rf-sec-03). A vanished or substituted backup returns False."""
    try:
        st = os.lstat(backup)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == expected


def atomic_swap(source: Path, target: Path, *, source_fd: int) -> None:
    """Replace `source` (a real dir) with a symlink to `target` (rf-arch-01).

    NOT atomic across process death, despite the name. The name is retained
    as a description of the symlink publication, not of the entire operation.

    The sequence is:

      1. no-replace rename to `source<BACKUP_SUFFIX>` — move the real dir aside
      2. `os.symlink(target, source)`                — only THIS step is atomic
      3. privately quarantine and delete the identity-checked backup

    A SIGKILL / power-loss between steps 1 and 2 leaves `source` renamed to
    `<name>.relocate-backup` with no symlink in place — see rf-rel-01 for the
    orphaned-backup detection and `--recover` recovery path. The in-`with`
    failure path (an exception from step 2) rolls the rename back; only an
    abrupt process death skips the rollback.

    Delegates the rename-aside / restore-or-clean lifecycle to
    `_backup_target`.

    Source identity and uid/gid come from the caller's still-open descriptor,
    captured before the target fsync and checked after quarantine. The
    replacement symlink can be lchown'd to the original owner. When run as root
    migrating a user-owned dir this keeps the symlink owned by the user instead
    of root."""
    src_st = os.fstat(source_fd)
    expected = (src_st.st_dev, src_st.st_ino)
    _fsync_tree(target)
    with _backup_target(source, expected):
        _create_symlink(source, target, owner=(src_st.st_uid, src_st.st_gid))
        _fsync_directory(source.parent)


def _create_symlink(link: Path, target: Path, *,
                    owner: "tuple[int, int] | None" = None) -> None:
    """Create `link -> target` atomically (rf-sec-01).

    Symlink is staged inside a freshly-mkdtemp'd directory beside `link` so
    that the path used for `os.symlink` is one we just created — no other
    process can have stamped a different inode onto it. The single no-replace
    rename into place is then atomic on the parent filesystem.

    The older unlink/symlink/rename sequence on a fixed `.relocate-tmp`
    name allowed an attacker on a world-writable parent to win the unlink
    race and substitute a different target between the symlink() and the
    rename().

    rf-sec-04 / rf-conc-04: `mkdtemp` is attempted directly and its
    ``PermissionError`` is translated to a typed
    ``RuntimeError("link parent not writable: …")``. The earlier
    pre-check via ``os.access`` was TOCTOU vs. `mkdtemp` — an attacker
    revoking write between the check and the call still leaked the
    bare ``PermissionError``. Letting `mkdtemp` itself fail and
    classifying the error retroactively closes the window and removes
    the redundant syscall."""
    parent = link.parent
    _sweep_orphaned_staging_dirs(parent)
    try:
        staging = Path(tempfile.mkdtemp(
            prefix=f"{STAGING_PREFIX}{os.getpid()}-",
            dir=str(parent),
        ))
        _write_staging_pid(staging)
    except PermissionError as exc:
        raise RuntimeError(
            f"link parent not writable: {parent} "
            f"(needs +w +x for the relocate stage-dir)"
        ) from exc
    if owner is not None:
        # rf-sec-03: as root the mkdtemp'd (mode 0700) staging dir is otherwise
        # root-owned — it momentarily blocks the real user and, on a cleanup
        # failure, leaks a root-owned .relocate-stage-* in the user's tree.
        # chown it to the source owner so any leftover belongs to the user.
        _chown_to_owner(staging, owner)
    try:
        tmp = staging / link.name
        os.symlink(target, tmp)
        try:
            _rename_noreplace(tmp, link)
        except FileExistsError as exc:
            raise FileExistsError(
                f"refusing to overwrite existing path at link target: {link} "
                f"(an unexpected file/symlink is already there)"
            ) from exc
        if owner is not None:
            # rf-sec-02: lchown the symlink itself (not its target) to the
            # captured source owner so a root-run migration doesn't leave a
            # root-owned symlink in a user's tree.
            _chown_to_owner(link, owner)
    finally:
        _cleanup_staging(staging)


def _write_staging_pid(staging: Path) -> None:
    marker = staging / STAGING_PID_FILE
    try:
        marker.write_text(f"{os.getpid()}\n", encoding="ascii")
    except OSError as exc:
        _log().warning("could not write staging owner marker %s: %s", marker, exc)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _staging_pid_from_name(path: Path) -> "int | None":
    if not path.name.startswith(STAGING_PREFIX):
        return None
    pid_text = path.name[len(STAGING_PREFIX):].split("-", 1)[0]
    try:
        return int(pid_text)
    except ValueError:
        return None


def _staging_owner_pid(path: Path) -> "int | None":
    try:
        pid_text = (path / STAGING_PID_FILE).read_text(encoding="ascii").strip()
        return int(pid_text)
    except (OSError, ValueError):
        return _staging_pid_from_name(path)


def _staging_dir_orphaned(path: Path) -> bool:
    pid = _staging_owner_pid(path)
    if pid is None:
        return True
    return not _pid_alive(pid)


def _sweep_orphaned_staging_dirs(parent: Path) -> int:
    removed = 0
    try:
        entries = list(parent.iterdir())
    except OSError as exc:
        _log().warning("could not scan staging dir parent %s: %s", parent, exc)
        return 0
    for entry in entries:
        if not _is_orphaned_staging_dir(entry):
            continue
        _cleanup_staging(entry)
        if not _path_taken(entry):
            removed += 1
    if removed:
        _log().warning(
            "removed %d orphaned relocate staging dir(s) from %s",
            removed,
            parent,
        )
    return removed


def _is_orphaned_staging_dir(entry: Path) -> bool:
    if not entry.name.startswith(STAGING_PREFIX):
        return False
    try:
        mode = entry.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) and not stat.S_ISLNK(mode) and _staging_dir_orphaned(entry)


def _chown_to_owner(path: Path, owner: tuple[int, int]) -> None:
    """lchown `path` to `owner` (rf-sec-02 / rf-sec-03). Used for the
    replacement symlink and the staging dir. A non-root caller can't change
    ownership; that EPERM is benign (the entry already belongs to the caller),
    so it's logged and absorbed rather than aborting the completed swap.
    `follow_symlinks=False` so a symlink is chowned itself, not its target."""
    uid, gid = owner
    _swallow_or_warn(
        f"lchown {path} to uid={uid} gid={gid}",
        os.chown, path, uid, gid, follow_symlinks=False,
    )


def _cleanup_staging(staging: Path) -> None:
    """Best-effort removal of a `_create_symlink` staging dir.

    Uses bare `os` calls (not `shutil.rmtree`) so that test fixtures which
    monkeypatch `shutil.rmtree` to simulate failures elsewhere in the
    pipeline don't accidentally trip this cleanup.

    Single `os.lstat` per child (rf-perf-05): one syscall replaces the
    `os.path.islink` + `os.path.isdir` pair, and the result is checked
    against `S_ISLNK` / `S_ISDIR` directly so we never accidentally
    follow a symlink into a directory tree we shouldn't be touching."""
    try:
        for child in os.listdir(staging):
            child_path = staging / child
            try:
                mode = os.lstat(child_path).st_mode
            except OSError:
                continue
            try:
                if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                    os.rmdir(child_path)
                else:
                    os.unlink(child_path)
            except OSError:
                pass
        os.rmdir(staging)
    except OSError:
        pass


# --- kill-safety recovery (rf-rel-01) ---------------------------------------

def _orphaned_backup(source: Path) -> "Path | None":
    """Return the orphaned `<source>.relocate-backup` path, or None.

    An orphan exists when `atomic_swap` died between renaming source to backup
    and the symlink creation: the real directory now lives at the backup name
    and `source` itself is gone. Detect that exact shape — source absent (not
    even a dangling symlink) AND the backup present as a real directory — so a
    normal in-progress run (source present) is never misread as a crash.

    rf-rel-04: the backup must be an actual directory (and not a symlink). A
    user's unrelated `foo.relocate-backup` regular file or symlink whose `foo`
    doesn't exist no longer triggers the misleading "killed mid-swap" warning,
    because `atomic_swap` only ever renames a real source directory aside."""
    backup = source.with_name(source.name + BACKUP_SUFFIX)
    if _path_taken(source):
        return None
    try:
        mode = os.lstat(backup).st_mode
    except OSError:
        return None
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        return backup
    return None


def _warn_orphaned_backup(backup: Path, source: Path) -> None:
    _log().warning(
        "orphaned backup detected: %s exists but %s is missing — a previous "
        "run was likely killed mid-swap. Re-run with --recover to restore it "
        "(renames the backup back to the source), or do it manually: "
        "`mv %s %s`",
        backup, source, backup, source,
    )


def _warn_stranded_backup(source: Path) -> None:
    """rf-robust-06: a SIGKILL between the symlink creation and the backup
    rmtree in `_backup_target` strands the full-size `<source>.relocate-backup`
    dir while `source` is already a valid symlink. `already_migrated` then skips
    and `_orphaned_backup` misses it (source is 'taken'), so the leftover copy
    silently consumes the space the migration existed to free. Surface it on the
    skip path with a manual-cleanup hint (never auto-delete a real dir)."""
    backup = source.with_name(source.name + BACKUP_SUFFIX)
    try:
        mode = os.lstat(backup).st_mode
    except OSError:
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        _log().warning(
            "leftover backup still parked: %s exists though %s is already "
            "migrated — a previous run was likely killed before removing the "
            "backup. It still consumes disk; remove it manually once verified: "
            "`rm -rf %s`",
            backup, source, backup,
        )


_RENAME_NOREPLACE = 1  # Linux/FreeBSD: fail if the destination exists
_RENAME_EXCL = 0x00000004  # macOS renamex_np equivalent


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Atomically rename ``src`` to an absent ``dst`` or fail closed."""
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.restype = ctypes.c_int
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            res = rename(os.fsencode(src), os.fsencode(dst), _RENAME_EXCL)
        else:
            rename = libc.renameat2
            rename.restype = ctypes.c_int
            rename.argtypes = [
                ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_uint,
            ]
            at_fdcwd = -100
            res = rename(
                at_fdcwd, os.fsencode(src), at_fdcwd, os.fsencode(dst),
                _RENAME_NOREPLACE,
            )
    except (OSError, AttributeError) as exc:
        raise RuntimeError(
            "atomic no-replace rename is unavailable on this POSIX host"
        ) from exc
    if res == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(
            err, f"destination already exists; refusing to replace {dst}", dst)
    if err in {
        errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    }:
        raise RuntimeError(
            f"filesystem does not support atomic no-replace rename: {dst}"
        )
    raise OSError(err, os.strerror(err), dst)


def recover(source: Path, *, force: bool = False) -> str:
    """Restore an orphaned `<source>.relocate-backup` to `source` (rf-rel-01).

    Returns a single-line status. Raises `FileNotFoundError` when there is
    nothing to recover and `FileExistsError` when `source` is already present
    (so recovery never clobbers live data).

    rf-rel-03: unlike `execute`, recovery is a one-rename restore with no copy,
    so the open-files concern is narrower — but a process still holding an FD
    under the backup keeps the old inode after the rename, exactly as in a
    migration. The open-files precheck therefore runs on the backup for `--force`
    parity with `execute`; pass `force=True` to skip it."""
    backup = source.with_name(source.name + BACKUP_SUFFIX)
    if _path_taken(source):
        raise FileExistsError(
            f"refusing to recover: {source} already exists; "
            f"resolve it manually before restoring {backup}"
        )
    if not _path_taken(backup):
        raise FileNotFoundError(f"no orphaned backup to recover at {backup}")
    # rf-rel-02: a real moved-aside source is a directory (and not a symlink).
    # A dangling symlink or regular file at the backup name was left by a third
    # party, not by atomic_swap — renaming it onto `source` would restore the
    # wrong thing. Verify the shape via lstat before touching it. A TOCTOU
    # unlink between the `_path_taken` gate above and this lstat surfaces as the
    # typed "no orphaned backup" message rather than a bare FileNotFoundError.
    try:
        backup_mode = os.lstat(backup).st_mode
    except OSError as exc:
        raise FileNotFoundError(
            f"no orphaned backup to recover at {backup}"
        ) from exc
    if not stat.S_ISDIR(backup_mode) or stat.S_ISLNK(backup_mode):
        raise NotADirectoryError(
            f"refusing to recover: {backup} is not a directory "
            f"(it may be a symlink or regular file left by another process); "
            f"inspect and restore it manually"
        )
    if not force:
        _check_no_open_files(backup)
    # rf-dist-01: no-replace rename so a source recreated between the
    # _path_taken gate above and here is never silently clobbered.
    _rename_noreplace(backup, source)
    return f"recovered: {backup} -> {source}"


# --- orchestration ----------------------------------------------------------

def _execute_preamble(plan: Plan,
                      advance: Callable[[MigrationState], None]) -> str | None:
    """Guard/skip checks that run before any filesystem mutation
    (rf-cx-30). Returns a terminal status line for the skip / dry-run
    outcomes, or None when the migration should proceed."""
    if already_migrated(plan.source, plan.target):
        advance(MigrationState.ALREADY_MIGRATED)
        _warn_stranded_backup(plan.source)
        return f"skipped: {plan.source} already symlinks to {plan.target}"
    if _symlink_points_at_empty_target(plan.source, plan.target):
        # rf-rel-01: the symlink is correct but the target was later
        # emptied. validate_source would crash with "already a symlink";
        # treat it as a legitimately-migrated dir and skip instead.
        advance(MigrationState.ALREADY_MIGRATED)
        _warn_stranded_backup(plan.source)
        return (f"skipped: {plan.source} already symlinks to {plan.target} "
                f"(already migrated, empty target)")
    orphan = _orphaned_backup(plan.source)
    if orphan is not None:
        # rf-rel-01: a killed swap left the dir at the backup name with the
        # source gone. Warn (never auto-mutate) and point at --recover
        # before validate_source raises a bare FileNotFoundError.
        _warn_orphaned_backup(orphan, plan.source)
    validate_source(plan.source)
    if not plan.force:
        _check_no_open_files(plan.source)
    # rf-rel-21: _check_cross_device is read-only (warn/raise), so keep it
    # for dry-run, but return BEFORE ensure_dest_root — which mkdirs/chowns
    # the destination — so --dry-run never mutates the filesystem.
    _check_cross_device(plan)
    if plan.dry_run:
        advance(MigrationState.DRY_RUN)
        return f"dry-run: would migrate {plan.source} -> {plan.target}"
    return None


def _execute_migration(plan: Plan,
                       advance: Callable[[MigrationState], None]) -> str:
    """Copy-verify-swap core of :func:`execute` (rf-cx-30)."""
    # rf-sec-01: open the source (O_NOFOLLOW|O_DIRECTORY) and hold the fd
    # across the copy so the inode can't be swapped for a symlink/other dir;
    # identity is re-checked just before copying. Opened AFTER the open-files
    # check (our own held fd would otherwise trip it) and after the dry-run
    # return (dry-run reads nothing).
    src_fd, src_id = _source_identity_fd(plan.source)
    try:
        created_dirs = ensure_dest_root(plan.target.parent, plan.source)
        try:
            # rf-sec-01: confirm the source path still names the inode we
            # opened before reading from it by path.
            _assert_source_identity(plan.source, src_id)
            # rf-ddd-02: _copy_and_verify advances to COPIED between copy and
            # verify so a verify-failed run logs an honest intermediate state.
            _copy_and_verify(
                plan, on_state=advance, source_fd=src_fd, source_id=src_id)
            advance(MigrationState.VERIFIED)
            _swap_or_explain(plan, source_fd=src_fd)
            advance(MigrationState.SWAPPED)
            return f"ok: {plan.source} -> {plan.target}"
        except BaseException:
            # rf-state-01: every post-ensure_dest_root failure path (verify
            # or swap failure) must unwind the dest dirs we created —
            # _copy_and_verify only cleans the leaf target, not the
            # ancestors mkdir'd here.
            _cleanup_created_dirs(created_dirs)
            raise
    finally:
        os.close(src_fd)


def _swap_or_explain(plan: Plan, *, source_fd: int) -> None:
    """Run the swap; on failure say that the target is a COMPLETE copy (rf-rob-50).

    Everything up to here succeeded, so `_copy_and_verify`'s cleanup no longer
    applies and the finished target survives on the destination volume. Without
    this line the operator's only clue is the NEXT run failing in `copy_tree`
    with "may be a stale partial target" — the opposite of the truth. Name both
    ways out instead."""
    try:
        atomic_swap(plan.source, plan.target, source_fd=source_fd)
    except BaseException:
        _log().error(
            "swap failed AFTER the copy was verified: %s holds a COMPLETE, "
            "verified copy. Preserve it while inspecting the source %s and "
            "backup %s before recovery; either path may contain newer data.",
            plan.target, plan.source,
            plan.source.with_name(plan.source.name + BACKUP_SUFFIX),
        )
        raise


def execute(plan: Plan) -> str:
    """Run the migration described by `plan` and return a single-line status.

    Returns a string that begins with one of: `skipped:`, `dry-run:`, `ok:`.
    Internally tracks the lifecycle via `MigrationState` (rf-ddd-01); the
    enum is not exposed in the return value to preserve the textual API
    used by `main` and the tests."""
    state = MigrationState.FAILED

    def _advance(new_state: MigrationState) -> None:
        nonlocal state
        state = new_state

    try:
        status = _execute_preamble(plan, _advance)
        if status is not None:
            return status
        return _execute_migration(plan, _advance)
    finally:
        # rf-obs-04: promote the terminal state to INFO so operators
        # running at the default verbosity see the canonical lifecycle
        # landmark for every migration. DEBUG was hidden by default and
        # made post-mortem log scans hard.
        _log().info("migration state=%s source=%s target=%s",
                    state.value, plan.source, plan.target)
        if state is MigrationState.FAILED:
            _log_failed_hint(plan)


def _log_failed_hint(plan: Plan) -> None:
    """On a FAILED migration, point the operator at the recovery surface
    (rf-obs-01).

    The terminal `state=failed` line says *that* it failed but not whether the
    source is intact or was renamed to `<name>.relocate-backup`. If the swap
    died after renaming the source aside, an orphaned backup exists — surface
    its path and the `--recover` hint so the operator can act without grepping
    the filesystem."""
    backup = plan.source.with_name(plan.source.name + BACKUP_SUFFIX)
    if _orphaned_backup(plan.source) is not None:
        _log().warning(
            "failed after the source was renamed aside: the original is at %s "
            "and %s is missing — re-run with --recover to restore it",
            backup, plan.source,
        )
    else:
        _log().info(
            "failed with source %s left intact (no orphaned backup at %s); "
            "any partial target was cleaned up",
            plan.source, backup,
        )


def _nearest_existing_dir(start: Path) -> Path | None:
    """Walk up from `start` until a directory is found (rf-arch-05).

    Skips entries that exist but aren't dirs (a misconfigured CLI run can
    point `target.parent` at a regular file; `st_dev` of that inode is
    technically correct but cosmetically misleading). Returns None if the
    walk reaches the filesystem root without finding a directory."""
    cur = start
    while cur != cur.parent:
        try:
            if cur.is_dir() and not cur.is_symlink():
                return cur
        except OSError:
            pass
        cur = cur.parent
    try:
        if cur.is_dir() and not cur.is_symlink():
            return cur
    except OSError:
        pass
    return None


def _check_cross_device(plan: Plan, *,
                        stat_fn: Callable[[Path], "os.stat_result"] | None = None,
                        ) -> None:
    """Warn (or, with `strict_cross_device`, raise) when source and the
    nearest existing ancestor of target share a filesystem (rf-sec-03).

    Same-fs migrations are a misconfiguration: the symlink trick still
    works but the copy duplicates data on the same volume instead of
    freeing space. Default is a warning so existing call sites that
    legitimately stage on the same FS (tests, scratch dirs) keep working;
    `--strict-cross-device` upgrades it to an error.

    `stat_fn` (rf-arch-07) is injectable so tests can pass a typed device
    probe instead of monkeypatching `Path.stat` globally. Defaults to
    `os.stat`."""
    probe_stat = stat_fn if stat_fn is not None else os.stat
    with _OperationStallWatchdog("cross-device precheck") as watchdog:
        try:
            src_dev = probe_stat(plan.source).st_dev
            watchdog.touch("cross-device source stat")
            probe = _nearest_existing_dir(plan.target.parent)
            watchdog.touch("cross-device target probe")
            if probe is None:
                if plan.strict_cross_device:
                    raise RuntimeError(
                        "could not determine destination device under "
                        "--strict-cross-device"
                    )
                return
            dst_dev = probe_stat(probe).st_dev
            watchdog.touch("cross-device target stat")
        except OSError as exc:
            if plan.strict_cross_device:
                raise RuntimeError(
                    "could not determine source/destination device under "
                    "--strict-cross-device"
                ) from exc
            return
        if src_dev != dst_dev:
            return
        # rf-obs-05: include the mount point so the operator doesn't have to
        # run `stat -c '%m'` themselves to figure out which volume to switch.
        mount = _device_mount_point(plan.source)
        watchdog.touch("cross-device mount probe")
        mount_note = f" at {mount}" if mount else ""
        msg = (
            f"source and destination are on the same filesystem "
            f"(st_dev={src_dev}{mount_note}); "
            f"migration would duplicate data rather than free the source volume"
        )
        if plan.strict_cross_device:
            raise RuntimeError(msg + " — refusing under --strict-cross-device")
        _log().warning("%s — pass --strict-cross-device to refuse instead", msg)


def _device_mount_point(path: Path) -> "str | None":
    """Best-effort mount-point lookup for `path` (rf-obs-05 / rf-rel-12).

    Walks `path.resolve()` ancestors until `os.path.ismount` returns True
    (matches the POSIX semantics `stat -c '%m'` uses). Returns None on
    `OSError` OR `RuntimeError` (the latter raised by `Path.resolve` on
    symlink loops) so the caller can fall back to the bare `st_dev` line
    instead of crashing on pathological paths."""
    try:
        cur = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    try:
        while cur != cur.parent:
            if os.path.ismount(cur):
                return str(cur)
            cur = cur.parent
        return str(cur)   # filesystem root
    except OSError:
        return None


def _copy_and_verify(
    plan: Plan,
    on_state: Callable[[MigrationState], None] | None = None,
    *,
    source_fd: int,
    source_id: tuple[int, int] | None = None,
) -> None:
    with _copy_target_owner(plan.target) as owner:
        _copy_and_verify_owned(
            plan, owner, on_state=on_state, source_fd=source_fd, source_id=source_id)


def _copy_and_verify_owned(
    plan: Plan,
    owner: _CopyTargetOwner,
    on_state: Callable[[MigrationState], None] | None,
    source_fd: int,
    source_id: tuple[int, int] | None,
) -> None:
    mode_cache: dict[Path, int] = {}
    if plan.strict:
        _refuse_pinned_specials(source_fd, plan.source, mode_cache)
    try:
        skipped = copy_tree(plan.source, plan.target, mode_cache=mode_cache,
                            check_space=plan.check_space,
                            progress_cb=(_copy_progress_callback()
                                         if plan.progress else None),
                            source_fd=source_fd, _target_owner=owner)
        if on_state is not None:
            on_state(MigrationState.COPIED)  # rf-ddd-02: data on disk, pre-verify
        _report_skipped(skipped, plan.strict, mode_cache=mode_cache)
        if plan.verify:
            verify_copy(plan.source, plan.target, plan.checksum,
                        verify_ownership=plan.verify_ownership, jobs=plan.jobs,
                        source_fd=source_fd)
        else:
            _log().warning("verification disabled (--no-verify): the source %s "
                           "will be deleted without checking the copy", plan.source)
        if source_id is not None:
            _assert_source_identity(plan.source, source_id)
    except BaseException:
        # Verification joins its readers before raising, including interrupts.
        # The shared owner also distinguishes refused/preexisting targets from
        # this attempt's copy and protects replacements during cleanup.
        owner.cleanup("verification failed or copy failed")
        raise


def _copy_progress_callback() -> Callable[[int, int], None]:
    last = [-5]

    def report(done: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(done * 100 / total))
        milestone = percent // 5 * 5
        if milestone <= last[0]:
            return
        last[0] = milestone
        _log().info("copy progress: %d%% (%d/%d bytes)", percent, done, total)

    return report


def _report_skipped(skipped: list[Path], strict: bool, *,
                    mode_cache: dict[Path, int] | None = None) -> None:
    if not skipped:
        return
    counts = _group_specials_by_kind(skipped, mode_cache=mode_cache)
    summary = ", ".join(f"{n} {kind}" for kind, n in counts.items())
    # rf-obs-06: accumulate `st_size` of skipped entries so the operator
    # sees how many bytes won't be migrated. A 500MB device-backed FIFO
    # shows as "0 bytes" (kernel objects); a FIFO with queued payload
    # shows the real backing. Either way it's a number the operator
    # can act on.
    skipped_bytes = _sum_skipped_bytes(skipped)
    bytes_note = (
        f" ({skipped_bytes} byte(s) skipped)" if skipped_bytes > 0 else ""
    )
    _log().warning("skipped %d non-regular file(s): %s%s",
                   len(skipped), summary, bytes_note)
    for p in skipped[:5]:
        _log().info("  skipped: %s", p)
    if len(skipped) > 5:
        _log().info("  ... and %d more (use --strict to refuse instead)",
                    len(skipped) - 5)
    if strict:
        raise RuntimeError(
            f"strict mode: refusing to migrate, {len(skipped)} special file(s) "
            f"would be skipped ({summary})"
        )


def _sum_skipped_bytes(paths: list[Path]) -> int:
    """Best-effort `st_size` sum across skipped non-regular files (rf-obs-06).
    Per-entry stat failures are ignored — the warning is informational."""
    total = 0
    for p in paths:
        try:
            total += os.lstat(p).st_size
        except OSError:
            continue
    return total


def _group_specials_by_kind(paths: list[Path], *,
                            mode_cache: dict[Path, int] | None = None,
                            ) -> dict[str, int]:
    """Count entries per special-kind label.

    When `mode_cache` already holds the mode from the copy walk (rf-perf-03),
    we skip a second `lstat` per path. Cache-miss paths fall back to a
    fresh lstat so the function still works on inputs that don't carry
    a cache (e.g. unit tests)."""
    counts: dict[str, int] = {}
    for p in paths:
        mode: int | None = None
        if mode_cache is not None:
            mode = mode_cache.get(p)
        if mode is None:
            try:
                mode = os.lstat(p).st_mode
            except OSError:
                continue
        kind = _kind_of(mode) or "other"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


# --- CLI --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argparse parser. Extracted from parse_args
    (rf-arch-02) so tests can introspect/extend flags without invoking
    `parse_args`."""
    p = argparse.ArgumentParser(
        description="Move a directory to another filesystem and symlink it back.",
        # rf-cfg-50: these three knobs had a default, a typed accessor, and
        # validation, but no documented surface anywhere.
        epilog=(
            "Environment variables:\n"
            "  RELOCATE_STALE_PID_WARN_AT      stale-PID count at which the "
            f"open-file precheck warns (default: 1)\n"
            "  RELOCATE_DISK_SPACE_HEADROOM    destination free-space factor "
            f"over the payload size (default: {_DISK_SPACE_HEADROOM}; values "
            "below 1.0 clamp to 1.0)\n"
            "  RELOCATE_SHA256_RETRY_ATTEMPTS  verify-hash attempts per file "
            f"for a transient truncation (default: {_SHA256_RETRY_ATTEMPTS}; "
            "clamped to at least 1)\n"
            "An unparseable value falls back to the default."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", help="path to the directory to move (e.g. ~/.cache)")
    p.add_argument("dest_root", nargs="?", default=None,
                   help="parent directory on the destination filesystem "
                        "(omit only with --recover)")
    p.add_argument("--recover", action="store_true",
                   help="rf-rel-01: restore an orphaned <source>.relocate-backup "
                        "left by a run killed mid-swap (renames it back to "
                        "<source>); dest_root is not required. Runs the "
                        "open-files precheck on the backup unless --force "
                        "(rf-rel-03)")
    p.add_argument("--dry-run", action="store_true",
                   help="show what would happen, do nothing")
    p.add_argument("--no-verify", action="store_true",
                   help="skip post-copy verification entirely (source still deleted — unsafe)")
    p.add_argument("--no-checksum", action="store_true",
                   help="size-only verification instead of per-file SHA-256 "
                        "(faster; a same-size corrupted copy would pass before "
                        "the original is deleted)")
    p.add_argument("--strict", action="store_true",
                   help="refuse to migrate if any non-regular files are present "
                        "(default: skip sockets, FIFOs, devices and continue)")
    p.add_argument("--force", action="store_true",
                   help="skip the pre-flight open-files check; proceed even "
                        "when running processes hold files open under <source>")
    p.add_argument("--verify-ownership", action="store_true",
                   help="also verify uid/gid/mode on every entry (rf-rel-04): "
                        "catches botched chowns on cross-filesystem copies "
                        "before the source is deleted")
    p.add_argument("--strict-cross-device", action="store_true",
                   help="rf-sec-03: refuse to proceed when source and dest "
                        "share a filesystem (default: warn and continue)")
    p.add_argument("--jobs", "-j", type=_positive_jobs, default=None,
                   help="rf-scal-02: pool width for checksum verification "
                        "(default: min(32, cpu_count+4))")
    p.add_argument("--no-space-check", action="store_true",
                   help="rf-perf-02: skip the pre-copy disk-space walk (saves "
                        "a full tree lstat on very large trees; an ENOSPC "
                        "mid-copy is still handled with cleanup)")
    p.add_argument("--progress", action="store_true",
                   help="report copy progress in 5%% milestones")
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true",
                           help="rf-cfg-01: DEBUG-level logging")
    verbosity.add_argument("--quiet", action="store_true",
                           help="rf-cfg-01: WARNING-level logging (mute INFO)")
    return p


def parse_namespace(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI argv into a raw `argparse.Namespace` (rf-arch-02)."""
    return _build_parser().parse_args(argv)


def parse_args(argv: list[str] | None = None) -> Plan:
    """Convenience wrapper: parse argv -> Namespace -> validated `Plan`.

    Path normalisation, target derivation, and same-path validation live
    in `Plan.from_args` (rf-arch-01) so impossible states are unreachable
    via the Plan constructor."""
    return Plan.from_args(parse_namespace(argv))


def _format_shutil_error(err: shutil.Error, max_lines: int = 10) -> str:
    """Render a shutil.Error's list of (src, dst, why) tuples on multiple lines."""
    failures = list(err.args[0]) if err.args else []
    if not failures:
        return str(err)
    head = [f"copy reported {len(failures)} error(s):"]
    for src, _dst, why in failures[:max_lines]:
        head.append(f"  {src}")
        head.append(f"      -> {why}")
    remaining = len(failures) - max_lines
    if remaining > 0:
        head.append(f"  ... and {remaining} more")
    return "\n".join(head)


def _log_level(ns: argparse.Namespace) -> int:
    """Map the rf-cfg-01 verbosity flags to a logging level."""
    if getattr(ns, "verbose", False):
        return logging.DEBUG
    if getattr(ns, "quiet", False):
        return logging.WARNING
    return logging.INFO


# rf-plat-01: the migration is built on POSIX primitives with no Windows
# equivalent — `os.chown` carries uid/gid across the copy, and the source is
# opened `O_NOFOLLOW|O_DIRECTORY` so the swap cannot be raced through a
# substituted symlink. Both are already `getattr`-guarded at their call sites,
# which on a non-POSIX host would silently downgrade the anti-race open instead
# of failing. Probe the capabilities themselves rather than `os.name` so an
# unusual-but-capable runtime is not refused for its label.
POSIX_REQUIRED_OS_ATTRS = ("chown", "fwalk", "O_NOFOLLOW", "O_DIRECTORY")


def _posix_capability_gaps() -> list[str]:
    """Return the `os` names this script needs that the host does not provide."""
    return [name for name in POSIX_REQUIRED_OS_ATTRS if not hasattr(os, name)]


def main(argv: list[str] | None = None) -> int:
    ns = parse_namespace(argv)
    logging.basicConfig(level=_log_level(ns), format="%(levelname)s %(message)s")
    gaps = _posix_capability_gaps()
    if gaps:
        _log().error(
            "FAILED: relocate_folder is POSIX-only and this platform (%s) is "
            "missing %s. It preserves uid/gid with os.chown and opens the "
            "source with O_NOFOLLOW|O_DIRECTORY to keep the symlink swap safe "
            "from a substitution race; neither has a Windows equivalent.",
            sys.platform, ", ".join("os." + name for name in gaps),
        )
        return 2
    if getattr(ns, "recover", False):
        return _run_recover(ns)
    if ns.dest_root is None:
        _log().error("FAILED: dest_root is required unless --recover is given")
        return 2
    try:
        plan = Plan.from_args(ns)
    except (OSError, ValueError) as exc:
        _log().error(FAILED_MESSAGE, exc)  # NOSONAR -- expected CLI validation stays concise.
        return 2
    _log().info("plan: source=%s target=%s dry_run=%s verify=%s checksum=%s strict=%s",
                plan.source, plan.target, plan.dry_run, plan.verify,
                plan.checksum, plan.strict)
    try:
        result = execute(plan)
    except shutil.Error as exc:
        _log().error(  # NOSONAR -- a structured copy failure is already user-readable.
            "FAILED during copy:\n%s", _format_shutil_error(exc)
        )
        return 1
    except Exception as exc:
        _log().error(FAILED_MESSAGE, exc)  # NOSONAR -- CLI failures intentionally omit tracebacks.
        return 1
    _log().info(result)
    return 0


def _run_recover(ns: argparse.Namespace) -> int:
    """Handle `--recover` (rf-rel-01): restore an orphaned backup."""
    if getattr(ns, "dest_root", None) is not None:
        # rf-rel-03: --recover only needs <source>; a positional dest_root is
        # silently dropped. Warn so a typo'd invocation (or a user expecting
        # the dir to be relocated) isn't surprised by the no-op on dest_root.
        _log().warning(
            "--recover ignores dest_root (%s); recovery only restores the "
            "<source>.relocate-backup directory to <source>", ns.dest_root,
        )
    source = Path(ns.source).expanduser().absolute()
    if not source.name:
        _log().error("FAILED: source has no basename to recover: %s", source)
        return 1
    backup_path = source.with_name(source.name + BACKUP_SUFFIX)
    if getattr(ns, "dry_run", False):
        backup = _orphaned_backup(source)
        if backup is None:
            _log().error("FAILED: no orphaned backup to recover at %s", backup_path)
            return 1
        _log().info("dry-run: would recover %s -> %s", backup, source)
        return 0
    try:
        result = recover(source, force=getattr(ns, "force", False))
    except Exception as exc:
        _log().error(FAILED_MESSAGE, exc)  # NOSONAR -- CLI failures intentionally omit tracebacks.
        return 1
    _log().info(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
