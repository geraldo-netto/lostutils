#!/usr/bin/env python3
"""Relocate a (hidden) directory to another filesystem and replace it with a symlink.

Typical use: move heavyweight cache/state directories off the home volume.

Example:
    sudo python relocate.py ~/.cache /backups/disk1/apps/profile
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

Caveats
-------
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
import stat
import sys
import tempfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
import contextvars
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

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


# rf-test-01: contextvar-backed hash injection. `_verify_content` calls
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
STAGING_PREFIX = ".relocate-stage-"


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
    except (PermissionError, OSError) as exc:
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
    except (PermissionError, OSError) as exc:
        raise RuntimeError(f"required {label} failed: {exc}") from exc


# Back-compat aliases (rf-arch-08): keep the old names so external
# callers / tests that imported `_safe` / `_required` continue to work.
# The verb-prefixed names are the source of truth.
_safe = _swallow_or_warn
_required = _raise_or_fail


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
        if src == target:
            raise ValueError(f"source equals computed target: {src}")
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
        )


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
    symlink, and ``O_DIRECTORY`` fails (ENOTDIR) if it is no longer a directory —
    closing the swap-for-symlink TOCTOU at open time. The caller holds the fd
    open across the migration (pinning the inode) and re-checks the path's
    identity against this fstat right before copying via
    :func:`_assert_source_identity`."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
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
    for path in created:
        os.mkdir(path, mode=0o700)
        # rf-rel-09: chown/chmod at setup time are correctness operations.
        # A silent best-effort failure leaves the dest dir with wrong owner
        # or wrong perms — the user only finds out at first access, long
        # after the source is gone. Raise via _required so the migration
        # aborts loudly instead of corrupting silently.
        _raise_or_fail(
            f"chown {path} to uid={st.st_uid} gid={st.st_gid}",
            os.chown, path, st.st_uid, st.st_gid,
        )
        _raise_or_fail(f"chmod {path} to 0o755", os.chmod, path, 0o755)
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


def already_migrated(source: Path, target: Path) -> bool:
    if not source.is_symlink():
        return False
    link_target = Path(os.readlink(source))
    if not link_target.is_absolute():
        link_target = source.parent / link_target
    try:
        if link_target.resolve(strict=False) != target.resolve(strict=False):
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
    if not source.is_symlink():
        return False
    link_target = Path(os.readlink(source))
    if not link_target.is_absolute():
        link_target = source.parent / link_target
    try:
        if link_target.resolve(strict=False) != target.resolve(strict=False):
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
    proc_dir = Path("/proc")
    if not proc_dir.is_dir():
        return OpenFileSnapshot(holders=(), stale_pids=0)
    src_resolved = source.resolve()
    results: list[tuple[int, str, tuple[Path, ...]]] = []
    stale = 0
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        files = _process_open_files_in(entry, src_resolved)
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
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "?"


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

def copy_tree(src: Path, dst: Path, *,
              mode_cache: dict[Path, int] | None = None,
              jobs: int | None = None,
              progress_cb: "Callable[[int, int], None] | None" = None,
              check_space: bool = True,
              ) -> list[Path]:
    """Copy src -> dst recursively, skipping non-regular files. Returns
    skipped paths.

    When `mode_cache` is supplied, the `lstat().st_mode` looked up by the
    ignore-callback is cached into it so the post-copy classification in
    `_group_specials_by_kind` skips a second `lstat` per skipped entry
    (rf-perf-03). Backward compatible: callers that don't care omit it.

    `progress_cb(bytes_done, bytes_total)` (rf-obs-01) is invoked after
    each completed file copy when supplied. Total bytes are computed up
    front via `_src_total_bytes`. The cb is wrapped in a copy_function
    shim around `shutil.copy2` so per-file accounting needs no walk of
    its own.

    rf-perf-02: `check_space=False` skips the pre-copy disk-space walk
    (`_src_total_bytes` + `_check_disk_space`). On a multi-TB tree that
    full lstat walk is itself expensive and merely duplicates the walk
    `shutil.copytree` does anyway; an ENOSPC mid-copy still triggers the
    same cleanup. The walk is also skipped entirely when neither the
    precheck nor a `progress_cb` needs the byte total."""
    if _path_taken(dst):
        raise FileExistsError(f"target already exists: {dst}")
    # rf-rel-14 / rf-perf-02: compute total bytes ONCE and share between the
    # precheck and (when present) the progress callback. The walk runs only
    # when something needs the total: the disk-space precheck (unless
    # disabled) or a progress callback. Otherwise it's skipped outright.
    apparent_total = 0
    alloc_total = 0
    if check_space or progress_cb is not None:
        apparent_total, alloc_total = _src_size_totals(src)
    if check_space:
        # rf-perf-06: size the precheck by allocated bytes, not apparent size.
        _check_disk_space(src, dst, total_bytes=alloc_total)
    skipped: list[Path] = []
    cache = mode_cache if mode_cache is not None else {}
    copy_function = shutil.copy2
    if progress_cb is not None:
        total = apparent_total
        done = [0]

        def tracking_copy2(s, d, *, follow_symlinks=True):
            result = shutil.copy2(s, d, follow_symlinks=follow_symlinks)
            try:
                # rf-rel-05 / rf-robust-04: account the bytes actually written
                # at the DESTINATION. Statting the source would let a concurrent
                # writer changing `s` between copytree's read and here skew
                # `done` past (or below) the total; the freshly-written `d` is
                # stable. follow_symlinks=False copies the link itself, so the
                # dst is a symlink — lstat it; otherwise it's the regular file.
                stat_fn = os.stat if follow_symlinks else os.lstat
                done[0] += stat_fn(d).st_size
            except OSError:
                pass
            try:
                progress_cb(done[0], total)
            except Exception:   # pragma: no cover - cb is user code
                pass
            return result

        copy_function = tracking_copy2
    try:
        shutil.copytree(
            src, dst,
            symlinks=True,
            copy_function=copy_function,
            ignore=_make_ignore_specials(skipped, cache),
        )
    except BaseException:
        # rf-robust-02: catch BaseException (not just Exception) so a
        # KeyboardInterrupt mid-copy also cleans the half-written `dst` instead
        # of leaving a partial target that blocks the O_EXCL/_path_taken retry.
        # rf-rel-11: log cleanup failures so an operator knows when a
        # half-written `dst` survived a failed copy (read-only mount,
        # permission-denied target). The original exception is still
        # raised — the warning is informational.
        rmtree_failures: list[tuple[str, OSError]] = []

        def _record(_fn, path, excinfo):
            exc = excinfo[1] if isinstance(excinfo, tuple) else excinfo
            if isinstance(exc, OSError):   # pragma: no branch - shutil.rmtree only passes OSError-shaped excinfo
                rmtree_failures.append((str(path), exc))

        shutil.rmtree(dst, onerror=_record)
        for path, exc in rmtree_failures[:10]:
            _log().warning(
                "cleanup after failed copy: could not remove %s: %s",
                path, exc,
            )
        if len(rmtree_failures) > 10:
            _log().warning(
                "cleanup: %d more removal errors suppressed; "
                "manual cleanup of %s may be needed",
                len(rmtree_failures) - 10, dst,
            )
        raise
    _replicate_ownership(src, dst, jobs=jobs)
    return skipped


# Headroom factor: filesystems need a little slack for metadata, journals, and
# block-size rounding. 5% over the raw payload is conservative enough to catch
# the "destination is exactly the right size minus a few KiB" trap.
_DISK_SPACE_HEADROOM = 1.05


def _check_disk_space(src: Path, dst: Path, *, total_bytes: int | None = None) -> None:
    """Pre-flight check that the destination filesystem has room for `src`
    (rf-rel-07). An ENOSPC mid-copy already triggers a cleanup, but the
    user-visible failure is then "copy errored, source still present"; a
    precheck fails immediately and cheaply.

    Uses `_src_total_bytes` (a one-pass lstat walk) and `shutil.disk_usage` on
    the nearest existing ancestor of `dst`. Raises `RuntimeError` when free
    space is below `_DISK_SPACE_HEADROOM * needed`.

    rf-rel-14: the check is BEST-EFFORT. The tree can grow between this
    call and `shutil.copytree`'s own walk; an ENOSPC mid-stream is still
    possible. Callers that already computed the allocated total (e.g.
    `copy_tree`) pass `total_bytes=` to skip the second walk.

    rf-perf-06: ``total_bytes`` is the ALLOCATED size (``st_blocks * 512``),
    not the apparent ``st_size``, so sparse files aren't over-counted."""
    needed = total_bytes if total_bytes is not None else _src_size_totals(src)[1]
    probe = dst
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return  # can't check; let the copy try
    required = int(needed * _DISK_SPACE_HEADROOM)
    if free < required:
        raise RuntimeError(
            f"insufficient space on destination filesystem at {probe}: "
            f"need ~{required} bytes (incl. headroom for ~{needed} of payload), "
            f"have {free} free"
        )


def _src_size_totals(src: Path) -> tuple[int, int]:
    """One walk returning ``(apparent_bytes, allocated_bytes)`` for regular
    files under ``src`` (rf-perf-06). ``apparent`` (``st_size``) drives the
    progress bar; ``allocated`` (``st_blocks * 512``) drives the disk-space
    precheck, so a sparse file whose holes the copy preserves isn't counted at
    its full logical size and falsely refused for "insufficient space".
    Non-regular files are zero-cost in the copy; symlinks are not followed."""
    apparent = 0
    allocated = 0
    for path, _dir_names, names in os.walk(src, followlinks=False):
        base = Path(path)
        for name in names:
            try:
                st = (base / name).lstat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                apparent += st.st_size
                allocated += st.st_blocks * 512
    return apparent, allocated


def _src_total_bytes(src: Path) -> int:
    """Apparent sum of regular-file sizes under `src` (rf-rel-07). Non-regular
    files (sockets, FIFOs, devices) are zero-cost in the copy, so they're
    excluded. Symlinks are not followed."""
    return _src_size_totals(src)[0]


def _make_ignore_specials(skipped: list[Path], mode_cache: dict[Path, int]):
    """Build a shutil.copytree ignore callback that filters non-regular files.

    Captures the lstat mode of each skipped entry into `mode_cache` so the
    later kind-summary pass can avoid a second syscall per path
    (rf-perf-03)."""
    def _ignore(directory: str, names: list[str]) -> list[str]:
        out: list[str] = []
        for name in names:
            full = Path(directory) / name
            try:
                mode = os.lstat(full).st_mode
            except OSError:
                continue
            if _is_special_file(mode):
                skipped.append(full)
                mode_cache[full] = mode
                out.append(name)
        return out
    return _ignore




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
    override via `Plan.jobs` and so the verify + ownership pools share
    one source of truth (rf-arch-06 unified the loop; this unifies the
    sizing)."""
    return min(32, (os.cpu_count() or 1) + 4)


# rf-conc-01: lots of small chown syscalls fan out well. The constant is
# kept for backward compat with tests / external introspection; the live
# value used by the pools is taken from `Plan.jobs` (defaults to
# `_default_worker_count()`).
_OWNERSHIP_WORKERS = _default_worker_count()
_OWNERSHIP_INFLIGHT = _OWNERSHIP_WORKERS * 4  # bound on queued+running futures (rf-scal-03)


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
    (rf-arch-06). Shared by `_replicate_ownership` and `_run_verify_pool`.

    `on_done(fut)` is invoked for every completed Future and returns True
    to short-circuit the submission loop (the verify pool aborts on first
    error; the ownership pool never aborts). Streaming preserves the
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
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                if on_done(fut):
                    abort = True
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


def _replicate_ownership(src: Path, dst: Path, *, jobs: int | None = None) -> None:
    """Replicate src's uid/gid onto every counterpart in dst, in parallel
    (rf-conc-01) — one chown per file/dir is fast individually but a sequential
    pass dominates the migration of large trees.

    Pairs are streamed lazily from `_pair_walk` (rf-scal-03): on a tree with
    millions of entries we never materialise the full `(src, dst)` list, only
    `_OWNERSHIP_INFLIGHT` pairs at a time. Unexpected exceptions are collected
    and logged instead of being swallowed by the executor's iterator
    (rf-conc-02); known `PermissionError` / `FileNotFoundError` paths inside
    `_chown_pair` continue to log-and-skip per-entry.

    rf-conc-05: the producer (`_pair_walk` / `os.walk`) can raise mid-stream
    when src disappears. The bare iterator previously let that propagate
    through `_run_streamed`, which aborted the executor context and left the
    operator with no visibility into how much partial ownership was applied.
    We wrap the producer so the walk error is logged with the count of pairs
    processed so far, and inflight tasks still drain cleanly."""
    errors: list[BaseException] = []
    processed = [0]
    walk_error: list[OSError] = []

    def on_done(fut: "Future") -> bool:
        _collect_chown_error(fut, errors)
        processed[0] += 1
        return False  # ownership never short-circuits

    def counted_pairs():
        try:
            yield from _pair_walk(src, dst)
        except OSError as exc:
            # rf-conc-05: surface the walk error rather than letting it abort
            # the executor context with inflight chowns in unknown state.
            walk_error.append(exc)
            return

    workers = _resolved_jobs(jobs)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        _run_streamed(
            partial(ex.submit, _chown_pair),
            counted_pairs(),
            _inflight_cap(workers),
            on_done,
        )
    if walk_error:
        _log().warning(
            "ownership replication: walk failed after %d pair(s): %s",
            processed[0], walk_error[0],
        )
    if errors:
        _log().warning("ownership replication: %d worker(s) raised unexpectedly "
                       "(first: %s)", len(errors), errors[0])


def _collect_chown_error(fut, errors: list[BaseException]) -> None:
    """Drain one ownership future, recording unexpected exceptions for the
    end-of-walk summary (rf-conc-02). Expected failures are already logged
    inside `_chown_pair`; this only catches the surprises."""
    exc = fut.exception()
    if exc is not None:
        errors.append(exc)


def _chown_pair(pair: tuple[Path, Path]) -> None:
    """Replicate one (src, dst) pair's uid/gid. We do NOT pre-check existence:
    the dst can vanish between the check and the chown (rf-conc-03), and the
    common skip-during-copy case (sockets, FIFOs) shows up as
    `FileNotFoundError` from `os.lstat` or `os.chown` anyway.

    rf-rel-13: `ValueError` from a NUL byte in a path (`os.chown` /
    `os.lstat` raise it bare instead of `OSError`) is caught here and
    treated as a per-entry skip. Without this clause the error would
    escape `_chown_pair`, bubble through `_collect_chown_error`, and
    surface only as "N workers raised unexpectedly (first: ValueError)"
    in the end-of-walk summary — with no attribution to the bad path."""
    src_path, dst_path = pair
    try:
        st = os.lstat(src_path)
        os.chown(dst_path, st.st_uid, st.st_gid, follow_symlinks=False)
    except FileNotFoundError:
        return  # dst skipped during copy (socket / FIFO / device) or vanished
    except (PermissionError, OSError) as exc:
        _log().warning("could not chown %s: %s", dst_path, exc)
    except ValueError as exc:
        # rf-rel-13: NUL byte in src/dst path. Attributed here so the
        # operator sees which entry caused it.
        _log().warning("could not chown %s (invalid path): %s", dst_path, exc)


def _pair_walk(src: Path, dst: Path) -> Iterable[tuple[Path, Path]]:
    yield src, dst
    for root, dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        for name in (*dirs, *files):
            yield Path(root) / name, dst / rel / name


_VERIFY_WORKERS = _OWNERSHIP_WORKERS  # rf-perf-02: reuse pool sizing
_VERIFY_INFLIGHT = _OWNERSHIP_INFLIGHT


def verify_copy(src: Path, dst: Path, checksum: bool = False,
                verify_ownership: bool = False,
                *, jobs: int | None = None) -> None:
    """Verify dst is a faithful copy of src in a SINGLE walk over src,
    comparing each entry against its counterpart on the fly — no full
    in-memory inventories and one tree traversal instead of two (rf-perf-01 /
    rf-scal-01). Files (and dirs/symlinks) are created in dst only from src, so
    a one-sided src walk catches every missing/changed entry; extra dst-only
    entries can't arise because copy_tree refuses a pre-existing target.

    When `verify_ownership` is True, every counterpart's mode/uid/gid is
    compared against src (rf-rel-04).

    When `checksum` is True the per-file SHA-256 work is fanned out across
    `_VERIFY_WORKERS` threads (rf-perf-02). Verification used to dominate
    wall time on content-heavy migrations because the existing
    ownership-replication pool was unused once we left `copy_tree`.

    Task contract (rf-scal-04)
    --------------------------
    Tasks reach `_run_verify_pool` as an `Iterator[Callable[[], None]]`
    drained lazily by `_run_streamed`'s inflight cap. This is deliberate:
    a `Sequence`-based API would force materialising the full
    `[task per file]` list on every migration, and on TB-scale trees
    that list itself would be hundreds of MB of `partial` objects
    before the first hash even starts. The streaming shape keeps RSS
    bounded by the inflight cap regardless of tree size, at the cost
    of giving up the ability to report a stable `len(tasks)` upfront."""
    tasks = _iter_verify_tasks(src, dst, checksum, verify_ownership)
    if not checksum:
        # cheap stat-only / readlink ops: sequential is fast and keeps the
        # error path deterministic for tests.
        for task in tasks:
            task()
        return
    _run_verify_pool(tasks, jobs=jobs)


def _iter_verify_tasks(src: Path, dst: Path, checksum: bool,
                       verify_ownership: bool) -> Iterator[Callable[[], None]]:
    """Yield zero-arg callables, one per check, for parallel execution.

    Symlink/dir checks are cheap but ride along in the same iterator so the
    walk happens only once. Ownership is appended after the kind-specific
    check so a missing entry fails with the more useful message first.

    rf-perf-01: each entry is lstat'd ONCE here and classified from
    `st_mode` (S_ISLNK/S_ISREG/S_ISDIR) instead of the previous
    `is_symlink()` + `is_file()` + `is_dir()` trio, which issued up to three
    stat syscalls per entry on large trees. The known src `st_size` is passed
    into the file check so `_verify_size` skips re-lstat'ing the source."""
    for full in _walk_entries(src):
        rel = full.relative_to(src)
        counterpart = dst / rel
        try:
            st = os.lstat(full)
        except OSError as exc:
            # rf-rel-01: a src entry that became unreadable mid-run must NOT be
            # silently skipped — the copy would be accepted and the source then
            # deleted. Yield a task that raises so verify_copy fails loudly.
            yield partial(_verify_unreadable_src, full, rel, exc)
            st = None
        is_copied_kind = False
        if st is not None and stat.S_ISLNK(st.st_mode):
            yield partial(_verify_symlink, full, counterpart, rel)
            is_copied_kind = True
        elif st is not None and stat.S_ISREG(st.st_mode):
            yield partial(_verify_file, full, counterpart, rel, checksum,
                          src_size=st.st_size)
            is_copied_kind = True
        elif st is not None and stat.S_ISDIR(st.st_mode):
            yield partial(_verify_dir, full, counterpart, rel)
            is_copied_kind = True
        # rf-rel-02: only verify ownership for entries copy_tree actually copied
        # (symlink/regular/dir). A skipped special file (socket/FIFO/device) has
        # no dst counterpart, so its ownership check would lstat a missing path
        # and fail the whole --verify-ownership migration.
        if verify_ownership and is_copied_kind:
            yield partial(_verify_ownership, full, counterpart, rel)


def _run_verify_pool(tasks: Iterator[Callable[[], None]], *,
                     jobs: int | None = None) -> None:
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


def _verify_unreadable_src(src_path: Path, rel: Path, exc: OSError) -> None:
    """Fail verification for a src entry that couldn't be lstat'd (rf-rel-01).

    Classification needs the src mode; when lstat fails we can't say whether the
    copy is faithful, so verification must not pass. Raising here (rather than
    skipping the entry) keeps the source from being deleted on a copy we never
    confirmed."""
    raise RuntimeError(
        f"could not stat source entry during verify: {rel} "
        f"(src={src_path}): {exc}"
    ) from exc


def _verify_dir(src_dir: Path, dst_dir: Path, rel: Path) -> None:
    if src_dir.is_symlink() or not dst_dir.is_dir() or dst_dir.is_symlink():
        # rf-obs-02: include absolute src path so log lines are actionable
        # without having to mentally join `rel` to the migration root.
        raise RuntimeError(f"missing directory in copy: {rel} (src={src_dir})")


def _verify_ownership(src_path: Path, dst_path: Path, rel: Path) -> None:
    """Fail if uid/gid/mode on `dst_path` don't match `src_path` (rf-rel-04).
    Uses lstat so the comparison covers symlinks themselves, not their targets."""
    try:
        s = src_path.lstat()
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


def _verify_symlink(src_link: Path, dst_link: Path, rel: Path) -> None:
    if not dst_link.is_symlink():
        raise RuntimeError(f"missing symlink in copy: {rel} (src={src_link})")
    if os.readlink(src_link) != os.readlink(dst_link):
        raise RuntimeError(
            f"symlink target mismatch for {rel} (src={src_link}): "
            f"{os.readlink(src_link)!r} != {os.readlink(dst_link)!r}")


def _verify_file(src_file: Path, dst_file: Path, rel: Path, checksum: bool,
                 *, src_size: int | None = None) -> None:
    """Compose size and (optionally) content verification (rf-cx-01).

    Split out so each branch is independently testable and so the parallel
    pool can swap the heavy checksum step in/out without touching the
    cheap kind check.

    rf-perf-01: `src_size` may be supplied by the caller (it already lstat'd
    `src_file` to classify it) so `_verify_size` doesn't re-stat the source.
    Direct callers that omit it fall back to a fresh lstat."""
    if dst_file.is_symlink() or not dst_file.is_file():
        raise RuntimeError(f"missing file in copy: {rel} (src={src_file})")
    _verify_size(src_file, dst_file, rel, src_size=src_size)
    if checksum:
        _verify_content(src_file, dst_file, rel)


def _verify_size(src_file: Path, dst_file: Path, rel: Path,
                 *, src_size: int | None = None) -> None:
    if src_size is None:
        src_size = src_file.lstat().st_size
    dst_size = dst_file.lstat().st_size
    if src_size != dst_size:
        raise RuntimeError(
            f"size mismatch for {rel} (src={src_file}): {src_size} != {dst_size}"
        )


def _verify_content(src_file: Path, dst_file: Path, rel: Path) -> None:
    """Hash both sides and compare.

    Invariant (rf-rel-16): the caller MUST have run :func:`_verify_size`
    first. If a writer truncates `src_file` AFTER the size match but
    BEFORE / DURING this content hash, the hash succeeds on the shorter
    byte stream and `_sha256` re-stats the file after closing to detect
    post-hoc truncation — a mismatch between the hashed length and the
    final size is reported as a typed error so the operator can rerun.

    rf-test-01: the hash function is taken from `_hash()` (a contextvar,
    defaulting to `_sha256`) so tests can inject a blocking/instrumented hash
    to drive the verify-pool race paths.
    """
    hash_fn = _hash()
    if hash_fn(src_file) != hash_fn(dst_file):
        raise RuntimeError(f"hash mismatch for {rel} (src={src_file})")


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
    to ``_SHA256_RETRY_ATTEMPTS`` times for the typed
    ``_HashTruncatedError`` / ``_HashVanishedError`` cases ONLY. Any
    other exception escapes immediately so a hashlib backend failure
    (or any future error shape) doesn't masquerade as a transient and
    burn a retry on a non-retryable condition."""
    last_exc: BaseException | None = None
    for attempt in range(_SHA256_RETRY_ATTEMPTS):
        try:
            return _hash_once_strict(path)
        except (_HashTruncatedError, _HashVanishedError) as exc:
            last_exc = exc
            continue
    # rf-rel-20: was `assert last_exc is not None` — stripped under `python -O`,
    # so a misconfigured _SHA256_RETRY_ATTEMPTS=0 silently returned None and
    # downstream digest compares blew up far from the cause. A real `raise`
    # survives optimization and names the misconfiguration.
    if last_exc is None:
        raise RuntimeError(
            f"_sha256({path}): no attempts ran; "
            f"_SHA256_RETRY_ATTEMPTS={_SHA256_RETRY_ATTEMPTS}"
        )
    raise last_exc


_SHA256_RETRY_ATTEMPTS = 2


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

@contextmanager
def _backup_target(target: Path) -> Iterator[Path]:
    """Move `target` aside to a sibling `<name>.relocate-backup`, yield the
    backup path, restore-or-clean on exit (rf-cx-02).

    Exit semantics:
      * normal completion -> shutil.rmtree(backup); failure to remove the
        backup is warned but not raised (rf-rel-03).
      * exception inside the `with` -> rename(backup, target) to restore;
        failure to restore is logged with explicit `mv` recovery
        instructions (rf-rel-05). The original exception is then re-raised.

    rf-sec-03: the backup's (st_dev, st_ino) is captured right after the
    rename-aside. On a world-writable parent an attacker could swap a
    symlink/different inode in at the backup name during the `with` body;
    restoring that onto `target` would silently install attacker-controlled
    content. Both the restore path and the success rmtree path re-lstat the
    backup and refuse to act when the identity no longer matches."""
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    if _path_taken(backup):
        raise FileExistsError(
            f"stale backup exists, refusing to overwrite: {backup}"
        )
    os.rename(target, backup)
    backup_st = os.lstat(backup)
    backup_id = (backup_st.st_dev, backup_st.st_ino)
    try:
        yield backup
    except BaseException:
        # rf-robust-03: BaseException (not just Exception) so a KeyboardInterrupt
        # /SystemExit between the rename-aside and the symlink restores the
        # source instead of leaving it missing with only <name>.relocate-backup.
        # Abrupt death (SIGKILL/power-loss) still falls to --recover.
        if not _backup_identity_ok(backup, backup_id):
            _log().error(
                "atomic_swap failed AND the backup at %s was substituted "
                "(inode/device changed since it was moved aside); refusing to "
                "restore it onto %s. Inspect both paths manually.",
                backup, target,
            )
            raise
        try:
            os.rename(backup, target)
        except OSError as restore_exc:
            _log().error(
                "atomic_swap failed AND rollback failed: %s is gone and the "
                "backup at %s could not be moved back (%s). Run "
                "`mv %s %s` manually before re-running.",
                target, backup, restore_exc, backup, target,
            )
        raise
    if not _backup_identity_ok(backup, backup_id):
        _log().warning(
            "backup at %s was substituted since it was moved aside "
            "(inode/device changed); leaving it in place instead of removing "
            "it — inspect it manually", backup,
        )
        return
    _swallow_or_warn(f"remove backup {backup}", shutil.rmtree, backup)


def _backup_identity_ok(backup: Path, expected: tuple[int, int]) -> bool:
    """True when `backup`'s current (st_dev, st_ino) still matches `expected`
    (rf-sec-03). A vanished or substituted backup returns False."""
    try:
        st = os.lstat(backup)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == expected


def atomic_swap(source: Path, target: Path) -> None:
    """Replace `source` (a real dir) with a symlink to `target` (rf-arch-01).

    NOT atomic across process death, despite the name. The name is retained
    only for backward compatibility with existing callers and tests; treat it
    as `swap_with_backup`.

    The sequence is:

      1. `os.rename(source, source<BACKUP_SUFFIX>)`  — move the real dir aside
      2. `os.symlink(target, source)`                — only THIS step is atomic
      3. `shutil.rmtree(backup)`                      — delete the moved-aside dir

    A SIGKILL / power-loss between steps 1 and 2 leaves `source` renamed to
    `<name>.relocate-backup` with no symlink in place — see rf-rel-01 for the
    orphaned-backup detection and `--recover` recovery path. The in-`with`
    failure path (an exception from step 2) rolls the rename back; only an
    abrupt process death skips the rollback.

    Delegates the rename-aside / restore-or-clean lifecycle to
    `_backup_target`.

    rf-sec-02: the source's uid/gid are captured BEFORE `_backup_target` moves
    the real dir aside (afterwards `source` no longer exists to stat), so the
    replacement symlink can be lchown'd to the original owner. When run as root
    migrating a user-owned dir this keeps the symlink owned by the user instead
    of root."""
    src_st = os.lstat(source)
    with _backup_target(source):
        _create_symlink(source, target, owner=(src_st.st_uid, src_st.st_gid))


def _create_symlink(link: Path, target: Path, *,
                    owner: "tuple[int, int] | None" = None) -> None:
    """Create `link -> target` atomically (rf-sec-01).

    Symlink is staged inside a freshly-mkdtemp'd directory beside `link` so
    that the path used for `os.symlink` is one we just created — no other
    process can have stamped a different inode onto it. The single
    `os.rename` into place is then atomic on the parent filesystem.

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
    try:
        staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=str(parent)))
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
        # rf-sec-02: refuse to clobber a pre-existing entry at `link`. The
        # plain os.rename silently replaces a file/symlink an attacker may
        # have pre-created at the link name on a world-writable parent, with
        # no audit. Assert the name is absent first. A residual TOCTOU window
        # remains between this lstat and the rename (renameat2 RENAME_NOREPLACE
        # would close it, but stdlib `os` doesn't expose it); the check still
        # turns a silent clobber into a loud refusal for the common case.
        if _path_taken(link):
            raise FileExistsError(
                f"refusing to overwrite existing path at link target: {link} "
                f"(an unexpected file/symlink is already there)"
            )
        os.rename(tmp, link)
        if owner is not None:
            # rf-sec-02: lchown the symlink itself (not its target) to the
            # captured source owner so a root-run migration doesn't leave a
            # root-owned symlink in a user's tree.
            _chown_to_owner(link, owner)
    finally:
        _cleanup_staging(staging)


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

    An orphan exists when `atomic_swap` died between `os.rename(source, backup)`
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


_RENAME_NOREPLACE = 1  # linux/fs.h: fail with EEXIST if the new path exists


def _rename_noreplace(src: Path, dst: Path) -> None:
    """Rename ``src`` -> ``dst`` failing with ``FileExistsError`` if ``dst``
    exists (rf-dist-01). Uses ``renameat2(RENAME_NOREPLACE)`` on Linux to close
    the gate→rename TOCTOU where a concurrently recreated ``dst`` would be
    silently clobbered by plain ``os.rename``. Falls back to ``os.rename`` (with
    the documented single-operator assumption) where the syscall/flag is
    unavailable."""
    import ctypes
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (OSError, AttributeError):
        os.rename(src, dst)  # no renameat2: single-operator assumption applies
        return
    # rf-sec-10: pin the prototype so ctypes marshals the args at the right
    # widths (two int fds, two char* paths, one unsigned-int flag) instead of
    # relying on default int marshalling, which can mis-pass pointers/flags.
    renameat2.restype = ctypes.c_int
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    AT_FDCWD = -100
    res = renameat2(AT_FDCWD, os.fsencode(str(src)),
                    AT_FDCWD, os.fsencode(str(dst)), _RENAME_NOREPLACE)
    if res == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(
            f"refusing to recover: {dst} reappeared during recovery; "
            f"resolve it manually before restoring {src}")
    if err in (errno.ENOSYS, errno.EINVAL):
        os.rename(src, dst)  # kernel/fs without RENAME_NOREPLACE: fall back
        return
    raise OSError(err, os.strerror(err))


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
        if already_migrated(plan.source, plan.target):
            _advance(MigrationState.ALREADY_MIGRATED)
            return f"skipped: {plan.source} already symlinks to {plan.target}"
        if _symlink_points_at_empty_target(plan.source, plan.target):
            # rf-rel-01: the symlink is correct but the target was later
            # emptied. validate_source would crash with "already a symlink";
            # treat it as a legitimately-migrated dir and skip instead.
            _advance(MigrationState.ALREADY_MIGRATED)
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
            _advance(MigrationState.DRY_RUN)
            return f"dry-run: would migrate {plan.source} -> {plan.target}"
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
                _copy_and_verify(plan, on_state=_advance)
                _advance(MigrationState.VERIFIED)
                atomic_swap(plan.source, plan.target)
                _advance(MigrationState.SWAPPED)
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
    try:
        src_dev = probe_stat(plan.source).st_dev
        probe = _nearest_existing_dir(plan.target.parent)
        if probe is None:
            return
        dst_dev = probe_stat(probe).st_dev
    except OSError:
        return
    if src_dev != dst_dev:
        return
    # rf-obs-05: include the mount point so the operator doesn't have to
    # run `stat -c '%m'` themselves to figure out which volume to switch.
    mount = _device_mount_point(plan.source)
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


def _copy_and_verify(plan: Plan, on_state: Callable[[MigrationState], None] | None = None) -> None:
    mode_cache: dict[Path, int] = {}
    skipped = copy_tree(plan.source, plan.target, mode_cache=mode_cache,
                        jobs=plan.jobs, check_space=plan.check_space)
    if on_state is not None:
        on_state(MigrationState.COPIED)  # rf-ddd-02: data on disk, pre-verify
    try:
        _report_skipped(skipped, plan.strict, mode_cache=mode_cache)
        if plan.verify:
            verify_copy(plan.source, plan.target, plan.checksum,
                        verify_ownership=plan.verify_ownership, jobs=plan.jobs)
        else:
            _log().warning("verification disabled (--no-verify): the source %s "
                           "will be deleted without checking the copy", plan.source)
    except BaseException:
        # rf-robust-02: BaseException (not just Exception) so a Ctrl+C during
        # verify also removes the partial target rather than blocking retry.
        # rf-rel-03 / rf-conc-01: rmtree(plan.target) is reached only AFTER
        # `verify_copy` has returned or raised. The rmtree-vs-read join
        # guarantee is specific to the CHECKSUM branch: there `verify_copy`
        # -> `_run_verify_pool` joins every still-running SHA-256 worker
        # (shutdown(wait=True)) before propagating, so no hash thread is
        # mid-read of a file under `plan.target` when we delete it. The
        # size-only / ownership branch (checksum=False) runs its tasks
        # sequentially with no pool, so there is no worker thread to race in
        # the first place.
        #
        # rf-rel-06: log the destruction. A transient verify failure otherwise
        # silently wipes a half-good target with no audit trail (unlike the
        # copy_tree cleanup path, which logs rmtree failures). The operator
        # needs to know the partial copy is gone before re-running.
        _log().warning(
            "removing target %s after verification failed; the partial copy "
            "has been deleted and the source is untouched — re-run to retry",
            plan.target,
        )
        shutil.rmtree(plan.target, ignore_errors=True)
        raise


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
        description="Move a directory to another filesystem and symlink it back."
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
                   help="rf-scal-02: pool width for ownership + verify "
                        "(default: min(32, cpu_count+4))")
    p.add_argument("--no-space-check", action="store_true",
                   help="rf-perf-02: skip the pre-copy disk-space walk (saves "
                        "a full tree lstat on very large trees; an ENOSPC "
                        "mid-copy is still handled with cleanup)")
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ns = parse_namespace(argv)
    if getattr(ns, "recover", False):
        return _run_recover(ns)
    if ns.dest_root is None:
        _log().error("FAILED: dest_root is required unless --recover is given")
        return 2
    plan = Plan.from_args(ns)
    _log().info("plan: source=%s target=%s dry_run=%s verify=%s checksum=%s strict=%s",
                plan.source, plan.target, plan.dry_run, plan.verify,
                plan.checksum, plan.strict)
    try:
        result = execute(plan)
    except shutil.Error as exc:
        _log().error("FAILED during copy:\n%s", _format_shutil_error(exc))
        return 1
    except Exception as exc:
        _log().error("FAILED: %s", exc)
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
    try:
        result = recover(source, force=getattr(ns, "force", False))
    except Exception as exc:
        _log().error("FAILED: %s", exc)
        return 1
    _log().info(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
