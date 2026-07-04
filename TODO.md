# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `bt-` bookmark-tidy.py, `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `mkp-` minikeypad.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-sec-01 | open | low | bookmark-tidy.py:924 — `--auto-install-llama` runs `pip install llama-cpp-python` from the network with no version or hash pin; the flag is explicit, but it still executes remote package/setup code without reproducibility. Pin the package/version and document or support a hash-constrained install. | STRIDE Tampering / EoP; supply-chain
mkp-sec-01 | open | med | minikeypad.py:1623 — on startup, when pyusb is missing, the app runs `pip install pyusb` automatically (default-on; only opt-out via `--no-auto-install`/env), executing remote package/setup code with no consent prompt and no version/hash pinning. Make auto-install opt-in or prompt first, and pin the version. | STRIDE Tampering / EoP; supply-chain
oze-sec-01 | open | low | organize_by_extension.py:1033 — `_require_regular_source` classifies via `os.lstat` then `move_file` (1068) hardlinks via `os.link` (default `follow_symlinks=True`); a hostile concurrent filesystem can swap the path for a symlink between the check and the link (TOCTOU). Scanner-fed paths are safe, but `move_file` is public API; harden with `os.link(..., follow_symlinks=False)` or an fd-based open+fstat. | STRIDE Tampering / TOCTOU
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-gov-01 | open | low | link_queue.py:261 — `DEFAULT_CONFIG["output_folder"]` ships a committed private absolute path `/backups/disk4` as the cwd for every subprocess; on any other machine it fails `_resolve_cwd` and silently inherits cwd, and it leaks the author's disk layout. Default to `""` (script dir) as the docstring already implies. | AGENTS "no private absolute paths" rule; add CI grep guard

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-di-01 | open | med | bookmark-tidy.py:365 — Firefox import copies only `places.sqlite` before reading it; live Firefox profiles commonly keep recent bookmark writes in `places.sqlite-wal`, so the export can miss uncheckpointed bookmarks or read a stale snapshot. Use SQLite's backup API or copy/open the WAL-aware database safely. | sqlite WAL snapshot integrity
oze-di-01 | open | low | organize_by_extension.py:930 — `BucketManager.choose` adds `source.name` to the bucket name set before the move is submitted, but a failed/skipped move (worker returns error tuple in `_drain_futures`, 1853) never releases the reservation; the in-memory bucket then counts toward `BUCKET_SIZE`/`_BUCKET_FULL` while disk has room, wasting bucket slots and allocating extra dirs. Release the reserved name on move failure. | in-memory vs filesystem drift

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
dnv3-perf-02 | open | low | deduplicate-by-namev3.py:176 — the strict-upper-triangle mask is built with a per-row Python loop (`for r in range(block.shape[0]): mask[r, :r+1] = False`), up to BLOCK_ROWS iterations per block; vectorize with `np.triu`/`np.tril_indices` to keep the masking in C. | numpy vectorization on hot path
ie-perf-01 | open | low | import_events.py:2143 — `_stage_cache_key` calls `_file_sha256` (full-file read) on every read and write, so one PDF with --stage-cache on is hashed up to ~6× per run (pdf_text/pdf_ocr/llm_text × read+write); memoize the digest per file path+mtime. | N+1 full-file hashing
oze-perf-02 | open | low | organize_by_extension.py:1665 — `plan_moves` validates sortedness with `all(a <= b for a, b in zip(files, files[1:]))`, which copies `files[1:]` (O(N) ref copy) and re-scans the full list on every call even though `organize` always passes `sorted(files)` (2001). Use `itertools`/pairwise without the slice copy, or gate the check behind a debug/assert. | redundant O(N) hot-path work
oze-perf-01 | open | med | organize_by_extension.py:444 — `_file_contains_pdf` streams the ENTIRE file with no size cap, and it is invoked from `resolve_real_extension` (425) inside the single-threaded `_preplan_resolve_collisions` loop (1731); one multi-GB mislabeled `.pdf` (non-PDF header) blocks the whole pipeline before any worker starts, and many serialize. Cap the embedded-PDF scan to first N MiB or move it off the serial plan path. | hot-path/serial planning

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-perf-07 | open | low | relocate_folder.py:1233 — `_verify_ownership` re-`lstat`s `src_path` even though `_iter_verify_tasks` (line 1108) already `lstat`'d that entry to classify it; pass the cached `st_mode/st_uid/st_gid` through like `src_size` is passed to `_verify_file` to save one syscall per entry under `--verify-ownership`. | redundant-stat

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-mt-01 | open | med | minikeypad.py:1318 — `_version_check` runs on the connect worker thread and writes shared `self.kp.ReportID`, while the Tk main thread mutates the same `KeyParam` via page-button handlers; the shared object is touched from two threads with no lock or `_ui_q` marshalling. Route the `ReportID` assignment through `_ui_q`. | shared-state mutation off the UI thread

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-dist-02 | open | med | link_queue.py:1096 — no single-instance / advisory file lock guards `STATE_FILE`; two concurrently-running app instances each `_restore_queue_from_state()` the same persisted queue (double-executing every idempotent-or-not command) and both `_save_state()` with last-write-wins, silently corrupting/losing each other's queue. Add an `fcntl.flock`/pidfile on the config or state dir and refuse/degrade on contention. | multi-process coordination; idempotent re-run

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-depend-01 | open | low | dedupl_numpy.py:55-57 — stdout writes have no BrokenPipeError guard, so piping into `head` raises a BrokenPipeError traceback on close. Wrap the write/flush in a BrokenPipeError handler or restore SIGPIPE to default. | common CLI pipe pattern

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-cx-01 | open | low | deduplicate-by-namev3.py:90 — `main()` cyclomatic complexity is 11 (>10 bar); extract stdout setup/input loading and duplicate-report emission into helpers. | radon CC=11
hr-cmplx-03 | open | low | hash-recursive-ai5.py:1174 — `_stage1_hash` cyclomatic complexity is 11 (>10 bar); split progress callback construction and alias/head bucketing branches. | radon CC=11
hr-cmplx-04 | open | med | hash-recursive-ai5.py:1310 — `find_duplicate_groups` spans 128 lines and orchestrates indexing, staged hashing, alias handling, callbacks, and final grouping in one function. Split stage orchestration from result assembly. | fat function / AGENTS complexity bar
hr-cmplx-02 | open | med | hash-recursive-ai5.py:1637 — `main()` spans ~1637-1924 (~190 lines) with 5 nested closures and many try/if branches; cognitive complexity far above the ceiling. Extract stdio setup, dump flush, and the summary block into helpers. | AGENTS complexity<=10
ie-cx-14 | open | low | import_events.py:690 — `ModelConfig.from_args` cyclomatic complexity is 12 (>10 bar); split path/default resolution, digest selection, and numeric option normalization. | radon CC=12
ie-cx-15 | open | med | import_events.py:1044 — `_download_to_cache` cyclomatic complexity is 12 (>10 bar); split resume setup, request/read loop, and completion/verification handling. | radon CC=12
ie-cx-10 | open | med | import_events.py:1435 — `_normalize_loose_time` cyclomatic complexity 15 (>10 bar); split the AM/PM vs 24h coercion into helpers. | radon CC=15
ie-cx-16 | open | low | import_events.py:1637 — `_date_parts_from_values` cyclomatic complexity is 11 (>10 bar); extract year/month/day validation and two-digit-year expansion. | radon CC=11
ie-cx-11 | open | med | import_events.py:1672 — `_split_table_time` cyclomatic complexity 25, by far the worst in the file; extract the has-time-columns token parse and the explicit-regex parse into named helpers. | radon CC=25
ie-cx-12 | open | med | import_events.py:1739 — `_calendar_hierarchy_lines` complexity 13; the month/heading/weekday/day-number/day-title state branches should be table-dispatched like `_calendar_table_lines` was in ie-cx-01. | radon CC=13
ie-cx-13 | open | med | import_events.py:2223 — `_paddle_texts` complexity 13 from deep dict/list/tuple recursion branches; split per container type. | radon CC=13
lq-cx-01 | open | med | link_queue.py:520 — `ConfigStore._normalize_config_schema` cyclomatic complexity is 16 (>10 bar); split protocol normalization from scalar coercion. | radon CC=16
lq-cx-02 | open | low | link_queue.py:901 — `Dispatcher._save_state` cyclomatic complexity is 14 (>10 bar); extract snapshot building and atomic YAML write handling. | radon CC=14
lq-cx-03 | open | low | link_queue.py:1096 — `Dispatcher._restore_queue_from_state` cyclomatic complexity is 11 (>10 bar); split persisted item load, queue restore, and in-flight retry handling. | radon CC=11
lq-cx-04 | open | low | link_queue.py:1337 — `Dispatcher._ensure_immediate_pool` cyclomatic complexity is 12 (>10 bar); extract consumer pruning and resize decisions. | radon CC=12
lq-cx-05 | open | low | link_queue.py:1920 — `Dispatcher._run_item` cyclomatic complexity is 13 (>10 bar); split process spawn, streaming, timeout, and metric recording. | radon CC=13
lq-cx-06 | open | med | link_queue.py:2140 — `Dispatcher._pick_next_item` cyclomatic complexity is 19 (>10 bar); isolate eligibility checks, domain-cap scoring, and claim mutation. | radon CC=19
lq-cx-07 | open | low | link_queue.py:3704 — `LinkQueueApp._update_status` cyclomatic complexity is 12 (>10 bar); extract status counters and message formatting. | radon CC=12
oze-cx-01 | open | low | organize_by_extension.py:534 — `list_files` cyclomatic complexity is 13 (>10 bar); split skip-path checks, bucket detection, and verbose logging. | radon CC=13
oze-cx-02 | open | med | organize_by_extension.py:1938 — `organize` cyclomatic complexity is 21 (>10 bar); split scan, plan, execute, and prune phases into smaller helpers. | radon CC=21
rf-cx-01 | open | low | relocate_folder.py:1091 — `_iter_verify_tasks` cyclomatic complexity is 11 (>10 bar); split file/dir/symlink task creation from ownership task creation. | radon CC=11
rdv3-cx-01 | open | low | remove-deduplv3.py:55 — `main()` cyclomatic complexity is 17 (>10 bar); extract argument/encoding setup, hash grouping, and rm-command emission. | radon CC=17

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-dup-04 | open | low | relocate_folder.py:410 — `_symlink_points_at_empty_target` duplicates the `is_symlink` -> `readlink` -> relative-to-absolute -> `resolve()`-compare preamble already implemented in `already_migrated` (line 368); extract a shared `_resolves_to(source, target)` helper (within-file, not intra-repo) so the two callers can't drift. | within-file dup

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
ie-rel-20 | open | low | import_events.py:3399 — `_end_precedes_start` does `end < start` when both are datetime; a tz-aware start with a tz-naive end (or vice versa) from model/ICS output raises TypeError, which is uncaught in build_ics/write_events_ics/_run_main and crashes the whole run. Normalize both to comparable kinds before comparing. | uncaught TypeError on mixed tz-awareness

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-robust-01 | open | low | bookmark-tidy.py:1232 — `_atomic_write_text` fsyncs the temp file then `os.replace`s it, but never fsyncs the parent directory, so a crash or power loss right after rename can lose the directory entry. Fsync the containing directory after replace. | atomic-write durability gap
dnp-robust-02 | open | low | dedupl_numpy.py:21 — `open()` has no error handling, so a missing/unreadable hash-file exits with a raw `FileNotFoundError`/`OSError` traceback instead of the clean `error:` + nonzero exit that remove-deduplv3.py uses. | graceful failure contract
dnp-robust-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(f.fileno(), 0, ...)` on a zero-byte input raises `ValueError: cannot mmap an empty file` (uncaught traceback); guard `os.fstat(f.fileno()).st_size == 0` and return cleanly. | interrupted/empty-input recovery
ie-robust-20 | open | low | import_events.py:3347 — `_atomic_write_bytes` fsyncs the temp file then os.replace, but never fsyncs the parent directory, so a crash/power loss right after rename can lose the rename; fsync the containing dir after replace for true durability. | atomic-write durability gap
lq-rob-01 | open | low | link_queue.py:953 — `_save_state` (and `_write_config_file`, line 590) create uniquely-named `*.tmp` files via `mkstemp` then rename; a SIGKILL between create and rename orphans them, and nothing sweeps stale `<name>.*.tmp` on startup, so they accumulate in the config/state dir across crashes. Sweep leftover temp siblings on load. | orphaned-resource cleanup
mkp-rob-10 | open | low | minikeypad.py:1117 — `_append_log` inserts into the `ScrolledText` log with no line cap, so a long-running session grows the widget (and its backing text) without bound; trim to the last N lines on insert. | unbounded buffer / memory growth
mkp-rob-11 | open | low | minikeypad.py:1126 — `_drain_ui` calls each queued callable as `self._ui_q.get_nowait()()`; any exception other than `queue.Empty` propagates past the `self.after(120, self._drain_ui)` reschedule, permanently killing the cross-thread UI pump (logs, connect state, write results all freeze). Wrap each callable in try/except (or reschedule in a `finally`). | self-healing loop dies on one bad callback

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
_clean — `ruff check *.py` reports no issues across all root files (rescan 2026-07-04)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `pyright *.py` reports 0 errors / 0 warnings across all root files (rescan 2026-07-04)._

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-obs-01 | open | low | dedupl_numpy.py:58 — the `equal files: X / N` summary is `print()`ed to stdout, intermixed with the machine-readable duplicate-path list written to `sys.stdout.buffer` (lines 55-57); route the summary to stderr (as remove-deduplv3.py does) so stdout stays a clean path stream. | three-pillars logs; stdout hygiene
dnv3-obs-01 | open | low | deduplicate-by-namev3.py:76 — lines whose cleaned form is empty (blank, or only REPLACEMENTS/WORD_TOKENS chars) are dropped with no count or notice, so the user can't tell input was discarded. Track and report a dropped count to stderr. | silent data loss
lq-obs-20 | open | low | link_queue.py:2310 — a timed-out item is double-counted: `_arm_command_timeout._on_timeout` (line 1802) records `timeouts` while the killed proc returns a nonzero code so `_worker_step` also records `failures`, so `_metrics_summary` shows "1 failed, 1 timed out" for a single event. Count a timeout as timeout-only (skip the failure increment when the timer fired). | three-pillars metrics accuracy
lq-obs-21 | open | low | link_queue.py:2608 — `LogSink._flush_batch` silently discards the batch on any write/flush exception (`_close_locked(); return`) with no user-visible notice, unlike open failures which `_note_open_failure` surfaces; a persistently failing target loops close→reopen→drop forever, losing all file-log lines invisibly. Surface a throttled write-failure warning. | silent-failure audit
lq-obs-22 | open | low | link_queue.py:3607 — `_on_add` folds a `"rejected"` outcome (unknown-protocol `default_command` with shell+bare `{url}`) into `total = sum(counts.values())` but the printed summary only reports queue/immediate/default/duplicate, so `total` and the breakdown disagree and a dropped link is near-invisible. Add rejected to the summary line. | docs-vs-behavior / count mismatch
oze-obs-01 | open | low | organize_by_extension.py:2244 — a Ctrl+C during scan/plan/prune is caught in `main` and returns exit 0 ("Interrupted."), but a Ctrl+C during the move stage exits 1 (`_run_moves` line 1919 `SystemExit(1)`); callers/scripts cannot reliably detect an interrupted run. Use a consistent non-zero exit code for all interrupt paths. | silent-failure/exit-code
rf-obs-07 | open | med | relocate_folder.py:1033 — `_chown_pair` logs one `could not chown …` warning per entry on `PermissionError`, and `_replicate_ownership` always fans out the full pool regardless of euid; a non-root migration of a tree owned by another user emits one warning per file (log flood). Summarize like the rmtree cleanup (cap at N + suppressed count) and/or skip when `os.geteuid() != 0` can't change owner. | silent-failure/log-flood

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-wd-01 | open | high | hash-recursive-ai5.py:496 — `_read_window_into`'s `f.read()` (and the walk's `scandir`/`stat`) have no timeout or stall detection; a file on a hung NFS/removable mount blocks a worker thread indefinitely while progress only reflects completed batches — no heartbeat or auto-abort. Add per-file I/O stall detection or document the limitation. | stall/liveness
oze-watch-01 | open | med | organize_by_extension.py:1837 — `_drain_futures` calls `wait(futures, return_when=FIRST_COMPLETED)` with no timeout, and `_run_moves` (1902-1914) never times out an individual move; a single `os.link`/`shutil.copy2`/`os.replace` hung on a stalled NFS/SMB mount stalls the whole run indefinitely with no heartbeat or abort. Add `wait(..., timeout=)` + stall detection or `fut.result(timeout=)` with a watchdog log. | liveness/stall detection
rf-wd-01 | open | med | relocate_folder.py:702 — `shutil.copytree`, `verify_copy`, and `_sha256` have no timeout/heartbeat/stall detection; a hung network mount (NFS/CIFS) blocks the migration indefinitely with no progress signal or abort. Add a progress-stall watchdog or per-op timeout for these long-running transfers/scans. | no-stall-detection

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-plat-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(..., prot=mmap.PROT_READ)` uses the POSIX-only `prot=` keyword and `PROT_READ`; on Windows this errors (needs `access=`). Document Linux-only support or branch on `os.name`. | POSIX-only primitive
dnv3-plat-01 | open | low | deduplicate-by-namev3.py:93 — `sys.stdout.reconfigure(errors="surrogateescape")` sets errors only, leaving stdout's encoding at the locale default; under a non-UTF-8 locale (LC_ALL=C/ascii) a legitimately-decoded non-ASCII cleaned string raises UnicodeEncodeError on write. Reconfigure with `encoding="utf-8", errors="surrogateescape"`. | stdout encoding not forced
hr-plat-04 | open | low | hash-recursive-ai5.py:563 — `_hash_file_windows` references `os.O_NOFOLLOW | os.O_CLOEXEC` unconditionally; both attributes are absent on Windows (AttributeError at hash time) and there is no platform guard or documented POSIX-only contract. Guard with `getattr(os, "O_NOFOLLOW", 0)` / `getattr(os, "O_CLOEXEC", 0)` or document the POSIX-only requirement. | cross-OS portability / POSIX-only primitive
lq-plat-10 | open | med | link_queue.py:2603 — `LogSink._open_locked` passes `os.O_NOFOLLOW`, which is Unix-only; on Windows accessing `os.O_NOFOLLOW` raises AttributeError, caught by the broad `except Exception`, so the log file silently never opens despite the module advertising Windows support. Guard with `getattr(os, "O_NOFOLLOW", 0)`. | POSIX-only primitive on a cross-OS surface
rf-plat-01 | open | low | relocate_folder.py:239 — `os.O_DIRECTORY` is used unguarded while `O_NOFOLLOW` uses `getattr(os, "O_NOFOLLOW", 0)`; on a platform lacking `O_DIRECTORY` this raises `AttributeError` at `_source_identity_fd` instead of degrading. Wrap it with the same `getattr(os, "O_DIRECTORY", 0)` fallback for consistency. | inconsistent-guard
rdv3-plat-01 | open | low | remove-deduplv3.py:81 — `sys.stdout.reconfigure(errors=err_mode)` sets errors only; under a non-UTF-8 locale a legitimately-decoded non-ASCII path crashes with UnicodeEncodeError when the rm line is written. Also force `encoding="utf-8"` on the reconfigure. | stdout encoding not forced

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cache-01 | open | med | import_events.py:2179 — the stage cache writes one JSON per file+stage+options hash with no size cap, no eviction, and no reset hook; stale entries for prior file versions accumulate in the cache dir forever. Add a cap/LRU pruning and a documented reset. | caching-strategy invariant (cap+invalidation+reset)

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-adapt-01 | open | med | dedupl_numpy.py:36 — hash width is hardcoded as `np.arange(32)` + `.view("S32")`, silently assuming 32-char MD5 hex; a SHA-1/SHA-256 hash file is mis-grouped with no error. Derive the width from the first line or expose it as a constant/flag. | magic-number / hardcoded assumption
rf-adapt-01 | open | low | relocate_folder.py:743 — `_DISK_SPACE_HEADROOM` (1.05) and `_SHA256_RETRY_ATTEMPTS` (line 1368, =2) are hardcoded constants with no env/CLI override, unlike the env-tunable `RELOCATE_STALE_PID_WARN_AT`; expose them via env accessors so operators can tune headroom/retries without a code edit. | magic-number

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-config-01 | open | low | deduplicate-by-namev3.py:33-39 — DEFAULT_THRESHOLD, BLOCK_THRESHOLD, BLOCK_ROWS, REPLACEMENTS, and WORD_TOKENS are module constants with no env/CLI override; WORD_TOKENS ("xxx","monography") is a hardcoded domain assumption that needs a code edit to change. Expose the token/replacement lists via flags or document them as fixed invariants. | hardcoded domain assumption
oze-cfg-01 | open | low | organize_by_extension.py:33 — `BUCKET_SIZE` (and `PROGRESS_EVERY`, `SUBMIT_BACKLOG_MULT`) are hardcoded module constants with no CLI flag or env override, yet `_log_run_stats`/comments (880-885) explicitly invite the user to "tune BUCKET_SIZE"; expose a `--bucket-size` flag or env knob with a typed default. | runtime knob w/o accessor

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-cli-01 | open | low | dedupl_numpy.py:18 — usage text is printed to stdout (should be stderr) on the error path, and the script hand-rolls arg handling with no `--help`; migrate to argparse for a consistent CLI surface. | error output on wrong stream
hr-cli-01 | open | low | hash-recursive-ai5.py:1683 — `args.jobs = max(1, args.jobs)` only lower-clamps; `-j 100000` spawns that many walk threads and a `ThreadPoolExecutor(max_workers=100000)`, exhausting threads/FDs. Add a sane upper clamp (e.g. multiple of cpu_count). | resource exhaustion / DoS
ie-cli-01 | open | med | import_events.py:2450 — `--ocr-engine auto` and `both` take the identical code path (always run Paddle+Tesseract and merge); auto's documented "Paddle first, falls back to Tesseract when weak" (help at line 3523) never happens. Implement the fallback or fix the help text so the two modes differ. | docs-vs-behavior drift
rf-cli-02 | open | med | relocate_folder.py:2122 — `--recover` dispatches to `_run_recover` before any `--dry-run` handling, so `relocate_folder.py --recover --dry-run <src>` still executes the real `rename(backup -> source)` mutation; recovery ignores `--dry-run` entirely. Guard `_run_recover` on `ns.dry_run` and print a would-recover line instead. | flag-ignored / mutation-under-dry-run

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-dep-01 | open | low | hash-recursive-ai5.py:44 — top-level `import blake3` is unguarded; a missing third-party dep aborts with a raw ImportError traceback instead of an actionable message. Wrap in try/except ImportError -> friendly `sys.exit`. | dependability / graceful degradation

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-doc-01 | open | low | minikeypad.py:23 — the module docstring says the app "offers to install it for you on first run," implying a prompt, but `_ensure_pyusb` installs silently with no confirmation; reword to state it auto-installs (or add the prompt to match the docs). | docs-vs-behavior drift
oze-doc-01 | open | low | organize_by_extension.py:374 — docstrings reference `:data:`ZIP_FAMILY`` (also line 250) as a runtime-extendable symbol, but no `ZIP_FAMILY` exists; it was replaced by `CONTAINER_FAMILIES` + `extra_zip_family`. Update the docstrings to the current names to remove stale cross-references. | docs-vs-code drift
rf-doc-02 | open | low | relocate_folder.py:7 — module docstring usage example invokes `sudo python relocate.py …`, but the script is `relocate_folder.py`; the documented command as written won't run. Update the example to the real filename. | docs-vs-behavior drift

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-ux-01 | open | low | hash-recursive-ai5.py:78 — default `--hashes-file=hashes.txt` writes/appends a persistent dump into the current working directory on every run with no opt-in, silently polluting cwd. Make the dump opt-in or default off. | Laws of UX (least surprise)
mkp-ux-01 | open | low | minikeypad.py:1150 — `_select_key` returns silently when `select_physical_key` refuses the click on the LED page (page 4); the user clicks a key, nothing happens, and no log/feedback is emitted. Add a hint (e.g. "Key selection disabled on LED page"). | Laws of UX: feedback / Doherty threshold

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-leg-01 | open | low | hash-recursive-ai5.py:705 — `_make_head_batch`/`_make_tail_batch` and the `_head_batch`/`_tail_batch` module shims (745-746) have no production caller (pipeline uses `_make_head_candidate_batch`/`_make_tail_stage2_batch`); only their own legacy-shim tests at test_hash_recursive.py:1903/1912 exercise them. Decide delete vs keep. | legacy/deprecation — test-only constituency
ie-unused-01 | open | low | import_events.py:1948 — `_decode_event_payload` has no production caller (parse_llm_events uses `_decode_event_payload_or_none`); only tests call it. Delete and point tests at the real function. | grep-proven test-only
ie-unused-02 | open | low | import_events.py:2520 — `_ocr_image_bytes` has no production caller (OCR paths use `_ocr_image_path`/`_ocr_image_path_once`); only tests call it. Delete or wire. | grep-proven test-only
ie-unused-03 | open | low | import_events.py:2841 — `_pdf_to_images` has no production caller (extract_from_pdf renders via `_render_pdf_image_paths`); several tests monkeypatch it expecting to gate PDF OCR, so those guards never fire and give false coverage (e.g. test_extract_from_pdf_auto_skips_ocr_when_text_is_usable). Delete and fix the tests to patch `_render_pdf_image_paths`. | dead + misleading tests
ie-unused-04 | open | low | import_events.py:2872 — `_pdf_ocr_from_file` has no production caller (extract_from_pdf inlines render_once + `_pdf_ocr_text_from_paths`); delete or wire. | grep-proven dead
oze-unf-01 | open | low | organize_by_extension.py:840 — `Bucket.reserve` (and the `Bucket.name` property at 833) have no production caller — `choose` uses `names.add(...)` directly (930) and `bucket.path` downstream; both are exercised only by tests. Decide delete vs wire (`choose` could call `reserve`). | grep-proven test-only
rf-unused-11 | open | low | relocate_folder.py:1030 — `_VERIFY_WORKERS` and `_VERIFY_INFLIGHT` are assigned but never read by live code (verify pool uses `_resolved_jobs`/`_inflight_cap`); only docstring references remain. Delete both. | dead const — delete
rf-wire-01 | open | med | relocate_folder.py:1921 — `_copy_and_verify` calls `copy_tree` with no `progress_cb`, and no CLI flag feeds one, so the entire `tracking_copy2` progress-accounting path (lines 677-700, the `_src_size_totals` apparent-byte walk that feeds it) is unreachable outside tests. Either wire a `--progress` flag through `Plan`/`execute` or drop the dead machinery. | wiring gap — shipped-but-unwired
rf-unused-10 | open | low | relocate_folder.py:797 — `_src_total_bytes` has no live caller (grep-proven; only docstring + test references); `_src_size_totals` superseded it and callers use its `[0]`/`[1]` directly. Delete `_src_total_bytes` (and update the test that pins it). | dead helper — delete
rf-unused-12 | open | low | relocate_folder.py:865 — `_OWNERSHIP_INFLIGHT` is assigned but never read by live code (only the dead `_VERIFY_INFLIGHT` alias + a docstring); the live inflight bound comes from `_inflight_cap(workers)`. Delete it. | dead const — delete

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
