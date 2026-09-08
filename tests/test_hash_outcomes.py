"""Final inode outcomes stay independent of failed alias attempt counts."""

import errno
import importlib.util
import os
import threading
from pathlib import Path

import pytest


_PATH = Path(__file__).resolve().parent.parent / "hash-recursive-ai5.py"
_SPEC = importlib.util.spec_from_file_location("hash_outcome_subject", _PATH)
hr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hr)

_ERRORS = {"vanished": errno.ENOENT, "denied": errno.EACCES}


def test_cancelled_batch_keeps_prior_results_and_stops_subsequent_work():
    visited = []

    def hash_item(item):
        visited.append(item)
        if item == "cancel":
            raise hr._HashCancelled
        return f"hashed:{item}"

    result = hr._run_cancelable_batch(["first", "cancel", "later"], hash_item, None)

    assert result == ["hashed:first"]
    assert visited == ["first", "cancel"]


@pytest.fixture(params=[False, True], ids=["serial", "threaded"])
def threaded_dispatch(request, monkeypatch):
    monkeypatch.setattr(hr, "THREAD_THRESHOLD_BYTES", 0 if request.param else 10**9)
    monkeypatch.setattr(hr, "HASH_BATCH", 1)
    return request.param


def _make_aliases(root, number, count):
    base = root / f"inode-{number}"
    base.write_bytes(b"same content for all inodes")
    paths = []
    for alias_number in range(count):
        alias = root / f"inode-{number}-alias-{alias_number}"
        os.link(base, alias)
        paths.append(str(alias))
    status = base.stat()
    return (status.st_dev, status.st_ino), status.st_size, paths


def _cohort(root, patterns):
    rep, sizes, aliases, faults = {}, {}, {}, {}
    for number, kinds in enumerate(patterns):
        key, size, paths = _make_aliases(root, number, len(kinds))
        rep[key], sizes[key], aliases[key] = paths[0], size, paths
        faults.update((path, _ERRORS[kind]) for path, kind in zip(paths, kinds) if kind != "ok")
    return {"rep": rep, "sizes": sizes, "aliases": aliases, "faults": faults}


def _faulty_opens(monkeypatch, records, cancel_event=None):
    real_open = hr._open_hash_file
    calls = []
    lock = threading.Lock()

    def open_file(path):
        with lock:
            calls.append((path, threading.get_ident()))
        error = records["faults"].get(path)
        if error is not None:
            if cancel_event is not None:
                cancel_event.set()
            raise OSError(error, "injected open failure", path)
        return real_open(path)

    monkeypatch.setattr(hr, "_open_hash_file", open_file)
    return calls


def _drive_stage(stage, records, config, cancel_event=None):
    rep, sizes, aliases = (records[name] for name in ("rep", "sizes", "aliases"))
    options = {"jobs": 2, "config": config, "aliases": aliases, "cancel_event": cancel_event}
    if stage == 1:
        return hr._stage1_hash([(sizes[key], key) for key in rep], rep, **options)
    if stage == 2:
        items = [(sizes[key], path, "HEAD", key) for key, path in rep.items()]
        return hr._stage2_hash(items, **options)
    return hr._stage3_hash({("HEAD", "TAIL"): list(rep)}, rep, sizes, **options)


_CASES = [
    pytest.param((("vanished", "ok"),), 0, 0, id="recovered-vanished"),
    pytest.param((("denied", "ok"),), 0, 0, id="recovered-denied"),
    pytest.param((("vanished", "vanished"),), 1, 0, id="only-benign-aliases"),
    pytest.param((("denied", "vanished"),), 1, 1, id="real-before-benign"),
    pytest.param((("vanished", "denied"),), 1, 1, id="benign-before-real"),
    pytest.param((("denied", "denied"),), 1, 1, id="only-real-aliases"),
    pytest.param((("vanished", "ok"), ("denied",)), 1, 1, id="recovered-benign-plus-real"),
    pytest.param((("vanished", "vanished"), ("denied",)), 2, 1, id="many-benign-attempts-plus-real"),
]


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("patterns,unresolved,real_errors", _CASES)
def test_stage_counts_final_inode_outcomes(
        tmp_path, monkeypatch, threaded_dispatch, stage, patterns, unresolved, real_errors):
    records = _cohort(tmp_path, patterns + (("ok",),))
    calls = _faulty_opens(monkeypatch, records)
    config = hr.RunConfig(block_size=4, sample_size=2, hash_error_verbose_cap=0)

    groups, info = _drive_stage(stage, records, config)

    assert info[f"stage{stage}_errors"] == unresolved
    assert info[f"stage{stage}_real_errors"] == real_errors
    assert hr._real_hash_error_count(info, config) == real_errors
    assert hr._run_exit_code(threading.Event(), {}, info, config) == bool(real_errors)
    assert sum(map(len, groups.values())) == 1 + sum("ok" in kinds for kinds in patterns)
    assert config.hash_skipped_vanished == sum(kinds.count("vanished") for kinds in patterns)
    assert config._hash_failures == {}
    assert any(thread != threading.get_ident() for _path, thread in calls) == threaded_dispatch


@pytest.mark.parametrize("later_stage", [2, 3])
def test_recovered_real_error_does_not_taint_later_benign_stage(
        tmp_path, monkeypatch, threaded_dispatch, later_stage):
    records = _cohort(tmp_path, (("denied", "ok"), ("ok",)))
    _faulty_opens(monkeypatch, records)
    config = hr.RunConfig(block_size=4, sample_size=2, hash_error_verbose_cap=0)

    _groups, first = _drive_stage(1, records, config)

    assert first["stage1_real_errors"] == 0
    assert config.hash_error_suppressed == 1
    assert config._hash_failures == {}
    failed_key = next(iter(records["rep"]))
    records["faults"].update((path, errno.ENOENT) for path in records["aliases"][failed_key])
    _groups, later = _drive_stage(later_stage, records, config)
    assert later[f"stage{later_stage}_errors"] == 1
    assert later[f"stage{later_stage}_real_errors"] == 0
    assert hr._real_hash_error_count({**first, **later}, config) == 0
    assert config._hash_failures == {}


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_cancel_after_failed_attempt_skips_alias_retry_and_keeps_exit_130(
        tmp_path, monkeypatch, threaded_dispatch, stage):
    records = _cohort(tmp_path, (("denied", "ok"), ("ok",)))
    cancel = threading.Event()
    calls = _faulty_opens(monkeypatch, records, cancel_event=cancel)
    config = hr.RunConfig(block_size=4, sample_size=2, hash_error_verbose_cap=0)

    _groups, info = _drive_stage(stage, records, config, cancel)

    failed_key = next(iter(records["rep"]))
    assert records["aliases"][failed_key][1] not in [path for path, _thread in calls]
    assert info[f"stage{stage}_errors"] == 1
    assert info[f"stage{stage}_real_errors"] == 1
    assert hr._run_exit_code(cancel, {}, info, config) == 130
    assert config._hash_failures == {}


@pytest.mark.parametrize("earlier_real_failure", [False, True])
def test_strict_short_read_is_benign_without_erasing_an_earlier_real_failure(
        tmp_path, monkeypatch, earlier_real_failure):
    records = _cohort(tmp_path, (("denied", "ok"),))
    key = next(iter(records["rep"]))
    paths = records["aliases"][key]
    expected = (*key, records["sizes"][key])
    config = hr.RunConfig(hash_error_verbose_cap=0)
    _faulty_opens(monkeypatch, records)
    if earlier_real_failure:
        assert hr.hash_full(paths[0], expected[2], config, expected) is None
    monkeypatch.setattr(hr, "_read_window_into", lambda *_args: False)

    assert hr.hash_full(paths[1], expected[2], config, expected) is None

    assert config.hash_skipped_shrank == 1
    assert hr._unresolved_hash_failures([(expected, None)], config) == earlier_real_failure
    assert config._hash_failures == {}
