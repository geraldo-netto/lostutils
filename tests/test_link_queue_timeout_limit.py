"""lq-conflict-91: attempts have a 30-minute ceiling and a terminal retry budget."""

import threading
from types import SimpleNamespace

import pytest

import link_queue as lq


@pytest.mark.parametrize("value, expected", [
    (None, 1800), (0, 1800), (-1, 1800), ("bad", 1800),
    (True, 1800), (1.5, 1800), (float("inf"), 1800),
    (1, 1), (120, 120), ("300", 300), (1800, 1800), (21600, 1800),
])
def test_lq_conflict_91_config_and_runner_enforce_timeout_limit(tmp_path, value, expected):
    config = dict(lq.DEFAULT_CONFIG, protocols={}, command_timeout_seconds=value)
    lq.ConfigStore._normalize_config_schema(config)
    assert config["command_timeout_seconds"] == expected
    dispatcher = lq.Dispatcher.headless(config, state_path=str(tmp_path / "state.yaml"))
    try:
        assert dispatcher._command_timeout_seconds() == expected
        dispatcher.config["command_timeout_seconds"] = value
        assert dispatcher._command_timeout_seconds() == expected
    finally:
        dispatcher.stop_event.set()
        dispatcher.close()


def test_lq_conflict_91_default_timeout_retries_then_finishes(tmp_path, monkeypatch):
    config = dict(lq.DEFAULT_CONFIG, sleep_between_items=0)
    dispatcher = lq.Dispatcher.headless(config, state_path=str(tmp_path / "state.yaml"))
    dispatcher._save_delay = 10**6
    timeouts = []
    item = lq.QueueItem("https://example.test/slow", "https", "unused", False)

    def timed_out(proc, _label, _url, timeout, _verbosity):
        timeouts.append(timeout)
        proc._link_queue_timed_out = True
        dispatcher._record_metric("timeouts")
        return True

    monkeypatch.setattr(dispatcher, "_spawn_proc", lambda *_: SimpleNamespace(stdout=object(), returncode=-15))
    monkeypatch.setattr(dispatcher, "_register_process", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_unregister_process", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_stream_and_wait", timed_out)
    dispatcher.queue_items.append(item)
    try:
        for attempts in range(3):
            assert dispatcher.queue_items[0].attempts == attempts
            dispatcher._worker_step(1, threading.Event())
        assert timeouts == [1800, 1800, 1800]
        assert not dispatcher.queue_items
        dispatcher._save_state()
        assert dispatcher._load_state_items() == ([], [])
        with dispatcher._dispatch_cv:
            assert dispatcher._try_claim_item(1) is None
    finally:
        dispatcher.stop_event.set()
        dispatcher.close()


def test_lq_conflict_91_reader_deadline_does_not_double_attempt_timeout(tmp_path, monkeypatch):
    dispatcher = lq.Dispatcher.headless(state_path=str(tmp_path / "state.yaml"))
    deadlines = []
    waits = []
    monkeypatch.setattr(lq.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(dispatcher, "_arm_command_timeout", lambda *_: None)
    monkeypatch.setattr(dispatcher, "_capture_subprocess_output",
                        lambda _stdout, _label, _url, deadline, _verbosity: deadlines.append(deadline) or True)
    proc = SimpleNamespace(stdout=object(), wait=lambda timeout: waits.append(timeout))
    try:
        assert dispatcher._stream_and_wait(proc, "queue", "url", 1800, "silent")
        assert deadlines == [1905.0]
        assert waits == [1805.0]
    finally:
        dispatcher.stop_event.set()
        dispatcher.close()
