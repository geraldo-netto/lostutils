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
  * A single N×N call costs N² bytes (uint8 dtype + score_cutoff clips
    above-threshold cells to threshold+1). For N≤30 K (~900 MB) this is
    fine; beyond that the lower triangle is wasted memory, so for large N
    we compute the matrix in row blocks (BLOCK_ROWS×N peak) and emit the
    upper-triangle pairs per block. Output is identical either way.
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
BLOCK_THRESHOLD = 4000  # N above which the matrix is computed in row blocks
BLOCK_ROWS = 2000  # rows per block when row-blocking (peak BLOCK_ROWS×N bytes)
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


def valid_workers(value):
    """argparse `type=` validator (dnv3-val-01): rapidfuzz cdist accepts only
    -1 (all cores) or a positive thread count; reject everything else (e.g.
    -5, 0) with a CLI error instead of passing it straight into cdist."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"workers must be an integer, got {value!r}")
    if iv != -1 and iv < 1:
        raise argparse.ArgumentTypeError(
            f"workers must be -1 (all cores) or a positive count, got {iv}")
    return iv


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
    ap.add_argument("-w", "--workers", type=valid_workers, default=-1,
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

    write = sys.stdout.write

    # Self-collisions (multiple raw lines collapsed to the same cleaned form).
    # cleaned_strs are distinct dict keys, so no two off-diagonal cells are
    # distance 0; emit_pairs keeps only j > i (strict upper triangle), so these
    # i==i reports never overlap with the cross-pair reports.
    for i in range(n):
        if cnts[i] > 1:
            write(f"{cleaned_strs[i]};{cleaned_strs[i]};0\n")

    emit_pairs(cleaned_strs, threshold, args.workers, write)


def emit_pairs(cleaned_strs, threshold, workers, write):
    """Emit upper-triangle pairs with distance ≤ threshold.

    cdist clips above-threshold cells to threshold+1 (255 at the 254 cap, no
    uint8 wrap), so the inclusive `<=` mask keeps only genuine within-threshold
    pairs. For large N the matrix is computed in row blocks so peak memory is
    BLOCK_ROWS×N instead of N²; the emitted pairs are identical to the
    single-call path.
    """
    n = len(cleaned_strs)
    step = n if n <= BLOCK_THRESHOLD else BLOCK_ROWS
    for start in range(0, n, step):
        block = cdist(
            cleaned_strs[start:start + step], cleaned_strs,
            scorer=Levenshtein.distance,
            score_cutoff=threshold,
            workers=workers,
            dtype=np.uint8,
        )
        mask = block <= threshold
        for r in range(block.shape[0]):
            mask[r, :start + r + 1] = False  # keep only j > global row index
        rows, cols = np.where(mask)
        for r, j in zip(rows.tolist(), cols.tolist()):
            i = start + r
            write(f"{cleaned_strs[i]};{cleaned_strs[j]};{int(block[r, j])}\n")


if __name__ == "__main__":
    main()
