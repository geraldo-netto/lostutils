# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-val-01 | open | low | deduplicate-by-namev3.py:66 — --workers accepts any int (e.g. -5, 0) with no validation; rapidfuzz only defines -1 (all cores) and positive counts, so out-of-range values pass straight into cdist. Reject workers < -1 with an argparse error. | option validation

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-gov-01 | open | low | import_events.py:601 — download and cache warnings log absolute local cache/model paths, which can leak private home-directory names when users paste logs. Redact to cache-relative paths or basename+digest while keeping enough context for diagnosis. | local path disclosure

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-di-01 | open | low | deduplicate-by-namev3.py:70 — input is decoded with `errors="replace"`, so distinct invalid byte sequences collapse to U+FFFD and can be reported as the same cleaned string. Use surrogateescape or binary-safe decoding so malformed filenames remain distinguishable. | derived state
lq-di-01 | open | med | link_queue.py:1360 — _dispatch_immediate releases _immediate_lock after _ensure_immediate_pool() then does _immediate_q.put_nowait(item) OUTSIDE the lock; a concurrent _resize_immediate_queue_locked (drains, clears, and swaps the queue under the lock) can run in the gap so the item lands in the orphaned old queue no consumer reads — silently lost. put_nowait while holding the lock, or re-read _immediate_q under it. | lost-write under queue swap

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
dnv3-perf-01 | open | med | deduplicate-by-namev3.py:113 — each row block recomputes cdist of its rows against ALL n columns including the already-emitted lower triangle, doubling work; only columns >= start are kept. Pass cleaned_strs[start:] as the column set and offset cols by start. | wasted lower-triangle compute

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-nplus1-01 | open | low | link_queue.py:1084 — _restore_queue_from_state calls _load_immediate_items() and _load_state_items(), each independently calling _read_state_dict() → the entire state file is opened, read, and YAML-parsed TWICE at startup. Read+parse the dict once and pass it to both _parse_state_list calls. | redundant file read+parse

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-conc-01 | open | low | link_queue.py:1380 — _note_immediate_depth samples depth = _immediate_q.qsize() BEFORE taking _immediate_lock, then makes the edge-trigger warn decision under the lock using that pre-lock sample; two dispatchers can sample different depths and interleave so the latched over/under transition disagrees with the true depth. Sample qsize inside the locked region. | edge-trigger sample/decision race

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-mt-01 | open | med | link_queue.py:1262 — _immediate_consumer's run loop wraps _run_immediate_item only in try/finally with no except (unlike _worker_loop which catches and continues); any exception escaping _run_immediate_item kills the consumer thread permanently, silently shrinking the immediate pool until the next _ensure_immediate_pool. Wrap the body in try/except Exception with a log, like the queue worker. | swallowed-future / thread lifecycle

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-dist-01 | open | med | link_queue.py:1267 — _immediate_consumer pulls item via _immediate_q.get() but calls task_done() on a re-read of self._immediate_q; if _resize_immediate_queue_locked swaps the queue in between, task_done() hits the NEW queue (no matching get) → ValueError: task_done() called too many times, killing the consumer. Capture the queue object at get() time and call task_done() on that same object. | shared-resource swap race
rf-dist-01 | open | med | relocate_folder.py:1525 — recover gates on _path_taken(source) then os.rename(backup, source); a concurrent recreation of source as an empty dir between the gate and the rename is silently replaced (Linux rename onto an empty dir succeeds), clobbering it. Document the single-operator assumption or use renameat2(RENAME_NOREPLACE) where available. | TOCTOU gate→rename

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-depend-01 | open | low | dedupl_numpy.py:55-57 — stdout writes have no BrokenPipeError guard, so piping into `head` raises a BrokenPipeError traceback on close. Wrap the write/flush in a BrokenPipeError handler or restore SIGPIPE to default. | common CLI pipe pattern

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---

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
dnv3-rel-02 | open | low | deduplicate-by-namev3.py:70 — readlines() then cleanup() drops every line that cleans to empty silently; the counts dict loses original line numbers so a duplicate report can't point back to source lines. Retain source indices if traceability is needed. |
lq-rel-01 | open | low | link_queue.py:3776 — _on_remove_selected calls _save_state() synchronously on the UI thread (full YAML serialize + tempfile + fsync-rename) while the sibling _on_clear_queue deliberately uses the debounced _request_save_state() to avoid a synchronous UI-thread write; on a large queue a single Remove stalls the GUI. Use _request_save_state() here too. | UI-thread blocking write
rf-rel-03 | open | low | relocate_folder.py:354 — already_migrated declares migration complete when _target_has_content finds a non-empty target dir, without comparing it to the source; a stale/partial prior target (correct symlink, garbage/incomplete contents) is treated as migrated and the copy is skipped, silently stranding/diverging data. At minimum log the skip-because-content-present decision. | content-presence != content-correctness
rf-rel-02 | open | high | relocate_folder.py:1005 — _iter_verify_tasks yields _verify_ownership for EVERY src entry unconditionally, including non-regular files copy_tree deliberately skipped; with --verify-ownership a skipped socket/FIFO/device has no dst counterpart so _verify_ownership's dst lstat raises RuntimeError and the whole migration fails on any tree containing a special file. Gate ownership on the same S_ISLNK/S_ISREG/S_ISDIR classification. | --verify-ownership + any skipped special = guaranteed failure
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge
rdv3-rel-02 | open | low | remove-deduplv3.py:109 — when the same path appears twice under one hash, to_remove keeps both copies and emits `rm -f /a /a`; confirmed on a 3-line input. Dedup paths per group (build to_remove from a set) before quoting. | distinct from line-107 keep-vs-keep tie

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-robust-01 | open | low | import_events.py:482 — _verify_sha256 deletes a mismatched cached model immediately, losing the artifact and forcing a full redownload even for transient/provenance issues. Move it aside as .bad.<digest> and keep diagnostics. | partial-state recovery
rf-robust-04 | open | low | relocate_folder.py:605 — tracking_copy2 adds stat_fn(s).st_size of the SOURCE post-copy for progress; a concurrent writer changing the source between copytree's read and this stat makes done diverge from total (bar exceeds 100% or stalls). Account bytes written to the dst instead. | progress-only
rf-robust-02 | open | med | relocate_folder.py:622/1737 — copy_tree and _copy_and_verify only clean partial targets on `Exception`; Ctrl-C raises `KeyboardInterrupt` and can leave a partially copied target that blocks retry. Handle KeyboardInterrupt/BaseException with best-effort cleanup or a surfaced recovery instruction. | Ctrl-C cleanup
rf-robust-03 | open | med | relocate_folder.py:1300 — _backup_target only rolls back on `Exception`; Ctrl-C/SystemExit while the source is renamed aside bypasses the restore path and can leave `<source>` missing with only `<source>.relocate-backup`. Roll back on BaseException and re-raise, leaving SIGKILL/power-loss to --recover. | Ctrl-C rollback
rdv3-robust-01 | open | low | remove-deduplv3.py:68 — detect_encoding() opens the file BEFORE the OSError-guarded with block (101-112), so a missing/unreadable input throws an uncaught FileNotFoundError traceback + exit 1 instead of the documented `error:` message + exit 2. Wrap the detect_encoding call in the same guard. | docstring promises clean error + exit code

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-state-01 | open | med | relocate_folder.py:1588 — on any failure after ensure_dest_root (verify failure, swap failure, --strict-cross-device raising at 1592), the dest ancestor dirs created by _create_missing_dirs are never cleaned up; only copy_tree/_copy_and_verify clean plan.target itself, not the parent dirs they mkdir'd, leaving empty .relocate dirs on the dest volume. Capture _create_missing_dirs' returned list and unwind it on the failure paths. | every post-ensure_dest_root error path leaks dirs

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-obs-01 | open | low | deduplicate-by-namev3.py:76 — lines whose cleaned form is empty (blank, or only REPLACEMENTS/WORD_TOKENS chars) are dropped with no count or notice, so the user can't tell input was discarded. Track and report a dropped count to stderr. | silent data loss
hr-obs-02 | open | low | hash-recursive-ai5.py:1716 — the hashes.txt dump writes only the stage-1 HEAD digest per file, so two files that share a head but differ past it (correctly split apart by the stage-2 head:tail composite) appear with IDENTICAL digests in the dump; the dump digest is not the dup-grouping identity. Document this, or write the composite digest for stage-2 files. | dump digest != grouping key
lq-obs-01 | open | low | link_queue.py:1269 — _resize_immediate_pool recomputes _immediate_pool_size but does not reset _immediate_depth_warned; after an upward resize the next _note_immediate_depth compares the live depth against the larger pool with a stale "already warned" latch, so a backlog over the old size but under the new never clears its warning until it fully drains. Reset _immediate_depth_warned on grow. | edge-trigger desync
lq-obs-02 | open | low | link_queue.py:3595 — _update_status reads _immediate_q.qsize() and compares to _immediate_pool_size without _immediate_lock, racing a concurrent _resize_immediate_queue_locked swap; under the swap the qsize can be read off the orphaned old queue, so "N immediate waiting" can show a stale/zero depth while the new queue has a backlog. Read depth under the lock. | status reads unsynchronized state
rf-obs-01 | open | low | relocate_folder.py:992 — the except OSError in _iter_verify_tasks swallows the stat error with no log line, so even if rf-rel-01 is fixed to raise, the operator gets no breadcrumb why an entry couldn't be classified. Log a warning with the path and errno. |
rf-obs-02 | open | low | relocate_folder.py:1757 — _copy_and_verify cleans a failed target with shutil.rmtree(..., ignore_errors=True); unlike the copy_tree cleanup path (which records+logs rmtree failures), a failed cleanup here is silently dropped, so a surviving partial target after a verify failure leaves no audit trail despite the log line at 1752 claiming it "has been deleted". Use an onerror recorder and warn on residual entries. | log claims deletion even when rmtree silently failed
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-watch-01 | open | med | import_events.py:1129 — _run_llm has no per-file timeout/stall heartbeat around client.create_chat_completion, so a stuck llama_cpp call can block the whole run until Ctrl-C. Add a monotonic deadline/worker timeout or watchdog progress log. | stall detection. PARTIAL: added _llm_heartbeat watchdog (LLM_HEARTBEAT_SECONDS) emitting a WARNING every interval around _create_chat_completion and stopping in finally, so a stall is visible; true per-file cancellation infeasible — create_chat_completion is a blocking native llama_cpp call that cannot be cancelled from Python without killing the process.

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-time-01 | open | med | link_queue.py:1990 — worker claim deadlines, failure cooldowns, and shutdown joins use time.time(), so wall-clock jumps can make waits expire early or stall. Use time.monotonic() for elapsed deadlines and keep wall time only for display timestamps. | monotonic deadline

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---

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
dnv3-cli-02 | open | low | deduplicate-by-namev3.py:34 — MAX_THRESHOLD/clamp only caps the upper bound; a threshold <= 0 is silently accepted and emit_pairs then walks the full N×N matrix even though exact-collision pairs were already printed. Short-circuit emit_pairs when threshold <= 0. | wasted full-matrix walk
dnv3-cli-01 | open | low | deduplicate-by-namev3.py:64 — --threshold accepts negative values; non-empty inputs then pass a negative score_cutoff into rapidfuzz and crash with OverflowError instead of a CLI validation error. Reject values below 0 before running cdist and cover the edge case. | option validation
rf-cli-01 | open | low | relocate_folder.py:1866 — --jobs/-j accepts 0 and negatives; _resolved_jobs silently maps jobs <= 0 to the default, so `-j 0`/`-j -4` run with the default pool while the user believes concurrency was constrained. Reject non-positive --jobs with a clear parser error. | silent fallback misleads

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-doc-01 | open | low | hash-recursive-ai5.py:607 — the SamplingStrategy base-class docstring still describes the default ThirdsStrategy as "tail-CAP plus two SAMPLE-byte windows at size//3 and 2*size//3", with no mention of the center 4 MiB block ThirdsStrategy now also hashes; the base docstring and the concrete strategy disagree. Update it. | docs-vs-behavior drift

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-i18n-02 | open | low | import_events.py:904 — _read_text hardcodes UTF-8 with `errors="ignore"`, so cp1251, Shift-JIS, Big5, ISO-8859, and other non-UTF-8 text can be silently mangled before language detection and LLM extraction. Add encoding detection or a --text-encoding/strict warning path. | non-UTF8 document handling

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
