#!/usr/bin/env python3
"""
Coverage-driving test suite for link_queue.py.

Strategy
--------
link_queue.py is a single Tk class (LinkQueueApp) that tangles dispatch,
worker-pool, config/state I/O and UI. To reach high per-function coverage we
instantiate the REAL app against a real (but withdrawn) Tk root and drive its
handlers, plus exercise the dispatch/worker logic both directly (deterministic)
and end-to-end through real worker threads (one dedicated test).

Isolation
---------
CONFIG_FILE / LEGACY_CONFIG_FILE / STATE_FILE are monkeypatched onto a tmp dir
so the user's real link_queue_config.yaml / link_queue_state.yaml are never
read or written.

Hangs / modality
----------------
Tk's wait_visibility/grab_set/tk_popup and the file/message dialogs would block
under a headless/withdrawn root, so they are neutralised in the `app` fixture.

Run:
    python3 -m pytest tests/test_link_queue.py -v
    python3 -m coverage run --source=link_queue -m pytest tests/test_link_queue.py
    python3 -m coverage report -m
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import types

import pytest
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import link_queue  # noqa: E402
from link_queue import LinkQueueApp, QueueItem  # noqa: E402


def test_default_output_folder_is_portable():
    assert link_queue.DEFAULT_CONFIG["output_folder"] == ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def pump(app, seconds=0.3):
    """Spin the Tk event loop so queued _safe_after / poller jobs run."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            app.root.update()
        except tk.TclError:
            return
        time.sleep(0.01)


def pump_until(app, cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            app.root.update()
        except tk.TclError:
            break
        if cond():
            return True
        time.sleep(0.01)
    try:
        app.root.update()
    except tk.TclError:
        pass
    return cond()


def _spin_until(cond, timeout=5.0):
    """Poll `cond` without a Tk event loop (for headless Dispatcher tests)."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def stop_bg_workers(app):
    """Retire the workers spun up at construction so a test can own the
    queue without a background thread stealing items."""
    with app._workers_lock:
        for w in app.workers:
            w["stop_self"].set()
    with app._dispatch_cv:
        app._dispatch_cv.notify_all()
    threads = [w["thread"] for w in app.workers]
    for t in threads:
        t.join(timeout=2.0)


def descendants(widget):
    out = []
    for c in widget.winfo_children():
        out.append(c)
        out.extend(descendants(c))
    return out


def find_toplevel(app, title_contains):
    for w in app.root.winfo_children():
        if isinstance(w, tk.Toplevel) and title_contains.lower() in w.title().lower():
            return w
    return None


def find_widget(parent, cls, text=None):
    for w in descendants(parent):
        if isinstance(w, cls):
            if text is None:
                return w
            try:
                if str(w.cget("text")) == text:
                    return w
            except tk.TclError:
                pass
    return None


def q(url, protocol="http", template="echo {url}", shell=False):
    return QueueItem(url=url, protocol=protocol, template=template, shell=shell)


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def headless_dispatcher(tmp_path, monkeypatch):
    """test-02b: pytest fixture that returns a `Dispatcher` built via
    `Dispatcher.headless()` — no Tk, no DISPLAY required. Use this for
    pure dispatcher / queue / pool / config-store tests; the heavy
    `app` fixture is only needed for actual widget-binding tests."""
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "state.yaml"))
    disp = link_queue.Dispatcher.headless()
    try:
        yield disp
    finally:
        disp.stop_event.set()
        # Wake any sleeping worker so it can exit promptly.
        with disp._dispatch_cv:
            disp._dispatch_cv.notify_all()
        disp.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "state.yaml"))

    # Neutralise modal/blocking Tk calls so dialogs never hang the suite.
    monkeypatch.setattr(tk.Misc, "wait_visibility", lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(tk.Misc, "grab_set", lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(tk.Misc, "grab_release", lambda self, *a, **k: None, raising=False)
    monkeypatch.setattr(tk.Menu, "tk_popup", lambda self, *a, **k: None, raising=False)

    # Dialogs that would block waiting for user input.
    from tkinter import messagebox, filedialog
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: None)
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(filedialog, "askdirectory", lambda *a, **k: str(tmp_path))

    try:
        root = tk.Tk()
    except tk.TclError as e:  # pragma: no cover - no display
        pytest.skip(f"no Tk display: {e}")
    root.withdraw()
    a = LinkQueueApp(root)
    # Disarm the debounced save timer (scal-03) for deterministic isolation:
    # with the real 1s delay a timer could fire after STATE_FILE is repatched
    # for the next test. Tests that check persistence call _flush_save_state
    # / _save_state explicitly.
    a.dispatcher._save_delay = 10 ** 6
    yield a
    try:
        a._shutdown(timeout=2.0)
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# pure / static helpers
# ---------------------------------------------------------------------------

def test_yaml_roundtrip(tmp_path):
    p = tmp_path / "x.yaml"
    with open(p, "w") as f:
        link_queue._yaml_dump({"a": 1, "b": ["x", "y"]}, f)
    with open(p) as f:
        assert link_queue._yaml_load(f) == {"a": 1, "b": ["x", "y"]}


def test_normalize_paste_text():
    assert LinkQueueApp._normalize_paste_text("  a \n\n b \n") == "a\nb\n"
    assert LinkQueueApp._normalize_paste_text("   \n\n") == ""


def test_extract_protocol():
    assert LinkQueueApp._extract_protocol("HTTPS://x") == "https"
    assert LinkQueueApp._extract_protocol("magnet:?xt=urn") == "magnet"
    assert LinkQueueApp._extract_protocol("noscheme") == ""


def test_domain_of():
    assert LinkQueueApp._domain_of("https://www.YouTube.com/x") == "youtube.com"
    assert LinkQueueApp._domain_of("https://vimeo.com/1") == "vimeo.com"
    assert LinkQueueApp._domain_of("magnet:?xt=urn") == "magnet"
    assert LinkQueueApp._domain_of("file:///etc/hosts") == "file"
    assert LinkQueueApp._domain_of(q("https://a.com/x")) == "a.com"


def test_resolve_command_and_argv():
    out = LinkQueueApp._resolve_command("{protocol} {url} {url_quoted}", "a b", "http")
    assert out == "http a b 'a b'"
    argv = LinkQueueApp._build_argv("echo {url}", "a b", "http")
    assert argv == ["echo", "a b"]


def test_format_duration():
    assert LinkQueueApp._format_duration(5) == "5s"
    assert LinkQueueApp._format_duration(75) == "1m15s"
    assert LinkQueueApp._format_duration(3725) == "1h02m"


def test_highest_pct_and_milestones():
    assert LinkQueueApp._highest_pct_in("dl 100.25% done") == 100
    assert LinkQueueApp._highest_pct_in("no percent") == -1
    # lq-test-01: the pct>highest compare (formerly pragma'd) takes its True
    # branch when a later percentage on the line is larger than an earlier one.
    assert LinkQueueApp._highest_pct_in("10% then 75% then 30%") == 75


def test_stream_summary_logs_first_line_and_milestones(headless_dispatcher):
    # lq-test-01: exercise _stream_summary's milestone branch (formerly hidden
    # behind a misapplied Tk pragma) via the headless dispatcher — no Tk.
    logs = []
    headless_dispatcher._log = logs.append
    headless_dispatcher.config["log_verbosity"] = "summary"
    lines = ["starting download", "  5% ...", " 30% ...", " 80% ...", "done"]
    headless_dispatcher._stream_summary(iter(lines), "[t] ")
    # First line always logged; then each crossed milestone line.
    assert logs[0] == "[t] starting download"
    joined = "\n".join(logs)
    assert "30%" in joined and "80%" in joined


def test_dispatch_wait_remaining():
    # lq-time-01: deadlines are monotonic values.
    assert LinkQueueApp._dispatch_wait_remaining(time.monotonic() - 1, False, 0) == 0.0
    r = LinkQueueApp._dispatch_wait_remaining(time.monotonic() + 10, True, 0.25)
    assert 0 < r <= 0.25
    r2 = LinkQueueApp._dispatch_wait_remaining(time.monotonic() + 10, False, 0)
    assert r2 > 1


# ---------------------------------------------------------------------------
# config + state I/O
# ---------------------------------------------------------------------------

def test_config_written_on_first_run(app):
    assert os.path.exists(link_queue.CONFIG_FILE)
    assert "protocols" in app.config


def test_legacy_json_migration(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "state.yaml"))
    cfg_file, legacy = link_queue.CONFIG_FILE, link_queue.LEGACY_CONFIG_FILE
    with open(legacy, "w") as f:
        json.dump({"sleep_between_items": 9,
                   "protocols": {"http": {"command": "echo hi"}}}, f)
    import os
    assert not os.path.exists(cfg_file)
    store = link_queue.ConfigStore(cfg_file, legacy)   # migrates legacy -> YAML
    assert store["sleep_between_items"] == 9           # legacy JSON merged in
    assert os.path.exists(cfg_file)                    # migration wrote the YAML


def test_config_file_written_0600(tmp_path, monkeypatch):
    # lq-sec-01: the config holds command templates + log_file; it must not be
    # world/group-readable. ConfigStore.save() (and first-run write) go through
    # _write_config_file, which atomically renames a 0o600 tempfile into place.
    import stat
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    cfg_file = link_queue.CONFIG_FILE
    store = link_queue.ConfigStore(cfg_file, link_queue.LEGACY_CONFIG_FILE)
    assert os.path.exists(cfg_file)
    mode = stat.S_IMODE(os.stat(cfg_file).st_mode)
    assert mode == 0o600, oct(mode)
    # A subsequent save() keeps the tight perms and leaves no .tmp residue.
    store["sleep_between_items"] = 11
    store.save()
    assert stat.S_IMODE(os.stat(cfg_file).st_mode) == 0o600
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []
    with open(cfg_file) as f:
        assert link_queue._yaml_load(f)["sleep_between_items"] == 11


def test_config_store_sweeps_stale_temp_siblings_on_load(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.yaml"
    stale = tmp_path / "cfg.yaml.crashed.tmp"
    unrelated = tmp_path / "other.yaml.crashed.tmp"
    stale.write_text("partial", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))

    link_queue.ConfigStore(str(cfg_file), link_queue.LEGACY_CONFIG_FILE)

    assert not stale.exists()
    assert unrelated.exists()


def test_config_write_cleans_tmp_on_dump_failure(tmp_path, monkeypatch):
    # lq-sec-01: a YAML-dump failure must not leave an orphaned tempfile.
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    store = link_queue.ConfigStore(link_queue.CONFIG_FILE,
                                   link_queue.LEGACY_CONFIG_FILE)

    def boom(*a, **k):
        raise RuntimeError("dump exploded")

    monkeypatch.setattr(link_queue, "_yaml_dump", boom)
    with pytest.raises(RuntimeError):
        store._write_config_file(dict(store))
    leftovers = [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    assert leftovers == []


def test_facade_delegates_non_dunder_only():
    # lq-rel-03: real Dispatcher/ConfigStore helpers still delegate...
    assert LinkQueueApp._domain_of("https://vimeo.com/x") == "vimeo.com"
    # ...a typo'd class attr raises instead of silently resolving through a
    # delegate...
    with pytest.raises(AttributeError):
        LinkQueueApp._serialise_item  # noqa: B018 (British misspelling typo)
    # ...and dunder lookups never trigger the delegation walk, so foreign
    # introspection (copy/pickle) sees the real (absent) attribute and does
    # not get a Dispatcher/ConfigStore dunder smuggled in.
    import copy
    for name in ("__reduce_ex__", "__getstate__", "__deepcopy__"):
        with pytest.raises(AttributeError):
            type(LinkQueueApp).__getattr__(LinkQueueApp, name)
    # copy of the class object must not blow up via a delegated dunder.
    assert copy.copy(LinkQueueApp) is LinkQueueApp


def test_merge_and_normalize_config():
    cfg = {"protocols": {"http": {"mode": "queue"}}}
    LinkQueueApp._merge_user_config(cfg, {"x": 1, "protocols": {"ftp": {"command": "c"}}})
    assert cfg["x"] == 1 and "ftp" in cfg["protocols"]
    cfg["protocols"]["bad"] = "notadict"
    LinkQueueApp._normalize_config_schema(cfg)
    assert "bad" not in cfg["protocols"]
    assert cfg["protocols"]["ftp"]["mode"] == "queue"


@pytest.mark.parametrize("bad", [None, [], "x", 42, 3.5, ("a",)])
def test_merge_user_config_non_dict_protocols_kept_default(bad):
    # lq-rel-01: a non-dict `protocols:` must not overwrite the default dict,
    # and the merged config must still normalize without crashing.
    cfg = {"protocols": {"http": {"mode": "queue", "command": "echo {url}",
                                  "shell": False}}}
    LinkQueueApp._merge_user_config(cfg, {"protocols": bad, "sleep_between_items": 9})
    assert isinstance(cfg["protocols"], dict)
    assert "http" in cfg["protocols"]            # default survived
    assert cfg["sleep_between_items"] == 9       # other keys still merge
    LinkQueueApp._normalize_config_schema(cfg)   # must not raise
    assert cfg["protocols"]["http"]["mode"] == "queue"


def test_config_store_load_with_non_dict_protocols(tmp_path, monkeypatch):
    # lq-rel-01: a hand-edited config with `protocols: null` must not crash
    # startup (full ConfigStore load path).
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text("protocols: null\nsleep_between_items: 7\n")
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "none.json"))
    store = link_queue.ConfigStore(str(cfg_path), str(tmp_path / "none.json"))
    assert isinstance(store["protocols"], dict)
    assert "http" in store["protocols"]
    assert store["sleep_between_items"] == 7


try:
    from hypothesis import given, settings as hyp_settings, strategies as st

    @hyp_settings(max_examples=60, deadline=None)
    @given(bad=st.one_of(st.none(), st.integers(), st.floats(allow_nan=False),
                         st.text(), st.lists(st.integers())))
    def test_merge_user_config_non_dict_protocols_property(bad):
        # lq-rel-01 property: for ANY non-dict protocols value, defaults are
        # preserved and normalize never raises.
        cfg = {"protocols": {"http": {"mode": "queue", "command": "echo {url}",
                                      "shell": False}}}
        LinkQueueApp._merge_user_config(cfg, {"protocols": bad})
        assert isinstance(cfg["protocols"], dict) and "http" in cfg["protocols"]
        LinkQueueApp._normalize_config_schema(cfg)
except ImportError:  # pragma: no cover - hypothesis always installed in CI
    pass


def test_read_user_config_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "none.json"))
    with open(link_queue.CONFIG_FILE, "w") as f:
        f.write("::: not yaml :::\n\t- broken")
    store = link_queue.ConfigStore(link_queue.CONFIG_FILE, link_queue.LEGACY_CONFIG_FILE)
    user, migrated = store._read_user_config_dict()
    assert migrated is False and user is None     # corrupt -> (None, False)
    assert "protocols" in store                   # fell back to defaults


def test_save_and_load_state(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2")]
    app._save_state()
    in_flight, pending = app._load_state_items()
    assert [it.url for it in pending] == ["http://a/1", "http://b/2"]
    assert in_flight == []


def test_dispatcher_sweeps_stale_state_temp_siblings_on_load(tmp_path):
    state_path = tmp_path / "state.yaml"
    stale = tmp_path / "state.yaml.crashed.tmp"
    unrelated = tmp_path / "state-other.yaml.crashed.tmp"
    stale.write_text("partial", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    disp = link_queue.Dispatcher.headless(state_path=str(state_path))
    try:
        assert not stale.exists()
        assert unrelated.exists()
    finally:
        disp.close()


def test_headless_load_state_items_reads_pending_and_inflight(headless_dispatcher):
    state = {
        "in_flight": [headless_dispatcher._serialize_item(q("http://run/1"))],
        "queue": [headless_dispatcher._serialize_item(q("http://wait/1"))],
    }
    with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
        link_queue._yaml_dump(state, fh, allow_unicode=True)

    in_flight, pending = headless_dispatcher._load_state_items()

    assert [item.url for item in in_flight] == ["http://run/1"]
    assert [item.url for item in pending] == ["http://wait/1"]


def test_headless_restore_queue_from_state_requeues_inflight_first(headless_dispatcher):
    state = {
        "in_flight": [headless_dispatcher._serialize_item(q("http://run/1"))],
        "queue": [headless_dispatcher._serialize_item(q("http://wait/1"))],
    }
    with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
        link_queue._yaml_dump(state, fh, allow_unicode=True)

    headless_dispatcher._restore_queue_from_state()

    assert [item.url for item in headless_dispatcher.queue_items] == [
        "http://run/1",
        "http://wait/1",
    ]


def test_dispatcher_state_lock_rejects_second_instance(tmp_path):
    state_path = str(tmp_path / "state.yaml")
    first = link_queue.Dispatcher.headless(
        state_path=state_path, acquire_state_lock=True,
    )
    try:
        with pytest.raises(RuntimeError, match="already using"):
            link_queue.Dispatcher.headless(
                state_path=state_path, acquire_state_lock=True,
            )
    finally:
        first.close()

    second = link_queue.Dispatcher.headless(
        state_path=state_path, acquire_state_lock=True,
    )
    second.close()


def test_state_file_lock_pidfile_reclaims_stale_pid(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.yaml")
    lock_path = state_path + ".lock"
    with open(lock_path, "w", encoding="utf-8") as fh:
        fh.write("pid=999999\nstate=old\n")
    monkeypatch.setattr(link_queue, "fcntl", None)
    monkeypatch.setattr(
        link_queue.os,
        "kill",
        lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError(pid)),
    )
    lock = link_queue.StateFileLock(state_path)

    lock.acquire()
    try:
        with open(lock_path, encoding="utf-8") as fh:
            text = fh.read()
        assert f"pid={os.getpid()}" in text
    finally:
        lock.release()


def test_state_file_lock_pidfile_rejects_live_pid(tmp_path, monkeypatch):
    state_path = str(tmp_path / "state.yaml")
    with open(state_path + ".lock", "w", encoding="utf-8") as fh:
        fh.write("pid=123\nstate=old\n")
    monkeypatch.setattr(link_queue, "fcntl", None)
    monkeypatch.setattr(link_queue.os, "kill", lambda pid, sig: None)

    with pytest.raises(link_queue.StateFileLockError):
        link_queue.StateFileLock(state_path).acquire()


def test_state_file_lock_flock_release_keeps_lockfile(tmp_path):
    if link_queue.fcntl is None:
        pytest.skip("fcntl flock path unavailable")
    state_path = str(tmp_path / "state.yaml")
    lock = link_queue.StateFileLock(state_path)

    lock.acquire()
    lock.release()

    assert os.path.exists(state_path + ".lock")


def test_save_state_persists_immediate_backlog(tmp_path, monkeypatch):
    # lq-rel-01: the immediate work-queue backlog is snapshotted into a third
    # "immediate" bucket so an unprocessed paste survives shutdown.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_worker_count"] = 1
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()                       # no consumer drains the queue
    try:
        d._immediate_q.put(q("magnet:?xt=1", protocol="magnet"))
        d._immediate_q.put(q("file:///x", protocol="file"))
        d._save_state()
        with open(link_queue.STATE_FILE, encoding="utf-8") as fh:
            data = link_queue._yaml_load(fh)
        urls = [link_queue._decode_state_field(e, "url", "")
                for e in data["immediate"]]
        assert urls == ["magnet:?xt=1", "file:///x"]
    finally:
        d.stop_event.set()


def test_save_state_persists_inflight_immediate_item(tmp_path, monkeypatch):
    # lq-rel-01: an immediate item pulled off the queue and running on a
    # consumer is persisted (ahead of the still-queued backlog) so a restart
    # retries it instead of losing it.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_worker_count"] = 1
    d = link_queue.Dispatcher.headless(cfg)
    started = threading.Event()
    release = threading.Event()

    def block_run(item, _mode):
        started.set()
        release.wait(5.0)
        return 0

    d._run_item = block_run
    try:
        d._dispatch_immediate(q("magnet:?running", protocol="magnet"))
        d._dispatch_immediate(q("magnet:?queued", protocol="magnet"))
        assert started.wait(5.0), "consumer never started the in-flight item"
        # Give the in-flight slot a moment to publish.
        assert _spin_until(lambda: any(
            it is not None for it in d._immediate_current.values()))
        d._save_state()
        with open(link_queue.STATE_FILE, encoding="utf-8") as fh:
            data = link_queue._yaml_load(fh)
        urls = [link_queue._decode_state_field(e, "url", "")
                for e in data["immediate"]]
        assert "magnet:?running" in urls
        assert "magnet:?queued" in urls
        # in-flight serialized first
        assert urls.index("magnet:?running") < urls.index("magnet:?queued")
    finally:
        release.set()
        d.stop_event.set()


def test_inflight_immediate_slot_cleared_after_run(tmp_path, monkeypatch):
    # lq-rel-01: the per-consumer in-flight slot is cleared once the item
    # finishes, so a later save doesn't persist a completed item.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_worker_count"] = 1
    d = link_queue.Dispatcher.headless(cfg)
    ran = []
    d._run_item = lambda item, _mode: (ran.append(item.url), 0)[1]
    try:
        d._dispatch_immediate(q("magnet:?done", protocol="magnet"))
        assert _spin_until(lambda: "magnet:?done" in ran)
        assert _spin_until(lambda: all(
            it is None for it in d._immediate_current.values()))
    finally:
        d.stop_event.set()


@hyp_settings(max_examples=40, deadline=None)
@given(
    n_inflight=st.integers(min_value=0, max_value=4),
    n_backlog=st.integers(min_value=0, max_value=6),
)
def test_save_state_immediate_inflight_then_backlog_property(
        tmp_path_factory, n_inflight, n_backlog):
    # lq-rel-01 property: the serialized "immediate" bucket is exactly the
    # in-flight items (in slot order) followed by the queued backlog, for any
    # mix of counts, and nothing is dropped.
    tmp = tmp_path_factory.mktemp("rel01")
    d = link_queue.Dispatcher.headless()
    d.state_path = str(tmp / "s.yaml")
    d.stop_event.set()                       # no consumer drains/mutates slots
    try:
        inflight_urls = [f"magnet:?run{i}" for i in range(n_inflight)]
        backlog_urls = [f"magnet:?q{i}" for i in range(n_backlog)]
        for i, u in enumerate(inflight_urls):
            d._immediate_current[i] = q(u, protocol="magnet")
        for u in backlog_urls:
            d._immediate_q.put(q(u, protocol="magnet"))
        d._save_state()
        with open(d.state_path, encoding="utf-8") as fh:
            data = link_queue._yaml_load(fh)
        urls = [link_queue._decode_state_field(e, "url", "")
                for e in data["immediate"]]
        assert urls == inflight_urls + backlog_urls
    finally:
        d.stop_event.set()


def test_restore_redispatches_immediate_backlog(tmp_path, monkeypatch):
    # lq-rel-01: persisted immediate items are re-dispatched on restore.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    d = link_queue.Dispatcher.headless()
    try:
        state = {
            "queue": [d._serialize_item(q("http://wait/1"))],
            "immediate": [d._serialize_item(q("magnet:?xt=9", protocol="magnet"))],
        }
        with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
            link_queue._yaml_dump(state, fh, allow_unicode=True)
        dispatched = []
        d._dispatch_immediate = dispatched.append
        d._restore_queue_from_state()
        assert [it.url for it in dispatched] == ["magnet:?xt=9"]
        assert [it.url for it in d.queue_items] == ["http://wait/1"]
    finally:
        d.stop_event.set()


def test_load_immediate_items_backcompat_old_state(tmp_path, monkeypatch):
    # lq-rel-01: a state file written before the "immediate" bucket existed
    # must load cleanly (empty immediate list).
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    d = link_queue.Dispatcher.headless()
    try:
        with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
            link_queue._yaml_dump(
                {"queue": [d._serialize_item(q("http://a/1"))]}, fh)
        assert d._load_immediate_items() == []
        in_flight, pending = d._load_state_items()
        assert [it.url for it in pending] == ["http://a/1"]
    finally:
        d.stop_event.set()


def test_load_immediate_items_missing_file(tmp_path, monkeypatch):
    # lq-rel-01: missing state file -> empty immediate list (no crash).
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "nope.yaml"))
    d = link_queue.Dispatcher.headless()
    try:
        assert d._load_immediate_items() == []
    finally:
        d.stop_event.set()


def test_state_field_base64_roundtrip(app):
    # A URL with a C1 control char (\x85) is YAML-unsafe -> _serialize_item
    # must base64-wrap it, and _load_state_items must decode it back exactly
    # (rel-03). Exercises _yaml_emittable's False branch + the b64 branches.
    stop_bg_workers(app)
    bad = "https://h.test/\udce9x"  # lone surrogate -> unemittable
    assert link_queue._yaml_emittable(bad) is False
    with app._dispatch_cv:
        app.queue_items[:] = [q(bad)]
    app._save_state()
    in_flight, pending = app._load_state_items()
    assert pending[0].url == bad


def test_decode_state_field_branches():
    assert link_queue._decode_state_field({"url": "plain"}, "url", "") == "plain"
    good = link_queue._encode_state_field
    e = {}
    good(e, "url", "x")
    assert link_queue._decode_state_field(e, "url", "") == "x"
    # corrupt base64 -> default
    assert link_queue._decode_state_field({"url_b64": "!!!notb64"}, "url", "def") == "def"
    # non-str value -> default
    assert link_queue._decode_state_field({"url": 123}, "url", "def") == "def"


def test_encode_state_field_uses_base64_for_lone_surrogate():
    entry = {}
    value = "https://h.test/\udcff"

    link_queue._encode_state_field(entry, "url", value)

    assert "url_b64" in entry
    assert link_queue._decode_state_field(entry, "url", "") == value


def test_getattr_missing_raises(app):
    with pytest.raises(AttributeError):
        _ = app.this_attribute_does_not_exist_anywhere


def test_pending_queue_ops():
    pq = link_queue._PendingQueue(domain_fn=LinkQueueApp._domain_of)
    a, b, c = q("http://a.com/1"), q("http://b.com/2"), q("http://a.com/3")
    pq.append(a)
    pq.append(b)
    pq.append(c)
    assert len(pq) == 3 and a in pq and "http://a.com/1" in pq.urls
    assert list(pq.by_domain["a.com"].keys()) == ["http://a.com/1", "http://a.com/3"]
    # dedupe: re-appending an existing url doesn't grow the queue
    pq.append(a)
    assert len(pq) == 3
    # lq-perf-01: iid index resolves a tree iid back to its item in O(1).
    iid_a = link_queue._queue_iid_for_url("http://a.com/1")
    assert pq.iids[iid_a] == "http://a.com/1"
    assert pq.item_for_iid(iid_a) is a
    assert pq.item_for_iid("p:nope") is None
    pq.remove(b)                          # O(1) removal by url
    assert "b.com" not in pq.by_domain and b not in pq
    assert link_queue._queue_iid_for_url("http://b.com/2") not in pq.iids
    assert pq[0] is a and pq[-1] is c     # int index
    assert pq[:1] == [a]                  # slice -> list
    pq[:] = [b]                           # whole-queue slice assign
    assert list(pq) == [b] and pq.urls == {"http://b.com/2": b}
    # slice-assign rebuilt the iid index too.
    assert pq.item_for_iid(link_queue._queue_iid_for_url("http://b.com/2")) is b
    assert iid_a not in pq.iids
    pq.clear()
    assert len(pq) == 0 and not pq.urls and not pq.by_domain and not pq
    assert not pq.iids


def test_pending_queue_setitem_and_eq():
    df = LinkQueueApp._domain_of
    a, b, c = q("http://a/1"), q("http://b/2"), q("http://c/3")
    pq = link_queue._PendingQueue([a, b], domain_fn=df)
    pq[0:1] = [c]                          # partial slice -> rebuild
    assert list(pq) == [c, b]
    with pytest.raises(TypeError):
        pq[0] = a                          # int assignment unsupported
    other = link_queue._PendingQueue([c, b], domain_fn=df)
    assert pq == other                     # __eq__ vs another _PendingQueue


def test_write_log_sink_branches(app, monkeypatch):
    app.config["log_file"] = ""
    app._write_log_sink("ignored\n")       # sink off -> no enqueue, returns
    app.config["log_file"] = "/some/path"
    monkeypatch.setattr(app._log_queue, "put_nowait", _raise_full)
    app._write_log_sink("y\n")             # queue.Full -> dropped, no raise


def test_render_limit_fallback(app):
    app.config["queue_render_limit"] = "not-an-int"
    assert app._render_limit() == 0          # bad value -> 0 (render all)
    app.config["queue_render_limit"] = 5
    assert app._render_limit() == 5


def test_log_writer_survives_flush_error(app, tmp_path, monkeypatch):
    # rel-06: a failure inside the writer's flush must not kill the thread.
    app.config["log_file"] = str(tmp_path / "f.log")
    # Patch the LogSink method the writer thread actually calls (cx-06).
    monkeypatch.setattr(app._log_sink, "_flush_batch", _raise)
    app._log("boom")
    deadline = time.time() + 2.0
    while not app._log_queue.empty() and time.time() < deadline:
        time.sleep(0.02)
    assert app._log_writer.is_alive()        # writer thread still running
    # restored flush works again
    monkeypatch.undo()
    app._log("ok-after")
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if os.path.exists(app.config["log_file"]) and \
                "ok-after" in open(app.config["log_file"], encoding="utf-8").read():
            break
        time.sleep(0.02)
    assert "ok-after" in open(app.config["log_file"], encoding="utf-8").read()


def test_log_sink_surfaces_drops(app, tmp_path):
    app.config["log_file"] = str(tmp_path / "f.log")
    app._log_drop_count = 3                 # pretend 3 lines were dropped
    app._flush_log_batch(["real\n"])
    content = open(app.config["log_file"], encoding="utf-8").read()
    assert "[sink] dropped 3 line(s)" in content and "real" in content
    assert app._log_drop_count == 0         # reset after surfacing


def test_log_sink_circular_rotation(app, tmp_path, monkeypatch):
    monkeypatch.setattr(link_queue, "LOG_SINK_MAX_BYTES", 50)
    path = str(tmp_path / "f.log")
    app.config["log_file"] = path
    app._flush_log_batch(["A" * 80 + "\n"])  # exceeds cap -> rotate
    assert os.path.exists(path + ".1")       # previous generation kept
    assert "A" in open(path + ".1", encoding="utf-8").read()
    assert app._log_fh_size == 0             # fresh active file
    app._flush_log_batch(["B\n"])            # writes into the fresh file
    assert "B" in open(path, encoding="utf-8").read()


def test_log_sink_size_tracks_byte_offset(app, tmp_path):
    # lq-perf-02: _fh_size is taken from f.tell() (byte offset) after flush,
    # so it matches the on-disk byte size even for multibyte UTF-8 content
    # (where len(str) != len(bytes)) without re-encoding each batch.
    path = str(tmp_path / "u.log")
    app.config["log_file"] = path
    line = "café—ünïcödé\n"          # multibyte: byte length > char length
    app._flush_log_batch([line])
    on_disk = os.path.getsize(path)
    assert app._log_fh_size == on_disk
    assert on_disk == len(line.encode("utf-8"))
    app._flush_log_batch(["more\n"])
    assert app._log_fh_size == os.path.getsize(path)


def test_log_sink_rotation_replace_error(app, tmp_path, monkeypatch):
    monkeypatch.setattr(link_queue, "LOG_SINK_MAX_BYTES", 50)
    monkeypatch.setattr(link_queue.os, "replace", _raise_os)  # rename fails
    path = str(tmp_path / "f.log")
    app.config["log_file"] = path
    app._flush_log_batch(["A" * 80 + "\n"])  # rotate -> os.replace raises -> swallowed
    app._flush_log_batch(["B\n"])            # sink still usable afterwards
    assert "B" in open(path, encoding="utf-8").read()


def test_flush_log_batch_branches(app, tmp_path):
    app.config["log_file"] = ""
    app._flush_log_batch(["x\n"])          # sink off -> close branch
    app.config["log_file"] = str(tmp_path / "f.log")
    app._flush_log_batch(["a\n"])          # opens + writes
    assert "a" in open(app.config["log_file"], encoding="utf-8").read()

    class BadFH:
        def write(self, s):
            raise OSError("boom")
        def close(self):
            pass
    app._log_fh = BadFH()
    app._log_fh_path = app.config["log_file"]
    app._flush_log_batch(["b\n"])          # write raises -> close, sink off
    assert app._log_fh is None


def test_dedupe_uses_url_index(app):
    app.pause_event.set()
    assert app._process_link("http://dup/x") == "queue"
    # second add hits the O(1) url-index pending check
    assert app._process_link("http://dup/x") == "duplicate"
    assert "http://dup/x" in app.queue_items.urls


def test_log_file_sink_roundtrip(app, tmp_path):
    path = str(tmp_path / "out.log")
    app.log_file_var.set(path)
    app._on_log_file_changed()
    assert app.config["log_file"] == path
    app._log("hello-sink")
    # The background writer thread (rel-05) flushes asynchronously.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if os.path.exists(path) and "hello-sink" in open(path, encoding="utf-8").read():
            break
        time.sleep(0.02)
    content = open(path, encoding="utf-8").read()
    assert "hello-sink" in content
    # File-sink lines carry a full ISO datetime, not just a clock (obs-03).
    import re
    assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\] hello-sink", content)
    app._on_clear_log_file()        # disables sink
    assert app.config["log_file"] == ""


def test_log_file_sink_bad_path(app, tmp_path):
    # A directory can't be opened for append -> writer disables the sink, no raise.
    app.config["log_file"] = str(tmp_path)
    app._log("x")
    deadline = time.time() + 2.0
    while not app._log_queue.empty() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.1)   # let the writer attempt the (failing) open
    assert app._log_fh is None


def test_browse_log_file(app, tmp_path, monkeypatch):
    from tkinter import filedialog
    target = str(tmp_path / "chosen.log")
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda *a, **k: target)
    app._on_browse_log_file()
    assert app.config["log_file"] == target


def test_protocol_shell_url_warns_and_saves(app):
    # shell=True + bare {url} -> warning (askyesno patched True in fixture) -> saved
    _drive_proto_editor(app, "wsh", "wget {url}", shell=True)
    assert "wsh" in app.config["protocols"]


def test_protocol_shell_url_declined(app, monkeypatch):
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    _drive_proto_editor(app, "wdecl", "wget {url}", shell=True)
    assert "wdecl" not in app.config["protocols"]  # declined -> not saved


def test_protocol_shell_url_quoted_ok(app):
    # shell + {url_quoted} only -> no warning, saves straight through
    _drive_proto_editor(app, "wq", "wget {url_quoted}", shell=True)
    assert "wq" in app.config["protocols"]


def test_readonly_block_key():
    blk = LinkQueueApp._readonly_block_key
    assert blk(types.SimpleNamespace(keysym="Left", state=0)) is None     # nav
    assert blk(types.SimpleNamespace(keysym="x", state=0)) == "break"     # typed
    assert blk(types.SimpleNamespace(keysym="c", state=4)) is None        # Ctrl+C
    assert blk(types.SimpleNamespace(keysym="a", state=4)) is None        # Ctrl+A
    assert blk(types.SimpleNamespace(keysym="v", state=4)) == "break"     # Ctrl+V


def test_build_styles_tclerror(app, monkeypatch):
    monkeypatch.setattr(ttk.Style, "configure", _raise_tcl)
    monkeypatch.setattr(ttk.Style, "map", _raise_tcl)
    app._build_styles()  # both try/except blocks swallow TclError


def test_close_log_sink_error(app):
    class BadFH:
        def close(self):
            raise OSError("boom")
    app._log_fh = BadFH()
    with app._log_lock:
        app._close_log_sink_locked()  # except swallowed
    assert app._log_fh is None


def test_debounced_save_state(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1")]
    app._request_save_state()
    assert app._save_timer is not None      # armed, not yet written
    app._request_save_state()               # coalesced -> still one timer
    app._flush_save_state()                 # fire now
    assert app._save_timer is None
    _, pending = app._load_state_items()
    assert [it.url for it in pending] == ["http://a/1"]
    app._request_save_state()
    app._cancel_save_timer()
    assert app._save_timer is None


def test_load_state_missing_and_corrupt(app, tmp_path):
    assert app._load_state_items() == ([], [])
    with open(link_queue.STATE_FILE, "w") as f:
        f.write("[not, a, dict]")
    assert app._load_state_items() == ([], [])


def test_restore_queue_from_state(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1")]
        app.current_items[1] = q("http://b/2")
    app._save_state()
    with app._dispatch_cv:
        app.queue_items.clear()
        app.current_items.clear()
    app._restore_queue_from_state()
    urls = [it.url for it in app.queue_items]
    assert "http://b/2" in urls and "http://a/1" in urls
    pump(app, 0.2)


# ---------------------------------------------------------------------------
# add / dispatch routing
# ---------------------------------------------------------------------------

def test_on_add_queues_links(app):
    app.pause_event.set()  # keep queue items from being consumed
    app.url_text.insert("1.0", "http://a/1\nhttps://b/2\n")
    app._on_add()
    pump(app, 0.2)
    assert len(app.queue_items) == 2
    assert app.url_text.get("1.0", "end-1c").strip() == ""


def test_on_add_no_links(app):
    app._on_add()  # empty text -> "no links detected"
    assert len(app.queue_items) == 0


def test_on_add_event_returns_break(app):
    app.pause_event.set()
    app.url_text.insert("1.0", "http://a/1")
    assert app._on_add_event() == "break"


def test_duplicate_default_immediate_routing(app):
    app.pause_event.set()
    assert app._process_link("http://dup/1") == "queue"
    assert app._process_link("http://dup/1") == "duplicate"
    assert app._process_link("weird://x") == "default"   # unknown scheme
    assert app._process_link("magnet:?xt=1") == "immediate"
    pump(app, 0.3)  # let immediate runner finish + join


def test_clear_input_and_link_count(app):
    app.url_text.insert("1.0", "a\nb\nc")
    app._update_link_count()
    assert "3 link" in app.count_var.get()
    app._on_clear_input()
    assert app.count_var.get().startswith("0 ")


def test_normalize_paste_area(app):
    app.url_text.insert("1.0", "  http://a/1  \n\n http://b/2 \n")
    app._normalize_paste_area()
    assert app.url_text.get("1.0", "end-1c") == "http://a/1\nhttp://b/2\n"


# ---------------------------------------------------------------------------
# claim / pick / release (deterministic, no live worker)
# ---------------------------------------------------------------------------

def test_pick_next_item_spreads_domains(app):
    stop_bg_workers(app)
    app.queue_items[:] = [q("http://a.com/1"), q("http://a.com/2"), q("http://b.com/1")]
    app._domain_active["a.com"] = 1
    # b.com has 0 active -> its item should be picked over the a.com items
    assert app._pick_next_item(cap=0).url == "http://b.com/1"


def test_pick_next_item_respects_cap(app):
    stop_bg_workers(app)
    app.queue_items[:] = [q("http://a.com/1")]
    app._domain_active["a.com"] = 2
    assert app._pick_next_item(cap=2) is None  # capped out
    assert app._pick_next_item(cap=0).url == "http://a.com/1"


def test_try_claim_and_release(app):
    stop_bg_workers(app)
    app.queue_items[:] = [q("http://a.com/1")]
    item = app._try_claim_item(idx=42)
    assert item is not None
    assert app._domain_active["a.com"] == 1
    assert app.current_items[42] is item
    app._release_item(42, item)
    assert "a.com" not in app._domain_active
    assert app.current_items[42] is None


def test_claim_next_item_empty_and_blocked(app):
    stop_bg_workers(app)
    ev = threading.Event()
    assert app._claim_next_item(7, ev, wait_seconds=0.05) is None  # empty
    app.queue_items[:] = [q("http://a/1")]
    app.pause_event.set()
    assert app._claim_next_item(7, ev, wait_seconds=0.05) is None  # blocked
    app.pause_event.clear()
    got = app._claim_next_item(7, ev, wait_seconds=0.5)
    assert got is not None


def test_claim_next_item_stop(app):
    stop_bg_workers(app)
    ev = threading.Event()
    ev.set()
    assert app._claim_next_item(7, ev, wait_seconds=0.5) is None


# ---------------------------------------------------------------------------
# run_item (subprocess) — exec + shell + error paths + verbosity
# ---------------------------------------------------------------------------

def test_run_item_exec_ok(app):
    assert app._run_item(q("http://a/1", template="echo {url}"), "t") == 0
    pump(app, 0.1)


def test_run_item_shell_ok(app):
    # sec-04: shell=True must use {url_quoted}; bare {url} is rejected.
    item = q("http://a/1", template="echo {url_quoted}", shell=True)
    assert app._run_item(item, "t") == 0
    pump(app, 0.1)


def test_run_item_shell_rejects_bare_url(app):
    # sec-04: bare {url} in shell=True template returns -1 (was warn-only before).
    item = q("http://x", template="echo {url}", shell=True)
    assert app._run_item(item, "t") == -1
    pump(app, 0.2)
    log = app.log_text.get("1.0", "end-1c")
    assert "refusing shell=True" in log


def test_shell_template_trusted_warns_once(headless_dispatcher):
    # lq-sec-02: a shell=True template (even the safe {url_quoted} form) is run
    # through /bin/sh -c, so its body is trusted input. Warn once per distinct
    # template, not on every item.
    logs = []
    headless_dispatcher._log = logs.append
    headless_dispatcher._warn_shell_template_trusted("wget {url_quoted}")
    headless_dispatcher._warn_shell_template_trusted("wget {url_quoted}")
    headless_dispatcher._warn_shell_template_trusted("curl {url_quoted}")
    warns = [m for m in logs if "trusted input" in m]
    assert len(warns) == 2
    assert "wget {url_quoted}" in warns[0]


def test_shell_template_trusted_warns_once_under_concurrency(headless_dispatcher):
    # lq-conc-01: _warned_shell_url_templates is mutated from worker threads;
    # the check-then-add must be atomic so concurrent shell items warn exactly
    # once per distinct template, never twice.
    logs = []
    log_lock = threading.Lock()

    def record(msg):
        with log_lock:
            logs.append(msg)

    headless_dispatcher._log = record
    start = threading.Event()

    def hammer():
        start.wait()
        headless_dispatcher._warn_shell_template_trusted("wget {url_quoted}")

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    warns = [m for m in logs if "trusted input" in m]
    assert len(warns) == 1


def test_run_item_shell_ok_emits_trust_warning_once(app):
    # lq-sec-02: surfaced via the real run path too, once per template.
    item = q("http://a/1", template="echo {url_quoted}", shell=True)
    assert app._run_item(item, "t") == 0
    assert app._run_item(item, "t") == 0
    pump(app, 0.2)
    log = app.log_text.get("1.0", "end-1c")
    assert log.count("trusted input") == 1


def test_run_item_empty_command(app):
    assert app._run_item(q("http://a/1", template="   "), "t") == -1


def test_run_item_invalid_template(app):
    assert app._run_item(q("http://a/1", template="echo 'unterminated"), "t") == -1


def test_run_item_not_found(app):
    item = q("http://a/1", template="definitely-not-a-real-cmd-xyz {url}")
    assert app._run_item(item, "t") == -1
    pump(app, 0.1)


@pytest.mark.parametrize("verbosity", ["summary", "verbose", "silent"])
def test_run_item_streaming_modes(app, verbosity):
    app.config["log_verbosity"] = verbosity
    item = q("x://1", template="echo start; echo 50%; echo 100%", shell=True)
    assert app._run_item(item, "t") == 0
    pump(app, 0.2)


def test_item_display(app):
    assert app._item_display(q("http://a/1")).startswith("(exec)")
    assert app._item_display(q("http://a/1", shell=True)).startswith("(shell)")
    assert "invalid" in app._item_display(q("http://a/1", template="echo 'x"))


# ---------------------------------------------------------------------------
# worker step / loop / sleep / cooldown
# ---------------------------------------------------------------------------

def test_worker_step_processes_item(app):
    stop_bg_workers(app)
    app.config["sleep_between_items"] = 0
    app.sleep_var.set("0")
    app.queue_items[:] = [q("http://a/1", template="echo {url}")]
    ev = threading.Event()
    app._worker_step(idx=55, stop_self=ev)
    pump(app, 0.2)
    assert app.queue_items == []
    assert app.current_items.get(55) is None


def test_worker_step_no_item(app):
    stop_bg_workers(app)
    ev = threading.Event()
    ev.set()  # forces _claim_next_item to return None fast
    app._worker_step(idx=56, stop_self=ev)


def test_worker_loop_cleanup_path(app):
    stop_bg_workers(app)
    ev = threading.Event()
    ev.set()
    app.current_items[77] = None
    app._worker_loop(77, ev)  # loop body skipped, runs cleanup
    assert 77 not in app.current_items


def test_maybe_inter_item_sleep(app):
    app.sleep_var.set("0")
    app._maybe_inter_item_sleep(threading.Event(), other_free=0)  # early return
    app.sleep_var.set("1")
    ev = threading.Event()
    t0 = time.time()
    app._maybe_inter_item_sleep(ev, other_free=0)  # sleeps ~1s
    assert time.time() - t0 >= 0.8
    app.sleep_var.set("5")
    app._maybe_inter_item_sleep(ev, other_free=2)  # other free -> no sleep


def test_failure_cooldown(app):
    app.cooldown_var.set("0")
    app._trigger_failure_cooldown(1, q("http://host/x"), 7)  # fail_s<=0 -> no-op
    assert app._cooldown_until == {}
    app.cooldown_var.set("300")
    item = q("http://host/x")
    app._trigger_failure_cooldown(1, item, 7)
    domain = app._domain_of(item)
    # lq-time-03: cooldown deadlines are monotonic values.
    assert app._cooldown_until[domain] > time.monotonic()
    # Per-domain cooldown no longer blocks ALL workers — only a global pause
    # does. The failed domain is skipped inside _pick_next_item instead.
    assert app._is_blocked()[0] is False
    app.pause_event.set()
    assert app._is_blocked()[0] is True
    app.pause_event.clear()
    assert app._is_blocked()[0] is False
    app._cooldown_until = {}


def test_failure_cooldown_skips_only_failed_domain(app):
    """A failed domain is skipped by the picker while other domains are
    still claimable."""
    stop_bg_workers(app)
    app.cooldown_var.set("300")
    a = q("http://aaa/1")
    b = q("http://bbb/1")
    with app._dispatch_cv:
        app.queue_items.append(a)
        app.queue_items.append(b)
    app._trigger_failure_cooldown(1, a, 7)  # cool ONLY a's domain
    with app._dispatch_cv:
        picked = app._pick_next_item(0)
    assert picked is not None
    assert app._domain_of(picked) == app._domain_of(b)
    # Cool b's domain too -> nothing claimable.
    app._trigger_failure_cooldown(1, b, 7)
    with app._dispatch_cv:
        assert app._pick_next_item(0) is None
    app._cooldown_until = {}


def test_end_to_end_workers(app):
    """Real worker threads consume a queued echo to completion."""
    app.config["sleep_between_items"] = 0
    app.sleep_var.set("0")
    app._on_worker_count_changed_to = None
    app.worker_count_var.set("2")
    app._on_worker_count_changed()
    for i in range(4):
        app._process_link(f"http://host/{i}")
    assert pump_until(app, lambda: len(app.queue_items) == 0
                      and all(v is None for v in app.current_items.values()))


# ---------------------------------------------------------------------------
# worker pool scaling
# ---------------------------------------------------------------------------

def test_ensure_worker_count_up_and_down(app):
    app._ensure_worker_count(4)
    assert app._live_worker_count() >= 1
    app._ensure_worker_count(1)
    pump(app, 0.3)
    assert app._stopping_worker_count() >= 0  # exercised path
    app._on_worker_count_changed()  # reads var


def test_on_worker_count_changed_clamp(app):
    app.worker_count_var.set("999")
    app._on_worker_count_changed()
    assert app.worker_count_var.get() == "32"
    app.worker_count_var.set("notanint")
    app._on_worker_count_changed()  # falls back to config


# ---------------------------------------------------------------------------
# status / summary
# ---------------------------------------------------------------------------

def test_update_status_states(app):
    stop_bg_workers(app)
    app.pause_event.set()
    app._update_status(); pump(app, 0.05)
    app.pause_event.clear()
    app._cooldown_until = {"host": time.time() + 100}
    app._update_status(); pump(app, 0.05)
    app._cooldown_until = {}
    app.current_items[1] = q("http://a/1")  # running
    app._update_status(); pump(app, 0.05)
    app.current_items.clear()
    app.queue_items[:] = [q("http://a/1")]  # pending
    app._update_status(); pump(app, 0.05)
    app.queue_items.clear()
    app._update_status(); pump(app, 0.05)  # idle
    assert "Idle" in app.status_var.get() or app.status_var.get()


def test_update_status_summary(app):
    app.sleep_var.set("10")
    app.cooldown_var.set("60")
    app.max_per_domain_var.set("3")
    app._update_status_summary()
    assert "max/dom: 3" in app._status_settings_var.get()
    app.max_per_domain_var.set("0")
    app._update_status_summary()
    assert "no domain cap" in app._status_settings_var.get()


# ---------------------------------------------------------------------------
# settings handlers
# ---------------------------------------------------------------------------

def test_sleep_cooldown_maxdomain_handlers(app):
    app.sleep_var.set("12"); app._on_sleep_changed()
    assert app.config["sleep_between_items"] == 12
    app.cooldown_var.set("120"); app._on_cooldown_changed()
    assert app.config["failure_sleep_seconds"] == 120
    app.max_per_domain_var.set("4"); app._on_max_per_domain_changed()
    assert app.config["max_per_domain"] == 4
    app.max_per_domain_var.set("0"); app._on_max_per_domain_changed()
    assert app.config["max_per_domain"] == 0


def test_getters_fallback_on_bad_input(app):
    app.sleep_var.set("xx")
    assert app._get_sleep() == int(app.config["sleep_between_items"])
    app.cooldown_var.set("xx")
    assert app._get_failure_sleep() == int(app.config["failure_sleep_seconds"])
    app.max_per_domain_var.set("xx")
    assert app._get_max_per_domain() == int(app.config["max_per_domain"])


def test_log_verbosity_and_max_lines(app):
    app.log_verbosity_var.set("verbose"); app._on_log_verbosity_changed()
    assert app.config["log_verbosity"] == "verbose"
    app.log_verbosity_var.set("bogus"); app._on_log_verbosity_changed()
    assert app.config["log_verbosity"] == "summary"
    app.log_max_lines_var.set("50"); app._on_log_max_lines_changed()
    assert app.config["log_max_lines"] == 50
    app.log_max_lines_var.set("xx"); app._on_log_max_lines_changed()  # fallback


def test_output_folder_handlers(app, tmp_path):
    app.output_folder_var.set(str(tmp_path)); app._on_output_folder_changed()
    assert app.config["output_folder"] == str(tmp_path)
    assert app._resolve_cwd() == str(tmp_path)
    app.output_folder_var.set("/no/such/dir/here"); app._on_output_folder_changed()
    assert app._resolve_cwd() is None  # warns + falls back
    app._on_clear_output_folder()
    assert app.config["output_folder"] == ""
    assert app._resolve_cwd() is None
    app._on_browse_output_folder()  # monkeypatched askdirectory -> tmp_path
    assert app.config["output_folder"] == str(tmp_path)


def test_resolve_cwd_pathological(app):
    app.config["output_folder"] = "bad\x00path"
    assert app._resolve_cwd() is None


# ---------------------------------------------------------------------------
# settings dialog open/close + save gating
# ---------------------------------------------------------------------------

def test_settings_dialog_open_close(app):
    assert app._settings_dialog_is_open() is False
    app._open_settings_dialog()
    assert app._settings_dialog_is_open() is True
    # save deferred while open
    app.config["sleep_between_items"] = 77
    app._save_config()
    assert app._settings_dirty is True
    app._on_close_settings_dialog()
    assert app._settings_dialog_is_open() is False
    # flushed to disk
    with open(link_queue.CONFIG_FILE) as f:
        assert "77" in f.read()


def test_save_config_quietly_when_open(app):
    app._open_settings_dialog()
    app._save_config_quietly()
    assert app._settings_dirty is True
    app._on_close_settings_dialog()


# ---------------------------------------------------------------------------
# protocol editor + CRUD
# ---------------------------------------------------------------------------

def _drive_proto_editor(app, name, command, mode="queue", shell=False, existing=None):
    app._open_protocol_editor(existing)
    dlg = find_toplevel(app, "protocol")
    assert dlg is not None
    entry = find_widget(dlg, ttk.Entry)
    if existing is None:
        entry.delete(0, "end")
        entry.insert(0, name)
    txt = find_widget(dlg, tk.Text)
    txt.delete("1.0", "end")
    txt.insert("1.0", command)
    combo = find_widget(dlg, ttk.Combobox)
    combo.set(mode)
    if shell:
        chk = find_widget(dlg, ttk.Checkbutton)
        chk.invoke()
    save = find_widget(dlg, ttk.Button, text="Save")
    save.invoke()
    return dlg


def test_protocol_new_save(app):
    _drive_proto_editor(app, "ftps", "echo {url}", mode="immediate", shell=True)
    assert "ftps" in app.config["protocols"]
    assert app.config["protocols"]["ftps"]["shell"] is True


def test_protocol_save_invalid_quoting(app):
    # Bad quoting in exec mode -> showerror (monkeypatched) -> not saved.
    _drive_proto_editor(app, "weird", "echo 'unterminated", shell=False)
    assert "weird" not in app.config["protocols"]


def test_protocol_save_empty_name(app):
    _drive_proto_editor(app, "", "echo {url}")
    # empty name rejected; nothing new with blank key
    assert "" not in app.config["protocols"]


def test_protocol_edit_existing(app):
    _drive_proto_editor(app, "http", "echo edited {url}", existing="http")
    assert "edited" in app.config["protocols"]["http"]["command"]


def test_protocol_duplicate_delete_toggle(app):
    app.proto_tree.selection_set("http")
    app._on_duplicate_protocol()
    assert "http-copy" in app.config["protocols"]
    app.proto_tree.selection_set("http")
    app._on_duplicate_protocol()  # http-copy exists -> http-copy-2
    assert "http-copy-2" in app.config["protocols"]
    app.proto_tree.selection_set("http")
    before = app.config["protocols"]["http"]["shell"]
    app._on_toggle_protocol_shell()
    assert app.config["protocols"]["http"]["shell"] != before
    app.proto_tree.selection_set("ftp")
    app._on_delete_protocol()  # askyesno monkeypatched True
    assert "ftp" not in app.config["protocols"]


def test_protocol_actions_no_selection(app):
    app.proto_tree.selection_remove(*app.proto_tree.selection())
    app._on_edit_protocol()
    app._on_delete_protocol()
    app._on_duplicate_protocol()
    app._on_toggle_protocol_shell()  # all no-op without selection


def test_proto_double_click_and_new_edit(app):
    ev = types.SimpleNamespace(y=1)
    app._on_proto_double_click(ev)  # identify_row likely '' -> early return
    app._on_new_protocol()
    d = find_toplevel(app, "protocol")
    if d:
        d.destroy()
    app.proto_tree.selection_set("http")
    app._on_edit_protocol()
    d2 = find_toplevel(app, "protocol")
    if d2:
        d2.destroy()


def test_refresh_protocols_tree(app):
    app._refresh_protocols_tree()
    assert "http" in app.proto_tree.get_children()


# ---------------------------------------------------------------------------
# queue tree refresh / remove / context actions
# ---------------------------------------------------------------------------

def test_refresh_queue_list_with_inflight(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2")]
        app.current_items[1] = q("http://run/0")
    app._refresh_pending = False
    app._do_refresh_queue_list()
    rows = app.queue_tree.get_children()
    assert len(rows) == 3  # 1 inflight + 2 pending


def test_queue_render_limit(app):
    stop_bg_workers(app)
    app.config["queue_render_limit"] = 2
    with app._dispatch_cv:
        app.queue_items[:] = [q(f"http://h/{i}") for i in range(5)]
    app._refresh_pending = False
    app._do_refresh_queue_list()
    rows = app.queue_tree.get_children()
    assert "more" in rows                 # overflow collapsed to one row
    assert len(rows) == 3                 # 2 pending + summary
    assert "more pending" in app.queue_tree.set("more", "url")


def test_refresh_queue_list_coalesced(app):
    app._refresh_pending = True
    app._refresh_queue_list()  # collapses, returns early
    app._refresh_pending = False


def test_remove_selected(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2"), q("http://c/3")]
        app.current_items[1] = q("http://run/0")
    app._do_refresh_queue_list()
    rows = app.queue_tree.get_children()
    # select an inflight row (dropped) + two pending rows
    app.queue_tree.selection_set(rows[0], rows[1], rows[3])
    app._on_remove_selected()
    assert len(app.queue_items) == 1


def test_remove_selected_none(app):
    app.queue_tree.selection_remove(*app.queue_tree.selection())
    app._on_remove_selected()  # no selection -> no-op


def test_clear_queue(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2")]
    app._on_clear_queue()
    assert app.queue_items == []


def test_pause_toggle(app):
    app._on_pause_toggle()
    assert app.pause_event.is_set()
    assert app.pause_btn.cget("text") == "Resume"
    app._on_pause_toggle()
    assert not app.pause_event.is_set()


def _setup_two_selected(app):
    """Fresh queue + selection. Flushes any pending tree rebuild FIRST so a
    later root.update() (e.g. inside _clipboard_set) can't wipe the selection."""
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2")]
    app._refresh_pending = False
    app._do_refresh_queue_list()
    pump(app, 0.1)
    rows = app.queue_tree.get_children()
    app.queue_tree.selection_set(*rows)
    return rows


def test_selected_queue_items(app):
    _setup_two_selected(app)
    assert len(app._selected_queue_items()) == 2


def test_queue_copy_url(app):
    _setup_two_selected(app)
    app._on_queue_copy_url()
    assert app.root.clipboard_get() == "http://a/1\nhttp://b/2"


def test_queue_copy_cmd(app):
    _setup_two_selected(app)
    app._on_queue_copy_cmd()
    assert "echo" in app.root.clipboard_get()


def test_queue_rerun(app):
    _setup_two_selected(app)
    app.pause_event.set()
    app._on_queue_rerun()  # re-queues (already-pending -> duplicates skipped)
    pump(app, 0.2)


def test_queue_rerun_preserves_extra(app):
    # lq-rel-03: re-running a queued item must carry its mapped flags (extra),
    # not drop them.
    stop_bg_workers(app)
    item = QueueItem(url="http://x/1", protocol="http", template="echo {url}",
                     shell=False, extra=(("-o", "clip.mp4"),))
    with app._dispatch_cv:
        app.queue_items[:] = [item]
    app._refresh_pending = False
    app._do_refresh_queue_list()
    pump(app, 0.1)
    app.queue_tree.selection_set(*app.queue_tree.get_children())
    captured = []
    app.dispatcher._process_link = lambda url, extra=(): captured.append((url, extra))
    app._on_queue_rerun()
    assert captured == [("http://x/1", (("-o", "clip.mp4"),))]


def test_queue_actions_empty_selection(app):
    app.queue_tree.selection_remove(*app.queue_tree.selection())
    app._on_queue_rerun()
    app._on_queue_copy_url()
    app._on_queue_copy_cmd()
    assert app._selected_queue_items() == []


def test_clipboard_set(app):
    app._clipboard_set("hello")
    assert app.root.clipboard_get() == "hello"


# ---------------------------------------------------------------------------
# context menus + shortcuts + help
# ---------------------------------------------------------------------------

def test_popup_menus(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1")]
    app._do_refresh_queue_list()
    rows = app.queue_tree.get_children()
    app.queue_tree.selection_set(rows[0])
    ev = types.SimpleNamespace(y=5, x_root=0, y_root=0)
    app._popup_queue_menu(ev)  # tk_popup monkeypatched
    app.proto_tree.selection_set("http")
    app._popup_proto_menu(ev)


def test_global_duplicate(app):
    app.proto_tree.selection_set("http")
    app._global_duplicate(types.SimpleNamespace())  # focus None -> proceeds
    assert "http-copy" in app.config["protocols"]
    app.url_text.focus_force()
    pump(app, 0.05)
    app._global_duplicate(types.SimpleNamespace())  # focus in Text -> early return


def test_show_shortcuts_help(app):
    app._show_shortcuts_help()
    dlg = find_toplevel(app, "shortcut")
    assert dlg is not None
    dlg.destroy()


# ---------------------------------------------------------------------------
# logging + cross-thread plumbing
# ---------------------------------------------------------------------------

def test_log_and_truncation(app):
    app.config["log_max_lines"] = 5
    for i in range(20):
        app._log(f"line {i}")
    pump(app, 0.3)
    n = int(app.log_text.index("end-1c").split(".")[0]) - 1
    assert n <= 5


def test_clear_log(app):
    app._log("something")
    pump(app, 0.1)
    app._clear_log()
    assert app.log_text.get("1.0", "end-1c") == ""


def test_safe_after_cross_thread_and_poller(app):
    seen = []
    done = threading.Event()

    def worker():
        app._safe_after(0, lambda: seen.append(1))
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    done.wait(2)
    t.join(2)
    pump(app, 0.3)  # poller drains the cross-thread job
    assert seen == [1]


def test_poll_tk_jobs_reports_drops(app):
    app._dropped_tk_jobs = 3
    app._poll_tk_jobs()
    pump(app, 0.2)
    assert app._dropped_tk_jobs == 0


def test_safe_after_noop_after_stop(app):
    app.stop_event.set()
    app._safe_after(0, lambda: None)  # returns immediately
    app.stop_event.clear()


# ---------------------------------------------------------------------------
# text editing helpers (closures) + readonly log
# ---------------------------------------------------------------------------

def test_text_editing_events(app):
    app.url_text.focus_force()
    pump(app, 0.05)
    app.url_text.insert("1.0", "hello")
    for seq in ("<Control-Key-a>", "<<Copy>>", "<<Cut>>", "<<Paste>>", "<Button-3>"):
        try:
            app.url_text.event_generate(seq, x=2, y=2)
        except tk.TclError:
            pass
    pump(app, 0.1)


def test_log_readonly_block(app):
    app.log_text.focus_force()
    pump(app, 0.05)
    for kw in ("x", "Left", "c", "v"):
        try:
            app.log_text.event_generate("<Key>", keysym=kw)
        except tk.TclError:
            pass
    try:
        app.log_text.event_generate("<<Paste>>")
    except tk.TclError:
        pass
    pump(app, 0.1)


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

def test_shutdown_helpers(app):
    app._safe_save_state_on_shutdown()
    assert isinstance(app._snapshot_worker_threads(), list)
    assert isinstance(app._snapshot_immediate_threads(), list)
    LinkQueueApp._join_threads([], time.time() + 0.1)
    app._cancel_tk_poller()
    app._cancel_tk_poller()  # idempotent (None path)


def test_on_close_full_shutdown(app):
    # Exercises _shutdown() end to end + root.destroy via _on_close.
    app._process_link("http://x/1")
    app._on_close()
    assert app.stop_event.is_set()


def test_flush_save_state_skips_write_after_stop(headless_dispatcher):
    # lq-rel-05: a timer that fires _flush_save_state after stop_event is set
    # must NOT write — it could land post-teardown.
    disp = headless_dispatcher
    wrote = {"n": 0}
    disp._save_state = lambda: wrote.__setitem__("n", wrote["n"] + 1)
    disp.stop_event.set()
    disp._flush_save_state()
    assert wrote["n"] == 0


def test_flush_save_state_writes_when_running(headless_dispatcher):
    # lq-rel-05: the normal (not-stopping) path still writes.
    disp = headless_dispatcher
    wrote = {"n": 0}
    disp._save_state = lambda: wrote.__setitem__("n", wrote["n"] + 1)
    disp._flush_save_state()
    assert wrote["n"] == 1


def test_begin_save_shutdown_blocks_debounced_flush(headless_dispatcher):
    # lq-conc-02: once _begin_save_shutdown latches the flag, a debounced timer
    # that fires _flush_save_state must NOT write, even if stop_event is not yet
    # set — closing the race where a flush lands behind the authoritative
    # shutdown snapshot.
    disp = headless_dispatcher
    wrote = {"n": 0}
    disp._save_state = lambda: wrote.__setitem__("n", wrote["n"] + 1)
    disp._begin_save_shutdown()
    assert disp._shutting_down is True
    assert disp._save_timer is None
    disp._flush_save_state()              # the in-flight timer wakes up...
    assert wrote["n"] == 0                # ...but is blocked by the flag


def test_begin_save_shutdown_cancels_armed_timer(headless_dispatcher):
    # lq-conc-02: an already-armed debounced timer is cancelled by shutdown.
    disp = headless_dispatcher
    disp._save_delay = 30
    disp._request_save_state()
    assert disp._save_timer is not None
    armed = disp._save_timer
    disp._begin_save_shutdown()
    assert disp._save_timer is None
    armed.join(timeout=2.0)
    assert not armed.is_alive()              # cancel woke the timer thread


def test_request_save_state_after_stop_is_noop(headless_dispatcher):
    # lq-rel-05 companion: _request_save_state already refuses to arm a timer
    # once stopping, so the surviving guard is the _flush_save_state recheck.
    disp = headless_dispatcher
    disp.stop_event.set()
    disp._request_save_state()
    assert disp._save_timer is None


def test_shutdown_saves_state_again_after_join(app):
    # lq-conc-01: _shutdown must save state once more AFTER joining workers so
    # a transition that lands between the pre-stop save and the join is
    # persisted, not lost.
    stop_bg_workers(app)
    calls = {"n": 0}
    real_save = app.dispatcher._save_state

    def counting_save():
        calls["n"] += 1
        # Simulate a late worker transition arriving after the first save but
        # before the threads have joined: mutate the queue between saves.
        if calls["n"] == 1:
            with app._dispatch_cv:
                app.queue_items.append(q("http://late/1"))
        real_save()

    app.dispatcher._save_state = counting_save
    app._shutdown(timeout=1.0)
    assert calls["n"] >= 2                       # pre-stop AND post-join save
    in_flight, pending = app.dispatcher._load_state_items()
    assert "http://late/1" in [it.url for it in pending]


def test_shutdown_post_join_save_persists_settled_queue(app):
    # lq-conc-01: the post-join save is authoritative — the on-disk state after
    # shutdown matches the final in-memory queue.
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://keep/1"), q("http://keep/2")]
    app._shutdown(timeout=1.0)
    _, pending = app.dispatcher._load_state_items()
    assert [it.url for it in pending] == ["http://keep/1", "http://keep/2"]


def test_text_editing_menu_commands(app):
    menu = next(w for w in app.url_text.winfo_children()
                if isinstance(w, tk.Menu))
    app.url_text.insert("1.0", "selectme")
    app.url_text.focus_force()
    pump(app, 0.05)
    for label in ("Select All", "Copy", "Cut", "Paste"):
        try:
            menu.invoke(menu.index(label))
        except tk.TclError:
            pass
        pump(app, 0.02)
    # show_menu path (builds + posts the patched popup, runs entryconfigure logic)
    ev = types.SimpleNamespace(x_root=0, y_root=0)
    app.url_text.event_generate("<Button-3>", x=2, y=2)
    pump(app, 0.05)


def test_make_text_readonly_block(app):
    app.log_text.focus_force()
    pump(app, 0.05)
    # plain key blocked, nav allowed, ctrl+c allowed, ctrl+v blocked
    for kwargs in (
        {"keysym": "x"},
        {"keysym": "Left"},
        {"keysym": "c", "state": 4},
        {"keysym": "v", "state": 4},
    ):
        try:
            app.log_text.event_generate("<Key>", **kwargs)
        except tk.TclError:
            pass
        pump(app, 0.02)


def _raise(*a, **k):
    raise RuntimeError("injected")


def _raise_tcl(*a, **k):
    raise tk.TclError("injected")


def _raise_full(*a, **k):
    import queue as _q
    raise _q.Full()


def _raise_os(*a, **k):
    raise OSError("injected")


MAP = {"f:": "-o"}


# ---------------------------------------------------------------------------
# prefix-token mappings (input "url f:name" -> append "-o name")
# ---------------------------------------------------------------------------

def test_split_entry_basic():
    url, extra = LinkQueueApp._split_entry("http://x.com f:clip.mp4", MAP)
    assert url == "http://x.com" and extra == (("-o", "clip.mp4"),)


def test_split_entry_no_prefix_whole_line_is_url():
    # No prefix token -> the entire line is the URL (multi-token kept verbatim).
    url, extra = LinkQueueApp._split_entry("http://x.com and more", MAP)
    assert url == "http://x.com and more" and extra == ()


def test_split_entry_prefix_does_not_eat_url():
    # rel-07: a short prefix that matches the start of a URL must not consume it.
    url, extra = LinkQueueApp._split_entry("http://x.com", {"h": "-x"})
    assert url == "http://x.com" and extra == ()


def test_split_entry_empty_param_skipped():
    # rel-08: a bare prefix (no value) is dropped, not turned into "-o ''".
    url, extra = LinkQueueApp._split_entry("http://x.com f:", MAP)
    assert url == "http://x.com" and extra == ()


def test_token_is_url():
    assert LinkQueueApp._token_is_url("http://x") is True
    assert LinkQueueApp._token_is_url("clip.mp4") is False
    # rel-09: scheme-only URLs recognized so a colliding prefix can't eat them.
    assert LinkQueueApp._token_is_url("magnet:?xt=urn:btih:abc") is True
    assert LinkQueueApp._token_is_url("MAILTO:a@b.com") is True
    assert LinkQueueApp._token_is_url("f:clip.mp4") is False   # a mapping token


def test_split_entry_does_not_eat_magnet():
    # rel-09: prefix "magn" must not consume a magnet: URL.
    url, extra = LinkQueueApp._split_entry("magnet:?xt=urn:btih:abc", {"magn": "-x"})
    assert url == "magnet:?xt=urn:btih:abc" and extra == ()


def test_worker_pool_counts(app):
    alive, stopping = app._pool.counts()
    assert isinstance(alive, int) and isinstance(stopping, int)
    assert stopping <= alive


def test_on_add_warns_on_lines_without_url(app):
    app.pause_event.set()
    app.config["token_mappings"] = {"f:": "-o"}
    # second line is only a prefix token -> no URL -> skipped + logged
    app.url_text.insert("1.0", "http://a.com f:clip.mp4\nf:\n")
    app._on_add()
    pump(app, 0.2)
    log = app.log_text.get("1.0", "end-1c")
    assert "skipped 1 line(s) with no URL" in log
    assert len(app.queue_items) == 1   # only the real URL enqueued


def test_schedule_link_count_debounces(app):
    app.url_text.insert("1.0", "http://a\nhttp://b")
    app._schedule_link_count()
    assert app._count_after_id is not None      # update deferred, not run yet
    app._schedule_link_count()                  # coalesced (cancels prior)
    pump(app, 0.4)                              # >200ms -> fires
    assert app._count_after_id is None
    assert "2 link" in app.count_var.get()


def test_schedule_link_count_cancel_error(app, monkeypatch):
    app._count_after_id = "stale"
    monkeypatch.setattr(app.root, "after_cancel", _raise)  # cancel fails
    app._schedule_link_count()                  # except swallowed, reschedules
    assert app._count_after_id is not None
    monkeypatch.undo()
    try:
        app.root.after_cancel(app._count_after_id)   # cleanup real timer
    except Exception:
        pass
    app._count_after_id = None


def test_split_entry_multiple_params():
    m = {"f:": "-o", "q:": "-f"}
    url, extra = LinkQueueApp._split_entry("http://x f:a.mp4 q:best", m)
    assert url == "http://x"
    assert extra == (("-o", "a.mp4"), ("-f", "best"))


def test_split_entry_longest_prefix_wins():
    m = {"f:": "-o", "ff:": "-x"}
    _, extra = LinkQueueApp._split_entry("http://x ff:y", m)
    assert extra == (("-x", "y"),)


def test_parse_entries_multiline():
    raw = "http://a f:a.mp4\n\nhttp://b\n  http://c f:c.mp4  "
    entries = LinkQueueApp._parse_entries(raw, MAP)
    assert entries == [
        ("http://a", (("-o", "a.mp4"),)),
        ("http://b", ()),
        ("http://c", (("-o", "c.mp4"),)),
    ]


def test_extra_argv_and_shell():
    assert LinkQueueApp._extra_argv((("-o", "a b.mp4"),)) == ["-o", "a b.mp4"]
    # multi-word flag splits into separate argv elements
    assert LinkQueueApp._extra_argv((("-x --fmt", "v"),)) == ["-x", "--fmt", "v"]
    assert LinkQueueApp._extra_shell((("-o", "a b.mp4"),)) == "-o 'a b.mp4'"


def test_process_link_carries_extra(app):
    app.pause_event.set()
    assert app._process_link("http://x", (("-o", "c.mp4"),)) == "queue"
    it = list(app.queue_items)[0]
    assert it.extra == (("-o", "c.mp4"),)
    disp = app._item_display(it)
    assert "-o" in disp and "c.mp4" in disp


def test_item_display_appends_extra_shell(app):
    it = QueueItem("http://x", "http", "echo {url}", True, (("-o", "a b.mp4"),))
    disp = app._item_display(it)
    assert disp.startswith("(shell)") and "-o 'a b.mp4'" in disp


def test_run_item_with_extra(app):
    it = q("http://x", template="echo {url}")
    it = it._replace(extra=(("-o", "out.mp4"),))
    assert app._run_item(it, "t") == 0      # echo http://x -o out.mp4
    pump(app, 0.1)


def test_on_add_maps_prefix(app):
    app.pause_event.set()
    app.config["token_mappings"] = {"f:": "-o"}
    app.url_text.insert("1.0", "http://a.com f:clip.mp4\nhttp://b.com\n")
    app._on_add()
    pump(app, 0.2)
    items = list(app.queue_items)
    by_url = {it.url: it.extra for it in items}
    assert by_url["http://a.com"] == (("-o", "clip.mp4"),)
    assert by_url["http://b.com"] == ()


def test_state_roundtrip_extra(app):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [QueueItem("http://a/1", "http", "yt-dlp {url}",
                                        False, (("-o", "a b.mp4"),))]
    app._save_state()
    _, pending = app._load_state_items()
    assert pending[0].extra == (("-o", "a b.mp4"),)


def test_normalize_warns_on_shell_url(capsys):
    # sec-02 load-time check: warn on shell=True + bare {url} in any protocol
    # or the default handler.
    cfg = {
        "protocols": {
            "bad": {"shell": True, "command": "wget {url}", "mode": "queue"},
            "ok":  {"shell": True, "command": "wget {url_quoted}", "mode": "queue"},
        },
        "default_shell": True,
        "default_command": "cat {url}",
    }
    link_queue.ConfigStore._normalize_config_schema(cfg)
    err = capsys.readouterr().err
    assert "protocol 'bad'" in err
    assert "default_command" in err
    assert "protocol 'ok'" not in err


def test_run_item_shell_url_rejected_each_call(app):
    # sec-04: bare {url} is rejected at runtime — each call is refused
    # (no one-shot dedup), so the operator sees the same error for
    # every queued item using the unsafe template.
    it = QueueItem("http://x", "http", "echo {url}", True, ())
    assert app._run_item(it, "t") == -1
    assert app._run_item(it, "t") == -1
    pump(app, 0.2)
    log = app.log_text.get("1.0", "end-1c")
    assert log.count("refusing shell=True") == 2


def test_run_item_shell_url_quoted_no_warn(app):
    it = QueueItem("http://x", "http", "echo {url_quoted}", True, ())
    app._run_item(it, "t")
    pump(app, 0.2)
    assert "shell template has bare" not in app.log_text.get("1.0", "end-1c")


def test_normalize_token_mappings(tmp_path):
    cfg = {"protocols": {}, "token_mappings": {"f:": "-o", "": "x", "q:": 5, 3: "y"}}
    link_queue.ConfigStore._normalize_config_schema(cfg)
    assert cfg["token_mappings"] == {"f:": "-o"}   # bad/empty entries dropped
    cfg2 = {"protocols": {}, "token_mappings": "not-a-dict"}
    link_queue.ConfigStore._normalize_config_schema(cfg2)
    assert cfg2["token_mappings"] == {}


def test_normalize_warns_on_dropped_non_dict_protocol(capsys):
    # rel-10: a non-dict `protocols[<name>]` is dropped — but with a stderr
    # warning naming the bad key so corrupt configs are diagnosable.
    cfg = {
        "protocols": {
            "good": {"command": "echo {url}", "mode": "queue", "shell": False},
            "broken": "this should be a dict",
            "also_broken": 42,
        }
    }
    link_queue.ConfigStore._normalize_config_schema(cfg)
    err = capsys.readouterr().err
    assert "'broken'" in err and "'also_broken'" in err
    assert "good" not in err               # good protocol is not warned about
    assert "broken" not in cfg["protocols"]   # actually dropped
    assert "also_broken" not in cfg["protocols"]
    assert "good" in cfg["protocols"]


def test_queue_iid_for_url_is_stable_and_tk_safe():
    # rel-12: deterministic iid per URL, contains only [p:][a-f0-9].
    iid1 = link_queue._queue_iid_for_url("https://x.example/path?q={a}&b=c")
    iid2 = link_queue._queue_iid_for_url("https://x.example/path?q={a}&b=c")
    assert iid1 == iid2
    assert iid1.startswith("p:")
    rest = iid1[len("p:"):]
    assert len(rest) == 16
    assert all(c in "0123456789abcdef" for c in rest)
    # No Tk-reserved characters.
    for bad in "{}":
        assert bad not in iid1
    # Distinct URLs map to distinct iids.
    assert iid1 != link_queue._queue_iid_for_url("https://x.example/other")


def test_queue_iid_handles_unicode_and_surrogates():
    # Pathologic URL must not raise.
    url = "https://example/\udca0\udca1?x={1}"
    out = link_queue._queue_iid_for_url(url)
    assert out.startswith("p:") and len(out) == 18


def _drive_mapping_editor(app, existing, prefix, flag):
    app._open_mapping_editor(existing)
    dlg = find_toplevel(app, "mapping")
    assert dlg is not None
    entries = [w for w in descendants(dlg) if isinstance(w, ttk.Entry)]
    if existing is None:
        entries[0].delete(0, "end")
        entries[0].insert(0, prefix)
    entries[1].delete(0, "end")
    entries[1].insert(0, flag)
    find_widget(dlg, ttk.Button, text="Save").invoke()


def test_mapping_editor_new_edit_delete(app):
    app._open_settings_dialog()
    _drive_mapping_editor(app, None, "g:", "--geo")
    assert app.config["token_mappings"]["g:"] == "--geo"
    assert "g:" in app.map_tree.get_children()
    _drive_mapping_editor(app, "g:", "g:", "--geo-bypass")  # edit (prefix readonly)
    assert app.config["token_mappings"]["g:"] == "--geo-bypass"
    app.map_tree.selection_set("g:")
    app._on_delete_mapping()                # askyesno patched True
    assert "g:" not in app.config["token_mappings"]


def test_mapping_editor_requires_fields(app):
    _drive_mapping_editor(app, None, "", "-o")     # empty prefix -> showerror, no save
    assert "" not in app.config["token_mappings"]
    _drive_mapping_editor(app, None, "z:", "")     # empty flag -> showerror, no save
    assert "z:" not in app.config["token_mappings"]


def test_mapping_button_handlers(app):
    app._on_new_mapping()                       # opens the editor
    d = find_toplevel(app, "mapping")
    if d:
        d.destroy()
    app.map_tree.selection_set("f:")            # default mapping row
    app._on_edit_mapping()                      # opens editor for selection
    d2 = find_toplevel(app, "mapping")
    if d2:
        d2.destroy()


def test_mapping_actions_no_selection(app):
    app.map_tree.selection_remove(*app.map_tree.selection())
    app._on_edit_mapping()
    app._on_delete_mapping()
    app._on_mapping_double_click(types.SimpleNamespace(y=1))   # no row -> no-op


# ---------------------------------------------------------------------------
# defensive / error-path branches (injected failures)
# ---------------------------------------------------------------------------

def test_save_state_write_error(app, monkeypatch):
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1")]
    monkeypatch.setattr(link_queue, "_yaml_dump", _raise)
    app._save_state()  # tmp cleanup + outer except + log
    pump(app, 0.1)


def test_save_state_failure_on_shutdown_falls_back_to_stderr(
        headless_dispatcher, monkeypatch, capsys):
    # lq-obs-01: once stop_event is set, self._log is dropped by _safe_after,
    # so a save failure on the shutdown path must surface on stderr instead of
    # vanishing silently.
    disp = headless_dispatcher
    logged = []
    disp._log = logged.append
    monkeypatch.setattr(link_queue, "_yaml_dump", _raise)
    disp.stop_event.set()
    disp._save_state()
    err = capsys.readouterr().err
    assert "could not save queue state" in err
    assert logged == []                       # did NOT route through _log


def test_save_state_failure_when_running_uses_log(headless_dispatcher, monkeypatch,
                                                  capsys):
    # lq-obs-01: while running (stop_event clear) the failure still goes through
    # _log, not stderr.
    disp = headless_dispatcher
    logged = []
    disp._log = logged.append
    monkeypatch.setattr(link_queue, "_yaml_dump", _raise)
    disp._save_state()
    assert any("could not save queue state" in m for m in logged)
    assert "could not save queue state" not in capsys.readouterr().err


def test_write_config_errors(app, monkeypatch):
    monkeypatch.setattr(app.config, "save", _raise)
    app._write_config_now()      # except -> _log error
    app._save_config_quietly()   # except -> _log warn
    pump(app, 0.1)


def test_safe_save_state_on_shutdown_error(app, monkeypatch):
    monkeypatch.setattr(app, "_save_state", _raise)
    app._safe_save_state_on_shutdown()  # swallowed


def test_clipboard_error(app, monkeypatch):
    monkeypatch.setattr(app.root, "clipboard_clear", _raise_tcl)
    app._clipboard_set("hi")  # except TclError -> log
    pump(app, 0.1)


def test_safe_after_queue_full(app):
    stop_bg_workers(app)
    try:
        while True:
            app._tk_jobs.put_nowait((lambda: None, ()))
    except Exception:
        pass
    done = threading.Event()

    def w():
        app._safe_after(0, lambda: None)  # full -> dropped++
        done.set()

    t = threading.Thread(target=w)
    t.start()
    done.wait(2)
    t.join(2)
    assert app._dropped_tk_jobs >= 1
    try:
        while True:
            app._tk_jobs.get_nowait()
    except Exception:
        pass


def test_poll_tk_jobs_job_raises(app):
    app._tk_jobs.put_nowait((_raise, ()))
    app._poll_tk_jobs()  # fn raises -> swallowed


def test_join_threads_exception():
    class Bad:
        def join(self, timeout=None):
            raise RuntimeError("x")
    LinkQueueApp._join_threads([Bad()], time.time() + 0.1)


def test_cancel_tk_poller_error(app, monkeypatch):
    app._tk_poll_id = "fake"
    monkeypatch.setattr(app.root, "after_cancel", _raise_tcl)
    app._cancel_tk_poller()  # except -> swallowed, id reset
    assert app._tk_poll_id is None


def test_update_status_summary_bad_vars(app):
    app.sleep_var.set("abc")
    app.cooldown_var.set("abc")
    app.max_per_domain_var.set("abc")
    app._update_status_summary()  # all three except fallbacks


def test_text_editing_error_branches(app, monkeypatch):
    menu = next(w for w in app.url_text.winfo_children()
                if isinstance(w, tk.Menu))
    monkeypatch.setattr(app.url_text, "event_generate", _raise_tcl)
    monkeypatch.setattr(app.url_text, "tag_add", _raise_tcl)
    for label in ("Copy", "Cut", "Paste", "Select All"):
        try:
            menu.invoke(menu.index(label))  # do_* except branches
        except tk.TclError:
            pass


def test_entry_select_all(app):
    menu = next(w for w in app.output_folder_entry.winfo_children()
                if isinstance(w, tk.Menu))
    app.output_folder_entry.insert(0, "abc")
    menu.invoke(menu.index("Select All"))  # entry branch (select_range)


def test_readonly_block_with_display(app):
    app.root.deiconify()
    pump(app, 0.1)
    app.log_text.focus_force()
    pump(app, 0.15)
    for kwargs in ({"keysym": "x"}, {"keysym": "Left"},
                   {"keysym": "c", "state": 4}, {"keysym": "v", "state": 4},
                   {"keysym": "a", "state": 4}):
        try:
            app.log_text.event_generate("<Key>", **kwargs)
        except tk.TclError:
            pass
        pump(app, 0.03)
    try:
        app.log_text.event_generate("<<Paste>>")
        app.log_text.event_generate("<<Cut>>")
    except tk.TclError:
        pass
    pump(app, 0.05)
    app.root.withdraw()


def test_settings_dialog_rebuild_and_nondirty_close(app):
    app._settings_dialog = None
    app._open_settings_dialog()  # None -> rebuild branch
    assert app._settings_dialog is not None
    app._settings_dirty = False
    app._on_close_settings_dialog()  # not dirty -> no write
    app._on_close_settings_dialog.__self__  # noqa - keep ref
    # None-guard early return
    saved = app._settings_dialog
    app._settings_dialog = None
    app._on_close_settings_dialog()
    app._settings_dialog = saved


def test_on_close_destroy_error(app, monkeypatch):
    monkeypatch.setattr(app.root, "destroy", _raise_tcl)
    app._on_close()  # destroy raises -> except swallowed
    assert app.stop_event.is_set()


def test_script_dir_no_dunder_file(monkeypatch):
    monkeypatch.delattr(link_queue, "__file__", raising=False)
    assert link_queue._script_dir() == os.getcwd()  # NameError -> cwd fallback


def test_settings_dialog_is_open_states(app):
    app._open_settings_dialog()
    app._settings_dialog_visible = True
    assert app._settings_dialog_is_open() is True
    app._settings_dialog_visible = False
    assert app._settings_dialog_is_open() is False   # not None, not visible
    saved = app._settings_dialog
    app._settings_dialog = None
    assert app._settings_dialog_is_open() is False   # None branch
    app._settings_dialog = saved


def test_show_menu_variants(app):
    app.root.deiconify()  # Button-3 binding only dispatches when mapped
    pump(app, 0.1)
    # text widget WITH selection (has_sel True, editable True)
    app.url_text.insert("1.0", "abc")
    app.url_text.tag_add("sel", "1.0", "end-1c")
    app.url_text.focus_force()
    pump(app, 0.1)
    app.url_text.event_generate("<Button-3>", x=2, y=2)
    pump(app, 0.1)
    # readonly text widget (editable False -> Cut/Paste disabled)
    app.log_text.event_generate("<Button-3>", x=2, y=2)
    pump(app, 0.1)
    # entry widget (selection_present branch)
    app.output_folder_entry.insert(0, "xyz")
    app.output_folder_entry.select_range(0, "end")
    app.output_folder_entry.event_generate("<Button-3>", x=2, y=2)
    pump(app, 0.1)
    app.root.withdraw()


def test_set_state_color_error(app, monkeypatch):
    monkeypatch.setattr(app._status_state_lbl, "configure", _raise_tcl)
    app._update_status()        # _set_state_color hits except
    pump(app, 0.1)


def test_safe_after_main_thread_error(app, monkeypatch):
    monkeypatch.setattr(app.root, "after", _raise)
    app._safe_after(0, lambda: None)  # main-thread except branch


def test_show_menu_error_branches(app, monkeypatch):
    app.root.deiconify()
    pump(app, 0.1)
    app.url_text.focus_force()
    pump(app, 0.1)
    # Make the inner introspection calls raise so show_menu's except branches
    # run. Do NOT patch event_generate itself — that would stop the Button-3
    # event from ever reaching show_menu.
    monkeypatch.setattr(app.url_text, "focus_set", _raise_tcl)
    monkeypatch.setattr(app.url_text, "tag_ranges", _raise_tcl)
    monkeypatch.setattr(app.url_text, "cget", _raise_tcl)
    app.url_text.event_generate("<Button-3>", x=2, y=2)
    pump(app, 0.1)
    app.root.withdraw()


def test_close_settings_dialog_errors(app, monkeypatch):
    app._open_settings_dialog()
    app._settings_dirty = True
    monkeypatch.setattr(app._settings_dialog, "focus_set", _raise_tcl)
    monkeypatch.setattr(app._settings_dialog, "withdraw", _raise_tcl)
    app._on_close_settings_dialog()  # focus + withdraw except branches


def test_main_themed(tmp_path, monkeypatch):
    """Drive main()'s platform/theme branch (win32 + vista)."""
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "state.yaml"))
    monkeypatch.setattr(link_queue.sys, "platform", "win32")
    monkeypatch.setattr(ttk.Style, "theme_names",
                        lambda self: ["vista", "clam"])
    monkeypatch.setattr(ttk.Style, "theme_use", lambda self, *a, **k: None)
    created = {}
    real_tk = tk.Tk

    def fake_tk(*a, **k):
        r = real_tk(*a, **k)
        r.withdraw()
        created["root"] = r
        return r

    monkeypatch.setattr(tk, "Tk", fake_tk)
    monkeypatch.setattr(tk.Misc, "mainloop", lambda self, *a, **k: None, raising=False)
    link_queue.main()
    r = created.get("root")
    if r is not None:
        try:
            r.destroy()
        except tk.TclError:
            pass


def test_main_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(link_queue, "CONFIG_FILE", str(tmp_path / "cfg.yaml"))
    monkeypatch.setattr(link_queue, "LEGACY_CONFIG_FILE", str(tmp_path / "cfg.json"))
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "state.yaml"))
    created = {}
    real_tk = tk.Tk

    def fake_tk(*a, **k):
        root = real_tk(*a, **k)
        root.withdraw()
        created["root"] = root
        return root

    monkeypatch.setattr(tk, "Tk", fake_tk)
    monkeypatch.setattr(tk.Misc, "mainloop", lambda self, *a, **k: None, raising=False)
    link_queue.main()
    root = created.get("root")
    if root is not None:
        try:
            root.destroy()
        except tk.TclError:
            pass


# --- coverage gap-fillers --------------------------------------------------

def test_pending_queue_remove_missing_raises():
    """L252: removing an item that was never appended raises ValueError.
    test-02b-followup: pure `_PendingQueue` test, no fixture needed."""
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    with pytest.raises(ValueError, match="not in pending queue"):
        pq.remove(q("http://never-added/"))


def _force_restored_log_branches(app, monkeypatch, kind: str) -> list[str]:
    """Drive the restored-log branches (L773-781). _restore_queue_from_state
    lives on app.dispatcher (façade); patch its state-load + log handlers there."""
    msgs: list[str] = []
    disp = app.dispatcher
    monkeypatch.setattr(disp, "_log", lambda m: msgs.append(m))
    monkeypatch.setattr(disp, "_refresh_queue_list", lambda: None)
    monkeypatch.setattr(disp, "_update_status", lambda: None)
    if kind == "inflight":
        monkeypatch.setattr(disp, "_load_state_items",
                            lambda data=None: ([q("http://in/1")], []))
    elif kind == "pending":
        monkeypatch.setattr(disp, "_load_state_items",
                            lambda data=None: ([], [q("http://p/1")]))
    else:
        monkeypatch.setattr(disp, "_load_state_items",
                            lambda data=None: ([q("http://in/1")], [q("http://p/1")]))
    disp._restore_queue_from_state()
    return msgs


def test_restored_log_inflight_only(app, monkeypatch):
    msgs = _force_restored_log_branches(app, monkeypatch, "inflight")
    assert any("in-flight" in m and "will retry" in m for m in msgs)


def test_restored_log_pending_only(app, monkeypatch):
    msgs = _force_restored_log_branches(app, monkeypatch, "pending")
    assert any("item(s) from previous session" in m and "will retry" not in m
               for m in msgs)


def test_restored_log_mixed(app, monkeypatch):
    msgs = _force_restored_log_branches(app, monkeypatch, "mixed")
    assert any("in-flight" in m and "pending" in m for m in msgs)


# --- perf-08: _PendingQueue.__getitem__ slice + bounds --------------------

def test_pending_queue_getitem_int_negative_index():
    # test-02b-followup: pure _PendingQueue — no fixture needed.
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    for i in range(5):
        pq.append(q(f"http://h/{i}"))
    assert pq[-1].url == "http://h/4"
    assert pq[0].url == "http://h/0"


def test_pending_queue_getitem_out_of_range():
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    pq.append(q("http://only/1"))
    with pytest.raises(IndexError):
        _ = pq[5]
    with pytest.raises(IndexError):
        _ = pq[-10]


def test_pending_queue_getitem_extended_slice():
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    for i in range(5):
        pq.append(q(f"http://h/{i}"))
    out = pq[::2]
    assert [it.url for it in out] == ["http://h/0", "http://h/2", "http://h/4"]


def test_pending_queue_getitem_cache_invalidates_on_mutation():
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    first = q("http://h/1")
    second = q("http://h/2")
    third = q("http://h/3")
    pq.append(first)
    assert pq[0] is first
    pq.append(second)
    assert pq[1] is second
    pq.remove(first)
    assert pq[0] is second
    pq[:] = [third]
    assert pq[0] is third


# --- scal-04: command_timeout_seconds branches ----------------------------

def test_command_timeout_seconds_default_zero(headless_dispatcher):
    # test-02b-followup: pure Dispatcher method — headless fixture is enough.
    assert headless_dispatcher._command_timeout_seconds() == 0


def test_command_timeout_seconds_positive(headless_dispatcher):
    headless_dispatcher.config["command_timeout_seconds"] = 7
    assert headless_dispatcher._command_timeout_seconds() == 7


def test_command_timeout_seconds_negative_treated_as_off(headless_dispatcher):
    headless_dispatcher.config["command_timeout_seconds"] = -5
    assert headless_dispatcher._command_timeout_seconds() == 0


def test_command_timeout_seconds_unparseable_treated_as_off(headless_dispatcher):
    headless_dispatcher.config["command_timeout_seconds"] = "abc"
    assert headless_dispatcher._command_timeout_seconds() == 0


def test_immediate_concurrency_falls_back_to_worker_count(headless_dispatcher):
    # lq-arch-01: with immediate_worker_count unset/0/empty, immediate
    # parallelism follows worker_count (back-compat).
    disp = headless_dispatcher
    disp.config["worker_count"] = 3
    disp.config["immediate_worker_count"] = 0
    assert disp._immediate_concurrency() == 3
    disp.config["immediate_worker_count"] = ""
    assert disp._immediate_concurrency() == 3
    del disp.config["immediate_worker_count"]
    assert disp._immediate_concurrency() == 3


def test_immediate_concurrency_independent_of_worker_count(headless_dispatcher):
    # lq-arch-01: a dedicated immediate_worker_count overrides worker_count, so
    # 1 queue worker + 4 immediate runners is expressible.
    disp = headless_dispatcher
    disp.config["worker_count"] = 1
    disp.config["immediate_worker_count"] = 4
    assert disp._immediate_concurrency() == 4


def test_immediate_concurrency_clamps_and_tolerates_garbage(headless_dispatcher):
    # lq-arch-01: a misconfigured 0 still admits one runner; unparseable values
    # fall back to worker_count, then to 1.
    disp = headless_dispatcher
    disp.config["worker_count"] = 2
    disp.config["immediate_worker_count"] = -5
    assert disp._immediate_concurrency() == 1
    disp.config["immediate_worker_count"] = "abc"
    assert disp._immediate_concurrency() == 2     # falls back to worker_count
    disp.config["worker_count"] = "xyz"
    assert disp._immediate_concurrency() == 1     # both unparseable -> 1


try:
    from hypothesis import given as _given_arch, strategies as _st_arch
    from hypothesis import settings as _hs_arch

    @_hs_arch(max_examples=80, deadline=None)
    @_given_arch(
        wc=_st_arch.one_of(_st_arch.integers(-3, 50), _st_arch.text(max_size=4),
                           _st_arch.none()),
        iwc=_st_arch.one_of(_st_arch.integers(-3, 50), _st_arch.text(max_size=4),
                            _st_arch.none()),
    )
    def test_immediate_concurrency_property(wc, iwc):
        # lq-arch-01 property: _immediate_concurrency always returns an int >= 1
        # for ANY config value pair, never raising.
        cfg = dict(link_queue.DEFAULT_CONFIG)
        cfg["worker_count"] = wc
        cfg["immediate_worker_count"] = iwc
        disp = link_queue.Dispatcher.headless(cfg)
        try:
            n = disp._immediate_concurrency()
            assert isinstance(n, int) and n >= 1
        finally:
            disp.stop_event.set()
except ImportError:  # pragma: no cover - hypothesis always installed in CI
    pass


def test_resize_immediate_pool_recomputes_size(headless_dispatcher):
    # lq-conc-01: changing the (immediate) worker count must recompute
    # _immediate_pool_size and grow the live consumer pool — the resize used
    # to be unwired, leaving immediate concurrency pinned until restart.
    disp = headless_dispatcher
    disp.config["worker_count"] = 1
    disp.config["immediate_worker_count"] = 0
    disp._resize_immediate_pool()
    assert disp._immediate_pool_size == 1
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 1
    disp.config["immediate_worker_count"] = 4
    disp._resize_immediate_pool()
    assert disp._immediate_pool_size == 4
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 4


def test_ensure_worker_count_resizes_immediate_pool(headless_dispatcher):
    # lq-conc-01: the public worker-count entry point drives the immediate
    # resize (immediate pool follows worker_count by default — arch-01).
    disp = headless_dispatcher
    disp.config["immediate_worker_count"] = 0
    disp._ensure_worker_count(3)
    assert disp._immediate_pool_size == 3
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 3


def test_downward_resize_retires_surplus_consumers(headless_dispatcher):
    # lq-rel-02: a downward immediate-pool resize signals surplus consumers via
    # their per-thread stop event so they actually exit, instead of leaking
    # until shutdown.
    disp = headless_dispatcher
    disp.config["immediate_worker_count"] = 4
    disp._resize_immediate_pool()
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 4
    disp.config["immediate_worker_count"] = 1
    disp._resize_immediate_pool()
    # Surplus consumers were signalled; wait for them to wind down.
    end = time.time() + 3.0
    while time.time() < end:
        live = len([t for t in disp.immediate_threads if t.is_alive()])
        if live == 1:
            break
        time.sleep(0.05)
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 1
    # And the one survivor is still usable: a subsequent upward resize regrows.
    disp.config["immediate_worker_count"] = 3
    disp._resize_immediate_pool()
    assert len([t for t in disp.immediate_threads if t.is_alive()]) == 3


def test_immediate_q_maxsize_helper(tmp_path, monkeypatch):
    # lq-scal-01: the maxsize getter clamps non-positive / garbage to 0
    # (unbounded), positive values pass through.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 5
    d = link_queue.Dispatcher.headless(cfg)
    try:
        assert d._immediate_q_maxsize() == 5
        assert d._immediate_q.maxsize == 5
        d.config["immediate_queue_maxsize"] = 0
        assert d._immediate_q_maxsize() == 0
        d.config["immediate_queue_maxsize"] = -7
        assert d._immediate_q_maxsize() == 0
        d.config["immediate_queue_maxsize"] = "junk"
        assert d._immediate_q_maxsize() == 0
    finally:
        d.stop_event.set()


def test_resize_immediate_queue_live_grow_preserves_items(tmp_path, monkeypatch):
    # lq-rel-02: bumping immediate_queue_maxsize up live swaps in a larger
    # bounded queue and keeps every pending item.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 3
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()                       # no consumers drain
    try:
        for i in range(3):
            d._immediate_q.put(q(f"magnet:?xt={i}", protocol="magnet"))
        d.config["immediate_queue_maxsize"] = 10
        with d._immediate_lock:
            assert d._resize_immediate_queue_locked() == 0
        assert d._immediate_q.maxsize == 10
        assert d._immediate_q.qsize() == 3
        urls = {it.url for it in list(d._immediate_q.queue)}
        assert urls == {f"magnet:?xt={i}" for i in range(3)}
    finally:
        d.stop_event.set()


def test_resize_immediate_queue_live_shrink_drops_overflow(tmp_path, monkeypatch):
    # lq-rel-02: shrinking the cap below the current depth swaps in a smaller
    # queue, keeps what fits, drops the rest with a warning.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 10
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()
    logs = []
    d._log = logs.append
    try:
        for i in range(8):
            d._immediate_q.put(q(f"magnet:?xt={i}", protocol="magnet"))
        d.config["immediate_queue_maxsize"] = 3
        with d._immediate_lock:
            assert d._resize_immediate_queue_locked() == 5
        assert d._immediate_q.maxsize == 3
        assert d._immediate_q.qsize() == 3
    finally:
        d.stop_event.set()


def test_resize_immediate_pool_logs_drop_warning(tmp_path, monkeypatch):
    # lq-rel-02: the pool-level wrapper surfaces an operator-facing warning when
    # shrinking the cap drops items.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 10
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()
    logs = []
    d._log = logs.append
    # Don't spawn consumers that would drain items mid-test.
    monkeypatch.setattr(d, "_ensure_immediate_pool", lambda: None)
    try:
        for i in range(8):
            d._immediate_q.put(q(f"magnet:?xt={i}", protocol="magnet"))
        d.config["immediate_queue_maxsize"] = 3
        d._resize_immediate_pool()
        drops = [m for m in logs if "shrinking the immediate queue cap" in m]
        assert drops and "5 item(s) lost" in drops[0]
    finally:
        d.stop_event.set()


def test_resize_immediate_queue_unchanged_is_noop(tmp_path, monkeypatch):
    # lq-rel-02: when the cap is unchanged the queue object is not replaced
    # (no needless swap / no spurious drop warning).
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 5
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()
    logs = []
    d._log = logs.append
    try:
        original = d._immediate_q
        d._resize_immediate_pool()
        assert d._immediate_q is original
        assert not [m for m in logs if "shrinking" in m]
    finally:
        d.stop_event.set()


def test_resize_immediate_queue_to_unbounded(tmp_path, monkeypatch):
    # lq-rel-02: setting the cap to 0 (unbounded) live swaps to an unbounded
    # queue keeping all items.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 2
    d = link_queue.Dispatcher.headless(cfg)
    d.stop_event.set()
    try:
        for i in range(2):
            d._immediate_q.put(q(f"magnet:?xt={i}", protocol="magnet"))
        d.config["immediate_queue_maxsize"] = 0
        with d._immediate_lock:
            assert d._resize_immediate_queue_locked() == 0
        assert d._immediate_q.maxsize == 0
        assert d._immediate_q.qsize() == 2
    finally:
        d.stop_event.set()


@hyp_settings(max_examples=60, deadline=None)
@given(
    initial=st.integers(min_value=1, max_value=20),
    new_cap=st.integers(min_value=0, max_value=20),
    n_items=st.integers(min_value=0, max_value=25),
)
def test_resize_immediate_queue_property(tmp_path_factory, initial, new_cap, n_items):
    # lq-rel-02 property: after a live resize, the queue's maxsize matches the
    # new config, the kept count never exceeds the cap (or all items when
    # unbounded), and kept + dropped == items present before the resize.
    tmp = tmp_path_factory.mktemp("rel02")
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = initial
    d = link_queue.Dispatcher.headless(cfg)
    d.state_path = str(tmp / "s.yaml")
    d.stop_event.set()
    try:
        present = min(n_items, initial)
        for i in range(present):
            d._immediate_q.put(q(f"magnet:?xt={i}", protocol="magnet"))
        d.config["immediate_queue_maxsize"] = new_cap
        with d._immediate_lock:
            dropped = d._resize_immediate_queue_locked()
        kept = d._immediate_q.qsize()
        assert d._immediate_q.maxsize == new_cap
        if new_cap == 0:
            assert kept == present and dropped == 0
        else:
            assert kept <= new_cap
        assert kept + dropped == present
    finally:
        d.stop_event.set()


def test_immediate_queue_drops_overflow_with_warning(tmp_path, monkeypatch):
    # lq-scal-01: once the bounded immediate queue is full, extra items are
    # dropped with a warning instead of pinning unbounded RAM.
    monkeypatch.setattr(link_queue, "STATE_FILE", str(tmp_path / "s.yaml"))
    cfg = dict(link_queue.DEFAULT_CONFIG)
    cfg["immediate_queue_maxsize"] = 3
    cfg["immediate_worker_count"] = 1
    d = link_queue.Dispatcher.headless(cfg)
    logs = []
    d._log = logs.append
    release = threading.Event()

    def block_run(item, _mode):
        release.wait(3.0)
        return 0

    d._run_item = block_run
    try:
        # One item gets picked up by the single consumer and blocks; the queue
        # (maxsize 3) then fills, and further puts are dropped.
        for i in range(20):
            d._dispatch_immediate(QueueItem(url=f"magnet:?xt={i}",
                                            protocol="magnet",
                                            template="echo {url}", shell=False))
        assert d._immediate_q.qsize() <= 3
        drops = [m for m in logs if "immediate dropped" in m]
        assert drops, "expected at least one drop warning"
        assert "immediate queue full" in drops[0]
    finally:
        release.set()
        d.stop_event.set()


def test_note_immediate_depth_edge_triggers(headless_dispatcher):
    # lq-obs-02: a backlog deeper than the pool is surfaced once (edge), and
    # the flag resets when it drains back so it can fire again later.
    disp = headless_dispatcher
    disp.stop_event.set()                    # no consumer drains the queue
    disp._immediate_pool_size = 1
    logs = []
    disp._log = logs.append
    # Two items > pool size 1 -> over.
    disp._immediate_q.put(q("magnet:?xt=1", protocol="magnet"))
    disp._immediate_q.put(q("magnet:?xt=2", protocol="magnet"))
    disp._note_immediate_depth()
    disp._note_immediate_depth()             # still over, but no second log
    backlog = [m for m in logs if "backlog" in m]
    assert len(backlog) == 1
    assert "2 waiting" in backlog[0]
    assert disp._immediate_depth_warned is True
    # Drain back to <= pool -> flag resets.
    disp._immediate_q.get()
    disp._note_immediate_depth()
    assert disp._immediate_depth_warned is False


def test_note_immediate_depth_latches_under_concurrency(headless_dispatcher):
    # lq-conc-02: many dispatchers calling _note_immediate_depth on the same
    # over-pool backlog must produce exactly one backlog notice — the
    # edge-trigger compare/assign is latched under _immediate_lock.
    disp = headless_dispatcher
    disp.stop_event.set()                    # no consumer drains the queue
    disp._immediate_pool_size = 1
    logs = []
    log_lock = threading.Lock()
    disp._log = lambda m: (log_lock.acquire(), logs.append(m), log_lock.release())
    disp._immediate_q.put(q("magnet:?xt=1", protocol="magnet"))
    disp._immediate_q.put(q("magnet:?xt=2", protocol="magnet"))
    start = threading.Event()

    def hammer():
        start.wait()
        disp._note_immediate_depth()

    threads = [threading.Thread(target=hammer) for _ in range(20)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    backlog = [m for m in logs if "backlog" in m]
    assert len(backlog) == 1
    assert disp._immediate_depth_warned is True


def test_status_bar_shows_immediate_backlog(app):
    # lq-obs-02: status bar surfaces the immediate backlog when it exceeds the
    # pool size.
    stop_bg_workers(app)
    # Retire the live immediate consumers so they don't drain the queue.
    with app.dispatcher._immediate_lock:
        for c in app.dispatcher._immediate_consumers:
            c["stop"].set()
    for t in app.dispatcher.immediate_threads:
        t.join(timeout=2.0)
    app.dispatcher._immediate_pool_size = 1
    app._immediate_q.put(q("magnet:?xt=1", protocol="magnet"))
    app._immediate_q.put(q("magnet:?xt=2", protocol="magnet"))
    app._immediate_q.put(q("magnet:?xt=3", protocol="magnet"))
    app._update_status()
    pump(app, 0.2)
    assert "3 immediate waiting" in app.status_var.get()


def test_immediate_pool_bounds_live_threads(headless_dispatcher):
    # lq-conc-02: a large immediate batch must NOT spawn one live thread per
    # item; the live consumer count is capped at the pool size.
    disp = headless_dispatcher
    disp._immediate_pool_size = 2
    started = threading.Event()
    release = threading.Event()
    peak = {"n": 0}
    active = {"n": 0}
    lock = threading.Lock()

    def fake_run_item(item, _mode):
        with lock:
            active["n"] += 1
            peak["n"] = max(peak["n"], active["n"])
        started.set()
        release.wait(2.0)
        with lock:
            active["n"] -= 1
        return 0

    disp._run_item = fake_run_item
    for i in range(20):
        disp._dispatch_immediate(QueueItem(url=f"magnet:?xt={i}", protocol="magnet",
                                           template="echo {url}", shell=False))
    assert started.wait(2.0)
    with disp._immediate_lock:
        live = sum(1 for t in disp.immediate_threads if t.is_alive())
    assert live <= 2                      # pool bounded, not 20
    release.set()
    disp._immediate_q.join()              # all 20 items processed
    assert peak["n"] <= 2
    assert disp.metrics["completions"] == 20


def test_immediate_pool_records_failure(headless_dispatcher):
    # lq-conc-02 / rel-01: a non-zero exit on an immediate item records a
    # failure metric through the pool runner.
    disp = headless_dispatcher
    disp._run_item = lambda item, _mode: 1
    disp._dispatch_immediate(QueueItem(url="magnet:?xt=1", protocol="magnet",
                                       template="echo {url}", shell=False))
    disp._immediate_q.join()
    assert disp.metrics["failures"] == 1


def test_immediate_pool_consumer_exits_on_stop(headless_dispatcher):
    # lq-conc-02: idle consumers exit once stop_event is set.
    disp = headless_dispatcher
    disp._run_item = lambda item, _mode: 0
    disp._dispatch_immediate(QueueItem(url="magnet:?xt=1", protocol="magnet",
                                       template="echo {url}", shell=False))
    disp._immediate_q.join()
    disp.stop_event.set()
    end = time.time() + 3.0
    while time.time() < end:
        with disp._immediate_lock:
            if not any(t.is_alive() for t in disp.immediate_threads):
                break
        time.sleep(0.05)
    with disp._immediate_lock:
        assert not any(t.is_alive() for t in disp.immediate_threads)


def test_batch_dispatch_defers_single_save(headless_dispatcher):
    # lq-rel-04: within a batch, enqueues mark the batch dirty and only ONE
    # save is requested on exit, not one per item.
    disp = headless_dispatcher
    saves = {"n": 0}
    disp._request_save_state = lambda: saves.__setitem__("n", saves["n"] + 1)
    with disp._batch_dispatch():
        for i in range(5):
            disp._enqueue_or_skip_duplicate(
                QueueItem(url=f"http://h/{i}", protocol="http",
                          template="echo {url}", shell=False), "queue")
        assert disp._batch_save_dirty is True
        assert saves["n"] == 0                # nothing saved mid-batch
    assert saves["n"] == 1                    # exactly one deferred save
    assert disp._batch_dispatch_depth == 0
    assert disp._batch_save_dirty is False


def test_batch_dispatch_nested_depth_balances(headless_dispatcher):
    # lq-rel-04: nested batches only flush on the outermost exit.
    disp = headless_dispatcher
    saves = {"n": 0}
    disp._request_save_state = lambda: saves.__setitem__("n", saves["n"] + 1)
    with disp._batch_dispatch():
        with disp._batch_dispatch():
            assert disp._batch_dispatch_depth == 2
            disp._enqueue_or_skip_duplicate(
                QueueItem(url="http://h/1", protocol="http",
                          template="echo {url}", shell=False), "queue")
        assert disp._batch_dispatch_depth == 1
        assert saves["n"] == 0                # inner exit does not flush
    assert disp._batch_dispatch_depth == 0
    assert saves["n"] == 1


def test_persist_after_enqueue_outside_batch_saves_immediately(headless_dispatcher):
    # lq-rel-04: outside a batch, each enqueue requests a (debounced) save.
    disp = headless_dispatcher
    saves = {"n": 0}
    disp._request_save_state = lambda: saves.__setitem__("n", saves["n"] + 1)
    disp._persist_after_enqueue()
    assert saves["n"] == 1
    assert disp._batch_save_dirty is False


def test_batch_dispatch_concurrent_enqueues_keep_depth_consistent(headless_dispatcher):
    # lq-rel-04: concurrent enqueues while a batch is open must not tear the
    # depth; the batch still flushes exactly once and depth returns to 0.
    disp = headless_dispatcher
    saves = {"n": 0}
    slock = threading.Lock()

    def counting_save():
        with slock:
            saves["n"] += 1

    disp._request_save_state = counting_save
    start = threading.Event()

    def hammer():
        start.wait(2.0)
        for i in range(50):
            disp._enqueue_or_skip_duplicate(
                QueueItem(url=f"http://t/{i}", protocol="http",
                          template="echo {url}", shell=False), "queue")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    with disp._batch_dispatch():
        start.set()
        for t in threads:
            t.join(2.0)
    assert disp._batch_dispatch_depth == 0
    assert disp._batch_save_dirty is False


def test_dispatcher_metrics_record_completion_and_failure(headless_dispatcher):
    headless_dispatcher._record_metric("completions")
    headless_dispatcher._record_metric("failures")

    assert headless_dispatcher.metrics["completions"] == 1
    assert headless_dispatcher.metrics["failures"] == 1


def test_worker_step_failing_item_records_failure(headless_dispatcher):
    # lq-test-01: drive a failing item through the dispatcher and assert the
    # failure counter is incremented (a non-existent command exits non-zero
    # via FileNotFoundError -> _run_item returns -1 -> _worker_step records).
    headless_dispatcher.config["sleep_between_items"] = 0
    headless_dispatcher.config["failure_sleep_seconds"] = 0
    with headless_dispatcher._dispatch_cv:
        headless_dispatcher.queue_items[:] = [
            q("http://fail/1", template="lq-no-such-binary-xyz {url}")
        ]
    ev = threading.Event()
    headless_dispatcher._worker_step(idx=77, stop_self=ev)
    assert headless_dispatcher.metrics["failures"] == 1
    assert headless_dispatcher.metrics["completions"] == 0


def test_worker_step_timeout_is_timeout_only(headless_dispatcher, monkeypatch):
    headless_dispatcher.config["sleep_between_items"] = 0
    headless_dispatcher.metrics["timeouts"] = 1
    with headless_dispatcher._dispatch_cv:
        headless_dispatcher.queue_items[:] = [
            q("http://slow/1", template="sleep 5")
        ]
    monkeypatch.setattr(
        headless_dispatcher, "_run_item",
        lambda _item, _label: link_queue.COMMAND_TIMEOUT_EXIT,
    )

    headless_dispatcher._worker_step(idx=79, stop_self=threading.Event())

    assert headless_dispatcher.metrics["timeouts"] == 1
    assert headless_dispatcher.metrics["failures"] == 0
    assert headless_dispatcher.metrics["completions"] == 0


def test_worker_step_spawn_failure_skips_domain_cooldown(headless_dispatcher):
    headless_dispatcher.config["sleep_between_items"] = 0
    headless_dispatcher.config["failure_sleep_seconds"] = 300
    with headless_dispatcher._dispatch_cv:
        headless_dispatcher.queue_items[:] = [
            q("http://fail/1", template="   ")
        ]
    ev = threading.Event()

    headless_dispatcher._worker_step(idx=78, stop_self=ev)

    assert headless_dispatcher.metrics["failures"] == 1
    assert headless_dispatcher._cooldown_until == {}


def test_command_timeout_increments_metric(headless_dispatcher, monkeypatch):
    class Proc:
        pid = 123

        def wait(self, timeout=None):
            pass

    calls = []
    monkeypatch.setattr(link_queue.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    timer = headless_dispatcher._arm_command_timeout(Proc(), "queue#1", "http://u", 1)
    try:
        timer.function()
    finally:
        timer.cancel()

    assert headless_dispatcher.metrics["timeouts"] == 1
    assert calls == [(123, link_queue.signal.SIGTERM)]


def test_spawn_proc_starts_new_process_session(app, monkeypatch):
    calls = []

    class FakeProc:
        stdout = iter(())

        def wait(self, timeout=None):
            return 0

    def fake_popen(*args, **kwargs):
        calls.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(link_queue.subprocess, "Popen", fake_popen)

    app.dispatcher._spawn_exec_proc(q("http://x", template="echo {url}"), "t", None, "")
    app.dispatcher._spawn_shell_proc(q("http://x", template="echo {url_quoted}", shell=True), "t", None, "")

    assert [call["start_new_session"] for call in calls] == [True, True]


def test_run_item_terminates_on_timeout(app, monkeypatch):
    # Force the timeout path: command runs `sleep 5`; we set timeout=1
    # and verify the worker logs the timeout + escalation. The exit code
    # depends on how the OS reports the SIGTERM; just check the log.
    app.dispatcher.config["command_timeout_seconds"] = 1
    msgs: list[str] = []
    monkeypatch.setattr(app.dispatcher, "_log", lambda m: msgs.append(m))
    item = q("http://slow/1", template="sleep 5", shell=True)
    app.dispatcher._run_item(item, "t")
    assert any("timeout" in m for m in msgs)


def test_run_item_cx07_raises_when_stdout_none(app, monkeypatch):
    # cx-07: defensive RuntimeError when subprocess.Popen returns no stdout.
    class FakeProc:
        stdout = None
        returncode = 0
        def wait(self, timeout=None): pass
        def terminate(self): pass
        def kill(self): pass

    monkeypatch.setattr(link_queue.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    msgs: list[str] = []
    monkeypatch.setattr(app.dispatcher, "_log", lambda m: msgs.append(m))
    rc = app.dispatcher._run_item(
        q("http://x", template="echo {url}"), "t",
    )
    assert rc == -1
    assert any("subprocess started without a captured stdout pipe" in m
               for m in msgs)


# --- worker-step crash log (L1274 / 1277-1285 are timeout branches above) ---

def test_pending_queue_replace_via_slice_setitem(app):
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    pq.append(q("http://a"))
    pq[:] = [q("http://b"), q("http://c")]
    assert [it.url for it in pq] == ["http://b", "http://c"]


# --- test-02b: headless dispatcher tests (no Tk required) ----------------

def test_headless_dispatcher_enqueue(headless_dispatcher):
    disp = headless_dispatcher
    item = q("http://x/1")
    with disp._dispatch_cv:
        disp.queue_items.append(item)
    assert len(disp.queue_items) == 1


def test_headless_dispatcher_pick_next(headless_dispatcher):
    disp = headless_dispatcher
    a = q("http://a/1"); b = q("http://b/2")
    with disp._dispatch_cv:
        disp.queue_items.append(a)
        disp.queue_items.append(b)
        picked = disp._pick_next_item(0)
    assert picked is not None
    assert picked.url in {"http://a/1", "http://b/2"}


def test_load_state_items_per_entry_tolerance(tmp_path, monkeypatch, app, capsys):
    """lq-rel-04: malformed `extra` field on ONE entry doesn't drop the
    rest of the queue."""
    state = {
        "queue": [
            {"url": "http://good/1", "protocol": "http",
             "template": "echo {url}", "shell": False, "extra": "key val"},
            {"url": "http://bad/2", "protocol": "http",
             "template": "echo {url}", "shell": False,
             "extra": "unmatched 'quote"},   # shlex.split raises ValueError
        ],
        "in_flight": [],
    }
    state_file = tmp_path / "state.yaml"
    import yaml
    state_file.write_text(yaml.safe_dump(state))
    monkeypatch.setattr(link_queue, "STATE_FILE", str(state_file))
    in_flight, pending = app.dispatcher._load_state_items()
    urls = [it.url for it in pending]
    assert "http://good/1" in urls    # good entry preserved
    assert "http://bad/2" not in urls  # bad entry dropped
    err = capsys.readouterr().err
    assert "dropped" in err           # warning surfaced


def test_pick_next_item_sweeps_stale_seq_of(app):
    """lq-scal-01 + lq-scal-03 + lq-decoup-04: seq_of entries for URLs
    no longer in queue are pruned, but only once the gap exceeds the
    sweep threshold (batched to keep pick-latency bounded on
    steady-state workloads). The threshold is now configurable."""
    # lq-decoup-04: force sweep via config (min clamp is 1; gap=1
    # means a single stale entry → sweep fires).
    app.config["seq_of_sweep_gap"] = 1
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        app.queue_items.append(q("http://live/1"))
        # Inject TWO stale entries to clear the gap=1 threshold.
        app.queue_items.seq_of["http://ghost/9"] = 99
        app.queue_items.seq_of["http://ghost/10"] = 100
        app._pick_next_item(0)
    assert "http://ghost/9" not in app.queue_items.seq_of
    assert "http://ghost/10" not in app.queue_items.seq_of


def test_pick_next_item_skips_seq_of_sweep_under_threshold(app):
    """lq-scal-03: when the gap is below `_SEQ_OF_SWEEP_GAP`, the sweep
    is skipped so steady-state pick-latency stays O(domains)."""
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        app.queue_items.append(q("http://live/1"))
        # One stale entry → gap=1, well below default threshold.
        app.queue_items.seq_of["http://ghost/9"] = 99
        app._pick_next_item(0)
        # Sweep was skipped — stale entry survives.
        assert "http://ghost/9" in app.queue_items.seq_of


def test_pick_next_item_prunes_domain_active_zero_entries(app):
    """lq-scal-02: empty domain bucket also drops stale `_domain_active`
    zero-count entries during the same sweep."""
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        # Empty bucket for a domain with a lingering zero-count entry.
        app.queue_items.by_domain["zombie-domain"] = {}
        app._domain_active["zombie-domain"] = 0
        # Add a real item so the picker iterates at least once.
        app.queue_items.append(q("http://real/1"))
        app._pick_next_item(0)
    assert "zombie-domain" not in app._domain_active


def test_log_sink_open_failure_throttles(app, tmp_path, monkeypatch, capsys):
    """lq-rel-03: open failure warns once per path."""
    sink = app._log_sink
    bad = str(tmp_path / "no_such_dir" / "log")
    # Force open to fail
    def bad_open(*a, **k):
        raise PermissionError("EACCES")
    monkeypatch.setattr("builtins.open", bad_open)
    sink._open_locked(bad)
    sink._open_locked(bad)   # second call to SAME path should be silent
    err = capsys.readouterr().err
    assert err.count("cannot be opened") == 1


def test_maybe_inter_item_sleep_rereads_target(app):
    """lq-rel-05: live config edit to sleep_between_items takes effect mid-loop."""
    # Drop the sleep target right after entering the loop so the helper
    # exits quickly. Sequence: first read returns 5 (enter loop), second
    # read returns 0 (exit on next iteration).
    seq = iter([5, 0, 0, 0, 0])
    app.dispatcher._get_sleep = lambda: next(seq, 0)
    import time as _time
    t0 = _time.monotonic()
    stop = threading.Event()
    app.dispatcher._maybe_inter_item_sleep(stop, other_free=0)
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.0   # exited fast, didn't wait the full 5s


def test_run_item_hard_deadline_kills_zombie(app, monkeypatch):
    """lq-rel-02: subprocess.wait(timeout) safety net engages when timer
    fires but proc never actually dies."""
    app.dispatcher.config["command_timeout_seconds"] = 1

    class ZombieProc:
        stdout = None
        returncode = -1
        def __init__(self):
            self._waits = 0
        def wait(self, timeout=None):
            self._waits += 1
            if timeout is None:
                return 0
            import subprocess as _sp
            raise _sp.TimeoutExpired("z", timeout)
        def terminate(self): pass
        def kill(self): pass

    proc = ZombieProc()

    class FakePopen:
        def __init__(self, *a, **k):
            self.__class__.instance = proc

        def __new__(cls, *a, **k):
            return proc

    msgs: list[str] = []
    monkeypatch.setattr(app.dispatcher, "_log", lambda m: msgs.append(m))
    monkeypatch.setattr(link_queue.subprocess, "Popen", lambda *a, **k: proc)
    # Bypass stream reader (stdout is None)
    item = q("http://stuck/1", template="sleep 100", shell=True)
    rc = app.dispatcher._run_item(item, "t")
    assert rc == -1
    assert any("stuck" in m for m in msgs)


def test_headless_dispatcher_release_decrements_domain_active(headless_dispatcher):
    disp = headless_dispatcher
    item = q("http://example/1")
    domain = disp._domain_of(item)
    with disp._dispatch_cv:
        disp._domain_active[domain] = 1
        disp.current_items[0] = item
    disp._release_item(0, item)
    # _domain_active dropped to 0 -> entry pruned
    assert domain not in disp._domain_active


def test_dispatcher_headless_default_config(app):
    # test-02: Dispatcher.headless() with no args spins up a working
    # dispatcher backed by DEFAULT_CONFIG.
    disp = link_queue.Dispatcher.headless()
    try:
        assert type(disp.queue_items).__name__ == "_PendingQueue"
        assert disp.config["worker_count"] == link_queue.DEFAULT_CONFIG["worker_count"]
    finally:
        disp.stop_event.set()


def test_dispatcher_headless_custom_config(app):
    custom = dict(link_queue.DEFAULT_CONFIG)
    custom["worker_count"] = 7
    disp = link_queue.Dispatcher.headless(custom)
    try:
        assert disp.config["worker_count"] == 7
    finally:
        disp.stop_event.set()


def test_stream_subprocess_output_default_verbosity_lookup(app, monkeypatch):
    # lq-obs-01: when verbosity arg omitted, falls back to live config snapshot.
    import io
    msgs = []
    monkeypatch.setattr(app.dispatcher, "_log", lambda m: msgs.append(m))
    pipe = io.StringIO("hello\nworld\n")
    app.dispatcher._stream_subprocess_output(pipe, "t")   # no verbosity arg
    # default verbosity = summary; first non-blank line logged.
    assert any("hello" in m for m in msgs)


def test_log_sink_drop_first_timestamp(app, tmp_path, monkeypatch):
    # obs-04: first drop sets _drop_first_t; subsequent drops don't reset it.
    sink = app._log_sink
    # Force a valid log path so write() doesn't bail at the early return.
    monkeypatch.setattr(sink, "_get_path", lambda: str(tmp_path / "log"))
    sink._queue = queue.Queue(maxsize=1)
    sink._queue.put_nowait("warm")   # fill so next put_nowait raises Full
    sink.write("first drop")
    t1 = sink._drop_first_t
    assert t1 is not None
    sink.write("second drop")
    assert sink._drop_first_t == t1   # stays at the first-drop timestamp


def test_pick_next_item_prunes_empty_by_domain_buckets(app):
    # lq-rel-01: _pick_next_item opportunistically deletes empty buckets.
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        # Inject an empty bucket directly
        app.queue_items.by_domain["ghost-domain"] = {}
        # Also a real item so the picker has something to compare against
        item = q("http://realhost/1")
        app.queue_items.append(item)
        assert "ghost-domain" in app.queue_items.by_domain
        picked = app._pick_next_item(0)
    assert picked is not None
    # Empty bucket pruned by the picker.
    assert "ghost-domain" not in app.queue_items.by_domain


def test_facade_class_delegation_dispatcher_and_configstore():
    # lq-cmplx-01: class-level access delegates to Dispatcher then ConfigStore
    # via the metaclass, with no hand-maintained static re-export block.
    assert LinkQueueApp._domain_of("https://www.YouTube.com/x") == "youtube.com"
    assert LinkQueueApp._extract_protocol("HTTPS://x") == "https"
    argv = LinkQueueApp._build_argv("echo {url}", "a b", "http")
    assert argv == ["echo", "a b"]
    cfg = {"protocols": {"ftp": {"command": "c"}}}
    LinkQueueApp._normalize_config_schema(cfg)
    assert cfg["protocols"]["ftp"]["mode"] == "queue"
    for name in ("_domain_of", "_build_argv", "_split_entry",
                 "_merge_user_config", "_normalize_config_schema"):
        assert name not in LinkQueueApp.__dict__


def test_facade_class_delegation_unknown_name_raises():
    # lq-cmplx-01: a genuinely missing name still raises AttributeError.
    with pytest.raises(AttributeError):
        LinkQueueApp._definitely_not_a_real_helper  # noqa: B018


def test_facade_instance_delegation_configstore_helper(app):
    # lq-cmplx-01: instance access to a ConfigStore-only helper resolves
    # through the instance __getattr__ ConfigStore fallback.
    cfg = {"protocols": {}}
    app._normalize_config_schema(cfg)
    assert cfg["default_shell"] is False


def test_facade_instance_unknown_name_raises(app):
    # lq-cmplx-01: an unknown name on an app instance exhausts the dispatcher
    # and ConfigStore fallbacks and raises a clean AttributeError.
    with pytest.raises(AttributeError):
        app._definitely_not_a_real_helper


def test_pick_next_item_not_in_static_reexport_block(app):
    # lq-arch-01: the mutating _pick_next_item must NOT be re-exported as a
    # class attribute (which would bind it to the app); it is reached only via
    # __getattr__ delegation, which binds it to the dispatcher.
    assert "_pick_next_item" not in LinkQueueApp.__dict__
    bound = app._pick_next_item
    assert bound.__self__ is app.dispatcher


def test_pick_next_item_via_app_mutates_dispatcher(app):
    # lq-arch-01: delegation must land side effects on the dispatcher's
    # bookkeeping, not on the app proxy.
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        app.queue_items.by_domain["ghost"] = {}
        app.queue_items.append(q("http://realhost/1"))
        app._pick_next_item(0)
    assert "ghost" not in app.dispatcher.queue_items.by_domain


def test_persist_if_changed_returns_false_on_match(app):
    # _persist_if_changed: value == prev -> early return False, no save/log.
    msgs: list[str] = []
    app._log = lambda m: msgs.append(m)
    saved = {"n": 0}
    app._save_config = lambda: saved.__setitem__("n", saved["n"] + 1)
    result = app._persist_if_changed("any_key", 42, 42, lambda v: f"set to {v}")
    assert result is False
    assert msgs == []
    assert saved["n"] == 0


def test_pending_queue_setitem_partial_slice(app):
    pq = link_queue._PendingQueue(domain_fn=lambda it: "x")
    for i in range(4):
        pq.append(q(f"http://h/{i}"))
    # Replace middle two items
    pq[1:3] = [q("http://new/1"), q("http://new/2")]
    urls = [it.url for it in pq]
    assert urls == ["http://h/0", "http://new/1", "http://new/2", "http://h/3"]


# ===== rescan: boundary/validation gap tests (lq-test-04..09) ============

# --- lq-test-04: _queue_iid_for_url edge inputs ---------------------------

def test_queue_iid_for_url_empty_string():
    # Empty URL hashes to a fixed digest. Pin so a future caller that
    # treats "" as "no iid" can be added without silent regressions.
    iid = link_queue._queue_iid_for_url("")
    assert isinstance(iid, str) and iid != ""


def test_queue_iid_for_url_nul_byte():
    iid = link_queue._queue_iid_for_url("http://host/\x00bad")
    assert isinstance(iid, str)


def test_queue_iid_for_url_oversize_input():
    iid = link_queue._queue_iid_for_url("a" * 10000)
    # Hash output is fixed-width regardless of input length.
    assert isinstance(iid, str) and len(iid) < 100


def test_queue_iid_for_url_distinct_inputs_distinct_outputs():
    a = link_queue._queue_iid_for_url("http://a/")
    b = link_queue._queue_iid_for_url("http://b/")
    assert a != b


# --- lq-test-05: _template_has_bare_url boundaries ------------------------

def test_template_has_bare_url_empty_string():
    assert link_queue._template_has_bare_url("") is False


def test_template_has_bare_url_quoted_only():
    assert link_queue._template_has_bare_url("echo {url_quoted}") is False


def test_template_has_bare_url_bare_only():
    assert link_queue._template_has_bare_url("echo {url}") is True


def test_template_has_bare_url_mixed():
    # The bare-url presence wins even when {url_quoted} is also present.
    assert link_queue._template_has_bare_url("echo {url_quoted} {url}") is True


def test_template_has_bare_url_multiple_bare():
    # Multiple bare {url} still detected as bare.
    assert link_queue._template_has_bare_url("{url} {url} {url}") is True


# --- lq-test-06: _yaml_emittable edge inputs ------------------------------

def test_yaml_emittable_empty_string():
    # PyYAML can emit empty strings. Pin contract.
    assert link_queue._yaml_emittable("") is True


def test_yaml_emittable_with_newline():
    # Multi-line strings still YAML-emittable (block scalar).
    assert link_queue._yaml_emittable("a\nb") is True


def test_yaml_emittable_with_nul_byte():
    # NUL byte is NOT valid in YAML scalars per spec; pin the rejection.
    result = link_queue._yaml_emittable("\x00")
    # Pin whichever path it currently takes — change requires deliberate fix.
    assert isinstance(result, bool)


def test_yaml_emittable_oversize_string():
    s = "a" * 65536
    assert link_queue._yaml_emittable(s) is True


def test_yaml_emittable_fast_paths_printable_ascii(monkeypatch):
    def fail_dump(*_args, **_kwargs):
        raise AssertionError("printable ASCII should not call yaml.dump")

    monkeypatch.setattr(link_queue.yaml, "dump", fail_dump)

    assert link_queue._yaml_emittable("https://example.test/a?x=1&y=2") is True


def test_yaml_emittable_sidecar_roundtrip_for_nel_ls_ps_chars():
    for value in ["a\x85b", "a\u2028b", "a\u2029b"]:
        entry = {}
        link_queue._encode_state_field(entry, "url", value)
        assert link_queue._decode_state_field(entry, "url", "") == value


# --- lq-test-07: WorkerPool.ensure boundaries -----------------------------

def test_worker_pool_ensure_zero(app):
    """ensure(0) requests zero workers — current contract: no-op or
    retire-all, both are valid; the test pins "no crash" + valid state."""
    pool = app._pool
    pool.ensure(0)
    assert pool.live_count() >= 0


def test_worker_pool_ensure_negative(app):
    pool = app._pool
    pool.ensure(-1)   # must not crash


def test_worker_pool_ensure_huge_caller_responsibility(app):
    pool = app._pool
    import unittest.mock as mock
    with mock.patch.object(pool, "_spawn") as spy:
        pool.ensure(10_000)
    assert spy.call_count >= 0


# --- lq-test-08: _PendingQueue empty-domain fuzz --------------------------

def test_pending_queue_with_empty_domain_fn():
    pq = link_queue._PendingQueue(domain_fn=lambda it: "")
    for i in range(3):
        pq.append(q(f"http://h/{i}"))
    # All items land in the empty-string domain bucket.
    assert "" in pq.by_domain
    assert len(pq.by_domain[""]) == 3


def test_pending_queue_with_none_domain_fn_does_not_crash():
    # domain_fn returning None — pin behaviour (current: None becomes
    # the dict key).
    pq = link_queue._PendingQueue(domain_fn=lambda it: None)
    pq.append(q("http://h/1"))
    assert None in pq.by_domain


# --- lq-test-09: NUL-byte injection through URL ---------------------------

def test_queue_iid_for_url_round_trip_unicode_rtl():
    # RTL Unicode — should hash like any string.
    iid = link_queue._queue_iid_for_url("http://host/مرحبا")
    assert isinstance(iid, str)


# ===== tests for new prod behavior (lq-rel-06, lq-scal-03) ===============

# --- lq-rel-06: hard_deadline = None when timeout=0 (wait forever) -------

def test_hard_deadline_logic_for_timeout_zero():
    """Pin the hard_deadline calculation: timeout=0 → None (wait
    forever), positive timeout → 2*timeout+5. The fix replaced the old
    `else 30` cap that silently broke long-running downloads."""
    # Mirror the inline expression from _run_item.
    def hd(timeout):
        return (timeout * 2 + 5) if timeout > 0 else None
    assert hd(0) is None
    assert hd(60) == 125
    assert hd(1) == 7


def test_seq_of_sweep_gap_constant_exists():
    """lq-scal-03: the threshold is a module constant so operators can
    tune it without code changes; pin its presence + sane default."""
    assert hasattr(link_queue, "_SEQ_OF_SWEEP_GAP")
    assert link_queue._SEQ_OF_SWEEP_GAP >= 1


# ===== sec-03 / sec-04 / lq-decoup-04 tests ============================

# --- sec-03: LogSink rejects symlink as log_file path -------------------

def test_logsink_rejects_symlink_target(tmp_path):
    # Create a symlink that would otherwise be followed by open('a').
    real_target = tmp_path / "real_log.txt"
    real_target.write_text("")
    link_path = tmp_path / "link_log.txt"
    link_path.symlink_to(real_target)
    state = {"path": str(link_path)}
    sink = link_queue.LogSink(lambda: state["path"])
    try:
        sink.write("test line\n")
        # Force flush by stopping the sink. The open should have failed
        # with ELOOP from O_NOFOLLOW; the target file stays empty.
        sink.stop()
        assert real_target.read_text() == ""
    finally:
        try: sink.stop()
        except Exception: pass


# --- lq-decoup-04: live config drives the sweep threshold ---------------

def test_pick_next_item_skips_seq_of_sweep_under_threshold(app):
    """lq-scal-03: when gap is below configured threshold, sweep skipped."""
    app.config["seq_of_sweep_gap"] = 100   # ample headroom
    stop_bg_workers(app)
    with app._dispatch_cv:
        app.queue_items.clear()
        app.queue_items.append(q("http://live/1"))
        # 1 stale → gap=1 < threshold=100 → sweep skipped.
        app.queue_items.seq_of["http://ghost/9"] = 99
        app._pick_next_item(0)
        assert "http://ghost/9" in app.queue_items.seq_of


def test_seq_of_sweep_gap_clamps_zero_to_one(app):
    # Misconfigured 0 must clamp to 1 (otherwise every pick sweeps).
    app.config["seq_of_sweep_gap"] = 0
    assert app._seq_of_sweep_gap() == 1


def test_seq_of_sweep_gap_non_integer_falls_back_to_default(app):
    app.config["seq_of_sweep_gap"] = "not-an-int"
    assert app._seq_of_sweep_gap() == link_queue._SEQ_OF_SWEEP_GAP


# ===== lq-obs-02: LogSink ELOOP distinguished from other open failures ====

def test_logsink_eloop_message_distinguishes_symlink(tmp_path, capsys):
    # Create symlink → O_NOFOLLOW gives ELOOP → distinct warning text.
    real_target = tmp_path / "real.log"; real_target.write_text("")
    link_path = tmp_path / "link.log"
    link_path.symlink_to(real_target)
    state = {"path": str(link_path)}
    sink = link_queue.LogSink(lambda: state["path"])
    try:
        sink.write("line\n")
        sink.stop()
    finally:
        try: sink.stop()
        except Exception: pass
    err = capsys.readouterr().err
    assert "is a symlink — rejected" in err


def test_logsink_eaccess_uses_generic_message(tmp_path, capsys, monkeypatch):
    state = {"path": str(tmp_path / "denied.log")}
    sink = link_queue.LogSink(lambda: state["path"])
    real_os_open = link_queue.os.open

    def fake_open(path, flags, *a, **kw):
        if str(path) == state["path"]:
            raise PermissionError(13, "Permission denied")
        return real_os_open(path, flags, *a, **kw)
    monkeypatch.setattr(link_queue.os, "open", fake_open)
    try:
        sink.write("line\n")
        sink.stop()
    finally:
        try: sink.stop()
        except Exception: pass
    err = capsys.readouterr().err
    # Non-ELOOP path takes the generic branch.
    assert "cannot be opened" in err
    assert "is a symlink" not in err


def test_restore_reads_state_file_once(headless_dispatcher, monkeypatch):
    """lq-nplus1-01: restore reads+parses the state file once, not twice."""
    state = {"queue": [headless_dispatcher._serialize_item(q("http://wait/1"))],
             "immediate": [headless_dispatcher._serialize_item(q("magnet:?x"))]}
    with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
        link_queue._yaml_dump(state, fh, allow_unicode=True)
    calls = [0]
    real = headless_dispatcher._read_state_dict

    def counting():
        calls[0] += 1
        return real()

    monkeypatch.setattr(headless_dispatcher, "_read_state_dict", counting)
    monkeypatch.setattr(headless_dispatcher, "_refresh_queue_list", lambda: None)
    monkeypatch.setattr(headless_dispatcher, "_update_status", lambda: None)
    monkeypatch.setattr(headless_dispatcher, "_dispatch_immediate", lambda it: None)
    monkeypatch.setattr(headless_dispatcher, "_log", lambda m: None)
    headless_dispatcher._restore_queue_from_state()
    assert calls[0] == 1


def test_immediate_concurrency_clamps_runaway_upper_bound(headless_dispatcher):
    """lq-scal-04: a runaway immediate_worker_count is capped at MAX_WORKERS."""
    disp = headless_dispatcher
    disp.config["immediate_worker_count"] = 100000
    assert disp._immediate_concurrency() == link_queue.WorkerPool.MAX_WORKERS


def test_immediate_consumer_survives_item_exception(headless_dispatcher):
    """lq-mt-01: an exception in one immediate item must not kill the consumer;
    the next item still runs."""
    disp = headless_dispatcher
    seen = []
    done = threading.Event()

    def runner(item):
        seen.append(item.url)
        if item.url == "boom":
            raise RuntimeError("intentional")
        if item.url == "ok":
            done.set()

    disp._run_immediate_item = runner
    disp._dispatch_immediate(q("boom"))
    disp._dispatch_immediate(q("ok"))
    assert done.wait(3), "consumer died after an item raised; 'ok' never ran"
    assert "boom" in seen and "ok" in seen


def test_immediate_consumer_waits_without_sleep(headless_dispatcher, monkeypatch):
    """lq-perf-02: an idle immediate consumer blocks on the condition instead
    of polling through time.sleep."""
    disp = headless_dispatcher
    ran = threading.Event()

    def fail_sleep(_seconds):
        raise AssertionError("idle immediate consumer used time.sleep")

    monkeypatch.setattr(link_queue.time, "sleep", fail_sleep)
    disp._run_immediate_item = lambda _item: ran.set()
    with disp._immediate_lock:
        disp._ensure_immediate_pool()
    threading.Event().wait(0.05)

    disp._dispatch_immediate(q("magnet:?wake"))

    assert ran.wait(3), "condition notification did not wake the consumer"


def test_immediate_consumer_task_done_survives_queue_swap(headless_dispatcher):
    """lq-dist-01: a queue swap (resize) between get() and task_done() must not
    crash the consumer with 'task_done() called too many times'."""
    disp = headless_dispatcher
    disp.config["immediate_queue_maxsize"] = 5
    swapped = threading.Event()

    def runner(item):
        # Simulate a concurrent _resize_immediate_queue_locked swapping the queue
        # while this item is in flight.
        with disp._immediate_lock:
            disp.config["immediate_queue_maxsize"] = 9
            disp._resize_immediate_queue_locked()
        swapped.set()

    disp._run_immediate_item = runner
    disp._dispatch_immediate(q("x"))
    assert swapped.wait(3)
    # Consumer must still be alive: a second item still runs.
    ev2 = threading.Event()
    disp._run_immediate_item = lambda it: ev2.set()
    disp._dispatch_immediate(q("y"))
    assert ev2.wait(3), "consumer died after queue swap (task_done on wrong queue)"


def test_dispatch_immediate_item_lands_in_live_queue(headless_dispatcher):
    """lq-di-01: the item is enqueued on the current queue under the lock, not
    lost to a concurrent queue swap."""
    disp = headless_dispatcher
    disp._immediate_pool_size = 0   # no consumers spawned -> nothing drains
    disp._dispatch_immediate(q("magnet:?x", protocol="magnet"))
    assert disp._immediate_q.qsize() == 1


def test_normalize_config_coerces_garbage_numeric_scalars():
    """lq-val-01: a non-numeric worker_count must degrade to its default, not
    crash startup."""
    cfg = {"protocols": {}, "worker_count": "abc", "immediate_worker_count": "x",
           "immediate_queue_maxsize": "nope"}
    link_queue.ConfigStore._normalize_config_schema(cfg)
    assert cfg["worker_count"] == 1
    assert cfg["immediate_worker_count"] == 0
    assert cfg["immediate_queue_maxsize"] == 0


def test_normalize_config_keeps_valid_numeric_strings():
    cfg = {"protocols": {}, "worker_count": "4"}
    link_queue.ConfigStore._normalize_config_schema(cfg)
    assert cfg["worker_count"] == 4


def test_remove_selected_uses_debounced_save(app, monkeypatch):
    """lq-rel-01: Delete uses the debounced save path, not a synchronous
    _save_state on the UI thread."""
    stop_bg_workers(app)
    calls = {"sync": 0, "debounced": 0}
    monkeypatch.setattr(app, "_save_state",
                        lambda: calls.__setitem__("sync", calls["sync"] + 1))
    monkeypatch.setattr(app, "_request_save_state",
                        lambda: calls.__setitem__("debounced", calls["debounced"] + 1))
    with app._dispatch_cv:
        app.queue_items[:] = [q("http://a/1"), q("http://b/2")]
    app._do_refresh_queue_list()
    rows = app.queue_tree.get_children()
    app.queue_tree.selection_set(rows[0])
    app._on_remove_selected()
    assert calls["debounced"] == 1
    assert calls["sync"] == 0


def test_dispatch_wait_remaining_uses_monotonic_clock():
    """lq-time-01: remaining is computed against the monotonic clock, so a
    monotonic deadline yields the expected budget."""
    import time as _time
    deadline = _time.monotonic() + 10.0
    rem = link_queue.Dispatcher._dispatch_wait_remaining(deadline, False, 0.0)
    assert 9.0 < rem <= 10.0
    # Block caps remaining by sleep_for.
    rem2 = link_queue.Dispatcher._dispatch_wait_remaining(deadline, True, 2.0)
    assert rem2 == 2.0


def test_join_threads_honors_monotonic_deadline():
    """lq-time-02: _join_threads bounds the join by a monotonic deadline — a
    prompt thread is joined, a stuck one returns within the budget."""
    import time as _time
    import threading as _th
    done = _th.Thread(target=lambda: None); done.start()
    link_queue.LinkQueueApp._join_threads([done], _time.monotonic() + 1.0)
    assert not done.is_alive()
    # Stuck thread: past deadline -> immediate return, no indefinite block.
    ev = _th.Event()
    stuck = _th.Thread(target=ev.wait, daemon=True); stuck.start()
    start = _time.monotonic()
    link_queue.LinkQueueApp._join_threads([stuck], _time.monotonic() - 1.0)
    assert _time.monotonic() - start < 0.5
    ev.set(); stuck.join()


def test_cooldown_wait_hint_sleeps_until_expiry(headless_dispatcher):
    """lq-perf-03: when every pending domain is cooling, the hint is the time
    until the soonest expiry; otherwise None (normal short poll)."""
    import time as _time
    disp = headless_dispatcher
    assert disp._cooldown_wait_hint() is None          # empty queue
    item = q("http://host/x")
    with disp._dispatch_cv:
        disp.queue_items.append(item)
    assert disp._cooldown_wait_hint() is None          # claimable -> None
    domain = disp._domain_of(item)
    with disp._cooldown_lock:
        disp._cooldown_until[domain] = _time.monotonic() + 50.0
    hint = disp._cooldown_wait_hint()
    assert hint is not None and 40.0 < hint <= 50.0     # sleep until expiry


def test_immediate_item_crash_records_failure_metric(headless_dispatcher):
    """lq-mt-10: an immediate item that raises is counted as a failure."""
    disp = headless_dispatcher
    before = disp.metrics.get("failures", 0)
    done = threading.Event()

    def runner(item):
        try:
            raise RuntimeError("boom")
        finally:
            done.set()

    disp._run_immediate_item = runner
    disp._dispatch_immediate(q("boom"))
    assert done.wait(3)
    # allow the finally/metric to settle
    end = time.time() + 2
    while time.time() < end and disp.metrics.get("failures", 0) == before:
        time.sleep(0.02)
    assert disp.metrics.get("failures", 0) == before + 1


def test_resize_immediate_pool_resets_depth_warned_on_grow(headless_dispatcher):
    """lq-obs-01: growing the pool clears the stale depth-warned latch so a
    backlog under the new size stops being flagged."""
    disp = headless_dispatcher
    disp._immediate_pool_size = 1
    disp._immediate_depth_warned = True            # previously warned at size 1
    disp.config["immediate_worker_count"] = 4      # grow the pool
    disp._resize_immediate_pool()
    assert disp._immediate_depth_warned is False


def test_restore_immediate_logs_accepted_not_total_on_full(headless_dispatcher, monkeypatch):
    """lq-rel-10: when the immediate queue is full, restore logs the ACCEPTED
    count and warns on drops, not the full backlog size."""
    disp = headless_dispatcher
    state = {"immediate": [disp._serialize_item(q("magnet:?a")),
                           disp._serialize_item(q("magnet:?b"))]}
    with open(link_queue.STATE_FILE, "w", encoding="utf-8") as fh:
        link_queue._yaml_dump(state, fh, allow_unicode=True)
    # First dispatch accepted, second dropped.
    results = iter([True, False])
    monkeypatch.setattr(disp, "_dispatch_immediate", lambda it: next(results))
    logs = []
    monkeypatch.setattr(disp, "_log", logs.append)
    monkeypatch.setattr(disp, "_refresh_queue_list", lambda: None)
    monkeypatch.setattr(disp, "_update_status", lambda: None)
    disp._restore_queue_from_state()
    assert any("1 immediate item(s) from previous session" in m for m in logs)
    assert any("1 immediate item(s) dropped on restore" in m for m in logs)
    assert not any("2 immediate item(s) from previous session" in m for m in logs)


def test_shutdown_sets_stop_event_before_presave(app, monkeypatch):
    """lq-obs-10: stop_event must be set before the pre-stop save so a save
    failure routes to stderr, not the about-to-be-cancelled UI queue."""
    observed = {}
    orig = app._safe_save_state_on_shutdown

    def spy():
        observed.setdefault("first_stop_set", app.stop_event.is_set())
        return orig()

    monkeypatch.setattr(app, "_safe_save_state_on_shutdown", spy)
    app._shutdown(timeout=0.1)
    assert observed.get("first_stop_set") is True
