#!/usr/bin/env python3

import importlib.util
import logging
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


def test_normalize_url_defaults_strip_noise_and_collapse_web_scheme():
    key, display = bookmark_tidy.normalize_url(
        "HTTP://www.Example.test:80/path/?utm_source=news&keep=1#section",
        bookmark_tidy.NormalizeOptions(),
    )

    assert key == "web://example.test/path?keep=1"
    assert display == "http://example.test/path?keep=1"


def test_tidy_bookmarks_deduplicates_and_keeps_longest_title():
    bookmarks = [
        bookmark_tidy.Bookmark("http://www.example.test/a/#old", "A", ("Inbox",)),
        bookmark_tidy.Bookmark("https://example.test/a", "Longer title", ("Other",)),
    ]

    tidied = bookmark_tidy.tidy_bookmarks(
        bookmarks,
        immutable_roots=[],
        options=bookmark_tidy.NormalizeOptions(),
        categorizer=lambda batch: {0: ["Reference", "Docs"]},
    )

    assert len(tidied) == 1
    assert tidied[0].url == "https://example.test/a"
    assert tidied[0].title == "Longer title"
    assert tidied[0].folder_path == ("Reference", "Docs")


def test_immutable_folder_is_copied_and_mutable_duplicate_is_removed(caplog):
    immutable = bookmark_tidy.Bookmark(
        "https://example.test/a#locked",
        "Locked",
        ("  Work  ",),
    )
    mutable = bookmark_tidy.Bookmark("http://www.example.test/a", "Mutable", ("Inbox",))

    caplog.set_level(logging.INFO, logger="bookmark-tidy")
    tidied = bookmark_tidy.tidy_bookmarks(
        [immutable, mutable],
        immutable_roots=["work"],
        options=bookmark_tidy.NormalizeOptions(),
        categorizer=lambda batch: {},
    )

    assert tidied == [bookmark_tidy.Bookmark(
        "https://example.test/a#locked",
        "Locked",
        ("  Work  ",),
        immutable=True,
    )]
    assert "Removed mutable duplicate of immutable bookmark" in caplog.text


def test_chrome_export_places_categorized_bookmarks_under_bookmark_bar():
    data = bookmark_tidy.export_chrome_bookmarks(
        [
            bookmark_tidy.Bookmark(
                "https://example.test/a",
                "Example",
                ("Reference", "Docs"),
                root="bookmark_bar",
            )
        ]
    )

    folder = data["roots"]["bookmark_bar"]["children"][0]
    nested = folder["children"][0]
    link = nested["children"][0]

    assert folder["name"] == "Reference"
    assert nested["name"] == "Docs"
    assert link["url"] == "https://example.test/a"
