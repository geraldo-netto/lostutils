# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-sec-01 | open | high | import_events.py:85 — urlretrieve downloads ~4GB model over HTTPS but MODEL_SHA256/CLIP_SHA256 default to None, so _verify_sha256 only warns and skips; a compromised mirror's GGUF is loaded and executed by llama_cpp. Ship pinned digests by default rather than None. | supply-chain
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
dnv3-perf-01 | open | med | deduplicate-by-namev3.py:113 — each row block recomputes cdist of its rows against ALL n columns including the already-emitted lower triangle, doubling work; only columns >= start are kept. Pass cleaned_strs[start:] as the column set and offset cols by start. | wasted lower-triangle compute

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-scal-01 | open | low | import_events.py:81 — _LLM_CACHE keeps every loaded Llama instance for the process lifetime keyed by (model_path, clip_path); multiple distinct model configs in one run accumulate multi-GB models in RAM with no eviction. Cap cache size or evict on config change. |
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the streaming docstring. Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-dup-01 | open | low | import_events.py:324 — the `global _extraction_failures; _extraction_failures += 1; logger.exception(...)` block is duplicated verbatim in _run_llm and extract_from_file. Extract a single in-file _record_failure(file, exc) helper. | within-file

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-arch-01 | open | low | organize_by_extension.py:1929 — _parse_extra_zip_family normalises items but does not apply EXTENSION_ALIASES, so `--extra-zip-family jpeg` (or any aliased synonym) is stored un-canonicalised while resolve_real_extension compares against declared_canon, so the synonym silently never matches. Map each parsed item through EXTENSION_ALIASES. |

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
dnv3-rel-02 | open | low | deduplicate-by-namev3.py:70 — readlines() then cleanup() drops every line that cleans to empty silently; the counts dict loses original line numbers so a duplicate report can't point back to source lines. Retain source indices if traceability is needed. |
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-obs-01 | open | low | hash-recursive-ai5.py:1055-1060 — stage-2 benign skips are counted in three overlapping summary fields with no reconciliation: a None stage-2 tail bumps stage2_errors, stage2_skipped, AND hash_skipped_vanished/shrank; the summary subtracts vanished+shrank from hash_errors but hashed_stage2_skipped still includes them. Document the overlap or split stage2_skipped into real-vs-benign. |
ie-obs-01 | open | low | import_events.py:128 — urlretrieve gives no progress/error context on a ~4GB download; a network failure mid-download leaves a truncated file that _verify_sha256 only catches if a digest is pinned. Download to a temp file, verify, then atomically rename; log byte counts. |
lq-obs-01 | open | low | link_queue.py:1269 — _resize_immediate_pool recomputes _immediate_pool_size but does not reset _immediate_depth_warned; after an upward resize the next _note_immediate_depth compares the live depth against the larger pool with a stale "already warned" latch, so a backlog over the old size but under the new never clears its warning until it fully drains. Reset _immediate_depth_warned on grow. | edge-trigger desync
oze-obs-01 | open | med | organize_by_extension.py:1457-1465 — _move_worker computes dest = bucket_dir / source.name up front and discards move_file's return value; when move_file renames the source aside to <name>.collisionN the file lands at that name but the worker still logs the original dest, so "Moved X -> <wrong dest>" is reported. Return/log the Path move_file actually returns. | logged dest diverges from reality
rf-obs-01 | open | low | relocate_folder.py:992 — the except OSError in _iter_verify_tasks swallows the stat error with no log line, so even if rf-rel-01 is fixed to raise, the operator gets no breadcrumb why an entry couldn't be classified. Log a warning with the path and errno. |
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit
