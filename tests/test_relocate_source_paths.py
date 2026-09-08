"""CLI source paths normalize parent components without dereferencing the leaf."""

import argparse
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relocate_folder as rf


pytestmark = pytest.mark.skipif(os.name == "nt", reason="relocation requires POSIX descriptors")


@pytest.mark.parametrize("source", ["..", "../", "child/..", "child/../."])
def test_plan_refuses_terminal_dotdot(tmp_path, monkeypatch, source):
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(source=source, dest_root=str(tmp_path / "destination"))
    with pytest.raises(ValueError, match="source must not end in"):
        rf.Plan.from_args(args)


@pytest.mark.parametrize("recover", [False, True])
def test_cli_rejects_dotdot_before_filesystem_work(tmp_path, monkeypatch, caplog, recover):
    monkeypatch.chdir(tmp_path)
    argv = [".."]
    if recover:
        argv.append("--recover")
    else:
        argv.append(str(tmp_path / "destination"))
    argv += ["--dry-run", "--force"]
    assert rf.main(argv) == (1 if recover else 2)
    assert "source must not end in" in caplog.text
    assert list(tmp_path.iterdir()) == []


def test_parent_normalization_obeys_symlink_then_dotdot(tmp_path):
    actual = tmp_path / "actual"
    (actual / "nested").mkdir(parents=True)
    source = actual / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual / "nested", target_is_directory=True)
    args = argparse.Namespace(
        source=str(alias / ".." / "source"), dest_root=str(tmp_path / "destination"))

    plan = rf.Plan.from_args(args)

    assert plan.source == source
    assert plan.target == tmp_path / "destination" / "source"


def test_normalized_source_preserves_final_symlink_for_idempotency(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    destination = tmp_path / "destination"
    target = destination / "source"
    target.mkdir(parents=True)
    (target / "payload").write_bytes(b"original")
    source = actual / "source"
    source.symlink_to(target, target_is_directory=True)
    args = argparse.Namespace(source=str(alias / "source"), dest_root=str(destination))

    plan = rf.Plan.from_args(args)

    assert plan.source == source
    assert plan.source.is_symlink()
    assert rf.execute(plan).startswith("skipped:")


def test_recovery_normalizes_parent_but_preserves_final_symlink(tmp_path):
    actual = tmp_path / "actual"
    (actual / "nested").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(actual / "nested", target_is_directory=True)
    backup = actual / ("source" + rf.BACKUP_SUFFIX)
    backup.mkdir()
    (backup / "payload").write_bytes(b"original")
    source_arg = str(alias / ".." / "source")

    assert rf.main([source_arg, "--recover", "--force"]) == 0
    source = actual / "source"
    assert (source / "payload").read_bytes() == b"original"

    source.rename(backup)
    foreign = tmp_path / "foreign"
    source.symlink_to(foreign, target_is_directory=True)
    assert rf.main([source_arg, "--recover", "--force"]) == 1
    assert source.is_symlink()
    assert not foreign.exists()
    assert (backup / "payload").read_bytes() == b"original"


def test_current_directory_gets_explicit_basename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(source=".", dest_root=str(tmp_path.parent / "destination"))
    plan = rf.Plan.from_args(args)
    assert plan.source == tmp_path
    assert plan.target.name == tmp_path.name


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("error", [PermissionError("parent denied"), RuntimeError("symlink loop")])
def test_cli_surfaces_parent_resolution_failure(tmp_path, monkeypatch, caplog, recover, error):
    def failed_resolution(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(Path, "resolve", failed_resolution)
    argv = [str(tmp_path / "source")]
    argv += ["--recover"] if recover else [str(tmp_path / "destination")]
    argv.append("--force")
    assert rf.main(argv) == (1 if recover else 2)
    assert str(error) in caplog.text
    assert list(tmp_path.iterdir()) == []
