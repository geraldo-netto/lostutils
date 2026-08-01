#!/usr/bin/env python3
"""Unit + fuzz tests for minikeypad.

The pure working-buffer/download logic and the install helpers run without any
GUI or USB hardware (the device layer is exercised through a fake `usb`
module).  The App tests need a Tk display; they are skipped automatically when
none is available.
"""

import logging
import os
import random
import sys
import time
import types

import pytest

import minikeypad
from minikeypad import KeyParam, MAX_KBD_GROUPS


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Keep write-retry backoff from slowing the suite."""
    monkeypatch.setattr(minikeypad.time, "sleep", lambda _s: None)


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def _select(kp, key_id=1):
    assert kp.select_physical_key(key_id) is True
    return kp


def _built(kp):
    result = kp.build_download_reports()
    assert result is not None
    return result


# ===========================================================================
#  KeyParam — selection / clearing
# ===========================================================================
def test_select_physical_key_disabled_on_led_page():
    kp = KeyParam()
    kp.KEY_Cur_Page = 4
    assert kp.select_physical_key(7) is False
    assert kp.data[KeyParam.KeySet_KeyNum] == 0


def test_key_cleared_resets_selection_and_buffer():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    kp.key_cleared()
    assert kp.data[KeyParam.KeySet_KeyNum] == 0
    assert kp.build_download_reports() is None


# ===========================================================================
#  KeyParam — keyboard packing
# ===========================================================================
def test_basic_key_packs_code_and_advances_pointer():
    kp = _select(KeyParam())
    assert kp.basic_key(4, "A") is True
    assert kp.data[5] == 4
    assert kp.KeyChar[0] == "A"
    assert kp.KEY_Char_Num == 7
    assert kp.data[KeyParam.KeyGroupCharNum] == 1
    assert kp.data[KeyParam.KeyType_Num] & 1


def test_basic_modifier_sets_bit_on_current_char():
    kp = _select(KeyParam())
    kp.basic_modifier(1, "Ctrl")          # writes d[KEY_Char_Num-1] == d[4]
    kp.basic_key(4, "A")
    assert kp.data[4] == 1                 # Ctrl modifier byte for char 0


def test_basic_key_masks_oversized_code():
    kp = _select(KeyParam())
    kp.basic_key(0x1FF, "x")
    assert kp.data[5] == 0xFF


def test_basic_key_refuses_when_buffer_full():
    kp = _select(KeyParam())
    kp.KEY_Char_Num = len(kp.data)        # past the end
    assert kp.basic_key(4, "A") is False


def test_kbd_download_reports_round_trip():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    reports, flash, truncated = _built(kp)
    assert flash == "kbd"
    assert truncated is False
    # one group -> probe (b=0) + the real group (b=1); flush handled by caller
    assert len(reports) == 2
    assert reports[0][3] == 0              # group index b
    assert reports[1][3] == 1
    assert reports[1][5] == 4             # keycode lands in arr[5]
    assert reports[0][0] == 1            # physical key id


def test_kbd_download_truncates_oversized_macro():
    kp = _select(KeyParam())
    for _ in range(MAX_KBD_GROUPS + 3):
        kp.basic_key(4, "A")
    reports, _flash, truncated = _built(kp)
    assert truncated is True
    assert len(reports) == MAX_KBD_GROUPS + 1
    assert all(r[2] == MAX_KBD_GROUPS for r in reports)


# ===========================================================================
#  KeyParam — fun page / shift
# ===========================================================================
def test_fun_combo_applies_each_modifier():
    kp = _select(KeyParam())
    kp.fun_combo([(1, "Ctrl"), (4, "Alt")])
    assert kp.FunKeyChar[0] == "Ctrl"
    assert kp.FunKeyChar[1] == "Alt"


def test_shift_and_packs_shift_bit_and_code():
    kp = _select(KeyParam())
    assert kp.shift_and(30, "!") is True
    assert kp.data[4] & 2                  # shift bit on the modifier byte
    assert kp.data[5] == 30
    assert kp.data[kp.KeyGroupCharNum] == 1


def test_shift_and_advances_when_slot_used():
    kp = _select(KeyParam())
    kp.shift_and(30, "!")
    before = kp.KEY_Char_Num
    kp.shift_and(31, "@")
    assert kp.KEY_Char_Num == before + 2


def test_shift_and_refuses_when_buffer_full():
    kp = _select(KeyParam())
    kp.KEY_Char_Num = len(kp.data)
    assert kp.shift_and(30, "!") is False


# ===========================================================================
#  KeyParam — multimedia / mouse / led
# ===========================================================================
def test_multimedia_selects_value_by_report_id():
    kp = _select(KeyParam())
    kp.ReportID = 0
    assert kp.multimedia("Vol +", (0, 2), (0, 64), (0, 233)) is True
    reports, flash, _ = _built(kp)
    assert flash == "kbd"
    assert reports[0][2] == 2             # rid0 value
    assert kp.data[KeyParam.KeyType_Num] & 0xF == 2


def test_multimedia_report_id_two_and_other():
    kp = _select(KeyParam())
    kp.ReportID = 2
    kp.multimedia("Play", (0, 64), (1, 4), (0, 205))
    assert kp.data[kp.KEY_Char_Num + 1] == 4   # rid2 offset/value
    kp2 = _select(KeyParam())
    kp2.ReportID = 5
    kp2.multimedia("Play", (0, 64), (1, 4), (0, 205))
    assert kp2.data[kp2.KEY_Char_Num] == 205   # "other" branch


def test_multimedia_uses_fixed_report_bytes_after_keyboard_pointer_moves():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    kp.data[KeyParam.KeyType_Num] = 0
    kp.ReportID = 2

    assert kp.multimedia("Play", (0, 64), (1, 4), (0, 205)) is True
    reports, _flash, _ = _built(kp)

    assert kp.KEY_Char_Num == 7
    assert kp.data[6] == 4
    assert reports[1][3] == 4


def test_multimedia_refuses_out_of_range():
    kp = _select(KeyParam())
    kp.KEY_Char_Num = len(kp.data) - 1
    assert kp.multimedia("x", (1, 9), (1, 9), (1, 9)) is False


def test_mouse_masks_all_bytes_to_one_byte():
    kp = _select(KeyParam())
    assert kp.mouse("Wheel -", 0, 0, 0, 0x1FF, 0x102) is True
    reports, flash, _ = _built(kp)
    assert flash == "kbd"
    assert kp.data[8] == 0xFF                      # b3 masked
    assert kp.data[9] == 0x02                      # b4 masked
    assert reports[0][5] == 0xFF


def test_mouse_without_b4():
    kp = _select(KeyParam())
    assert kp.mouse("L Click", 1, 0, 0, 0) is True
    assert kp.data[5] == 1


def test_mouse_refuses_out_of_range():
    kp = _select(KeyParam())
    kp.KEY_Char_Num = len(kp.data) - 2
    assert kp.mouse("x", 1, 1, 1, 1, 1) is False


def test_led_uses_led_flash_and_mode_byte():
    kp = KeyParam()
    kp.KEY_Cur_Page = 4
    kp.led(2, "LED Mode 2")
    reports, flash, _ = _built(kp)
    assert flash == "led"
    assert reports[0][0] == 176                    # LED pseudo-key id
    assert reports[0][2] == 2                       # mode byte


def test_layer_switch_report_prepended_when_report_id_nonzero():
    kp = _select(KeyParam())
    kp.ReportID = 2
    kp.KEY_Cur_Layer = 3
    kp.basic_key(4, "A")
    reports, _flash, _ = _built(kp)
    assert reports[0][0] == 0xA1                    # swlayer command first
    assert reports[0][1] == 3                       # layer
    assert reports[1][1] == ((3 << 4) | (kp.data[KeyParam.KeyType_Num])) & 0xFF


def test_swlayer_defaults_layer_to_one_when_zero():
    kp = _select(KeyParam())
    kp.KEY_Cur_Layer = 0
    assert kp._swlayer_buf()[1] == 1


def test_build_returns_none_for_unknown_kind():
    kp = _select(KeyParam())
    kp.data[KeyParam.KeyType_Num] = 4                # nibble 4 -> no builder
    assert kp.build_download_reports() is None


# ===========================================================================
#  KeyParam — display helpers
# ===========================================================================
def test_key_text_empty_without_selection():
    assert KeyParam().key_text() == ""


def test_fun_text_empty_without_selection():
    assert KeyParam().fun_text() == ""


def test_key_text_joins_assigned_labels():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    assert kp.key_text() == "A"


def test_fun_text_joins_modifier_names():
    kp = _select(KeyParam())
    kp.fun_combo([(1, "Ctrl"), (4, "Alt")])
    assert kp.fun_text() == "Ctrl Alt"


# ===========================================================================
#  KeyParam — Unicode-entry macros
# ===========================================================================
def test_unicode_macro_linux_holds_ctrl_shift_over_u_and_hex():
    kp = _select(KeyParam())
    assert kp.unicode_macro(0x03B1, "linux") is True       # Greek alpha
    d = kp.data
    # 5 keystrokes: U, '0','3','b','1' -- each with Ctrl|Shift (=3) held
    assert d[kp.KeyGroupCharNum] == 5
    assert d[4] == 3 and d[5] == 24                         # Ctrl+Shift + 'u'
    assert d[6] == 3 and d[7] == minikeypad.HEX_HID["0"]
    assert d[8] == 3 and d[9] == minikeypad.HEX_HID["3"]
    assert d[10] == 3 and d[11] == minikeypad.HEX_HID["b"]
    assert d[12] == 3 and d[13] == minikeypad.HEX_HID["1"]
    reports, flash, _ = _built(kp)
    assert flash == "kbd" and reports[0][0] == 1


def test_unicode_macro_darwin_holds_option_over_4_hex():
    kp = _select(KeyParam())
    assert kp.unicode_macro(0x05D0, "darwin") is True       # Hebrew alef
    d = kp.data
    assert d[kp.KeyGroupCharNum] == 4                        # 4 hex, no 'u'
    assert d[4] == 4 and d[5] == minikeypad.HEX_HID["0"]    # Option held
    assert d[6] == 4 and d[7] == minikeypad.HEX_HID["5"]
    assert d[8] == 4 and d[9] == minikeypad.HEX_HID["d"]
    assert d[10] == 4 and d[11] == minikeypad.HEX_HID["0"]


def test_unicode_macro_unsupported_platform_returns_false():
    kp = _select(KeyParam())
    assert kp.unicode_macro(0x03B1, "win32") is False


def test_unicode_macro_rejects_non_bmp():
    kp = _select(KeyParam())
    assert kp.unicode_macro(0x10348, "linux") is False      # outside the BMP


def test_add_keystroke_refuses_when_buffer_full():
    kp = _select(KeyParam())
    kp.KEY_Char_Num = len(kp.data)
    assert kp._add_keystroke(3, 24, "x") is False


def test_unicode_macro_aborts_if_keystroke_rejected(monkeypatch):
    kp = _select(KeyParam())
    monkeypatch.setattr(kp, "_add_keystroke", lambda *_a: False)
    assert kp.unicode_macro(0x03B1, "linux") is False


def test_unicode_platform_detection(monkeypatch):
    monkeypatch.setattr(minikeypad.sys, "platform", "linux")
    assert minikeypad._unicode_platform() == "linux"
    monkeypatch.setattr(minikeypad.sys, "platform", "darwin")
    assert minikeypad._unicode_platform() == "darwin"
    monkeypatch.setattr(minikeypad.sys, "platform", "win32")
    assert minikeypad._unicode_platform() is None


def test_layout_tables_scancodes_are_valid_hid():
    scripts = [(n, e) for n, e in minikeypad.LAYOUTS if e is not None]
    assert minikeypad.LAYOUTS[0][1] is None          # "US (basic)" sentinel
    names = [n for n, _ in minikeypad.LAYOUTS]
    for expected in ("Greek / Cyrillic", "Hebrew", "German / Nordic", "Latin (accents)"):
        assert expected in names
    gc = {g for g, _ in dict(minikeypad.LAYOUTS)["Greek / Cyrillic"]}
    assert "α" in gc and "я" in gc        # Greek + Cyrillic merged
    nordic = {g for g, _ in dict(minikeypad.LAYOUTS)["German / Nordic"]}
    assert {"ä", "ö", "ü", "ß", "å", "æ", "ø"} <= nordic
    for _title, entries in scripts:
        assert entries, "layout table must not be empty"
        for glyph, scancode in entries:
            assert len(glyph) == 1
            assert 0 < scancode < 256


def test_merged_latin_covers_pt_fr_es_it():
    latin = {g for g, _ in dict(minikeypad.LAYOUTS)["Latin (accents)"]}
    # Portuguese ã, French ç/œ, Spanish ñ/¿, Italian ì/ò all in one block
    for glyph in ("ã", "ç", "œ", "ñ", "¿", "ì", "ò", "é"):
        assert glyph in latin
    assert len(latin) == len([g for g, _ in dict(minikeypad.LAYOUTS)["Latin (accents)"]])


# ===========================================================================
#  Unicode-method availability gating
# ===========================================================================
def test_ibus_available_via_path(monkeypatch):
    monkeypatch.setattr(minikeypad.shutil, "which", lambda _x: "/usr/bin/ibus")
    assert minikeypad._ibus_available() is True


def test_ibus_available_via_im_env(monkeypatch):
    monkeypatch.setattr(minikeypad.shutil, "which", lambda _x: None)
    monkeypatch.setenv("GTK_IM_MODULE", "ibus")
    assert minikeypad._ibus_available() is True


def test_ibus_absent(monkeypatch):
    monkeypatch.setattr(minikeypad.shutil, "which", lambda _x: None)
    for v in ("GTK_IM_MODULE", "QT_IM_MODULE", "XMODIFIERS"):
        monkeypatch.delenv(v, raising=False)
    assert minikeypad._ibus_available() is False


def test_unicode_available_per_platform(monkeypatch):
    monkeypatch.setattr(minikeypad.sys, "platform", "linux")
    monkeypatch.setattr(minikeypad, "_ibus_available", lambda: True)
    assert minikeypad._unicode_available() is True
    monkeypatch.setattr(minikeypad, "_ibus_available", lambda: False)
    assert minikeypad._unicode_available() is False
    monkeypatch.setattr(minikeypad.sys, "platform", "darwin")
    assert minikeypad._unicode_available() is True
    monkeypatch.setattr(minikeypad.sys, "platform", "win32")
    assert minikeypad._unicode_available() is False


def test_available_layouts_gates_on_unicode(monkeypatch):
    monkeypatch.setattr(minikeypad, "_unicode_available", lambda: True)
    assert len(minikeypad._available_layouts()) == len(minikeypad.LAYOUTS)
    monkeypatch.setattr(minikeypad, "_unicode_available", lambda: False)
    only = minikeypad._available_layouts()
    assert len(only) == 1 and only[0][1] is None     # US basic only


# ===========================================================================
#  fuzz — random op streams must never raise or emit out-of-range bytes
# ===========================================================================
def test_fuzz_keyparam_operations_stay_in_bounds():
    rnd = random.Random(20240625)
    kp = _select(KeyParam())
    for _ in range(6000):
        op = rnd.randrange(8)
        if op == 0:
            kp.select_physical_key(rnd.randint(1, 18))
        elif op == 1:
            kp.basic_key(rnd.randint(0, 511), "x")
        elif op == 2:
            kp.basic_modifier(rnd.choice([1, 2, 4, 8]), "m")
        elif op == 3:
            kp.fun_combo(rnd.choice(minikeypad.FUN_MODS)[1])
        elif op == 4:
            kp.shift_and(rnd.randint(0, 511), "s")
        elif op == 5:
            name, r0, r2, ro = rnd.choice(minikeypad.MULTIMEDIA)
            kp.multimedia(name, r0, r2, ro)
        elif op == 6:
            entry = rnd.choice(minikeypad.MOUSE)
            kp.mouse(entry[0], *entry[1])
        else:
            kp.key_cleared()
            kp.select_physical_key(rnd.randint(1, 18))
        kp.ReportID = rnd.choice([0, 2, 5])
        result = kp.build_download_reports()
        if result is not None:
            reports, flash, _trunc = result
            assert flash in ("kbd", "led")
            for r in reports:
                assert len(r) == 8
                assert all(0 <= b <= 255 for b in r)


# ===========================================================================
#  install helpers (mocked: never touch the network)
# ===========================================================================
def test_pip_install_uses_argv_list_no_shell(monkeypatch):
    seen = {}

    def fake_check_call(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["shell"] = kwargs.get("shell", False)
        return 0

    monkeypatch.setattr(minikeypad.subprocess, "check_call", fake_check_call)
    assert minikeypad._pip_install("pyusb") is True
    assert isinstance(seen["cmd"], list)        # argv, not a string
    assert seen["cmd"][-1] == "pyusb"
    assert "-m" in seen["cmd"] and "pip" in seen["cmd"]
    assert seen["shell"] is False


def test_pip_install_falls_back_to_user_then_reports_failure(monkeypatch):
    calls = []

    def always_fail(cmd, **kwargs):
        calls.append(cmd)
        raise minikeypad.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(minikeypad.subprocess, "check_call", always_fail)
    assert minikeypad._pip_install("pyusb") is False
    assert any("--user" in c for c in calls)    # tried the --user fallback


def test_ensure_pyusb_short_circuits_when_already_loaded(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", True)

    def boom(_pkg):
        raise AssertionError("must not attempt install when pyusb present")

    monkeypatch.setattr(minikeypad, "_pip_install", boom)
    assert minikeypad._ensure_pyusb() is True


def test_ensure_pyusb_returns_false_when_install_fails(monkeypatch, caplog):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setattr(minikeypad, "_pip_install", lambda _pkg: False)
    assert minikeypad._ensure_pyusb() is False
    assert "Automatic install failed" in caplog.text


def test_ensure_pyusb_installs_pinned_requirement(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setattr(minikeypad, "_USB_ERR", "missing")
    calls = []
    monkeypatch.setattr(minikeypad, "_pip_install", lambda pkg: calls.append(pkg) or False)

    assert minikeypad._ensure_pyusb() is False
    assert calls == [minikeypad.PYUSB_REQUIREMENT]


def test_ensure_pyusb_success_reimports(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    # _ensure_pyusb rebinds the module global `usb`; record it so monkeypatch
    # restores the real module and background poll threads stay happy.
    monkeypatch.setattr(minikeypad, "usb", getattr(minikeypad, "usb", None),
                        raising=False)
    monkeypatch.setattr(minikeypad, "_pip_install", lambda _pkg: True)
    monkeypatch.setitem(sys.modules, "usb", types.ModuleType("usb"))
    monkeypatch.setitem(sys.modules, "usb.core", types.ModuleType("usb.core"))
    monkeypatch.setitem(sys.modules, "usb.util", types.ModuleType("usb.util"))
    assert minikeypad._ensure_pyusb() is True
    assert minikeypad._USB_OK is True


def test_ensure_pyusb_install_then_import_fails(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setattr(minikeypad, "_pip_install", lambda _pkg: True)
    monkeypatch.setitem(sys.modules, "usb", None)   # forces ImportError
    assert minikeypad._ensure_pyusb() is False


# ===========================================================================
#  KeypadDevice — exercised through a fake `usb` module
# ===========================================================================
class FakeEP:
    def __init__(self, addr=0x81, n=9):
        self.bEndpointAddress = addr
        self._n = n
        self.written = []

    def write(self, data, timeout):
        self.written.append((bytes(data), timeout))
        return self._n


class FakeIntf:
    def __init__(self, num=1):
        self.bInterfaceNumber = num


class FakeUsbDev:
    def __init__(self, *, kernel_active=True, cfg=None, detach_exc=None,
                 cfg_exc=None, ctrl_n=9):
        self.kernel_active = kernel_active
        self.detached = self.attached = False
        self.detach_exc = detach_exc
        self.cfg_exc = cfg_exc
        self.cfg = cfg if cfg is not None else {}
        self.ctrl_n = ctrl_n
        self.ctrl_calls = []

    def is_kernel_driver_active(self, _i):
        return self.kernel_active

    def detach_kernel_driver(self, _i):
        if self.detach_exc:
            raise self.detach_exc
        self.detached = True

    def attach_kernel_driver(self, _i):
        self.attached = True

    def get_active_configuration(self):
        if self.cfg_exc:
            raise self.cfg_exc
        return self.cfg

    def ctrl_transfer(self, *a):
        self.ctrl_calls.append(a)
        return self.ctrl_n


def make_usb(find_dev=None, ep=None):
    ns = types.SimpleNamespace()

    class USBError(Exception):
        pass

    core = types.SimpleNamespace()
    core.USBError = USBError
    core._find_dev = find_dev
    core.find = lambda **kw: core._find_dev
    util = types.SimpleNamespace()
    util.ENDPOINT_OUT = 0
    util.endpoint_direction = lambda addr: 0
    util.find_descriptor = lambda intf, custom_match=None: ep
    util.disposed = []
    util.dispose_resources = util.disposed.append
    ns.core = core
    ns.util = util
    return ns, USBError


def _install_usb(monkeypatch, usb):
    monkeypatch.setattr(minikeypad, "usb", usb, raising=False)
    monkeypatch.setattr(minikeypad, "_USB_OK", True)


def test_connect_returns_false_without_pyusb(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    assert minikeypad.KeypadDevice().connect() is False


def test_connect_idempotent(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    d.dev = object()
    assert d.connect() is True


def test_connect_no_device(monkeypatch):
    usb, _ = make_usb(find_dev=None)
    _install_usb(monkeypatch, usb)
    assert minikeypad.KeypadDevice().connect() is False


def test_connect_success_with_endpoint(monkeypatch):
    ep = FakeEP()
    dev = FakeUsbDev(cfg={(1, 0): FakeIntf(1)})
    usb, _ = make_usb(find_dev=dev, ep=ep)
    _install_usb(monkeypatch, usb)
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    assert d.connect() is True
    assert d.connected and d.ep_out is ep
    assert dev.detached is True
    assert any("Connected" in m for m in logs)


def test_connect_interface_fallback(monkeypatch, caplog):
    dev = FakeUsbDev(cfg={(0, 0): FakeIntf(0)})        # no (1,0) -> fallback
    usb, _ = make_usb(find_dev=dev, ep=FakeEP())
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    with caplog.at_level("WARNING", logger="minikeypad"):
        assert d.connect() is True
    assert d.intf is not None
    assert d.intf.bInterfaceNumber == 0
    # mkp-obs-01: the fallback to interface 0 must be surfaced, not silent.
    assert any("falling back to interface 0" in r.message for r in caplog.records)


def test_connect_control_path_when_no_endpoint(monkeypatch):
    dev = FakeUsbDev(cfg={(1, 0): FakeIntf(1)})
    usb, _ = make_usb(find_dev=dev, ep=None)
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    assert d.connect() is True
    assert d.ep_out is None


def test_detach_not_implemented_is_ignored(monkeypatch):
    dev = FakeUsbDev(cfg={(1, 0): FakeIntf(1)}, detach_exc=NotImplementedError())
    usb, _ = make_usb(find_dev=dev, ep=FakeEP())
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    assert d.connect() is True
    assert d._detached is False


def test_detach_usberror_logged(monkeypatch):
    usb, USBError = make_usb(ep=FakeEP())
    dev = FakeUsbDev(cfg={(1, 0): FakeIntf(1)}, detach_exc=USBError("denied"))
    usb.core._find_dev = dev
    _install_usb(monkeypatch, usb)
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    assert d.connect() is True
    assert any("detach" in m.lower() for m in logs)


def test_connect_usberror_returns_false(monkeypatch):
    usb, USBError = make_usb(ep=FakeEP())
    dev = FakeUsbDev(kernel_active=False, cfg_exc=USBError("boom"))
    usb.core._find_dev = dev
    _install_usb(monkeypatch, usb)
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    assert d.connect() is False
    assert d.dev is None
    assert any("USB error" in m for m in logs)


def test_connect_failure_reattaches_and_disposes_detached_device(monkeypatch):
    usb, USBError = make_usb(ep=FakeEP())
    dev = FakeUsbDev(kernel_active=True, cfg_exc=USBError("claim failed"))
    usb.core._find_dev = dev
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()

    assert d.connect() is False

    assert dev.detached is True
    assert dev.attached is True
    assert dev in usb.util.disposed
    assert d.dev is None
    assert d._detached is False


def test_rollback_connect_ignores_failure_before_device_discovery(monkeypatch):
    device = minikeypad.KeypadDevice()
    existing = object()
    device.dev = existing
    monkeypatch.setattr(
        device,
        "close",
        lambda: (_ for _ in ()).throw(AssertionError("close called")),
    )

    device._rollback_connect(None)

    assert device.dev is existing


def test_still_connected_none_when_no_dev():
    assert minikeypad.KeypadDevice().still_connected() is False


def test_still_connected_true(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    d.dev = FakeUsbDev()
    assert d.still_connected() is True


def test_still_connected_uses_existing_handle_not_bus_scan(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    usb.core.find = lambda **_kw: (_ for _ in ()).throw(AssertionError("bus scan"))
    d = minikeypad.KeypadDevice()
    d.dev = FakeUsbDev()
    assert d.still_connected() is True


def test_still_connected_exception_drops(monkeypatch):
    usb, USBError = make_usb()
    _install_usb(monkeypatch, usb)

    d = minikeypad.KeypadDevice()
    d.dev = FakeUsbDev(cfg_exc=USBError("gone"))
    assert d.still_connected() is False
    assert d.dev is None


def test_close_reattaches_and_disposes(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    dev = FakeUsbDev()
    d = minikeypad.KeypadDevice()
    d.dev = dev
    d._detached = True
    d.close()
    assert dev.attached is True
    assert dev in usb.util.disposed
    assert d.dev is None and d._detached is False


def test_close_dispose_exception_is_logged(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)

    def boom(_x):
        raise RuntimeError("nope")

    usb.util.dispose_resources = boom
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    d.dev = FakeUsbDev()
    d.close()
    assert d.dev is None
    assert any("dispose_resources failed" in m for m in logs)


def test_reattach_failure_is_logged(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    dev = FakeUsbDev()

    def boom(_i):
        raise RuntimeError("nope")

    dev.attach_kernel_driver = boom
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    d.dev = dev
    d._detached = True
    d.close()
    assert d._detached is False
    assert any("re-attach kernel driver" in m for m in logs)


def test_write_device_none_dev():
    assert minikeypad.KeypadDevice().write_device(0, bytearray(8)) is False


def test_write_device_endpoint(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    ep = FakeEP(n=9)
    d = minikeypad.KeypadDevice()
    d.dev = object()
    d.ep_out = ep
    assert d.write_device(2, bytes(range(1, 9))) is True
    sent = ep.written[0][0]
    assert sent[0] == 2 and sent[1] == 1
    assert len(sent) == minikeypad.REPORT_LEN + 1


def test_write_device_endpoint_zero_is_failure(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    d = minikeypad.KeypadDevice()
    d.dev = object()
    d.ep_out = FakeEP(n=0)
    assert d.write_device(0, bytearray(8)) is False


def test_write_device_control_path(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    dev = FakeUsbDev(ctrl_n=9)
    d = minikeypad.KeypadDevice()
    d.dev = dev
    d.ep_out = None
    d.intf = FakeIntf(1)
    assert d.write_device(3, bytearray(8)) is True
    assert dev.ctrl_calls


def test_write_device_control_path_without_intf(monkeypatch):
    usb, _ = make_usb()
    _install_usb(monkeypatch, usb)
    dev = FakeUsbDev(ctrl_n=9)
    d = minikeypad.KeypadDevice()
    d.dev = dev
    d.ep_out = None
    d.intf = None
    assert d.write_device(3, bytearray(8)) is True


def test_write_device_usberror(monkeypatch):
    usb, USBError = make_usb()
    _install_usb(monkeypatch, usb)
    ep = FakeEP()

    def boom(data, timeout):
        raise USBError("fail")

    ep.write = boom
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    d.dev = object()
    d.ep_out = ep
    assert d.write_device(0, bytearray(8)) is False
    assert any("write failed" in m for m in logs)
    assert any("retry" in m for m in logs)


def test_write_device_retries_then_succeeds(monkeypatch):
    usb, USBError = make_usb()
    _install_usb(monkeypatch, usb)
    ep = FakeEP()
    n = {"v": 0}

    def flaky(data, timeout):
        n["v"] += 1
        if n["v"] < 2:
            raise USBError("transient")
        return 9

    ep.write = flaky
    logs = []
    d = minikeypad.KeypadDevice(log=logs.append)
    d.dev = object()
    d.ep_out = ep
    assert d.write_device(0, bytearray(8)) is True
    assert n["v"] == 2
    assert any("retry" in m for m in logs)


# ===========================================================================
#  flash buffer
# ===========================================================================
def test_flash_buf_mapping():
    assert minikeypad.App._flash_buf("kbd")[1] == 0xAA
    assert minikeypad.App._flash_buf("led")[1] == 0xA1


def test_version_constant_is_used_by_cli():
    assert minikeypad.__version__
    with pytest.raises(SystemExit):
        minikeypad.main(["--version"])


# ===========================================================================
#  App — needs a Tk display
# ===========================================================================
def _has_display():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


pytestmark_app = pytest.mark.skipif(not _has_display(), reason="no Tk display")


class FakeDev:
    """Stand-in for KeypadDevice in App tests."""

    def __init__(self, connected=True, write_ok=True, fail_index=None):
        self._connected = connected
        self.write_ok = write_ok
        self.fail_index = fail_index      # 1-based write call that returns False
        self.writes = []
        self._calls = 0
        self.closed = False

    @property
    def connected(self):
        return self._connected

    def connect(self):
        return self._connected

    def still_connected(self):
        return self._connected

    def write_device(self, rid, buf):
        self.writes.append((rid, bytes(buf)))
        self._calls += 1
        if self.fail_index is not None and self._calls == self.fail_index:
            return False
        return self.write_ok

    def close(self):
        self.closed = True


class FlakyDev(FakeDev):
    """Reports connected but fails the liveness re-check."""

    def still_connected(self):
        return False


class CrashDev(FakeDev):
    """write_device raises -> exercises the download worker's crash guard."""

    def write_device(self, rid, buf):
        raise RuntimeError("boom")


class ConnCrashDev(FakeDev):
    """connect() raises -> exercises the connect worker's crash guard."""

    def connect(self):
        raise RuntimeError("boom")


def _drain(app):
    while not app._ui_q.empty():
        app._ui_q.get_nowait()()


def _wait_drain(app, timeout=2.0):
    end = time.monotonic() + timeout
    saw_callback = False
    while time.monotonic() < end:
        if not app._ui_q.empty():
            saw_callback = True
            _drain(app)
        if saw_callback and not app._io_busy and app._ui_q.empty():
            return
        time.sleep(0.005)
    _drain(app)


@pytest.fixture
def app(monkeypatch):
    if not _has_display():
        pytest.skip("no Tk display")
    # Force Unicode availability so the extended layouts load regardless of
    # whether IBus is installed on the test host.
    monkeypatch.setattr(minikeypad, "_unicode_available", lambda: True)
    a = minikeypad.App()
    # Fully settle the startup connect probe so its _connect_done can't land
    # mid-test and race a later worker's completion.
    end = time.time() + 2.0
    while time.time() < end and a._io_busy:
        _drain(a)
        time.sleep(0.005)
    _drain(a)
    try:
        yield a
    finally:
        a.destroy()


def test_select_key_and_basic(app):
    app._select_key(1)
    app._basic_key(4, "A")
    assert app.set_text.get() == "A"
    assert app._selected_id == 1


def test_need_key_blocks_every_handler_without_selection(app):
    app._clear()
    app._basic_key(4, "A")
    app._basic_mod(1, "Ctrl")
    app._fun_combo([(1, "Ctrl")])
    app._shift_and(30, "!")
    app._multimedia(("Vol +", (0, 2), (0, 64), (0, 233)))
    app._mouse(("L Click", (1, 0, 0, 0)))
    assert app.set_text.get() == "" and app.fun_text.get() == ""


def test_append_log_falls_back_to_print(app, capsys):
    app.log_box.destroy()                 # make widget ops raise -> print path
    app._append_log("boom-line")
    assert "boom-line" in capsys.readouterr().out


def test_append_log_trims_to_line_cap():
    class FakeLogBox:
        def __init__(self):
            self.lines = []

        def configure(self, **_kwargs):
            pass

        def insert(self, _where, text):
            self.lines.extend(text.splitlines())

        def index(self, _where):
            return f"{len(self.lines)}.0"

        def delete(self, _start, end):
            stop = int(end.split(".", 1)[0]) - 1
            del self.lines[:stop]

        def see(self, _where):
            pass

    app = types.SimpleNamespace(log_box=FakeLogBox())
    app._trim_log_lines = minikeypad.App._trim_log_lines.__get__(app, type(app))

    for index in range(minikeypad.MAX_LOG_LINES + 7):
        minikeypad.App._append_log(app, f"line-{index}")

    assert len(app.log_box.lines) == minikeypad.MAX_LOG_LINES
    assert app.log_box.lines[0] == "line-7"
    assert app.log_box.lines[-1] == f"line-{minikeypad.MAX_LOG_LINES + 6}"


def test_drain_ui_survives_callback_exception(caplog):
    class DummyApp:
        def __init__(self):
            self._ui_q = minikeypad.queue.SimpleQueue()
            self.after_calls = []
            self._drain_ui = minikeypad.App._drain_ui.__get__(self, type(self))
            self._run_ui_callback = minikeypad.App._run_ui_callback.__get__(
                self, type(self)
            )

        def after(self, delay, callback):
            self.after_calls.append((delay, callback))

    app = DummyApp()
    ran = []

    def bad():
        ran.append("bad")
        raise RuntimeError("boom")

    app._ui_q.put(bad)
    app._ui_q.put(lambda: ran.append("good"))

    with caplog.at_level(logging.ERROR):
        minikeypad.App._drain_ui(app)

    assert ran == ["bad", "good"]
    assert app.after_calls[0][0] == 120
    assert app.after_calls[0][1].__func__ is minikeypad.App._drain_ui
    assert "queued UI callback failed" in caplog.text


def test_version_check_applies_report_id_on_ui_queue():
    class ProbeDev:
        def write_device(self, rid, _buf):
            return rid == 2

    logs = []
    app = types.SimpleNamespace(
        dev=ProbeDev(),
        kp=KeyParam(),
        _ui_q=minikeypad.queue.SimpleQueue(),
        log=logs.append,
    )
    app.kp.ReportID = 99
    app._apply_report_id = minikeypad.App._apply_report_id.__get__(app, type(app))

    minikeypad.App._version_check(app)

    assert app.kp.ReportID == 99
    app._ui_q.get_nowait()()
    assert app.kp.ReportID == 2
    assert logs == ["Keyboard reportID = 2"]


def test_select_key_logs_when_disabled_on_led_page():
    class LedPageKeyParam:
        def select_physical_key(self, _key_id):
            return False

    logs = []
    app = types.SimpleNamespace(kp=LedPageKeyParam(), log=logs.append)

    minikeypad.App._select_key(app, 2)

    assert logs == ["Key selection disabled on LED page"]


def test_select_disabled_on_led_page(app):
    app.kp.KEY_Cur_Page = 4
    app._select_key(2)
    assert app._selected_id is None


def test_basic_mod_handler(app):
    app._select_key(1)
    app._basic_key(4, "A")
    app._basic_mod(1, "Ctrl")
    assert "Ctrl" in app.fun_text.get()


def test_fun_combo_handler(app):
    app._select_key(1)
    app._fun_combo([(1, "Ctrl"), (4, "Alt")])
    assert "Ctrl" in app.fun_text.get()


def test_shift_and_handler(app):
    app._select_key(1)
    app._shift_and(30, "!")
    assert "!" in app.set_text.get()


def test_multimedia_handler(app):
    app._select_key(1)
    app._multimedia(("Vol +", (0, 2), (0, 64), (0, 233)))
    assert "Vol +" in app.set_text.get()


def test_mouse_handler(app):
    app._select_key(1)
    app._mouse(("L Click", (1, 0, 0, 0)))
    assert "L Click" in app.set_text.get()


def test_led_handler(app):
    app.kp.KEY_Cur_Page = 4
    app._led(("LED Mode 1", 1))
    assert "LED Mode 1" in app.set_text.get()


def test_on_page_clears_on_led(app):
    app._select_key(1)
    app._basic_key(4, "A")
    app.nb.select(app.tab_led)
    app._on_page()
    assert app.kp.KEY_Cur_Page == 4
    assert app.set_text.get() == ""


def test_on_page_clears_keys_before_multimedia(app):
    app._select_key(1)
    app._basic_key(4, "A")
    assert app.kp.data[KeyParam.KeyType_Num] & 0xF == 1

    app.nb.select(app.tab_mul)
    app._on_page()

    assert app.kp.KEY_Cur_Page == 3
    assert app.kp.data[KeyParam.KeyType_Num] == 0
    assert app.set_text.get() == ""


def test_on_layer(app):
    app.layer_var.set(2)
    app._on_layer()
    assert app.kp.KEY_Cur_Layer == 2


def test_clear_handler(app):
    app._select_key(1)
    app._basic_key(4, "A")
    app._clear()
    assert app.set_text.get() == "" and app._selected_id is None


def test_log_appends(app):
    app.log("hello-log")
    _drain(app)
    assert "hello-log" in app.log_box.get("1.0", "end")


def test_build_ui_logs_missing_pyusb(monkeypatch):
    if not _has_display():
        pytest.skip("no Tk display")
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setattr(minikeypad, "_USB_ERR", "no backend")
    a = minikeypad.App()
    try:
        _drain(a)
        assert "pyusb not available" in a.log_box.get("1.0", "end")
    finally:
        a.destroy()


def test_download_not_connected(app):
    app._io_busy = False
    app.dev = FakeDev(connected=False)
    app._download()
    assert "failed" in app.dl_status.cget("text").lower()


def test_download_busy(app):
    app._io_busy = True
    app._download()
    assert "busy" in app.dl_status.cget("text").lower()


@pytest.mark.parametrize(
    "show",
    [lambda app: app._dl_result(False), lambda app: app._dl_note("Busy")],
)
def test_status_flashes_clear_to_the_same_neutral_state(app, monkeypatch, show):
    scheduled = []
    monkeypatch.setattr(
        app, "after", lambda delay, callback: scheduled.append((delay, callback))
    )

    show(app)
    delay, clear = scheduled[-1]
    clear()

    assert delay == minikeypad.STATUS_CLEAR_MS
    assert app.dl_status.cget("text") == ""
    assert app.dl_status.cget("fg") == "black"
    assert app.dl_status.cget("bg") == app.cget("bg")


def test_download_nothing_assigned(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True)
    app._clear()
    app._download()
    assert "nothing" in app.dl_status.cget("text").lower()


def test_download_success(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True)
    app.kp.select_physical_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert "success" in app.dl_status.cget("text").lower()
    assert app.dev.writes


def test_download_pre_flash_failure_says_intact(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=False)   # first report never ACKs
    app.kp.select_physical_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    log = app.log_box.get("1.0", "end").lower()
    assert "failed" in app.dl_status.cget("text").lower()
    assert "intact" in log and "partial" not in log


def test_download_flash_failure_warns_partial(app):
    app._io_busy = False
    # one basic_key -> 2 reports (b0, b1); flash is the 3rd write -> fail it
    app.dev = FakeDev(connected=True, write_ok=True, fail_index=3)
    app.kp.select_physical_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert "failed" in app.dl_status.cget("text").lower()
    assert "partial" in app.log_box.get("1.0", "end").lower()


def test_download_flash_failure_records_ambiguous_assignment(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True, fail_index=3)
    app.kp.select_physical_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)

    rec = app._assignments[(1, 1)]
    assert rec["desc"] == "A"
    assert rec["ambiguous"] is True


def test_handlers_log_dropped_clicks_when_buffer_full(app):
    app._select_key(1)
    app.kp.KEY_Char_Num = len(app.kp.data)      # buffer pointer past the end
    app._basic_key(4, "A")
    app._shift_and(30, "!")
    app._multimedia(("Vol +", (0, 2), (0, 64), (0, 233)))
    app._mouse(("L Click", (1, 0, 0, 0)))
    _drain(app)
    assert app.log_box.get("1.0", "end").lower().count("ignored") == 4


def test_download_truncated_logs(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True)
    app.kp.select_physical_key(1)
    for _ in range(minikeypad.MAX_KBD_GROUPS + 3):
        app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert "first %d" % minikeypad.MAX_KBD_GROUPS in app.log_box.get("1.0", "end")


def test_send_reports_all_ok(app):
    app.dev = FakeDev(write_ok=True)
    assert app._send_reports([bytearray(8), bytearray(8)], bytearray(8), 0) == "ok"
    assert len(app.dev.writes) == 3        # 2 reports + flash


def test_send_reports_report_stage_failure(app):
    app.dev = FakeDev(fail_index=1)
    assert app._send_reports([bytearray(8)], bytearray(8), 0) == "reports"
    _drain(app)
    assert "report 1/1 not acknowledged" in app.log_box.get("1.0", "end").lower()


def test_send_reports_flash_stage_failure(app):
    app.dev = FakeDev(fail_index=2)        # report ok, flash (2nd call) fails
    assert app._send_reports([bytearray(8)], bytearray(8), 0) == "flash"
    _drain(app)
    assert "flash commit not acknowledged" in app.log_box.get("1.0", "end").lower()


def test_download_worker_crash_recovers(app):
    app._io_busy = False
    app.dev = CrashDev(connected=True)
    app.kp.select_physical_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert "failed" in app.dl_status.cget("text").lower()
    assert app._io_busy is False
    assert str(app.dl_btn.cget("state")) == "normal"


def test_try_connect_crash_recovers(app):
    app._io_busy = True
    app.dev = ConnCrashDev(connected=False)
    app._try_connect()
    _wait_drain(app)
    assert app._io_busy is False


def test_version_check_sets_report_id(app):
    app.dev = FakeDev(connected=True, write_ok=True)
    app._version_check()
    _drain(app)
    assert app.kp.ReportID == 3


def test_version_check_defaults_zero(app):
    app.dev = FakeDev(connected=True, write_ok=False)
    app._version_check()
    _drain(app)
    assert app.kp.ReportID == 0


def test_update_state_connected(app):
    app.dev = FakeDev(connected=True)
    app._update_state()
    assert app.state_lbl.cget("text") == "Connected"


def test_update_state_disconnected(app):
    app.dev = FakeDev(connected=False)
    app._update_state()
    assert app.state_lbl.cget("text") == "Not connected"


def test_keys_tab_defaults_to_us_basic(app):
    assert app.layout_var.get() == "US (basic)"
    # US basic renders letter buttons, not the script note
    assert app._keys_body.winfo_children()


def test_app_without_unicode_method_loads_us_only(monkeypatch):
    if not _has_display():
        pytest.skip("no Tk display")
    monkeypatch.setattr(minikeypad, "_unicode_available", lambda: False)
    a = minikeypad.App()
    try:
        _drain(a)
        assert a._layouts == minikeypad.LAYOUTS[:1]
        assert a.layout_var.get() == "US (basic)"
        assert "extended-script layouts disabled" in a.log_box.get("1.0", "end")
    finally:
        a.destroy()


def test_switching_layout_rerenders_body(app):
    app.layout_var.set("Greek / Cyrillic")
    app._render_layout()
    app.layout_var.set("Latin (accents)")
    app._render_layout()
    # script view present -> the note Label sits directly under the body
    kinds = {w.winfo_class() for w in app._keys_body.winfo_children()}
    assert "TLabel" in kinds


def test_script_key_scancode_mode(app):
    app.layout_var.set("Greek / Cyrillic")
    app._render_layout()
    app._select_key(1)
    app.unicode_var.set(False)
    app._script_key("α", 4)
    assert app.kp.data[5] == 4              # raw scancode written


def test_script_key_unicode_mode(app):
    app._select_key(1)
    app.unicode_var.set(True)
    app._script_key("α", 4)                 # ord -> macro on this (linux) host
    _drain(app)
    assert app.kp.data[KeyParam.KeyType_Num] & 0xF == 1
    assert "U+03B1" in app.log_box.get("1.0", "end")


def test_unicode_char_blocks_without_key(app):
    app._clear()
    app._unicode_char("α")                  # no key selected -> no-op
    assert app.set_text.get() == ""


def test_unicode_char_logs_unsupported_os(app, monkeypatch):
    monkeypatch.setattr(minikeypad.sys, "platform", "win32")
    app._select_key(1)
    app._unicode_char("α")
    _drain(app)
    assert "not supported" in app.log_box.get("1.0", "end").lower()


def test_unicode_char_drops_non_bmp(app):
    app._select_key(1)
    app._unicode_char("𐍈")                  # U+10348, outside the BMP
    _drain(app)
    assert "ignored" in app.log_box.get("1.0", "end").lower()


def test_destroy_is_idempotent(app):
    app.destroy()
    app.destroy()                           # second call must not raise TclError
    assert app._destroyed is True


# ===========================================================================
#  session map: record on write, key colouring, save/load, write-all
# ===========================================================================
def test_successful_write_records_assignment(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True)
    app._select_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert app._assignments[(1, 1)]["desc"] == "A"


def test_failed_write_does_not_record(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=False)
    app._select_key(1)
    app.kp.basic_key(4, "A")
    app._download()
    _wait_drain(app)
    assert app._assignments == {}


def test_refresh_key_map_marks_mapped_and_idle(app):
    app._assignments[(1, 2)] = {"data": bytes(65), "desc": "Xtra-long"}
    app._selected_id = None
    app._refresh_key_map()
    mapped = app._phys_buttons[2]
    assert mapped.cget("bg") == minikeypad.COL_KEY_MAPPED
    assert mapped.cget("relief") == "ridge"
    assert "Xtra-lon" in mapped.cget("text")          # desc truncated to 8
    assert app._phys_buttons[3].cget("bg") == minikeypad.COL_KEY_IDLE
    assert app._phys_buttons[3].cget("relief") == "raised"

    app._selected_id = 2
    app._refresh_key_map()
    assert mapped.cget("bg") == minikeypad.COL_KEY_SEL
    assert mapped.cget("relief") == "sunken"


def test_key_name_mapping():
    assert minikeypad.App._key_name(1) == "KEY1"
    assert minikeypad.App._key_name(14) == "Knob1-Press"
    assert minikeypad.App._key_name(176) == "LED"
    assert minikeypad.App._key_name(999) == "id999"


def test_save_and_load_profile_round_trip(app, tmp_path):
    key1 = bytes([1]) + bytes(range(1, 65))
    app._assignments = {(1, 1): {"data": key1, "desc": "A"},
                        (2, 5): {"data": bytes([5]) + bytes(64), "desc": "Vol +"}}
    path = str(tmp_path / "p.json")
    app._save_profile(path)
    app._assignments = {}
    assert app._load_profile(path) == 2
    assert app._assignments[(1, 1)]["desc"] == "A"
    assert app._assignments[(1, 1)]["data"] == key1


def test_save_and_load_profile_preserves_ambiguous_flag(app, tmp_path):
    app._assignments = {
        (1, 1): {"data": bytes([1]) + bytes(range(1, 65)), "desc": "A",
                 "ambiguous": True}
    }
    path = str(tmp_path / "p.json")

    app._save_profile(path)
    app._assignments = {}
    app._load_profile(path)

    assert app._assignments[(1, 1)]["ambiguous"] is True


def test_load_profile_rejects_bad_buffer_length(app, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"version":1,"assignments":'
                   '[{"layer":1,"key_id":1,"desc":"x","data":"00ff"}]}')
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_reports_for_rebuilds_saved_key(app):
    app._select_key(1)
    app.kp.basic_key(4, "A")
    built = app._reports_for(1, bytes(app.kp.data))
    assert built is not None
    reports, flash, _ = built
    assert flash == "kbd" and reports[0][0] == 1


def test_write_all_not_connected(app):
    app._io_busy = False
    app.dev = FakeDev(connected=False)
    app._assignments = {(1, 1): {"data": bytes(65), "desc": "A"}}
    app._write_all()
    assert "failed" in app.dl_status.cget("text").lower()


def test_write_all_busy(app):
    app._io_busy = True
    app._write_all()
    assert "busy" in app.dl_status.cget("text").lower()


def test_write_all_empty(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True)
    app._assignments = {}
    app._write_all()
    assert "nothing saved" in app.dl_status.cget("text").lower()


def _one_key_assignment(app, key_id=1, code=4, label="A"):
    app._select_key(key_id)
    app.kp.basic_key(code, label)
    return bytes(app.kp.data)


def test_write_all_success(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True)
    data = _one_key_assignment(app)
    app._assignments = {(1, 1): {"data": data, "desc": "A"}}
    app._write_all()
    _wait_drain(app)
    assert "1/1" in app.log_box.get("1.0", "end")
    assert "success" in app.dl_status.cget("text").lower()


def test_write_all_partial_failure(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=False)
    data = _one_key_assignment(app)
    app._assignments = {(1, 1): {"data": data, "desc": "A"}}
    app._write_all()
    _wait_drain(app)
    assert "0/1" in app.log_box.get("1.0", "end")
    assert "failed" in app.dl_status.cget("text").lower()


def test_write_all_marks_ambiguous_on_failed_commit(app):
    app._io_busy = False
    data = _one_key_assignment(app)
    app._assignments = {(1, 1): {"data": data, "desc": "A"}}
    reports, _flash, _ = app._reports_for(1, data)
    # Every report ACKs; the trailing flash commit fails -> outcome "flash".
    app.dev = FakeDev(connected=True, write_ok=True, fail_index=len(reports) + 1)
    app._write_all()
    _wait_drain(app)
    assert app._assignments[(1, 1)].get("ambiguous") is True
    assert "0/1" in app.log_box.get("1.0", "end")


def test_write_all_reports_failure_stays_unambiguous(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=False)   # first report fails
    data = _one_key_assignment(app)
    app._assignments = {(1, 1): {"data": data, "desc": "A"}}
    app._write_all()
    _wait_drain(app)
    assert "ambiguous" not in app._assignments[(1, 1)]


def test_write_all_skips_unbuildable(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True, write_ok=True)
    app._assignments = {(1, 7): {"data": bytes(65), "desc": ""}}  # key id 0 -> None
    app._write_all()
    _wait_drain(app)
    assert "nothing to send" in app.log_box.get("1.0", "end").lower()


def test_write_all_worker_crash_logged(app):
    app._io_busy = False
    app.dev = CrashDev(connected=True)              # write_device raises
    data = _one_key_assignment(app)
    app._assignments = {(1, 1): {"data": data, "desc": "A"}}
    app._write_all()
    _wait_drain(app)
    log = app.log_box.get("1.0", "end").lower()
    assert "error" in log and "0/1" in log
    assert app._io_busy is False


def test_run_download_start_failure_clears_busy(app, monkeypatch):
    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(minikeypad.threading, "Thread", BrokenThread)
    app._io_busy = False

    app._run_download([], "kbd")
    _wait_drain(app)

    assert app._io_busy is False
    assert "can't start new thread" in app.log_box.get("1.0", "end")


def test_run_write_all_start_failure_clears_busy(app, monkeypatch):
    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(minikeypad.threading, "Thread", BrokenThread)
    app._io_busy = False

    app._run_write_all([])
    _wait_drain(app)

    assert app._io_busy is False
    assert "can't start new thread" in app.log_box.get("1.0", "end")


def test_connect_done_clears_busy(app):
    app._io_busy = True
    app._connect_done(True)
    assert app._io_busy is False


def test_try_connect_runs_version_check(app):
    app._io_busy = True
    app.dev = FakeDev(connected=True, write_ok=True)
    app._try_connect()
    _wait_drain(app)
    assert app._io_busy is False
    assert app.kp.ReportID == 3


def test_poll_connection_connected_branch(app):
    app._io_busy = False
    app.dev = FakeDev(connected=True)
    app._poll_connection()
    assert app.state_lbl.cget("text") == "Connected"


def test_poll_connection_disconnected_spawns(app):
    app._io_busy = False
    app.dev = FakeDev(connected=False)
    app._poll_connection()
    _wait_drain(app)
    assert app.state_lbl.cget("text") == "Not connected"


def test_poll_connection_logs_lost_device(app):
    app._io_busy = False
    app.dev = FlakyDev(connected=True)     # connected, but liveness check fails
    app._poll_connection()
    _drain(app)
    assert "Device disconnected" in app.log_box.get("1.0", "end")


# ===========================================================================
#  main()
# ===========================================================================
class _FakeApp:
    created = 0

    def __init__(self):
        _FakeApp.created += 1
        self.destroyed = False

    def mainloop(self):
        pass

    def quit(self):
        pass

    def destroy(self):
        self.destroyed = True


def test_main_skips_install_with_flag(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    called = []
    monkeypatch.setattr(minikeypad, "_ensure_pyusb", lambda: called.append(True))
    minikeypad.main(["--no-auto-install"])
    assert called == []


def test_main_skips_install_by_default(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.delenv("MINIKEYPAD_AUTO_INSTALL", raising=False)
    called = []
    monkeypatch.setattr(minikeypad, "_ensure_pyusb", lambda: called.append(True))
    minikeypad.main([])
    assert called == []


def test_main_auto_installs_with_opt_in_flag(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    called = []
    monkeypatch.setattr(minikeypad, "_ensure_pyusb", lambda: called.append(True))
    minikeypad.main(["--auto-install-pyusb"])
    assert called == [True]


def test_main_auto_installs_with_opt_in_env(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setenv("MINIKEYPAD_AUTO_INSTALL", "1")
    called = []
    monkeypatch.setattr(minikeypad, "_ensure_pyusb", lambda: called.append(True))
    minikeypad.main([])
    assert called == [True]


def test_main_skips_install_when_usb_ok(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", True)
    called = []
    monkeypatch.setattr(minikeypad, "_ensure_pyusb", lambda: called.append(True))
    minikeypad.main([])
    assert called == []


def test_main_installs_signals_and_destroys(monkeypatch):
    monkeypatch.setattr(minikeypad, "App", _FakeApp)
    monkeypatch.setattr(minikeypad, "_USB_OK", True)
    installed = []
    monkeypatch.setattr(minikeypad, "_install_signal_handlers",
                        lambda a: installed.append(a))
    minikeypad.main([])
    assert installed and installed[0].destroyed is True


def test_main_handles_keyboard_interrupt(monkeypatch):
    class KApp(_FakeApp):
        def mainloop(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(minikeypad, "App", KApp)
    monkeypatch.setattr(minikeypad, "_install_signal_handlers", lambda a: None)
    monkeypatch.setattr(minikeypad, "_USB_OK", True)
    minikeypad.main([])          # must not propagate


# ===========================================================================
#  logging + signal helpers
# ===========================================================================
def test_configure_logging_attaches_handler():
    minikeypad._configure_logging(False)
    minikeypad._configure_logging(True)
    assert logging.getLogger().handlers


def test_install_signal_handlers_registers_and_quits(monkeypatch):
    captured = {}
    monkeypatch.setattr(minikeypad.signal, "signal",
                        lambda sig, h: captured.__setitem__(sig, h))

    class A:
        def __init__(self):
            self.quit_called = False

        def quit(self):
            self.quit_called = True

    a = A()
    minikeypad._install_signal_handlers(a)
    assert minikeypad.signal.SIGINT in captured
    captured[minikeypad.signal.SIGINT](minikeypad.signal.SIGINT, None)
    assert a.quit_called is True


def test_install_signal_handlers_swallows_failure(monkeypatch):
    def boom(_sig, _h):
        raise ValueError("not the main thread")

    monkeypatch.setattr(minikeypad.signal, "signal", boom)
    minikeypad._install_signal_handlers(object())   # must not raise


def test_load_profile_rejects_unknown_version(app, tmp_path):
    """A profile whose version != PROFILE_VERSION must fail closed (mkp-rob-01)."""
    bad = tmp_path / "futurever.json"
    bad.write_text('{"version":999,"assignments":[]}')
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_load_profile_rejects_missing_version(app, tmp_path):
    bad = tmp_path / "nover.json"
    bad.write_text('{"assignments":[]}')
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_load_profile_rejects_non_object(app, tmp_path):
    """A top-level JSON array/scalar must fail closed, not crash (mkp-rel-02)."""
    for payload in ("[]", "42", '"str"', "null"):
        bad = tmp_path / "nonobj.json"
        bad.write_text(payload)
        with pytest.raises(ValueError):
            app._load_profile(str(bad))


def test_load_profile_rejects_out_of_range_layer(app, tmp_path):
    """layer outside {1,2,3} is rejected, not stored invisibly (mkp-input-01)."""
    import json as _json
    data_hex = bytes(len(minikeypad.KeyParam().data)).hex()
    bad = tmp_path / "badlayer.json"
    bad.write_text(_json.dumps({"version": 1, "assignments": [
        {"layer": 9, "key_id": 1, "desc": "x", "data": data_hex}]}))
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_load_profile_rejects_out_of_range_key_id(app, tmp_path):
    import json as _json
    data_hex = bytes(len(minikeypad.KeyParam().data)).hex()
    bad = tmp_path / "badkid.json"
    bad.write_text(_json.dumps({"version": 1, "assignments": [
        {"layer": 1, "key_id": 999, "desc": "x", "data": data_hex}]}))
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_load_profile_accepts_led_and_knob_ids(app, tmp_path):
    """In-range ids (LED 176, knob 18) still load (mkp-input-01 no false-reject)."""
    import json as _json
    size = len(minikeypad.KeyParam().data)
    good = tmp_path / "good.json"
    good.write_text(_json.dumps({"version": 1, "assignments": [
        {"layer": 3, "key_id": 176, "desc": "led",
         "data": (bytes([176]) + bytes(size - 1)).hex()},
        {"layer": 2, "key_id": 18, "desc": "knob",
         "data": (bytes([18]) + bytes(size - 1)).hex()}]}))
    assert app._load_profile(str(good)) == 2


def test_load_profile_rejects_key_byte_mismatch(app, tmp_path):
    """Buffer key byte data[0] must match the entry's key_id (mkp-input-02)."""
    import json as _json
    buf = bytearray(len(minikeypad.KeyParam().data))
    buf[0] = 2                                   # buffer targets KEY2
    bad = tmp_path / "mismatch.json"
    bad.write_text(_json.dumps({"version": 1, "assignments": [
        {"layer": 1, "key_id": 1, "desc": "x", "data": bytes(buf).hex()}]}))
    with pytest.raises(ValueError):
        app._load_profile(str(bad))


def test_probe_done_clears_busy(app):
    """_probe_done resets the io mutex so the next poll can run (mkp-thread-01)."""
    app._io_busy = True
    app._probe_done(True)
    assert app._io_busy is False


def test_probe_timeout_abandons_worker_generation_and_logs(app):
    app._io_busy = True
    app._probe_token = 7
    app._monitor.probe_thread = object()
    old_device = app.dev

    app._probe_timeout(7, "Connect")
    _wait_drain(app)

    assert app._io_busy is False
    assert app._monitor.probe_thread is None
    assert app._probe_token == 8
    assert app._monitor.unresponsive is True
    assert app.dev is not old_device
    assert app.state_lbl.cget("text") == "USB unresponsive"
    assert "probe stalled" in app.log_box.get("1.0", "end").lower()


def test_stale_probe_done_does_not_clear_new_busy(app):
    app._io_busy = True
    app._probe_token = 8

    app._probe_done(True, token=7)

    assert app._io_busy is True
    assert app._probe_token == 8


def test_stale_connect_done_does_not_clear_new_busy(app):
    app._io_busy = True
    app._probe_token = 8

    app._connect_done(True, token=7)

    assert app._io_busy is True
    assert app._probe_token == 8


def test_probe_alive_offthread_reports_disconnect(app):
    """_probe_alive posts _probe_done back via the UI queue; a vanished device
    clears busy and is reported, all without blocking the Tk thread."""
    app.dev = FlakyDev()       # connected, but still_connected() -> False
    app._io_busy = True
    app._probe_alive()
    _wait_drain(app)
    assert app._io_busy is False


def test_probe_alive_offthread_reports_alive(app):
    app.dev = FakeDev(connected=True)
    app._io_busy = True
    app._probe_alive()
    _wait_drain(app)
    assert app._io_busy is False


def test_load_dialog_handles_malformed_profile_typeerror(app, tmp_path, monkeypatch):
    """mkp-rel-01: a JSON null where an int is expected raises TypeError from
    _load_profile; _load_dialog must catch it and log 'Load failed', not crash."""
    import json as _json
    data_hex = bytes(len(minikeypad.KeyParam().data)).hex()
    bad = tmp_path / "bad.json"
    bad.write_text(_json.dumps({"version": 1, "assignments": [
        {"layer": None, "key_id": 1, "desc": "x", "data": data_hex}]}))
    monkeypatch.setattr(minikeypad.filedialog, "askopenfilename", lambda **k: str(bad))
    logs = []
    monkeypatch.setattr(app, "log", logs.append)
    app._load_dialog()   # must not raise
    assert any("Load failed" in m for m in logs)


def test_save_profile_fsyncs_before_rename(app, tmp_path, monkeypatch):
    """mkp-robust-21: the temp handle is fsynced before os.replace."""
    calls = []
    real_replace = minikeypad.os.replace
    monkeypatch.setattr(minikeypad.os, "fsync", lambda _fd: calls.append("fsync"))
    monkeypatch.setattr(
        minikeypad.os, "replace",
        lambda src, dst: (calls.append("replace"), real_replace(src, dst)))
    app._assignments = {(1, 1): {"data": bytes([1]) + bytes(64), "desc": "A"}}

    app._save_profile(str(tmp_path / "p.json"))

    assert "fsync" in calls and "replace" in calls
    assert calls.index("fsync") < calls.index("replace")


def test_save_profile_cleans_tmp_on_write_failure(app, tmp_path, monkeypatch):
    """mkp-robust-20: a json.dump failure must not orphan the <path>.tmp file."""
    app._assignments = {(1, 1): {"data": bytes(range(65)), "desc": "A"}}
    path = str(tmp_path / "p.json")

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(minikeypad.json, "dump", boom)
    with pytest.raises(RuntimeError):
        app._save_profile(path)
    assert not (tmp_path / "p.json.tmp").exists(), "orphaned .tmp left behind"


def test_profile_fsync_dir_ignores_open_and_fsync_failures(tmp_path, monkeypatch):
    class OsProxy:
        O_RDONLY = os.O_RDONLY
        fail_open = True

        def open(self, path, flags):
            if self.fail_open:
                raise OSError("open failed")
            return os.open(path, flags)

        @staticmethod
        def fsync(_fd):
            raise OSError("fsync unsupported")

        close = staticmethod(os.close)

    proxy = OsProxy()
    monkeypatch.setattr(minikeypad, "os", proxy)

    minikeypad.ProfileStore._fsync_dir(str(tmp_path))
    proxy.fail_open = False
    minikeypad.ProfileStore._fsync_dir(str(tmp_path))


def test_connection_monitor_surfaces_probe_thread_start_failure(monkeypatch):
    logs = []
    states = []
    scheduled = []
    monitor = minikeypad.ConnectionMonitor(
        lambda: types.SimpleNamespace(connected=False),
        logs.append,
        lambda delay, callback: scheduled.append((delay, callback)),
        lambda callback: callback(),
        lambda: states.append(True),
        lambda: None,
    )

    class Thread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread failed")

    monkeypatch.setattr(minikeypad, "threading", types.SimpleNamespace(Thread=Thread))

    monitor._start_probe_worker(lambda _token: None, "Connect")

    assert not monitor.io_busy
    assert "thread failed" in logs[0]
    assert states == [True]
    assert scheduled == []


def test_connection_monitor_probe_alive_handles_device_failure():
    callbacks = []
    monitor = minikeypad.ConnectionMonitor(
        lambda: types.SimpleNamespace(
            still_connected=lambda: (_ for _ in ()).throw(OSError("gone"))
        ),
        lambda _message: None,
        lambda _delay, _callback: None,
        callbacks.append,
        lambda: None,
        lambda: None,
    )

    monitor.probe_alive(3)
    callbacks[0]()

    assert not monitor.io_busy


def test_connection_monitor_timeout_allows_fresh_generation():
    scheduled = []
    states = []
    recoveries = []
    monitor = minikeypad.ConnectionMonitor(
        lambda: types.SimpleNamespace(connected=False),
        lambda _message: None,
        lambda delay, callback: scheduled.append((delay, callback)),
        lambda callback: callback(),
        lambda: states.append(True),
        lambda: None,
        lambda: recoveries.append(True),
    )
    monitor.token = 4
    monitor.io_busy = True
    monitor.probe_thread = object()

    monitor.probe_timeout(4, "Connect")

    assert monitor.unresponsive
    assert not monitor.io_busy
    assert monitor.probe_thread is None
    assert monitor.token == 5
    assert recoveries == [True]
    assert scheduled == []

    monitor.probe_done(True, token=4)
    assert monitor.unresponsive
    assert not monitor.io_busy
    assert monitor.probe_thread is None
    assert monitor.token == 5
    assert len(states) == 1


def test_trim_log_lines_ignores_invalid_widget_index():
    app = object.__new__(minikeypad.App)
    app.log_box = types.SimpleNamespace(
        index=lambda _position: (_ for _ in ()).throw(ValueError("bad index"))
    )

    app._trim_log_lines()
