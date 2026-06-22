#!/usr/bin/env python3

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "masterclass_mass_rename", REPO / "masterclass-mass-rename.py"
)
mmr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mmr)


def test_clean_name_removes_tokens_with_regex_table():
    assert mmr.clean_name("[MasterClass] FreeCourseWeb.com - Lesson_01™") == "lesson01"


def test_rename_links_then_unlinks_file_without_overwriting(tmp_path):
    source = tmp_path / "old.txt"
    target = tmp_path / "new.txt"
    source.write_text("payload", encoding="utf-8")

    mmr.rename(str(source), str(target))

    assert not source.exists()
    assert target.read_text(encoding="utf-8") == "payload"


def test_rename_raises_when_target_exists(tmp_path):
    source = tmp_path / "old.txt"
    target = tmp_path / "new.txt"
    source.write_text("payload", encoding="utf-8")
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        mmr.rename(str(source), str(target))

    assert source.read_text(encoding="utf-8") == "payload"
    assert target.read_text(encoding="utf-8") == "existing"
