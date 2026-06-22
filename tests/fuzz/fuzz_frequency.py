#!/usr/bin/env python3

from collections import Counter

from hypothesis import given, settings, strategies as st

import frequency


FUZZ = settings(max_examples=300, deadline=None)


@given(
    values=st.lists(st.text(max_size=30), max_size=100),
    prefix=st.integers(min_value=0, max_value=100),
)
@FUZZ
def test_find_repeating_matches_counter_prefix(values, prefix):
    size = min(prefix, len(values))

    assert frequency.findRepeating(values, size) == Counter(values[:size])
