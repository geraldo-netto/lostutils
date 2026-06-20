# Project Review TODO

Proposed corrections / improvements. One table per review category. Format:

`id | status | effort | description | notes`

Scan scope = root-directory `.py`/`.sh` files only (per AGENTS.md). Tables sorted by `description` (each starts with `file:line`).

id prefixes: `cmx-` check-mx-domain.py, `dnp-` dedupl_numpy.py, `dnv3-` deduplicate-by-namev3.py, `hr-` hash-recursive-ai5.py, `ie-` import_events.py, `lq-` link_queue.py, `mmr-` masterclass-mass-rename.py, `nm-` numero_magicov2.py, `oze-` organize_by_extension.py, `rf-` relocate_folder.py, `rdv3-` remove-deduplv3.py.

## security

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-sec-01 | open | low | check-mx-domain.py:43 — `str(argv[0])` passed unvalidated; an email-like string with no `@` raises IndexError caught later, but no length/charset bound means a huge arg reaches the resolver. Validate email shape (regex) before DNS resolution. | input validation
dnp-sec-01 | open | low | dedupl_numpy.py:21 — `sys.argv[1]` opened with no validation; a non-existent path raises an uncaught FileNotFoundError traceback instead of a clean error. Wrap open in try/except and exit non-zero. | input validation
ie-sec-01 | open | high | import_events.py:32 — `urlretrieve(url, path_str)` downloads ~4GB model over HTTPS with no checksum/signature verification (MITM or compromised HF repo serves a malicious GGUF executed by llama_cpp). Pin and verify SHA-256 of each file before use. | supply-chain
ie-sec-02 | open | med | import_events.py:150 — file content from arbitrary folder files is interpolated straight into the LLM user prompt with no size cap; enables prompt-injection and context overflow. Truncate to n_ctx budget and treat content as untrusted. | prompt injection
lq-sec-01 | open | med | link_queue.py:524 — `_normalize_config_schema` only WARNS on default_command shell=True + bare `{url}`; the default path is the fallback for every unknown scheme. Add the same hard reject at runtime in `_resolve_protocol`'s default branch, not just a load-time stderr warning. | STRIDE-Tampering/Elevation
lq-sec-03 | open | low | link_queue.py:786 — `_save_state` tmpfile uses mkstemp 0o600 but the dir is only forced 0o700 in `_user_config_dir`, not on the `_script_dir()` (repo-dir) fallback. Document/guard the fallback dir perms. | STRIDE-Info-disclosure
lq-sec-02 | open | low | link_queue.py:1133 — `_extra_shell` appends `flag` verbatim into the `/bin/sh -c` string; a hand-edited config flag with shell metacharacters is interpolated unquoted. shlex.quote the flag (or split+rejoin). | STRIDE-Tampering
mmr-sec-02 | open | med | masterclass-mass-rename.py:88 — REMOVE_PATTERN strips substrings anywhere (e.g. `ams`, `bm`, `cup`, `cms`) including inside legitimate words, corrupting unrelated filenames. Anchor tokens to word boundaries and order by length. | reliability
mmr-sec-01 | open | high | masterclass-mass-rename.py:112-115 — `list_files` returns bare basenames but rename/os.path.exists/os.link operate relative to CWD, so renames target wrong/nonexistent paths unless CWD == argv[1]. Join names with the directory via os.path.join. | path traversal
oze-mt-01 | open | low | organize_by_extension.py:73 — `normalize_extension` rejects only the space char, not tab/newline/other control chars, so a suffix like `.a\tb` becomes a bucket dir name with a control char. Use a stricter charset test. | input validation
oze-mt-02 | open | low | organize_by_extension.py:1548 — `organize()` does not validate num_threads (only the CLI's _positive_int does); a library caller passing 0/negative reaches ThreadPoolExecutor(max_workers=0) and ValueError deep in the run. Validate num_threads >= 1 at entry. | input validation
rf-sec-01 | open | med | relocate_folder.py:189 — `validate_source` has a TOCTOU gap: is_symlink/exists/is_dir each re-stat source; an attacker can swap source for a symlink between validate and copy_tree's os.walk. Open the source dir with O_NOFOLLOW/O_DIRECTORY once and operate on the fd. | STRIDE-Tampering
rf-sec-02 | open | med | relocate_folder.py:529 — copy_tree uses copytree(symlinks=True); combined with no re-check that target.parent stays on the intended device after ensure_dest_root, a swapped dest_root component redirects the whole copy. Resolve+pin dest device after creation and verify. | STRIDE-Tampering
rf-sec-03 | open | low | relocate_folder.py:1160 — `_create_symlink` staging dir cleanup only runs in the call's finally; an external kill leaves `.relocate-stage-*` / TMPLINK dirs. Sweep stale STAGING_PREFIX/TMPLINK_SUFFIX siblings at startup. | STRIDE-DoS
rf-sec-04 | open | low | relocate_folder.py:1483 — main logs full source/target paths plus open-file holder comm/pids; on shared hosts these may land in world-readable logs. Scope log file perms; gate holder-enumeration detail behind verbosity. | STRIDE-Info-disclosure

## performance

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-perf-01 | open | med | dedupl_numpy.py:36-41 — `hash_idx = line_starts[:,None] + np.arange(32)` materializes an (n_lines,32) int64 index array plus a contiguous copy (~8x the hash bytes), and never validates line length so a short final line reads past its newline. Slice with stride or validate uniform width. |
dnv3-perf-01 | open | high | deduplicate-by-namev3.py:69-75 — `cdist` builds a full N×N matrix (~900MB at N=30k, O(N²) memory) with no guard; large inputs OOM. Add an N threshold that falls back to bucketed iteration, or chunk the matrix. | scalability
hr-perf-01 | open | med | hash-recursive-ai5.py:763,872 — `stage1_paths` and `rep` are two separate O(N×aliases) passes over candidates. Build `rep` only for keys that survive to hashing, or fuse it with the stage1_paths build. | cpu/passes
hr-perf-02 | open | low | hash-recursive-ai5.py:764,785 — `stage1_bytes`/`stage2_bytes` do a full O(N) sum solely to pick serial-vs-threaded; short-circuit once THREAD_THRESHOLD_BYTES is exceeded. | early-exit
oze-perf-01 | open | low | organize_by_extension.py:1464 — `plan_moves` validates order via `files != sorted(files)`, a full-copy O(n log n) sort every call that contradicts the streaming claim and duplicates organize()'s own sorted(files) at 1639 (double sort). Validate adjacency cheaply or drop the check. | SOLID/SRP
oze-perf-02 | open | med | organize_by_extension.py:1508 — `_preplan_resolve_collisions` eagerly builds pairs for ALL files and runs resolve_real_extension on every file before any move, priming head_cache for the whole tree at once and defeating the per-window RSS goal. Process in bounded chunks. |

## scalability

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-scal-02 | open | low | hash-recursive-ai5.py:856,860 — candidates (sorted list), cand_keys (set), inode_size, and aliases are all live simultaneously at peak. Free inode_size once candidates is computed. | peak-memory
hr-scal-01 | open | med | hash-recursive-ai5.py:861 — `aliases = {k: v ... if k in cand_keys}` rebuilds the whole dict, briefly doubling the candidate footprint and contradicting the streaming RSS goal. Mutate in place (del non-candidates) or skip non-candidate buckets in index_inodes. | memory
lq-scal-01 | open | med | link_queue.py:1001 — `_dispatch_immediate` spawns one daemon thread + subprocess per immediate-mode link with NO concurrency cap; a paste of N magnet/file links launches N simultaneous subprocesses. Add a bounded immediate pool / semaphore mirroring worker_count. |
lq-scal-02 | open | low | link_queue.py:3004 — `_update_link_count` re-parses the entire textarea via _parse_entries on the debounced timer; for very large pastes this is O(lines) on the UI thread each fire. Cache the count from add/paste-normalize or cap parsing. |
oze-scal-01 | open | med | organize_by_extension.py:1471 — `plan_moves` does list(files) and _preplan_resolve_collisions returns a fully materialized list[tuple] plus a needed_dirs set, so a 1M-file run holds the entire plan in memory despite the iterator return type; the docstring's streaming guarantee is not met. |

## concurrency

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-conc-02 | open | low | hash-recursive-ai5.py:176,215 — `out_q` is an unbounded SimpleQueue; if index_inodes consumes slower than walk workers produce on a huge tree, out_q grows without bound, undermining the low-RSS goal. Bound the queue or document the consumer-keep-up assumption. | backpressure
hr-conc-01 | open | med | hash-recursive-ai5.py:226-247 — a worker that re-raises a BaseException decrements inflight in finally, but directories it had not yet pushed are lost beyond the dir_error tick; a worker dying mid-scandir silently drops its subtree. Drain/mark the subtree on fatal worker exit. | threading
lq-conc-01 | open | low | link_queue.py:1592 — `_pick_next_item` is documented as a reader but MUTATES by_domain (pop), _domain_active (pop), and seq_of (sweep); safe under _dispatch_cv but the "domain-aware READER" contract understates it. Rename/document as a mutating claim helper. | SOLID-SRP
lq-conc-02 | open | low | link_queue.py:2382 — `__getattr__` delegates any missing attr to self.dispatcher; a typo on a real dispatcher attr is silently proxied with no recursion guard if dispatcher itself triggers __getattr__. Add a recursion guard. |
oze-conc-01 | open | med | organize_by_extension.py:1625 — `_drain` calls fut.result() which re-raises any non-OSError from a worker (RuntimeError from _unlink_with_rollback double-copy, or collision-cap exhaustion) on the main thread, aborting the whole run instead of skipping one file. Map these to MoveResult errors. | STRIDE-DoS
rf-conc-01 | open | med | relocate_folder.py:723 — `_replicate_ownership` walks src but chowns dst; a src entry created after the copy is chowned on a nonexistent dst silently. Walk dst for replication or snapshot. |
rf-conc-02 | open | low | relocate_folder.py:914 — `_run_verify_pool` calls shutdown(wait=False, cancel_futures=True) on error, but already-running SHA-256 threads keep reading files after the function returns; the subsequent rmtree(target) in _copy_and_verify races those readers. Use wait=True or join running futures before rmtree. |

## code complexity

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-cx-02 | open | low | hash-recursive-ai5.py:170-276 — `_WalkIter.__iter__` is a ~100-line method nesting worker()/coordinator()/finally-inflight/stats-merge; worker alone juggles a scanned flag across try/3×except/finally. Promote worker to a module-level function over shared state. | cognitive complexity
hr-cx-01 | open | med | hash-recursive-ai5.py:806-921 — `find_duplicate_groups` exceeds the cognitive-complexity ceiling: config defaulting, ingest-cap, overflow pruning, rep build, two stage orchestrations, an inline _accept_group closure, and stage2 assembly (~8 decisions + nested closure). Extract candidate-prep (849-872) into a helper. | cognitive complexity ≤10
ie-cmplx-01 | open | med | import_events.py:88-100 — `parse_llm_events` scans every char calling raw_decode on each `[`/`{` start — O(n) decode attempts over the whole string. Find the first `[`/`{` and decode once, or strip + json.loads. |
lq-cmplx-02 | open | low | link_queue.py:1360 — `_run_item` is ~120 lines handling shell/exec branch, timeout arming, streaming, hard-deadline wait, and 4 except clauses. Split the shell-vs-exec Popen construction into a `_spawn_proc(item,label)` helper. | cyclomatic>10
lq-cmplx-01 | open | med | link_queue.py:3305 — `_do_refresh_queue_list` mixes snapshot, diff-delete, diff-insert, number-cache update and overflow-row logic in one ~55-line method (>10 paths). Extract the tree-diff apply into a helper taking (desired, tree). | cyclomatic>10
oze-cmplx-01 | open | med | organize_by_extension.py:1544 — `organize()` is ~155 lines mixing scan, executor lifecycle, the nested _drain closure, backpressure submission loop, progress logging, stats logging, KeyboardInterrupt handling, and prune; branch count exceeds ≤10. Extract the execute stage into its own runner. | SOLID/SRP
rf-cmplx-01 | open | low | relocate_folder.py:1206 — `execute` mixes orchestration, state machine, idempotency, validation ordering, and a finally-logging side effect; the nested _advance/_copy_and_verify callback makes the swap-ordering invariant hard to audit. Extract a linear pipeline of named stages returning MigrationState. | SOLID-SRP

## code duplication

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnp-dup-01 | open | low | dedupl_numpy.py:13 — dedupl_numpy and remove-deduplv3 parse the same `<hash> <path>` format with divergent rules (fixed offset 26 vs split(None,1)) and can disagree on the same file. Extract a shared record-parsing helper. |
hr-dup-01 | open | low | hash-recursive-ai5.py:525-530 — bare `_head_batch`/`_tail_batch` shims duplicate the `_make_*_batch` closure bodies. Define the shims as `_make_head_batch(None)` / `_make_tail_batch(None)`. | DRY
lq-dup-02 | open | low | link_queue.py:194/4189 — `_template_has_bare_url` and `ProtocolEditor._validate` both re-implement "any m.group(1)=='url' over _PLACEHOLDER_RE"; _validate should call _template_has_bare_url. | DRY
lq-dup-01 | open | low | link_queue.py:3116/3154/3166 — `_get_sleep`, `_get_failure_sleep`, `_get_max_per_domain` are three near-identical try/int(float(var.get())) getters differing only in clamp+key. Collapse into one parametrized getter. | DRY
oze-dup-01 | open | med | organize_by_extension.py:1700 — `_count_prunable_dirs` duplicates the root-resolve, symlink-reject, post-order os.walk, and per-dir symlink logic of prune_empty_dirs (1310); the two will drift. Extract one traversal helper parameterized by a remove-vs-mark callback. | SOLID/DRY
rf-dup-01 | open | low | relocate_folder.py:679 — `_OWNERSHIP_WORKERS/_OWNERSHIP_INFLIGHT` and `_VERIFY_WORKERS/_VERIFY_INFLIGHT` (820) duplicate _resolved_jobs/_inflight_cap logic and are dead for the live path (pools use Plan.jobs); they remain only for tests. Delete the constants and update tests to call the helpers. | DRY
rf-dup-02 | open | low | relocate_folder.py:924 — `_capture_first` duplicates the exception-draining logic inlined in _run_verify_pool's on_done and _collect_chown_error; three near-identical future-exception collectors exist. Consolidate into one collector parametrized by first-only vs count. | DRY
rdv3-dup-01 | open | low | remove-deduplv3.py:107 — path/basename extraction logic duplicated across the dedup family with different semantics. Consolidate into one module. |

## architecture/modularity/SOLID

id | status | effort | description | notes
--- | --- | --- | --- | ---
hr-arch-02 | open | low | hash-recursive-ai5.py:100,849 — "no cap" is encoded as `2**31` in RunConfig then reverse-detected via `< 2**31` in the pipeline; the sentinel meaning is split across two classes. Store None or expose an explicit alias_cap_enabled. | leaky abstraction
hr-arch-03 | open | low | hash-recursive-ai5.py:396-400 — `_hash_file_windows` dispatches on `len(window) == 3` (tuple arity) to support raw tuples vs FileWindow; a stray 4-tuple legacy caller is mis-parsed silently. Normalise inputs to FileWindow at the boundary. | typing/Liskov
hr-arch-01 | open | med | hash-recursive-ai5.py:869 — `find_duplicate_groups` stashes `config.overflow = overflow` as a hidden side channel to _expand_keys_to_paths; two runs sharing a RunConfig clobber each other and it violates CQS. Thread overflow explicitly through on_group. | hidden coupling/SRP
ie-arch-02 | open | low | import_events.py:11-16 — module-level constants for model paths/URLs mix configuration with code; no override without editing source. Move to argparse/env vars. |
ie-arch-01 | open | med | import_events.py:208-221 — hardcoded `target_folder = "./events_data"` and no CLI args though process_folder accepts a param. Add argparse to accept folder + model paths. |
lq-arch-01 | open | med | link_queue.py:786,840 — Dispatcher reads module global STATE_FILE in _save_state/_load_state_items while ConfigStore takes injected paths; the state path is not injectable, so a headless test / second instance can't relocate it. Inject state_path into Dispatcher.__init__ for symmetry. | DIP
lq-arch-02 | open | low | link_queue.py:2411 — LinkQueueApp re-exports ~20 Dispatcher methods as class attrs purely so call sites resolve on the app; combined with __getattr__ proxying this is two overlapping delegation mechanisms. Pick one (prefer __getattr__). | SRP
mmr-arch-01 | open | low | masterclass-mass-rename.py:73-81 — `list_files` contains dead commented-out os.walk code; recursive intent abandoned. Remove dead code or implement recursion via a flag. |
rf-arch-02 | open | low | relocate_folder.py:50 — module-global LOG plus ContextVar _log_ctx plus _log() indirection is an ad-hoc DI mechanism for one dependency; every function calls _log() defeating testability gains. Pass logger through Plan or a small context object. | SOLID-DIP
rf-arch-01 | open | med | relocate_folder.py:104 — `_swallow_or_warn`/`_raise_or_fail` plus back-compat aliases `_safe`/`_required` (141) expose four names for two behaviors; the policy choice is scattered across call sites. Use a single typed wrapper taking an explicit policy enum, drop aliases. | SOLID-OCP

## decoupling

id | status | effort | description | notes
--- | --- | --- | --- | ---
lq-decoup-01 | open | low | link_queue.py:1818 — `_resolve_cwd` reads self.config["output_folder"] directly and logs via self._log, coupling cwd resolution to live config + UI log. Pass the folder value in so the worker-side helper is pure and unit-testable. |

## reliability/correctness

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-rel-01 | open | low | check-mx-domain.py:40 — usage error path writes usage to stdout while other errors go to stderr; inconsistent. Print usage to stderr. |
dnp-rel-01 | open | med | dedupl_numpy.py:22 — mmap is never closed and file `f` is closed at end of `with` while mmap still references the fd; accessing mm after f closes is platform-undefined. Use the mmap inside the `with` or close it explicitly. |
dnp-rel-02 | open | med | dedupl_numpy.py:32 — `line_starts[1:] = nl[:-1]+1` assumes the file ends with newline; if the last line has no trailing `\n`, its hash is silently dropped. Handle missing final newline. |
dnv3-rel-01 | open | med | deduplicate-by-namev3.py:85-88 — with dtype=uint8 and score_cutoff=threshold, above-cutoff cells are clipped to threshold+1; if threshold ≥ 255 the uint8 saturates and the `<= threshold` mask misbehaves. Validate threshold < 255 or widen dtype. |
hr-rel-05 | open | low | hash-recursive-ai5.py:585 — `if hasattr(exc, "add_note")` is tagged `pragma: no branch - 3.11+` but in-code comments claim 3.10 support where add_note is absent and the branch is taken; the pragma would mask a real branch on 3.10. Drop the pragma or pin the floor to 3.11. | coverage-pragma
hr-rel-02 | open | med | hash-recursive-ai5.py:872,494-500 — `rep` builds via _readable_rep, which calls os.access on every alias of a hardlink-heavy inode just to pick a representative (O(aliases) syscalls). Cap the probe to the first few aliases. |
hr-rel-01 | open | high | hash-recursive-ai5.py:884-887 — when on_group is supplied, groups stream out and final_groups stays empty, so a caller that also reads DedupResult.groups silently gets nothing. Make on_group vs batched-groups mutual exclusivity explicit or populate groups regardless. | contract/invariants
lq-rel-01 | open | med | link_queue.py:1001 — immediate-mode subprocess exit codes are discarded: _run_item returns an int but _dispatch_immediate ignores it, so a failing handler records no failure metric and never triggers cooldown. At minimum _record_metric on non-zero. |
lq-rel-02 | open | low | link_queue.py:1818 — `_resolve_cwd` docstring says it falls back to the script dir with a warning, but it returns None, making subprocess inherit the PROCESS cwd. Either return _script_dir() or fix the docstring. | doc/behavior mismatch
lq-rel-04 | open | low | link_queue.py:3251 — `_on_clear_queue` calls _save_state() synchronously on the UI thread (blocking YAML write) instead of _request_save_state(); a large in_flight set makes the click janky. Use the debounced path or run off-thread. |
lq-rel-03 | open | low | link_queue.py:3285 — `_remove_pending_urls` does `queue_items[:] = [filtered]`, triggering _PendingQueue.replace() which clear()s and re-appends every survivor, re-basing global-FIFO ordering. Use targeted remove() per URL instead of whole-queue rebuild. |
mmr-rel-01 | open | med | masterclass-mass-rename.py:101-103 — uses os.link then os.unlink to "rename"; hardlink fails across filesystems (EXDEV) and on directories. Use os.rename/shutil.move uniformly. |
mmr-rel-02 | open | med | masterclass-mass-rename.py:114 — clean_name can return an empty string (all tokens stripped) producing an empty target name passed to rename → error or silent loss. Skip/raise when cleaned name is empty. |
nm-rel-01 | open | low | numero_magicov2.py:7 — LETTER_MAP uses English alphabet only; Portuguese names with accented letters (á, ç, ã) are silently dropped, skewing totals. Normalize via unicodedata before mapping. |
oze-rel-02 | open | med | organize_by_extension.py:1283 — `_move_cross_device` reserves a 0-byte target then copies+replaces; a crash between reserve and os.replace (1293) leaks a permanent 0-byte file that a later run's is_bucketed_file treats as real and choose_bucket counts toward BUCKET_SIZE. Reserve a temp name and rename, or sweep stale reservations at startup. | kill-safety
oze-rel-03 | open | low | organize_by_extension.py:1395 — `resolve_root` binds `resolved = Path(root)` without resolving then runs is_dir() on the un-canonicalized path (resolve only at the final return); a symlinked root is accepted. Resolve before the is_dir check and reject a symlinked root. | STRIDE-Tampering
oze-rel-01 | open | low | organize_by_extension.py:1424 — `make_worker._move_worker` catches only OSError, but move_file can raise RuntimeError (collision-cap exhaustion, double-copy at 1023) which escapes and kills the future. Broaden the except to convert RuntimeError into a skip-with-error tuple. |
rf-rel-02 | open | med | relocate_folder.py:159 — Plan.from_args uses source.absolute() not resolve(), so a source under a symlinked parent yields an unresolved path for os.walk and symlink creation while _check_cross_device/already_migrated use resolve(); device comparison and idempotency can disagree. Normalize consistently. |
rf-rel-05 | open | low | relocate_folder.py:278 — already_migrated compares resolve(strict=False) of link target vs target; an equivalent-but-different path (relative vs absolute) makes a migrated dir look not-migrated and re-copied into FileExistsError. Compare os.path.realpath consistently and test the relative-link case. |
rf-rel-06 | open | low | relocate_folder.py:614 — `_src_total_bytes` sums only S_ISREG sizes, but copytree also writes symlink inodes and directory entries; the disk-space precheck can pass then ENOSPC on a tree of many tiny dirs/symlinks. Add per-entry inode overhead estimate or widen headroom. |
rf-rel-04 | open | med | relocate_folder.py:1130 — atomic_swap never re-validates that plan.target exists and is a complete dir before destroying source; with --no-verify a silently-empty copy still replaces source with a symlink to a valid-but-wrong target. Assert target is non-empty (or matches expected entry count) before swap. |
rf-rel-01 | open | high | relocate_folder.py:1235 — atomic_swap is not atomic and leaves no recovery path if interrupted: a crash between os.rename(target,backup) (1113) and _create_symlink leaves source missing with only the backup, and nothing records that target already holds the good copy. Write a journal/marker file for restart recovery. |
rf-rel-03 | open | med | relocate_folder.py:1346 — `_copy_and_verify` does rmtree(plan.target, ignore_errors=True) on verify failure, silently discarding a half-good copy and ignoring all errors; a retry then hits FileExistsError in copy_tree (498) with no guidance. Log rmtree outcome; detect/clean leftover target on retry. |
rdv3-rel-01 | open | low | remove-deduplv3.py:107 — tiebreaker `max(... (basename_len, path))` keeps the lexicographically-greatest path on ties, but the docstring implies smallest. Confirm intended survivor and document/fix direction. |

## testing

id | status | effort | description | notes
--- | --- | --- | --- | ---
cmx-test-01 | open | med | check-mx-domain.py:27 — `has_mx_record` does real DNS resolution with no injection point for the resolver, untestable without network. Inject a resolver factory for unit tests. | test coverage
hr-test-01 | open | med | hash-recursive-ai5.py:328-335 — the partial-read loop in _read_window_into (kernel returns a short chunk then more) is the stage-2 correctness core but has no test using a fake file returning partial chunks; existing tests only cover full-EOF. Add a short-chunk fake. | branch coverage
ie-test-01 | open | med | import_events.py — no tests; parse_llm_events (pure, complex char-scan logic) is the highest-value unit-test target and is fully testable without the model. Add tests for malformed/embedded JSON. | test coverage
lq-test-01 | open | low | link_queue.py:694 — the metrics dict (timeouts/failures/completions) is recorded but never asserted or surfaced; add a headless Dispatcher test that drives a failing item and checks _record_metric increments. |
mmr-test-01 | open | low | masterclass-mass-rename.py:83 — clean_name is pure and token-heavy but untested; substring-stripping edge cases (mmr-sec-02) would be caught by tests. Add cases for `ams`/`cup`/empty-result. | test coverage
rf-test-02 | open | low | relocate_folder.py:515 — tracking_copy2 / progress_cb path is untested and unreachable from the CLI (see rf-obs-01), so the bytes-accounting and exception-swallowing at 523 have no coverage. Add a test driving copy_tree with progress_cb, or remove. |
rf-test-01 | open | med | relocate_folder.py:1130 — atomic_swap / _backup_target rollback path (rename-back-on-exception, restore-failure logging) has no observable hook and depends on filesystem rename failure; the restore-failure branch (1119) is effectively untestable. Inject a rename fn for fault simulation. |

## observability

id | status | effort | description | notes
--- | --- | --- | --- | ---
dnv3-obs-01 | open | low | deduplicate-by-namev3.py:51 — file open errors raise raw tracebacks; no user-facing message unlike remove-deduplv3 which handles OSError. Wrap open and exit cleanly. |
hr-obs-02 | open | low | hash-recursive-ai5.py:284 — `[ai5] hash failed for {path}: {exc}` lacks a severity tag, so it can't be greped apart from `[ai5] WARNING:` lines. Add a level prefix. | log structure
hr-obs-01 | open | low | hash-recursive-ai5.py:1170-1172,414-415 — summary `hash_errors=A+B` conflates benign vanished-file ENOENT with real EACCES/EIO; the count is misleading. Split benign-skip from real-error counters. | metric fidelity
ie-obs-01 | open | low | import_events.py:218 — no logging.basicConfig is configured, so logger.warning/error/exception output is suppressed by default; the script appears silent on errors. Configure logging in __main__. |
lq-obs-01 | open | med | link_queue.py:719 — self.metrics is incremented by _record_metric but never read, logged, or shown — pure dead observability. Surface counts in _update_status or a periodic log line. | wiring gap
lq-obs-02 | open | low | link_queue.py:1472 — the [done] exit line is logged per item but run/done lines are not correlated by a per-item id; with many workers interleaving, matching relies on the url field alone. Add a short item id to run/done/error lines. |
mmr-obs-01 | open | low | masterclass-mass-rename.py:113-115 — the rename loop has no per-file error handling; the first FileExistsError/OSError aborts the whole batch, leaving partial renames with no summary. Catch per-file and report a tally. |
oze-obs-01 | open | low | organize_by_extension.py:1860 — `_parse_extra_zip_family` silently drops malformed/over-length items, so a --extra-zip-family typo vanishes and files wrongly land under zip/. Log a warning per dropped item while keeping it non-fatal. | CLI integrity
rf-obs-02 | open | low | relocate_folder.py:1243 — execute's finally logs terminal state but FAILED is logged identically whether the failure was validation, copy, verify, or swap; no stage attribution. Record last successful state + failing stage in the log line. |
rf-obs-01 | open | low | relocate_folder.py:1332 — `_copy_and_verify` accepts a progress_cb but execute never passes one, so the whole progress-reporting machinery is dead in the CLI and a long copy shows no progress. Wire a default logging progress_cb or remove the unused param. |
