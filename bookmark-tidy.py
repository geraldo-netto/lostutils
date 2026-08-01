#!/usr/bin/env python3
"""Merge, deduplicate, and reorganize browser bookmarks with llama.cpp."""

import argparse
import json
import logging
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from glob import glob
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LOGGER = logging.getLogger("bookmark-tidy")
WEBKIT_EPOCH_OFFSET_SECONDS = 11644473600
DEFAULT_FALLBACK_CATEGORY = "Uncategorized"
DEFAULT_LLM_BATCH_SIZE = 30
DEFAULT_LLM_CONTEXT = 4096
DEFAULT_LLM_MAX_TOKENS = 1024
LLAMA_CPP_PYTHON_REQUIREMENT = "llama-cpp-python==0.3.32"
LLAMA_INSTALL_TIMEOUT_SECONDS = 300
LLAMA_INFERENCE_TIMEOUT_SECONDS = 120
LLAMA_MODEL_LOAD_TIMEOUT_SECONDS = 300
LLAMA_PROCESS_STOP_SECONDS = 1
LLM_CONSECUTIVE_FAILURE_LIMIT = 3
FORMAT_SNIFF_CHARS = 4096
MOZLZ4_MAGIC = b"mozLz40\x00"
MAX_LZ4_INPUT_BYTES = 64 * 1024 * 1024
MAX_LZ4_OUTPUT_BYTES = 256 * 1024 * 1024
TRACKING_PARAM_NAMES = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "oly_anon_id",
        "oly_enc_id",
        "vero_conv",
        "vero_id",
        "yclid",
    }
)
ROOT_DISPLAY = {
    "bookmark_bar": "Bookmarks Bar",
    "menu": "Bookmarks Menu",
    "other": "Other Bookmarks",
    "synced": "Mobile Bookmarks",
}
CHROME_ROOT_NAMES = {
    "bookmark_bar": "Bookmarks bar",
    "other": "Other bookmarks",
    "synced": "Mobile bookmarks",
}
FIREFOX_ROOT_GUIDS = {
    "bookmark_bar": "toolbar_____",
    "menu": "menu________",
    "other": "unfiled_____",
    "synced": "mobile______",
}
ROOT_ALIASES = {
    "bookmarks bar": "bookmark_bar",
    "bookmarks toolbar": "bookmark_bar",
    "personal toolbar": "bookmark_bar",
    "bookmarks menu": "menu",
    "other bookmarks": "other",
    "unfiled bookmarks": "other",
    "mobile bookmarks": "synced",
}


@dataclass
class Bookmark:
    url: str
    title: str
    folder_path: tuple[str, ...] = ()
    root: str = "bookmark_bar"
    add_date: int | None = None
    last_modified: int | None = None
    source: str = ""
    immutable: bool = False


@dataclass(frozen=True)
class NormalizeOptions:
    strip_fragment: bool = True
    collapse_http_https: bool = True
    strip_trailing_slash: bool = True
    strip_default_port: bool = True
    strip_www: bool = True
    strip_tracking_params: bool = True
    lowercase_host: bool = True


class UserError(Exception):
    pass


def _clean_folder_part(value: Any) -> str:
    return " ".join(str(value).strip().split())


def _folder_match_key(value: str) -> str:
    return _clean_folder_part(value).casefold()


def _root_from_name(name: str) -> str | None:
    return ROOT_ALIASES.get(_folder_match_key(name))


def _root_display(root: str) -> str:
    return ROOT_DISPLAY.get(root, ROOT_DISPLAY["bookmark_bar"])


def _split_root_path(parts: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    if not parts:
        return "bookmark_bar", ()
    root = _root_from_name(parts[0])
    if root is None:
        return "bookmark_bar", tuple(parts)
    return root, tuple(parts[1:])


def _bookmark_title(title: str, url: str) -> str:
    clean = _clean_folder_part(title)
    return clean or url


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _chrome_time_to_unix(value: Any) -> int | None:
    raw = _safe_int(value)
    if raw is None or raw <= 0:
        return None
    return max(0, int(raw / 1_000_000 - WEBKIT_EPOCH_OFFSET_SECONDS))


def _unix_to_chrome_time(value: int | None) -> str:
    seconds = int(value if value is not None else time.time())
    return str((seconds + WEBKIT_EPOCH_OFFSET_SECONDS) * 1_000_000)


def _firefox_time_to_unix(value: Any) -> int | None:
    raw = _safe_int(value)
    if raw is None or raw <= 0:
        return None
    return int(raw / 1_000_000)


def _unix_to_firefox_time(value: int | None) -> int:
    seconds = int(value if value is not None else time.time())
    return seconds * 1_000_000


def _attrs_to_dict(attrs: Iterable[tuple[str, str | None]]) -> dict[str, str]:
    return {key.casefold(): value or "" for key, value in attrs}


class NetscapeBookmarkParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.bookmarks: list[Bookmark] = []
        self._source = source
        self._folders: list[str] = []
        self._pending_folder: str | None = None
        self._pending_folder_text: list[str] = []
        self._active_link_attrs: dict[str, str] | None = None
        self._active_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "h3":
            self._pending_folder = ""
            self._pending_folder_text = []
        elif tag == "a":
            self._active_link_attrs = _attrs_to_dict(attrs)
            self._active_link_text = []
        elif tag == "dl" and self._pending_folder is not None:
            self._folders.append(_clean_folder_part("".join(self._pending_folder_text)))
            self._pending_folder = None
            self._pending_folder_text = []

    def handle_data(self, data: str) -> None:
        if self._pending_folder is not None:
            self._pending_folder_text.append(data)
        if self._active_link_attrs is not None:
            self._active_link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a":
            self._close_link()
        elif tag == "h3" and self._pending_folder is not None:
            self._pending_folder = _clean_folder_part("".join(self._pending_folder_text))
        elif tag == "dl" and self._folders:
            self._folders.pop()

    def _close_link(self) -> None:
        if self._active_link_attrs is None:
            return
        href = self._active_link_attrs.get("href", "").strip()
        if href:
            self.bookmarks.append(self._make_bookmark(href))
        self._active_link_attrs = None
        self._active_link_text = []

    def _make_bookmark(self, href: str) -> Bookmark:
        attrs = self._active_link_attrs or {}
        root, folder_path = _split_root_path(self._folders)
        return Bookmark(
            url=href,
            title=_bookmark_title("".join(self._active_link_text), href),
            folder_path=folder_path,
            root=root,
            add_date=_safe_int(attrs.get("add_date")),
            last_modified=_safe_int(attrs.get("last_modified")),
            source=self._source,
        )


def _read_utf8_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UserError(f"bookmark file is not valid UTF-8: {path}") from exc


def read_netscape_bookmarks(path: Path) -> list[Bookmark]:
    return netscape_html_to_bookmarks(_read_utf8_text(path), str(path))


def netscape_html_to_bookmarks(text: str, source: str) -> list[Bookmark]:
    parser = NetscapeBookmarkParser(source)
    parser.feed(text)
    return parser.bookmarks


def read_chromium_bookmarks(path: Path) -> list[Bookmark]:
    return chromium_json_data_to_bookmarks(_load_json_bookmark(path), str(path))


def chromium_json_data_to_bookmarks(data: Any, source: str) -> list[Bookmark]:
    roots = data.get("roots", {})
    bookmarks: list[Bookmark] = []
    for root_key, node in roots.items():
        root = root_key if root_key in ROOT_DISPLAY else "other"
        for child in node.get("children", []):
            if isinstance(child, Mapping):
                _walk_chromium_node(child, root, (), source, bookmarks)
    return bookmarks


def _walk_chromium_node(
    node: Mapping[str, Any],
    root: str,
    folders: tuple[str, ...],
    source: str,
    out: list[Bookmark],
) -> None:
    node_type = str(node.get("type", "folder"))
    if node_type == "url":
        url = str(node.get("url", "")).strip()
        if url:
            out.append(_chromium_bookmark(node, root, folders, source, url))
        return
    name = _clean_folder_part(node.get("name", ""))
    next_folders = folders + (name,) if name else folders
    for child in node.get("children", []):
        if isinstance(child, Mapping):
            _walk_chromium_node(child, root, next_folders, source, out)


def _chromium_bookmark(
    node: Mapping[str, Any],
    root: str,
    folders: tuple[str, ...],
    source: str,
    url: str,
) -> Bookmark:
    return Bookmark(
        url=url,
        title=_bookmark_title(str(node.get("name", "")), url),
        folder_path=folders,
        root=root,
        add_date=_chrome_time_to_unix(node.get("date_added")),
        last_modified=_chrome_time_to_unix(node.get("date_last_used")),
        source=source,
    )


def read_firefox_sqlite_bookmarks(path: Path) -> list[Bookmark]:
    with tempfile.TemporaryDirectory(prefix="bookmark-tidy-firefox-") as tmp_dir:
        snapshot = Path(tmp_dir) / "places.sqlite"
        _backup_sqlite_database(path, snapshot)
        return _read_firefox_sqlite_copy(snapshot, str(path))


def _sqlite_ro_uri(path: Path) -> str:
    # Build the read-only URI via Path.as_uri() so Windows paths (C:\...) and
    # paths containing ?, #, or % are percent-encoded correctly; raw f-string
    # interpolation would misparse them and silently open the wrong file or
    # fail (bt-robust-01). as_uri() requires an absolute path.
    return f"{path.resolve().as_uri()}?mode=ro"


def _backup_sqlite_database(source: Path, target: Path) -> None:
    src = sqlite3.connect(_sqlite_ro_uri(source), uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _read_firefox_sqlite_copy(path: Path, source: str) -> list[Bookmark]:
    conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
    try:
        rows = conn.execute(_firefox_places_query()).fetchall()
    finally:
        conn.close()
    return _firefox_rows_to_bookmarks(rows, source)


def _firefox_places_query() -> str:
    return """
        SELECT b.id, b.type, b.parent, b.title, b.dateAdded, b.lastModified,
               b.guid, p.url, p.title
          FROM moz_bookmarks AS b
          LEFT JOIN moz_places AS p ON b.fk = p.id
         ORDER BY b.position, b.id
    """


def _firefox_rows_to_bookmarks(rows: Sequence[tuple[Any, ...]], source: str) -> list[Bookmark]:
    nodes = {int(row[0]): row for row in rows}
    children = _firefox_children_by_parent(rows)
    roots = _firefox_root_ids(rows)
    bookmarks: list[Bookmark] = []
    for root_id, root in roots.items():
        _walk_firefox_rows(root_id, root, (), nodes, children, source, bookmarks)
    return bookmarks


def _firefox_children_by_parent(rows: Sequence[tuple[Any, ...]]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for row in rows:
        parent = _safe_int(row[2])
        if parent is not None:
            children.setdefault(parent, []).append(int(row[0]))
    return children


def _firefox_root_ids(rows: Sequence[tuple[Any, ...]]) -> dict[int, str]:
    result = {}
    guid_map = {value: key for key, value in FIREFOX_ROOT_GUIDS.items()}
    for row in rows:
        root = guid_map.get(str(row[6]))
        if root is not None:
            result[int(row[0])] = root
    return result


def _walk_firefox_rows(
    node_id: int,
    root: str,
    folders: tuple[str, ...],
    nodes: Mapping[int, tuple[Any, ...]],
    children: Mapping[int, list[int]],
    source: str,
    out: list[Bookmark],
    visited: frozenset[int] | None = None,
) -> None:
    path_seen = (visited or frozenset()) | {node_id}
    for child_id in children.get(node_id, []):
        if child_id in path_seen:
            LOGGER.warning("skipping cyclic Firefox bookmark folder edge in %s: %s -> %s", source, node_id, child_id)
            continue
        row = nodes[child_id]
        if int(row[1]) == 1 and row[7]:
            out.append(_firefox_bookmark(row, root, folders, source))
        elif int(row[1]) == 2:
            name = _clean_folder_part(row[3] or "")
            _walk_firefox_rows(child_id, root, folders + (name,), nodes, children, source, out, path_seen)


def _firefox_bookmark(
    row: tuple[Any, ...],
    root: str,
    folders: tuple[str, ...],
    source: str,
) -> Bookmark:
    url = str(row[7])
    return Bookmark(
        url=url,
        title=_bookmark_title(str(row[3] or row[8] or ""), url),
        folder_path=folders,
        root=root,
        add_date=_firefox_time_to_unix(row[4]),
        last_modified=_firefox_time_to_unix(row[5]),
        source=source,
    )


def read_firefox_json_bookmarks(path: Path) -> list[Bookmark]:
    return firefox_json_data_to_bookmarks(_load_json_bookmark(path), str(path))


def read_firefox_jsonlz4_bookmarks(path: Path) -> list[Bookmark]:
    data = json.loads(_decode_mozlz4(path.read_bytes()).decode("utf-8"))
    return firefox_json_data_to_bookmarks(data, str(path))


def firefox_json_data_to_bookmarks(data: Any, source: str) -> list[Bookmark]:
    if not isinstance(data, Mapping):
        raise UserError(f"Firefox JSON bookmark backup must be an object: {source}")
    bookmarks: list[Bookmark] = []
    _walk_firefox_json(data, "bookmark_bar", (), source, bookmarks)
    return bookmarks


def _decode_mozlz4(data: bytes) -> bytes:
    if not data.startswith(MOZLZ4_MAGIC):
        raise UserError("Firefox jsonlz4 backup has an invalid mozLz4 header")
    return _decode_lz4_block(data[len(MOZLZ4_MAGIC):])


def _decode_lz4_block(
    data: bytes,
    *,
    max_input_size: int | None = None,
    max_output_size: int | None = None,
) -> bytes:
    input_limit = MAX_LZ4_INPUT_BYTES if max_input_size is None else max_input_size
    output_limit = MAX_LZ4_OUTPUT_BYTES if max_output_size is None else max_output_size
    if len(data) > input_limit:
        raise UserError("LZ4 input exceeds the decoded bookmark size limit")
    output = bytearray()
    index = 0
    while index < len(data):
        token = data[index]
        index += 1
        literal_len, index = _lz4_length(data, index, token >> 4)
        index = _copy_lz4_literals(data, index, literal_len, output, output_limit)
        if index >= len(data):
            break
        if index + 2 > len(data):
            raise UserError("truncated LZ4 match offset")
        offset = data[index] | (data[index + 1] << 8)
        index += 2
        match_len, index = _lz4_length(data, index, token & 0x0F)
        _copy_lz4_match(output, offset, match_len + 4, output_limit)
    return bytes(output)


def _copy_lz4_literals(
    data: bytes,
    index: int,
    length: int,
    output: bytearray,
    output_limit: int,
) -> int:
    literal_end = index + length
    if literal_end > len(data):
        raise UserError("truncated LZ4 literal run")
    if len(output) + length > output_limit:
        raise UserError("decoded LZ4 bookmark exceeds the size limit")
    output.extend(data[index:literal_end])
    return literal_end


def _lz4_length(data: bytes, index: int, nibble: int) -> tuple[int, int]:
    total = nibble
    if total != 15:
        return total, index
    while index < len(data):
        value = data[index]
        index += 1
        total += value
        if value != 255:
            return total, index
    raise UserError("truncated LZ4 length")


def _copy_lz4_match(
    output: bytearray,
    offset: int,
    length: int,
    output_limit: int = MAX_LZ4_OUTPUT_BYTES,
) -> None:
    if offset <= 0 or offset > len(output):
        raise UserError("invalid LZ4 match offset")
    if length > output_limit - len(output):
        raise UserError("decoded LZ4 bookmark exceeds the size limit")
    start = len(output) - offset
    while length > 0:
        available = len(output) - start
        take = min(length, available)
        output.extend(output[start:start + take])
        length -= take


def _walk_firefox_json(
    node: Mapping[str, Any],
    root: str,
    folders: tuple[str, ...],
    source: str,
    out: list[Bookmark],
) -> None:
    node_root = _firefox_json_root(node, root)
    if _is_firefox_json_bookmark(node):
        out.append(_firefox_json_bookmark(node, node_root, folders, source))
        return
    title = _clean_folder_part(node.get("title", ""))
    next_folders = folders + (title,) if title and node.get("root") is None else folders
    for child in node.get("children", []):
        if isinstance(child, Mapping):
            _walk_firefox_json(child, node_root, next_folders, source, out)


def _firefox_json_root(node: Mapping[str, Any], current: str) -> str:
    root_value = str(node.get("root", ""))
    if root_value == "toolbarFolder":
        return "bookmark_bar"
    if root_value == "bookmarksMenuFolder":
        return "menu"
    if root_value == "unfiledBookmarksFolder":
        return "other"
    if root_value == "mobileFolder":
        return "synced"
    return current


def _is_firefox_json_bookmark(node: Mapping[str, Any]) -> bool:
    return bool(node.get("uri")) or node.get("typeCode") == 1


def _firefox_json_bookmark(
    node: Mapping[str, Any],
    root: str,
    folders: tuple[str, ...],
    source: str,
) -> Bookmark:
    url = str(node.get("uri", ""))
    return Bookmark(
        url=url,
        title=_bookmark_title(str(node.get("title", "")), url),
        folder_path=folders,
        root=root,
        add_date=_firefox_time_to_unix(node.get("dateAdded")),
        last_modified=_firefox_time_to_unix(node.get("lastModified")),
        source=source,
    )


def _detect_bookmark_format_with_data(path: Path) -> tuple[str, Any | None]:
    if path.suffix.casefold() == ".jsonlz4":
        return "firefox-jsonlz4", None
    if path.name == "places.sqlite" or path.suffix.casefold() in {".sqlite", ".sqlite3"}:
        return "firefox-sqlite", None
    text = _read_utf8_text(path)
    stripped = text[:FORMAT_SNIFF_CHARS].lstrip()
    if stripped.startswith("<"):
        return "netscape", text
    if stripped.startswith("{"):
        data = _parse_json_bookmark_text(text, path)
        return _json_bookmark_format(data, path), data
    raise UserError(f"unsupported bookmark file: {path}")


def _load_json_bookmark(path: Path) -> Any:
    return _parse_json_bookmark_text(_read_utf8_text(path), path)


def _parse_json_bookmark_text(text: str, path: Path) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UserError(f"invalid JSON bookmark file {path}: {exc}") from exc


def _json_bookmark_format(data: Any, path: Path) -> str:
    if isinstance(data, Mapping) and "roots" in data:
        return "chromium"
    if isinstance(data, Mapping) and ("children" in data or "typeCode" in data):
        return "firefox-json"
    raise UserError(f"unrecognized JSON bookmark format: {path}")


def read_bookmark_file(path: Path) -> list[Bookmark]:
    fmt, data = _detect_bookmark_format_with_data(path)
    if fmt == "chromium":
        if data is not None:
            return chromium_json_data_to_bookmarks(data, str(path))
        return read_chromium_bookmarks(path)
    if fmt == "firefox-sqlite":
        return read_firefox_sqlite_bookmarks(path)
    if fmt == "firefox-json":
        if data is not None:
            return firefox_json_data_to_bookmarks(data, str(path))
        return read_firefox_json_bookmarks(path)
    if fmt == "firefox-jsonlz4":
        return read_firefox_jsonlz4_bookmarks(path)
    if isinstance(data, str):
        return netscape_html_to_bookmarks(data, str(path))
    return read_netscape_bookmarks(path)


def _supported_input_file(path: Path) -> bool:
    suffix = path.suffix.casefold()
    return path.name == "Bookmarks" or path.name == "places.sqlite" or suffix in {
        ".htm",
        ".html",
        ".json",
        ".jsonlz4",
        ".sqlite",
        ".sqlite3",
    }


def expand_input_paths(inputs: Sequence[str], recursive: bool) -> list[Path]:
    result: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if not path.exists():
            raise UserError(f"missing bookmark file: {path}")
        result.extend(_expand_one_input(path, recursive))
    return _unique_paths(result)


def _expand_one_input(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path] if _supported_input_file(path) else []
    iterator = path.rglob("*") if recursive else path.glob("*")
    return [candidate for candidate in iterator if candidate.is_file() and _supported_input_file(candidate)]


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            result.append(resolved)
            seen.add(resolved)
    return result


def discover_browser_bookmarks() -> list[Path]:
    home = Path.home()
    patterns = _linux_browser_patterns(home) + _mac_browser_patterns(home) + _windows_browser_patterns()
    return _unique_paths(Path(match) for pattern in patterns for match in glob(str(pattern)))


def _linux_browser_patterns(home: Path) -> list[Path]:
    return [
        home / ".config" / "google-chrome" / "*" / "Bookmarks",
        home / ".config" / "chromium" / "*" / "Bookmarks",
        home / ".config" / "microsoft-edge" / "*" / "Bookmarks",
        home / ".config" / "microsoft-edge-beta" / "*" / "Bookmarks",
        home / ".mozilla" / "firefox" / "*" / "places.sqlite",
    ]


def _mac_browser_patterns(home: Path) -> list[Path]:
    base = home / "Library" / "Application Support"
    return [
        base / "Google" / "Chrome" / "*" / "Bookmarks",
        base / "Chromium" / "*" / "Bookmarks",
        base / "Microsoft Edge" / "*" / "Bookmarks",
        base / "Firefox" / "Profiles" / "*" / "places.sqlite",
    ]


def _windows_browser_patterns() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA")
    roaming = os.environ.get("APPDATA")
    patterns: list[Path] = []
    if local:
        base = Path(local)
        patterns.extend(
            [
                base / "Google" / "Chrome" / "User Data" / "*" / "Bookmarks",
                base / "Microsoft" / "Edge" / "User Data" / "*" / "Bookmarks",
            ]
        )
    if roaming:
        patterns.append(Path(roaming) / "Mozilla" / "Firefox" / "Profiles" / "*" / "places.sqlite")
    return patterns


def normalize_url(url: str, options: NormalizeOptions) -> tuple[str, str]:
    raw = url.strip()
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError:
        return _normalize_opaque_url(raw, options)
    if not parsed.scheme or not parsed.netloc:
        return _normalize_opaque_url(raw, options)
    display = _normalized_display_url(parsed, options)
    key = _normalized_key_url(display, options)
    return key, display


def _normalize_opaque_url(url: str, options: NormalizeOptions) -> tuple[str, str]:
    clean = url.split("#", 1)[0] if options.strip_fragment else url
    return clean, clean


def _normalized_display_url(parsed: Any, options: NormalizeOptions) -> str:
    scheme = parsed.scheme.casefold()
    netloc = _normalized_netloc(parsed, options)
    path = _normalized_path(parsed.path, options)
    query = _normalized_query(parsed.query, options)
    fragment = "" if options.strip_fragment else parsed.fragment
    return urlunsplit((scheme, netloc, path, query, fragment))


def _normalized_key_url(display_url: str, options: NormalizeOptions) -> str:
    parsed = urlsplit(display_url)
    scheme = "web" if options.collapse_http_https and parsed.scheme in {"http", "https"} else parsed.scheme
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _normalized_netloc(parsed: Any, options: NormalizeOptions) -> str:
    host = parsed.hostname or ""
    if not options.lowercase_host:
        host = _raw_host_part(parsed) or host
    host = host.casefold() if options.lowercase_host else host
    host = host[4:] if options.strip_www and host.startswith("www.") else host
    host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
    port = _normalized_port(parsed, options)
    auth = _url_auth_part(parsed)
    return f"{auth}{host_part}{port}"


def _raw_host_part(parsed: Any) -> str:
    host_port = parsed.netloc.rsplit("@", 1)[-1]
    if host_port.startswith("["):
        end = host_port.find("]")
        return host_port[1:end] if end != -1 else host_port
    if host_port.count(":") == 1:
        return host_port.rsplit(":", 1)[0]
    return host_port


def _normalized_port(parsed: Any, options: NormalizeOptions) -> str:
    port = parsed.port
    if port is None:
        return ""
    if options.strip_default_port and (parsed.scheme.casefold(), port) in {("http", 80), ("https", 443)}:
        return ""
    return f":{port}"


def _url_auth_part(parsed: Any) -> str:
    if not parsed.username:
        return ""
    password = f":{parsed.password}" if parsed.password else ""
    return f"{parsed.username}{password}@"


def _normalized_path(path: str, options: NormalizeOptions) -> str:
    if not options.strip_trailing_slash:
        return path
    clean = path.rstrip("/")
    return "" if clean == "" else clean


def _normalized_query(query: str, options: NormalizeOptions) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    if options.strip_tracking_params:
        pairs = [(key, value) for key, value in pairs if not _is_tracking_param(key)]
    return urlencode(pairs, doseq=True)


def _is_tracking_param(name: str) -> bool:
    lowered = name.casefold()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAM_NAMES


def _immutable_names(values: Iterable[str]) -> set[str]:
    return {_folder_match_key(value) for value in values if _folder_match_key(value)}


def is_immutable_bookmark(bookmark: Bookmark, immutable_roots: set[str]) -> bool:
    names = [_root_display(bookmark.root)]
    if bookmark.folder_path:
        names.append(bookmark.folder_path[0])
    return any(_folder_match_key(name) in immutable_roots for name in names)


def deduplicate_bookmarks(
    bookmarks: Sequence[Bookmark],
    immutable_roots: set[str],
    options: NormalizeOptions,
) -> tuple[list[Bookmark], list[Bookmark]]:
    immutable, mutable = _split_immutable(bookmarks, immutable_roots)
    immutable_keys = {_canonical_key(bookmark, options) for bookmark in immutable}
    grouped = _group_mutable_bookmarks(mutable, immutable_keys, options)
    return immutable, [_merge_bookmark_group(group, options) for group in grouped.values()]


def _split_immutable(
    bookmarks: Sequence[Bookmark],
    immutable_roots: set[str],
) -> tuple[list[Bookmark], list[Bookmark]]:
    immutable: list[Bookmark] = []
    mutable: list[Bookmark] = []
    for bookmark in bookmarks:
        if is_immutable_bookmark(bookmark, immutable_roots):
            immutable.append(_replace_bookmark(bookmark, immutable=True))
        else:
            mutable.append(bookmark)
    return immutable, mutable


def _group_mutable_bookmarks(
    bookmarks: Sequence[Bookmark],
    immutable_keys: set[str],
    options: NormalizeOptions,
) -> OrderedDict[str, list[Bookmark]]:
    grouped: OrderedDict[str, list[Bookmark]] = OrderedDict()
    for bookmark in bookmarks:
        key = _canonical_key(bookmark, options)
        if key in immutable_keys:
            LOGGER.info("Removed mutable duplicate of immutable bookmark: %s", bookmark.url)
            continue
        grouped.setdefault(key, []).append(bookmark)
    return grouped


def _canonical_key(bookmark: Bookmark, options: NormalizeOptions) -> str:
    return normalize_url(bookmark.url, options)[0]


def _merge_bookmark_group(group: Sequence[Bookmark], options: NormalizeOptions) -> Bookmark:
    title_source = max(group, key=lambda item: len(item.title.strip()))
    display_url = _preferred_display_url(group, options)
    return Bookmark(
        url=display_url,
        title=_bookmark_title(title_source.title, display_url),
        folder_path=(),
        root="bookmark_bar",
        add_date=_oldest_date(group),
        last_modified=_newest_date(group),
        source=", ".join(sorted({item.source for item in group if item.source})),
    )


def _preferred_display_url(group: Sequence[Bookmark], options: NormalizeOptions) -> str:
    displays = [normalize_url(bookmark.url, options)[1] for bookmark in group]
    return max(displays, key=_display_url_score)


def _display_url_score(url: str) -> tuple[int, int]:
    scheme = urlsplit(url).scheme.casefold()
    return (1 if scheme == "https" else 0, -len(url))


def _oldest_date(group: Sequence[Bookmark]) -> int | None:
    dates = [item.add_date for item in group if item.add_date is not None]
    return min(dates) if dates else None


def _newest_date(group: Sequence[Bookmark]) -> int | None:
    dates = [item.last_modified for item in group if item.last_modified is not None]
    return max(dates) if dates else None


def _replace_bookmark(bookmark: Bookmark, **changes: Any) -> Bookmark:
    data = bookmark.__dict__.copy()
    data.update(changes)
    return Bookmark(**data)


CategoryProvider = Callable[[Sequence[Bookmark]], Mapping[int, Sequence[str] | str]]


def tidy_bookmarks(
    bookmarks: Sequence[Bookmark],
    immutable_roots: Iterable[str],
    options: NormalizeOptions,
    categorizer: CategoryProvider | None,
    fallback_category: str = DEFAULT_FALLBACK_CATEGORY,
    batch_size: int = DEFAULT_LLM_BATCH_SIZE,
) -> list[Bookmark]:
    immutable, mutable = deduplicate_bookmarks(bookmarks, _immutable_names(immutable_roots), options)
    if mutable and categorizer is None:
        raise UserError("mutable bookmarks require --model so llama.cpp can categorize them")
    categorized = _assign_categories(mutable, categorizer, fallback_category, batch_size)
    return immutable + categorized


def _assign_categories(
    bookmarks: Sequence[Bookmark],
    categorizer: CategoryProvider | None,
    fallback: str,
    batch_size: int,
) -> list[Bookmark]:
    if not bookmarks:
        return []
    result: list[Bookmark] = []
    failures = 0
    for batch in _chunks(bookmarks, max(1, batch_size)):
        categories, failures = _categorize_if_available(categorizer, batch, failures)
        result.extend(_apply_category_batch(batch, categories, fallback))
    return result


def _categorize_if_available(
    categorizer: CategoryProvider | None,
    batch: Sequence[Bookmark],
    failures: int,
) -> tuple[Mapping[int, Sequence[str] | str], int]:
    if categorizer is None or failures >= LLM_CONSECUTIVE_FAILURE_LIMIT:
        return {}, failures
    categories, failed = _categorize_batch(categorizer, batch)
    failures = failures + 1 if failed else 0
    if failures == LLM_CONSECUTIVE_FAILURE_LIMIT:
        LOGGER.warning(
            "LLM categorization aborted after %d consecutive batch failures; remaining bookmarks use the fallback category",
            LLM_CONSECUTIVE_FAILURE_LIMIT,
        )
    return categories, failures


def _categorize_batch(
    categorizer: CategoryProvider,
    batch: Sequence[Bookmark],
) -> tuple[Mapping[int, Sequence[str] | str], bool]:
    try:
        return categorizer(batch), False
    except Exception as exc:
        LOGGER.warning("LLM categorization failed for %d bookmark(s); using fallback category: %s", len(batch), exc)
        return {}, True


def _chunks(items: Sequence[Bookmark], size: int) -> Iterable[Sequence[Bookmark]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _apply_category_batch(
    batch: Sequence[Bookmark],
    categories: Mapping[int, Sequence[str] | str],
    fallback: str,
) -> list[Bookmark]:
    result = []
    for index, bookmark in enumerate(batch):
        path = _category_path(categories.get(index), fallback)
        result.append(_replace_bookmark(bookmark, folder_path=path, root="bookmark_bar", immutable=False))
    return result


def _category_path(value: Sequence[str] | str | None, fallback: str) -> tuple[str, ...]:
    if value is None:
        return (_clean_folder_part(fallback),)
    if isinstance(value, str):
        parts = value.replace("\\", "/").replace(">", "/").split("/")
    else:
        parts = [str(part) for part in value]
    clean = tuple(part for part in (_clean_folder_part(part) for part in parts) if part)
    return clean or (_clean_folder_part(fallback),)


def _complete_llama(llm: Any, prompt: str, max_tokens: int) -> str:
    if hasattr(llm, "create_chat_completion"):
        response = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=max_tokens,
        )
        return str(response["choices"][0]["message"]["content"])
    response = llm(prompt, temperature=0, max_tokens=max_tokens)
    return str(response["choices"][0]["text"])


def _llama_process_worker(
    connection: Any,
    model_path: str,
    auto_install: bool,
    context: int,
    gpu_layers: int,
    max_tokens: int,
) -> None:
    try:
        Llama = _import_llama(auto_install)
        llm = Llama(
            model_path=model_path,
            n_ctx=context,
            n_gpu_layers=gpu_layers,
            verbose=False,
        )
        connection.send(("ready", ""))
        while True:
            command, value = connection.recv()
            if command == "close":
                break
            try:
                connection.send(("ok", _complete_llama(llm, value, max_tokens)))
            except Exception as exc:
                connection.send(("error", str(exc)))
    except EOFError:
        pass
    except Exception as exc:
        try:
            connection.send(("startup_error", str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


class LlamaCategorizer:
    def __init__(
        self,
        model_path: Path,
        auto_install: bool,
        context: int,
        gpu_layers: int,
        max_tokens: int,
    ) -> None:
        context_name = "spawn" if os.name == "nt" else "fork"
        process_context = multiprocessing.get_context(context_name)
        parent, child = process_context.Pipe()
        self._connection = parent
        self._process = process_context.Process(
            target=_llama_process_worker,
            args=(
                child,
                str(model_path),
                auto_install,
                context,
                gpu_layers,
                max_tokens,
            ),
            daemon=True,
        )
        try:
            self._process.start()
            child.close()
        except Exception as exc:
            parent.close()
            child.close()
            raise UserError(
                f"could not start llama.cpp worker for {model_path}: {exc}"
            ) from exc
        if not parent.poll(LLAMA_MODEL_LOAD_TIMEOUT_SECONDS):
            self._abort()
            raise UserError(
                f"could not load model {model_path}: timed out after "
                f"{LLAMA_MODEL_LOAD_TIMEOUT_SECONDS:g}s"
            )
        try:
            status, message = parent.recv()
        except (EOFError, OSError) as exc:
            self._abort()
            raise UserError(
                f"could not load model {model_path}: worker exited"
            ) from exc
        if status != "ready":
            self._abort()
            raise UserError(
                f"could not load model {model_path}: {message}; "
                "verify the file is a valid GGUF model compatible with "
                "llama-cpp-python"
            )

    def __call__(self, bookmarks: Sequence[Bookmark]) -> Mapping[int, Sequence[str] | str]:
        prompt = _category_prompt(bookmarks)
        return parse_category_response(self._complete(prompt), len(bookmarks))

    def _complete(self, prompt: str) -> str:
        try:
            self._connection.send(("complete", prompt))
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise UserError("LLM inference worker is unavailable") from exc
        if not self._connection.poll(LLAMA_INFERENCE_TIMEOUT_SECONDS):
            self._abort()
            raise UserError(
                "LLM inference timed out after "
                f"{LLAMA_INFERENCE_TIMEOUT_SECONDS:g}s"
            )
        try:
            status, value = self._connection.recv()
        except (EOFError, OSError) as exc:
            self._abort()
            raise UserError("LLM inference worker exited unexpectedly") from exc
        if status == "ok":
            return str(value)
        raise UserError(f"LLM inference failed: {value}")

    def _abort(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(LLAMA_PROCESS_STOP_SECONDS)
            if self._process.is_alive():
                self._process.kill()
                self._process.join()
        self._connection.close()

    def close(self) -> None:
        if self._process.is_alive():
            try:
                self._connection.send(("close", ""))
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(LLAMA_PROCESS_STOP_SECONDS)
        if self._process.is_alive():
            self._abort()
        else:
            self._connection.close()


def _import_llama(auto_install: bool) -> Any:
    try:
        from llama_cpp import Llama
        return Llama
    except ImportError as exc:
        if not auto_install:
            raise UserError("llama-cpp-python is missing; install it or pass --auto-install-llama") from exc
    LOGGER.warning("Installing %s with pip because --auto-install-llama was provided.", LLAMA_CPP_PYTHON_REQUIREMENT)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", LLAMA_CPP_PYTHON_REQUIREMENT],
            timeout=LLAMA_INSTALL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise UserError(f"failed to install {LLAMA_CPP_PYTHON_REQUIREMENT}: {exc}") from exc
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise UserError(f"{LLAMA_CPP_PYTHON_REQUIREMENT} is still unavailable after install") from exc
    return Llama


def _category_prompt(bookmarks: Sequence[Bookmark]) -> str:
    rows = [
        {"id": index, "title": bookmark.title, "url": bookmark.url}
        for index, bookmark in enumerate(bookmarks)
    ]
    return (
        "Categorize these browser bookmarks into concise folder paths. "
        "You may create any categories that fit the content. "
        "Return only JSON shaped as {\"items\":[{\"id\":0,\"category\":[\"Folder\",\"Subfolder\"]}]}. "
        f"Bookmarks: {json.dumps(rows, ensure_ascii=False)}"
    )


def parse_category_response(text: str, expected_count: int) -> dict[int, tuple[str, ...]]:
    data = _loads_json_object(text)
    if isinstance(data.get("items"), list):
        return _parse_category_items(data["items"], expected_count)
    return _parse_category_mapping(data, expected_count)


def _loads_json_object(text: str) -> Mapping[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise UserError("LLM did not return a JSON object")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise UserError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise UserError("LLM JSON response must be an object")
    return data


def _parse_category_items(items: Sequence[Any], expected_count: int) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    for item in items:
        if isinstance(item, Mapping):
            _add_category_result(result, item.get("id"), item.get("category"), expected_count)
    return result


def _parse_category_mapping(data: Mapping[str, Any], expected_count: int) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    for key, value in data.items():
        _add_category_result(result, key, value, expected_count)
    return result


def _add_category_result(
    result: dict[int, tuple[str, ...]],
    raw_index: Any,
    raw_category: Any,
    expected_count: int,
) -> None:
    index = _safe_int(raw_index)
    if index is None or index < 0 or index >= expected_count:
        return
    result[index] = _category_path(raw_category, DEFAULT_FALLBACK_CATEGORY)


FolderIndex = dict[int, dict[Any, dict[str, Any]]]


def export_chrome_bookmarks(bookmarks: Sequence[Bookmark]) -> dict[str, Any]:
    ids = _IdFactory()
    index: FolderIndex = {}
    roots = {
        key: _chrome_folder_node(ids, name)
        for key, name in CHROME_ROOT_NAMES.items()
    }
    for bookmark in bookmarks:
        root, folder_path = _chrome_output_location(bookmark)
        _add_chrome_bookmark(roots[root], folder_path, bookmark, ids, index)
    return {"checksum": "", "roots": roots, "version": 1}


def _folder_map(
    parent: dict[str, Any],
    index: FolderIndex,
    type_key: str,
    type_value: Any,
    name_key: str,
) -> dict[Any, dict[str, Any]]:
    folders = index.get(id(parent))
    if folders is None:
        folders = {}
        for child in parent["children"]:
            if child.get(type_key) == type_value:
                folders.setdefault(child.get(name_key), child)
        index[id(parent)] = folders
    return folders


def _chrome_output_location(bookmark: Bookmark) -> tuple[str, tuple[str, ...]]:
    if bookmark.root == "menu":
        return "other", ("Bookmarks Menu", *bookmark.folder_path)
    if bookmark.root in CHROME_ROOT_NAMES:
        return bookmark.root, bookmark.folder_path
    return "other", bookmark.folder_path


def _chrome_folder_node(ids: "_IdFactory", name: str) -> dict[str, Any]:
    return {
        "children": [],
        "date_added": _unix_to_chrome_time(None),
        "date_modified": _unix_to_chrome_time(None),
        "guid": str(uuid.uuid4()),
        "id": ids.next(),
        "name": name,
        "type": "folder",
    }


def _add_chrome_bookmark(
    root: dict[str, Any],
    folder_path: Sequence[str],
    bookmark: Bookmark,
    ids: "_IdFactory",
    index: FolderIndex,
) -> None:
    folder = _ensure_folder(
        root, folder_path, ids, index, _child_chrome_folder
    )
    folder["children"].append(
        {
            "date_added": _unix_to_chrome_time(bookmark.add_date),
            "guid": str(uuid.uuid4()),
            "id": ids.next(),
            "name": bookmark.title,
            "type": "url",
            "url": bookmark.url,
        }
    )


def _ensure_folder(
    root: dict[str, Any],
    folder_path: Sequence[str],
    ids: "_IdFactory",
    index: FolderIndex,
    child_finder: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    current = root
    for name in folder_path:
        current = child_finder(current, name, ids, index)
    return current


def _child_chrome_folder(
    parent: dict[str, Any],
    name: str,
    ids: "_IdFactory",
    index: FolderIndex | None = None,
) -> dict[str, Any]:
    folders = _folder_map(parent, {} if index is None else index, "type", "folder", "name")
    child = folders.get(name)
    if child is None:
        child = _chrome_folder_node(ids, name)
        parent["children"].append(child)
        folders[name] = child
    return child


def export_firefox_bookmarks(bookmarks: Sequence[Bookmark]) -> dict[str, Any]:
    ids = _IdFactory()
    index: FolderIndex = {}
    root = _firefox_root_node(ids)
    folder_nodes = {key: _firefox_named_root(ids, key) for key in FIREFOX_ROOT_GUIDS}
    root["children"] = list(folder_nodes.values())
    for bookmark in bookmarks:
        target = folder_nodes.get(bookmark.root, folder_nodes["other"])
        _add_firefox_bookmark(target, bookmark.folder_path, bookmark, ids, index)
    return root


def _firefox_root_node(ids: "_IdFactory") -> dict[str, Any]:
    return _firefox_folder_node(ids, "", "root________", "placesRoot")


def _firefox_named_root(ids: "_IdFactory", root: str) -> dict[str, Any]:
    root_names = {
        "bookmark_bar": ("Bookmarks Toolbar", "toolbarFolder"),
        "menu": ("Bookmarks Menu", "bookmarksMenuFolder"),
        "other": ("Other Bookmarks", "unfiledBookmarksFolder"),
        "synced": ("Mobile Bookmarks", "mobileFolder"),
    }
    title, root_name = root_names[root]
    return _firefox_folder_node(ids, title, FIREFOX_ROOT_GUIDS[root], root_name)


def _firefox_folder_node(
    ids: "_IdFactory",
    title: str,
    guid: str | None = None,
    root: str | None = None,
) -> dict[str, Any]:
    node = {
        "children": [],
        "dateAdded": _unix_to_firefox_time(None),
        "guid": guid or str(uuid.uuid4()),
        "id": int(ids.next()),
        "index": 0,
        "lastModified": _unix_to_firefox_time(None),
        "title": title,
        "type": "text/x-moz-place-container",
        "typeCode": 2,
    }
    if root is not None:
        node["root"] = root
    return node


def _add_firefox_bookmark(
    root: dict[str, Any],
    folder_path: Sequence[str],
    bookmark: Bookmark,
    ids: "_IdFactory",
    index: FolderIndex,
) -> None:
    folder = _ensure_folder(
        root, folder_path, ids, index, _child_firefox_folder
    )
    folder["children"].append(_firefox_bookmark_node(bookmark, ids))


def _child_firefox_folder(
    parent: dict[str, Any],
    name: str,
    ids: "_IdFactory",
    index: FolderIndex | None = None,
) -> dict[str, Any]:
    folders = _folder_map(parent, {} if index is None else index, "typeCode", 2, "title")
    child = folders.get(name)
    if child is None:
        child = _firefox_folder_node(ids, name)
        parent["children"].append(child)
        folders[name] = child
    return child


def _firefox_bookmark_node(bookmark: Bookmark, ids: "_IdFactory") -> dict[str, Any]:
    return {
        "dateAdded": _unix_to_firefox_time(bookmark.add_date),
        "guid": str(uuid.uuid4()),
        "id": int(ids.next()),
        "index": 0,
        "lastModified": _unix_to_firefox_time(bookmark.last_modified),
        "title": bookmark.title,
        "type": "text/x-moz-place",
        "typeCode": 1,
        "uri": bookmark.url,
    }


def export_netscape_bookmarks(bookmarks: Sequence[Bookmark]) -> str:
    grouped = _group_by_root(bookmarks)
    lines = [
        "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        "<TITLE>Bookmarks</TITLE>",
        "<H1>Bookmarks</H1>",
        "<DL><p>",
    ]
    for root, root_bookmarks in grouped.items():
        lines.extend(_netscape_folder_lines(_root_display(root), root_bookmarks, 1))
    lines.append("</DL><p>")
    return "\n".join(lines) + "\n"


def _group_by_root(bookmarks: Sequence[Bookmark]) -> OrderedDict[str, list[Bookmark]]:
    grouped: OrderedDict[str, list[Bookmark]] = OrderedDict()
    for bookmark in bookmarks:
        grouped.setdefault(bookmark.root, []).append(bookmark)
    return grouped


def _netscape_folder_lines(name: str, bookmarks: Sequence[Bookmark], depth: int) -> list[str]:
    indent = "    " * depth
    lines = [f'{indent}<DT><H3 ADD_DATE="{int(time.time())}">{escape(name)}</H3>', f"{indent}<DL><p>"]
    for folder_name, folder_bookmarks in _group_by_next_folder(bookmarks).items():
        lines.extend(_netscape_folder_lines(folder_name, folder_bookmarks, depth + 1))
    for bookmark in [item for item in bookmarks if not item.folder_path]:
        lines.append(_netscape_link_line(bookmark, depth + 1))
    lines.append(f"{indent}</DL><p>")
    return lines


def _group_by_next_folder(bookmarks: Sequence[Bookmark]) -> OrderedDict[str, list[Bookmark]]:
    grouped: OrderedDict[str, list[Bookmark]] = OrderedDict()
    for bookmark in bookmarks:
        if bookmark.folder_path:
            head, *tail = bookmark.folder_path
            grouped.setdefault(head, []).append(_replace_bookmark(bookmark, folder_path=tuple(tail)))
    return grouped


def _netscape_link_line(bookmark: Bookmark, depth: int) -> str:
    indent = "    " * depth
    add_date = int(bookmark.add_date if bookmark.add_date is not None else time.time())
    return f'{indent}<DT><A HREF="{escape(bookmark.url, quote=True)}" ADD_DATE="{add_date}">{escape(bookmark.title)}</A>'


class _IdFactory:
    def __init__(self) -> None:
        self._next = 1

    def next(self) -> str:
        value = str(self._next)
        self._next += 1
        return value


def write_output(bookmarks: Sequence[Bookmark], output: Path, output_format: str, force: bool) -> None:
    if output.exists() and not force:
        raise UserError(f"output exists, pass --force to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "chrome":
        text = json.dumps(export_chrome_bookmarks(bookmarks), ensure_ascii=False, indent=2)
    elif output_format == "firefox":
        text = json.dumps(export_firefox_bookmarks(bookmarks), ensure_ascii=False, indent=2)
    else:
        text = export_netscape_bookmarks(bookmarks)
    _atomic_write_text(output, text)


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_parent_dir(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _fsync_parent_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(path.parent, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def default_output_path(output_format: str) -> Path:
    suffix = ".html" if output_format == "netscape" else ".json"
    return Path(f"bookmarks_tidy_output{suffix}")


def load_immutable_file(path: str | None) -> list[str]:
    if path is None:
        return []
    immutable_path = Path(path).expanduser()
    try:
        text = immutable_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"could not read immutable file {immutable_path}: {exc}") from exc
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def read_all_bookmarks(paths: Sequence[Path]) -> list[Bookmark]:
    bookmarks: list[Bookmark] = []
    for path in paths:
        try:
            found = read_bookmark_file(path)
        except UserError as exc:
            LOGGER.warning("%s", exc)
            continue
        except Exception as exc:
            LOGGER.warning("could not read bookmark file %s: %s", path, exc)
            continue
        LOGGER.info("Read %d bookmarks from %s", len(found), path)
        bookmarks.extend(found)
    return bookmarks


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {text}")
    return value


def _gpu_layers_int(text: str) -> int:
    # llama.cpp semantics: 0 = CPU only, -1 = offload all layers.
    value = int(text)
    if value < -1:
        raise argparse.ArgumentTypeError(f"must be >= -1, got {text}")
    return value


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", help="Bookmark files or folders. If omitted, browser profiles are discovered.")
    parser.add_argument("-o", "--output", help="Output file path. Defaults to bookmarks_tidy_output.json/html.")
    parser.add_argument(
        "--output-format", "--format",
        choices=("chrome", "firefox", "netscape"),
        default="chrome",
        help="Bookmark export format (default: chrome).",
    )
    parser.add_argument("--model", type=Path, help="Local GGUF model path loaded through llama-cpp-python.")
    parser.add_argument("--auto-install-llama", action="store_true", help="Install llama-cpp-python with pip if missing.")
    parser.add_argument("--immutable-root", action="append", default=[], help="Top-level folder/category name to copy untouched.")
    parser.add_argument("--immutable-file", help="Text file with one immutable folder/category name per line.")
    parser.add_argument("--discover-browsers", action="store_true", help="Add default Chrome, Edge, and Firefox profiles to inputs.")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false", default=True, help="Do not scan input folders recursively.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output file.")
    parser.add_argument("--keep-fragments", dest="strip_fragment", action="store_false",
                        default=True, help="Keep URL fragments when matching duplicates.")
    parser.add_argument("--keep-http-https-distinct", dest="collapse_http_https",
                        action="store_false", default=True,
                        help="Treat HTTP and HTTPS URLs as distinct.")
    parser.add_argument("--keep-trailing-slash", dest="strip_trailing_slash",
                        action="store_false", default=True,
                        help="Keep trailing URL path slashes.")
    parser.add_argument("--keep-default-port", dest="strip_default_port",
                        action="store_false", default=True,
                        help="Keep explicit default ports such as :80 and :443.")
    parser.add_argument("--keep-www", dest="strip_www", action="store_false",
                        default=True, help="Keep a leading www. host label.")
    parser.add_argument("--keep-tracking-params", dest="strip_tracking_params",
                        action="store_false", default=True,
                        help="Keep known URL tracking query parameters.")
    parser.add_argument("--preserve-url-host-case", dest="lowercase_host",
                        action="store_false", default=True,
                        help="Preserve host letter case in exported URLs.")
    parser.add_argument("--llm-context", type=_positive_int, default=DEFAULT_LLM_CONTEXT,
                        help=f"llama.cpp context size in tokens (default {DEFAULT_LLM_CONTEXT})")
    parser.add_argument("--llm-gpu-layers", type=_gpu_layers_int, default=0,
                        help="number of llama.cpp model layers to offload to GPU; -1 offloads all (default 0)")
    parser.add_argument("--llm-max-tokens", type=_positive_int, default=DEFAULT_LLM_MAX_TOKENS,
                        help=f"maximum tokens generated for categorization (default {DEFAULT_LLM_MAX_TOKENS})")
    parser.add_argument("--llm-batch-size", type=_positive_int, default=DEFAULT_LLM_BATCH_SIZE,
                        help=f"llama.cpp prompt batch size (default {DEFAULT_LLM_BATCH_SIZE})")
    parser.add_argument(
        "--fallback-category",
        default=DEFAULT_FALLBACK_CATEGORY,
        help=("Category used when LLM categorization fails "
              f"(default: {DEFAULT_FALLBACK_CATEGORY})."),
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args(argv)


def _normalization_from_args(args: argparse.Namespace) -> NormalizeOptions:
    return NormalizeOptions(
        strip_fragment=args.strip_fragment,
        collapse_http_https=args.collapse_http_https,
        strip_trailing_slash=args.strip_trailing_slash,
        strip_default_port=args.strip_default_port,
        strip_www=args.strip_www,
        strip_tracking_params=args.strip_tracking_params,
        lowercase_host=args.lowercase_host,
    )


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(format="%(levelname)s: %(message)s", level=level)


def _input_paths_from_args(args: argparse.Namespace) -> list[Path]:
    paths = expand_input_paths(args.inputs, args.recursive) if args.inputs else []
    if args.discover_browsers or not args.inputs:
        paths.extend(discover_browser_bookmarks())
    return _unique_paths(paths)


def _categorizer_from_args(args: argparse.Namespace, bookmarks: Sequence[Bookmark]) -> CategoryProvider | None:
    if not bookmarks:
        return None
    if args.model is None:
        raise UserError("missing --model for LLM categorization")
    model_path = args.model.expanduser()
    if not model_path.is_file():
        raise UserError(f"model file not found: {model_path}")
    return LlamaCategorizer(
        model_path=model_path,
        auto_install=args.auto_install_llama,
        context=args.llm_context,
        gpu_layers=args.llm_gpu_layers,
        max_tokens=args.llm_max_tokens,
    )


def _run(args: argparse.Namespace) -> int:
    paths = _input_paths_from_args(args)
    if not paths:
        raise UserError("no bookmark inputs found")
    bookmarks = read_all_bookmarks(paths)
    if not bookmarks:
        raise UserError("no bookmarks found in supported input files")
    immutable = list(args.immutable_root) + load_immutable_file(args.immutable_file)
    options = _normalization_from_args(args)
    categorizer = (
        _categorizer_from_args(args, bookmarks)
        if args.model is not None
        else None
    )
    try:
        organized = tidy_bookmarks(
            bookmarks,
            immutable,
            options,
            categorizer,
            args.fallback_category,
            args.llm_batch_size,
        )
    finally:
        close = getattr(categorizer, "close", None)
        if callable(close):
            close()
    duplicate_count = len(bookmarks) - len(organized)
    if duplicate_count:
        LOGGER.warning("Merged/removed %d duplicate bookmark(s).", duplicate_count)
    output = Path(args.output).expanduser() if args.output else default_output_path(args.output_format)
    write_output(organized, output, args.output_format, args.force)
    LOGGER.warning("Wrote %d bookmarks to %s", len(organized), output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(list(argv) if argv is not None else sys.argv[1:])
    _configure_logging(args.verbose)
    try:
        return _run(args)
    except (UserError, OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
