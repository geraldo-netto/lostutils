"""Focused tests for the legacy NumPy duplicate extractor."""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("numpy")

SCRIPT = Path(__file__).resolve().parent.parent / "dedupl_numpy.py"
SPEC = importlib.util.spec_from_file_location("dedupl_numpy", SCRIPT)
dedupl_numpy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dedupl_numpy)


def test_main_requires_hash_file(monkeypatch, capsys):
    monkeypatch.setattr(dedupl_numpy.sys, "argv", ["dedupl_numpy.py"])

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" in captured.err


def test_main_supports_help(monkeypatch, capsys):
    monkeypatch.setattr(dedupl_numpy.sys, "argv", ["dedupl_numpy.py", "--help"])

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 0
    assert "hash_file" in capsys.readouterr().out


def test_main_groups_duplicate_hashes(monkeypatch, tmp_path, capfd):
    digest = b"a" * 32
    source = tmp_path / "hashes.txt"
    source.write_bytes(
        digest + b" /data/first.bin\n"
        + digest + b" /data/second.bin\n"
        + b"b" * 32 + b" /data/unique.bin\n"
    )
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    dedupl_numpy.main()

    output = capfd.readouterr().out
    assert "first.bin" in output
    assert "second.bin" in output
    assert "unique.bin" not in output
    assert "equal files: 1 / 3" in output


def test_group_duplicates_is_pure():
    digest = b"a" * 32
    raw = digest + b" /one\n" + digest + b" /two\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


@pytest.mark.parametrize("hash_width", [40, 64])
def test_group_duplicates_derives_hash_width(hash_width):
    digest = b"a" * hash_width
    raw = digest + b" /one\n" + digest + b" /two\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


@pytest.mark.parametrize(
    "raw,error",
    [
        (b"short\n", "separator"),
        (b"\n", "separator"),
        (b"a" * 32 + b" /one\n" + b"b" * 31 + b" /two\n", "same hash width"),
    ],
)
def test_group_duplicates_rejects_invalid_record_layout(raw, error):
    with pytest.raises(ValueError, match=error):
        dedupl_numpy.group_duplicates(
            dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
        )


def test_main_reports_invalid_record_layout(monkeypatch, tmp_path, capfd):
    source = tmp_path / "hashes.txt"
    source.write_bytes(b"short\n")
    monkeypatch.setattr(
        dedupl_numpy.sys,
        "argv",
        ["dedupl_numpy.py", str(source)],
    )

    with pytest.raises(SystemExit) as exc:
        dedupl_numpy.main()

    assert exc.value.code == 1
    assert "error:" in capfd.readouterr().err


def test_group_duplicates_includes_final_unterminated_record():
    digest = b"a" * 32
    raw = digest + b" /one\n" + digest + b" /two"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2


def test_group_duplicates_strips_crlf_record_terminator():
    digest = b"a" * 32
    raw = digest + b" /one\r\n" + digest + b" /two\r\n"

    paths, equal_files, record_count = dedupl_numpy.group_duplicates(
        dedupl_numpy.np.frombuffer(raw, dtype=dedupl_numpy.np.uint8),
    )

    assert paths == {b"/one", b"/two"}
    assert equal_files == 1
    assert record_count == 2
