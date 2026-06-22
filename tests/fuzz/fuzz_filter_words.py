#!/usr/bin/env python3

from hypothesis import given, settings, strategies as st

import filter_words


FUZZ = settings(max_examples=300, deadline=None)


@given(words=st.lists(st.text(max_size=30), max_size=100))
@FUZZ
def test_dedup_sort_is_sorted_unique(words):
    result = filter_words.dedup_sort(words)

    assert result == sorted(dict.fromkeys(words))


@given(word=st.text(alphabet=st.characters(min_codepoint=65, max_codepoint=122), max_size=30))
@FUZZ
def test_expand_synonyms_uses_supplied_cache(word):
    calls = []

    def fake_synsets(value):
        calls.append(value)
        return []

    original = filter_words.wordnet.synsets
    cache = {}
    try:
        filter_words.wordnet.synsets = fake_synsets
        assert filter_words.expand_synonyms(word, cache) == []
        assert filter_words.expand_synonyms(word.swapcase(), cache) == []
        assert len(calls) == 1
    finally:
        filter_words.wordnet.synsets = original
