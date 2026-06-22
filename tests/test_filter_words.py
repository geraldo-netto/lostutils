#!/usr/bin/env python3

import filter_words
import runpy
import sys


class Lemma:
    def __init__(self, name):
        self._name = name

    def name(self):
        return self._name


class Synset:
    def __init__(self, *names):
        self._lemmas = [Lemma(name) for name in names]

    def lemmas(self):
        return self._lemmas


def test_expand_synonyms_caches_by_casefold(monkeypatch):
    calls = []

    def fake_synsets(word):
        calls.append(word)
        return [Synset("first_word")]

    cache = {}
    monkeypatch.setattr(filter_words.wordnet, "synsets", fake_synsets)

    assert filter_words.expand_synonyms("Alpha", cache) == ["first word"]
    assert filter_words.expand_synonyms("alpha", cache) == ["first word"]
    assert calls == ["Alpha"]


def test_extract_words_returns_deduped_sorted_words(monkeypatch):
    monkeypatch.setattr(
        filter_words,
        "tag",
        lambda _lines: [("R-000123", "NN"), ("Alpha", "NN"), ("Alpha", "NN")],
    )
    monkeypatch.setattr(
        filter_words,
        "expand_synonyms",
        lambda word: ["zeta", "alpha synonym"] if word == "Alpha" else [],
    )

    assert filter_words.extract_words(["ignored"], should_emit=False) == [
        "Alpha",
        "R-000123",
        "alpha synonym",
        "zeta",
    ]


def test_cli_usage_exits_with_failure(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["filter_words.py"])

    try:
        runpy.run_path("filter_words.py", run_name="__main__")
    except SystemExit as ex:
        assert ex.code == 1
    else:
        raise AssertionError("expected SystemExit")
