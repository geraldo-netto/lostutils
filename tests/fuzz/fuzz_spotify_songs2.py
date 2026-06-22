#!/usr/bin/env python3

import importlib.util
from pathlib import Path

from hypothesis import given, settings, strategies as st


REPO = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("spotify_songs2", REPO / "spotify-songs2.py")
spv2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spv2)

FUZZ = settings(max_examples=300, deadline=None)


@given(tracks=st.lists(st.text(max_size=40), max_size=30))
@FUZZ
def test_resolve_youtube_urls_matches_input_order(tracks):
    urls = spv2.resolve_youtube_urls(
        tracks,
        workers=4,
        matcher=lambda track: f"url:{track}",
    )

    assert urls == [f"url:{track}" for track in tracks]
