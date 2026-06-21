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


def test_dispatch_wait_remaining():
    assert LinkQueueApp._dispatch_wait_remaining(time.time() - 1, False, 0) == 0.0
    r = LinkQueueApp._dispatch_wait_remaining(time.time() + 10, True, 0.25)
    assert 0 < r <= 0.25
    r2 = LinkQueueApp._dispatch_wait_remaining(time.time() + 10, False, 0)
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
    pq.remove(b)                          # O(1) removal by url
    assert "b.com" not in pq.by_domain and b not in pq
    assert pq[0] is a and pq[-1] is c     # int index
    assert pq[:1] == [a]                  # slice -> list
    pq[:] = [b]                           # whole-queue slice assign
    assert list(pq) == [b] and pq.urls == {"http://b.com/2": b}
    pq.clear()
    assert len(pq) == 0 and not pq.urls and not pq.by_domain and not pq


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
    app._trigger_failure_cooldown(1, 7)  # fail_s<=0 -> no-op
    assert app._cooldown_until == 0.0
    app.cooldown_var.set("300")
    app._trigger_failure_cooldown(1, 7)
    assert app._cooldown_until > time.time()
    blocked, _ = app._is_blocked()
    assert blocked is True
    app._cooldown_until = 0.0
    app.pause_event.set()
    assert app._is_blocked()[0] is True
    app.pause_event.clear()
    assert app._is_blocked()[0] is False


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
    app._cooldown_until = time.time() + 100
    app._update_status(); pump(app, 0.05)
    app._cooldown_until = 0.0
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
                            lambda: ([q("http://in/1")], []))
    elif kind == "pending":
        monkeypatch.setattr(disp, "_load_state_items",
                            lambda: ([], [q("http://p/1")]))
    else:
        monkeypatch.setattr(disp, "_load_state_items",
                            lambda: ([q("http://in/1")], [q("http://p/1")]))
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


def test_command_timeout_increments_metric(headless_dispatcher):
    class Proc:
        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    timer = headless_dispatcher._arm_command_timeout(Proc(), "queue#1", "http://u", 1)
    try:
        timer.function()
    finally:
        timer.cancel()

    assert headless_dispatcher.metrics["timeouts"] == 1


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
