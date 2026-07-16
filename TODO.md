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
ie-dist-01 | open | med | import_events.py:3717 — `_output_lock` creates `<output>.lock` with O_CREAT|O_EXCL and writes pid, but a hard kill (SIGKILL) skips the finally-unlink and the written pid is never read back; the next run raises FileExistsError forever until manual removal. On FileExistsError read the pid and reclaim the lock when that process is dead. | distributed systems — idempotent re-run / stale-lock recovery

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
bt-dup-02 | open | med | bookmark-tidy.py:1199-1207,1277-1285 — `_ensure_chrome_folder` and `_ensure_firefox_folder` are byte-identical except for calling `_child_chrome_folder` vs `_child_firefox_folder`; collapse to one helper parametrized by the child-finder callable. | within-file code duplication
bt-dup-01 | open | med | bookmark-tidy.py:1442-1449 — `_run` duplicates the full body of `tidy_bookmarks` (deduplicate → guard → `_assign_categories`); collapse `_run` onto `tidy_bookmarks` to keep one code path. | within-file dup
ie-dup-01 | open | low | import_events.py:2231 — the prompt preamble f"{USER_PROMPT}\n\n{_language_instruction(language)}\n\n{_event_policy_instruction(config)}" is hand-assembled identically in `_text_messages` (2230-2237) and `_image_messages_from_bytes` (2256-2258); a change to the ordering/spacing must be edited in two places. Extract a `_prompt_preamble(language, config)` helper. | code duplication (within-file)
mkp-dup-05 | open | low | minikeypad.py:1199 — five function-button handlers (`_basic_key` :1199, `_shift_and` :1238, `_multimedia` :1245, `_mouse` :1253, `_unicode_char` :1213) repeat the identical shape `if not self._need_key(): return / if not <mutator>(...): self._dropped(label) / self._refresh_display()`. Add a `_apply_mutation(fn, label)` wrapper. | code duplication — repeated guard+dropped+refresh envelope
mkp-dup-06 | open | low | minikeypad.py:1349 — `_connect_done` and `_probe_done` (:1327) repeat the identical token-guard + token-bump + `_io_busy=False` + `_update_state()` envelope. Extract a shared `_probe_settled(token)` helper both call. | within-file duplication (DRY/SOLID)
mkp-dup-04 | open | low | minikeypad.py:1360 — `_dl_result` (:1360) and `_dl_note` (:1366) each hand-roll the transient status flash `self.after(2500, lambda: self.dl_status.configure(...))` with divergent reset (one resets bg, one only text). Factor a `_flash_status(text, fg, bg)` so both clear identically. | code duplication — divergent copies risk inconsistent reset
mkp-dup-01 | open | med | minikeypad.py:1368 — `_download` (1368-1375) and `_write_all` (1551-1558) duplicate the write-guard prelude (`if self._io_busy: _dl_note("Busy, try again"); if not self.dev.connected: log + _dl_result(False)`). Extract a shared `_precheck_write()` returning ok/reason. | code duplication / SOLID SRP — one guard, one place
mkp-dup-02 | open | med | minikeypad.py:1417 — `_run_download` (1417-1433) and `_run_write_all` (1572-1592) duplicate the I/O-worker scaffolding (`_io_busy=True`, `_set_actions("disabled")`, `rid=self.kp.ReportID`, `worker()` with try/except+`LOG.exception`, spawn daemon thread, marshal `*_done` via `_ui_q`). Extract `_spawn_io_worker(fn, done)`. | code duplication / composition — collapse two near-identical thread launchers
mkp-dup-03 | open | low | minikeypad.py:419 — `shift_and` re-inlines the body of `_general_char_set` (`data[KeyType_Num] |= 1; KEY_Char_Num += 2; data[KeyGroupCharNum] += 1`, 419-421) instead of calling the helper it duplicates (372-375). Replace inline with `self._general_char_set()` then `FunKEY_Char_Num += 1`. | code duplication — single source for the char-set increment

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-arch-01 | open | low | dedupl_numpy.py:16 — single `main()` fuses arg parsing, mmap I/O, vectorized grouping, and stdout emission with no seam; the grouping logic can't be unit-tested without a file + subprocess (feeds dnp-test-01). Extract a pure `group_duplicates(data) -> (paths, counts)` helper. | architecture / decoupling / SOLID — no testability boundary; SRP

## decoupling

id | status | effort | description | notes
--- | --- | --- | --- | ---

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-04 | open | low | dedupl_numpy.py:25 — a file whose last line lacks a trailing newline drops that final record (no 0x0A, so line_starts/n_lines never include it); confirmed 0/1 on a 2-duplicate file. Append a virtual line start at EOF when data[-1] != 0x0A. | opposite of the line-36 overrun case
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
dnp-rel-05 | open | low | dedupl_numpy.py:52 — path slice `data[line_starts[i]+PATH_OFFSET : nl[i]]` ends at the `\n` position, so on CRLF input the preceding `\r` is included in every extracted path; strip a trailing 0x0D (e.g. end at `nl[i] - (data[nl[i]-1]==0x0D)`). | reliability/platform — CRLF portability; sibling scripts rstrip("\r\n"), this one does not

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-robust-02 | open | low | dedupl_numpy.py:21 — `open()` has no error handling, so a missing/unreadable hash-file exits with a raw `FileNotFoundError`/`OSError` traceback instead of the clean `error:` + nonzero exit that remove-deduplv3.py uses. | graceful failure contract
dnp-robust-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(f.fileno(), 0, ...)` on a zero-byte input raises `ValueError: cannot mmap an empty file` (uncaught traceback); guard `os.fstat(f.fileno()).st_size == 0` and return cleanly. | interrupted/empty-input recovery
dnv3-rob-03 | open | low | deduplicate-by-namev3.py:137-143 — `configure_stdout()` silently no-ops when `reconfigure` raises ValueError (`except ValueError: pass`) or stdout is not a TextIOWrapper, so the surrogateescape encoder is never applied and a later `sys.stdout.write` of surrogate-escaped bytes raises UnicodeEncodeError; fall back to wrapping the binary buffer or fail loudly. | robustness / silent-failure audit — breaks documented lossless round-trip
dnv3-rob-01 | open | med | deduplicate-by-namev3.py:178 — `open(path, ...)` in `_load_cleaned_lines` is unguarded; a missing/unreadable/directory path raises FileNotFoundError/PermissionError/IsADirectoryError as a traceback rather than a clean CLI error + nonzero exit. Wrap the open (or main) in try/except that prints to stderr and exits 1. | robustness/recovery + product engineering — actionable runtime failure
dnv3-rob-02 | open | low | deduplicate-by-namev3.py:215-263 — the streaming `sys.stdout.write` loop has no BrokenPipeError handling; piping into `head`/`less` and quitting early emits an "Exception ignored … BrokenPipeError" traceback at shutdown. Catch BrokenPipeError around the write loop (redirect fd / exit quietly). | robustness + platform — POSIX SIGPIPE/pipe semantics
rdv3-rob-01 | open | med | remove-deduplv3.py:57-63,100 — `detect_encoding` opens the file and consumes the first 4 bytes, then `_read_groups` re-opens it; for a non-seekable input (named pipe/FIFO/process substitution) those header bytes are lost and the first record is corrupted, and for regular files it is a redundant double-open/TOCTOU. Read the head from the same handle or pass the peeked bytes forward. | robustness — non-seekable byte-loss + TOCTOU

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-02 | open | med | dedupl_numpy.py:1-62 — no `tests/test_dedupl_numpy.py` exists (grep of tests/ returns zero references); the entire module — np.unique grouping, path extraction, summary, no-newline/CRLF/short-line handling — is uncovered, not just the bounds fixture. Add a focused test module. | test coverage — ≥80% CI gate; distinct from dnp-test-01 (whole-module zero coverage)
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
hr-test-01 | open | low | hash-recursive-ai5.py:1802 — `HashDumpWriter.write_overflow` (production-wired via `_finalize_hash_dump:2008`, emits the `+N more (alias-cap)` markers into the dump) has zero test coverage — grep of tests/ finds no reference — leaving the ingest-elision dump path unverified. Add a focused test that caps aliases, runs with `--hashes-file`, and asserts the overflow marker lines. | test coverage — critical I/O path uncovered (AGENTS.md ≥80% gate); cross-ref hr-obs-10

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## ruff (lint)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `uvx ruff check *.py` reports no issues across all root files (rescan 2026-07-16)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_Remaining pyright diagnostics (rescan 2026-07-16) are `reportMissingImports`/`reportMissingModuleSource` for optional third-party deps absent in the scan env (llama_cpp, blake3, rapidfuzz, paddleocr, paddle, charset_normalizer, pypdf, fitz, icalendar/yaml stubs) — all behind guarded imports; environmental, not code findings._

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-obs-01 | open | low | dedupl_numpy.py:58 — the `equal files: X / N` summary is `print()`ed to stdout, intermixed with the machine-readable duplicate-path list written to `sys.stdout.buffer` (lines 55-57); route the summary to stderr (as remove-deduplv3.py does) so stdout stays a clean path stream. | three-pillars logs; stdout hygiene
hr-obs-11 | open | med | hash-recursive-ai5.py:1315 — when a stage-1 head digest is None but recovered from a readable hardlink sibling via `_retry_head_alias` (1315-1316), the original None was already tallied into `stage1_errors` by `_collect_batch:867` and is never decremented, so the summary's `hash_errors` (1964-1969) over-reports real failures by the count of successfully-recovered inodes (identical defect for stage-2 tail retries at 1362-1363); decrement the stage error count (or track a `recovered` counter subtracted alongside vanished/shrank) when a retry yields a non-None digest. | observability — three-pillars metric accuracy / silent mis-metric; cross-ref summary :1955
ie-obs-01 | open | med | import_events.py:3188 — `_safe_pdf_stage` catches every Exception, logs a WARNING, and returns the fallback without calling `_record_extraction_failure`; a PDF whose pdf_text, render, and OCR stages all fail therefore yields "0 events" and process exit 0, masking total failure as clean success. Increment `_extraction_failures` (or re-raise into the extract_from_file handler) so the run exits non-zero. | observability; silent-failure audit — diverges from the `_extraction_failures` contract at :851/:4069
lq-obs-20 | open | low | link_queue.py:1452 — `_process_link` discards the `bool` returned by `_dispatch_immediate` (which returns False and drops the item when the bounded immediate queue is full), so under backpressure the item is lost yet the Add-All summary at :3927 still counts it as an accepted "immediate". Return a distinct outcome (e.g. "dropped") when `_dispatch_immediate` is False and tally it. | silent-failure audit (three pillars: logs vs. metrics disagree); cross-ref lq-rel-10 at :1351-1360
lq-obs-21 | open | low | link_queue.py:1580 — the immediate consumer's `finally` clears its in-flight slot but never calls `_note_immediate_depth()`/`_update_status()`, so as the immediate backlog drains the "N immediate waiting" status fragment (`_update_status` :4076) and the `_immediate_depth_warned` edge latch (:1734) stay stale until the next `_dispatch_immediate`; when queue workers are idle nothing else repaints. Nudge `_note_immediate_depth()` (or `_update_status`) after each item in the consumer's finally. | three-pillars/health — stale operator-visible state with no refresh trigger
oze-obs-11 | open | med | organize_by_extension.py:504 — `is_bucketed_file` (L606) calls `resolve_real_extension` for every scanned file, so an already-correctly-placed misnamed file (e.g. pdf/p00000/mypdf.doc) re-emits the WARNING "header mismatch ... bucketing under pdf/" on every run even though nothing will move. Add a quiet/no-log mode to `resolve_real_extension` for the membership check (or suppress the warning when the resolved ext already equals current placement) so scans don't spam false move warnings. | observability silent-failure/noise audit — misleading log on a read-only path; xref :606,:684

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-watch-01 | open | med | bookmark-tidy.py:1045-1059 — `_call_with_timeout` starts a daemon worker (`threading.Thread(..., daemon=True)`) and on `queue.Empty` raises a timeout, but the underlying llama.cpp inference keeps running uncancelled in the abandoned thread, leaking CPU/GPU until process exit; repeated batch timeouts stack orphaned threads. Add real abort semantics or process-level isolation so a stalled inference is actually stopped. | watchdog — automatic abort/recovery on stall
lq-watch-20 | open | med | link_queue.py:434 — the only per-item liveness guard is `command_timeout_seconds`, which ships default 0 (off), and even when enabled it is a total wall-clock cap (`_stream_and_wait` :2269 / `_arm_command_timeout` :2052) with no progress-stall detection; out of the box a single hung yt-dlp/aria2c that stops producing output pins a worker forever with no abort. Add a no-output-for-N-seconds stall watchdog on the streamed pipe independent of total wall-time, and/or ship a sane non-zero default. | watchdog — heartbeat / progress-stall detection + automatic abort
mkp-watch-02 | open | med | minikeypad.py:1311 — `_probe_timeout` declares the probe "stalled", bumps the token and clears `_io_busy`, but never aborts the still-running daemon worker; a genuinely hung libusb call leaves that thread blocked on the device `_lock` while `_poll_connection` re-arms every 1s and spawns fresh probe workers that also block on `_lock` — unbounded thread pileup with no cap or recovery. Track the in-flight worker/token and refuse to spawn a new probe until the prior one finishes, and surface a persistent "unresponsive" state. | watchdog — timeout without abort/recovery semantics; multithreading
oze-watch-01 | open | high | organize_by_extension.py:2002 — `_drain_futures` (and `_ScanStallMonitor._maybe_warn`:448) only LOG a stall every wait_timeout; a genuinely hung worker (an os.link/copy on a wedged NFS/SMB mount that never returns) blocks the entire run indefinitely with periodic warnings and no abort. Add a max-stall deadline that abandons/fails the stuck move (or aborts the run non-zero) so liveness detection has recovery teeth, not just detection. | watchdog — stall detected but no automatic abort/recovery semantics

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-plat-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(..., prot=mmap.PROT_READ)` uses the POSIX-only `prot=` keyword and `PROT_READ`; on Windows this errors (needs `access=`). Document Linux-only support or branch on `os.name`. | POSIX-only primitive
lq-plat-10 | open | med | link_queue.py:2603 — `LogSink._open_locked` passes `os.O_NOFOLLOW`, which is Unix-only; on Windows accessing `os.O_NOFOLLOW` raises AttributeError, caught by the broad `except Exception`, so the log file silently never opens despite the module advertising Windows support. Guard with `getattr(os, "O_NOFOLLOW", 0)`. | POSIX-only primitive on a cross-OS surface

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:36 — `hash_idx = line_starts[:,None] + np.arange(32)` materializes an (n_lines,32) int64 index matrix (n_lines×256 bytes, 8× the gathered uint8 data — the real peak); use `np.arange(32, dtype=np.int32)` (or `as_strided`) to quarter it. | distinct from dnp-perf-01 (byte copy) and dnp-rel-01 (bounds)
ie-mem-02 | open | low | import_events.py:2227 — `_image_messages` reads the whole image file and base64-encodes it in memory with no size bound before the vision call; a multi-GB image is fully materialized. Bound image size like `_read_text` bounds text. |
ie-mem-03 | open | med | import_events.py:2581 — each cached PaddleOCR engine in `_PADDLE_OCR` holds hundreds of MB; a multilingual auto chain materializes all of them concurrently with no ceiling (peak-memory risk). Cap/evict — pairs with ie-cache-02. |
oze-mem-01 | open | med | organize_by_extension.py:958 — `BucketManager._reserved_names` accumulates one entry per moved filename (`.setdefault(bucket_path, set()).add(source.name)`) and is pruned ONLY on `release()` (the skip path); successfully-moved files are never removed, so it grows O(files) for the whole run — defeating oze-scal-02's `_BUCKET_FULL` frozenset memory bound. Drop a bucket's reserved-name set once it collapses to `_BUCKET_FULL` (release() can rebuild from disk), or clear per-source on successful drain. | memory / caching strategy — cache with no success-path invalidation

## data structure

id | status | effort | description | notes
--- | --- | --- | --- | ---

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-adapt-01 | open | med | dedupl_numpy.py:36 — hash width is hardcoded as `np.arange(32)` + `.view("S32")`, silently assuming 32-char MD5 hex; a SHA-1/SHA-256 hash file is mis-grouped with no error. Derive the width from the first line or expose it as a constant/flag. | magic-number / hardcoded assumption
dnv3-adapt-01 | open | low | deduplicate-by-namev3.py:196 — output rows hardcode a literal `;` in the f-strings (also :263) instead of the `OUTPUT_DELIMITER` constant (:41) used to derive REPLACEMENTS/cleanup stripping; changing the constant would strip the new delimiter during cleanup but keep emitting `;`, desyncing input-cleaning from output format. Use `OUTPUT_DELIMITER` in both f-strings. | adaptability / code duplication — single source of truth for the delimiter

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-cfg-01 | open | low | link_queue.py:336 — `command_timeout_seconds` (the hung-download safety cap) plus `immediate_worker_count`, `immediate_queue_maxsize`, `queue_render_limit`, `seq_of_sweep_gap` have no UI surface; `_SettingsTabs._build_dispatcher` exposes only sleep/workers/cooldown/max-per-domain/output-folder. Surface `command_timeout_seconds` at least. | safety-relevant timeout is hand-edit-YAML-only. Shipped: `command_timeout_seconds` spinbox in the Dispatcher tab (var + clamped handler + tests). Deferred: the four expert knobs (`immediate_worker_count`, `immediate_queue_maxsize`, `queue_render_limit`, `seq_of_sweep_gap`) — tuning-only, low user value in the dialog; revisit if requested

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-api-20 | open | low | link_queue.py:1443 — `_process_link`'s docstring declares it returns "'queue', 'immediate', 'default', or 'duplicate'", but it also returns "rejected" (:1447, from `_resolve_protocol`'s hard shell+bare-{url} reject), which callers rely on (`_on_add` counts["rejected"] at :3936). Update the docstring to list the full return set so the error surface is an accurate contract. | API contract & compatibility — full error surface must be declared
oze-api-01 | open | med | organize_by_extension.py:2236,2265 — `BUCKET_SIZE` is a mutable module global reassigned by `main()` via `global`; `organize()` exposes no `bucket_size` param, so library callers can't set it and the mutation is process-global and never restored (leaks across successive `organize()`/test calls in one process). Thread `bucket_size` through `organize()`/`BucketManager`. | config discoverability: --bucket-size has no per-call accessor
rdv3-api-01 | open | low | remove-deduplv3.py:70,121-127,94 — documented exit-code contract (epilog "2 input file error, 3 decode error") is mislabeled: an invalid `--encoding` argument exits 3 ("decode error") though it is an argument error, and a missing stdout binary buffer exits 2 ("input file error") though it is an output error. Align codes/labels or add an argument-error code. | API contract & compatibility — error-surface taxonomy

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-cli-01 | open | low | bookmark-tidy.py:1451,1459-1465,1474 — `--output-format`/`--format`, all seven `--keep-*`/`--preserve-url-host-case` flags, and `--fallback-category` are registered with no `help=` text, so `--help` lists them without any description of what they do. Add help strings for each. | CLI / option integrity + product engineering — discoverable, self-documenting options
dnp-cli-01 | open | low | dedupl_numpy.py:18 — usage text is printed to stdout (should be stderr) on the error path, and the script hand-rolls arg handling with no `--help`; migrate to argparse for a consistent CLI surface. | error output on wrong stream
hr-cli-02 | open | low | hash-recursive-ai5.py:1902 — `--quiet` help claims it only "Suppress[es] the end-of-run summary", but it also silences start/done markers and every progress line (`_log_line(..., quiet)` at 1705 driving 1862/2098/2154/2183) and run warnings (1938), while stall warnings (1752, hardcoded `False`) ignore `--quiet` entirely. Reword the help to state the real scope and either gate stall warnings on quiet or document the deliberate exception. | CLI/option integrity — help-vs-behavior drift; Laws of UX: Jakob's Law (consistent flag semantics)
ie-cli-01 | open | low | import_events.py:3802 — `--timezone` is not validated at parse time; an invalid IANA name only raises inside `_apply_default_tz` per ICS file, surfacing as per-file failures instead of one upfront error. Validate with `ZoneInfo` in an argparse `type`. |
ie-cli-02 | open | low | import_events.py:3969 — the `--pdf-ocr-mode` help clause "never skips OCR and uses vision for unreadable PDFs" reads as "never skips OCR" (i.e. always OCRs), the opposite of the actual behavior where mode "never" makes `_should_pdf_ocr` return False. Reword to "never: skip OCR entirely, using vision only for unreadable PDFs". | CLI/option integrity — docs-vs-behavior (Laws of UX — clarity/consistency)
lq-cli-20 | open | low | link_queue.py:5319 — `main()` never parses `sys.argv`; `link_queue.py --help`, `--version`, or any flag is silently ignored and launches the Tk GUI instead, giving no way to discover config paths/knobs from the shell. Add a minimal argparse front door (at least `--help`/`--version`, optionally `--config`/`--state`) before constructing the app. | CLI / option integrity — unhandled flags are silently ignored (misleading interface)
oze-cli-01 | open | low | organize_by_extension.py:2348 — `parser.set_defaults(sniff=True, prune_empty=False)` is a no-op: `--no-sniff` (store_false, dest=sniff) already defaults sniff to True and `--prune-empty-dirs` (store_true, dest=prune_empty) already defaults prune_empty to False. Delete the redundant line. | CLI/option integrity — dead argparse config

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-doc-01 | open | med | hash-recursive-ai5.py:13 — module docstring ("Stage 2 hash — for groups with size > 8 MiB") plus hash_tail_and_samples/ThirdsStrategy docstrings (692, 704, 707, 722) claim stage 2 runs only when size > 2*CAP (8 MiB); the actual gate in `_split_stage1_buckets:1369` is size > HEAD_TAIL_THRESHOLD (= CAP = 4 MiB). Docs understate when stage 2 fires by 2x. Correct every reference to "size > CAP". | documentation — docs-vs-behavior drift on the headline algorithm
lq-doc-01 | open | low | link_queue.py:20 — module docstring says legacy `~/.link_queue_config.json` is migrated on first run, but `LEGACY_CONFIG_FILE_NAME="link_queue_config.json"` resolves via `_resolve_state_path` to the XDG/script dir, never `~/.link_queue_config.json`; correct the docstring to the actual location. | docs-vs-behavior drift
mkp-doc-02 | open | low | minikeypad.py:1285 — the `_poll_connection` comment states `still_connected()` "runs usb.core.find (full bus enumeration)", but `still_connected` (:220) only calls `self.dev.get_active_configuration()` on the existing handle with no bus scan (asserted by tests/test_minikeypad.py:686). Correct the comment to state the per-device config query can still block on libusb, hence off-thread. | documentation — comment drift vs code/tests
oze-doc-01 | open | low | organize_by_extension.py:200 — the EXTENSION_ALIASES comment claims the alias exists "so a real JPEG named .jpeg isn't moved to a separate jpg/ bucket," but `resolve_real_extension` (L479-481) returns the unchanged declared extension, so a real `.jpeg` buckets under `jpeg/` separate from `jpg/` (verified: tests/fuzz/fuzz_organize_by_extension.py:205 asserts resolve_real_extension('photo.jpeg')=='jpeg'). Rewrite the comment to state aliases only feed the declared-vs-detected compatibility check, not bucket unification. | documentation — comment-vs-behavior drift; xref resolve_real_extension:479-481
rf-doc-30 | open | low | relocate_folder.py:865 — `_check_disk_space` docstring ("Uses `_src_total_bytes`") and `copy_tree` docstring (800, 805) still describe the byte total as coming from `_src_total_bytes`, but the live path computes it via `_src_size_totals(src)` (823, 876); `_src_total_bytes` is itself dead (rf-unused-10). Update the docstrings to name `_src_size_totals` so they don't reference a helper marked for deletion. | documentation — docs-vs-code drift, cross-ref rf-unused-10

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
rdv3-i18n-01 | open | med | remove-deduplv3.py:37 — `_ = gettext.gettext` routes every user string, but no domain is ever bound (no `bindtextdomain`/`textdomain`/`gettext.translation(...).install`), so `_()` always returns the source English text — translation is impossible without a code edit. Bind a domain+localedir or document the plumbing as intentionally inert. | i18n — hook present but no catalog ever consulted

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-unused-01 | open | low | dedupl_numpy.py:43 — `uniq` unpacked from `np.unique` is never used (only `inverse`/`counts` are); bind to `_`. Ruff/pyright don't flag tuple-unpack unused, so it survives lint. |
ie-unused-01 | open | low | import_events.py:2131 — `_decode_event_payload` has no production caller (parse_llm_events uses `_decode_event_payload_or_none`); only tests call it. Delete and point tests at the real function. | grep-proven test-only
ie-unused-02 | open | low | import_events.py:2817 — `_ocr_image_bytes` has no production caller (OCR paths use `_ocr_image_path`/`_ocr_image_path_once`); only tests call it. Delete or wire. | grep-proven test-only
ie-unused-03 | open | low | import_events.py:3157 — `_pdf_to_images` has no production caller (extract_from_pdf renders via `_render_pdf_image_paths`); several tests monkeypatch it expecting to gate PDF OCR, so those guards never fire and give false coverage (e.g. test_extract_from_pdf_auto_skips_ocr_when_text_is_usable). Delete and fix the tests to patch `_render_pdf_image_paths`. | dead + misleading tests
ie-unused-04 | open | low | import_events.py:3188 — `_pdf_ocr_from_file` has no production caller (extract_from_pdf inlines render_once + `_pdf_ocr_text_from_paths`); delete or wire. | grep-proven dead
oze-unf-01 | open | low | organize_by_extension.py:840 — `Bucket.reserve` (and the `Bucket.name` property at 833) have no production caller — `choose` uses `names.add(...)` directly (930) and `bucket.path` downstream; both are exercised only by tests. Decide delete vs wire (`choose` could call `reserve`). | grep-proven test-only
rf-unused-11 | open | low | relocate_folder.py:1030 — `_VERIFY_WORKERS` and `_VERIFY_INFLIGHT` are assigned but never read by live code (verify pool uses `_resolved_jobs`/`_inflight_cap`); only docstring references remain. Delete both. | dead const — delete
rf-wire-01 | open | med | relocate_folder.py:1921 — `_copy_and_verify` calls `copy_tree` with no `progress_cb`, and no CLI flag feeds one, so the entire `tracking_copy2` progress-accounting path (lines 677-700, the `_src_size_totals` apparent-byte walk that feeds it) is unreachable outside tests. Either wire a `--progress` flag through `Plan`/`execute` or drop the dead machinery. | wiring gap — shipped-but-unwired
rf-unused-10 | open | low | relocate_folder.py:797 — `_src_total_bytes` has no live caller (grep-proven; only docstring + test references); `_src_size_totals` superseded it and callers use its `[0]`/`[1]` directly. Delete `_src_total_bytes` (and update the test that pins it). | dead helper — delete
rf-unused-12 | open | low | relocate_folder.py:865 — `_OWNERSHIP_INFLIGHT` is assigned but never read by live code (only the dead `_VERIFY_INFLIGHT` alias + a docstring); the live inflight bound comes from `_inflight_cap(workers)`. Delete it. | dead const — delete
rdv3-unused-01 | open | low | remove-deduplv3.py:152-153 — `if not to_remove: continue` is dead: after `dict.fromkeys` dedup (147) and the `len(paths) < 2` guard (148), paths holds ≥2 distinct strings and `keep` is exactly one of them, so `to_remove` is always non-empty. Delete the branch. | unused/dead-code — logic-proven unreachable; recommend delete

## unused functions/methods

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-unf-01 | open | low | bookmark-tidy.py:146,151,156,160,172,179 — `config_read`, `bookmark_read`, `links_extract`, `links_cleanup`, `filter_youtube`, `get_current_unix_epoch` have no production caller (grep-proven: only tests/fuzz); dead BeautifulSoup-era path superseded by `NetscapeBookmarkParser`. Delete them and their tests. | grep-proven test-only
bt-unused-01 | open | low | bookmark-tidy.py:597 — `detect_bookmark_format` (:597) and `_detect_json_format` (:624) have no in-file caller (only `_detect_bookmark_format_with_data` and `_json_bookmark_format` are used). Delete or wire. | grep-proven unused
bt-unf-02 | open | low | bookmark-tidy.py:901 — `tidy_bookmarks` is called only from tests; `_run` reimplements the same dedup+categorize logic inline, so the real entry point never uses it. Wire `_run` to call it or delete. | test-only symbol (see bt-dup-01)
hr-unf-01 | open | low | hash-recursive-ai5.py:170 — `threaded_walk` has no production caller (main uses `iter_threaded_walk`); grep shows only tests/ invoke it. Keep as documented API or delete with its tests. | grep-proven test-only, same class as hr-leg-01
ie-unused-06 | open | low | import_events.py:565 — `_ocr_language_chain` has no production caller (production uses `_ocr_language_chain_with_source`); only tests (test_import_events.py:1018/1024) call it. Delete and point tests at `_ocr_language_chain_with_source`, or wire. | unused functions/methods — grep-proven test-only wrapper
ie-unused-07 | open | low | import_events.py:581 — `_language_for_text` is called by no production code (every extractor uses `_language_for_text_with_source` at 545/582/3124/3148/3155/3300/3348/3357); only tests reference it (test_import_events.py:965/982/1005). Record delete, or have tests assert via the `_with_source` variant. | unused functions/methods — grep-proven test-only, parallels the recorded _language_for_ocr chain
ie-unused-05 | open | low | import_events.py:590 — `_language_for_ocr` and its sole helper `_language_for_ocr_with_source` (:585) have no production caller (production calls `_ocr_language_chain_with_source` directly); only tests (test_import_events.py:1006/1012) reach it. Delete the pair and point tests at the live chain builder, or wire. | unused functions/methods — grep-proven test-only chain
oze-unf-02 | open | low | organize_by_extension.py:2229 — `parse_args()` has no production caller (main calls `build_parser().parse_args()` directly at :2238); only tests import it. Keep as documented API or inline. | grep-proven test-only

## legacy / deprecation

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-leg-01 | open | low | hash-recursive-ai5.py:705 — `_make_head_batch`/`_make_tail_batch` and the `_head_batch`/`_tail_batch` module shims (745-746) have no production caller (pipeline uses `_make_head_candidate_batch`/`_make_tail_stage2_batch`); only their own legacy-shim tests at test_hash_recursive.py:1903/1912 exercise them. Decide delete vs keep. | legacy/deprecation — test-only constituency
rf-leg-30 | open | low | relocate_folder.py:234 — `_safe`/`_required` back-compat aliases (lines 234-235) have no production caller: all live code uses the verb-named `_swallow_or_warn`/`_raise_or_fail` (e.g. 382, 386, 1655), and the standalone-script repo rule forbids intra-repo imports so no in-repo "external caller" the comment cites can exist; only tests reference them (test at :2900 merely asserts identity). Delete both aliases and the two identity-assert tests, or record as intentionally-kept in notes. | legacy/deprecation — constituency grep-proven gone (grep of relocate_folder.py + tests/)
rf-leg-31 | open | low | relocate_folder.py:730 — `shutil.rmtree(path, onerror=_record)` uses the `onerror` parameter, deprecated since Python 3.12 in favor of `onexc` (raises DeprecationWarning now, slated for removal). `_record` already tolerates both excinfo shapes (line 726 tuple check), so switch to `onexc=` with a version guard (`sys.version_info >= (3, 12)`) or fall back to `onerror` on older runtimes. | legacy/deprecation + platform — CPython 3.12 shutil.rmtree onexc migration

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
