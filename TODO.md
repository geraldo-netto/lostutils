# TODO

## Open

| id | status | severity | effort | description |
|---|---|---|---|---|
| bt-sec-01 | open | medium | low | bookmark-tidy.py:1237-1255 — [security; STRIDE Elevation of privilege / supply chain] Remove in-process `pip install` or add an explicit code-execution trust warning before `--auto-install-llama` runs pinned third-party package/build code. |
| dnp-mem-01 | open | medium | medium | dedupl_numpy.py:79-83 — [memory/CPU] Process record starts in bounded chunks; broadcasting `line_starts[:, None] + np.arange(hash_width)` materializes an `(n_lines, hash_width)` int64 matrix plus gathered bytes. |
| hr-sec-07 | open | high | medium | hash-recursive-ai5.py:621-685 — [security; STRIDE Tampering, TOCTOU attack tree] Bind each hash to the walked identity: pass expected `(device, inode, size)`, compare `fstat` before and after reading, and reject mutation/path swaps. |
| ie-test-50 | open | medium | low | import_events.py:1959-1976 — [test coverage] Add focused invalid-hour/minute/second and three-column time cases; `_split_time_columns` has 78.57% statement coverage (11/14), so the required 80% per-function CI gate fails. |
| ie-dep-50 | open | medium | low | import_events.py:3420-3432 / requirements-ci.txt:8 — [dependency/security; STRIDE Denial of service] Require `pypdf>=6.15.0` and refresh the hashed lock; pinned 6.14.2 is vulnerable to crafted-font memory/runtime exhaustion (PYSEC-2026-3655 and PYSEC-2026-3656) during `extract_text`. |
| lq-sec-04 | open | high | high | link_queue.py:2100-2108,2133-2149,2467-2490 — [security; STRIDE Tampering/Elevation of privilege, OWASP ASVS command injection] Stop using POSIX `shlex.quote` for Windows `cmd.exe` shell mode; reject shell mode there or implement and adversarially test a correct host-shell quoting contract. |
| lq-dead-50 | open | low | low | link_queue.py:3203-3405 — [unused code] Either emit the promised recovery-duration warning or delete `_open_fail_first_t` and `_write_fail_first_t`; both are written/cleared but never read. |
| lq-obs-50 | open | medium | medium | link_queue.py:3232-3268 — [observability/operability] Record and surface `LogSink` liveness plus its last unexpected result; the writer loop swallows every unhandled flush/drain exception and `stop()` does not report a timed-out writer, so batches can disappear silently. |
| lq-gov-50 | open | low | low | link_queue.py:5827-5831 — [data governance] Render config/state paths home-relative in `--help`; resolved absolute paths disclose the user's home path when help output is shared. |
| mkp-sec-01 | open | medium | low | minikeypad.py:81-103,2116-2133 — [security; STRIDE Elevation of privilege / supply chain] Remove in-process auto-install or add an explicit code-execution trust warning before the CLI/environment opt-in runs pinned PyPI package/build code. |
| mkp-sec-02 | open | medium | medium | minikeypad.py:914-935 — [security; STRIDE Tampering/Information disclosure, CWE-59] Replace predictable `<profile>.tmp` writes with an exclusive same-directory tempfile, restrictive mode, fsync, and atomic replace so saves cannot follow/truncate a planted symlink. |
| oze-sec-04 | open | high | high | organize_by_extension.py:1741-1862 — [security; STRIDE Information disclosure/Tampering, TOCTOU] Pin cross-device source identity with a no-follow descriptor, copy from that descriptor, and identity-check again before unlink; current path-based compare/copy/unlink can follow a swapped source. |
| oze-dead-50 | open | low | medium | organize_by_extension.py:2234-2309 — [unused code/legacy] Delete `_preplan_resolve_collisions` and its three exclusive helpers, then retarget tests; production planning uses `_spooled_plan_pairs`, leaving this approximately 75-line path test-only. |
| rf-sec-50 | open | high | medium | relocate_folder.py:1883-1906,1991-2047,2241-2274 — [security; STRIDE Tampering, TOCTOU] Route backup/link publication through a genuinely no-replace rename on every supported POSIX host; both sites still gate then call clobbering `os.rename`, and the existing helper falls back to the same unsafe operation off Linux. |
| rf-sec-04 | open | high | high | relocate_folder.py:2361-2391,2594-2608 — [security; STRIDE Tampering, TOCTOU] Traverse, copy, and verify from the pinned source directory descriptor; holding its fd while reopening `plan.source` by path still permits swap-copy-restore of attacker-selected content. |
| rdv3-i18n-01 | open | low | medium | remove-deduplv3.py:29-38,87-325 — [i18n] Bind an application domain/localedir and ship catalogs, or remove the inert gettext wrapper; `gettext.gettext` currently has no project catalog to consult. |
| ulf-test-01 | open | medium | high | update-linux-firmware.sh:1-422 — [test/release engineering] Add hermetic failure-path/integration tests and CI `shellcheck`; the privileged updater currently has no automated coverage and the workflow only gates Python files. |
| ulf-doc-01 | open | medium | low | update-linux-firmware.sh:8,37-45 / README.md:1-196 — [documentation/product engineering] Document updater prerequisites, root/filesystem effects, backups, recovery, supported Linux families, and environment knobs; add it to the quick reference and fix the stale `update-firmware-unified.sh` usage name. |
| ulf-sec-01 | open | high | medium | update-linux-firmware.sh:23,105-107,186-230 — [security; STRIDE Tampering/Denial of service] Create and verify the persistent cache as a private, owned, non-symlink directory before root execution; a local user can precreate `/var/tmp/firmware-update-cache` and planted output symlinks that root `curl -o` follows. |
| ulf-input-01 | open | high | low | update-linux-firmware.sh:33-34,137-146 — [input validation/configuration] Validate overridden space thresholds as non-negative integers and fail closed; nonnumeric or negative values make the `[` checks false and silently disable the pre-install ENOSPC guard. |
| ulf-cli-01 | open | low | low | update-linux-firmware.sh:37-45 — [CLI/UI; Jakob's Law] Support `-h`/`--help` with exit 0 and reserve nonzero status for invalid options; the standard help flag is currently reported as an error. |
| ulf-dep-01 | open | low | low | update-linux-firmware.sh:55,211-220 — [dependency] Check the decompressor selected by the discovered suffix; `gzip` is used but never preflighted, while `xz` is required even when only a `.tar.gz` release is chosen. |
| ulf-dist-01 | open | high | medium | update-linux-firmware.sh:105-124 — [distributed systems/concurrency] Acquire one process lock before touching shared cache, backup, firmware, stamp, or initramfs state; concurrent manual/cron runs can corrupt a resumed download and interleave privileged installs. |
| ulf-sec-02 | open | medium | low | update-linux-firmware.sh:152-177 — [security; STRIDE Tampering, rollback attack tree] Refuse a discovered release older than the root-owned installed stamp unless a separate explicit downgrade option is supplied; signed archives authenticate content but the unsigned index provides no freshness/rollback protection. |
| ulf-watch-01 | open | medium | low | update-linux-firmware.sh:153,188,197,229-230 — [watchdog/dependability] Bound index/signature/keyserver/download stalls with connect and low-speed timeouts while preserving resumable downloads; every network step can currently hang forever. |
| ulf-rel-01 | open | medium | medium | update-linux-firmware.sh:173-177,391-404 — [reliability/state integrity] Persist and retry a pending initramfs rebuild separately from the installed-release stamp; after rebuild failure, later unattended runs report “nothing to do” and never repair early-boot firmware. |
| ulf-rob-01 | open | high | high | update-linux-firmware.sh:343-404 — [robustness/recovery] Make installation rollback-safe or automatically restore the verified backup on pre-stamp failure; interrupted/failed `rsync`, stale-variant removal, or stamp writes leave a mixed firmware tree and some paths emit no recovery instructions. |

## Blocked / Deferred

| id | status | severity | effort | description |
|---|---|---|---|---|
| ie-arch-50 | blocked | low | high | import_events.py:1464-4142 — [architecture/modularity] Migrate model lifecycle and extraction behind OmniTensor's planned accelerator-only `event-extraction` workload only after that external workload proves file-format, privacy, partial-result, JSON/ICS, offline, and exit-code parity. |

## Rejected / Won't fix

| id | status | severity | effort | description |
|---|---|---|---|---|
| bt-dead-50 | wont_fix | low | — | bookmark-tidy.py:1754-1758 — Keep the private helper's defensive missing-model guard; direct callers get a typed `UserError` even though the production caller already guards it. |
| dnv3-i18n-01 | wont_fix | low | — | deduplicate-by-namev3.py:54-62,162-261 — No translation catalog for this developer CLI unless localization becomes a product requirement; hard-coded English is accepted. |
| dnp-i18n-01 | wont_fix | low | — | dedupl_numpy.py:21-30,228-259 — No translation catalog for this developer CLI unless localization becomes a product requirement; hard-coded English is accepted. |
| hr-plat-05 | rejected | low | — | hash-recursive-ai5.py:2028-2041 — Text-mode hash-dump offsets remain valid on Windows because `tell()`/`seek()` use compatible cookies and the patched fixed-width digest contains no newline. |
| ie-mem-01 | rejected | medium | — | import_events.py:32 — Keep native/default LLM context sizing (`0`) per explicit product decision; users can lower memory with `--llm-context`. |
| ie-api-01 | rejected | low | — | import_events.py:671-730 — Keep direct `ModelConfig` custom-path/default-pin behavior; the public CLI factory already clears default pins, and hand-constructed mismatched configs are not a user flow. |
| ie-obs-51 | rejected | low | — | import_events.py:1000-1022 — `_redirect_stdout_stderr` does not swallow other-worker logs because startup logging owns a duplicated real stderr descriptor. |
| lq-plat-02 | rejected | low | — | link_queue.py:209-234 — Keep inherited Windows profile ACL behavior; it supplies the write-integrity goal that POSIX mode `0700` provides without adding a `pywin32`/`icacls` dependency. |
| lq-plat-03 | rejected | low | — | link_queue.py:2485-2533 — Keep `start_new_session=True`; CPython accepts and ignores it on Windows, while process-tree shutdown uses `taskkill /T`. |
| mkp-i18n-01 | wont_fix | low | — | minikeypad.py:1045-1510 — No GUI translation catalog unless localization becomes a product requirement; hard-coded English is accepted. |
| oze-unused-50 | wont_fix | low | — | organize_by_extension.py:749-756 — Keep `list_files` as the explicit compatibility wrapper; tests and external importers may rely on its list-returning public surface while runtime uses streaming `_iter_files`. |
| rdv3-plat-01 | rejected | low | — | remove-deduplv3.py:1-24 — POSIX-shell output is the utility's explicit deliverable; adding Windows command dialects would expand scope and review risk. |
