# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`). Scripts are standalone — dedup findings are within-file only, never cross-file module extraction.

id prefixes: `cmx-` check-mx-domain.py, `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `mmr-` masterclass-mass-rename.py, `nm-` numero_magicov2.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-sec-01 | open | low | check-mx-domain.py:34 — resolver.resolve(domain, "MX") passes the user email's domain straight to DNS with no length/charset validation; combined with cmx-rel-01 a domain like ".." or an empty label raises unexpected exceptions. Validate domain shape (non-empty labels, max length) before resolving. | input-trust
ie-sec-01 | open | high | import_events.py:85 — urlretrieve downloads ~4GB model over HTTPS but MODEL_SHA256/CLIP_SHA256 default to None, so _verify_sha256 only warns and skips; a compromised mirror's GGUF is loaded and executed by llama_cpp. Ship pinned digests by default rather than None. | supply-chain
ie-sec-02 | open | low | import_events.py:371 — process_folder with recursive rglob follows into any subdir and _read_text/extract_with_llm opens every file; no symlink guard means a symlink to a sensitive or device file is read and fed to the model/output. Skip symlinks (file.is_symlink()) or resolve+confine to the root. | path-trust
lq-sec-02 | open | low | link_queue.py:519 — the load-time shell+bare-{url} warning only fires for hand-edited configs; a protocol created via the editor with shell=True+{url_quoted} is never re-warned and _spawn_shell_proc (1453) fully trusts the template. Document "protocol templates are trusted input" or warn once per template at run time. | trust boundary
lq-sec-01 | open | low | link_queue.py:529 — _write_config_file writes config with the default umask (often 0o644 world-readable), unlike STATE_FILE which is chmod 0o600 at 823. The config holds command templates and the log_file path; add os.chmod(self.config_file, 0o600) after the write for parity. | file perms
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-perf-01 | open | med | deduplicate-by-namev3.py:69-75 — full N×N cdist materializes an O(N²) uint8 matrix though only the upper triangle is used; near the 30K doc-cap that's ~900MB with the lower triangle wasted. Compute banded/bucketed for large N. | memory
ie-perf-01 | open | low | import_events.py:314-324 — _pdf_to_images renders every page to PNG even after enough events are found, and get_pixmap default DPI may be too low for vision OCR; for large scanned PDFs this is slow/memory-heavy with no page cap. Cap pages / set DPI deliberately. | scale
lq-perf-02 | open | low | link_queue.py:2099 — self._fh_size += len(text.encode("utf-8")) re-encodes every flushed batch just to count rotation bytes; on high-volume verbose logging this doubles encoding work. Track size via f.tell() after write. | log sink
lq-perf-01 | open | low | link_queue.py:3378 — _on_remove_selected and _selected_queue_items (4032) each rebuild a full {iid: item} map over the ENTIRE pending queue under the lock on every Delete/right-click regardless of selection size. Cache the iid→url map from the last refresh, or index lazily. | hot path under lock
rf-perf-02 | open | low | relocate_folder.py:506 — copy_tree always walks the full tree via _src_total_bytes (disk-space precheck) before shutil.copytree walks again. Allow disabling/sampling the precheck for very large trees. |
rf-perf-01 | open | low | relocate_folder.py:872 — _iter_verify_tasks issues up to 3 lstat syscalls per entry (is_symlink/is_file/is_dir) and the chosen _verify_* task lstats again, tripling stat traffic on large trees. lstat once, classify from st_mode, pass the mode into the task. |

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-scal-01 | open | med | hash-recursive-ai5.py:774-775 — _expand_keys_to_paths does `if len(out) < cap: out.extend(paths)`, extending with the entire per-inode bucket; a single inode whose bucket far exceeds cap (alias_cap disabled) materializes the full list before the final out[:cap] slice, defeating the bounded-memory goal. Extend only up to cap - len(out). |
oze-scal-02 | open | low | organize_by_extension.py:574 — during scan, list_files→is_bucketed_file→resolve_real_extension populates the shared head_cache for every file in the tree; entries are only popped later in _drain_futures/plan_moves, so peak head_cache holds one HeadBytes per scanned file alongside the file list. Consider an LRU-bounded head cache or dropping scan-phase entries the planner re-reads. | peak memory
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) (1490) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the "never materialises the full plan" docstring (1474). Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-conc-01 | open | high | organize_by_extension.py:1218 — _atomic_rename_to_free_slot is not clobber-safe: POSIX os.rename(source, candidate) silently REPLACES an existing regular file and does not raise FileExistsError, so the candidate.exists() guard (1215) is a TOCTOU window and the except FileExistsError retry (1220) never fires for a file. A sibling worker creating candidate as a file gets it overwritten. Use os.link+os.unlink or renameat2(RENAME_NOREPLACE)/O_EXCL reserve. | TOCTOU / data-loss
rf-conc-02 | open | low | relocate_folder.py:719 — _run_streamed's trailing `for fut in inflight: on_done(fut)` calls fut.exception(), which blocks per still-running future, so cancel_futures only short-circuits queued (not running) hashes and abort still waits on up to `workers` in-flight hashes. Document or actively cancel/ignore running ones. |
rf-conc-01 | open | med | relocate_folder.py:907 — _run_verify_pool builds ThreadPoolExecutor outside a `with` and only shutdown(wait=False) in finally, leaking running threads on abort (see rf-rel-02). Use a context manager or shutdown(wait=True) on the abort path. | resource lifecycle

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-cmplx-01 | open | low | import_events.py:165-174 — _coerce_start merges start/date/time keys with str() fallbacks that can produce strings like "NoneTNone"; the date+time concat assumes both are date/time-shaped. Tighten to validated ISO assembly. | robustness
oze-cmplx-02 | open | low | organize_by_extension.py:1174 — _free_collision_name is no longer used in the live path (superseded by _atomic_rename_to_free_slot) and survives only via tests; its docstring warns it is TOCTOU-unsafe. Remove it and migrate tests, or mark clearly test-only to stop new callers. | superseded shim

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-dup-01 | open | low | import_events.py:239-257 — _image_messages_from_bytes/_image_messages duplicate the base64 encode of encode_image (159-162), which is now dead/unused. Remove encode_image or route image paths through it. | dead-code
oze-dup-02 | open | med | organize_by_extension.py:416 — resolve_real_extension carries a 3-way sentinel-vs-ctx dual API (416-456) with a ValueError mixing-guard; every live caller already passes ctx=. Collapse to ctx-only (thin deprecated wrapper if external imports need it) to remove the sentinel machinery. | dual API
oze-dup-01 | open | low | organize_by_extension.py:710 — back-compat shims existing_bucket_indices (710), _detect_iso_bmff_or_riff (267), _source_blocks_destination (1158) are unreferenced by the live pipeline and kept alive only by tests, widening the public surface. Consolidate tests onto the canonical impls and delete the shims. | back-compat surface

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-arch-01 | open | med | import_events.py:52 — module-level mutable globals (_LLM, MODEL_PATH, DEFAULT_TZ) mutated by _apply_config (471-476) make functions non-reentrant and order-dependent (extract_from_ics reads DEFAULT_TZ implicitly). Pass a small config object/params (within this single file, no shared-module extraction). | within-file
rf-arch-01 | open | low | relocate_folder.py:1130 — atomic_swap is misnamed: only the final os.rename(tmp, link) is atomic; the rename-aside + symlink + backup-delete sequence is not atomic across process death, so the name overpromises kill-safety. Rename to swap_with_backup or prominently document the non-atomic window. | honest naming

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-rel-01 | open | low | check-mx-domain.py:20-24 — domain_from_email splits only on "@" and accepts "a@b" with no dot; a NoAnswer/NXDOMAIN raises before the len()>0 check, so valid-but-MX-less and malformed domains both surface as a generic error. Validate TLD presence and catch NoAnswer/NXDOMAIN distinctly. | edge-case
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes (line 36); 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is created inside the with-block but used as np.frombuffer source after f is closed; the handle closes at block exit yet mm/array outlive it, and mm is never closed (leak). Keep the file open (or copy) and close mm. | resource
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank line or md5(32 hex)-vs-sha256 mismatch reads across the newline into the next record or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
dnv3-rel-02 | open | low | deduplicate-by-namev3.py:36-38 — cleanup strips substrings "xxx"/"monography" anywhere, so words containing them are mangled, changing distances. Use word-boundary matching or document the intent. | semantics
dnv3-rel-01 | open | med | deduplicate-by-namev3.py:74 — dtype=np.uint8 caps distances at 255; with a user threshold >255 (e.g. -t 300) the uint8 matrix silently overflows/wraps. Clamp/validate threshold ≤254. | overflow
ie-rel-01 | open | med | import_events.py:134 — Calendar.from_ical(f.read().decode("utf-8")) hard-decodes UTF-8; ICS files in latin-1/other encodings raise UnicodeDecodeError, caught only by the broad handler and silently dropped. Pass bytes to from_ical (icalendar accepts bytes) or detect encoding. | encoding
ie-rel-03 | open | low | import_events.py:418 — _parse_iso/_coerce_start can emit "2026-06-22T14:00" without seconds/zone; on Python <3.11 datetime.fromisoformat rejects some such forms, so valid LLM outputs silently skip in build_ics. Normalize/validate ISO and document the minimum Python version. | version-compat
ie-rel-02 | open | med | import_events.py:459-467 — parse_args reads default=MODEL_PATH at import time but get_llm uses the globals set by _apply_config, making behavior order-dependent and hard to test. Thread config via params instead of globals. | global-state
mmr-rel-03 | open | med | masterclass-mass-rename.py:97 — the guard `old_name.lower() != new_name.lower()` skips rename on case-only differences, dropping a desired case change on case-sensitive FS; also os.link-then-unlink is non-atomic vs os.rename and a partial failure leaves two hardlinks. Use os.rename uniformly. | atomicity
mmr-rel-02 | open | med | masterclass-mass-rename.py:99-105 — if clean_name maps two files to the same new_name the second raises FileExistsError and aborts the whole run (no try/continue); clean_name can also return "" after stripping all tokens, producing an empty target. Skip/disambiguate collisions and guard the empty result. | data-loss
mmr-rel-01 | open | high | masterclass-mass-rename.py:112-115 — list_files returns bare basenames (os.listdir) but rename() calls os.path.exists/os.link/os.rename on those relative names; run from any cwd other than the target dir it operates on wrong/nonexistent paths. Join the directory with each filename. | path-bug
nm-rel-01 | open | low | numero_magicov2.py:7 — LETTER_MAP = (i%9)+1 maps A-Z as 1..9 repeating (Z=8) and drops accented Portuguese letters silently. Verify the mapping matches the intended numerology system and normalize accents via unicodedata. | domain-logic
oze-rel-02 | open | low | organize_by_extension.py:1405 — ROOT_MAX_LENGTH is checked against the unresolved str(root) (1405), then the path is resolve()d at return (1416); a short input that expands via symlinks/.. to >4096 chars bypasses the guard. Re-apply the length check to the resolved path before returning. | guard placement
oze-rel-01 | open | high | organize_by_extension.py:1490 — --preview is not honored during planning: plan_moves→_preplan_resolve_collisions executes real os.rename via _atomic_rename_to_free_slot (1547) regardless of preview, violating the "preview never touches the filesystem" docstring (1721,1459). Thread a preview flag into plan_moves/_preplan_resolve_collisions and record (not perform) the rename when previewing. | preview-mode contract
rf-rel-05 | open | low | relocate_folder.py:73 — TMPLINK_SUFFIX is defined but unused since the mkdtemp-staging rewrite; the dead constant implies a fixed .relocate-tmp name still exists. Remove it. |
rf-rel-04 | open | low | relocate_folder.py:278 — already_migrated declares migrated purely on link_target.resolve() == target.resolve() without confirming the target exists/has content; a stale or hand-made symlink that resolves equal makes the tool skip the copy and never move the source data. Also require target to exist and be a non-empty dir. | correctness
rf-rel-02 | open | med | relocate_folder.py:914 — _run_verify_pool's docstring claims it drains inflight futures so background SHA-256 work doesn't leak, but shutdown(wait=False, cancel_futures=...) returns while running worker threads still execute _sha256; the function proceeds with detached hash threads. shutdown(wait=True) on the error path or join running futures first. | kill-safety
rf-rel-01 | open | high | relocate_folder.py:1113 — _backup_target is not kill-safe: SIGKILL/power-loss between os.rename(target, backup) and _create_symlink completing leaves the dir renamed to <name>.relocate-backup with no symlink, and nothing recovers it (re-run already_migrated False → validate_source raises FileNotFoundError, dead-ending the operator). On startup detect an orphaned .relocate-backup when source is missing and restore it, or add --recover. | partial-state recovery
rf-rel-03 | open | med | relocate_folder.py:1346 — _copy_and_verify calls shutil.rmtree(plan.target, ignore_errors=True) on verify failure while running hash threads from rf-rel-02 may still be open-reading files under plan.target; rmtree races those reads. Fully join the verify pool before rmtree. | concurrency vs cleanup

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
ie-test-01 | open | low | import_events.py:198-213 — _decode_event_payload heuristics (earliest [ vs {, raw_decode) are fragile and untested against LLM noise (trailing prose, nested braces in title). Add unit tests for malformed payloads. | parser-robustness
lq-test-01 | open | low | link_queue.py:986 — non-Tk core logic (the _batch_dispatch depth-0 flush at 986, _stream_summary milestone at 1337, _highest_pct_in compare at 1367) is marked `# pragma: no cover - withdrawn-root Tk early-exit` though it is not Tk-related, hiding untested dispatch/log logic. Test via Dispatcher.headless() and drop the misapplied pragmas. | coverage honesty
mmr-test-01 | open | low | masterclass-mass-rename.py:83-94 — clean_name has no tests; token-substring stripping and the empty-result edge (mmr-rel-02) are unverified. Add tests including a name that reduces to "". | coverage
rf-test-01 | open | med | relocate_folder.py:907 — the verify-pool leak (rf-rel-02) and rmtree-vs-running-threads race (rf-rel-03) are untestable: there is no seam to pause a worker mid-hash. Make the hash function injectable (param or contextvar like _log) so a test can block a thread inside _sha256 on the error path. |

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-obs-01 | open | low | check-mx-domain.py:48-52 — distinct failure modes (timeout, NXDOMAIN, no-MX, malformed email) all print "bad: ..."; hard to triage in batch use. Differentiate messages/exit codes per cause. | diagnosability
hr-obs-01 | open | low | hash-recursive-ai5.py:864 — stage2_skipped is computed and stored in info but never surfaced in the summary line (1241-1256 prints stage2_errors but not stage2_skipped); short-read/shrink skips during stage 2 are invisible. Add hashed_stage2_skipped to the summary. |
lq-obs-01 | open | low | link_queue.py:831-836 — _save_state's outer except logs via self._log, which marshals through _safe_after; once stop_event is set _safe_after early-returns (3755), so a state-save failure on the shutdown path is silently swallowed. Add a stderr fallback for the shutdown path. | silent failure
mmr-obs-01 | open | low | masterclass-mass-rename.py:107-116 — no summary (renamed/skipped/error counts) and errors raise uncaught aborting mid-batch; only per-rename lines print. Add counters and per-file try/except with a final tally. | ux
oze-obs-01 | open | low | organize_by_extension.py:1622 — the periodic "progress:" line is emitted only inside the submit loop; during the final drain loop (1627-1628) no progress is logged, so a run whose last >10k files complete after the plan iterator is exhausted shows no progress until the summary. Evaluate PROGRESS_EVERY in the drain phase too. | progress gap
rf-obs-01 | open | low | relocate_folder.py:1238 — execute's finally logs state=FAILED with no indication of which step failed or whether the source is intact vs renamed to .relocate-backup; after a kill-safety failure (rf-rel-01) the operator gets no pointer to the orphaned backup. Log last-completed MigrationState and the backup-path hint. |
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit
