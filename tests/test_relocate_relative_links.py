"""rf-link-80: refuse relative symlinks that escape a relocated tree."""

import os
from pathlib import Path

import pytest
from hypothesis import given, strategies as st

import relocate_folder as rf

pytestmark = pytest.mark.skipif(os.name != "posix", reason="relocation requires POSIX")


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("relative, link_text", [
    ("link", "../neighbor"),
    ("nested/link", "../../neighbor"),
    ("link", "../missing"),
])
def test_rf_link_80_refuses_before_changing_source_or_destination(tmp_path, dry_run, relative, link_text):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("keep")
    (tmp_path / "neighbor").write_text("outside")
    link = source / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(link_text)
    destination = tmp_path / "new" / "destination" / "source"
    plan = rf.Plan(source, destination, force=True, verify=False, check_space=False, dry_run=dry_run)

    with pytest.raises(RuntimeError, match="relative symlink escapes source tree"):
        rf.execute(plan)

    assert source.is_dir() and not source.is_symlink()
    assert (source / "payload").read_text() == "keep"
    assert os.readlink(link) == link_text
    assert (tmp_path / "neighbor").read_text() == "outside"
    assert not (tmp_path / "new").exists()


def test_rf_link_80_keeps_internal_and_absolute_links(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "payload").write_text("keep")
    outside = tmp_path / "neighbor"
    outside.write_text("outside")
    links = {"inside": "nested/../payload", "nested/up": "../payload",
             "dangling": "absent", "absolute": str(outside)}
    for relative, target in links.items():
        (source / relative).symlink_to(target)
    destination = tmp_path / "destination" / "source"

    assert rf.execute(rf.Plan(source, destination, force=True, check_space=False)).startswith("ok:")

    for relative, target in links.items():
        assert os.readlink(destination / relative) == target
    assert (source / "inside").read_text() == "keep"
    assert (source / "absolute").read_text() == "outside"
    assert not (source / "dangling").exists()


def test_rf_link_80_rechecks_links_created_after_preflight(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("keep")
    (tmp_path / "neighbor").write_text("outside")
    destination = tmp_path / "new" / "source"
    probe = rf._probe_source_rename

    def add_link(parent):
        probe(parent)
        (source / "late").symlink_to("../neighbor")

    monkeypatch.setattr(rf, "_probe_source_rename", add_link)
    with pytest.raises(RuntimeError, match="relative symlink escapes source tree"):
        rf.execute(rf.Plan(source, destination, force=True, check_space=False))

    assert not source.is_symlink()
    assert (source / "payload").read_text() == "keep"
    assert (source / "late").read_text() == "outside"
    assert not destination.exists()


@given(depth=st.integers(0, 8), parents=st.integers(0, 10))
def test_rf_link_80_relative_parent_traversal_boundary(depth, parents):
    relative = Path(*(["nested"] * depth), "link")
    target = "../" * parents + "payload"
    if parents > depth:
        with pytest.raises(RuntimeError, match="relative symlink escapes source tree"):
            rf._validate_relative_symlink(Path("source"), relative, target)
    else:
        rf._validate_relative_symlink(Path("source"), relative, target)
