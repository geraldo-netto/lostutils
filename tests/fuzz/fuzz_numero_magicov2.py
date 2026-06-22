#!/usr/bin/env python3

from hypothesis import given, settings, strategies as st

import numero_magicov2 as nm


FUZZ = settings(max_examples=300, deadline=None)


@given(name=st.text(max_size=100))
@FUZZ
def test_calculate_name_total_matches_letter_value_sum(name):
    values = nm.calculate_letter_values(name)

    assert nm.calculate_name_total(name) == sum(value for _char, value in values)
    assert all(char in nm.LETTER_MAP for char, _value in values)


@given(n=st.integers(min_value=0, max_value=10**12))
@FUZZ
def test_reduce_to_single_digit_returns_digital_root_range(n):
    result = nm.reduce_to_single_digit(n)

    if n == 0:
        assert result == 0
    else:
        assert 1 <= result <= 9
        assert result == (n - 1) % 9 + 1
