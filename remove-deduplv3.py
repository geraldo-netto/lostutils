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
  * Reads the input incrementally, while retaining hash groups in memory
    so survivor selection stays stable and deterministic.
  * Errors go to stderr; exit code reflects success.

Format expected: one record per line, `<hash><whitespace><path>`. The
hash is the first whitespace-separated token; everything after the
first run of whitespace is treated as the path (so paths may contain
spaces).
"""
from __future__ import annotations

import argparse
import codecs
import gettext
import io
import os
import shlex
import sys
from collections import defaultdict
from typing import NoReturn

_ = gettext.gettext

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
SAFETY_BANNER_TEMPLATE = (
    "# WARNING: generated destructive rm -f commands.\n"
    "# Review this file before piping it to sh; this script does not delete files by itself.\n\n"
)
# Per-`rm` argv byte budget. Linux ARG_MAX is typically ~2 MiB but is shared
# with the environment; 128 KiB keeps each emitted command safely below it.
RM_ARGV_BYTE_LIMIT = 128 * 1024


class _InputDecodeError(Exception):
    def __init__(self, encoding, error):
        super().__init__(str(error))
        self.encoding = encoding
        self.error = error


def detect_encoding(head):
    for bom, enc in BOM_TABLE:
        if head.startswith(bom):
            return enc
    return "utf-8"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=_("Emit `rm` commands to clear content-duplicates "
                      "while keeping the entry with the longest basename."),
        epilog=_("Exit codes: 0 success, 2 input file error, 3 decode error."))
    ap.add_argument("file", help=_("hash file (one '<hash> <path>' per line)"))
    ap.add_argument("--encoding", default=None,
                    help=_("Force encoding (e.g. utf-8, utf-16, gbk, "
                           "latin-1). Default: BOM-detect → utf-8."))
    ap.add_argument("--strict", action="store_true",
                    help=_("Fail on undecodable bytes (default: "
                           "surrogateescape)."))
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
    _fail(_("stdout does not expose a binary buffer for utf-8 output"), 2)


def _read_groups(lines):
    groups = defaultdict(list)
    skipped = 0
    for raw in lines:
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


def _load_groups(path, forced_encoding, err_mode):
    with open(path, "rb") as binary:
        encoding = _validate_encoding(
            forced_encoding or detect_encoding(binary.peek(4))
        )
        with io.TextIOWrapper(binary, encoding=encoding, errors=err_mode) as text:
            try:
                groups, skipped = _read_groups(text)
            except UnicodeDecodeError as exc:
                raise _InputDecodeError(encoding, exc) from exc
    return encoding, groups, skipped


def _validate_encoding(encoding):
    try:
        codecs.lookup(encoding)
    except LookupError as e:
        _fail(
            _("unknown encoding {encoding!r}: {error}").format(
                encoding=encoding,
                error=e,
            ),
            3,
        )
    return encoding


def _survivor(paths):
    # max key: (basename_length, path). Computing basename length via
    # rfind avoids building a basename string per call (str.rfind +
    # arithmetic is ~5× faster than os.path.basename).
    seps = (os.sep,) if os.altsep is None else (os.sep, os.altsep)
    return max(paths, key=lambda p: (len(p) - max(p.rfind(sep) for sep in seps) - 1, p))


def _chunked_quoted(paths, limit=RM_ARGV_BYTE_LIMIT):
    # rdv3-scal-01: split a group's removals across several `rm -f` lines so
    # one huge hash group can't exceed ARG_MAX when the output is piped to sh.
    chunk, size = [], 0
    for p in paths:
        q = shlex.quote(p)
        cost = len(q.encode("utf-8", "surrogateescape")) + 1  # +1 separator
        if chunk and size + cost > limit:
            yield " ".join(chunk)
            chunk, size = [], 0
        chunk.append(q)
        size += cost
    if chunk:
        yield " ".join(chunk)


def _emit_remove_commands(groups, out):
    out(_(SAFETY_BANNER_TEMPLATE))
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
        out(_("# duplicates: {hash}\n# saving: {path}\n").format(hash=h, path=keep))
        for quoted in _chunked_quoted(to_remove):
            out(f"rm -f {quoted}\n")
        out("\n")
    return groups_with_dups, files_to_remove


def _format_summary(group_count, groups_with_dups, files_to_remove, skipped_lines):
    summary = _(
        "summary: {group_count} hash group(s), {dup_count} with duplicates, "
        "{remove_count} file(s) queued for removal"
    ).format(
        group_count=group_count, dup_count=groups_with_dups, remove_count=files_to_remove
    )
    if skipped_lines:
        summary += _(", {skipped_count} skipped line(s)").format(
            skipped_count=skipped_lines
        )
    return summary


def _fail(msg, code) -> NoReturn:
    print(_("error: {message}").format(message=msg), file=sys.stderr)
    sys.exit(code)


def _silence_stdout_after_broken_pipe():
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
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


def main(argv=None):
    args = parse_args(argv)
    err_mode = "strict" if args.strict else "surrogateescape"
    _configure_stdout_errors(err_mode)

    try:
        encoding, groups, skipped_lines = _load_groups(
            args.file, args.encoding, err_mode
        )
    except OSError as e:
        _fail(e, 2)
    except _InputDecodeError as e:
        print(
                _("decode error in {file} (encoding={encoding}): {error}\n"
                  "hint: try --encoding <name> or omit --strict").format(
                    file=args.file,
                    encoding=e.encoding,
                    error=e.error,
            ),
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        groups_with_dups, files_to_remove = _emit_remove_commands(groups, sys.stdout.write)
        # rdv3-obs-01: audit summary to stderr (groups with all-identical paths or a
        # single survivor are otherwise silently skipped with no trace).
        print(
            _format_summary(len(groups), groups_with_dups, files_to_remove, skipped_lines),
            file=sys.stderr,
        )
    except BrokenPipeError:
        _silence_stdout_after_broken_pipe()
        return


if __name__ == "__main__":
    main()
