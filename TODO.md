# TODO

## Open

| id | status | severity | effort | description |
|---|---|---|---|---|
| ulf-test-01 | open | medium | high | update-linux-firmware.sh:1-422 — [test/release engineering] Add hermetic failure-path/integration tests and CI `shellcheck`; the privileged updater currently has no automated coverage and the workflow only gates Python files. |
| ulf-doc-01 | open | medium | low | update-linux-firmware.sh:8,37-45 / README.md:1-196 — [documentation/product engineering] Document updater prerequisites, root/filesystem effects, backups, recovery, supported Linux families, and environment knobs; add it to the quick reference and fix the stale `update-firmware-unified.sh` usage name. |
| ulf-sec-01 | open | high | medium | update-linux-firmware.sh:23,105-107,186-230 — [security; STRIDE Tampering/Denial of service] Create and verify the persistent cache as a private, owned, non-symlink directory before root execution; a local user can precreate `/var/tmp/firmware-update-cache` and planted output symlinks that root `curl -o` follows. |
| ulf-input-01 | open | high | low | update-linux-firmware.sh:33-34,137-146 — [input validation/configuration] Validate overridden space thresholds as non-negative integers and fail closed; nonnumeric or negative values make the `[` checks false and silently disable the pre-install ENOSPC guard. |
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
