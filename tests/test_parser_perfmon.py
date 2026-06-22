#!/usr/bin/env python3

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("parser_perfmon", REPO / "parser-perfmon.py")
pp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pp)


def test_normalize_header_replaces_perfmon_header_and_machine_prefix():
    row = [
        pp.PERFMON_DATE_HEADER,
        r"\\sample\Processor(_Total)\% Processor Time",
    ]

    assert pp.normalize_header(row, "sample") == "date;Processor(_Total)\\% Processor Time\n"


def test_normalize_data_row_preserves_original_date_contract():
    row = ["06/05/2026 09:10:00.000", "1.25", " 3.50 "]

    assert pp.normalize_data_row(row) == "05/06/2026;1,25;3,50\n"


def test_convert_file_streams_to_named_output(tmp_path, monkeypatch):
    source = tmp_path / "server_cpu.csv"
    source.write_text(
        '"%s","\\\\server\\Counter"\n"06/05/2026 09:10:00.000","1.25"\n'
        % pp.PERFMON_DATE_HEADER,
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert pp.convert_file(str(source), True) == "server"
    assert (tmp_path / "server.csv").read_text(encoding="utf-8") == (
        "date;Counter\n"
        "05/06/2026;1,25\n"
    )
