#!/usr/bin/env python3

import importlib.util
import json
import string
from pathlib import Path

from hypothesis import given, settings, strategies as st


REPO = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("bookmark_tidy", REPO / "bookmark-tidy.py")
bookmark_tidy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bookmark_tidy)

FUZZ = settings(max_examples=300, deadline=None)


href_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters='"\x00<>'),
    max_size=100,
)
url_part = st.text(alphabet=string.ascii_letters + string.digits + "-_", min_size=1, max_size=12)
host_label = st.text(alphabet=string.ascii_lowercase + string.digits, min_size=1, max_size=12)
web_url = st.builds(
    lambda scheme, host, path, query, fragment: (
        f"{scheme}://www.{host}.example.test"
        f"{''.join('/' + segment for segment in path)}"
        f"{'?' + query if query else ''}"
        f"{'#' + fragment if fragment else ''}"
    ),
    st.sampled_from(["http", "https"]),
    host_label,
    st.lists(url_part, max_size=3),
    st.sampled_from(["", "a=1", "utm_source=x&a=1", "gclid=x"]),
    st.sampled_from(["", "frag", "section-1"]),
)
folder_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00/\\>"),
    min_size=0,
    max_size=30,
)


def _link(href):
    tree = bookmark_tidy.BeautifulSoup(f'<a href="{href}">x</a>', "html.parser")
    return bookmark_tidy.links_extract(tree)[0]


@given(hrefs=st.lists(href_text, max_size=50))
@FUZZ
def test_links_cleanup_keeps_first_unique_href(hrefs):
    links = [_link(href) for href in hrefs]

    cleaned = bookmark_tidy.links_cleanup(links)

    expected = list(dict.fromkeys(hrefs))
    assert [link.get("href", "") for link in cleaned] == expected


@given(href=href_text)
@FUZZ
def test_filter_youtube_never_removes_non_youtube_href(href):
    link = _link(href)

    filtered = bookmark_tidy.filter_youtube(link)

    if "youtube" not in href or "&" not in href:
        assert filtered.get("href", "") == href


@given(url=web_url)
@FUZZ
def test_normalize_url_default_is_idempotent(url):
    first = bookmark_tidy.normalize_url(url, bookmark_tidy.NormalizeOptions())
    second = bookmark_tidy.normalize_url(first[1], bookmark_tidy.NormalizeOptions())

    assert first == second
    assert "#" not in first[1]
    assert "utm_" not in first[0]
    assert "gclid=" not in first[0]
    assert first[0].startswith("web://")


@given(urls=st.lists(web_url, max_size=40))
@FUZZ
def test_deduplicate_bookmarks_keeps_unique_mutable_canonical_urls(urls):
    bookmarks = [
        bookmark_tidy.Bookmark(url, f"title {index}", ("Inbox",))
        for index, url in enumerate(urls)
    ]

    immutable, mutable = bookmark_tidy.deduplicate_bookmarks(
        bookmarks,
        immutable_roots=set(),
        options=bookmark_tidy.NormalizeOptions(),
    )

    keys = [bookmark_tidy.normalize_url(bookmark.url, bookmark_tidy.NormalizeOptions())[0] for bookmark in mutable]
    assert immutable == []
    assert len(keys) == len(set(keys))
    assert len(mutable) <= len(urls)


@given(category=st.one_of(folder_text, st.lists(folder_text, max_size=5)))
@FUZZ
def test_category_path_never_returns_empty_parts(category):
    path = bookmark_tidy._category_path(category, "Fallback")

    assert path
    assert all(part and part == " ".join(part.split()) for part in path)


@given(category=st.lists(url_part, min_size=1, max_size=4))
@FUZZ
def test_parse_category_response_roundtrips_item_categories(category):
    payload = json.dumps({"items": [{"id": 0, "category": category}]})

    parsed = bookmark_tidy.parse_category_response(payload, 1)

    assert parsed == {0: tuple(category)}


@given(
    items=st.lists(
        st.tuples(web_url, url_part, st.lists(url_part, max_size=2)),
        min_size=1,
        max_size=12,
    )
)
@settings(max_examples=80, deadline=None)
def test_netscape_export_parse_preserves_urls(items):
    bookmarks = [
        bookmark_tidy.Bookmark(url, title, tuple(folders), root="bookmark_bar")
        for url, title, folders in items
    ]
    parser = bookmark_tidy.NetscapeBookmarkParser("fuzz")

    parser.feed(bookmark_tidy.export_netscape_bookmarks(bookmarks))

    assert sorted(bookmark.url for bookmark in parser.bookmarks) == sorted(url for url, _, _ in items)
