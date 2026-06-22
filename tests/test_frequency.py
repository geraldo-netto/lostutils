#!/usr/bin/env python3

from collections import Counter

import frequency


def test_find_repeating_counts_only_requested_prefix():
    values = ["alpha\n", "beta\n", "alpha\n", "ignored\n"]

    assert frequency.findRepeating(values, 3) == Counter({"alpha\n": 2, "beta\n": 1})


def test_find_repeating_handles_empty_prefix():
    assert frequency.findRepeating(["alpha\n"], 0) == Counter()
