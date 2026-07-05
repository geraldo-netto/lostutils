# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefix | file name
--- | ---
bt- | bookmark-tidy.py
dnp- | dedupl_numpy.py
dnv3- | deduplicate-by-namev3.py
hr- | hash-recursive-ai5.py
ie- | import_events.py
lq- | link_queue.py
mkp- | minikeypad.py
oze- | organize_by_extension.py
rf- | relocate_folder.py
rdv3- | remove-deduplv3.py

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-sec-01 | open | low | bookmark-tidy.py:998-1008 — `--auto-install-llama` runs a live `pip install` of a PyPI package into the running interpreter (arbitrary code execution via the package/its build); the pinned version limits but does not remove supply-chain risk. Default to failing with install instructions and document the trust boundary. | STRIDE Elevation of privilege / supply-chain
rf-sec-04 | open | med | relocate_folder.py:1787 — source inode identity is asserted once before the copy, but `atomic_swap` renames `plan.source` by path after a long copy+verify with no re-assert; on a world-writable source parent under sudo the path can be swapped during the copy window (TOCTOU). Re-call `_assert_source_identity(plan.source, src_id)` immediately before `atomic_swap`. | TOCTOU; src_fd pins the inode, not the path binding

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-val-01 | open | med | hash-recursive-ai5.py:1940 — main validates `--block-size >= 1` and `--sample-size >= 1` but not sample-size relative to block-size; stage 2 fires for size > block-size and its windows are `strict=True`, so `--sample-size` larger than the smallest stage-2 file short-reads, ticks `_tick_shrank`, discards the digest, and reports identical files as NON-duplicates (silent false negative). Reject `sample-size >= block-size` at the CLI boundary, or relax strict when the window exceeds file size. | input validation / reliability — CLI knob without a guard, silent loss of true duplicates
rdv3-input-01 | open | med | remove-deduplv3.py:156 — an invalid `--encoding` value (e.g. `bogus`) makes `open(..., encoding=encoding)` raise `LookupError`, which the try at 164-172 does not catch (only OSError/UnicodeDecodeError), so it escapes as a traceback instead of the advertised clean exit. Validate with `codecs.lookup(encoding)` up front or catch `LookupError` → `_fail(...,3)`. | input validation / CLI-option integrity — unvalidated CLI input reaches codec lookup; violates the exit-code contract

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-di-01 | open | low | minikeypad.py:1445 — on a `flash`/`error` outcome the flash-commit ACK is lost but the write may have landed on the write-only (unreadable) device; `_download_done` logs a warning yet never records the key in `_assignments`, so the session map can silently lack a mapping the device holds (Save…/Write all won't replay it) — the inverse of the documented partial-write case, equally unreconciled. Optimistically record ambiguous commits (flagged) or prompt a re-write. | data integrity — in-memory map diverges from device state on lost ACK

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-scal-08 | open | low | organize_by_extension.py:781 — `_find_reusable_bucket` restarts its scan at next_expected=0 and walks every already-full bucket on EVERY choose; as K full buckets accumulate per (ext,prefix) the per-file cost is O(K) → O(K²) over the run. Cache a "first non-full index" cursor per (ext_dir,prefix) for O(1) amortized. | scalability / performance — linear rescan of full buckets on the hot planning path
rdv3-scal-01 | open | low | remove-deduplv3.py:16 — docstring claims "Streams the file; no readlines() into memory," but `_read_groups` accumulates every path into a defaultdict held wholly in RAM → memory is O(total paths), not streaming. Soften the doc or spill for very large inputs. | scalability / documentation drift — reading is streamed but state materialization is not

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-conc-01 | open | low | import_events.py:3657 — `_atomic_write_bytes` names its temp `.{name}.{os.getpid()}.tmp` keyed only on PID, not thread; under `--stage-cache` with 2+ workers processing files whose content+options hash to the SAME cache key, both compute the same tmp path → concurrent `open("wb")` + `os.replace` interleave (one replace moves the tmp out from under the other, which keeps writing into the published cache file then fails its own replace with FileNotFoundError). Add thread id or use `tempfile.mkstemp` in the target dir. | concurrency / robustness — non-unique temp filename across threads; corrupts stage cache for duplicate-content inputs

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-mt-01 | open | low | hash-recursive-ai5.py:921 — in `_run_stage_windowed`, when one `fut.result()` raises, sibling futures already in the completed `done` set (and those still in `inflight`) are never `.result()`-checked, so their exceptions are swallowed while `shutdown(wait=True)` still blocks on them. `batch_fn` only returns None on OSError today, so defense-in-depth. Drain/inspect or cancel remaining futures before re-raising. | multithreading — swallowed-future audit
ie-mt-01 | open | med | import_events.py:2610 — `_get_paddle_ocr` builds the PaddleOCR engine OUTSIDE `_PADDLE_OCR_LOCK` (only prediction is serialized by `_PADDLE_RUN_LOCK`); with 4 default workers two threads both miss the cache for the same (lang,device), both construct the engine, and the loser re-acquires the lock, sees the winner's cached engine, and `return cached` at 2626 — silently dropping its own freshly built engine (hundreds of MB) without `_close_paddle_ocr_engine` → leak. Build under a per-key construction lock, or close the redundant engine before returning cached. | multithreading — double-checked-lock resource leak; heavy OCR engine never freed

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-dep-01 | open | med | bookmark-tidy.py:1034 — after `--auto-install-llama`, `subprocess.check_call(pip install ...)` raising `CalledProcessError` and the follow-up `from llama_cpp import Llama` (:1035) raising `ImportError` are not wrapped in `UserError`, so a failed install crashes with a traceback rather than an actionable message. Wrap both in try/except → `UserError`. | dependability — fallback chain must not amplify into raw failure
dnp-depend-01 | open | low | dedupl_numpy.py:55-57 — stdout writes have no BrokenPipeError guard, so piping into `head` raises a BrokenPipeError traceback on close. Wrap the write/flush in a BrokenPipeError handler or restore SIGPIPE to default. | common CLI pipe pattern

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-cmplx-05 | open | low | hash-recursive-ai5.py:365 — `_WalkIter.__iter__` is CC 11 (radon), the only function still over the ≤10 ceiling; extract the coordinator-thread setup + post-iteration stats-finalisation block (395-431) into a helper. | code complexity — still >10 per radon after the refactor pass
ie-cx-17 | open | med | import_events.py:2581 — `_get_paddle_ocr` is CC 14 per radon (still >10): import/miss/build/double-checked-store branches plus three `_PADDLE_OCR_DISABLED/_MISSING` guards inline. Extract the import+device-resolution and the build-and-cache halves into helpers. | code complexity — radon CC 14 > 10
ie-cx-18 | open | low | import_events.py:3928 — `_run_main` is CC 11 per radon (still >10): arg parse, cache reset, folder-create shortcut, ModelUnavailableError partial-emit branch, dedup, dual output writes, and failure-count exit all inline. Split the ModelUnavailableError partial-emit path and the normal write/emit path into helpers. | code complexity — radon CC 11 > 10

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-dup-01 | open | med | bookmark-tidy.py:1442-1449 — `_run` duplicates the full body of `tidy_bookmarks` (deduplicate → guard → `_assign_categories`); collapse `_run` onto `tidy_bookmarks` to keep one code path. | within-file dup
mkp-dup-05 | open | low | minikeypad.py:1199 — five function-button handlers (`_basic_key` :1199, `_shift_and` :1238, `_multimedia` :1245, `_mouse` :1253, `_unicode_char` :1213) repeat the identical shape `if not self._need_key(): return / if not <mutator>(...): self._dropped(label) / self._refresh_display()`. Add a `_apply_mutation(fn, label)` wrapper. | code duplication — repeated guard+dropped+refresh envelope
mkp-dup-04 | open | low | minikeypad.py:1360 — `_dl_result` (:1360) and `_dl_note` (:1366) each hand-roll the transient status flash `self.after(2500, lambda: self.dl_status.configure(...))` with divergent reset (one resets bg, one only text). Factor a `_flash_status(text, fg, bg)` so both clear identically. | code duplication — divergent copies risk inconsistent reset
mkp-dup-01 | open | med | minikeypad.py:1368 — `_download` (1368-1375) and `_write_all` (1551-1558) duplicate the write-guard prelude (`if self._io_busy: _dl_note("Busy, try again"); if not self.dev.connected: log + _dl_result(False)`). Extract a shared `_precheck_write()` returning ok/reason. | code duplication / SOLID SRP — one guard, one place
mkp-dup-02 | open | med | minikeypad.py:1417 — `_run_download` (1417-1433) and `_run_write_all` (1572-1592) duplicate the I/O-worker scaffolding (`_io_busy=True`, `_set_actions("disabled")`, `rid=self.kp.ReportID`, `worker()` with try/except+`LOG.exception`, spawn daemon thread, marshal `*_done` via `_ui_q`). Extract `_spawn_io_worker(fn, done)`. | code duplication / composition — collapse two near-identical thread launchers
mkp-dup-03 | open | low | minikeypad.py:419 — `shift_and` re-inlines the body of `_general_char_set` (`data[KeyType_Num] |= 1; KEY_Char_Num += 2; data[KeyGroupCharNum] += 1`, 419-421) instead of calling the helper it duplicates (372-375). Replace inline with `self._general_char_set()` then `FunKEY_Char_Num += 1`. | code duplication — single source for the char-set increment

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-arch-01 | open | low | dedupl_numpy.py:16 — single `main()` fuses arg parsing, mmap I/O, vectorized grouping, and stdout emission with no seam; the grouping logic can't be unit-tested without a file + subprocess (feeds dnp-test-01). Extract a pure `group_duplicates(data) -> (paths, counts)` helper. | architecture / decoupling / SOLID — no testability boundary; SRP

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
dnp-robust-02 | open | low | dedupl_numpy.py:21 — `open()` has no error handling, so a missing/unreadable hash-file exits with a raw `FileNotFoundError`/`OSError` traceback instead of the clean `error:` + nonzero exit that remove-deduplv3.py uses. | graceful failure contract
dnp-robust-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(f.fileno(), 0, ...)` on a zero-byte input raises `ValueError: cannot mmap an empty file` (uncaught traceback); guard `os.fstat(f.fileno()).st_size == 0` and return cleanly. | interrupted/empty-input recovery
dnv3-rob-01 | open | med | deduplicate-by-namev3.py:178 — `open(path, ...)` in `_load_cleaned_lines` is unguarded; a missing/unreadable/directory path raises FileNotFoundError/PermissionError/IsADirectoryError as a traceback rather than a clean CLI error + nonzero exit. Wrap the open (or main) in try/except that prints to stderr and exits 1. | robustness/recovery + product engineering — actionable runtime failure
dnv3-rob-02 | open | low | deduplicate-by-namev3.py:215-263 — the streaming `sys.stdout.write` loop has no BrokenPipeError handling; piping into `head`/`less` and quitting early emits an "Exception ignored … BrokenPipeError" traceback at shutdown. Catch BrokenPipeError around the write loop (redirect fd / exit quietly). | robustness + platform — POSIX SIGPIPE/pipe semantics
hr-rel-31 | open | med | hash-recursive-ai5.py:1652 — the SIGINT handler restores `signal.SIG_DFL` on the second Ctrl-C, which terminates the process abruptly rather than raising KeyboardInterrupt, so main's finally / `_finalize_hash_dump` (2093-2098) never runs: the hashes dump is not flushed/closed and the prior handler is not restored — contradicting the documented "second Ctrl-C → KeyboardInterrupt" contract (1642-1644). Restore `signal.default_int_handler` (or the captured previous handler) instead. | robustness/recovery — kill-safety, state-machine cleanup on error path
lq-rob-01 | open | low | link_queue.py:1126 — `Dispatcher._atomic_write_state` (and `ConfigStore._write_config_file:766`) do write-tmp-then-`os.replace` but never fsync the tmp fd before the rename nor fsync the parent dir after; `os.replace` is atomic against concurrent readers, not power loss, so a crash can make the rename durable while data blocks are not → zero-length/truncated `link_queue_state.yaml`, losing the whole persisted queue. `f.flush(); os.fsync(f.fileno())` before `os.replace`, and best-effort fsync the dir fd after (gate/measure the added latency on this 1s-debounced hot path). | robustness/recovery — atomic-write durability gap
rf-robust-07 | open | low | relocate_folder.py:1602 — `_create_symlink` mkdtemp's a `.relocate-stage-*` dir (:1568) whose only cleanup is `_cleanup_staging` in the finally (1601-1602); a hard kill in that window leaks the staging dir into `source.parent`, and nothing (recover/startup sweep) ever removes stale `STAGING_PREFIX` dirs. Sweep orphaned `.relocate-stage-*` dirs on start/recover, or document manual cleanup. | robustness/recovery — orphaned-resource cleanup (finally is not kill-safe)
rf-robust-06 | open | med | relocate_folder.py:1786 — a SIGKILL/power-loss in `_backup_target` AFTER the symlink is created (:1541) but BEFORE `shutil.rmtree(backup)` (:1499) strands the full-size `<source>.relocate-backup` dir on the source volume; the next run's `already_migrated` sees source→target with content and returns `skipped` (1786-1788) while `_orphaned_backup` returns None (because `_path_taken(source)` is now true), so the leftover copy is invisible to both recovery and `_log_failed_hint`, permanently consuming the space the migration existed to free. On the skip path lstat `<source>.relocate-backup` and warn with a manual-cleanup hint when a real dir is still parked. | robustness/recovery — kill-safety window in the non-atomic swap; STRIDE-DoS (disk exhaustion), silent partial-state
rf-robust-08 | open | low | relocate_folder.py:697 — a SIGKILL during `copy_tree` leaves a partially-written `plan.target` (the `except BaseException` rmtree at 743-751 does not run on an uncatchable signal); every subsequent run then dies at `_path_taken(dst)` with a bare `FileExistsError("target already exists")` — no stale-partial vs legit detection, no `--recover` path, no guidance. Make the message name the stale-partial possibility and point at manual removal (or add guarded recovery). | robustness/recovery + design-thinking — loud refusal is safe but the dead-end blocks re-runs
rdv3-robust-01 | open | med | remove-deduplv3.py:146-150 — `_silence_stdout_after_broken_pipe` reassigns `sys.stdout` to `/dev/null` at the Python level but does not `os.dup2` at the fd level; interpreter-shutdown flush of the original stdout can still raise "BrokenPipeError / Exception ignored in: <TextIOWrapper>". Use `os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())`. | robustness/recovery — incomplete broken-pipe handling per CPython SIGPIPE note

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-sm-01 | open | low | minikeypad.py:1419 — `_run_download` (:1419) and `_run_write_all` (:1573) set `_io_busy=True` + disable action buttons before `threading.Thread(...).start()`, with no try/except; if `start()` raises (`RuntimeError: can't start new thread`) the exception unwinds to the Tk dispatcher and `_io_busy` stays True with buttons permanently disabled — no error path resets it. Wrap the spawn so a failed start re-enables actions and clears `_io_busy`. | state machine integrity / robustness — error path leaves stuck terminal state, no cleanup

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
rdv3-test-01 | open | med | remove-deduplv3.py:1 — no unit tests for BOM detection (`detect_encoding`), `_survivor` tiebreak, in-group `dict.fromkeys` dedup, `shlex.quote` command building, or the exit-code contract (2/3). Add focused tests for these critical parse/command-build paths. | test coverage / test-fuzz — command-builder + parser lack the ≥80% focused coverage

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## ruff (lint)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `ruff check *.py` reports no issues across all root files (rescan 2026-07-05)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-pyright-01 | open | low | hash-recursive-ai5.py:627 — `_require_blake3().blake3()` flagged `reportOptionalMemberAccess` ("blake3" is not a known attribute of "None"); pyright can't correlate the `_BLAKE3_IMPORT_ERROR` sentinel with the `blake3` global (`ModuleType | None`). Narrow inside `_require_blake3` (`if blake3 is None: raise ...; return blake3`) so the return is non-Optional — no `# type: ignore`. | pyright (reportOptionalMemberAccess) — prefer narrowing over suppression

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-obs-01 | open | med | bookmark-tidy.py:1502 — `main()` only catches `UserError`; an `OSError` from `write_output`/`_atomic_write_text` (permission denied, disk full), a `sqlite3.Error`, or a `RuntimeError` from llama model load escapes as a raw traceback instead of a clean `error:` line + exit 1. Also catch `OSError`/`sqlite3.Error` at the top level and format like `UserError`. | observability/operability — silent/ugly-failure audit (three-pillars: logs)
bt-obs-02 | open | low | bookmark-tidy.py:867 — duplicate removal is only visible as per-item INFO logs in `_group_mutable_bookmarks`; the run never reports how many duplicates were merged/removed (final log at 1495 shows only bookmarks written), so users can't see the tool's primary effect. Log a summary count of merged duplicates. | observability/operability + UX — Doherty Threshold / feedback (Laws of UX)
dnp-obs-01 | open | low | dedupl_numpy.py:58 — the `equal files: X / N` summary is `print()`ed to stdout, intermixed with the machine-readable duplicate-path list written to `sys.stdout.buffer` (lines 55-57); route the summary to stderr (as remove-deduplv3.py does) so stdout stays a clean path stream. | three-pillars logs; stdout hygiene
lq-obs-20 | open | low | link_queue.py:2310 — a timed-out item is double-counted: `_arm_command_timeout._on_timeout` (line 1802) records `timeouts` while the killed proc returns a nonzero code so `_worker_step` also records `failures`, so `_metrics_summary` shows "1 failed, 1 timed out" for a single event. Count a timeout as timeout-only (skip the failure increment when the timer fired). | three-pillars metrics accuracy
lq-obs-21 | open | low | link_queue.py:2608 — `LogSink._flush_batch` silently discards the batch on any write/flush exception (`_close_locked(); return`) with no user-visible notice, unlike open failures which `_note_open_failure` surfaces; a persistently failing target loops close→reopen→drop forever, losing all file-log lines invisibly. Surface a throttled write-failure warning. | silent-failure audit
lq-obs-22 | open | low | link_queue.py:3607 — `_on_add` folds a `"rejected"` outcome (unknown-protocol `default_command` with shell+bare `{url}`) into `total = sum(counts.values())` but the printed summary only reports queue/immediate/default/duplicate, so `total` and the breakdown disagree and a dropped link is near-invisible. Add rejected to the summary line. | docs-vs-behavior / count mismatch
lq-obs-23 | open | low | link_queue.py:4715 — `_on_queue_rerun` logs "[queue] re-queued {len(items)}" but `_process_link` silently skips items whose URL is still pending/running (returns "duplicate"), so the count overstates what was re-queued. Count non-duplicate outcomes. | metrics accuracy
mkp-obs-01 | open | low | minikeypad.py:93,95,101,105 — `_ensure_pyusb`/`_pip_install` emit progress with `print()` rather than `LOG`, bypassing the configured handler/format/level (logging is configured in `main` before the call). Route through `LOG`. | inconsistent with rest of module
rf-obs-07 | open | med | relocate_folder.py:1033 — `_chown_pair` logs one `could not chown …` warning per entry on `PermissionError`, and `_replicate_ownership` always fans out the full pool regardless of euid; a non-root migration of a tree owned by another user emits one warning per file (log flood). Summarize like the rmtree cleanup (cap at N + suppressed count) and/or skip when `os.geteuid() != 0` can't change owner. | silent-failure/log-flood

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-wd-02 | open | low | bookmark-tidy.py:1006 — `subprocess.check_call([pip install …])` has no timeout; a hung network/pip resolve stalls the process indefinitely. Pass a timeout and surface failure. |
bt-wd-01 | open | med | bookmark-tidy.py:986-995 — `LlamaCategorizer._complete` calls the model with no timeout; a stuck/looping llama.cpp inference hangs the whole run with no stall detection or abort. Add a timeout/watchdog around inference. |
hr-wd-01 | open | low | hash-recursive-ai5.py:1794 — no stall/heartbeat/timeout on walk or hash workers: a thread blocked in kernel scandir/stat/read on a hung mount stops all progress with no automatic abort and no "no progress for Ns" diagnostic. The limit is documented in the parser epilog but never surfaced at runtime. Add a monitor thread emitting a stall warning. | watchdog — liveness/stall detection, documented but unmitigated
lq-wd-01 | open | med | link_queue.py:2025 — `Popen` (both `_spawn_exec_proc` and `_spawn_shell_proc:1988`) starts children without `start_new_session`; `_arm_command_timeout` (:1905) only terminate()/kill()s the direct child, so grandchildren (yt-dlp→ffmpeg, aria2c) keep the inherited stdout pipe open, the reader never sees EOF, and the worker hangs past the timeout. Use `start_new_session=True` and `os.killpg` on timeout. | timeout can't reap the process tree
mkp-wd-01 | open | med | minikeypad.py:1290,1315,225 — probe threads set `_io_busy=True` then call libusb (`usb.core.find`/claim/`still_connected`) with no timeout/watchdog; a stuck libusb call leaves `_io_busy` True forever, silently wedging all writes and polls. Bound the probe with a deadline, force-reset `_io_busy`, and surface the stall. | write path has WRITE_TIMEOUT_MS; connect/enumerate paths don't
oze-watch-01 | open | med | organize_by_extension.py:1837 — `_drain_futures` calls `wait(futures, return_when=FIRST_COMPLETED)` with no timeout, and `_run_moves` (1902-1914) never times out an individual move; a single `os.link`/`shutil.copy2`/`os.replace` hung on a stalled NFS/SMB mount stalls the whole run indefinitely with no heartbeat or abort. Add `wait(..., timeout=)` + stall detection or `fut.result(timeout=)` with a watchdog log. | liveness/stall detection
oze-watch-02 | open | med | organize_by_extension.py:333 — the single-threaded scan opens and reads header bytes of EVERY file (`read_head_bytes` at :333, `_file_contains_pdf` at :468) with no timeout; one file on a stalled NFS/SMB/autofs mount freezes `list_files` before the 10k-file heartbeat (:616) can fire. oze-watch-01 covers only the move stage; the scan-stage sniff is a distinct hang surface. Bound sniff I/O (thread+timeout / os.stat pre-check) or add a per-file stall watchdog. | watchdog — scan-I/O liveness; hung-mount DoS (STRIDE Denial-of-service)
rf-wd-02 | open | low | relocate_folder.py:518 — the pre-flight open-files check reaches a hung mount before rf-wd-01's scope: `find_open_file_holders` calls `source.resolve()` (:518) and iterates `/proc/<pid>/fd` symlinks (:548) whose targets may sit on a stalled mount, and `_check_cross_device`→`os.stat` / `_device_mount_point`→`resolve` (1915/1947) run before any copy, all without timeout. rf-wd-01 covers only copytree/verify/_sha256. Extend the stall/timeout guard to the pre-flight resolve/stat/scan phase. | watchdog — hung-mount stall in the pre-flight phase, distinct from rf-wd-01
rf-wd-01 | open | med | relocate_folder.py:702 — `shutil.copytree`, `verify_copy`, and `_sha256` have no timeout/heartbeat/stall detection; a hung network mount (NFS/CIFS) blocks the migration indefinitely with no progress signal or abort. Add a progress-stall watchdog or per-op timeout for these long-running transfers/scans. | no-stall-detection

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-plat-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(..., prot=mmap.PROT_READ)` uses the POSIX-only `prot=` keyword and `PROT_READ`; on Windows this errors (needs `access=`). Document Linux-only support or branch on `os.name`. | POSIX-only primitive
lq-plat-10 | open | med | link_queue.py:2603 — `LogSink._open_locked` passes `os.O_NOFOLLOW`, which is Unix-only; on Windows accessing `os.O_NOFOLLOW` raises AttributeError, caught by the broad `except Exception`, so the log file silently never opens despite the module advertising Windows support. Guard with `getattr(os, "O_NOFOLLOW", 0)`. | POSIX-only primitive on a cross-OS surface
lq-plat-11 | open | med | link_queue.py:287 — `StateFileLock._pid_is_running` calls `os.kill(pid, 0)` as a liveness probe, but on Windows (the exact platform where `fcntl is None` forces this pidfile branch, 253-266) `os.kill(pid, 0)` calls `TerminateProcess(handle, 0)`, actually killing the target; a second instance reads the first's live pid and terminates it, then still refuses to start. Use `OpenProcess`/`GetExitCodeProcess` (or a `sys.platform`-guarded check) instead. | platform — POSIX-only primitive with destructive Windows semantics; STRIDE Denial-of-service. Distinct from lq-plat-10 (O_NOFOLLOW)

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cache-01 | open | med | import_events.py:1327 — `get_llm` builds the LRU key from `_llm_file_identity` (stat) BEFORE `ensure_models_exist` (:1355) downloads the file; on a lazy first download the key uses the absent-file sentinel `(None,)`, then a later call re-keys on the real (mtime,size) and reloads the multi-GB model a second time. Compute identity after `ensure_models_exist`. | double model load within one run
ie-cache-02 | open | med | import_events.py:2581 — `_get_paddle_ocr` caches engines in `_PADDLE_OCR` keyed by (lang,device) with no size cap/LRU (unlike `_LLM_CACHE`/`_FILE_SHA256_CACHE`); the default 6-language chain leaves 6+ heavy engines resident for the process. Add a cap/eviction. | reset hook exists; cap+invalidation missing
ie-cache-03 | open | low | import_events.py:2687 — `_TESSERACT_PATH_CACHE` has no reset hook (every other cache does) and permanently caches a negative (`None`) result, so tesseract installed after the first miss is never re-detected in-process. Add a reset and/or don't cache the miss. | sticky negative + no invalidation
ie-cache-04 | open | low | import_events.py:3001 — the `llm_text` stage-cache key omits any hash of SYSTEM_PROMPT/USER_PROMPT, so editing a prompt serves stale cached responses unless `STAGE_CACHE_VERSION` is bumped manually. Fold a prompt digest into the key. |

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:36 — `hash_idx = line_starts[:,None] + np.arange(32)` materializes an (n_lines,32) int64 index matrix (n_lines×256 bytes, 8× the gathered uint8 data — the real peak); use `np.arange(32, dtype=np.int32)` (or `as_strided`) to quarter it. | distinct from dnp-perf-01 (byte copy) and dnp-rel-01 (bounds)
ie-mem-02 | open | low | import_events.py:2227 — `_image_messages` reads the whole image file and base64-encodes it in memory with no size bound before the vision call; a multi-GB image is fully materialized. Bound image size like `_read_text` bounds text. |
ie-mem-03 | open | med | import_events.py:2581 — each cached PaddleOCR engine in `_PADDLE_OCR` holds hundreds of MB; a multilingual auto chain materializes all of them concurrently with no ceiling (peak-memory risk). Cap/evict — pairs with ie-cache-02. |
oze-mem-01 | open | med | organize_by_extension.py:958 — `BucketManager._reserved_names` accumulates one entry per moved filename (`.setdefault(bucket_path, set()).add(source.name)`) and is pruned ONLY on `release()` (the skip path); successfully-moved files are never removed, so it grows O(files) for the whole run — defeating oze-scal-02's `_BUCKET_FULL` frozenset memory bound. Drop a bucket's reserved-name set once it collapses to `_BUCKET_FULL` (release() can rebuild from disk), or clear per-source on successful drain. | memory / caching strategy — cache with no success-path invalidation

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-adapt-01 | open | med | dedupl_numpy.py:36 — hash width is hardcoded as `np.arange(32)` + `.view("S32")`, silently assuming 32-char MD5 hex; a SHA-1/SHA-256 hash file is mis-grouped with no error. Derive the width from the first line or expose it as a constant/flag. | magic-number / hardcoded assumption
dnv3-adapt-01 | open | low | deduplicate-by-namev3.py:196 — output rows hardcode a literal `;` in the f-strings (also :263) instead of the `OUTPUT_DELIMITER` constant (:41) used to derive REPLACEMENTS/cleanup stripping; changing the constant would strip the new delimiter during cleanup but keep emitting `;`, desyncing input-cleaning from output format. Use `OUTPUT_DELIMITER` in both f-strings. | adaptability / code duplication — single source of truth for the delimiter
mkp-adapt-01 | open | low | minikeypad.py:1146,1296,1364,1370 — timing knobs are hardcoded inline magic numbers (drain interval 120, connection-poll 1000, status-clear 2500 ×2, window geometry 1440x880/1024x768 at 802-803); hoist to module-level named constants like `WRITE_TIMEOUT_MS`. | magic-number
rf-adapt-01 | open | low | relocate_folder.py:743 — `_DISK_SPACE_HEADROOM` (1.05) and `_SHA256_RETRY_ATTEMPTS` (line 1368, =2) are hardcoded constants with no env/CLI override, unlike the env-tunable `RELOCATE_STALE_PID_WARN_AT`; expose them via env accessors so operators can tune headroom/retries without a code edit. | magic-number

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-cfg-01 | open | med | bookmark-tidy.py:1418 — `--llm-context`, `--llm-max-tokens`, `--llm-gpu-layers`, `--llm-batch-size` accept zero/negative values with no validation; a negative/zero `--llm-context` is passed straight to `Llama(n_ctx=...)` producing an opaque crash. Validate `> 0` (positive-int type) in `parse_args`. | configuration discoverability / input validation — every knob needs validation
lq-cfg-01 | open | low | link_queue.py:336 — `command_timeout_seconds` (the hung-download safety cap) plus `immediate_worker_count`, `immediate_queue_maxsize`, `queue_render_limit`, `seq_of_sweep_gap` have no UI surface; `_SettingsTabs._build_dispatcher` exposes only sleep/workers/cooldown/max-per-domain/output-folder. Surface `command_timeout_seconds` at least. | safety-relevant timeout is hand-edit-YAML-only
rf-cfg-01 | open | low | relocate_folder.py:2120 — `main` hardcodes `logging.basicConfig(level=INFO)` with no `--verbose`/`--quiet`/`-v` flag or env knob; a root migration tool gives operators no way to raise to DEBUG or mute INFO. Add a verbosity flag mapping to log level. | every other runtime knob is tunable

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-api-01 | open | med | organize_by_extension.py:2236,2265 — `BUCKET_SIZE` is a mutable module global reassigned by `main()` via `global`; `organize()` exposes no `bucket_size` param, so library callers can't set it and the mutation is process-global and never restored (leaks across successive `organize()`/test calls in one process). Thread `bucket_size` through `organize()`/`BucketManager`. | config discoverability: --bucket-size has no per-call accessor

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-cli-01 | open | low | dedupl_numpy.py:18 — usage text is printed to stdout (should be stderr) on the error path, and the script hand-rolls arg handling with no `--help`; migrate to argparse for a consistent CLI surface. | error output on wrong stream
ie-cli-01 | open | low | import_events.py:3802 — `--timezone` is not validated at parse time; an invalid IANA name only raises inside `_apply_default_tz` per ICS file, surfacing as per-file failures instead of one upfront error. Validate with `ZoneInfo` in an argparse `type`. |

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-doc-01 | open | med | hash-recursive-ai5.py:13 — module docstring ("Stage 2 hash — for groups with size > 8 MiB") plus hash_tail_and_samples/ThirdsStrategy docstrings (692, 704, 707, 722) claim stage 2 runs only when size > 2*CAP (8 MiB); the actual gate in `_split_stage1_buckets:1369` is size > HEAD_TAIL_THRESHOLD (= CAP = 4 MiB). Docs understate when stage 2 fires by 2x. Correct every reference to "size > CAP". | documentation — docs-vs-behavior drift on the headline algorithm
lq-doc-01 | open | low | link_queue.py:20 — module docstring says legacy `~/.link_queue_config.json` is migrated on first run, but `LEGACY_CONFIG_FILE_NAME="link_queue_config.json"` resolves via `_resolve_state_path` to the XDG/script dir, never `~/.link_queue_config.json`; correct the docstring to the actual location. | docs-vs-behavior drift

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-ux-01 | open | low | deduplicate-by-namev3.py:188-196 — `_emit_self_collisions` interleaves `# source lines: …` comment lines with `a;b;dist` data lines, while cross-pair output carries no comments/provenance; a consumer splitting every line on `;` breaks on the comment lines, and the two record shapes are asymmetric with no documented/`--format` contract. Document the `#` comment convention or make provenance consistent. | UI/UX — Jakob's Law (predictable line format) + API contract

## accessibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-a11y-01 | open | low | minikeypad.py:1165 — the selected physical key is distinguished only by red background (`COL_KEY_SEL`) and mapped keys only by blue background (`COL_KEY_MAPPED`); color-only cues fail for color-blind users. Add a relief/border or text marker. | Laws of UX / WCAG use-of-color

## product engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-i18n-01 | open | low | dedupl_numpy.py:18 — user-facing strings ("Usage:", "equal files:") hardcoded, not routed through a catalog. Acceptable for a dev CLI; flagged for category completeness. | i18n — no translation seam; low value for a single-locale dev tool
dnv3-i18n-01 | open | low | deduplicate-by-namev3.py:58 — user-facing diagnostics ("warning: threshold…", "dropped … empty cleaned line(s)") are hardcoded English with no catalog; route through a message layer if localization is in scope. |
mkp-i18n-01 | open | low | minikeypad.py — all user-facing strings (labels, logs, status) are hardcoded English with no translation catalog; applicable only if localization is a goal. | marginal for single-file util
rdv3-i18n-01 | open | low | remove-deduplv3.py:47 — user-facing strings (banner, `error:`, decode error, summary:) hardcoded, no catalog. Acceptable for a dev CLI; flagged for completeness. | i18n — no translation seam; low value single-locale tool

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data structure

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-unused-01 | open | low | dedupl_numpy.py:43 — `uniq` unpacked from `np.unique` is never used (only `inverse`/`counts` are); bind to `_`. Ruff/pyright don't flag tuple-unpack unused, so it survives lint. |
hr-leg-01 | open | low | hash-recursive-ai5.py:705 — `_make_head_batch`/`_make_tail_batch` and the `_head_batch`/`_tail_batch` module shims (745-746) have no production caller (pipeline uses `_make_head_candidate_batch`/`_make_tail_stage2_batch`); only their own legacy-shim tests at test_hash_recursive.py:1903/1912 exercise them. Decide delete vs keep. | legacy/deprecation — test-only constituency
ie-unused-01 | open | low | import_events.py:2131 — `_decode_event_payload` has no production caller (parse_llm_events uses `_decode_event_payload_or_none`); only tests call it. Delete and point tests at the real function. | grep-proven test-only
ie-unused-02 | open | low | import_events.py:2817 — `_ocr_image_bytes` has no production caller (OCR paths use `_ocr_image_path`/`_ocr_image_path_once`); only tests call it. Delete or wire. | grep-proven test-only
ie-unused-03 | open | low | import_events.py:3157 — `_pdf_to_images` has no production caller (extract_from_pdf renders via `_render_pdf_image_paths`); several tests monkeypatch it expecting to gate PDF OCR, so those guards never fire and give false coverage (e.g. test_extract_from_pdf_auto_skips_ocr_when_text_is_usable). Delete and fix the tests to patch `_render_pdf_image_paths`. | dead + misleading tests
ie-unused-04 | open | low | import_events.py:3188 — `_pdf_ocr_from_file` has no production caller (extract_from_pdf inlines render_once + `_pdf_ocr_text_from_paths`); delete or wire. | grep-proven dead
oze-unf-01 | open | low | organize_by_extension.py:840 — `Bucket.reserve` (and the `Bucket.name` property at 833) have no production caller — `choose` uses `names.add(...)` directly (930) and `bucket.path` downstream; both are exercised only by tests. Decide delete vs wire (`choose` could call `reserve`). | grep-proven test-only
rf-unused-11 | open | low | relocate_folder.py:1030 — `_VERIFY_WORKERS` and `_VERIFY_INFLIGHT` are assigned but never read by live code (verify pool uses `_resolved_jobs`/`_inflight_cap`); only docstring references remain. Delete both. | dead const — delete
rf-wire-01 | open | med | relocate_folder.py:1921 — `_copy_and_verify` calls `copy_tree` with no `progress_cb`, and no CLI flag feeds one, so the entire `tracking_copy2` progress-accounting path (lines 677-700, the `_src_size_totals` apparent-byte walk that feeds it) is unreachable outside tests. Either wire a `--progress` flag through `Plan`/`execute` or drop the dead machinery. | wiring gap — shipped-but-unwired
rf-unused-10 | open | low | relocate_folder.py:797 — `_src_total_bytes` has no live caller (grep-proven; only docstring + test references); `_src_size_totals` superseded it and callers use its `[0]`/`[1]` directly. Delete `_src_total_bytes` (and update the test that pins it). | dead helper — delete
rf-unused-12 | open | low | relocate_folder.py:865 — `_OWNERSHIP_INFLIGHT` is assigned but never read by live code (only the dead `_VERIFY_INFLIGHT` alias + a docstring); the live inflight bound comes from `_inflight_cap(workers)`. Delete it. | dead const — delete

## unused functions/methods

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-unf-01 | open | low | bookmark-tidy.py:146,151,156,160,172,179 — `config_read`, `bookmark_read`, `links_extract`, `links_cleanup`, `filter_youtube`, `get_current_unix_epoch` have no production caller (grep-proven: only tests/fuzz); dead BeautifulSoup-era path superseded by `NetscapeBookmarkParser`. Delete them and their tests. | grep-proven test-only
bt-unused-01 | open | low | bookmark-tidy.py:597 — `detect_bookmark_format` (:597) and `_detect_json_format` (:624) have no in-file caller (only `_detect_bookmark_format_with_data` and `_json_bookmark_format` are used). Delete or wire. | grep-proven unused
bt-unf-02 | open | low | bookmark-tidy.py:901 — `tidy_bookmarks` is called only from tests; `_run` reimplements the same dedup+categorize logic inline, so the real entry point never uses it. Wire `_run` to call it or delete. | test-only symbol (see bt-dup-01)
hr-unf-01 | open | low | hash-recursive-ai5.py:170 — `threaded_walk` has no production caller (main uses `iter_threaded_walk`); grep shows only tests/ invoke it. Keep as documented API or delete with its tests. | grep-proven test-only, same class as hr-leg-01
ie-unused-06 | open | low | import_events.py:565 — `_ocr_language_chain` has no production caller (production uses `_ocr_language_chain_with_source`); only tests (test_import_events.py:1018/1024) call it. Delete and point tests at `_ocr_language_chain_with_source`, or wire. | unused functions/methods — grep-proven test-only wrapper
ie-unused-05 | open | low | import_events.py:590 — `_language_for_ocr` and its sole helper `_language_for_ocr_with_source` (:585) have no production caller (production calls `_ocr_language_chain_with_source` directly); only tests (test_import_events.py:1006/1012) reach it. Delete the pair and point tests at the live chain builder, or wire. | unused functions/methods — grep-proven test-only chain
oze-unf-02 | open | low | organize_by_extension.py:2229 — `parse_args()` has no production caller (main calls `build_parser().parse_args()` directly at :2238); only tests import it. Keep as documented API or inline. | grep-proven test-only

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
