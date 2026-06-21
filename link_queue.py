#!/usr/bin/env python3
"""
Link Processing Queue — a cross-platform GUI for routing links to commands.

Depending on a link's protocol (URL scheme), it is either:
  * processed IMMEDIATELY in a background thread, or
  * appended to a QUEUE that is consumed one item at a time with a configurable
    sleep between consumptions.

Cross-platform: runs on Windows, macOS and Linux using only the Python standard
library (Tkinter). On some Linux distros you may need to install the Tk
bindings for Python (e.g. `sudo apt install python3-tk`).

Command templates support the following placeholders:
    {url}          the raw URL
    {url_quoted}   the URL, shell-quoted (recommended when shell=True)
    {protocol}     the URL scheme (http, https, ftp, magnet, ...)

Config is persisted to ~/.link_queue_config.json.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import errno
import hashlib
import itertools
import json
import os
import tempfile
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import namedtuple
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlparse

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "error: PyYAML is required. Install with:\n"
        "    pip install pyyaml\n"
    )
    sys.exit(2)

# Prefer the libyaml C extension when available — pure-Python PyYAML is the
# default and is ~10-20x slower for the kind of (de)serialization we do
# (the state file at every queue mutation, the config file at startup).
# We expose a single (load, dump) pair that always uses the fast path
# when possible and falls back transparently. The which-version-am-I-using
# flag is logged at app startup so the user can see it.
try:
    from yaml import CSafeLoader as _YamlLoader, CSafeDumper as _YamlDumper
    YAML_USING_LIBYAML = True
except ImportError:  # pragma: no cover
    from yaml import SafeLoader as _YamlLoader, SafeDumper as _YamlDumper
    YAML_USING_LIBYAML = False


def _yaml_load(stream):
    """Load YAML using the fastest safe loader available."""
    return yaml.load(stream, Loader=_YamlLoader)


def _yaml_dump(data, stream, **kwargs):
    """Dump YAML using the fastest safe dumper available."""
    return yaml.dump(data, stream, Dumper=_YamlDumper, **kwargs)


def _yaml_emittable(s: str) -> bool:
    """True iff the active YAML backend can serialise `s`. The pure-Python
    PyYAML emitter rejects C1 control chars (NEL \\x85) and the line/paragraph
    separators even with allow_unicode=True; the libyaml backend tolerates
    those but still chokes on lone surrogates (\\udcXX, as produced by
    os.fsdecode of undecodable bytes) with a UnicodeEncodeError. Either way a
    URL carrying such a char would make _save_state raise and silently lose
    the queue item (rel-03), so we treat it as needing a base64 sidecar.
    """
    try:
        yaml.dump(s, Dumper=_YamlDumper, allow_unicode=True)
        return True
    except (yaml.YAMLError, UnicodeError, ValueError):
        return False


def _encode_state_field(entry: dict, key: str, value) -> None:
    """Store `value` under `key`, falling back to a base64 sidecar key
    ("<key>_b64") when the string can't be emitted as YAML (rel-03). Keeps
    normal URLs human-readable in the state file; only pathological ones get
    base64-wrapped. surrogatepass keeps lone surrogates round-trippable."""
    if isinstance(value, str) and not _yaml_emittable(value):
        raw = value.encode("utf-8", "surrogatepass")
        entry[key + "_b64"] = base64.b64encode(raw).decode("ascii")
    else:
        entry[key] = value


def _decode_state_field(entry: dict, key: str, default: str) -> str:
    """Inverse of _encode_state_field: prefer the base64 sidecar if present,
    else the plain key. Tolerates corrupt base64 by returning `default`."""
    b64 = entry.get(key + "_b64")
    if isinstance(b64, str):
        try:
            return base64.b64decode(b64.encode("ascii")).decode("utf-8", "surrogatepass")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            # lq-rel-05: narrow the exception so MemoryError, KeyboardInterrupt,
            # SystemExit etc. propagate instead of being silently swallowed as
            # "corrupt base64".
            return default
    val = entry.get(key, default)
    return val if isinstance(val, str) else default


CONFIG_FILE_NAME = "link_queue_config.yaml"
LEGACY_CONFIG_FILE_NAME = "link_queue_config.json"
STATE_FILE_NAME = "link_queue_state.yaml"

# Log-file sink (obs-02): cap the active file at 10 MB, rotating to one
# previous generation ("<path>.1") so total on-disk use stays bounded.
LOG_SINK_MAX_BYTES = 10 * 1024 * 1024


def _script_dir() -> str:
    """Directory the script lives in. Tests can monkey-patch this by
    overriding CONFIG_FILE / LEGACY_CONFIG_FILE / STATE_FILE directly on
    the module."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        # __file__ is missing if run via an embedded interpreter.
        return os.getcwd()


def _user_config_dir() -> str:
    """lq-sec-02: per-user config directory ($XDG_CONFIG_HOME/link_queue or
    ~/.config/link_queue), created with mode 0o700 so a co-tenant on a
    shared install can't rewrite ``default_command`` to inject a shell
    payload. Falls back to the script dir only if the user dir cannot be
    created (read-only $HOME, etc.) so legacy single-user installs keep
    working."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    target = os.path.join(base, "link_queue")
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        try:
            # Tighten perms even if the dir already existed with looser bits.
            os.chmod(target, 0o700)
        except OSError:
            pass
        return target
    except OSError:
        return _script_dir()


def _resolve_state_path(name: str) -> str:
    """Prefer the per-user dir; fall back to a pre-existing file in the
    script dir so an upgrade doesn't orphan an in-flight queue."""
    user_path = os.path.join(_user_config_dir(), name)
    if os.path.exists(user_path):
        return user_path
    legacy_path = os.path.join(_script_dir(), name)
    if os.path.exists(legacy_path):
        return legacy_path
    return user_path

CONFIG_FILE = _resolve_state_path(CONFIG_FILE_NAME)
LEGACY_CONFIG_FILE = _resolve_state_path(LEGACY_CONFIG_FILE_NAME)
STATE_FILE = _resolve_state_path(STATE_FILE_NAME)

_PLACEHOLDER_RE = re.compile(r"\{(url_quoted|protocol|url)\}")


def _queue_iid_for_url(url: str) -> str:
    """Stable, Tk-safe Treeview iid for a pending queue row keyed by URL
    (rel-12). Tk reserves a few characters in iids — using the URL verbatim
    breaks `tree.exists()` silently on inputs containing ``{``, ``}`` or
    whitespace. 16 hex chars of blake2b is collision-safe at queue scale
    (10k items: <1e-23) and stable across re-renders for the same URL."""
    h = hashlib.blake2b(url.encode("utf-8", "surrogatepass"), digest_size=8)
    return f"p:{h.hexdigest()}"


def _template_has_bare_url(template: str) -> bool:
    """True iff `template` contains a `{url}` placeholder (not `{url_quoted}`),
    used by sec-02 checks to flag shell-injection-prone configurations."""
    return any(m.group(1) == "url" for m in _PLACEHOLDER_RE.finditer(template or ""))

# One item in the processing queue. Templates + shell flag are kept in full so
# the worker always has the source of truth (the resolved display string is
# recomputed for the tree at render time).
# `extra` holds mapped (flag, value) pairs parsed from prefix tokens on the
# input line (e.g. "f:clip.mp4" -> ("-o", "clip.mp4")); appended to the command
# at run time. Defaults to () so older call sites that build a 4-field item are
# unaffected.
QueueItem = namedtuple("QueueItem", "url protocol template shell extra")
QueueItem.__new__.__defaults__ = ((),)

# lq-scal-03: default threshold for the opportunistic stale-key sweep
# in `_pick_next_item`. The sweep runs only when `len(seq_of) - len(urls)`
# exceeds this, so pick-latency is O(domains) under steady state.
# Tuned conservatively: with churn rate of ~1000 picks/sec, 256 keeps
# the sweep cadence at < 1/sec while bounding memory drift.
# lq-decoup-04: the live value is read from `DEFAULT_CONFIG` via
# `_seq_of_sweep_gap()`, so a power user with a million-item churn
# session can tune the threshold via config without editing source.
# The module-level constant stays for legacy callers that import the
# bare name (tests).
_SEQ_OF_SWEEP_GAP = 256

DEFAULT_CONFIG = {
    "seq_of_sweep_gap": _SEQ_OF_SWEEP_GAP,   # lq-decoup-04
    "sleep_between_items": 5,
    "worker_count": 1,
    "failure_sleep_seconds": 300,   # 5 minutes — global cooldown after a failure
    "command_timeout_seconds": 0,   # scal-04: per-item subprocess wall-time cap;
                                    # 0 = off (wait forever). When > 0, a hung
                                    # yt-dlp / aria2c gets terminate-then-kill so
                                    # the worker can pick up the next item.
    "max_per_domain": 0,            # 0 = no cap; otherwise cap concurrent workers per domain
    "log_verbosity": "summary",     # "summary" = first line + 25/50/75% milestones + exit
                                    # "verbose" = every line of subprocess output
                                    # "silent"  = just run + exit lines, no subprocess output
    "log_max_lines": 100,           # cap on lines kept in the log panel; 0 = unlimited
                                    # protects against an unbounded log over a long-running
                                    # session in verbose mode (where a few workers can spew
                                    # thousands of lines/minute)
    "log_file": "",                 # optional path: every log line is also appended here
                                    # (captures lines even if the in-memory panel drops or
                                    # truncates them); empty = panel only (obs-01)
    "queue_render_limit": 2000,     # max pending rows drawn in the tree; extras collapse to a
                                    # single "… N more" row so a huge backlog can't make the
                                    # Tk Treeview itself the bottleneck. 0 = render all (scal-02)
    "token_mappings": {             # input-line prefix -> command flag: a token like "f:clip.mp4"
        "f:": "-o",                 # becomes the flag + value ("-o clip.mp4") appended to the
    },                              # command. The rest of the line is the URL.

    "output_folder": "/backups/disk4",  # cwd for all protocol actions; empty = script dir
    "default_mode": "queue",
    "default_command": "echo {url}",
    "default_shell": False,
    "protocols": {
        "http":    {"mode": "queue",     "shell": False, "command": "echo [http] {url}"},
        "https":   {"mode": "queue",     "shell": False, "command": "echo [https] {url}"},
        "ftp":     {"mode": "queue",     "shell": False, "command": "echo [ftp] {url}"},
        "magnet":  {"mode": "immediate", "shell": False, "command": "echo [magnet] {url}"},
        "file":    {"mode": "immediate", "shell": False, "command": "echo [file] {url}"},
    },
}


# ---------------------------------------------------------------------------
# Pending queue with O(1) dispatch indexes
# ---------------------------------------------------------------------------


class _PendingQueue:
    """Ordered collection of pending QueueItems, keyed internally by URL so
    the dispatcher's hot paths are all O(1)/O(#domains) (perf-01/02/03):

      * ``urls``      dict url -> item: O(1) duplicate check, O(1) removal
                      (perf-02/perf-03), no per-claim list scan.
      * ``by_domain`` domain -> ordered {url: item}: the claim picker iterates
                      DOMAINS (few) and reads each domain's FIFO head in O(1)
                      (perf-01 / scal-01).
      * ``seq_of``    url -> monotonic insertion seq: global-FIFO tie-break
                      between domains with equal active-worker counts.

    URLs are unique among pending items (the dispatcher dedupes on enqueue),
    which is what lets the URL key double as identity. This is a composition
    (not a list subclass), so there are no un-synced list mutators to leak
    through (rel-04); the small list-like surface the call sites use is
    implemented explicitly below.

    Concurrency contract (conc-03)
    ------------------------------
    ``_PendingQueue`` is NOT thread-safe on its own. Every mutator
    (``append``, ``remove``, ``__setitem__``, ``__delitem__``) AND every
    domain-aware reader (``head_of_domain``, ``__iter__``) requires the
    caller to hold the dispatcher's ``queue_lock`` (the same lock that
    backs ``Dispatcher._dispatch_cv``). The ``Dispatcher`` and its
    ``_batch_dispatch`` context manager hold the lock around their queue
    touch points so callers don't need to think about it — but utility
    code that pokes at a ``_PendingQueue`` directly MUST acquire the lock
    or risk skewing ``urls`` / ``by_domain`` / ``seq_of`` against each
    other (which silently desyncs the dispatcher's FIFO + domain-cap
    invariants).
    """

    def __init__(self, iterable=(), *, domain_fn):
        self._domain_fn = domain_fn
        self._next_seq = 0
        self.urls: dict = {}          # url -> item (insertion-ordered = FIFO)
        self.by_domain: dict = {}     # domain -> {url: item} (FIFO within)
        self.seq_of: dict = {}        # url -> insertion seq
        self._ordered_values = None
        for it in iterable:
            self.append(it)

    # -- mutators -----------------------------------------------------------
    def _invalidate_order_cache(self) -> None:
        self._ordered_values = None

    def _values_by_index(self):
        if self._ordered_values is None:
            self._ordered_values = list(self.urls.values())
        return self._ordered_values

    def append(self, it) -> None:
        url = it.url
        if url in self.urls:          # dedupe is the caller's contract; keep
            self.urls[url] = it       # first position/seq, refresh payload
            self._invalidate_order_cache()
            return
        self.urls[url] = it
        self.by_domain.setdefault(self._domain_fn(it), {})[url] = it
        self.seq_of[url] = self._next_seq
        self._next_seq += 1
        self._invalidate_order_cache()

    def remove(self, it) -> None:
        url = it.url
        if url not in self.urls:
            raise ValueError("item not in pending queue")
        del self.urls[url]
        self.seq_of.pop(url, None)
        d = self._domain_fn(it)
        bucket = self.by_domain.get(d)
        if bucket is not None:  # pragma: no cover - withdrawn-root Tk early-exit
            bucket.pop(url, None)
            if not bucket:
                del self.by_domain[d]
        self._invalidate_order_cache()

    def clear(self) -> None:
        self.urls.clear()
        self.by_domain.clear()
        self.seq_of.clear()
        self._next_seq = 0
        self._invalidate_order_cache()

    def replace(self, items) -> None:
        """Atomically reset the queue to `items` (used by slice-assign)."""
        self.clear()
        for it in items:
            self.append(it)

    # -- list-like read surface ---------------------------------------------
    def __iter__(self):
        return iter(self.urls.values())

    def __len__(self) -> int:
        return len(self.urls)

    def __bool__(self) -> bool:
        return bool(self.urls)

    def __contains__(self, it) -> bool:
        return getattr(it, "url", it) in self.urls

    def __getitem__(self, key):
        values = self._values_by_index()
        if isinstance(key, int):
            return values[key]
        return values[key]

    def __setitem__(self, key, value) -> None:
        # Only whole-queue slice assignment (queue_items[:] = [...]) is used.
        if isinstance(key, slice) and key == slice(None):
            self.replace(value)
        elif isinstance(key, slice):
            items = list(self.urls.values())
            items[key] = value
            self.replace(items)
        else:
            raise TypeError("element assignment by index is not supported")

    def __eq__(self, other) -> bool:
        if isinstance(other, _PendingQueue):
            other = list(other)
        return list(self.urls.values()) == other

    __hash__ = None


# ---------------------------------------------------------------------------
# Configuration store
# ---------------------------------------------------------------------------


class ConfigStore(dict):
    """Owns the configuration and its persistence (arch-03): load (YAML,
    migrating a legacy JSON file), schema-normalize, and save. A dict subclass
    so existing ``config[...]`` access is unchanged, but the file I/O that used
    to live on LinkQueueApp is encapsulated here. The file paths are injected
    (dec-03) rather than read from module globals, so the store carries its own
    location instead of depending on module state.
    """

    def __init__(self, config_file: str, legacy_file: str):
        super().__init__()
        self.config_file = config_file
        self.legacy_file = legacy_file
        self.update(self._load())

    def _load(self) -> dict:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))   # deep copy of defaults
        user, migrated = self._read_user_config_dict()
        self._merge_user_config(cfg, user)
        self._normalize_config_schema(cfg)

        # Ensure a YAML config exists after first load: write defaults on a
        # fresh install, or the merged contents when migrating from JSON. The
        # legacy JSON, if any, is left untouched as a backup.
        wrote_first_run = False
        if not os.path.exists(self.config_file):
            try:
                self._write_config_file(cfg)
                wrote_first_run = not migrated
            except Exception as e:  # pragma: no cover - config-write OSError on first-run
                print(f"[warn] could not create {self.config_file}: {e}",  # pragma: no cover - stderr emit on config-write failure
                      file=sys.stderr)  # pragma: no cover - stderr emit on config-write failure
        if migrated:
            print(f"[info] migrated config {self.legacy_file} -> {self.config_file}",
                  file=sys.stderr)
        elif wrote_first_run:
            print(f"[info] created default config at {self.config_file}",
                  file=sys.stderr)
        return cfg

    def save(self) -> None:
        """Write the current config to disk (raw — gating/deferral is the
        caller's concern)."""
        self._write_config_file(dict(self))

    def _read_user_config_dict(self) -> "tuple[dict | None, bool]":
        """Read the user's config from YAML if present, falling back to
        legacy JSON. Returns (user_dict_or_None, migrated_from_json).
        Tolerates missing/corrupt files by returning (None, False)."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    return (_yaml_load(f) or {}), False
            except Exception as e:
                print(f"[warn] could not read {self.config_file}: {e}",
                      file=sys.stderr)
                return None, False
        if os.path.exists(self.legacy_file):
            try:
                with open(self.legacy_file, "r", encoding="utf-8") as f:
                    return json.load(f), True
            except Exception as e:  # pragma: no cover - legacy-config read OSError
                print(f"[warn] could not read legacy {self.legacy_file}: {e}",  # pragma: no cover - stderr emit on legacy-config failure
                      file=sys.stderr)  # pragma: no cover - stderr emit on legacy-config failure
                return None, False  # pragma: no cover - return after legacy-config failure
        return None, False

    @staticmethod
    def _merge_user_config(cfg: dict, user: "dict | None") -> None:
        """Merge user-provided keys into cfg in place. Protocols are merged
        protocol-by-protocol so built-ins survive if the user file only
        overrides a subset."""
        if not isinstance(user, dict):
            return
        for k, v in user.items():
            if k == "protocols":
                # lq-rel-01: a non-dict `protocols:` (null/list/scalar) must
                # NOT overwrite the default protocols dict — doing so makes
                # _normalize_config_schema crash on `cfg["protocols"].items()`
                # at startup. Skip it so the built-in protocols survive.
                if not isinstance(v, dict):
                    print(
                        f"[warn] config: ignoring non-dict 'protocols' "
                        f"(got {type(v).__name__}); keeping defaults",
                        file=sys.stderr,
                    )
                    continue
                for name, pc in v.items():
                    if isinstance(pc, dict):
                        cfg["protocols"][name] = dict(pc)
            else:
                cfg[k] = v

    @staticmethod
    def _normalize_config_schema(cfg: dict) -> None:
        """Ensure every protocol has a complete (mode, command, shell)
        triple, and that scalar config values have the right types.

        Drops any non-dict `protocols[<name>]` entries, but warns about each
        one (rel-10): a corrupt config that silently loses protocols is
        worse than one that surfaces the keys it threw away."""
        for name, pc in list(cfg["protocols"].items()):
            if not isinstance(pc, dict):
                print(
                    f"[warn] config: dropping non-dict protocol entry "
                    f"{name!r} (got {type(pc).__name__}); the rest of the "
                    f"config will load normally",
                    file=sys.stderr,
                )
                cfg["protocols"].pop(name, None)
                continue
            pc.setdefault("mode", "queue")
            pc.setdefault("command", "echo {url}")
            pc.setdefault("shell", False)
            pc["shell"] = bool(pc["shell"])
        cfg.setdefault("default_shell", False)
        cfg["default_shell"] = bool(cfg.get("default_shell", False))
        # token_mappings: keep only str-prefix -> str-flag entries.
        tm = cfg.get("token_mappings")
        if not isinstance(tm, dict):
            tm = {}
        cfg["token_mappings"] = {
            str(k): str(v) for k, v in tm.items()
            if isinstance(k, str) and k and isinstance(v, str)
        }
        # sec-02: warn (load time) on shell=True + bare {url} — the URL would
        # be substituted UNQUOTED into the /bin/sh -c string, opening a
        # shell-injection path for a crafted URL. The protocol editor already
        # warns on save; this catches hand-edited configs and the default.
        for name, pc in cfg["protocols"].items():
            if pc.get("shell") and _template_has_bare_url(pc.get("command", "")):
                print(f"[warn] protocol '{name}' uses shell=True with bare "
                      "{url} — prefer {url_quoted} (shell-injection risk)",
                      file=sys.stderr)
        if cfg.get("default_shell") and _template_has_bare_url(cfg.get("default_command", "")):
            print("[warn] default_command uses shell=True with bare "
                  "{url} — prefer {url_quoted} (shell-injection risk)",
                  file=sys.stderr)

    def _write_config_file(self, cfg: dict) -> None:
        with open(self.config_file, "w", encoding="utf-8") as f:
            _yaml_dump(
                cfg, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------


class WorkerPool:
    """Owns the worker-thread lifecycle (cx-03): spawn, retire and count live
    threads. Dispatch-agnostic — it just runs ``worker_target(idx, stop_self)``
    in daemon threads and can scale the live count up or down. ``wake`` is
    called after retiring so workers blocked on the dispatch CV observe the
    stop signal immediately instead of waiting out their timeout."""

    MAX_WORKERS = 32

    def __init__(self, worker_target, wake):
        self._target = worker_target
        self._wake = wake
        self.workers: list[dict] = []
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def lock(self):
        """Public guard for `workers`, so collaborators iterate the list safely
        without reaching into a private attribute (arch-05)."""
        return self._lock

    def ensure(self, target: int) -> int:
        """Scale to `target` live workers; returns the clamped target."""
        target = max(1, min(self.MAX_WORKERS, int(target)))
        with self._lock:
            # Purge dead threads, then count those not asked to stop as "live".
            self.workers = [w for w in self.workers if w["thread"].is_alive()]
            live = [w for w in self.workers if not w["stop_self"].is_set()]
            delta = target - len(live)
            if delta > 0:
                self._spawn(delta)
            elif delta < 0:
                self._retire(live, -delta)
        return target

    def _spawn(self, count: int) -> None:   # caller holds self._lock
        for _ in range(count):
            self._counter += 1
            idx = self._counter
            stop_self = threading.Event()
            t = threading.Thread(
                target=self._target, args=(idx, stop_self),
                daemon=True, name=f"queue-worker-{idx}")
            self.workers.append(
                {"idx": idx, "thread": t, "stop_self": stop_self})
            t.start()

    def _retire(self, live: list, count: int) -> None:  # caller holds self._lock
        for w in sorted(live, key=lambda w: w["idx"], reverse=True)[:count]:
            w["stop_self"].set()
        self._wake()

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for w in self.workers if w["thread"].is_alive())

    def stopping_count(self) -> int:
        with self._lock:
            return sum(1 for w in self.workers
                       if w["thread"].is_alive() and w["stop_self"].is_set())

    def counts(self) -> tuple[int, int]:
        """(alive, stopping) read in one critical section so the pair is
        mutually consistent (conc-01) — avoids alive/stopping coming from two
        different instants."""
        with self._lock:
            alive = sum(1 for w in self.workers if w["thread"].is_alive())
            stopping = sum(1 for w in self.workers
                           if w["thread"].is_alive() and w["stop_self"].is_set())
        return alive, stopping


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class Dispatcher:
    """UI-free core: owns the pending queue, the worker pool, per-domain
    dispatch, subprocess execution, the failure cooldown, and queue-state
    persistence (arch-02). It knows nothing about Tk — every interaction with
    the outside world goes through injected callbacks (dec-01):

        log(msg)            append a line to the log
        refresh()           ask the UI to repaint the queue
        status()            ask the UI to repaint the status bar
        marshal(ms, fn, *a) run fn on the UI thread (cross-thread safe)
        get_sleep()         current inter-item sleep (seconds)
        get_failure_sleep() current post-failure cooldown (seconds)
        save_config()       persist config (worker_count autoscale writes it)

    `config` is a ConfigStore (a read/write Mapping, not the bare dict it used
    to be — dec-02), read for protocol routing, output folder, log verbosity
    and worker count. The configuration is intentionally LIVE-shared with the
    UI: workers must observe a sleep/cap change the instant the user makes it,
    so an immutable snapshot would be wrong here; depending on the typed store
    (rather than a free-floating dict) is what makes that sharing explicit.
    The callbacks are stored under the same names the original methods used
    (self._log, self._refresh_queue_list, ...) so the relocated bodies are
    unchanged.
    """

    @classmethod
    def headless(cls, config=None) -> "Dispatcher":
        """Construct a Dispatcher with no-op callbacks for headless tests
        (test-02). Bypasses Tk entirely so the queue / dispatch / worker /
        config-store logic can be exercised without a DISPLAY.

        `config` accepts any Mapping (a plain dict works; ConfigStore is
        not required). Defaults to a copy of DEFAULT_CONFIG so each
        Dispatcher gets its own mutable snapshot.

        The returned instance still spawns worker threads if its pool is
        scaled up via `_ensure_worker_count` — set `stop_event` before
        teardown to keep tests clean."""
        if config is None:
            config = dict(DEFAULT_CONFIG)
        return cls(
            config,
            log=lambda _msg: None,
            refresh=lambda: None,
            status=lambda: None,
            marshal=lambda _ms, _fn, *_a, **_k: None,
            get_sleep=lambda: int(config.get("sleep_between_items", 5)),
            get_failure_sleep=lambda: int(config.get("failure_sleep_seconds", 300)),
            save_config=lambda: None,
        )

    def __init__(self, config, *, log, refresh, status, marshal,
                 get_sleep, get_failure_sleep, save_config, state_path=None):
        self.config = config
        self._log = log
        self._refresh_queue_list = refresh
        self._update_status = status
        self._safe_after = marshal
        self._get_sleep = get_sleep
        self._get_failure_sleep = get_failure_sleep
        self._save_config = save_config
        # arch-01: the state-file location is an instance attribute so a
        # headless test or a second instance can relocate it. Resolved at
        # construction (not import) so a monkeypatched module STATE_FILE is
        # honoured; passing `state_path` overrides the module default.
        self.state_path = state_path if state_path is not None else STATE_FILE

        # ---- queue + dispatch state (relocated from LinkQueueApp) ----
        # _PendingQueue keeps the url-set + per-domain indexes the picker and
        # dedupe rely on (perf-01/perf-02/scal-01) while still being a list.
        self.queue_items: list[QueueItem] = _PendingQueue(domain_fn=self._domain_of)
        self.queue_lock = threading.Lock()
        self._dispatch_cv = threading.Condition(self.queue_lock)
        self._domain_active: dict[str, int] = {}
        self._batch_dispatch_depth = 0
        self._batch_save_dirty = False
        self.current_items: dict[int, QueueItem | None] = {}
        self.metrics = {"timeouts": 0, "failures": 0, "completions": 0}
        self._metrics_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self._cooldown_until: float = 0.0
        # conc-04: RLock so the LinkQueueApp property accessor can acquire
        # this lock even when an outer scope (e.g. `_update_status`) already
        # holds it. With a plain Lock, the property re-entry deadlocks.
        self._cooldown_lock = threading.RLock()
        # Worker-thread lifecycle delegated to WorkerPool (cx-03); the pool runs
        # _worker_loop and is woken via _wake_workers after retirements.
        self._pool = WorkerPool(self._worker_loop, self._wake_workers)
        # conc-02: immediate-mode items run through a REAL bounded pool — a
        # work queue consumed by at most `_immediate_pool_size` long-lived
        # consumer threads. The old model spawned one blocked-on-a-semaphore
        # thread per item, so a paste of N links grew the thread list to N
        # before draining. Now the live-thread count is bounded by the pool
        # size regardless of paste size; excess items wait in the queue.
        self.immediate_threads: list[threading.Thread] = []
        self._immediate_lock = threading.Lock()
        self._immediate_q: "queue.Queue[QueueItem]" = queue.Queue()
        self._immediate_pool_size = self._immediate_concurrency()
        # Templates already flagged at runtime for the sec-02 shell+{url} check;
        # warn once per distinct template to avoid log spam.
        self._warned_shell_url_templates: set = set()

        # Debounced state persistence (scal-03): hot paths (enqueue, claim,
        # release) request a save rather than writing the whole file each
        # time; a single timer coalesces a burst into one write.
        self._save_delay = 1.0
        self._save_timer: "threading.Timer | None" = None
        self._save_timer_lock = threading.Lock()

    def _record_metric(self, name: str, amount: int = 1) -> None:
        with self._metrics_lock:
            self.metrics[name] = self.metrics.get(name, 0) + amount

    def _metrics_summary(self) -> str:
        """A compact, human-readable view of the run metrics for the status
        bar (obs-01: previously incremented but never surfaced). Omitted
        entirely while every counter is zero so a fresh session stays clean."""
        with self._metrics_lock:
            done = self.metrics.get("completions", 0)
            failed = self.metrics.get("failures", 0)
            timed_out = self.metrics.get("timeouts", 0)
        if not (done or failed or timed_out):
            return ""
        return f"{done} done, {failed} failed, {timed_out} timed out"

    # -- runtime state persistence (queue items) ----------------------------

    def _request_save_state(self) -> None:
        """Coalesce a burst of mutations into a single deferred write (scal-03).
        Thread-safe: _save_state touches no UI, so a background timer can run
        it directly. A request while a timer is already armed is a no-op.

        lq-conc-05: the Timer is constructed under ``_save_timer_lock`` but
        ``start()`` runs OUTSIDE the lock. Holding the lock during ``start()``
        risks the new Timer thread firing ``_flush_save_state`` and
        re-entering ``_save_timer_lock`` before the start frame returns, which
        on platforms with eager thread scheduling self-deadlocks."""
        with self._save_timer_lock:
            if self._save_timer is not None or self.stop_event.is_set():
                return
            timer = threading.Timer(self._save_delay, self._flush_save_state)
            timer.daemon = True
            self._save_timer = timer
        timer.start()

    def _flush_save_state(self) -> None:
        with self._save_timer_lock:
            self._save_timer = None
        self._save_state()

    def _cancel_save_timer(self) -> None:
        with self._save_timer_lock:
            t, self._save_timer = self._save_timer, None
        if t is not None:
            t.cancel()

    def _save_state(self) -> None:
        """Persist the queue to STATE_FILE so a restart can resume.

        Snapshot is taken under self.queue_lock and written via a uniquely
        named tempfile + atomic rename, so a concurrent reader (or a crash
        mid-write) cannot observe a half-written file.

        File format:
          queue:     list of pending items (queue_items)
          in_flight: list of items that were being processed when the app
                     was last shut down. On restart these are restored
                     ahead of the pending queue (FIFO) so they get retried.
                     For idempotent commands (yt-dlp, wget --continue,
                     curl, etc.) this is the right thing — they resume.
                     For non-idempotent commands the user can clear them
                     before resuming.

        Tolerates being called from any thread; concurrent calls produce
        last-write-wins on the file.
        """
        try:
            with self.queue_lock:
                pending = [self._serialize_item(it) for it in self.queue_items]
                in_flight = [
                    self._serialize_item(it)
                    for it in self.current_items.values() if it is not None
                ]
            # Use a tempfile in the same directory as STATE_FILE so the
            # final os.replace() stays on the same filesystem and is atomic.
            # The unique name prevents two concurrent writers from both
            # opening "STATE_FILE.tmp" and producing interleaved garbage —
            # which os.replace() cannot rescue once the bytes are mixed.
            d = os.path.dirname(self.state_path) or "."
            base = os.path.basename(self.state_path)
            fd, tmp = tempfile.mkstemp(prefix=base + ".", suffix=".tmp", dir=d)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    _yaml_dump(
                        {"queue": pending, "in_flight": in_flight}, f,
                        default_flow_style=False, sort_keys=False,
                        allow_unicode=True,
                    )
                os.replace(tmp, self.state_path)
                # lq-sec-03: os.replace preserves the destination's prior
                # mode bits. Older versions wrote STATE_FILE 0o644, which
                # leaks queued URLs (including auth tokens) to other users
                # on the host. Force 0o600 after every write.
                try:
                    os.chmod(self.state_path, 0o600)
                except OSError:
                    pass
            except Exception:
                # Best-effort cleanup of the orphaned tmp file.
                try: os.unlink(tmp)
                except OSError: pass  # pragma: no cover - best-effort tmp cleanup on state save
                raise
        except Exception as e:
            # Never let a state-save failure interrupt normal flow.
            try:
                self._log(f"[warn] could not save queue state: {e}")
            except Exception:  # pragma: no cover - defensive: _log itself raised
                pass  # pragma: no cover - defensive: _log itself raised

    @staticmethod
    def _serialize_item(it: QueueItem) -> dict:
        """One queue item -> a YAML-safe dict for the state file. String
        fields that YAML can't emit are base64-wrapped so no item is ever
        lost on save (rel-03)."""
        entry: dict = {"shell": bool(it.shell)}
        _encode_state_field(entry, "url", it.url)
        _encode_state_field(entry, "protocol", it.protocol)
        _encode_state_field(entry, "template", it.template)
        if it.extra:
            # Flatten the (flag, value) pairs to a shlex string; each pair
            # round-trips as two tokens. b64-wrapped if YAML-unsafe (rel-03).
            flat = [x for pair in it.extra for x in pair]
            _encode_state_field(entry, "extra", shlex.join(flat))
        return entry

    def _load_state_items(self) -> "tuple[list[QueueItem], list[QueueItem]]":
        """Read STATE_FILE if present and return (in_flight, pending).

        Tolerates missing/corrupt files (returns ([], [])). Files written
        by an older version that only had the "queue" key still load
        cleanly; in_flight will simply be empty.
        """
        if not os.path.exists(self.state_path):
            return ([], [])
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = _yaml_load(f) or {}
        except Exception as e:
            print(f"[warn] could not read {self.state_path}: {e}", file=sys.stderr)
            return ([], [])
        if not isinstance(data, dict):
            return ([], [])

        def parse_list(key: str) -> list[QueueItem]:
            raw = data.get(key) or []
            if not isinstance(raw, list):
                return []
            out: list[QueueItem] = []
            dropped = 0
            for entry in raw:
                if not isinstance(entry, dict):
                    dropped += 1
                    continue
                # Decodes both plain keys and the base64 sidecars written
                # for YAML-unsafe strings (rel-03); back-compatible with
                # state files written before the sidecar existed.
                url = _decode_state_field(entry, "url", "")
                if not url:
                    dropped += 1
                    continue
                # lq-rel-04: per-entry try/except so one malformed
                # `extra` field doesn't discard the entire restored
                # queue (the older `parse_list`-wide except did that).
                try:
                    extra_s = _decode_state_field(entry, "extra", "")
                    extra = ()
                    if extra_s:
                        flat = shlex.split(extra_s)
                        extra = tuple((flat[i], flat[i + 1])
                                      for i in range(0, len(flat) - 1, 2))
                    out.append(QueueItem(
                        url=url,
                        protocol=_decode_state_field(entry, "protocol", ""),
                        template=_decode_state_field(entry, "template", "echo {url}"),
                        shell=bool(entry.get("shell", False)),
                        extra=extra,
                    ))
                except (ValueError, TypeError) as exc:
                    dropped += 1
                    print(
                        f"[warn] state[{key}] entry {url!r} unparseable "
                        f"({exc}); dropped, other entries preserved",
                        file=sys.stderr,
                    )
            if dropped:
                print(
                    f"[info] state[{key}]: dropped {dropped} unparseable "
                    f"entries; kept {len(out)}",
                    file=sys.stderr,
                )
            return out

        return (parse_list("in_flight"), parse_list("queue"))

    def _restore_queue_from_state(self) -> None:
        """Push any persisted items back onto the queue at startup. Items
        that were in flight when the app last shut down are restored AHEAD
        of the pending queue (FIFO), so they get retried first. For
        idempotent commands (yt-dlp, wget --continue, curl, …) this is
        the right thing — they resume. For non-idempotent commands the
        user can clear them via the UI before doing anything else.

        Each restored QueueItem already carries the template + shell flag
        that was in effect when it was originally enqueued, so re-routing
        through the protocol config is unnecessary (and would be wrong if
        the user has since edited that protocol)."""
        in_flight, pending = self._load_state_items()
        items = in_flight + pending  # in-flight retries go FIRST
        if not items:
            return
        with self._dispatch_cv:
            for it in items:
                self.queue_items.append(it)
            self._dispatch_cv.notify_all()
        self._refresh_queue_list()
        self._update_status()
        if in_flight and pending:
            self._log(
                f"[restored] {len(in_flight)} in-flight + {len(pending)} "
                f"pending item(s) from previous session"
            )
        elif in_flight:
            self._log(
                f"[restored] {len(in_flight)} in-flight item(s) from previous "
                f"session (will retry)"
            )
        else:
            self._log(
                f"[restored] {len(pending)} item(s) from previous session"
            )


    @contextlib.contextmanager
    def _batch_dispatch(self):
        """Within this context, queue-add notifications AND state saves
        are deferred. Each call to _process_link still appends to
        queue_items, but does NOT wake any worker AND does NOT immediately
        write STATE_FILE. When the outermost context exits, a single
        notify_all() fires (so workers wake up to the FULLY-formed batch
        and can pick optimally based on domain spread) AND a single
        _save_state() fires if any deferred save accumulated.

        Without batching, calling _process_link in a tight loop (Add-All
        with N URLs) had two performance problems:
          1. Workers would grab items one-by-one as they're added,
             defeating per-domain spreading.
          2. Each add wrote the entire YAML state file (~55 ms per save
             with a 500-item queue) — so a 500-URL paste cost ~27 s.

        Re-entrant: a nested `with self._batch_dispatch():` does nothing
        until the outermost exits.
        """
        self._batch_dispatch_depth += 1
        try:
            yield
        finally:
            self._batch_dispatch_depth -= 1
            if self._batch_dispatch_depth == 0:  # pragma: no cover - withdrawn-root Tk early-exit
                with self._dispatch_cv:
                    self._dispatch_cv.notify_all()
                if self._batch_save_dirty:
                    self._batch_save_dirty = False
                    self._request_save_state()

    def _process_link(self, url: str, extra: tuple = ()) -> str:
        """Route one link (with optional mapped flags `extra`). Returns
        'queue', 'immediate', 'default', or 'duplicate' (if the URL was already
        pending/running on the queue)."""
        protocol = self._extract_protocol(url)
        mode, cmd_tpl, shell, outcome = self._resolve_protocol(protocol)
        if outcome == "rejected":
            return outcome
        item = QueueItem(url=url, protocol=protocol, template=cmd_tpl,
                         shell=shell, extra=tuple(extra))

        if mode == "immediate":
            self._dispatch_immediate(item)
            return outcome
        return self._enqueue_or_skip_duplicate(item, outcome)

    def _resolve_protocol(self, protocol: str) -> "tuple[str, str, bool, str]":
        """Pick (mode, command_template, shell, outcome) for a protocol.
        Falls back to the default_* config if the protocol isn't registered."""
        proto_cfg = self.config["protocols"].get(protocol)
        if proto_cfg is None:
            mode = self.config.get("default_mode", "queue")
            cmd_tpl = self.config.get("default_command", "echo {url}")
            shell = bool(self.config.get("default_shell", False))
            if shell and _template_has_bare_url(cmd_tpl):
                # sec-01: mirror the per-item runtime reject (_run_item) at
                # routing time so an unknown scheme can't be run through a
                # default_command that interpolates {url} UNQUOTED into the
                # shell. The reject is hard, not a warning.
                self._log(
                    f"[error] refusing default_command with shell=True and bare "
                    f"{{url}} for unknown protocol '{protocol}' — use {{url_quoted}}"
                )
                return mode, cmd_tpl, shell, "rejected"
            self._log(f"[warn] unknown protocol '{protocol}' — using default ({mode})")
            return mode, cmd_tpl, shell, "default"
        mode = proto_cfg.get("mode", "queue")
        cmd_tpl = proto_cfg.get("command", "echo {url}")
        shell = bool(proto_cfg.get("shell", False))
        return mode, cmd_tpl, shell, mode

    def _immediate_concurrency(self) -> int:
        """Concurrency cap for immediate-mode runners, mirroring the configured
        worker_count (scal-01). Clamped to >= 1 so a misconfigured 0 still
        admits one runner at a time."""
        try:
            return max(1, int(self.config.get("worker_count", 1)))
        except (TypeError, ValueError):
            return 1

    def _run_immediate_item(self, item: QueueItem) -> None:
        """Run one immediate item and record a failure/completion metric on its
        exit (rel-01) so immediate failures aren't invisible."""
        exit_code = self._run_item(item, "immediate")
        if exit_code != 0:
            self._record_metric("failures")
        else:
            self._record_metric("completions")

    def _immediate_consumer(self) -> None:
        """Bounded-pool consumer (conc-02): pull items off the immediate work
        queue and run them one at a time. Exits when the queue is idle and the
        app is stopping, so the pool drains cleanly at shutdown."""
        while True:
            try:
                item = self._immediate_q.get(timeout=0.25)
            except queue.Empty:
                if self.stop_event.is_set():
                    return
                continue
            try:
                self._run_immediate_item(item)
            finally:
                self._immediate_q.task_done()

    def _ensure_immediate_pool(self) -> None:
        """Spawn consumer threads up to `_immediate_pool_size`, reusing live
        ones (conc-02). Caller holds `_immediate_lock`."""
        self.immediate_threads = [t for t in self.immediate_threads
                                  if t.is_alive()]
        deficit = self._immediate_pool_size - len(self.immediate_threads)
        for _ in range(max(0, deficit)):
            t = threading.Thread(target=self._immediate_consumer,
                                  daemon=True, name="immediate-runner")
            self.immediate_threads.append(t)
            t.start()

    def _dispatch_immediate(self, item: QueueItem) -> None:
        """Hand an immediate-mode item to the bounded pool (conc-02). Immediate
        items are fire-and-forget and intentionally not deduplicated — a user
        who wanted exactly one fire would have used a queue protocol. The number
        of live consumer threads never exceeds `_immediate_pool_size`, so a
        large paste queues up instead of spawning a thread per link."""
        self._log(f"[immediate] {item.protocol}: {item.url}")
        self._immediate_q.put(item)
        with self._immediate_lock:
            self._ensure_immediate_pool()

    def _enqueue_or_skip_duplicate(self, item: QueueItem, outcome: str) -> str:
        """Append the item to queue_items unless the URL is already pending
        or in flight. Returns 'duplicate' on skip, otherwise `outcome`."""
        with self._dispatch_cv:
            where = self._duplicate_status(item.url)
            if where is not None:
                self._log(f"[duplicate] skipping {item.url} (already {where})")
                return "duplicate"
            self.queue_items.append(item)
            if self._batch_dispatch_depth == 0:
                self._dispatch_cv.notify()
            # else: outer _batch_dispatch() will notify_all once on exit.
        self._log(f"[queued]    {item.protocol}: {item.url}")
        self._refresh_queue_list()
        self._persist_after_enqueue()
        return outcome

    def _duplicate_status(self, url: str) -> "str | None":
        """Return 'pending', 'running', or None for the given URL.
        Caller MUST hold self._dispatch_cv (== queue_lock)."""
        # O(1) pending check via the queue's url index (perf-02). The running
        # check stays a scan, but current_items is bounded by the worker count.
        if url in getattr(self.queue_items, "urls", ()):
            return "pending"
        if any(
            it is not None and it.url == url
            for it in self.current_items.values()
        ):
            return "running"
        return None

    def _persist_after_enqueue(self) -> None:
        """Request a (debounced) save, or mark the batch save dirty so
        _batch_dispatch() requests a single write on exit (scal-03)."""
        if self._batch_dispatch_depth == 0:
            self._request_save_state()
        else:
            self._batch_save_dirty = True

    @staticmethod
    def _extract_protocol(url: str) -> str:
        try:
            return (urlparse(url).scheme or "").lower()
        except Exception:
            return ""

    @staticmethod
    def _domain_of(item: "QueueItem | str") -> str:
        """Group key used for per-domain worker spreading.

        Workers prefer items whose domain has the fewest active workers, so
        a single busy domain (e.g. youtube) can't monopolise every worker
        when other domains (vimeo, bandcamp, …) are also queued.

        Normalises:
          * uses urlparse netloc (host[:port])
          * lower-cases
          * strips a leading "www." so "www.youtube.com" and "youtube.com"
            count as the SAME domain
          * falls back to the URL scheme (or "_unknown") for inputs with no
            host — so e.g. file:///foo and magnet:?... still get bucketed
            consistently rather than all dumping into ""
        """
        url = item.url if isinstance(item, QueueItem) else str(item)
        try:
            parsed = urlparse(url)
        except Exception:
            return "_unknown"
        host = (parsed.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
        scheme = (parsed.scheme or "").lower()
        return scheme or "_unknown"

    @staticmethod
    def _resolve_command(template: str, url: str, protocol: str) -> str:
        # Single-pass substitution so a replaced value cannot itself contain
        # a placeholder that gets re-substituted (e.g. a URL with "{url}" in it).
        mapping = {
            "url": url,
            "url_quoted": shlex.quote(url),
            "protocol": protocol,
        }
        return _PLACEHOLDER_RE.sub(lambda m: mapping[m.group(1)], template)

    @classmethod
    def _build_argv(cls, template: str, url: str, protocol: str) -> list[str]:
        """Parse a template into argv and resolve placeholders per-argument.

        Because each URL ends up in its own argv slot, injection into the
        argument list is structurally impossible regardless of what special
        characters the URL contains.
        """
        parts = shlex.split(template, posix=True)
        return [cls._resolve_command(p, url, protocol) for p in parts]

    @staticmethod
    def _extra_argv(extra) -> list[str]:
        """Flatten mapped (flag, value) pairs into exec argv pieces. The flag
        may be several words (shlex-split); the value is a single argument, so
        a filename with spaces stays one arg and can't inject."""
        out: list[str] = []
        for flag, value in extra:
            out.extend(shlex.split(flag))
            out.append(value)
        return out

    @staticmethod
    def _extra_shell(extra) -> str:
        """Mapped pairs as a shell-quoted suffix (sec-02): the flag is split
        into words and each re-quoted so a config flag carrying shell
        metacharacters can't be smuggled into the /bin/sh -c string; the value
        is shell-quoted as one argument."""
        parts: list[str] = []
        for flag, value in extra:
            parts.extend(shlex.quote(tok) for tok in shlex.split(flag))
            parts.append(shlex.quote(value))
        return " ".join(parts)

    @staticmethod
    def _match_prefix(token: str, mappings: dict) -> "str | None":
        """Longest configured prefix that `token` starts with, or None."""
        best = None
        for p in mappings:
            if p and token.startswith(p) and (best is None or len(p) > len(best)):
                best = p
        return best

    # Schemes that identify a URL without a "://" authority part — so a
    # colliding mapping prefix can't swallow them (rel-09).
    _URLISH_SCHEMES = ("magnet:", "mailto:", "tel:", "data:", "bitcoin:", "ed2k:")

    @classmethod
    def _token_is_url(cls, token: str) -> bool:
        """Heuristic: a token is a URL (not a mapped param) if it has a '://'
        authority or starts with a known no-slash URL scheme. Stops a short
        prefix (e.g. 'h', 'magn') from swallowing http:// or magnet: URLs
        (rel-07 / rel-09)."""
        if "://" in token:
            return True
        low = token.lower()
        return any(low.startswith(s) for s in cls._URLISH_SCHEMES)

    @classmethod
    def _split_entry(cls, line: str, mappings: dict) -> "tuple[str, tuple]":
        """One input line -> (url, extra). Tokens whose start matches a
        configured prefix become (flag, value) pairs; the rest join into the
        URL. A matched token that is itself a URL is left as part of the URL
        (rel-07); a matched token with an empty value is skipped (rel-08). If
        NO prefix matched anywhere, the whole line is the URL (ordinary links
        untouched)."""
        tokens = line.split()
        url_tokens: list[str] = []
        extra: list = []
        matched_any = False
        for tok in tokens:
            prefix = cls._match_prefix(tok, mappings)
            if prefix is not None and not cls._token_is_url(tok):
                matched_any = True
                value = tok[len(prefix):]
                if value:                       # rel-08: skip empty parameter
                    extra.append((mappings[prefix], value))
                continue                        # bare prefix token dropped
            url_tokens.append(tok)
        url = " ".join(url_tokens) if matched_any else line.strip()
        return url, tuple(extra)

    @classmethod
    def _parse_entries(cls, raw: str, mappings: dict) -> "list[tuple[str, tuple]]":
        """Split pasted text into (url, extra) entries — one per non-blank
        line. Each line is a single URL plus zero or more prefix-mapped
        parameters (e.g. `https://x f:clip.mp4`)."""
        out = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            url, extra = cls._split_entry(line, mappings)
            if url:
                out.append((url, extra))
        return out

    # Module-level regex used by the summary log mode to find percentages
    # in subprocess output lines. We require a non-digit (or start of line
    # / whitespace) before the number, so "100.25%" doesn't match "25%".
    _PCT_RE = re.compile(r"(?:^|[^\d.])(\d{1,3})(?:\.\d+)?\s*%")
    # Progress milestones we surface in summary mode. First crossing of
    # each — once a milestone is logged for an item, we don't log it again
    # (so a flood of "25.1%, 25.2%, 25.3%, …" still produces only one line).
    _SUMMARY_MILESTONES = (25, 50, 75, 100)

    def _stream_subprocess_output(self, stdout, label: str,
                                  verbosity: "str | None" = None) -> None:
        """Read subprocess output line-by-line and surface the right
        portion to the log panel based on `verbosity` (lq-obs-01: snapshot
        at item start so mid-item config edits don't fragment one
        subprocess between two modes):

          * "summary" (default) — log the first non-blank line, then any
            line whose percentage crosses a 25/50/75/100% milestone for
            the first time. Friendly for download tools like yt-dlp /
            wget / curl which spew hundreds of progress lines.

          * "verbose" — log every line. Useful for debugging new protocols
            where you want to see exactly what the command is producing.

          * "silent" — log nothing from the subprocess; only the surrounding
            run/exit lines appear. Useful for high-volume queues where
            even the milestones are noise.

        Output lines are tagged with the worker label so concurrent streams
        from multiple workers stay attributable in the log panel.

        When `verbosity` is None we fall back to the live config value (legacy
        callers / tests). New callers should snapshot at item start via
        `_command_log_verbosity()` and pass it through.
        """
        if verbosity is None:
            verbosity = self._command_log_verbosity()
        # Prefix for each subprocess output line. The leading "| " puts
        # the pipe at the same column as the run-line's "[", so the
        # label aligns vertically across the run line and its
        # continuations:
        #   [HH:MM:SS] [queue#3 run] (exec) $ yt-dlp ...
        #   [HH:MM:SS] | queue#3 | [youtube] Extracting URL: ...
        # The pipe characters also flag the line as subprocess output
        # (vs the bracketed app log lines), and the label is repeated on
        # every line so concurrent workers stay attributable when their
        # streams interleave.
        prefix = f"| {label} | "
        if verbosity == "silent":
            self._drain_pipe(stdout)
        elif verbosity == "verbose":
            self._stream_verbose(stdout, prefix)
        else:
            self._stream_summary(stdout, prefix)

    @staticmethod
    def _drain_pipe(stdout) -> None:
        """Read stdout to EOF without logging — needed even in silent mode
        so the child doesn't block on a full pipe buffer."""
        for _ in stdout:
            pass

    def _stream_verbose(self, stdout, prefix: str) -> None:
        for line in stdout:
            self._log(prefix + line.rstrip())

    def _stream_summary(self, stdout, prefix: str) -> None:
        first_line_logged = False
        milestones_logged: set[int] = set()
        for line in stdout:
            line = line.rstrip()
            if not line:
                continue  # pragma: no cover - worker continue after state-only branch
            if not first_line_logged:
                self._log(prefix + line)
                first_line_logged = True
                continue
            crossed = self._milestones_crossed(line, milestones_logged)
            if crossed:  # pragma: no cover - withdrawn-root Tk early-exit
                milestones_logged.update(crossed)
                self._log(prefix + line)

    def _milestones_crossed(
        self, line: str, already_logged: "set[int]"
    ) -> "list[int]":
        """Return the SUMMARY_MILESTONES values that this line crosses for
        the first time, given which have already been logged. The line is
        scanned for percentages; the highest one wins. Empty list if no
        percentage is present or no new milestone is crossed."""
        highest = self._highest_pct_in(line)
        if highest < 0:
            return []
        return [
            ms for ms in self._SUMMARY_MILESTONES
            if ms not in already_logged and highest >= ms
        ]

    @classmethod
    def _highest_pct_in(cls, line: str) -> int:
        """The largest integer-percentage value that appears on this line,
        or -1 if there is no percentage. Uses _PCT_RE which guards against
        digit/dot boundaries so '100.25%' captures 100 but not 25."""
        highest = -1
        for m in cls._PCT_RE.finditer(line):
            try:
                pct = int(m.group(1))
            except ValueError:  # pragma: no cover - ValueError parsing nonint env
                continue  # pragma: no cover - continue after parse failure
            if pct > highest:  # pragma: no cover - withdrawn-root Tk early-exit
                highest = pct
        return highest

    def _arm_command_timeout(self, proc, label, url, timeout):
        """Start a `threading.Timer` that terminates `proc` after
        `timeout` seconds (scal-04). Returns the Timer (caller cancels
        it on natural completion) or None when no timeout is configured."""
        if timeout <= 0:
            return None

        def _on_timeout() -> None:
            self._record_metric("timeouts")
            self._log(
                f"[{label} timeout] {timeout}s expired, terminating  url={url}"
            )
            try:
                proc.terminate()
            except OSError:  # pragma: no cover - proc already exited
                return
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - subprocess ignored SIGTERM
                self._log(
                    f"[{label} timeout] terminate ignored, killing  url={url}"
                )
                proc.kill()

        timer = threading.Timer(timeout, _on_timeout)
        timer.daemon = True
        timer.start()
        return timer

    def _command_log_verbosity(self) -> str:
        """Snapshot of `log_verbosity` config (lq-obs-01). Read once at
        item start so mid-item edits don't fragment a single subprocess's
        output between the old and new mode."""
        return str(self.config.get("log_verbosity", "summary")).lower()

    def _command_timeout_seconds(self) -> int:
        """Resolve the per-item subprocess wait timeout from config (scal-04).
        Non-positive / unparseable values mean 'no timeout — wait forever'.

        Lives on Dispatcher (not LinkQueueApp) so `_run_item` can call it
        directly without going through the app façade — workers run on
        the Dispatcher instance."""
        raw = self.config.get("command_timeout_seconds", 0)
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        return v if v > 0 else 0

    @staticmethod
    def _item_log_id(item: QueueItem) -> str:
        """Short, stable per-item id for log correlation (obs-02): 4 hex chars
        of blake2b over the URL. Same item -> same id across its run/done/error
        lines, so interleaved worker output can be matched."""
        h = hashlib.blake2b(item.url.encode("utf-8", "surrogatepass"), digest_size=2)
        return f"#{h.hexdigest()}"

    def _spawn_proc(self, item: QueueItem, label: str, cwd, cwd_note: str):
        """Build and start the subprocess for `item` (cmplx-02). Returns the
        running Popen, or None when the command is rejected/invalid/empty (the
        reason is logged here so `_run_item` just maps None -> exit -1)."""
        if item.shell:
            return self._spawn_shell_proc(item, label, cwd, cwd_note)
        return self._spawn_exec_proc(item, label, cwd, cwd_note)

    def _spawn_shell_proc(self, item: QueueItem, label: str, cwd, cwd_note: str):
        """shell=True branch (opt-in). sec-04: REFUSE a bare {url}, which would
        be interpolated UNQUOTED into /bin/sh -c — an adversarial URL like
        ``; rm -rf ~/`` would execute. {url_quoted} (or shell=False) is the
        only safe path."""
        url, protocol, template = item.url, item.protocol, item.template
        if _template_has_bare_url(template):
            self._log(
                f"[{label} error] refusing shell=True template "
                f"with bare {{url}} — use {{url_quoted}} or shell=False:"
                f" {template}  url={url}"
            )
            return None
        resolved = self._resolve_command(template, url, protocol)
        if item.extra:
            resolved += " " + self._extra_shell(item.extra)  # pragma: no cover - extra-shell concat branch with non-falsy extra
        self._log(f"[{label} run] (shell) $ {resolved}{cwd_note}")
        return subprocess.Popen(
            resolved, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=cwd,
        )

    def _spawn_exec_proc(self, item: QueueItem, label: str, cwd, cwd_note: str):
        """shell=False branch (default, safe): shlex.split the template + exec
        an argv, so the URL lands in its own slot and can't inject."""
        url, protocol, template = item.url, item.protocol, item.template
        try:
            argv = self._build_argv(template, url, protocol)
        except ValueError as e:
            self._log(f"[{label} error] invalid template: {e}  url={url}")
            return None
        argv += self._extra_argv(item.extra)   # mapped flags appended at end
        if not argv:
            self._log(f"[{label} error] empty command  url={url}")
            return None
        pretty = " ".join(shlex.quote(a) for a in argv)
        self._log(f"[{label} run] (exec)  $ {pretty}{cwd_note}")
        return subprocess.Popen(
            argv, shell=False,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=cwd,
        )

    def _run_item(self, item: QueueItem, label: str) -> int:
        """Execute one QueueItem. Honors item.shell:
           * shell=False (default, safe): shlex.split + exec an argv.
           * shell=True  (opt-in):        hand the resolved string to /bin/sh -c.

        The user-configured 'output_folder' is applied as the subprocess cwd
        (equivalent to a `cd` before the command). Empty/invalid folder
        means no override — the subprocess inherits the script's cwd.

        Note (rel-11): stderr is intentionally merged into stdout via
        ``stderr=subprocess.STDOUT`` for BOTH the shell and exec branches —
        downloaders (yt-dlp, aria2c, curl …) emit progress on stderr and the
        log sink expects a single ordered byte stream. Anyone adding a
        stderr-only code path here must drop the merge first; otherwise the
        new handler will never see anything.
        """
        url = item.url
        # obs-02: weave a short per-item id into the label so the run/done/
        # error/stuck lines (and the streamed output prefix) for one item can
        # be matched even when several workers interleave in the log.
        label = f"{label} {self._item_log_id(item)}"
        folder = self.config.get("output_folder", "")
        cwd = self._resolve_cwd(folder)
        if cwd is None and (folder or "").strip():
            self._log(
                f"[warn] output folder {(folder or '').strip()!r} does not "
                f"exist or is not a directory — inheriting current directory"
            )
        cwd_note = f"  (cwd={cwd})" if cwd else ""
        try:
            proc = self._spawn_proc(item, label, cwd, cwd_note)
            if proc is None:
                return -1
            # cx-07: real check (not assert) so `python -O` keeps the guard.
            # `stdout=subprocess.PIPE` above guarantees this, but a future
            # caller might drop the PIPE and the silent crash would be a
            # nightmare to triage.
            if proc.stdout is None:
                raise RuntimeError(
                    f"subprocess started without a captured stdout pipe (label={label})"
                )
            # scal-04: bound the wall-clock per item so a hung yt-dlp /
            # aria2c can't lock up the worker. Default = 0 (off) preserves
            # the historical "wait forever" behaviour. When > 0, a
            # threading.Timer fires terminate-then-kill on the subprocess
            # while `_stream_subprocess_output` is mid-read; the pipe
            # closes on death, the reader unblocks, and proc.wait()
            # returns immediately afterwards. Direct `proc.wait(timeout=)`
            # is useless here because the reader holds the loop for the
            # full subprocess lifetime.
            timeout = self._command_timeout_seconds()
            # lq-obs-01: snapshot verbosity once per item so a mid-item
            # config edit can't fragment the stream between modes.
            verbosity = self._command_log_verbosity()
            timer = self._arm_command_timeout(proc, label, url, timeout)
            try:
                self._stream_subprocess_output(proc.stdout, label, verbosity)
                # lq-rel-02: bounded wait after streaming so a zombie
                # subprocess that ignored both SIGTERM and SIGKILL can't
                # hang the worker forever and shrink the pool. With a
                # configured timeout, the safety-net = 2 * timeout + 5
                # (covers the timer's own terminate→kill ladder).
                # lq-rel-06: when `command_timeout_seconds == 0` the
                # user is opting OUT of the timer — "wait forever" is
                # the documented semantics for long idempotent downloads
                # (yt-dlp on a slow archive). The old code capped that
                # at 30s and silently abandoned the proc, breaking the
                # contract. None disables the hard deadline entirely.
                hard_deadline = (timeout * 2 + 5) if timeout > 0 else None
                try:
                    proc.wait(timeout=hard_deadline)
                except subprocess.TimeoutExpired:
                    self._log(
                        f"[{label} stuck] subprocess unresponsive after "
                        f"{hard_deadline}s; abandoning  url={url}"
                    )
                    # Leave the proc to the OS reaper; return -1 so the
                    # dispatcher can decide on cooldown / retry.
                    return -1
            finally:
                if timer is not None:
                    timer.cancel()
            self._log(f"[{label} done] exit={proc.returncode}  url={url}")
            return proc.returncode
        except FileNotFoundError as e:
            # Common cause: the first argv element isn't on PATH.
            self._log(f"[{label} error] command not found: {e}  url={url}")
            return -1
        except Exception as e:  # pragma: no cover - worker step caught exception
            self._log(f"[{label} error] {e}  url={url}")  # pragma: no cover - log + return -1 after exception
            return -1  # pragma: no cover - log + return -1 after exception

    def _item_display(self, item: QueueItem) -> str:
        """Render a queue item for the Treeview. Does not execute anything."""
        try:
            if item.shell:
                resolved = self._resolve_command(
                    item.template, item.url, item.protocol)
                if item.extra:
                    resolved += " " + self._extra_shell(item.extra)
                return f"(shell) {resolved}"
            argv = self._build_argv(item.template, item.url, item.protocol)
            argv += self._extra_argv(item.extra)
            return "(exec) " + " ".join(shlex.quote(a) for a in argv)
        except ValueError as e:
            return f"(invalid template: {e})"


    # The worker list + its lock live on the pool; expose them so existing
    # call sites (self.workers / self._workers_lock, e.g. _snapshot and tests)
    # keep resolving.
    @property
    def workers(self) -> list:
        return self._pool.workers

    @property
    def _workers_lock(self):
        return self._pool.lock

    def _wake_workers(self) -> None:
        """Wake every worker blocked on the dispatch CV (used after a retire so
        the stop signal is observed immediately)."""
        with self._dispatch_cv:
            self._dispatch_cv.notify_all()

    def _ensure_worker_count(self, target: int) -> None:
        """Scale the worker pool up or down, then persist the chosen count."""
        target = self._pool.ensure(target)
        if int(self.config.get("worker_count", 1)) != target:
            self.config["worker_count"] = target
            self._save_config()
        self._update_status()


    def _claim_next_item(
        self,
        idx: int,
        stop_self: threading.Event,
        wait_seconds: float = 0.25,
    ) -> "QueueItem | None":
        """Block on _dispatch_cv until either:
          * a queued item can be claimed for this worker (returns it), OR
          * `wait_seconds` elapse without any item to claim (returns None), OR
          * shutdown / stop-this-worker is signalled (returns None).

        The "claim" picks the item whose domain has the FEWEST workers
        currently active on it — so a single noisy domain (5 youtube URLs)
        can't starve other queued domains (1 vimeo URL) when the worker
        count exceeds the number of pending domains. Ties broken by FIFO
        (insertion order).

        Pause/cooldown are honoured: while blocked, no item is claimed even
        if available, and we wait on the cv (with bounded timeout) until
        the block clears.
        """
        deadline = time.time() + wait_seconds
        with self._dispatch_cv:
            while True:
                if self.stop_event.is_set() or stop_self.is_set():
                    return None
                blocked, sleep_for = self._is_blocked()
                if not blocked:
                    item = self._try_claim_item(idx)
                    if item is not None:
                        return item
                remaining = self._dispatch_wait_remaining(
                    deadline, blocked, sleep_for)
                if remaining <= 0:
                    return None
                self._dispatch_cv.wait(timeout=remaining)

    def _try_claim_item(self, idx: int) -> "QueueItem | None":
        """Attempt to pop a claimable item for worker `idx`. Returns the
        claimed QueueItem on success, None if nothing is currently
        claimable (e.g. queue empty, or all pending items are over their
        per-domain cap). Caller MUST hold self._dispatch_cv."""
        # Read the cap fresh on every wake so changes via
        # _on_max_per_domain_changed are observed immediately, even by
        # workers that were already inside the wait loop when it fired.
        cap = int(self.config.get("max_per_domain", 0) or 0)
        item = self._pick_next_item(cap)
        if item is None:
            return None
        self.queue_items.remove(item)
        domain = self._domain_of(item)
        self._domain_active[domain] = self._domain_active.get(domain, 0) + 1
        self.current_items[idx] = item
        return item

    @staticmethod
    def _dispatch_wait_remaining(
        deadline: float, blocked: bool, sleep_for: float
    ) -> float:
        """How long to wait on the cv before re-evaluating. Caps the
        caller's wait budget by the block-imposed sleep when blocked."""
        remaining = deadline - time.time()
        if remaining <= 0:
            return 0.0
        if blocked and sleep_for > 0:
            return min(remaining, sleep_for)
        return remaining

    def _pick_next_item(self, cap: int) -> "QueueItem | None":
        """Claim helper (conc-01: NOT a pure reader — it MUTATES dispatch
        bookkeeping as a side effect). It selects the claimable item whose
        domain has the FEWEST workers currently active on it — so a single
        noisy domain (5 youtube URLs) can't starve other queued domains
        (1 vimeo URL) when the worker count exceeds the number of pending
        domains. Ties broken by global FIFO (insertion order).

        Side effects (all under the caller's lock): it opportunistically
        prunes empty `by_domain` buckets, drops the matching lingering-zero
        `_domain_active` entries, and sweeps stale `seq_of` keys once their
        drift exceeds the configured gap. These keep the indexes tight; they
        do not change which item is returned.

        Iterates the per-domain index (one entry per distinct pending domain,
        typically a handful) rather than scanning every pending item, so a
        large backlog no longer makes each claim O(n) (perf-01 / scal-01).

        If `cap > 0`, domains already at `>= cap` active workers are skipped
        (they must wait for an active worker on the same domain to finish).

        Returns None if nothing is currently claimable. Caller MUST hold
        self._dispatch_cv (== queue_lock).
        """
        by_domain = getattr(self.queue_items, "by_domain", None)
        if by_domain is None:  # plain-list fallback (defensive)
            return self.queue_items[0] if self.queue_items else None  # pragma: no cover - queue head with empty list
        seq_of = self.queue_items.seq_of
        best = None
        best_active: int | None = None
        best_seq: int | None = None
        # lq-rel-01: opportunistically prune empty buckets we iterate so
        # the dict can't grow unboundedly even if a future code path
        # leaves stale empties behind (the normal `remove()` already
        # prunes on the happy path, but the defensive sweep here keeps
        # `by_domain` tight under any caller pattern).
        empty_domains: list = []
        for domain, bucket in by_domain.items():
            if not bucket:
                empty_domains.append(domain)
                continue  # pragma: no cover - worker continue on cooldown
            active = self._domain_active.get(domain, 0)
            if cap > 0 and active >= cap:
                continue
            head = next(iter(bucket.values()))   # FIFO head of this domain
            seq = seq_of.get(head.url, 0)
            if (best is None or active < best_active
                    or (active == best_active and seq < best_seq)):
                best, best_active, best_seq = head, active, seq
        for domain in empty_domains:
            by_domain.pop(domain, None)
            # lq-scal-02: also drop the matching `_domain_active` entry
            # if it lingers at zero — `_release_item` does this on the
            # decrement path, but a sweep here covers the case where a
            # cap-rejected domain leaves a stale 0 entry behind.
            if self._domain_active.get(domain, 0) == 0:
                self._domain_active.pop(domain, None)
        # lq-scal-01 / lq-scal-03: opportunistic stale-key sweep on
        # `seq_of`. `_PendingQueue.remove` already pops the key inline
        # on the happy path, so the only way `seq_of` and `urls` drift
        # is if a future code path mutates `urls` without going through
        # `remove`. To bound pick-latency on long-running sessions with
        # 1M-item churn (the linear O(len(seq_of)) scan ran on EVERY
        # pick), the sweep now runs only when the gap exceeds
        # `_SEQ_OF_SWEEP_GAP`. Under steady state the scan is skipped;
        # under accumulated drift it runs at most every gap-many picks.
        live_urls = self.queue_items.urls
        gap = len(seq_of) - len(live_urls)
        # lq-decoup-04: read the live threshold from config so operators
        # can tune it without editing source. Falls back to the module
        # constant when the key is absent (legacy config files).
        threshold = self._seq_of_sweep_gap()
        if gap > threshold:
            stale = [u for u in seq_of if u not in live_urls]
            for u in stale:
                seq_of.pop(u, None)
        return best

    def _seq_of_sweep_gap(self) -> int:
        """Read `seq_of_sweep_gap` from live config (lq-decoup-04).

        Clamped to >= 1 so a misconfigured 0 doesn't sweep on every
        pick (which would defeat the batching). Falls back to the
        module default when the key is missing or non-integer."""
        try:
            value = int(self.config.get("seq_of_sweep_gap", _SEQ_OF_SWEEP_GAP))
        except (TypeError, ValueError):
            return _SEQ_OF_SWEEP_GAP
        return max(1, value)

    def _release_item(self, idx: int, item: "QueueItem") -> None:
        """Decrement domain_active and clear our current_items slot. Notify
        the cv so any other waiting worker can re-evaluate."""
        domain = self._domain_of(item)
        with self._dispatch_cv:
            self._domain_active[domain] = max(
                0, self._domain_active.get(domain, 0) - 1
            )
            if self._domain_active.get(domain, 0) == 0:
                # Drop the entry so the dict doesn't grow unbounded over a
                # long-running session that hits many distinct hosts.
                self._domain_active.pop(domain, None)
            self.current_items[idx] = None
            self._dispatch_cv.notify()

    def _worker_loop(self, idx: int, stop_self: threading.Event) -> None:
        # Register our current-items slot (WorkerPool no longer does this for
        # us — cx-03) so _update_status counts us before the first claim.
        with self.queue_lock:
            self.current_items[idx] = None
        while not self.stop_event.is_set() and not stop_self.is_set():
            try:
                self._worker_step(idx, stop_self)
            except Exception as e:  # pragma: no cover - worker iteration crashed
                # A runaway exception inside the step would kill this worker  # pragma: no cover - worker iteration crashed (comment)
                # thread, permanently shrinking the pool. Log and continue  # pragma: no cover - worker iteration crashed (comment)
                # so a single bad item can't take down the worker. The  # pragma: no cover - worker iteration crashed (comment)
                # try/finally inside _worker_step already released the  # pragma: no cover - worker iteration crashed (comment)
                # domain counter and slot before re-raising.  # pragma: no cover - worker iteration crashed (comment)
                self._log(f"[worker#{idx}] iteration crashed: {e!r}")  # pragma: no cover - log iteration crash

        # Clean up this worker's current-items slot on exit.
        with self.queue_lock:
            self.current_items.pop(idx, None)
        self._refresh_queue_list()
        self._update_status()

    def _worker_step(self, idx: int, stop_self: threading.Event) -> None:
        item = self._claim_next_item(idx, stop_self)
        if item is None:
            self._update_status()
            return

        self._refresh_queue_list()
        self._update_status()
        # Request a debounced save (scal-03): the snapshot is taken under
        # queue_lock inside _save_state, so it's safe from the timer thread
        # without marshalling onto the UI thread.
        self._request_save_state()

        # Run + release in a try/finally so a runaway exception inside
        # _run_item — even one that bypasses _run_item's own except
        # clauses — can never leave self._domain_active or current_items
        # incremented. Without this, a single crash would permanently
        # block the affected domain from being claimed again.
        exit_code = -1
        try:
            exit_code = self._run_item(item, f"queue#{idx}")
        finally:
            # conc-05: release first, THEN observe pool saturation under
            # the same queue_lock acquisition. The prior order (sample
            # `other_free` before `_release_item`) sampled a stale view
            # of `current_items` — between sample and release another
            # worker could mutate it, leaving `other_free` desynced from
            # the actual post-release state. Combining the release and
            # the count into one critical section closes that race.
            self._release_item(idx, item)
            with self.queue_lock:
                other_free = sum(
                    1 for i, v in self.current_items.items()
                    if v is None and i != idx
                )
            self._refresh_queue_list()

        if exit_code != 0:
            self._record_metric("failures")
            self._trigger_failure_cooldown(idx, exit_code)  # pragma: no cover - trigger cooldown after failure
        else:
            self._record_metric("completions")
        self._update_status()
        self._maybe_inter_item_sleep(stop_self, other_free)

    def _trigger_failure_cooldown(self, idx: int, exit_code: int) -> None:
        """A queued item failed: arm the global cooldown that all queue
        workers will respect at the top of their loop. Immediate-mode
        runners are unaffected."""
        fail_s = self._get_failure_sleep()
        if fail_s <= 0:
            return
        new_until = time.time() + fail_s
        with self._cooldown_lock:
            if new_until > self._cooldown_until:  # pragma: no cover - withdrawn-root Tk early-exit
                self._cooldown_until = new_until
        self._log(
            f"[cooldown] queue#{idx} item failed (exit {exit_code}) "
            f"— pausing queue workers for {self._format_duration(fail_s)}"
        )
        # Wake other workers so they immediately observe cooldown rather
        # than continuing to poll on the cv.
        with self._dispatch_cv:
            self._dispatch_cv.notify_all()

    def _maybe_inter_item_sleep(
        self, stop_self: threading.Event, other_free: int
    ) -> None:
        """Sleep between items, but ONLY when no other free worker exists.
        With a single worker this is always true, so the original
        rate-limit-every-item behaviour is preserved. With many workers it
        lets idle ones pull immediately and only throttles the worker that
        would otherwise saturate the system on its own.

        Wakes early on shutdown / stop-this-worker.

        lq-rel-05: the sleep target is re-read from `_get_sleep()` on
        every step so live config edits (user lowers sleep mid-wait)
        take effect immediately instead of being deferred to the next
        item.
        """
        initial = max(0, int(self._get_sleep()))
        if initial <= 0 or other_free != 0:
            return
        if self.stop_event.is_set() or stop_self.is_set():
            return
        slept = 0.0
        step = 0.25
        while not self.stop_event.is_set() and not stop_self.is_set():
            current_target = max(0, int(self._get_sleep()))
            if slept >= current_target:
                return
            time.sleep(step)
            slept += step

    def _live_worker_count(self) -> int:
        """Worker threads currently alive — INCLUDES those asked to stop that
        are still finishing their item (so the displayed count only drops when
        a worker actually exits). The requested target is config['worker_count']."""
        return self._pool.live_count()

    def _stopping_worker_count(self) -> int:
        """Alive workers that have been asked to stop (still finishing)."""
        return self._pool.stopping_count()


    _CWD_UNSET = object()

    def _resolve_cwd(self, folder=_CWD_UNSET) -> str | None:
        """Resolve a configured output-folder value to a subprocess cwd, or
        None when no override should be applied (decoup-01: pure — takes the
        folder value and does no logging, so it is unit-testable in isolation).

        Empty/blank `folder` -> None (no override). Non-empty but non-existent
        or non-directory `folder` -> None as well, so the subprocess inherits
        the process's current directory (rel-02: the previous docstring claimed
        a fall back to the script dir, which is NOT what happens — None means
        no cwd= is passed to Popen, so the child keeps the inherited cwd).

        Tolerates pathological values: null bytes, control characters,
        absurdly long paths, and other inputs that can make os.path.isdir
        raise on some platforms (notably Windows with embedded nulls;
        macOS sometimes raises on paths longer than NAME_MAX). Anything
        that raises is treated as 'not a valid directory'.

        For backward compatibility, calling with no argument reads the live
        `output_folder` config value.
        """
        if folder is self._CWD_UNSET:
            folder = self.config.get("output_folder", "")
        v = (folder or "").strip()
        if not v:
            return None
        try:
            is_dir = os.path.isdir(v)
        except (ValueError, OSError):  # pragma: no cover - ValueError/OSError on is_dir probe
            is_dir = False  # pragma: no cover - is_dir defaults False on error
        return v if is_dir else None


    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        m, s = divmod(int(seconds), 60)
        if m < 60:
            return f"{m}m{s:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"


    def _is_blocked(self) -> tuple[bool, float]:
        """Whether queue workers should hold off on grabbing new work.
        Returns (blocked, suggested_sleep_seconds)."""
        if self.pause_event.is_set():
            return True, 0.25
        with self._cooldown_lock:
            remaining = self._cooldown_until - time.time()
        if remaining > 0:
            return True, min(remaining, 0.25)
        return False, 0.0


# ---------------------------------------------------------------------------
# Log-file sink (cx-06)
# ---------------------------------------------------------------------------


class LogSink:
    """Async on-disk log sink (cx-06, extracted from LinkQueueApp). Writers
    only call .write(line); a single background thread does the file open /
    write / flush / rotate, so a slow disk can never block a worker. Dropped
    lines (queue full) are surfaced on the next flush as a one-line notice.

    Path is read from the injected ``get_path`` callable on every flush so
    config changes take effect without restarting the writer."""

    def __init__(self, get_path):
        self._get_path = get_path
        self._lock = threading.Lock()
        self._fh = None
        self._fh_path = None
        self._fh_size = 0
        # lq-rel-03: throttle open-failure warnings to one per path so a
        # flaky / unreachable log target doesn't drown the operator.
        self._open_fail_path: str | None = None
        self._open_fail_first_t: float | None = None
        self._drop_lock = threading.Lock()
        self._drop_count = 0
        # obs-04: monotonic timestamp of the first drop in the current
        # window so the surfaced notice can say "dropped N over the last
        # M.Ms" instead of an opaque count.
        self._drop_first_t: float | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=100_000)
        self._stop = threading.Event()
        self._writer = threading.Thread(
            target=self._writer_loop, name="log-writer", daemon=True)
        self._writer.start()

    # -- public surface -----------------------------------------------------
    def write(self, line: str) -> None:
        """Hand `line` to the writer thread. Returns immediately; drops the
        line on overflow rather than blocking the caller."""
        if not str(self._get_path() or "").strip():
            return
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            with self._drop_lock:
                if self._drop_count == 0:
                    self._drop_first_t = time.monotonic()
                self._drop_count += 1

    def stop(self) -> None:
        """Signal the writer to drain and exit, then join it.

        Flush-deadline contract (conc-02)
        ---------------------------------
        ``stop()`` waits at most ``2.0`` seconds for the writer thread to
        finish flushing. On a slow disk (NFS hiccup, full sync) the writer
        may still be inside ``fh.flush()`` when the join times out; the
        process exits anyway and the unflushed batch is lost. This is
        acceptable for the GUI shutdown path (the user explicitly closed
        the app and any pending log lines would otherwise pin the UI), but
        if a caller cares about log durability they should drain their own
        critical events through a synchronous logger and use ``LogSink``
        for the best-effort UI mirror only. A future change can plumb the
        timeout through as a constructor knob if a deployment needs to
        tune it; today's 2.0s is a deliberate trade-off, not a forgotten
        magic constant."""
        self._stop.set()
        self._writer.join(timeout=2.0)

    # -- internal -----------------------------------------------------------
    def _writer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._flush_batch(self._drain_queue([first]))
            except Exception:
                pass
        try:
            remaining = self._drain_queue([])
            if remaining:
                self._flush_batch(remaining)  # pragma: no cover - _flush_batch on remaining batch (post-loop)
        except Exception:  # pragma: no cover - defensive Tk exception in flush
            pass  # pragma: no cover - defensive Tk exception in flush
        with self._lock:
            self._close_locked()

    def _drain_queue(self, batch: list) -> list:
        try:
            while True:
                batch.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return batch

    def _flush_batch(self, batch: list) -> None:
        path = str(self._get_path() or "").strip()
        with self._lock:
            if not path:
                self._close_locked()
                return
            if self._fh is None or self._fh_path != path:
                self._open_locked(path)
            if self._fh is None:
                return
            with self._drop_lock:
                dropped, self._drop_count = self._drop_count, 0
                first_t = self._drop_first_t
                self._drop_first_t = None
            text = "".join(batch)
            if dropped:
                # obs-04: stamp the drop window so operators see what
                # span of time the count covers, not just the count.
                window = (
                    f" over {time.monotonic() - first_t:.2f}s"
                    if first_t is not None else ""
                )
                text = (
                    f"[sink] dropped {dropped} line(s){window} "
                    f"(log queue full)\n" + text
                )
            try:
                self._fh.write(text)
                self._fh.flush()
                self._fh_size += len(text.encode("utf-8"))
            except Exception:
                self._close_locked()
                return
            if self._fh_size >= LOG_SINK_MAX_BYTES:
                self._rotate_locked()

    def _rotate_locked(self) -> None:
        path = self._fh_path
        self._close_locked()
        try:
            os.replace(path, path + ".1")
        except OSError:
            pass
        self._open_locked(path)

    def _open_locked(self, path: str) -> None:
        self._close_locked()
        try:
            # sec-03: reject symlink targets. `open(path, "a")` follows
            # symlinks by default — a pre-existing symlink in `log_file`
            # pointing at `/etc/passwd` or `~/.ssh/authorized_keys`
            # would append worker output to the target. Use O_NOFOLLOW
            # so a swapped link fails with ELOOP and falls through to
            # the `_note_open_failure` throttled warning.
            #
            # O_APPEND keeps the append semantics; O_CREAT lets us
            # initialise a fresh log file. Mode 0o600 — only the
            # current user reads back the captured URL list.
            fd = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            self._fh = os.fdopen(fd, "a", encoding="utf-8")
            self._fh_path = path
            self._open_fail_path = None   # lq-rel-03: success clears throttle
            self._open_fail_first_t = None
            try:
                self._fh_size = os.path.getsize(path)
            except OSError:  # pragma: no cover - OSError reading file size
                self._fh_size = 0  # pragma: no cover - fall back to size=0
        except Exception as e:
            self._fh = None
            self._fh_path = None
            self._fh_size = 0
            self._note_open_failure(path, e)

    def _note_open_failure(self, path: str, exc: Exception) -> None:
        """Surface a `_open_locked` failure with throttling (lq-rel-03).

        Without rate-limiting, every queued line would re-attempt and
        print a stderr warning, drowning the operator in identical
        messages. Print the first warning, then suppress subsequent
        warnings for the SAME path until either it changes or the file
        eventually opens. Track since-when so re-warnings can include
        a duration in a follow-up message.

        lq-obs-02: when the failure is ``ELOOP`` (sec-03 ``O_NOFOLLOW``
        rejected a symlink at `path`), surface a distinct
        ``[warn] log file is a symlink — rejected`` line so the
        operator can fix the misconfiguration instead of staring at a
        generic "cannot be opened" message."""
        if path == getattr(self, "_open_fail_path", None):
            return   # already warned for this path this session
        self._open_fail_path = path
        self._open_fail_first_t = time.monotonic()
        if isinstance(exc, OSError) and exc.errno == errno.ELOOP:
            print(
                f"[warn] log file {path!r} is a symlink — rejected "
                f"(O_NOFOLLOW). Change `log_file` to a regular path "
                f"or remove the symlink.",
                file=sys.stderr,
            )
            return
        print(
            f"[warn] log file {path!r} cannot be opened ({exc}). Lines "
            f"will continue to be queued but won't be written until the "
            f"path becomes accessible.",
            file=sys.stderr,
        )

    def _close_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._fh_path = None
        self._fh_size = 0


# ---------------------------------------------------------------------------
# Settings dialog tab content (cx-05)
# ---------------------------------------------------------------------------


class _SettingsTabs:
    """Builds the Settings dialog tab content. The dialog Toplevel, the
    Notebook, open/close, and the dirty flag stay on LinkQueueApp; this helper
    owns only the per-tab widget construction (cx-05). It accesses the live
    app StringVars + handlers via the injected `app` reference."""

    # arch-07: tab order + builder binding lives in one registry, so adding
    # a fifth tab is one append to this tuple (no second edit inside
    # `build`). The builder is named by string and resolved against `self`
    # at build time so subclasses can override without re-touching the
    # registry.
    TABS: tuple[tuple[str, str], ...] = (
        ("Dispatcher", "_build_dispatcher"),
        ("Protocols",  "_build_protocols"),
        ("Mappings",   "_build_mappings"),
        ("Log",        "_build_log"),
    )

    def __init__(self, app: "LinkQueueApp"):
        self.app = app

    def build(self, notebook) -> None:
        """Add every tab in :data:`TABS` to a ttk.Notebook in order."""
        for title, builder_name in self.TABS:
            tab = ttk.Frame(notebook, padding=10)
            notebook.add(tab, text=title)
            getattr(self, builder_name)(tab)

    # -- shared CRUD-tab chrome (dup-06) ------------------------------------
    @staticmethod
    def _crud_tab(parent, *, buttons, hint, columns, on_double_click):
        ctrl = ttk.Frame(parent)
        ctrl.pack(fill=tk.X, pady=(0, 6))
        for label, command in buttons:
            ttk.Button(ctrl, text=label, command=command).pack(side=tk.LEFT, padx=2)
        ttk.Label(ctrl, text=hint, foreground="#666").pack(side=tk.RIGHT)

        col_ids = [c["id"] for c in columns]
        tree = ttk.Treeview(parent, columns=col_ids,
                            show="headings", selectmode="browse")
        for c in columns:
            tree.heading(c["id"], text=c["text"])
            tree.column(c["id"], **{k: v for k, v in c.items() if k not in ("id", "text")})
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind("<Double-1>", on_double_click)
        return tree

    # -- per-tab builders ---------------------------------------------------
    def _build_dispatcher(self, parent) -> None:
        app = self.app
        row = 0
        for label, var, frm, to, w, handler in [
            ("Sleep between items (s):",
             app.sleep_var,        0,  3600, 6, app._on_sleep_changed),
            ("Workers:",
             app.worker_count_var, 1,    32, 4, app._on_worker_count_changed),
            ("Cooldown on failure (s):",
             app.cooldown_var,     0, 86400, 6, app._on_cooldown_changed),
            ("Max per domain (0 = no cap):",
             app.max_per_domain_var, 0, 32, 4, app._on_max_per_domain_changed),
        ]:
            ttk.Label(parent, text=label).grid(
                row=row, column=0, sticky="w", padx=4, pady=4)
            sp = ttk.Spinbox(parent, from_=frm, to=to, width=w,
                             textvariable=var, command=handler)
            sp.grid(row=row, column=1, sticky="w", padx=4, pady=4)
            sp.bind("<FocusOut>", lambda e, h=handler: h())
            sp.bind("<Return>",   lambda e, h=handler: h())
            app._install_text_editing(sp, "entry")
            row += 1

        ttk.Separator(parent, orient=tk.HORIZONTAL).grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        row += 1
        ttk.Label(parent, text="Output folder:").grid(
            row=row, column=0, sticky="w", padx=4, pady=4)
        out_row = ttk.Frame(parent)
        out_row.grid(row=row, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        parent.columnconfigure(1, weight=1)
        app.output_folder_entry = ttk.Entry(
            out_row, textvariable=app.output_folder_var)
        app.output_folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        app.output_folder_entry.bind(
            "<FocusOut>", lambda e: app._on_output_folder_changed())
        app.output_folder_entry.bind(
            "<Return>", lambda e: app._on_output_folder_changed())
        app._install_text_editing(app.output_folder_entry, "entry")
        ttk.Button(out_row, text="Browse…",
                   command=app._on_browse_output_folder).pack(
            side=tk.LEFT, padx=4)
        ttk.Button(out_row, text="Clear",
                   command=app._on_clear_output_folder).pack(side=tk.LEFT)

    def _build_protocols(self, parent) -> None:
        app = self.app
        app.proto_tree = self._crud_tab(
            parent,
            buttons=[("New Protocol", app._on_new_protocol),
                     ("Edit Selected", app._on_edit_protocol),
                     ("Duplicate", app._on_duplicate_protocol),
                     ("Delete Selected", app._on_delete_protocol)],
            hint="Placeholders: {url}  {url_quoted}  {protocol}",
            columns=[
                {"id": "protocol", "text": "Protocol", "width": 120, "stretch": False},
                {"id": "mode",     "text": "Mode",     "width": 100, "stretch": False},
                {"id": "shell",    "text": "Shell",    "width": 60,
                 "stretch": False, "anchor": "center"},
                {"id": "command",  "text": "Command template", "width": 540},
            ],
            on_double_click=app._on_proto_double_click,
        )

    def _build_mappings(self, parent) -> None:
        app = self.app
        app.map_tree = self._crud_tab(
            parent,
            buttons=[("New Mapping", app._on_new_mapping),
                     ("Edit Selected", app._on_edit_mapping),
                     ("Delete Selected", app._on_delete_mapping)],
            hint='e.g.  f:  →  -o   (input "f:clip.mp4" appends  -o clip.mp4)',
            columns=[
                {"id": "prefix", "text": "Prefix",       "width": 120, "stretch": False},
                {"id": "flag",   "text": "Command flag", "width": 560},
            ],
            on_double_click=app._on_mapping_double_click,
        )

    def _build_log(self, parent) -> None:
        app = self.app
        ttk.Label(parent, text="Verbosity:").grid(
            row=0, column=0, sticky="w", padx=4, pady=4)
        app._make_verbosity_combo(parent, width=12).grid(
            row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(parent, text="Max log lines (0 = unlimited):").grid(
            row=1, column=0, sticky="w", padx=4, pady=4)
        sp = ttk.Spinbox(
            parent, from_=0, to=1_000_000, width=8,
            textvariable=app.log_max_lines_var,
            command=app._on_log_max_lines_changed,
        )
        sp.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        sp.bind("<FocusOut>", lambda e: app._on_log_max_lines_changed())
        sp.bind("<Return>",   lambda e: app._on_log_max_lines_changed())
        app._install_text_editing(sp, "entry")

        ttk.Label(parent, text="Log file (empty = panel only):").grid(
            row=2, column=0, sticky="w", padx=4, pady=4)
        lf_row = ttk.Frame(parent)
        lf_row.grid(row=2, column=1, sticky="ew", padx=4, pady=4)
        parent.columnconfigure(1, weight=1)
        lf_entry = ttk.Entry(lf_row, textvariable=app.log_file_var)
        lf_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        lf_entry.bind("<FocusOut>", lambda e: app._on_log_file_changed())
        lf_entry.bind("<Return>", lambda e: app._on_log_file_changed())
        app._install_text_editing(lf_entry, "entry")
        ttk.Button(lf_row, text="Browse…",
                   command=app._on_browse_log_file).pack(side=tk.LEFT, padx=4)
        ttk.Button(lf_row, text="Clear",
                   command=app._on_clear_log_file).pack(side=tk.LEFT)

        ttk.Label(
            parent,
            text=("• summary — first line + 25/50/75/100% milestones + exit\n"
                  "• verbose — every line of subprocess output\n"
                  "• silent  — just run + exit lines"),
            foreground="#666", justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=2, sticky="w",
               padx=4, pady=(12, 4))


# Source classes the LinkQueueApp façade delegates pure/read-only helpers to,
# tried in order. Dispatcher owns the dispatch/queue/state helpers; ConfigStore
# owns the path-free config-merge helpers (lq-cmplx-01).
_FACADE_DELEGATES = (Dispatcher, ConfigStore)


class _FacadeMeta(type):
    """Metaclass that delegates *class-level* attribute access on LinkQueueApp
    to Dispatcher / ConfigStore (lq-cmplx-01). The instance ``__getattr__``
    already covers ``app.<name>``; this covers ``LinkQueueApp.<name>`` call
    sites (and tests) without the parallel block of ~25 static re-exports that
    silently went stale when a helper was added to Dispatcher. One delegation
    path, sourced from the same classes, so there is nothing to keep in sync.
    """

    def __getattr__(cls, name: str):
        for src in _FACADE_DELEGATES:
            try:
                return getattr(src, name)
            except AttributeError:
                continue
        raise AttributeError(name)


class LinkQueueApp(metaclass=_FacadeMeta):
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Link Processing Queue")
        self.root.geometry("980x700")
        self.root.minsize(820, 560)

        # ConfigStore owns load/normalize/save (arch-03); paths injected (dec-03).
        self.config = ConfigStore(CONFIG_FILE, LEGACY_CONFIG_FILE)

        # Optional on-disk log sink (obs-01 / cx-06): the LogSink helper owns
        # the queue + writer thread + rotation; the app just calls
        # self._log_sink.write(line) and exposes legacy attrs as properties
        # for tests / callers that still poke at them.
        self._log_sink = LogSink(lambda: self.config.get("log_file", ""))

        # The dispatch engine — pending queue, domain-aware worker pool,
        # subprocess execution, failure cooldown and queue-state persistence —
        # lives in the UI-free Dispatcher (arch-02). LinkQueueApp wires it to
        # the UI through injected callbacks (dec-01) and proxies attribute /
        # method access to it (see __getattr__), so existing call sites such
        # as self.queue_items or self._worker_step keep working unchanged.
        # Built before _build_ui so any early state access already resolves.
        self.dispatcher = Dispatcher(
            self.config,
            log=self._log,
            refresh=self._refresh_queue_list,
            status=self._update_status,
            marshal=self._safe_after,
            get_sleep=self._get_sleep,
            get_failure_sleep=self._get_failure_sleep,
            save_config=self._save_config,
        )

        # Set when a refresh has been scheduled but hasn't yet rebuilt.
        # Lets _refresh_queue_list be called many times in a tight loop
        # (one per add, one per worker pickup, one per release) without
        # queueing N separate Treeview rebuilds. The single pending rebuild
        # always reads the current queue state at run time, so collapsing
        # them is safe — the LAST scheduled rebuild's effect is identical
        # to a fresh one as long as it runs after all the mutations.
        self._refresh_pending = False
        # Cache of the number/marker text last written per row iid (perf-06):
        # the tree diff compares against this dict instead of reading every
        # row's column back through Tcl on each refresh.
        self._row_numbers: dict = {}
        # Pending debounced link-count update id (perf-07).
        self._count_after_id = None

        # Cross-thread channel for UI updates: worker threads can't call Tk
        # methods directly (Tcl serialises non-main-thread calls and blocks
        # if the main thread is outside its event loop). Workers drop work
        # into this queue; the main thread drains it via `_poll_tk_jobs`,
        # rescheduled every 50 ms.
        #
        # Bounded so a runaway worker (or a verbose-mode flood from many
        # workers at once) can't grow the queue without limit. The poller
        # processes up to 200 jobs per cycle at 20 cycles/s = 4,000 jobs/s,
        # well above any realistic workload (4 workers * ~hundreds of
        # lines/s each tops out around 1k/s). 10,000 gives several seconds
        # of headroom for a transient burst before we have to start
        # dropping. When we DO drop, the counter is bumped and the poller
        # reports it on the log so the user can see something was lost
        # rather than silently swallowing it.
        self._tk_jobs: queue.Queue = queue.Queue(maxsize=10_000)
        self._dropped_tk_jobs = 0
        # Guards _dropped_tk_jobs so the poller's read-and-reset is atomic
        # against worker-thread increments (rel-02): without it a drop that
        # lands between the read and the reset is silently lost.
        self._dropped_lock = threading.Lock()
        self._tk_poll_id: object | None = None

        self._build_ui()
        self._refresh_protocols_tree()
        self._refresh_mappings_tree()
        self._refresh_queue_list()
        # Begin draining cross-thread UI jobs.
        self._tk_poll_id = self.root.after(50, self._poll_tk_jobs)
        # Spin up the initial worker pool to match config.
        self._ensure_worker_count(int(self.config.get("worker_count", 1)))
        # Reseed the queue with anything left from the previous session.
        # Safe to do AFTER workers exist — they'll start consuming the
        # restored items immediately, which is the natural resume behaviour.
        self._restore_queue_from_state()

        # Log YAML backend so the user can tell at a glance whether the
        # fast path (libyaml C extension) is in play. PyYAML's pure-Python
        # safe dumper is ~10-20x slower; on long state files the
        # difference is human-perceptible.
        if YAML_USING_LIBYAML:
            self._log("[startup] YAML backend: libyaml (CSafeLoader/CSafeDumper)")
        else:
            self._log(  # pragma: no cover - in-flight restore log line
                "[startup] YAML backend: pure Python (libyaml not available; "
                "install libyaml-dev + reinstall PyYAML for ~10-20x faster I/O)"
            )

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- dispatcher façade --------------------------------------------------
    # The dispatch engine's state and methods live on self.dispatcher. Any
    # attribute the app itself doesn't define (queue_items, current_items,
    # _dispatch_cv, stop_event, _run_item, _worker_step, _save_state, …) is
    # transparently delegated there, so the UI code and tests can keep using
    # the original self./app. names (arch-02 / dec-01).

    def __getattr__(self, name: str):
        # __getattr__ runs ONLY when normal attribute lookup fails, so it
        # never shadows the app's own UI methods/attrs.
        #
        # conc-02 recursion/safety guard: read `dispatcher` via __dict__ (NOT
        # plain attribute access) so a lookup that arrives here BEFORE
        # `self.dispatcher` is assigned — including a lookup OF `dispatcher`
        # itself — can't re-enter __getattr__ and recurse forever. Genuinely
        # missing names raise a clean AttributeError.
        if name == "dispatcher":
            raise AttributeError(name)
        disp = self.__dict__.get("dispatcher")
        if disp is not None:  # pragma: no cover - withdrawn-root Tk early-exit
            try:
                return getattr(disp, name)
            except AttributeError:
                pass
        # lq-cmplx-01: a pure ConfigStore helper (e.g. _normalize_config_schema)
        # accessed on the app instance resolves here, mirroring the class-level
        # delegation in _FacadeMeta so there is a single delegation contract.
        try:
            return getattr(ConfigStore, name)
        except AttributeError:
            pass
        raise AttributeError(name)

    @property
    def _cooldown_until(self) -> float:
        # conc-04: read under the lock that `_trigger_failure_cooldown`
        # writes under, so a stale read can't survive a write that
        # happened on another worker before this thread's load.
        with self.dispatcher._cooldown_lock:
            return self.dispatcher._cooldown_until

    @_cooldown_until.setter
    def _cooldown_until(self, value: float) -> None:
        with self.dispatcher._cooldown_lock:
            self.dispatcher._cooldown_until = value

    # lq-cmplx-01: the pure Dispatcher/ConfigStore helpers are NOT re-exported
    # as ~25 static class attributes any more. Class-level call sites
    # (`LinkQueueApp._domain_of(...)`, `_build_argv`, `_split_entry`, the
    # path-free config helpers `_merge_user_config` / `_normalize_config_schema`,
    # …) resolve through `_FacadeMeta.__getattr__`; instance call sites resolve
    # through the instance `__getattr__` above. A single delegation contract,
    # so a helper added to Dispatcher/ConfigStore can never go stale against a
    # hand-maintained re-export list. (Mutating helpers like `_pick_next_item`
    # are reached the same way; nothing pins them as class attributes, so a
    # class-level call binds them to their owning class, not the app proxy.)

    def _save_config(self) -> None:
        """Persist config to YAML — but if the Settings dialog is open, defer
        the write and just mark dirty, so every spinbox <FocusOut> doesn't
        trigger a redundant write. The dirty flag is flushed by
        _on_close_settings_dialog; until then the in-memory config is still the
        source of truth, so live behaviour (workers reading sleep / cap / etc.)
        is unchanged."""
        if self._settings_dialog_is_open():
            self._settings_dirty = True
            return
        self._write_config_now()

    def _save_config_quietly(self) -> None:
        """Same gating as _save_config, with the silent-on-error behaviour the
        log/verbosity handlers want."""
        if self._settings_dialog_is_open():
            self._settings_dirty = True
            return
        try:
            self.config.save()
        except Exception as e:
            self._log(f"[warn] could not save config: {e}")

    def _write_config_now(self) -> None:
        """Unconditional write — bypasses the dialog gate. Used by the Close
        handler to flush deferred changes."""
        try:
            self.config.save()
        except Exception as e:
            self._log(f"[error] could not save config: {e}")

    def _settings_dialog_is_open(self) -> bool:
        """True iff the user has clicked Settings… and not yet closed
        the dialog. Tracked explicitly via _settings_dialog_visible
        rather than reading tk's wm state, because state() can lag
        behind deiconify()/withdraw() under certain WM configurations
        (notably when the parent window is itself iconified, which
        happens in the test suite where root is withdrawn for
        invisibility — but is also a real production case if the user
        minimises the main window with the dialog open).
        """
        if self._settings_dialog is None:
            return False
        return bool(getattr(self, "_settings_dialog_visible", False))

    # -- UI -----------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assemble the main window. Each pane is built by its own helper
        (cx-01) so this stays a readable table of contents. Order matters:
        the status bar packs to BOTTOM first to claim its strip, then the
        three paned regions are added top→middle→bottom."""
        self._build_styles()
        self._init_ui_vars()
        self._build_status_bar()

        # ----- Three resizable panes: paste / queue / log -----
        self._main_paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        self._main_paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        self._build_paste_pane(self._main_paned)
        self._build_queue_pane(self._main_paned)
        self._init_settings_dialog()
        self._build_context_menus()
        self._bind_shortcuts()
        self._build_log_pane(self._main_paned)

        # Initial paint of the bottom-bar settings summary.
        self._update_status_summary()

    def _build_styles(self) -> None:
        # Make text-selection clearly visible across every Entry/Spinbox in
        # the app. The default colours under most ttk themes are very faint
        # — easy to miss that anything is selected. We force a strong blue
        # at the style level so it applies to every TEntry/TSpinbox without
        # touching individual widgets.
        self._tk_selection_bg = "#3a7bd5"
        self._tk_selection_fg = "#ffffff"
        style = ttk.Style(self.root)
        for cls in ("TEntry", "TSpinbox"):
            try:
                style.configure(
                    cls,
                    selectbackground=self._tk_selection_bg,
                    selectforeground=self._tk_selection_fg,
                    insertcolor="#222222",
                )
                style.map(
                    cls,
                    selectbackground=[("readonly", self._tk_selection_bg),
                                      ("focus",    self._tk_selection_bg),
                                      ("!focus",   self._tk_selection_bg)],
                    selectforeground=[("readonly", self._tk_selection_fg),
                                      ("focus",    self._tk_selection_fg),
                                      ("!focus",   self._tk_selection_fg)],
                )
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass

        # Pause button gets a distinct red treatment when paused so the
        # user can SEE the queue is paused at a glance instead of having
        # to read the button text. The 'TButton' default style is used
        # when active.
        try:
            style.configure(
                "Paused.TButton",
                foreground="white", background="#c0392b",
            )
            style.map(
                "Paused.TButton",
                background=[("active", "#a93226"),
                            ("disabled", "#999999")],
            )
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            pass

    def _init_ui_vars(self) -> None:
        # StringVars used by the toolbar AND the Settings dialog. Built up
        # here so widgets in either place can bind to the same source of
        # truth. Changing a value (e.g. the Sleep spinbox in the Settings
        # dialog) immediately propagates through the same _on_*_changed
        # handler used before.
        self.sleep_var = tk.StringVar(
            value=str(self.config.get("sleep_between_items", 5)))
        self.worker_count_var = tk.StringVar(
            value=str(self.config.get("worker_count", 1)))
        self.cooldown_var = tk.StringVar(
            value=str(self.config.get("failure_sleep_seconds", 300)))
        self.max_per_domain_var = tk.StringVar(
            value=str(self.config.get("max_per_domain", 0)))
        self.output_folder_var = tk.StringVar(
            value=str(self.config.get("output_folder", "")))
        self.log_verbosity_var = tk.StringVar(
            value=str(self.config.get("log_verbosity", "summary")))
        self.log_max_lines_var = tk.StringVar(
            value=str(self.config.get("log_max_lines", 100)))
        self.log_file_var = tk.StringVar(
            value=str(self.config.get("log_file", "")))
        # status bar variables (rich state, plus settings summary)
        self.status_var = tk.StringVar(value="Idle")
        self._status_state_var = tk.StringVar(value="ACTIVE")
        self._status_settings_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="0 link(s) pending")

    def _build_status_bar(self) -> None:
        # Packed FIRST so it claims its strip at the bottom; if main_paned
        # were packed first, expand=True would consume the entire root and
        # leave no cavity for the statbar.
        statbar = ttk.Frame(self.root, relief="sunken", padding=(8, 3))
        statbar.pack(side=tk.BOTTOM, fill=tk.X)
        # Active/paused state with a colored dot — flips between green
        # and red depending on _update_status output.
        self._status_state_lbl = ttk.Label(
            statbar, textvariable=self._status_state_var,
            font=("TkDefaultFont", 9, "bold"),
        )
        self._status_state_lbl.pack(side=tk.LEFT)
        ttk.Separator(statbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        # The full status string (running/pending/workers/cooldown).
        ttk.Label(statbar, textvariable=self.status_var,
                  foreground="#444").pack(side=tk.LEFT)
        ttk.Separator(statbar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8)
        # Settings summary (sleep/cooldown/max-per-domain) — kept visible
        # at a glance even though the spinboxes themselves now live in
        # the Settings dialog.
        ttk.Label(statbar, textvariable=self._status_settings_var,
                  foreground="#666").pack(side=tk.LEFT)
        # Right side: which YAML backend is in use.
        yaml_backend_label = "YAML: libyaml" if YAML_USING_LIBYAML else "YAML: pure-py"
        ttk.Label(statbar, text=yaml_backend_label,
                  foreground="#999").pack(side=tk.RIGHT)

    def _build_paste_pane(self, paned: ttk.PanedWindow) -> None:
        top = ttk.LabelFrame(
            paned,
            text="Paste link(s) — one per line; trailing prefix tokens (e.g. f:name.mp4) map to flags",
            padding=8,
        )
        paned.add(top, weight=1)

        btns = ttk.Frame(top)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        ttk.Button(btns, text="Add All", command=self._on_add).pack(side=tk.LEFT)
        ttk.Button(btns, text="Clear", command=self._on_clear_input).pack(
            side=tk.LEFT, padx=4)

        ttk.Label(btns, textvariable=self.count_var,
                  foreground="#225").pack(side=tk.LEFT, padx=12)
        ttk.Label(btns, text="Ctrl+Enter to add",
                  foreground="#666").pack(side=tk.RIGHT)

        self.url_text = scrolledtext.ScrolledText(
            top, height=5, wrap=tk.WORD, undo=True)
        self.url_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._install_text_editing(self.url_text, "text")
        self.url_text.bind("<Control-Return>", self._on_add_event)
        self.url_text.bind("<Command-Return>", self._on_add_event)
        self.url_text.bind("<KeyRelease>", lambda e: self._schedule_link_count())
        self.url_text.bind(
            "<<Paste>>",
            lambda e: self.root.after(0, self._normalize_paste_area),
        )

    def _build_queue_pane(self, paned: ttk.PanedWindow) -> None:
        qtab = ttk.Frame(paned, padding=6)
        paned.add(qtab, weight=3)

        # Slim toolbar: just the actions. Settings (incl. dispatcher knobs
        # AND the Protocols editor AND log options) live in the dialog
        # opened by the Settings… button.
        qctrl = ttk.Frame(qtab)
        qctrl.pack(fill=tk.X, pady=(0, 6))

        self.pause_btn = ttk.Button(
            qctrl, text="Pause", command=self._on_pause_toggle)
        self.pause_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(qctrl, text="Clear Queue",
                   command=self._on_clear_queue).pack(side=tk.LEFT, padx=2)
        ttk.Button(qctrl, text="Remove Selected",
                   command=self._on_remove_selected).pack(side=tk.LEFT, padx=2)
        ttk.Separator(qctrl, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2)
        ttk.Button(qctrl, text="Settings",
                   command=self._open_settings_dialog).pack(side=tk.LEFT, padx=2)
        ttk.Button(qctrl, text="Help", width=6,
                   command=self._show_shortcuts_help).pack(side=tk.LEFT, padx=2)

        # Queue treeview — same columns, same tagging.
        cols = ("idx", "protocol", "url", "command")
        self.queue_tree = ttk.Treeview(
            qtab, columns=cols, show="headings", selectmode="extended")
        self.queue_tree.heading("idx", text="#")
        self.queue_tree.heading("protocol", text="Protocol")
        self.queue_tree.heading("url", text="URL")
        self.queue_tree.heading("command", text="Resolved command")
        self.queue_tree.column("idx", width=50, anchor="e", stretch=False)
        self.queue_tree.column("protocol", width=90, stretch=False)
        self.queue_tree.column("url", width=320)
        self.queue_tree.column("command", width=380)
        self.queue_tree.tag_configure("running", foreground="#07703a")
        self.queue_tree.tag_configure("more", foreground="#999999")

        qscroll = ttk.Scrollbar(qtab, orient=tk.VERTICAL,
                                command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=qscroll.set)
        self.queue_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        qscroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _init_settings_dialog(self) -> None:
        # Built eagerly (hidden until Settings…) so proto_tree always exists
        # — Ctrl+N / Ctrl+D / context-menu paths and the initial
        # _refresh_protocols_tree() call all work without special-casing.
        self._settings_dialog: tk.Toplevel | None = None
        # True iff the user has clicked Settings… and not yet closed.
        # Tracked explicitly because reading the Toplevel's wm state
        # can lag behind deiconify()/withdraw() under certain WMs.
        self._settings_dialog_visible = False
        # Set whenever a deferred config change happens while the
        # Settings dialog is open (see _save_config). Flushed to disk
        # by _on_close_settings_dialog when the user actually closes.
        self._settings_dirty = False
        self._build_settings_dialog()

    def _build_log_pane(self, paned: ttk.PanedWindow) -> None:
        log_frame = ttk.LabelFrame(paned, text="Log", padding=6)
        paned.add(log_frame, weight=1)

        log_ctrl = ttk.Frame(log_frame)
        log_ctrl.pack(fill=tk.X)
        ttk.Label(log_ctrl, text="Verbosity:").pack(side=tk.LEFT)
        self._make_verbosity_combo(log_ctrl, width=10).pack(
            side=tk.LEFT, padx=(4, 12))
        ttk.Button(log_ctrl, text="Clear Log",
                   command=self._clear_log).pack(side=tk.RIGHT)

        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self._install_text_editing(self.log_text, "text")
        self._make_text_readonly(self.log_text)

    # --- Settings dialog (Dispatcher / Protocols / Log) -----------------

    def _build_settings_dialog(self) -> None:
        """Create the Settings Toplevel hidden. Reused by every click of
        the Settings… button — a non-modal dialog the user can leave open
        while working in the queue. All widgets bind to the SAME StringVars
        that the rest of the app uses, so changes propagate live; no
        OK/Cancel needed."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Settings — Link Processing Queue")
        dlg.geometry("780x520")
        # Settings has a fixed-content layout — block all forms of resize
        # (including maximize). resizable(False, False) handles direct
        # resize handles and greys the maximize button on most platforms,
        # but maximize can still be triggered via keyboard shortcuts or
        # title-bar double-click on certain WMs. maxsize() sets a WM-
        # enforced upper bound that caps the action even if it fires.
        # transient() marks it as a dialog window so most WMs hide the
        # maximize control entirely.
        dlg.resizable(False, False)
        dlg.maxsize(780, 520)
        try:
            dlg.transient(self.root)
        except tk.TclError:  # pragma: no cover - WM-only failure path
            pass
        dlg.withdraw()                       # hidden until requested
        # Closing the X just hides; flushes any deferred config writes
        # (see _save_config). Keeps state and keeps proto_tree alive.
        dlg.protocol("WM_DELETE_WINDOW", self._on_close_settings_dialog)
        self._settings_dialog = dlg

        nb = ttk.Notebook(dlg)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        _SettingsTabs(self).build(nb)   # cx-05: tab content lives in its own class

        # Close button strip.
        bottom = ttk.Frame(dlg, padding=8)
        bottom.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(
            bottom,
            text="Changes apply immediately — no Apply needed.",
            foreground="#666",
        ).pack(side=tk.LEFT)
        ttk.Button(bottom, text="Close",
                   command=self._on_close_settings_dialog).pack(side=tk.RIGHT)

    @staticmethod
    def _refresh_tree(tree, rows) -> None:
        """Clear `tree` and repopulate it from an iterable of (iid, values)
        pairs (dup-06)."""
        for r in tree.get_children():
            tree.delete(r)
        for iid, values in rows:
            tree.insert("", tk.END, iid=iid, values=values)

    def _refresh_mappings_tree(self) -> None:
        self._refresh_tree(self.map_tree, (
            (prefix, (prefix, flag))
            for prefix, flag in sorted(self.config.get("token_mappings", {}).items())
        ))

    def _on_new_mapping(self) -> None:
        self._open_mapping_editor(None)

    def _on_edit_mapping(self) -> None:
        self._crud_edit(self.map_tree, self._open_mapping_editor)

    def _on_mapping_double_click(self, event) -> None:
        self._crud_double_click(self.map_tree, event, self._open_mapping_editor)

    def _on_delete_mapping(self) -> None:
        self._crud_delete(self.map_tree, self.config["token_mappings"],
                          "mapping", self._refresh_mappings_tree,
                          after=self._update_link_count)

    def _open_mapping_editor(self, prefix: "str | None") -> None:
        # Own dialog class (cx-04/arch-06), depending on a narrow host.
        host = MappingEditorHost(
            root=self.root,
            mappings=self.config.setdefault("token_mappings", {}),
            install_text_editing=self._install_text_editing,
            center_and_grab=self._center_and_grab,
            save=self._save_config,
            refresh=self._refresh_mappings_tree,
            log=self._log,
            after=self._update_link_count,
        )
        MappingEditor(host, prefix)

    def _make_verbosity_combo(self, parent: tk.Widget, width: int) -> ttk.Combobox:
        """A log-verbosity dropdown bound to log_verbosity_var + its handler.
        Built in two places (Log toolbar and Settings>Log), so factored here
        (dup-03)."""
        combo = ttk.Combobox(
            parent, textvariable=self.log_verbosity_var,
            values=["summary", "verbose", "silent"],
            state="readonly", width=width,
        )
        combo.bind("<<ComboboxSelected>>",
                   lambda e: self._on_log_verbosity_changed())
        return combo

    def _on_log_file_changed(self) -> None:
        # Next _log() reopens/closes the sink lazily (path-change aware).
        self._apply_path_setting(
            self.log_file_var, "log_file",
            "[config] log file: {path}", "[config] log file disabled")

    def _on_browse_log_file(self) -> None:
        from tkinter import filedialog
        self._browse_into(
            self.log_file_var, self._on_log_file_changed,
            lambda parent: filedialog.asksaveasfilename(
                title="Choose log file", parent=parent,
                initialfile="link_queue.log"))

    def _on_clear_log_file(self) -> None:
        self.log_file_var.set("")
        self._on_log_file_changed()

    def _open_settings_dialog(self) -> None:
        """Show the Settings Toplevel. Idempotent — repeated clicks just
        bring it to the front."""
        if self._settings_dialog is None:
            # Race-only: dialog was destroyed somehow. Rebuild.
            self._build_settings_dialog()
        self._settings_dialog.deiconify()
        self._settings_dialog.lift()
        self._settings_dialog_visible = True
        try:
            self._settings_dialog.focus_set()
        except tk.TclError:  # pragma: no cover - withdrawn-window edge
            pass

    def _on_close_settings_dialog(self) -> None:
        """Hide the Settings dialog, flushing any config changes that
        were deferred while it was open. The dirty flag is set by
        _save_config (and per-handler comparisons skip setting it when
        the value didn't actually change), so no write happens at all
        if the user opened the dialog and either changed nothing or
        changed something back to its original value.

        Also drops focus from any spinbox/entry first so a final
        FocusOut handler runs against the current dialog-open state
        (and is therefore subject to the same gating). Without this
        nudge, a focused spinbox's pending unsubmitted edit could be
        applied AFTER our flush.
        """
        if self._settings_dialog is None:
            return
        try:
            # Force any focused entry to commit its value through the
            # FocusOut handler BEFORE we flush.
            self._settings_dialog.focus_set()
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            pass
        if self._settings_dirty:
            self._settings_dirty = False
            self._write_config_now()
            self._log("[config] settings saved")
        self._settings_dialog_visible = False
        try:
            self._settings_dialog.withdraw()
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            pass

    def _on_log_verbosity_changed(self) -> None:
        v = str(self.log_verbosity_var.get()).lower()
        if v not in ("summary", "verbose", "silent"):
            v = "summary"
            self.log_verbosity_var.set(v)
        if self.config.get("log_verbosity") != v:  # pragma: no cover - withdrawn-root Tk early-exit
            self.config["log_verbosity"] = v
            self._save_config_quietly()
            self._log(f"[config] log verbosity: {v}")

    def _on_log_max_lines_changed(self) -> None:
        try:
            n = max(0, int(float(self.log_max_lines_var.get())))
        except (TypeError, ValueError):
            n = int(self.config.get("log_max_lines", 100))
            self.log_max_lines_var.set(str(n))
        if self.config.get("log_max_lines") != n:
            self.config["log_max_lines"] = n
            self._save_config_quietly()
            self._log(f"[config] log_max_lines: {n}")

    def _read_int_setting(self, var, config_key: str, default: int) -> int:
        """Parse a Tk StringVar as int, falling back to the config value
        on parse failure (lq-perf-01: centralises the try/except parse
        pattern that `_update_status_summary` used to repeat three
        times)."""
        try:
            return int(float(var.get()))
        except Exception:
            return int(self.config.get(config_key, default))

    def _update_status_summary(self) -> None:
        """Refresh the right-hand status-bar fragment that summarises the
        dispatcher knobs (since their spinboxes now live in the Settings
        dialog and aren't visible in the main window).

        lq-perf-01: factored through `_read_int_setting` so the three
        try/except parse blocks aren't repeated; the helper short-circuits
        to the config value when the Tk widget content is unparseable."""
        sleep_s = self._read_int_setting(
            self.sleep_var, "sleep_between_items", 5,
        )
        cd_s = self._read_int_setting(
            self.cooldown_var, "failure_sleep_seconds", 300,
        )
        cap = self._read_int_setting(
            self.max_per_domain_var, "max_per_domain", 0,
        )
        cap_text = "no domain cap" if cap <= 0 else f"max/dom: {cap}"
        text = (f"sleep {self._format_duration(sleep_s)}   "
                f"cooldown {self._format_duration(cd_s)}   "
                f"{cap_text}")
        self._status_settings_var.set(text)

    # -- adding / dispatching ----------------------------------------------

    def _on_add_event(self, event=None) -> str:
        # Bound to Ctrl+Enter in the Text widget; returning "break" prevents the
        # default newline insertion.
        self._on_add()
        return "break"

    def _on_add(self) -> None:
        raw = self.url_text.get("1.0", tk.END)
        entries = self._parse_entries(raw, self.config.get("token_mappings", {}))
        # Surface lines that produced no URL (e.g. only prefix tokens) instead
        # of silently dropping them and their mapped params (rel-09).
        nonblank = sum(1 for ln in raw.splitlines() if ln.strip())
        dropped = nonblank - len(entries)
        if dropped > 0:
            self._log(f"[add] skipped {dropped} line(s) with no URL")
        if not entries:
            self._log("[add] no links detected")
            return

        counts = {"queue": 0, "immediate": 0, "default": 0, "duplicate": 0}
        with self._batch_dispatch():
            for url, extra in entries:
                outcome = self._process_link(url, extra)
                counts[outcome] = counts.get(outcome, 0) + 1

        total = sum(counts.values())
        summary = (
            f"[add] processed {total} link(s): "
            f"{counts['queue']} queued, {counts['immediate']} immediate"
        )
        if counts["default"]:
            summary += f", {counts['default']} via default handler"  # pragma: no cover - summary with default-handler counts
        if counts["duplicate"]:
            summary += f", {counts['duplicate']} duplicate(s) skipped"  # pragma: no cover - summary with duplicate counts
        self._log(summary)

        self.url_text.delete("1.0", tk.END)
        self._update_link_count()

    def _on_clear_input(self) -> None:
        self.url_text.delete("1.0", tk.END)
        self._update_link_count()

    def _schedule_link_count(self) -> None:
        """Debounce the link count on keystrokes (perf-07): re-parsing the
        whole textarea on every <KeyRelease> lags on large pastes, so coalesce
        rapid typing into one update ~200 ms after the last key."""
        if self._count_after_id is not None:
            try:
                self.root.after_cancel(self._count_after_id)
            except Exception:
                pass
        self._count_after_id = self.root.after(200, self._update_link_count)

    def _update_link_count(self) -> None:
        self._count_after_id = None
        n = len(self._parse_entries(
            self.url_text.get("1.0", tk.END), self.config.get("token_mappings", {})))
        self.count_var.set(f"{n} link(s) pending")

    @staticmethod
    def _normalize_paste_text(raw: str) -> str:
        """Pure helper: strip per-line whitespace and blank lines, then
        ensure exactly one trailing newline so the cursor can park on a
        fresh empty line. Empty input -> empty output (no spurious \\n)."""
        non_empty = [ln for ln in (ln.strip() for ln in raw.split("\n")) if ln]
        if not non_empty:
            return ""
        return "\n".join(non_empty) + "\n"

    def _normalize_paste_area(self) -> None:
        """Strip blank lines + per-line whitespace from the paste textarea
        and ensure the cursor is parked on a fresh empty line below the
        last URL.

        Called on <<Paste>>. Two things matter:

        1. The user's input field should visibly mirror what _parse_entries
           will see — otherwise pasted blocks with separator blank lines
           look messy until Add All clears the area.

        2. After the paste, the next paste / keystroke must NOT glue onto
           the last URL. Tk's behaviour is to park the insert mark right
           after the inserted text, which sits at the end of the last URL
           if we don't explicitly add a trailing newline. We add one and
           move the cursor onto it.

        We also park the cursor at "end-1c" (rather than tk.END) — tk.END
        points PAST the widget's implicit trailing newline, where on some
        platforms inserting Enter is a no-op (the keystroke appears
        eaten). "end-1c" puts the cursor on the trailing empty line we
        just created, where Enter and paste both behave normally.
        """
        raw = self.url_text.get("1.0", "end-1c")
        cleaned = self._normalize_paste_text(raw)
        if cleaned != raw:  # pragma: no cover - withdrawn-root Tk early-exit
            self.url_text.delete("1.0", tk.END)
            self.url_text.insert("1.0", cleaned)
        # Always park the cursor on the trailing empty line — even when
        # `cleaned == raw` (idempotent paste), the insert mark may still
        # be sitting at the end of the last URL after Tk's default paste
        # handling, which would glue the next paste.
        self.url_text.mark_set("insert", "end-1c")
        self._update_link_count()

    # -- worker pool --------------------------------------------------------

    def _on_worker_count_changed(self) -> None:
        try:
            n = int(float(self.worker_count_var.get()))
        except ValueError:
            n = int(self.config.get("worker_count", 1))
        n = max(1, min(32, n))
        # Reflect the clamped value back into the UI.
        self.worker_count_var.set(str(n))
        prev = int(self.config.get("worker_count", 1))
        if n != prev:
            self._log(f"[config] worker count {prev} → {n}")
        self._ensure_worker_count(n)

    def _update_status(self) -> None:
        # alive+stopping in one pool-lock acquisition so they agree (conc-01).
        alive, stopping = self._pool.counts()
        with self.queue_lock:
            running = sum(1 for v in self.current_items.values() if v is not None)
            pending = len(self.queue_items)
        target = int(self.config.get("worker_count", 1))
        with self._cooldown_lock:
            cd_remaining = max(0.0, self._cooldown_until - time.time())

        # Worker fragment: surfaces transitional states so the user always
        # sees the truth (alive count vs target) instead of a count that
        # collapses the moment they bumped the spinbox.
        if stopping > 0:
            worker_text = f"{alive} worker(s), target {target}, {stopping} stopping"
        elif alive != target:
            worker_text = f"{alive}/{target} worker(s)"
        else:
            worker_text = f"{alive} worker(s)"

        if self.pause_event.is_set():
            text = f"Paused — {worker_text}, {pending} pending"
            state, color = "● PAUSED", "#c0392b"
        elif cd_remaining > 0:
            text = (
                f"Cooling down {self._format_duration(int(cd_remaining + 0.5))} "
                f"after failure • {pending} pending"
            )
            state, color = "● COOLDOWN", "#e67e22"
        elif running > 0:
            text = f"{running} working • {pending} pending • {worker_text}"
            state, color = "● ACTIVE", "#1e8449"
        elif pending > 0:
            text = f"0 working • {pending} pending • {worker_text}"
            state, color = "● ACTIVE", "#1e8449"
        else:
            text = f"Idle • {worker_text}"
            state, color = "● IDLE", "#7f8c8d"
        metrics_text = self._metrics_summary()
        if metrics_text:
            text = f"{text} • {metrics_text}"
        self._safe_after(0, self.status_var.set, text)
        self._safe_after(0, self._status_state_var.set, state)
        # Color updates must run on the main thread.
        def _set_state_color(c=color):
            try:
                self._status_state_lbl.configure(foreground=c)
            except (tk.TclError, AttributeError):
                pass
        self._safe_after(0, _set_state_color)

    def _get_int_setting(self, var, key, default, clamp_min=None) -> int:
        """Shared body for the spinbox getters (dup-01): parse the var as an
        int (via float, so "5.0" works), clamp to `clamp_min` when given, and
        fall back to the stored config value on any parse error."""
        try:
            value = int(float(var.get()))
        except Exception:
            return int(self.config.get(key, default))
        return max(clamp_min, value) if clamp_min is not None else value

    def _get_sleep(self) -> int:
        return self._get_int_setting(self.sleep_var, "sleep_between_items", 5)

    def _persist_if_changed(self, key, value, prev, log_msg) -> bool:
        """Persist `value` to `self.config[key]` + save + log iff it differs
        from `prev` (dup-07). Returns True on change. Centralises the
        read/compare/save/log/refresh shape shared by `_apply_int_setting`
        and `_apply_path_setting`."""
        if value == prev:
            return False
        self.config[key] = value
        self._save_config()
        self._log(log_msg(value) if callable(log_msg) else log_msg)
        return True

    def _apply_int_setting(self, var, getter, key, default, log_msg,
                           after=None) -> None:
        """Shared body for the spinbox change handlers (dup-01 / dup-07):
        read the clamped value via `getter`, reflect it back into `var`,
        delegate the persist+log decision to `_persist_if_changed`, run an
        optional `after(v)` side effect, then repaint the status summary."""
        v = getter()
        var.set(str(v))
        self._persist_if_changed(
            key, v, int(self.config.get(key, default)), log_msg,
        )
        if after is not None:
            after(v)
        self._update_status_summary()

    def _on_sleep_changed(self) -> None:
        self._apply_int_setting(
            self.sleep_var, self._get_sleep, "sleep_between_items", 5,
            lambda v: f"[config] sleep set to {v}s")

    def _get_failure_sleep(self) -> int:
        return self._get_int_setting(
            self.cooldown_var, "failure_sleep_seconds", 300, clamp_min=0)

    def _on_cooldown_changed(self) -> None:
        self._apply_int_setting(
            self.cooldown_var, self._get_failure_sleep,
            "failure_sleep_seconds", 300,
            lambda v: f"[config] failure cooldown set to {self._format_duration(v)}")

    def _get_max_per_domain(self) -> int:
        return self._get_int_setting(
            self.max_per_domain_var, "max_per_domain", 0, clamp_min=0)

    def _on_max_per_domain_changed(self) -> None:
        def wake_workers(_v):
            # Wake all workers so any that were skipping over capped items get
            # to re-evaluate (e.g. after the user RAISED the cap).
            with self._dispatch_cv:
                self._dispatch_cv.notify_all()
        self._apply_int_setting(
            self.max_per_domain_var, self._get_max_per_domain,
            "max_per_domain", 0,
            lambda v: ("[config] per-domain worker cap removed" if v == 0
                       else f"[config] per-domain worker cap set to {v}"),
            after=wake_workers)

    def _apply_path_setting(self, var, key, on_msg, off_msg) -> None:
        """Shared body for the path settings handlers (dup-04 / dup-07):
        strip the var, delegate the persist+log decision to
        `_persist_if_changed`, then write the normalised value back."""
        v = var.get().strip()
        self._persist_if_changed(
            key, v, self.config.get(key, ""),
            lambda val: on_msg.format(path=val) if val else off_msg,
        )
        var.set(v)

    def _browse_into(self, var, changed, run_dialog) -> None:
        """Run a file/dir chooser parented to whichever window the Browse
        button is in (the Settings dialog when open, else root — otherwise the
        chooser can open BEHIND the still-visible dialog), then apply the pick
        (dup-04). `run_dialog(parent)` returns the chosen path or ''."""
        parent = (self._settings_dialog
                  if self._settings_dialog_is_open() else self.root)
        chosen = run_dialog(parent)
        if chosen:  # pragma: no cover - withdrawn-root Tk early-exit
            var.set(chosen)
            changed()

    def _on_output_folder_changed(self) -> None:
        self._apply_path_setting(
            self.output_folder_var, "output_folder",
            "[config] output folder set to {path}",
            "[config] output folder cleared (running in script dir)")

    def _on_browse_output_folder(self) -> None:
        from tkinter import filedialog
        initial = self.output_folder_var.get().strip()
        if not initial or not os.path.isdir(initial):  # pragma: no cover - withdrawn-root Tk early-exit
            initial = os.path.expanduser("~")
        self._browse_into(
            self.output_folder_var, self._on_output_folder_changed,
            lambda parent: filedialog.askdirectory(
                title="Select output folder", initialdir=initial,
                mustexist=True, parent=parent))

    def _on_clear_output_folder(self) -> None:
        self.output_folder_var.set("")
        self._on_output_folder_changed()

    def _on_pause_toggle(self) -> None:
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="Pause", style="TButton")
            self._log("[queue] resumed")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="Resume", style="Paused.TButton")
            self._log("[queue] paused")
        # Wake every waiting worker so they immediately observe the change
        # rather than continuing to sleep on the cv.
        with self._dispatch_cv:
            self._dispatch_cv.notify_all()
        self._update_status()

    def _on_clear_queue(self) -> None:
        with self._dispatch_cv:
            n = len(self.queue_items)
            self.queue_items.clear()
            # No need to notify — there's nothing to claim.
        self._refresh_queue_list()
        self._log(f"[queue] cleared ({n} pending item(s) removed)")
        # rel-04: use the debounced save path (same as other mutations) so the
        # whole YAML file isn't written synchronously on the UI thread.
        self._request_save_state()

    def _on_remove_selected(self) -> None:
        sel = self.queue_tree.selection()
        if not sel:
            return
        # Map the selection back to QueueItem URLs by resolving each pending
        # iid against the live queue's hashed-iid map (rel-12 — iids are now a
        # `p:<blake2b>` hash, not `p:<url>`); in-flight rows ("r:<idx>") are
        # not removable and are dropped. Removing by URL (not by row position)
        # is immune to the queue being mutated by a worker between paint and
        # click (rel-01).
        with self.queue_lock:
            iid_to_url = {_queue_iid_for_url(it.url): it.url
                          for it in self.queue_items}
        urls = [iid_to_url[iid] for iid in sel if iid in iid_to_url]
        removed = self._remove_pending_urls(urls)

        self._refresh_queue_list()
        for it in removed:
            self._log(f"[queue] removed {it.url}")
        if removed:  # pragma: no cover - withdrawn-root Tk early-exit
            self._save_state()

    def _remove_pending_urls(self, urls: "list[str]") -> "list[QueueItem]":
        """Remove pending items whose URL is in `urls`, atomically under the
        dispatch cv. Returns the items actually removed (may be fewer than
        requested if a worker already claimed one in the interim)."""
        want = set(urls)
        if not want:
            return []
        with self._dispatch_cv:
            removed = [it for it in self.queue_items if it.url in want]
            # rel-03: remove each target individually so survivors keep their
            # original insertion seq / global-FIFO ordering. A slice-assign
            # would clear() and re-append every survivor, re-basing the seq.
            for it in removed:
                self.queue_items.remove(it)
        return removed

    # -- tree refresh -------------------------------------------------------

    def _refresh_queue_list(self) -> None:
        """Schedule a queue-tree rebuild on the main thread. Coalesced:
        if a rebuild is already pending, additional calls collapse into
        the in-flight one (which always reads the latest queue state at
        run time, so the result is the same as a fresh rebuild).

        Safe to call from any thread — workers go through _safe_after.
        """
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._safe_after(0, self._do_refresh_queue_list)

    def _do_refresh_queue_list(self) -> None:
        # Clear the flag FIRST so any mutation that lands while we're
        # rebuilding can schedule a follow-up rebuild against fresh data.
        self._refresh_pending = False
        limit = self._render_limit()
        with self.queue_lock:
            total = len(self.queue_items)
            # Only the rows that will actually be drawn are snapshotted, so a
            # huge backlog isn't fully copied on every refresh (perf-05).
            if limit <= 0:
                shown = list(self.queue_items)  # pragma: no cover - shown items snapshot branch
            else:
                shown = list(itertools.islice(self.queue_items, limit))
            running = sorted(
                ((idx, it) for idx, it in self.current_items.items()
                 if it is not None),
                key=lambda kv: kv[0])

        # Build the desired row order. Stable per-row iids ("r:<worker idx>"
        # for in-flight, "p:<url>" for pending — URLs are unique because
        # enqueue dedupes) let remove/re-run map a selection back to the exact
        # item by identity (rel-01) AND let this refresh DIFF the tree instead
        # of rebuilding it: only changed rows are touched, and the costly
        # _item_display() is computed only for genuinely new rows (perf-04).
        desired = self._desired_queue_rows(shown, running, total, limit)
        self._apply_queue_rows(desired, self.queue_tree)

    def _apply_queue_rows(self, desired, tree) -> None:
        """Reconcile `tree` to the `desired` rows (cmplx-01: the apply phase,
        split from the snapshot phase in _do_refresh_queue_list so each is
        separately testable).

        Rows no longer wanted are deleted; survivors keep their relative order
        (claims/removals never reorder the rest), so inserting each new row at
        its target position and refreshing only the leading number column
        reconstitutes the order without any per-row move() or full rebuild. The
        number/note is only pushed to Tcl when it differs from our cache
        (perf-06)."""
        desired_iids = {d[0] for d in desired}
        for iid in tree.get_children():
            if iid not in desired_iids:
                tree.delete(iid)
        prev_numbers = self._row_numbers
        new_numbers: dict = {}
        for pos, (iid, idx_text, item, tags, note) in enumerate(desired):
            cached = note if item is None else idx_text
            new_numbers[iid] = cached
            if tree.exists(iid):
                if prev_numbers.get(iid) == cached:
                    continue               # unchanged -> no Tcl call at all
                if item is None:           # the "… N more" summary row
                    tree.set(iid, "url", note)  # pragma: no cover - tree set with note text
                else:
                    tree.set(iid, "idx", idx_text)
            elif item is None:
                tree.insert("", pos, iid=iid, tags=tags,
                            values=(idx_text, "", note, ""))
            else:
                tree.insert(
                    "", pos, iid=iid, tags=tags,
                    values=(idx_text, item.protocol, item.url,
                            self._item_display(item)))
        self._row_numbers = new_numbers

    def _render_limit(self) -> int:
        try:
            return int(self.config.get("queue_render_limit", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _desired_queue_rows(self, shown, running, total, limit):
        """The (iid, number-text, item, tags, note) rows the queue tree should
        show, in order: in-flight markers, then the already-capped `shown`
        pending items numbered 1..len(shown). When `total` exceeds `limit` the
        overflow collapses into one sentinel row (item=None) so a huge backlog
        can't make the Treeview the bottleneck (scal-02). `shown` is already
        limited by the caller so the whole queue isn't copied (perf-05).

        The pending-row iid is derived by hashing the URL (rel-12) — Tk's
        Treeview reserves a few characters in iids (``{`` ``}`` whitespace),
        and a URL like ``https://example.com/path?q={x}`` would silently
        break ``tree.exists(iid)``. A short blake2b hash is collision-safe at
        this scale and stable across re-renders for the same URL."""
        rows = [(f"r:{idx}", f"▶#{idx}", item, ("running",), "")
                for idx, item in running]
        for i, item in enumerate(shown, start=1):
            rows.append((_queue_iid_for_url(item.url), str(i), item, (), ""))
        if limit > 0 and total > limit:
            extra = total - limit
            rows.append(("more", "",
                         None, ("more",),
                         f"… and {extra} more pending (render limit {limit})"))
        return rows

    def _refresh_protocols_tree(self) -> None:
        self._refresh_tree(self.proto_tree, (
            (proto, (proto, cfg.get("mode", "queue"),
                     "yes" if bool(cfg.get("shell", False)) else "no",
                     cfg.get("command", "")))
            for proto, cfg in sorted(self.config["protocols"].items())
        ))

    # -- generic tree CRUD (shared by Protocols + Mappings — dup-05) --------

    def _crud_edit(self, tree, opener) -> None:
        sel = tree.selection()
        if sel:
            opener(sel[0])

    def _crud_double_click(self, tree, event, opener) -> None:
        # Use the row under the cursor, not a stale selection.
        row_id = tree.identify_row(event.y)
        if not row_id:
            return  # pragma: no cover - early return when no widget focused
        tree.selection_set(row_id)
        opener(row_id)

    def _crud_delete(self, tree, store, label, refresh, after=None) -> None:
        sel = tree.selection()
        if not sel:
            return
        key = sel[0]
        if messagebox.askyesno(f"Delete {label}", f"Delete {label} '{key}'?"):  # pragma: no cover - withdrawn-root Tk early-exit
            store.pop(key, None)
            self._save_config()
            refresh()
            if after is not None:
                after()
            self._log(f"[config] deleted {label} '{key}'")

    # -- protocol editor ----------------------------------------------------

    def _on_new_protocol(self) -> None:
        self._open_protocol_editor(None)

    def _on_edit_protocol(self) -> None:
        self._crud_edit(self.proto_tree, self._open_protocol_editor)

    def _on_proto_double_click(self, event) -> None:
        self._crud_double_click(self.proto_tree, event, self._open_protocol_editor)

    def _on_delete_protocol(self) -> None:
        self._crud_delete(self.proto_tree, self.config["protocols"],
                          "protocol", self._refresh_protocols_tree)

    def _open_protocol_editor(self, proto: str | None) -> None:
        # The dialog lives in its own class (cx-02) and depends only on this
        # narrow host contract (arch-04), not the whole app.
        host = ProtocolEditorHost(
            root=self.root,
            protocols=self.config["protocols"],
            install_text_editing=self._install_text_editing,
            center_and_grab=self._center_and_grab,
            save=self._save_config,
            refresh=self._refresh_protocols_tree,
            log=self._log,
        )
        ProtocolEditor(host, proto)

    # -- editing helpers (cut/copy/paste/select-all + context menu) ---------

    def _install_text_editing(self, widget, kind: str = "auto") -> None:
        """Install reliable Cut/Copy/Paste/Select-All bindings + a right-click
        context menu on a text-input widget.

        Tk's class bindings for these shortcuts technically exist by default,
        but they're flaky in practice: they depend on the active keyboard
        layout (the keysym 'c' may not match what's typed under non-US
        layouts), and on some ttk themes the selection highlight is so faint
        the user can't see it. We bind explicitly at the widget level —
        returning 'break' so any later root-level handler can't hijack the
        event — and force visible selection colours on tk.Text widgets.

        kind:
          "text"  -> tk.Text/ScrolledText (uses tag_add/index 'sel')
          "entry" -> tk.Entry/ttk.Entry/ttk.Spinbox (uses select_range)
          "auto"  -> infer from widget class
        """
        if kind == "auto":
            kind = "text" if isinstance(widget, tk.Text) else "entry"  # pragma: no cover - widget kind detection: Text vs Entry

        # ---- Selection visibility on plain tk.Text (ttk handled at style) -
        if kind == "text":
            try:
                widget.configure(
                    selectbackground=self._tk_selection_bg,
                    selectforeground=self._tk_selection_fg,
                    inactiveselectbackground=self._tk_selection_bg,
                )
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass

        # ---- Helpers --------------------------------------------------------
        def do_copy(_e=None):
            try:
                widget.event_generate("<<Copy>>")
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass
            return "break"

        def do_cut(_e=None):
            try:
                widget.event_generate("<<Cut>>")
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass
            return "break"

        def do_paste(_e=None):
            try:
                widget.event_generate("<<Paste>>")
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass
            return "break"

        def do_select_all(_e=None):
            try:
                if kind == "text":
                    widget.tag_add("sel", "1.0", "end-1c")
                    widget.mark_set("insert", "1.0")
                    widget.see("insert")
                else:
                    widget.select_range(0, "end")
                    widget.icursor("end")
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass
            return "break"

        # ---- Keyboard bindings -------------------------------------------
        # IMPORTANT: we deliberately do NOT bind <Control-Key-c|v|x>. Tk's
        # Text and Entry class bindings already handle these via the virtual
        # events <<Copy>>, <<Paste>>, <<Cut>>. Adding our own bindings on top
        # — even when they call event_generate — has historically interacted
        # badly with focus, IME state, and keyboard layouts on some Linux
        # window managers (symptom: Enter and other keys appear to be eaten
        # after a paste, so subsequent input lands concatenated on the same
        # line). See user report.
        #
        # We DO bind Ctrl+A explicitly because Tk's Text class on
        # Linux/Windows does not provide a default select-all (only macOS
        # does, via Cmd+A).
        widget.bind("<Control-Key-a>", do_select_all)
        widget.bind("<Control-Key-A>", do_select_all)
        widget.bind("<Command-Key-a>", do_select_all)  # macOS
        widget.bind("<Command-Key-A>", do_select_all)

        # ---- Right-click context menu --------------------------------------
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Cut",        accelerator="Ctrl+X", command=do_cut)
        menu.add_command(label="Copy",       accelerator="Ctrl+C", command=do_copy)
        menu.add_command(label="Paste",      accelerator="Ctrl+V", command=do_paste)
        menu.add_separator()
        menu.add_command(label="Select All", accelerator="Ctrl+A", command=do_select_all)

        def show_menu(event):
            # Make sure the widget gets focus so Cut/Copy/Paste have a target.
            try:
                widget.focus_set()
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                pass
            # Disable items that can't apply right now.
            try:
                has_sel = bool(widget.tag_ranges("sel")) if kind == "text" \
                    else widget.selection_present()
            except (tk.TclError, AttributeError):
                has_sel = False
            # Read-only widgets refuse paste/cut; detect via 'state'.
            try:
                st = str(widget.cget("state"))
            except tk.TclError:  # pragma: no cover - Tk teardown defensive
                st = "normal"
            editable = st not in ("disabled", "readonly")
            menu.entryconfigure("Cut",   state=tk.NORMAL if (has_sel and editable) else tk.DISABLED)
            menu.entryconfigure("Copy",  state=tk.NORMAL if has_sel else tk.DISABLED)
            menu.entryconfigure("Paste", state=tk.NORMAL if editable else tk.DISABLED)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", show_menu)   # Linux/Windows right-click
        widget.bind("<Button-2>", show_menu)   # macOS right-click

    def _make_text_readonly(self, widget) -> None:
        """Make a tk.Text behave as read-only WITHOUT setting state=DISABLED.

        state=DISABLED prevents the user from selecting and copying on many
        platforms, which is what people actually want to do with a log. We
        keep the widget in state=NORMAL (so selection works) and reject
        edits via key/paste/cut bindings instead.
        """
        widget.bind("<Key>", self._readonly_block_key)
        widget.bind("<<Paste>>", lambda e: "break")
        widget.bind("<<Cut>>",   lambda e: "break")

    # Modifier-key state bits (Tk): Shift=0x1, Control=0x4
    _READONLY_ALLOW_NAV = {
        "Left", "Right", "Up", "Down",
        "Home", "End", "Prior", "Next",
        "Shift_L", "Shift_R", "Control_L", "Control_R",
        "Alt_L", "Alt_R", "Meta_L", "Meta_R",
        "Tab", "ISO_Left_Tab", "Escape",
    }

    @classmethod
    def _readonly_block_key(cls, event):
        """<Key> handler for read-only Text widgets: allow navigation and
        Ctrl+C / Ctrl+A (copy / select-all), block everything that would
        edit. A named method (vs an inline closure) so it's directly
        unit-testable."""
        ks = event.keysym
        if ks in cls._READONLY_ALLOW_NAV:
            return None
        # With Control held, allow copy/select-all but block cut/paste.
        if event.state & 0x4:
            if ks.lower() in ("c", "a"):
                return None
            # everything else (incl. Ctrl-V, Ctrl-X, Ctrl-Z) is blocked
            return "break"
        # Plain typed character -> block.
        return "break"

    # -- logging ------------------------------------------------------------

    def _safe_after(self, ms, func, *args) -> None:
        """Schedule a UI-thread callback, silently tolerating shutdown races.

        From the main thread, schedules via `root.after()` directly. From any
        other thread (worker, immediate-runner), this CANNOT call into Tk
        directly: Tcl's threading model serialises non-main-thread calls
        through the main thread's event loop, so a worker's call blocks
        whenever the main thread is outside `mainloop()` (e.g. in a
        synchronous handler or — pathologically — busy-polling). We avoid
        that by depositing the call into a queue.Queue that the main thread
        drains every 50 ms (see `_poll_tk_jobs`).
        """
        if self.stop_event.is_set():
            return
        if threading.current_thread() is threading.main_thread():
            try:
                self.root.after(ms, func, *args)
            except (RuntimeError, tk.TclError):  # pragma: no cover - Tk teardown defensive
                pass
        else:
            # Cross-thread path — never touches Tk directly.
            try:
                self._tk_jobs.put_nowait((func, args))
            except queue.Full:
                # Drop the job and bump a counter; the poller surfaces a
                # single warning message per drained burst so the user
                # knows something was lost. Workers must NEVER block on
                # the UI — that's the whole point of the queue.
                with self._dropped_lock:
                    self._dropped_tk_jobs += 1
            except Exception:  # pragma: no cover - defensive exception in tk poll
                pass  # pragma: no cover - defensive exception in tk poll

    def _poll_tk_jobs(self) -> None:
        """Drain queued cross-thread UI jobs on the main thread (every 50 ms).
        Capped per cycle to keep the event loop responsive under bursts."""
        if self.stop_event.is_set():
            self._tk_poll_id = None
            return
        # If anything was dropped since last drain, surface a single
        # consolidated warning. Read-and-reset is done under _dropped_lock
        # (rel-02) so a concurrent worker increment can't be lost between
        # the read and the reset.
        with self._dropped_lock:
            dropped = self._dropped_tk_jobs
            self._dropped_tk_jobs = 0
        if dropped > 0:
            self._log(
                f"[warn] dropped {dropped} cross-thread UI job(s) "
                f"(queue full — worker output too fast for UI poller)"
            )
        for _ in range(200):  # pragma: no cover - withdrawn-root Tk early-exit
            try:
                fn, args = self._tk_jobs.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                pass  # never let a UI bug nuke the poller
        try:
            self._tk_poll_id = self.root.after(50, self._poll_tk_jobs)
        except (RuntimeError, tk.TclError):  # pragma: no cover - Tk teardown defensive
            self._tk_poll_id = None

    def _log(self, msg: str) -> None:
        now = datetime.now()
        # Panel keeps the compact clock; the file sink gets a full ISO
        # datetime so lines stay unambiguous across midnight / multi-day
        # runs (obs-03).
        ui_line = f"[{now.strftime('%H:%M:%S')}] {msg}\n"
        file_line = f"[{now.isoformat(timespec='seconds')}] {msg}\n"
        # Persist to the file sink BEFORE the UI hop so a line survives even
        # if the cross-thread UI queue is full and drops it (obs-01).
        self._write_log_sink(file_line)
        # Always marshal log writes to the UI thread.
        self._safe_after(0, self._append_log, ui_line)

    # ---- log sink: thin delegates to self._log_sink (cx-06) --------------
    # Tests / handlers still reach for app._log_* attrs and methods, so the
    # legacy names are kept as properties + one-line delegates.
    @property
    def _log_lock(self):       return self._log_sink._lock
    @property
    def _log_drop_lock(self):  return self._log_sink._drop_lock  # pragma: no cover - withdrawn-root Tk early-exit
    @property
    def _log_queue(self):      return self._log_sink._queue
    @property
    def _log_writer(self):     return self._log_sink._writer
    @property
    def _log_fh(self):         return self._log_sink._fh
    @_log_fh.setter
    def _log_fh(self, v):      self._log_sink._fh = v
    @property
    def _log_fh_path(self):    return self._log_sink._fh_path  # pragma: no cover - withdrawn-root Tk early-exit
    @_log_fh_path.setter
    def _log_fh_path(self, v): self._log_sink._fh_path = v
    @property
    def _log_fh_size(self):    return self._log_sink._fh_size
    @_log_fh_size.setter
    def _log_fh_size(self, v): self._log_sink._fh_size = v  # pragma: no cover - withdrawn-root Tk early-exit
    @property
    def _log_drop_count(self):    return self._log_sink._drop_count
    @_log_drop_count.setter
    def _log_drop_count(self, v): self._log_sink._drop_count = v

    def _write_log_sink(self, line):           self._log_sink.write(line)
    def _flush_log_batch(self, batch):         self._log_sink._flush_batch(batch)
    def _close_log_sink_locked(self):          self._log_sink._close_locked()
    def _open_log_sink_locked(self, path):     self._log_sink._open_locked(path)  # pragma: no cover - withdrawn-root Tk early-exit
    def _rotate_log_sink_locked(self):         self._log_sink._rotate_locked()  # pragma: no cover - withdrawn-root Tk early-exit
    def _drain_log_queue(self, batch):  return self._log_sink._drain_queue(batch)  # pragma: no cover - withdrawn-root Tk early-exit
    def _log_writer_loop(self):                self._log_sink._writer_loop()  # pragma: no cover - withdrawn-root Tk early-exit

    def _append_log(self, line: str) -> None:
        # log_text stays in state=NORMAL permanently (read-only is enforced
        # via key/paste/cut bindings, not state) so selection works for the
        # user. We can insert directly without flipping state.
        self.log_text.insert(tk.END, line)
        # Truncate from the top to keep the log bounded. Without this a
        # long verbose-mode session can grow the widget indefinitely.
        try:
            cap = int(self.config.get("log_max_lines", 100) or 0)
        except (TypeError, ValueError):  # pragma: no cover - TypeError/ValueError parsing cap
            cap = 100  # pragma: no cover - cap defaults to 100
        if cap > 0:  # pragma: no cover - withdrawn-root Tk early-exit
            # Tk Text widgets always behave as if there's an implicit
            # trailing newline, so int(end-1c line) is one MORE than the
            # number of content lines we've inserted. After 6 inserts
            # ending in \n, end-1c reads "7.0" — we have 6 content lines.
            # Subtract 1 to get the real count.
            n_lines = int(self.log_text.index("end-1c").split(".")[0]) - 1
            if n_lines > cap:
                # Delete from line 1 up to (but not including) the first
                # line we want to keep.
                first_kept = n_lines - cap + 1
                self.log_text.delete("1.0", f"{first_kept}.0")
        self.log_text.see(tk.END)

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)

    # -- context menus + keyboard shortcuts ---------------------------------

    SHORTCUTS = [
        ("Ctrl+Enter",  "Add all pasted links"),
        ("Ctrl+P",      "Pause / resume the queue"),
        ("Del",         "Remove selected (queue) / delete protocol"),
        ("Enter",       "Edit selected protocol"),
        ("Ctrl+N",      "New protocol"),
        ("Ctrl+D",      "Duplicate selected protocol"),
        ("Double-click","Edit the row under the cursor (protocols)"),
        ("Right-click", "Context menu (queue / protocols)"),
        ("F1",          "Show this help"),
    ]

    def _build_context_menus(self) -> None:
        # Queue rows: act on selection.
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Re-run", command=self._on_queue_rerun)
        m.add_command(label="Cancel  (Del)", command=self._on_remove_selected)
        m.add_separator()
        m.add_command(label="Copy URL",       command=self._on_queue_copy_url)
        m.add_command(label="Copy command",   command=self._on_queue_copy_cmd)
        self._queue_menu = m
        self.queue_tree.bind("<Button-3>", self._popup_queue_menu)
        self.queue_tree.bind("<Button-2>", self._popup_queue_menu)  # macOS

        # Protocol rows.
        pm = tk.Menu(self.root, tearoff=0)
        pm.add_command(label="Edit…  (Enter)",        command=self._on_edit_protocol)
        pm.add_command(label="Duplicate  (Ctrl+D)",   command=self._on_duplicate_protocol)
        pm.add_command(label="Toggle Shell flag",     command=self._on_toggle_protocol_shell)
        pm.add_separator()
        pm.add_command(label="Delete  (Del)",         command=self._on_delete_protocol)
        self._proto_menu = pm
        self.proto_tree.bind("<Button-3>", self._popup_proto_menu)
        self.proto_tree.bind("<Button-2>", self._popup_proto_menu)  # macOS

    def _popup_queue_menu(self, event) -> None:
        row = self.queue_tree.identify_row(event.y)
        if row and row not in self.queue_tree.selection():
            self.queue_tree.selection_set(row)  # pragma: no cover - selection_set after item appears
        if self.queue_tree.selection():  # pragma: no cover - withdrawn-root Tk early-exit
            try:
                self._queue_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._queue_menu.grab_release()

    def _popup_proto_menu(self, event) -> None:
        row = self.proto_tree.identify_row(event.y)
        if row:  # pragma: no cover - withdrawn-root Tk early-exit
            self.proto_tree.selection_set(row)
        if self.proto_tree.selection():  # pragma: no cover - withdrawn-root Tk early-exit
            try:
                self._proto_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self._proto_menu.grab_release()

    def _bind_shortcuts(self) -> None:
        # Global (root-level) shortcuts.
        self.root.bind_all("<Control-p>",   lambda e: self._on_pause_toggle())
        self.root.bind_all("<Control-P>",   lambda e: self._on_pause_toggle())
        self.root.bind_all("<F1>",          lambda e: self._show_shortcuts_help())
        self.root.bind_all("<Control-n>",   lambda e: self._on_new_protocol())
        self.root.bind_all("<Control-N>",   lambda e: self._on_new_protocol())
        # Ctrl+D is bound globally too (in addition to the proto_tree binding
        # below) because focus on a Treeview can be lost to header clicks or
        # other widgets — we want the shortcut to keep working as long as
        # there's a selected protocol row.
        self.root.bind_all("<Control-d>",   self._global_duplicate)
        self.root.bind_all("<Control-D>",   self._global_duplicate)

        # Tree-local shortcuts. Delete is bound only on the trees themselves
        # so that pressing Delete inside the URL Text widget never accidentally
        # drops queue rows or protocols.
        self.queue_tree.bind("<Delete>",    lambda e: self._on_remove_selected())
        self.queue_tree.bind("<BackSpace>", lambda e: self._on_remove_selected())

        self.proto_tree.bind("<Delete>",    lambda e: self._on_delete_protocol())
        self.proto_tree.bind("<Return>",    lambda e: self._on_edit_protocol())
        self.proto_tree.bind("<Control-d>", lambda e: (self._on_duplicate_protocol(), "break")[1])
        self.proto_tree.bind("<Control-D>", lambda e: (self._on_duplicate_protocol(), "break")[1])

    def _global_duplicate(self, event) -> None:
        """Ctrl+D dispatcher: duplicate the selected protocol if a protocol row
        is selected. Suppressed inside text-input widgets so users can still
        type a literal 'd' character with Ctrl held (rare, but possible)."""
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Text, tk.Entry, ttk.Entry)):
            return  # pragma: no cover - early return: no row selected
        if self.proto_tree.selection():  # pragma: no cover - withdrawn-root Tk early-exit
            self._on_duplicate_protocol()

    def _center_and_grab(self, dlg) -> None:
        """Center a Toplevel over the main window and grab input once the WM
        has actually mapped it (dup-02). Deferring grab_set until after
        wait_visibility avoids Tk's 'grab failed: window not viewable'; the
        after() retry covers WMs where wait_visibility itself raises."""
        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() // 2) - (dlg.winfo_width() // 2)
        y = self.root.winfo_rooty() + (self.root.winfo_height() // 2) - (dlg.winfo_height() // 2)
        dlg.geometry(f"+{x}+{y}")
        try:
            dlg.wait_visibility()
            dlg.grab_set()
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            dlg.after(50, lambda: dlg.grab_set() if dlg.winfo_exists() else None)
        dlg.focus_set()

    def _show_shortcuts_help(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Keyboard shortcuts")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frm, text="Keyboard shortcuts",
            font=("TkDefaultFont", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for r, (key, desc) in enumerate(self.SHORTCUTS, start=1):
            ttk.Label(frm, text=key, font=("TkFixedFont",)).grid(
                row=r, column=0, sticky="w", padx=(0, 16), pady=1
            )
            ttk.Label(frm, text=desc).grid(row=r, column=1, sticky="w", pady=1)
        ttk.Button(frm, text="Close", command=dlg.destroy).grid(
            row=len(self.SHORTCUTS) + 1, column=0, columnspan=2,
            pady=(12, 0), sticky="e",
        )
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._center_and_grab(dlg)

    # -- queue context-menu actions -----------------------------------------

    def _selected_queue_items(self) -> list[QueueItem]:
        """Resolve the currently-selected tree rows back to QueueItem values
        by their stable iid ("r:<idx>" running, "p:<blake2b>" pending — rel-12),
        looked up against the live queue under the lock. Mapping by identity
        rather than row position avoids returning the wrong item if the queue
        shifted since the tree was painted (rel-01)."""
        sel = self.queue_tree.selection()
        if not sel:
            return []
        with self.queue_lock:
            running = {
                f"r:{idx}": it for idx, it in self.current_items.items()
                if it is not None
            }
            pending = {_queue_iid_for_url(it.url): it
                       for it in self.queue_items}
        out = []
        for iid in sel:
            it = running.get(iid) or pending.get(iid)
            if it is not None:  # pragma: no cover - withdrawn-root Tk early-exit
                out.append(it)
        return out

    def _on_queue_rerun(self) -> None:
        items = self._selected_queue_items()
        if not items:
            return
        with self._batch_dispatch():
            for it in items:
                self._process_link(it.url)
        self._log(f"[queue] re-queued {len(items)} item(s)")

    def _on_queue_copy_url(self) -> None:
        items = self._selected_queue_items()
        if not items:
            return
        text = "\n".join(it.url for it in items)
        self._clipboard_set(text)
        self._log(f"[clipboard] copied {len(items)} URL(s)")

    def _on_queue_copy_cmd(self) -> None:
        items = self._selected_queue_items()
        if not items:
            return
        text = "\n".join(self._item_display(it) for it in items)
        self._clipboard_set(text)
        self._log(f"[clipboard] copied {len(items)} command(s)")

    def _clipboard_set(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            # Make sure the clipboard data is held by the X server even after
            # the app exits — without update() many WMs lose the contents.
            self.root.update()
        except tk.TclError as e:
            self._log(f"[clipboard error] {e}")

    # -- protocol context-menu actions --------------------------------------

    def _on_duplicate_protocol(self) -> None:
        sel = self.proto_tree.selection()
        if not sel:
            return
        src_name = sel[0]
        src_cfg = self.config["protocols"].get(src_name)
        if not src_cfg:
            return  # pragma: no cover - early return: empty selection
        # Pick a unique copy-name: "<name>-copy", "<name>-copy-2", ...
        base = f"{src_name}-copy"
        new_name = base
        i = 2
        while new_name in self.config["protocols"]:
            new_name = f"{base}-{i}"
            i += 1
        self.config["protocols"][new_name] = dict(src_cfg)
        self._save_config()
        self._refresh_protocols_tree()
        self.proto_tree.selection_set(new_name)
        self.proto_tree.focus(new_name)
        self._log(f"[config] duplicated '{src_name}' -> '{new_name}'")

    def _on_toggle_protocol_shell(self) -> None:
        sel = self.proto_tree.selection()
        if not sel:
            return
        name = sel[0]
        cfg = self.config["protocols"].get(name)
        if not cfg:
            return  # pragma: no cover - early return: empty selection
        new_val = not bool(cfg.get("shell", False))
        cfg["shell"] = new_val
        self._save_config()
        self._refresh_protocols_tree()
        self.proto_tree.selection_set(name)
        self._log(f"[config] '{name}' shell={new_val}")

    # -- shutdown -----------------------------------------------------------

    def _shutdown(self, timeout: float = 2.0) -> None:
        """Signal workers to exit and wait for them before any Tk teardown.

        The Tk interpreter aborts with "Tcl_AsyncDelete: async handler deleted
        by the wrong thread" if root.destroy() runs while any background thread
        still has pending after() callbacks. Joining every thread we spawned
        (queue workers AND immediate-mode runners) first avoids that.
        """
        # Snapshot pending work to disk BEFORE we set stop_event — workers
        # may still be running and shrinking queue_items right up until we
        # signal them to stop, so this is the closest-to-truth view we get.
        self._safe_save_state_on_shutdown()

        self.stop_event.set()
        self.pause_event.clear()
        # Cancel any pending debounced save (scal-03): we just wrote the
        # latest snapshot above, and the timer must not fire post-teardown.
        self._cancel_save_timer()
        with self._dispatch_cv:
            self._dispatch_cv.notify_all()
        self._cancel_tk_poller()

        deadline = time.time() + timeout
        self._join_threads(self._snapshot_worker_threads(), deadline)
        self._join_threads(self._snapshot_immediate_threads(), deadline)

        # lq-conc-01: the pre-stop snapshot above can miss transitions a worker
        # made between that save and observing stop_event (a claim/completion
        # that mutated queue_items/current_items). Now that every worker has
        # joined, nothing else can touch the queue, so re-save the settled
        # state. This is the authoritative snapshot a restart resumes from.
        self._safe_save_state_on_shutdown()

        # Stop the log writer: it drains any queued lines and closes the file
        # (obs-01/rel-05/cx-06). LogSink.stop() joins, so the file is flushed
        # before we return.
        self._log_sink.stop()

        # Flush any after() callbacks scheduled before stop_event was set.
        try:
            self.root.update()
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            pass

    def _safe_save_state_on_shutdown(self) -> None:
        """Best-effort state save; tolerates a read-only / full FS."""
        try:
            self._save_state()
        except Exception:
            pass

    def _cancel_tk_poller(self) -> None:
        """Cancel the cross-thread UI-jobs poller; further put()s are no-ops
        because workers check stop_event before calling _safe_after."""
        if self._tk_poll_id is None:
            return
        try:
            self.root.after_cancel(self._tk_poll_id)
        except (RuntimeError, tk.TclError):  # pragma: no cover - Tk teardown defensive
            pass
        self._tk_poll_id = None

    def _snapshot_worker_threads(self) -> list:
        with self._workers_lock:
            return [w["thread"] for w in self.workers]

    def _snapshot_immediate_threads(self) -> list:
        with self._immediate_lock:
            return [t for t in self.immediate_threads if t.is_alive()]

    @staticmethod
    def _join_threads(threads: list, deadline: float) -> None:
        """Join each thread, capping the total wall-clock time at `deadline`."""
        for t in threads:
            remaining = max(0.0, deadline - time.time())
            try:
                t.join(timeout=remaining)
            except Exception:
                pass

    def _on_close(self) -> None:
        self._shutdown(timeout=2.0)
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover - Tk teardown defensive
            pass


# ---------------------------------------------------------------------------
# Protocol editor dialog
# ---------------------------------------------------------------------------


class _FormDialog:
    """Shared chrome for the small config-editor dialogs (dup-05): a Toplevel
    with a body frame and a Save/Cancel button strip, then center-and-grab.
    The button strip is packed at the bottom BEFORE the body so the body fills
    the space above it. Subclasses set their own attributes, call
    super().__init__(root, title, center_and_grab), and implement
    _build_body(frame) (which builds the fields) + _on_save()."""

    def __init__(self, root, title, center_and_grab):
        dlg = self.dlg = tk.Toplevel(root)
        dlg.title(title)
        dlg.transient(root)
        dlg.resizable(False, False)
        # NB: grab is deferred to center_and_grab — grabbing a not-yet-viewable
        # Toplevel raises "grab failed: window not viewable".
        bar = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bar, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT, padx=3)
        ttk.Button(bar, text="Save", command=self._on_save).pack(side=tk.RIGHT)
        body = ttk.Frame(dlg, padding=10)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        # _build_body and _on_save are provided by concrete subclasses
        # (ProtocolEditor / MappingEditor); _FormDialog is never instantiated
        # directly.
        self._build_body(body)
        center_and_grab(dlg)


ProtocolEditorHost = namedtuple(
    "ProtocolEditorHost",
    "root protocols install_text_editing center_and_grab save refresh log")


class ProtocolEditor(_FormDialog):
    """New/Edit-protocol dialog (cx-02). Depends on a narrow host contract
    (ProtocolEditorHost) — root window, the protocols dict, and a few
    callables — rather than reaching into the whole LinkQueueApp (arch-04)."""

    def __init__(self, host: "ProtocolEditorHost", proto: "str | None"):
        self.host = host
        self.proto = proto
        self.existing = host.protocols.get(proto, {}) if proto else {}
        super().__init__(host.root,
                         "Edit protocol" if proto else "New protocol",
                         host.center_and_grab)

    def _build_body(self, frm) -> None:
        host = self.host
        ttk.Label(frm, text="Protocol (scheme):").grid(row=0, column=0, sticky="w")
        self.proto_var = tk.StringVar(value=self.proto or "")
        proto_entry = ttk.Entry(frm, textvariable=self.proto_var, width=30)
        proto_entry.grid(row=0, column=1, sticky="we", pady=2)
        if self.proto:
            proto_entry.configure(state="readonly")
        host.install_text_editing(proto_entry, "entry")

        ttk.Label(frm, text="Mode:").grid(row=1, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=self.existing.get("mode", "queue"))
        ttk.Combobox(
            frm, textvariable=self.mode_var,
            values=["queue", "immediate"], state="readonly", width=27,
        ).grid(row=1, column=1, sticky="we", pady=2)

        ttk.Label(frm, text="Shell:").grid(row=2, column=0, sticky="w")
        self.shell_var = tk.BooleanVar(value=bool(self.existing.get("shell", False)))
        shell_row = ttk.Frame(frm)
        shell_row.grid(row=2, column=1, sticky="we", pady=2)
        ttk.Checkbutton(
            shell_row,
            text="Run via /bin/sh -c (enables pipes, redirects, &&, …)",
            variable=self.shell_var,
        ).pack(side=tk.LEFT)

        ttk.Label(frm, text="Command template:").grid(row=3, column=0, sticky="nw")
        self.cmd_text = tk.Text(frm, width=56, height=4)
        self.cmd_text.grid(row=3, column=1, sticky="we", pady=2)
        self.cmd_text.insert("1.0", self.existing.get("command", "echo {url}"))
        host.install_text_editing(self.cmd_text, "text")

        ttk.Label(
            frm,
            text=(
                "Placeholders: {url} (safe as a direct argv), "
                "{url_quoted} (shell-quoted, for use inside shell contexts), "
                "{protocol}"
            ),
            foreground="#666", wraplength=520, justify="left",
        ).grid(row=4, column=1, sticky="w", pady=(0, 6))

        frm.columnconfigure(1, weight=1)
        self.cmd_text.focus_set()

    @staticmethod
    def _validate(command: str, shell: bool) -> bool:
        """True if the template is OK to save. exec mode must shlex-split;
        shell mode with a bare {url} prompts (sec-01) and returns the user's
        choice."""
        probe = command or "echo {url}"
        if not shell:
            try:
                shlex.split(probe)
            except ValueError as e:
                messagebox.showerror(
                    "Invalid template",
                    f"The command cannot be parsed as a shell argv:\n\n{e}\n\n"
                    "Either fix the quoting or enable the Shell option.")
                return False
        elif _template_has_bare_url(probe):
            return messagebox.askyesno(
                "Unsafe shell template",
                "This command runs via the shell and uses {url}, which is "
                "substituted UNQUOTED — a malicious URL could inject shell "
                "commands.\n\nUse {url_quoted} for a shell-safe value.\n\n"
                "Save anyway?")
        return True

    def _on_save(self) -> None:
        host = self.host
        name = self.proto_var.get().strip().lower()
        if not name:
            messagebox.showerror("Error", "Protocol name is required.")
            return
        command = self.cmd_text.get("1.0", tk.END).strip()
        shell = bool(self.shell_var.get())
        if not self._validate(command, shell):
            return
        host.protocols[name] = {
            "mode": self.mode_var.get(),
            "shell": shell,
            "command": command or "echo {url}",
        }
        host.save()
        host.refresh()
        host.log(f"[config] saved protocol '{name}' "
                 f"({self.mode_var.get()}, shell={shell})")
        self.dlg.destroy()


MappingEditorHost = namedtuple(
    "MappingEditorHost",
    "root mappings install_text_editing center_and_grab save refresh log after")


class MappingEditor(_FormDialog):
    """New/Edit prefix→flag mapping dialog (cx-04/arch-06). Same shape as
    ProtocolEditor via the shared _FormDialog chrome; depends on a narrow
    MappingEditorHost rather than the whole app."""

    def __init__(self, host: "MappingEditorHost", prefix: "str | None"):
        self.host = host
        self.prefix = prefix
        super().__init__(host.root,
                         "Edit mapping" if prefix else "New mapping",
                         host.center_and_grab)

    def _build_body(self, frm) -> None:
        host = self.host
        ttk.Label(frm, text="Prefix (e.g. f:):").grid(row=0, column=0, sticky="w")
        self.prefix_var = tk.StringVar(value=self.prefix or "")
        prefix_entry = ttk.Entry(frm, textvariable=self.prefix_var, width=20)
        prefix_entry.grid(row=0, column=1, sticky="we", pady=2)
        if self.prefix:
            prefix_entry.configure(state="readonly")
        host.install_text_editing(prefix_entry, "entry")

        ttk.Label(frm, text="Command flag (e.g. -o):").grid(
            row=1, column=0, sticky="w")
        self.flag_var = tk.StringVar(
            value=host.mappings.get(self.prefix, "") if self.prefix else "")
        flag_entry = ttk.Entry(frm, textvariable=self.flag_var, width=40)
        flag_entry.grid(row=1, column=1, sticky="we", pady=2)
        host.install_text_editing(flag_entry, "entry")

        ttk.Label(
            frm,
            text=('A line token starting with the prefix is removed from the '
                  'URL; the flag + the token\'s value are appended to the '
                  'command. "f:clip.mp4" with flag "-o" -> ... -o clip.mp4'),
            foreground="#666", wraplength=380, justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))

        frm.columnconfigure(1, weight=1)
        flag_entry.focus_set()

    def _on_save(self) -> None:
        host = self.host
        p = self.prefix_var.get().strip()
        flag = self.flag_var.get().strip()
        if not p:
            messagebox.showerror("Error", "Prefix is required.")
            return
        if not flag:
            messagebox.showerror("Error", "Command flag is required.")
            return
        host.mappings[p] = flag
        host.save()
        host.refresh()
        host.after()
        host.log(f"[config] saved mapping '{p}' -> '{flag}'")
        self.dlg.destroy()


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    try:
        style = ttk.Style()
        themes = style.theme_names()
        if sys.platform == "win32" and "vista" in themes:  # pragma: no cover - win32 only
            style.theme_use("vista")
        elif sys.platform == "darwin" and "aqua" in themes:  # pragma: no cover - darwin only
            style.theme_use("aqua")
        elif "clam" in themes:  # pragma: no cover - withdrawn-root Tk early-exit
            style.theme_use("clam")
    except Exception:  # pragma: no cover - defensive against odd Tk themes
        pass
    LinkQueueApp(root)
    root.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
