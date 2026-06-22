#!/usr/bin/env python3

import cloud_words
import pytest


def test_read_first_description_reads_only_needed_column_and_row(monkeypatch):
    calls = {}

    class Frame:
        columns = ["description"]
        empty = False

        class Description(list):
            @property
            def iloc(self):
                return self

        description = Description(["first review"])

    def fake_read_csv(csv_path, **kwargs):
        calls["csv_path"] = csv_path
        calls["kwargs"] = kwargs
        return Frame()

    monkeypatch.setattr(cloud_words.pd, "read_csv", fake_read_csv)

    assert cloud_words.read_first_description("reviews.csv") == "first review"
    assert calls == {
        "csv_path": "reviews.csv",
        "kwargs": {"usecols": ["description"], "nrows": 1},
    }


def test_read_first_description_rejects_empty_description(monkeypatch):
    class Series:
        iloc = [None]

    class Frame:
        columns = ["description"]
        empty = False
        description = Series()

    monkeypatch.setattr(cloud_words.pd, "read_csv", lambda *args, **kwargs: Frame())

    with pytest.raises(ValueError):
        cloud_words.read_first_description("reviews.csv")


def test_main_reports_missing_csv(capsys):
    assert cloud_words.main(["/no/such/file.csv"]) == 1
    assert "missing CSV file" in capsys.readouterr().err
