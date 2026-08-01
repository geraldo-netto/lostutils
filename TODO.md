# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md); cache/build paths are excluded. Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

Latest full rescan: 2026-08-01 — 10 root scripts / 22,430 lines (no `.sh` files in root); cache/build paths (`__pycache__`, `.ruff_cache`, `.pytest_cache`, `.complexipy_cache`, `.hypothesis`, `coverage.json`, `.coverage`) excluded. Neither lint tool is on `PATH` in this workspace — invoke via `uvx ruff@0.15.22` / `uvx pyright@1.1.411`.

Latest verification (after the data-integrity → state-machine-integrity fix batch): `ruff@0.15.22 check *.py` → all checks passed; `pyright@1.1.411 --pythonpath /usr/bin/python3.12 *.py` → 0 errors/warnings/informations; `lizard -l python -C 11 *.py` → 0 warnings; CI-equivalent suite 2,096 passed / 1 skipped / 6 subtests plus 103 explicitly collected fuzz cases; total coverage 97.53%; all 1,340 functions/methods meet the 80% function gate. The Python 3.12 POSIX-fork warning is gone — `bt-mt-01` shipped, and the only remaining `fork` use is an explicit, locally-suppressed test seam.

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

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---

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

## dependability

id | status | effort | description | notes
--- | --- | --- | --- | ---

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

## robustness / recovery

id | status | effort | description | notes
--- | --- | --- | --- | ---

## state machine integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
mkp-state-50 | open | low | minikeypad.py:1820-1844 — `_warn_if_layers_collapse` now warns before a multi-layer Write-all on a reportID-0 device, but the write still proceeds and the layers still overwrite each other on the hardware. | state machine integrity — PARTIAL: the warning shipped (see `_warn_if_layers_collapse`); the hard refusal is deferred because whether reportID 0 really means "this firmware cannot switch layers" is a property of the original C# protocol that is still unconfirmed, and refusing would break a legitimate program-one-layer-at-a-time workflow. Confirm against the decompiled original, then decide refuse vs keep-warning.

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

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-cli-50 | open | low | bookmark-tidy.py:1609-1610 — `--llm-batch-size` help reads "llama.cpp prompt batch size", but the value is the number of bookmarks per categorization batch (`_assign_categories`, 918-931) and is never passed to llama.cpp (`n_batch` is not set anywhere). Reword the help. | CLI / option integrity — help text describes a different knob than the flag controls
ie-cli-50 | open | med | import_events.py:4208-4212 — `_positive_int` exists but is wired to `--max-ics-bytes` only. `--workers`, `--llm-max-tokens`, `--llm-cache-size`, `--llm-context`, `--llm-main-gpu`, `--pdf-vision-pages`, `--pdf-vision-dpi` and `--stage-cache-max-entries` all use bare `type=int` and are silently clamped in `ModelConfig.from_args` (721-752), so `--workers -5` runs at 1 while the user believes concurrency is constrained. Route them through the existing validator. | CLI / option integrity — silent clamping instead of a parse-time error, with the validator already in the file
mkp-cli-50 | open | low | minikeypad.py:1893-1894 — `--no-auto-install` is declared with `help=argparse.SUPPRESS`, so it is invisible in `--help` even though it silently overrides both `--auto-install-pyusb` and `MINIKEYPAD_AUTO_INSTALL=1`. The module docstring (24-26) documents the opt-ins but not the override. Unhide it or document it. | CLI / option integrity — hidden flag with precedence over two documented ones

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
