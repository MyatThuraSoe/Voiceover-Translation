from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))


class TwoRatioTabTests(unittest.TestCase):
    def test_controller_has_a_dedicated_tab_and_no_checkbox_flow(self) -> None:
        ui = (APP / "controller.py").read_text(encoding="utf-8")
        self.assertIn("from two_ratio_tab import TwoRatioTab", ui)
        self.assertIn("TwoRatioTab(self.tools_tabs, app=self)", ui)
        self.assertIn('self.tools_tabs.add(self.two_ratio_tab, text="Two-Ratio")', ui)
        self.assertIn("def start_two_ratio(self, first: dict, second: dict)", ui)
        self.assertIn("render_only=True", ui)
        self.assertNotIn("Generate 2 ratios", ui)
        self.assertNotIn("EDIT 2ND STYLE", ui)
        self.assertNotIn("DONE 2ND STYLE", ui)
        self.assertNotIn("START + 2 RATIOS", ui)

    def test_start_two_ratio_runs_one_full_pass_and_one_render_only_pass(self) -> None:
        from controller import App

        class Var:
            def __init__(self, value=""):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        class FakeApp:
            worker = None
            batch_files = []
            stop_event = threading.Event()
            source_language = Var("English")
            status = Var()
            stage = Var()

            def __init__(self):
                self.cfg = {}
                self.progress = {"value": 0}
                self.batch_manager = mock.Mock(queue=[])
                self.logbox = mock.Mock()
                self.msgq = mock.Mock()
                self.calls: list[dict] = []
                self.done = threading.Event()

            def _running(self, yes):
                pass

            def _log(self, text):
                pass

            def _run_one_job(self, settings, source, ffmpeg, ffprobe, cfg_values, *,
                             batch_mode=False, render_only=False):
                self.calls.append({"render_only": render_only, "settings": dict(settings)})
                return f"final-{len(self.calls)}", Path("job")

        app = FakeApp()
        first = {"source": Path("source.mp4"), "output_ratio": "16:9", "voice": "en-GB-RyanNeural"}
        second = {"source": Path("source.mp4"), "output_ratio": "9:16", "title_size": 77}
        variants = {
            "16x9": {"ratio": "16:9", "output": "source_English_16x9.mp4"},
            "9x16": {"ratio": "9:16", "output": "source_English_9x16.mp4"},
        }
        with mock.patch("controller._tool_path", return_value="ffmpeg.exe"), \
             mock.patch("controller.restore_voice_config", side_effect=lambda job, settings: dict(settings)), \
             mock.patch("controller.load_state", return_value={"variants": variants}):
            App.start_two_ratio(app, first, second)
            assert app.worker is not None
            app.worker.join(timeout=5)
        self.assertFalse(app.worker.is_alive())
        self.assertEqual([call["render_only"] for call in app.calls], [False, True])
        self.assertEqual(app.calls[0]["settings"]["output_ratio"], "16:9")
        self.assertEqual(app.calls[1]["settings"]["output_ratio"], "9:16")
        done_messages: list[str] = []
        for call in app.msgq.put.call_args_list:
            if call.args and isinstance(call.args[0], tuple) and call.args[0][0] == "done":
                done_messages.append(str(call.args[0][1]))
        self.assertTrue(done_messages, msg=str(app.msgq.put.call_args_list))
        self.assertIn("source_English_16x9.mp4", done_messages[0])
        self.assertIn("source_English_9x16.mp4", done_messages[0])

    def test_tab_owns_two_full_style_panels_and_one_start_button(self) -> None:
        tab = (APP / "two_ratio_tab.py").read_text(encoding="utf-8")
        self.assertIn("class TwoRatioTab(ttk.Frame)", tab)
        self.assertIn("class StylePanel(ttk.Frame)", tab)
        self.assertIn("Video A — first render", tab)
        self.assertIn("Video B — second render", tab)
        self.assertIn('text="START 2 VIDEOS"', tab)
        self.assertIn("prepare_two_ratio_settings(", tab)
        self.assertIn("def profile(self) -> dict:", tab)
        for key in ("title_font_size", "title_position", "extra_text_font_size",
                    "extra_text_position", "subtitles", "crop", "color_correction",
                    "audio_ducking"):
            self.assertIn(key, tab)


if __name__ == "__main__":
    unittest.main()
