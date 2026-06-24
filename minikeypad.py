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

Dependencies:
    pip install pyusb        # and a libusb backend (libusb-1.0)
    If pyusb is missing the app offers to install it for you on first run
    (pip install pyusb).  Disable with --no-auto-install or
    MINIKEYPAD_NO_AUTO_INSTALL=1.  The native libusb-1.0 backend still has to
    come from your OS package manager.
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
# pyright: reportMissingImports=false, reportPossiblyUnboundVariable=false

import argparse
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext

try:  # pragma: no cover - import guard (env-dependent: pyusb present)
    import usb.core
    import usb.util
    _USB_OK = True
    _USB_ERR = ""
except Exception as e:  # pragma: no cover - import guard
    _USB_OK = False
    _USB_ERR = repr(e)


def _pip_install(pkg):
    """Install pkg into the current interpreter. argv list, never a shell."""
    base = [sys.executable, "-m", "pip", "install"]
    for extra in ([], ["--user"]):     # --user fallback for system pythons
        try:
            subprocess.check_call(base + extra + [pkg],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.STDOUT)
            return True
        except Exception:
            continue
    return False


def _ensure_pyusb():
    """One-shot attempt to pip-install pyusb, then re-import. Returns ok bool."""
    global _USB_OK, _USB_ERR, usb
    if _USB_OK:
        return True
    print("pyusb not found; attempting automatic install (pip install pyusb)...")
    if not _pip_install("pyusb"):
        print("Automatic install failed. Install manually: pip install pyusb")
        return False
    try:
        import usb.core
        import usb.util
        _USB_OK, _USB_ERR = True, ""
        print("pyusb installed.")
        return True
    except Exception as e:
        _USB_ERR = repr(e)
        print("pyusb installed but import still failed: %s" % _USB_ERR)
        return False


LOG = logging.getLogger("minikeypad")

VID = 0x1189
PID = 0x8890
HID_INTERFACE = 1          # "mi_01"
REPORT_LEN = 64            # data bytes following the report ID
WRITE_TIMEOUT_MS = 500
MAX_KBD_GROUPS = 5         # firmware accepts groups 0..5 (6 keystrokes)
WRITE_RETRIES = 2          # extra attempts after the first on a transient USBError
WRITE_RETRY_BACKOFF_S = 0.05

# Colours mirroring the original WinForms app.
COL_KEY_IDLE = "#98fb98"   # 152,251,152  pale green
COL_KEY_SEL = "#ff3030"    # 255,48,48    selected red
COL_MENU = "#c8c8a9"
COL_CONNECTED = "#188a18"
COL_DISCONNECTED = "#c83232"


# --------------------------------------------------------------------------- #
#  USB / device layer (port of HidLib + the FormMain send routines)
# --------------------------------------------------------------------------- #
class KeypadDevice:
    """Thin pyusb wrapper around the keypad's HID OUT interface.

    All handle-touching methods are serialized by a lock so the background
    download/connect threads never race the connection poller on libusb.
    """

    def __init__(self, log=lambda *a: None):
        self.log = log
        self.dev = None
        self.ep_out = None
        self.intf = None
        self._detached = False
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
            try:
                dev = usb.core.find(idVendor=VID, idProduct=PID)
                if dev is None:
                    return False
                self._detach_kernel_driver(dev)
                intf = self._claim_interface(dev)
                ep_out = usb.util.find_descriptor(
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
            except usb.core.USBError as e:
                self.log(f"USB error on connect: {e}")
                self.dev = None
                return False
            except Exception as e:  # pragma: no cover
                self.log(f"connect() failed: {e}")
                self.dev = None
                return False

    def _detach_kernel_driver(self, dev):
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
    def _claim_interface(dev):
        cfg = dev.get_active_configuration()
        try:
            return cfg[(HID_INTERFACE, 0)]
        except (KeyError, IndexError):
            return cfg[(0, 0)]  # fall back to first interface

    def still_connected(self):
        """Cheap liveness check; drops state if the device vanished."""
        with self._lock:
            if self.dev is None:
                return False
            try:
                if usb.core.find(idVendor=VID, idProduct=PID) is None:
                    self.close()
                    return False
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
                except Exception:
                    pass
            self.dev = None
            self.ep_out = None
            self.intf = None

    def _reattach_kernel_driver(self):
        """Re-bind usbhid so the keypad works as a normal HID after we exit."""
        if not self._detached or self.dev is None:
            return
        try:
            self.dev.attach_kernel_driver(HID_INTERFACE)
        except Exception:
            pass
        self._detached = False

    def write_device(self, report_id, buf8):
        """Send one output report: reportID + 8 data bytes padded to REPORT_LEN.

        Mirrors HidLib.WriteDevice (Data[0..7]) -> HID output report.  Output
        reports are idempotent, so a transient USBError is retried with a short
        backoff before giving up.  Returns True on success.
        """
        with self._lock:
            if self.dev is None:
                return False
            data = self._frame(report_id, buf8)
            return self._send_with_retry(report_id, data)

    @staticmethod
    def _frame(report_id, buf8):
        data = bytearray(REPORT_LEN + 1)
        data[0] = report_id & 0xFF
        for i in range(8):
            data[1 + i] = buf8[i] & 0xFF
        return data

    def _send_once(self, report_id, data):
        assert self.dev is not None        # guarded by write_device under the lock
        if self.ep_out is not None:
            return self.ep_out.write(bytes(data), WRITE_TIMEOUT_MS)
        # No OUT endpoint -> HID SET_REPORT over control (output report=0x02).
        wValue = (0x02 << 8) | (report_id & 0xFF)
        return self.dev.ctrl_transfer(
            0x21, 0x09, wValue,
            self.intf.bInterfaceNumber if self.intf else HID_INTERFACE,
            bytes(data), WRITE_TIMEOUT_MS)

    def _send_with_retry(self, report_id, data):
        reason = "unknown"
        for attempt in range(WRITE_RETRIES + 1):
            try:
                if self._send_once(report_id, data) > 0:
                    return True
                reason = "device returned 0 bytes"
            except usb.core.USBError as e:
                reason = str(e)
            if attempt < WRITE_RETRIES:
                self.log(f"write retry {attempt + 1}/{WRITE_RETRIES}: {reason}")
                time.sleep(WRITE_RETRY_BACKOFF_S * (attempt + 1))
        self.log(f"write failed after {WRITE_RETRIES + 1} attempts: {reason}")
        return False


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
        # ShiftGeneral_Char_Set2
        self.data[self.KeyType_Num] |= 1
        self.KEY_Char_Num += 2
        self.data[self.KeyGroupCharNum] += 1
        self.FunKEY_Char_Num += 1
        return True

    # -- Multimedia page (MULKey) -------------------------------------------
    def _mul_general_char_set(self):
        self.data[self.KeyType_Num] |= 2

    def multimedia(self, name, v_rid0, v_rid2, v_rid_other):
        """Each tuple is (target_index_offset, value).  Selects by ReportID."""
        kc = self.KEY_Char_Num
        if self.ReportID == 0:
            off, val = v_rid0
        elif self.ReportID == 2:
            off, val = v_rid2
        else:
            off, val = v_rid_other
        if not self._fits(kc + off):
            return False
        self.data[kc + off] = val & 0xFF
        self._store_char(self.KeyChar, kc - 5, name)
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
#  Application
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MINI-KeyBoard configurator")
        self.geometry("1040x680")
        self.minsize(900, 600)

        # Cross-thread UI marshalling: background device threads enqueue
        # callables; only the Tk main thread ever touches widgets.
        self._ui_q = queue.SimpleQueue()
        self._io_busy = False

        self.kp = KeyParam()
        self.dev = KeypadDevice(log=self.log)
        self._phys_buttons = {}     # key_id -> Button
        self._selected_id = None

        self._build_ui()
        self._drain_ui()
        self._poll_connection()

    # ---- UI construction --------------------------------------------------
    def _build_ui(self):
        root = ttk.Frame(self, padding=6)
        root.pack(fill="both", expand=True)

        # ---- left column: device, layers, physical keys ----
        left = ttk.Frame(root)
        left.pack(side="left", fill="y", padx=(0, 8))

        self.state_lbl = tk.Label(left, text="Not connected", width=22,
                                  bg=COL_DISCONNECTED, fg="white", relief="ridge",
                                  font=("TkDefaultFont", 10, "bold"))
        self.state_lbl.pack(fill="x", pady=(0, 6))

        lf = ttk.LabelFrame(left, text="Layer")
        lf.pack(fill="x", pady=(0, 6))
        self.layer_var = tk.IntVar(value=1)
        for i in (1, 2, 3):
            ttk.Radiobutton(lf, text="Layer %d" % i, value=i,
                            variable=self.layer_var,
                            command=self._on_layer).pack(side="left", padx=4, pady=2)

        kf = ttk.LabelFrame(left, text="Keys")
        kf.pack(fill="x", pady=(0, 6))
        grid = ttk.Frame(kf)
        grid.pack(padx=4, pady=4)
        for idx, (label, kid) in enumerate(PHYS_KEYS):
            r, c = divmod(idx, 4)
            b = tk.Button(grid, text=label, width=7, height=2, bg=COL_KEY_IDLE,
                          command=lambda k=kid: self._select_key(k))
            b.grid(row=r, column=c, padx=3, pady=3)
            self._phys_buttons[kid] = b

        for name, knob in (("Knob 1", KNOB1), ("Knob 2", KNOB2)):
            nf = ttk.LabelFrame(left, text=name + "  (rotate / press)")
            nf.pack(fill="x", pady=(0, 6))
            row = ttk.Frame(nf)
            row.pack(padx=4, pady=4)
            for label, kid in knob:
                b = tk.Button(row, text=label, width=6, height=2, bg=COL_KEY_IDLE,
                              command=lambda k=kid: self._select_key(k))
                b.pack(side="left", padx=3)
                self._phys_buttons[kid] = b

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
        self.dl_btn = ttk.Button(btns, text="Download ▶", command=self._download)
        self.dl_btn.pack(side="left", expand=True, fill="x", padx=2)

        self.dl_status = tk.Label(left, text="", anchor="center")
        self.dl_status.pack(fill="x", pady=(4, 0))

        # ---- right column: function pages ----
        right = ttk.Frame(root)
        right.pack(side="left", fill="both", expand=True)

        nb = ttk.Notebook(right)
        nb.pack(fill="both", expand=True)
        nb.bind("<<NotebookTabChanged>>", self._on_page)
        self.nb = nb

        self.tab_key = self._build_key_tab(nb)
        self.tab_fun = self._build_fun_tab(nb)
        self.tab_mul = self._build_mul_tab(nb)
        self.tab_led = self._build_led_tab(nb)
        self.tab_mouse = self._build_mouse_tab(nb)
        nb.add(self.tab_key, text="KEY")
        nb.add(self.tab_fun, text="Ctrl Shift Alt")
        nb.add(self.tab_mul, text="Multimedia")
        nb.add(self.tab_led, text="LED")
        nb.add(self.tab_mouse, text="Mouse")
        # page index -> KEY_Cur_Page value used by the firmware
        self._page_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}

        # ---- log ----
        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=False, side="bottom", padx=6, pady=(0, 6))
        self.log_box = scrolledtext.ScrolledText(logf, height=7, state="disabled",
                                                 font=("TkFixedFont", 9))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

        if not _USB_OK:
            self.log("pyusb not available: %s" % _USB_ERR)
            self.log("Install with:  pip install pyusb   (needs a libusb backend)")

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

    def _build_key_tab(self, nb):
        tab = ttk.Frame(nb)
        # letters/numbers/specials
        for row in BASIC_ROWS:
            rf = ttk.Frame(tab)
            rf.pack(anchor="w", padx=6, pady=1)
            for label, code in row:
                tk.Button(rf, text=label, width=6, height=2,
                          command=lambda lbl=label, c=code: self._basic_key(c, lbl)
                          ).pack(side="left", padx=1, pady=1)
        mf = ttk.LabelFrame(tab, text="Modifiers (combine with a key)")
        mf.pack(anchor="w", padx=6, pady=6)
        for bit, name in BASIC_MODS:
            tk.Button(mf, text=name, width=8, height=2,
                      command=lambda b=bit, n=name: self._basic_mod(b, n)
                      ).pack(side="left", padx=2, pady=2)
        return tab

    def _build_fun_tab(self, nb):
        tab = ttk.Frame(nb)
        mf = ttk.LabelFrame(tab, text="Modifiers & combos")
        mf.pack(anchor="w", fill="x", padx=6, pady=6)
        for i, (label, mods) in enumerate(FUN_MODS):
            r, c = divmod(i, 5)
            tk.Button(mf, text=label, width=14, height=2,
                      command=lambda m=mods: self._fun_combo(m)
                      ).grid(row=r, column=c, padx=2, pady=2)
        sf = ttk.LabelFrame(tab, text="Shift + symbol")
        sf.pack(anchor="w", fill="x", padx=6, pady=6)
        for i, (label, code) in enumerate(FUN_SHIFTED):
            r, c = divmod(i, 11)
            tk.Button(sf, text=label, width=4, height=2,
                      command=lambda lbl=label, c2=code: self._shift_and(c2, lbl)
                      ).grid(row=r, column=c, padx=2, pady=2)
        return tab

    def _build_mul_tab(self, nb):
        tab = ttk.Frame(nb)
        self._grid_buttons(tab, MULTIMEDIA,
                           lambda it: self._multimedia(it), per_row=3, width=14)
        return tab

    def _build_mouse_tab(self, nb):
        tab = ttk.Frame(nb)
        self._grid_buttons(tab, MOUSE,
                           lambda it: self._mouse(it), per_row=4, width=13)
        return tab

    def _build_led_tab(self, nb):
        tab = ttk.Frame(nb)
        self._grid_buttons(tab, LED_MODES,
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
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        except Exception:
            print(msg)

    def _drain_ui(self):
        try:
            while True:
                self._ui_q.get_nowait()()
        except queue.Empty:
            pass
        self.after(120, self._drain_ui)

    # ---- physical-key colour handling ------------------------------------
    def _colour_init(self):
        for b in self._phys_buttons.values():
            b.configure(bg=COL_KEY_IDLE)

    def _select_key(self, key_id):
        if not self.kp.select_physical_key(key_id):
            return  # LED page: selection disabled
        self._selected_id = key_id
        self._colour_init()
        self._phys_buttons[key_id].configure(bg=COL_KEY_SEL)
        self._refresh_display()
        self.log("Selected key id %d (layer %d)" % (key_id, self.kp.KEY_Cur_Layer))

    # ---- page / layer events ---------------------------------------------
    def _on_page(self, _event=None):
        idx = self.nb.index(self.nb.select())
        page = self._page_map.get(idx, 1)
        self.kp.KEY_Cur_Page = page
        # LED and Mouse pages clear the working buffer on entry (as in C#).
        if page in (4, 5):
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

    def _basic_key(self, code, label):
        if not self._need_key():
            return
        self.kp.basic_key(code, label)
        self._refresh_display()

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
        if not self._need_key():
            return
        self.kp.shift_and(code, label)
        self._refresh_display()

    def _multimedia(self, item):
        if not self._need_key():
            return
        name, r0, r2, ro = item
        self.kp.multimedia(name, r0, r2, ro)
        self._refresh_display()

    def _mouse(self, item):
        if not self._need_key():
            return
        name, vals = item
        self.kp.mouse(name, *vals)
        self._refresh_display()

    def _led(self, item):
        name, mode = item
        self.kp.led(mode, name)
        self._refresh_display()
        self.log("LED -> %s" % name)

    def _clear(self):
        self.kp.key_cleared()
        self._selected_id = None
        self._colour_init()
        self._refresh_display()

    def _refresh_display(self):
        self.set_text.delete(0, "end")
        self.set_text.insert(0, self.kp.key_text())
        self.fun_text.delete(0, "end")
        self.fun_text.insert(0, self.kp.fun_text())

    # ---- connection polling ----------------------------------------------
    def _poll_connection(self):
        if not self._io_busy:
            if self.dev.connected:
                if not self.dev.still_connected():
                    self.log("Device disconnected")
            else:
                self._io_busy = True
                threading.Thread(target=self._try_connect, daemon=True).start()
        self._update_state()
        self.after(1000, self._poll_connection)

    def _try_connect(self):
        """Runs off the Tk thread so the connect/version probe never freezes UI."""
        try:
            ok = self.dev.connect()
            if ok:
                self._version_check()
        except Exception as e:                 # never strand _io_busy True
            LOG.exception("connect worker crashed")
            self.log("Connect error: %s" % e)
            ok = False
        self._ui_q.put(lambda: self._connect_done(ok))

    def _connect_done(self, ok):
        self._io_busy = False
        self._update_state()

    def _update_state(self):
        if self.dev.connected:
            self.state_lbl.configure(text="Connected", bg=COL_CONNECTED)
        else:
            self.state_lbl.configure(text="Not connected", bg=COL_DISCONNECTED)

    def _version_check(self):
        """Port of KeyBoardVersion_Check (WriteMode==1): probe report IDs 3,0,2."""
        zero = bytearray(8)
        for rid in (3, 0, 2):
            if self.dev.write_device(rid, zero):
                self.kp.ReportID = rid
                self.log("Keyboard reportID = %d" % rid)
                return
        self.kp.ReportID = 0
        self.log("Version check: no reportID accepted, defaulting to 0")

    # ---- send routines (port of FormMain.Download_Click etc.) ------------
    @staticmethod
    def _flash_buf(flash):
        buf = bytearray(8)
        buf[0] = 0xAA
        buf[1] = 0xA1 if flash == "led" else 0xAA
        return buf

    def _dl_result(self, ok):
        if ok:
            self.dl_status.configure(text="Download success", fg="white", bg=COL_CONNECTED)
            self.log("Download success")
        else:
            self.dl_status.configure(text="Download failed", fg="white", bg=COL_DISCONNECTED)
            self.log("Download failed")
        self.after(2500, lambda: self.dl_status.configure(text="", bg=self.cget("bg")))

    def _dl_note(self, msg):
        """Neutral, visible feedback for the 'nothing to send' paths."""
        self.dl_status.configure(text=msg, fg="black", bg=self.cget("bg"))
        self.log(msg)
        self.after(2500, lambda: self.dl_status.configure(text=""))

    def _download(self):
        if self._io_busy:
            self._dl_note("Busy, try again")
            return
        if not self.dev.connected:
            self.log("Download ignored: device not connected.")
            self._dl_result(False)
            return
        result = self.kp.build_download_reports()
        if result is None:
            self._dl_note("Nothing to download (no key / function assigned).")
            return
        reports, flash, truncated = result
        if truncated:
            self.log("Macro longer than %d groups; sending first %d."
                     % (MAX_KBD_GROUPS, MAX_KBD_GROUPS))
        self._run_download(reports, flash)

    def _send_reports(self, reports, flash_buf, rid):
        """Push every report + the flash commit, logging which step fails."""
        total = len(reports)
        for i, buf in enumerate(reports, 1):
            if not self.dev.write_device(rid, buf):
                self.log("Download: report %d/%d failed" % (i, total))
                return False
            LOG.debug("report %d/%d sent", i, total)
        if not self.dev.write_device(rid, flash_buf):
            self.log("Download: flash commit failed")
            return False
        self.log("Download: %d reports + flash committed" % total)
        return True

    def _run_download(self, reports, flash):
        """Send all reports on a worker thread; the UI stays responsive."""
        self._io_busy = True
        self.dl_btn.configure(state="disabled")
        rid = self.kp.ReportID
        flash_buf = self._flash_buf(flash)

        def worker():
            try:
                ok = self._send_reports(reports, flash_buf, rid)
            except Exception as e:             # never strand the disabled button
                LOG.exception("download worker crashed")
                self.log("Download error: %s" % e)
                ok = False
            self._ui_q.put(lambda: self._download_done(ok))

        threading.Thread(target=worker, daemon=True).start()

    def _download_done(self, ok):
        self._io_busy = False
        self.dl_btn.configure(state="normal")
        self._dl_result(ok)

    def destroy(self):
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
    parser.add_argument("--version", action="version", version="minikeypad 1.0")
    parser.add_argument("--no-auto-install", action="store_true",
                        help="do not try to pip install pyusb when it is missing")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose (DEBUG) terminal logging")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    auto = (not args.no_auto_install
            and os.environ.get("MINIKEYPAD_NO_AUTO_INSTALL") != "1")
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
