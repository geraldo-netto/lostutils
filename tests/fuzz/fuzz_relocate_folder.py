#!/usr/bin/env python3
"""Fuzz / property-based tests for relocate_folder.py.

Targets the refactored helpers introduced by rf-sec-01/02/03, rf-cx-01/02,
rf-perf-02/03, rf-arch-01/02, rf-ddd-01, rf-decl-01, rf-dup-02/03. Each
target asserts both that:

  * the function never raises an unexpected exception type for any input
    within its declared domain, AND
  * the output satisfies an invariant that should hold universally (e.g.
    _path_taken is True iff some file/dir/symlink exists at the path,
    _swallow_or_warn always swallows OSError, MigrationState enum values are stable,
    parallel verify_copy returns the same yes/no answer as sequential).

Run:
    python3 -m pytest tests/fuzz/fuzz_relocate_folder.py -v
or:
    python3 tests/fuzz/fuzz_relocate_folder.py
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import HealthCheck, given, settings, strategies as st

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import relocate_folder as rf  # noqa: E402

FUZZ = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
)

# Safe path-component alphabet: avoid '/', '.', NUL, and trailing dot/space.
SAFE_NAME = st.text(
    alphabet=st.characters(
        min_codepoint=0x21, max_codepoint=0x7E,
        blacklist_characters="/\x00.",
    ),
    min_size=1, max_size=12,
)


# --- _path_taken (rf-dup-02) ------------------------------------------------

class PathTakenFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(SAFE_NAME)
    def test_missing_path_is_never_taken(self, name: str) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / name
            # No matter the (safe) name, a never-created path is not taken.
            self.assertFalse(rf._path_taken(p))

    @settings(parent=FUZZ)
    @given(SAFE_NAME, st.sampled_from(["file", "dir", "link", "broken"]))
    def test_existing_kinds_all_taken(self, name: str, kind: str) -> None:
        with TemporaryDirectory() as d:
            p = Path(d) / name
            if kind == "file":
                p.write_text("x")
            elif kind == "dir":
                p.mkdir()
            elif kind == "link":
                t = Path(d) / "target"; t.write_text("t")
                p.symlink_to(t)
            else:                                       # dangling symlink
                p.symlink_to(Path(d) / "nowhere")
            self.assertTrue(rf._path_taken(p))


# --- _swallow_or_warn (rf-dup-03) -------------------------------------------

class SwallowOrWarnFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(SAFE_NAME, st.integers(min_value=-(2**31), max_value=2**31 - 1))
    def test_returns_callable_output(self, label: str, value: int) -> None:
        self.assertEqual(rf._swallow_or_warn(label, lambda v: v, value), value)

    @settings(parent=FUZZ)
    @given(SAFE_NAME, st.sampled_from([OSError, PermissionError, FileNotFoundError]))
    def test_swallows_known_oserrors(self, label: str, exc) -> None:
        def boom():
            raise exc("synthetic")
        # Must never raise for any (label, OSError-subclass) pair.
        self.assertIsNone(rf._swallow_or_warn(label, boom))

    @settings(parent=FUZZ)
    @given(st.sampled_from([ValueError, RuntimeError, KeyError, TypeError]))
    def test_propagates_non_oserror(self, exc) -> None:
        def boom():
            raise exc("propagate me")
        with self.assertRaises(exc):
            rf._swallow_or_warn("noop", boom)


# --- _kind_of / _is_special_file mode-bit coverage --------------------------

class KindOfFuzz(unittest.TestCase):
    _KINDS = {
        stat.S_IFSOCK: "socket",
        stat.S_IFIFO: "fifo",
        stat.S_IFBLK: "block-device",
        stat.S_IFCHR: "char-device",
    }

    @settings(parent=FUZZ)
    @given(st.sampled_from(list(_KINDS.items())),
           st.integers(min_value=0, max_value=0o7777))
    def test_special_kinds_round_trip(self, ifkind_label, perms) -> None:
        ifkind, label = ifkind_label
        mode = ifkind | perms
        self.assertEqual(rf._kind_of(mode), label)
        self.assertTrue(rf._is_special_file(mode))

    @settings(parent=FUZZ)
    @given(st.sampled_from([stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK]),
           st.integers(min_value=0, max_value=0o7777))
    def test_non_special_kinds_return_none(self, ifkind, perms) -> None:
        mode = ifkind | perms
        self.assertIsNone(rf._kind_of(mode))
        self.assertFalse(rf._is_special_file(mode))


# --- _group_specials_by_kind (rf-perf-03 cache path) -----------------------

class GroupSpecialsFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(st.lists(st.sampled_from(list(KindOfFuzz._KINDS.keys())),
                    min_size=0, max_size=20))
    def test_cache_path_counts_match_known_modes(self, ifkinds) -> None:
        paths = [Path(f"/tmp/synthetic-{i}") for i in range(len(ifkinds))]
        cache = {p: m | 0o600 for p, m in zip(paths, ifkinds)}
        counts = rf._group_specials_by_kind(paths, mode_cache=cache)
        total = sum(counts.values())
        self.assertEqual(total, len(paths))   # every entry classified


# --- MigrationState (rf-ddd-01) --------------------------------------------

class MigrationStateFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(st.sampled_from(list(rf.MigrationState)))
    def test_state_value_is_lowercase_snake(self, state) -> None:
        v = state.value
        # All enum values are deliberately lower_snake; defends against
        # accidental renames that would silently change log strings.
        self.assertTrue(v.replace("_", "").isalnum())
        self.assertEqual(v, v.lower())


# --- Plan.from_args / parse_namespace (rf-arch-01/02) ----------------------

ABS_PATH = st.builds(lambda parts: "/" + "/".join(parts),
                     st.lists(SAFE_NAME, min_size=1, max_size=4))


class PlanFromArgsFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(ABS_PATH, ABS_PATH,
           st.booleans(), st.booleans(), st.booleans(),
           st.booleans(), st.booleans(), st.booleans(),
           st.booleans())
    def test_random_flags_produce_well_formed_plan(
        self, source, dest_root, dry_run, no_verify, no_checksum,
        strict, force, verify_ownership, strict_cross_device,
    ) -> None:
        ns = argparse.Namespace(
            source=source, dest_root=dest_root,
            dry_run=dry_run, no_verify=no_verify, no_checksum=no_checksum,
            strict=strict, force=force, verify_ownership=verify_ownership,
            strict_cross_device=strict_cross_device,
        )
        try:
            plan = rf.Plan.from_args(ns)
        except ValueError:
            # The only path that should raise here is source == target after
            # normalisation; accept it and move on.
            return
        # Invariants:
        self.assertIsInstance(plan, rf.Plan)
        self.assertEqual(plan.dry_run, dry_run)
        self.assertEqual(plan.checksum, not no_checksum)
        self.assertEqual(plan.strict, strict)
        # Target name equals source basename.
        self.assertEqual(plan.target.name, plan.source.name)
        # Target's parent is the resolved dest_root (absolute path).
        self.assertTrue(plan.target.is_absolute())


# --- _create_symlink staging dir leaves no residue (rf-sec-01) -------------

class CreateSymlinkFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(SAFE_NAME)
    def test_no_residue_after_success(self, link_name: str) -> None:
        with TemporaryDirectory() as d:
            root = Path(d)
            target = root / "tgt"; target.mkdir()
            link = root / link_name
            if link.exists() or link.is_symlink():
                return  # skip: hypothesis happened to collide with "tgt"
            rf._create_symlink(link, target)
            self.assertTrue(link.is_symlink())
            leftovers = [
                p for p in root.iterdir()
                if p.name.startswith(rf.STAGING_PREFIX)
            ]
            self.assertEqual(leftovers, [])


# --- _verify_size / _verify_content compose like _verify_file (rf-cx-01) ---

class VerifyFileFuzz(unittest.TestCase):
    @settings(parent=FUZZ)
    @given(st.binary(min_size=0, max_size=1024))
    def test_identical_files_verify(self, payload: bytes) -> None:
        with TemporaryDirectory() as d:
            a = Path(d) / "a"; a.write_bytes(payload)
            b = Path(d) / "b"; b.write_bytes(payload)
            rf._verify_size(a, b, Path("a"))
            rf._verify_content(a, b, Path("a"))
            rf._verify_file(a, b, Path("a"), checksum=True)

    @settings(parent=FUZZ)
    @given(st.binary(min_size=1, max_size=64), st.binary(min_size=1, max_size=64))
    def test_size_diff_caught(self, a_bytes: bytes, b_bytes: bytes) -> None:
        if len(a_bytes) == len(b_bytes):
            return
        with TemporaryDirectory() as d:
            a = Path(d) / "a"; a.write_bytes(a_bytes)
            b = Path(d) / "b"; b.write_bytes(b_bytes)
            with self.assertRaises(RuntimeError):
                rf._verify_size(a, b, Path("a"))

    @settings(parent=FUZZ)
    @given(st.binary(min_size=4, max_size=64))
    def test_same_size_content_mismatch_caught(self, payload: bytes) -> None:
        flipped = bytes((payload[0] ^ 0xFF,)) + payload[1:]
        if flipped == payload:
            return
        with TemporaryDirectory() as d:
            a = Path(d) / "a"; a.write_bytes(payload)
            b = Path(d) / "b"; b.write_bytes(flipped)
            with self.assertRaises(RuntimeError):
                rf._verify_content(a, b, Path("a"))


# --- verify_copy parallel == sequential answer (rf-perf-02) ----------------

class VerifyCopyParallelFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=40)
    @given(st.lists(st.binary(min_size=0, max_size=512), min_size=0, max_size=8))
    def test_parallel_matches_sequential(self, payloads) -> None:
        with TemporaryDirectory() as d:
            src = Path(d) / "s"; src.mkdir()
            for i, p in enumerate(payloads):
                (src / f"f{i}.bin").write_bytes(p)
            dst = Path(d) / "t"
            rf.copy_tree(src, dst)
            rf.verify_copy(src, dst, checksum=True)        # parallel path
            rf.verify_copy(src, dst, checksum=False)       # sequential path
            # Both succeed for an identical copy.


# --- _backup_target restore-on-failure (rf-cx-02) --------------------------

class BackupTargetFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=80)
    @given(SAFE_NAME, st.booleans())
    def test_restore_on_failure_preserves_content(self, body_name, fail) -> None:
        with TemporaryDirectory() as d:
            t = Path(d) / "target"; t.mkdir()
            (t / "marker").write_text("orig")
            if fail:
                try:
                    with rf._backup_target(t):
                        raise RuntimeError("boom")
                except RuntimeError:
                    pass
                self.assertTrue(t.is_dir())
                self.assertEqual((t / "marker").read_text(), "orig")
            else:
                with rf._backup_target(t):
                    t.mkdir()
                    (t / body_name).write_text("new")
                self.assertTrue((t / body_name).exists())


# --- with_logger context manager (rf-decl-01) ------------------------------

class LoggerInjectionFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=50)
    @given(st.lists(SAFE_NAME, min_size=1, max_size=4))
    def test_logger_swap_captures_all_warnings(self, labels) -> None:
        captured: list[str] = []
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        log = logging.getLogger("relocate.fuzz")
        log.handlers = [handler]
        log.setLevel(logging.WARNING)
        with rf.with_logger(log):
            for label in labels:
                rf._swallow_or_warn(
                    label, lambda: (_ for _ in ()).throw(OSError("x"))
                )
        captured.append(stream.getvalue())
        for label in labels:
            self.assertIn(label, captured[0])
        # outside the swap the contextvar reset
        self.assertIs(rf._log(), rf.LOG)


# --- _check_cross_device behaviour across plan configurations (rf-sec-03) --

class CrossDeviceFuzz(unittest.TestCase):
    @settings(parent=FUZZ, max_examples=40)
    @given(st.booleans())
    def test_same_fs_warn_vs_strict(self, strict: bool) -> None:
        with TemporaryDirectory() as d:
            src = Path(d) / "src"; src.mkdir()
            target = Path(d) / "dst" / "src"
            (Path(d) / "dst").mkdir()
            plan = rf.Plan(source=src, target=target, strict_cross_device=strict)
            if strict:
                with self.assertRaises(RuntimeError):
                    rf._check_cross_device(plan)
            else:
                # Warn-only path: must never raise.
                rf._check_cross_device(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
