#!/usr/bin/env python3

from hypothesis import given, settings, strategies as st

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location("check_mx_domain", REPO / "check-mx-domain.py")
cmx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cmx)

FUZZ = settings(max_examples=300, deadline=None)


@given(email=st.text(max_size=100))
@FUZZ
def test_domain_from_email_never_silently_accepts_malformed(email):
    try:
        domain = cmx.domain_from_email(email)
    except IndexError:
        return

    assert email.count("@") == 1
    assert domain
