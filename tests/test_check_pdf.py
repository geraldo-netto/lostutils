#!/usr/bin/env python3

import check_pdf


def test_is_pdf_rejects_non_pdf_suffix(tmp_path):
    source = tmp_path / "report.pdf.bak"
    source.write_text("not a pdf", encoding="utf-8")

    assert check_pdf.is_pdf(str(source)) is False


def test_is_pdf_uses_file_command_without_shell(monkeypatch, tmp_path):
    source = tmp_path / "report.pdf"
    source.write_text("%PDF", encoding="utf-8")
    calls = {}

    class Result:
        stdout = "report.pdf: application/pdf; charset=binary"

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(check_pdf.subprocess, "run", fake_run)

    assert check_pdf.is_pdf(str(source)) is True
    assert calls["cmd"] == ["/usr/bin/file", "--mime", str(source)]
    assert calls["kwargs"]["capture_output"] is True


def test_main_reports_invalid_without_deleting(monkeypatch, tmp_path):
    source = tmp_path / "bad.pdf"
    source.write_text("bad", encoding="utf-8")
    monkeypatch.setattr(check_pdf, "validate_pdf", lambda _path: (_ for _ in ()).throw(ValueError()))

    assert check_pdf.main([str(source)]) == 1
    assert source.exists()
