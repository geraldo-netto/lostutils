#!/usr/bin/env python3
"""
v3: rapidfuzz.process.cdist with multi-threading. Same input/output as
v1/v2; trades a bit of memory for a single vectorized C call instead of
Python-level per-pair iteration.

Wins vs v2:
  * One C call computes the entire N×N distance matrix instead of N²
    Python-to-C transitions. The per-call overhead matters at scale.
  * cdist uses SIMD inside the kernel.
  * `workers=-1` parallelizes across all cores.

Tradeoff:
  * Memory is N² bytes (uint8 dtype + score_cutoff clips above-threshold
    cells to threshold+1). For N≤30 K (~900 MB) this is fine; beyond
    that, prefer v2's length-bucketed iteration.
  * The length pre-filter from v2 isn't needed here — cdist's
    score_cutoff makes its own short-circuit per cell, and the matrix
    walk in numpy is cheap.
"""
from __future__ import annotations

import argparse
import re
import sys

import numpy as np
from rapidfuzz.distance import Levenshtein
from rapidfuzz.process import cdist

DEFAULT_THRESHOLD = 7
MAX_THRESHOLD = 254  # uint8 distance matrix caps at 255; stay below to avoid wrap
REPLACEMENTS = (",", "[", "]")
WORD_TOKENS = ("xxx", "monography")
_WORD_RE = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, WORD_TOKENS)))


def clamp_threshold(threshold):
    if threshold > MAX_THRESHOLD:
        print(
            f"warning: threshold {threshold} exceeds uint8 limit; "
            f"clamping to {MAX_THRESHOLD}",
            file=sys.stderr,
        )
        return MAX_THRESHOLD
    return threshold


def cleanup(entry):
    s = entry.strip().lower()
    for tok in REPLACEMENTS:
        s = s.replace(tok, "")
    return _WORD_RE.sub("", s)


def main():
    ap = argparse.ArgumentParser(
        description="Find near-duplicate strings via batched Levenshtein.")
    ap.add_argument("file", help="text file, one string per line")
    ap.add_argument("-t", "--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"max distance to report (default {DEFAULT_THRESHOLD})")
    ap.add_argument("-w", "--workers", type=int, default=-1,
                    help="cdist worker threads (-1 = all cores)")
    args = ap.parse_args()

    with open(args.file, "r", encoding="utf-8", errors="replace") as f:
        raw_lines = f.readlines()

    counts = {}
    for raw in raw_lines:
        cleaned = cleanup(raw)
        if not cleaned:
            continue
        counts[cleaned] = counts.get(cleaned, 0) + 1

    items = list(counts.items())
    n = len(items)
    if n == 0:
        return
    cleaned_strs = [c for c, _ in items]
    cnts = [c for _, c in items]
    threshold = clamp_threshold(args.threshold)

    matrix = cdist(
        cleaned_strs, cleaned_strs,
        scorer=Levenshtein.distance,
        score_cutoff=threshold,
        workers=args.workers,
        dtype=np.uint8,
    )

    write = sys.stdout.write

    # Self-collisions (multiple raw lines collapsed to the same cleaned form).
    for i in range(n):
        if cnts[i] > 1:
            write(f"{cleaned_strs[i]};{cleaned_strs[i]};0\n")

    # Upper-triangle pairs with distance ≤ threshold.
    mask = np.triu(matrix <= threshold, k=1)
    rows, cols = np.where(mask)
    for i, j in zip(rows.tolist(), cols.tolist()):
        write(f"{cleaned_strs[i]};{cleaned_strs[j]};{int(matrix[i, j])}\n")


if __name__ == "__main__":
    main()
