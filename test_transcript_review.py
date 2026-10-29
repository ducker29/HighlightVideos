"""Truthful clip/context rendering, including the c002 padding regression."""
import copy
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest

from job_store import load_json, save_json
from renderer import approval_key, clip_text, output_filename
from review_ui import HighlightApp
from transcript_review import END_MARKER, START_MARKER, review_sections, transcript_chunks


def text_of(sections):
    return "".join(text for tag, text in transcript_chunks(sections))


def inside_text(text):
    return text.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]


def sentence(i):
    return {"id": f"s{i}", "start": i * 10, "end": (i + 1) * 10, "text": f"Реплика {i}."}


class TranscriptReviewTests(unittest.TestCase):
    def test_export_matches_dialogue_view_without_technical_data(self):
        transcript = {"sentences": [sentence(i) for i in range(12)]}
        clip = {"candidate_id": "c001", "title": "Название", "reason": "Законченная мысль",
                "start": 40, "end": 70, "approval_key": "secret"}
        text = clip_text(clip, transcript)
        expected = text_of(review_sections(transcript, 40, 70))
        technical = "Границы исходного видео: 00:00:40.000–00:01:10.000. Разметка ASR приблизительная.\n"
        self.assertEqual(text, "Название клипа: Название\n\nПочему выбран: Законченная мысль\n\n"
                         + expected.replace(technical, ""))
        self.assertNotIn("secret", text)
        clip["start"] = 45
        partial = clip_text(clip, transcript)
        self.assertIn("НЕПОЛНАЯ РЕПЛИКА", partial)
        self.assertNotIn("word-level", partial)
        with tempfile.TemporaryDirectory() as directory:
            name = output_filename(clip, directory)
            Path(directory, name).with_suffix(".txt").write_text("Старый текст", encoding="utf-8")
            self.assertNotEqual(output_filename(clip, directory), name)

    def test_three_before_and_after_are_outside_markers(self):
        transcript = {"sentences": [sentence(i) for i in range(12)]}
        original = copy.deepcopy(transcript)
        view = review_sections(transcript, 40, 70)
        self.assertEqual([s["id"] for s in view["before"]], ["s1", "s2", "s3"])
        self.assertEqual([r["sentence"]["id"] for r in view["rows"]], ["s4", "s5", "s6"])
        self.assertEqual([s["id"] for s in view["after"]], ["s7", "s8", "s9"])
        text = text_of(view)
        body = inside_text(text)
        self.assertIn("Реплика 4.", body)
        self.assertIn("Реплика 6.", body)
        self.assertNotIn("Реплика 3.", body)
        self.assertNotIn("Реплика 7.", body)
        self.assertLess(text.index("Реплика 3."), text.index(START_MARKER))
        self.assertGreater(text.index("Реплика 7."), text.index(END_MARKER))
        self.assertEqual(transcript, original)

    def test_c002_next_sentence_is_not_promised_by_half_second_padding(self):
        last = {"id": "s187", "start": 849.45, "end": 862.45, "text": "Занимался партийной дисциплиной, казной, философствованиями."}
        following = {"id": "s188", "start": 862.47, "end": 868.65,
                     "text": "Именно Дитрих Эккарт разглядел в Гитлере потенциал, который потом будет выплеснут в политику."}
        later = [{"id": "s189", "start": 868.71, "end": 879.99, "text": "Следующая история."},
                 {"id": "s190", "start": 880.09, "end": 883.69, "text": "Ещё реплика."},
                 {"id": "s191", "start": 883.95, "end": 887.29, "text": "Четвёртая реплика."}]
        transcript = {"sentences": [last, following] + later,
                      "segments": [{"start": 862.47, "end": 868.65,
                                    "words": [{"word": " Именно", "start": 862.47, "end": 863.05}]}]}
        view = review_sections(transcript, 695.97, 862.95)
        text = text_of(view)
        body = inside_text(text)
        self.assertIn(last["text"], body)
        self.assertNotIn(following["text"], body)
        self.assertIn("НЕПОЛНАЯ РЕПЛИКА", body)
        self.assertIn("Граница проходит внутри слов: «Именно»", body)
        self.assertIn(following["text"], text.split(END_MARKER)[1])
        self.assertEqual([s["id"] for s in view["after"]], ["s188", "s189", "s190"])
        self.assertIn("попало только 0.480 с", text)
        self.assertNotIn(later[-1]["text"], text)

    def test_partial_start_shows_only_whole_words_and_context(self):
        transcript = {"sentences": [sentence(0), dict(sentence(1), text="До часть входит полностью."), sentence(2), sentence(3)],
                      "segments": [{"start": 10, "end": 20, "words": [
                          {"word": "До", "start": 10, "end": 12},
                          {"word": "часть", "start": 12, "end": 15},
                          {"word": "входит", "start": 15, "end": 17},
                          {"word": "полностью.", "start": 17, "end": 20}]}]}
        view = review_sections(transcript, 14, 40)
        body = inside_text(text_of(view))
        self.assertIn("входит полностью.", body)
        self.assertNotIn("До часть входит полностью.", body)
        self.assertIn("часть", view["rows"][0]["cut_words"])
        self.assertEqual(view["before"][-1]["id"], "s1")

    def test_single_partial_sentence_without_word_times_is_context_only(self):
        source = {"id": "s0", "start": 0, "end": 100, "text": "Длинная реплика без разметки отдельных слов."}
        view = review_sections({"sentences": [source]}, 20, 40)
        text = text_of(view)
        self.assertNotIn(source["text"], inside_text(text))
        self.assertIn("Нет word-level разметки", inside_text(text))
        self.assertEqual(text.count(source["text"]), 1)
        self.assertEqual(view["after"], [source])

    def test_source_edges_and_silence_keep_both_markers(self):
        view = review_sections({"sentences": [sentence(0), sentence(1)]}, 0, 20)
        self.assertEqual(view["before"], [])
        self.assertEqual(view["after"], [])
        self.assertTrue(all(row["kind"] == "full" for row in view["rows"]))
        silence = review_sections({"sentences": [dict(sentence(0), end=4), dict(sentence(1), start=16)]}, 5, 15)
        self.assertEqual(silence["rows"], [])
        self.assertIn(START_MARKER, text_of(silence))
        self.assertIn(END_MARKER, text_of(silence))


class LiveTranscriptUITests(unittest.TestCase):
    def test_unsaved_bounds_refresh_context_without_changing_approval(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            app = HighlightApp(root)
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
                path = Path(temp) / "report.json"
                candidate = {"candidate_id": "c001", "start": 30, "end": 60, "title": "Тема", "reason": "Мысль",
                             "score": 80, "status": "approved"}
                candidate["approval_key"] = approval_key(candidate)
                report = {"settings": {"min_duration_sec": 1, "max_duration_sec": 100}, "candidates": [candidate]}
                save_json(path, report)
                before = path.read_bytes()
                app.context = {"report": report, "report_path": path,
                               "manifest": {"media": {"duration_sec": 100}},
                               "transcript": {"sentences": [sentence(i) for i in range(10)]}}
                app.refresh_table()
                root.update()
                self.assertNotIn("Реплика 6.", inside_text(app.details.get("1.0", "end")))
                app.end.set("70")
                self.assertIn("Реплика 6.", inside_text(app.details.get("1.0", "end")))
                self.assertIn("ПРЕДПРОСМОТР:", app.details.get("1.0", "end"))
                self.assertTrue(app.has_unsaved_edits())
                app.start.set("40")
                self.assertNotIn("Реплика 3.", inside_text(app.details.get("1.0", "end")))
                app.end.set("")
                self.assertIn("Границы редактируются", app.details.get("1.0", "end"))
                app.end.set("75")
                text = app.details.get("1.0", "end")
                self.assertIn("НЕПОЛНАЯ РЕПЛИКА", inside_text(text))
                self.assertNotIn("Реплика 7.", inside_text(text))
                self.assertIn("Реплика 7.", text.split(END_MARKER)[1])
                self.assertTrue(app.details.tag_ranges("context"))
                self.assertTrue(app.details.tag_ranges("marker"))
                self.assertTrue(app.details.tag_ranges("partial"))
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(load_json(path)["candidates"][0]["approval_key"], candidate["approval_key"])
                self.assertEqual(report["candidates"][0]["end"], 60)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
