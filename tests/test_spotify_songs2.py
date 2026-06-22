#!/usr/bin/env python3

import importlib.util
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("spotify_songs2", REPO / "spotify-songs2.py")
spv2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spv2)


def test_resolve_youtube_urls_preserves_track_order():
    tracks = ["one", "two", "three"]

    urls = spv2.resolve_youtube_urls(
        tracks,
        workers=3,
        matcher=lambda track: "url:" + track,
    )

    assert urls == ["url:one", "url:two", "url:three"]


def test_resolve_youtube_urls_supports_sequential_mode():
    calls = []

    urls = spv2.resolve_youtube_urls(
        ["one", "two"],
        workers=1,
        matcher=lambda track: calls.append(track) or "url:" + track,
    )

    assert calls == ["one", "two"]
    assert urls == ["url:one", "url:two"]


def test_youtube_best_match_surfaces_subprocess_failure(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, ["yt-dlp"], stderr="bad search")

    monkeypatch.setattr(spv2.subprocess, "run", fail)

    assert spv2.youtube_best_match("track") == ""
    assert "bad search" in capsys.readouterr().err


def test_youtube_best_match_uses_timeout_and_check(monkeypatch):
    calls = {}

    class Result:
        stdout = '{"view_count": 10, "webpage_url": "https://video"}\n'

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(spv2.subprocess, "run", fake_run)

    assert spv2.youtube_best_match("track") == "https://video"
    assert calls["kwargs"]["timeout"] == 60
    assert calls["kwargs"]["check"] is True
