#!/usr/bin/env python3
"""Organize files by extension into bucketed directories.

This script scans a directory tree, creates a top-level directory for each file extension,
then moves each file into: <extension>/<first-letter>00000/<filename>
A directory is filled up to 500 files, and new numbered directories are created as needed.
Files already inside a matching extension bucket are skipped.

Bucketing uses the file's *real* type as inferred from its header bytes
(``mypdf.doc`` → ``pdf/``). When the header is unknown or compatible with the
declared extension (e.g. ``.jpeg`` for a JPEG, ``.docx`` for a ZIP container),
the declared extension is kept. Pass ``--no-sniff`` to disable header inspection
and revert to extension-only bucketing.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import secrets
import stat as _stat
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from itertools import pairwise
from pathlib import Path
import shutil
from typing import Callable, Iterable, Iterator, NamedTuple, NoReturn, Protocol, Set

BUCKET_SIZE = 500
PROGRESS_EVERY = 10_000   # oze-obs-02: emit a progress line every N done items
# Outstanding-futures cap relative to ``num_threads`` (oze-scal-04 / oze-conc-03).
# The planner blocks on a drain when the in-flight set reaches
# ``num_threads * SUBMIT_BACKLOG_MULT``. 4× keeps every worker fed at full
# rate (each can pre-fetch ~3 jobs) without growing memory linearly with N.
SUBMIT_BACKLOG_MULT = 4
# oze-robust-11: hard ceiling on the worker pool. `--threads <huge>` would
# otherwise build a ThreadPoolExecutor with that many threads and exhaust
# memory/FDs before any move runs. 32× the CPU count is generous for the
# I/O-bound move stage while staying bounded.
MAX_NUM_THREADS = max(8, 32 * (os.cpu_count() or 1))
# Bucket directory width: 5 zero-padded digits (oze-rel-08). The previous
# 4-digit format topped out at 9999 buckets per (ext, prefix) pair — for a
# tree with >5M files in one extension that ceiling was reachable. 5 digits
# raises it to 99999 buckets × BUCKET_SIZE = ~50M files per prefix.
BUCKET_INDEX_WIDTH = 5
BUCKET_NAME_PATTERN = re.compile(r"^(.)(\d{5})$")
ROOT_MAX_LENGTH = 4096
# Number of header bytes to read for type sniffing. 32 covers every signature
# in MAGIC_SIGNATURES (longest is the OLE2 8-byte stamp; ISO BMFF needs offset
# 4 + 4 bytes; RIFF subtype needs offset 8 + 4 bytes), with slack.
HEADER_SNIFF_BYTES = 32
SCAN_STALL_WARN_SECONDS = 60.0
MOVE_STALL_WARN_SECONDS = 60.0
MOVE_MAX_STALL_SECONDS = 300.0
# oze-rel-05/oze-rel-08: refuse to treat out-of-range bucket indices as the
# floor for new allocations. Matches the regex contract exactly: 5 digits → 0..99999.
BUCKET_INDEX_MAX = 99_999

# Sentinel placed in `state_cache` once a bucket reaches BUCKET_SIZE so the
# planner can skip it without re-reading the directory and without holding the
# full name set forever (oze-scal-02). Frozenset is hashable, immutable, and
# distinguishable by `is`.
_BUCKET_FULL: frozenset[str] = frozenset()

# Configure logging
logger = logging.getLogger(__name__)


WEAK_HEADER_LABELS = frozenset({"bmp", "bz2", "exe", "mp3"})


def normalize_extension(path: Path) -> str:
    """Return the normalized extension string for a file path.

    Files without a valid suffix (no dot, suffix with spaces, etc.)
    are grouped under `no_extension`.
    """
    suffix = path.suffix
    # Heuristic: valid extensions are non-empty and free of whitespace/control
    # characters (a control char would leak into the bucket directory name).
    if not suffix or suffix == '.' or any(c.isspace() or ord(c) < 0x20 for c in suffix):
        return 'no_extension'
    return suffix.lower().lstrip('.')


# Header-sniff registry uses a uniform `Signature` protocol (oze-arch-03):
# a `matches(head: bytes) -> str | None` method per detector. Two concrete
# implementations cover (a) flat byte-offset stamps and (b) RIFF / ISO BMFF
# style container peeks that look at a sub-range. `SIGNATURES` is the single
# iteration list used by :func:`detect_type_by_header`; `MAGIC_SIGNATURES`
# below is preserved as the flat-stamp seed for back-compat with external
# imports / tests.

class Signature(Protocol):
    """Protocol every header detector implements. Returns canonical extension
    on a positive match, ``None`` on miss. Detectors must never raise."""

    def matches(self, head: bytes) -> str | None: ...  # pragma: no cover


@dataclass(frozen=True)
class MagicSignature:
    """Flat byte-offset stamp: ``head[offset:offset+len(sig)] == sig``."""

    sig: bytes
    offset: int
    label: str

    def matches(self, head: bytes) -> str | None:
        end = self.offset + len(self.sig)
        if len(head) >= end and head[self.offset:end] == self.sig:
            return self.label
        return None


@dataclass(frozen=True)
class IsoBmffSignature:
    """ISO BMFF (mp4 / mov / heic / …) stores its ``ftyp`` box at offset 4."""

    def matches(self, head: bytes) -> str | None:
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return "mp4"
        return None


@dataclass(frozen=True)
class RiffSignature:
    """RIFF stores its subtype at offset 8 — wav/avi/webp differ only there."""

    def matches(self, head: bytes) -> str | None:
        if head[:4] == b"RIFF" and len(head) >= 12:
            sub = head[8:12]
            if sub == b"WAVE":
                return "wav"
            if sub == b"AVI ":
                return "avi"
            if sub == b"WEBP":
                return "webp"
        return None


# (signature, offset, label) — checked in order; first match wins.
# Offsets are byte offsets into the file head. Labels are the canonical
# extension we want to bucket under when the header is the authoritative type.
MAGIC_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"%PDF-", 0, "pdf"),
    (b"\x89PNG\r\n\x1a\n", 0, "png"),
    (b"\xff\xd8\xff", 0, "jpg"),
    (b"GIF87a", 0, "gif"),
    (b"GIF89a", 0, "gif"),
    (b"PK\x03\x04", 0, "zip"),
    (b"PK\x05\x06", 0, "zip"),
    (b"PK\x07\x08", 0, "zip"),
    (b"Rar!\x1a\x07\x00", 0, "rar"),
    (b"Rar!\x1a\x07\x01\x00", 0, "rar"),
    (b"\x1f\x8b", 0, "gz"),
    (b"BZh", 0, "bz2"),
    (b"\xfd7zXZ\x00", 0, "xz"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"ID3", 0, "mp3"),
    (b"\xff\xfb", 0, "mp3"),
    (b"\xff\xf3", 0, "mp3"),
    (b"\xff\xf2", 0, "mp3"),
    (b"OggS", 0, "ogg"),
    (b"fLaC", 0, "flac"),
    (b"\x1aE\xdf\xa3", 0, "mkv"),
    (b"MZ", 0, "exe"),
    (b"\x7fELF", 0, "elf"),
    (b"\xca\xfe\xba\xbe", 0, "class"),
    (b"\xcf\xfa\xed\xfe", 0, "macho"),
    (b"BM", 0, "bmp"),
    (b"{\\rtf", 0, "rtf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, "ole2"),
    (b"SQLite format 3\x00", 0, "sqlite"),
    (b"II*\x00", 0, "tiff"),
    (b"MM\x00*", 0, "tiff"),
    (b"%!PS", 0, "ps"),
    (b"wOFF", 0, "woff"),
    (b"wOF2", 0, "woff2"),
    (b"OTTO", 0, "otf"),
)


# Unified registry — container detectors first (they peek inside RIFF/ISO BMFF
# at sub-offsets the flat list can't express), then every entry from
# MAGIC_SIGNATURES wrapped as a `MagicSignature`. `detect_type_by_header`
# iterates this once with no special-case branch (oze-arch-03).
SIGNATURES: tuple[Signature, ...] = (
    IsoBmffSignature(),
    RiffSignature(),
    *(MagicSignature(sig, off, label) for sig, off, label in MAGIC_SIGNATURES),
)


# Synonym → canonical extension used only when comparing a declared extension
# with a detected header type. Bucket names retain the file's declared suffix.
EXTENSION_ALIASES: dict[str, str] = {
    "jpeg": "jpg",
    "tif": "tiff",
    "htm": "html",
}


@dataclass(frozen=True)
class ContainerFamily:
    """Group of declared extensions that share the same container header (oze-pat-02).

    When a file's header reports ``detected_label`` but its declared extension
    is in ``members``, the declared extension is preserved — a ``.docx`` IS a
    ZIP but should not be bucketed under ``zip/``. One concept, one value
    object, replacing the previous fan-out across six module-level frozensets
    and a separate ``_FAMILY_BY_DETECTED`` lookup dict.
    """

    detected_label: str
    members: frozenset[str]


# Registry list — single source of truth for "which declared extensions count
# as compatible with which header-detected container type". Iteration order
# doesn't matter; lookup is done by `detected_label` (see :func:`_family_for`).
CONTAINER_FAMILIES: tuple[ContainerFamily, ...] = (
    ContainerFamily("zip", frozenset({
        "zip", "docx", "xlsx", "pptx", "odt", "ods", "odp",
        "epub", "jar", "apk", "ipa", "war", "ear", "kmz", "xpi",
        "cbz",   # comic book ZIP — keep its own cbz/ bucket, not zip/
    })),
    # rar/cbr share the RAR header; cbr is a comic book RAR and gets its own
    # cbr/ bucket instead of being lumped under rar/.
    ContainerFamily("rar", frozenset({"rar", "cbr"})),
    ContainerFamily("ole2", frozenset({
        "ole2", "doc", "xls", "ppt", "msi", "msg", "vsd",
    })),
    ContainerFamily("mp4", frozenset({
        "mp4", "m4a", "m4v", "mov", "3gp", "3g2", "heic", "heif", "f4v",
    })),
    ContainerFamily("mp3", frozenset({"mp3", "mp2"})),
    ContainerFamily("gz", frozenset({"gz", "tgz"})),
)

_FAMILY_BY_DETECTED: dict[str, ContainerFamily] = {
    fam.detected_label: fam for fam in CONTAINER_FAMILIES
}


def _family_for(
    detected_label: str,
    extra_zip_family: frozenset[str] = frozenset(),
) -> frozenset[str] | None:
    """Return the member set for ``detected_label`` or None when no family
    is registered. For the ``zip`` family, union in ``extra_zip_family``
    (oze-rel-07) so runtime extensions are honoured without mutating
    ``CONTAINER_FAMILIES``.
    """
    fam = _FAMILY_BY_DETECTED.get(detected_label)
    if fam is None:
        return None
    if detected_label == "zip" and extra_zip_family:
        return fam.members | extra_zip_family
    return fam.members


class _Unreadable:
    """Singleton sentinel returned by :func:`read_head_bytes` when the file
    can't be opened/read (oze-rel-11). A dedicated class — not a magic byte
    string — so identity checks (``is _HEAD_UNREADABLE``) are unambiguous and
    consumers iterating ``head_cache.values()`` can ``isinstance``-test rather
    than guess from the bytes.

    Behaves like a zero-length bytes object for backwards compatibility: any
    code that previously did ``if not head:`` to skip empty files still skips
    the unreadable sentinel naturally.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<_Unreadable>"

    def __bool__(self) -> bool:
        return False

    def __len__(self) -> int:
        return 0


_HEAD_UNREADABLE: _Unreadable = _Unreadable()
HeadBytes = bytes | _Unreadable


@contextmanager
def _safe_scandir(path: Path) -> Iterator[Iterable[os.DirEntry]]:
    """Yield ``os.scandir(path)`` entries, or an empty iterable on OSError
    (oze-dup-04). The same try/except/with pattern was open-coded in three
    callers; centralising it makes "scan and skip on permission denied" a
    one-line contract.

    Errors are logged at debug so ``-v`` users see what was skipped (oze-rel-10).
    """
    try:
        scanner = os.scandir(path)
    except OSError as exc:
        logger.debug("scandir failed for %s: %s", path, exc)
        yield ()
        return
    try:
        yield scanner
    finally:
        scanner.close()


def read_head_bytes(
    path: Path,
    head_cache: dict[Path, HeadBytes] | None = None,
) -> HeadBytes:
    """Return the first ``HEADER_SNIFF_BYTES`` bytes of ``path``.

    When ``head_cache`` is provided, the read is memoised by ``path`` (oze-perf-04):
    the scan stage primes the cache once per file and every downstream sniff
    consumer (``is_bucketed_file`` during scan, ``resolve_real_extension``
    during planning) reuses the same bytes without re-opening the file.

    On OSError the ``_HEAD_UNREADABLE`` singleton is returned (and cached) so
    callers can branch on "unreadable" without re-attempting the open
    (oze-rel-06 / oze-rel-11). An empty file returns ``b""``.
    """
    if head_cache is not None and path in head_cache:
        return head_cache[path]
    head: HeadBytes
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEADER_SNIFF_BYTES)
    except OSError:
        head = _HEAD_UNREADABLE
    if head_cache is not None:
        head_cache[path] = head
    return head


def detect_type_by_header(
    path: Path,
    head_cache: dict[Path, HeadBytes] | None = None,
) -> str | None:
    """Return canonical extension inferred from ``path``'s leading bytes, or
    ``None`` when no signature matches or the file can't be read.

    Reads at most ``HEADER_SNIFF_BYTES`` once; subsequent calls served from
    ``head_cache`` when supplied (oze-perf-04). Unreadable files log at INFO
    (visible under ``-v``) and return ``None`` so the caller falls back to the
    declared extension — a transient read failure can never relocate the file
    (oze-rel-06).
    """
    head = read_head_bytes(path, head_cache=head_cache)
    if isinstance(head, _Unreadable):
        logger.info("header sniff: cannot read %s — keeping declared extension", path)
        return None
    if not head:
        return None
    for signature in SIGNATURES:
        label = signature.matches(head)
        if label is not None:
            return label
    return None


@dataclass(frozen=True)
class SniffContext:
    """Per-run sniff configuration (oze-dup-03).

    Bundles the three values that every sniff consumer needs:

    * ``sniff`` — whether to consult file headers at all (``--no-sniff`` flips this off).
    * ``head_cache`` — shared per-path head-bytes memo (oze-perf-04). Single dict
      threaded from the scan stage through the planner so each file is opened
      at most once. ``None`` disables caching.
    * ``extra_zip_family`` — runtime extension of the zip entry in
      ``CONTAINER_FAMILIES`` (oze-rel-07) so users can teach the script about
      new zip-based formats without editing source.

    Passing one ``SniffContext`` parameter end-to-end replaces fifteen-plus
    verbatim argument-pass lines.
    """

    sniff: bool = True
    head_cache: dict[Path, HeadBytes] | None = None
    extra_zip_family: frozenset[str] = frozenset()


# Module-level default — used when a caller omits ``ctx`` and doesn't need
# caching/family extension. Safe to share because the dataclass is frozen and
# its head_cache field is None.
_DEFAULT_SNIFF_CTX = SniffContext()


class _ScanStallMonitor:
    """Warn when one scan-stage file check is stuck for too long."""

    def __init__(self, warning_after=SCAN_STALL_WARN_SECONDS, now_fn=time.monotonic):
        self._warning_after = warning_after
        self._now = now_fn
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._started = self._now()
        self._warned = False
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def begin(self, path: Path) -> None:
        with self._lock:
            self._path = path
            self._started = self._now()
            self._warned = False

    def end(self, path: Path) -> None:
        with self._lock:
            if self._path == path:
                self._path = None

    def _run(self) -> None:
        interval = max(1.0, min(self._warning_after / 4.0, 10.0))
        while not self._stop.wait(interval):
            self._maybe_warn()

    def _maybe_warn(self) -> None:
        now = self._now()
        with self._lock:
            if self._path is None or self._warned:
                return
            elapsed = now - self._started
            if elapsed < self._warning_after:
                return
            self._warned = True
            path = self._path
        logger.warning(
            "scan stage stalled: checking %s has taken %.0fs; "
            "header sniff I/O may be blocked",
            path, elapsed,
        )


def _weak_header_should_keep_declared(declared: str, detected: str) -> bool:
    return declared != "no_extension" and detected in WEAK_HEADER_LABELS


def resolve_real_extension(
    path: Path,
    ctx: SniffContext | None = None,
    *,
    log_mismatch: bool = True,
) -> str:
    """Return the bucket extension for ``path`` (oze-dup-02).

    Header detection wins for strong mismatches (oze-perf-06): ``mypdf.doc`` →
    ``pdf``, ``photo.png`` declared as ``.gif`` → ``png``, with a WARNING log
    line so the user notices misnamed files. Family-compatible declarations
    (``.docx`` ↔ ZIP, ``.mov`` ↔ MP4 container, ``.jpeg`` alias of jpg) and
    weak 2-3 byte header coincidences on declared files are preserved silently.

    ``ctx`` bundles ``sniff``/``head_cache``/``extra_zip_family``; omit it to
    use the shared default :data:`_DEFAULT_SNIFF_CTX`.
    """
    if ctx is None:
        ctx = _DEFAULT_SNIFF_CTX
    declared = normalize_extension(path)
    if not ctx.sniff:
        return declared
    detected = detect_type_by_header(path, head_cache=ctx.head_cache)
    if detected is None:
        return declared
    return _resolve_detected_extension(
        path, declared, detected, ctx, log_mismatch)


def _resolve_detected_extension(
    path: Path,
    declared: str,
    detected: str,
    ctx: SniffContext,
    log_mismatch: bool,
) -> str:
    declared_canon = EXTENSION_ALIASES.get(declared, declared)
    if declared_canon == detected:
        return declared
    family = _family_for(detected, ctx.extra_zip_family)
    if family is not None and declared_canon in family:
        return declared
    # oze-pdf-01: a declared `.pdf` whose header is NOT a PDF may be a carrier
    # (e.g. a Wrapster MP3 wrapper) with the real PDF embedded past the header.
    # Do a 3rd check ONLY for `.pdf`: scan the payload for the %PDF- marker and,
    # if present, route to pdf/. Otherwise fall through to the detected content
    # type so a genuinely-mislabelled file still lands by its real bytes.
    # oze-rel-01: the weak-header keep must not apply to a declared `.pdf`
    # that failed the payload scan — a real PDF header is the 5-byte %PDF-,
    # never a legitimately-weak 2-byte match, so fall through to detected.
    if declared_canon == "pdf":
        if _file_contains_pdf(path):
            logger.info(
                "embedded PDF found in %s (header is %s) — bucketing under pdf/",
                path, detected,
            )
            return "pdf"
    elif _weak_header_should_keep_declared(declared, detected):
        logger.info(
            "weak header match on %s: declared .%s but header is %s — keeping declared extension",
            path, declared, detected,
        )
        return declared
    # oze-perf-06: real mismatch — header authoritative. Warn so the user
    # notices misnamed / mistyped files.
    if log_mismatch:
        logger.warning(
            "header mismatch on %s: declared .%s but header is %s — "
            "bucketing under %s/",
            path, declared, detected, detected,
        )
    return detected


_PDF_MAGIC = b"%PDF-"
_PDF_SCAN_CHUNK = 1 << 20   # 1 MiB read window for the embedded-PDF scan
_PDF_SCAN_MAX_BYTES = 16 << 20


def _file_contains_pdf(path: Path) -> bool:
    """True when the ``%PDF-`` marker appears near the start of ``path``.

    Used only for declared-``.pdf`` files whose header isn't a PDF, to route a
    carrier (Wrapster-style MP3 wrapper, etc.) holding an embedded PDF into
    pdf/. Scans a bounded prefix in chunks with a small overlap so the marker is
    never split across a boundary inside the budget; any read error answers
    False (treat as not-a-PDF)."""
    overlap = len(_PDF_MAGIC) - 1
    try:
        with open(path, "rb") as fh:
            prev = b""
            remaining = _PDF_SCAN_MAX_BYTES
            while remaining > 0:
                chunk = fh.read(min(_PDF_SCAN_CHUNK, remaining))
                if not chunk:
                    return False
                remaining -= len(chunk)
                if _PDF_MAGIC in prev + chunk:
                    return True
                prev = chunk[-overlap:]
            return False
    except OSError:
        return False


def normalize_prefix(name: str) -> str:
    """Return a filesystem-safe first-letter prefix for a filename.

    Non-alphanumeric first characters are mapped to `_`.
    """
    if not name:
        return '_'
    first = name[0].lower()
    return first if first.isalnum() else '_'


def bucket_name(prefix: str, index: int) -> str:
    """Format a bucket directory name from a prefix and numeric index.

    Validates ``index`` is within ``[0, BUCKET_INDEX_MAX]`` (oze-rel-09); silent
    formatting of negative or out-of-range indices would emit a directory name
    the next scan's ``BUCKET_NAME_PATTERN`` rejects, leaking the bucket from
    the planner's view and triggering a duplicate allocation. Width is
    ``BUCKET_INDEX_WIDTH`` digits (oze-rel-08).
    """
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError(f"bucket index must be int, got {type(index).__name__}")
    if index < 0 or index > BUCKET_INDEX_MAX:
        raise ValueError(
            f"bucket index out of range: {index} (allowed 0..{BUCKET_INDEX_MAX})"
        )
    return f"{prefix}{index:0{BUCKET_INDEX_WIDTH}d}"


def is_bucketed_file(
    root: Path,
    path: Path,
    ctx: SniffContext | None = None,
) -> bool:
    """Determine if a file is already inside a valid bucket structure.

    Returns True if the file path matches the expected <ext>/<prefix><index>/<filename> format.
    When sniffing is enabled the bucket directory is compared against the
    header-resolved extension, so a file already sitting under its true type
    (e.g. ``pdf/p00000/mypdf.doc``) is recognised as bucketed and not moved.
    Pass ``ctx`` to share head-bytes / extra-family settings (oze-dup-03).
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False

    # Exactly <ext>/<bucket>/<filename>: a file nested deeper than a bucket
    # (e.g. <ext>/<bucket>/sub/file) is NOT directly bucketed (oze-rel-01).
    if len(relative.parts) != 3:
        return False

    ext_dir, bucket_dir = relative.parts[0], relative.parts[1]
    if not bucket_dir or not BUCKET_NAME_PATTERN.match(bucket_dir):
        return False

    if ctx is None:
        ctx = _DEFAULT_SNIFF_CTX

    # Under "header always wins" (oze-perf-06) we cannot trust the declared
    # extension to confirm placement — a real PDF sitting under doc/d00000/
    # with a .doc suffix would slip through a structural-only check. The sniff
    # head_cache (oze-perf-04) keeps the cost to one read per file across the
    # whole run anyway.
    return ext_dir == resolve_real_extension(
        path, ctx=ctx, log_mismatch=False
    )


def _scan_regular_path(
    entry: "os.DirEntry[str]",
    skip: set[str],
    root: Path,
    symlink_resolve_cache: dict[Path, Path | None],
) -> Path | None:
    """Return the :class:`Path` for a regular, non-skipped file entry, else None.

    oze-conc-04: a single `stat(follow_symlinks=False)` and a branch on
    `st_mode` so a hostile filesystem can't swap a symlink for a regular file
    between checks. oze-sec-02: a skipped symlink that escapes `root` is logged
    as defence-in-depth (the file is never read or moved regardless)."""
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    mode = st.st_mode
    if _stat.S_ISLNK(mode) or not _stat.S_ISREG(mode):
        if _stat.S_ISLNK(mode):
            _warn_if_symlink_escapes_root(Path(entry.path), root, symlink_resolve_cache)
        return None
    if entry.path in skip:
        return None
    return Path(entry.path)


def list_files(
    root: Path,
    skip_paths: Iterable[Path],
    verbose: bool = False,
    ctx: SniffContext | None = None,
) -> list[Path]:
    """Return all files under the root, excluding skipped paths and bucketed outputs.

    Implementation note (oze-perf-03): walks the tree with `os.scandir` instead
    of `Path.rglob('*')`, so we never materialise the full tree into a list
    before filtering. Each `DirEntry` caches its stat result, so the symlink /
    is-file checks below use the cache instead of a fresh syscall per file
    (oze-perf-02).

    Pass a single :class:`SniffContext` ``ctx`` (oze-dup-03) to share
    head-bytes / extra-zip-family / sniff toggle with downstream stages.
    """
    if ctx is None:
        ctx = _DEFAULT_SNIFF_CTX
    skip = {str(p) for p in skip_paths}   # O(1) membership; compare on path string (oze-perf-01)
    files: list[Path] = []
    already_bucketed = 0
    scanned = 0   # oze-obs-04: regular files examined so far (for scan progress)
    symlink_resolve_cache: dict[Path, Path | None] = {}
    scan_monitor = _ScanStallMonitor()
    scan_monitor.start()
    try:
        for entry in _walk_scandir(root):
            path = _scan_regular_path(entry, skip, root, symlink_resolve_cache)
            if path is None:
                continue
            # oze-obs-04: the scan can run for minutes on a large tree (every file
            # is opened for header sniffing) and previously emitted nothing until
            # the single "Scanning complete" line at the end. Emit a heartbeat every
            # PROGRESS_EVERY regular files so `--verbose` shows the scan is alive and
            # a Ctrl+C isn't mistaken for a freeze. Logs at INFO (visible under -v),
            # matching the move-stage progress cadence.
            scanned += 1
            # oze-obs-06: -vv emits one line per file so a long silent scan on a
            # smaller-but-slow tree (header sniff opens each file) shows live
            # progress before the 10k-file INFO heartbeat would ever fire.
            logger.debug("scan: %s", path)
            _log_scan_progress(scanned, len(files), already_bucketed)
            if _scan_path_is_bucketed(root, path, ctx, scan_monitor):
                already_bucketed += 1
                _evict_sniff_head(ctx, path)
                continue
            files.append(path)
    finally:
        scan_monitor.stop()
    if verbose: # Use logger.info for verbose output
        logger.info(f"Scanning complete. Found {len(files)} files to organize ({already_bucketed} already bucketed).")
    return files


def _log_scan_progress(scanned: int, pending: int, already_bucketed: int) -> None:
    if scanned % PROGRESS_EVERY == 0:
        logger.info(
            "scanning: %d files seen (%d to move, %d already bucketed)",
            scanned, pending, already_bucketed,
        )


def _scan_path_is_bucketed(
    root: Path,
    path: Path,
    ctx: SniffContext,
    scan_monitor: _ScanStallMonitor,
) -> bool:
    scan_monitor.begin(path)
    try:
        return is_bucketed_file(root, path, ctx=ctx)
    finally:
        scan_monitor.end(path)


def _evict_sniff_head(ctx: SniffContext, path: Path) -> None:
    if ctx.head_cache is not None:
        ctx.head_cache.pop(path, None)


def _warn_if_symlink_escapes_root(
    link_path: Path,
    root: Path,
    resolve_cache: dict[Path, Path | None] | None = None,
) -> None:
    """Log a debug message when `link_path`'s target resolves OUTSIDE
    `root` (oze-sec-02).

    Pure defence-in-depth: the walker already skips symlinks, so the
    file at `link_path` is never moved or read. The log surfaces the
    case where someone introduced a symlink into the tree by mistake,
    helping the operator notice it before it accumulates.

    Failure of `resolve()` itself (broken link, NUL byte) is swallowed
    — there's nothing actionable for the operator and the skip
    semantics already protect them."""
    if resolve_cache is None:
        resolve_cache = {}
    if link_path in resolve_cache:
        target = resolve_cache[link_path]
        if target is None:
            return
    else:
        try:
            target = link_path.resolve()
        except (OSError, ValueError):
            resolve_cache[link_path] = None
            return
        resolve_cache[link_path] = target
    try:
        target.relative_to(root)
        return    # target is INSIDE root — quiet skip
    except ValueError:
        # target is OUTSIDE root — log but stay quiet at default level.
        logger.debug(
            "skipping symlink whose target escapes root: %s -> %s",
            link_path, target,
        )


def _walk_scandir(root: Path):
    """Iteratively yield `os.DirEntry` objects under `root` (depth-first).

    Generator-based so a 1M-file tree never sits in memory as a list, and so
    each entry carries its own cached stat for the caller (oze-perf-02 / oze-perf-03).
    Permission/OS errors on a sub-directory are skipped via :func:`_safe_scandir`
    (oze-dup-04) and logged at debug (oze-rel-10) — we'd rather organise what
    we can than abort the whole run.

    oze-rel-20: uses an explicit stack instead of `yield from _walk_scandir(...)`
    recursion so a tree deeper than the Python recursion limit (~1000 by
    default) doesn't raise `RecursionError` mid-walk. The stack is a list of
    `Path`s pending traversal; depth-first order is preserved by appending
    subdirectories in reverse so the leftmost subdir is processed first.
    """
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        subdirs: list[Path] = []
        with _safe_scandir(current) as it:
            for entry in it:
                yield entry
                # Recurse into real subdirs only; symlinked dirs are not followed
                # so the walk never escapes `root` or loops.
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    subdirs.append(Path(entry.path))
        # Reverse so popping the stack yields children in original scandir order.
        stack.extend(reversed(subdirs))


def _scan_bucket_indices(ext_dir: Path) -> dict[str, list[int]]:
    """One-shot scandir of ``ext_dir`` returning ``{prefix: sorted indices}``.

    Walks the directory once and partitions every matching bucket name by its
    first-letter prefix. For an extension with 26 prefixes the same directory
    was previously opened 26 times (oze-perf-05).

    Indices beyond ``BUCKET_INDEX_MAX`` are dropped with a warning (oze-rel-05).
    """
    by_prefix: dict[str, list[int]] = {}
    with _safe_scandir(ext_dir) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            match = BUCKET_NAME_PATTERN.match(entry.name)
            if not match:
                continue
            idx = int(match.group(2))
            if idx > BUCKET_INDEX_MAX:
                logger.warning(
                    "ignoring out-of-range bucket index %d under %s (cap=%d)",
                    idx, ext_dir, BUCKET_INDEX_MAX)
                continue
            by_prefix.setdefault(match.group(1), []).append(idx)
    for indices in by_prefix.values():
        indices.sort()
    return by_prefix


def bucket_file_names(bucket_path: Path) -> Set[str]:
    """Return the set of file names currently present in a bucket."""
    with _safe_scandir(bucket_path) as entries:
        return {entry.name for entry in entries
                if entry.is_file(follow_symlinks=False)}


class BucketChoice(NamedTuple):
    """Result of :func:`_find_reusable_bucket` (oze-cx-02).

    ``bucket`` is the reusable bucket directory if one was found, else None.
    ``next_index`` is the lowest bucket index the caller should allocate when
    ``bucket`` is None — either the gap slot or one past the last index.
    ``first_non_full`` is the updated first-non-full cursor the caller should
    persist for the next call (oze-arch-04: previously smuggled out through a
    mutable ``cursor_out`` list parameter).
    """
    bucket: Path | None
    next_index: int
    first_non_full: int


def _find_reusable_bucket(
    ext_dir: Path,
    prefix: str,
    filename: str,
    state_cache: dict[Path, Set[str] | frozenset[str]],
    indices: list[int],
    first_non_full_index: int = 0,
    bucket_size: int = BUCKET_SIZE,
) -> BucketChoice:
    """Walk `indices` sorted, looking for an existing bucket with room and
    without a name clash on `filename` (oze-cx-01). Returns a
    :class:`BucketChoice` (oze-cx-02): ``bucket`` is the reusable bucket if
    any (else None); ``next_index`` is the lowest index to allocate when no
    reusable bucket is found; ``first_non_full`` is the advanced cursor.
    Populates `state_cache` lazily.

    Once a bucket is full, its entry in `state_cache` is collapsed to the
    `_BUCKET_FULL` sentinel (oze-scal-02): we skip it without re-reading the
    directory and without retaining the full name set, which would otherwise
    grow unbounded on million-file runs."""
    next_expected = first_non_full_index
    first_non_full = first_non_full_index
    # oze-perf-10: `indices` is already sorted by `_scan_bucket_indices`
    # (L644-645). The redundant `sorted(...)` ran on every bucket selection;
    # iterating directly is identical in result and skips a list copy.
    for index in indices:
        if index < first_non_full_index:
            continue
        if index > next_expected:
            break                           # first gap: caller fills it
        bucket_path, full = _reusable_bucket_at_index(
            ext_dir, prefix, filename, state_cache, index, bucket_size)
        if full:
            next_expected = index + 1
            first_non_full = index + 1
            continue
        if bucket_path is not None:
            return BucketChoice(bucket_path, next_expected, first_non_full)
        next_expected = index + 1
    return BucketChoice(None, next_expected, first_non_full)


def _reusable_bucket_at_index(
    ext_dir: Path,
    prefix: str,
    filename: str,
    state_cache: dict[Path, Set[str] | frozenset[str]],
    index: int,
    bucket_size: int,
) -> tuple[Path | None, bool]:
    bucket_path = ext_dir / bucket_name(prefix, index)
    names = state_cache.get(bucket_path)
    if names is _BUCKET_FULL:
        return None, True
    if names is None:
        names = bucket_file_names(bucket_path)
        state_cache[bucket_path] = names
    if len(names) >= bucket_size:
        state_cache[bucket_path] = _BUCKET_FULL
        return None, True
    return (bucket_path if filename not in names else None), False


def _allocate_new_bucket(
    ext_dir: Path,
    prefix: str,
    index: int,
    state_cache: dict[Path, Set[str] | frozenset[str]],
    indices: list[int],
) -> Path:
    """Create the bucket-path entry for `index` (oze-cx-01): seed an empty
    state_cache entry and record the index. The directory itself is created
    later by ensure_directory on the actual move."""
    new_path = ext_dir / bucket_name(prefix, index)
    state_cache[new_path] = set()
    indices.append(index)
    return new_path


def choose_bucket(
    ext_dir: Path,
    prefix: str,
    filename: str,
    state_cache: dict[Path, Set[str] | frozenset[str]],
    indices: list[int],
    first_non_full_index: int = 0,
    bucket_size: int = BUCKET_SIZE,
) -> tuple[Path, int]:
    """Choose or create the bucket for `filename`, preferring gaps and
    existing rooms. Returns ``(bucket_path, first_non_full)`` where the
    second element is the advanced cursor to persist (oze-arch-04). Thin
    orchestrator over _find_reusable_bucket + _allocate_new_bucket
    (oze-cx-01)."""
    choice = _find_reusable_bucket(
        ext_dir, prefix, filename, state_cache, indices,
        first_non_full_index, bucket_size)
    if choice.bucket is not None:
        return choice.bucket, choice.first_non_full
    bucket = _allocate_new_bucket(
        ext_dir, prefix, choice.next_index, state_cache, indices)
    return bucket, choice.first_non_full


@dataclass(frozen=True)
class Bucket:
    """Immutable value object for a bucket directory (oze-pat-01 / oze-cx-06).

    A bucket is a (path, prefix, index, members) tuple — previously expressed
    as an anonymous ``Path`` keying into a ``set[str]`` inside
    ``BucketManager.state_cache``. Lifting it to a named value object gives
    the planner a single handle to pass downstream and makes the "full?"
    predicate a method rather than a sentinel-identity check.

    `frozen=True` (oze-cx-06): a caller holding a Bucket reference no longer
    sees its `members` field silently reassigned to `_BUCKET_FULL` after
    creation. The transition to full is now visible only via the manager's
    `state_cache`; a new `Bucket` is constructed on each `choose` so the
    in-hand reference's view is always the snapshot it was handed.
    """

    path: Path
    prefix: str
    index: int
    members: Set[str] | frozenset[str] = field(default_factory=set)
    capacity: int = BUCKET_SIZE

    @property
    def name(self) -> str:
        return self.path.name

    def is_full(self) -> bool:
        """True once the bucket has reached its capacity (oze-scal-02)."""
        return self.members is _BUCKET_FULL or len(self.members) >= self.capacity

    def reserve(self, filename: str) -> None:
        """Record ``filename`` as taken in this bucket. Caller must check
        :meth:`is_full` first — `BucketManager.choose` enforces that contract.

        Mutates the shared `members` set in place (the underlying object is
        still mutable even though the dataclass is frozen). The frozen flag
        only blocks rebinding the field, which is the bug we wanted to
        prevent (oze-cx-06)."""
        if isinstance(self.members, frozenset):  # _BUCKET_FULL is frozenset
            raise RuntimeError(f"reserve on full bucket {self.path}")
        self.members.add(filename)


@dataclass
class BucketManager:
    """Bundles the bookkeeping for bucket selection (oze-arch-02).

    Encapsulates ``state_cache`` (per-bucket filename sets and the
    ``_BUCKET_FULL`` sentinel) and ``indices_cache`` (per (ext, prefix) list
    of seen bucket indices). The plan stage talks to ``choose`` only; the
    internal dicts stay private so "selected but never created" drift between
    selection and bucket I/O is impossible.

    The directory itself is still created lazily by :func:`move_file` via
    :func:`ensure_directory` — :meth:`choose` just reserves the slot in the
    planner caches and the target filename.
    """

    root: Path
    bucket_size: int = BUCKET_SIZE
    state_cache: dict[Path, Set[str] | frozenset[str]] = field(default_factory=dict)
    # Indices cache is keyed by *directory* (oze-perf-05): one scandir per
    # ext_dir populates every prefix at once, so the 26-times-per-extension
    # re-scan is gone.
    _by_dir_cache: dict[Path, dict[str, list[int]]] = field(default_factory=dict)
    # Legacy-shape view kept for tests/external callers that introspect
    # ``BucketManager.indices_cache[(ext_dir, prefix)]``. Populated lazily on
    # each ``_indices_for`` call from the directory-level cache.
    indices_cache: dict[tuple[Path, str], list[int]] = field(default_factory=dict)
    _first_non_full: dict[tuple[Path, str], int] = field(default_factory=dict)
    _reserved_names: dict[Path, set[str]] = field(default_factory=dict)
    # oze-obs-01: per-allocation counters surfaced in the end-of-run
    # debug line so the user can tune BUCKET_SIZE / spot pathological
    # name distributions. Incremented inside `choose` and `choose_bucket`.
    stats: dict[str, int] = field(default_factory=lambda: {
        "new_bucket_allocated": 0,
        "bucket_reused": 0,
        "buckets_full": 0,
    })

    def _indices_for(self, ext_dir: Path, prefix: str) -> list[int]:
        # Honour any pre-seeded entry in the legacy-shape view first so tests
        # / external callers that populate `indices_cache` directly still win.
        seeded = self.indices_cache.get((ext_dir, prefix))
        if seeded is not None:
            return seeded
        by_prefix = self._by_dir_cache.get(ext_dir)
        if by_prefix is None:
            by_prefix = _scan_bucket_indices(ext_dir)
            self._by_dir_cache[ext_dir] = by_prefix
        indices = by_prefix.setdefault(prefix, [])
        # Mirror into the legacy-shape view so existing tests/observers still
        # see the same list object (mutations to either reach both).
        self.indices_cache[(ext_dir, prefix)] = indices
        return indices

    def choose(self, source: Path, ext_dir: Path, prefix: str) -> Bucket:
        """Reserve a bucket for ``source.name`` under ``ext_dir`` and return
        a :class:`Bucket` value object (oze-pat-01).

        Records ``source.name`` in the bucket's name set BEFORE the caller
        submits the move (oze-conc-01) — workers never touch the cache, so
        the invariant "every accepted destination is in the cache" holds
        trivially. Collapses to ``_BUCKET_FULL`` once the bucket reaches
        ``BUCKET_SIZE`` (oze-scal-02).
        """
        indices = self._indices_for(ext_dir, prefix)
        # oze-obs-01 / oze-hyg-01: tell reuse from fresh allocation for the
        # stats counter. oze-perf-11: only `_allocate_new_bucket` appends to
        # `indices`, so a length delta is an O(1) signal — the old
        # `set(state_cache.keys())` snapshot copied the whole keyset per file,
        # O(files × buckets) on the hot planning path.
        pre_indices = len(indices)
        cursor_key = (ext_dir, prefix)
        cursor = self._first_non_full.get(cursor_key, 0)
        bucket_path, new_cursor = choose_bucket(
            ext_dir, prefix, source.name, self.state_cache, indices, cursor,
            self.bucket_size)
        self._first_non_full[cursor_key] = new_cursor
        names = self.state_cache[bucket_path]
        if not isinstance(names, set):  # _BUCKET_FULL frozenset sentinel (oze-cx-05)
            raise RuntimeError(
                f"choose_bucket returned full bucket {bucket_path}"
            )
        if len(indices) > pre_indices:
            self.stats["new_bucket_allocated"] += 1
        else:
            self.stats["bucket_reused"] += 1
        names.add(source.name)
        self._reserved_names.setdefault(bucket_path, set()).add(source.name)
        match = BUCKET_NAME_PATTERN.match(bucket_path.name)
        if match is None:
            raise RuntimeError(
                f"choose_bucket returned non-conforming path {bucket_path}"
            )
        bucket = Bucket(
            path=bucket_path,
            prefix=match.group(1),
            index=int(match.group(2)),
            members=names,
            capacity=self.bucket_size,
        )
        if bucket.is_full():
            self.state_cache[bucket_path] = _BUCKET_FULL
            self.stats["buckets_full"] += 1
            if self._first_non_full.get(cursor_key, 0) == bucket.index:
                self._first_non_full[cursor_key] = bucket.index + 1
            # oze-cx-06: Bucket is frozen — return a fresh instance with
            # the sentinel members instead of mutating the in-hand object.
            bucket = Bucket(
                path=bucket.path,
                prefix=bucket.prefix,
                index=bucket.index,
                members=_BUCKET_FULL,
                capacity=self.bucket_size,
            )
        return bucket

    def release(self, source: Path, bucket_dir: Path) -> None:
        """Undo a planned reservation after a move is skipped."""
        names = self.state_cache.get(bucket_dir)
        if names is None:
            return
        if names is _BUCKET_FULL:
            names = self._restore_mutable_bucket_names(bucket_dir)
        if (bucket_dir / source.name).exists():
            return
        if not isinstance(names, set):
            return
        names.discard(source.name)
        self._mark_bucket_non_full(bucket_dir)
        self._release_reserved_name(bucket_dir, source.name)

    def _restore_mutable_bucket_names(self, bucket_dir: Path) -> Set[str]:
        names = bucket_file_names(bucket_dir)
        names.update(self._reserved_names.get(bucket_dir, set()))
        self.state_cache[bucket_dir] = names
        return names

    def _mark_bucket_non_full(self, bucket_dir: Path) -> None:
        match = BUCKET_NAME_PATTERN.match(bucket_dir.name)
        if match is None:
            return
        key = (bucket_dir.parent, match.group(1))
        index = int(match.group(2))
        self._first_non_full[key] = min(
            self._first_non_full.get(key, index), index)

    def _release_reserved_name(self, bucket_dir: Path, name: str) -> None:
        reserved = self._reserved_names.get(bucket_dir)
        if reserved is None:
            return
        reserved.discard(name)
        if not reserved:
            self._reserved_names.pop(bucket_dir, None)


def ensure_directory(path: Path) -> None:
    """Create a directory path if it does not already exist."""
    path.mkdir(parents=True, exist_ok=True)


def _reject_existing_target(target: Path, exc: FileExistsError) -> NoReturn:
    """Raise the unified 'target already exists' error from a no-overwrite
    reservation failure (oze-dup-01) — shared by the same-fs link path and
    the cross-fs O_EXCL path so both produce the same message."""
    raise FileExistsError(f"Target file already exists: {target}") from exc


def _link_exclusive(src: Path, dst: Path) -> None:
    """Hardlink ``src`` → ``dst`` with no-overwrite semantics (oze-dup-02).

    Wraps :func:`_link_with_transient_retry` and maps FileExistsError through
    :func:`_reject_existing_target` so the same-fs path produces a uniform
    error message. Other OSErrors (notably ``errno.EXDEV``) propagate so the
    caller can fall back to the cross-device copy path.
    """
    try:
        _link_with_transient_retry(src, dst)
    except FileExistsError as exc:
        _reject_existing_target(dst, exc)


def _reserve_target(target: Path) -> None:
    """Reserve ``target`` with ``O_CREAT|O_EXCL`` (oze-dup-02).

    Atomic no-overwrite create — the symmetric primitive of
    :func:`_link_exclusive` for the cross-device copy path. Closes the
    descriptor immediately; ``os.replace`` later atomically swaps the
    completed temp file into this reserved slot.
    """
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        _reject_existing_target(target, exc)
    os.close(fd)


def _unlink_with_rollback(source: Path, target: Path) -> None:
    """Remove ``source`` after a successful hardlink; roll back the new link
    if the source unlink fails (oze-cx-04).

    Flattens the previous three-deep ``try`` nest in :func:`move_file`. The
    invariant is "the file ends up in exactly one place" — either the hardlink
    survives and the source is gone (success), or both are removed and the
    caller sees the original OSError (rollback succeeded), or a RuntimeError
    surfaces with both errnos so manual cleanup is unmistakable
    (oze-rel-02 / oze-rel-04).
    """
    try:
        os.unlink(source)
        return
    except OSError as src_exc:
        try:
            os.unlink(target)
        except OSError as tgt_exc:
            raise RuntimeError(
                f"double-copy: hardlinked {target} but failed to remove "
                f"both the source ({src_exc}) and the new link ({tgt_exc}); "
                f"manual cleanup required"
            ) from src_exc
        raise


def _require_regular_source(path: Path) -> None:
    """Reject a non-regular move source (oze-rel-01).

    ``move_file``/``plan_moves`` are public and can be handed an arbitrary
    path. The scanner deliberately refuses to follow symlinks, so a symlink (or
    device/fifo/socket) passed straight to ``move_file`` must not be hardlinked
    into a bucket — that would relocate link semantics the scan skips. Classify
    via ``lstat`` so the link itself is inspected, never its target; a vanished
    source surfaces as the underlying ``OSError`` (per-file skip).
    """
    st = os.lstat(path)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
        raise ValueError(f"refusing to move non-regular source: {path}")


def move_file(path: Path, destination: Path) -> Path:
    """Move a file into the destination directory without overwriting an existing target.

    Same-filesystem moves use an atomic hardlink+unlink: `os.link` fails with
    FileExistsError if the target name is taken, closing the TOCTOU window of a
    separate exists()-then-move check (conc-02). Cross-filesystem moves fall back
    to copy-to-temp + atomic rename, so an interrupted copy never leaves a partial
    file at the target (conc-03).

    Transient FD-exhaustion errnos (EMFILE/ENFILE/EAGAIN) are retried with
    jittered exponential backoff (oze-perf-07) before we fall through to the
    cross-device copy.

    oze-rel-01: on a filesystem without hardlink support (FAT/exFAT/SMB/NFS)
    the primary ``os.link`` raises EPERM/ENOSYS/EOPNOTSUPP (and per-file
    EMLINK). Those ``_LINK_UNSUPPORTED_ERRNOS`` route to the cross-device copy
    path too, not just EXDEV, so the move falls back instead of aborting.

    oze-rel-12: a source whose name collides with an ancestor of the
    destination (e.g. a file literally named ``avi`` whose header pushes it
    under ``<root>/avi/a00000/``) would otherwise blow up `ensure_directory`
    with ``NotADirectoryError``. The source is renamed in place to a
    collision-resolved sibling before the dir is created so the data is
    preserved and the move proceeds.
    """
    _require_regular_source(path)
    path = _resolve_source_collision(path, destination)
    ensure_directory(destination)
    target = destination / path.name
    try:
        _link_exclusive(path, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV and exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
            raise
        _move_cross_device(path, target)
    else:
        _unlink_with_rollback(path, target)
    return target


def _resolve_source_collision(source: Path, destination: Path) -> Path:
    """If any ancestor of `destination` exists as a non-directory, rename
    the blocker aside before the destination dir is created (oze-rel-12 /
    oze-rel-13).

    Two collision shapes are handled by the same rename-aside strategy:

      * **self-collision** (oze-rel-12): the blocker IS `source`. Rename
        the source and return the new path so the move can proceed under
        the bucket.
      * **cross-source collision** (oze-rel-13): some unrelated regular
        file already occupies a path the destination needs. Rename that
        file (it stays in the same dir, just with a `.collision<n>`
        suffix) so the bucket dir can be created. The source is returned
        unchanged.

    Picks the first free `<name>.collision<n>` sibling for the renamed
    file so re-runs are deterministic and idempotent."""
    # Loop because a sibling worker thread may resolve the same blocker
    # in parallel; if the rename races and loses (FileNotFoundError or
    # ENOENT), the blocker is already gone — re-probe and either move on
    # or pick up the next blocker up the chain.
    for _ in range(_STACKED_BLOCKER_RETRY_CAP):
        blocker = _find_destination_blocker(destination)
        if blocker is None:
            return source
        candidate = _rename_destination_blocker(blocker)
        if candidate is None:
            continue
        if blocker == source:
            logger.warning(
                "name collision: source %s would collide with destination "
                "ancestor %s; renamed to %s before placing under bucket",
                source, destination, candidate,
            )
            return candidate
        # oze-rel-03: a cross-source blocker was cleared, but a SECOND
        # non-dir blocker may sit further up the ancestor chain. Re-probe
        # instead of returning so stacked blockers are all resolved before
        # ensure_directory runs; otherwise the survivor raises NotADirectoryError.
        logger.warning(
            "name collision: file %s blocks destination ancestor under %s; "
            "renamed to %s so the bucket dir can be created",
            blocker, destination, candidate,
        )
        continue
    # Retry budget exhausted (extremely unlikely on real workloads).
    logger.warning(
        "name collision retries exhausted for %s -> %s; falling through",
        source, destination,
    )
    return source


def _rename_destination_blocker(blocker: Path) -> Path | None:
    try:
        return _atomic_rename_to_free_slot(blocker)
    except FileNotFoundError:
        return None
    except OSError as exc:
        permanent_errnos = {
            errno.EACCES, errno.EPERM, errno.EROFS, errno.EISDIR, errno.EBUSY,
        }
        if exc.errno in permanent_errnos:
            raise
        return None


def _find_destination_blocker(destination: Path) -> "Path | None":
    """Walk up `destination` looking for the first ancestor that exists
    but is not a directory (oze-rel-13). Returns that ancestor or None
    when the chain is unblocked. Symlinks are treated as blockers — even
    a symlink-to-dir would mean the bucket lives under the link, which
    we refuse to follow for safety."""
    cur = destination
    while cur != cur.parent:
        try:
            st = cur.lstat()
        except FileNotFoundError:
            cur = cur.parent
            continue
        except NotADirectoryError:
            # An ancestor exists as a regular file: lstat on a deeper
            # path raises ENOTDIR. Walk up and try the parent — the
            # blocker is somewhere above.
            cur = cur.parent
            continue
        except OSError:
            return None
        # Symlinks count as blockers; a file blocks; a dir is fine.
        if _stat.S_ISDIR(st.st_mode) and not _stat.S_ISLNK(st.st_mode):
            cur = cur.parent
            continue
        return cur
    return None


_COLLISION_RETRY_CAP = 1000

# oze-adapt-01: retry budget for `_resolve_source_collision`'s stacked-blocker
# loop — each pass clears at most one non-dir ancestor, so this bounds the
# blocker-chain depth handled before giving up.
_STACKED_BLOCKER_RETRY_CAP = 8

# oze-rel-01: errnos meaning "this filesystem does not support hardlinks"
# (FAT/exFAT, many SMB/NFS mounts). On any of these the os.link reservation
# strategy can never succeed, so we switch to the O_CREAT|O_EXCL + os.rename
# fallback. oze-rel-06: EMLINK (source already at its max link count) is the
# same situation per-file — the link can't be made, so fall back rather than
# letting a bare OSError abort the whole plan.
_LINK_UNSUPPORTED_ERRNOS = frozenset({
    errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, errno.EMLINK,
})


def _raise_collision_exhausted(source: Path, last_exc: OSError | None) -> NoReturn:
    """Log the probed-candidate count and raise the retry-cap RuntimeError
    (oze-obs-02) so an exhaustion leaves a trace even when the worker pool
    converts the exception into a per-file skip tuple."""
    logger.error(
        "collision reservation exhausted for %s after probing %d "
        "`.collisionN` slots", source, _COLLISION_RETRY_CAP,
    )
    raise RuntimeError(
        f"unable to atomically reserve a collision name for {source} "
        f"after {_COLLISION_RETRY_CAP} attempts"
    ) from last_exc


def _reserve_slot_via_rename(source: Path) -> Path:
    """Fallback reservation for filesystems without hardlink support (oze-rel-01).

    Reserves `<name>.collisionN` with ``O_CREAT|O_EXCL`` (atomic no-clobber,
    raises FileExistsError if taken) then ``os.rename``s the source onto the
    reserved slot. The rename overwrites the just-created empty placeholder we
    own, so no sibling worker's data is ever clobbered. Same directory, so the
    rename never hits EXDEV. Caps at ``_COLLISION_RETRY_CAP`` attempts."""
    last_exc: OSError | None = None
    for n in range(1, _COLLISION_RETRY_CAP + 1):
        candidate = source.with_name(f"{source.name}.collision{n}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError as exc:
            last_exc = exc
            continue
        os.close(fd)
        os.rename(source, candidate)
        return candidate
    _raise_collision_exhausted(source, last_exc)


def _unlink_source_or_rollback_candidate(source: Path, candidate: Path) -> None:
    """Remove `source` after a successful reservation link; roll back the new
    link if the source unlink fails (oze-rel-02).

    Either the source is gone and the candidate survives (success), or the
    candidate is removed and the original OSError is re-raised (rollback), or a
    RuntimeError carrying both errnos surfaces so the leaked hardlink is logged
    and manual cleanup is unmistakable (oze-obs-02)."""
    try:
        os.unlink(source)
        return
    except OSError as src_exc:
        try:
            os.unlink(candidate)
        except OSError as cand_exc:
            logger.error(
                "collision reservation: linked %s -> %s but could not remove "
                "the source (%s) or roll back the new link (%s); the file is "
                "left at both paths and a hardlink is leaked — manual cleanup "
                "required", source, candidate, src_exc, cand_exc,
            )
            raise RuntimeError(
                f"double-link: reserved {candidate} but failed to remove both "
                f"the source ({src_exc}) and the new link ({cand_exc}); "
                f"manual cleanup required"
            ) from src_exc
        raise


def _atomic_rename_to_free_slot(source: Path) -> Path:
    """Rename `source` to the first free `<name>.collisionN` slot with
    no-clobber, TOCTOU-safe semantics (oze-conc-01 / oze-rel-19).

    POSIX `os.rename` SILENTLY replaces an existing regular file, so a
    rename-based reservation could clobber a sibling worker's data. Instead
    reserve the slot with `os.link(source, candidate)` — link raises
    `FileExistsError` if `candidate` already exists — then `os.unlink` the
    source. The candidate is always a sibling of the source (same
    directory, hence same filesystem) so `os.link` never hits EXDEV.

    Loops: on `FileExistsError` (slot taken by us or a racing worker) bump
    `n` and retry. Caps at `_COLLISION_RETRY_CAP` overall attempts.

    oze-rel-01/oze-rel-06: on a filesystem without hardlink support (`os.link`
    raises EPERM/ENOSYS/EOPNOTSUPP) or when the source is already at its max
    link count (EMLINK), fall back to the O_CREAT|O_EXCL + os.rename
    reservation, which works on FAT/exFAT/SMB/NFS.

    oze-rel-02: if the link succeeds but the source `os.unlink` fails, roll
    back the just-created candidate and re-raise, so the file is never left at
    BOTH paths and no hardlink is leaked (mirrors :func:`_unlink_with_rollback`)."""
    last_exc: OSError | None = None
    for n in range(1, _COLLISION_RETRY_CAP + 1):
        candidate = source.with_name(f"{source.name}.collision{n}")
        reserved, linked, last_exc = _reserve_collision_candidate(
            source, candidate, last_exc)
        if reserved is None:
            continue
        if not linked:
            return reserved
        _finish_linked_collision_move(source, candidate)
        return reserved
    _raise_collision_exhausted(source, last_exc)


def _reserve_collision_candidate(
    source: Path,
    candidate: Path,
    last_exc: OSError | None,
) -> tuple[Path | None, bool, OSError | None]:
    try:
        os.link(source, candidate)
    except FileExistsError as exc:
        return None, False, exc
    except OSError as exc:
        if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
            return _reserve_slot_via_rename(source), False, last_exc
        raise
    return candidate, True, last_exc


def _finish_linked_collision_move(source: Path, candidate: Path) -> None:
    try:
        _unlink_source_or_rollback_candidate(source, candidate)
    except BaseException:
        # A late interrupt after link but before unlink must not leak a hardlink.
        if source.exists():
            try:
                os.unlink(candidate)
            except OSError:
                pass
        raise


_TRANSIENT_LINK_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE, errno.EAGAIN})

# Jittered exponential backoff schedule for `_link_with_transient_retry`
# (oze-perf-07). Three attempts at 5ms / 20ms / 80ms — covers the typical
# FD-exhaustion window without burning a fixed 20ms when the slot frees up
# sooner. Each delay is multiplied by a uniform random factor in [0.5, 1.5]
# to desynchronise contending workers.
_LINK_RETRY_DELAYS_SEC: tuple[float, ...] = (0.005, 0.020, 0.080)

# oze-sec-03: `random` is per-process state derived from the import-time seed;
# workers forked after that share the same sequence and reproduce correlated
# jitter, which defeats the desynchronisation we want under FD exhaustion.
# `SystemRandom` reads from the kernel CSPRNG on every call, so each worker
# (and any post-fork child) draws independent values.
_RETRY_JITTER = secrets.SystemRandom()


def _link_with_transient_retry(src: Path, dst: Path) -> None:
    """`os.link(src, dst)` with jittered exponential backoff on transient
    FD-exhaustion errnos (oze-conc-02 / oze-perf-07).

    First attempt is unconditional; on EMFILE/ENFILE/EAGAIN we retry up to
    ``len(_LINK_RETRY_DELAYS_SEC)`` times. The delays start at 5ms — most
    EMFILE bursts clear faster than the old fixed 20ms — and grow geometrically
    to 80ms. Jitter avoids the thundering-herd where N workers all wake at the
    same instant and reproduce the original FD pressure.
    """
    # oze-perf-09: `_RETRY_JITTER` is module-level so heavy migrations don't pay
    # the import-lookup cost per retry attempt.
    last_exc: OSError | None = None
    for attempt, base_delay in enumerate((0.0,) + _LINK_RETRY_DELAYS_SEC):
        if base_delay > 0.0:
            time.sleep(base_delay * _RETRY_JITTER.uniform(0.5, 1.5))
        try:
            _link_regular_no_follow(src, dst)
            return
        except OSError as exc:
            if exc.errno not in _TRANSIENT_LINK_ERRNOS:
                raise
            last_exc = exc
    # Exhausted retries — surface the last transient error so the caller can
    # decide whether to fall back to the cross-device copy path. Convert the
    # assert to a real check so `python -O` still enforces it (oze-cx-05).
    if last_exc is None:
        raise RuntimeError("link retry loop exhausted without capturing an exception")  # pragma: no cover - defensive: loop returns on success
    raise last_exc


def _link_regular_no_follow(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst, follow_symlinks=False)
    except TypeError:
        os.link(src, dst)
    st = os.lstat(dst)
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
        try:
            os.unlink(dst)
        except OSError as exc:
            logger.warning("could not remove non-regular link target %s after refusing %s: %s", dst, src, exc)
        raise ValueError(f"refusing to move source that changed to non-regular during link: {src}")


# oze-di-01: byte-compare chunk for recognising a target left behind by an
# interrupted cross-device move. 64 KiB balances syscall count against the
# transient read buffer.
_CONTENT_CMP_CHUNK = 1 << 16


def _same_file_content(a: Path, b: Path) -> bool:
    """True when ``a`` and ``b`` are byte-identical (oze-di-01).

    Cross-device copies land on different inodes, so ``os.path.samefile`` can't
    recognise a completed-but-not-finalised move; we compare size then bytes.
    Any stat/read failure answers False — the caller then treats the target as
    a genuine collision rather than silently dropping the source.
    """
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                chunk_a = fa.read(_CONTENT_CMP_CHUNK)
                chunk_b = fb.read(_CONTENT_CMP_CHUNK)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def _move_cross_device(source: Path, target: Path) -> None:
    """Move a file across filesystems, kill-safe and idempotent (oze-di-01).

    Reserves the final name with O_EXCL (no-overwrite), copies to a temp file,
    then atomically renames it into place. Any failure before the rename rolls
    back the temp and reservation so no partial file survives.

    The window between ``os.replace`` and ``os.unlink(source)`` is NOT
    crash-atomic: a kill there (Ctrl+C at any moment, SIGKILL, power loss)
    leaves the file at BOTH paths. Recovery is idempotent — a re-run finds the
    target already holding this exact content and finishes the move by removing
    the leftover source, instead of failing the O_EXCL reservation forever and
    endlessly re-suffixing ``.collision<n>``. A target with *different* content
    is a real name collision and still raises.
    """
    if _prepare_cross_device_target(source, target):
        return
    # oze-sec-01: replace the PID-based suffix with cryptographically
    # random bytes. The previous `.{name}.{pid}.tmp` pattern was
    # predictable: an attacker with write access to the bucket dir
    # could pre-create that exact path (or symlink it elsewhere) and
    # race `shutil.copy2` into clobbering the wrong target. 8 bytes
    # of `secrets.token_hex` collapse that race to ~2^-64 odds.
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    _copy_cross_device_target(source, target, tmp)
    _unlink_cross_device_source(source, target)


def _prepare_cross_device_target(source: Path, target: Path) -> bool:
    try:
        _reserve_target(target)
        return False
    except FileExistsError:
        if _same_file_content(source, target):
            os.unlink(source)
            return True
        raise


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _copy_cross_device_target(source: Path, target: Path, tmp: Path) -> None:
    replaced = False
    try:
        shutil.copy2(source, tmp)
        _fsync_file(tmp)
        os.replace(tmp, target)  # atomic: target only ever holds the complete file
        replaced = True
        _fsync_directory(target.parent)
    except BaseException:
        # oze-cx-03: cleanup must never mask the real failure. Narrow to OSError
        # (the only thing os.unlink can raise on a missing/locked leftover) and
        # log at debug so unexpected exception classes still surface. Once the
        # rename succeeded the target IS the completed file — never unlink it on
        # a late interrupt; leave it for the idempotent re-run above.
        leftovers = (tmp,) if replaced else (tmp, target)
        for leftover in leftovers:
            try:
                os.unlink(leftover)
            except OSError as unlink_exc:
                logger.debug(
                    "cross-device cleanup: could not unlink %s: %s",
                    leftover, unlink_exc,
                )
        raise


def _unlink_cross_device_source(source: Path, target: Path) -> None:
    # oze-robust-01: the copy is committed at `target`; source removal has no
    # rollback (cross-fs target is an independent copy, not a hardlink). If the
    # unlink fails the file exists at BOTH paths as full copies — surface it
    # loudly instead of as a bare OSError so the orphaned duplicate is visible.
    # The move itself succeeded, so don't re-raise; an idempotent re-run reclaims
    # the source via the same-content check above.
    try:
        os.unlink(source)
    except OSError as unlink_exc:
        logger.warning(
            "moved %s -> %s but could not remove source (%s); "
            "duplicate left at %s — remove it manually",
            source, target, unlink_exc, source,
        )
        raise PartialMoveError(source, target, unlink_exc) from unlink_exc
    else:
        _fsync_directory(source.parent)


def _walk_prunable_dirs(
    root: Path,
    visit: Callable[[Path], None],
    label: str,
) -> bool:
    """Post-order walk of directories strictly under ``root`` (oze-dup-01).

    Shared traversal for :func:`prune_empty_dirs` and
    :func:`_count_prunable_dirs`: resolves ``root``, rejects a symlinked /
    non-directory root, then visits every non-symlink directory below it in
    ``os.walk(topdown=False)`` order, invoking ``visit(current)`` for each.
    Returns False when the root is unusable (caller returns 0).
    """
    root_resolved = _resolved_prunable_root(root, label)
    if root_resolved is None:
        return False
    for dirpath, _dirnames, _filenames in os.walk(
        root_resolved,
        topdown=False,
        followlinks=False,
        onerror=partial(_log_prunable_walk_error, label),
    ):
        current = _prunable_walk_path(dirpath, root_resolved, label)
        if current is not None:
            visit(current)
    return True


def _resolved_prunable_root(root: Path, label: str) -> Path | None:
    try:
        resolved = Path(root).resolve(strict=True)
    except OSError as exc:
        logger.debug("%s: cannot resolve root %s: %s", label, root, exc)
        return None
    return resolved if not resolved.is_symlink() and resolved.is_dir() else None


def _log_prunable_walk_error(label: str, exc: OSError) -> None:
    logger.debug("%s: walk error: %s", label, exc)


def _prunable_walk_path(
    dirpath: str,
    root_resolved: Path,
    label: str,
) -> Path | None:
    try:
        current = Path(dirpath)
    except (ValueError, OSError) as exc:
        logger.debug("%s: skip unparseable path %r: %s", label, dirpath, exc)
        return None
    if current == root_resolved:
        return None
    try:
        return None if current.is_symlink() else current
    except OSError:
        return None


def prune_empty_dirs(root: Path, verbose: bool = False) -> int:
    """Remove every empty directory strictly *under* ``root``.

    Post-order traversal (``os.walk(topdown=False)``) lets a directory whose
    only contents were now-removed empty children also be pruned. ``root``
    itself is never removed, even when empty.

    Robustness contract:

    * Symlinks are entries, not destinations — we never follow a symlink during
      the walk (``followlinks=False``) and a symlinked directory is not pruned
      even if its target is empty. A directory containing only symlinks is
      therefore NOT empty.
    * Race-tolerant: ``rmdir`` failures with ``ENOENT`` / ``ENOTEMPTY`` /
      ``EEXIST`` are normal outcomes (concurrent removal / late-arriving entry)
      and counted silently as skipped. All other ``OSError`` (notably
      ``EACCES``, ``EBUSY``) are logged at debug and skipped.
    * Encoding-safe: paths are walked via the OS-native byte layer
      (``os.walk`` over ``os.scandir``), so surrogate-escaped names from
      non-UTF-8 filesystems flow through without raising.
    * Name-content-blind: any byte sequence the kernel accepts as a dirname
      (control chars, newlines, BOMs, RTL marks, …) is handled — emptiness is
      determined by ``rmdir``, never by parsing the name.

    Returns the number of directories actually removed.
    """
    counter = {"removed": 0}

    def _try_remove(current: Path) -> None:
        try:
            os.rmdir(current)
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                logger.debug("prune_empty_dirs: cannot remove %s: %s", current, exc)
            return
        counter["removed"] += 1
        if verbose:
            logger.info("Removed empty dir: %s", current)

    _walk_prunable_dirs(root, _try_remove, "prune_empty_dirs")
    return counter["removed"]


def resolve_root(root: str | Path | None) -> Path:
    """Validate and resolve the root path to an absolute Path object."""
    if root is None or (isinstance(root, (str, Path)) and not str(root).strip()):
        raise ValueError('root path must be a non-empty string or Path')

    # Apply the length guard to Path too, not just str (oze-rel-03).
    if isinstance(root, (str, Path)) and len(str(root)) > ROOT_MAX_LENGTH:
        raise ValueError(f'root path must be at most {ROOT_MAX_LENGTH} characters')

    if not isinstance(root, (str, Path)):
        raise TypeError('root must be a path or string')

    # No canonicalization here: the is_dir() check and FileNotFoundError
    # message run against the raw path (oze-rel-05). Canonicalization with
    # `.resolve()` happens below once existence is confirmed.
    resolved = Path(root)

    if not resolved.is_dir():
        raise FileNotFoundError(f'Error: root folder does not exist: {resolved}')

    # oze-rel-02: re-apply the length guard to the RESOLVED path. A short
    # input can expand past ROOT_MAX_LENGTH via symlinks / `..`, bypassing
    # the pre-resolution check above.
    canonical = resolved.resolve()
    if len(str(canonical)) > ROOT_MAX_LENGTH:
        raise ValueError(
            f'resolved root path must be at most {ROOT_MAX_LENGTH} characters'
        )
    return canonical


class PartialMoveError(RuntimeError):
    def __init__(self, source: Path, destination: Path, cause: OSError) -> None:
        super().__init__(
            f"destination committed but source remains: {source} -> "
            f"{destination}: {cause}"
        )
        self.destination = destination


MoveResult = tuple[Path, Path, Exception | None]
WorkerFn = Callable[[Path, Path], MoveResult]


def make_worker(preview: bool) -> WorkerFn:
    """Return the per-move worker (oze-arch-01).

    Closing over ``preview`` lets the worker body stay pure: the planner picks
    the worker once, then submits ``(source, bucket_dir)`` pairs without
    re-passing the preview flag on every call. Preview mode short-circuits
    without touching the filesystem; live mode delegates to :func:`move_file`
    and converts expected per-file move failures into a skip-with-error tuple
    so a single failure cannot kill the worker pool. OSError covers filesystem
    errors; RuntimeError (collision-cap exhaustion, double-copy) and ValueError
    (out-of-range bucket index) are equally per-file, not run-fatal. A
    BaseException (KeyboardInterrupt, SystemExit) is never swallowed.
    """
    def _move_worker(source: Path, bucket_dir: Path) -> MoveResult:
        dest = bucket_dir / source.name
        if preview:
            return source, dest, None
        try:
            # oze-obs-01: log where the file actually landed. move_file may
            # rename the source aside to `<name>.collision<n>`, so the precomputed
            # `dest` can diverge from reality — use its return value.
            actual = move_file(source, bucket_dir)
            return source, actual, None
        except PartialMoveError as exc:
            return source, exc.destination, exc
        except (OSError, RuntimeError, ValueError) as exc:
            return source, dest, exc
    return _move_worker


def plan_moves(
    root: Path,
    files: Iterable[Path],
    manager: BucketManager,
    ctx: SniffContext | None = None,
    preview: bool = False,
) -> Iterator[tuple[Path, Path]]:
    """Yield ``(source, bucket_dir)`` pairs for every file in ``files`` (oze-decl-01).

    Pure planning: only the manager's caches are mutated; no filesystem writes
    happen here EXCEPT the pre-pass at the top that resolves source-name /
    bucket-dir collisions (oze-rel-14) — those renames MUST happen serially
    before any worker fires, otherwise worker A can rename the source of
    worker B mid-flight (the prior in-worker resolution race). Bucket
    directories are reserved in the manager's name-set cache as a side effect
    of :meth:`BucketManager.choose` so a later iteration can see the
    reservation (oze-conc-01).

    When ``preview`` is True (oze-rel-01) the collision pre-pass records the
    would-rename instead of performing it, honouring the "preview never touches
    the filesystem" contract.

    The caller is responsible for ordering — pass ``sorted(files)`` if
    deterministic plans matter; the function asserts the sort to catch
    callers that forget (oze-decl-02).

    Returning an iterator means the plan can be inspected (or unit-tested)
    without running any moves, and moves stream out one at a time rather than
    buffering the executed plan. The serial collision pre-pass does hold an
    O(files) list of ``(source, ext_dir)`` pairs — unavoidable for a global
    cross-worker collision view — but it no longer retains head bytes for the
    whole tree: those are evicted as each ext is resolved (oze-scal-01), so peak
    head_cache RSS tracks the in-flight move window, not the file count.
    """
    if ctx is None:
        ctx = _DEFAULT_SNIFF_CTX
    # oze-decl-02: enforce the docstring contract. Sorted-list inputs are
    # required for deterministic plans; iterators are accepted (we have
    # no way to assert order without consuming first).
    if isinstance(files, list):
        if not all(a <= b for a, b in pairwise(files)):
            raise ValueError(
                "plan_moves expects `files` to be sorted; pass sorted(files)"
            )
    # Returns [(current_source_path, original_ext_dir)] — preplan resolves
    # the extension BEFORE any rename so a `.collision<n>` suffix doesn't
    # accidentally re-classify the file (oze-rel-14).
    plan_pairs = _preplan_resolve_collisions(root, list(files), ctx, preview=preview)
    for source, ext_dir in plan_pairs:
        # oze-rel-18: use `.name` so dotfiles (`.bashrc` → stem="") and
        # multi-dot names (`archive.tar.gz` → stem="archive.tar") get
        # bucketed off the same first letter the scanner sees on disk.
        prefix = normalize_prefix(source.name)
        # `manager.choose` returns a Bucket value object (oze-pat-01); the
        # worker pipeline only needs the path, so unwrap here.
        #
        # oze-rel-02: bucket-space exhaustion makes `bucket_name(index > max)`
        # raise ValueError, and a non-conforming bucket path raises RuntimeError.
        # Both are per-file planning failures — skip the one file with a warning
        # instead of letting the exception propagate out of the plan generator
        # and kill the entire run.
        try:
            bucket = manager.choose(source, ext_dir, prefix)
        except (ValueError, RuntimeError) as exc:
            logger.warning("Skipped %s: bucket selection failed: %s", source, exc)
            continue
        # oze-cmplx-01: the head_cache entry for this source is dropped by
        # `_drain_futures` once the move completes; popping it here too was a
        # redundant no-op on the live pipeline. The single drain-side pop keeps
        # the cache RSS tracking the in-flight plan window (oze-scal-06).
        yield source, bucket.path


def _preplan_resolve_collisions(
    root: Path,
    files: list[Path],
    ctx: "SniffContext",
    preview: bool = False,
) -> list[tuple[Path, Path]]:
    """Pre-pass over `files` (oze-rel-14): if any source file's own path
    sits on the ancestor chain of a bucket dir that ANY file in the plan
    needs, rename that source aside now — serially, before any worker is
    submitted. After this pass every yielded source is at a path that no
    concurrent worker can race to rename.

    Returns a list of ``(current_source_path, original_ext_dir)`` tuples.
    The extension is resolved BEFORE any rename so a `.collision<n>` suffix
    doesn't accidentally re-classify the file (e.g. ``avi`` (no header)
    renamed to ``avi.collision1`` must still bucket under ``no_extension/``,
    not ``collision1/``).

    When ``preview`` is True (oze-rel-01) no rename is performed: the
    would-rename is logged and the source path is left unchanged in the
    returned plan, so ``--preview`` never mutates the filesystem.
    """
    # Compute the target ext-dir for every file once; build a set of every
    # ancestor path that any plan needs to exist as a directory.
    #
    # oze-scal-01: the global collision pass must know every file's resolved
    # ext, but nothing downstream re-sniffs (choose()/move_file work off the
    # ext-dir computed here). Evict each file's head bytes as soon as the ext is
    # captured so the pre-pass does NOT prime head_cache for the whole tree —
    # that restores the bounded per-window cache the drain-side pop assumes and
    # keeps peak RSS off the file count.
    pairs = _resolved_plan_pairs(root, files, ctx)
    needed_dirs = _needed_plan_dirs(root, pairs)
    rename_map = _planning_collision_renames(pairs, needed_dirs, ctx, preview)
    return [(rename_map.get(src, src), ext_dir) for src, ext_dir in pairs]


def _resolved_plan_pairs(
    root: Path,
    files: list[Path],
    ctx: SniffContext,
) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for source in files:
        ext_dir = root / resolve_real_extension(source, ctx=ctx)
        pairs.append((source, ext_dir))
        _evict_sniff_head(ctx, source)
    return pairs


def _needed_plan_dirs(
    root: Path,
    pairs: list[tuple[Path, Path]],
) -> set[Path]:
    needed: set[Path] = set()
    for _, ext_dir in pairs:
        current = ext_dir
        while current != current.parent and current != root:
            needed.add(current)
            current = current.parent
    return needed


def _planning_collision_renames(
    pairs: list[tuple[Path, Path]],
    needed_dirs: set[Path],
    ctx: SniffContext,
    preview: bool,
) -> dict[Path, Path]:
    rename_map: dict[Path, Path] = {}
    for source, _ext_dir in pairs:
        if not _blocks_a_needed_dir(source, needed_dirs):
            continue
        candidate = _resolve_one_planning_collision(source, ctx, preview)
        if candidate is not None:
            rename_map[source] = candidate
    return rename_map


def _blocks_a_needed_dir(source: Path, needed_dirs: set[Path]) -> bool:
    """True when ``source``'s own path occupies a directory another file needs.

    Two shapes block bucket creation:

    * **ext-dir** (oze-rel-14): ``source`` IS a needed ``root/<ext>`` dir.
    * **bucket-dir** (oze-conc-01): ``source`` sits at ``root/<ext>/<prefix>NNNNN``
      — a bucket dir whose index ``choose()`` picks *after* this serial pass, so
      it's invisible to the ext-only ``needed_dirs`` set. Detect it structurally:
      the parent is a needed ext-dir and the name matches the bucket pattern.
      Reserving it here keeps the runtime ``_resolve_source_collision`` from
      renaming a different worker's in-flight source (the cross-worker race the
      preplan exists to close).
    """
    if source in needed_dirs:
        return True
    return (
        source.parent in needed_dirs
        and BUCKET_NAME_PATTERN.match(source.name) is not None
    )


def _resolve_one_planning_collision(
    source: Path,
    ctx: "SniffContext",
    preview: bool,
) -> "Path | None":
    """Resolve one source that blocks a bucket ancestor (oze-rel-14).

    Returns the renamed path, or None when nothing was renamed (source
    vanished, isn't a regular file, or ``preview`` suppresses the rename).

    oze-rel-04: classify via ``lstat`` + ``S_ISREG`` rather than
    ``Path.is_file()`` (which follows symlinks). plan_moves is public and
    accepts arbitrary inputs; a symlink-to-regular-file source must not be
    hardlinked by its target via :func:`_atomic_rename_to_free_slot`.
    """
    if not os.path.lexists(source):
        return None
    try:
        st = os.lstat(source)
    except OSError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return None
    if preview:
        # oze-rel-01: preview never touches the filesystem — log the
        # would-rename and leave the source in place.
        logger.info(
            "Preview: name collision — source %s would be renamed aside "
            "before placing under its bucket",
            source,
        )
        return None
    try:
        # oze-rel-19: atomic — re-probes free slot on FileExistsError.
        candidate = _atomic_rename_to_free_slot(source)
    except FileNotFoundError:
        return None   # vanished between our scan and rename — race tolerated
    logger.warning(
        "name collision (planning): source %s would block bucket "
        "ancestor; renamed to %s before any worker starts",
        source, candidate,
    )
    # oze-scal-01: no head_cache re-key needed — the caller evicts each file's
    # head bytes as soon as its ext is resolved (the move path never re-sniffs),
    # so there is no entry under the old key to carry over.
    return candidate


@dataclass
class _RunStats:
    """Mutable processed/skipped tally threaded through :func:`_run_moves`."""

    processed: int = 0
    skipped: int = 0
    partial: int = 0


def _drain_futures(
    futures: dict[Future, Path],
    stats: _RunStats,
    preview: bool,
    head_cache: dict[Path, HeadBytes],
    manager: BucketManager | None = None,
    wait_timeout: float = MOVE_STALL_WARN_SECONDS,
    max_stall_seconds: float = MOVE_MAX_STALL_SECONDS,
    now_fn: Callable[[], float] = time.monotonic,
) -> None:
    """Block until at least one future completes, then log results and prune
    the head_cache for finished sources (oze-conc-03 / oze-scal-05)."""
    done = _wait_for_move_futures(
        futures, wait_timeout, max_stall_seconds, now_fn)
    for future in done:
        _drain_move_future(
            future, futures, stats, preview, head_cache, manager)


def _wait_for_move_futures(
    futures: dict[Future, Path],
    wait_timeout: float,
    max_stall_seconds: float,
    now_fn: Callable[[], float],
) -> set[Future]:
    stalled_since = now_fn()
    while True:
        done, _ = wait(
            futures, timeout=wait_timeout, return_when=FIRST_COMPLETED)
        if done:
            return done
        elapsed = now_fn() - stalled_since
        if elapsed >= max_stall_seconds:
            for future in futures:
                future.cancel()
            raise RuntimeError(
                "move stage aborted after "
                f"{elapsed:.0f}s with no completed worker "
                f"({len(futures)} in flight)"
            )
        logger.warning(
            "move stage stalled: no completed worker for %.0fs "
            "(%d in flight)",
            elapsed, len(futures),
        )


def _drain_move_future(
    future: Future,
    futures: dict[Future, Path],
    stats: _RunStats,
    preview: bool,
    head_cache: dict[Path, HeadBytes],
    manager: BucketManager | None,
) -> None:
    source = futures.pop(future)
    head_cache.pop(source, None)
    try:
        _, destination, error = future.result()
    except Exception as exc:
        logger.warning("Skipped %s: unexpected worker error: %s", source, exc)
        stats.skipped += 1
        return
    if error:
        if isinstance(error, PartialMoveError):
            logger.error("Partial move %s: %s", source, error)
            stats.partial += 1
            return
        logger.warning(f"Skipped {source}: {error}")
        if manager is not None:
            manager.release(source, destination.parent)
        stats.skipped += 1
        return
    action = "Preview:" if preview else "Moved"
    logger.info(f"{action} {source} -> {destination}")
    stats.processed += 1


def _maybe_log_progress(
    stats: _RunStats, total_files: int, progress: dict[str, int]
) -> None:
    """Emit the periodic ``progress:`` line once every ``PROGRESS_EVERY``
    completed items (oze-obs-01). ``progress["last"]`` tracks the count at the
    previous emission so submit-loop and drain-loop calls share one cadence.

    oze-obs-10: ``total_files`` is the SCAN-stage count; the plan can drop files
    (bucket-selection failure) and add ``.collision`` renames, so the final
    processed+skipped tally need not equal it. The denominator is rendered with
    a leading ``~`` to mark it as an estimate, not a hard target."""
    done_so_far = stats.processed + stats.skipped + stats.partial
    if (done_so_far - progress["last"]) >= PROGRESS_EVERY:
        logger.info("progress: processed %d/~%d, skipped %d",
                    stats.processed, total_files, stats.skipped)
        progress["last"] = done_so_far


def _run_moves(
    plan: Iterator[tuple[Path, Path]],
    worker: WorkerFn,
    num_threads: int,
    preview: bool,
    total_files: int,
    head_cache: dict[Path, HeadBytes],
    manager: BucketManager,
) -> _RunStats:
    """Execute stage (oze-cmplx-01): own the thread pool, the bounded-backlog
    submission loop, drain, and progress logging. Returns the run tally.

    Bounded-outstanding submission (oze-scal-04) keeps at most
    ``num_threads * SUBMIT_BACKLOG_MULT`` futures in flight. The executor is
    managed manually so KeyboardInterrupt can cancel pending moves immediately
    (conc-01) instead of draining them via ``with`` __exit__.
    """
    stats = _RunStats()
    max_outstanding = max(1, num_threads * SUBMIT_BACKLOG_MULT)
    executor = ThreadPoolExecutor(max_workers=num_threads)
    futures: dict[Future, Path] = {}
    progress = {"last": 0}
    try:
        for source, bucket_dir in plan:
            while len(futures) >= max_outstanding:
                _drain_futures(futures, stats, preview, head_cache, manager)
                _maybe_log_progress(stats, total_files, progress)
            futures[executor.submit(worker, source, bucket_dir)] = source
            _maybe_log_progress(stats, total_files, progress)
        # oze-obs-01: also evaluate PROGRESS_EVERY while draining the tail, so
        # a run whose last >PROGRESS_EVERY files finish after the plan iterator
        # is exhausted still emits progress instead of going silent until the
        # summary.
        while futures:
            _drain_futures(futures, stats, preview, head_cache, manager)
            _maybe_log_progress(stats, total_files, progress)
    except KeyboardInterrupt:
        executor.shutdown(wait=True, cancel_futures=True)
        logger.info(f"\nInterrupted. Processed {stats.processed} file(s), "
                    f"skipped {stats.skipped} file(s), partial {stats.partial}.")
        raise SystemExit(1) from None
    finally:
        # Running filesystem calls cannot be cancelled safely. Wait for them so
        # no mutation continues after this function reports failure or returns.
        executor.shutdown(wait=True, cancel_futures=True)
    return stats


def _log_run_stats(head_cache: dict[Path, HeadBytes], manager: BucketManager) -> None:
    """End-of-run debug line with cache + bucket counters (oze-obs-03)."""
    logger.debug(
        "stats: head_cache_entries=%d unreadable=%d "
        "new_bucket_allocated=%d bucket_reused=%d buckets_full=%d",
        len(head_cache),
        sum(1 for v in head_cache.values() if isinstance(v, _Unreadable)),
        manager.stats["new_bucket_allocated"],
        manager.stats["bucket_reused"],
        manager.stats["buckets_full"],
    )


def _clamp_num_threads(num_threads: int) -> int:
    """Validate and ceiling-clamp the worker count (oze-robust-11)."""
    if not isinstance(num_threads, int) or isinstance(num_threads, bool) or num_threads < 1:
        raise ValueError(f"num_threads must be a positive integer (>=1), got {num_threads!r}")
    if num_threads > MAX_NUM_THREADS:
        logger.warning(
            "num_threads %d exceeds the ceiling %d; clamping.",
            num_threads, MAX_NUM_THREADS)
        return MAX_NUM_THREADS
    return num_threads


def _run_move_stage(root: Path, files: list[Path], ctx: SniffContext, *,
                    preview: bool, verbose: bool, num_threads: int,
                    bucket_size: int,
                    bucket_manager: BucketManager | None,
                    head_cache: dict[Path, HeadBytes]) -> None:
    """Plan then execute the moves for the scanned `files` (oze-arch pipeline
    stage): plan_moves over a BucketManager, run the pool, and log run stats."""
    manager = (
        bucket_manager
        if bucket_manager is not None
        else BucketManager(root=root, bucket_size=bucket_size)
    )
    plan = plan_moves(root, sorted(files), manager, ctx=ctx, preview=preview)
    stats = _run_moves(
        plan,
        worker=make_worker(preview),
        num_threads=num_threads,
        preview=preview,
        total_files=len(files),
        head_cache=head_cache,
        manager=manager,
    )
    if verbose or preview or stats.processed > 0 or stats.skipped > 0 or stats.partial > 0:
        logger.info(
            f"Finished. Processed {stats.processed} file(s), "
            f"skipped {stats.skipped} file(s), partial {stats.partial}."
        )
    _log_run_stats(head_cache, manager)


def _run_prune_stage(root: Path, *, preview: bool, verbose: bool) -> None:
    """Prune empty directories under `root`, or count them under `--preview`
    without mutating the tree."""
    if preview:
        # Preview mode never touches the filesystem during planning; honour
        # that contract here too — count what *would* be removed.
        count = _count_prunable_dirs(root)
        if verbose or count > 0:
            logger.info("Preview: would remove %d empty directory/ies under %s", count, root)
    else:
        count = prune_empty_dirs(root, verbose=verbose)
        if verbose or count > 0:
            logger.info("Pruned %d empty directory/ies under %s", count, root)


def organize(
    root: str | Path,
    preview: bool = False,
    verbose: bool = False,
    num_threads: int = 3,
    sniff: bool = True,
    extra_zip_family: frozenset[str] = frozenset(),
    bucket_size: int = BUCKET_SIZE,
    bucket_manager: BucketManager | None = None,
    head_cache: dict[Path, HeadBytes] | None = None,
    prune_empty: bool = False,
) -> None:
    """Organize files under the root path into extension-based buckets.

    If ``preview`` is True, the script prints move actions without performing them.
    If ``sniff`` is True (default), the file's header bytes are inspected and the
    detected real type wins over the declared extension (``mypdf.doc`` → ``pdf/``).
    ``extra_zip_family`` extends the ZIP container family (oze-rel-07) so newly
    arrived zip-based formats (``.usdz``, ``.crx``, …) are not bucketed under
    ``zip/`` by mistake. Handles KeyboardInterrupt gracefully by printing a
    summary and exiting.

    Architecture (oze-decl-01 / oze-arch-01 / oze-arch-02): the body is a thin
    pipeline — *scan* (with shared head-bytes cache, oze-perf-04) → *plan*
    (``plan_moves`` over a :class:`BucketManager`) → *execute* (closures
    returned by :func:`make_worker` submitted to a thread pool). The pipeline
    stays streaming: plans are issued one at a time, never buffered.
    """
    num_threads = _clamp_num_threads(num_threads)
    if (not isinstance(bucket_size, int) or isinstance(bucket_size, bool)
            or bucket_size < 1):
        raise ValueError(
            f"bucket_size must be a positive integer, got {bucket_size!r}"
        )
    root = resolve_root(root)
    if verbose:
        logger.info(f"Organizing files in: {root}")
    # oze-decl-02: accept caller-provided pipeline state so tests / alternate
    # runners can pre-seed or assert against it. Defaults preserve the
    # original behaviour for the CLI entry point.
    if head_cache is None:
        head_cache = {}
    ctx = SniffContext(sniff=sniff, head_cache=head_cache,
                       extra_zip_family=extra_zip_family)
    files = list_files(
        root,
        skip_paths={Path(__file__).resolve()},
        verbose=verbose,
        ctx=ctx,
    )

    # oze-rel-12: when there are no files to move, skip the move stage but
    # still fall through to the prune stage below — an already-organized tree
    # (every file already bucketed) is the common case where the user runs
    # --prune-empty-dirs to clean up leftover empty dirs. Returning here
    # silently dropped the prune request whenever --verbose/--preview was set.
    if not files:
        if preview or verbose:
            logger.info(f"No files to organize under {root} (already bucketed or empty).")
    else:
        _run_move_stage(
            root, files, ctx,
            preview=preview, verbose=verbose, num_threads=num_threads,
            bucket_size=bucket_size,
            bucket_manager=bucket_manager, head_cache=head_cache,
        )

    if prune_empty:
        _run_prune_stage(root, preview=preview, verbose=verbose)


def _count_prunable_dirs(root: Path) -> int:
    """Dry-run companion to :func:`prune_empty_dirs` used by ``--preview``.

    Walks the same post-order, tracks which directories would be removed by
    simulating the cascade (a parent becomes prunable once all its children
    were marked prunable), and returns the count. Pure read-only — no
    ``rmdir``. Symlinks are entries (never pruned, never followed).
    """
    would_remove: set[Path] = set()

    def _mark_if_prunable(current: Path) -> None:
        if _dir_is_prunable(current, would_remove):
            would_remove.add(current)

    _walk_prunable_dirs(root, _mark_if_prunable, "_count_prunable_dirs")
    return len(would_remove)


def _dir_is_prunable(current: Path, would_remove: set[Path]) -> bool:
    """True iff every entry in ``current`` is a subdirectory already marked in
    ``would_remove`` (oze-dup-01). Files / symlinks of any kind / unreadable
    entries make it non-empty. Routes through :func:`_safe_scandir`
    (oze-dup-03) so an unreadable directory is logged at debug instead of
    being silently swallowed by an open-coded ``try``. An unreadable directory
    (``_safe_scandir`` yields the empty tuple) is treated as non-empty so it is
    never marked prunable."""
    with _safe_scandir(current) as it:
        if isinstance(it, tuple):
            return False
        try:
            for entry in it:
                if entry.is_symlink():
                    return False
                if not entry.is_dir(follow_symlinks=False):
                    return False
                if Path(entry.path) not in would_remove:
                    return False
        except OSError:
            return False
    return True


def _positive_int(value: str) -> int:
    """argparse `type=` validator (oze-test-04): accept only positive
    integers. Replaces the bare ``type=int`` on ``--threads`` so
    ``--threads 0`` / ``--threads -3`` surface a clean usage error at
    parse time instead of a cryptic ``ThreadPoolExecutor(max_workers=0)``
    crash deep inside the run."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer (>=1), got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser used by the script."""
    parser = argparse.ArgumentParser(
        description='Organize files by extension into bucketed subdirectories.'
    )
    parser.add_argument(
        'root',
        nargs='?',
        help='Root folder to scan and reorganize.',
    )
    parser.add_argument(
        '--preview',
        '-n',
        action='store_true',
        help='Show planned moves without applying them.',
    )
    parser.add_argument(
        '--verbose',
        '-v',
        action='count',
        default=0,
        help='Enable verbose output. Repeat (-vv) for per-file DEBUG scan logging.',
    )
    parser.add_argument(
        '--threads',
        '-j',
        type=_positive_int,
        default=3,
        help='Number of worker threads (positive int, default: 3).',
    )
    parser.add_argument(
        '--bucket-size',
        type=_positive_int,
        default=BUCKET_SIZE,
        help=f'Max files per bucket directory (positive int, default: {BUCKET_SIZE}).',
    )
    parser.add_argument(
        '--no-sniff',
        dest='sniff',
        action='store_false',
        help='Disable file-header type detection; bucket strictly by filename extension.',
    )
    parser.add_argument(
        '--extra-zip-family',
        dest='extra_zip_family',
        default='',
        help=(
            'Comma-separated extensions to treat as ZIP-family containers '
            '(e.g. "usdz,crx,xpi"). Files of these extensions whose header is '
            'PK\\x03\\x04 stay under their declared extension instead of being '
            'bucketed under zip/.'
        ),
    )
    parser.add_argument(
        '--prune-empty-dirs',
        dest='prune_empty',
        action='store_true',
        help=(
            'After moving files into buckets, recursively remove every empty '
            'directory under the root. Root itself is preserved. Symlinks are '
            'treated as entries: a symlinked directory is never pruned and a '
            'directory containing only symlinks is NOT considered empty.'
        ),
    )
    return parser


_EXTRA_ZIP_FAMILY_MAX_LEN = 16
_EXTRA_ZIP_FAMILY_VALID = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _parse_extra_zip_family(raw: str) -> frozenset[str]:
    """Split a ``--extra-zip-family`` CSV into a normalised frozenset.

    Lowercased and stripped of dots/whitespace so ``"USDZ, .crx"`` and
    ``"usdz,crx"`` produce the same set. Empty/blank entries are dropped.

    oze-sec-03: each item is validated AFTER normalisation. The set is
    only used for membership tests today, so no exploit exists, but
    defence-in-depth rejects items longer than
    ``_EXTRA_ZIP_FAMILY_MAX_LEN`` chars or containing anything outside
    ``[a-z0-9]`` (path separators, NUL, control chars, Unicode).

    oze-obs-01: a dropped item is non-fatal (a hard raise would block the
    whole run on a single typo) but is logged at WARNING so a malformed
    ``--extra-zip-family`` entry doesn't vanish without a trace."""
    if not raw:
        return frozenset()
    out: set[str] = set()
    for item in raw.split(','):
        normalised = item.strip().lstrip('.').lower()
        if not normalised:
            continue
        if len(normalised) > _EXTRA_ZIP_FAMILY_MAX_LEN or any(
                ch not in _EXTRA_ZIP_FAMILY_VALID for ch in normalised):
            logger.warning(
                "ignoring invalid --extra-zip-family item %r "
                "(must be 1..%d chars of [a-z0-9])",
                item, _EXTRA_ZIP_FAMILY_MAX_LEN)
            continue
        # oze-arch-01: canonicalise synonyms so membership matches the
        # `declared_canon` resolve_real_extension compares against — otherwise an
        # aliased item (e.g. `jpeg`) is stored un-canonicalised and never matches.
        out.add(EXTENSION_ALIASES.get(normalised, normalised))
    return frozenset(out)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments and return the parsed namespace."""
    return build_parser().parse_args()


def main() -> None:
    """Entry point for script execution."""
    parser = build_parser()
    args = parser.parse_args()
    # oze-dup-08: single basicConfig call. The previous code configured
    # logging twice — once with INFO when `args.root is None` (help
    # path) and again with the computed level on the normal path. The
    # second call was silently dropped because basicConfig is a no-op
    # after the first invocation, so the help path ended up at INFO
    # regardless of `--verbose`. One call, computed level, no surprise.
    if args.root is None:
        log_level = logging.INFO
    elif args.verbose >= 2:
        # -vv: per-file scan trace (oze-obs-06) for a long silent scan where
        # the 10k-file heartbeat never fires on a smaller-but-slow tree.
        log_level = logging.DEBUG
    elif args.verbose or args.preview:
        # --preview is a dry run whose whole point is to show planned
        # moves, so it always logs at INFO even without --verbose.
        log_level = logging.INFO
    else:
        log_level = logging.WARNING
    logging.basicConfig(level=log_level, format='%(message)s')
    if args.root is None:
        parser.print_help()
        raise SystemExit(0)
    try:
        root = resolve_root(args.root)
    except (ValueError, TypeError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from exc
    try:
        organize(
            root,
            preview=args.preview,
            verbose=bool(args.verbose),
            num_threads=args.threads,
            sniff=args.sniff,
            extra_zip_family=_parse_extra_zip_family(args.extra_zip_family),
            bucket_size=args.bucket_size,
            prune_empty=args.prune_empty,
        )
    except KeyboardInterrupt:
        # oze-obs-05: a Ctrl+C during the scan / plan / prune stages reaches
        # here (the move stage prints its own summary and exits non-zero via
        # SystemExit, bypassing this handler). Surface a single line so an
        # interrupt during a long silent scan isn't mistaken for a clean no-op.
        # Logged at WARNING so it shows even without --verbose, then exit
        # non-zero like the move stage so callers can detect interruption.
        logger.warning("Interrupted.")
        raise SystemExit(1) from None


if __name__ == '__main__':  # pragma: no cover
    main()
