#!/usr/bin/env python3

import json
from pathlib import Path
from datetime import date, datetime, timezone

import pytest
from hypothesis import given, strategies as st

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


def test_coerce_start_prefers_explicit_start():
    assert import_events._coerce_start({"start": "2026-06-22T14:00"}) == "2026-06-22T14:00"


def test_coerce_start_joins_only_well_shaped_date_and_time():
    assert import_events._coerce_start(
        {"date": "2026-06-22", "time": "14:00"}
    ) == "2026-06-22T14:00"
    assert import_events._coerce_start(
        {"date": "2026-06-22", "time": "14:00:30"}
    ) == "2026-06-22T14:00:30"


def test_coerce_start_does_not_build_garbage_strings():
    # Malformed date/time must not be concatenated into "fooTbar" forms.
    assert import_events._coerce_start({"date": "next week", "time": "noon"}) == "next week"
    assert import_events._coerce_start({"date": None, "time": None}) == "Unknown"
    assert import_events._coerce_start({}) == "Unknown"


def test_coerce_start_falls_back_to_single_present_field():
    assert import_events._coerce_start({"date": "2026-06-22"}) == "2026-06-22"
    assert import_events._coerce_start({"time": "14:00"}) == "14:00"


@given(
    day=st.one_of(st.none(), st.text(max_size=20)),
    clock=st.one_of(st.none(), st.text(max_size=20)),
    start=st.one_of(st.none(), st.text(max_size=20)),
)
def test_coerce_start_never_emits_none_token(day, clock, start):
    event = {}
    if day is not None:
        event["date"] = day
    if clock is not None:
        event["time"] = clock
    if start is not None:
        event["start"] = start

    result = import_events._coerce_start(event)

    assert isinstance(result, str)
    # Result must be one of the inputs, a validated date+time join, or "Unknown".
    joined = f"{day}T{clock}" if (day and clock) else None
    candidates = {str(start), str(day), str(clock), "Unknown"}
    if joined is not None:
        candidates.add(joined)
    assert result in candidates
    if result == joined:
        d, _, t = result.partition("T")
        assert import_events._DATE_SHAPE.match(d)
        assert import_events._TIME_SHAPE.match(t)


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


ICS_TEMPLATE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:1@test\r\n"
    "DTSTART:20260622T100000\r\n"
    "DTEND:20260622T110000\r\n"
    "SUMMARY:{summary}\r\n"
    "LOCATION:{location}\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_extract_from_ics_reads_utf8(tmp_path):
    pytest.importorskip("icalendar")
    ics = tmp_path / "u.ics"
    ics.write_text(ICS_TEMPLATE.format(summary="Café", location="Açores"),
                   encoding="utf-8")

    events = import_events.extract_from_ics(ics)

    assert events[0]["title"] == "Café"
    assert events[0]["location"] == "Açores"
    assert events[0]["type"] == "ICS"


def test_extract_from_ics_reads_non_utf8_without_dropping(tmp_path):
    pytest.importorskip("icalendar")
    ics = tmp_path / "latin1.ics"
    # Latin-1 bytes would raise UnicodeDecodeError under a hard utf-8 decode.
    ics.write_bytes(
        ICS_TEMPLATE.format(summary="Café", location="").encode("latin-1")
    )

    events = import_events.extract_from_ics(ics)

    assert len(events) == 1
    assert "Caf" in events[0]["title"]


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


def test_parse_iso_accepts_time_without_seconds():
    parsed = import_events._parse_iso("2026-06-22T14:00")
    assert parsed == datetime(2026, 6, 22, 14, 0)


def test_parse_iso_accepts_time_without_seconds_with_offset():
    parsed = import_events._parse_iso("2026-06-22T14:00+02:00")
    assert parsed is not None
    assert parsed.utcoffset() is not None


def test_parse_iso_date_only_and_full_datetime():
    # datetime.fromisoformat is tried first, so a bare date yields midnight.
    assert import_events._parse_iso("2026-06-22") == datetime(2026, 6, 22, 0, 0)
    assert import_events._parse_iso("2026-06-22T14:00:30") == datetime(2026, 6, 22, 14, 0, 30)


def test_parse_iso_rejects_garbage():
    assert import_events._parse_iso("Unknown") is None
    assert import_events._parse_iso("") is None
    assert import_events._parse_iso(None) is None


def test_normalize_iso_pads_only_seconds_less_forms():
    assert import_events._normalize_iso("2026-06-22T14:00") == "2026-06-22T14:00:00"
    assert import_events._normalize_iso("2026-06-22T14:00Z") == "2026-06-22T14:00:00Z"
    assert import_events._normalize_iso("2026-06-22T14:00:30") == "2026-06-22T14:00:30"
    assert import_events._normalize_iso("2026-06-22") == "2026-06-22"
    assert import_events._normalize_iso("garbage") == "garbage"


def test_build_ics_keeps_seconds_less_event():
    pytest.importorskip("icalendar")
    events = [{"title": "Sync", "start": "2026-06-22T14:00", "end": "2026-06-22T15:00",
               "location": "", "source": "s", "type": "Text/LLM"}]

    ics = import_events.build_ics(events).decode("utf-8")

    assert "SUMMARY:Sync" in ics


@given(
    h=st.integers(min_value=0, max_value=23),
    m=st.integers(min_value=0, max_value=59),
)
def test_parse_iso_roundtrips_seconds_less_times(h, m):
    value = f"2026-06-22T{h:02d}:{m:02d}"
    parsed = import_events._parse_iso(value)
    assert parsed == datetime(2026, 6, 22, h, m)


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


def test_apply_default_tz_attaches_to_naive_datetime():
    naive = datetime(2026, 6, 22, 14, 0)

    result = import_events._apply_default_tz(naive, "UTC")

    assert result.tzinfo is not None


def test_apply_default_tz_noop_without_tz():
    naive = datetime(2026, 6, 22, 14, 0)
    assert import_events._apply_default_tz(naive, None) is naive
    assert import_events._apply_default_tz(naive) is naive


def test_apply_default_tz_leaves_date_and_aware_untouched():
    d = date(2026, 6, 22)
    assert import_events._apply_default_tz(d, "UTC") is d
    aware = datetime(2026, 6, 22, 14, 0, tzinfo=timezone.utc)
    assert import_events._apply_default_tz(aware, "Europe/Lisbon") is aware


def test_extract_from_ics_applies_default_tz(tmp_path):
    pytest.importorskip("icalendar")
    ics = tmp_path / "tz.ics"
    ics.write_text(ICS_TEMPLATE.format(summary="Naive", location=""), encoding="utf-8")

    events = import_events.extract_from_ics(ics, default_tz="UTC")

    assert events[0]["start"].endswith("+00:00")


def test_process_folder_threads_default_tz(tmp_path):
    pytest.importorskip("icalendar")
    (tmp_path / "tz.ics").write_text(
        ICS_TEMPLATE.format(summary="Naive", location=""), encoding="utf-8"
    )

    events = import_events.process_folder(str(tmp_path), default_tz="UTC")

    assert events[0]["start"].endswith("+00:00")


def test_model_config_from_args_threads_values():
    args = import_events.parse_args(
        ["dir", "--model-path", "m.gguf", "--clip-path", "c.gguf",
         "--model-sha256", "a" * 64, "--clip-sha256", "b" * 64]
    )

    cfg = import_events.ModelConfig.from_args(args)

    assert cfg.model_path == "m.gguf"
    assert cfg.clip_path == "c.gguf"
    assert cfg.model_sha256 == "a" * 64
    assert cfg.clip_sha256 == "b" * 64


def test_model_config_defaults_match_module_constants():
    cfg = import_events.ModelConfig()
    assert cfg.model_path == import_events.MODEL_PATH
    assert cfg.clip_path == import_events.CLIP_PATH
    assert cfg.model_sha256 == import_events.MODEL_SHA256


def test_ensure_models_exist_uses_config_paths(tmp_path, monkeypatch):
    model = tmp_path / "m.gguf"
    clip = tmp_path / "c.gguf"
    model.write_bytes(b"m")
    clip.write_bytes(b"c")
    calls = []
    monkeypatch.setattr(import_events, "urlretrieve",
                        lambda url, path: calls.append((url, path)))

    cfg = import_events.ModelConfig(model_path=str(model), clip_path=str(clip))
    import_events.ensure_models_exist(cfg)

    assert calls == []  # both exist, nothing downloaded


def test_ensure_models_exist_downloads_missing_from_config(tmp_path, monkeypatch):
    model = tmp_path / "m.gguf"
    clip = tmp_path / "c.gguf"
    clip.write_bytes(b"c")

    def fake_retrieve(url, path):
        Path(path).write_bytes(b"downloaded")

    monkeypatch.setattr(import_events, "urlretrieve", fake_retrieve)

    cfg = import_events.ModelConfig(
        model_path=str(model), clip_path=str(clip), model_url="http://x", clip_url="http://y"
    )
    import_events.ensure_models_exist(cfg)

    assert model.exists()


def test_get_llm_threads_config_paths(monkeypatch):
    captured = {}

    class FakeHandler:
        def __init__(self, clip_model_path):
            captured["clip"] = clip_model_path

    class FakeLlama:
        def __init__(self, model_path, chat_handler, n_ctx):
            captured["model"] = model_path

    import types
    fake_llama_cpp = types.ModuleType("llama_cpp")
    fake_llama_cpp.Llama = FakeLlama
    fake_chat = types.ModuleType("llama_cpp.llama_chat_format")
    fake_chat.Llava15ChatHandler = FakeHandler
    monkeypatch.setitem(__import__("sys").modules, "llama_cpp", fake_llama_cpp)
    monkeypatch.setitem(__import__("sys").modules, "llama_cpp.llama_chat_format", fake_chat)
    monkeypatch.setattr(import_events, "_LLM", None)
    monkeypatch.setattr(import_events, "ensure_models_exist", lambda config=None: None)

    cfg = import_events.ModelConfig(model_path="MM.gguf", clip_path="CC.gguf")
    import_events.get_llm(cfg)

    assert captured == {"clip": "CC.gguf", "model": "MM.gguf"}


def test_main_writes_json_and_ics(tmp_path, monkeypatch):
    pytest.importorskip("icalendar")
    (tmp_path / "event.txt").write_text("Launch tomorrow", encoding="utf-8")
    out = tmp_path / "out.json"
    ics = tmp_path / "out.ics"
    monkeypatch.setattr(
        import_events, "get_llm",
        lambda config=None: FakeLlm('[{"title": "Launch", "start": "2026-06-06"}]'),
    )

    rc = import_events.main([str(tmp_path), "-o", str(out), "--emit-ics", str(ics)])

    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data and data[0]["title"] == "Launch"
    assert "SUMMARY:Launch" in ics.read_text(encoding="utf-8")
