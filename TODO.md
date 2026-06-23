# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-sec-02 | open | low | import_events.py:853 — Tesseract is launched as bare `tesseract` from PATH; argv protects the image path, but a poisoned PATH can execute a different binary when OCR is enabled. Resolve the executable once with shutil.which or a validated --tesseract-path and log the resolved path. | STRIDE-Elevation of privilege
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-gov-01 | open | low | import_events.py:460 — download and cache warnings log absolute local cache/model paths, which can leak private home-directory names when users paste logs. Redact to cache-relative paths or basename+digest while keeping enough context for diagnosis. | local path disclosure

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-di-01 | open | low | import_events.py:1104 — dedupe_events collapses records by (title, start) only, dropping distinct events that share a title/time but differ by end/location/source. Include more fields or emit a conflict report before discarding. | derived state

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
dnv3-perf-01 | open | med | deduplicate-by-namev3.py:113 — each row block recomputes cdist of its rows against ALL n columns including the already-emitted lower triangle, doubling work; only columns >= start are kept. Pass cleaned_strs[start:] as the column set and offset cols by start. | wasted lower-triangle compute
ie-perf-01 | open | low | import_events.py:813 — when PaddleOCR is not installed, _get_paddle_ocr retries the import on every image/PDF page despite warning once. Cache a missing sentinel so large runs avoid repeated import machinery. | optional dependency hot path
ie-perf-02 | open | med | import_events.py:1028 — extract_from_pdf renders and OCRs pages even after _pdf_text found enough text, so text-native PDFs still pay PyMuPDF + Paddle/Tesseract cost. Skip OCR when parsed text is sufficient, or make "always OCR" an explicit flag. | avoid unnecessary OCR

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-scal-02 | open | med | import_events.py:1087 — process_folder sorts the whole iterator before work starts; recursive scans materialize every path and delay the first extraction. Stream with a heap/window or make deterministic ordering opt-in. | large recursive trees
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the streaming docstring. Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-conc-01 | open | med | import_events.py:1122 — atomic replace protects a single writer from partial files but concurrent runs targeting the same output still race and the last writer silently wins. Add an advisory output lock or refuse when a sibling lock exists. | shared output race

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-depend-01 | open | med | import_events.py:1017 — extract_from_pdf calls _pdf_text before image fallback without isolating pypdf/PyMuPDF failures, so one corrupt/encrypted page can skip OCR and vision for the whole file. Catch stage-level exceptions and continue to later stages with a surfaced warning. | graceful degradation

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---

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

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-robust-01 | open | low | import_events.py:341 — _verify_sha256 deletes a mismatched cached model immediately, losing the artifact and forcing a full redownload even for transient/provenance issues. Move it aside as .bad.<digest> and keep diagnostics. | partial-state recovery

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
ie-test-01 | open | med | import_events.py:1017 — tests cover mocked happy-path OCR stages but not corrupt/encrypted PDFs or PyMuPDF render failures; add fixtures that prove PDF extraction degrades from text -> OCR -> vision without losing failure counts. | coverage

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-fuzz-01 | open | med | import_events.py:759 — _merge_text_blocks handles OCR output normalization/dedupe but fuzz coverage does not stress Unicode whitespace, repeated lines, huge OCR blocks, or mixed backend output order. Add property tests for idempotence, max_chars, and stable dedupe. | fuzz/property

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-obs-03 | open | low | import_events.py:640 — malformed LLM JSON warning omits response size/excerpt/hash, making prompt/regression debugging hard without rerunning. Log a bounded sanitized excerpt or persist a debug sidecar behind an opt-in flag. | actionable logs
lq-obs-01 | open | low | link_queue.py:1269 — _resize_immediate_pool recomputes _immediate_pool_size but does not reset _immediate_depth_warned; after an upward resize the next _note_immediate_depth compares the live depth against the larger pool with a stale "already warned" latch, so a backlog over the old size but under the new never clears its warning until it fully drains. Reset _immediate_depth_warned on grow. | edge-trigger desync
oze-obs-01 | open | med | organize_by_extension.py:1457-1465 — _move_worker computes dest = bucket_dir / source.name up front and discards move_file's return value; when move_file renames the source aside to <name>.collisionN the file lands at that name but the worker still logs the original dest, so "Moved X -> <wrong dest>" is reported. Return/log the Path move_file actually returns. | logged dest diverges from reality
rf-obs-01 | open | low | relocate_folder.py:992 — the except OSError in _iter_verify_tasks swallows the stat error with no log line, so even if rf-rel-01 is fixed to raise, the operator gets no breadcrumb why an entry couldn't be classified. Log a warning with the path and errno. |
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-watch-01 | open | med | import_events.py:911 — _run_llm has no per-file timeout/stall heartbeat around client.create_chat_completion, so a stuck llama_cpp call can block the whole run until Ctrl-C. Add a monotonic deadline/worker timeout or watchdog progress log. | stall detection

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-time-01 | open | low | import_events.py:445 — download timeout is per socket operation, not an overall monotonic deadline, so slow trickle responses can run indefinitely while still resetting the timeout. Add total deadline/stall elapsed checks with time.monotonic(). | monotonic deadline

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-plat-01 | open | low | import_events.py:853 — OCR assumes a `tesseract` executable name on PATH and POSIX-like process behavior; Windows/package-manager installs may use a different binary path. Add a configurable executable path and startup validation. | external binary portability

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cache-01 | open | med | import_events.py:284 — _LLM_CACHE has a size cap but no public reset/close hook and cache keys ignore model file identity, so replacing a GGUF at the same path can reuse a stale loaded model. Add reset_llm_cache() and include digest/mtime identity or clear on verification changes. | key shape + invalidation + reset hook

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-mem-02 | open | med | import_events.py:1001 — _pdf_to_images stores rendered page PNG bytes for up to PDF_VISION_MAX_PAGES before OCR/vision, so page images are materialized together. Stream one rendered page at a time through OCR/vision. | streaming

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | open | med | import_events.py:201 — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | public dataclass contract

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-mem-01 | rejected | med | import_events.py:30 — DEFAULT_LLM_CONTEXT_SIZE=65536 raises llama.cpp KV-cache memory and can trigger a train-context warning on 4k-trained GGUF files. | User explicitly requested keeping/increasing 64k context and text budget; eliminating the warning while staying at 64k requires a model trained/extended for that context, not a code-only change.
