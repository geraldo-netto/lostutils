#!/usr/bin/env python3
"""Unit tests for minikeypad's pure working-buffer + download assembly.

No Tk window and no USB device are created: every test drives KeyParam, the
device-independent buffer packer, directly.
"""

import minikeypad
from minikeypad import KeyParam, MAX_KBD_GROUPS


def _select(kp, key_id=1):
    assert kp.select_physical_key(key_id) is True
    return kp


def _built(kp):
    result = kp.build_download_reports()
    assert result is not None
    return result


# --------------------------------------------------------------------------- #
#  selection / clearing
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
#  keyboard packing
# --------------------------------------------------------------------------- #
def test_basic_key_packs_code_and_advances_pointer():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
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


def test_kbd_download_reports_round_trip():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    reports, flash, truncated = _built(kp)
    assert flash == "kbd"
    assert truncated is False
    # one group -> probe (b=0) + the real group (b=1) + flush handled by caller
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
    # groups clamped to MAX_KBD_GROUPS -> MAX_KBD_GROUPS+1 reports (b=0..MAX)
    assert len(reports) == MAX_KBD_GROUPS + 1
    assert all(r[2] == MAX_KBD_GROUPS for r in reports)


# --------------------------------------------------------------------------- #
#  multimedia / mouse / led
# --------------------------------------------------------------------------- #
def test_multimedia_selects_value_by_report_id():
    kp = _select(KeyParam())
    kp.ReportID = 0
    kp.multimedia("Vol +", (0, 2), (0, 64), (0, 233))
    reports, flash, _ = _built(kp)
    assert flash == "kbd"
    assert reports[0][2] == 2             # rid0 value
    assert kp.data[KeyParam.KeyType_Num] & 0xF == 2


def test_mouse_masks_all_bytes_to_one_byte():
    kp = _select(KeyParam())
    kp.mouse("Wheel -", 0, 0, 0, 0x1FF, 0x102)   # over-wide values
    reports, flash, _ = _built(kp)
    assert flash == "kbd"
    assert kp.data[8] == 0xFF                      # b3 masked
    assert kp.data[9] == 0x02                      # b4 masked
    assert reports[0][5] == 0xFF


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


# --------------------------------------------------------------------------- #
#  display helpers
# --------------------------------------------------------------------------- #
def test_key_text_empty_without_selection():
    assert KeyParam().key_text() == ""


def test_key_text_joins_assigned_labels():
    kp = _select(KeyParam())
    kp.basic_key(4, "A")
    assert kp.key_text() == "A"


def test_flash_buf_mapping():
    assert minikeypad.App._flash_buf("kbd")[1] == 0xAA
    assert minikeypad.App._flash_buf("led")[1] == 0xA1


# --------------------------------------------------------------------------- #
#  pyusb auto-install (mocked: never touches the network)
# --------------------------------------------------------------------------- #
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


def test_ensure_pyusb_returns_false_when_install_fails(monkeypatch):
    monkeypatch.setattr(minikeypad, "_USB_OK", False)
    monkeypatch.setattr(minikeypad, "_pip_install", lambda _pkg: False)
    assert minikeypad._ensure_pyusb() is False
