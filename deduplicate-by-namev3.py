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
import io
import re
import sys

import numpy as np
from rapidfuzz.distance import Levenshtein
from rapidfuzz.process import cdist

DEFAULT_THRESHOLD = 7
MAX_THRESHOLD = 254  # uint8 distance matrix caps at 255; stay below to avoid wrap
BLOCK_THRESHOLD = 4000  # N above which the matrix is computed in row blocks
BLOCK_ROWS = 2000  # rows per block when row-blocking (peak BLOCK_ROWS×N bytes)
REPLACEMENTS = (",", "[", "]", ";")  # dnv3-cli-03: ";" is the output field
# delimiter (`{a};{b};{dist}`); strip it from cleaned strings so a value
# containing ";" can't produce rows a downstream ;-split parser mis-reads.
WORD_TOKENS = ("xxx", "monography")
DEFAULT_STRIP_CHARS = "".join(REPLACEMENTS)
DEFAULT_WORD_TOKENS = ",".join(WORD_TOKENS)


def compile_word_re(word_tokens):
    if not word_tokens:
        return None
    return re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, word_tokens)))


_WORD_RE = compile_word_re(WORD_TOKENS)


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


def valid_threshold(value):
    """argparse `type=` validator (dnv3-cli-01): a negative --threshold becomes
    a negative score_cutoff that makes rapidfuzz crash with OverflowError; reject
    it with a clean CLI error instead. Upper bound is handled by clamp_threshold."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"threshold must be an integer, got {value!r}")
    if iv < 0:
        raise argparse.ArgumentTypeError(f"threshold must be >= 0, got {iv}")
    return iv


def parse_word_tokens(value):
    return tuple(token.strip().lower() for token in value.split(",") if token.strip())


def cleanup(entry, replacements=REPLACEMENTS, word_re=_WORD_RE):
    s = entry.strip().lower()
    for tok in replacements:
        s = s.replace(tok, "")
    return word_re.sub("", s) if word_re is not None else s


def configure_stdout():
    if isinstance(sys.stdout, io.TextIOWrapper):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="surrogateescape")
        except ValueError:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="Find near-duplicate strings via batched Levenshtein.")
    ap.add_argument("file", help="text file, one string per line")
    ap.add_argument("-t", "--threshold", type=valid_threshold, default=DEFAULT_THRESHOLD,
                    help=f"max distance to report (default {DEFAULT_THRESHOLD})")
    ap.add_argument("-w", "--workers", type=valid_workers, default=-1,
                    help="cdist worker threads (-1 = all cores)")
    ap.add_argument("--strip-chars", default=DEFAULT_STRIP_CHARS,
                    help=f"characters removed during cleanup (default {DEFAULT_STRIP_CHARS!r})")
    ap.add_argument("--word-tokens", type=parse_word_tokens, default=WORD_TOKENS,
                    help=("comma-separated whole-word tokens removed during cleanup "
                          f"(default {DEFAULT_WORD_TOKENS!r}; empty disables)"))
    args = ap.parse_args()

    # dnv3-di-01: surrogateescape (not "replace") so distinct undecodable byte
    # sequences stay distinguishable instead of all collapsing to U+FFFD and
    # being reported as the same cleaned string. Round-trips losslessly to
    # stdout below once it is reconfigured to match.
    with open(args.file, "r", encoding="utf-8", errors="surrogateescape") as f:
        raw_lines = f.readlines()
    configure_stdout()

    # dnv3-rel-02: keep the 1-based source line numbers behind each cleaned
    # key so a self-collision report can point back at the input lines that
    # produced it (the counts dict alone lost that mapping).
    counts = {}
    line_nums = {}
    dropped_empty = 0
    replacements = tuple(args.strip_chars)
    word_re = compile_word_re(args.word_tokens)
    for lineno, raw in enumerate(raw_lines, 1):
        cleaned = cleanup(raw, replacements, word_re)
        if not cleaned:
            dropped_empty += 1
            continue
        counts[cleaned] = counts.get(cleaned, 0) + 1
        line_nums.setdefault(cleaned, []).append(lineno)
    if dropped_empty:
        print(f"dropped {dropped_empty} empty cleaned line(s)", file=sys.stderr)

    items = list(counts.items())
    n = len(items)
    if n == 0:
        return
    cleaned_strs = [c for c, _ in items]
    threshold = clamp_threshold(args.threshold)

    write = sys.stdout.write

    # Self-collisions (multiple raw lines collapsed to the same cleaned form).
    # cleaned_strs are distinct dict keys, so no two off-diagonal cells are
    # distance 0; emit_pairs keeps only j > i (strict upper triangle), so these
    # i==i reports never overlap with the cross-pair reports.
    for cleaned, lines in line_nums.items():
        if len(lines) > 1:
            src = ",".join(str(x) for x in lines)
            write(f"# source lines: {src}\n")
            write(f"{cleaned};{cleaned};0\n")

    emit_pairs(cleaned_strs, threshold, args.workers, write)


def emit_pairs(cleaned_strs, threshold, workers, write):
    """Emit upper-triangle pairs with distance ≤ threshold.

    cdist clips above-threshold cells to threshold+1 (255 at the 254 cap, no
    uint8 wrap), so the inclusive `<=` mask keeps only genuine within-threshold
    pairs. For large N the matrix is computed in row blocks so peak memory is
    BLOCK_ROWS×N instead of N²; the emitted pairs are identical to the
    single-call path.
    """
    # dnv3-cli-02: with threshold 0 the only matches would be distance-0 cells,
    # but cleaned_strs are distinct dict keys so no off-diagonal cell is 0 — the
    # full N×N walk finds nothing. Skip it (exact self-collisions already emitted).
    if threshold <= 0:
        return
    n = len(cleaned_strs)
    step = n if n <= BLOCK_THRESHOLD else BLOCK_ROWS
    for start in range(0, n, step):
        # dnv3-perf-01: only columns >= start can yield an upper-triangle pair
        # (j > i >= start), so compute against cleaned_strs[start:] instead of
        # all n columns — halves the cdist work and peak block width, identical
        # output. Block column c maps to global index j = start + c.
        block = cdist(
            cleaned_strs[start:start + step], cleaned_strs[start:],
            scorer=Levenshtein.distance,
            score_cutoff=threshold,
            workers=workers,
            dtype=np.uint8,
        )
        mask = block <= threshold
        for r in range(block.shape[0]):
            mask[r, :r + 1] = False  # local: keep only c > r  (j = start+c > i = start+r)
        rows, cols = np.where(mask)
        for r, c in zip(rows.tolist(), cols.tolist()):
            i = start + r
            j = start + c
            write(f"{cleaned_strs[i]};{cleaned_strs[j]};{int(block[r, c])}\n")


if __name__ == "__main__":
    main()
