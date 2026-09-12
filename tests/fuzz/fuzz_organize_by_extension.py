#!/usr/bin/env python3
"""Fuzz / property-based tests for organize_by_extension.py.

Targets the pure / disk-touching helpers: header sniffer, extension resolver,
prefix / bucket-name normalisers, and the bucket allocator. Each fuzz target
asserts that:

  * the function never raises an unexpected exception, AND
  * the output satisfies an invariant that should hold for *any* input
    (e.g. resolve_real_extension always returns a non-empty str,
    detect_type_by_header returns None or a known label, etc.).

Run:
    python3 -m pytest tests/fuzz/fuzz_organize_by_extension.py -v
or:
    python3 tests/fuzz/fuzz_organize_by_extension.py
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from hypothesis import HealthCheck, assume, given, settings, strategies as st

# Import target relative to repo root.
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import organize_by_extension as oze  # noqa: E402
from organize_by_extension import (  # noqa: E402
    BUCKET_NAME_PATTERN,
    BUCKET_SIZE,
    SIGNATURES,
    IsoBmffSignature,
    MagicSignature,
    RiffSignature,
    SniffContext,
    _find_reusable_bucket,
    bucket_name,
    detect_type_by_header,
    is_bucketed_file,
    normalize_extension,
    normalize_prefix,
    resolve_real_extension,
)

FUZZ = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.function_scoped_fixture],
)

def _registry_header_seeds() -> tuple[bytes, ...]:
    seeds = []
    for signature in SIGNATURES:
        if isinstance(signature, MagicSignature):
            seeds.append(b"\x00" * signature.offset + signature.sig)
        elif isinstance(signature, IsoBmffSignature):
            seeds.append(b"\x00\x00\x00\x18ftypisom")
        elif isinstance(signature, RiffSignature):
            seeds.extend(
                b"RIFF\x00\x00\x00\x00" + subtype
                for subtype in (b"WAVE", b"AVI ", b"WEBP")
            )
        else:
            raise AssertionError(
                f"fuzz seed missing for signature type {type(signature).__name__}"
            )
    return tuple(seeds)


REGISTRY_HEADER_SEEDS = _registry_header_seeds()
KNOWN_LABELS: frozenset[str] = frozenset(
    label
    for head in REGISTRY_HEADER_SEEDS
    for signature in SIGNATURES
    if (label := signature.matches(head)) is not None
)

# Strategies ----------------------------------------------------------------

# Arbitrary header bytes (0..64 long). Lets the sniffer see truncated headers,
# random junk, and the exact magic prefixes via mix-in below.
random_bytes = st.binary(min_size=0, max_size=64)

# Signature strategy that prefixes a real magic value, possibly with trailing
# random bytes — flushes out off-by-one in offset arithmetic.
signature_bytes = st.builds(
    lambda seed, tail: seed + tail,
    st.sampled_from(REGISTRY_HEADER_SEEDS),
    st.binary(min_size=0, max_size=32),
)

# Header bytes that *look* like an ISO BMFF (offset-4 ftyp) or RIFF container.
ftyp_bytes = st.builds(
    lambda pad, brand, rest: pad + b"ftyp" + brand + rest,
    st.binary(min_size=4, max_size=4),
    st.binary(min_size=4, max_size=4),
    st.binary(min_size=0, max_size=16),
)
riff_subtype = st.sampled_from([b"WAVE", b"AVI ", b"WEBP", b"XXXX"])
riff_bytes = st.builds(
    lambda size, sub, rest: b"RIFF" + size + sub + rest,
    st.binary(min_size=4, max_size=4),
    riff_subtype,
    st.binary(min_size=0, max_size=16),
)

header_bytes = st.one_of(random_bytes, signature_bytes, ftyp_bytes, riff_bytes)

# A filename strategy: occasionally pathological (empty stem, weird suffix).
filename_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\x00"),
    min_size=1,
    max_size=40,
)
extensions = st.sampled_from([
    "pdf", "PDF", "doc", "docx", "jpg", "jpeg", "png", "gif", "zip", "tar",
    "gz", "tgz", "mp3", "mp4", "mov", "wav", "txt", "html", "htm", "xml",
    "rar", "7z", "xz", "ico", "tif", "tiff", "bin", "", " ", ".",
])
ext_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\x00."),
    min_size=0,
    max_size=8,
)


def _write_with_head(directory: Path, name: str, head: bytes) -> Path:
    """Materialise a fuzz file under ``directory`` and return its path."""
    p = directory / name
    p.write_bytes(head)
    return p


# Fuzz targets --------------------------------------------------------------


class HeaderSnifferFuzz(unittest.TestCase):
    """detect_type_by_header on arbitrary header bytes."""

    @given(head=header_bytes, suffix=extensions, stem=filename_text)
    @FUZZ
    def test_returns_none_or_known_label(self, head, suffix, stem):
        with TemporaryDirectory() as d:
            safe_stem = stem.strip(".") or "x"
            name = f"{safe_stem}.{suffix}" if suffix.strip() else safe_stem
            try:
                p = _write_with_head(Path(d), name, head)
            except (OSError, ValueError):
                return  # filesystem refused this name; not a sniffer bug
            result = detect_type_by_header(p)
            self.assertTrue(result is None or isinstance(result, str))
            if result is not None:
                self.assertIn(result, KNOWN_LABELS)

    def test_unreadable_path_returns_none(self):
        # Nonexistent path triggers OSError on open — must be swallowed.
        self.assertIsNone(detect_type_by_header(Path("/nonexistent/zzz_no_such_file_zzz")))

    def test_empty_file_returns_none(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "empty.bin"
            p.write_bytes(b"")
            self.assertIsNone(detect_type_by_header(p))


class ResolveRealExtensionFuzz(unittest.TestCase):
    """resolve_real_extension over arbitrary (header, suffix) pairs."""

    @given(head=header_bytes, suffix=extensions, stem=filename_text, sniff=st.booleans())
    @FUZZ
    def test_returns_nonempty_str(self, head, suffix, stem, sniff):
        with TemporaryDirectory() as d:
            safe_stem = stem.strip(".") or "x"
            name = f"{safe_stem}.{suffix}" if suffix.strip() else safe_stem
            try:
                p = _write_with_head(Path(d), name, head)
            except (OSError, ValueError):
                return
            ext = resolve_real_extension(p, ctx=SniffContext(sniff=sniff))
            self.assertIsInstance(ext, str)
            self.assertNotEqual(ext, "")
            # Bucket directory name must not contain path separators or NUL.
            self.assertNotIn("/", ext)
            self.assertNotIn("\x00", ext)

    @given(head=header_bytes, suffix=extensions, stem=filename_text)
    @FUZZ
    def test_no_sniff_equals_normalize_extension(self, head, suffix, stem):
        with TemporaryDirectory() as d:
            safe_stem = stem.strip(".") or "x"
            name = f"{safe_stem}.{suffix}" if suffix.strip() else safe_stem
            try:
                p = _write_with_head(Path(d), name, head)
            except (OSError, ValueError):
                return
            self.assertEqual(
                resolve_real_extension(p, ctx=SniffContext(sniff=False)),
                normalize_extension(p),
            )


class HeaderOverridesExtensionTests(unittest.TestCase):
    """Targeted (non-property) regressions for the main user case."""

    def test_pdf_header_with_doc_extension_routes_to_pdf(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "mypdf.doc"
            p.write_bytes(b"%PDF-1.4\n")
            self.assertEqual(resolve_real_extension(p), "pdf")

    def test_jpeg_alias_keeps_declared_extension(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "photo.jpeg"
            p.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
            self.assertEqual(resolve_real_extension(p), "jpeg")

    def test_zip_family_member_keeps_declared_extension(self):
        with TemporaryDirectory() as d:
            for ext in ("docx", "xlsx", "epub", "jar"):
                p = Path(d) / f"f.{ext}"
                p.write_bytes(b"PK\x03\x04rest")
                self.assertEqual(resolve_real_extension(p), ext)

    def test_ole2_family_member_keeps_declared_extension(self):
        with TemporaryDirectory() as d:
            for ext in ("doc", "xls", "ppt", "msi"):
                p = Path(d) / f"f.{ext}"
                p.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest")
                self.assertEqual(resolve_real_extension(p), ext)

    def test_iso_bmff_family_keeps_declared(self):
        with TemporaryDirectory() as d:
            for ext in ("mp4", "mov", "m4a"):
                p = Path(d) / f"v.{ext}"
                p.write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00")
                self.assertEqual(resolve_real_extension(p), ext)

    def test_unknown_header_falls_back_to_extension(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "notes.doc"
            p.write_bytes(b"plain text, nothing magical here")
            self.assertEqual(resolve_real_extension(p), "doc")

    def test_riff_wav_overrides_wrong_extension(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "song.bin"
            p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
            self.assertEqual(resolve_real_extension(p), "wav")


class NormalizePrefixFuzz(unittest.TestCase):
    @given(name=st.text(max_size=80))
    @FUZZ
    def test_returns_single_char_alnum_or_underscore(self, name):
        result = normalize_prefix(name)
        self.assertEqual(len(result), 1)
        self.assertTrue(result.isalnum() or result == "_")


class NormalizeExtensionFuzz(unittest.TestCase):
    @given(suffix=ext_text, stem=filename_text)
    @FUZZ
    def test_no_path_separator_in_result(self, suffix, stem):
        safe_stem = stem.strip(".") or "x"
        name = f"{safe_stem}.{suffix}" if suffix else safe_stem
        with TemporaryDirectory() as d:
            try:
                p = Path(d) / name
                p.write_bytes(b"")
            except (OSError, ValueError):
                return
            ext = normalize_extension(p)
            self.assertIsInstance(ext, str)
            self.assertNotEqual(ext, "")
            self.assertNotIn("/", ext)
            self.assertNotIn("\x00", ext)


class BucketNameFuzz(unittest.TestCase):
    @given(
        prefix=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=1),
        index=st.integers(min_value=0, max_value=99_999),
    )
    @FUZZ
    def test_format_invariants(self, prefix, index):
        out = bucket_name(prefix, index)
        self.assertTrue(out.startswith(prefix))
        # 5-digit zero-padded suffix (oze-rel-08)
        self.assertEqual(len(out), len(prefix) + 5)
        self.assertTrue(out[len(prefix):].isdigit())

    @given(prefix=st.text(min_size=1, max_size=1), index=st.integers())
    @FUZZ
    def test_rejects_out_of_range_index(self, prefix, index):
        from organize_by_extension import BUCKET_INDEX_MAX
        if 0 <= index <= BUCKET_INDEX_MAX:
            bucket_name(prefix, index)  # should succeed
        else:
            with self.assertRaises((ValueError, TypeError)):
                bucket_name(prefix, index)


class FindReusableBucketFuzz(unittest.TestCase):
    """The planner must never crash on synthetic index lists / state caches."""

    @given(
        indices=st.lists(st.integers(min_value=0, max_value=200), min_size=0, max_size=30),
        filename=st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\x00"), min_size=1, max_size=12),
        fill=st.integers(min_value=0, max_value=BUCKET_SIZE + 5),
    )
    @FUZZ
    def test_returns_path_or_none_and_valid_index(self, indices, filename, fill):
        ext_dir = Path("/tmp/__fuzz_oze_nonexistent__")  # not touched on disk
        prefix = "x"
        # Synthesize a state_cache so the planner never touches the filesystem.
        state_cache: dict = {}
        seen = set()
        for idx in indices:
            if idx in seen:
                continue
            seen.add(idx)
            bucket_path = ext_dir / bucket_name(prefix, idx)
            if fill >= BUCKET_SIZE:
                state_cache[bucket_path] = oze._BUCKET_FULL
            else:
                names = {f"existing_{i}" for i in range(fill)}
                state_cache[bucket_path] = oze._BucketState(names, set(names))
        choice = _find_reusable_bucket(
            ext_dir, prefix, filename, state_cache, list(seen)
        )
        self.assertIsInstance(choice.next_index, int)
        self.assertGreaterEqual(choice.next_index, 0)
        self.assertGreaterEqual(choice.first_non_full, 0)
        if choice.bucket is not None:
            self.assertIsInstance(choice.bucket, Path)
            self.assertRegex(choice.bucket.name, r"^.\d{5}$")


class IsBucketedFileFuzz(unittest.TestCase):
    @given(
        parts=st.lists(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="/\x00"), min_size=1, max_size=20),
            min_size=0,
            max_size=6,
        ),
        sniff=st.booleans(),
    )
    @FUZZ
    def test_returns_bool_for_arbitrary_layout(self, parts, sniff):
        with TemporaryDirectory() as d:
            root = Path(d)
            # Walk down parts as directories; the last part is the filename.
            if not parts:
                return
            try:
                target = root.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"%PDF-1.4\n")
            except (OSError, ValueError):
                return
            result = is_bucketed_file(
                root, target, ctx=SniffContext(sniff=sniff)
            )
            self.assertIsInstance(result, bool)


class OrganizeIntegrationFuzz(unittest.TestCase):
    """End-to-end fuzz: build a small random tree, organize it, assert
    invariants on the resulting layout. Catches any explosion in the planner
    or move logic under mixed real / mismatched / unknown-header inputs.
    """

    @given(
        files=st.lists(
            st.tuples(filename_text, extensions, header_bytes),
            min_size=0,
            max_size=15,
            unique_by=lambda t: (t[0], t[1]),
        )
    )
    @FUZZ
    def test_organize_layout_invariants(self, files):
        with TemporaryDirectory() as d:
            root = Path(d)
            for stem, suffix, head in files:
                safe_stem = stem.strip(".").strip() or "x"
                name = f"{safe_stem}.{suffix}" if suffix.strip() else safe_stem
                # Some random names will still be illegal on the FS; ignore them.
                try:
                    p = root / name
                    if p.exists():
                        continue
                    p.write_bytes(head)
                except (OSError, ValueError):
                    continue
            oze.organize(root, verbose=False)
            # Every MOVED file must live under <ext>/<bucket>/<name>. Files
            # that organize had to skip (e.g. source name collides with the
            # bucket dir it would have landed in — "avi" with an AVI header
            # cannot become "avi/a00000/avi") stay at the root; the layout
            # invariant only applies to relocations.
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(root)
                if len(rel.parts) == 1:
                    continue   # not moved (collision-skipped or no-op)
                self.assertEqual(len(rel.parts), 3, f"unexpected layout: {rel}")
                self.assertRegex(rel.parts[1], r"^.\d{5}$")


# ---------------------------------------------------------------------------
# prune_empty_dirs fuzz suite
# ---------------------------------------------------------------------------

from organize_by_extension import prune_empty_dirs, _count_prunable_dirs  # noqa: E402

# Dirname strategy: covers ascii, unicode, control chars, leading/trailing
# dots and spaces. Excludes path separators and NUL (kernel-rejected) plus
# surrogates (Python rejects on Path round-trip without surrogateescape).
dirname_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters="/\x00",
    ),
    min_size=1,
    max_size=24,
)

# Strategy for a small random tree: list of (path-as-tuple-of-parts, is_file).
tree_node = st.tuples(
    st.lists(dirname_text, min_size=1, max_size=5),
    st.booleans(),  # True = file, False = directory
)


def _safe_create(root: Path, parts: list[str], is_file: bool) -> Path | None:
    """Materialise ``root/parts`` as a file or dir. Returns the created path
    or ``None`` when the filesystem refused the name (long-name, invalid
    bytes on this FS, name collides with existing file vs dir, …)."""
    try:
        target = root.joinpath(*parts)
    except (ValueError, OSError):
        return None
    try:
        if is_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.is_dir():
                return None
            target.write_bytes(b"")
        else:
            target.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, NotADirectoryError):
        return None
    return target


def _has_empty_descendant(root: Path) -> bool:
    """True iff any descendant directory of ``root`` is empty AND is not a
    symlink. Used as the post-condition oracle for the prune fuzz."""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        cur = Path(dirpath)
        if cur == root:
            continue
        if cur.is_symlink():
            continue
        try:
            with os.scandir(cur) as it:
                if not any(True for _ in it):
                    return True
        except OSError:
            continue
    return False


class PruneEmptyDirsFuzz(unittest.TestCase):
    """prune_empty_dirs on arbitrary trees — never raises; post-conditions hold."""

    @given(nodes=st.lists(tree_node, min_size=0, max_size=20))
    @FUZZ
    def test_no_empty_descendant_dirs_remain(self, nodes):
        with TemporaryDirectory() as d:
            root = Path(d)
            files_kept = []
            for parts, is_file in nodes:
                created = _safe_create(root, parts, is_file)
                if created is not None and is_file:
                    files_kept.append(created)

            removed = prune_empty_dirs(root)
            self.assertIsInstance(removed, int)
            self.assertGreaterEqual(removed, 0)

            # Post-condition 1: no empty descendant directory remains.
            self.assertFalse(_has_empty_descendant(root),
                             f"empty descendant survived prune under {root}")
            # Post-condition 2: pruning directories never removes a file we
            # successfully created.
            for f in files_kept:
                self.assertTrue(f.is_file(), f"file removed during prune: {f}")
            # Post-condition 3: root itself survives.
            self.assertTrue(root.exists())
            self.assertTrue(root.is_dir())

    @given(nodes=st.lists(tree_node, min_size=0, max_size=15))
    @FUZZ
    def test_count_matches_actual_removal(self, nodes):
        """``_count_prunable_dirs`` is the dry-run shadow of
        :func:`prune_empty_dirs` and must agree on every reachable tree."""
        with TemporaryDirectory() as d1, TemporaryDirectory() as d2:
            r1, r2 = Path(d1), Path(d2)
            # Mirror creation in two directories so they start identical.
            for parts, is_file in nodes:
                _safe_create(r1, parts, is_file)
                _safe_create(r2, parts, is_file)
            count = _count_prunable_dirs(r1)
            removed = prune_empty_dirs(r2)
            self.assertEqual(count, removed)

    @given(nodes=st.lists(tree_node, min_size=0, max_size=12))
    @FUZZ
    def test_idempotent_second_run_removes_zero(self, nodes):
        """A second prune on an already-pruned tree never removes anything."""
        with TemporaryDirectory() as d:
            root = Path(d)
            for parts, is_file in nodes:
                _safe_create(root, parts, is_file)
            prune_empty_dirs(root)
            self.assertEqual(prune_empty_dirs(root), 0)


class PruneSymlinkFuzz(unittest.TestCase):
    """Symlink properties: never followed, never broken-into."""

    @given(
        target_parts=st.lists(dirname_text, min_size=1, max_size=3),
        link_parts=st.lists(dirname_text, min_size=1, max_size=3),
    )
    @FUZZ
    def test_symlink_to_dir_outside_root_preserved(self, target_parts, link_parts):
        with TemporaryDirectory() as outside_dir, TemporaryDirectory() as d:
            outside = Path(outside_dir).joinpath(*target_parts)
            try:
                outside.mkdir(parents=True, exist_ok=True)
            except (OSError, ValueError):
                return
            root = Path(d)
            try:
                link = root.joinpath(*link_parts)
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.exists() or link.is_symlink():
                    return
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, ValueError, NotImplementedError):
                return
            prune_empty_dirs(root)
            self.assertTrue(link.is_symlink(), f"symlink removed during prune: {link}")
            self.assertEqual(link.resolve(), outside.resolve())
            # The outside target is untouched (we never followed the link).
            self.assertTrue(outside.exists(),
                            f"prune followed symlink and removed target {outside}")


class PruneEncodingFuzz(unittest.TestCase):
    """Byte-level dirnames the kernel accepts but Python's str layer can't
    decode as UTF-8. Walked transparently via surrogateescape."""

    @given(
        raw_tails=st.lists(
            st.binary(min_size=1, max_size=8).filter(
                lambda b: b"/" not in b and b"\x00" not in b
                and b not in (b".", b"..")
            ),
            min_size=0,
            max_size=6,
            unique=True,
        )
    )
    @FUZZ
    def test_surrogate_escape_dirnames_pruned(self, raw_tails):
        if os.name == "nt":
            self.skipTest("raw byte path names are not supported on Windows")
        with TemporaryDirectory() as d:
            root = Path(d)
            created = 0
            for tail in raw_tails:
                full = os.path.join(os.fsencode(d), b"raw_" + tail)
                try:
                    os.mkdir(full)
                    created += 1
                except (OSError, ValueError):
                    continue
            removed = prune_empty_dirs(root)
            self.assertEqual(removed, created,
                             f"raw-byte dirnames not all pruned: {removed} vs {created}")


# --- oze-test-02: extra_zip_family knob -----------------------------------

EXTRA_EXT = st.text(
    alphabet=st.characters(
        min_codepoint=ord("a"), max_codepoint=ord("z"),
    ),
    min_size=1, max_size=8,
)


class ExtraZipFamilyFuzz(unittest.TestCase):
    """For any extension declared in `extra_zip_family` with a PK header,
    `resolve_real_extension` must return that declared extension — never
    'zip' (oze-test-02)."""

    @settings(parent=FUZZ, max_examples=80)
    @given(EXTRA_EXT)
    def test_extra_zip_family_preserves_declared_ext(self, ext):
        assume(ext not in oze.EXTENSION_ALIASES)
        assume(ext != "zip")
        with TemporaryDirectory() as d:
            path = Path(d) / f"file.{ext}"
            path.write_bytes(b"PK\x03\x04rest")
            resolved = oze.resolve_real_extension(
                path,
                ctx=oze.SniffContext(extra_families={"zip": frozenset({ext})}),
            )
            self.assertEqual(resolved, ext)


# --- oze-test-03: head_cache shared between scan and plan -----------------

class HeadCacheSharedFuzz(unittest.TestCase):
    """Inject a pre-populated head_cache and confirm the planner consults
    it rather than re-opening the file (oze-test-03)."""

    @settings(parent=FUZZ, max_examples=20)
    @given(st.lists(st.binary(min_size=8, max_size=64), min_size=1, max_size=5,
                    unique=True))
    def test_pre_populated_cache_is_honoured(self, payloads):
        with TemporaryDirectory() as d:
            root = Path(d)
            files = []
            cache: dict = {}
            for i, p in enumerate(payloads):
                f = root / f"f{i}.bin"
                f.write_bytes(p)
                # Seed cache with the EXACT bytes the file already has so the
                # planner's behaviour shouldn't change.
                cache[f] = p
                files.append(f)
            opens = {"n": 0}
            real_open = __builtins__["open"] if isinstance(__builtins__, dict) else open

            def counting_open(path, *args, **kwargs):
                opens["n"] += 1
                return real_open(path, *args, **kwargs)

            import builtins
            real_builtin_open = builtins.open
            builtins.open = counting_open
            try:
                for f in files:
                    oze.resolve_real_extension(
                        f, ctx=oze.SniffContext(head_cache=cache)
                    )
            finally:
                builtins.open = real_builtin_open
            # With seeded cache, no fresh `open` was needed.
            self.assertEqual(opens["n"], 0,
                             f"planner reopened cached entries ({opens['n']} opens)")


if __name__ == "__main__":
    unittest.main()
