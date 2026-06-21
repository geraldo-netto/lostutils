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
ie-sec-02 | open | low | import_events.py:281 — untrusted file content is wrapped in <<<CONTENT ... CONTENT delimiters the model is told to treat as data, but content containing a literal "CONTENT" line can break out of the fence. Use a random per-call nonce delimiter. | prompt injection
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rf-sec-03 | open | low | relocate_folder.py:1370 — _create_symlink mkdtemp's the staging dir but never chowns it; as root the staging dir is root-owned and (mode 0700) momentarily blocks the real user, and on cleanup-failure leaks a root-owned .relocate-stage-* in the user's home. Chown staging to source owner or document. | STRIDE-Info/DoS
rf-sec-02 | open | med | relocate_folder.py:1378 — atomic_swap creates the replacement symlink via os.symlink with no lchown, so when run as root migrating a user-owned dir the symlink ends up root-owned instead of inheriting the original uid/gid. Capture source st_uid/st_gid before _backup_target and os.chown(link, uid, gid, follow_symlinks=False) after the rename. | STRIDE-Elevation
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory
dnv3-perf-01 | open | med | deduplicate-by-namev3.py:113 — each row block recomputes cdist of its rows against ALL n columns including the already-emitted lower triangle, doubling work; only columns >= start are kept. Pass cleaned_strs[start:] as the column set and offset cols by start. | wasted lower-triangle compute

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-scal-01 | open | low | import_events.py:81 — _LLM_CACHE keeps every loaded Llama instance for the process lifetime keyed by (model_path, clip_path); multiple distinct model configs in one run accumulate multi-GB models in RAM with no eviction. Cap cache size or evict on config change. |
oze-scal-02 | open | low | organize_by_extension.py:574 — during scan, list_files→is_bucketed_file→resolve_real_extension populates the shared head_cache for every file in the tree; entries are only popped later, so peak head_cache holds one HeadBytes per scanned file alongside the file list. Consider an LRU-bounded head cache or dropping scan-phase entries the planner re-reads. | peak memory
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the streaming docstring. Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-conc-02 | open | low | link_queue.py:1312 — _immediate_depth_warned is set/cleared in _note_immediate_depth (called from _dispatch_immediate) without a lock; concurrent dispatchers can interleave the edge-trigger compare/assign, producing a missed or duplicate backlog notice. Latch the flag under _immediate_lock. | log-only
lq-conc-01 | open | low | link_queue.py:1720 — _warned_shell_url_templates (a plain set) is read+mutated in _warn_shell_template_trusted on worker threads; concurrent shell-protocol items can double-log and, rarely, race the set's internal resize. Guard with a lock (or reuse _metrics_lock). | shell=True only

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-cmplx-01 | open | low | hash-recursive-ai5.py:71 — NO_CAP = None is now dead after the Optional[int] migration (alias_cap_active uses `is not None`, ingest uses None directly); the "back-compat alias for old importers" equals literal None and gives importers nothing. Remove it (and its comment), or declare it in a documented __all__. | dead code
ie-cmplx-01 | open | low | import_events.py:249 — _decode_event_payload sorts (pos, bracket) tuples then ignores the bracket; the position ordering only ever picks the earliest of [ or { and the second-bracket branch is dead. Simplify to first-successful-decode at min position. |

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
ie-dup-01 | open | low | import_events.py:324 — the `global _extraction_failures; _extraction_failures += 1; logger.exception(...)` block is duplicated verbatim in _run_llm and extract_from_file. Extract a single in-file _record_failure(file, exc) helper. | within-file
oze-dup-02 | open | low | organize_by_extension.py:230-241 — _FAMILY_BY_DETECTED plus the five ZIP_FAMILY/OLE2_FAMILY/ISO_BMFF_FAMILY/MP3_FAMILY/GZIP_FAMILY module aliases are "legacy aliases for back-compat" yet nothing consumes them (all lookups go through _family_for). Remove the unused aliases. |
oze-dup-01 | open | med | organize_by_extension.py:431 — the "removed back-compat shims" is contradicted: resolve_real_extension_kw plus standalone sniff/head_cache/extra_zip_family keyword params on is_bucketed_file/list_files/plan_moves still exist alongside the SniffContext ctx path, each rebuilding a SniffContext when ctx is None. Drop the keyword forms now that ctx is threaded end-to-end. | dead parallel API

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-arch-01 | open | low | check-mx-domain.py:14 — the ImportError fallback fabricates a SimpleNamespace dns with Resolver=None; has_mx_record then raises a generic DNSException("not installed") at call time instead of failing fast at import. Raise a clear ImportError in main() when the stub is active. |
mmr-arch-01 | open | low | masterclass-mass-rename.py:73-81 — list_files returns os.listdir (non-recursive) with a dead commented os.walk block; the renamer can't descend dirs and clean_name is applied to directory names too. Decide recursive vs flat and remove dead code. | dead code / scope
oze-arch-01 | open | low | organize_by_extension.py:1929 — _parse_extra_zip_family normalises items but does not apply EXTENSION_ALIASES, so `--extra-zip-family jpeg` (or any aliased synonym) is stored un-canonicalised while resolve_real_extension compares against declared_canon, so the synonym silently never matches. Map each parsed item through EXTENSION_ALIASES. |

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-rel-01 | open | low | check-mx-domain.py:20-24 — domain_from_email splits only on "@" and accepts "a@b" with no dot; NoAnswer/NXDOMAIN/Timeout are all DNSException so a transient network failure is indistinguishable from a real "no MX". Validate TLD presence and catch timeout vs negative answer distinctly. | edge-case
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
dnv3-rel-02 | open | low | deduplicate-by-namev3.py:70 — readlines() then cleanup() drops every line that cleans to empty silently; the counts dict loses original line numbers so a duplicate report can't point back to source lines. Retain source indices if traceability is needed. |
hr-rel-02 | open | med | hash-recursive-ai5.py:1089 — single-representative hashing yields false negatives: _prepare_candidates picks one rep per inode (_readable_rep probes only 8 aliases) and _stage1_hash drops the whole inode group when head_by_path[rep] is None. If that rep was deleted/unreadable between walk and hash but other aliases remain readable, the inode is silently excluded. On a None digest for a multi-alias inode, retry the next readable alias. | aggravated by the 8-alias probe cap
ie-rel-02 | open | low | import_events.py:218 — _coerce_start only combines date+time when both regex-match; a model returning date="2026-06-22" and time="2 PM" silently drops the time and emits date-only. Attempt looser time parsing or log the dropped time. |
ie-rel-01 | open | med | import_events.py:504 — _match_end_to_start builds datetime(end.year,...) from end, but when start is aware and end was parsed as a date, the new datetime gets start.tzinfo at midnight, so an all-day end silently becomes 00:00 of that day, shifting/shrinking multi-day events. For date-typed ends keep them as dates or add a full-day span. |
lq-rel-02 | open | med | link_queue.py:778 — _immediate_q maxsize is fixed at construction and never re-applied; editing immediate_queue_maxsize in config has no effect until restart, unlike _resize_immediate_pool which re-reads pool size live. On resize, if the configured maxsize changed, swap in a new bounded queue (draining the old) or document the cap as restart-only. | asymmetry with live pool size
lq-rel-01 | open | high | link_queue.py:1211 — immediate-mode items already pulled from _immediate_q and running inside _run_immediate_item are tracked nowhere: absent from current_items and from the _immediate_q snapshot, so on shutdown they are silently lost and never retried on restart. Register the in-flight immediate item (a per-consumer slot, like current_items) and serialize it into the immediate state bucket. | data-loss (bounded-pool refactor)
mmr-rel-04 | open | med | masterclass-mass-rename.py:69 — REMOVE_PATTERN joins short tokens (ams, cup, bm, fco) as unescaped substrings with no word boundary, so "exams"/"streams"/"cupcake" are corrupted. Anchor short alpha tokens with \b. | over-broad match
mmr-rel-03 | open | med | masterclass-mass-rename.py:97 — the guard `old_name.lower() != new_name.lower()` skips rename on case-only differences, dropping a desired case change on case-sensitive FS; also os.link-then-unlink is non-atomic vs os.rename and a partial failure leaves two hardlinks. Use os.rename uniformly. | atomicity
mmr-rel-02 | open | med | masterclass-mass-rename.py:99-105 — if clean_name maps two files to the same new_name the second raises FileExistsError and aborts the whole run; clean_name can also return "" after stripping all tokens, producing an empty target. Skip/disambiguate collisions and guard the empty result. | data-loss
mmr-rel-01 | open | high | masterclass-mass-rename.py:112-115 — list_files returns bare basenames (os.listdir) but rename() calls os.path.exists/os.link/os.rename on those relative names; run from any cwd other than the target dir it operates on wrong/nonexistent paths. Join the directory with each filename. | path-bug
nm-rel-01 | open | low | numero_magicov2.py:7 — LETTER_MAP = (i%9)+1 maps A-Z as 1..9 repeating (Z=8) and drops accented Portuguese letters silently. Verify the mapping matches the intended numerology system and normalize accents via unicodedata. | domain-logic
oze-rel-01 | open | high | organize_by_extension.py:1015-1020 — move_file falls back only on errno.EXDEV; on a no-hardlink filesystem (FAT/exFAT/SMB/NFS) the primary os.link raises EPERM/ENOSYS/EOPNOTSUPP (and per-file EMLINK), which `if exc.errno != errno.EXDEV: raise` re-raises, aborting the move. The fallback was added to the collision helper _atomic_rename_to_free_slot but NOT the main move path. Route link-unsupported errnos to _move_cross_device (or a rename-based same-dir move). | regression: fallback only on collision path
oze-rel-02 | open | high | organize_by_extension.py:1690 — manager.choose runs inside the plan generator consumed by _run_moves' submit loop, which is NOT wrapped by make_worker's per-file try; when a (prefix,ext) bucket space is exhausted, bucket_name(index>BUCKET_INDEX_MAX) raises ValueError that propagates through plan_moves → organize → main uncaught, killing the whole run instead of skipping one file. Catch ValueError/RuntimeError around the plan loop or guard choose. | planning errors run-fatal
oze-rel-03 | open | low | organize_by_extension.py:1836 — _dir_is_prunable only guards the initial scandir failure (isinstance(it, tuple)); an OSError raised while iterating entries mid-walk (NFS/permission flip) is uncaught and propagates out of _count_prunable_dirs into the --preview path. Wrap the entry loop in try/except OSError returning False. |
rf-rel-01 | open | med | relocate_folder.py:990-1002 — _iter_verify_tasks emits NO kind-check task when os.lstat(full) fails (st is None), so a src entry that became unreadable mid-run is never verified yet the copy is accepted and the source later deleted. Treat lstat failure as a verify error (yield a task that raises) instead of skipping. | SOLID
rf-rel-02 | open | low | relocate_folder.py:1481 — recover() calls os.lstat(backup) bare right after the _path_taken(backup) gate; a TOCTOU unlink between the two raises FileNotFoundError that escapes as an untyped error instead of the intended typed "no orphaned backup" message. Wrap in try/except OSError and re-raise the typed message. |
rf-rel-03 | open | low | relocate_folder.py:1525 — execute() runs _check_no_open_files only for migrations; --recover (os.rename of the backup over source) skips it entirely, giving no open-files awareness or --force parity. Note/handle that recover doesn't pre-check. |
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-test-01 | open | low | check-mx-domain.py:27 — has_mx_record does live DNS with no injection seam for the resolver, so unit tests must monkeypatch dns.resolver.Resolver. Accept an optional resolver param for testability. | seam
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
hr-test-01 | open | med | hash-recursive-ai5.py:234-255 — the BaseException re-enqueue path (root cause of hr-rel-01) is untested: no test makes a worker raise a non-OSError mid-_scan_dir. On a multi-level tree, patch _scan_dir to raise BaseException on one directory and assert all sibling/child regular files are still emitted and stats are finalized non-zero. | regression guard
mmr-test-01 | open | low | masterclass-mass-rename.py:83-94 — clean_name has no tests; token-substring stripping and the empty-result edge (mmr-rel-02) are unverified. Add tests including a name that reduces to "". | coverage
nm-test-01 | open | low | numero_magicov2.py:35-40 — reduce_to_single_digit returns 0 for input 0 but main guards total==0 before calling, so the 0-branch is untested dead-ish path. Add a test or assert the precondition. | coverage
rf-test-02 | open | low | relocate_folder.py:990 — no test covers the st is None branch of _iter_verify_tasks (rf-rel-01): a src entry whose lstat fails should not pass verification. Monkeypatch os.lstat to raise for one path and assert verify_copy raises rather than silently passing. |
rf-test-01 | open | med | relocate_folder.py:1378 — no test asserts the replacement symlink's ownership after a root-run atomic_swap (rf-sec-02); add a test (skip-if-not-root or monkeypatched os.chown spy) asserting _create_symlink/atomic_swap chowns the link to the source owner. |

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-obs-01 | open | low | check-mx-domain.py:48-52 — distinct failure modes (timeout, NXDOMAIN, no-MX, malformed email) all print "bad: ..."; hard to triage in batch use. Differentiate messages/exit codes per cause. | diagnosability
hr-obs-01 | open | low | hash-recursive-ai5.py:1055-1060 — stage-2 benign skips are counted in three overlapping summary fields with no reconciliation: a None stage-2 tail bumps stage2_errors, stage2_skipped, AND hash_skipped_vanished/shrank; the summary subtracts vanished+shrank from hash_errors but hashed_stage2_skipped still includes them. Document the overlap or split stage2_skipped into real-vs-benign. |
ie-obs-01 | open | low | import_events.py:128 — urlretrieve gives no progress/error context on a ~4GB download; a network failure mid-download leaves a truncated file that _verify_sha256 only catches if a digest is pinned. Download to a temp file, verify, then atomically rename; log byte counts. |
lq-obs-01 | open | low | link_queue.py:1269 — _resize_immediate_pool recomputes _immediate_pool_size but does not reset _immediate_depth_warned; after an upward resize the next _note_immediate_depth compares the live depth against the larger pool with a stale "already warned" latch, so a backlog over the old size but under the new never clears its warning until it fully drains. Reset _immediate_depth_warned on grow. | edge-trigger desync
mmr-obs-01 | open | low | masterclass-mass-rename.py:107-116 — no summary (renamed/skipped/error counts) and errors raise uncaught aborting mid-batch; only per-rename lines print. Add counters and per-file try/except with a final tally. | ux
oze-obs-01 | open | med | organize_by_extension.py:1457-1465 — _move_worker computes dest = bucket_dir / source.name up front and discards move_file's return value; when move_file renames the source aside to <name>.collisionN the file lands at that name but the worker still logs the original dest, so "Moved X -> <wrong dest>" is reported. Return/log the Path move_file actually returns. | logged dest diverges from reality
rf-obs-01 | open | low | relocate_folder.py:992 — the except OSError in _iter_verify_tasks swallows the stat error with no log line, so even if rf-rel-01 is fixed to raise, the operator gets no breadcrumb why an entry couldn't be classified. Log a warning with the path and errno. |
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit
