"""Tests for deduplicate-by-namev3.py — dnv3-rel-* reliability fixes
(threshold clamp against uint8 overflow, word-boundary cleanup)."""
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("rapidfuzz")
from hypothesis import given, strategies as st

_PATH = Path(__file__).resolve().parent.parent / "deduplicate-by-namev3.py"
_spec = importlib.util.spec_from_file_location("deduplicate_by_namev3", _PATH)
dn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dn)


# --- dnv3-rel-01: threshold clamped to <= MAX_THRESHOLD (uint8 safety) -------

def test_clamp_threshold_passthrough_default():
    assert dn.clamp_threshold(dn.DEFAULT_THRESHOLD) == dn.DEFAULT_THRESHOLD


def test_clamp_threshold_at_limit_unchanged():
    assert dn.clamp_threshold(dn.MAX_THRESHOLD) == dn.MAX_THRESHOLD


def test_clamp_threshold_over_limit_is_clamped(capsys):
    assert dn.clamp_threshold(300) == dn.MAX_THRESHOLD
    err = capsys.readouterr().err
    assert "clamp" in err.lower()
    assert str(dn.MAX_THRESHOLD) in err


def test_clamp_threshold_no_warning_within_limit(capsys):
    dn.clamp_threshold(0)
    assert capsys.readouterr().err == ""


@given(st.integers(min_value=-10, max_value=10_000))
def test_clamp_threshold_never_exceeds_limit(t):
    assert dn.clamp_threshold(t) <= dn.MAX_THRESHOLD


@given(st.integers(min_value=-10, max_value=dn.MAX_THRESHOLD))
def test_clamp_threshold_identity_within_range(t):
    assert dn.clamp_threshold(t) == t


# --- end-to-end: large -t does not wrap / corrupt grouping ------------------

def _run_main(monkeypatch, tmp_path, lines, threshold):
    f = tmp_path / "in.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        dn.sys, "argv",
        ["prog", str(f), "-t", str(threshold), "-w", "1"],
    )
    out = []
    monkeypatch.setattr(dn.sys.stdout, "write", lambda s: out.append(s) or len(s))
    dn.main()
    return "".join(out)


def test_main_large_threshold_clamped_not_wrapped(monkeypatch, tmp_path):
    # Two far-apart strings; an unclamped uint8 wrap could falsely match them.
    lines = ["a", "z" * 50]
    out = _run_main(monkeypatch, tmp_path, lines, 300)
    # distance is 50, well under clamp(254); no pair should be reported as 0-ish wrap
    for ln in out.splitlines():
        parts = ln.split(";")
        assert int(parts[2]) <= dn.MAX_THRESHOLD


def test_main_reports_self_collision(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, ["dup", "dup"], 7)
    assert "dup;dup;0" in out


def test_main_reports_near_pair(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, ["hello", "hallo"], 7)
    assert any(line.endswith(";1") for line in out.splitlines())


def test_main_empty_file_noop(monkeypatch, tmp_path):
    out = _run_main(monkeypatch, tmp_path, [""], 7)
    assert out == ""


# --- dnv3-rel-02: cleanup strips only standalone "xxx"/"monography" ---------

def test_cleanup_strips_standalone_tokens():
    assert dn.cleanup("xxx monography") == " "


def test_cleanup_keeps_words_containing_tokens():
    assert dn.cleanup("xxxl") == "xxxl"
    assert dn.cleanup("monographymania") == "monographymania"
    assert dn.cleanup("amonography") == "amonography"


def test_cleanup_strips_token_among_words():
    assert dn.cleanup("my xxx file") == "my  file"


def test_cleanup_basic_replacements_and_case():
    assert dn.cleanup("  A,[B] ") == "ab"


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12))
def test_cleanup_never_strips_token_inside_longer_word(word):
    # A pure-alpha word longer than a token and containing it as a substring,
    # but not equal to it, must survive intact (no boundary).
    for tok in dn.WORD_TOKENS:
        if tok in word and word != tok:
            assert dn.cleanup(word) == word


@given(st.lists(st.sampled_from(["xxx", "monography", "alpha", "beta"]),
                min_size=1, max_size=6))
def test_cleanup_drops_all_standalone_tokens(words):
    result = dn.cleanup(" ".join(words))
    for tok in dn.WORD_TOKENS:
        assert tok not in result.split()
