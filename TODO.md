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
rf-sec-01 | open | med | relocate_folder.py:1287 — source is re-stat'd/walked by path across validate_source -> _check_no_open_files -> ensure_dest_root -> _check_cross_device -> copy_tree with no held handle, leaving a TOCTOU window where source can be swapped for a symlink after the non-symlink check. Open the source dir once with O_DIRECTORY|O_NOFOLLOW and fstat/walk relative to that fd. | STRIDE-Tampering
rdv3-sec-01 | open | low | remove-deduplv3.py:112 — output is `rm -f` commands; shlex.quote is correct but the script emits destructive commands with no header warning/--dry-run note and no guard that the survivor still exists. Add a leading "review before piping to sh" banner and consider verifying paths. | destructive-output

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | low | dedupl_numpy.py:36-41 — np.ascontiguousarray(data[hash_idx]) materializes a full (n_lines×32) copy, transiently doubling memory for large files. Process in chunks or view directly where strides allow. | memory

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-scal-03 | open | low | hash-recursive-ai5.py:686 — the cooperative-cancel break only stops collecting results; because ex.map already submitted every batch (hr-scal-02), Ctrl-C does not stop in-flight/queued hashing and the `with` exit waits for running batches, so cancel latency is "all queued batches" not "next batch boundary" as documented. Fix tied to hr-scal-02 (bounded submission). | cancel reality vs docstring
lq-scal-01 | open | med | link_queue.py:730 — _immediate_q = queue.Queue() is unbounded and immediate items are never deduped, so a large paste of file:/magnet: links pins N QueueItems in RAM regardless of _immediate_pool_size. Bound the queue (maxsize) with backpressure or drop-with-warning. | regression from bounded immediate pool
oze-scal-02 | open | low | organize_by_extension.py:574 — during scan, list_files→is_bucketed_file→resolve_real_extension populates the shared head_cache for every file in the tree; entries are only popped later, so peak head_cache holds one HeadBytes per scanned file alongside the file list. Consider an LRU-bounded head cache or dropping scan-phase entries the planner re-reads. | peak memory
oze-scal-01 | open | med | organize_by_extension.py:1527 — _preplan_resolve_collisions materializes list(files) and builds pairs with a resolve_real_extension call for EVERY file up front, priming head_cache for the whole tree before the first move and contradicting the per-window pop and the streaming docstring. Restrict the pre-pass to needed_dirs members, or stream it in windows. | streaming claim vs reality

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
mmr-arch-01 | open | low | masterclass-mass-rename.py:73-81 — list_files returns os.listdir (non-recursive) with a dead commented os.walk block; the renamer can't descend dirs and clean_name is applied to directory names too. Decide recursive vs flat and remove dead code. | dead code / scope

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-rel-01 | open | low | check-mx-domain.py:20-24 — domain_from_email splits only on "@" and accepts "a@b" with no dot; NoAnswer/NXDOMAIN/Timeout are all DNSException so a transient network failure is indistinguishable from a real "no MX". Validate TLD presence and catch timeout vs negative answer distinctly. | edge-case
dnp-rel-02 | open | med | dedupl_numpy.py:13 — PATH_OFFSET=26 is hardcoded ("preserved from v1") but the hash slice is 32 bytes; 26 vs 32 are inconsistent, so path extraction starts mid-hash for 32-char hashes. Derive offset from the actual hash+separator width. | magic-number
dnp-rel-03 | open | med | dedupl_numpy.py:21-24 — mmap is used as the np.frombuffer source after the file handle closes at with-exit and is never closed (leak); an empty file also makes mmap raise. Keep the file open (or copy), close mm, and guard zero-length files. | resource
dnp-rel-01 | open | high | dedupl_numpy.py:36 — hash_idx = line_starts[:,None] + arange(32) assumes every line is ≥32+PATH_OFFSET bytes with the hash exactly 32 chars at offset 0; a short/blank/final line reads across the newline or past buffer end, corrupting grouping. Validate line length / derive hash width. | array-bounds
lq-rel-01 | open | med | link_queue.py:820 — _save_state serializes only pending + queue-worker in-flight items; items waiting in _immediate_q are never persisted, so an unprocessed immediate backlog is silently lost on shutdown (before the bounded pool, immediate items had dedicated threads so no backlog could exist). Snapshot _immediate_q into a third state bucket and re-enqueue on restore. | data-loss regression
mmr-rel-04 | open | med | masterclass-mass-rename.py:69 — REMOVE_PATTERN joins short tokens (ams, cup, bm, fco) as unescaped substrings with no word boundary, so "exams"/"streams"/"cupcake" are corrupted. Anchor short alpha tokens with \b. | over-broad match
mmr-rel-03 | open | med | masterclass-mass-rename.py:97 — the guard `old_name.lower() != new_name.lower()` skips rename on case-only differences, dropping a desired case change on case-sensitive FS; also os.link-then-unlink is non-atomic vs os.rename and a partial failure leaves two hardlinks. Use os.rename uniformly. | atomicity
mmr-rel-02 | open | med | masterclass-mass-rename.py:99-105 — if clean_name maps two files to the same new_name the second raises FileExistsError and aborts the whole run; clean_name can also return "" after stripping all tokens, producing an empty target. Skip/disambiguate collisions and guard the empty result. | data-loss
mmr-rel-01 | open | high | masterclass-mass-rename.py:112-115 — list_files returns bare basenames (os.listdir) but rename() calls os.path.exists/os.link/os.rename on those relative names; run from any cwd other than the target dir it operates on wrong/nonexistent paths. Join the directory with each filename. | path-bug
nm-rel-01 | open | low | numero_magicov2.py:7 — LETTER_MAP = (i%9)+1 maps A-Z as 1..9 repeating (Z=8) and drops accented Portuguese letters silently. Verify the mapping matches the intended numerology system and normalize accents via unicodedata. | domain-logic
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — the max tiebreaker keeps a stable survivor, but if two byte-identical path strings appear in a group (duplicate line) both compare equal and one is emitted for removal though it's the same file. Dedup identical path strings within a group first. | duplicate-path edge

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-test-01 | open | low | check-mx-domain.py:27 — has_mx_record does live DNS with no injection seam for the resolver, so unit tests must monkeypatch dns.resolver.Resolver. Accept an optional resolver param for testability. | seam
dnp-test-01 | open | med | dedupl_numpy.py:16-62 — no input validation leaves the array-bounds path (dnp-rel-01) untested; add fixtures with short lines, single line, and md5-vs-sha256 widths to lock behavior. | coverage
lq-test-01 | open | low | link_queue.py:986 — non-Tk core logic (the _batch_dispatch depth-0 flush, _stream_summary milestone, _highest_pct_in compare) is marked `# pragma: no cover - withdrawn-root Tk early-exit` though not Tk-related, hiding untested dispatch/log logic. Test via Dispatcher.headless() and drop the misapplied pragmas. | coverage honesty
mmr-test-01 | open | low | masterclass-mass-rename.py:83-94 — clean_name has no tests; token-substring stripping and the empty-result edge (mmr-rel-02) are unverified. Add tests including a name that reduces to "". | coverage
nm-test-01 | open | low | numero_magicov2.py:35-40 — reduce_to_single_digit returns 0 for input 0 but main guards total==0 before calling, so the 0-branch is untested dead-ish path. Add a test or assert the precondition. | coverage

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-obs-01 | open | low | check-mx-domain.py:48-52 — distinct failure modes (timeout, NXDOMAIN, no-MX, malformed email) all print "bad: ..."; hard to triage in batch use. Differentiate messages/exit codes per cause. | diagnosability
lq-obs-01 | open | low | link_queue.py:831-836 — _save_state's outer except logs via self._log, which marshals through _safe_after; once stop_event is set _safe_after early-returns, so a state-save failure on the shutdown path is silently swallowed. Add a stderr fallback for the shutdown path. | silent failure
lq-obs-02 | open | low | link_queue.py:1109 — _dispatch_immediate logs acceptance and put()s but never surfaces immediate-queue depth or a "waiting vs running" distinction, so under a saturated bounded pool thousands of items look accepted while sitting invisibly in _immediate_q. Include _immediate_q.qsize() in the status bar/log when depth exceeds pool size. |
mmr-obs-01 | open | low | masterclass-mass-rename.py:107-116 — no summary (renamed/skipped/error counts) and errors raise uncaught aborting mid-batch; only per-rename lines print. Add counters and per-file try/except with a final tally. | ux
rdv3-obs-01 | open | low | remove-deduplv3.py:101-112 — groups with all-identical paths or a single survivor are silently skipped; no stderr summary of #groups/#files-to-remove. Emit a stderr summary before the rm block for auditability. | audit
