"""lq-rel-91: failed queued links remain recoverable until their attempt limit."""
from __future__ import annotations

import threading
import subprocess
import sys
import time

import pytest

import link_queue as lq


@pytest.fixture
def dispatcher(tmp_path):
    config = dict(lq.DEFAULT_CONFIG, sleep_between_items=0)
    instance = lq.Dispatcher.headless(config, state_path=str(tmp_path / "state.yaml"))
    instance._save_delay = 10**6
    yield instance
    instance.stop_event.set()
    with instance._dispatch_cv:
        instance._dispatch_cv.notify_all()
    instance.close()


def item(url="https://failed.test/link", extra=()):
    return lq.QueueItem(url, "https", "echo {url}", False, extra)


def test_lq_rel_91_cooldown_retains_failed_link_and_allows_other_domains(dispatcher, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(lq.time, "monotonic", lambda: clock[0])
    failed, healthy = item(), item("https://healthy.test/link")
    dispatcher.queue_items[:] = [failed, healthy]
    outcomes = {failed.url: [7, 0], healthy.url: [0]}
    monkeypatch.setattr(dispatcher, "_run_item", lambda it, _label: outcomes[it.url].pop(0))

    dispatcher._worker_step(0, threading.Event())
    assert [it.url for it in dispatcher.queue_items] == [healthy.url, failed.url]
    retry = dispatcher.queue_items[-1]
    assert retry.attempts == 1
    dispatcher._save_state()
    persisted = dispatcher._load_state_items()[1]
    assert persisted[-1] == retry
    assert dispatcher._duplicate_status(failed) == "pending"

    dispatcher._worker_step(1, threading.Event())
    with dispatcher._dispatch_cv:
        assert dispatcher._try_claim_item(1) is None
    clock[0] += dispatcher.config["failure_sleep_seconds"] + 1
    dispatcher._worker_step(0, threading.Event())
    assert list(dispatcher.queue_items) == []
    assert dispatcher.metrics == {"failures": 1, "completions": 2, "timeouts": 0}


@pytest.mark.parametrize("limit", [1, 3, 5])
@pytest.mark.parametrize("outcome", [7, -1, lq.COMMAND_TIMEOUT_EXIT])
def test_lq_rel_91_failed_attempts_stop_at_configured_limit(dispatcher, monkeypatch, limit, outcome):
    dispatcher.config.update(max_attempts=limit, failure_sleep_seconds=0)
    dispatcher.queue_items.append(item())
    calls, logs = [], []
    monkeypatch.setattr(dispatcher, "_run_item", lambda it, _label: calls.append(it) or outcome)
    monkeypatch.setattr(dispatcher, "_log", logs.append)

    for _ in range(limit):
        assert len(dispatcher.queue_items) == 1
        dispatcher._worker_step(0, threading.Event())
    assert len(calls) == limit
    assert [it.attempts for it in calls] == list(range(limit))
    assert list(dispatcher.queue_items) == []
    assert dispatcher._build_state_snapshot()["queue"] == []
    assert any("exhausted" in line and f"{limit}/{limit}" in line for line in logs)


def test_lq_rel_91_pending_retry_survives_restart_without_resetting_budget(dispatcher, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 0
    original = item(extra=(("-o", "a file.mp4"),))
    dispatcher.queue_items.append(original)
    monkeypatch.setattr(dispatcher, "_run_item", lambda *_: 9)
    dispatcher._worker_step(0, threading.Event())
    dispatcher._save_state()

    restored = lq.Dispatcher.headless(dict(dispatcher.config), state_path=dispatcher.state_path)
    restored._save_delay = 10**6
    calls = []
    monkeypatch.setattr(restored, "_run_item", lambda it, _label: calls.append(it) or 9)
    try:
        restored._restore_queue_from_state()
        assert len(restored.queue_items) == 1
        assert restored.queue_items[0].extra == original.extra
        for _ in range(2):
            restored._worker_step(0, threading.Event())
        assert [it.attempts for it in calls] == [1, 2]
        assert not restored.queue_items
        restored._save_state()
        assert restored._load_state_items() == ([], [])
    finally:
        restored.stop_event.set()
        restored.close()


def test_lq_rel_91_zero_cooldown_retry_is_published_after_releasing_slot(dispatcher, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 0
    original = item()
    dispatcher.queue_items.append(original)
    monkeypatch.setattr(dispatcher, "_run_item", lambda *_: 1)
    release = dispatcher._release_item
    claims = []

    def competing_release(index, current, **kwargs):
        with dispatcher._dispatch_cv:
            assert dispatcher._try_claim_item(1) is None
        release(index, current, **kwargs)
        with dispatcher._dispatch_cv:
            claims.append(dispatcher._try_claim_item(1))
            claims.append(dispatcher._try_claim_item(2))

    monkeypatch.setattr(dispatcher, "_release_item", competing_release)
    dispatcher._worker_step(0, threading.Event())
    assert claims[0] is not None
    assert claims[0].url == original.url
    assert claims[0].attempts == 1
    assert claims[1] is None
    assert dispatcher.current_items[0] is None
    assert dispatcher._domain_active == {"failed.test": 1}


def test_lq_rel_91_lowered_limit_stops_an_already_pending_retry(dispatcher, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 0
    dispatcher.queue_items.append(item())
    calls = []
    monkeypatch.setattr(dispatcher, "_run_item", lambda it, _label: calls.append(it) or 1)
    dispatcher._worker_step(0, threading.Event())
    assert len(dispatcher.queue_items) == 1
    dispatcher.config["max_attempts"] = 1
    dispatcher._worker_step(0, threading.Event())
    assert len(calls) == 1
    assert not dispatcher.queue_items
    assert dispatcher.metrics["failures"] == 1


def test_lq_rel_91_shutdown_keeps_retry_budget_without_scheduling_an_extra_attempt(dispatcher, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 0
    dispatcher.queue_items.append(item())
    monkeypatch.setattr(dispatcher, "_run_item", lambda *_: 1)
    dispatcher._worker_step(0, threading.Event())
    assert len(dispatcher.queue_items) == 1
    retry = dispatcher.queue_items[0]

    def interrupt(*_):
        dispatcher.stop_event.set()
        return lq.COMMAND_INTERRUPTED_EXIT

    monkeypatch.setattr(dispatcher, "_run_item", interrupt)
    dispatcher._worker_step(0, threading.Event())
    snapshot = dispatcher._build_state_snapshot()
    assert snapshot["queue"] == []
    assert snapshot["in_flight"] == [dispatcher._serialize_item(retry)]
    assert snapshot["in_flight"][0]["attempts"] == 1
    assert dispatcher.metrics["failures"] == 1


def test_lq_rel_91_unexpected_runner_error_does_not_lose_link(dispatcher, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 0
    dispatcher.queue_items.append(item())

    def fail(*_):
        raise RuntimeError("runner failed")

    monkeypatch.setattr(dispatcher, "_run_item", fail)
    dispatcher._worker_step(0, threading.Event())
    assert len(dispatcher.queue_items) == 1
    assert dispatcher.queue_items[0].attempts == 1
    assert dispatcher.current_items[0] is None
    assert dispatcher.metrics["failures"] == 1


def test_lq_rel_91_immediate_links_still_run_once(dispatcher, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "_run_item", lambda it, _label: calls.append(it) or 1)
    assert dispatcher._run_immediate_item(item()) == 1
    assert len(calls) == 1
    assert not dispatcher.queue_items


@pytest.mark.parametrize("value", [True, 1.5, float("inf"), "invalid", None, 0, -1])
def test_lq_rel_91_worker_rejects_invalid_attempt_limits(dispatcher, value):
    dispatcher.config["max_attempts"] = value
    assert dispatcher._max_attempts() == 3


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", None])
def test_lq_rel_91_invalid_saved_attempts_do_not_discard_valid_neighbors(value, capsys):
    saved = {"url": "https://bad.test/link", "attempts": value}
    neighbor = {"url": "https://good.test/link", "attempts": 2}
    restored = lq.Dispatcher._parse_state_list({"queue": [saved, neighbor]}, "queue")
    assert len(restored) == 1
    assert restored[0].url == neighbor["url"]
    assert restored[0].attempts == 2
    assert "dropped 1" in capsys.readouterr().err


def test_lq_rel_91_real_worker_retries_after_cooldown_until_process_succeeds(dispatcher, tmp_path, monkeypatch):
    dispatcher.config["failure_sleep_seconds"] = 1
    dispatcher._save_delay = 0.01
    counter = tmp_path / "attempts.txt"
    program = (
        "from pathlib import Path; import sys; p = Path(sys.argv[1]); "
        "n = int(p.read_text()) + 1 if p.exists() else 1; "
        "p.write_text(str(n)); sys.exit(0 if n == 3 else 7)"
    )
    starts = []

    def spawn(*_):
        starts.append(time.monotonic())
        return subprocess.Popen(
            [sys.executable, "-c", program, str(counter)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )

    completed = threading.Event()
    record = dispatcher._record_metric

    def record_and_signal(name, amount=1):
        record(name, amount)
        if name == "completions":
            completed.set()

    monkeypatch.setattr(dispatcher, "_spawn_proc", spawn)
    monkeypatch.setattr(dispatcher, "_record_metric", record_and_signal)
    dispatcher.queue_items.append(item())
    retired = threading.Event()
    worker = threading.Thread(target=dispatcher._worker_loop, args=(0, retired))
    worker.start()
    try:
        assert completed.wait(10), dispatcher._build_state_snapshot()
        assert counter.read_text() == "3"
        assert len(starts) == 3
        assert starts[1] - starts[0] >= 1
        assert starts[2] - starts[1] >= 1
        assert not dispatcher.queue_items
        assert dispatcher.metrics == {"failures": 2, "completions": 1, "timeouts": 0}
    finally:
        retired.set()
        with dispatcher._dispatch_cv:
            dispatcher._dispatch_cv.notify_all()
        worker.join(2)
        assert not worker.is_alive()


@pytest.mark.parametrize('blocked_by', ['cap', 'cooldown'])
def test_lq_rel_92_credentials_share_domain_limits(dispatcher, blocked_by):
    first = item('https://alice:secret@www.Example.test:8443/a')
    second = item('https://bob:other@example.test:8443/b')
    dispatcher.config['max_per_domain'] = 1
    dispatcher.queue_items[:] = [first, second]
    if blocked_by == 'cooldown':
        dispatcher._trigger_failure_cooldown(0, first, 1)
        dispatcher.queue_items[:] = [second]
    with dispatcher._dispatch_cv:
        if blocked_by == 'cap':
            assert dispatcher._try_claim_item(0) == first
        assert dispatcher._try_claim_item(1) is None
    assert dispatcher._domain_of(first) == 'example.test:8443'


@pytest.mark.parametrize('url,expected', [
    ('https://user:secret@[2001:db8::1]:8443/a', '[2001:db8::1]:8443'),
    ('https://user:secret@[2001:db8::1]/a', '[2001:db8::1]'),
    ('https://user:secret@example.test:invalid/a', '_unknown'),
    ('https://user:secret@example.test:99999/a', '_unknown'),
])
def test_lq_rel_92_domain_keys_handle_ipv6_and_invalid_ports(dispatcher, url, expected):
    assert dispatcher._domain_of(url) == expected


@pytest.mark.parametrize('elapsed', [60, 301])
def test_lq_time_92_restart_preserves_remaining_cooldown(dispatcher, monkeypatch, elapsed):
    wall, mono = [1000.0], [100.0]
    monkeypatch.setattr(lq.time, 'time', lambda: wall[0])
    monkeypatch.setattr(lq.time, 'monotonic', lambda: mono[0])
    dispatcher.queue_items.append(item())
    monkeypatch.setattr(dispatcher, '_run_item', lambda *_: 1)
    dispatcher._worker_step(0, threading.Event())
    dispatcher._save_state()
    wall[0] += elapsed
    mono[0] = 10.0  # Reboot: the old monotonic epoch is gone.
    restored = lq.Dispatcher.headless(dict(dispatcher.config), state_path=dispatcher.state_path)
    try:
        restored._restore_queue_from_state()
        assert restored.queue_items[0].attempts == 1
        healthy = item('https://healthy.test/a')
        restored.queue_items.append(healthy)
        with restored._dispatch_cv:
            if elapsed < 300:
                assert restored._try_claim_item(0) == healthy
                assert restored._try_claim_item(1) is None
                # A wall-clock jump in this process cannot change the delay.
                wall[0] += 100000
                assert restored._try_claim_item(1) is None
                mono[0] += 300 - elapsed
            assert restored._try_claim_item(1).url == item().url
    finally:
        restored.stop_event.set()
        restored.close()


@pytest.mark.parametrize('cooldowns', [[], {'failed.test': 'tomorrow'}, {'failed.test': float('inf')}, {'failed.test': True}, {1: 1234}])
def test_lq_time_92_invalid_cooldowns_preserve_state(dispatcher, cooldowns):
    from pathlib import Path
    path = Path(dispatcher.state_path)
    with path.open('w') as stream:
        lq._yaml_dump({'queue': [dispatcher._serialize_item(item())], 'cooldowns': cooldowns}, stream)
    original = path.read_bytes()
    dispatcher._restore_queue_from_state()
    assert not dispatcher.queue_items
    assert dispatcher.state_load_error
    dispatcher._save_state()
    assert path.read_bytes() == original
