"""Native CLI, Tk, and OS contracts shared by Linux, macOS, and Windows CI."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PORTABLE_SCRIPTS = (
    "bookmark-tidy.py", "dedupl_numpy.py", "deduplicate-by-namev3.py",
    "hash-recursive-ai5.py", "import_events.py", "link_queue.py",
    "minikeypad.py", "organize_by_extension.py", "remove-deduplv3.py",
)


@pytest.fixture
def isolated(tmp_path):
    work = tmp_path / "portable space café"
    work.mkdir()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "XDG_CONFIG_HOME": str(work / "configuration"),
        "XDG_CACHE_HOME": str(work / "cache"),
        "PYTHONUTF8": "1",
        "MINIKEYPAD_AUTO_INSTALL": "0",
    })
    return work, env


def _copy_script(work, name):
    return shutil.copyfile(ROOT / name, work / name)


def _run(isolated, *arguments, expected=0):
    work, env = isolated
    result = subprocess.run(
        [sys.executable, *map(str, arguments)], cwd=work, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in result.stderr
    return result


def _load_script(work, name):
    path = _copy_script(work, name)
    module_name = "portable_" + Path(name).stem.replace("-", "_")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", PORTABLE_SCRIPTS)
def test_copied_standalone_script_help(isolated, name):
    result = _run(isolated, _copy_script(isolated[0], name), "--help")
    assert "usage:" in result.stdout.lower()


def test_bookmark_cli_reads_bom_and_writes_unicode_path(isolated):
    work, _ = isolated
    source = work / "entrée.txt"
    source.write_text("https://example.org/caf%C3%A9\n", encoding="utf-8-sig")
    output = work / "résultat.json"
    _run(isolated, _copy_script(work, "bookmark-tidy.py"), source,
         "--immutable-root", "Bookmarks Bar", "-o", output)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["roots"]["bookmark_bar"]["children"][0]["url"] == "https://example.org/caf%C3%A9"


def test_hash_cli_compares_files_with_unicode_names(isolated):
    work, _ = isolated
    source = work / "inputs"
    source.mkdir()
    (source / "première.txt").write_bytes(b"same payload")
    (source / "deuxième.txt").write_bytes(b"same payload")
    hashes = work / "empreintes.txt"
    result = _run(isolated, _copy_script(work, "hash-recursive-ai5.py"),
                  source, "-j", "1", "--hashes-file", hashes)
    assert "première.txt" in result.stdout
    assert "deuxième.txt" in result.stdout
    assert hashes.exists() and hashes.stat().st_size > 0


def test_organizer_cli_preview_then_move(isolated):
    work, _ = isolated
    source = work / "inputs"
    source.mkdir()
    original = source / "résumé.txt"
    original.write_bytes(b"portable text\n")
    script = _copy_script(work, "organize_by_extension.py")
    _run(isolated, script, source, "--preview", "--no-sniff")
    assert original.read_bytes() == b"portable text\n"
    assert not (source / "txt").exists()
    _run(isolated, script, source, "--threads", "1", "--no-sniff")
    assert not original.exists()
    moved = list((source / "txt").rglob(original.name))
    assert len(moved) == 1 and moved[0].read_bytes() == b"portable text\n"


def test_events_cli_imports_and_exports_ics_without_models(isolated):
    work, _ = isolated
    source = work / "calendriers"
    source.mkdir()
    (source / "réunion.ics").write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Portable tests//EN\n"
        "BEGIN:VEVENT\nUID:portable-event@example.org\nDTSTAMP:20260101T000000Z\n"
        "DTSTART:20261012T080000Z\nDTEND:20261012T090000Z\n"
        "SUMMARY:Réunion\nEND:VEVENT\nEND:VCALENDAR\n", encoding="utf-8",
    )
    output = work / "événements.json"
    exported = work / "export.ics"
    model_cache = work / "models"
    _run(isolated, _copy_script(work, "import_events.py"), source, "-o", output,
         "--emit-ics", exported, "--model-cache-dir", model_cache)
    events = json.loads(output.read_text(encoding="utf-8"))
    assert len(events) == 1 and events[0]["title"] == "Réunion"
    from icalendar import Calendar
    calendar = Calendar.from_ical(exported.read_bytes())
    assert str(calendar.walk("VEVENT")[0]["SUMMARY"]) == "Réunion"
    assert not list(model_cache.rglob("*.gguf"))


_LOAD_CHILD = """
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("portable_child", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
"""

_GUI_CHILD = _LOAD_CHILD + """
instances = []

def queue_init(self, root):
    original(self, root)
    instances.append(self)
    assert root.winfo_children()
    root.withdraw()
    root.after(100, self._on_close)

def keypad_init(self):
    original(self)
    instances.append(self)
    assert self._phys_buttons
    assert self.state_lbl.cget("text") == "Not connected"
    self.withdraw()
    self.after(100, self.destroy)

if sys.argv[2] == "queue":
    original = module.LinkQueueApp.__init__
    module.LinkQueueApp.__init__ = queue_init
    module.main([])
    assert Path(module.STATE_FILE).exists()
else:
    module._USB_OK = False
    original = module.App.__init__
    module.App.__init__ = keypad_init
    module.main(["--no-auto-install"])
    assert instances[0]._destroyed

assert len(instances) == 1
print("GUI opened and closed")
"""


@pytest.mark.parametrize("name,kind", [("link_queue.py", "queue"), ("minikeypad.py", "keypad")])
def test_real_tk_entrypoint_opens_and_closes(isolated, name, kind):
    # Missing Tk/display must fail this gate: a help-only pass cannot prove GUI portability.
    script = _copy_script(isolated[0], name)
    result = _run(isolated, "-c", _GUI_CHILD, script, kind)
    assert "GUI opened and closed" in result.stdout


_LOCK_CHILD = _LOAD_CHILD + """
from contextlib import contextmanager

@contextmanager
def ownership(kind, path):
    if kind == "events":
        with module._exclusive_lock(path, "portable output"):
            yield
        return
    lock = module.StateFileLock(str(path))
    lock.acquire()
    try:
        yield
    finally:
        lock.release()

kind, mode, target, ready = sys.argv[2:]
busy_error = FileExistsError if kind == "events" else module.StateFileLockError
try:
    with ownership(kind, Path(target)):
        if mode == "hold":
            Path(ready).write_text("ready", encoding="utf-8")
            sys.stdin.readline()
        print("acquired")
except busy_error:
    print("busy")
    raise SystemExit(7)
"""


def _wait_ready(owner, ready):
    deadline = time.monotonic() + 10
    while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "lock owner exited or did not become ready within 10 seconds"


@pytest.mark.parametrize("name,kind", [("import_events.py", "events"), ("link_queue.py", "queue")])
def test_native_lock_contention_and_killed_owner_recovery(isolated, name, kind):
    work, env = isolated
    script = _copy_script(work, name)
    target, ready = work / "shared état.lock", work / "owner-ready"
    args = ["-c", _LOCK_CHILD, script, kind]
    owner = subprocess.Popen(
        [sys.executable, *args, "hold", str(target), str(ready)], cwd=work, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        _wait_ready(owner, ready)
        result = _run(isolated, *args, "probe", target, ready, expected=7)
        assert "busy" in result.stdout
        assert owner.poll() is None, "contention must not terminate the existing owner"
        owner.kill()
        owner.communicate(timeout=10)
        result = _run(isolated, *args, "probe", target, ready)
        assert "acquired" in result.stdout
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)


def test_usb_backend_without_kernel_driver_support_degrades(isolated):
    module = _load_script(isolated[0], "minikeypad.py")

    class UnsupportedKernelDriver:
        def is_kernel_driver_active(self, _interface):
            raise NotImplementedError("backend has no kernel-driver interface")

    device = module.KeypadDevice()
    device._detach_kernel_driver(UnsupportedKernelDriver())
    assert not device._detached


def test_hardlink_fallback_when_no_follow_keyword_is_unsupported(isolated, monkeypatch):
    work, _ = isolated
    module = _load_script(work, "organize_by_extension.py")
    source, target = work / "source.txt", work / "target.txt"
    source.write_bytes(b"portable hardlink")
    real_link = os.link

    def unsupported_keyword(src, dst, **kwargs):
        if "follow_symlinks" in kwargs:
            raise NotImplementedError("no follow_symlinks support")
        return real_link(src, dst)

    monkeypatch.setattr(module.os, "link", unsupported_keyword)
    module._link_regular_no_follow(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert os.path.samefile(source, target)
