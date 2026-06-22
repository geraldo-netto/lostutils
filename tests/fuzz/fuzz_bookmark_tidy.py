#!/usr/bin/env python3

import importlib.util
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
