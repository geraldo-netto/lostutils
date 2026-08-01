# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md); cache/build paths are excluded. Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

Latest full rescan: 2026-08-01 — 10 root scripts / 22,430 lines (no `.sh` files in root); cache/build paths (`__pycache__`, `.ruff_cache`, `.pytest_cache`, `.complexipy_cache`, `.hypothesis`, `coverage.json`, `.coverage`) excluded. Neither lint tool is on `PATH` in this workspace — invoke via `uvx ruff@0.15.22` / `uvx pyright@1.1.411`.

Latest verification (after the multiplatform fix batch): `ruff@0.15.22 check *.py` → all checks passed; `pyright@1.1.411 --pythonpath /usr/bin/python3.12 *.py` → 0 errors/warnings/informations; `lizard -l python -C 11 *.py` → 0 warnings over 1,359 functions; CI-equivalent suite 2,221 passed / 1 skipped / 6 subtests plus 103 explicitly collected fuzz cases; total coverage 97.63%; all 1,355 functions/methods meet the 80% function gate. The Python 3.12 POSIX-fork warning is gone — `bt-mt-01` shipped, and the only remaining `fork` use is an explicit, locally-suppressed test seam.

Latest targeted rescan: 2026-08-01 — multiplatform sweep of all 10 root scripts against the AGENTS.md "multiplatform by design" rule. Checked POSIX-only module/API use, hardcoded POSIX paths, `os.rename`/`os.link`/`chmod` divergence, subprocess/signal portability and pid-liveness probes, path separators, newline translation on seek-patched files, home/config discovery, and filename legality. Six defects were found and fixed (`oze-plat-01/02/03`, `rf-plat-01`, `lq-plat-01`, `ie-plat-01` — see `git log`); four candidates were verified as non-defects and recorded under "Audit picks deliberately rejected", `lq-plat-02` among them once the Windows profile ACL was confirmed to already provide what its `0o700` buys on POSIX.

Second multiplatform rescan: 2026-08-01 — the same 10 root scripts swept again with a different reviewer, reading each file end to end rather than grepping for known API families. It found 22 further defects, now filling the `platform` table; none of them duplicate the first sweep's fixes or its four rejections. The line numbers and quoted code in every row were re-verified against the working tree before recording. Nothing has been fixed yet. Highest severity first: `hr-plat-06` (hash-recursive reports zero duplicates on any tree on Windows, silently, exit 0), `rdv3-plat-03` (a case-variant path pair on macOS/Windows makes the emitted script delete the only copy), `lq-plat-06` (every shipped default command fails on a stock Windows install), `oze-plat-05` and `oze-plat-04` (organize skips every file, or permanently re-skips case-variant names, off Linux). Two rows — `ie-plat-03` and `lq-plat-05` — are defects inside the first sweep's own `OpenProcess` fix and must be fixed together, since the standalone-script rule keeps that probe duplicated in both files. `dedupl_numpy.py` and `deduplicate-by-namev3.py` came back clean.

Exit codes are now documented for the three scripts whose contract changed: `organize_by_extension.py` (0/1/2/3), `import_events.py` (0/1/2), and `dedupl_numpy.py` gained `--print0`. Each is stated in both the parser epilog and README.

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

## watchdog

id | status | effort | description | notes
--- | --- | --- | --- | ---

## time & scheduling correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---

## platform

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-plat-06 | open | med | hash-recursive-ai5.py:331 — the walk sources `(size, st_dev, st_ino)` from `DirEntry.stat(follow_symlinks=False)`, but CPython documents `st_ino`/`st_dev`/`st_nlink` as always zero for `DirEntry.stat` on Windows; consumed at 344 and 1092-1107. | platform: the most severe finding of the sweep. On Windows every file is emitted as `(path, size, 0, 0)`, so `index_inodes` folds the entire tree into one inode `(0, 0)`, `size_collision_candidates` never satisfies `len(keys) >= 2`, and the run hashes nothing and reports zero duplicates with exit 0 on any tree — a silent total no-op with no warning. Secondary: `skip_ino` from `os.fstat` (2246) holds real values, so hr-log-04 never matches and the run's own dump is scanned and self-listed. Fix: re-`os.stat` the entry when the cheap stat reports `st_dev == 0 and st_ino == 0`. Confidence high
ie-plat-02 | open | low | import_events.py:2960 — `_resolve_tesseract_executable` demands an exact filename in the separator/absolute branch (`candidate.is_file() and os.access(candidate, os.X_OK)`) with no PATHEXT or `.exe` resolution. | platform: on Windows `--tesseract-path "C:\Program Files\Tesseract-OCR\tesseract"` — the natural spelling, and one the bare-name `shutil.which` branch would accept — fails `is_file()`, resolves to None, and OCR is dropped for the whole run with only a "Tesseract executable not found" warning despite the binary being installed. Fix: fall back to `shutil.which(str(candidate))` in that branch. Confidence med
ie-plat-04 | open | low | import_events.py:4452 — `_iana_timezone` maps `ZoneInfoNotFoundError` unconditionally to `invalid IANA timezone {value!r}`. | platform: Windows ships no system tz database, so stdlib `zoneinfo` needs the `tzdata` package; without it every valid name fails and `--timezone Europe/Lisbon` exits 2 claiming the name is invalid, sending the user to hunt a typo instead of a missing package. `icalendar>=6` pulls `tzdata` transitively but is a deliberately optional lazy import here, so a lean install hits this. Fix: when `ZoneInfo("UTC")` also fails, report the missing tz database instead. Confidence med
lq-plat-04 | open | low | link_queue.py:63 — the missing-PyYAML path calls `sys.stderr.write(...)` directly, while every other diagnostic in the file uses `print(..., file=sys.stderr)`. | platform: under `pythonw.exe` — the normal way to launch a Tkinter GUI on Windows without a console — `sys.stderr` is None, so this raises `AttributeError` instead of reaching the intended `sys.exit(2)`: an invisible traceback and exit 1 rather than the documented graceful exit. `print(..., file=None)` tolerates None, which is why only this one call site crashes. Violates the "degrade gracefully — never crash" clause. Confidence med
lq-plat-06 | open | low | link_queue.py:532 — every shipped default command template is `echo …` while the same protocol entries set `"shell": False` (table at 535-539; the same literal is the fallback at 857, 867, 942, 1498, 1662, 1677). | platform: `echo` is a cmd.exe builtin, not an executable, so with `shell=False` CreateProcess cannot resolve it. On a stock Windows install every link routed through any shipped default raises `FileNotFoundError` (WinError 2) in `_spawn_exec_proc`, caught at 2515 and logged as "command not found" with exit -1 — 100% of items fail out of the box, each arming the 5-minute domain cooldown, while the identical config works on Linux and macOS. Fix: pick the default per-OS (`cmd /c echo {url}`) or use a genuinely portable placeholder. Confidence high
lq-plat-07 | open | low | link_queue.py:2077 — `_extra_shell` lexes the mapped flag with unconditional POSIX-mode `shlex.split`, even though the exec path was routed through the host-aware `_split_command_template` by lq-plat-01 (`_extra_argv`, 2065). | platform: on Windows, backslash is an escape in POSIX mode, so a shell=True protocol whose token mapping carries a path flag (`--paths C:\dl`) is mangled to `C:dl` before it reaches the command line, and `_item_display` (2614) shows the mangled form too. Distinct from the open lq-sec-04, which is about the cmd.exe *quoting* dialect of `shlex.quote` — fixing sec-04 alone would not restore the eaten backslashes, but both feed the same shell string and should be fixed together. Confidence med
lq-plat-09 | open | low | link_queue.py:5608 — the shell-mode checkbox is labelled "Run via /bin/sh -c (enables pipes, redirects, &&, …)" unconditionally (docstrings at 2400 and 2462 state the same POSIX-only fact). | platform: on Windows `shell=True` executes through `%COMSPEC%` (cmd.exe), not `/bin/sh`. No crash — a truthfulness defect — but combined with the open lq-sec-04 it actively steers Windows users into writing sh syntax that cmd.exe mishandles. Fix: make the label name the system shell rather than a specific one. Confidence high
mkp-plat-01 | open | low | minikeypad.py:201 — `usb.core.find` sits under a bare `except Exception` (222) that logs the failure on every connection poll, with no special case for `usb.core.NoBackendError`. | platform: pyusb imports fine without libusb, which is the default state on Windows and macOS, so `find` raises `NoBackendError` once per second forever (`CONNECTION_POLL_MS`, 151/990) and the message never says libusb is what's missing. On Linux with no device attached `find` returns None and the same poll is silent, so the spam is platform-asymmetric: a noisy non-actionable degradation where the rule asks for a guided one. Fix: catch `NoBackendError` specifically, log one actionable message, stop retrying for that condition. Confidence high
mkp-plat-02 | open | low | minikeypad.py:1153 — the log pane passes `font=("TkFixedFont", 9)`, and 1165 passes `("TkDefaultFont", 10, "bold")`; these are Tk *named fonts*, valid only as a whole font spec, so inside a `(family, size)` tuple they are read as a nonexistent family. | platform: every OS substitutes its own default family, so the log pane is not monospace anywhere — Segoe UI on Windows, the system font on macOS, the fontconfig default on Linux — and which font you get differs per platform, silently defeating the fixed-width intent. The 1165 case degrades benignly but is the same latent misuse. Fix: `tkinter.font.nametofont("TkFixedFont").copy()` with the size overridden. Confidence med
mkp-plat-03 | open | med | minikeypad.py:1481 — all physical-key state feedback is carried by `tk.Button` `bg`/`relief` (buttons built at 1194 and 1206, repainted at 1472-1478), which native Aqua Tk does not render on button faces. | platform: on macOS `configure(bg=COL_KEY_SEL, relief="sunken")` changes nothing visible. Mapped keys still read because their description text is appended (1473), but *selected* changes only bg and relief — so the user cannot see which key is selected before assigning functions to it. Correct on Linux and Windows. Fix: carry the state through something Aqua renders (`highlightbackground`, a bordering frame, a ttk style map, or a text marker). Confidence med — Aqua ignoring button bg/relief is well documented, unverified on real hardware here
rf-plat-02 | open | low | relocate_folder.py:681 — `_read_comm` calls `Path(f"/proc/{pid}/comm").read_text()` with no `encoding=` and catches only `OSError`. | platform: an encoding assumption on this file's own Linux-only path, not a Windows gap. `comm` is arbitrary bytes — any process can set it via `prctl(PR_SET_NAME)` — and `read_text()` decodes with the locale encoding under strict errors, so invalid bytes raise `UnicodeDecodeError`, a `ValueError`, which escapes `_read_comm` through `find_open_file_holders` into `_check_no_open_files`: an unrelated process's odd name aborts an otherwise-valid migration with `FAILED` exit 1. The sibling `_staging_owner_pid` (2084) already gets this right with `encoding="ascii"` and `except (OSError, ValueError)`. Confidence med — the uncaught path is certain, the trigger rare

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:72 — `hash_idx = line_starts[:, None] + np.arange(hash_width)` materializes an `(n_lines, hash_width)` int64 index matrix in addition to the gathered bytes; process record starts in bounded chunks so peak auxiliary memory is capped. | memory scaling — an int32 `arange` alone does not help because the int64 line starts upcast the result

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

## API contract & compatibility

id | status | effort | description | notes
--- | --- | --- | --- | ---

## CLI / option integrity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## dependency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## documentation

id | status | effort | description | notes
--- | --- | --- | --- | ---

## UI / UX

id | status | effort | description | notes
--- | --- | --- | --- | ---

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
hr-plat-05 | rejected | low | hash-recursive-ai5.py:2013-2020 — `_open_hash_dump` opens the dump in text mode without `newline=""`, so on Windows each record would be written CRLF while `HashDumpWriter.patch_composite` seeks back to a recorded offset to overwrite the fixed-width digest field. | Not a defect: text-mode `tell()` returns an opaque cookie that `seek()` accepts back, and the digest field holds no newline, so newline translation can never shift the patch target. Consumers already accept either ending — `dedupl_numpy._line_content_end` strips a trailing 0x0D by design. Don't re-pick.
lq-plat-02 | rejected | med | link_queue.py:174-200 — `_user_config_dir` creates the config directory with `mode=0o700` and `os.chmod(target, 0o700)`; on Windows `os.chmod` only toggles the read-only bit, so the hardening looked like it silently did not hold off POSIX. | Not a defect: the guarantee is write-integrity (the config holds `command` templates this script executes), not secrecy, and on Windows the directory lands under `%USERPROFILE%` and inherits the profile ACL, which grants other standard users no access — the same protection `0o700` buys on POSIX, already applied without our code. A `pywin32`/`icacls` DACL call would add untestable surface for zero gain. Residual gap on both platforms, too narrow to code around: an `$XDG_CONFIG_HOME` aimed at a shared writable base (POSIX `chmod` still tightens that case; Windows inheritance follows the parent). Recorded in the `_user_config_dir` docstring; don't re-pick.
lq-plat-03 | rejected | low | link_queue.py:2387,2424 — both `subprocess.Popen` calls pass `start_new_session=True`, which is documented as POSIX-only and looked like it would raise on Windows. | Not a defect: CPython's Windows `_execute_child` accepts the argument as `unused_start_new_session` and ignores it (verified in `/usr/lib/python3.12/subprocess.py:1445`), and `_terminate_process_tree`/`_kill_process_tree` already branch to `taskkill /T` on `win32`, so the process-group intent is covered natively there. Don't re-pick.
rdv3-plat-01 | rejected | low | remove-deduplv3.py:57-59 — the emitted script is POSIX shell (`rm -f --` plus `shlex.quote`), which no Windows shell can execute. | Intentional, and stated in the first line of the module docstring: the deliverable *is* a reviewable POSIX shell script, and the script never deletes anything itself. Emitting a second `del`/PowerShell dialect would double the output surface and the review burden. Survivor-path splitting already uses `os.sep`/`os.altsep`. Don't re-pick.
