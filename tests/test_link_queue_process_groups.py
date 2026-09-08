"""Real process-group cancellation without leaking orphaned test children."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="isolated harness uses Linux child subreaping")
@pytest.mark.parametrize("mode", ["timeout", "shutdown"])
@pytest.mark.parametrize("leader_exits_early", [False, True])
def test_cancellation_kills_descendants_after_leader_exit(tmp_path, mode, leader_exits_early):
    # The harness alone becomes a subreaper so it can reap grandchildren after
    # their leader exits; pytest's process and unrelated children are unaffected.
    code = textwrap.dedent('''\
        import ctypes
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path
        import link_queue

        folder, mode, early = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == "True"
        assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
        def expired(_signal, _frame):
            raise TimeoutError("process-group harness deadline")
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(12)
        marker = folder / "leaf"
        leaf_code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(" + repr(str(marker)) + ").write_text(str(os.getpid())); time.sleep(60)"
        leader_code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(leaf_code) + "]); time.sleep(" + ("0.1" if early else "60") + ")"
        dispatcher = link_queue.Dispatcher.headless(config={}, state_path=str(folder / "state.yaml"))
        proc = subprocess.Popen([sys.executable, "-c", leader_code], start_new_session=True)
        (folder / "group").write_text(str(proc.pid))
        leaf_pid = None
        reaped = False
        try:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            leaf_pid = int(marker.read_text())
            if early:
                proc.wait(timeout=3)
            dispatcher._register_process(proc)
            if mode == "timeout":
                dispatcher._on_command_timeout(proc, "test", "https://example.test", 1)
            else:
                dispatcher._terminate_active_processes(time.monotonic() + 0.2)
            proc.wait(timeout=3)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                pid, status = os.waitpid(leaf_pid, os.WNOHANG)
                if pid:
                    reaped = True
                    assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
                    break
                time.sleep(0.01)
            assert reaped, "SIGTERM-ignoring descendant survived group leader exit"
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=3)
            if leaf_pid is not None and not reaped:
                os.waitpid(leaf_pid, 0)
            dispatcher._unregister_process(proc)
            dispatcher.stop_event.set()
            dispatcher.close()
            signal.alarm(0)
    ''')
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path), mode, str(leader_exits_early)],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        group = tmp_path / "group"
        if group.exists():
            try:
                os.killpg(int(group.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        raise
    assert result.returncode == 0, result.stdout + result.stderr
