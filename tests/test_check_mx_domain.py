#!/usr/bin/env python3

import importlib.util
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_mx_domain", REPO / "check-mx-domain.py")
cmx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cmx)


def test_domain_from_email_requires_exactly_one_at():
    assert cmx.domain_from_email("user@example.com") == "example.com"
    with pytest.raises(IndexError):
        cmx.domain_from_email("missing-at")
    with pytest.raises(IndexError):
        cmx.domain_from_email("a@b@c")


def test_has_mx_record_uses_resolver_timeout_and_resolve(monkeypatch):
    created = []

    class FakeResolver:
        def __init__(self):
            self.lifetime = None
            self.timeout = None
            created.append(self)

        def resolve(self, domain, record_type):
            assert domain == "example.com"
            assert record_type == "MX"
            return ["mx"]

    monkeypatch.setattr(cmx.dns.resolver, "Resolver", FakeResolver)

    assert cmx.has_mx_record("user@example.com", lifetime=7)
    assert created[0].lifetime == 7
    assert created[0].timeout == 7


def test_main_reports_dns_failure_to_stderr(monkeypatch, capsys):
    def fail(_email):
        raise cmx.dns.exception.DNSException("lookup failed")

    monkeypatch.setattr(cmx, "has_mx_record", fail)

    assert cmx.main(["user@example.com"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "DNS" in captured.err
    assert "lookup failed" in captured.err
