#!/usr/bin/env python3

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("bookmark_tidy", REPO / "bookmark-tidy.py")
bookmark_tidy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bookmark_tidy)


def _tree(html):
    return bookmark_tidy.BeautifulSoup(html, "html.parser")


def test_links_cleanup_deduplicates_by_href_preserving_first_seen():
    links = bookmark_tidy.links_extract(
        _tree(
            '<a href="https://example.test/a">A</a>'
            '<a href="https://example.test/a">Again</a>'
            '<a href="https://example.test/b">B</a>'
        )
    )

    cleaned = bookmark_tidy.links_cleanup(links)

    assert [link.get("href") for link in cleaned] == [
        "https://example.test/a",
        "https://example.test/b",
    ]


def test_filter_youtube_removes_query_tail_in_href_only():
    link = bookmark_tidy.links_extract(
        _tree('<a href="https://youtube.test/watch?v=1&list=2">Video</a>')
    )[0]

    filtered = bookmark_tidy.filter_youtube(link)

    assert filtered.get("href") == "https://youtube.test/watch?v=1"
    assert filtered.text == "Video"


def test_main_reports_missing_bookmark_file(capsys):
    assert bookmark_tidy.main(["/no/such/bookmarks.html", "/no/such/config.json"]) == 1
    assert "missing bookmark file" in capsys.readouterr().err
