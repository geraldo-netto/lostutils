# Agents Behavior Guide

This file defines the expected behavior and usage model for AI agents working in this repository.

## Purpose

- Provide a standard set of guidelines for agent interactions.
- Ensure consistent behavior when using AI tooling in this workspace.

## General Agent Behavior

- Always be polite and concise.
- Prefer short, actionable responses.
- Respect workspace context and avoid guessing when information is missing.
- When making code changes, clearly describe what was changed and why.
- When editing files, include exact context around replacements to avoid ambiguity.

## Rules

- Don't assume. Don't hide confusion. Surface tradeoffs and ask the user when unclear.
- Write the minimum code that solves the problem. Avoid speculative or unneeded changes.
- Touch only what you must. Clean up only your own mess and leave the workspace cleaner than you found it.
- Define success criteria before making changes. Verify against those criteria and iterate until satisfied.
- Keep code complexity <= 10 for any new function, class, or method.
- Avoid code duplication and apply SOLID principles where practical.
- Document assumptions, constraints, and design intent in comments or commit notes when they matter.
- Do not add comments that restate what the code plainly says. Comment only the non-obvious: why a choice was made, a constraint, or a subtle edge case. Delete redundant comments rather than write them.
- Prefer explicit, maintainable solutions over clever shortcuts.
- Propose business/design patterns and DDD only when they improve clarity or structure.
- ALWAYS record review findings in `TODO.md` — never report them only in chat. Any time you
  scan, review, audit, or "look for issues" (not just major changes), add each finding to the
  matching category table in `TODO.md` before/while reporting it.
- ALWAYS remove completed items from `TODO.md` — once a finding is implemented + tested + merged,
  delete its row from the table outright. No "shipped" sub-sections, no struck-through entries.
  `git log` is the durable record. Exceptions: the "Open — parked" section keeps open-but-deferred
  items with a why-not-now annotation; the "Audit picks deliberately rejected" section keeps the
  rationale so future passes don't re-pick the same items.
- When making major changes, rescan the whole project and create or update `TODO.md` with one
  table per review category defined below. Each table uses the format:
  `id | status | effort | description | notes`.
- Keep every `TODO.md` table sorted by the `description` column. Each description starts with the
  affected `file:line`, so sorting clusters findings in the same file together — letting related
  items be fixed in one batch. Re-sort a table whenever you add or edit its rows.

### Category definitions

Use the lens that fits the finding; when a category names a framework, cite the specific
framework/law in the finding's `notes`.

- **security** — injection boundaries (command, path traversal, SQL, untrusted
  file/network input) and privilege boundaries (subprocess calls, file permissions,
  credential handling). Every code path that acts on untrusted input validates it first.
  Threat-model through complementary lenses and name the one used: **STRIDE** (Spoofing,
  Tampering, Repudiation, Information disclosure, Denial of service, Elevation of
  privilege) per data-flow boundary; the **OWASP ASVS** checklist where it maps; and
  **attack trees** to decompose a high-value target into concrete leaf attacks.
- **input validation / command safety** — file, network, and CLI inputs are validated
  before they are used to build shell, SQL, or subprocess commands; no argument injection
  via embedded spaces/newlines; command arguments are passed as argv lists, never a
  split string.
- **data integrity** — in-memory state stays consistent with the underlying files,
  database, or external state: no stale reference to a removed record, derived state kept
  in sync on add/remove/update.
- **data governance** — no private absolute paths (`/home/…`), secrets, or API keys are
  committed, with a guard (CI grep / pre-commit) enforcing it; logs minimize sensitive
  identifiers to what is needed and never leak them beyond the local session.
- **reliability / correctness** — logic bugs under normal flow.
- **robustness / recovery** — kill-safety, atomic writes (write-temp-then-rename),
  partial-state recovery (a dying process or interrupted operation can't corrupt state or
  orphan a resource), and cleanup of orphaned resources.
- **dependability** — stays useful when a dependency, provider, or optional subsystem
  fails: graceful degradation, retry/backoff with timeout coverage, fallback chains that
  stop before they amplify damage or hide partial failure.
- **observability / operability** — failures are *surfaced*, not merely logged; every
  background task exposes liveness + last result; timeouts are sensible and logging is
  actionable. Assess via the three pillars (logs / metrics / health), and a silent-failure
  audit — enumerate every way the system can degrade with no user-visible symptom.
- **concurrency** — concurrency-correctness on shared state and coordination; guards
  against interleaved updates and double-submit.
- **multithreading** — thread-safety and thread-resource issues beyond concurrency:
  background-thread lifecycle, swallowed futures whose exceptions are never checked, and
  lock granularity.
- **distributed systems** — multi-process coordination (even on one box): lock
  correctness, idempotent re-runs, shared-resource contention, and partial-write
  durability across processes.
- **watchdog** — liveness/stall detection for long-running operations (downloads,
  transfers, scans): timeouts, heartbeats, progress-stall detection, and automatic
  abort/recovery semantics.
- **state machine integrity** — every lifecycle transition guards illegal transitions,
  prevents terminal-state re-entry, and cleans up on every error path — not just the
  cancel path.
- **time & scheduling correctness** — elapsed-time math uses a monotonic clock; timeouts
  and intervals are keyed so replay or clock skew never double-fires or stalls; guard
  zero/negative elapsed time.
- **platform** — cross-OS/runtime portability: POSIX-only primitives, signal handling,
  path/encoding assumptions, and Python version assumptions; production-vs-local
  divergence.
- **performance** — bottlenecks on hot paths (large-file scans, tight loops, repeated
  I/O).
- **scalability** — behavior as inputs, files, and data volume grow.
- **N+1 / call efficiency** — avoid per-row repeated queries or I/O where one batched
  read suffices; batch lookups; no fan-out of one round-trip per item.
- **caching strategy** — every cache declares key shape + size cap + invalidation trigger
  + a public reset hook; derived state invalidates on source change.
- **data structure** — right structures on hot paths: sets/maps for membership, no O(N²)
  dedup, no per-item re-parse where a cache belongs.
- **memory and cpu management** — peak memory, streaming vs materialization, and CPU-heavy
  work kept off the critical path.
- **code complexity** — cognitive complexity ≤ 10; fat functions split into helpers.
- **code duplication** — shared logic (input validation, I/O, command building) lives in
  one place, not copy-pasted across modules.
- **architecture / modularity / SOLID** — proper boundaries: CLI/entry thin, business
  logic in functions/modules, I/O and external access behind a clear layer, no logic
  buried in glue code.
- **system design** — end-to-end subsystem boundaries and feedback loops: whether the
  architecture preserves isolation, operability, and extension seams across module
  boundaries.
- **decoupling** — separation of concerns across module seams; modules are independently
  testable.
- **composition** — prefer small collaborators and explicit composition over god
  objects, inheritance-heavy shapes, and copy-pasted registries when that reduces
  coupling.
- **dependency** — third-party and optional imports are justified, pinned sensibly, and
  degrade gracefully when absent; a vendor dependency sits behind an app-owned adapter
  rather than being imported across the codebase.
- **configuration discoverability** — every runtime knob (env var / config file / CLI
  flag) has a default, a typed accessor, documented coverage, validation where needed, and
  tests for security-sensitive defaults.
- **API contract & compatibility** — public function signatures, CLI interfaces, and
  file/data formats are reviewed as compatibility artifacts: stable signatures, the full
  error surface declared, and breaking changes that are deliberate, named, and versioned.
- **CLI / option integrity** — command options, help text, and defaults match actual
  behavior across each script's entry point; ignored or misleading flags are findings.
- **wiring gaps** — shipped functions, modules, or commands that exist and pass tests but
  are not connected to the runtime path expected by docs or tests. A feature is "shipped"
  only when the entry point actually invokes it.
- **unused code** — public-shaped functions/handlers with no caller, no test, no use; each
  finding records keep / inline / delete.
- **unused functions/methods** — narrower grep-proven dead or test-only callable symbols
  (no leading `_`, imported by no production code), including `__init__` re-exports no
  caller pulls; each finding records delete / wire / intentionally keep in `notes`.
- **legacy / deprecation** — back-compat shims whose constituency is grep-proven gone are
  flagged to remove; still-live shims are recorded as "do not remove" with the live caller
  so a future pass doesn't re-pick them.
- **plugin extensibility** — advertised extension points stay open through registries and
  documented contracts rather than closed `if`/`switch` dispatch or private-only hooks.
- **adaptability** — hardcoded assumptions that block change without a code edit: magic
  numbers, locale/timeout/path constants, and lookup maps that should be config or a
  documented invariant.
- **business / design patterns / DDD** — apply patterns only when they remove a concrete
  pain; a missing pattern is a finding only when a named pattern would clarify a real
  boundary or lifecycle.
- **release & deploy engineering** — the path from green CI to a healthy installed build
  is engineered, not improvised: CI gates fail closed and mirror reality (job ordering,
  smoke tests against the real build, pinned actions, reproducible builds from committed
  lockfiles), with a documented upgrade/rollback story.
- **UI / UX** — CLI/output surfaces are usable; empty/error states are handled; flows
  work as documented. Where a user-facing interface exists, assess through the **Laws of
  UX** ([lawsofux.com](https://lawsofux.com/)) and cite the relevant law per finding.
- **accessibility** — where a user-facing interface exists: clear output, keyboard
  navigation, focus management, and sufficient contrast.
- **product engineering** — shipped-default sanity, setup/onboarding friction, actionable
  runtime failures, and docs-vs-behavior drift from an end-user perspective.
- **design thinking** — user-centered empty/error states, recovery paths, and decisions
  grounded in observed user needs rather than internal convenience.
- **documentation** — `README` and setup docs stay truthful: documented commands work as
  written and advertised features/flags match the code.
- **i18n** — user-facing strings route through a translation catalog where applicable; no
  hard-coded locale assumptions on user surfaces.
- **purpose** — mission alignment to the script's stated utility; scope-creep flagged.
- **test coverage** — new functions carry focused unit/feature coverage before merge
  (≥80% target in CI); critical paths — input validation, command building, I/O, data
  transforms — carry focused tests.
- **test / fuzz coverage** — property/fuzz/adversarial coverage exists for parsers and
  command builders (file parsing, shell/SQL argument construction, network inputs),
  concurrency, and interface contracts; counts toward the same coverage gate.

## File Editing

- Avoid overwriting existing files unless the user explicitly asks or the file is missing.
- For text edits, preserve surrounding context and keep modifications minimal.
- Use repository-specific structure and conventions when adding or updating files.

## Communications

- Use headings and bullets for readability.
- Highlight changed files and key points.
- Keep final answers brief and professional.

## References

- This workspace currently contains only a small Python utility script, so agent actions should remain lightweight and focused.
