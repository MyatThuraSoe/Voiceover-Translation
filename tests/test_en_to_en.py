"""English → English mode (2026-11): rewrite the wording instead of translating to Burmese, and narrate with
Edge English voices (en-GB-RyanNeural first). Burmese remains the default and is unchanged."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import controller  # noqa: E402
import core  # noqa: E402
import narration_editor  # noqa: E402
import recap_planner as rp  # noqa: E402
import subtitle_png  # noqa: E402


def _unit(uid: int, start: float, end: float, text: str) -> dict:
    return {"id": uid, "speech_start": start, "speech_end": end, "source_text": text,
            "source_transcript_id": uid - 1, "timestamp": f"{rp._fmt_ts(start)} --> {rp._fmt_ts(end)}"}


class EnglishRewritePromptTests(unittest.TestCase):
    def test_english_prompt_rewrites_and_does_not_translate(self):
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return {"items": [{"id": 1, "translation": "He made his way back to the house that evening."}]}

        with mock.patch.object(rp, "_gemini_json", answer):
            out = rp.translate_batch(
                None, story_context={}, previous_context=[],
                current=[_unit(1, 0.0, 2.0, "He came home that evening.")],
                next_context=[], log=lambda _m: None, target_language="en",
            )
        self.assertEqual(out[0]["translation"], "He made his way back to the house that evening.")
        self.assertEqual(len(prompts), 1)
        self.assertIn("ENGLISH NARRATION REWRITER", prompts[0])
        self.assertIn("CHANGE the WORDING", prompts[0])
        self.assertIn("A near-copy that changes only one or two words is a FAIL", prompts[0])
        self.assertIn("Do not repeat a source phrase", prompts[0])
        self.assertIn("NOT a translation", prompts[0])
        self.assertNotIn("BURMESE NARRATION TRANSLATOR", prompts[0])

    def test_another_script_is_asked_again(self):
        answers = [{"items": [{"id": 1, "translation": "သူ အိမ်ပြန်လာတယ်"}]},
                   {"items": [{"id": 1, "translation": "He finally returned home."}]}]
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with mock.patch.object(rp, "_gemini_json", answer):
            out = rp.translate_batch(
                None, story_context={}, previous_context=[],
                current=[_unit(1, 0.0, 2.0, "He came home.")],
                next_context=[], log=lambda _m: None, target_language="en",
            )
        self.assertEqual(out[0]["translation"], "He finally returned home.")
        self.assertIn("letters of another script inside the English", prompts[1])
        self.assertEqual(rp._non_latin_letters("သူ"), ["သ"])
        self.assertEqual(rp._non_latin_letters("café déjà vu"), [])

    def test_a_copied_line_is_asked_again(self):
        source = "She quietly walked into the room and closed the door behind her."
        answers = [{"items": [{"id": 1, "translation": source}]},
                   {"items": [{"id": 1, "translation": "Without a sound, she stepped in and shut the door."}]}]
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with mock.patch.object(rp, "_gemini_json", answer):
            rp.translate_batch(
                None, story_context={}, previous_context=[],
                current=[_unit(1, 0.0, 2.0, source)],
                next_context=[], log=lambda _m: None, target_language="en",
            )
        self.assertIn("copied unchanged", prompts[1])

    def test_a_near_copy_is_asked_again(self):
        source = "She quietly walked into the room and closed the door behind her."
        near_copy = "She quietly walked into the room and shut the door behind her."
        answers = [{"items": [{"id": 1, "translation": near_copy}]},
                   {"items": [{"id": 1, "translation": "Without a sound, she stepped in and pulled the door shut."}]}]
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with mock.patch.object(rp, "_gemini_json", answer):
            out = rp.translate_batch(
                None, story_context={}, previous_context=[],
                current=[_unit(1, 0.0, 2.0, source)],
                next_context=[], log=lambda _m: None, target_language="en",
            )
        self.assertEqual(len(prompts), 2)
        self.assertIn("copied unchanged or too close to the source", prompts[1])
        self.assertIn("replace shared phrases", prompts[1])
        self.assertEqual(out[0]["translation"], "Without a sound, she stepped in and pulled the door shut.")

    def test_a_reordered_copy_is_asked_again(self):
        source = "The guard opened the gate and the children ran into the yard."
        reordered = "After the gate was opened by the guard, the children ran into the yard."
        answers = [{"items": [{"id": 1, "translation": reordered}]},
                   {"items": [{"id": 1, "translation": "Once the guard had unlocked the gate, the children rushed "
                                                      "into the yard."}]}]
        prompts = []

        def answer(_ai, prompt, **_kw):
            prompts.append(prompt)
            return answers[len(prompts) - 1]

        with mock.patch.object(rp, "_gemini_json", answer):
            rp.translate_batch(
                None, story_context={}, previous_context=[],
                current=[_unit(1, 0.0, 2.0, source)],
                next_context=[], log=lambda _m: None, target_language="en",
            )
        self.assertEqual(len(prompts), 2)
        self.assertIn("copied unchanged or too close to the source", prompts[1])

    def test_english_plan_marks_rewrite(self):
        units = [dict(_unit(1, 1.0, 3.0, "One."), translation="First, one."),
                 dict(_unit(2, 5.0, 7.0, "Two."), translation="Then, two.")]
        plan = rp.make_translation_plan(units, 10.0, source_title="t", transcript={}, story_context={},
                                        planner_mode=rp.PLANNER_MODE_EN, target_language="en")
        self.assertEqual(plan["target_language"], "en")
        self.assertTrue(plan["rewrite_task"])
        self.assertFalse(plan["translation_task"])

    def test_modes_are_separate(self):
        self.assertNotEqual(rp.planner_mode_for("en"), rp.planner_mode_for("mm"))
        self.assertEqual(rp.planner_mode_for("en"), rp.PLANNER_MODE_EN)
        self.assertEqual(rp.planner_mode_for("mm"), rp.PLANNER_MODE)


class EnglishHelpersTests(unittest.TestCase):
    def test_phrase_overlap_detector(self):
        source = "She quietly walked into the room and closed the door behind her."
        self.assertTrue(rp._english_too_close(source, source))
        self.assertTrue(rp._english_too_close(
            source, "She quietly walked into the room and shut the door behind her."))
        self.assertTrue(rp._english_too_close(
            "The guard opened the gate and the children ran into the yard.",
            "After the gate was opened by the guard, the children ran into the yard."))
        self.assertFalse(rp._english_too_close(
            source, "Without a sound, she stepped in and shut the door."))
        self.assertEqual(rp._copied_english_rows([
            dict(_unit(1, 0.0, 1.0, "One."), translation="First, one."),
            dict(_unit(2, 2.0, 3.0, "Two."), translation="Then, two."),
        ]), [])

    def test_note_detection_and_latin_brackets(self):
        self.assertFalse(rp._note_text("He walked home (alone) that night.", "en"))
        self.assertTrue(rp._note_text("continuation of the previous ID", "en"))
        self.assertTrue(rp._note_text("(ရှေ့စာကြောင်း၏ အဆက်)", "en"))
        self.assertTrue(rp._note_text("(ရှေ့စာကြောင်း၏ အဆက်)", "mm"))

    def test_english_brackets_survive_voice_cleanup(self):
        text = "He walked home (alone) that night."
        self.assertEqual(core.spoken_text(text), text)
        self.assertEqual(subtitle_png._clean(text), text)
        self.assertEqual(core.spoken_text("ဂူ (Gu) က ပြန်လာတယ်။"), "ဂူ က ပြန်လာတယ်။")

    def test_english_voice_list_starts_with_uk_ryan(self):
        self.assertEqual(controller.EDGE_VOICES_EN[0], "en-GB-RyanNeural")
        self.assertIn("en-US-GuyNeural", controller.EDGE_VOICES_EN)
        self.assertIn("en-AU-WilliamNeural", controller.EDGE_VOICES_EN)
        self.assertIn("gemini:", " ".join(controller.NARRATOR_VOICES_EN))

    def test_ui_passes_the_target_through(self):
        source = (APP / "controller.py").read_text(encoding="utf-8")
        self.assertIn('self.cfg["target_language"] = target', source)
        self.assertIn('"target_language": settings.get("target_language", "mm")', source)
        self.assertIn('suffix = "English" if settings.get("target_language") == "en" else "Myanmar"', source)

    def test_edit_text_checks_the_right_script(self):
        self.assertEqual(narration_editor.narration_problems("He walked home (alone) that night.", "en"), [])
        self.assertTrue(narration_editor.narration_problems("သူ အိမ်ပြန်လာတယ်", "en"))
        self.assertTrue(narration_editor.narration_problems("...", "en"))
        self.assertTrue(narration_editor.narration_problems("English words here", "mm"))
        self.assertEqual(narration_editor.narration_problems("သူ အိမ်ပြန်လာတယ်", "mm"), [])


if __name__ == "__main__":
    unittest.main()
