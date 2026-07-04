#!/usr/bin/env python3

import builtins
import importlib.util
import json
import logging
import sqlite3
import sys
import types
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("bookmark_tidy", REPO / "bookmark-tidy.py")
bookmark_tidy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bookmark_tidy)


def _tree(html):
    return bookmark_tidy.BeautifulSoup(html, "html.parser")


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


def _mozlz4_literal(raw):
    extra = len(raw) - 15
    payload = bytearray([0xF0 if extra >= 0 else len(raw) << 4])
    while extra >= 255:
        payload.append(255)
        extra -= 255
    if extra >= 0:
        payload.append(extra)
    payload.extend(raw)
    return bookmark_tidy.MOZLZ4_MAGIC + bytes(payload)


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


def _install_fake_llama(monkeypatch, llama_cls):
    module = types.ModuleType("llama_cpp")
    module.Llama = llama_cls
    monkeypatch.setitem(sys.modules, "llama_cpp", module)


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


def test_fallback_soup_extracts_links():
    tree = bookmark_tidy._FallbackSoup('<a href="https://example.test">Text</a>')

    links = tree.find_all("a")

    assert len(links) == 1
    assert links[0].get("href") == "https://example.test"
    assert links[0].text == "Text"
    assert tree.find_all("div") == []


def test_config_and_bookmark_read_helpers(tmp_path):
    config = tmp_path / "config.json"
    html = tmp_path / "bookmarks.html"
    config.write_text('{"options":["x"]}', encoding="utf-8")
    html.write_text('<a href="https://example.test">Example</a>', encoding="utf-8")

    assert bookmark_tidy.config_read(str(config)) == {"options": ["x"]}
    assert bookmark_tidy.links_extract(bookmark_tidy.bookmark_read(str(html)))[0].text == "Example"
    assert bookmark_tidy.get_current_unix_epoch() > 0


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
    assert bookmark_tidy.detect_bookmark_format(path) == "firefox-jsonlz4"
    assert bookmark_tidy._supported_input_file(path)


def test_read_firefox_jsonlz4_rejects_bad_container():
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_mozlz4(b"not-lz4")
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._decode_lz4_block(bytes([0x10, 0x01, 0x00]))


def test_lz4_match_copy_decodes_repeated_sequence():
    data = bytes([0x32]) + b"abc" + b"\x03\x00"

    assert bookmark_tidy._decode_lz4_block(data) == b"abcabcabc"

    output = bytearray(b"abc")
    bookmark_tidy._copy_lz4_match(output, 3, 6)
    assert bytes(output) == b"abcabcabc"

    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._copy_lz4_match(bytearray(b"a"), 2, 1)


def test_detect_and_read_bookmark_formats(tmp_path):
    chrome = tmp_path / "chrome.json"
    firefox = tmp_path / "firefox.json"
    netscape = tmp_path / "bookmarks.html"
    bad_json = tmp_path / "bad.json"
    unknown_json = tmp_path / "unknown.json"
    unsupported = tmp_path / "notes.txt"
    chrome.write_text('{"roots":{"bookmark_bar":{"children":[]}}}', encoding="utf-8")
    firefox.write_text('{"children":[]}', encoding="utf-8")
    netscape.write_text(_netscape_html(), encoding="utf-8")
    bad_json.write_text("{", encoding="utf-8")
    unknown_json.write_text('{"x":1}', encoding="utf-8")
    unsupported.write_text("plain", encoding="utf-8")

    assert bookmark_tidy.detect_bookmark_format(chrome) == "chromium"
    assert bookmark_tidy.detect_bookmark_format(firefox) == "firefox-json"
    assert bookmark_tidy.detect_bookmark_format(netscape) == "netscape"
    assert bookmark_tidy.read_bookmark_file(netscape)[0].title == "Alpha"
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.detect_bookmark_format(bad_json)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.detect_bookmark_format(unknown_json)
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.detect_bookmark_format(unsupported)


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
        monkeypatch.setattr(bookmark_tidy, "detect_bookmark_format", lambda _path, value=fmt: value)
        monkeypatch.setattr(bookmark_tidy, "read_chromium_bookmarks", lambda _path: ["chrome"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_sqlite_bookmarks", lambda _path: ["sqlite"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_json_bookmarks", lambda _path: ["json"])
        monkeypatch.setattr(bookmark_tidy, "read_firefox_jsonlz4_bookmarks", lambda _path: ["jsonlz4"])

        assert bookmark_tidy.read_bookmark_file(path) == [marker]


def test_expand_inputs_and_discovery_helpers(tmp_path, monkeypatch):
    folder = tmp_path / "inputs"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    direct = folder / "Bookmarks"
    html = nested / "bookmarks.html"
    ignored = nested / "ignored.txt"
    direct.write_text('{"roots":{"bookmark_bar":{"children":[]}}}', encoding="utf-8")
    html.write_text(_netscape_html(), encoding="utf-8")
    ignored.write_text("x", encoding="utf-8")

    assert bookmark_tidy.expand_input_paths([str(folder)], recursive=False) == [direct.resolve()]
    assert bookmark_tidy.expand_input_paths([str(folder)], recursive=True) == [direct.resolve(), html.resolve()]
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
    assert bookmark_tidy.normalize_url("/relative#x", bookmark_tidy.NormalizeOptions()) == ("/relative", "/relative")
    assert bookmark_tidy.normalize_url("http://example.test:bad/a", bookmark_tidy.NormalizeOptions())[1] == "http://example.test/a"
    assert bookmark_tidy.normalize_url("https://[::1]:443/a", bookmark_tidy.NormalizeOptions())[1] == "https://[::1]/a"


def test_raw_host_part_handles_brackets_auth_and_ipv6_text():
    assert bookmark_tidy._raw_host_part(bookmark_tidy.urlsplit("https://user:pw@[::1]:443/a")) == "::1"
    assert bookmark_tidy._raw_host_part(types.SimpleNamespace(netloc="[broken")) == "[broken"
    assert bookmark_tidy._raw_host_part(bookmark_tidy.urlsplit("scheme://2001:db8::1/path")) == "2001:db8::1"


def test_tidy_bookmarks_requires_categorizer_for_mutable_bookmarks():
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy.tidy_bookmarks(
            [_sample_bookmark()],
            immutable_roots=[],
            options=bookmark_tidy.NormalizeOptions(),
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


def test_llama_categorizer_chat_and_text_paths(monkeypatch, tmp_path):
    _install_fake_llama(monkeypatch, FakeLlamaChat)
    chat = bookmark_tidy.LlamaCategorizer(tmp_path / "model.gguf", False, 128, 0, 64)

    assert chat([_sample_bookmark()]) == {0: ("AI", "Docs")}
    assert FakeLlamaChat.kwargs["model_path"].endswith("model.gguf")

    _install_fake_llama(monkeypatch, FakeLlamaText)
    text = bookmark_tidy.LlamaCategorizer(tmp_path / "model.gguf", False, 128, 1, 64)

    assert text([_sample_bookmark()]) == {0: ("Reference", "Docs")}
    assert FakeLlamaText.kwargs["n_gpu_layers"] == 1


def test_import_llama_missing_and_auto_install(monkeypatch):
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
    monkeypatch.setattr(bookmark_tidy.subprocess, "check_call", lambda cmd: calls.append(cmd))
    assert bookmark_tidy._import_llama(True) is FakeLlamaChat
    assert calls[0] == [sys.executable, "-m", "pip", "install", bookmark_tidy.LLAMA_CPP_PYTHON_REQUIREMENT]


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
    unsupported = tmp_path / "notes.txt"
    immutable.write_text("# comment\nWork\n\nPersonal\n", encoding="utf-8")
    html.write_text(_netscape_html(), encoding="utf-8")
    unsupported.write_text("plain", encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="bookmark-tidy")
    assert bookmark_tidy.load_immutable_file(str(immutable)) == ["Work", "Personal"]
    assert bookmark_tidy.load_immutable_file(None) == []
    assert len(bookmark_tidy.read_all_bookmarks([html, unsupported])) == 2
    assert "unsupported bookmark file" in caplog.text


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
    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._categorizer_from_args(args, [_sample_bookmark()])

    class FakeCategorizer:
        def __init__(self, **kwargs):
            pass

        def __call__(self, bookmarks):
            return {index: ["Imported"] for index, _ in enumerate(bookmarks)}

    monkeypatch.setattr(bookmark_tidy, "LlamaCategorizer", FakeCategorizer)
    assert bookmark_tidy.main([str(html), "--model", str(tmp_path / "model.gguf"), "-o", str(output)]) == 0
    assert output.exists()
    assert bookmark_tidy.main([str(tmp_path / "missing.html")]) == 1
    assert "missing bookmark file" in capsys.readouterr().err


def test_run_without_inputs_reports_error(monkeypatch):
    monkeypatch.setattr(bookmark_tidy, "discover_browser_bookmarks", lambda: [])

    with pytest.raises(bookmark_tidy.UserError):
        bookmark_tidy._run(bookmark_tidy.parse_args([]))
