#!/usr/bin/env python3

import book_chatbot


def test_decode_epub_content_replaces_invalid_utf8():
    assert "\ufffd" in book_chatbot.decode_epub_content(b"valid \xff invalid")


def test_decode_epub_content_uses_declared_xml_encoding():
    raw = '<?xml version="1.0" encoding="iso-8859-1"?><p>ol\xe1</p>'.encode("iso-8859-1")

    assert "olá" in book_chatbot.decode_epub_content(raw)
