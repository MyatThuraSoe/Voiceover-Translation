"""Preview sizing, interaction and final-render fidelity regressions."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for folder in (ROOT, APP):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import core  # noqa: E402
import preview_tools  # noqa: E402


class SourceTests(unittest.TestCase):
    def test_two_ratio_preview_is_adjustable_and_interactive(self) -> None:
        tab = (APP / "two_ratio_tab.py").read_text(encoding="utf-8")
        self.assertIn("PREVIEW_HEIGHTS = (280, 400, 540, 680)", tab)
        self.assertIn('text="Preview size"', tab)
        self.assertIn('self.canvas.bind("<MouseWheel>", self._wheel_seek)', tab)
        self.assertIn("two_ratio_preview_zoom", tab)
        self.assertIn('_tag_index(tag, "custom_")', tab)
        self.assertIn("_apply_crop_values(crop, notify=False, redraw=False)", tab)
        self.assertIn("if self._drag_dirty:", tab)
        self.assertIn("cursor=\"fleur\"", tab)
        self.assertNotIn("cursor=\"move\"", tab)
        self.assertIn('(idx := _tag_index(tag, "crop_handle_")) is not None and idx < 8', tab)
        self.assertIn("scroller.yview_moveto(0.0)", tab)

    def test_wheel_helper_leaves_preview_canvases_alone(self) -> None:
        controller = (APP / "controller.py").read_text(encoding="utf-8")
        self.assertIn("if isinstance(widget, (tk.Canvas, ttk.Scale, ttk.Combobox, ttk.Spinbox)):",
                      controller)
        self.assertIn("widget.bind(\"<MouseWheel>\", lambda e: canvas.yview_scroll", controller)
        self.assertNotIn('add="+")\n\n    def _bind_wheel_recursive', controller)

    def test_preview_text_uses_the_final_png_renderer(self) -> None:
        tab = (APP / "two_ratio_tab.py").read_text(encoding="utf-8")
        self.assertIn("_render_title_png", tab)
        self.assertIn("_render_extra_text_png", tab)
        self.assertIn('anchor="center", tags=("editor", "title")', tab)
        self.assertIn("# Match the final filter graph: blur → images → title → logo → extra → subtitles.", tab)
        self.assertIn("blur_boxes=[dict(b) for b in self.panel.blur_boxes]", tab)
        self.assertIn("round(int(self.panel.title_size.get()) * pw / max(1, self._frame_size[0]))", tab)
        self.assertIn("round(int(self.panel.extra_size.get()) * pw / max(1, self._frame_size[0]))", tab)
        self.assertIn("_render_extra_text_png(text, out, size, 0.18, max_width=max_width,", tab)
        self.assertIn('rgba.putalpha(rgba.getchannel("A").point(lambda a: int(a * opacity)))', tab)

    def test_blur_boxes_survive_presets_and_ratio_switches(self) -> None:
        preset = (APP / "preset_manager.py").read_text(encoding="utf-8")
        controller = (APP / "controller.py").read_text(encoding="utf-8")
        self.assertIn('"subtitles", "crop", "blur_boxes", "custom_overlays"', preset)
        self.assertIn('"blur_boxes": [dict(b) for b in getattr(controller, "blur_boxes", [])]', preset)
        self.assertIn('controller.blur_boxes = [dict(b) for b in (settings["blur_boxes"] or [])', preset)
        self.assertIn('if "blur_boxes" in profile and hasattr(self, "blur_boxes"):', controller)

    def test_title_shrink_never_grows_past_requested_size(self) -> None:
        service = (ROOT / "services" / "text_overlay_service.py").read_text(encoding="utf-8")
        self.assertIn("Math.max(1, Math.min(", service)
        self.assertIn("const minSize=Math.max(1, Math.min({int(min_font_size)}, size));", service)

    def test_blur_radius_is_scaled_for_lower_quality(self) -> None:
        source = (APP / "core.py").read_text(encoding="utf-8")
        self.assertIn("blur_strength = max(2, min(40, int(round(max(4, min(40, int(overlays.get(\"blur_strength\", 18)))) * k))))",
                      source)

    def test_configured_myanmar_font_is_honoured(self) -> None:
        controller = (APP / "controller.py").read_text(encoding="utf-8")
        self.assertIn("configured_font = str(self.cfg.get(\"title_font_path\") or \"\").strip()", controller)
        self.assertIn("bool(latin_font or (configured_font and Path(configured_font).is_file()))", controller)


class RendererTests(unittest.TestCase):
    def test_title_panel_is_sized_for_requested_font(self) -> None:
        captured: dict = {}

        def fake_render(text, out_path, cfg):
            captured.update(cfg)
            return out_path

        with patch("services.text_overlay_service.make_video_title_png", side_effect=fake_render):
            core._render_title_png("A two line title", Path("unused.png"), "", 54, max_width=1497)
        self.assertGreaterEqual(captured["panel_width"], 1460)
        self.assertGreaterEqual(captured["video_title_panel_height"], 180)
        self.assertGreaterEqual(captured["min_title_font_size"], 8)

    def test_preview_blur_graph_scales_radius_to_preview_width(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "frame.png"

            def fake_run(cmd, **_kwargs):
                output.write_bytes(b"png")
                return SimpleNamespace(returncode=0, stderr="")

            with patch.object(preview_tools.subprocess, "run", side_effect=fake_run) as run:
                preview_tools.extract_preview_frame(
                    "ffmpeg", "source.mp4", 1.0, 1.0, False, str(output),
                    640, 360, blur_boxes=[{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25}],
                    blur_strength=18, frame_size=(1920, 1080),
                )
            cmd = run.call_args.args[0]
        self.assertIn("-filter_complex", cmd)
        graph = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("split=2", graph)
        self.assertIn("boxblur=luma_radius=6:luma_power=2:chroma_radius=6", graph)
        self.assertIn("overlay=64:72", graph)


if __name__ == "__main__":
    unittest.main()
