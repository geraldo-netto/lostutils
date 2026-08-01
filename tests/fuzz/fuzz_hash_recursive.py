#!/usr/bin/env python3
"""Fuzz / property-based tests for hash-recursive-ai5.py.

Targets the pure pipeline helpers: index_inodes, size_collision_candidates,
_readable_rep, hash_head, hash_tail_and_samples, find_duplicate_groups,
emit_groups. Each property asserts an invariant that should hold for any
input within the function's domain.

Run:
    python3 -m pytest tests/fuzz/fuzz_hash_recursive.py -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import HealthCheck, given, settings, strategies as st

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
_HR_PATH = REPO / "hash-recursive-ai5.py"
_spec = importlib.util.spec_from_file_location("hash_recursive_ai5", _HR_PATH)
hr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hr)

FUZZ = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
)


# --- index_inodes invariants -----------------------------------------------

@st.composite
def file_tuples(draw):
    n = draw(st.integers(min_value=0, max_value=20))
    out = []
    for i in range(n):
        size = draw(st.integers(min_value=0, max_value=10000))
        dev = draw(st.integers(min_value=1, max_value=3))
        ino = draw(st.integers(min_value=1, max_value=10))
        out.append((f"/p/{i}.bin", size, dev, ino))
    return out


class IndexInodesFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(file_tuples())
    def test_aliases_keys_match_inode_size_keys(self, files):
        aliases, inode_size = hr.index_inodes(files)
        # Every (dev,ino) in inode_size has a non-empty alias list.
        self.assertEqual(set(aliases), set(inode_size))
        for key, paths in aliases.items():
            self.assertGreater(len(paths), 0)

    @settings(parent=FUZZ)
    @given(file_tuples())
    def test_size_collision_candidates_are_subset(self, files):
        _, inode_size = hr.index_inodes(files)
        candidates = hr.size_collision_candidates(inode_size)
        cand_keys = {key for _, key in candidates}
        # Every candidate key was in the inode_size dict.
        self.assertTrue(cand_keys <= set(inode_size))
        # All candidates correspond to a size that appears at least twice.
        sizes = [s for s, _ in candidates]
        # No singletons survive the filter.
        for size, _ in candidates:
            self.assertGreaterEqual(sizes.count(size), 2)


# --- _readable_rep returns a member of the input ---------------------------

_SAFE_PATH_CHARS = st.characters(
    min_codepoint=0x21, max_codepoint=0x7E,
    blacklist_characters="/\x00",
)

_RECORD_PATHS = st.one_of(
    st.text(alphabet=_SAFE_PATH_CHARS, min_size=1, max_size=8),
    st.builds(
        lambda marker, tail: marker + tail,
        st.sampled_from(("line\nbreak", "line\rbreak", "@lostutils-json:")),
        st.text(alphabet=_SAFE_PATH_CHARS, max_size=8),
    ),
)


class ReadableRepFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(st.lists(st.text(alphabet=_SAFE_PATH_CHARS, min_size=1, max_size=20),
                    min_size=1, max_size=10))
    def test_rep_is_in_input(self, paths):
        # NUL and slash are excluded so every generated value is a legal
        # synthetic path component on the supported filesystems.
        rep = hr._readable_rep(paths)
        self.assertIn(rep, paths)


# --- hash_head / hash_tail_and_samples on real bytes ----------------------

class HashIdempotenceFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=50)
    @given(st.binary(min_size=0, max_size=4096))
    def test_hash_head_deterministic(self, payload):
        with TemporaryDirectory() as d:
            p = Path(d) / "f.bin"
            p.write_bytes(payload)
            h1 = hr.hash_head(str(p))
            h2 = hr.hash_head(str(p))
            self.assertEqual(h1, h2)

    @settings(parent=FUZZ, max_examples=50)
    @given(st.binary(min_size=0, max_size=4096))
    def test_hash_head_distinct_payload_distinct_hash_or_short(self, payload):
        with TemporaryDirectory() as d:
            a = Path(d) / "a.bin"; a.write_bytes(payload)
            flipped = bytes((payload[0] ^ 0xFF,)) + payload[1:] if payload else b"\x00"
            if flipped == payload:
                return
            b = Path(d) / "b.bin"; b.write_bytes(flipped)
            # Different bytes within the head window must yield different hashes.
            self.assertNotEqual(hr.hash_head(str(a)), hr.hash_head(str(b)))


# --- find_duplicate_groups identifies real duplicates ---------------------

class FindDuplicatesFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=30)
    @given(st.integers(min_value=2, max_value=6),
           st.binary(min_size=10, max_size=512))
    def test_n_identical_files_form_one_group(self, n, payload):
        with TemporaryDirectory() as d:
            files = []
            for i in range(n):
                p = Path(d) / f"f{i}.bin"
                p.write_bytes(payload)
                st_ = os.stat(p)
                files.append((str(p), st_.st_size, st_.st_dev, st_.st_ino))
            result = hr.find_duplicate_groups(files, jobs=1)
            self.assertEqual(len(result.groups), 1)
            keys = next(iter(result.groups.values()))
            expected_keys = {(dev, ino) for _path, _size, dev, ino in files}
            self.assertEqual(set(keys), expected_keys)
            self.assertEqual(len(keys), n)
            paths = [path for key in keys for path in result.aliases[key]]
            self.assertEqual(set(paths), {path for path, *_rest in files})
            self.assertEqual(len(paths), n)

    @settings(parent=FUZZ, max_examples=80)
    @given(
        payload=st.binary(min_size=32, max_size=256),
        replacement=st.integers(min_value=0, max_value=255),
    )
    def test_sample_collision_never_emits_byte_distinct_group(
        self,
        payload,
        replacement,
    ):
        if payload[5] == replacement:
            return
        changed = bytearray(payload)
        changed[5] = replacement
        config = hr.RunConfig(block_size=4, sample_size=1)
        with TemporaryDirectory() as directory:
            files = []
            for name, content in (("a.bin", payload), ("b.bin", changed)):
                path = Path(directory) / name
                path.write_bytes(content)
                stat_result = path.stat()
                files.append(
                    (
                        str(path),
                        stat_result.st_size,
                        stat_result.st_dev,
                        stat_result.st_ino,
                    )
                )
            self.assertEqual(
                hr.hash_tail_and_samples(files[0][0], len(payload), config=config),
                hr.hash_tail_and_samples(files[1][0], len(payload), config=config),
            )

            result = hr.find_duplicate_groups(files, jobs=1, config=config)

            for keys in result.groups.values():
                contents = {
                    Path(path).read_bytes()
                    for key in keys
                    for path in result.aliases[key]
                }
                self.assertEqual(len(contents), 1)


# --- emit_groups never writes single-alias groups -------------------------

class EmitGroupsFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(st.data())
    def test_emit_round_trips_one_record_per_path(self, data):
        # Build coherent inputs: every key in final_groups MUST have a
        # matching entry in aliases — that's the pipeline contract.
        keys = data.draw(st.lists(
            st.tuples(st.integers(0, 5), st.integers(0, 5)),
            min_size=0, max_size=8, unique=True,
        ))
        aliases = {
            k: data.draw(st.lists(
                _RECORD_PATHS,
                min_size=1, max_size=4,
            ))
            for k in keys
        }
        digests = data.draw(st.lists(
            st.text(alphabet=_SAFE_PATH_CHARS, min_size=1, max_size=8),
            min_size=0, max_size=4, unique=True,
        ))
        final_groups = {
            d: data.draw(st.lists(st.sampled_from(keys), min_size=1, max_size=4))
            for d in digests
        } if keys else {}

        written: list[str] = []
        dup_groups, dup_paths = hr.emit_groups(final_groups, aliases, written.append)

        expected: list[tuple[str, str]] = []
        expected_groups = 0
        for digest, group_keys in final_groups.items():
            paths = [path for key in group_keys for path in aliases[key]]
            if len(paths) <= 1:
                continue
            expected_groups += 1
            expected.extend((hr._format_digest(digest), path) for path in paths)

        records: list[tuple[str, str]] = []
        for chunk in written:
            self.assertTrue(chunk.endswith("\n"))
            for line in chunk.splitlines():
                label, encoded_path = line.split(" ", 1)
                if encoded_path.startswith(hr._ESCAPED_PATH_PREFIX):
                    encoded_path = json.loads(
                        encoded_path[len(hr._ESCAPED_PATH_PREFIX):]
                    )
                records.append((label, encoded_path))

        self.assertEqual(dup_groups, expected_groups)
        self.assertEqual(dup_paths, len(expected))
        self.assertEqual(records, expected)


# --- threaded iterator on a real synthetic tree -----------------------------

class ThreadedWalkFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=20)
    @given(st.lists(st.integers(min_value=0, max_value=200),
                    min_size=0, max_size=10))
    def test_walk_finds_every_regular_file(self, sizes):
        with TemporaryDirectory() as d:
            root = Path(d)
            for i, sz in enumerate(sizes):
                (root / f"f{i}.bin").write_bytes(b"x" * sz)
            walk = hr.iter_threaded_walk(root, jobs=2)
            results = list(walk)
            stats = walk.stats
            self.assertEqual(stats["files"], len(sizes))
            self.assertEqual(len({p for p, *_ in results}), len(sizes))


# --- hr-obs-01: _fmt_count boundary fuzz ----------------------------------

class FmtCountFuzz(unittest.TestCase):
    """Boundary + property fuzz for `_fmt_count` (hr-obs-01)."""

    @settings(parent=FUZZ, max_examples=200)
    @given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
    def test_fmt_count_returns_nonempty_str(self, n: int) -> None:
        out = hr._fmt_count(n)
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 0)

    @settings(parent=FUZZ, max_examples=50)
    @given(st.integers(min_value=1, max_value=999_999))
    def test_fmt_count_small_is_bare_int(self, n: int) -> None:
        self.assertEqual(hr._fmt_count(n), str(n))

    @settings(parent=FUZZ, max_examples=30)
    @given(st.integers(min_value=-100, max_value=100))
    def test_fmt_count_signed_symmetric(self, n: int) -> None:
        if n == 0:
            return
        positive = hr._fmt_count(abs(n))
        negative = hr._fmt_count(-abs(n))
        self.assertEqual(negative, "-" + positive)


# --- hr-perf-04: _iter_batches boundary fuzz ------------------------------

class IterBatchesFuzz(unittest.TestCase):
    """Boundary fuzz: `_iter_batches` must yield every item exactly once."""

    @settings(parent=FUZZ, max_examples=120)
    @given(st.lists(st.integers(), min_size=0, max_size=200),
           st.integers(min_value=1, max_value=50))
    def test_every_item_yielded_once(self, items, batch_size) -> None:
        seen: list = []
        for chunk in hr._iter_batches(items, batch_size):
            self.assertLessEqual(len(chunk), batch_size)
            seen.extend(chunk)
        self.assertEqual(seen, items)


# --- hr-rel-12 boundary: _iter_batches yields fresh lists ----------------

class IterBatchesFreshLists(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=80)
    @given(st.lists(st.integers(), min_size=1, max_size=50),
           st.integers(min_value=1, max_value=20))
    def test_mutating_batch_does_not_corrupt_source(self, src, batch_size):
        original = list(src)
        for batch in hr._iter_batches(src, batch_size):
            batch.clear()           # mutate the yielded list
        self.assertEqual(src, original)   # source untouched


# --- hr-hyg-02 boundary: _expand_keys_to_paths sentinel always typed ----

class ExpandKeysSentinelTyped(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=40)
    @given(st.integers(min_value=1, max_value=200),   # n paths
           st.integers(min_value=1, max_value=100))   # cap
    def test_sentinel_only_when_truncated(self, n, cap):
        aliases = {("d", 0): [f"/p/{i}" for i in range(n)]}
        out = hr._expand_keys_to_paths([("d", 0)], aliases, cap=cap)
        if n > cap:
            self.assertEqual(len(out), cap + 1)
            self.assertIsInstance(out[-1], hr._MoreSentinel)
            self.assertEqual(hr._count_real_paths(out), cap)
        else:
            self.assertEqual(len(out), n)
            self.assertEqual(hr._count_real_paths(out), n)


if __name__ == "__main__":
    unittest.main(verbosity=2)
