#!/usr/bin/env python3
"""Vectorized via numpy. mmap the file, find newline positions in one
SIMD pass, build a fixed-width view of hash bytes, and call np.unique
to do the grouping in C. The only per-line Python work is the final
path extraction (variable-width slicing isn't naturally vectorizable)."""
from __future__ import annotations

import argparse
import mmap
import os
import sys

import numpy as np

HASH_SEPARATOR_WIDTH = 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print paths belonging to duplicate hash groups.",
    )
    parser.add_argument("hash_file", help="file containing hash/path records")
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
    with open(path, "rb") as source:
        if os.fstat(source.fileno()).st_size == 0:
            return None
        return mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ)


def group_duplicates(data: np.ndarray) -> tuple[set[bytes], int, int]:
    """Return duplicate paths, redundant-file count, and record count."""
    nl = np.flatnonzero(data == 0x0A)
    if len(data) == 0:
        return set(), 0, 0
    if data[-1] != 0x0A:
        nl = np.append(nl, len(data))

    line_starts, hash_width, path_offset = _record_layout(data, nl)
    n_lines = len(line_starts)

    # Build the hash slice via index broadcasting; view as fixed-width bytes.
    hash_idx = line_starts[:, None] + np.arange(hash_width)
    hashes = (
        np.ascontiguousarray(data[hash_idx])
        .view(f"S{hash_width}")
        .ravel()
    )

    uniq, inverse, counts = np.unique(
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

    out = sys.stdout.buffer
    for p in sorted(paths):
        out.write(p + b"\n")
    print(f"equal files: {file_equal} / {n_lines}")


if __name__ == "__main__":
    main()
