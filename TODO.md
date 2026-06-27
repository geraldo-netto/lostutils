# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `mkp-` minikeypad.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-depend-01 | open | low | dedupl_numpy.py:55-57 — stdout writes have no BrokenPipeError guard, so piping into `head` raises a BrokenPipeError traceback on close. Wrap the write/flush in a BrokenPipeError handler or restore SIGPIPE to default. | common CLI pipe pattern

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cx-02 | open | med | import_events.py:3055 — `_run_file_workers` cyclomatic complexity 23 (radon D), exceeds the ≤10 limit; split thread setup, result collection, and the four except handlers into helpers. | radon cc=23

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-04 | open | low | dedupl_numpy.py:25 — a file whose last line lacks a trailing newline drops that final record (no 0x0A, so line_starts/n_lines never include it); confirmed 0/1 on a 2-duplicate file. Append a virtual line start at EOF when data[-1] != 0x0A. | opposite of the line-36 overrun case
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## ruff (lint)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `ruff check *.py` reports no issues across all root files (rescan 2026-06-27)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `pyright *.py` reports 0 errors / 0 warnings across all root files (rescan 2026-06-27)._

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-obs-01 | open | low | deduplicate-by-namev3.py:76 — lines whose cleaned form is empty (blank, or only REPLACEMENTS/WORD_TOKENS chars) are dropped with no count or notice, so the user can't tell input was discarded. Track and report a dropped count to stderr. | silent data loss

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-plat-01 | open | low | deduplicate-by-namev3.py:93 — `sys.stdout.reconfigure(errors="surrogateescape")` sets errors only, leaving stdout's encoding at the locale default; under a non-UTF-8 locale (LC_ALL=C/ascii) a legitimately-decoded non-ASCII cleaned string raises UnicodeEncodeError on write. Reconfigure with `encoding="utf-8", errors="surrogateescape"`. | stdout encoding not forced
hr-plat-04 | open | low | hash-recursive-ai5.py:563 — `_hash_file_windows` references `os.O_NOFOLLOW | os.O_CLOEXEC` unconditionally; both attributes are absent on Windows (AttributeError at hash time) and there is no platform guard or documented POSIX-only contract. Guard with `getattr(os, "O_NOFOLLOW", 0)` / `getattr(os, "O_CLOEXEC", 0)` or document the POSIX-only requirement. | cross-OS portability / POSIX-only primitive
lq-plat-10 | open | med | link_queue.py:2603 — `LogSink._open_locked` passes `os.O_NOFOLLOW`, which is Unix-only; on Windows accessing `os.O_NOFOLLOW` raises AttributeError, caught by the broad `except Exception`, so the log file silently never opens despite the module advertising Windows support. Guard with `getattr(os, "O_NOFOLLOW", 0)`. | POSIX-only primitive on a cross-OS surface
rdv3-plat-01 | open | low | remove-deduplv3.py:81 — `sys.stdout.reconfigure(errors=err_mode)` sets errors only; under a non-UTF-8 locale a legitimately-decoded non-ASCII path crashes with UnicodeEncodeError when the rm line is written. Also force `encoding="utf-8"` on the reconfigure. | stdout encoding not forced

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-config-01 | open | low | deduplicate-by-namev3.py:33-39 — DEFAULT_THRESHOLD, BLOCK_THRESHOLD, BLOCK_ROWS, REPLACEMENTS, and WORD_TOKENS are module constants with no env/CLI override; WORD_TOKENS ("xxx","monography") is a hardcoded domain assumption that needs a code edit to change. Expose the token/replacement lists via flags or document them as fixed invariants. | hardcoded domain assumption

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-doc-01 | open | low | link_queue.py:19 — module docstring says config is persisted to `~/.link_queue_config.json`, but the current runtime uses `link_queue_config.yaml` under `$XDG_CONFIG_HOME/link_queue` or `~/.config/link_queue`, with script-dir reuse for pre-existing files and legacy JSON migration. | README now documents the current runtime path; update the in-code docstring separately.

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-unused-10 | open | low | relocate_folder.py:797 — `_src_total_bytes` has no live caller (grep-proven; only docstring + test references); `_src_size_totals` superseded it and callers use its `[0]`/`[1]` directly. Delete `_src_total_bytes` (and update the test that pins it). | dead helper — delete
rf-unused-12 | open | low | relocate_folder.py:865 — `_OWNERSHIP_INFLIGHT` is assigned but never read by live code (only the dead `_VERIFY_INFLIGHT` alias + a docstring); the live inflight bound comes from `_inflight_cap(workers)`. Delete it. | dead const — delete
rf-unused-11 | open | low | relocate_folder.py:1030 — `_VERIFY_WORKERS` and `_VERIFY_INFLIGHT` are assigned but never read by live code (verify pool uses `_resolved_jobs`/`_inflight_cap`); only docstring references remain. Delete both. | dead const — delete

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
