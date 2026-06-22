#!/usr/bin/env python3

import importlib.util
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st


REPO = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location(
    "masterclass_mass_rename", REPO / "masterclass-mass-rename.py"
)
mmr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mmr)

FUZZ = settings(max_examples=300, deadline=None)


@given(name=st.text(max_size=120))
@FUZZ
def test_clean_name_trims_and_removes_configured_chars(name):
    if not name or len(name.strip()) == 0:
        with pytest.raises(ValueError):
            mmr.clean_name(name)
        return

    cleaned = mmr.clean_name(name)

    assert cleaned == cleaned.strip()
    assert not any(char in cleaned for char in mmr.REMOVE_CHARS)
