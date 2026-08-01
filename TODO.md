# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md); cache/build paths are excluded. Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

Latest full verification: 2026-08-01 — 10 root scripts / 22,430 lines; pinned lint and type gates clean; CI Python 3.12 suite 2,116 passed, 1 skipped, 6 subtests passed, including 103 explicitly collected fuzz cases; total coverage 97.46%; all 1,304 functions/methods meet the 80% function gate; one Python 3.12 POSIX-fork warning is recorded as `bt-mt-01`.

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
bt-sec-01 | open | low | bookmark-tidy.py:1182-1201 — `--auto-install-llama` runs a live `pip install` of a PyPI package into the running interpreter (arbitrary code execution via the package/its build); the pinned version limits but does not remove supply-chain risk. Default to failing with install instructions and document the trust boundary. | STRIDE Elevation of privilege / supply-chain; live pip call at 1191-1194
hr-sec-07 | open | med | hash-recursive-ai5.py:614-690 — hash opens never compare descriptor `(dev, ino, size)` with walked identity or recheck stability after reads, so path swaps/mutation can bind a digest to stale identity. Pass expected identity; `fstat` before/after hashing and reject changes. | STRIDE Tampering; TOCTOU attack tree
lq-sec-04 | open | high | link_queue.py:1929-1942,2187-2208 — `{url_quoted}` uses POSIX `shlex.quote`, but Windows `shell=True` invokes `cmd.exe`, where single quotes do not protect `&`/`\|`; crafted URLs can inject commands. Disallow shell mode on Windows or implement a correct platform shell contract with adversarial tests. | STRIDE Tampering/Elevation of privilege; OWASP ASVS command injection
mkp-sec-01 | open | low | minikeypad.py:74-85,1863-1876 — opt-in auto-install executes live PyPI package/build code inside the current interpreter environment. Default to external install instructions or explicitly document the trust boundary. | STRIDE Elevation of privilege / supply chain
mkp-sec-02 | open | low | minikeypad.py:814-836 — profile save uses predictable `<path>.tmp` with ordinary `open`, following/truncating a pre-existing symlink and racing concurrent saves. Use same-directory `mkstemp`/`NamedTemporaryFile(delete=False)` with exclusive creation, restrictive mode, fsync, then replace. | STRIDE Tampering/Information disclosure; CWE-59
oze-sec-04 | open | high | organize_by_extension.py:1226-1235,1621,1647 — cross-device fallback reopens `source` by name with `shutil.copy2` after an earlier `lstat`; a swapped symlink can copy data outside root and then unlink the wrong entry. Open with `O_NOFOLLOW`, pin dev/inode, copy from fd, and identity-check before unlink. | STRIDE Information disclosure/Tampering; TOCTOU
rf-sec-04 | open | high | relocate_folder.py:2150-2161 — source fd pins the original inode, but copy/verify still read `plan.source` by path; an attacker can swap in B for copying, restore A before a final identity check, then pass verification and swap. Traverse/copy from the pinned directory fd (`openat`/`dir_fd`) and separately identity-check the path immediately before a no-replace swap. | STRIDE Tampering; TOCTOU; a final path reassert alone is insufficient

## input validation / command safety

id | status | effort | description | notes
--- | --- | --- | --- | ---

## data governance

id | status | effort | description | notes
--- | --- | --- | --- | ---
bt-gov-01 | open | low | bookmark-tidy.py:902 — verbose duplicate logging prints full URLs, including `user:password@host`. Redact URL userinfo before logging. | sensitive-log minimization

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
bt-mt-01 | open | low | bookmark-tidy.py:1047-1048 — `LlamaCategorizer` explicitly selects the `fork` multiprocessing context on POSIX even when the parent process is multithreaded, which Python 3.12 warns can deadlock. Use `spawn` or `forkserver` and retain the existing startup/abort lifecycle tests. | multithreading — background-process lifecycle / fork safety; full CI-equivalent run emits `DeprecationWarning: This process is multi-threaded, use of fork() may lead to deadlocks in the child`

## distributed systems

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-dist-02 | open | med | import_events.py:1168 — shared model downloads use one stable `.<name>.part` without a process lock; concurrent runs can truncate, append, or rename the same partial. Add a per-model cross-process lock around resume/download/verify/publish with stale-lock recovery. | distributed systems — shared-resource coordination
ie-dist-01 | open | med | import_events.py:3805 — `_output_lock` creates `<output>.lock` with O_CREAT|O_EXCL and writes pid, but a hard kill skips unlink and the pid is never read; the next run fails forever until manual removal. On collision read the pid and reclaim only when that process is dead. | distributed systems — idempotent re-run / stale-lock recovery

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

## caching strategy

id | status | effort | description | notes
--- | --- | --- | --- | ---

## memory and cpu management

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-mem-01 | open | med | dedupl_numpy.py:72 — `hash_idx = line_starts[:, None] + np.arange(hash_width)` materializes an `(n_lines, hash_width)` int64 index matrix in addition to the gathered bytes; process record starts in bounded chunks so peak auxiliary memory is capped. | memory scaling — an int32 `arange` alone does not help because the int64 line starts upcast the result
ie-mem-02 | open | low | import_events.py:2308 — `_image_messages` reads the whole image file and base64-encodes it in memory with no size bound before the vision call; a multi-GB image is fully materialized. Bound image size like `_read_text` bounds text. | memory bound
oze-mem-01 | open | med | organize_by_extension.py:1064 — `BucketManager._reserved_names` accumulates one entry per moved filename and is pruned only on `release()` (skip path); successfully moved files are never removed, so it grows O(files). Drop reserved-name sets at bucket saturation or clear per-source after success. | memory / caching strategy — cache with no success-path invalidation

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
mkp-i18n-01 | open | low | minikeypad.py:1023 — all GUI strings (labels, logs, status) are hardcoded English with no translation catalog; add a message catalog only if localization enters scope. | marginal for single-file util
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

## unused functions/methods

id | status | effort | description | notes
--- | --- | --- | --- | ---

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
