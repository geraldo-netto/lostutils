#!/usr/bin/env python3

import builtins
import importlib.util
import json
import logging
import multiprocessing
import sqlite3
import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("bookmark_tidy", REPO / "bookmark-tidy.py")
bookmark_tidy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bookmark_tidy)


def _sample_bookmark(url="https://example.test/a", title="Example", folders=("Docs",)):
    return bookmark_tidy.Bookmark(url, title, folders, root="bookmark_bar", add_date=10, last_modified=20)


def _netscape_html():
    return """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3>Bookmarks Bar</H3>
    <DL><p>
        <DT><H3>Work</H3>
        <DL><p>
            <DT><A HREF="https://example.test/a#frag" ADD_DATE="12" LAST_MODIFIED="20">Alpha</A>
        </DL><p>
    </DL><p>
    <DT><H3>Other Bookmarks</H3>
    <DL><p>
        <DT><A HREF="http://www.example.test/b/">Beta</A>
    </DL><p>
</DL><p>
"""


def _lz4_literal_block(raw):
    extra = len(raw) - 15
    payload = bytearray([0xF0 if extra >= 0 else len(raw) << 4])
    while extra >= 255:
        payload.append(255)
        extra -= 255
    if extra >= 0:
        payload.append(extra)
    payload.extend(raw)
    return bytes(payload)


def _mozlz4_literal(raw, declared=None):
    """Real mozLz4 layout: magic + little-endian uint32 decompressed size + LZ4
    block (bt-rel-50). `declared` overrides the size field for the mismatch test."""
    size = len(raw) if declared is None else declared
    return (
        bookmark_tidy.MOZLZ4_MAGIC
        + size.to_bytes(bookmark_tidy.MOZLZ4_SIZE_BYTES, "little")
        + _lz4_literal_block(raw)
    )


class FakeLlamaChat:
    kwargs = {}

    def __init__(self, **kwargs):
        FakeLlamaChat.kwargs = kwargs

    def create_chat_completion(self, **kwargs):
        assert "messages" in kwargs
        return {"choices": [{"message": {"content": '{"items":[{"id":0,"category":["AI","Docs"]}]}'}}]}


class FakeLlamaText:
    kwargs = {}

    def __init__(self, **kwargs):
        FakeLlamaText.kwargs = kwargs

    def __call__(self, prompt, **kwargs):
        assert "Categorize" in prompt
        return {"choices": [{"text": '{"0":"Reference/Docs"}'}]}


class HangingLlama:
    kwargs = {}

    def __init__(self, **kwargs):
        HangingLlama.kwargs = kwargs

    def create_chat_completion(self, **kwargs):
        assert "messages" in kwargs
        bookmark_tidy.time.sleep(0.2)
        return {"choices": [{"message": {"content": '{"items":[]}'}}]}


class BrokenInferenceLlama:
    def __init__(self, **kwargs):
        pass

    def create_chat_completion(self, **kwargs):
        raise RuntimeError("worker failed")


class FakeLlamaConnection:
    def __init__(self, received=(), *, poll_result=True, send_error=None, recv_error=None):
        self.received = list(received)
        self.poll_result = poll_result
        self.send_error = send_error
        self.recv_error = recv_error
        self.sent = []
        self.closed = False

    def send(self, value):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(value)

    def poll(self, _timeout):
        return self.poll_result

    def recv(self):
        if self.recv_error is not None:
            raise self.recv_error
        return self.received.pop(0)

    def close(self):
        self.closed = True


def _install_fake_llama(monkeypatch, llama_cls):
    module = types.ModuleType("llama_cpp")
    module.Llama = llama_cls
    monkeypatch.setitem(sys.modules, "llama_cpp", module)


# bt-mt-01: production uses `spawn`. The tests below drive a REAL worker
# process against the in-process fake `llama_cpp` installed above, which only
# `fork` can inherit — so they inject a fork context explicitly. The
# DeprecationWarning they trigger is about fork, which production no longer
# uses; it is suppressed per-test rather than globally.
_FORK_CONTEXT = (
    multiprocessing.get_context("fork")
    if "fork" in multiprocessing.get_all_start_methods()
    else None
)
_forking_worker = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _fork_categorizer(*args, **kwargs):
    if _FORK_CONTEXT is None:  # pragma: no cover - non-POSIX
        pytest.skip("fork start method unavailable on this platform")
    return bookmark_tidy.LlamaCategorizer(*args, mp_context=_FORK_CONTEXT, **kwargs)


def test_llama_start_method_is_not_fork():
    """bt-mt-01: forking a multithreaded parent can deadlock the child."""
    assert bookmark_tidy.LLAMA_START_METHOD == "spawn"
    assert bookmark_tidy.LLAMA_START_METHOD in multiprocessing.get_all_start_methods()


def test_main_reports_missing_bookmark_file(capsys):
    assert bookmark_tidy.main(["/no/such/bookmarks.html", "/no/such/config.json"]) == 1
    assert "missing bookmark file" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (OSError("disk full"), "disk full"),
        (sqlite3.OperationalError("database locked"), "database locked"),
        (RuntimeError("model failed"), "model failed"),
    ],
)
def test_main_formats_operational_errors(monkeypatch, capsys, exc, message):
    monkeypatch.setattr(bookmark_tidy, "_run", lambda _args: (_ for _ in ()).throw(exc))

    assert bookmark_tidy.main([]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert message in err


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


def test_merge_bookmark_group_prefers_human_title_to_url_placeholder():
    url_placeholder = "http://www.example.test/a/"
    merged = bookmark_tidy._merge_bookmark_group(
        [
            bookmark_tidy.Bookmark(url_placeholder, url_placeholder),
            bookmark_tidy.Bookmark("https://example.test/a", "Docs"),
        ],
        bookmark_tidy.NormalizeOptions(),
    )

    assert merged.title == "Docs"


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


def test_mutable_duplicate_log_redacts_url_userinfo(caplog):
    immutable = bookmark_tidy.Bookmark(
        "https://alice:secret@example.test/a",
        "Locked",
        ("Work",),
    )
    mutable = bookmark_tidy.Bookmark(
        "https://alice:secret@example.test/a#copy",
        "Mutable",
        ("Inbox",),
    )

    caplog.set_level(logging.INFO, logger="bookmark-tidy")
    bookmark_tidy.tidy_bookmarks(
        [immutable, mutable],
        immutable_roots=["work"],
        options=bookmark_tidy.NormalizeOptions(),
        categorizer=lambda batch: {},
    )

    assert "secret" not in caplog.text
    assert "alice" not in caplog.text
    assert "https://[redacted]@example.test/a#copy" in caplog.text


def test_immutable_root_matches_only_browser_root_or_top_level_folder():
    top_level = bookmark_tidy.Bookmark("https://example.test/top", "Top", ("Work",))
    nested = bookmark_tidy.Bookmark("https://example.test/nested", "Nested", ("Archive", "Work"))
    browser_root = bookmark_tidy.Bookmark("https://example.test/root", "Root", (), root="other")
    immutable = bookmark_tidy._immutable_names(["work", "Other Bookmarks"])

    assert bookmark_tidy.is_immutable_bookmark(top_level, immutable)
    assert not bookmark_tidy.is_immutable_bookmark(nested, immutable)
    assert bookmark_tidy.is_immutable_bookmark(browser_root, immutable)


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


def test_small_helpers_cover_root_and_time_edges():
    assert bookmark_tidy._clean_folder_part("  A   B  ") == "A B"
    assert bookmark_tidy._folder_match_key(" Work ") == "work"
    assert bookmark_tidy._root_from_name("Bookmarks Toolbar") == "bookmark_bar"
    assert bookmark_tidy._root_display("unknown") == "Bookmarks Bar"
    assert bookmark_tidy._split_root_path(["Other Bookmarks", "X"]) == ("other", ("X",))
    assert bookmark_tidy._split_root_path(["X"]) == ("bookmark_bar", ("X",))
    assert bookmark_tidy._bookmark_title("  ", "https://fallback.test") == "https://fallback.test"
    assert bookmark_tidy._safe_int("bad") is None
    assert bookmark_tidy._chrome_time_to_unix(None) is None
    assert bookmark_tidy._chrome_time_to_unix(0) is None
    assert bookmark_tidy._firefox_time_to_unix("2000000") == 2
    assert bookmark_tidy._unix_to_chrome_time(1) == str((1 + bookmark_tidy.WEBKIT_EPOCH_OFFSET_SECONDS) * 1_000_000)
    assert bookmark_tidy._unix_to_firefox_time(2) == 2_000_000
    assert bookmark_tidy._attrs_to_dict([("HREF", "x"), ("empty", None)]) == {"href": "x", "empty": ""}


def test_read_netscape_bookmarks_parses_roots_and_folders(tmp_path):
    path = tmp_path / "bookmarks.html"
    path.write_text(_netscape_html(), encoding="utf-8")

    bookmarks = bookmark_tidy.read_netscape_bookmarks(path)

    assert [(item.root, item.folder_path, item.title) for item in bookmarks] == [
        ("bookmark_bar", ("Work",), "Alpha"),
        ("other", (), "Beta"),
    ]
    assert bookmarks[0].add_date == 12
    assert bookmarks[0].last_modified == 20


def test_read_netscape_bookmarks_honours_localized_root_markers():
    text = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
    <DL><p>
      <DT><H3 PERSONAL_TOOLBAR_FOLDER="TrUe">Favoritenleiste</H3>
      <DL><p><DT><A HREF="https://example.test/bar">Bar</A></DL><p>
      <DT><H3 UNFILED_BOOKMARKS_FOLDER="true">Nicht abgelegt</H3>
      <DL><p><DT><A HREF="https://example.test/other">Other</A></DL><p>
    </DL><p>
    """

    bookmarks = bookmark_tidy.netscape_html_to_bookmarks(text, "localized.html")

    assert [(item.root, item.folder_path) for item in bookmarks] == [
        ("bookmark_bar", ()),
        ("other", ()),
    ]
    assert bookmark_tidy.is_immutable_bookmark(
        bookmarks[1], bookmark_tidy._immutable_names(["Other Bookmarks"])
    )


def test_read_chromium_bookmarks_parses_nested_folders(tmp_path):
    path = tmp_path / "Bookmarks"
    path.write_text(
        json.dumps(
            {
                "roots": {
                    "bookmark_bar": {
                        "children": [
                            {
                                "type": "folder",
                                "name": "Tech",
                                "children": [
                                    {
                                        "type": "url",
                                        "name": "Alpha",
                                        "url": "https://example.test/a",
                                        "date_added": bookmark_tidy._unix_to_chrome_time(5),
                                    }
                                ],
                            }
                        ]
                    },
                    "other": {"children": [{"type": "url", "name": "", "url": "https://example.test/b"}]},
                }
            }
        ),
        encoding="utf-8",
    )

    bookmarks = bookmark_tidy.read_chromium_bookmarks(path)

    assert bookmarks[0].folder_path == ("Tech",)
    assert bookmarks[0].add_date == 5
    assert bookmarks[1].title == "https://example.test/b"


def test_read_chromium_bookmarks_skips_non_mapping_root_metadata(tmp_path):
    path = tmp_path / "Bookmarks"
    path.write_text(
        json.dumps(
            {
                "roots": {
                    "sync_transaction_version": "1",
                    "bookmark_bar": {
                        "children": [
                            {
                                "type": "url",
                                "name": "Alpha",
                                "url": "https://example.test/a",
                            }
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    bookmarks = bookmark_tidy.read_chromium_bookmarks(path)

    assert [bookmark.url for bookmark in bookmarks] == ["https://example.test/a"]


def test_read_firefox_sqlite_bookmarks(tmp_path):
    path = tmp_path / "places.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_bookmarks (
            id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER, parent INTEGER,
            position INTEGER, title TEXT, dateAdded INTEGER, lastModified INTEGER, guid TEXT
        );
        """
    )
    conn.execute("INSERT INTO moz_places VALUES (1, 'https://example.test/a', 'Place title')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (1, 2, NULL, NULL, 0, 'root', 0, 0, 'root________')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (2, 2, NULL, 1, 0, 'toolbar', 0, 0, 'toolbar_____')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (3, 2, NULL, 2, 0, 'Research', 0, 0, 'folder______')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (4, 1, 1, 3, 0, NULL, 3000000, 4000000, 'bookmark____')")
    conn.commit()
    conn.close()

    bookmarks = bookmark_tidy.read_firefox_sqlite_bookmarks(path)

    assert bookmarks == [
        bookmark_tidy.Bookmark(
            "https://example.test/a",
            "Place title",
            ("Research",),
            root="bookmark_bar",
            add_date=3,
            last_modified=4,
            source=str(path),
        )
    ]


def test_read_firefox_sqlite_bookmarks_includes_wal_rows(tmp_path):
    path = tmp_path / "places.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT);
        CREATE TABLE moz_bookmarks (
            id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER, parent INTEGER,
            position INTEGER, title TEXT, dateAdded INTEGER, lastModified INTEGER, guid TEXT
        );
        """
    )
    conn.execute("INSERT INTO moz_places VALUES (1, 'https://wal.example.test', 'WAL title')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (1, 2, NULL, NULL, 0, 'root', 0, 0, 'root________')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (2, 2, NULL, 1, 0, 'toolbar', 0, 0, 'toolbar_____')")
    conn.execute("INSERT INTO moz_bookmarks VALUES (3, 1, 1, 2, 0, 'Bookmark', 3000000, 4000000, 'bookmark____')")
    conn.commit()
    try:
        bookmarks = bookmark_tidy.read_firefox_sqlite_bookmarks(path)
    finally:
        conn.close()

    assert bookmarks[0].url == "https://wal.example.test"


def test_firefox_rows_to_bookmarks_skips_parent_cycle(caplog):
    rows = [
        (1, 2, 2, "toolbar", 0, 0, "toolbar_____", None, None),
        (2, 2, 1, "Research", 0, 0, "folder______", None, None),
        (3, 1, 2, "Leaf", 3000000, 4000000, "bookmark____", "https://example.test/a", "Place title"),
    ]

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")
    bookmarks = bookmark_tidy._firefox_rows_to_bookmarks(rows, "places.sqlite")

    assert [bookmark.url for bookmark in bookmarks] == ["https://example.test/a"]
    assert bookmarks[0].folder_path == ("Research",)
    assert "cyclic Firefox bookmark folder edge" in caplog.text


def test_read_firefox_json_bookmarks(tmp_path):
    path = tmp_path / "firefox.json"
    path.write_text(
        json.dumps(
            {
                "root": "placesRoot",
                "children": [
                    {
                        "root": "toolbarFolder",
                        "children": [
                            {
                                "title": "Research",
                                "children": [
                                    {
                                        "typeCode": 1,
                                        "uri": "https://example.test/a",
                                        "title": "",
                                        "dateAdded": 5_000_000,
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    bookmarks = bookmark_tidy.read_firefox_json_bookmarks(path)

    assert bookmarks[0].root == "bookmark_bar"
    assert bookmarks[0].folder_path == ("Research",)
    assert bookmarks[0].title == "https://example.test/a"
    assert bookmarks[0].add_date == 5


def test_read_firefox_jsonlz4_bookmarks(tmp_path):
    path = tmp_path / "bookmarks.jsonlz4"
    payload = {
        "root": "placesRoot",
        "children": [
            {
                "root": "unfiledBookmarksFolder",
                "children": [{"typeCode": 1, "uri": "https://lz4.example.test", "title": "LZ4"}],
            }
        ],
    }
    path.write_bytes(_mozlz4_literal(json.dumps(payload).encode("utf-8")))

    bookmarks = bookmark_tidy.read_firefox_jsonlz4_bookmarks(path)

    assert bookmarks == [
        bookmark_tidy.Bookmark("https://lz4.example.test", "LZ4", (), root="other", source=str(path))
    ]
    assert bookmark_tidy._detect_bookmark_format_with_data(path)[0] == "firefox-jsonlz4"
    assert bookmark_tidy._supported_input_file(path)


def test_read_firefox_jsonlz4_rejects_bad_container():
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(b"not-lz4")
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_lz4_block(bytes([0x10, 0x01, 0x00]))


def test_decode_mozlz4_honours_the_size_header():
    """bt-rel-50: the 4 bytes after the magic are a size field, not block data."""
    raw = b'{"root":"placesRoot"}'
    assert bookmark_tidy._decode_mozlz4(_mozlz4_literal(raw)) == raw
    # The pre-fix layout (magic immediately followed by the block) must now fail
    # instead of decoding, so a regression can't pass silently.
    legacy_payload = bookmark_tidy.MOZLZ4_MAGIC + _lz4_literal_block(raw)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(legacy_payload)


@pytest.mark.parametrize("declared", [3, 999])
def test_decode_mozlz4_rejects_size_mismatch(declared):
    payload = _mozlz4_literal(b"payload-bytes", declared)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(payload)


def test_decode_mozlz4_rejects_truncated_size_header():
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(bookmark_tidy.MOZLZ4_MAGIC + b"\x01\x02")


def test_decode_mozlz4_rejects_oversized_declared_size():
    oversized = bookmark_tidy.MAX_LZ4_OUTPUT_BYTES + 1
    payload = (
        bookmark_tidy.MOZLZ4_MAGIC
        + oversized.to_bytes(bookmark_tidy.MOZLZ4_SIZE_BYTES, "little")
    )
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(payload)


@pytest.mark.parametrize("suffix", [".html", ".json"])
def test_bookmark_readers_reject_invalid_utf8(tmp_path, suffix):
    path = tmp_path / f"bookmarks{suffix}"
    path.write_bytes(b'{"roots":{"bookmark_bar":{"children":[' + b"\xff" + b"]}}}")

    with pytest.raises(bookmark_tidy.UserError, match="valid UTF-8"):
        bookmark_tidy.read_bookmark_file(path)


def test_lz4_match_copy_decodes_repeated_sequence():
    data = bytes([0x32]) + b"abc" + b"\x03\x00"

    assert bookmark_tidy._decode_lz4_block(data) == b"abcabcabc"

    output = bytearray(b"abc")
    bookmark_tidy._copy_lz4_match(output, 3, 6)
    assert bytes(output) == b"abcabcabc"

    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._copy_lz4_match(bytearray(b"a"), 2, 1)


def test_lz4_match_copy_handles_long_overlapping_run():
    output = bytearray(b"a")

    bookmark_tidy._copy_lz4_match(output, 1, 4096)

    assert output == b"a" * 4097


def test_lz4_decoder_rejects_truncated_literal_and_size_overflow():
    with pytest.raises(bookmark_tidy.UserError, match="truncated.*literal"):
        bookmark_tidy._decode_lz4_block(b"\x20a")
    with pytest.raises(bookmark_tidy.UserError, match="size limit"):
        bookmark_tidy._decode_lz4_block(b"\x20ab", max_output_size=1)
    with pytest.raises(bookmark_tidy.UserError, match="input exceeds"):
        bookmark_tidy._decode_lz4_block(b"\x00", max_input_size=0)


def test_lz4_match_copy_enforces_output_limit():
    with pytest.raises(bookmark_tidy.UserError, match="size limit"):
        bookmark_tidy._copy_lz4_match(
            bytearray(b"a"),
            1,
            10,
            output_limit=5,
        )


def test_detect_and_read_bookmark_formats(tmp_path):
    chrome = tmp_path / "chrome.json"
    firefox = tmp_path / "firefox.json"
    netscape = tmp_path / "bookmarks.html"
    bad_json = tmp_path / "bad.json"
    unknown_json = tmp_path / "unknown.json"
    unsupported = tmp_path / "notes.md"
    chrome.write_text('{"roots":{"bookmark_bar":{"children":[]}}}', encoding="utf-8")
    firefox.write_text('{"children":[]}', encoding="utf-8")
    netscape.write_text(_netscape_html(), encoding="utf-8")
    bad_json.write_text("{", encoding="utf-8")
    unknown_json.write_text('{"x":1}', encoding="utf-8")
    unsupported.write_text("plain", encoding="utf-8")

    assert bookmark_tidy._detect_bookmark_format_with_data(chrome)[0] == "chromium"
    assert bookmark_tidy._detect_bookmark_format_with_data(firefox)[0] == "firefox-json"
    assert bookmark_tidy._detect_bookmark_format_with_data(netscape)[0] == "netscape"
    assert bookmark_tidy.read_bookmark_file(netscape)[0].title == "Alpha"
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._detect_bookmark_format_with_data(bad_json)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._detect_bookmark_format_with_data(unknown_json)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._detect_bookmark_format_with_data(unsupported)


@pytest.mark.parametrize(
    "contents",
    [
        "https://one.example\nhttps://two.example/path\nmailto:user@example.test\n",
        "https://one.example https://two.example/path\tmailto:user@example.test",
    ],
)
def test_plain_text_url_lists_accept_newlines_and_spaces(tmp_path, contents):
    path = tmp_path / "urls.txt"
    path.write_text(contents, encoding="utf-8")

    assert bookmark_tidy._detect_bookmark_format_with_data(path)[0] == "plain-text"
    bookmarks = bookmark_tidy.read_bookmark_file(path)

    expected = [
        "https://one.example",
        "https://two.example/path",
        "mailto:user@example.test",
    ]
    assert [bookmark.url for bookmark in bookmarks] == expected
    assert [bookmark.title for bookmark in bookmarks] == expected
    assert all(bookmark.source == str(path) for bookmark in bookmarks)


@pytest.mark.parametrize(
    "contents, item",
    [
        ("", None),
        ("https://one.example not-a-url https://two.example", 2),
        ("https://", 1),
        ("https://example.test:bad-port", 1),
    ],
)
def test_plain_text_url_lists_reject_empty_or_invalid_input(tmp_path, contents, item):
    path = tmp_path / "urls.txt"
    path.write_text(contents, encoding="utf-8")

    match = "is empty" if item is None else f"non-URL token at item {item}"
    with pytest.raises(bookmark_tidy.UserError, match=match):
        bookmark_tidy.read_bookmark_file(path)


def test_read_bookmark_file_dispatches_every_format(monkeypatch, tmp_path):
    path = tmp_path / "bookmarks.any"
    path.write_text("", encoding="utf-8")
    readers = {
        "chromium": "chrome",
        "firefox-sqlite": "sqlite",
        "firefox-json": "json",
        "firefox-jsonlz4": "jsonlz4",
    }

    for fmt, marker in readers.items():
        monkeypatch.setattr(
            bookmark_tidy,
            "_detect_bookmark_format_with_data",
            lambda _path, value=fmt: (value, None),
        )
        monkeypatch.setattr(bookmark_tidy, "read_chromium_bookmarks", lambda _path: ["chrome"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_sqlite_bookmarks", lambda _path: ["sqlite"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_json_bookmarks", lambda _path: ["json"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_jsonlz4_bookmarks", lambda _path: ["jsonlz4"])

        assert bookmark_tidy.read_bookmark_file(path) == [marker]


def test_read_bookmark_file_parses_json_once(monkeypatch, tmp_path):
    path = tmp_path / "chrome.json"
    path.write_text('{"roots":{"bookmark_bar":{"children":[]}}}', encoding="utf-8")
    real_loads = bookmark_tidy.json.loads
    calls = []

    def counting_loads(text):
        calls.append(text)
        return real_loads(text)

    monkeypatch.setattr(bookmark_tidy.json, "loads", counting_loads)

    assert bookmark_tidy.read_bookmark_file(path) == []
    assert len(calls) == 1


def test_read_bookmark_file_reads_netscape_once(monkeypatch, tmp_path):
    path = tmp_path / "bookmarks.html"
    path.write_text(_netscape_html(), encoding="utf-8")
    monkeypatch.setattr(
        bookmark_tidy,
        "read_netscape_bookmarks",
        lambda _path: pytest.fail("netscape file re-read after sniff"),
    )

    assert bookmark_tidy.read_bookmark_file(path)[0].title == "Alpha"


def test_read_bookmark_file_parses_json_larger_than_sniff_window(tmp_path):
    path = tmp_path / "chrome.json"
    padding = "x" * (bookmark_tidy.FORMAT_SNIFF_CHARS * 2)
    path.write_text(
        '{"roots":{"bookmark_bar":{"children":[{"type":"url","url":"https://example.test/a","name":"A"}]}},'
        f'"pad":"{padding}"}}',
        encoding="utf-8",
    )

    assert bookmark_tidy._detect_bookmark_format_with_data(path)[0] == "chromium"
    bookmarks = bookmark_tidy.read_bookmark_file(path)
    assert [item.url for item in bookmarks] == ["https://example.test/a"]


def test_expand_inputs_and_discovery_helpers(tmp_path, monkeypatch):
    folder = tmp_path / "inputs"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    direct = folder / "Bookmarks"
    html = nested / "bookmarks.html"
    urls = nested / "urls.txt"
    ignored = nested / "ignored.md"
    direct.write_text('{"roots":{"bookmark_bar":{"children":[]}}}', encoding="utf-8")
    html.write_text(_netscape_html(), encoding="utf-8")
    urls.write_text("https://example.test", encoding="utf-8")
    ignored.write_text("x", encoding="utf-8")

    assert bookmark_tidy.expand_input_paths([str(folder)], recursive=False) == [direct.resolve()]
    assert bookmark_tidy.expand_input_paths([str(folder)], recursive=True) == [
        direct.resolve(), html.resolve(), urls.resolve()]
    assert bookmark_tidy._supported_input_file(folder / "places.sqlite")
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.expand_input_paths([str(folder / "missing")], recursive=True)

    monkeypatch.setattr(bookmark_tidy, "glob", lambda pattern: [str(direct)] if pattern.endswith("Bookmarks") else [])
    assert bookmark_tidy.discover_browser_bookmarks() == [direct.resolve()]
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert bookmark_tidy._windows_browser_patterns()
    assert bookmark_tidy._linux_browser_patterns(tmp_path)
    assert bookmark_tidy._mac_browser_patterns(tmp_path)


def test_explicit_unsupported_file_is_retained_for_content_sniff(tmp_path, caplog):
    path = tmp_path / "Bookmarks.bak"
    path.write_text("plain", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")
    paths = bookmark_tidy.expand_input_paths([str(path)], recursive=True)

    assert paths == [path.resolve()]
    assert bookmark_tidy.read_all_bookmarks(paths) == []
    assert "unsupported bookmark file" in caplog.text


def test_normalize_url_option_edges():
    options = bookmark_tidy.NormalizeOptions(
        strip_fragment=False,
        collapse_http_https=False,
        strip_trailing_slash=False,
        strip_default_port=False,
        strip_www=False,
        strip_tracking_params=False,
        lowercase_host=False,
    )

    key, display = bookmark_tidy.normalize_url("https://User:pw@Example.test:443/a/?utm_source=x#frag", options)

    assert key == display
    assert display == "https://User:pw@Example.test:443/a/?utm_source=x#frag"
    assert bookmark_tidy.normalize_url("/relative#x", bookmark_tidy.NormalizeOptions()) == ("/relative#x", "/relative#x")
    assert bookmark_tidy.normalize_url(
        "http://example.test:bad/a",
        bookmark_tidy.NormalizeOptions(),
    )[1] == "http://example.test:bad/a"
    assert bookmark_tidy.normalize_url("https://[::1]:443/a", bookmark_tidy.NormalizeOptions())[1] == "https://[::1]/a"


def test_invalid_port_does_not_collide_with_no_port_url():
    options = bookmark_tidy.NormalizeOptions()

    malformed = bookmark_tidy.normalize_url("http://example.test:bad/a", options)
    valid = bookmark_tidy.normalize_url("http://example.test/a", options)

    assert malformed[0] != valid[0]


def test_raw_host_part_handles_brackets_auth_and_ipv6_text():
    assert bookmark_tidy._raw_host_part(bookmark_tidy.urlsplit("https://user:pw@[::1]:443/a")) == "::1"
    assert bookmark_tidy._raw_host_part(types.SimpleNamespace(netloc="[broken")) == "[broken"
    assert bookmark_tidy._raw_host_part(bookmark_tidy.urlsplit("scheme://2001:db8::1/path")) == "2001:db8::1"


def test_tidy_bookmarks_requires_categorizer_for_mutable_bookmarks():
    bookmarks = [_sample_bookmark()]
    options = bookmark_tidy.NormalizeOptions()
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.tidy_bookmarks(
            bookmarks,
            immutable_roots=[],
            options=options,
            categorizer=None,
        )
    assert bookmark_tidy.tidy_bookmarks([], [], bookmark_tidy.NormalizeOptions(), None) == []


def test_tidy_bookmarks_falls_back_when_categorizer_fails(caplog):
    def failing_categorizer(bookmarks):
        raise bookmark_tidy.UserError("bad json")

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")

    tidied = bookmark_tidy.tidy_bookmarks(
        [_sample_bookmark()],
        immutable_roots=[],
        options=bookmark_tidy.NormalizeOptions(),
        categorizer=failing_categorizer,
        fallback_category="Fallback",
    )

    assert tidied[0].folder_path == ("Fallback",)
    assert "LLM categorization failed" in caplog.text


def test_assign_categories_stops_after_consecutive_failures(caplog):
    limit = bookmark_tidy.LLM_CONSECUTIVE_FAILURE_LIMIT
    calls = []

    def always_failing(batch):
        calls.append(len(batch))
        raise bookmark_tidy.UserError("LLM inference timed out")

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")
    bookmarks = [_sample_bookmark(f"https://example.test/{i}", f"B{i}") for i in range(limit + 2)]

    result = bookmark_tidy._assign_categories(bookmarks, always_failing, "Fallback", batch_size=1)

    assert len(calls) == limit
    assert len(result) == limit + 2
    assert all(item.folder_path == ("Fallback",) for item in result)
    assert "aborted after" in caplog.text


def test_assign_categories_failure_streak_resets_on_success():
    limit = bookmark_tidy.LLM_CONSECUTIVE_FAILURE_LIMIT
    calls = []

    def flaky(batch):
        calls.append(len(batch))
        if len(calls) % 2:
            raise bookmark_tidy.UserError("transient")
        return {0: ["Ok"]}

    bookmarks = [_sample_bookmark(f"https://example.test/{i}", f"B{i}") for i in range(2 * limit)]

    result = bookmark_tidy._assign_categories(bookmarks, flaky, "Fallback", batch_size=1)

    assert len(calls) == 2 * limit
    assert [item.folder_path for item in result] == [("Fallback",), ("Ok",)] * limit


def test_category_response_parsing_and_errors():
    assert bookmark_tidy.parse_category_response(
        'prefix {"items":[{"id":0,"category":"Dev/Docs"},{"id":9,"category":"Nope"}]} suffix',
        1,
    ) == {0: ("Dev", "Docs")}
    assert bookmark_tidy.parse_category_response('{"0":["A","B"],"bad":"C"}', 1) == {0: ("A", "B")}
    assert bookmark_tidy._category_path(None, "Fallback") == ("Fallback",)
    assert bookmark_tidy._category_path("A>B\\C", "Fallback") == ("A", "B", "C")
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.parse_category_response("no json", 1)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.parse_category_response("{bad", 1)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.parse_category_response("{bad}", 1)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.parse_category_response("[1]", 1)


def test_loads_json_object_rejects_non_mapping_response(monkeypatch):
    monkeypatch.setattr(bookmark_tidy.json, "loads", lambda _text: [])

    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._loads_json_object("{}")


@_forking_worker
def test_llama_categorizer_chat_and_text_paths(monkeypatch, tmp_path):
    _install_fake_llama(monkeypatch, FakeLlamaChat)
    chat = _fork_categorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    assert chat([_sample_bookmark()]) == {0: ("AI", "Docs")}
    chat.close()

    _install_fake_llama(monkeypatch, FakeLlamaText)
    text = _fork_categorizer(tmp_path / "model.gguf", False, 128, 1, 64)

    assert text([_sample_bookmark()]) == {0: ("Reference", "Docs")}
    text.close()


def test_complete_llama_supports_chat_and_text_models():
    assert "AI" in bookmark_tidy._complete_llama(FakeLlamaChat(), "prompt", 64)
    assert "Reference" in bookmark_tidy._complete_llama(FakeLlamaText(), "Categorize", 64)


@pytest.mark.parametrize(
    ("llama_cls", "expected_status"),
    [(FakeLlamaChat, "ok"), (BrokenInferenceLlama, "error")],
)
def test_llama_process_worker_reports_completion(monkeypatch, llama_cls, expected_status):
    connection = FakeLlamaConnection([("complete", "prompt"), ("close", "")])
    monkeypatch.setattr(bookmark_tidy, "_import_llama", lambda _auto_install: llama_cls)

    bookmark_tidy._llama_process_worker(connection, "model.gguf", False, 128, 2, 64)

    assert connection.sent[0] == ("ready", "")
    assert connection.sent[1][0] == expected_status
    assert connection.closed


def test_llama_process_worker_reports_startup_failure(monkeypatch):
    connection = FakeLlamaConnection()

    def fail_import(_auto_install):
        raise RuntimeError("missing runtime")

    monkeypatch.setattr(bookmark_tidy, "_import_llama", fail_import)

    bookmark_tidy._llama_process_worker(connection, "model.gguf", False, 128, 0, 64)

    assert connection.sent == [("startup_error", "missing runtime")]
    assert connection.closed


@_forking_worker
def test_llama_categorizer_wraps_model_load_failure(monkeypatch, tmp_path):
    class BrokenLlama:
        def __init__(self, **kwargs):
            raise ValueError("bad magic")

    _install_fake_llama(monkeypatch, BrokenLlama)

    with pytest.raises(bookmark_tidy.UserError, match="could not load model .*bad magic"):
        _fork_categorizer(tmp_path / "model.gguf", False, 128, 0, 64)


def test_llama_categorizer_wraps_worker_start_failure(monkeypatch, tmp_path):
    parent = FakeLlamaConnection()
    child = FakeLlamaConnection()
    process = types.SimpleNamespace(start=lambda: (_ for _ in ()).throw(OSError("no process")))
    context = types.SimpleNamespace(
        Pipe=lambda: (parent, child),
        Process=lambda **_kwargs: process,
    )
    monkeypatch.setattr(bookmark_tidy.multiprocessing, "get_context", lambda _name: context)

    with pytest.raises(bookmark_tidy.UserError, match="could not start .*no process"):
        bookmark_tidy.LlamaCategorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    assert parent.closed
    assert child.closed


def test_llama_categorizer_wraps_model_load_timeout(monkeypatch, tmp_path):
    parent = FakeLlamaConnection(poll_result=False)
    child = FakeLlamaConnection()
    process = types.SimpleNamespace(
        start=lambda: None,
        is_alive=lambda: False,
    )
    context = types.SimpleNamespace(
        Pipe=lambda: (parent, child),
        Process=lambda **_kwargs: process,
    )
    monkeypatch.setattr(bookmark_tidy.multiprocessing, "get_context", lambda _name: context)

    with pytest.raises(bookmark_tidy.UserError, match="timed out"):
        bookmark_tidy.LlamaCategorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    assert parent.closed
    assert child.closed


@_forking_worker
def test_llama_categorizer_inference_timeout(monkeypatch, tmp_path):
    _install_fake_llama(monkeypatch, HangingLlama)
    monkeypatch.setattr(bookmark_tidy, "LLAMA_INFERENCE_TIMEOUT_SECONDS", 0.01)
    chat = _fork_categorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    with pytest.raises(bookmark_tidy.UserError, match="LLM inference timed out"):
        chat._complete("prompt")
    assert not chat._process.is_alive()


@_forking_worker
def test_llama_categorizer_surfaces_worker_failure(monkeypatch, tmp_path):
    _install_fake_llama(monkeypatch, BrokenInferenceLlama)
    chat = _fork_categorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    with pytest.raises(bookmark_tidy.UserError, match="worker failed"):
        chat._complete("prompt")

    chat.close()


@pytest.mark.parametrize(
    ("connection", "message"),
    [
        (FakeLlamaConnection(send_error=BrokenPipeError()), "unavailable"),
        (FakeLlamaConnection(recv_error=EOFError()), "exited unexpectedly"),
    ],
)
def test_llama_categorizer_wraps_connection_failures(connection, message):
    chat = object.__new__(bookmark_tidy.LlamaCategorizer)
    chat._connection = connection
    chat._process = types.SimpleNamespace(is_alive=lambda: False)

    with pytest.raises(bookmark_tidy.UserError, match=message):
        chat._complete("prompt")


def test_llama_categorizer_abort_kills_stubborn_process():
    alive = iter([True, True])
    calls = []
    process = types.SimpleNamespace(
        is_alive=lambda: next(alive),
        terminate=lambda: calls.append("terminate"),
        kill=lambda: calls.append("kill"),
        join=lambda *args: calls.append(("join", args)),
    )
    connection = FakeLlamaConnection()
    chat = object.__new__(bookmark_tidy.LlamaCategorizer)
    chat._process = process
    chat._connection = connection

    chat._abort()

    assert calls == [
        "terminate",
        ("join", (bookmark_tidy.LLAMA_PROCESS_STOP_SECONDS,)),
        "kill",
        ("join", ()),
    ]
    assert connection.closed


def test_llama_categorizer_close_aborts_unresponsive_process():
    class StalledProcess:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, *_args):
            pass

        def terminate(self):
            self.alive = False

    connection = FakeLlamaConnection(send_error=BrokenPipeError())
    chat = object.__new__(bookmark_tidy.LlamaCategorizer)
    chat._process = StalledProcess()
    chat._connection = connection

    chat.close()

    assert connection.closed
    assert not chat._process.is_alive()


def test_import_llama_missing_and_auto_install(monkeypatch, caplog):
    real_import = builtins.__import__
    state = {"calls": 0}
    fake_module = types.ModuleType("llama_cpp")
    fake_module.Llama = FakeLlamaChat

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "llama_cpp":
            state["calls"] += 1
            if state["calls"] == 1:
                raise ImportError("missing")
            return fake_module
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._import_llama(False)

    state["calls"] = 0
    calls = []
    caplog.set_level(logging.WARNING, logger="bookmark-tidy")

    def record_install(cmd, timeout):
        assert "downloads and executes third-party package and build code" in caplog.text
        calls.append((cmd, timeout))

    monkeypatch.setattr(
        bookmark_tidy.subprocess,
        "check_call",
        record_install,
    )
    assert bookmark_tidy._import_llama(True) is FakeLlamaChat
    assert calls[0] == (
        [sys.executable, "-m", "pip", "install", bookmark_tidy.LLAMA_CPP_PYTHON_REQUIREMENT],
        bookmark_tidy.LLAMA_INSTALL_TIMEOUT_SECONDS,
    )


def test_import_llama_wraps_auto_install_failures(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "llama_cpp":
            raise ImportError("missing")
        return real_import(name, globals, locals, fromlist, level)

    def fail_install(_cmd, timeout):
        assert timeout == bookmark_tidy.LLAMA_INSTALL_TIMEOUT_SECONDS
        raise bookmark_tidy.subprocess.CalledProcessError(1, "pip")

    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(bookmark_tidy.subprocess, "check_call", fail_install)

    with pytest.raises(bookmark_tidy.UserError, match="failed to install"):
        bookmark_tidy._import_llama(True)


def test_import_llama_wraps_auto_install_timeout(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "llama_cpp":
            raise ImportError("missing")
        return real_import(name, globals, locals, fromlist, level)

    def timeout_install(_cmd, timeout):
        raise bookmark_tidy.subprocess.TimeoutExpired("pip", timeout)

    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(bookmark_tidy.subprocess, "check_call", timeout_install)

    with pytest.raises(bookmark_tidy.UserError, match="failed to install"):
        bookmark_tidy._import_llama(True)


def test_import_llama_wraps_auto_install_import_miss(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "llama_cpp":
            raise ImportError("missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "llama_cpp", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(
        bookmark_tidy.subprocess, "check_call", lambda _cmd, timeout: None)

    with pytest.raises(bookmark_tidy.UserError, match="still unavailable"):
        bookmark_tidy._import_llama(True)


def test_exports_and_write_output(tmp_path):
    bookmarks = [
        _sample_bookmark("https://example.test/a", "A", ("Folder",)),
        bookmark_tidy.Bookmark("https://example.test/menu", "M", ("Sub",), root="menu"),
    ]

    firefox = bookmark_tidy.export_firefox_bookmarks(bookmarks)
    netscape = bookmark_tidy.export_netscape_bookmarks(bookmarks)

    assert firefox["children"][0]["title"] == "Bookmarks Toolbar"
    assert "https://example.test/a" in netscape
    assert "Bookmarks Menu" in json.dumps(bookmark_tidy.export_chrome_bookmarks(bookmarks))
    assert bookmark_tidy.default_output_path("chrome").suffix == ".json"
    assert bookmark_tidy.default_output_path("netscape").suffix == ".html"

    chrome_path = tmp_path / "out.json"
    html_path = tmp_path / "out.html"
    bookmark_tidy.write_output(bookmarks, chrome_path, "chrome", force=False)
    bookmark_tidy.write_output(bookmarks, html_path, "netscape", force=False)
    assert json.loads(chrome_path.read_text(encoding="utf-8"))["version"] == 1
    assert html_path.read_text(encoding="utf-8").startswith("<!DOCTYPE NETSCAPE")
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.write_output(bookmarks, chrome_path, "firefox", force=False)
    bookmark_tidy.write_output(bookmarks, chrome_path, "firefox", force=True)
    assert json.loads(chrome_path.read_text(encoding="utf-8"))["root"] == "placesRoot"


def test_exports_share_folder_nodes_across_bookmarks():
    bookmarks = [
        _sample_bookmark(f"https://example.test/{i}", f"B{i}", ("Shared", "Deep"))
        for i in range(3)
    ]

    chrome_bar = bookmark_tidy.export_chrome_bookmarks(bookmarks)["roots"]["bookmark_bar"]
    firefox_bar = bookmark_tidy.export_firefox_bookmarks(bookmarks)["children"][0]

    assert [child["name"] for child in chrome_bar["children"]] == ["Shared"]
    assert len(chrome_bar["children"][0]["children"][0]["children"]) == 3
    assert [child["title"] for child in firefox_bar["children"]] == ["Shared"]
    assert len(firefox_bar["children"][0]["children"][0]["children"]) == 3


def test_firefox_folder_export_reuses_existing_folder_node():
    ids = bookmark_tidy._IdFactory()
    parent = {"children": [bookmark_tidy._firefox_folder_node(ids, "Existing")]}

    first = bookmark_tidy._child_firefox_folder(parent, "Existing", ids)
    second = bookmark_tidy._child_firefox_folder(parent, "New", ids)

    assert first is parent["children"][0]
    assert second["title"] == "New"
    assert len(parent["children"]) == 2


def test_atomic_write_fsyncs_parent_directory(tmp_path, monkeypatch):
    output = tmp_path / "out.txt"
    calls = []
    real_open = bookmark_tidy.os.open
    real_fsync = bookmark_tidy.os.fsync
    real_close = bookmark_tidy.os.close

    def tracking_open(path, flags, mode=0o777):
        fd = real_open(path, flags, mode)
        calls.append(("open", Path(path), flags, fd))
        return fd

    def tracking_fsync(fd):
        calls.append(("fsync", fd))
        return real_fsync(fd)

    def tracking_close(fd):
        calls.append(("close", fd))
        return real_close(fd)

    monkeypatch.setattr(bookmark_tidy.os, "open", tracking_open)
    monkeypatch.setattr(bookmark_tidy.os, "fsync", tracking_fsync)
    monkeypatch.setattr(bookmark_tidy.os, "close", tracking_close)

    bookmark_tidy._atomic_write_text(output, "data")

    opened_dir_fd = calls[-3][3]
    assert output.read_text(encoding="utf-8") == "data"
    assert calls[-3][0:2] == ("open", tmp_path)
    assert calls[-2] == ("fsync", opened_dir_fd)
    assert calls[-1] == ("close", opened_dir_fd)


def test_fsync_parent_dir_ignores_unsupported_directory_fsync(tmp_path, monkeypatch):
    monkeypatch.setattr(bookmark_tidy.os, "open", lambda path, flags: (_ for _ in ()).throw(OSError("no dir fsync")))

    bookmark_tidy._fsync_parent_dir(tmp_path / "out.txt")


def test_load_immutable_file_and_read_all_bookmarks(tmp_path, caplog):
    immutable = tmp_path / "immutable.txt"
    html = tmp_path / "bookmarks.html"
    unsupported = tmp_path / "notes.md"
    immutable.write_text("# comment\nWork\n\nPersonal\n", encoding="utf-8")
    html.write_text(_netscape_html(), encoding="utf-8")
    unsupported.write_text("plain", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")
    assert bookmark_tidy.load_immutable_file(str(immutable)) == ["Work", "Personal"]
    assert bookmark_tidy.load_immutable_file(None) == []
    assert len(bookmark_tidy.read_all_bookmarks([html, unsupported])) == 2
    assert "unsupported bookmark file" in caplog.text


def test_supported_input_file_matches_bare_names_case_insensitively(tmp_path):
    """bt-plat-02: on Windows/macOS the typed case need not match on disk, and
    Chrome's profile file has no suffix to fall back on."""
    for name in ("Bookmarks", "bookmarks", "BOOKMARKS",
                 "places.sqlite", "Places.sqlite", "PLACES.SQLITE"):
        assert bookmark_tidy._supported_input_file(tmp_path / name), name

    assert bookmark_tidy._supported_input_file(tmp_path / "notes.txt")
    assert bookmark_tidy._supported_input_file(tmp_path / "URLS.TXT")
    assert not bookmark_tidy._supported_input_file(tmp_path / "bookmarksbak")


def test_bom_prefixed_bookmark_files_are_still_recognised(tmp_path):
    """bt-plat-01: U+FEFF is not whitespace, so a surviving BOM would sit in
    front of the '<' / '{' the format sniff keys on."""
    html = tmp_path / "bookmarks.html"
    json_path = tmp_path / "Bookmarks"
    html.write_text(_netscape_html(), encoding="utf-8-sig")
    json_path.write_text(
        json.dumps({"roots": {"bookmark_bar": {
            "type": "folder", "name": "Bar", "children": [
                {"type": "url", "name": "Example", "url": "https://example.com/"}]}}}),
        encoding="utf-8-sig")

    assert len(bookmark_tidy.read_all_bookmarks([html])) == 2
    assert len(bookmark_tidy.read_all_bookmarks([json_path])) == 1


def test_load_immutable_file_missing_path_is_user_error(tmp_path):
    with pytest.raises(bookmark_tidy.UserError, match="could not read immutable file"):
        bookmark_tidy.load_immutable_file(str(tmp_path / "missing.txt"))


def test_read_all_bookmarks_skips_unexpected_parser_error(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "places.sqlite"
    good = tmp_path / "bookmarks.html"
    expected = [_sample_bookmark()]

    def fake_read(path):
        if path == bad:
            raise sqlite3.OperationalError("database is locked")
        return expected

    monkeypatch.setattr(bookmark_tidy, "read_bookmark_file", fake_read)
    caplog.set_level(logging.WARNING, logger="bookmark-tidy")

    assert bookmark_tidy.read_all_bookmarks([bad, good]) == expected
    assert "could not read bookmark file" in caplog.text
    assert "database is locked" in caplog.text


def test_parse_args_logging_and_cli_helpers(tmp_path, monkeypatch, capsys):
    html = tmp_path / "bookmarks.html"
    output = tmp_path / "out.json"
    html.write_text(_netscape_html(), encoding="utf-8")
    args = bookmark_tidy.parse_args(
        [
            str(html),
            "--output-format",
            "chrome",
            "--keep-fragments",
            "--keep-http-https-distinct",
            "--keep-trailing-slash",
            "--keep-default-port",
            "--keep-www",
            "--keep-tracking-params",
            "--preserve-url-host-case",
        ]
    )

    options = bookmark_tidy._normalization_from_args(args)

    assert not options.strip_fragment
    bookmark_tidy._configure_logging(0)
    bookmark_tidy._configure_logging(1)
    bookmark_tidy._configure_logging(2)
    assert bookmark_tidy._input_paths_from_args(args) == [html.resolve()]
    bookmarks = [_sample_bookmark()]
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._categorizer_from_args(args, bookmarks)

    class FakeCategorizer:
        def __init__(self, **kwargs):
            pass

        def __call__(self, bookmarks):
            return {index: ["Imported"] for index, _ in enumerate(bookmarks)}

    monkeypatch.setattr(bookmark_tidy, "LlamaCategorizer", FakeCategorizer)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    assert bookmark_tidy.main([str(html), "--model", str(model), "-o", str(output)]) == 0
    assert output.exists()
    assert bookmark_tidy.main([str(tmp_path / "missing.html")]) == 1
    assert "missing bookmark file" in capsys.readouterr().err


def test_main_logs_duplicate_summary(tmp_path, monkeypatch, caplog):
    html = tmp_path / "bookmarks.html"
    output = tmp_path / "out.json"
    html.write_text(
        """<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p>
        <DT><A HREF="http://www.example.test/a/#old">A</A>
        <DT><A HREF="https://example.test/a">Longer title</A>
        </DL><p>""",
        encoding="utf-8",
    )

    class FakeCategorizer:
        def __init__(self, **kwargs):
            pass

        def __call__(self, bookmarks):
            return {index: ["Imported"] for index, _ in enumerate(bookmarks)}

    monkeypatch.setattr(bookmark_tidy, "LlamaCategorizer", FakeCategorizer)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    caplog.set_level(logging.WARNING, logger="bookmark-tidy")

    assert bookmark_tidy.main([str(html), "--model", str(model), "-o", str(output)]) == 0
    assert "Merged/removed 1 duplicate bookmark(s)." in caplog.text


def test_run_uses_tidy_path_for_all_immutable_bookmarks(tmp_path, monkeypatch):
    output = tmp_path / "out.json"
    args = bookmark_tidy.parse_args(
        ["input.html", "--immutable-root", "Work", "-o", str(output)]
    )
    monkeypatch.setattr(
        bookmark_tidy, "_input_paths_from_args", lambda _args: [tmp_path / "input.html"]
    )
    monkeypatch.setattr(
        bookmark_tidy,
        "read_all_bookmarks",
        lambda _paths: [
            bookmark_tidy.Bookmark(
                "https://example.test", "Example", ("Work",)
            )
        ],
    )

    assert bookmark_tidy._run(args) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["roots"]["bookmark_bar"]["children"][0]["name"] == "Work"


def test_run_skips_model_load_when_every_bookmark_is_immutable(tmp_path, monkeypatch):
    """bt-perf-50: an all-immutable run must not pay a full GGUF load."""
    output = tmp_path / "out.json"
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    args = bookmark_tidy.parse_args(
        ["input.html", "--immutable-root", "Work", "--model", str(model),
         "-o", str(output)]
    )
    monkeypatch.setattr(
        bookmark_tidy, "_input_paths_from_args", lambda _args: [tmp_path / "input.html"]
    )
    monkeypatch.setattr(
        bookmark_tidy, "read_all_bookmarks",
        lambda _paths: [
            bookmark_tidy.Bookmark("https://example.test", "Example", ("Work",))
        ],
    )
    built = []
    monkeypatch.setattr(
        bookmark_tidy, "LlamaCategorizer",
        lambda **kwargs: built.append(kwargs) or (lambda _b: {}),
    )

    assert bookmark_tidy._run(args) == 0
    assert built == []


def test_lazy_categorizer_builds_once_and_closes(monkeypatch):
    calls = []

    class FakeProvider:
        def __init__(self):
            self.closed = False

        def __call__(self, bookmarks):
            calls.append(len(bookmarks))
            return {}

        def close(self):
            self.closed = True

    provider = FakeProvider()
    built = []

    def factory():
        built.append(1)
        return provider

    lazy = bookmark_tidy._LazyCategorizer(factory)
    assert built == []
    lazy([_sample_bookmark()])
    lazy([_sample_bookmark()])
    assert built == [1]
    assert calls == [1, 1]
    lazy.close()
    assert provider.closed
    # close() is idempotent and a never-loaded categorizer closes cleanly.
    lazy.close()
    bookmark_tidy._LazyCategorizer(factory).close()
    assert built == [1]


def test_lazy_categorizer_load_failure_is_fatal_not_a_fallback():
    """bt-perf-50: the deferred load must not be swallowed by the per-batch
    except in _categorize_batch."""
    def factory():
        raise bookmark_tidy.UserError("boom")

    lazy = bookmark_tidy._LazyCategorizer(factory)
    bookmarks = [_sample_bookmark()]
    with pytest.raises(bookmark_tidy.UserError, match="boom"):
        bookmark_tidy._assign_categories(bookmarks, lazy, "Uncategorized", 30)


def test_lazy_categorizer_rejects_a_none_factory_result():
    lazy = bookmark_tidy._LazyCategorizer(lambda: None)
    with pytest.raises(bookmark_tidy.UserError, match="missing --model"):
        lazy.ensure()


def test_ensure_categorizer_ready_ignores_plain_callables():
    bookmark_tidy._ensure_categorizer_ready(None)
    bookmark_tidy._ensure_categorizer_ready(lambda _b: {})


def test_parse_args_help_documents_llm_tuning_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        bookmark_tidy.parse_args(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "llama.cpp context size" in out
    assert "model layers to offload" in out
    assert "maximum tokens generated" in out
    assert "bookmarks sent to the model per categorization request" in out
    assert "Bookmark export format" in out
    assert "Keep URL fragments" in out
    assert "Treat HTTP and HTTPS" in out
    assert "Keep trailing URL" in out
    assert "Keep explicit default ports" in out
    assert "Keep a leading www" in out
    assert "Keep known URL tracking" in out
    assert "Preserve host letter case" in out
    assert "Category used when LLM" in out


@pytest.mark.parametrize("flag", ["--llm-context", "--llm-max-tokens", "--llm-batch-size"])
@pytest.mark.parametrize("value", ["0", "-1", "-5"])
def test_parse_args_rejects_non_positive_llm_values(capsys, flag, value):
    with pytest.raises(SystemExit) as exc:
        bookmark_tidy.parse_args([flag, value])

    assert exc.value.code == 2
    assert "must be a positive integer" in capsys.readouterr().err


def test_parse_args_gpu_layers_allows_llama_cpp_sentinels(capsys):
    assert bookmark_tidy.parse_args(["--llm-gpu-layers", "0"]).llm_gpu_layers == 0
    assert bookmark_tidy.parse_args(["--llm-gpu-layers", "-1"]).llm_gpu_layers == -1
    assert bookmark_tidy.parse_args(["--llm-gpu-layers", "8"]).llm_gpu_layers == 8
    with pytest.raises(SystemExit) as exc:
        bookmark_tidy.parse_args(["--llm-gpu-layers", "-2"])
    assert exc.value.code == 2
    assert "must be >= -1" in capsys.readouterr().err


def test_parse_args_accepts_positive_llm_values():
    args = bookmark_tidy.parse_args(
        ["--llm-context", "2048", "--llm-max-tokens", "256", "--llm-batch-size", "10"]
    )

    assert args.llm_context == 2048
    assert args.llm_max_tokens == 256
    assert args.llm_batch_size == 10


def test_categorizer_from_args_rejects_missing_model(tmp_path):
    args = bookmark_tidy.parse_args(["--model", str(tmp_path / "missing.gguf")])
    bookmarks = [_sample_bookmark()]

    with pytest.raises(bookmark_tidy.UserError, match="model file not found"):
        bookmark_tidy._categorizer_from_args(args, bookmarks)


def test_run_without_inputs_reports_error(monkeypatch):
    monkeypatch.setattr(bookmark_tidy, "discover_browser_bookmarks", lambda: [])
    args = bookmark_tidy.parse_args([])

    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._run(args)


def test_llm_batch_size_is_bookmarks_per_request_not_n_batch(monkeypatch):
    """bt-cli-50: the help said "llama.cpp prompt batch size", but the value is
    the number of bookmarks per categorization call and never reaches
    llama.cpp."""
    seen = []

    class RecordingCategorizer:
        def __call__(self, batch):
            seen.append(len(batch))
            return {index: ["Cat"] for index, _ in enumerate(batch)}

    bookmarks = [
        bookmark_tidy.Bookmark(title=f"b{i}", url=f"https://e.test/{i}")
        for i in range(7)
    ]

    bookmark_tidy._assign_categories(bookmarks, RecordingCategorizer(), "Fallback", 3)

    assert seen == [3, 3, 1]      # bookmarks per request, not tokens per batch


@pytest.mark.parametrize("url", [
    "javascript:document.querySelector('#target').click()",
    "data:text/html,<h1 id='target'>Title</h1>#target",
    "mailto:person@example.test?subject=Issue%20#42",
])
def test_opaque_bookmark_payload_survives_normalization(url):
    options = bookmark_tidy.NormalizeOptions()
    key, display = bookmark_tidy.normalize_url(url, options)
    assert key == display == url
    assert bookmark_tidy.normalize_url(display, options) == (key, display)


@pytest.mark.parametrize("query", [
    "token=%FF", "token=%FE", "q=a%20b&q=a+b", "q=%2f&q=%2F",
    "flag&empty=&repeat=1&repeat=2", "%FF=value&&tail=",
])
@pytest.mark.parametrize("strip_tracking", [False, True])
def test_query_normalization_preserves_retained_raw_fields(query, strip_tracking):
    options = bookmark_tidy.NormalizeOptions(strip_tracking_params=strip_tracking)
    raw = query + "&%75tm_source=campaign"
    expected = query if strip_tracking else raw
    assert bookmark_tidy._normalized_query(raw, options) == expected


def test_distinct_query_octets_remain_distinct_bookmarks():
    urls = ["https://example.test?token=%FF", "https://example.test?token=%FE"]
    immutable, mutable = bookmark_tidy.deduplicate_bookmarks(
        [_sample_bookmark(url) for url in urls], set(), bookmark_tidy.NormalizeOptions())
    assert immutable == []
    assert [bookmark.url for bookmark in mutable] == urls


@pytest.mark.parametrize("url,expected", [
    ("https://user:synthetic@[broken", "<redacted URL>"),
    ("https://[broken", "https://[broken"),
])
def test_malformed_url_redaction_never_exposes_credentials(url, expected):
    assert bookmark_tidy._redacted_url(url) == expected
