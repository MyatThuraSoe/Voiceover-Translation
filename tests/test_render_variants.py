from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import core
from render_variants import (apply_profile_to_settings, load_state, missing_artifacts,
                             profile_from_settings, ratio_key, record_variant,
                             restore_voice_config, variant_filename)

RATE = 24000


def make_clip(path: Path, seconds: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        frames = []
        for i in range(int(seconds * RATE)):
            t = i / RATE
            frames.append(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * t))))
        writer.writeframes(b"".join(frames))


class RenderVariantHelpersTests(unittest.TestCase):
    def test_ratio_keys_and_filenames(self) -> None:
        self.assertEqual(ratio_key("9:16"), "9x16")
        self.assertEqual(ratio_key("Original"), "Original")
        self.assertEqual(variant_filename("my video", "1:1", "English"), "my_video_English_1x1.mp4")

    def test_profile_round_trip_keeps_text_size_and_position(self) -> None:
        settings = {
            "output_ratio": "9:16",
            "frame_fill": "crop",
            "overlays": {
                "title_font_size": 72,
                "title_position": [0.5, 0.08],
                "extra_text_font_size": 58,
                "extra_text_position": [0.5, 0.9],
                "subtitles": {"size": 60, "position": [0.5, 0.84], "enabled": True},
            },
        }
        profile = profile_from_settings(settings)
        self.assertEqual(profile["title_size"], 72)
        self.assertEqual(profile["extra_text_size"], 58)

        merged = apply_profile_to_settings({"output_ratio": "16:9", "overlays": {}}, profile)
        self.assertEqual(merged["output_ratio"], "9:16")
        self.assertEqual(merged["frame_fill"], "crop")
        self.assertEqual(merged["overlays"]["title_font_size"], 72)
        self.assertEqual(merged["overlays"]["extra_text_font_size"], 58)
        self.assertEqual(merged["overlays"]["subtitles"]["position"], [0.5, 0.84])

    def test_missing_artifacts_and_saved_voice_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            job = Path(td) / "job"
            self.assertIn("script / timeline", missing_artifacts(job))
            edge = job / "edge_tts_smart_sync"
            edge.mkdir(parents=True)
            (job / "smart_sync_plan.json").write_text("p" * 1200, encoding="utf-8")
            (edge / "smart_sync_plan_edge_tts.json").write_text("p" * 1200, encoding="utf-8")
            (edge / "voice_edge_tts.wav").write_bytes(b"a" * 1200)
            self.assertEqual(missing_artifacts(job), [])

            final = edge / "final_edge_tts_smart_sync.mp4"
            final.write_bytes(b"v" * 8000)
            settings = {
                "source": job / "source.mp4",
                "output_ratio": "9:16",
                "target_language": "en",
                "voice": "en-GB-RyanNeural",
                "edge_tts_rate": "+10%",
                "title_size": 60,
                "overlays": {"title_font_size": 60},
            }
            variant = record_variant(job, settings, final, "source", "English")
            self.assertTrue(variant.is_file())
            self.assertEqual(variant.name, "source_English_9x16.mp4")
            state = load_state(job)
            self.assertIn("9x16", state["variants"])
            self.assertEqual(state["voice"]["edge_tts_rate"], "+10%")

            restored = restore_voice_config(job, {"voice": "wrong-voice", "edge_tts_rate": "-30%"})
            self.assertEqual(restored["voice"], "en-GB-RyanNeural")
            self.assertEqual(restored["edge_tts_rate"], "+10%")


class RenderOnlyPipelineTests(unittest.TestCase):
    """A second ratio render must never call Gemini/Whisper/TTS again."""

    def _plan(self, path: Path) -> None:
        path.write_text(json.dumps({"segments": [
            {"segment_id": "S0001", "video_start": 0.0, "video_end": 1.4, "narration": "First line."},
            {"segment_id": "S0002", "video_start": 1.4, "video_end": 3.0, "narration": "Second line."},
        ]}, ensure_ascii=False), encoding="utf-8")

    def _run(self, job: Path, source: Path, plan: Path, *, render_only: bool, ratio: str) -> dict:
        runner = core.EdgeSmartSync(
            job_dir=str(job), source_path=str(source), plan_path=str(plan),
            voice="my-MM-ThihaNeural", ffmpeg_path="/bin/true", ffprobe_path="/bin/true",
            render_final_video=True, render_only=render_only, output_ratio=ratio,
            mirror_video=False, zoom_factor=1.0, max_video_speed=1.25)

        class FakeEngine:
            created = 0

            def __init__(self, *, ffmpeg, work_dir, rate, pitch, log):
                FakeEngine.created += 1
                self.work_dir = Path(work_dir)
                self.work_dir.mkdir(parents=True, exist_ok=True)
                self.n = 0

            def generate_channel99_voice(self, text, voice):
                if render_only:
                    raise AssertionError("TTS was called during a render-only pass")
                self.n += 1
                out = self.work_dir / f"fake_{self.n}.wav"
                make_clip(out, 1.0 if self.n == 1 else 1.6)
                return str(out)

        def probe(_ffprobe, path):
            try:
                with wave.open(str(path), "rb") as reader:
                    return reader.getnframes() / reader.getframerate()
            except Exception:
                return 9.0

        def fake_render(_ffmpeg, _source, dst, _seg, target, _encoder, *_args, **_kwargs):
            Path(dst).write_bytes(b"v" * 6000)
            return {"hybrid_used": 0.0, "applied_factor": 1.0}

        with mock.patch.object(core, "EdgeTTSEngine", FakeEngine), \
             mock.patch.object(core, "probe_duration", side_effect=probe), \
             mock.patch.object(core, "concat_audio", side_effect=lambda _f, _p, out, _w: Path(out).write_bytes(b"a" * 2000)), \
             mock.patch.object(core, "ffmpeg_encoders", return_value={"libx264"}), \
             mock.patch.object(core, "render_video_part", side_effect=fake_render), \
             mock.patch.object(core, "concat_video_parts", side_effect=lambda _f, _p, out, _w: Path(out).write_bytes(b"v" * 6000)), \
             mock.patch.object(core, "mux_audio", side_effect=lambda _f, _v, _a, out, *_x: Path(out).write_bytes(b"m" * 6000)):
            result = runner.run()
        self.assertEqual(FakeEngine.created, 0 if render_only else 1)
        return result

    def test_second_ratio_reuses_voice_and_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            job = base / "job"
            job.mkdir()
            source = base / "source.mp4"
            source.write_bytes(b"source")
            plan = job / "smart_sync_plan.json"
            self._plan(plan)

            first = self._run(job, source, plan, render_only=False, ratio="16:9")
            self.assertTrue(Path(first["final"]).is_file())
            saved_plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved_plan["tts_timing_segments"]), 2)

            second = self._run(job, source, plan, render_only=True, ratio="9:16")
            self.assertTrue(Path(second["final"]).is_file())
            self.assertEqual(second["segments"], 2)

    def test_render_only_refuses_to_synthesize_a_missing_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            job = base / "job"
            job.mkdir()
            source = base / "source.mp4"
            source.write_bytes(b"source")
            plan = job / "smart_sync_plan.json"
            self._plan(plan)
            self._run(job, source, plan, render_only=False, ratio="16:9")

            parts = sorted((job / "edge_tts_smart_sync" / "tts_parts").glob("*.wav"))
            self.assertTrue(parts)
            parts[0].unlink()
            with self.assertRaisesRegex(RuntimeError, "Render Another Ratio must reuse"):
                self._run(job, source, plan, render_only=True, ratio="9:16")


if __name__ == "__main__":
    unittest.main()
