#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import date, datetime, timezone

import pytest

import import_events


class FakeLlm:
    def __init__(self, content='[{"title": "Launch", "start": "2026-06-06"}]'):
        self.messages = []
        self._content = content

    def create_chat_completion(self, messages):
        self.messages.append(messages)
        return {"choices": [{"message": {"content": self._content}}]}


def test_extract_with_llm_uses_injected_client(tmp_path):
    source = tmp_path / "event.txt"
    source.write_text("Launch party tomorrow", encoding="utf-8")
    fake = FakeLlm()

    events = import_events.extract_with_llm(source, llm_client=fake)

    assert events == [
        {
            "title": "Launch",
            "start": "2026-06-06",
            "end": "",
            "location": "",
            "source": "event.txt",
            "type": "Text/LLM",
        }
    ]
    assert fake.messages


def test_process_folder_uses_injected_client_for_text_files(tmp_path):
    (tmp_path / "event.txt").write_text("Launch party tomorrow", encoding="utf-8")

    events = import_events.process_folder(str(tmp_path), llm_client=FakeLlm())

    assert [event["title"] for event in events] == ["Launch"]


def test_normalize_event_date_returns_iso_strings():
    assert import_events.normalize_event_date(date(2026, 6, 6)) == "2026-06-06"
    assert import_events.normalize_event_date(
        datetime(2026, 6, 6, 9, 30, tzinfo=timezone.utc)
    ) == "2026-06-06T09:30:00+00:00"


def test_parse_llm_events_handles_object_and_nested_arrays_in_strings():
    text = 'prefix {"title": "Board [internal]", "start": "2026-06-06"} suffix'

    events = import_events.parse_llm_events(text, Path("source.txt"), "Text/LLM")

    assert events[0]["title"] == "Board [internal]"
    assert events[0]["start"] == "2026-06-06"
    assert events[0]["type"] == "Text/LLM"


def test_parse_llm_events_folds_separate_date_and_time():
    text = '[{"title": "Sync", "date": "2026-06-22", "time": "14:00"}]'

    events = import_events.parse_llm_events(text, Path("img.png"), "Image/Vision")

    assert events[0]["start"] == "2026-06-22T14:00"


def test_parse_llm_events_keeps_end_and_location():
    text = ('[{"title": "Expo", "start": "2026-06-22T10:00", '
            '"end": "2026-06-22T18:00", "location": "Hall A"}]')

    events = import_events.parse_llm_events(text, Path("p.pdf"), "PDF")

    assert events[0]["end"] == "2026-06-22T18:00"
    assert events[0]["location"] == "Hall A"


def test_parse_llm_events_synthesizes_missing_title():
    events = import_events.parse_llm_events('[{"start": "2026-06-22"}]', Path("i.png"), "x")
    assert events[0]["title"] == "No Title"


def test_image_messages_uses_correct_mime(tmp_path):
    img = tmp_path / "poster.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    messages = import_events._image_messages(img)

    url = messages[1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_process_folder_handles_images_with_various_formats(tmp_path):
    for name in ("a.png", "b.webp", "c.gif", "d.bmp", "e.tiff"):
        (tmp_path / name).write_bytes(b"fake-image-bytes")
    fake = FakeLlm('[{"title": "Expo", "start": "2026-06-22T10:00"}]')

    events = import_events.process_folder(str(tmp_path), llm_client=fake)

    assert len(events) == 5
    assert all(e["type"] == "Image/Vision" for e in events)


def test_process_folder_recursive(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "event.txt").write_text("Launch", encoding="utf-8")

    flat = import_events.process_folder(str(tmp_path), llm_client=FakeLlm())
    deep = import_events.process_folder(str(tmp_path), llm_client=FakeLlm(), recursive=True)

    assert flat == []
    assert [e["title"] for e in deep] == ["Launch"]


def test_dedupe_events_drops_same_title_and_start():
    events = [
        {"title": "A", "start": "2026-06-22"},
        {"title": "A", "start": "2026-06-22"},
        {"title": "A", "start": "2026-06-23"},
    ]
    assert import_events.dedupe_events(events) == [
        {"title": "A", "start": "2026-06-22"},
        {"title": "A", "start": "2026-06-23"},
    ]


def test_write_events_json_roundtrips(tmp_path):
    events = [{"title": "X", "start": "2026-06-22", "end": "", "location": "", "source": "s", "type": "ICS"}]
    out = tmp_path / "events.json"

    import_events.write_events_json(events, out)

    assert json.loads(out.read_text(encoding="utf-8")) == events


def test_build_ics_emits_importable_calendar():
    pytest.importorskip("icalendar")
    events = [
        {"title": "Expo", "start": "2026-06-22T10:00", "end": "2026-06-22T18:00",
         "location": "Hall A", "source": "p.pdf", "type": "PDF"},
        {"title": "Bad", "start": "Unknown", "end": "", "location": "", "source": "x", "type": "ICS"},
    ]

    ics = import_events.build_ics(events).decode("utf-8")

    assert "BEGIN:VCALENDAR" in ics
    assert "SUMMARY:Expo" in ics
    assert "LOCATION:Hall A" in ics
    assert "Bad" not in ics  # unparseable start is skipped


def test_verify_sha256_deletes_on_mismatch(tmp_path):
    f = tmp_path / "model.gguf"
    f.write_bytes(b"corrupt")

    with pytest.raises(ValueError):
        import_events._verify_sha256(str(f), "0" * 64)
    assert not f.exists()


def test_verify_sha256_passes_on_match(tmp_path):
    import hashlib
    f = tmp_path / "model.gguf"
    data = b"good-bytes"
    f.write_bytes(data)

    import_events._verify_sha256(str(f), hashlib.sha256(data).hexdigest())

    assert f.exists()


def test_apply_default_tz_attaches_to_naive_datetime(monkeypatch):
    monkeypatch.setattr(import_events, "DEFAULT_TZ", "UTC")
    naive = datetime(2026, 6, 22, 14, 0)

    result = import_events._apply_default_tz(naive)

    assert result.tzinfo is not None


def test_main_writes_json_and_ics(tmp_path, monkeypatch):
    pytest.importorskip("icalendar")
    (tmp_path / "event.txt").write_text("Launch tomorrow", encoding="utf-8")
    out = tmp_path / "out.json"
    ics = tmp_path / "out.ics"
    monkeypatch.setattr(import_events, "get_llm",
                        lambda: FakeLlm('[{"title": "Launch", "start": "2026-06-06"}]'))

    rc = import_events.main([str(tmp_path), "-o", str(out), "--emit-ics", str(ics)])

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data and data[0]["title"] == "Launch"
    assert "SUMMARY:Launch" in ics.read_text(encoding="utf-8")
