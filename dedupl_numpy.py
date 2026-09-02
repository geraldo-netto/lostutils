#!/usr/bin/env python3
"""Vectorized via numpy. mmap the file, find newline positions in one
SIMD pass, build a fixed-width view of hash bytes, and call np.unique
to do the grouping in C. The only per-line Python work is the final
path extraction (variable-width slicing isn't naturally vectorizable)."""
from __future__ import annotations

import argparse
import json
import mmap
import os
import sys
from typing import BinaryIO

import numpy as np

HASH_SEPARATOR_WIDTH = 1
HASH_GATHER_CHUNK_RECORDS = 16_384
_ESCAPED_PATH_PREFIX = b"@lostutils-json:"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print paths belonging to duplicate hash groups.",
    )
    parser.add_argument("hash_file", help="file containing hash/path records")
    parser.add_argument(
        "-0", "--print0", action="store_true",
        help="separate paths with NUL instead of newline, so that paths "
             "containing a newline are emitted literally instead of staying "
             "in their escaped form",
    )
    return parser.parse_args()


def _record_layout(
    data: np.ndarray,
    line_ends: np.ndarray,
) -> tuple[np.ndarray, int, int]:
    line_starts = np.empty(len(line_ends), dtype=np.int64)
    line_starts[0] = 0
    line_starts[1:] = line_ends[:-1] + 1

    separators = np.flatnonzero(data[: line_ends[0]] == 0x20)
    if len(separators) == 0:
        raise ValueError("line 1 has no hash/path separator")
    hash_width = int(separators[0])
    path_offset = hash_width + HASH_SEPARATOR_WIDTH

    line_lengths = line_ends - line_starts
    if hash_width == 0 or np.any(line_lengths <= path_offset):
        raise ValueError("every record must contain a hash and nonempty path")
    if np.any(data[line_starts + hash_width] != 0x20):
        raise ValueError("all records must use the same hash width")
    return line_starts, hash_width, path_offset


def _line_content_end(data: np.ndarray, line_end: int) -> int:
    return line_end - int(line_end > 0 and data[line_end - 1] == 0x0D)


def _open_input(path: str) -> mmap.mmap | None:
    with open(path, "rb") as source:  # NOSONAR -- input path is the CLI's explicit contract.
        if os.fstat(source.fileno()).st_size == 0:
            return None
        return mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)


def _gather_hashes(
    data: np.ndarray,
    line_starts: np.ndarray,
    hash_width: int,
) -> np.ndarray:
    hash_bytes = np.empty((len(line_starts), hash_width), dtype=np.uint8)
    offsets = np.arange(hash_width)
    for start in range(0, len(line_starts), HASH_GATHER_CHUNK_RECORDS):
        stop = min(start + HASH_GATHER_CHUNK_RECORDS, len(line_starts))
        hash_idx = line_starts[start:stop, None] + offsets
        np.take(data, hash_idx, out=hash_bytes[start:stop])
    return hash_bytes.view(f"S{hash_width}").ravel()


def group_duplicates(data: np.ndarray) -> tuple[set[bytes], int, int]:
    """Return duplicate paths, redundant-file count, and record count."""
    nl = np.flatnonzero(data == 0x0A)
    if len(data) == 0:
        return set(), 0, 0
    if data[-1] != 0x0A:
        nl = np.append(nl, len(data))

    line_starts, hash_width, path_offset = _record_layout(data, nl)
    n_lines = len(line_starts)

    hashes = _gather_hashes(data, line_starts, hash_width)

    _, inverse, counts = np.unique(
        hashes, return_inverse=True, return_counts=True)
    group_size = counts[inverse]
    dup_line_indices = np.flatnonzero(group_size > 1)

    file_equal = int((counts[counts > 1] - 1).sum())

    # Path extraction is variable-width → list comprehension.
    paths = {
        bytes(
            data[
                line_starts[i] + path_offset : _line_content_end(data, int(nl[i]))
            ]
        )
        for i in dup_line_indices.tolist()
    }
    return paths, file_equal, n_lines


def _decode_record_path(path: bytes) -> bytes | None:
    """Recover the real path from hash-recursive-ai5.py's tagged JSON form.

    The record format is ``<digest> <path>``, so a path that would not survive
    that parse — one holding a newline, edged with whitespace, or empty — is
    written as ``@lostutils-json:<json>`` instead. Emitting that token verbatim
    hands the caller a string that is not a path (dnp-api-50). Mirrors
    remove-deduplv3.py's decoder, duplicated in-file because every script here
    is standalone.

    ``json.loads`` raises `UnicodeDecodeError` on non-UTF-8 bytes and
    `JSONDecodeError` on a malformed payload; both derive from `ValueError`.
    Returns ``None`` for a record whose escape cannot be read back.
    """
    if not path.startswith(_ESCAPED_PATH_PREFIX):
        return path
    try:
        decoded = json.loads(path[len(_ESCAPED_PATH_PREFIX):])
    except ValueError:
        return None
    if not isinstance(decoded, str):
        return None
    # hash-recursive-ai5.py escapes with ensure_ascii=True, so a filename that
    # is not valid UTF-8 arrives as the surrogates os.fsdecode produced;
    # os.fsencode is what turns them back into the original bytes.
    return os.fsencode(decoded)


def _decode_paths(
    paths: set[bytes],
    line_oriented: bool,
) -> tuple[set[bytes], int, int]:
    """Decode escaped record paths. Returns the paths plus the number that
    were unreadable and the number held back as unsafe to print.

    A path containing a newline cannot be written into a newline-separated
    stream without splitting into two records, so in that mode it stays in its
    escaped form — still lossless, and still decodable by a reader that knows
    the format. ``--print0`` lifts the restriction.
    """
    decoded_paths: set[bytes] = set()
    unreadable = 0
    unprintable = 0
    for raw in paths:
        path = _decode_record_path(raw)
        if path is None:
            unreadable += 1
            continue
        if line_oriented and (b"\n" in path or b"\r" in path):
            unprintable += 1
            path = raw
        decoded_paths.add(path)
    return decoded_paths, unreadable, unprintable


def _report_decode_warnings(unreadable: int, unprintable: int) -> None:
    if unreadable:
        print(
            f"warning: {unreadable} escaped path(s) could not be decoded "
            "and were dropped",
            file=sys.stderr,
        )
    if unprintable:
        print(
            f"warning: {unprintable} path(s) contain a newline and were left "
            "escaped; re-run with --print0 to emit them literally",
            file=sys.stderr,
        )


def _write_paths(
    paths: set[bytes],
    out: BinaryIO,
    separator: bytes = b"\n",
) -> bool:
    try:
        for path in sorted(paths):
            out.write(path + separator)
        out.flush()
    except BrokenPipeError:
        return False
    return True


def _silence_stdout_after_broken_pipe() -> None:
    """Point fd 1 at /dev/null after a handled BrokenPipeError (dnp-rob-50).

    Catching the exception in :func:`_write_paths` is not enough: the
    interpreter still flushes stdout at shutdown, that flush re-raises, and
    `dedupl_numpy.py hashes.txt | head` ends with
    ``Exception ignored in: <_io.TextIOWrapper name='<stdout>'>
    BrokenPipeError`` and exit status 120 — noise for what is the expected
    result of the reader closing the pipe early. Redirecting the descriptor
    (not just rebinding ``sys.stdout``) is what makes the shutdown flush
    succeed. Mirrors remove-deduplv3.py, duplicated in-file because every
    script here is standalone.
    """
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        except (AttributeError, OSError, ValueError):
            pass
        try:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            pass
    finally:
        try:
            os.close(devnull_fd)
        except OSError:
            pass


def main() -> None:
    args = _parse_args()

    try:
        mm = _open_input(args.hash_file)
    except OSError as exc:
        print(f"error: cannot read {args.hash_file}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if mm is None:
        return

    data = np.frombuffer(mm, dtype=np.uint8)
    error = None
    result = None
    try:
        result = group_duplicates(data)
    except ValueError as exc:
        error = str(exc)
    del data
    try:
        mm.close()
    finally:
        if error is not None:
            print(f"error: {error}", file=sys.stderr)
            raise SystemExit(1)
    if result is None:
        raise RuntimeError("duplicate grouping produced no result")
    paths, file_equal, n_lines = result
    if n_lines == 0:
        return

    paths, unreadable, unprintable = _decode_paths(paths, not args.print0)
    separator = b"\0" if args.print0 else b"\n"
    if not _write_paths(paths, sys.stdout.buffer, separator):
        _silence_stdout_after_broken_pipe()
        return
    _report_decode_warnings(unreadable, unprintable)
    print(f"equal files: {file_equal} / {n_lines}", file=sys.stderr)


if __name__ == "__main__":
    main()
