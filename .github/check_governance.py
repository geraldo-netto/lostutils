#!/usr/bin/env python3
"""Reject sensitive literals in Git-tracked text without printing their values."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


RULES = (
    ("private-home-path", re.compile(
        r"(?:/(?:home|Users)/[^/\s\"'`<>]+|[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"'`<>]+)"
    )),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
    ("provider-token", re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
        r"|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|sk_live_[A-Za-z0-9]{16,}"
        r"|xox[baprs]-[A-Za-z0-9-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}"
        r"|AIza[A-Za-z0-9_-]{35})\b"
    )),
    ("credential-literal", re.compile(
        r'''(?i)["']?\b(?:api[_-]?key|client[_-]?secret|(?:access|refresh|auth)[_-]?token|password)["']?'''
        r'''\s*[:=]\s*(["'])([^"'\r\n]+)\1'''
    )),
    ("credential-literal", re.compile(
        r"^\s*(?:export\s+)?(?:API_KEY|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN|AUTH_TOKEN|PASSWORD)"
        r"\s*[:=]\s*[^\s\"'#$][^\s#]*"
    )),
)
RULE_NAMES = {name for name, _pattern in RULES}
TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".lock", ".env", ".pem", ".key"}
ALLOWLIST = ".github/governance-allowlist.json"
Finding = tuple[str, int, str]
ExceptionKey = tuple[str, str, str]


class CheckError(Exception):
    """A check could not run completely; messages never contain file contents."""


def tracked_paths(repo: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CheckError("cannot enumerate tracked files with Git") from exc
    return [part.decode("utf-8", errors="surrogateescape") for part in result.stdout.split(b"\0") if part]


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def exception_key(entry: object) -> ExceptionKey:
    if not isinstance(entry, dict) or set(entry) != {"path", "rule", "sha256", "reason"}:
        raise CheckError("invalid governance exception fields")
    if not all(isinstance(value, str) and value.strip() for value in entry.values()):
        raise CheckError("governance exceptions require nonempty string fields")
    path = PurePosixPath(entry["path"])
    if path.is_absolute() or ".." in path.parts or "\\" in entry["path"]:
        raise CheckError("governance exceptions require repository-relative paths")
    if entry["rule"] not in RULE_NAMES or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None:
        raise CheckError("invalid governance exception rule or SHA-256")
    return entry["path"], entry["rule"], entry["sha256"]


def load_exceptions(repo: Path) -> set[ExceptionKey]:
    path = repo / ALLOWLIST
    if not path.exists():
        return set()
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CheckError("cannot read governance exception file") from exc
    if not isinstance(entries, list):
        raise CheckError("governance exception file must contain a list")
    keys = [exception_key(entry) for entry in entries]
    if len(set(keys)) != len(keys):
        raise CheckError("duplicate governance exception")
    return set(keys)


def tracked_text(repo: Path, name: str) -> str | None:
    path = repo / name
    try:
        if path.is_symlink():
            return str(path.readlink())
        data = path.read_bytes()
    except OSError as exc:
        raise CheckError(f"cannot read tracked file {json.dumps(name)}") from exc
    try:
        text = data.decode("utf-8-sig")
        if "\0" not in text:
            return text
    except UnicodeError:
        pass
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.startswith("."):
        raise CheckError(f"tracked text is not valid UTF-8: {json.dumps(name)}")
    return None


def scan_line(name: str, number: int, line: str, exceptions: set[ExceptionKey], used: set[ExceptionKey]) -> list[Finding]:
    findings = []
    for rule, pattern in RULES:
        for match in pattern.finditer(line):
            key = name, rule, fingerprint(match.group())
            if key in exceptions:
                used.add(key)
            else:
                findings.append((name, number, rule))
    return findings


def scan_text(name: str, text: str, exceptions: set[ExceptionKey], used: set[ExceptionKey]) -> list[Finding]:
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_line(name, number, line, exceptions, used))
    return findings


def scan_repository(repo: Path) -> list[Finding]:
    exceptions = load_exceptions(repo)
    used: set[ExceptionKey] = set()
    findings = []
    for name in tracked_paths(repo):
        text = tracked_text(repo, name)
        if text is not None:
            findings.extend(scan_text(name, text, exceptions, used))
    if exceptions - used:
        raise CheckError("unused governance exception; remove it or update its exact match")
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        findings = scan_repository(args.repo)
    except CheckError as exc:
        print(f"Governance check failed: {exc}", file=sys.stderr)
        return 2
    for name, line, rule in findings:
        print(f"{json.dumps(name)}:{line}: {rule}", file=sys.stderr)
    if findings:
        print(f"Governance check failed: {len(findings)} finding(s); matched values redacted.", file=sys.stderr)
        return 1
    print("Governance check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
