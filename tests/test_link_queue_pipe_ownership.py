"""Output deadlines close owned wrappers without touching reused descriptors."""

import os
import subprocess
import sys
import threading
import textwrap
import time
import types
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import link_queue


@pytest.fixture
def dispatcher(tmp_path):
    instance = link_queue.Dispatcher.headless(config={}, state_path=str(tmp_path / "state.yaml"))
    try:
        yield instance
    finally:
        instance.stop_event.set()
        instance.close()


def test_output_timeout_cannot_close_a_reused_descriptor(tmp_path, monkeypatch, dispatcher):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
    proc = types.SimpleNamespace(stdout=stdout, wait=lambda timeout: 0)
    monkeypatch.setattr(dispatcher, "_arm_command_timeout", lambda *_args: None)
    monkeypatch.setattr(dispatcher, "_deadline_remaining", lambda _deadline: 0.0)
    monkeypatch.setattr(dispatcher, "_kill_process_tree", lambda _proc: None)
    monkeypatch.setattr(dispatcher, "_wait_process_tree", lambda *_args: True)
    replacement = None
    try:
        assert not dispatcher._stream_and_wait(proc, "test", "https://example.test", 1, "summary")
        replacement = os.open(tmp_path / "unrelated", os.O_CREAT | os.O_RDWR, 0o600)
        assert replacement == read_fd, "test did not exercise descriptor reuse"
        os.close(write_fd)
        write_fd = -1
        stdout.close()
        os.fstat(replacement)
        os.write(replacement, b"unrelated data")
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        stdout.close()
        if replacement is not None:
            try:
                os.close(replacement)
            except OSError:
                pass


def test_live_pipe_deadline_closes_wrapper_without_a_reader_thread(dispatcher):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8")
    before = set(threading.enumerate())
    started = time.monotonic()
    try:
        assert not dispatcher._capture_subprocess_output(
            stdout, "test", "https://example.test", started + 0.05, "verbose")
        assert time.monotonic() - started < 1
        assert stdout.closed
        assert set(threading.enumerate()) == before
        with pytest.raises(OSError):
            os.fstat(read_fd)
    finally:
        os.close(write_fd)
        stdout.close()


@pytest.mark.parametrize("failure", ["setup", "read"])
def test_pipe_io_failure_is_visible_and_closes_wrapper(dispatcher, monkeypatch, failure):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8")
    messages = []

    def fail(*_args):
        raise OSError(f"{failure} unavailable")

    monkeypatch.setattr(dispatcher, "_log", messages.append)
    monkeypatch.setattr(link_queue.os, "set_blocking" if failure == "setup" else "read", fail)
    try:
        assert not dispatcher._capture_subprocess_output(stdout, "job", "url", None, "verbose")
        assert stdout.closed
        assert any(f"{failure} unavailable" in message for message in messages)
    finally:
        os.close(write_fd)
        stdout.close()


@pytest.mark.parametrize("verbosity, expected", [
    ("verbose", ["start", "25%", "50%", "75%", "100%", "bad � end"]),
    ("summary", ["start", "25%", "50%", "75%", "100%"]),
    ("silent", []),
])
def test_pipe_decoding_preserves_logging_modes(dispatcher, monkeypatch, verbosity, expected):
    read_fd, write_fd = os.pipe()
    stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
    os.write(write_fd, b"start\r25%\r\n50%\n75%\n100%\nbad \x81 end")
    os.close(write_fd)
    messages = []
    monkeypatch.setattr(dispatcher, "_log", messages.append)
    assert dispatcher._capture_subprocess_output(stdout, "job", "url", None, verbosity)
    assert messages == ["| job | " + line for line in expected]
    assert stdout.closed


def test_utf8_and_crlf_survive_chunk_boundaries(dispatcher, monkeypatch):
    chunks = [b"a\xe2", b"\x82", b"\xac\r", b"\nb\r", b"c\n\n", b"tail\xe2"]
    monkeypatch.setattr(dispatcher, "_iter_output_chunks", lambda *_args: iter(chunks))
    assert list(dispatcher._iter_output_lines(None, None)) == ["a€\n", "b\n", "c\n", "\n", "tail�"]


@settings(max_examples=100, deadline=None)
@given(payload=st.binary(max_size=1024))
def test_output_decoding_preserves_arbitrary_bytes_across_chunks(payload):
    instance = link_queue.Dispatcher.__new__(link_queue.Dispatcher)
    instance._iter_output_chunks = lambda *_args: (payload[index:index + 1] for index in range(len(payload)))
    actual = "".join(instance._iter_output_lines(None, None))
    expected = payload.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    assert actual == expected


def test_standalone_script_stops_child_if_nonblocking_setup_is_unsupported(tmp_path):
    (tmp_path / "link_queue.py").write_text(Path(link_queue.__file__).read_text())
    code = textwrap.dedent('''\
        import subprocess
        import sys
        from pathlib import Path
        import link_queue

        dispatcher = link_queue.Dispatcher.headless(config={}, state_path=str(Path.cwd() / "state.yaml"))
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdout=subprocess.PIPE, text=True, start_new_session=True)
        def unsupported(*args):
            raise OSError("nonblocking pipes unsupported")
        link_queue.os.set_blocking = unsupported
        messages = []
        dispatcher._log = messages.append
        try:
            assert not dispatcher._stream_and_wait(proc, "test", "url", 0, "verbose")
            assert proc.poll() is not None
            assert proc.stdout.closed
            assert any("nonblocking pipes unsupported" in message for message in messages)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
            proc.stdout.close()
            dispatcher.stop_event.set()
            dispatcher.close()
    ''')
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('verbosity', ['silent', 'summary', 'verbose'])
def test_lq_mem_92_unterminated_output_has_bounded_memory(dispatcher, monkeypatch, verbosity):
    import io
    import tracemalloc
    chunk = b'x' * 65536
    drained, logged = [], []
    def chunks(*_args):
        for index in range(256):
            drained.append(index)
            yield chunk
    def log(message):
        logged.append(len(message))
    monkeypatch.setattr(dispatcher, '_iter_output_chunks', chunks)
    monkeypatch.setattr(dispatcher, '_log', log)
    tracemalloc.start()
    try:
        assert dispatcher._capture_subprocess_output(io.StringIO(), 'job', 'url', None, verbosity)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(drained) == 256  # All 16 MiB consumed, including in silent mode.
    assert peak < 2_000_000
    if verbosity == 'silent':
        assert logged == []
    else:
        assert max(logged) <= 65536 + len('| job | ')


def test_lq_mem_92_split_long_unicode_lines_preserves_all_text(dispatcher, monkeypatch):
    payload = ('€' * 70000 + '\r\n' + 'x' * 70000 + '\n尾').encode()
    monkeypatch.setattr(dispatcher, '_iter_output_chunks', lambda *_: (
        payload[index:index + 65536] for index in range(0, len(payload), 65536)))
    lines = list(dispatcher._iter_output_lines(None, None))
    assert ''.join(lines) == payload.decode().replace('\r\n', '\n')
    assert max(map(len, lines)) <= 65536
