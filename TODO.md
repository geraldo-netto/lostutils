# TODO

## Open

### `bookmark-tidy.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `deduplicate-by-namev3.py`

| id | status | severity | effort | description |
|---|---|---|---|---|
| dnv3-rel-70 | open | low | low | deduplicate-by-namev3.py:201 — [reliability / correctness] Input is opened as `utf-8`, not `utf-8-sig`, and `strip()` keeps U+FEFF, so a BOM-prefixed file reports `<BOM>abc;abc;1` as a distance-1 pair instead of the distance-0 collision; remove-deduplv3.py already BOM-detects, so the two disagree. Open with `utf-8-sig` (still `surrogateescape`). |
| dnv3-api-60 | open | medium | low | deduplicate-by-namev3.py:219 — [API contract & compatibility] Make data records distinguishable from comment records when a cleaned input starts with `#`; input lines `#abc` and `#abd` emit `#abc;#abd;1`, which consumers following the documented instruction to skip `#` lines discard. Define escaping or an unambiguous comment marker and cover both emitters. |
| dnv3-rob-60 | open | low | low | deduplicate-by-namev3.py:235 — [robustness / recovery] Flush output inside the BrokenPipeError guard and silence stdout after an early reader close; a real subprocess whose reader closes after one byte currently exits 120 with `Exception ignored while flushing sys.stdout`. `_emit_results` returns False but main ignores it. |
| dnv3-mem-80 | open | medium | medium | deduplicate-by-namev3.py:310 — [memory and cpu management] Stream matching column indices per row instead of materializing all matching pairs and converting both coordinate arrays to Python lists. With 1,000 four-digit strings at threshold 7 and a no-op writer, tracemalloc reports a 40,737,758-byte peak for a 1,000,000-byte distance matrix; dense 2,000-row blocks amplify this as input grows. Bound temporary output-index memory by row/block width and preserve pair ordering. |

### `hash-recursive-ai5.py`

| id | status | severity | effort | description |
|---|---|---|---|---|
| hr-test-90 | open | low | low | hash-recursive-ai5.py:231 — [test coverage] Cover the no-config path through `_record_dump_failure` (return at line 233) via a meaningful hash-dump failure scenario. The full unit/integration and fuzz suites cover 3/4 statements (75%), failing `tests/check_function_coverage.py --minimum 80`; this unchanged helper is the sole remaining repository-wide function coverage failure. |
| hr-obs-80 | open | medium | low | hash-recursive-ai5.py:2564 — [observability / operability] Include requested hash-dump failures in the final exit status; setup, write, patch, and close errors only warn and never reach _run_exit_code. Reproduced --hashes-file with a missing parent: no dump is created but the CLI exits 0. Track dump failure through finalization and return nonzero while preserving useful duplicate stdout. Three pillars: logs expose the error, process health falsely reports success. |

### `import_events.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `link_queue.py`

| id | status | severity | effort | description |
|---|---|---|---|---|
| lq-time-92 | open | medium | medium | link_queue.py:1485 — [time & scheduling correctness] Persist active domain cooldowns along with retry state. Reproduced a failed attempt with the default 300-second cooldown, saved state, and restored a fresh Dispatcher: attempts remained 1 but the same URL was immediately claimable because `_cooldown_until` starts empty and is absent from the snapshot. Save a restart-safe expiry and reconstruct the remaining monotonic wait; cover restart before expiry, after expiry, and unrelated-domain dispatch. |
| lq-rob-92 | open | high | medium | link_queue.py:1600 — [robustness / recovery] Preserve the original state file after a read/parse failure instead of treating failure as an empty queue that can be saved. Reproduced an unterminated YAML template with a recoverable URL: restore warned, then the ordinary save path replaced the source with empty queue/in_flight/immediate lists. Track state-load failure, surface it in the GUI, and refuse destructive replacement until recovery or an explicit reset; cover malformed YAML and a transient read error followed by shutdown. |
| lq-rel-92 | open | medium | low | link_queue.py:2186 — [reliability / correctness] Exclude URL userinfo from the domain scheduling key. `_domain_of` lowercases the entire netloc, so `https://alice:secret@example.test/a` and `https://bob:secret@example.test/b` occupy different domain buckets: both were claimed with `max_per_domain=1`, and a cooldown on one does not block the other. Build the key from normalized hostname and the intended port policy without credentials; cover different credentials sharing both a cap and a cooldown. |
| lq-stop-92 | open | medium | medium | link_queue.py:2837 — [robustness / recovery] Make the nonblocking output loop observe shutdown independently of the command timeout. Reproduced a child that starts a detached descendant inheriting stdout: after setting stop_event and terminating the tracked process group, the worker remained blocked reading the descendant's open pipe with timeout=0; `_iter_output_chunks` checks only its deadline, and `_shutdown` releases the state lock after a bounded join even if the worker survives. Close/cancel the reader on shutdown while preserving the interrupted queue item; cover an inherited pipe held outside the tracked process group with timeout disabled and enabled. |
| lq-mem-92 | open | medium | medium | link_queue.py:2854 — [memory and cpu management] Bound unterminated subprocess output instead of retaining every decoded fragment until newline/EOF. A 16 MiB newline-free stream consumed 33,568,716 bytes of traced allocations even in silent mode; a continuing child can exhaust GUI memory before its command timeout. Drain raw chunks in silent mode and cap/split retained text in logging modes; cover long newline-free output with bounded memory and continued pipe draining. |
| lq-conc-92 | open | medium | medium | link_queue.py:3911 — [distributed systems] Acquire the single-instance lock before constructing ConfigStore or sweeping its temporary files. Startup currently loads config before Dispatcher acquires the state lock; reproduced a second startup deleting the first instance's active config tempfile in `_sweep_temp_siblings`, then being rejected by the state lock, while the first save failed with FileNotFoundError and retained the old attempts setting. Put config cleanup/load/write inside the same instance-ownership boundary; cover overlapping first-instance save and rejected second-instance startup. |
| lq-ui-92 | open | medium | low | link_queue.py:5089 — [UI / UX] Move existing Treeview rows when their desired order changes after a fast retry. `_apply_queue_rows` assumes surviving rows never reorder, but a claim/failure/requeue can happen between coalesced refreshes: changing pending order from A,B to B,A left the widget showing A,B with row numbers 2,1. Reconcile row positions as well as values while preserving selection; cover a retry that completes before its running row is painted. |
| lq-ui-93 | open | low | medium | link_queue.py:5702 — [wiring gaps] Make the queue's Re-run action perform an explicit rerun. Selection resolves only currently pending/running items, and `_on_queue_rerun` routes each through the same duplicate guard, so a selected queued item with unchanged routing always reports `re-queued 0 item(s)`; completed items have no selectable row. Implement deliberate rerun semantics separately from ordinary paste deduplication, and count only accepted submissions; cover pending and running selections. |
| lq-plat-92 | open | medium | low | link_queue.py:5968 — [platform / UI / UX] Reject or disable Shell mode in the Windows protocol editor and context-menu toggle. The editor labels it `cmd.exe /c` and accepts `echo {url_quoted}` with shell=True, but `_spawn_shell_proc` always refuses shell=True on Windows, so a configuration accepted by the UI consumes the queued retry budget without launching. Reproduced the editor/runner mismatch with the Windows platform branch selected; make the UI explain the unsupported mode and cover both editor save and toggle. |
| lq-val-92 | open | medium | low | link_queue.py:944 — [input validation / data integrity] Treat a non-mapping YAML configuration as a load failure. A file containing `- command: custom-handler` was silently ignored, left `load_error=None`, and was overwritten by defaults on `save()`, bypassing the advertised configuration recovery guard. Validate the parsed root before merging and retain the source until repaired; cover sequence/scalar roots and verify subsequent saves leave their original bytes intact. |
| lq-test-91 | open | low | low | tests/test_link_queue.py:4592 — [test coverage] Rename the second `test_pick_next_item_skips_seq_of_sweep_under_threshold` so it does not shadow the distinct test at line 4106 during pytest collection; retain and collect both regression scenarios. |

### `minikeypad.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `organize_by_extension.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `relocate_folder.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `remove-deduplv3.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

## Blocked / Deferred

| id | status | severity | effort | description |
|---|---|---|---|---|
| ie-arch-50 | blocked | low | high | import_events.py:1464-4142 — [architecture/modularity] Migrate model lifecycle and extraction behind OmniTensor's planned accelerator-only `event-extraction` workload only after that external workload proves file-format, privacy, partial-result, JSON/ICS, offline, and exit-code parity. |
| lq-conflict-91 | blocked | low | low | link_queue.py:2739 — [documentation] `_run_item` says the default command timeout is 0 (off), while `DEFAULT_COMMAND_TIMEOUT_SECONDS` at link_queue.py:609 and `DEFAULT_CONFIG` use six hours. Confirm the intended default and align this comment with the configuration and timeout-default tests; the retry work preserves the current six-hour behavior. |
| mkp-obs-70 | blocked | medium | low | minikeypad.py:315-317 — [observability / operability] `_report_missing_backend` dedupes only `NoBackendError`; a device present but not permitted (Linux without the udev rule, macOS kernel-owned HID) makes `is_kernel_driver_active` and `get_active_configuration` raise `USBError [Errno 13]` on every 1 s poll, logging two lines forever (reproduced: 5 polls, 10 lines), so the 1000-line GUI log cap scrolls away every other message within ~8 min. Dedupe by error text and log on change, like the backend case at lines 275-278. Blocked: this deduplication requirement (TODO.md:159) conflicts with tests/test_minikeypad.py:2656-2672, which explicitly requires identical real USB errors to be logged on every poll. Decide whether to retain per-poll logging, deduplicate on change, or scope deduplication to permission errors, then align the test. |
| oze-conflict-90 | blocked | low | low | organize_by_extension.py:782 — [legacy / deprecation] The `oze-unused-50` retention instruction (TODO.md:92) keeps `list_files` solely for compatibility, while AGENTS.md:35 prohibits compatibility-only wrappers. Decide to delete the wrapper and migrate maintained callers, or explicitly retain it for a purpose beyond compatibility; align the function and rejected finding before closing this contradiction. |
| rf-link-80 | blocked | medium | medium | relocate_folder.py:1042 — [API contract & compatibility] Decide how relocation handles valid relative symlinks that escape the source tree: src/link pointing to ../neighbor becomes dangling after a successful migration. relocate_folder.py:1658-1659 and tests/test_relocate_pinned_verification.py:95-101 deliberately require literal target preservation, including dangling links, so automatic rebasing changes that contract. Choose rebasing with revised verification or a preflight warning/refusal policy that preserves literal targets; document the outcome alongside README.md:94-101. |

## Rejected / Won't fix

| id | status | severity | effort | description |
|---|---|---|---|---|
| bt-dead-50 | wont_fix | low | — | bookmark-tidy.py:1754-1758 — Keep the private helper's defensive missing-model guard; direct callers get a typed `UserError` even though the production caller already guards it. |
| dnv3-i18n-01 | wont_fix | low | — | deduplicate-by-namev3.py:54-62,162-261 — No translation catalog for this developer CLI unless localization becomes a product requirement; hard-coded English is accepted. |
| hr-plat-05 | rejected | low | — | hash-recursive-ai5.py:2028-2041 — Text-mode hash-dump offsets remain valid on Windows because `tell()`/`seek()` use compatible cookies and the patched fixed-width digest contains no newline. |
| ie-obs-51 | rejected | low | — | import_events.py:1000-1022 — `_redirect_stdout_stderr` does not swallow other-worker logs because startup logging owns a duplicated real stderr descriptor. |
| ie-mem-01 | rejected | medium | — | import_events.py:32 — Keep native/default LLM context sizing (`0`) per explicit product decision; users can lower memory with `--llm-context`. |
| ie-api-01 | rejected | low | — | import_events.py:671-730 — Keep direct `ModelConfig` custom-path/default-pin behavior; the public CLI factory already clears default pins, and hand-constructed mismatched configs are not a user flow. |
| lq-plat-02 | rejected | low | — | link_queue.py:209-234 — Keep inherited Windows profile ACL behavior; it supplies the write-integrity goal that POSIX mode `0700` provides without adding a `pywin32`/`icacls` dependency. |
| lq-plat-03 | rejected | low | — | link_queue.py:2485-2533 — Keep `start_new_session=True`; CPython accepts and ignores it on Windows, while process-tree shutdown uses `taskkill /T`. |
| mkp-i18n-01 | wont_fix | low | — | minikeypad.py:1045-1510 — No GUI translation catalog unless localization becomes a product requirement; hard-coded English is accepted. |
| oze-unused-50 | wont_fix | low | — | organize_by_extension.py:749-756 — Keep `list_files` as the explicit compatibility wrapper; tests and external importers may rely on its list-returning public surface while runtime uses streaming `_iter_files`. |
| rdv3-plat-01 | rejected | low | — | remove-deduplv3.py:1-24 — POSIX-shell output is the utility's explicit deliverable; adding Windows command dialects would expand scope and review risk. |
