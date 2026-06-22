#!/usr/bin/env python3

import numero_magicov2 as nm


def test_calculate_letter_values_returns_pairs_without_side_effects(capsys):
    assert nm.calculate_letter_values("Ana!") == [("A", 1), ("N", 5), ("A", 1)]
    assert capsys.readouterr().out == ""


def test_calculate_name_total_uses_letter_values():
    assert nm.calculate_name_total("Ana!") == 7


def test_reduce_to_single_digit_keeps_nine_as_nine():
    assert nm.reduce_to_single_digit(18) == 9
