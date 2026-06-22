#!/usr/bin/env python3
"""Vectorized via numpy. mmap the file, find newline positions in one
SIMD pass, build a (n_lines, 32) view of hash bytes, and call np.unique
to do the grouping in C. The only per-line Python work is the final
path extraction (variable-width slicing isn't naturally vectorizable)."""
from __future__ import annotations

import mmap
import sys

import numpy as np

PATH_OFFSET = 26  # preserved from v1's slice index


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <hash-file>")
        sys.exit(1)

    with open(sys.argv[1], "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)

    data = np.frombuffer(mm, dtype=np.uint8)
    nl = np.flatnonzero(data == 0x0A)
    if len(nl) == 0:
        return

    # Line starts: 0, then position after each newline.
    line_starts = np.empty(len(nl), dtype=np.int64)
    line_starts[0] = 0
    line_starts[1:] = nl[:-1] + 1
    n_lines = len(line_starts)

    # Build (n_lines, 32) hash slice via index broadcasting; view as S32.
    hash_idx = line_starts[:, None] + np.arange(32)
    hashes = (
        np.ascontiguousarray(data[hash_idx])
        .view("S32")
        .ravel()
    )

    uniq, inverse, counts = np.unique(
        hashes, return_inverse=True, return_counts=True)
    group_size = counts[inverse]
    dup_line_indices = np.flatnonzero(group_size > 1)

    file_equal = int((counts[counts > 1] - 1).sum())

    # Path extraction is variable-width → list comprehension.
    paths = {
        bytes(data[line_starts[i] + PATH_OFFSET : nl[i]])
        for i in dup_line_indices.tolist()
    }
    out = sys.stdout.buffer
    for p in sorted(paths):
        out.write(p + b"\n")
    print(f"equal files: {file_equal} / {n_lines}")


if __name__ == "__main__":
    main()
