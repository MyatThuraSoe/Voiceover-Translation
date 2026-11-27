"""V2 feature tests (spec §14): presets, filter order, crop, overlays, color,
audio ducking, batch queue and the TTS fallback chain.

Run from the project root:
    py -3.12 -m unittest tests.test_v2_features -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import batch_manager  # noqa: E402
import crop_detect  # noqa: E402
import filter_chain  # noqa: E402
import preset_manager  # noqa: E402
import tts_engine  # noqa: E402


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_load_delete_and_default(self):
        manager = preset_manager.PresetManager(self.root, log=lambda _message: None)
        settings = {"output_ratio": "9:16", "zoom_factor": 1.2, "mirror_video": True,
                    "edge_pitch_hz": -10,
                    "crop": {"enabled": True, "x_percent": 0.1, "y_percent": 0.2,
                             "width_percent": 0.8, "height_percent": 0.6}}
        stored = manager.save_preset("  My   Shorts ", settings)
        self.assertEqual(stored, "My Shorts")
        # A second manager reads the file from disk: the preset survives a restart.
        reloaded = preset_manager.PresetManager(self.root, log=lambda _message: None)
        canonical = reloaded.find_preset("my shorts")
        self.assertEqual(canonical, "My Shorts")
        self.assertEqual(reloaded.get_settings(canonical), {
            "output_ratio": "9:16", "zoom_factor": 1.2, "mirror_video": True, "edge_pitch_hz": -10,
            "crop": {
                "enabled": True, "x_percent": 0.1, "y_percent": 0.2,
                "width_percent": 0.8, "height_percent": 0.6}})
        reloaded.set_default("My Shorts")
        self.assertEqual(reloaded.get_default_name(), "My Shorts")
        self.assertTrue(reloaded.delete_preset("My Shorts"))
        self.assertEqual(reloaded.get_default_name(), "")

    def test_blur_boxes_are_included_in_preset_round_trip(self):
        manager = preset_manager.PresetManager(self.root, log=lambda _message: None)
        boxes = [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}]
        manager.save_preset("Blurred", {"output_ratio": "16:9", "blur_boxes": boxes})
        reloaded = preset_manager.PresetManager(self.root, log=lambda _message: None)
        self.assertEqual(reloaded.get_settings("Blurred")["blur_boxes"], boxes)

    def test_title_background_survives_preset_round_trip(self):
        manager = preset_manager.PresetManager(self.root, log=lambda _message: None)
        manager.save_preset("No title box", {"title_box_enabled": False, "title_box_opacity": 20})
        reloaded = preset_manager.PresetManager(self.root, log=lambda _message: None)
        settings = reloaded.get_settings("No title box")
        self.assertIs(settings["title_box_enabled"], False)
        self.assertEqual(settings["title_box_opacity"], 20)

    def test_factory_presets_are_protected(self):
        manager = preset_manager.PresetManager(self.root, log=lambda _message: None)
        names = manager.get_preset_names()
        self.assertIn("YouTube Long Form", names)
        self.assertIn("TikTok / Reels Viral", names)
        self.assertIn("Facebook Square", names)
        self.assertTrue(manager.is_factory("YouTube Long Form"))
        self.assertFalse(manager.delete_preset("YouTube Long Form"))
        with self.assertRaises(ValueError):
            manager.save_preset("youtube long form", {"output_ratio": "1:1"})


class FilterChainTests(unittest.TestCase):
    def test_chain_order_is_always_the_same(self):
        chain = filter_chain.FFmpegFilterChain(video_w=1920, video_h=1080)
        chain.add_crop({"enabled": True, "x_percent": 0.0, "y_percent": 0.1,
                        "width_percent": 1.0, "height_percent": 0.8})
        chain.add_color({"enabled": True, "brightness": 0.1, "contrast": 1.1, "saturation": 1.2})
        chain.add_zoom_mirror(1.1, True)
        chain.add_overlay("watermark.png", 0.8, 0.05, 0.1, 0.9)
        self.assertEqual(chain.steps, ["crop", "color", "zoom_mirror", "overlays"])
        self.assertTrue(chain.filters[0].startswith("crop="))
        self.assertTrue(chain.filters[1].startswith("eq="))

    def test_crop_filter_rounds_to_even_numbers(self):
        value = filter_chain.build_crop_filter(
            {"enabled": True, "x_percent": 0.01, "y_percent": 0.01,
             "width_percent": 0.5, "height_percent": 0.5}, 1921, 1081)
        self.assertIsNotNone(value)
        numbers = [int(part) for part in value.removeprefix("crop=").split(":")]
        self.assertTrue(all(number % 2 == 0 for number in numbers), value)

    def test_full_frame_crop_is_disabled(self):
        self.assertIsNone(filter_chain.build_crop_filter({"enabled": True}, 1920, 1080))
        self.assertIsNone(filter_chain.build_crop_filter({"enabled": False}, 1920, 1080))

    def test_overlay_positions_are_resolution_independent(self):
        overlay = {"path": "logo.png", "x_percent": 0.85, "y_percent": 0.05,
                   "width_percent": 0.1, "opacity": 0.9, "enabled": True}
        x1, y1, w1 = filter_chain.overlay_pixel_geometry(overlay, 1920, 1080)
        x2, y2, w2 = filter_chain.overlay_pixel_geometry(overlay, 3840, 2160)
        self.assertEqual((x1, y1, w1), (1632, 54, 192))
        self.assertEqual((x2, y2, w2), (3264, 108, 384))
        clamped = dict(overlay, x_percent=1.0, y_percent=1.0)
        x, y, width = filter_chain.overlay_pixel_geometry(clamped, 1920, 1080)
        self.assertLessEqual(x + width, 1920)
        self.assertLess(y, 1080)

    def test_color_filter(self):
        self.assertEqual(
            filter_chain.build_color_filter({"enabled": True, "brightness": 0.06,
                                             "contrast": 1.1, "saturation": 1.3}),
            "eq=brightness=0.060:contrast=1.100:saturation=1.300")
        self.assertIsNone(filter_chain.build_color_filter({"enabled": False}))
        self.assertIsNone(filter_chain.build_color_filter(
            {"enabled": True, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0}))

    def test_prefilter_returns_effective_crop_size(self):
        prefilter, size = filter_chain.build_prefilter(
            {"enabled": True, "x_percent": 0.0, "y_percent": 0.1,
             "width_percent": 1.0, "height_percent": 0.8},
            {"enabled": True, "brightness": 0.1, "contrast": 1.0, "saturation": 1.0},
            1920, 1080)
        self.assertTrue(prefilter.startswith("crop="))
        self.assertIn("eq=brightness", prefilter)
        self.assertEqual(size, (1920, 864))


class CropDetectTests(unittest.TestCase):
    def test_parse_and_median(self):
        text = "crop=1920:800:0:140\ncrop=1920:800:0:140\ncrop=1920:802:0:142\n"
        crops = crop_detect.parse_cropdetect_output(text)
        self.assertEqual(len(crops), 3)
        self.assertEqual(crop_detect.median_crop(crops), (1920, 800, 0, 140))
        percent = crop_detect.crop_to_percent(1920, 800, 0, 140, 1920, 1080)
        self.assertAlmostEqual(percent["height_percent"], 800 / 1080)
        self.assertAlmostEqual(percent["y_percent"], 140 / 1080)


class DuckingTests(unittest.TestCase):
    def test_expression_has_fades_and_never_disables_pauses(self):
        value = filter_chain.build_ducking_filter(
            [{"audio_start": 1.0, "audio_end": 3.0}, {"audio_start": 5.0, "audio_end": 6.0}],
            0.3, 200)
        self.assertIsNotNone(value)
        self.assertIn("volume='", value)
        self.assertIn("eval=frame", value)
        self.assertIn("1-0.700*", value)
        self.assertIn("between", value + "between")  # windows are part of the ramp math
        self.assertIn("max(", value)

    def test_no_segments_or_full_volume_returns_none(self):
        self.assertIsNone(filter_chain.build_ducking_filter([], 0.3))
        self.assertIsNone(filter_chain.build_ducking_filter(
            [{"audio_start": 0.0, "audio_end": 1.0}], 1.0))


class BatchTests(unittest.TestCase):
    def test_add_move_remove_and_summary(self):
        manager = batch_manager.BatchManager()
        root = Path(tempfile.gettempdir())
        added = manager.add_files([root / "a.mp4", root / "b.mp4", root / "c.mp4"])
        self.assertEqual(len(added), 3)
        self.assertEqual(manager.add_files([root / "a.mp4"]), [])  # duplicate ignored
        self.assertTrue(manager.move_down(0))
        self.assertEqual([item.path.name for item in manager.queue], ["b.mp4", "a.mp4", "c.mp4"])
        self.assertTrue(manager.move_up(2))
        self.assertEqual([item.path.name for item in manager.queue], ["b.mp4", "c.mp4", "a.mp4"])
        self.assertTrue(manager.move_to(0, 2))
        self.assertEqual([item.path.name for item in manager.queue], ["c.mp4", "a.mp4", "b.mp4"])
        manager.mark_done(manager.item_at(0), root / "out.mp4")
        manager.mark_failed(manager.item_at(1), "boom")
        summary = manager.get_summary()
        self.assertEqual((summary["total"], summary["done"], summary["failed"],
                          summary["pending"]), (3, 1, 1, 1))
        skipped_item = manager.item_at(2)
        skipped_item.skip("already completed")
        self.assertEqual(skipped_item.status, "skipped")
        self.assertIn("Skipped", skipped_item.row_values()[1])
        self.assertEqual(manager.get_summary()["skipped"], 1)
        self.assertEqual(manager.retry_failed(), 2)
        self.assertEqual(manager.item_at(1).status, "pending")
        self.assertEqual(manager.item_at(2).status, "pending")
        manager.remove_item(0)
        self.assertEqual(len(manager), 2)


class _FakeEngine(tts_engine.TTSEngineBase):
    def __init__(self, name, fail=False, available=True):
        self.name = name
        self.fail = fail
        self.available = available
        self.calls = 0

    def generate(self, text, voice, output_path, **kwargs):
        return Path(output_path)

    def get_voices(self):
        return [{"id": self.name, "label": self.name}]

    def is_available(self):
        return self.available

    def generate_channel99_voice(self, text, voice):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        return f"{self.name}.wav"


class TTSFallbackTests(unittest.TestCase):
    def test_falls_back_to_next_available_engine(self):
        logs = []
        primary = _FakeEngine("mms_myanmar", fail=True)
        fallback = _FakeEngine("edge_tts")
        chain = tts_engine.TTSFallbackChain([primary, fallback], primary="mms_myanmar",
                                            log=logs.append, max_tries=1)
        self.assertEqual(chain.generate_channel99_voice("hello", "voice"), "edge_tts.wav")
        self.assertEqual(primary.calls, 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(chain.active, "edge_tts")
        self.assertTrue(any("falling back" in line for line in logs))

    def test_unavailable_engine_is_skipped(self):
        unavailable = _FakeEngine("mms_myanmar", available=False)
        fallback = _FakeEngine("edge_tts")
        chain = tts_engine.TTSFallbackChain([unavailable, fallback], primary="mms_myanmar",
                                            log=lambda _line: None, max_tries=3)
        self.assertEqual(chain.generate_channel99_voice("hello", "voice"), "edge_tts.wav")
        self.assertEqual(unavailable.calls, 0)

    def test_unknown_primary_uses_the_first_available_engine(self):
        logs = []
        edge = _FakeEngine("edge_tts")
        chain = tts_engine.TTSFallbackChain([edge], primary="gemini_tts",
                                            log=logs.append, max_tries=1)
        self.assertEqual(chain.primary, "edge_tts")
        self.assertEqual(chain.generate_channel99_voice("hello", "voice"), "edge_tts.wav")
        self.assertTrue(any("not available" in line for line in logs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
