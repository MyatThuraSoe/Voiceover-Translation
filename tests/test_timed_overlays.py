"""Per-image start/end windows for Custom Image Overlays.

Run from the project root:
    py -3.12 -m unittest tests.test_timed_overlays -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import core  # noqa: E402
import filter_chain  # noqa: E402
import preset_manager  # noqa: E402


class TimeParsingTests(unittest.TestCase):
    def test_accepts_seconds_mm_ss_and_h_mm_ss(self):
        self.assertEqual(filter_chain.parse_time_seconds("5"), 5.0)
        self.assertEqual(filter_chain.parse_time_seconds(12.5), 12.5)
        self.assertEqual(filter_chain.parse_time_seconds("0:05"), 5.0)
        self.assertEqual(filter_chain.parse_time_seconds("01:02"), 62.0)
        self.assertEqual(filter_chain.parse_time_seconds("1:02:03"), 3723.0)
        self.assertEqual(filter_chain.parse_time_seconds("1:02:03.5"), 3723.5)

    def test_blank_and_invalid_values_use_the_default(self):
        self.assertIsNone(filter_chain.parse_time_seconds(""))
        self.assertIsNone(filter_chain.parse_time_seconds("abc"))
        self.assertIsNone(filter_chain.parse_time_seconds("-1"))
        self.assertEqual(filter_chain.parse_time_seconds("", default=0.0), 0.0)

    def test_format_round_trip(self):
        self.assertEqual(filter_chain.format_time_seconds(None), "")
        self.assertEqual(filter_chain.format_time_seconds(0), "00:00")
        self.assertEqual(filter_chain.format_time_seconds(65.5), "01:05.5")
        self.assertEqual(filter_chain.format_time_seconds(3723), "1:02:03")
        for text in ("0:05", "1:02:03", "12.25"):
            parsed = filter_chain.parse_time_seconds(text)
            self.assertEqual(filter_chain.parse_time_seconds(filter_chain.format_time_seconds(parsed)), parsed)


class OverlayWindowTests(unittest.TestCase):
    def test_legacy_overlay_means_whole_video(self):
        ov = filter_chain.normalize_overlay({"path": "image.png"})
        self.assertEqual(ov["start_sec"], 0.0)
        self.assertIsNone(ov["end_sec"])
        self.assertEqual(filter_chain.overlay_time_window(ov), (0.0, None))
        self.assertEqual(filter_chain.overlay_enable_option(ov), "")

    def test_visibility_is_inclusive_at_both_edges(self):
        ov = filter_chain.normalize_overlay({"path": "image.png", "start_sec": "0:05", "end_sec": "0:12"})
        self.assertFalse(filter_chain.overlay_visible_at(ov, 4.999))
        self.assertTrue(filter_chain.overlay_visible_at(ov, 5.0))
        self.assertTrue(filter_chain.overlay_visible_at(ov, 8.0))
        self.assertTrue(filter_chain.overlay_visible_at(ov, 12.0))
        self.assertFalse(filter_chain.overlay_visible_at(ov, 12.001))

    def test_open_ended_window_starts_at_start_and_never_ends(self):
        ov = filter_chain.normalize_overlay({"path": "image.png", "start_sec": 5})
        self.assertFalse(filter_chain.overlay_visible_at(ov, 4.9))
        self.assertTrue(filter_chain.overlay_visible_at(ov, 3600.0))

    def test_enable_expressions(self):
        bounded = filter_chain.normalize_overlay({"start_sec": 5, "end_sec": 12})
        self.assertEqual(filter_chain.overlay_enable_expr(bounded), "between(t,5.000,12.000)")
        self.assertEqual(filter_chain.overlay_enable_option(bounded), ":enable='between(t,5.000,12.000)'")
        open_ended = filter_chain.normalize_overlay({"start_sec": 5})
        self.assertEqual(filter_chain.overlay_enable_option(open_ended), ":enable='gte(t,5.000)'")
        self.assertEqual(filter_chain.overlay_enable_option({}), "")

    def test_filter_builders_include_the_enable_option(self):
        graph, inputs, out_label, indices = filter_chain.build_overlay_filters(
            [{"path": "image.png", "enabled": True, "start_sec": "0:05", "end_sec": "0:12"}],
            1920, 1080)
        self.assertEqual(inputs, ["image.png"])
        self.assertEqual(indices, [1])
        self.assertIn(":enable='between(t,5.000,12.000)'", graph)
        self.assertIn("eof_action=repeat:enable=", graph)

        chain = filter_chain.FFmpegFilterChain(video_w=1920, video_h=1080)
        chain.add_overlay("image.png", 0.1, 0.1, 0.1, 0.9, "0:05", "")
        graph, inputs = chain.build()
        self.assertEqual(inputs, ["image.png"])
        self.assertIn(":enable='gte(t,5.000)'", graph)


class ProductionRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_core_graph_gates_each_image(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is required for the image verification step")
        image_path = self.root / "image.png"
        Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(image_path)
        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        output = self.root / "output.mp4"
        overlay = {
            "path": str(image_path), "enabled": True, "x_percent": 0.1, "y_percent": 0.1,
            "width_percent": 0.2, "opacity": 0.8, "start_sec": "0:05", "end_sec": "0:12",
        }

        def fake_run(cmd, *_args, **_kwargs):
            captured.append(list(cmd))
            output.write_bytes(b"0" * 6000)
            return SimpleNamespace(returncode=0, stderr="")

        captured: list[list[str]] = []
        with patch.object(core.subprocess, "run", side_effect=fake_run):
            core.apply_production_overlays(
                "ffmpeg.exe", source, output, {"custom_overlays": [overlay]},
                "libx264", self.root)
        self.assertEqual(len(captured), 1)
        cmd = captured[0]
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn(":enable='between(t,5.000,12.000)'", graph)
        self.assertIn("eof_action=repeat:enable=", graph)

    def test_core_graph_has_no_enable_for_whole_video_overlay(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is required for the image verification step")
        image_path = self.root / "image.png"
        Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(image_path)
        source = self.root / "input.mp4"
        source.write_bytes(b"source")
        output = self.root / "output.mp4"
        captured: list[list[str]] = []

        def fake_run(cmd, *_args, **_kwargs):
            captured.append(list(cmd))
            output.write_bytes(b"0" * 6000)
            return SimpleNamespace(returncode=0, stderr="")

        with patch.object(core.subprocess, "run", side_effect=fake_run):
            core.apply_production_overlays(
                "ffmpeg.exe", source, output,
                {"custom_overlays": [{"path": str(image_path), "enabled": True}]},
                "libx264", self.root)
        graph = captured[0][captured[0].index("-filter_complex") + 1]
        self.assertNotIn(":enable=", graph)


class PresetCompatibilityTests(unittest.TestCase):
    def test_old_preset_gains_timing_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = preset_manager.PresetManager(Path(tmp), log=lambda _message: None)

            class FakeController:
                def __init__(self):
                    self._pending_preset = {"custom_overlays": [{"path": "old.png", "enabled": True}]}
                    self.custom_overlays = []

            controller = FakeController()
            manager.apply_to_controller(controller)
            ov = controller.custom_overlays[0]
            self.assertEqual(ov["start_sec"], 0.0)
            self.assertIsNone(ov["end_sec"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
