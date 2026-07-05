#!/usr/bin/env python3
"""
remove-dedupl v3 — emit shell `rm` commands to delete content-duplicates
while keeping one survivor. Drop-in for v1/v2 with a few critical fixes:

  * Shell-safe quoting via shlex.quote() — paths with single/double
    quotes, $, backticks, semicolons, newlines, or any other special
    char are escaped properly. v2's `f'"{p}"'` was unsafe (a malicious
    or malformed path with `";rm -rf /;"` in it would be executed).
  * Encoding-aware: auto-detects BOM (UTF-8, UTF-16 LE/BE, UTF-32 LE/BE)
    or accepts --encoding to override. Uses errors='surrogateescape'
    so undecodable bytes survive a round-trip into the output (matches
    what the kernel already does with filename bytes on Linux).
  * Stable tiebreaker (longest basename, then lexicographic path) — the
    same input always nominates the same survivor across runs.
  * Streams the file; no readlines() into memory.
  * Errors go to stderr; exit code reflects success.

Format expected: one record per line, `<hash><whitespace><path>`. The
hash is the first whitespace-separated token; everything after the
first run of whitespace is treated as the path (so paths may contain
spaces).
"""
from __future__ import annotations

import argparse
import io
import os
import shlex
import sys
from collections import defaultdict

# Order matters: UTF-32 BOMs share a 2-byte prefix with UTF-16 BOMs,
# so the 4-byte UTF-32 entries must come first. The mapped codec names
# (utf-16, utf-32, utf-8-sig) are the BOM-stripping variants — picking
# utf-16-le/-be would leave the BOM as a U+FEFF in the first line,
# corrupting the first hash.
BOM_TABLE = (
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\xfe\xff",         "utf-16"),
    (b"\xff\xfe",         "utf-16"),
    (b"\xef\xbb\xbf",     "utf-8-sig"),
)
SAFETY_BANNER = (
    "# WARNING: generated destructive rm -f commands.\n"
    "# Review this file before piping it to sh; this script does not delete files by itself.\n\n"
)


def detect_encoding(path):
    with open(path, "rb") as f:
        head = f.read(4)
    for bom, enc in BOM_TABLE:
        if head.startswith(bom):
            return enc
    return "utf-8"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Emit `rm` commands to clear content-duplicates "
                    "while keeping the entry with the longest basename.",
        epilog="Exit codes: 0 success, 2 input file error, 3 decode error.")
    ap.add_argument("file", help="hash file (one '<hash> <path>' per line)")
    ap.add_argument("--encoding", default=None,
                    help="Force encoding (e.g. utf-8, utf-16, gbk, "
                         "latin-1). Default: BOM-detect → utf-8.")
    ap.add_argument("--strict", action="store_true",
                    help="Fail on undecodable bytes (default: "
                         "surrogateescape).")
    return ap.parse_args(argv)


def _configure_stdout_errors(err_mode):
    # Reconfigure stdout to round-trip surrogateescape bytes losslessly
    # (matches Linux kernel filename semantics).
    if isinstance(sys.stdout, io.TextIOWrapper):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors=err_mode)
            return
        except ValueError:
            pass  # fall back to wrapping the binary buffer below
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout = io.TextIOWrapper(buffer, encoding="utf-8", errors=err_mode)
        return
    _fail("stdout does not expose a binary buffer for utf-8 output", 2)


def _read_groups(path, encoding, err_mode):
    groups = defaultdict(list)
    skipped = 0
    with open(path, "r", encoding=encoding, errors=err_mode) as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            if not line:
                skipped += 1
                continue
            # split(None, 1) consumes any leading whitespace AND the
            # whole gap between hash and path; the path keeps its
            # interior whitespace (tabs, multiple spaces, etc.).
            parts = line.split(None, 1)
            if len(parts) < 2:
                skipped += 1
                continue
            groups[parts[0]].append(parts[1])
    return groups, skipped


def _survivor(paths):
    # max key: (basename_length, path). Computing basename length via
    # rfind avoids building a basename string per call (str.rfind +
    # arithmetic is ~5× faster than os.path.basename).
    return max(paths, key=lambda p: (len(p) - p.rfind("/") - 1, p))


def _emit_remove_commands(groups, out):
    out(SAFETY_BANNER)
    groups_with_dups = 0
    files_to_remove = 0
    for h, paths in groups.items():
        # rdv3-rel-01: collapse byte-identical path strings within a group (a
        # duplicate input line) so the same file can't be picked as survivor AND
        # emitted for removal. dict.fromkeys preserves first-seen order.
        paths = list(dict.fromkeys(paths))
        if len(paths) < 2:
            continue
        keep = _survivor(paths)
        to_remove = [p for p in paths if p != keep]
        if not to_remove:
            continue
        groups_with_dups += 1
        files_to_remove += len(to_remove)
        quoted = " ".join(shlex.quote(p) for p in to_remove)
        out(f"# duplicates: {h}\n# saving: {keep}\nrm -f {quoted}\n\n")
    return groups_with_dups, files_to_remove


def _fail(msg, code):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _silence_stdout_after_broken_pipe():
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        pass


def main(argv=None):
    args = parse_args(argv)
    try:
        encoding = args.encoding or detect_encoding(args.file)
    except OSError as e:
        # Same clean contract as the read loop below: a missing/unreadable
        # input gets `error:` + exit 2, not an uncaught traceback (rdv3-robust-01).
        _fail(e, 2)
    err_mode = "strict" if args.strict else "surrogateescape"
    _configure_stdout_errors(err_mode)

    try:
        groups, skipped_lines = _read_groups(args.file, encoding, err_mode)
    except OSError as e:
        _fail(e, 2)
    except UnicodeDecodeError as e:
        print(f"decode error in {args.file} (encoding={encoding}): {e}\n"
              f"hint: try --encoding <name> or omit --strict",
              file=sys.stderr)
        sys.exit(3)

    try:
        groups_with_dups, files_to_remove = _emit_remove_commands(groups, sys.stdout.write)
        # rdv3-obs-01: audit summary to stderr (groups with all-identical paths or a
        # single survivor are otherwise silently skipped with no trace).
        summary = (
            f"summary: {len(groups)} hash group(s), {groups_with_dups} with "
            f"duplicates, {files_to_remove} file(s) queued for removal"
        )
        if skipped_lines:
            summary += f", {skipped_lines} skipped line(s)"
        print(summary, file=sys.stderr)
    except BrokenPipeError:
        _silence_stdout_after_broken_pipe()
        return


if __name__ == "__main__":
    main()
