#!/usr/bin/env python3

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hypothesis import given, settings, strategies as st

import import_events


FUZZ = settings(max_examples=300, deadline=None)


jsonish = st.one_of(
    st.text(max_size=300),
    st.dictionaries(
        st.text(min_size=1, max_size=10),
        st.text(max_size=30),
        max_size=5,
    ).map(lambda value: "prefix " + __import__("json").dumps(value) + " suffix"),
    st.lists(
        st.dictionaries(st.text(min_size=1, max_size=10), st.text(max_size=30), max_size=5),
        max_size=5,
    ).map(lambda value: "prefix " + __import__("json").dumps(value) + " suffix"),
)


@given(text=jsonish)
@FUZZ
def test_parse_llm_events_never_raises_on_text(text):
    events = import_events.parse_llm_events(text, Path("source.txt"), "Text/LLM")

    assert isinstance(events, list)
    assert all(event["source"] == "source.txt" for event in events)
    assert all(event["type"] == "Text/LLM" for event in events)
