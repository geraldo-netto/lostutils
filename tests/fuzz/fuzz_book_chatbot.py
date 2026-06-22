#!/usr/bin/env python3

from hypothesis import given, settings, strategies as st

import book_chatbot


FUZZ = settings(max_examples=300, deadline=None)


@given(raw=st.binary(max_size=300))
@FUZZ
def test_decode_epub_content_never_raises(raw):
    assert isinstance(book_chatbot.decode_epub_content(raw), str)
