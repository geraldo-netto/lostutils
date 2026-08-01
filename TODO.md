# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md); cache/build paths are excluded. Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

Latest full rescan: 2026-08-01 — 10 root scripts / 22,430 lines (no `.sh` files in root); cache/build paths (`__pycache__`, `.ruff_cache`, `.pytest_cache`, `.complexipy_cache`, `.hypothesis`, `coverage.json`, `.coverage`) excluded. Pinned gates re-run clean: `ruff@0.15.22 check *.py` → all checks passed; `pyright@1.1.411 --pythonpath /usr/bin/python3.12 *.py` → 0 errors/warnings/informations; `lizard -l python -C 11 *.py` → 0 warnings (highest per-file average CCN 3.7). Neither tool is on `PATH` in this workspace — invoke via `uvx ruff@0.15.22` / `uvx pyright@1.1.411`. One Python 3.12 POSIX-fork warning is recorded as `bt-mt-01`.

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
bt-sec-01 | open | low | bookmark-tidy.py:1139-1158 — `--auto-install-llama` runs a live `pip install` of a PyPI package into the running interpreter (arbitrary code execution via the package/its build); the pinned version limits but does not remove supply-chain risk. Default to failing with install instructions and document the trust boundary. | STRIDE Elevation of privilege / supply-chain; live pip call at 1148-1151
hr-sec-07 | open | med | hash-recursive-ai5.py:641-667 — hash opens never compare descriptor `(dev, ino, size)` with walked identity or recheck stability after reads, so path swaps/mutation can bind a digest to stale identity. Pass expected identity; `fstat` before/after hashing and reject changes. | STRIDE Tampering; TOCTOU attack tree
lq-sec-04 | open | high | link_queue.py:2004-2013,2366-2388 — `{url_quoted}` uses POSIX `shlex.quote` (`_resolve_command`, and `_extra_shell` at 2037-2047), but Windows `shell=True` invokes `cmd.exe`, where single quotes do not protect `&`/`\|`; crafted URLs can inject commands. Disallow shell mode on Windows or implement a correct platform shell contract with adversarial tests. | STRIDE Tampering/Elevation of privilege; OWASP ASVS command injection
mkp-sec-01 | open | low | minikeypad.py:74-85,1899-1904 — opt-in auto-install executes live PyPI package/build code inside the current interpreter environment. Default to external install instructions or explicitly document the trust boundary. | STRIDE Elevation of privilege / supply chain
mkp-sec-02 | open | low | minikeypad.py:821-843 — profile save uses predictable `<path>.tmp` with ordinary `open`, following/truncating a pre-existing symlink and racing concurrent saves. Use same-directory `mkstemp`/`NamedTemporaryFile(delete=False)` with exclusive creation, restrictive mode, fsync, then replace. | STRIDE Tampering/Information disclosure; CWE-59
oze-sec-04 | open | high | organize_by_extension.py:1230-1242,1621,1704 — cross-device fallback reopens `source` by name with `shutil.copy2` after an earlier `lstat`; a swapped symlink can copy data outside root and then unlink the wrong entry. Open with `O_NOFOLLOW`, pin dev/inode, copy from fd, and identity-check before unlink. | STRIDE Information disclosure/Tampering; TOCTOU
rf-sec-04 | open | high | relocate_folder.py:2242-2255 — source fd pins the original inode, but copy/verify still read `plan.source` by path; an attacker can swap in B for copying, restore A before a final identity check, then pass verification and swap. Traverse/copy from the pinned directory fd (`openat`/`dir_fd`) and separately identity-check the path immediately before a no-replace swap. | STRIDE Tampering; TOCTOU; a final path reassert alone is insufficient
rf-sec-50 | open | low | relocate_folder.py:1774-1778,1915-1920 — `_backup_target` and `_create_symlink` both gate on `_path_taken(...)` then call plain `os.rename`, leaving the classic check-then-rename window; `_create_symlink`'s comment even claims "stdlib `os` doesn't expose renameat2". The module already ships `_rename_noreplace` (2114-2147, ctypes `renameat2(RENAME_NOREPLACE)`) and uses it in `recover` (2192). Route both sites through it and drop the stale comment. | STRIDE Tampering; TOCTOU; fix is in-file, no new dependency

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-gov-01 | open | low | bookmark-tidy.py:850 — verbose duplicate logging prints full URLs, including `user:password@host`. Redact URL userinfo before logging. | sensitive-log minimization
lq-gov-50 | open | low | link_queue.py:5654-5665 — `build_parser` puts the resolved `CONFIG_FILE` / `STATE_FILE` absolute paths in the `--help` epilog, so `link_queue.py --help` output pasted into an issue discloses `/home/<user>/…`. import_events.py already solves this shape with `_display_path` (ie-gov-01); apply the same home-relative rendering. | data governance — private absolute path in user-facing output

## data integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-api-50 | open | med | hash-recursive-ai5.py:1231-1239 — `_encode_record_path` escapes only `\n`, `\r`, and the `@lostutils-json:` prefix, so a path with leading/trailing whitespace is emitted bare. remove-deduplv3.py:132 parses with `line.split(None, 1)`, which strips it, so the emitted `rm -f --` names a DIFFERENT path than the real file. Extend the JSON tagging to whitespace-edged paths. | verified: record `abc  leading-space.txt` → remove-deduplv3 nominates the nonexistent `leading-space.txt` as survivor and emits `rm -f -- plain.txt`; dedupl_numpy (fixed-offset parse) reads it correctly, so the two consumers disagree
mkp-di-50 | open | low | minikeypad.py:1844-1847 — `_write_all_done` sets `ambiguous=True` on a failed key but never clears it, so a key that later writes successfully via Write-all stays flagged forever. The single-key path clears it only incidentally, because `_record_pending_assignment` replaces the whole record. Clear the flag on an `ok` outcome. | data integrity — derived UI state not invalidated on the success path

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-scal-50 | open | med | import_events.py:3325-3354 — `_render_pdf_image_paths` caps pages only when `pdf_vision_max_pages > 0`, and the shipped default `PDF_VISION_MAX_PAGES = 0` means "all pages". A large scanned PDF writes one 150-DPI PNG per page into the `tempfile.TemporaryDirectory` (3541) — unbounded total bytes, always under TMPDIR. Per-page pixels are capped by `PDF_RENDER_MAX_PIXELS`, but nothing bounds the aggregate. Add a byte budget or a non-zero default page cap. | scalability — temp-disk growth tracks input size with no ceiling

## N+1 / call efficiency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-conc-50 | open | med | hash-recursive-ai5.py:1414-1420 — `_stage2_tail` calls `_retry_tail_alias` for any item whose tail is None, including items `_run_stage` never hashed because the SIGINT cooperative cancel fired. After Ctrl-C, stage 2 therefore performs unbounded synchronous re-hashing of every multi-alias candidate on the main thread with no `cancel_event` check. `_bucket_stage1_heads` (1352-1353) has the right shape — it skips keys absent from `head_by_key`. | concurrency — cancellation not honoured on the recovery path; asymmetric with stage 1

## multithreading

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-mt-50 | open | med | link_queue.py:2922,2957,2965 — `_trigger_failure_cooldown` and `_maybe_inter_item_sleep` run on worker threads and call the injected `_get_failure_sleep` / `_get_sleep`, which reach `LinkQueueApp._get_int_setting` (4403-4411) and read a `tk.StringVar` via `var.get()` — a Tcl call from a non-main thread, exactly what `_safe_after` (4951-4982) exists to prevent. `_maybe_inter_item_sleep` repeats it every 0.25s per worker. The `except Exception` fallback absorbs a `RuntimeError`, but a Tcl build without thread support can block instead of raising. Read these knobs from `self.config` (already live-shared) instead of the widget vars. | multithreading — cross-thread Tk access contradicting the file's own documented concurrency contract

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-dist-02 | open | med | import_events.py:1158-1189 — shared model downloads use one stable `.<name>.part` without a process lock; concurrent runs can truncate, append, or rename the same partial. Add a per-model cross-process lock around resume/download/verify/publish with stale-lock recovery. | distributed systems — shared-resource coordination
ie-dist-01 | open | med | import_events.py:3990-4019 — `_output_lock` creates `<output>.lock` with O_CREAT\|O_EXCL and writes pid, but a hard kill skips unlink and the pid is never read; the next run fails forever until manual removal. On collision read the pid and reclaim only when that process is dead. | distributed systems — idempotent re-run / stale-lock recovery

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-dep-50 | open | med | organize_by_extension.py:1981 — `sqlite3.connect("")` opens an on-disk temporary database (TMPDIR / SQLITE_TMPDIR), not `:memory:`, and neither `_spooled_plan_pairs`, `organize`, nor `main` catches `sqlite3.Error`. A full or read-only temp filesystem aborts the run with a raw `sqlite3.OperationalError` traceback; a duplicate source path handed to the public `plan_moves` raises `IntegrityError` the same way (`source BLOB PRIMARY KEY`). Catch and degrade, and document the temp-disk requirement in `--help`/README. | dependability — hard failure on an undeclared external dependency
rf-dep-50 | open | low | relocate_folder.py:1303 — `_assert_complete_inventory_match` uses the same `sqlite3.connect("")` on-disk temp database with no `except sqlite3.Error`. It runs at the very end of verify, so a full temp filesystem converts a fully-copied, fully-verified migration into a traceback (and, per rf-rob-50, strands the target). | dependability — same shape as oze-dep-50; fix both or neither

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---

## architecture / modularity / SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---

## system design

id | status | effort | description | notes
--- | --- | --- | --- | ---

## decoupling

id | status | effort | description | notes
--- | --- | --- | --- | ---

## composition

id | status | effort | description | notes
--- | --- | --- | --- | ---

## reliability / correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-rel-50 | open | med | hash-recursive-ai5.py:1423-1448 — `_stage3_hash` has no sibling-alias retry, unlike stage 1 (`_retry_head_alias`, 1268, hr-rel-02) and stage 2 (`_retry_tail_alias`, 1286, hr-rel-30). A hardlinked inode whose representative becomes unreadable between stage 2 and stage 3 is silently dropped from its confirmed duplicate group even though a readable sibling exists. | reliability — recovery path present at two of three stages
ie-rel-50 | open | low | import_events.py:2904-2911 — `subprocess.run(..., text=True)` passes no `errors=`, so OCR output that is not decodable in the active locale raises `UnicodeDecodeError`. That is outside the caught `(subprocess.TimeoutExpired, OSError)` set at 2912, so it escapes `_ocr_with_tesseract` and turns a recoverable OCR miss into a counted whole-file extraction failure. Pass `errors="replace"`. | reliability — over-narrow except around a decode boundary
rf-rel-50 | open | med | relocate_folder.py:1309-1314 — `_assert_complete_inventory_match` runs `src EXCEPT tgt UNION ALL tgt EXCEPT src`. SQLite evaluates compound operators strictly left to right, so this reduces to `((src EXCEPT tgt) UNION ALL tgt) EXCEPT src` == `tgt \ src` — target-only extras only. The last check before the source is deleted therefore detects the case `verify_copy`'s own docstring (1243-1244) says cannot arise, and misses the missing-entry case it exists for. Parenthesise each side, or run two separate EXCEPT queries. | reliability — verified in sqlite3: a source entry absent from the target returns None; a target-only extra is returned. Per-entry `_verify_file` still catches missing files, so this is a broken backstop rather than the sole protection

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-rob-50 | open | low | dedupl_numpy.py:96-103 — `_write_paths` catches `BrokenPipeError` and returns, but never redirects fd 1, so the interpreter's shutdown flush re-raises and the run ends with `Exception ignored in: <_io.TextIOWrapper name='<stdout>'…> BrokenPipeError` and exit 120. remove-deduplv3.py:237-256 already has the `dup2`-devnull fix (`_silence_stdout_after_broken_pipe`); port it. | robustness — verified: `dedupl_numpy.py big.txt \| head -2` → rc=120 + ignored-exception noise, while remove-deduplv3.py and deduplicate-by-namev3.py both exit 0 cleanly under the same test
mkp-rob-50 | open | med | minikeypad.py:988-1000,1563-1564 — `probe_timeout` abandons the stalled probe thread and `_replace_stalled_device` installs a fresh `KeypadDevice`. The old device's libusb handle is never disposed and the orphan thread is never joined, so each stall leaks one thread plus one USB handle with no ceiling. Track abandoned devices and dispose them once their probe returns. | robustness / watchdog — the abandonment is deliberate and logged, but its resource cost is unbounded
rf-rob-50 | open | med | relocate_folder.py:1739-1753 — `_fsync_tree` (the first statement of `atomic_swap`) has no error handling, so one `os.open`/`os.fsync` failure propagates. By then `_copy_and_verify` has returned, so its cleanup no longer applies, and `_execute_migration`'s except only runs `_cleanup_created_dirs` (rmdir, stops at the first non-empty). The fully-copied, fully-verified target survives, and the next run dies in `copy_tree` (827-832) with "target already exists … may be a stale partial target" — which it is not. Guard the fsync walk and/or make the message distinguish complete from partial targets. | robustness — post-verify failure leaves an unrecoverable-looking state

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-state-50 | open | med | minikeypad.py:1804-1818 — `_write_all` replays every saved `(layer, key_id)`, but `build_download_reports` (599-625) emits the `0xA1` layer-switch command only when `ReportID != 0`. On a device that negotiated report ID 0, all three layers are written to whichever layer is currently selected, silently overwriting each other. Confirm the intended protocol against the decompiled C# original before changing behaviour; if confirmed, refuse Write-all for multi-layer profiles on a report-ID-0 device. | state machine integrity — NEEDS CONFIRMATION against the original firmware protocol; do not "fix" on inference alone

## test coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## test / fuzz coverage

id | status | effort | description | notes
--- | --- | --- | --- | ---

## ruff (lint)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `ruff check *.py` under pinned Ruff 0.15.22 reports no issues across all root files (rescan 2026-08-01)._

## pylance / pyright (type check)

id | status | effort | description | notes
--- | --- | --- | --- | ---
_clean — `pyright==1.1.411 --pythonpath /usr/bin/python3.12 *.py` reports 0 errors, warnings, or informations (rescan 2026-08-01)._

## observability / operability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-obs-50 | open | med | import_events.py:3786-3801 — `_abandon_stalled_results` logs an ERROR, sets `stop_event`, and returns; `_run_file_workers` (3952) then returns the PARTIAL event list and `_run_main` writes it and returns 0, because a wedged (never-raised) LLM call never increments `extraction_failure_count()`. A truncated run is indistinguishable from a complete one by exit code. `_handle_model_unavailable` (4365-4375) already returns 2 for the same class of partial result — mirror it. | observability — silent-failure audit: the documented give-up path has no user-visible symptom outside the log
mkp-obs-50 | open | low | minikeypad.py:74-85 — `_pip_install` sends pip's stdout/stderr to `DEVNULL` and swallows every exception, so the only signal a user gets is "Automatic install failed. Install manually: …". Capture and surface the last few lines of pip output. | observability — the actionable diagnostic is discarded at the point of failure
oze-obs-50 | open | low | organize_by_extension.py:2219 — `_wait_for_move_futures` raises `RuntimeError` when the move stage stalls past `MOVE_MAX_STALL_SECONDS`, but `main` (2632-2681) catches only `KeyboardInterrupt`. The documented watchdog abort therefore reaches the user as an unhandled traceback rather than a clean message plus exit code. | observability — a designed failure mode surfaces as a crash

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:72 — `hash_idx = line_starts[:, None] + np.arange(hash_width)` materializes an `(n_lines, hash_width)` int64 index matrix in addition to the gathered bytes; process record starts in bounded chunks so peak auxiliary memory is capped. | memory scaling — an int32 `arange` alone does not help because the int64 line starts upcast the result
ie-mem-02 | open | low | import_events.py:2330-2337 — `_image_messages` reads the whole image file and base64-encodes it in memory with no size bound before the vision call; a multi-GB image is fully materialized. Bound image size like `_read_text` bounds text. | memory bound
oze-mem-01 | open | med | organize_by_extension.py:1036,1095 — `BucketManager._reserved_names` accumulates one entry per moved filename and is pruned only on `release()` (skip path); successfully moved files are never removed, so it grows O(files). Drop reserved-name sets at bucket saturation or clear per-source after success. | memory / caching strategy — cache with no success-path invalidation

## data structure

id | status | effort | description | notes
--- | --- | --- | --- | ---

## adaptability

id | status | effort | description | notes
--- | --- | --- | --- | ---

## business / design patterns / DDD

id | status | effort | description | notes
--- | --- | --- | --- | ---

## configuration discoverability

id | status | effort | description | notes
--- | --- | --- | --- | ---
rf-cfg-50 | open | low | relocate_folder.py:671-686,876-888,1683-1696 — three runtime knobs (`RELOCATE_STALE_PID_WARN_AT`, `RELOCATE_DISK_SPACE_HEADROOM`, `RELOCATE_SHA256_RETRY_ATTEMPTS`) each have a default, a typed accessor, and validation, but none appears in `_build_parser`'s help (2569-2621), the module docstring, or README.md. Document them in one of those surfaces. | configuration discoverability — undocumented coverage is the only leg of the AGENTS.md checklist these knobs fail

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-api-50 | open | med | dedupl_numpy.py:85-92 — the path slice is emitted verbatim, so the `@lostutils-json:<json>` escaping that hash-recursive-ai5.py:1238 produces for line-breaking paths (and remove-deduplv3.py:77-84 decodes) is printed as a literal token instead of a path. Decode the prefix like remove-deduplv3 does. | verified: `dedupl_numpy.py` prints `@lostutils-json:"/tmp/a\nb"` where remove-deduplv3 emits `rm -f -- '/tmp/a<newline>b'`; the two consumers of one format disagree

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-cli-50 | open | low | bookmark-tidy.py:1609-1610 — `--llm-batch-size` help reads "llama.cpp prompt batch size", but the value is the number of bookmarks per categorization batch (`_assign_categories`, 918-931) and is never passed to llama.cpp (`n_batch` is not set anywhere). Reword the help. | CLI / option integrity — help text describes a different knob than the flag controls
ie-cli-50 | open | med | import_events.py:4208-4212 — `_positive_int` exists but is wired to `--max-ics-bytes` only. `--workers`, `--llm-max-tokens`, `--llm-cache-size`, `--llm-context`, `--llm-main-gpu`, `--pdf-vision-pages`, `--pdf-vision-dpi` and `--stage-cache-max-entries` all use bare `type=int` and are silently clamped in `ModelConfig.from_args` (721-752), so `--workers -5` runs at 1 while the user believes concurrency is constrained. Route them through the existing validator. | CLI / option integrity — silent clamping instead of a parse-time error, with the validator already in the file
mkp-cli-50 | open | low | minikeypad.py:1893-1894 — `--no-auto-install` is declared with `help=argparse.SUPPRESS`, so it is invisible in `--help` even though it silently overrides both `--auto-install-pyusb` and `MINIKEYPAD_AUTO_INSTALL=1`. The module docstring (24-26) documents the opt-ins but not the override. Unhide it or document it. | CLI / option integrity — hidden flag with precedence over two documented ones
oze-cli-50 | open | med | organize_by_extension.py:2632-2681 — `main` never propagates `_RunStats.skipped` / `.partial` to the exit code, so a run that skipped files or left a duplicate behind after a `PartialMoveError` still exits 0; only `KeyboardInterrupt` yields non-zero. hash-recursive-ai5.py `_run_exit_code` (2099-2108) is the shape to copy. | CLI / API contract — scripted callers cannot detect partial success

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-ux-50 | open | low | import_events.py:4419-4423 — `_run_main` creates a missing input `directory` with `mkdir(parents=True)` and returns 0, so a mistyped path silently creates an empty tree instead of reporting the typo. Create only when the path was defaulted, or require an explicit opt-in. | product engineering / design thinking — the recovery path for "no input" masks the far more likely "wrong input"
lq-ux-50 | open | low | link_queue.py:4551-4560 — "Clear Queue" discards every pending item and rewrites the state file with no confirmation, while deleting a single protocol or mapping (4748) does prompt via `messagebox.askyesno`. Add the same confirmation, scaled to the item count. | UI / UX — Jakob's Law: users carry the expectation set by this same app's other destructive action (and by every other queue UI) that bulk destruction confirms

## accessibility

id | status | effort | description | notes
--- | --- | --- | --- | ---

## product engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## design thinking

id | status | effort | description | notes
--- | --- | --- | --- | ---

## i18n

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-i18n-01 | open | low | dedupl_numpy.py:18 — user-facing strings ("Usage:", "equal files:") hardcoded, not routed through a catalog. Acceptable for a dev CLI; flagged for category completeness. | i18n — no translation seam; low value for a single-locale dev tool
dnv3-i18n-01 | open | low | deduplicate-by-namev3.py:58 — user-facing diagnostics ("warning: threshold…", "dropped … empty cleaned line(s)") are hardcoded English with no catalog; route through a message layer if localization is in scope. | i18n
mkp-i18n-01 | open | low | minikeypad.py:1050 — all GUI strings (labels, logs, status) are hardcoded English with no translation catalog; add a message catalog only if localization enters scope. | marginal for single-file util
rdv3-i18n-01 | open | med | remove-deduplv3.py:37 — `_ = gettext.gettext` routes every user string, but no domain is ever bound (no `bindtextdomain`/`textdomain`/`gettext.translation(...).install`), so `_()` always returns the source English text — translation is impossible without a code edit. Bind a domain+localedir or document the plumbing as intentionally inert. | i18n — hook present but no catalog ever consulted

## purpose

id | status | effort | description | notes
--- | --- | --- | --- | ---

## release & deploy engineering

id | status | effort | description | notes
--- | --- | --- | --- | ---

## wiring gaps

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-wire-50 | open | low | hash-recursive-ai5.py:1536-1546 — `_emit_stage2_groups` has no production caller: `find_duplicate_groups` (1660-1674) goes stage 2 → `_stage3_hash` → `_emit_stage3_groups` directly. Only tests/test_hash_recursive.py:175-176 reach it, and its helper `_publish_stage2_digests` is reachable only through it. Delete both, or wire the stage-2 composite `head:tail` digests into `on_composite` if the dump was meant to record them. | wiring gaps — decide delete vs wire; stage 3 already publishes a full-file digest for the same keys, so deletion is likely correct

## unused code

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-dead-50 | open | low | bookmark-tidy.py:1652-1653 — `_categorizer_from_args` raises `UserError("missing --model for LLM categorization")`, but its only caller (`_run`, 1675-1679) is guarded by `if args.model is not None`, so the branch is unreachable in production. Record keep (as an API guard for direct callers) or delete. | unused code — dead branch behind a caller-side guard
lq-dead-50 | open | low | link_queue.py:3097-3099,3279,3299 — `LogSink._open_fail_first_t` and `_write_fail_first_t` are initialised, set on failure, and cleared on success, but never read anywhere. `_note_open_failure`'s docstring (3266-3269) promises "a follow-up message" carrying the duration that does not exist. Either emit the follow-up warning or delete both attributes and the docstring claim. | unused code — dead state plus a docstring describing behaviour that was never implemented
oze-dead-50 | open | med | organize_by_extension.py:2025-2101 — `_preplan_resolve_collisions` and its three exclusive helpers `_resolved_plan_pairs` (2062), `_needed_plan_dirs` (2075) and `_planning_collision_renames` (2088) are the pre-spool planning path; `plan_moves` (1948) uses `_spooled_plan_pairs` instead. Delete all four (~75 lines) and retarget the tests. | unused code / legacy — grep: `_preplan_resolve_collisions` prod=1 (its own def) / test=11; the three helpers prod=2 (def + the call from the dead function) / test=0

## unused functions/methods

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-unused-50 | open | low | organize_by_extension.py:717 — `list_files` is a public compatibility wrapper over `_iter_files` with no production caller (prod=1, its own def; test=12). Record keep (documented back-compat surface) or delete and update the tests to call `_iter_files`. | unused functions — decision needed: keep / wire / delete

## legacy / deprecation

id | status | effort | description | notes
--- | --- | --- | --- | ---

## plugin extensibility

id | status | effort | description | notes
--- | --- | --- | --- | ---

## Audit picks deliberately rejected

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-mem-01 | rejected | med | import_events.py:32 — DEFAULT_LLM_CONTEXT_SIZE=0 delegates context sizing to llama.cpp/model metadata, which may allocate the full native window and increase KV-cache memory versus the prior 64k default. | User explicitly requested letting the model use its default/native context without hardcoding the size; lower it with --llm-context when RAM pressure matters.
ie-api-01 | rejected | med | import_events.py:667 — `ModelConfig()` direct construction with a custom model path still inherits default SHA pins, while `from_args` disables pins for custom paths. Move digest selection into a constructor/factory invariant so programmatic callers do not get surprising hash mismatches. | User judged the SHA pin non-critical: the only public entry point (from_args/CLI) already nulls pins for custom paths, so the mismatch is reachable only by hand-constructing ModelConfig with a default pin + custom path — not a real user flow. Don't re-pick.
ie-obs-51 | rejected | low | import_events.py:947-962 — `_redirect_stdout_stderr` dup2's process fds 1/2 to /dev/null while one worker loads the LLM, which looked like it would swallow every other worker's log output. | Not a defect: `_configure_logging` (4450-4465) attaches the root handler to `os.fdopen(os.dup(2))` captured at startup, so the handler keeps writing to the real stderr through the redirect. Verified by reading both call sites; don't re-pick.
