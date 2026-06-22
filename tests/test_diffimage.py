#!/usr/bin/env python3

import diffimage


def test_compare_images_rejects_unreadable_original(monkeypatch):
    class FakeCv2:
        COLOR_BGR2GRAY = object()

        @staticmethod
        def imread(_path):
            return None

    monkeypatch.setattr(diffimage, "_load_image_libs", lambda: None)
    monkeypatch.setattr(diffimage, "cv2", FakeCv2)

    try:
        diffimage.compare_images("missing-a.png", "missing-b.png")
    except FileNotFoundError as ex:
        assert "missing-a.png" in str(ex)
    else:
        raise AssertionError("expected FileNotFoundError")


def test_main_requires_two_paths(capsys):
    assert diffimage.main(["only-one.png"]) == 1
    assert "Usage:" in capsys.readouterr().err
