#!/usr/bin/env python3

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("spotify_songs", REPO / "spotify-songs.py")
sp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sp)


class BadRow:
    def find_element(self, *_args):
        raise RuntimeError("selector drift")


class Driver:
    def get(self, _url):
        pass

    def execute_script(self, _script):
        pass

    def find_elements(self, *_args):
        return [BadRow()]


class Wait:
    def __init__(self, *_args):
        pass

    def until(self, _condition):
        return True


def test_get_spotify_songs_logs_selector_failures(monkeypatch, caplog):
    monkeypatch.setattr(sp, "WebDriverWait", Wait)
    monkeypatch.setattr(sp.time, "sleep", lambda _seconds: None)

    with caplog.at_level("WARNING"):
        assert sp.get_spotify_songs(Driver(), "https://spotify.test") == []

    assert "selector drift" in caplog.text
