# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `bt-` bookmark-tidy.py, `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `mkp-` minikeypad.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

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
lq-gov-01 | open | low | link_queue.py:261 — `DEFAULT_CONFIG["output_folder"]` ships a committed private absolute path `/backups/disk4` as the cwd for every subprocess; on any other machine it fails `_resolve_cwd` and silently inherits cwd, and it leaks the author's disk layout. Default to `""` (script dir) as the docstring already implies. | AGENTS "no private absolute paths" rule; add CI grep guard

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-int-01 | open | med | deduplicate-by-namev3.py:120 — `--strip-chars` default includes `;` (the output field delimiter) but a user override can omit it; a cleaned value then containing `;` corrupts the `{a};{b};{dist}` line for a `;`-splitting downstream parser. Force-add `;` to the effective strip set or escape `;` on output. | user override re-enables the injection the default guards

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
mkp-perf-01 | open | low | minikeypad.py:225 — `still_connected()` runs a full `usb.core.find` bus enumeration every 1 s while connected instead of validating the held handle; a cheaper liveness check on the existing handle avoids re-enumerating the whole bus per second. | off-thread already (not UI-blocking) but wasteful
oze-perf-02 | open | low | organize_by_extension.py:1665 — `plan_moves` validates sortedness with `all(a <= b for a, b in zip(files, files[1:]))`, which copies `files[1:]` (O(N) ref copy) and re-scans the full list on every call even though `organize` always passes `sorted(files)` (2001). Use `itertools`/pairwise without the slice copy, or gate the check behind a debug/assert. | redundant O(N) hot-path work
oze-perf-01 | open | med | organize_by_extension.py:444 — `_file_contains_pdf` streams the ENTIRE file with no size cap, and it is invoked from `resolve_real_extension` (425) inside the single-threaded `_preplan_resolve_collisions` loop (1731); one multi-GB mislabeled `.pdf` (non-PDF header) blocks the whole pipeline before any worker starts, and many serialize. Cap the embedded-PDF scan to first N MiB or move it off the serial plan path. | hot-path/serial planning

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-scal-01 | open | low | deduplicate-by-namev3.py:83 — `_read_groups` materializes every hash and path into `groups`, so peak memory is O(total input); the docstring's "Streams the file; no readlines() into memory" (line 16) overstates it (line reading streams, grouping does not). Soften the docstring claim. | doc-vs-behavior; inherent to whole-file grouping
ie-scal-01 | open | med | import_events.py:1334 — `extract_from_ics` does `Calendar.from_ical(f.read())`, reading the whole `.ics` unbounded unlike `_read_text` which caps the byte window; a large/hostile `.ics` balloons RAM. Bound the read. | only ICS path lacks a size bound

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-n1-01 | open | low | bookmark-tidy.py:317,591,602 — a JSON input is read+parsed up to 3×: `detect_bookmark_format` samples 4 KB (591), `_detect_json_format` reads+parses the whole file (602), then `read_chromium_bookmarks`/`read_firefox_json_bookmarks` (317) reads+parses again. Parse once and pass the decoded object down. |
rf-perf-07 | open | low | relocate_folder.py:1233 — `_verify_ownership` re-`lstat`s `src_path` even though `_iter_verify_tasks` (line 1108) already `lstat`'d that entry to classify it; pass the cached `st_mode/st_uid/st_gid` through like `src_size` is passed to `_verify_file` to save one syscall per entry under `--verify-ownership`. | redundant-stat

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-conc-01 | open | med | organize_by_extension.py:960-962 — `release()` reconstructs the bucket name set only on the `_BUCKET_FULL` path, discarding the "every accepted destination is in the cache" reservation invariant `choose()` relies on; a full-bucket move failure is the trigger for the collision. Retain in-flight reservations across the re-read. | STRIDE Tampering on in-memory plan state; same root as oze-int-01

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-mt-01 | open | low | import_events.py:2408 — `_get_paddle_ocr` holds `_PADDLE_OCR_LOCK` across the paddle import and the multi-second `_build_paddle_ocr`, blocking all workers from reading an already-cached engine for a different language. Build outside the lock / double-checked insert. | lock granularity
oze-mt-01 | open | low | organize_by_extension.py:1951-1952 — `_run_moves` `finally` does `executor.shutdown(wait=False)` with no `cancel_futures` on the non-KeyboardInterrupt exit path; an unexpected exception abandons in-flight futures (neither cancelled nor awaited), swallowing their exceptions while workers may still be moving files during unwind. Use `cancel_futures=True`. | swallowed futures / partial moves continue

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-dist-01 | open | med | link_queue.py:230 — `StateFileLock._acquire_pidfile` (no-fcntl/Windows path) uses `O_CREAT|O_EXCL` with no stale-PID liveness check; a crashed instance orphans the lock permanently so every future launch fails until manual removal. On `FileExistsError`, read the stored pid and reclaim if dead. | only the flock branch auto-releases on crash
lq-dist-02 | open | med | link_queue.py:248 — `StateFileLock._release_flock` unlinks the lockfile on release — the classic flock+unlink race: another process can open+flock the same inode before the unlink, then a third creates a fresh inode and also locks, so two instances both own the queue. Don't unlink the flock lockfile on release (or unlink only under a guard). | STRIDE Elevation/Tampering; single-instance guarantee broken

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-dep-01 | open | med | bookmark-tidy.py:1355 — `read_all_bookmarks` catches only `UserError`, but `read_chromium_bookmarks` (json), `_read_firefox_sqlite_copy` (sqlite3.OperationalError on a locked/corrupt DB), and `read_firefox_jsonlz4_bookmarks` (UnicodeDecodeError) raise other exceptions; one bad/locked file aborts the whole run instead of being skipped. Broaden the except / wrap parsers to degrade per-file. |
dnp-depend-01 | open | low | dedupl_numpy.py:55-57 — stdout writes have no BrokenPipeError guard, so piping into `head` raises a BrokenPipeError traceback on close. Wrap the write/flush in a BrokenPipeError handler or restore SIGPIPE to default. | common CLI pipe pattern
lq-dep-01 | open | med | link_queue.py:2420 — `_worker_step` treats `_run_item`'s `-1` return (spawn rejected: command-not-found, empty/invalid template) identically to a real non-zero subprocess exit and arms a per-domain failure cooldown; a permanently broken protocol then stalls every item on that domain for `failure_sleep_seconds` (default 300 s) in a loop. Skip the cooldown for spawn/infra failure. | fallback amplifies instead of contains a config error

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

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-dup-01 | open | med | bookmark-tidy.py:1442-1449 — `_run` duplicates the full body of `tidy_bookmarks` (deduplicate → guard → `_assign_categories`); collapse `_run` onto `tidy_bookmarks` to keep one code path. | within-file dup
dnv3-dup-01 | open | low | deduplicate-by-namev3.py:70 — `valid_workers` (70-73) and `valid_threshold` (85-87) repeat identical int-parse try/except boilerplate; extract a shared `_parse_int(value, name)` helper. | within-file dup
rf-dup-04 | open | low | relocate_folder.py:410 — `_symlink_points_at_empty_target` duplicates the `is_symlink` -> `readlink` -> relative-to-absolute -> `resolve()`-compare preamble already implemented in `already_migrated` (line 368); extract a shared `_resolves_to(source, target)` helper (within-file, not intra-repo) so the two callers can't drift. | within-file dup
rf-dup-05 | open | med | relocate_folder.py:716 — the rmtree `onerror` collector (failures list + `_record` closure + first-10 warn + overflow warn) is duplicated in `copy_tree` (716-734) and `_copy_and_verify` (1956-1971); extract a shared `_rmtree_logging(path, context)` helper. | AGENTS "shared I/O in one place"
rdv3-dup-01 | open | low | remove-deduplv3.py:136 — the `print(f"error: {e}", file=sys.stderr); sys.exit(2)` block is duplicated verbatim at 143-145; hoist to a `_fail(msg, code)` helper. | within-file dup

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
dnv3-rel-01 | open | low | deduplicate-by-namev3.py:74 — `valid_workers` accepts any positive int (e.g. 1_000_000) and passes it straight to `cdist` as a thread count with no upper bound vs `os.cpu_count()`; clamp or warn when workers exceed available cores. |
mkp-rel-01 | open | med | minikeypad.py:1183,608 — `_on_page` clears the working buffer only when entering LED/Mouse pages, so a key set on the Keys tab leaves `data[KeyType_Num]` set; a later Multimedia click ORs `|=2` → type `3`, and `build_download_reports` (`kind = type & 0xF`) routes to `_mouse_reports` instead of multimedia. Reset the buffer on every page change or recompute kind exclusively. | reachable cross-page state bleed
mkp-rel-02 | open | low | minikeypad.py:489,566 — `multimedia()` stores the value at dynamic `data[kc+off]` but `_mul_reports()` always reads fixed `data[5],data[6]`; they agree only while `KEY_Char_Num==5`, so after any prior keystroke the value is written to an index the report never reads. Use the same fixed index in both. | coupled to mkp-rel-01
oze-rel-01 | open | med | organize_by_extension.py:165,169 — 2-3 byte magic stamps (MZ→exe, BM→bmp, ID3→mp3, BZh→bz2) plus "header always wins" at `resolve_real_extension:433` relocate a plausibly-named file on a 2-byte coincidence: a `notes.txt` starting "MZ"/"BM" is MOVED into `exe/`/`bmp/` with only a warning. Require declared-extension corroboration or a minimum signature length before overriding a non-empty declared extension. | weak-signature false positive → real data move
rf-rel-01 | open | low | relocate_folder.py:1129 — a comment claims the ownership task is yielded after the kind check "so a missing entry fails with the more useful message first", but under `checksum=True` both partials run concurrently in `_run_verify_pool`, so a missing entry may surface the less-useful "ownership stat failed" first via abort-on-first-error. Gate `_verify_ownership` behind a dst-exists check or drop the ordering claim. | ordering only holds on the sequential path

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-rob-02 | open | low | bookmark-tidy.py:1340-1347 — `load_immutable_file` calls `read_text` unguarded; a missing/unreadable `--immutable-file` raises `FileNotFoundError` that escapes `main`'s `UserError` handler as a traceback. Wrap in `UserError`. |
bt-rob-03 | open | low | bookmark-tidy.py:1424-1430 — an invalid `--model` GGUF path is passed straight to `Llama(...)`; the non-`UserError` exception escapes `main`'s handler as a raw traceback. Validate `model_path.is_file()` and raise `UserError`. |
bt-rob-01 | open | low | bookmark-tidy.py:442-448 — `_walk_firefox_rows` recurses on the parent/child graph with no visited-set/cycle guard; a corrupted `places.sqlite` with a parent cycle triggers unbounded recursion (RecursionError/crash). Track visited node ids. |
dnp-robust-02 | open | low | dedupl_numpy.py:21 — `open()` has no error handling, so a missing/unreadable hash-file exits with a raw `FileNotFoundError`/`OSError` traceback instead of the clean `error:` + nonzero exit that remove-deduplv3.py uses. | graceful failure contract
dnp-robust-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(f.fileno(), 0, ...)` on a zero-byte input raises `ValueError: cannot mmap an empty file` (uncaught traceback); guard `os.fstat(f.fileno()).st_size == 0` and return cleanly. | interrupted/empty-input recovery
hr-rob-02 | open | med | hash-recursive-ai5.py:1895 — the `hashes.txt` dump is persisted only in the `finally`, so a SIGKILL/power-loss before it loses the entire dump, contradicting the "complete file on abrupt exit" intent (comment at :1730). Write head digest at stage-1 and patch the composite at stage-2, or flush `dump_pending` periodically. |
hr-rob-01 | open | med | hash-recursive-ai5.py:265 — on a `BaseException` a dying walk worker re-enqueues `d` leaving inflight unchanged and relies on a sibling to retry; if it is the last live worker, `d` stays pending with inflight>0, sentinels are never posted, and the consumer blocks forever on `out_q.get()`. Post sentinels/poison on worker death or bound the get with a liveness check. | all-workers-die deadlock, low-prob unbounded hang
ie-rob-01 | open | med | import_events.py:2939 — `page.get_pixmap(dpi=...)` renders each PDF page with no bound on output dimensions; a crafted PDF with a huge MediaBox produces an enormous bitmap (OOM/DoS from untrusted input). Cap pixel dimensions or estimate size before rendering. | resource exhaustion via crafted PDF
ie-rob-02 | open | low | import_events.py:849 — `reset_paddle_ocr_state` drops the cached engine references (`_PADDLE_OCR = None`) without closing/releasing them, unlike `reset_llm_cache` which calls `_close_cached_llm`; native/GPU handles are leaked on reset. |
lq-rob-01 | open | low | link_queue.py:953 — `_save_state` (and `_write_config_file`, line 590) create uniquely-named `*.tmp` files via `mkstemp` then rename; a SIGKILL between create and rename orphans them, and nothing sweeps stale `<name>.*.tmp` on startup, so they accumulate in the config/state dir across crashes. Sweep leftover temp siblings on load. | orphaned-resource cleanup
rf-rob-01 | open | low | relocate_folder.py:2154 — `_run_recover` computes `source.with_name(name+SUFFIX)` and runs the dry-run branch outside any try/except (main:2122 calls it unwrapped); `--recover /` (empty basename) makes `with_name` raise `ValueError` as an uncaught traceback. Add the empty-basename guard `Plan.from_args` has (line 196). |
rdv3-rob-01 | open | low | remove-deduplv3.py:79 — `_configure_stdout_errors` swallows the reconfigure failure (`except ValueError: pass`) and skips the non-TextIOWrapper branch; when stdout can't be set to surrogateescape, a path with surrogate chars raises an uncaught `UnicodeEncodeError` at write (124). Fall back to `sys.stdout.buffer` with surrogateescape or fail early. |
rdv3-rob-02 | open | low | remove-deduplv3.py:152 — `_emit_remove_commands` writes to `sys.stdout` with no `BrokenPipeError` guard; piping to `head`/`less` yields an uncaught `BrokenPipeError` plus "Exception ignored" noise. Wrap emit/summary in try/except BrokenPipeError and exit cleanly. | generator meant to be piped

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
_clean — `ruff check *.py` reports no issues across all root files (rescan 2026-07-05)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `pyright *.py` reports 0 errors / 0 warnings across all root files (rescan 2026-07-05)._

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-obs-01 | open | low | dedupl_numpy.py:58 — the `equal files: X / N` summary is `print()`ed to stdout, intermixed with the machine-readable duplicate-path list written to `sys.stdout.buffer` (lines 55-57); route the summary to stderr (as remove-deduplv3.py does) so stdout stays a clean path stream. | three-pillars logs; stdout hygiene
hr-obs-01 | open | med | hash-recursive-ai5.py:255 — a walk worker hitting a non-(OSError/ValueError) `BaseException` re-raises inside a daemon thread, reaching only `threading.excepthook` (a stderr traceback) and never main(); the run summary can't reflect the crash and per-worker stats undercount. Record a shared "worker failed"/last-error flag and surface it in the summary. | silent-failure: background task exposes no last-result
lq-obs-20 | open | low | link_queue.py:2310 — a timed-out item is double-counted: `_arm_command_timeout._on_timeout` (line 1802) records `timeouts` while the killed proc returns a nonzero code so `_worker_step` also records `failures`, so `_metrics_summary` shows "1 failed, 1 timed out" for a single event. Count a timeout as timeout-only (skip the failure increment when the timer fired). | three-pillars metrics accuracy
lq-obs-21 | open | low | link_queue.py:2608 — `LogSink._flush_batch` silently discards the batch on any write/flush exception (`_close_locked(); return`) with no user-visible notice, unlike open failures which `_note_open_failure` surfaces; a persistently failing target loops close→reopen→drop forever, losing all file-log lines invisibly. Surface a throttled write-failure warning. | silent-failure audit
lq-obs-22 | open | low | link_queue.py:3607 — `_on_add` folds a `"rejected"` outcome (unknown-protocol `default_command` with shell+bare `{url}`) into `total = sum(counts.values())` but the printed summary only reports queue/immediate/default/duplicate, so `total` and the breakdown disagree and a dropped link is near-invisible. Add rejected to the summary line. | docs-vs-behavior / count mismatch
lq-obs-23 | open | low | link_queue.py:4715 — `_on_queue_rerun` logs "[queue] re-queued {len(items)}" but `_process_link` silently skips items whose URL is still pending/running (returns "duplicate"), so the count overstates what was re-queued. Count non-duplicate outcomes. | metrics accuracy
mkp-obs-01 | open | low | minikeypad.py:93,95,101,105 — `_ensure_pyusb`/`_pip_install` emit progress with `print()` rather than `LOG`, bypassing the configured handler/format/level (logging is configured in `main` before the call). Route through `LOG`. | inconsistent with rest of module
rf-obs-07 | open | med | relocate_folder.py:1033 — `_chown_pair` logs one `could not chown …` warning per entry on `PermissionError`, and `_replicate_ownership` always fans out the full pool regardless of euid; a non-root migration of a tree owned by another user emits one warning per file (log flood). Summarize like the rmtree cleanup (cap at N + suppressed count) and/or skip when `os.geteuid() != 0` can't change owner. | silent-failure/log-flood
rdv3-obs-01 | open | low | remove-deduplv3.py:88 — blank and single-token lines are silently `continue`d in `_read_groups`; malformed/dropped input never surfaces and the summary counts only groups, so corrupt input reads as "0 duplicates". Count skipped lines and include in the stderr summary. | silent-failure audit

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-wd-02 | open | low | bookmark-tidy.py:1006 — `subprocess.check_call([pip install …])` has no timeout; a hung network/pip resolve stalls the process indefinitely. Pass a timeout and surface failure. |
bt-wd-01 | open | med | bookmark-tidy.py:986-995 — `LlamaCategorizer._complete` calls the model with no timeout; a stuck/looping llama.cpp inference hangs the whole run with no stall detection or abort. Add a timeout/watchdog around inference. |
hr-wd-01 | open | high | hash-recursive-ai5.py:496 — `_read_window_into`'s `f.read()` (and the walk's `scandir`/`stat`) have no timeout or stall detection; a file on a hung NFS/removable mount blocks a worker thread indefinitely while progress only reflects completed batches — no heartbeat or auto-abort. Add per-file I/O stall detection or document the limitation. | stall/liveness
lq-wd-01 | open | med | link_queue.py:2025 — `Popen` (both `_spawn_exec_proc` and `_spawn_shell_proc:1988`) starts children without `start_new_session`; `_arm_command_timeout` (:1905) only terminate()/kill()s the direct child, so grandchildren (yt-dlp→ffmpeg, aria2c) keep the inherited stdout pipe open, the reader never sees EOF, and the worker hangs past the timeout. Use `start_new_session=True` and `os.killpg` on timeout. | timeout can't reap the process tree
mkp-wd-01 | open | med | minikeypad.py:1290,1315,225 — probe threads set `_io_busy=True` then call libusb (`usb.core.find`/claim/`still_connected`) with no timeout/watchdog; a stuck libusb call leaves `_io_busy` True forever, silently wedging all writes and polls. Bound the probe with a deadline, force-reset `_io_busy`, and surface the stall. | write path has WRITE_TIMEOUT_MS; connect/enumerate paths don't
oze-watch-01 | open | med | organize_by_extension.py:1837 — `_drain_futures` calls `wait(futures, return_when=FIRST_COMPLETED)` with no timeout, and `_run_moves` (1902-1914) never times out an individual move; a single `os.link`/`shutil.copy2`/`os.replace` hung on a stalled NFS/SMB mount stalls the whole run indefinitely with no heartbeat or abort. Add `wait(..., timeout=)` + stall detection or `fut.result(timeout=)` with a watchdog log. | liveness/stall detection
rf-wd-01 | open | med | relocate_folder.py:702 — `shutil.copytree`, `verify_copy`, and `_sha256` have no timeout/heartbeat/stall detection; a hung network mount (NFS/CIFS) blocks the migration indefinitely with no progress signal or abort. Add a progress-stall watchdog or per-op timeout for these long-running transfers/scans. | no-stall-detection

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-plat-01 | open | low | dedupl_numpy.py:22 — `mmap.mmap(..., prot=mmap.PROT_READ)` uses the POSIX-only `prot=` keyword and `PROT_READ`; on Windows this errors (needs `access=`). Document Linux-only support or branch on `os.name`. | POSIX-only primitive
hr-plat-04 | open | low | hash-recursive-ai5.py:563 — `_hash_file_windows` references `os.O_NOFOLLOW | os.O_CLOEXEC` unconditionally; both attributes are absent on Windows (AttributeError at hash time) and there is no platform guard or documented POSIX-only contract. Guard with `getattr(os, "O_NOFOLLOW", 0)` / `getattr(os, "O_CLOEXEC", 0)` or document the POSIX-only requirement. | cross-OS portability / POSIX-only primitive
lq-plat-10 | open | med | link_queue.py:2603 — `LogSink._open_locked` passes `os.O_NOFOLLOW`, which is Unix-only; on Windows accessing `os.O_NOFOLLOW` raises AttributeError, caught by the broad `except Exception`, so the log file silently never opens despite the module advertising Windows support. Guard with `getattr(os, "O_NOFOLLOW", 0)`. | POSIX-only primitive on a cross-OS surface
rf-plat-01 | open | low | relocate_folder.py:239 — `os.O_DIRECTORY` is used unguarded while `O_NOFOLLOW` uses `getattr(os, "O_NOFOLLOW", 0)`; on a platform lacking `O_DIRECTORY` this raises `AttributeError` at `_source_identity_fd` instead of degrading. Wrap it with the same `getattr(os, "O_DIRECTORY", 0)` fallback for consistency. | inconsistent-guard
rdv3-plat-01 | open | low | remove-deduplv3.py:104 — `_survivor` hardcodes `"/"` for the basename split (`p.rfind("/")`); Windows `\` paths compute a wrong basename length, changing survivor selection. Document the POSIX-only assumption or use `os.sep`-aware logic. | tool targets Linux filename bytes

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cache-01 | open | med | import_events.py:1270 — `get_llm` builds the LRU key from `_llm_file_identity` (stat) BEFORE `ensure_models_exist` (:1295) downloads the file; on a lazy first download the key uses the absent-file sentinel `(None,)`, then a later call re-keys on the real (mtime,size) and reloads the multi-GB model a second time. Compute identity after `ensure_models_exist`. | double model load within one run
ie-cache-02 | open | med | import_events.py:2411 — `_get_paddle_ocr` caches engines in `_PADDLE_OCR` keyed by (lang,device) with no size cap/LRU (unlike `_LLM_CACHE`/`_FILE_SHA256_CACHE`); the default 6-language chain leaves 6+ heavy engines resident for the process. Add a cap/eviction. | reset hook exists; cap+invalidation missing
ie-cache-03 | open | low | import_events.py:2496 — `_TESSERACT_PATH_CACHE` has no reset hook (every other cache does) and permanently caches a negative (`None`) result, so tesseract installed after the first miss is never re-detected in-process. Add a reset and/or don't cache the miss. | sticky negative + no invalidation
ie-cache-04 | open | low | import_events.py:2807 — the `llm_text` stage-cache key omits any hash of SYSTEM_PROMPT/USER_PROMPT, so editing a prompt serves stale cached responses unless `STAGE_CACHE_VERSION` is bumped manually. Fold a prompt digest into the key. |

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:36 — `hash_idx = line_starts[:,None] + np.arange(32)` materializes an (n_lines,32) int64 index matrix (n_lines×256 bytes, 8× the gathered uint8 data — the real peak); use `np.arange(32, dtype=np.int32)` (or `as_strided`) to quarter it. | distinct from dnp-perf-01 (byte copy) and dnp-rel-01 (bounds)
hr-mem-01 | open | med | hash-recursive-ai5.py:1721 — `dump_pending` accumulates (digest, alias-paths) for every successfully hashed candidate inode and is only drained in the finally (:1895); with `--hashes-file` active (the default) this materializes all candidate paths in RAM, defeating the streaming design for large trees. Flush/evict incrementally. | pairs with hr-rob-02
ie-mem-02 | open | low | import_events.py:2067 — `_image_messages` reads the whole image file and base64-encodes it in memory with no size bound before the vision call; a multi-GB image is fully materialized. Bound image size like `_read_text` bounds text. |
ie-mem-03 | open | med | import_events.py:2433 — each cached PaddleOCR engine in `_PADDLE_OCR` holds hundreds of MB; a multilingual auto chain materializes all of them concurrently with no ceiling (peak-memory risk). Cap/evict — pairs with ie-cache-02. |

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-adapt-01 | open | med | dedupl_numpy.py:36 — hash width is hardcoded as `np.arange(32)` + `.view("S32")`, silently assuming 32-char MD5 hex; a SHA-1/SHA-256 hash file is mis-grouped with no error. Derive the width from the first line or expose it as a constant/flag. | magic-number / hardcoded assumption
mkp-adapt-01 | open | low | minikeypad.py:1146,1296,1364,1370 — timing knobs are hardcoded inline magic numbers (drain interval 120, connection-poll 1000, status-clear 2500 ×2, window geometry 1440x880/1024x768 at 802-803); hoist to module-level named constants like `WRITE_TIMEOUT_MS`. | magic-number
rf-adapt-01 | open | low | relocate_folder.py:743 — `_DISK_SPACE_HEADROOM` (1.05) and `_SHA256_RETRY_ATTEMPTS` (line 1368, =2) are hardcoded constants with no env/CLI override, unlike the env-tunable `RELOCATE_STALE_PID_WARN_AT`; expose them via env accessors so operators can tune headroom/retries without a code edit. | magic-number

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-cfg-01 | open | low | deduplicate-by-namev3.py:36 — `BLOCK_THRESHOLD` and `BLOCK_ROWS` are hardcoded memory-tuning knobs with no CLI/env override; expose as `--block-rows`/`--block-threshold` or env vars with documented defaults. |
lq-cfg-01 | open | low | link_queue.py:336 — `command_timeout_seconds` (the hung-download safety cap) plus `immediate_worker_count`, `immediate_queue_maxsize`, `queue_render_limit`, `seq_of_sweep_gap` have no UI surface; `_SettingsTabs._build_dispatcher` exposes only sleep/workers/cooldown/max-per-domain/output-folder. Surface `command_timeout_seconds` at least. | safety-relevant timeout is hand-edit-YAML-only
rf-cfg-01 | open | low | relocate_folder.py:2120 — `main` hardcodes `logging.basicConfig(level=INFO)` with no `--verbose`/`--quiet`/`-v` flag or env knob; a root migration tool gives operators no way to raise to DEBUG or mute INFO. Add a verbosity flag mapping to log level. | every other runtime knob is tunable

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-api-01 | open | med | organize_by_extension.py:2236,2265 — `BUCKET_SIZE` is a mutable module global reassigned by `main()` via `global`; `organize()` exposes no `bucket_size` param, so library callers can't set it and the mutation is process-global and never restored (leaks across successive `organize()`/test calls in one process). Thread `bucket_size` through `organize()`/`BucketManager`. | config discoverability: --bucket-size has no per-call accessor
rdv3-api-01 | open | low | remove-deduplv3.py:137 — exit codes 2 (OSError) and 3 (UnicodeDecodeError, line 150) are part of the CLI contract but documented nowhere in `--help` or the docstring; note them in the argparse epilog. | callers scripting this can't branch reliably

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-cli-01 | open | low | bookmark-tidy.py:1382-1386 — `--llm-context`, `--llm-gpu-layers`, `--llm-max-tokens`, `--llm-batch-size` have no `help=` text (unlike other flags), so `--help` shows them undocumented. Add help strings. |
dnp-cli-01 | open | low | dedupl_numpy.py:18 — usage text is printed to stdout (should be stderr) on the error path, and the script hand-rolls arg handling with no `--help`; migrate to argparse for a consistent CLI surface. | error output on wrong stream
hr-cli-01 | open | low | hash-recursive-ai5.py:1683 — `args.jobs = max(1, args.jobs)` only lower-clamps; `-j 100000` spawns that many walk threads and a `ThreadPoolExecutor(max_workers=100000)`, exhausting threads/FDs. Add a sane upper clamp (e.g. multiple of cpu_count). | resource exhaustion / DoS
ie-cli-01 | open | low | import_events.py:3589 — `--timezone` is not validated at parse time; an invalid IANA name only raises inside `_apply_default_tz` per ICS file, surfacing as per-file failures instead of one upfront error. Validate with `ZoneInfo` in an argparse `type`. |
rf-cli-01 | open | low | relocate_folder.py:2155 — `--recover --dry-run` prints "would recover <backup> -> <source>" unconditionally, even when no backup exists or it's a symlink/regular file; gate the message on `_orphaned_backup(source)` like `recover()` checks. | docs-vs-behavior on dry-run surface

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-dep-01 | open | low | hash-recursive-ai5.py:44 — top-level `import blake3` is unguarded; a missing third-party dep aborts with a raw ImportError traceback instead of an actionable message. Wrap in try/except ImportError -> friendly `sys.exit`. | dependability / graceful degradation

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-doc-01 | open | low | deduplicate-by-namev3.py:16 — docstring says a single N×N call is fine "for N≤30 K" before row-blocking, but `BLOCK_THRESHOLD = 4000` triggers blocking at N>4000; reconcile the 30K narrative with the 4000 threshold. |
hr-doc-01 | open | low | hash-recursive-ai5.py:1730 — comment claims the dump is "line-buffered so … a complete file on an abrupt exit", but no dump line is written during hashing (all writes happen in the finally at :1895); correct the comment or make writes incremental. | ties to hr-rob-02
lq-doc-01 | open | low | link_queue.py:20 — module docstring says legacy `~/.link_queue_config.json` is migrated on first run, but `LEGACY_CONFIG_FILE_NAME="link_queue_config.json"` resolves via `_resolve_state_path` to the XDG/script dir, never `~/.link_queue_config.json`; correct the docstring to the actual location. | docs-vs-behavior drift
rf-doc-02 | open | low | relocate_folder.py:7 — module docstring usage example invokes `sudo python relocate.py …`, but the script is `relocate_folder.py`; the documented command as written won't run. Update the example to the real filename. | docs-vs-behavior drift

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-ux-01 | open | low | hash-recursive-ai5.py:78 — default `--hashes-file=hashes.txt` writes/appends a persistent dump into the current working directory on every run with no opt-in, silently polluting cwd. Make the dump opt-in or default off. | Laws of UX (least surprise)

## accessibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-a11y-01 | open | low | minikeypad.py:1165 — the selected physical key is distinguished only by red background (`COL_KEY_SEL`) and mapped keys only by blue background (`COL_KEY_MAPPED`); color-only cues fail for color-blind users. Add a relief/border or text marker. | Laws of UX / WCAG use-of-color

## product engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-prod-01 | open | low | deduplicate-by-namev3.py:41 — `WORD_TOKENS = ("xxx", "monography")` ships a domain-specific whole-word removal default ("monography" looks like a leftover from one dataset), silently mutating every user's cleaned strings. Make the shipped default empty (opt-in) or document why universal. |

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-i18n-01 | open | low | deduplicate-by-namev3.py:58 — user-facing diagnostics ("warning: threshold…", "dropped … empty cleaned line(s)") are hardcoded English with no catalog; route through a message layer if localization is in scope. |
mkp-i18n-01 | open | low | minikeypad.py — all user-facing strings (labels, logs, status) are hardcoded English with no translation catalog; applicable only if localization is a goal. | marginal for single-file util

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data structure

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-ds-01 | open | low | deduplicate-by-namev3.py:138 — the `counts` dict is fully redundant with `line_nums` (`counts[k] == len(line_nums[k])`, and line 168's `cnts[i] > 1` is `len(line_nums[...]) > 1`); drop `counts` and derive from `line_nums` to remove parallel-state drift. |

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-unused-01 | open | low | dedupl_numpy.py:43 — `uniq` unpacked from `np.unique` is never used (only `inverse`/`counts` are); bind to `_`. Ruff/pyright don't flag tuple-unpack unused, so it survives lint. |
dnv3-unused-01 | open | low | deduplicate-by-namev3.py:52 — `_WORD_RE` and `cleanup`'s default `replacements`/`word_re` params are dead: `cleanup` is only called at line 144 with explicit args, so the module-level compile and defaults never run. Inline or delete. |
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

## unused functions/methods

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-unf-01 | open | low | bookmark-tidy.py:146,151,156,160,172,179 — `config_read`, `bookmark_read`, `links_extract`, `links_cleanup`, `filter_youtube`, `get_current_unix_epoch` have no production caller (grep-proven: only tests/fuzz); dead BeautifulSoup-era path superseded by `NetscapeBookmarkParser`. Delete them and their tests. | grep-proven test-only
bt-unf-02 | open | low | bookmark-tidy.py:901 — `tidy_bookmarks` is called only from tests; `_run` reimplements the same dedup+categorize logic inline, so the real entry point never uses it. Wire `_run` to call it or delete. | test-only symbol (see bt-dup-01)
hr-unf-01 | open | low | hash-recursive-ai5.py:170 — `threaded_walk` has no production caller (main uses `iter_threaded_walk`); grep shows only tests/ invoke it. Keep as documented API or delete with its tests. | grep-proven test-only, same class as hr-leg-01
oze-unf-02 | open | low | organize_by_extension.py:2229 — `parse_args()` has no production caller (main calls `build_parser().parse_args()` directly at :2238); only tests import it. Keep as documented API or inline. | grep-proven test-only

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-api-01 | rejected | med | import_events.py — ModelConfig() direct construction with a custom model_path still inherits the default SHA pins, while from_args disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
