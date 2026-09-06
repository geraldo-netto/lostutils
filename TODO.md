# TODO

## Open

| id | status | severity | effort | description |
|---|---|---|---|---|
| dnp-val-60 | open | medium | low | dedupl_numpy.py:140 — [input validation / command safety] Reject empty paths, embedded NULs, and unencodable JSON surrogates before emitting decoded paths; `"first\u0000second"` produces two operands under `--print0`, `"\ud800"` raises an uncaught UnicodeEncodeError, and a CRLF record with an empty path is accepted. Validate decoded and raw paths and report malformed records consistently. |
| dnv3-api-60 | open | medium | low | deduplicate-by-namev3.py:219 — [API contract & compatibility] Make data records distinguishable from comment records when a cleaned input starts with `#`; input lines `#abc` and `#abd` emit `#abc;#abd;1`, which consumers following the documented instruction to skip `#` lines discard. Define escaping or an unambiguous comment marker and cover both emitters. |
| dnv3-rob-60 | open | low | low | deduplicate-by-namev3.py:235 — [robustness / recovery] Flush output inside the BrokenPipeError guard and silence stdout after an early reader close; a real subprocess whose reader closes after one byte currently exits 120 with `Exception ignored while flushing sys.stdout`. `_emit_results` returns False but main ignores it. |
| hr-obs-60 | open | medium | medium | hash-recursive-ai5.py:2230 — [observability / operability] Count unresolved hash failures without subtracting benign attempts already removed by alias recovery; recovered ENOENT plus an independent EACCES currently leaves stage1_errors=1 and hash_skipped_vanished=1, so `_real_hash_error_count` and the process exit code both become 0. Track final per-inode outcomes and test mixed recovery/failure. Logs/health pillars; silent-failure audit: an incomplete scan reports successful exit. |
| hr-di-60 | open | high | low | hash-recursive-ai5.py:684 — [data integrity] Compare modification/change timestamps as well as device, inode, and size across each hash read; same-size writes during hashing currently pass the final identity check and produce stale duplicate confirmations. A real-BLAKE3 reproduction overwrites eight-byte `original` with eight-byte `modified` after reading but before final fstat; the pipeline still groups it with an unchanged original and reports no error. Preserve internal mtime_ns/ctime_ns snapshots and reject detectable mid-read changes. |
| dnp-doc-60 | open | high | low | README.md:64 — [documentation] Replace the destructive `dedupl_numpy.py --print0` example with a non-destructive consumer or explicitly explain that it deletes every copy; `group_duplicates` selects all members of each repeated-hash group, so the documented `xargs -0 rm -f --` pipeline preserves no survivor. Also correct line 59: hash width is inferred, not fixed at 32 bytes. |
| rf-test-65 | open | low | low | relocate_folder.py:1008 — [test coverage] Add focused coverage for _refuse_pinned_specials: the full suite plus fuzz reaches 0% statement coverage, below the enforced 80% per-function gate. Verify special-file discovery aborts strict pinned copies before publication. |
| rdv3-cx-61 | open | low | low | remove-deduplv3.py:226 — [code complexity] Split _emit_remove_commands; Complexipy measures cognitive complexity 17, above the repository limit of 10. Preserve survivor selection, case warnings, and removal output. |
| rdv3-di-60 | open | high | medium | remove-deduplv3.py:234 — [data integrity] Collapse or conservatively reject path aliases before selecting a survivor; records for `file.txt` and `./file.txt` currently promise to save `file.txt` while emitting `rm -f -- ./file.txt`, deleting that same directory entry. Cover normalized relative/absolute spellings and directory aliases without conflating distinct hardlink entries. |
| rdv3-sec-60 | open | high | low | remove-deduplv3.py:247 — [security] Encode filenames as single-line text in saving and case-conflict comments; decoded newline-bearing paths currently escape the `#` comment and inject shell commands despite quoting removal operands. STRIDE Tampering / Elevation of privilege; safe reproduction with `rm` stubbed executed a `printf AUDIT_MARKER` filename suffix through both line 247 and `_emit_case_variant_warning` line 222. Add shell-execution regression coverage for both comment paths. |

## Blocked / Deferred

| id | status | severity | effort | description |
|---|---|---|---|---|
| ie-arch-50 | blocked | low | high | import_events.py:1464-4142 — [architecture/modularity] Migrate model lifecycle and extraction behind OmniTensor's planned accelerator-only `event-extraction` workload only after that external workload proves file-format, privacy, partial-result, JSON/ICS, offline, and exit-code parity. |
| rf-test-51 | blocked | low | low | tests/test_relocate_folder.py:1181-1190 — [test/reliability] Decide whether `_check_cross_device` must remain controllable through a global `Path.stat` monkeypatch or the legacy test should use the documented `stat_fn` injection seam; both expectations cannot hold under the current implementation, and the legacy test fails the full suite. |

## Rejected / Won't fix

| id | status | severity | effort | description |
|---|---|---|---|---|
| bt-dead-50 | wont_fix | low | — | bookmark-tidy.py:1754-1758 — Keep the private helper's defensive missing-model guard; direct callers get a typed `UserError` even though the production caller already guards it. |
| dnp-i18n-01 | wont_fix | low | — | dedupl_numpy.py:21-30,228-259 — No translation catalog for this developer CLI unless localization becomes a product requirement; hard-coded English is accepted. |
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
