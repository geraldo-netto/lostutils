"""Pinned source hashes run concurrently with bounded descriptor ownership."""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


def test_source_hashes_run_in_parallel_under_pinned_directories(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    for index in range(4):
        directory = source / str(index)
        directory.mkdir()
        (directory / "payload").write_bytes(b"original" * 100)
    source_fd, _identity = rf._source_identity_fd(source)
    barrier = threading.Barrier(2, timeout=3)
    original_digest = rf._digest_pinned_file
    worker_ids = set()
    lock = threading.Lock()

    def concurrent_digest(directory_fd, name, source_stat):
        with lock:
            worker_ids.add(threading.get_ident())
        barrier.wait()
        return original_digest(directory_fd, name, source_stat)

    try:
        rf.copy_tree(source, target, source_fd=source_fd)
        monkeypatch.setattr(rf, "_digest_pinned_file", concurrent_digest)
        rf.verify_copy(source, target, checksum=True, jobs=2, source_fd=source_fd)
    finally:
        os.close(source_fd)
    assert len(worker_ids) == 2
    assert threading.get_ident() not in worker_ids


def _make_hash_task(directory):
    directory.mkdir()
    source, target = directory / "source", directory / "target"
    source.write_bytes(b"original")
    target.write_bytes(b"original")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        task = rf._pinned_kind_verify_task(
            source, target, Path("source"), descriptor, source.name, source.stat(), True)
    finally:
        os.close(descriptor)
    assert isinstance(task, rf._PinnedFileVerification)
    return task


def _assert_task_closed(task, descriptor):
    assert task.directory_fd == -1
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_cancelled_queued_hash_releases_its_descriptor(tmp_path):
    task = _make_hash_task(tmp_path / "task")
    descriptor = task.directory_fd
    started, release = threading.Event(), threading.Event()

    def blocked_worker():
        started.set()
        assert release.wait(3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(blocked_worker)
        try:
            assert started.wait(3)
            future = rf._submit_in_context(executor, task)
            assert future.cancel()
            _assert_task_closed(task, descriptor)
        finally:
            release.set()
            task.close()
        blocker.result(timeout=3)


@pytest.mark.parametrize("error", [RuntimeError("submit failed"), KeyboardInterrupt()])
def test_submission_failure_releases_hash_descriptor(tmp_path, error):
    task = _make_hash_task(tmp_path / "task")
    descriptor = task.directory_fd

    class FailedExecutor:
        def submit(self, *_args):
            raise error

    with pytest.raises(type(error)):
        rf._submit_in_context(FailedExecutor(), task)
    _assert_task_closed(task, descriptor)


def test_backpressure_does_not_create_unsubmitted_hash_tasks(tmp_path, monkeypatch):
    created = []

    def tasks():
        for index in range(10):
            task = _make_hash_task(tmp_path / str(index))
            created.append((task, task.directory_fd))
            yield task

    def failed_digest(*_args):
        raise RuntimeError("hash failed")

    monkeypatch.setattr(rf, "_digest_pinned_file", failed_digest)
    monkeypatch.setattr(rf, "_inflight_cap", lambda _workers: 1)
    with pytest.raises(RuntimeError, match="hash failed"):
        rf._run_verify_pool(tasks(), jobs=1)
    assert len(created) == 1
    _assert_task_closed(*created[0])


def test_hash_task_keeps_original_directory_after_path_swap(tmp_path):
    directory = tmp_path / "task"
    task = _make_hash_task(directory)
    descriptor = task.directory_fd
    parked = tmp_path / "parked"
    directory.rename(parked)
    directory.mkdir()
    (directory / "source").write_bytes(b"attacker")
    # Destination checks still use their original label, while the source hash
    # must read the pinned directory now parked under another name.
    (directory / "target").write_bytes(b"original")

    task()

    _assert_task_closed(task, descriptor)
    assert (directory / "source").read_bytes() == b"attacker"


def test_queued_hash_rejects_replaced_source_entry(tmp_path):
    directory = tmp_path / "task"
    task = _make_hash_task(directory)
    descriptor = task.directory_fd
    source = directory / "source"
    source.rename(directory / "original")
    source.write_bytes(b"attacker")

    with pytest.raises(RuntimeError, match="changed"):
        task()

    _assert_task_closed(task, descriptor)
    assert source.read_bytes() == b"attacker"


def test_producer_failure_joins_worker_and_releases_descriptors(tmp_path, monkeypatch):
    task = _make_hash_task(tmp_path / "task")
    descriptor = task.directory_fd
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_digest = rf._digest_pinned_file

    def delayed_digest(*args):
        started.set()
        assert release.wait(3)
        try:
            return original_digest(*args)
        finally:
            finished.set()

    def broken_producer():
        yield task
        assert started.wait(3)
        release.set()
        raise RuntimeError("walk failed")

    monkeypatch.setattr(rf, "_digest_pinned_file", delayed_digest)
    with pytest.raises(RuntimeError, match="walk failed"):
        rf._run_verify_pool(broken_producer(), jobs=1)
    assert finished.is_set()
    _assert_task_closed(task, descriptor)


@pytest.mark.parametrize("failure", [None, "hash", "walk", "dup"])
def test_runtime_releases_walk_and_task_descriptors(tmp_path, monkeypatch, failure):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    for index in range(12):
        (source / str(index)).write_bytes(b"original")
    source_fd, identity = rf._source_identity_fd(source)
    original_task = rf._pinned_kind_verify_task
    original_walk = rf._walk_pinned_entries
    created = []
    walked = threading.Event()

    def recorded_task(*args):
        task = original_task(*args)
        if isinstance(task, rf._PinnedFileVerification):
            created.append(task)
        return task

    def checked_walk(descriptor):
        try:
            with closing(original_walk(descriptor)) as entries:
                for entry in entries:
                    yield entry
                    if failure == "walk":
                        raise RuntimeError("walk failure")
        finally:
            walked.set()

    def fail(*_args):
        raise RuntimeError(f"{failure} failure")

    try:
        rf.copy_tree(source, target, source_fd=source_fd)
        monkeypatch.setattr(rf, "_pinned_kind_verify_task", recorded_task)
        monkeypatch.setattr(rf, "_walk_pinned_entries", checked_walk)
        if failure == "hash":
            monkeypatch.setattr(rf, "_digest_pinned_file", fail)
        if failure == "dup":
            monkeypatch.setattr(rf.os, "dup", fail)
        outcome = pytest.raises(RuntimeError, match=f"{failure} failure") if failure else nullcontext()
        with outcome:
            rf.verify_copy(source, target, checksum=True, jobs=2, source_fd=source_fd)
        assert walked.is_set()
        assert all(task.directory_fd == -1 for task in created)
        assert os.fstat(source_fd).st_ino == identity[1]
    finally:
        os.close(source_fd)
