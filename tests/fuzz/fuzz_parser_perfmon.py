#!/usr/bin/env python3

import importlib.util
from pathlib import Path

from hypothesis import given, settings, strategies as st


REPO = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("parser_perfmon", REPO / "parser-perfmon.py")
pp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pp)

FUZZ = settings(max_examples=300, deadline=None)


@given(parts=st.lists(st.text(max_size=30), min_size=1, max_size=10))
@FUZZ
def test_normalize_header_returns_line(parts):
    result = pp.normalize_header(parts, "host")

    assert result.endswith("\n")
    assert '"' not in result


@given(path=st.text(min_size=1, max_size=100))
@FUZZ
def test_output_name_never_returns_path_separator(path):
    name = pp.output_name(path)

    assert "/" not in name
    assert "\\" not in name
