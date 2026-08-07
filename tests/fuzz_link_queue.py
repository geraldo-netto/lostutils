#!/usr/bin/env python3
"""
Fuzz / property-based test harness for link_queue.py

Targets the parts that don't require a live Tk display: pure parsers,
URL/protocol routing, command-template resolution, config merging, and
on-disk state-file loading. Each fuzz target asserts:

  * the function never raises an unexpected exception, AND
  * its output satisfies an invariant that should hold for *any* input
    (e.g. _build_argv always returns a list of str, _domain_of always
    returns a non-empty string, _format_duration always returns a non-
    empty str, etc.).

Run:
    python3 -m pytest tests/fuzz_link_queue.py -v
or:
    python3 tests/fuzz_link_queue.py
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from hypothesis import HealthCheck, given, settings, strategies as st

# Make sure standalone execution imports the file under test from the repo root.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import link_queue  # noqa: E402
from link_queue import LinkQueueApp, QueueItem  # noqa: E402

# Hypothesis settings: more examples than the default to actually fuzz.
FUZZ = settings(
    max_examples=400,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)

# Default no-op template — used in any number of QueueItem builders below.
DEFAULT_TPL = "echo {url}"

# Placeholder tokens the templates support. Hoisted so the literals stop
# duplicating across strategies and fuzz targets.
PH_URL = "{url}"
PH_URL_QUOTED = "{url_quoted}"


# ---------------------------------------------------------------------------
# Reusable strategies
# ---------------------------------------------------------------------------

# A printable-but-tricky text strategy: includes whitespace, control chars,
# unicode, NULs and high planes — but no surrogates (which can't be encoded
# as utf-8 and would crash YAML/JSON before reaching our code).
weird_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),  # surrogates only
    ),
    max_size=200,
)

# Per-script alphabets (codepoint ranges from the Unicode standard).
# Used both directly in URLs and inside path / query segments to make
# sure non-ASCII URLs survive parse / resolve / argv-build / state I/O.
#
# Each alphabet excludes characters that would structurally break a URL:
# delimiters from RFC 3986 (`: / ? # [ ] @`), the percent sign that
# starts a percent-escape, and ASCII whitespace. This keeps the
# generated URLs syntactically valid while still varying the scripts
# being exercised.
_URL_RESERVED = set(":/?#[]@%& \t\r\n")


def _safe_url_alphabet(min_cp: int, max_cp: int):
    return st.characters(
        min_codepoint=min_cp, max_codepoint=max_cp,
        blacklist_characters="".join(sorted(_URL_RESERVED)),
    )


cyrillic_alphabet = _safe_url_alphabet(0x0400, 0x04FF)
hiragana_alphabet = _safe_url_alphabet(0x3040, 0x309F)
katakana_alphabet = _safe_url_alphabet(0x30A0, 0x30FF)
cjk_alphabet      = _safe_url_alphabet(0x4E00, 0x9FFF)
arabic_alphabet   = _safe_url_alphabet(0x0600, 0x06FF)
hebrew_alphabet   = _safe_url_alphabet(0x0590, 0x05FF)
emoji_alphabet    = _safe_url_alphabet(0x1F300, 0x1FAFF)

# Mixed: any non-surrogate codepoint, but still excluding URL-reserved.
mixed_alphabet = st.characters(
    blacklist_categories=("Cs",),
    blacklist_characters="".join(sorted(_URL_RESERVED)),
)

# International text — a non-ASCII slug 1..30 chars from a randomly chosen
# script (or a mix). Hypothesis will sample across all of them.
international_text = st.one_of(
    st.text(alphabet=cyrillic_alphabet, min_size=1, max_size=30),
    st.text(alphabet=hiragana_alphabet, min_size=1, max_size=30),
    st.text(alphabet=katakana_alphabet, min_size=1, max_size=30),
    st.text(alphabet=cjk_alphabet,      min_size=1, max_size=30),
    st.text(alphabet=arabic_alphabet,   min_size=1, max_size=30),
    st.text(alphabet=hebrew_alphabet,   min_size=1, max_size=30),
    st.text(alphabet=emoji_alphabet,    min_size=1, max_size=10),
    st.text(alphabet=mixed_alphabet,    min_size=1, max_size=40),
)

# International URLs: full-form (raw unicode in host/path), Punycode/IDN,
# and percent-encoded UTF-8. All three shapes appear in the wild and any
# URL-handling code is supposed to cope with all of them.
international_url_strategy = st.one_of(
    # Raw unicode host (e.g. https://россия.рф/привет, https://中国.cn/路径)
    st.builds(
        lambda host, path: f"https://{host}.test/{path}",
        international_text, international_text,
    ),
    # Punycode / IDN (e.g. xn--h1alffa9f.xn--p1ai for россия.рф)
    st.sampled_from([
        "https://xn--h1alffa9f.xn--p1ai/",          # россия.рф
        "https://xn--fiqs8s.cn/路径",                # 中国.cn
        "https://xn--mgbh0fb.xn--mgbaam7a8h/",      # مثال.إمارات
        "https://xn--80akhbyknj4f.com/тест",        # испытание.com
        "https://例え.テスト/",                       # raw Japanese
    ]),
    # Percent-encoded UTF-8 (e.g. /%D1%80%D1%83 = "ру")
    st.builds(
        lambda txt: "https://example.com/" + "".join(
            f"%{b:02X}" for b in txt.encode("utf-8")
        ),
        international_text,
    ),
)

# A "URL-ish" string: scheme://something or junk.  We deliberately include
# malformed shapes (no scheme, multiple ://, embedded NULs, very long, etc.)
url_strategy = st.one_of(
    st.from_regex(r"\Ahttps?://[a-zA-Z0-9.\-]{1,40}(/[a-zA-Z0-9._\-/]*)?\Z",
                  fullmatch=True),
    st.from_regex(r"\A(magnet|file|ftp|s3|gemini)://[^\s]{0,80}\Z",
                  fullmatch=True),
    international_url_strategy,
    weird_text,
    st.just(""),
    st.just("http://"),
    st.just("://example.com"),
    st.just("\x00\x00\x00"),
    st.just("a" * 10_000),
)

# Templates: legal placeholders, broken placeholders, plain shell.
template_strategy = st.one_of(
    st.just(DEFAULT_TPL),
    st.just("curl -L {url_quoted}"),
    st.just("yt-dlp {url} --output 'x {protocol}.mp4'"),
    st.just("echo {url} {protocol} {url_quoted} {url}"),
    st.just("echo 'unmatched"),                 # invalid argv quoting
    st.just(""),
    st.just("{url"),                            # incomplete placeholder
    st.just("{nope}"),                          # unknown placeholder name
    st.just("rm -rf /"),                        # no placeholder at all
    weird_text,
)

protocol_name_strategy = st.one_of(
    st.from_regex(r"\A[a-z][a-z0-9+\-.]{0,15}\Z", fullmatch=True),
    st.just(""),
    weird_text,
)


# ---------------------------------------------------------------------------
# Pure-helper fuzz targets  (no Tk / no filesystem)
# ---------------------------------------------------------------------------

class FuzzPureHelpers(unittest.TestCase):

    @FUZZ
    @given(
        raw=st.text(max_size=400),
        mappings=st.dictionaries(
            keys=st.text(min_size=1, max_size=4),
            values=st.text(min_size=1, max_size=8),
            max_size=5),
    )
    def test_parse_entries_well_formed(self, raw, mappings):
        """Any pasted text + any prefix map -> a list of (url, extra) where
        url is a non-empty str and extra is a tuple of (flag, value) str pairs.
        Round-trips through _extra_argv/_extra_shell without crashing."""
        entries = LinkQueueApp._parse_entries(raw, mappings)
        self.assertIsInstance(entries, list)
        for url, extra in entries:
            self.assertIsInstance(url, str)
            self.assertTrue(url)                       # empty-url entries dropped
            self.assertIsInstance(extra, tuple)
            for pair in extra:
                self.assertEqual(len(pair), 2)
                flag, value = pair
                self.assertIsInstance(flag, str)
                self.assertIsInstance(value, str)
            # assembly helpers tolerate whatever was parsed
            self.assertIsInstance(LinkQueueApp._extra_argv(extra), list)
            self.assertIsInstance(LinkQueueApp._extra_shell(extra), str)

    @FUZZ
    @given(url=url_strategy)
    def test_extract_protocol_never_crashes(self, url):
        """Any string in -> str out (possibly empty), no exception."""
        out = LinkQueueApp._extract_protocol(url)
        self.assertIsInstance(out, str)
        # Result is always lower case (or empty).
        self.assertEqual(out, out.lower())

    @FUZZ
    @given(url=url_strategy)
    def test_domain_of_url_never_crashes(self, url):
        out = LinkQueueApp._domain_of(url)
        self.assertIsInstance(out, str)
        # Never empty (falls back to scheme or "_unknown").
        self.assertTrue(out)
        # Never starts with "www." (we strip that prefix).
        self.assertFalse(out.startswith("www."))

    @FUZZ
    @given(url=url_strategy, proto=protocol_name_strategy,
           tpl=template_strategy)
    def test_domain_of_queueitem_never_crashes(self, url, proto, tpl):
        # Build a QueueItem with arbitrary fields and feed it in.
        it = QueueItem(url=url, protocol=proto, template=tpl, shell=False)
        out = LinkQueueApp._domain_of(it)
        self.assertIsInstance(out, str)
        self.assertTrue(out)

    @FUZZ
    @given(tpl=template_strategy, url=url_strategy, proto=protocol_name_strategy)
    def test_resolve_command_substitutes_safely(self, tpl, url, proto):
        out = LinkQueueApp._resolve_command(tpl, url, proto)
        self.assertIsInstance(out, str)
        # Single-pass substitution: a substituted value's own braces
        # must NOT be re-substituted. So if the URL contains the {url}
        # placeholder text, that literal must survive untouched.
        replacements = (
            (PH_URL, url),
            (PH_URL_QUOTED, shlex.quote(url)),
            ("{protocol}", proto),
        )
        for placeholder, replacement in replacements:
            if placeholder in tpl:
                self.assertIn(replacement, out)
        if PH_URL in url and PH_URL in tpl:
            self.assertIn(PH_URL, out)

    def test_resolve_command_no_self_resubstitution(self):
        # Direct regression test for the property the docstring promises.
        out = LinkQueueApp._resolve_command(
            f"x={PH_URL}", PH_URL_QUOTED, "https")
        # _url_quoted_ is the substituted value, NOT the placeholder
        # name, so no second pass should happen.
        self.assertEqual(out, "x={url_quoted}")

    @FUZZ
    @given(tpl=template_strategy, url=url_strategy, proto=protocol_name_strategy)
    def test_build_argv_either_returns_list_of_str_or_raises_value_error(
        self, tpl, url, proto,
    ):
        try:
            argv = LinkQueueApp._build_argv(tpl, url, proto)
        except ValueError:
            return  # documented: bad shell quoting -> ValueError
        self.assertIsInstance(argv, list)
        for a in argv:
            self.assertIsInstance(a, str)

    @FUZZ
    @given(seconds=st.integers(min_value=0, max_value=10**9))
    def test_format_duration_nonneg_returns_text(self, seconds):
        out = LinkQueueApp._format_duration(seconds)
        self.assertIsInstance(out, str)
        self.assertTrue(out)
        # Output never contains a negative sign for non-negative input.
        self.assertNotIn("-", out)

    @FUZZ
    @given(seconds=st.integers(min_value=-10**9, max_value=-1))
    def test_format_duration_negative_doesnt_crash(self, seconds):
        # Not formally documented, but the function should not raise on
        # negatives — it's called with `int(time_remaining + 0.5)` which
        # can theoretically go negative under clock skew.
        out = LinkQueueApp._format_duration(seconds)
        self.assertIsInstance(out, str)


    @FUZZ
    @given(raw=weird_text)
    def test_normalize_paste_text_invariants(self, raw):
        """The pure helper backing _normalize_paste_area must:
          * never crash on arbitrary input,
          * never insert blank lines in the output,
          * always end with exactly one trailing '\\n' (or be empty),
          * be idempotent (norm(norm(x)) == norm(x)),
          * and never glue tokens together: every non-whitespace token
            in the input survives as a standalone line.
        These properties are the whole reason the helper exists — they
        are what stops paste-paste-paste from concatenating URLs."""
        out = LinkQueueApp._normalize_paste_text(raw)
        self.assertIsInstance(out, str)
        # Empty input -> empty output. Otherwise, exactly one trailing \n.
        if not out:
            self.assertFalse(any(c.strip() for c in raw))
        else:
            self.assertTrue(out.endswith("\n"))
            self.assertFalse(out.endswith("\n\n"))
            # No blank lines anywhere inside.
            for line in out[:-1].split("\n"):
                self.assertTrue(line)
                self.assertEqual(line, line.strip())
        # Idempotence.
        self.assertEqual(LinkQueueApp._normalize_paste_text(out), out)
        # Token preservation: every whitespace-separated token in the
        # original must appear (verbatim) somewhere in the cleaned output.
        for tok in raw.split():
            self.assertIn(tok, out)

    def test_normalize_paste_text_no_glue_on_consecutive_pastes(self):
        """Regression for the user-reported bug: pasting URL #2 after
        URL #1 would land directly after URL #1 (no separator). Pasting
        the second URL into the *cleaned* buffer must produce two
        separate lines, not one glued line."""
        first = LinkQueueApp._normalize_paste_text("https://a.test/foo")
        # Simulating the second paste: Tk would insert "url2" at the
        # cursor position, which after our fix sits on a fresh empty
        # line. So the resulting raw buffer is `first + "url2"`.
        second_raw = first + "https://b.test/bar"
        second_cleaned = LinkQueueApp._normalize_paste_text(second_raw)
        lines = second_cleaned.rstrip("\n").split("\n")
        self.assertEqual(lines, [
            "https://a.test/foo",
            "https://b.test/bar",
        ])

    @FUZZ
    @given(line=weird_text)
    def test_highest_pct_in_returns_int_in_bounds(self, line):
        out = LinkQueueApp._highest_pct_in(line)
        self.assertIsInstance(out, int)
        # _PCT_RE matches \d{1,3} so the captured int is 0..999.
        self.assertGreaterEqual(out, -1)
        self.assertLessEqual(out, 999)

    @FUZZ
    @given(line=weird_text,
           already=st.sets(st.sampled_from((25, 50, 75, 100)), max_size=4))
    def test_milestones_crossed_subset_of_constant(self, line, already):
        app = _dispatcher()
        out = app._milestones_crossed(line, already)
        self.assertIsInstance(out, list)
        for m in out:
            self.assertIn(m, LinkQueueApp._SUMMARY_MILESTONES)
            self.assertNotIn(m, already)


# ---------------------------------------------------------------------------
# Refactored / static helpers added by the complexity-reduction pass
# ---------------------------------------------------------------------------

class FuzzRefactoredHelpers(unittest.TestCase):

    @FUZZ
    @given(
        domain_active=st.dictionaries(
            keys=st.text(min_size=1, max_size=10),
            values=st.integers(min_value=0, max_value=20),
            max_size=10,
        ),
        urls=st.lists(url_strategy, max_size=20),
        cap=st.integers(min_value=-1, max_value=10),
    )
    def test_pick_next_item_picks_min_active_under_cap(
        self, domain_active, urls, cap
    ):
        app = _dispatcher()
        app._domain_active = dict(domain_active)
        # _pick_next_item reads the per-domain index, so build a real
        # _PendingQueue (the plain-list path can't carry those indexes).
        app.queue_items = link_queue._PendingQueue(
            (QueueItem(url=u, protocol="http", template=DEFAULT_TPL, shell=False)
             for u in urls),
            domain_fn=LinkQueueApp._domain_of,
        )
        chosen = LinkQueueApp._pick_next_item(app, cap)
        if chosen is None:
            # Either queue is empty OR every domain is at/over the cap.
            if not app.queue_items:
                return
            if cap > 0:
                for it in app.queue_items:
                    self.assertGreaterEqual(
                        app._domain_active.get(LinkQueueApp._domain_of(it), 0),
                        cap,
                    )
            return
        # Otherwise the chosen item is a real pending item whose domain's
        # active count is <= every other claimable item's.
        self.assertIn(chosen, list(app.queue_items))
        chosen_active = app._domain_active.get(
            LinkQueueApp._domain_of(chosen), 0)
        for it in app.queue_items:
            d = LinkQueueApp._domain_of(it)
            a = app._domain_active.get(d, 0)
            if cap > 0 and a >= cap:
                continue
            self.assertLessEqual(chosen_active, a)

    @FUZZ
    @given(
        deadline_offset=st.floats(
            min_value=-5.0, max_value=5.0,
            allow_nan=False, allow_infinity=False,
        ),
        blocked=st.booleans(),
        sleep_for=st.floats(
            min_value=0.0, max_value=10.0,
            allow_nan=False, allow_infinity=False,
        ),
    )
    def test_dispatch_wait_remaining_bounded(
        self, deadline_offset, blocked, sleep_for
    ):
        import time as _t
        deadline = _t.monotonic() + deadline_offset
        out = LinkQueueApp._dispatch_wait_remaining(deadline, blocked, sleep_for)
        self.assertIsInstance(out, float)
        # Always non-negative.
        self.assertGreaterEqual(out, 0.0)
        # Never exceeds the original "remaining" budget.
        self.assertLessEqual(out, max(0.0, deadline - _t.monotonic()) + 1e-3)
        # If blocked and a positive sleep_for is given, output is capped by it.
        if blocked and sleep_for > 0 and out > 0:
            self.assertLessEqual(out, sleep_for + 1e-3)

    @FUZZ
    @given(
        urls=st.lists(url_strategy, min_size=0, max_size=10),
        running_urls=st.lists(url_strategy, min_size=0, max_size=4),
        target=url_strategy,
    )
    def test_duplicate_status_tri_state(self, urls, running_urls, target):
        app = _dispatcher()
        # _duplicate_status reads queue_items.urls, so use a real _PendingQueue.
        app.queue_items = link_queue._PendingQueue(
            (QueueItem(url=u, protocol="x", template=DEFAULT_TPL, shell=False)
             for u in urls),
            domain_fn=LinkQueueApp._domain_of,
        )
        app.current_items = {
            i: QueueItem(url=u, protocol="x", template=DEFAULT_TPL, shell=False)
            for i, u in enumerate(running_urls)
        }
        target_item = QueueItem(
            url=target, protocol="x", template=DEFAULT_TPL, shell=False
        )
        out = LinkQueueApp._duplicate_status(app, target_item)
        self.assertIn(out, (None, "pending", "running"))
        # Cross-check with reality.
        if any(u == target for u in urls):
            self.assertEqual(out, "pending")
        elif any(u == target for u in running_urls):
            self.assertEqual(out, "running")
        else:
            self.assertIsNone(out)

    @FUZZ
    @given(
        urls=st.lists(url_strategy, min_size=0, max_size=20, unique=True),
        sel_urls=st.lists(url_strategy, min_size=0, max_size=20),
    )
    def test_remove_pending_urls_well_formed(self, urls, sel_urls):
        # rel-01 replaced the row-index arithmetic with URL-keyed removal.
        # Property: _remove_pending_urls removes exactly the pending items
        # whose URL was selected, leaves the rest in order, and never errors.
        import threading as _th
        app = _dispatcher()
        app.queue_lock = _th.Lock()
        app._dispatch_cv = _th.Condition(app.queue_lock)
        app.queue_items = link_queue._PendingQueue(
            (
                QueueItem(
                    url=u, protocol="x", template=DEFAULT_TPL, shell=False
                )
                for u in urls
            ),
            domain_fn=LinkQueueApp._domain_of,
        )
        want = set(sel_urls)
        before = list(app.queue_items)
        removed = LinkQueueApp._remove_pending_urls(
            app, [(url, ()) for url in sel_urls]
        )
        # Removed items are exactly the pending ones whose URL was requested.
        self.assertEqual({it.url for it in removed}, want & set(urls))
        # Survivors are the complement, preserving original order.
        self.assertEqual(
            app.queue_items, [it for it in before if it.url not in want]
        )

    @FUZZ
    @given(
        protocol=protocol_name_strategy,
        proto_cfg=st.one_of(
            st.none(),
            st.fixed_dictionaries({
                "mode": st.sampled_from(["queue", "immediate", "queue", ""]),
                "command": template_strategy,
                "shell": st.booleans(),
            }),
            st.dictionaries(weird_text, weird_text, max_size=3),
            st.text(max_size=20),  # not a dict at all
        ),
    )
    def test_resolve_protocol_returns_quad(self, protocol, proto_cfg):
        app = _StubApp()
        # Build a minimal config.
        protocols = {}
        if proto_cfg is not None and isinstance(proto_cfg, dict):
            protocols[protocol] = proto_cfg
        app.config = {
            "protocols": protocols,
            "default_mode": "queue",
            "default_command": DEFAULT_TPL,
            "default_shell": False,
        }
        # Must not crash.
        out = LinkQueueApp._resolve_protocol(app, protocol)
        self.assertEqual(len(out), 4)
        mode, cmd_tpl, shell, outcome = out
        self.assertIsInstance(mode, str)
        self.assertIsInstance(cmd_tpl, str)
        self.assertIsInstance(shell, bool)
        self.assertIn(outcome, ("queue", "immediate", "default", "", "queue"))


# ---------------------------------------------------------------------------
# Config schema fuzzing
# ---------------------------------------------------------------------------

class FuzzConfigSchema(unittest.TestCase):

    @FUZZ
    @given(
        key=st.sampled_from(tuple(
            key
            for key, value in link_queue.DEFAULT_CONFIG.items()
            if not isinstance(value, dict)
        )),
        value=st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.floats(allow_nan=True, allow_infinity=True),
            weird_text,
            st.lists(weird_text, max_size=3),
            st.dictionaries(weird_text, weird_text, max_size=3),
        ),
    )
    def test_every_default_scalar_normalizes_to_declared_type(self, key, value):
        import copy
        cfg = copy.deepcopy(link_queue.DEFAULT_CONFIG)
        cfg[key] = value

        LinkQueueApp._normalize_config_schema(cfg)

        default = link_queue.DEFAULT_CONFIG[key]
        if isinstance(default, bool):
            self.assertIsInstance(cfg[key], bool)
        elif isinstance(default, int):
            self.assertIsInstance(cfg[key], int)
            self.assertGreaterEqual(
                cfg[key],
                link_queue._CONFIG_INT_SCHEMA[key][1],
            )
        elif isinstance(default, str):
            self.assertIsInstance(cfg[key], str)
        if key in link_queue._CONFIG_ENUM_SCHEMA:
            self.assertIn(cfg[key], link_queue._CONFIG_ENUM_SCHEMA[key][1])

    @FUZZ
    @given(
        user=st.one_of(
            st.none(),
            st.text(max_size=20),
            st.lists(st.integers(), max_size=3),
            st.dictionaries(
                weird_text,
                st.one_of(
                    weird_text,
                    st.booleans(),
                    st.integers(),
                    st.dictionaries(weird_text, weird_text, max_size=3),
                ),
                max_size=8,
            ),
        ),
    )
    def test_merge_user_config_tolerates_anything(self, user):
        import copy
        cfg = copy.deepcopy(link_queue.DEFAULT_CONFIG)
        # Should never crash, regardless of how malformed `user` is.
        LinkQueueApp._merge_user_config(cfg, user)
        # Built-in protocols must still be a dict afterwards.
        self.assertIsInstance(cfg["protocols"], dict)

    @FUZZ
    @given(
        protocols=st.dictionaries(
            weird_text,
            st.one_of(
                weird_text,
                st.integers(),
                st.dictionaries(weird_text, weird_text, max_size=4),
            ),
            max_size=6,
        ),
    )
    def test_normalize_config_schema_drops_non_dict_protocols(self, protocols):
        cfg = {"protocols": dict(protocols)}
        LinkQueueApp._normalize_config_schema(cfg)
        # Every surviving protocol value is a dict with the required keys.
        for name, pc in cfg["protocols"].items():
            self.assertIsInstance(pc, dict)
            self.assertIn("mode", pc)
            self.assertIn("command", pc)
            self.assertIn("shell", pc)
            self.assertIn(pc["mode"], ("queue", "immediate"))
            self.assertIsInstance(pc["command"], str)
            self.assertIsInstance(pc["shell"], bool)
        self.assertIn("default_shell", cfg)
        self.assertIsInstance(cfg["default_shell"], bool)


# ---------------------------------------------------------------------------
# State-file load fuzzing  (touches the filesystem in a tempdir)
# ---------------------------------------------------------------------------

class FuzzStateFileLoad(unittest.TestCase):
    """Hammer _load_state_items with random YAML/junk and assert it never
    raises and always returns the documented (in_flight, pending) shape."""

    def setUp(self):
        # Save and replace the module-level STATE_FILE so the helper reads
        # from a tempfile we control.
        self._tmpdir = tempfile.mkdtemp(prefix="lq-fuzz-")
        self._saved_state_file = link_queue.STATE_FILE
        link_queue.STATE_FILE = os.path.join(self._tmpdir, "state.yaml")
        # _load_state_items prints '[warn] ...' to stderr on parse errors —
        # which is exactly the path we're trying to hammer. Mute it for the
        # duration of the test so the output stays readable.
        self._saved_stderr = sys.stderr
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    def tearDown(self):
        try:
            sys.stderr.close()
        finally:
            sys.stderr = self._saved_stderr
        link_queue.STATE_FILE = self._saved_state_file
        try:
            for f in Path(self._tmpdir).iterdir():
                f.unlink()
            os.rmdir(self._tmpdir)
        except Exception:
            pass

    @FUZZ
    @given(blob=st.binary(max_size=4096))
    def test_random_bytes_in_state_file_safe(self, blob):
        with open(link_queue.STATE_FILE, "wb") as f:
            f.write(blob)
        app = _dispatcher(link_queue.STATE_FILE)
        in_flight, pending = app._load_state_items()
        self.assertIsInstance(in_flight, list)
        self.assertIsInstance(pending, list)
        for it in in_flight + pending:
            self.assertIsInstance(it, QueueItem)
            self.assertIsInstance(it.url, str)
            self.assertTrue(it.url)  # parser drops empty-url entries

    @FUZZ
    @given(
        queue=st.lists(
            st.one_of(
                st.none(),
                st.text(max_size=10),
                st.integers(),
                st.fixed_dictionaries({
                    "url": st.one_of(
                        st.text(max_size=40), st.none(), st.integers()),
                    "protocol": st.text(max_size=10),
                    "template": st.text(max_size=40),
                    "shell": st.one_of(
                        st.booleans(), st.integers(), st.text(max_size=4)),
                }),
            ),
            max_size=10,
        ),
        in_flight=st.one_of(
            st.none(),
            st.text(max_size=10),
            st.lists(
                st.fixed_dictionaries({
                    "url": st.text(max_size=20),
                    "protocol": st.text(max_size=10),
                    "template": st.text(max_size=20),
                    "shell": st.booleans(),
                }),
                max_size=5,
            ),
        ),
    )
    def test_well_formed_yaml_with_junky_entries_safe(self, queue, in_flight):
        import yaml
        data = {"queue": queue, "in_flight": in_flight}
        with open(link_queue.STATE_FILE, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        app = _dispatcher(link_queue.STATE_FILE)
        out_in_flight, out_pending = app._load_state_items()
        for it in out_in_flight + out_pending:
            self.assertIsInstance(it, QueueItem)
            self.assertIsInstance(it.url, str)
            self.assertTrue(it.url)
            self.assertIsInstance(it.shell, bool)


# ---------------------------------------------------------------------------
# International / encoding fuzzing
# ---------------------------------------------------------------------------
# Three classes of input matter here, and each is hammered separately:
#
#   1. Native unicode str inputs (Python's internal repr is always
#      codepoint-based, so the encoding question is really "do downstream
#      stages — urlparse, shlex, YAML I/O — preserve the text?").
#   2. Punycode/IDN URLs (host part is ASCII, but the *real* host is
#      non-ASCII — _domain_of must not drop or normalize it incorrectly).
#   3. Bytes that AREN'T utf-8 (utf-16, utf-32, cp1251, gb18030, …) —
#      the state file must not crash when handed any of those.

# Encoded byte strategies: we encode unicode text into a non-utf-8 codec
# so the bytes-on-disk look completely different from utf-8. The state
# loader opens with encoding="utf-8" and is expected to fail GRACEFULLY
# (returning empty lists, no exception leaking out).
_NON_UTF8_CODECS = ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be",
                    "cp1251", "cp1252", "cp936", "gb18030", "shift_jis",
                    "big5", "latin-1", "iso-8859-7")


def _encode_or_skip(text: str, codec: str) -> bytes | None:
    """Encode `text` in `codec`, returning None if a character isn't
    representable (e.g. a CJK glyph in cp1251). The fuzzer skips on None."""
    try:
        return text.encode(codec)
    except (UnicodeEncodeError, LookupError):
        return None


class FuzzInternationalEncodings(unittest.TestCase):
    """URL inputs in Cyrillic/CJK/Arabic/etc., punycode, percent-encoded
    UTF-8, plus state-file bytes in UTF-16/UTF-32/cp1251/gb18030/…."""

    def setUp(self):
        # Same plumbing as FuzzStateFileLoad — let us point STATE_FILE at
        # a tempfile and silence the [warn] noise from the loader's
        # graceful-failure path.
        self._tmpdir = tempfile.mkdtemp(prefix="lq-fuzz-i18n-")
        self._saved_state_file = link_queue.STATE_FILE
        link_queue.STATE_FILE = os.path.join(self._tmpdir, "state.yaml")
        self._saved_stderr = sys.stderr
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    def tearDown(self):
        try:
            sys.stderr.close()
        finally:
            sys.stderr = self._saved_stderr
        link_queue.STATE_FILE = self._saved_state_file
        try:
            for f in Path(self._tmpdir).iterdir():
                f.unlink()
            os.rmdir(self._tmpdir)
        except Exception:
            pass

    # --- chaotic-input regression tests -------------------------------------
    # These re-add the coverage of "URL syntax is broken / hostile" inputs
    # that we removed when we constrained the strategy. The contract here
    # is weaker (no crash + graceful fallback) — but it's the contract
    # production code actually promises, so this is what we should test.

    def test_extract_protocol_bracket_host_falls_back_gracefully(self):
        """Regression for the case Hypothesis surfaced earlier:
        `https://[.test/Ѐ` — `[` triggers urlparse's IPv6-bracket path
        and the URL doesn't parse cleanly. Documented behaviour:
        `_extract_protocol` returns '' (empty), `_resolve_protocol`
        falls through to default_mode + default_command + warning."""
        url = "https://[.test/Ѐ"
        proto = LinkQueueApp._extract_protocol(url)
        self.assertEqual(proto, "")  # graceful fallback
        # Now confirm the downstream wiring also handles the empty
        # protocol gracefully — no crash, fallthrough to default.
        app = _StubApp()
        app.config = {
            "protocols": {},
            "default_mode": "queue",
            "default_command": DEFAULT_TPL,
            "default_shell": False,
        }
        mode, cmd, shell, outcome = LinkQueueApp._resolve_protocol(app, proto)
        self.assertEqual(mode, "queue")
        self.assertEqual(cmd, DEFAULT_TPL)
        self.assertFalse(shell)
        self.assertEqual(outcome, "default")

    @FUZZ
    @given(url=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1, max_size=80,
    ))
    def test_chaotic_url_helpers_never_crash(self, url):
        """Genuinely-malformed URLs (anything goes — embedded brackets,
        nul bytes, multi-`://`, raw control characters, mixed scripts).
        We assert the documented graceful contract: each helper either
        returns a valid str / list, or raises a documented exception."""
        # _extract_protocol: any str -> str (possibly empty), no exception.
        proto = LinkQueueApp._extract_protocol(url)
        self.assertIsInstance(proto, str)
        # _domain_of: always non-empty (falls back to scheme or _unknown).
        domain = LinkQueueApp._domain_of(url)
        self.assertIsInstance(domain, str)
        self.assertTrue(domain)
        # _resolve_command: always returns a str (best-effort).
        out = LinkQueueApp._resolve_command(DEFAULT_TPL, url, proto)
        self.assertIsInstance(out, str)
        # DEFAULT_TPL is valid shell syntax, so arbitrary URL content cannot
        # make parsing fail: template splitting happens before substitution.
        argv = LinkQueueApp._build_argv(DEFAULT_TPL, url, proto)
        self.assertIsInstance(argv, list)
        for a in argv:
            self.assertIsInstance(a, str)

    # --- pure helpers on international URLs ---------------------------------

    @FUZZ
    @given(url=international_url_strategy)
    def test_extract_protocol_unicode_url(self, url):
        """Cyrillic/CJK/Arabic in host or path must not break protocol
        extraction — the scheme is ASCII anyway."""
        out = LinkQueueApp._extract_protocol(url)
        self.assertIsInstance(out, str)
        # All international_url_strategy outputs are https-scheme.
        self.assertEqual(out, "https")

    @FUZZ
    @given(url=international_url_strategy)
    def test_domain_of_unicode_url(self, url):
        """`_domain_of` must produce a non-empty, lowercased, www-stripped
        domain string for any unicode URL."""
        out = LinkQueueApp._domain_of(url)
        self.assertIsInstance(out, str)
        self.assertTrue(out)
        self.assertEqual(out, out.lower())
        self.assertFalse(out.startswith("www."))

    @FUZZ
    @given(url=international_url_strategy, tpl=template_strategy)
    def test_resolve_command_unicode_url(self, url, tpl):
        """Template substitution must not corrupt unicode codepoints."""
        out = LinkQueueApp._resolve_command(tpl, url, "https")
        self.assertIsInstance(out, str)
        # If the template contained `{url}`, the URL substring must
        # appear verbatim in the output. A buggy substitution that went
        # through bytes (e.g. .encode().decode()) would mangle non-ASCII.
        # Be conservative — only assert verbatim presence when {url}
        # appears AND the template has no other braces (e.g.
        # {url_quoted}, {nope}) that could interfere with the
        # expectation.
        has_only_plain_url = (
            PH_URL in tpl
            and PH_URL_QUOTED not in tpl
            and "{nope}" not in tpl
        )
        if has_only_plain_url:
            self.assertIn(url, out)

    @FUZZ
    @given(url=international_url_strategy)
    def test_build_argv_unicode_url(self, url):
        """`_build_argv` must keep unicode URLs in their own argv slot
        with the codepoints intact (proves it didn't bytes-roundtrip)."""
        argv = LinkQueueApp._build_argv("echo {url}", url, "https")
        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[0], "echo")
        self.assertEqual(argv[1], url)

    @FUZZ
    @given(url=international_url_strategy)
    def test_resolve_command_quoted_is_shell_safe(self, url):
        """`{url_quoted}` must produce a shlex-quoted form that
        round-trips through shlex.split as a single token equal to the
        original url — even when the URL contains non-ASCII / quote
        characters / spaces."""
        import shlex
        out = LinkQueueApp._resolve_command(PH_URL_QUOTED, url, "https")
        # shlex.split must give back exactly the original url.
        parts = shlex.split(out)
        self.assertEqual(parts, [url])

    # --- state-file round trip in utf-8 -------------------------------------

    @FUZZ
    @given(url=international_url_strategy)
    def test_state_file_utf8_roundtrip(self, url):
        """An international URL serialised through the production save path
        (_serialize_item, which base64-wraps any string the active YAML
        backend can't emit — rel-03) must come back identical on load.
        Dumps via the production _yaml_dump so the emittability decision in
        _serialize_item matches the dumper actually used."""
        entry = LinkQueueApp._serialize_item(
            QueueItem(url=url, protocol="https", template=DEFAULT_TPL, shell=False)
        )
        data = {"queue": [entry], "in_flight": []}
        with open(link_queue.STATE_FILE, "w", encoding="utf-8") as f:
            link_queue._yaml_dump(data, f, allow_unicode=True)
        app = _dispatcher(link_queue.STATE_FILE)
        in_flight, pending = app._load_state_items()
        self.assertEqual(in_flight, [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].url, url)

    # --- non-utf-8 state-file bytes -----------------------------------------

    @FUZZ
    @given(text=international_text,
           codec=st.sampled_from(_NON_UTF8_CODECS))
    def test_state_file_non_utf8_bytes_safe(self, text, codec):
        """Hand the loader bytes encoded in UTF-16/UTF-32/cp1251/gb18030/
        etc. The loader opens the file as utf-8, so it MUST fail
        gracefully (return empty lists, no exception)."""
        encoded = _encode_or_skip(text, codec)
        if encoded is None:
            return
        # Wrap in a YAML-ish frame so we exercise both the decode path
        # AND (occasionally) the YAML-parse path on the rare codec where
        # the bytes happen to be valid utf-8.
        framed = (b"queue:\n  - url: " + encoded + b"\n    protocol: https\n"
                  b"    template: echo {url}\n    shell: false\n")
        with open(link_queue.STATE_FILE, "wb") as f:
            f.write(framed)
        app = _dispatcher(link_queue.STATE_FILE)
        # Documented contract: any failure -> ([], [])
        in_flight, pending = app._load_state_items()
        self.assertIsInstance(in_flight, list)
        self.assertIsInstance(pending, list)
        for it in in_flight + pending:
            self.assertIsInstance(it, QueueItem)
            self.assertIsInstance(it.url, str)

    # --- BOM-prefixed utf-8 (and other BOMs) --------------------------------

    @FUZZ
    @given(url=international_url_strategy,
           bom=st.sampled_from([
               b"\xef\xbb\xbf",          # utf-8 BOM
               b"\xff\xfe",              # utf-16-le BOM
               b"\xfe\xff",              # utf-16-be BOM
               b"\xff\xfe\x00\x00",      # utf-32-le BOM
               b"\x00\x00\xfe\xff",      # utf-32-be BOM
           ]))
    def test_state_file_with_bom_safe(self, url, bom):
        """Files saved by Notepad-on-Windows often carry a BOM. Whatever
        the loader does (parse despite, or fail-soft), it must not
        raise."""
        import yaml
        body = yaml.safe_dump(
            {"queue": [{"url": url, "protocol": "https",
                         "template": DEFAULT_TPL, "shell": False}],
             "in_flight": []},
            allow_unicode=True,
        ).encode("utf-8")
        with open(link_queue.STATE_FILE, "wb") as f:
            f.write(bom + body)
        app = _dispatcher(link_queue.STATE_FILE)
        in_flight, pending = app._load_state_items()
        self.assertIsInstance(in_flight, list)
        self.assertIsInstance(pending, list)


# ---------------------------------------------------------------------------
# Helpers used by the fuzzers
# ---------------------------------------------------------------------------

def _dispatcher(state_path=None):
    return link_queue.Dispatcher.headless(
        state_path=state_path, acquire_state_lock=False
    )


class _StubApp(LinkQueueApp):
    """Bare-minimum stand-in for LinkQueueApp.  Inheriting gives us all
    the pure / static / class helpers (_domain_of, _highest_pct_in,
    _milestones_crossed, _resolve_command, ...) for free, while
    allowing us to skip the Tk-bound __init__ and only hand-build the
    attributes the unit under test actually reads."""

    # Skip the real __init__ (it builds a Tk window). Subclasses can still
    # call helper methods on themselves — they just won't have UI state.
    def __init__(self) -> None:  # noqa: D401, super-init-not-called
        # Intentionally do NOT call super().__init__(); we want the
        # subclass-of-LinkQueueApp dispatch behavior without the UI.
        pass

    def _log(self, *_args, **_kwargs) -> None:
        """No-op log — fuzz tests don't care about UI output."""
        return None


class _Rand:
    """Tiny LCG so the test stays deterministic given a hypothesis seed,
    without pulling in `random` (whose state could leak between examples)."""

    def __init__(self, seed: int) -> None:
        self.s = seed & 0xFFFFFFFF

    def next(self) -> int:
        self.s = (self.s * 1103515245 + 12345) & 0x7FFFFFFF
        return self.s


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
