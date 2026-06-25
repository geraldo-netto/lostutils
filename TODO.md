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
mkp-rel-01 | open | low | minikeypad.py:1462 — _load_profile does `int(item["layer"])`/`int(item["key_id"])`; a profile with a JSON null (or list) value raises TypeError, which _load_dialog (catches OSError/ValueError/KeyError/JSONDecodeError, NOT TypeError) lets escape and crash the handler. Add TypeError to the caught set or coerce defensively. | uncaught TypeError on malformed profile
rf-sec-10 | open | low | relocate_folder.py:1632 — `_rename_noreplace` calls `libc.renameat2` without setting `restype`/`argtypes`; the 5 args (two int fds, two char*, one unsigned-int flag) rely on ctypes default int marshalling, which can mis-pass pointers/flags on some ABIs. Set `renameat2.restype = ctypes.c_int` and `argtypes = [c_int, c_char_p, c_int, c_char_p, c_uint]`. | ctypes-no-argtypes

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-di-20 | open | med | organize_by_extension.py:1379-1381 — _move_cross_device treats ANY 0-byte target with a non-empty source as a stranded O_EXCL reservation and unlinks it; a user's intentional empty file at that name is silently deleted and overwritten by the move. Restrict reclaim to temp/reservation files this process owns, or skip reclaim when the 0-byte target was not created in this run. | 0-byte file is not proof of a stranded reservation

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
lq-conc-10 | open | med | link_queue.py:935 — `_save_state` snapshots `inflight` (under `_immediate_lock`) and the immediate `backlog` (under `_immediate_q.mutex`) in two separate lock sections; a consumer that `work_q.get()`s then publishes to `_immediate_current` in the gap leaves an in-flight immediate item in neither snapshot, losing it. Snapshot both under a single `_immediate_lock` hold (consumer also takes `_immediate_lock` around get+publish). | torn multi-lock snapshot, narrow data-loss window

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-mt-10 | open | low | link_queue.py:1297 — when `_run_immediate_item` raises, the consumer's `except` (lq-mt-01) logs but records NO metric, so an immediate item that crashes counts as neither failure nor completion (the queue-worker path records a failure on exception). Call `self._record_metric("failures")` in the except so immediate crashes stay visible. | silent metric gap on immediate crash

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
rf-rel-30 | open | low | relocate_folder.py:191 — `Plan.from_args` derives `target = dst_root / src.name`; when `source` has an empty basename (`/` or trailing-slash root) `src.name == ''`, so `target == dst_root`, the `src == target` guard doesn't fire, and the run proceeds on a root-shaped path. Reject empty `src.name` in `from_args` with a typed ValueError. | empty-basename
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge
rdv3-rel-02 | open | low | remove-deduplv3.py:109 — when the same path appears twice under one hash, to_remove keeps both copies and emits `rm -f /a /a`; confirmed on a 3-line input. Dedup paths per group (build to_remove from a set) before quoting. | distinct from line-107 keep-vs-keep tie

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-rel-30 | open | med | hash-recursive-ai5.py:1276 — _split_stage1_buckets builds stage-2 items with `rep[key]` and _stage2_hash hashes only that path with no readable-alias retry, unlike stage-1's _retry_head_alias; if the representative of a multi-alias (hardlinked) inode vanishes or loses read access between stages while a sibling alias stays readable, the tail hash returns None and the inode is silently dropped from its duplicate group. Add the same sibling-alias retry on a None stage-2 tail. | asymmetric recovery / partial-state
lq-rel-10 | open | low | link_queue.py:1111 — `_restore_queue_from_state` re-dispatches persisted immediate items via `_dispatch_immediate` (which silently drops on `queue.Full`) but logs `len(immediate)` as "restored", overstating the count when the restored backlog exceeds `immediate_queue_maxsize`. Have `_dispatch_immediate` return accepted/dropped and log only the accepted count plus a "[warn] N immediate dropped on restore (queue full)" line. | silent drop + miscount on restore
mkp-robust-20 | open | low | minikeypad.py:1439-1442 — _save_profile writes `path + ".tmp"` then os.replace, but on a json.dump/write failure the .tmp file is orphaned (no try/finally cleanup). Wrap write+replace so a failed save unlinks the temp. | orphaned temp on write failure
oze-robust-20 | open | low | organize_by_extension.py:1380-1381 — stranded-reservation reclaim has a TOCTOU between _is_stranded_reservation's stat and os.unlink(target): another process can write real content into the 0-byte target in the window, and the unlink then destroys it (the re-reserve guard only protects the slot, not the unlink). Re-check or open the target with O_EXCL-style semantics atomically before removing. | TOCTOU on reclaim unlink

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
_clean — `ruff check *.py` reports no issues across all root files (rescan 2026-06-25)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `pyright *.py` reports 0 errors / 0 warnings across all root files (rescan 2026-06-25)._

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-obs-01 | open | low | deduplicate-by-namev3.py:76 — lines whose cleaned form is empty (blank, or only REPLACEMENTS/WORD_TOKENS chars) are dropped with no count or notice, so the user can't tell input was discarded. Track and report a dropped count to stderr. | silent data loss
hr-obs-02 | open | low | hash-recursive-ai5.py:1716 — the hashes.txt dump writes only the stage-1 HEAD digest per file, so two files that share a head but differ past it (correctly split apart by the stage-2 head:tail composite) appear with IDENTICAL digests in the dump; the dump digest is not the dup-grouping identity. Document this, or write the composite digest for stage-2 files. | dump digest != grouping key
hr-obs-10 | open | low | hash-recursive-ai5.py:1722 — _on_hashed dumps `aliases.get(key, ())`, but when --alias-cap is active index_inodes caps each inode's alias list at ingest (977), so the dump silently omits every hardlink past the cap with NO `+N more` marker (the emit path adds one). The "dump every hashed file" contract is silently violated for hardlink-heavy inodes. Dump from overflow-aware data or append a truncation marker. | silent-failure audit / incomplete dump under alias cap
lq-obs-01 | open | low | link_queue.py:1269 — _resize_immediate_pool recomputes _immediate_pool_size but does not reset _immediate_depth_warned; after an upward resize the next _note_immediate_depth compares the live depth against the larger pool with a stale "already warned" latch, so a backlog over the old size but under the new never clears its warning until it fully drains. Reset _immediate_depth_warned on grow. | edge-trigger desync
lq-obs-02 | open | low | link_queue.py:3595 — _update_status reads _immediate_q.qsize() and compares to _immediate_pool_size without _immediate_lock, racing a concurrent _resize_immediate_queue_locked swap; under the swap the qsize can be read off the orphaned old queue, so "N immediate waiting" can show a stale/zero depth while the new queue has a backlog. Read depth under the lock. | status reads unsynchronized state
lq-obs-10 | open | low | link_queue.py:4644 — `_shutdown` calls the pre-stop state save BEFORE `stop_event.set()`, so a save failure there routes through `self._log` → `_safe_after` → the Tk job queue (not stderr); with the poller about to be cancelled, that warning is never drained and the failure is invisible. `_save_state`'s stderr fallback only triggers once `stop_event` is set. Set `stop_event` before the pre-stop save, or force the stderr path there. | silent-failure audit: dropped shutdown-save warning
oze-obs-10 | open | low | organize_by_extension.py:1783 — the `progress:` line denominator `total_files` is the scan-stage `len(files)`, but the plan drops files (bucket-selection failure, 1602) and adds `.collision` renames, so `processed X/total` can never converge to the final processed+skipped tally and misleads on long runs. Label it approximate or recompute from the plan tally. | three-pillars / metrics accuracy
rf-obs-01 | open | low | relocate_folder.py:992 — the except OSError in _iter_verify_tasks swallows the stat error with no log line, so even if rf-rel-01 is fixed to raise, the operator gets no breadcrumb why an entry couldn't be classified. Log a warning with the path and errno. |
rf-obs-02 | open | low | relocate_folder.py:1757 — _copy_and_verify cleans a failed target with shutil.rmtree(..., ignore_errors=True); unlike the copy_tree cleanup path (which records+logs rmtree failures), a failed cleanup here is silently dropped, so a surviving partial target after a verify failure leaves no audit trail despite the log line at 1752 claiming it "has been deleted". Use an onerror recorder and warn on residual entries. | log claims deletion even when rmtree silently failed
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit

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
dnv3-cli-02 | open | low | deduplicate-by-namev3.py:34 — MAX_THRESHOLD/clamp only caps the upper bound; a threshold <= 0 is silently accepted and emit_pairs then walks the full N×N matrix even though exact-collision pairs were already printed. Short-circuit emit_pairs when threshold <= 0. | wasted full-matrix walk
dnv3-cli-03 | open | low | deduplicate-by-namev3.py:96 — `cleanup()` strips `,[]` but not `;`, while output uses `;` as the field delimiter (`{a};{b};{dist}`); a cleaned string containing `;` yields rows a downstream `;`-split parser cannot disambiguate. Strip `;` in REPLACEMENTS or quote/escape fields. | output delimiter collision
rf-cli-01 | open | low | relocate_folder.py:1866 — --jobs/-j accepts 0 and negatives; _resolved_jobs silently maps jobs <= 0 to the default, so `-j 0`/`-j -4` run with the default pool while the user believes concurrency was constrained. Reject non-positive --jobs with a clear parser error. | silent fallback misleads

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---

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
