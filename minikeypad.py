#!/usr/bin/env python3
"""
MINI-KeyBoard configurator — Python/Tkinter port of the decompiled C# "HIDTester"
(MINI-KeyBoard) app for a programmable USB keypad with 12 keys + 2 rotary knobs.

Faithful 1:1 re-implementation of the original device protocol:
  * USB VID 0x1189 / PID 0x8890, HID interface 1 ("mi_01")
  * Output report = [reportID] + 8 data bytes (zero-padded to 64)
  * Per-key working buffer (Data_Send_Buff) packed exactly as the C# original
  * Pages: KEY / Ctrl-Shift-Alt / Multimedia / Mouse / LED
  * 3 layers, selected via the 0xA1 layer-switch command
  * Commit via WriteFlash (0xAA 0xAA) or WriteFlashLED (0xAA 0xA1)

Device I/O uses pyusb (libusb backend).  The GUI runs even without pyusb /
without the device attached; it just reports "Not connected".

Programming a key sends several reports followed by a flash-commit; this is
NOT atomic.  If a write fails mid-sequence the key may be left partially
programmed -- just press Write again to re-send the full sequence.

Dependencies:
    pip install pyusb==1.3.1        # and a libusb backend (libusb-1.0)
    If pyusb is missing the app starts without device access by default.
    To opt into installing the pinned dependency at startup, pass
    --auto-install-pyusb or set MINIKEYPAD_AUTO_INSTALL=1.  --no-auto-install
    wins over both, so a shared environment can pin the behaviour off.
    The native libusb-1.0 backend still has to come from your OS package
    manager.
Linux note:
    Accessing the device needs permission.  The recommended way is a udev rule
    (avoid running the whole GUI as root) -- e.g.
    /etc/udev/rules.d/99-minikeypad.rules :
        SUBSYSTEM=="usb", ATTRS{idVendor}=="1189", ATTRS{idProduct}=="8890", MODE="0666"
    then: sudo udevadm control --reload && sudo udevadm trigger
    The usbhid kernel driver is auto-detached from interface 1 when needed and
    re-attached on exit so the keypad keeps working as a normal HID device.
"""

# pyusb is an optional runtime dependency: the GUI runs without it and `usb`
# is only ever touched after the `_USB_OK` guard succeeds.  Silence the type
# checker's missing-import / possibly-unbound noise for that intentional shape.
# The opaque pyusb handles themselves are typed `Any` in the device adapter so
# this stays clean whether or not pyusb (with its loose Union return types) is
# installed.
# pyright: reportMissingImports=false, reportPossiblyUnboundVariable=false

import argparse
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext
from typing import Any

try:  # pragma: no cover - import guard (env-dependent: pyusb present)
    import usb.core
    import usb.util
    _USB_OK = True
    _USB_ERR = ""
except Exception as e:  # pragma: no cover - import guard
    _USB_OK = False
    _USB_ERR = repr(e)

PYUSB_REQUIREMENT = "pyusb==1.3.1"


PIP_OUTPUT_TAIL_LINES = 8


def _pip_install(pkg):
    """Install pkg into the current interpreter. argv list, never a shell.

    Returns ``(ok, transcript)``. mkp-obs-50: pip's own output is the only
    thing that says WHY an install failed -- no network, no compiler,
    externally-managed environment -- and sending it to DEVNULL left the user
    with "Automatic install failed" and nothing to act on.
    """
    base = [sys.executable, "-m", "pip", "install"]
    transcript = []
    for extra in ([], ["--user"]):     # --user fallback for system pythons
        argv = base + extra + [pkg]
        try:
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", check=False)
        except Exception as exc:       # pip itself missing, exec failure, ...
            transcript.append(f"$ {' '.join(argv)}\n{exc!r}")
            continue
        if completed.returncode == 0:
            return True, ""
        transcript.append(f"$ {' '.join(argv)}\n{completed.stdout or ''}")
    return False, "\n".join(transcript)


def _tail_lines(text, limit=PIP_OUTPUT_TAIL_LINES):
    """Last `limit` non-blank lines -- pip puts the actual cause at the end."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(lines[-limit:])


def _ensure_pyusb():
    """One-shot attempt to pip-install pyusb, then re-import. Returns ok bool."""
    global _USB_OK, _USB_ERR, usb
    if _USB_OK:
        return True
    LOG.warning("pyusb not found; installing pinned dependency (%s)...", PYUSB_REQUIREMENT)
    installed, transcript = _pip_install(PYUSB_REQUIREMENT)
    if not installed:
        tail = _tail_lines(transcript)
        if tail:
            LOG.error("pip reported:\n%s", tail)
        LOG.error("Automatic install failed. Install manually: pip install %s", PYUSB_REQUIREMENT)
        return False
    try:
        import usb.core
        import usb.util
        _USB_OK, _USB_ERR = True, ""
        LOG.info("pyusb installed.")
        return True
    except Exception as e:
        _USB_ERR = repr(e)
        LOG.error("pyusb installed but import still failed: %s", _USB_ERR)
        return False


__version__ = "1.0"

LOG = logging.getLogger("minikeypad")
MAX_LOG_LINES = 1000

VID = 0x1189
PID = 0x8890
HID_INTERFACE = 1          # "mi_01"
REPORT_LEN = 64            # data bytes following the report ID
WRITE_TIMEOUT_MS = 500
PROBE_TIMEOUT_MS = 5000
MAX_KBD_GROUPS = 5         # firmware accepts groups 0..5 (6 keystrokes)
WRITE_RETRIES = 2          # extra attempts after the first on a transient USBError
WRITE_RETRY_BACKOFF_S = 0.05
UI_DRAIN_INTERVAL_MS = 120
CONNECTION_POLL_MS = 1000
STATUS_CLEAR_MS = 2500
WINDOW_GEOMETRY = "1440x880"
WINDOW_MIN_SIZE = (1024, 768)

# Colours mirroring the original WinForms app.
COL_KEY_IDLE = "#98fb98"   # 152,251,152  pale green
COL_KEY_SEL = "#ff3030"    # 255,48,48    selected red
COL_KEY_MAPPED = "#add8e6"  # light blue: written this session on the cur. layer
# mkp-plat-03: native Aqua renders neither `bg` nor `relief` on a tk.Button
# face, so on macOS the colours above are invisible and a selected key looks
# exactly like an idle one — with nothing else to go on, since selection is the
# only state that has no text of its own. `highlightbackground` IS rendered
# there, so every key also carries a ring in its current state colour. The
# thickness never changes, so reserving it costs no reflow, and on Linux and
# Windows the ring simply reads as a border around the matching fill.
KEY_STATE_RING_PX = 3
PROFILE_VERSION = 1
# mkp-input-01: the device model has 3 layers, key ids 1..18 (12 keys + 2
# knobs × 3), and 176 for the LED. A profile entry outside these is invalid.
VALID_LAYERS = frozenset((1, 2, 3))
VALID_KEY_IDS = frozenset(range(1, 19)) | {176}
COL_MENU = "#c8c8a9"
COL_CONNECTED = "#188a18"
COL_DISCONNECTED = "#c83232"


# --------------------------------------------------------------------------- #
#  USB / device layer (port of HidLib + the FormMain send routines)
# --------------------------------------------------------------------------- #
def _sized_named_font(name: str, size: int, weight: str = "normal"):
    """A resized copy of Tk's named font `name`.

    mkp-plat-02: a named font like "TkFixedFont" is a whole font spec, valid
    only on its own. Inside a `(family, size)` tuple Tk reads it as a font
    *family*, which no platform has, so each substitutes its own default —
    the log pane renders proportional everywhere, in a different font per OS,
    while looking like it asked for monospace.
    """
    spec = tkfont.nametofont(name).copy()
    spec.configure(size=size, weight=weight)
    return spec


class KeypadDevice:
    """Thin pyusb wrapper around the keypad's HID OUT interface.

    All handle-touching methods are serialized by a lock so the background
    download/connect threads never race the connection poller on libusb.
    """

    def __init__(self, log=lambda *a: None):
        self.log = log
        self.dev: Any = None       # opaque pyusb Device handle (loosely typed)
        self.ep_out: Any = None    # OUT endpoint, or None -> control SET_REPORT
        self.intf: Any = None
        self._detached = False
        # mkp-plat-01: the connection poller runs once a second for the life of
        # the process, so a permanent condition must be reported once, not
        # every tick.
        self._backend_missing_logged = False
        self._lock = threading.RLock()

    @property
    def connected(self):
        return self.dev is not None

    def connect(self):
        """Find the device and claim interface 1.  Returns True on success."""
        if not _USB_OK:
            return False
        with self._lock:
            if self.dev is not None:
                return True
            dev: Any = None
            try:
                dev = usb.core.find(idVendor=VID, idProduct=PID)
                if dev is None:
                    return False
                self._detach_kernel_driver(dev)
                intf = self._claim_interface(dev)
                ep_out: Any = usb.util.find_descriptor(
                    intf,
                    custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
                    == usb.util.ENDPOINT_OUT,
                )
                self.dev = dev
                self.intf = intf
                self.ep_out = ep_out  # may be None -> use control SET_REPORT
                self.log(f"Connected: VID={VID:04X} PID={PID:04X} "
                         f"interface={intf.bInterfaceNumber} "
                         f"out_ep={'ctrl' if ep_out is None else hex(ep_out.bEndpointAddress)}")
                return True
            except usb.core.NoBackendError:
                self._report_missing_backend()
                self._rollback_connect(dev)
                return False
            except usb.core.USBError as e:
                self.log(f"USB error on connect: {e}")
                self._rollback_connect(dev)
                return False
            except Exception as e:  # pragma: no cover
                self.log(f"connect() failed: {e}")
                self._rollback_connect(dev)
                return False

    def _report_missing_backend(self) -> None:
        """Say what is missing once, instead of once per poll.

        mkp-plat-01: pyusb imports fine without libusb, which is the default
        state on Windows and macOS, and then every `usb.core.find` raises
        NoBackendError. On Linux with no device attached `find` just returns
        None and the poll is silent, so this noise is platform-asymmetric —
        and the generic handler's message never said libusb was the problem.
        """
        if self._backend_missing_logged:
            return
        self._backend_missing_logged = True
        self.log(
            "No USB backend: pyusb is installed but libusb-1.0 is not. "
            "Install libusb-1.0 (Windows: a libusb driver/DLL; "
            "macOS: `brew install libusb`). Retrying quietly.")

    def _rollback_connect(self, dev: Any) -> None:
        if dev is None:
            return
        self.dev = dev
        self.close()

    def _detach_kernel_driver(self, dev: Any):
        """Detach usbhid from the HID interface so we can claim it (Linux)."""
        try:
            if dev.is_kernel_driver_active(HID_INTERFACE):
                dev.detach_kernel_driver(HID_INTERFACE)
                self._detached = True
        except NotImplementedError:
            pass  # non-Linux backend: nothing to detach
        except usb.core.USBError as e:
            self.log(f"Cannot detach kernel driver on interface {HID_INTERFACE} "
                     f"(need root or a udev rule?): {e}")

    @staticmethod
    def _claim_interface(dev: Any) -> Any:
        cfg = dev.get_active_configuration()
        try:
            return cfg[(HID_INTERFACE, 0)]
        except (KeyError, IndexError):
            # mkp-obs-01: surface the fallback so a device whose HID interface
            # number differs from HID_INTERFACE is diagnosable instead of
            # silently claiming interface 0.
            LOG.warning(
                "HID interface %d not found; falling back to interface 0",
                HID_INTERFACE)
            return cfg[(0, 0)]

    def still_connected(self):
        """Cheap liveness check; drops state if the device vanished."""
        with self._lock:
            if self.dev is None:
                return False
            try:
                self.dev.get_active_configuration()
                return True
            except Exception:
                self.close()
                return False

    def close(self):
        with self._lock:
            if self.dev is not None:
                self._reattach_kernel_driver()
                try:
                    usb.util.dispose_resources(self.dev)
                except Exception as e:
                    self.log(f"dispose_resources failed: {e}")
            self.dev = None
            self.ep_out = None
            self.intf = None

    def _reattach_kernel_driver(self):
        """Re-bind usbhid so the keypad works as a normal HID after we exit."""
        if not self._detached or self.dev is None:
            return
        try:
            self.dev.attach_kernel_driver(HID_INTERFACE)
        except Exception as e:
            self.log(f"Failed to re-attach kernel driver on interface "
                     f"{HID_INTERFACE}; the keypad may stay inactive until you "
                     f"replug it: {e}")
        self._detached = False

    def write_device(self, report_id, buf8):
        """Send one output report: reportID + 8 data bytes padded to REPORT_LEN.

        Mirrors HidLib.WriteDevice (Data[0..7]) -> HID output report.  Output
        reports are idempotent, so a transient USBError is retried with a short
        backoff before giving up.  The backoff sleeps *outside* the device lock
        so the connection poller is not blocked while a write is retrying.
        Returns True on success.
        """
        data = self._frame(report_id, buf8)
        reason = "unknown"
        for attempt in range(WRITE_RETRIES + 1):
            ok, reason = self._attempt_send(report_id, data)
            if ok:
                return True
            if reason == "not connected":
                return False                # no point retrying a missing device
            if attempt < WRITE_RETRIES:
                self.log(f"write retry {attempt + 1}/{WRITE_RETRIES}: {reason}")
                time.sleep(WRITE_RETRY_BACKOFF_S * (attempt + 1))
        self.log(f"write failed after {WRITE_RETRIES + 1} attempts: {reason}")
        return False

    @staticmethod
    def _frame(report_id, buf8):
        data = bytearray(REPORT_LEN + 1)
        data[0] = report_id & 0xFF
        for i in range(8):
            data[1 + i] = buf8[i] & 0xFF
        return data

    def _attempt_send(self, report_id, data):
        """One lock-held send attempt.  Returns (ok, reason)."""
        with self._lock:
            if self.dev is None:
                return False, "not connected"
            try:
                if self._send_once(report_id, data) > 0:
                    return True, ""
                return False, "device returned 0 bytes"
            except usb.core.USBError as e:
                return False, str(e)

    def _send_once(self, report_id, data):
        assert self.dev is not None        # guarded by _attempt_send under the lock
        if self.ep_out is not None:
            return self.ep_out.write(bytes(data), WRITE_TIMEOUT_MS)
        # No OUT endpoint -> HID SET_REPORT over control (output report=0x02).
        wValue = (0x02 << 8) | (report_id & 0xFF)
        return self.dev.ctrl_transfer(
            0x21, 0x09, wValue,
            self.intf.bInterfaceNumber if self.intf else HID_INTERFACE,
            bytes(data), WRITE_TIMEOUT_MS)


# --------------------------------------------------------------------------- #
#  Working buffer (port of FormMain.KeyParam + the per-page click handlers)
# --------------------------------------------------------------------------- #
class KeyParam:
    # Fixed buffer index meanings (from the C# statics).
    KeySet_KeyNum = 0
    KeyType_Num = 1
    KeyGroupCharNum = 2
    KeySet_KeyValNum = 3
    Key_Fun_Num = 4

    def __init__(self):
        self.data = bytearray(65)
        self.KeyChar = [None] * 100
        self.FunKeyChar = [None] * 100
        self.KEY_Char_Num = 5      # moving write pointer
        self.FunKEY_Char_Num = 0
        self.ReportID = 0
        self.KEY_Cur_Layer = 1
        self.KEY_Cur_Page = 1

    # -- housekeeping --------------------------------------------------------
    def clear_key_char(self):
        for i in range(100):
            self.KeyChar[i] = None
            self.FunKeyChar[i] = None
        self.FunKEY_Char_Num = 0

    def set_key_init(self):
        self.KEY_Char_Num = 5
        d = self.data
        d[self.KeyType_Num] = 0
        d[self.KeyGroupCharNum] = 0
        d[self.KeySet_KeyValNum] = 0
        d[self.Key_Fun_Num] = 0
        for i in range(0, 19):
            d[5 + i] = 0

    def select_physical_key(self, key_id):
        """KEYn_Click / Kn_*_Click: pick a physical key (disabled on LED page)."""
        if self.KEY_Cur_Page == 4:
            return False
        self.data[self.KeySet_KeyNum] = key_id
        self.set_key_init()
        self.clear_key_char()
        return True

    def key_cleared(self):
        self.clear_key_char()
        self.set_key_init()
        self.data[self.KeySet_KeyNum] = 0

    # -- bounds guards (the C# buffer is fixed-size; clicking past it is a
    #    no-op here instead of an IndexError) --------------------------------
    def _fits(self, idx):
        return 0 <= idx < len(self.data)

    def _store_char(self, arr, idx, value):
        if 0 <= idx < len(arr):
            arr[idx] = value

    # -- KEY page (BasicKeys) ------------------------------------------------
    def _general_char_set(self):
        self.data[self.KeyType_Num] |= 1
        self.KEY_Char_Num += 2
        self.data[self.KeyGroupCharNum] += 1

    def basic_key(self, keycode, label):
        if not self._fits(self.KEY_Char_Num):
            return False
        self.data[self.KEY_Char_Num] = keycode & 0xFF
        self._store_char(self.KeyChar, self.KEY_Char_Num - 5, label)
        self._general_char_set()
        return True

    def basic_modifier(self, bit, name):
        """Key_Ctrl/Shift/Alt/Win on the KEY page."""
        self.data[self.KEY_Char_Num - 1] |= bit
        self._store_char(self.FunKeyChar, self.FunKEY_Char_Num, name)
        self.data[self.KeyType_Num] |= 1
        self.FunKEY_Char_Num += 1

    # -- Ctrl/Shift/Alt page (FunKey) ---------------------------------------
    def _fun_general_char_set(self):
        self.data[self.KeyType_Num] |= 1
        self.FunKEY_Char_Num += 1

    def fun_modifier(self, bit, name):
        self.data[self.KEY_Char_Num - 1] |= bit
        self._store_char(self.FunKeyChar, self.FunKEY_Char_Num, name)
        self._fun_general_char_set()

    def fun_combo(self, mods):
        """mods: list of (bit, name) applied in sequence (e.g. Ctrl+Alt)."""
        for bit, name in mods:
            self.fun_modifier(bit, name)

    def shift_and(self, keycode, label):
        """Shift+<symbol> buttons."""
        kc = self.KEY_Char_Num
        if self.data[kc - 1] != 0:
            kc += 2
        if not self._fits(kc):
            return False
        self.KEY_Char_Num = kc
        self.data[kc - 1] |= 2                          # Shift
        self.data[kc] = keycode & 0xFF
        self._store_char(self.KeyChar, kc - 5, label)
        self._general_char_set()
        self.FunKEY_Char_Num += 1
        return True

    # -- Unicode-entry macro (keyboard page; OS types the glyph) -------------
    def _add_keystroke(self, mods, keycode, label):
        """Append one keystroke with held modifiers; False if the buffer is full."""
        if not self._fits(self.KEY_Char_Num):
            return False
        self.data[self.KEY_Char_Num - 1] |= mods & 0xFF
        return self.basic_key(keycode, label)

    @staticmethod
    def _unicode_seq(cp, platform):
        """Keystrokes [(mods, keycode, label), ...] for one codepoint, or None.

        Both supported methods hold a modifier across the digits and commit on
        release, so they fit the firmware's 5-keystroke ceiling:
          linux  -- Ctrl+Shift held over U + 4 hex (GTK/IBus)
          darwin -- Option held over 4 hex (Unicode Hex Input)
        Only the Basic Multilingual Plane (4 hex digits) is supported.
        """
        if cp > 0xFFFF:
            return None
        digits = format(cp, "04x")
        if platform == "linux":
            mods = 1 | 2                       # Ctrl + Shift
            seq = [(mods, HID_U, chr(cp))]
            seq += [(mods, HEX_HID[h], "") for h in digits]
            return seq
        if platform == "darwin":
            mods = 4                            # Alt / Option
            return [(mods, HEX_HID[h], chr(cp) if i == 0 else "")
                    for i, h in enumerate(digits)]
        return None

    def unicode_macro(self, cp, platform):
        """Program the selected key as the OS Unicode-entry macro for `cp`.

        Returns False if the platform is unsupported or the macro will not fit.
        """
        seq = self._unicode_seq(cp, platform)   # BMP -> at most 5 keystrokes
        if not seq:
            return False
        self.set_key_init()
        self.clear_key_char()
        for mods, keycode, label in seq:
            if not self._add_keystroke(mods, keycode, label):
                return False
        return True

    # -- Multimedia page (MULKey) -------------------------------------------
    def _mul_general_char_set(self):
        self.data[self.KeyType_Num] |= 2

    def multimedia(self, name, v_rid0, v_rid2, v_rid_other):
        """Each tuple is (target_index_offset, value).  Selects by ReportID."""
        base = 5
        if self.ReportID == 0:
            off, val = v_rid0
        elif self.ReportID == 2:
            off, val = v_rid2
        else:
            off, val = v_rid_other
        # The multimedia slot spans 2 bytes from the write pointer; refuse a
        # full buffer like the other mutators do (mouse checks kc + 4).
        if not (self._fits(base + off) and self._fits(self.KEY_Char_Num + 1)):
            return False
        self.data[base + off] = val & 0xFF
        self._store_char(self.KeyChar, 0, name)
        self._mul_general_char_set()
        return True

    # -- Mouse page (MouseKey) ----------------------------------------------
    def _mouse_general_char_set(self):
        self.data[self.KeyType_Num] |= 3

    def mouse(self, name, b0, b1, b2, b3, b4=None):
        kc = self.KEY_Char_Num
        if not self._fits(kc + 4):
            return False
        self._mouse_general_char_set()
        self.data[kc] = b0 & 0xFF
        self.data[kc + 1] = b1 & 0xFF
        self.data[kc + 2] = b2 & 0xFF
        self.data[kc + 3] = b3 & 0xFF
        if b4 is not None:
            self.data[kc + 4] = b4 & 0xFF
        self._store_char(self.KeyChar, kc - 5, name)
        return True

    # -- LED page (LEDkey) ---------------------------------------------------
    def led(self, mode, name):
        self.data[self.KeySet_KeyNum] = 176
        self.data[self.KeyType_Num] |= 8
        self.data[2] = mode & 0xFF
        self._store_char(self.KeyChar, self.KEY_Char_Num - 5, name)

    # -- display helpers -----------------------------------------------------
    def key_text(self):
        if self.data[self.KeySet_KeyNum] == 0:
            return ""
        parts = [self.KeyChar[i] or "" for i in (0, 2, 4, 6, 8)]
        return " ".join(p for p in parts if p)

    def fun_text(self):
        if self.data[self.KeySet_KeyNum] == 0:
            return ""
        parts = [self.FunKeyChar[i] or "" for i in (0, 1, 2, 3)]
        return " ".join(p for p in parts if p)

    # -- download payload assembly (pure; testable without a device) --------
    def _swlayer_buf(self):
        buf = bytearray(8)
        buf[0] = 0xA1
        buf[1] = self.KEY_Cur_Layer or 1
        return buf

    def _kbd_pair(self, b):
        """(arr[4], arr[5]) byte pair for keyboard group index b."""
        d = self.data
        if b == 0:
            return d[4], 0
        return d[2 + 2 * b], d[3 + 2 * b]

    def _kbd_reports(self, key, type_byte):
        d = self.data
        groups = min(d[self.KeyGroupCharNum], MAX_KBD_GROUPS)
        truncated = d[self.KeyGroupCharNum] > MAX_KBD_GROUPS
        bufs = []
        for b in range(0, groups + 1):
            arr = bytearray(8)
            arr[0] = key
            arr[1] = type_byte
            arr[2] = groups
            arr[3] = b
            arr[4], arr[5] = self._kbd_pair(b)
            bufs.append(arr)
        return bufs, "kbd", truncated

    def _mul_reports(self, key, type_byte):
        d = self.data
        arr = bytearray(8)
        arr[0] = key
        arr[1] = type_byte
        arr[2] = d[5]
        arr[3] = d[6]
        return [arr], "kbd", False

    def _led_reports(self, key, type_byte):
        arr = bytearray(8)
        arr[0] = key
        arr[1] = type_byte
        arr[2] = self.data[2]
        return [arr], "led", False

    def _mouse_reports(self, key, type_byte):
        d = self.data
        arr = bytearray(8)
        arr[0] = key
        arr[1] = type_byte
        arr[2] = d[5]
        arr[3] = d[6]
        arr[4] = d[7]
        arr[5] = d[8]
        arr[6] = d[9]
        return [arr], "kbd", False

    def build_download_reports(self):
        """Assemble the ordered output reports for the current assignment.

        Returns (reports, flash, truncated) where reports is a list of 8-byte
        buffers to send with self.ReportID, flash is 'kbd' or 'led', and
        truncated flags a keyboard macro clipped to MAX_KBD_GROUPS.
        Returns None when there is nothing to download.
        """
        d = self.data
        key = d[self.KeySet_KeyNum]
        if key == 0:
            return None
        reports = []
        if self.ReportID == 0:
            d[self.KeyType_Num] &= 15
            type_byte = d[self.KeyType_Num]
        else:
            reports.append(self._swlayer_buf())
            type_byte = ((self.KEY_Cur_Layer << 4) | d[self.KeyType_Num]) & 0xFF
        kind = d[self.KeyType_Num] & 0xF
        builder = {1: self._kbd_reports, 2: self._mul_reports,
                   8: self._led_reports, 3: self._mouse_reports}.get(kind)
        if builder is None:
            return None
        body, flash, truncated = builder(key, type_byte)
        reports.extend(body)
        return reports, flash, truncated


# --------------------------------------------------------------------------- #
#  Key tables (extracted verbatim from BasicKeys.cs / FunKey.cs / MULKey.cs)
# --------------------------------------------------------------------------- #
# (label, HID usage code) — BasicKeys page
BASIC_ROWS = [
    [("A", 4), ("B", 5), ("C", 6), ("D", 7), ("E", 8), ("F", 9), ("G", 10),
     ("H", 11), ("I", 12), ("J", 13), ("K", 14), ("L", 15), ("M", 16)],
    [("N", 17), ("O", 18), ("P", 19), ("Q", 20), ("R", 21), ("S", 22), ("T", 23),
     ("U", 24), ("V", 25), ("W", 26), ("X", 27), ("Y", 28), ("Z", 29)],
    [("1", 30), ("2", 31), ("3", 32), ("4", 33), ("5", 34), ("6", 35), ("7", 36),
     ("8", 37), ("9", 38), ("0", 39), ("-", 45), ("=", 46), ("`", 53)],
    [("[", 47), ("]", 48), ("\\", 49), (";", 51), ("'", 52), (",", 54), (".", 55),
     ("/", 56), ("Enter", 40), ("ESC", 41), ("BkSp", 42), ("Tab", 43), ("Space", 44)],
    [("F1", 58), ("F2", 59), ("F3", 60), ("F4", 61), ("F5", 62), ("F6", 63),
     ("F7", 64), ("F8", 65), ("F9", 66), ("F10", 67), ("F11", 68), ("F12", 69),
     ("CapsLk", 57)],
    [("PrtSc", 70), ("ScrLk", 71), ("Pause", 72), ("Ins", 73), ("Home", 74),
     ("PgUp", 75), ("Del", 76), ("End", 77), ("PgDn", 78), ("Menu", 101),
     ("NumLk", 83), ("→", 79), ("←", 80)],
    [("↑", 82), ("↓", 81), ("KP/", 84), ("KP*", 85), ("KP-", 86), ("KP+", 87),
     ("KP1", 89), ("KP2", 90), ("KP3", 91), ("KP4", 92), ("KP5", 93), ("KP6", 94),
     ("KP7", 95)],
    [("KP8", 96), ("KP9", 97), ("KP0", 98), ("KP.", 99)],
]
# KEY-page modifiers (bit, name)
BASIC_MODS = [(1, "Ctrl"), (2, "Shift"), (4, "Alt"), (8, "Win")]

# Ctrl/Shift/Alt page — single modifiers (left & right) and combos
FUN_MODS = [
    ("L-Ctrl", [(1, "Ctrl")]), ("L-Shift", [(2, "Shift")]),
    ("L-Alt", [(4, "Alt")]), ("L-Win", [(8, "Win")]),
    ("R-Ctrl", [(16, "Ctrl")]), ("R-Shift", [(32, "Shift")]),
    ("R-Alt", [(64, "Alt")]), ("R-Win", [(128, "Win")]),
    ("Ctrl+Alt", [(1, "Ctrl"), (4, "Alt")]),
    ("Ctrl+Shift", [(1, "Ctrl"), (2, "Shift")]),
    ("Alt+Shift", [(4, "Alt"), (2, "Shift")]),
    ("Shift+Win", [(2, "Shift"), (8, "Win")]),
    ("Ctrl+Shift+Alt", [(1, "Ctrl"), (2, "Shift"), (4, "Alt")]),
    ("Ctrl+Alt+Win", [(1, "Ctrl"), (4, "Alt"), (8, "Win")]),
    ("C+A+S+Win", [(1, "Ctrl"), (4, "Alt"), (2, "Shift"), (8, "Win")]),
]
# Shift+<symbol>: (label, keycode) — the shifted character of that physical key
FUN_SHIFTED = [
    ("~", 53), ("!", 30), ("@", 31), ("#", 32), ("$", 33), ("%", 34), ("^", 35),
    ("&", 36), ("*", 37), ("(", 38), (")", 39), ("_", 45), ("+", 46), ("{", 47),
    ("}", 48), ("|", 49), (":", 51), ('"', 52), ("<", 54), (">", 55), ("?", 56),
]

# Multimedia — (label, rid0=(off,val), rid2=(off,val), other=(off,val))
MULTIMEDIA = [
    ("Play/Pause", (0, 64), (1, 4), (0, 205)),
    ("Vol +", (0, 2), (0, 64), (0, 233)),
    ("Vol -", (0, 1), (0, 128), (0, 234)),
    ("Mute", (0, 4), (1, 1), (0, 226)),
    ("Prev", (0, 128), (1, 11), (0, 182)),
    ("Next", (1, 1), (1, 10), (0, 181)),
]

# Mouse — (label, b0,b1,b2,b3[,b4])
MOUSE = [
    ("L Click", (1, 0, 0, 0)),
    ("R Click", (2, 0, 0, 0)),
    ("M Click", (4, 0, 0, 0)),
    ("Wheel +", (0, 0, 0, 1)),
    ("Wheel -", (0, 0, 0, 0xFF)),
    ("Ctrl+Wheel↑", (0, 0, 0, 1, 1)),
    ("Ctrl+Wheel↓", (0, 0, 0, 0xFF, 1)),
    ("Shift+Wheel↑", (0, 0, 0, 1, 2)),
    ("Shift+Wheel↓", (0, 0, 0, 0xFF, 2)),
    ("Alt+Wheel↑", (0, 0, 0, 1, 4)),
    ("Alt+Wheel↓", (0, 0, 0, 0xFF, 4)),
]

LED_MODES = [("LED Off / Mode 0", 0), ("LED Mode 1", 1), ("LED Mode 2", 2)]

# Physical keys: (label, key_id).  12 keys + 2 knobs (L/press/R) = ids 1..18
PHYS_KEYS = [("KEY%d" % i, i) for i in range(1, 13)]
KNOB1 = [("◀", 13), ("●", 14), ("▶", 15)]
KNOB2 = [("◀", 16), ("●", 17), ("▶", 18)]

# --------------------------------------------------------------------------- #
#  Extended-script keycaps.  A USB HID keyboard sends *scancodes*, not Unicode,
#  so each glyph is the HID usage code of the US-QWERTY key POSITION that yields
#  that glyph under the standard national layout.  Sent as-is they produce the
#  glyph only when the matching OS keyboard layout is active; "Unicode mode"
#  (below) instead replays an OS Unicode-entry macro so the glyph appears
#  regardless of the active layout.  (glyph, scancode) pairs.
# --------------------------------------------------------------------------- #
# Greek + Russian Cyrillic share one block (distinct scripts, no overlap).
GREEK_CYRILLIC_KEYS = [
    # Greek (standard layout positions)
    ("α", 4), ("β", 5), ("γ", 10), ("δ", 7), ("ε", 8), ("ζ", 29), ("η", 11),
    ("θ", 24), ("ι", 12), ("κ", 14), ("λ", 15), ("μ", 16), ("ν", 17), ("ξ", 13),
    ("ο", 18), ("π", 19), ("ρ", 21), ("σ", 22), ("ς", 26), ("τ", 23), ("υ", 28),
    ("φ", 9), ("χ", 27), ("ψ", 6), ("ω", 25),
    # Russian Cyrillic (ЙЦУКЕН layout positions)
    ("а", 9), ("б", 54), ("в", 7), ("г", 24), ("д", 15), ("е", 23), ("ё", 53),
    ("ж", 51), ("з", 19), ("и", 5), ("й", 20), ("к", 21), ("л", 14), ("м", 25),
    ("н", 28), ("о", 13), ("п", 10), ("р", 11), ("с", 6), ("т", 17), ("у", 8),
    ("ф", 4), ("х", 47), ("ц", 26), ("ч", 27), ("ш", 12), ("щ", 18), ("ъ", 48),
    ("ы", 22), ("ь", 16), ("э", 52), ("ю", 55), ("я", 29),
]
HEBREW_KEYS = [
    ("א", 23), ("ב", 6), ("ג", 7), ("ד", 22), ("ה", 25), ("ו", 24), ("ז", 29),
    ("ח", 13), ("ט", 28), ("י", 11), ("כ", 9), ("ל", 14), ("מ", 17), ("נ", 5),
    ("ס", 27), ("ע", 10), ("פ", 19), ("צ", 16), ("ק", 8), ("ר", 21), ("ש", 4),
    ("ת", 54), ("ך", 15), ("ם", 18), ("ן", 12), ("ף", 51), ("ץ", 55),
]
# Germanic + Nordic: glyphs the basic US page lacks.  Scancodes are the single
# key that yields the glyph on the respective national layout (German QWERTZ /
# the Nordic layouts); Swedish ä/ö coincide with German.
GERMANIC_KEYS = [
    ("ä", 52), ("ö", 51), ("ü", 47), ("ß", 45),    # German
    ("å", 47), ("æ", 51), ("ø", 52),               # Danish / Norwegian / Swedish
]

# Accented Latin merged across Portuguese / French / Spanish / Italian.  Most
# come from dead-key sequences that vary per layout and cannot be one scancode,
# so the scancode here is the BASE letter (typed in scancode mode); Unicode mode
# types the real accented glyph on any layout.
LATIN_KEYS = [
    ("á", 4), ("à", 4), ("â", 4), ("ã", 4),
    ("é", 8), ("è", 8), ("ê", 8), ("ë", 8),
    ("í", 12), ("ì", 12), ("î", 12), ("ï", 12),
    ("ó", 18), ("ò", 18), ("ô", 18), ("õ", 18),
    ("ú", 24), ("ù", 24), ("û", 24), ("ü", 24),
    ("ç", 6), ("ñ", 17), ("œ", 18), ("¿", 56), ("¡", 30),
]

# Layouts offered by the Keys-tab combobox; None == the US basic keyboard page.
LAYOUTS = [
    ("US (basic)", None),
    ("Greek / Cyrillic", GREEK_CYRILLIC_KEYS),
    ("Hebrew", HEBREW_KEYS),
    ("German / Nordic", GERMANIC_KEYS),
    ("Latin (accents)", LATIN_KEYS),
]

# HID usage codes for the hex digits used by Unicode-entry macros.
HEX_HID = {"0": 39, "1": 30, "2": 31, "3": 32, "4": 33, "5": 34, "6": 35,
           "7": 36, "8": 37, "9": 38,
           "a": 4, "b": 5, "c": 6, "d": 7, "e": 8, "f": 9}
HID_U = 24            # 'u' key, for the Linux Ctrl+Shift+U entry method


def _unicode_platform():
    """Which OS Unicode-entry method to use, or None if unsupported here."""
    if sys.platform.startswith("linux"):
        return "linux"      # GTK/IBus: hold Ctrl+Shift, press U then 4 hex
    if sys.platform == "darwin":
        return "darwin"     # macOS Unicode Hex Input: hold Option, 4 hex
    return None             # Windows hex-alt is unreliable for these scripts


def _ibus_available():
    """True if IBus looks installed/active (binary on PATH or selected IM)."""
    if shutil.which("ibus"):
        return True
    im = " ".join(os.environ.get(v, "")
                  for v in ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS"))
    return "ibus" in im.lower()


def _unicode_available():
    """Whether the OS Unicode-entry method this app uses is actually present."""
    plat = _unicode_platform()
    if plat == "linux":
        return _ibus_available()
    return plat == "darwin"   # macOS ships Unicode Hex Input; Windows: no


def _available_layouts():
    """Full layout list when Unicode entry is available, else US basic only.

    The extended scripts are only reliably usable through the Unicode-entry
    method, so when it is missing we offer just the default US keyboard.
    """
    return LAYOUTS if _unicode_available() else LAYOUTS[:1]


# --------------------------------------------------------------------------- #
#  Collaborators (Tk-free, independently testable)
# --------------------------------------------------------------------------- #
class ProfileStore:
    """Session assignment map + JSON profile persistence.

    The device is write-only, so this map is the only record of what was
    written: (layer, key_id) -> {"data": bytes, "desc": str[, "ambiguous"]}.
    """

    def __init__(self):
        self.assignments = {}

    def save(self, path):
        """Write the map to `path` as JSON (atomic temp+rename, fsynced)."""
        payload = {"version": PROFILE_VERSION, "assignments": [
            self._assignment_payload(layer, kid, rec)
            for (layer, kid), rec in self.assignments.items()]}
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                # mkp-robust-21: without fsync a crash after the rename can
                # atomically replace a good profile with a truncated one.
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            self._fsync_dir(os.path.dirname(path) or ".")
        except BaseException:
            # mkp-robust-20: a failed/interrupted write must not leave an
            # orphaned <path>.tmp behind.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _fsync_dir(dirpath):
        """Best-effort: persist the rename (unsupported on some OS/filesystems)."""
        try:
            fd = os.open(dirpath, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _assignment_payload(layer, kid, rec):
        item = {
            "layer": layer,
            "key_id": kid,
            "desc": rec["desc"],
            "data": rec["data"].hex(),
        }
        if rec.get("ambiguous"):
            item["ambiguous"] = True
        return item

    def load(self, path):
        """Replace the map from a JSON profile. Returns the count."""
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        # mkp-rel-02: a top-level JSON array/scalar has no .get(), which would
        # raise AttributeError — not in _load_dialog's caught set — and crash
        # the handler. Reject a non-object payload as a ValueError so it
        # surfaces as "Load failed" like every other malformed profile.
        if not isinstance(payload, dict):
            raise ValueError("profile must be a JSON object")
        # mkp-rob-01: fail closed on an unknown/future format instead of
        # silently loading mismatched fields and pushing wrong bytes to the
        # device. _load_dialog surfaces the ValueError as "Load failed".
        version = payload.get("version")
        if version != PROFILE_VERSION:
            raise ValueError(
                "unsupported profile version %r (expected %d)"
                % (version, PROFILE_VERSION))
        size = len(KeyParam().data)
        loaded = {}
        for item in payload.get("assignments", []):
            key, rec = self._validated_entry(item, size)
            loaded[key] = rec
        self.assignments = loaded
        return len(loaded)

    @staticmethod
    def _validated_entry(item, size):
        data = bytes.fromhex(item["data"])
        if len(data) != size:
            raise ValueError("bad assignment buffer length")
        layer, kid = int(item["layer"]), int(item["key_id"])
        # mkp-input-01: reject out-of-range entries instead of storing an
        # assignment the UI can never display yet _write_all still replays
        # to the device.
        if layer not in VALID_LAYERS or kid not in VALID_KEY_IDS:
            raise ValueError(
                "assignment out of range: layer=%r key_id=%r" % (layer, kid))
        # mkp-input-02: the device is programmed from the buffer's own key
        # byte (data[0]) while the UI colours by key_id; a mismatched entry
        # would silently program a different physical key (or the LED).
        if data[KeyParam.KeySet_KeyNum] != kid:
            raise ValueError(
                "assignment key byte %d does not match key_id %d"
                % (data[KeyParam.KeySet_KeyNum], kid))
        rec = {"data": data, "desc": str(item.get("desc", ""))}
        if item.get("ambiguous"):
            rec["ambiguous"] = True
        return (layer, kid), rec


class ConnectionMonitor:
    """Off-thread connect/liveness probing with stall detection.

    Owns the shared device-I/O mutex (`io_busy`) and the probe token that
    lets a stalled probe be abandoned without a late result clobbering fresh
    state. All UI work goes through the injected callbacks; `dev` is a
    callable because the owner may swap the device instance at runtime.
    """

    def __init__(
        self,
        dev,
        log,
        schedule,
        post,
        on_state,
        on_connected,
        on_stall=lambda: None,
    ):
        self._dev = dev            # () -> current KeypadDevice
        self._log = log
        self._schedule = schedule  # (delay_ms, fn): run fn on the UI thread later
        self._post = post          # (fn): marshal fn onto the UI thread
        self._on_state = on_state
        self._on_connected = on_connected
        self._on_stall = on_stall
        self.io_busy = False
        self.token = 0
        self.probe_thread = None
        self.unresponsive = False

    def poll(self):
        if not self.io_busy and self.probe_thread is None:
            if self._dev().connected:
                # mkp-thread-01: still_connected() queries the device config
                # under the device lock; run it off the Tk thread like the
                # connect probe so a slow bus never freezes the UI.
                self._start_probe_worker(self.probe_alive, "Device liveness")
            else:
                self._start_probe_worker(self.try_connect, "Connect")
        self._on_state()
        self._schedule(CONNECTION_POLL_MS, self.poll)

    def _start_probe_worker(self, target, label):
        self.io_busy = True
        self.unresponsive = False
        self.token += 1
        token = self.token
        try:
            worker = threading.Thread(
                target=lambda: target(token), daemon=True
            )
            self.probe_thread = worker
            worker.start()
        except Exception as e:
            LOG.exception("%s probe failed to start", label)
            self._log("%s probe error: %s" % (label, e))
            self.probe_thread = None
            self.io_busy = False
            self._on_state()
            return
        self._schedule(
            PROBE_TIMEOUT_MS,
            lambda token=token, label=label: self.probe_timeout(token, label),
        )

    def probe_timeout(self, token, label):
        if token != self.token or self.probe_thread is None:
            return
        self.token += 1
        self.probe_thread = None
        self.io_busy = False
        self.unresponsive = True
        self._on_stall()
        self._log(
            "%s probe stalled; abandoned the USB handle and will reconnect"
            % label
        )
        self._on_state()

    def probe_alive(self, token=None):
        """Off-thread liveness check (mkp-thread-01)."""
        try:
            alive = self._dev().still_connected()
        except Exception:
            alive = False
        self._post(lambda: self.probe_done(alive, token))

    def _probe_settled(self, token):
        if token is not None and token != self.token:
            return False
        if token is not None:
            self.token += 1
        self.probe_thread = None
        self.unresponsive = False
        self.io_busy = False
        self._on_state()
        return True

    def probe_done(self, alive, token=None):
        if not self._probe_settled(token):
            return
        if not alive:
            self._log("Device disconnected")

    def try_connect(self, token=None):
        """Runs off the Tk thread so the connect/version probe never freezes UI."""
        try:
            ok = self._dev().connect()
            if ok:
                self._on_connected()
        except Exception as e:                 # never strand io_busy True
            LOG.exception("connect worker crashed")
            self._log("Connect error: %s" % e)
            ok = False
        self._post(lambda: self.connect_done(ok, token))

    def connect_done(self, ok, token=None):
        del ok                                 # logged by the device layer
        self._probe_settled(token)


# --------------------------------------------------------------------------- #
#  Application
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MINI-KeyBoard configurator")
        self.geometry(WINDOW_GEOMETRY)
        self.minsize(*WINDOW_MIN_SIZE)

        # Cross-thread UI marshalling: background device threads enqueue
        # callables; only the Tk main thread ever touches widgets.
        self._ui_q = queue.SimpleQueue()

        self.kp = KeyParam()
        # mkp-state-50: KeyParam starts at ReportID 0, which is also the
        # no-answer fallback — untrue until _version_check says otherwise.
        self._report_id_confirmed = False
        self.dev = KeypadDevice(log=self.log)
        # mkp-rob-50: devices abandoned by a stalled probe, waiting for their
        # orphan thread to leave libusb so the handle can be released.
        self._stalled_devices: queue.SimpleQueue = queue.SimpleQueue()
        self._reaper_lock = threading.Lock()
        self._device_reaper = None
        self._monitor = ConnectionMonitor(
            dev=lambda: self.dev, log=self.log, schedule=self.after,
            post=self._ui_q.put, on_state=self._update_state,
            on_connected=self._version_check,
            on_stall=self._replace_stalled_device)
        self._profiles = ProfileStore()
        self._phys_buttons = {}     # key_id -> Button
        self._phys_base = {}        # key_id -> base button label
        self._selected_id = None
        self._destroyed = False
        self._pending = None        # candidate assignment awaiting write ACK
        self._action_buttons = []   # disabled while a write is in flight
        # Only offer the extended scripts when the OS Unicode-entry method is
        # present; otherwise fall back to the default US keyboard only.
        self._uni_available = _unicode_available()
        self._layouts = _available_layouts()

        self._build_ui()
        self._drain_ui()
        self._poll_connection()
        if not self._uni_available:
            self.log("No OS Unicode input method (IBus / macOS Hex Input) "
                     "detected; extended-script layouts disabled (US only).")

    # ---- collaborator state under the historical names --------------------
    # The write paths and the tests address the shared I/O mutex, probe token,
    # and session map through App; the collaborators own them.
    @property
    def _io_busy(self):
        return self._monitor.io_busy

    @_io_busy.setter
    def _io_busy(self, value):
        self._monitor.io_busy = value

    @property
    def _probe_token(self):
        return self._monitor.token

    @_probe_token.setter
    def _probe_token(self, value):
        self._monitor.token = value

    @property
    def _assignments(self):
        return self._profiles.assignments

    @_assignments.setter
    def _assignments(self, value):
        self._profiles.assignments = value

    # ---- UI construction --------------------------------------------------
    def _build_ui(self):
        # Pack the log FIRST at the bottom so it reserves a fixed strip; the
        # main area then expands above it instead of squeezing the keypad.
        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="x", side="bottom", padx=6, pady=(0, 6))
        self.log_box = scrolledtext.ScrolledText(
            logf, height=7, state="disabled",
            font=_sized_named_font("TkFixedFont", 9))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

        root = ttk.Frame(self, padding=6)
        root.pack(side="top", fill="both", expand=True)

        # ---- left column: device, layers, physical keys ----
        left = ttk.Frame(root)
        left.pack(side="left", fill="y", padx=(0, 8))

        self.state_lbl = tk.Label(left, text="Not connected", width=22,
                                  bg=COL_DISCONNECTED, fg="white", relief="ridge",
                                  font=_sized_named_font("TkDefaultFont", 10, "bold"))
        self.state_lbl.pack(fill="x", pady=(0, 6))

        lf = ttk.LabelFrame(left, text="Layer")
        lf.pack(fill="x", pady=(0, 6))
        self.layer_var = tk.IntVar(value=1)
        for i in (1, 2, 3):
            ttk.Radiobutton(lf, text="Layer %d" % i, value=i,
                            variable=self.layer_var,
                            command=self._on_layer).pack(side="left", padx=4, pady=2)

        uf = ttk.LabelFrame(left, text="Unicode mode")
        uf.pack(fill="x", pady=(0, 6))
        self.unicode_var = tk.BooleanVar(value=False)
        if self._uni_available:
            label = "Type glyph via OS (%s)" % _unicode_platform()
            state = "normal"
        else:
            label = "Unavailable (no IBus / Hex Input)"
            state = "disabled"
        ttk.Checkbutton(uf, variable=self.unicode_var, text=label, state=state
                        ).pack(anchor="w", padx=4, pady=2)

        kf = ttk.LabelFrame(left, text="Keys")
        kf.pack(fill="x", pady=(0, 6))
        grid = ttk.Frame(kf)
        grid.pack(padx=4, pady=4)
        for idx, (label, kid) in enumerate(PHYS_KEYS):
            r, c = divmod(idx, 4)
            b = tk.Button(grid, text=label, width=7, height=2, bg=COL_KEY_IDLE,
                          highlightbackground=COL_KEY_IDLE,
                          highlightthickness=KEY_STATE_RING_PX,
                          command=lambda k=kid: self._select_key(k))
            b.grid(row=r, column=c, padx=3, pady=3)
            self._phys_buttons[kid] = b
            self._phys_base[kid] = label

        for name, knob in (("Knob 1", KNOB1), ("Knob 2", KNOB2)):
            nf = ttk.LabelFrame(left, text=name + "  (rotate / press)")
            nf.pack(fill="x", pady=(0, 6))
            row = ttk.Frame(nf)
            row.pack(padx=4, pady=4)
            for label, kid in knob:
                b = tk.Button(row, text=label, width=6, height=2, bg=COL_KEY_IDLE,
                              highlightbackground=COL_KEY_IDLE,
                              highlightthickness=KEY_STATE_RING_PX,
                              command=lambda k=kid: self._select_key(k))
                b.pack(side="left", padx=3)
                self._phys_buttons[kid] = b
                self._phys_base[kid] = label

        # current assignment display
        disp = ttk.LabelFrame(left, text="Current key assignment")
        disp.pack(fill="x", pady=(0, 6))
        self.set_text = tk.Entry(disp)
        self.set_text.pack(fill="x", padx=4, pady=(4, 2))
        self.fun_text = tk.Entry(disp)
        self.fun_text.pack(fill="x", padx=4, pady=(0, 4))

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="Clear", command=self._clear).pack(side="left", expand=True, fill="x", padx=2)
        self.dl_btn = ttk.Button(btns, text="Write ▶", command=self._download)
        self.dl_btn.pack(side="left", expand=True, fill="x", padx=2)

        # Profile row: persist the session map (the device cannot be read back)
        # and replay every saved key in one pass.
        pbtns = ttk.Frame(left)
        pbtns.pack(fill="x", pady=(4, 0))
        self.wa_btn = ttk.Button(pbtns, text="Write all", command=self._write_all)
        save_btn = ttk.Button(pbtns, text="Save…", command=self._save_dialog)
        load_btn = ttk.Button(pbtns, text="Load…", command=self._load_dialog)
        for b in (save_btn, load_btn, self.wa_btn):
            b.pack(side="left", expand=True, fill="x", padx=2)
        self._action_buttons = [self.dl_btn, self.wa_btn, save_btn, load_btn]

        self.dl_status = tk.Label(left, text="", anchor="center")
        self.dl_status.pack(fill="x", pady=(4, 0))

        # ---- right column: function pages ----
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        nb.bind("<<NotebookTabChanged>>", self._on_page)
        self.nb = nb

        # The KEY page and all extended scripts share one "Keys" tab; the
        # layout is picked by a combobox (US basic + Greek/Cyrillic/Hebrew/
        # accented-Latin), so they all map to firmware page 1.
        self.tab_keys = self._build_keys_tab(nb)
        self.tab_fun = self._build_fun_tab(nb)
        self.tab_mul = self._build_mul_tab(nb)
        self.tab_led = self._build_led_tab(nb)
        self.tab_mouse = self._build_mouse_tab(nb)
        nb.add(self.tab_keys, text="Keys")
        nb.add(self.tab_fun, text="Ctrl Shift Alt")
        nb.add(self.tab_mul, text="Multimedia")
        nb.add(self.tab_led, text="LED")
        nb.add(self.tab_mouse, text="Mouse")
        # page index -> KEY_Cur_Page value used by the firmware
        self._page_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}

        if not _USB_OK:
            self.log("pyusb not available: %s" % _USB_ERR)
            self.log("Install with:  pip install pyusb   (needs a libusb backend)")

    def _scroll_area(self, parent):
        """Pack a vertical-scroll canvas into `parent`; return the inner body."""
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        vbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        self._bind_wheel(canvas)
        return body

    def _scroll_tab(self, nb):
        """A notebook tab whose whole body scrolls vertically.

        Returns (outer, body): add `outer` to the notebook, fill `body`.
        """
        outer = ttk.Frame(nb)
        return outer, self._scroll_area(outer)

    def _bind_wheel(self, canvas):
        """Route the mouse wheel to `canvas` while the pointer is over it."""
        def scroll(amount):
            canvas.yview_scroll(amount, "units")  # pragma: no cover - wheel glue
        canvas.bind("<Enter>", lambda _e: (
            canvas.bind_all("<MouseWheel>",
                            lambda ev: scroll(-1 if ev.delta > 0 else 1)),
            canvas.bind_all("<Button-4>", lambda _ev: scroll(-1)),
            canvas.bind_all("<Button-5>", lambda _ev: scroll(1))))
        canvas.bind("<Leave>", lambda _e: (
            canvas.unbind_all("<MouseWheel>"),
            canvas.unbind_all("<Button-4>"),
            canvas.unbind_all("<Button-5>")))

    def _grid_buttons(self, parent, items, on_click, per_row=8, width=11):
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, padx=6, pady=6)
        for i, item in enumerate(items):
            r, c = divmod(i, per_row)
            label = item[0]
            b = tk.Button(frame, text=label, width=width, height=2,
                          command=lambda it=item: on_click(it))
            b.grid(row=r, column=c, padx=2, pady=2, sticky="nsew")
        return frame

    def _build_keys_tab(self, nb):
        """Single Keys tab: a layout combobox over a fill-to-panel keycap grid."""
        tab = ttk.Frame(nb)
        bar = ttk.Frame(tab)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        ttk.Label(bar, text="Layout:").pack(side="left")
        self.layout_var = tk.StringVar(value=self._layouts[0][0])
        cb = ttk.Combobox(bar, textvariable=self.layout_var, state="readonly",
                          width=18, values=[name for name, _ in self._layouts])
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda _e: self._render_layout())
        self._keys_body = ttk.Frame(tab)
        self._keys_body.pack(fill="both", expand=True, padx=4, pady=4)
        self._render_layout()
        return tab

    def _render_layout(self):
        for widget in self._keys_body.winfo_children():
            widget.destroy()
        entries = dict(self._layouts).get(self.layout_var.get())
        if entries is None:
            self._render_basic(self._keys_body)
        else:
            self._render_script(self._keys_body, entries)

    @staticmethod
    def _fill_grid(parent, rows):
        """Lay buttons out on a weighted grid so every cell stretches to fill."""
        widest = max(len(r) for r in rows)
        for c in range(widest):
            parent.columnconfigure(c, weight=1, uniform="keys")
        for r, row in enumerate(rows):
            parent.rowconfigure(r, weight=1)
            for c, button in enumerate(row):
                button.grid(row=r, column=c, sticky="nsew", padx=1, pady=1)

    def _render_basic(self, body):
        grid = ttk.Frame(body)
        grid.pack(fill="both", expand=True)
        rows = [[tk.Button(grid, text=label,
                           command=lambda lbl=label, c=code: self._basic_key(c, lbl))
                 for label, code in row]
                for row in BASIC_ROWS]
        self._fill_grid(grid, rows)
        # Modifiers stay a distinct labelled panel below the key grid.
        mf = ttk.LabelFrame(body, text="Modifiers (combine with a key)")
        mf.pack(fill="x", padx=4, pady=(6, 0))
        mgrid = ttk.Frame(mf)
        mgrid.pack(fill="x", padx=2, pady=2)
        mods = [tk.Button(mgrid, text=name,
                          command=lambda b=bit, n=name: self._basic_mod(b, n))
                for bit, name in BASIC_MODS]
        self._fill_grid(mgrid, [mods])

    def _render_script(self, body, entries):
        note = ttk.Label(
            body, foreground="#555", wraplength=900,
            text="Scancode mode sends the national-layout key (needs that OS "
                 "layout active; accented Latin sends the base letter).  Turn on "
                 "Unicode mode to type the glyph itself on any layout.")
        note.pack(anchor="w", fill="x", padx=6, pady=(2, 4))
        grid = ttk.Frame(body)
        grid.pack(fill="both", expand=True)
        per_row = 11
        buttons = [tk.Button(grid, text=glyph,
                             command=lambda g=glyph, s=scancode: self._script_key(g, s))
                   for glyph, scancode in entries]
        rows = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
        self._fill_grid(grid, rows)

    def _build_fun_tab(self, nb):
        tab, body = self._scroll_tab(nb)
        mf = ttk.LabelFrame(body, text="Modifiers & combos")
        mf.pack(anchor="w", fill="x", padx=6, pady=6)
        for i, (label, mods) in enumerate(FUN_MODS):
            r, c = divmod(i, 5)
            tk.Button(mf, text=label, width=14, height=2,
                      command=lambda m=mods: self._fun_combo(m)
                      ).grid(row=r, column=c, padx=2, pady=2)
        sf = ttk.LabelFrame(body, text="Shift + symbol")
        sf.pack(anchor="w", fill="x", padx=6, pady=6)
        for i, (label, code) in enumerate(FUN_SHIFTED):
            r, c = divmod(i, 11)
            tk.Button(sf, text=label, width=4, height=2,
                      command=lambda lbl=label, c2=code: self._shift_and(c2, lbl)
                      ).grid(row=r, column=c, padx=2, pady=2)
        return tab

    def _build_mul_tab(self, nb):
        tab, body = self._scroll_tab(nb)
        self._grid_buttons(body, MULTIMEDIA,
                           lambda it: self._multimedia(it), per_row=3, width=14)
        return tab

    def _build_mouse_tab(self, nb):
        tab, body = self._scroll_tab(nb)
        self._grid_buttons(body, MOUSE,
                           lambda it: self._mouse(it), per_row=4, width=13)
        return tab

    def _build_led_tab(self, nb):
        tab, body = self._scroll_tab(nb)
        self._grid_buttons(body, LED_MODES,
                           lambda it: self._led(it), per_row=1, width=20)
        return tab

    # ---- cross-thread UI plumbing ----------------------------------------
    def log(self, msg):
        """Thread-safe: mirror to the terminal logger and the GUI log pane."""
        text = str(msg)
        LOG.info("%s", text)
        self._ui_q.put(lambda m=text: self._append_log(m))

    def _append_log(self, msg):
        try:
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self._trim_log_lines()
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            print(msg)

    def _trim_log_lines(self):
        try:
            line_count = int(str(self.log_box.index("end-1c")).split(".", 1)[0])
        except (AttributeError, ValueError, IndexError):
            return
        excess = line_count - MAX_LOG_LINES
        if excess > 0:
            self.log_box.delete("1.0", f"{excess + 1}.0")

    def _drain_ui(self):
        try:
            while True:
                self._run_ui_callback(self._ui_q.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.after(UI_DRAIN_INTERVAL_MS, self._drain_ui)

    def _run_ui_callback(self, callback):
        try:
            callback()
        except Exception:
            LOG.exception("queued UI callback failed")

    # ---- physical-key colour handling ------------------------------------
    def _refresh_key_map(self):
        """Repaint/label physical keys: mapped-this-layer, selected, or idle."""
        layer = self.kp.KEY_Cur_Layer
        for kid, btn in self._phys_buttons.items():
            base = self._phys_base[kid]
            rec = self._assignments.get((layer, kid))
            if rec:
                btn.configure(
                    text="%s\n%s" % (base, rec["desc"][:8]),
                    bg=COL_KEY_MAPPED,
                    highlightbackground=COL_KEY_MAPPED,
                    relief="ridge",
                )
            else:
                btn.configure(text=base, bg=COL_KEY_IDLE,
                              highlightbackground=COL_KEY_IDLE, relief="raised")
        if self._selected_id is not None:
            self._phys_buttons[self._selected_id].configure(
                bg=COL_KEY_SEL, highlightbackground=COL_KEY_SEL, relief="sunken"
            )

    def _select_key(self, key_id):
        if not self.kp.select_physical_key(key_id):
            self.log("Key selection disabled on LED page")
            return  # LED page: selection disabled
        self._selected_id = key_id
        self._refresh_key_map()
        self._refresh_display()
        self.log("Selected key id %d (layer %d)" % (key_id, self.kp.KEY_Cur_Layer))

    # ---- page / layer events ---------------------------------------------
    def _on_page(self, _event=None):
        idx = self.nb.index(self.nb.select())
        page = self._page_map.get(idx, 1)
        self.kp.KEY_Cur_Page = page
        self._clear()

    def _on_layer(self):
        self.kp.KEY_Cur_Layer = self.layer_var.get()
        # PageBet_Inte_Cmd=1 -> Key_Clear_Fun on next tick in the original.
        self._clear()
        self.log("Layer -> %d" % self.kp.KEY_Cur_Layer)

    # ---- function-button handlers ----------------------------------------
    def _need_key(self):
        if self.kp.data[KeyParam.KeySet_KeyNum] == 0 and self.kp.KEY_Cur_Page != 4:
            self.log("Pick a physical key first.")
            return False
        return True

    def _dropped(self, label):
        """A mutator refused the click (buffer full); tell the user."""
        self.log("Key buffer full; '%s' ignored." % label)

    def _apply_mutation(self, mutate, label):
        if not self._need_key():
            return False
        changed = mutate()
        if not changed:
            self._dropped(label)
        self._refresh_display()
        return changed

    def _basic_key(self, code, label):
        self._apply_mutation(lambda: self.kp.basic_key(code, label), label)

    def _script_key(self, glyph, scancode):
        """Extended-script keycap: scancode, or Unicode macro when toggled on."""
        if self.unicode_var.get():
            self._unicode_char(glyph)
        else:
            self._basic_key(scancode, glyph)

    def _unicode_char(self, glyph):
        platform = _unicode_platform()
        if platform is None:
            if self._need_key():
                self.log(
                    "Unicode mode not supported on this OS (%s)." % sys.platform
                )
            return
        if self._apply_mutation(
                lambda: self.kp.unicode_macro(ord(glyph), platform), glyph):
            self.log("Unicode %s -> U+%04X macro (%s)" % (glyph, ord(glyph), platform))

    def _basic_mod(self, bit, name):
        if not self._need_key():
            return
        self.kp.basic_modifier(bit, name)
        self._refresh_display()

    def _fun_combo(self, mods):
        if not self._need_key():
            return
        self.kp.fun_combo(mods)
        self._refresh_display()

    def _shift_and(self, code, label):
        self._apply_mutation(lambda: self.kp.shift_and(code, label), label)

    def _multimedia(self, item):
        name, r0, r2, ro = item
        self._apply_mutation(
            lambda: self.kp.multimedia(name, r0, r2, ro), name
        )

    def _mouse(self, item):
        name, vals = item
        self._apply_mutation(lambda: self.kp.mouse(name, *vals), name)

    def _led(self, item):
        name, mode = item
        self.kp.led(mode, name)
        self._refresh_display()
        self.log("LED -> %s" % name)

    def _clear(self):
        self.kp.key_cleared()
        self._selected_id = None
        self._refresh_key_map()
        self._refresh_display()

    def _refresh_display(self):
        self.set_text.delete(0, "end")
        self.set_text.insert(0, self.kp.key_text())
        self.fun_text.delete(0, "end")
        self.fun_text.insert(0, self.kp.fun_text())

    # ---- connection polling (delegates to ConnectionMonitor) --------------
    def _poll_connection(self):
        self._monitor.poll()

    def _probe_timeout(self, token, label):
        self._monitor.probe_timeout(token, label)

    def _replace_stalled_device(self):
        """Install a fresh device and hand the abandoned one to the reaper
        (mkp-rob-50).

        The stalled probe thread may still be inside libusb holding the old
        device's lock, so closing it here would freeze the Tk thread. Queue it
        instead: a single daemon reaper drains the queue and each close() waits
        on that lock, releasing the handle (and re-attaching usbhid) the moment
        the orphan returns. Previously nothing ever released it, so every stall
        leaked one libusb handle for the life of the process.
        """
        stalled, self.dev = self.dev, KeypadDevice(log=self.log)
        self._stalled_devices.put(stalled)
        self._ensure_device_reaper()

    def _ensure_device_reaper(self):
        with self._reaper_lock:
            if self._device_reaper is not None and self._device_reaper.is_alive():
                return
            try:
                self._device_reaper = threading.Thread(
                    target=self._reap_stalled_devices,
                    name="device-reaper", daemon=True)
                self._device_reaper.start()
            except Exception as e:
                LOG.exception("stalled-device reaper failed to start")
                self._device_reaper = None
                self.log("Device cleanup error: %s" % e)

    def _reap_stalled_devices(self):
        """Close abandoned devices, blocking on each one's lock until its
        orphan probe leaves libusb. Exits once the queue is drained; the
        re-check under `_reaper_lock` closes the race with a producer that
        enqueued just as this thread was giving up."""
        while True:
            try:
                device = self._stalled_devices.get_nowait()
            except queue.Empty:
                with self._reaper_lock:
                    if self._stalled_devices.empty():
                        self._device_reaper = None
                        return
                continue
            try:
                device.close()
            except Exception as e:
                LOG.debug("could not release stalled device: %s", e)

    def _probe_alive(self, token=None):
        self._monitor.probe_alive(token)

    def _probe_done(self, alive, token=None):
        self._monitor.probe_done(alive, token)

    def _try_connect(self, token=None):
        self._monitor.try_connect(token)

    def _connect_done(self, ok, token=None):
        self._monitor.connect_done(ok, token)

    def _update_state(self):
        if self._monitor.unresponsive:
            self.state_lbl.configure(
                text="USB unresponsive", bg=COL_DISCONNECTED
            )
        elif self.dev.connected:
            self.state_lbl.configure(text="Connected", bg=COL_CONNECTED)
        else:
            self.state_lbl.configure(text="Not connected", bg=COL_DISCONNECTED)

    def _version_check(self):
        """Port of KeyBoardVersion_Check (WriteMode==1): probe report IDs 3,0,2."""
        zero = bytearray(8)
        for rid in (3, 0, 2):
            if self.dev.write_device(rid, zero):
                self._ui_q.put(lambda found=rid: self._apply_report_id(
                    found, "Keyboard reportID = %d" % found))
                return
        self._ui_q.put(lambda: self._apply_report_id(
            0, "Version check: no reportID accepted, defaulting to 0",
            confirmed=False))

    def _apply_report_id(self, report_id, message, confirmed=True):
        self.kp.ReportID = report_id
        # mkp-state-50: 0 is both a real report ID and the no-answer fallback.
        # Only the device-reported case says anything about the protocol.
        self._report_id_confirmed = confirmed
        self.log(message)

    # ---- send routines (port of FormMain.Download_Click etc.) ------------
    @staticmethod
    def _flash_buf(flash):
        buf = bytearray(8)
        buf[0] = 0xAA
        buf[1] = 0xA1 if flash == "led" else 0xAA
        return buf

    def _flash_status(self, text, fg, bg):
        self.dl_status.configure(text=text, fg=fg, bg=bg)
        self.after(
            STATUS_CLEAR_MS,
            lambda: self.dl_status.configure(
                text="", fg="black", bg=self.cget("bg")
            ),
        )

    def _dl_result(self, ok):
        if ok:
            text, bg = "Write success", COL_CONNECTED
        else:
            text, bg = "Write failed", COL_DISCONNECTED
        self._flash_status(text, "white", bg)
        self.log(text)

    def _dl_note(self, msg):
        """Neutral, visible feedback for the 'nothing to send' paths."""
        self._flash_status(msg, "black", self.cget("bg"))
        self.log(msg)

    def _precheck_write(self, action):
        if self._io_busy:
            self._dl_note("Busy, try again")
            return False
        if not self.dev.connected:
            self.log("%s ignored: device not connected." % action)
            self._dl_result(False)
            return False
        return True

    def _download(self):
        if not self._precheck_write("Write"):
            return
        result = self.kp.build_download_reports()
        if result is None:
            self._dl_note("Nothing to write (no key / function assigned).")
            return
        reports, flash, truncated = result
        if truncated:
            self.log("Macro longer than %d groups; sending first %d."
                     % (MAX_KBD_GROUPS, MAX_KBD_GROUPS))
        desc = (self.kp.key_text() + " " + self.kp.fun_text()).strip() or "raw"
        self._pending = (self.kp.KEY_Cur_Layer,
                         self.kp.data[KeyParam.KeySet_KeyNum],
                         bytes(self.kp.data), desc)
        self._run_download(reports, flash)

    def _send_reports(self, reports, flash_buf, rid):
        """Push every report, ACK-checked, then the flash commit.

        Each write_device must ACK (the device returned >0 bytes) before the
        next is sent, so a report that did not land aborts the sequence.
        Returns the stage that failed:
          'ok'      -- every report + the flash commit ACKed
          'reports' -- a report failed BEFORE the commit; nothing persisted,
                       the previous flash mapping is still intact
          'flash'   -- the commit itself failed; persisted state is ambiguous
        """
        total = len(reports)
        for i, buf in enumerate(reports, 1):
            if not self.dev.write_device(rid, buf):
                self.log("Write: report %d/%d not acknowledged" % (i, total))
                return "reports"
            LOG.debug("report %d/%d acked", i, total)
        if not self.dev.write_device(rid, flash_buf):
            self.log("Write: flash commit not acknowledged")
            return "flash"
        self.log("Write: %d reports + flash committed and acknowledged" % total)
        return "ok"

    def _set_actions(self, state):
        for b in self._action_buttons:
            b.configure(state=state)

    def _start_action_worker(self, target, label):
        try:
            threading.Thread(target=target, daemon=True).start()
            return True
        except Exception as e:
            LOG.exception("%s worker failed to start", label)
            self.log("%s error: %s" % (label, e))
            self._io_busy = False
            self._set_actions("normal")
            self._update_state()
            return False

    def _spawn_io_worker(self, work, done, label, failure_result):
        self._io_busy = True
        self._set_actions("disabled")
        rid = self.kp.ReportID

        def worker():
            try:
                result = work(rid)
            except Exception as e:
                LOG.exception("%s worker crashed", label.lower())
                self.log("%s error: %s" % (label, e))
                result = failure_result
            self._ui_q.put(lambda: done(result))

        self._start_action_worker(worker, label)

    def _run_download(self, reports, flash):
        """Send all reports on a worker thread; the UI stays responsive."""
        flash_buf = self._flash_buf(flash)
        self._spawn_io_worker(
            lambda rid: self._send_reports(reports, flash_buf, rid),
            self._download_done,
            "Write",
            "error",
        )

    def _download_done(self, outcome):
        self._io_busy = False
        self._set_actions("normal")
        if outcome == "ok" and self._pending:
            self._record_pending_assignment(ambiguous=False)
            self._refresh_key_map()
        elif outcome == "reports":
            # Failure before the (last) flash commit: nothing was persisted.
            self.log("Not committed -- previous mapping intact. Press Write to retry.")
        elif outcome in ("flash", "error"):
            # Commit step ambiguous: re-writing resends the whole sequence.
            if self._pending:
                self._record_pending_assignment(ambiguous=True)
                self._refresh_key_map()
            self.log("Commit may be partial -- write again to be safe.")
        self._pending = None
        self._dl_result(outcome == "ok")

    def _apply_write_all_outcomes(self, results):
        """Sync each key's `ambiguous` flag with this run's outcome (mkp-di-50).

        Same post-commit semantics as _download_done: 'flash'/'error' left the
        device state ambiguous, 'reports' persisted nothing (so the previous
        verdict stands). A successful rewrite must CLEAR the flag — without
        that a key stayed marked ambiguous forever once any write-all had
        failed on it, even after a later run confirmed it. The single-key path
        clears it implicitly because _record_pending_assignment replaces the
        whole record. Returns True when the key map needs a repaint.
        """
        changed = False
        for key, outcome in results:
            rec = self._assignments.get(key)
            if rec is None:
                continue
            if outcome in ("flash", "error"):
                if not rec.get("ambiguous"):
                    rec["ambiguous"] = True
                    changed = True
            elif outcome == "ok" and rec.pop("ambiguous", None) is not None:
                changed = True
        return changed

    def _record_pending_assignment(self, ambiguous):
        pending = self._pending
        if pending is None:
            return
        layer, kid, data, desc = pending
        rec = {"data": data, "desc": desc}
        if ambiguous:
            rec["ambiguous"] = True
        self._assignments[(layer, kid)] = rec

    # ---- session profile: save / load / replay ---------------------------
    @staticmethod
    def _key_name(kid):
        special = {13: "Knob1-L", 14: "Knob1-Press", 15: "Knob1-R",
                   16: "Knob2-L", 17: "Knob2-Press", 18: "Knob2-R", 176: "LED"}
        if 1 <= kid <= 12:
            return "KEY%d" % kid
        return special.get(kid, "id%d" % kid)

    def _save_profile(self, path):
        self._profiles.save(path)

    def _load_profile(self, path):
        count = self._profiles.load(path)
        self._refresh_key_map()
        return count

    def _save_dialog(self):
        path = filedialog.asksaveasfilename(  # pragma: no cover - dialog glue
            title="Save profile", defaultextension=".json",
            filetypes=[("JSON profile", "*.json")])
        if not path:  # pragma: no cover - dialog glue
            return
        try:  # pragma: no cover - dialog glue
            self._save_profile(path)
            self.log("Saved %d key(s) to %s" % (len(self._assignments), path))
        except OSError as e:  # pragma: no cover - dialog glue
            self.log("Save failed: %s" % e)

    def _load_dialog(self):
        path = filedialog.askopenfilename(  # pragma: no cover - dialog glue
            title="Load profile", filetypes=[("JSON profile", "*.json")])
        if not path:  # pragma: no cover - dialog glue
            return
        try:  # pragma: no cover - dialog glue
            self.log("Loaded %d key(s) from %s" % (self._load_profile(path), path))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as e:  # pragma: no cover
            # mkp-rel-01: a profile with a JSON null/list where an int/hex string
            # is expected raises TypeError from int()/bytes.fromhex(); catch it
            # too so a malformed profile reports "Load failed" instead of
            # crashing the handler.
            self.log("Load failed: %s" % e)

    def _reports_for(self, layer, data):
        """Rebuild the report sequence for a saved key on the current device."""
        kp = KeyParam()
        kp.data = bytearray(data)
        kp.KEY_Cur_Layer = layer
        kp.ReportID = self.kp.ReportID
        return kp.build_download_reports()

    def _write_all(self):
        if not self._precheck_write("Write-all"):
            return
        if not self._assignments:
            self._dl_note("Nothing saved to write.")
            return
        if not self._confirm_layer_collapse():
            self._dl_note("Write-all cancelled.")
            return
        jobs = []
        for (layer, kid), rec in self._assignments.items():
            built = self._reports_for(layer, rec["data"])
            if built is None:
                self.log("Write-all: %s has nothing to send" % self._key_name(kid))
                continue
            reports, flash, _ = built
            jobs.append(((layer, kid), reports, self._flash_buf(flash)))
        self._run_write_all(jobs)

    def _confirm_layer_collapse(self):
        """Confirm a multi-layer write-all on a reportID-0 device (mkp-state-50).

        `build_download_reports` prepends the 0xA1 layer-switch command only
        when ReportID != 0. With reportID 0 there is no switch, so every saved
        layer is written to whichever layer the device is showing right now and
        the later ones overwrite the earlier — the profile is intact on disk,
        but the device keeps only the last layer written.

        A confirmation rather than a refusal, for two reasons. Programming one
        layer at a time is a legitimate workflow. And reportID 0 is also what
        `_version_check` falls back to when NO report ID was accepted, so the
        value does not reliably mean "this firmware cannot switch layers" —
        `_report_id_confirmed` separates the two cases in the prompt.

        Returns True when the write should go ahead.
        """
        if self.kp.ReportID != 0:
            return True
        layers = sorted({layer for layer, _kid in self._assignments})
        if len(layers) < 2:
            return True
        listed = ", ".join(str(x) for x in layers)
        self.log(
            "Write-all: reportID 0 sends no layer-switch command, so layers "
            "%s will all be written to the layer the device is on now and "
            "overwrite each other. Select one layer at a time to program them "
            "separately." % listed
        )
        unconfirmed = "" if self._report_id_confirmed else (
            "\n\nThe version check accepted no report ID, so 0 here is the "
            "fallback default rather than a value the device reported."
        )
        return messagebox.askyesno(
            "Layers will overwrite each other",
            "reportID 0 sends no layer-switch command, so layers %s will all "
            "be written to the layer the device is on now and overwrite each "
            "other.%s\n\nSelect one layer at a time to program them "
            "separately.\n\nWrite all layers anyway?" % (listed, unconfirmed),
            default="no",
            parent=self,
        )

    def _run_write_all(self, jobs):
        def write_all(rid):
            results = []
            for key, reports, flash_buf in jobs:
                try:
                    outcome = self._send_reports(reports, flash_buf, rid)
                except Exception as e:
                    LOG.exception("write-all worker crashed")
                    self.log("Write-all: %s error: %s" % (self._key_name(key[1]), e))
                    outcome = "error"
                if outcome != "ok":
                    self.log("Write-all: %s failed (%s)" % (self._key_name(key[1]), outcome))
                results.append((key, outcome))
            return results

        self._spawn_io_worker(
            write_all, self._write_all_done, "Write-all", []
        )

    def _write_all_done(self, results):
        self._io_busy = False
        self._set_actions("normal")
        if self._apply_write_all_outcomes(results):
            self._refresh_key_map()
        ok = sum(1 for _, outcome in results if outcome == "ok")
        total = len(results)
        self.log("Write-all: %d/%d key(s) written" % (ok, total))
        self._dl_result(ok == total and total > 0)

    def destroy(self):
        # Idempotent: closing the window already calls destroy(), and main()'s
        # finally calls it again -- without this guard the second call raises
        # TclError ("application has been destroyed").
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.dev.close()
        finally:
            super().destroy()


def _configure_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")


def _install_signal_handlers(app):
    """Ctrl-C / SIGTERM break the Tk loop so destroy() re-attaches usbhid."""
    def handler(signum, _frame):
        LOG.info("signal %d received; shutting down", signum)
        app.quit()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="MINI-KeyBoard configurator")
    parser.add_argument("--version", action="version",
                        version="minikeypad %s" % __version__)
    parser.add_argument("--auto-install-pyusb", action="store_true",
                        help=f"install missing pyusb with pip ({PYUSB_REQUIREMENT})")
    # mkp-cli-50: this used to be argparse.SUPPRESS, so a flag that overrides
    # both documented opt-ins was invisible in --help.
    parser.add_argument("--no-auto-install", action="store_true",
                        help=("never install pyusb, overriding both "
                              "--auto-install-pyusb and MINIKEYPAD_AUTO_INSTALL=1"))
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose (DEBUG) terminal logging")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    auto = (
        args.auto_install_pyusb
        or os.environ.get("MINIKEYPAD_AUTO_INSTALL") == "1"
    ) and not args.no_auto_install
    if not _USB_OK and auto:
        _ensure_pyusb()
    app = App()
    _install_signal_handlers(app)
    try:
        app.mainloop()
    except KeyboardInterrupt:           # Ctrl-C delivered inside an after() tick
        LOG.info("interrupted")
    finally:
        app.destroy()                   # closes the device -> re-attaches usbhid


if __name__ == "__main__":  # pragma: no cover
    main()
