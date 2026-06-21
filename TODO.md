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
lq-sec-02 | open | low | link_queue.py:519 — the load-time shell+bare-{url} warning only fires for hand-edited configs; a protocol created via the editor with shell=True+{url_quoted} is never re-warned and _spawn_shell_proc fully trusts the template. Document "protocol templates are trusted input" or warn once per template at run time. | trust boundary
lq-sec-01 | open | low | link_queue.py:529 — _write_config_file writes config with the default umask (often 0o644 world-readable), unlike STATE_FILE which is chmod 0o600. The config holds command templates and the log_file path; write via mkstemp+chmod(0o600)+os.replace, or chmod after write. | file perms
rf-sec-03 | open | med | relocate_folder.py:1175 — _backup_target does os.rename(target, backup) then later os.rename(backup, target) on restore without re-checking backup wasn't substituted; on a world-writable parent an attacker swapping a symlink at the backup name gets it renamed onto target. Verify backup st_ino/st_dev (captured at creation) before restoring, or use fd-relative renames. | STRIDE-Tampering
rf-sec-02 | open | low | relocate_folder.py:1240 — _create_symlink's final os.rename(tmp, link) silently clobbers a pre-existing symlink at link (e.g. one an attacker pre-created on a world-writable parent) with no audit. lstat(link) and assert absent before rename, or use renameat2(RENAME_NOREPLACE). | STRIDE-Tampering
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
lq-perf-02 | open | low | link_queue.py:2099 — self._fh_size += len(text.encode("utf-8")) re-encodes every flushed batch just to count rotation bytes; on high-volume verbose logging this doubles encoding work. Track size via f.tell() after write. | log sink
lq-perf-01 | open | low | link_queue.py:3378 — _on_remove_selected and _selected_queue_items each rebuild a full {iid: item} map over the ENTIRE pending queue under the lock on every Delete/right-click regardless of selection size. Cache the iid→url map from the last refresh, or index lazily. | hot path under lock
rf-perf-02 | open | low | relocate_folder.py:506 — copy_tree always walks the full tree via _src_total_bytes (disk-space precheck) before shutil.copytree walks again. Allow disabling/sampling the precheck for very large trees. |
rf-perf-01 | open | low | relocate_folder.py:872 — _iter_verify_tasks issues up to 3 lstat syscalls per entry (is_symlink/is_file/is_dir) and the chosen _verify_* task lstats again, tripling stat traffic on large trees. lstat once, classify from st_mode, pass the mode into the task. |

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-scal-02 | open | med | hash-recursive-ai5.py:680 — ThreadPoolExecutor.map(batch_fn, _iter_batches(...)) consumes the whole batch generator eagerly and queues every task before the first result; the "batches stream lazily as workers free up" docstring claim is false, so a 10M-candidate stage pins all batch lists + future objects upfront. Bound submission with a sliding window or a queue-fed pool. | streaming claim
hr-scal-03 | open | low | hash-recursive-ai5.py:686 — the cooperative-cancel break only stops collecting results; because ex.map already submitted every batch (hr-scal-02), Ctrl-C does not stop in-flight/queued hashing and the `with` exit waits for running batches, so cancel latency is "all queued batches" not "next batch boundary" as documented. Fix tied to hr-scal-02 (bounded submission). | cancel reality vs docstring
lq-scal-01 | open | med | link_queue.py:730 — _immediate_q = queue.Queue() is unbounded and immediate items are never deduped, so a large paste of file:/magnet: links pins N QueueItems in RAM regardless of _immediate_pool_size. Bound the queue (maxsize) with backpressure or drop-with-warning. | regression from bounded immediate pool
oze-scal-02 | open | low | organize_by_extension.py:574 — during scan, list_files→is_bucketed_file→resolve_real_extension populates the shared head_cache for every file in the tree; entries are only popped later, so peak head_cache holds one HeadBytes per scanned file alongside the file list. Consider an LRU-bounded head cache or dropping scan-phase entries the planner re-reads. | peak memory
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the streaming docstring. Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-conc-01 | open | low | hash-recursive-ai5.py:217 — the _walk_worker BaseException branch re-raises after the finally decrements inflight and posts sentinels, but the directory d popped by the dying worker is dropped: its not-yet-enqueued subdirectories are never scanned, silently undercounting the tree. Re-enqueue d (or record a dir_error) before re-raising. | BaseException safety net
lq-conc-01 | open | med | link_queue.py:731 — _immediate_pool_size is computed once in __init__ and never recomputed; _ensure_worker_count/_on_worker_count_changed resize only the queue-worker pool, so changing Workers in Settings has no effect on immediate concurrency until restart, contradicting the docstring. Recompute and call _ensure_immediate_pool() under _immediate_lock on worker-count change. | regression
lq-conc-02 | open | low | link_queue.py:779 — _flush_save_state's stop_event recheck narrows but doesn't close the shutdown race: a timer past the recheck but not yet in _save_state can write after _shutdown's post-join authoritative snapshot, persisting a stale view. Use a shutting-down flag checked inside _save_state under the timer lock, or join the timer thread. |
oze-conc-01 | open | low | organize_by_extension.py:1535 — _preplan_resolve_collisions resolves collisions serially and moves the head_cache key on the renamed source; confirm no stale head_cache entry accumulates on the 1M-file path when a collision is resolved (key keyed on original vs renamed source). | confirm
rf-conc-02 | open | low | relocate_folder.py:762 — _run_streamed's trailing drain `for fut in inflight: on_done(fut)` ignores on_done's abort bool; the path where the only failing task surfaces during the final drain (all inflight, none completed during submission) is not exercised by tests. Confirm the drain captures it and add a test. |
rf-conc-01 | open | low | relocate_folder.py:899 — verify_copy with checksum=False runs ownership tasks sequentially (no pool), so the rmtree-after-join guarantee documented in _copy_and_verify only applies to the checksum path; the comment overstates it. Scope the comment to the checksum branch. |

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-cmplx-01 | open | low | organize_by_extension.py:1599 — _drain_futures pops head_cache[source], but plan_moves already popped that exact key before yielding, so the second pop is always a no-op. Drop one of the two pops (keep the drain-side, remove the plan-side, or document). |
rf-cmplx-01 | open | low | relocate_folder.py:481 — _format_open_file_warning is annotated holders: list[tuple[int, str, list[Path]]] but the caller passes the now-immutable tuple-of-tuples snapshot.holders; the stale annotation contradicts the immutability invariant. Update to the tuple shape (or Sequence). | SOLID-ISP
rf-cmplx-02 | open | low | relocate_folder.py:986 — _capture_first and _collect_chown_error are near-duplicate "drain one future's exception into a list" helpers; _capture_first is dead (kept for tests). Drop it or have _collect_chown_error delegate. | SOLID-DRY

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
oze-dup-02 | open | med | organize_by_extension.py:416 — resolve_real_extension carries a 3-way sentinel-vs-ctx dual API with a ValueError mixing-guard; every live caller already passes ctx=. Collapse to ctx-only (thin deprecated wrapper if external imports need it) to remove the sentinel machinery. | dual API
oze-dup-01 | open | low | organize_by_extension.py:710 — back-compat shims existing_bucket_indices, _detect_iso_bmff_or_riff (267), _source_blocks_destination (1158) are unreferenced by the live pipeline and kept alive only by tests, widening the public surface. Consolidate tests onto the canonical impls and delete the shims. | back-compat surface
oze-dup-03 | open | low | organize_by_extension.py:1764 — _dir_is_prunable uses os.scandir directly with its own try/except instead of the _safe_scandir contextmanager (309) used elsewhere; the open-coded version logs nothing on OSError. Route through _safe_scandir. |

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-arch-01 | open | low | link_queue.py:1063 — _immediate_concurrency reuses worker_count for immediate-pool sizing, conflating queue parallelism with immediate parallelism (can't run 1 queue worker + 4 immediate runners). Add an immediate_worker_count config key defaulting to worker_count. |
mmr-arch-01 | open | low | masterclass-mass-rename.py:73-81 — list_files returns os.listdir (non-recursive) with a dead commented os.walk block; the renamer can't descend dirs and clean_name is applied to directory names too. Decide recursive vs flat and remove dead code. | dead code / scope

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-rel-01 | open | low | check-mx-domain.py:20-24 — domain_from_email splits only on "@" and accepts "a@b" with no dot; NoAnswer/NXDOMAIN/Timeout are all DNSException so a transient network failure is indistinguishable from a real "no MX". Validate TLD presence and catch timeout vs negative answer distinctly. | edge-case
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
hr-rel-02 | open | low | hash-recursive-ai5.py:486 — `del h` in the inner finally is a dead safety measure: h is a fresh per-call local never shared across calls/frames, so the rebind cannot prevent partial state surviving as the comment claims. Remove the del/finally and the misleading comment, or document it as a no-op. | refactor leftover
hr-rel-01 | open | low | hash-recursive-ai5.py:495 — a strict short-read returns None from _hash_file_windows but that None is neither logged via _log_hash_error nor ticked via _tick_vanished; _run_stage counts it in stageN_errors, so a file truncated mid-run inflates hash_errors with zero stderr trace. Route the strict-short-read None through _log_hash_error or a dedicated hash_skipped_shrank counter. |
lq-rel-01 | open | med | link_queue.py:820 — _save_state serializes only pending + queue-worker in-flight items; items waiting in _immediate_q are never persisted, so an unprocessed immediate backlog is silently lost on shutdown (before the bounded pool, immediate items had dedicated threads so no backlog could exist). Snapshot _immediate_q into a third state bucket and re-enqueue on restore. | data-loss regression
lq-rel-02 | open | low | link_queue.py:1085 — _immediate_consumer runs until stop_event only; _ensure_immediate_pool spawns on deficit but never retires surplus, so a downward immediate-pool resize (once lq-conc-01 is fixed) can't shrink. Give each consumer a per-thread stop Event signalled on a downward resize. |
lq-rel-03 | open | low | link_queue.py:2436 — _FacadeMeta.__getattr__ makes any missing class attr on LinkQueueApp fall through to Dispatcher/ConfigStore, so a typo'd class-level call resolves silently instead of raising, and foreign introspection (copy/pickle/IDE) triggers the delegation walk. Restrict delegation to a name allowlist or to non-dunder names. | regression
mmr-rel-04 | open | med | masterclass-mass-rename.py:69 — REMOVE_PATTERN joins short tokens (ams, cup, bm, fco) as unescaped substrings with no word boundary, so "exams"/"streams"/"cupcake" are corrupted. Anchor short alpha tokens with \b. | over-broad match
mmr-rel-03 | open | med | masterclass-mass-rename.py:97 — the guard `old_name.lower() != new_name.lower()` skips rename on case-only differences, dropping a desired case change on case-sensitive FS; also os.link-then-unlink is non-atomic vs os.rename and a partial failure leaves two hardlinks. Use os.rename uniformly. | atomicity
mmr-rel-02 | open | med | masterclass-mass-rename.py:99-105 — if clean_name maps two files to the same new_name the second raises FileExistsError and aborts the whole run; clean_name can also return "" after stripping all tokens, producing an empty target. Skip/disambiguate collisions and guard the empty result. | data-loss
mmr-rel-01 | open | high | masterclass-mass-rename.py:112-115 — list_files returns bare basenames (os.listdir) but rename() calls os.path.exists/os.link/os.rename on those relative names; run from any cwd other than the target dir it operates on wrong/nonexistent paths. Join the directory with each filename. | path-bug
nm-rel-01 | open | low | numero_magicov2.py:7 — LETTER_MAP = (i%9)+1 maps A-Z as 1..9 repeating (Z=8) and drops accented Portuguese letters silently. Verify the mapping matches the intended numerology system and normalize accents via unicodedata. | domain-logic
oze-rel-03 | open | med | organize_by_extension.py:1108-1120 — _resolve_source_collision returns after resolving ONE cross-source blocker instead of re-probing; with two stacked non-dir blockers on the destination ancestor chain the second survives and ensure_directory raises NotADirectoryError. continue the loop after a cross-source rename instead of returning. |
oze-rel-01 | open | high | organize_by_extension.py:1191 — _atomic_rename_to_free_slot reserves via os.link, which fails on filesystems without hardlink support (FAT/exFAT/many SMB/NFS) with EPERM/ENOSYS/EOPNOTSUPP; _resolve_one_planning_collision only catches FileNotFoundError, so on a no-hardlink FS a single collision aborts the whole plan_moves generator. Detect link-unsupported errnos and fall back to os.rename with an O_CREAT|O_EXCL reservation, or surface a clear per-file skip. | regression
oze-rel-06 | open | med | organize_by_extension.py:1191 — _atomic_rename_to_free_slot can also fail with EMLINK (source at max link count), neither a FileExistsError nor caught anywhere; it propagates as a bare OSError and kills the whole plan in the planning path. Treat non-EEXIST OSErrors as fall-back-to-rename or per-file skip. | regression
oze-rel-02 | open | high | organize_by_extension.py:1195 — _atomic_rename_to_free_slot has no rollback: if os.link(source, candidate) succeeds but os.unlink(source) then fails, the file exists at BOTH source and candidate; the blocker at source survives, the probe loop re-links to candidate2... burning all retries and leaking N hardlinks, then ensure_directory raises. On unlink failure, unlink the just-created candidate and re-raise (mirror _unlink_with_rollback). | regression
oze-rel-05 | open | low | organize_by_extension.py:1385 — the comment claims "Path.resolve() canonicalizes" but the code is `resolved = Path(root)` with no .resolve() (canonicalization happens later at 1393), so the is_dir() check and the FileNotFoundError message run against the raw path. Correct the comment or move the resolve up. |
oze-rel-04 | open | med | organize_by_extension.py:1549 — _resolve_one_planning_collision uses source.is_file() which follows symlinks; plan_moves is public and accepts arbitrary files, so a symlink-to-regular-file source would pass and then _atomic_rename_to_free_slot hardlinks the symlink target. Use os.path.lexists + S_ISREG on lstat like the rest of the module. |
rf-rel-05 | open | low | relocate_folder.py:540 — tracking_copy2 accounts done[0] via os.lstat(s).st_size after the copy; for a symlink copied with follow_symlinks the size uses the link size not the followed target, so progress total and per-file done can diverge. Use os.stat to match the bytes actually copied, or accept the drift. | cosmetic
rf-rel-04 | open | low | relocate_folder.py:1295 — _orphaned_backup triggers purely on "source absent AND <name>.relocate-backup present", so a user's unrelated foo.relocate-backup sibling whose foo doesn't exist gets a misleading "previous run killed mid-swap" warning. Stat-match the backup is a dir, or document the false-positive surface. |
rf-rel-02 | open | med | relocate_folder.py:1324 — recover does os.rename(backup, source) without verifying backup is the real moved-aside directory (could be a dangling symlink or regular file left by a third party) and never confirms the restored source is a directory. lstat(backup) and require S_ISDIR (and not a symlink) before restoring, else raise. |
rf-rel-01 | open | med | relocate_folder.py:1344 — REGRESSION from _target_has_content: when a prior migration completed but the target later became empty, already_migrated returns False and validate_source then raises "source is already a symlink", crashing instead of skipping a legitimately-migrated dir. When source is a symlink resolving to an empty target, return a distinct "already migrated (empty target)" skip. | regression
rf-rel-06 | open | low | relocate_folder.py:1481 — _copy_and_verify's failure path calls shutil.rmtree(plan.target, ignore_errors=True); if the verify failure was transient the half-good target is silently destroyed with no log line (unlike the copy_tree cleanup path which logs rmtree failures). Log that the target was removed after verify failure. | STRIDE-Repudiation
rf-rel-03 | open | low | relocate_folder.py:1647 — _run_recover ignores ns.dest_root entirely; `--recover <src> <dest_root>` silently drops dest_root and a typo in source dead-ends in a bare FileNotFoundError. Warn that dest_root is ignored with --recover. |
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-test-01 | open | low | check-mx-domain.py:27 — has_mx_record does live DNS with no injection seam for the resolver, so unit tests must monkeypatch dns.resolver.Resolver. Accept an optional resolver param for testability. | seam
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
lq-test-01 | open | low | link_queue.py:986 — non-Tk core logic (the _batch_dispatch depth-0 flush, _stream_summary milestone, _highest_pct_in compare) is marked `# pragma: no cover - withdrawn-root Tk early-exit` though not Tk-related, hiding untested dispatch/log logic. Test via Dispatcher.headless() and drop the misapplied pragmas. | coverage honesty
mmr-test-01 | open | low | masterclass-mass-rename.py:83-94 — clean_name has no tests; token-substring stripping and the empty-result edge (mmr-rel-02) are unverified. Add tests including a name that reduces to "". | coverage
nm-test-01 | open | low | numero_magicov2.py:35-40 — reduce_to_single_digit returns 0 for input 0 but main guards total==0 before calling, so the 0-branch is untested dead-ish path. Add a test or assert the precondition. | coverage
oze-test-02 | open | low | organize_by_extension.py:1108 — the multi-blocker cross-source case (oze-rel-03) is untested; add a fixture with two stacked regular-file blockers on a destination ancestor chain and assert the bucket is still created. |
oze-test-01 | open | med | organize_by_extension.py:1174 — the os.link-based _atomic_rename_to_free_slot reservation has no coverage for the no-hardlink-FS branch (oze-rel-01), the link-succeeds/unlink-fails rollback gap (oze-rel-02), or EMLINK (oze-rel-06). Monkeypatch os.link/os.unlink to raise EPERM/EACCES and assert graceful per-file skip vs plan abort. | regression coverage
rf-test-02 | open | med | relocate_folder.py:284 — no test covers already_migrated returning False for an empty-but-migrated target nor the resulting execute crash (rf-rel-01). Add: source is symlink->target, target exists but empty, assert execute skips (after rf-rel-01 fix). |
rf-test-04 | open | low | relocate_folder.py:762 — no test forces the verify pool's first-and-only error into the trailing _run_streamed drain (all tasks inflight, none completing during submission) to prove the drain captures it (rf-conc-02). |
rf-test-01 | open | med | relocate_folder.py:907 — the verify-pool leak and rmtree-vs-running-threads race are hard to test: there is no seam to pause a worker mid-hash. Make the hash function injectable (param or contextvar like _log) so a test can block a thread inside _sha256 on the error path. |
rf-test-03 | open | low | relocate_folder.py:1324 — no test covers recover when <source>.relocate-backup is a non-directory (symlink/regular file); current code blindly renames it into place (rf-rel-02). Add a test asserting recover refuses a non-dir backup. |

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-obs-01 | open | low | check-mx-domain.py:48-52 — distinct failure modes (timeout, NXDOMAIN, no-MX, malformed email) all print "bad: ..."; hard to triage in batch use. Differentiate messages/exit codes per cause. | diagnosability
hr-obs-02 | open | low | hash-recursive-ai5.py:96 — the hash_skipped_vanished docstring asserts the summary subtracts this counter, but main prints hash_errors=X+Y and hash_skipped=Z side by side with no subtraction, and Z is a subset of X+Y. Subtract hash_skipped_vanished from the printed hash_errors, or fix the comment to say it is reported alongside as a subset. | comment vs summary
hr-obs-01 | open | low | hash-recursive-ai5.py:864 — stage2_skipped is computed and stored in info but never surfaced in the summary line (prints stage2_errors but not stage2_skipped); short-read/shrink skips during stage 2 are invisible. Add hashed_stage2_skipped to the summary. |
lq-obs-01 | open | low | link_queue.py:831-836 — _save_state's outer except logs via self._log, which marshals through _safe_after; once stop_event is set _safe_after early-returns, so a state-save failure on the shutdown path is silently swallowed. Add a stderr fallback for the shutdown path. | silent failure
lq-obs-02 | open | low | link_queue.py:1109 — _dispatch_immediate logs acceptance and put()s but never surfaces immediate-queue depth or a "waiting vs running" distinction, so under a saturated bounded pool thousands of items look accepted while sitting invisibly in _immediate_q. Include _immediate_q.qsize() in the status bar/log when depth exceeds pool size. |
mmr-obs-01 | open | low | masterclass-mass-rename.py:107-116 — no summary (renamed/skipped/error counts) and errors raise uncaught aborting mid-batch; only per-rename lines print. Add counters and per-file try/except with a final tally. | ux
oze-obs-02 | open | low | organize_by_extension.py:1567 — _atomic_rename_to_free_slot's retry-cap exhaustion raises RuntimeError with no count of how many .collisionN links were probed, and a partial-failure (oze-rel-02) leaves orphan hardlinks with zero log trace. Log the candidate count and any orphaned link on the unlink-failure path. |
oze-obs-01 | open | low | organize_by_extension.py:1622 — the periodic "progress:" line is emitted only inside the submit loop; during the final drain loop no progress is logged, so a run whose last >10k files complete after the plan iterator is exhausted shows no progress until the summary. Evaluate PROGRESS_EVERY in the drain phase too. | progress gap
rf-obs-01 | open | low | relocate_folder.py:1238 — execute's finally logs state=FAILED with no indication of which step failed or whether the source is intact vs renamed to .relocate-backup. Log last-completed MigrationState and the backup-path hint. |
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit
