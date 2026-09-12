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
| lq-conflict-80 | blocked | medium | low | link_queue.py:1704 — [documentation] Resolve the startup-policy conflict: _restore_queue_from_state says users can clear restored non-idempotent commands before execution, but link_queue.py:3983-3984 starts workers first and explicitly consumes restored items immediately. Decide whether startup must pause for review or whether immediate auto-resume remains the contract and the clearing claim must be corrected; update implementation/docs/tests consistently. |
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
