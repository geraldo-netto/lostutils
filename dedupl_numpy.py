#!/usr/bin/env python3
"""Vectorized via numpy. mmap the file, find newline positions in one
SIMD pass, build a fixed-width view of hash bytes, and call np.unique
to do the grouping in C. The only per-line Python work is the final
path extraction (variable-width slicing isn't naturally vectorizable)."""
from __future__ import annotations

import argparse
import mmap
import sys

import numpy as np

HASH_SEPARATOR_WIDTH = 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print paths belonging to duplicate hash groups.",
    )
    parser.add_argument("hash_file", help="file containing hash/path records")
    return parser.parse_args()


def group_duplicates(data: np.ndarray) -> tuple[set[bytes], int, int]:
    """Return duplicate paths, redundant-file count, and record count."""
    nl = np.flatnonzero(data == 0x0A)
    if len(nl) == 0:
        return set(), 0, 0

    separators = np.flatnonzero(data[: nl[0]] == 0x20)
    hash_width = int(separators[0])
    path_offset = hash_width + HASH_SEPARATOR_WIDTH

    # Line starts: 0, then position after each newline.
    line_starts = np.empty(len(nl), dtype=np.int64)
    line_starts[0] = 0
    line_starts[1:] = nl[:-1] + 1
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
        bytes(data[line_starts[i] + path_offset : nl[i]])
        for i in dup_line_indices.tolist()
    }
    return paths, file_equal, n_lines


def main() -> None:
    args = _parse_args()

    with open(args.hash_file, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)

    data = np.frombuffer(mm, dtype=np.uint8)
    paths, file_equal, n_lines = group_duplicates(data)
    if n_lines == 0:
        return

    out = sys.stdout.buffer
    for p in sorted(paths):
        out.write(p + b"\n")
    print(f"equal files: {file_equal} / {n_lines}")


if __name__ == "__main__":
    main()
