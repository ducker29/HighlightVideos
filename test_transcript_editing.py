import copy
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from job_store import load_json
from renderer import approval_key, shorts_approval_key, build_subtitles_srt, clip_text, render_shorts_approved
from review_ui import HighlightApp
from transcript_editing import apply_transcript_edits


def source():
    return {"sentences": [{"start": 10, "end": 13, "text": "Привет мир снова"}],
            "segments": [{"start": 10, "end": 13, "words": [
                {"word": "Привет", "start": 10, "end": 11},
                {"word": "мир", "start": 11, "end": 12},
                {"word": "снова", "start": 12, "end": 13}]}]}


def edits(text):
    return [{"sentence_index": 0, "start": 10, "end": 13, "text": text}]


class TranscriptEditingTests(unittest.TestCase):
    def test_replacement_keeps_other_words_and_cache(self):
        original = source()
        before = copy.deepcopy(original)
        corrected = apply_transcript_edits(original, edits("Привет друг снова"))
        self.assertEqual(original, before)
        words = corrected["segments"][0]["words"]
        self.assertEqual(words[0], original["segments"][0]["words"][0])
        self.assertEqual(words[2], original["segments"][0]["words"][2])
        self.assertEqual(words[1], {"word": "друг", "start": 11, "end": 12})

    def test_insert_delete_replace_and_missing_words(self):
        for value in ("Привет дорогой мир снова", "Привет мир снова сегодня",
                      "Вот Привет мир снова", "Привет снова", "Совсем другой текст реплики"):
            with self.subTest(value=value):
                result = apply_transcript_edits(source(), edits(value))
                words = result["segments"][0]["words"]
                self.assertEqual(" ".join(w["word"] for w in words), value)
                self.assertTrue(all(10 <= w["start"] < w["end"] <= 13 for w in words))
                self.assertTrue(all(a["end"] <= b["start"] for a, b in zip(words, words[1:])))
        result = apply_transcript_edits({"sentences": source()["sentences"]}, edits("Новые слова"))
        self.assertIn("Новые слова", build_subtitles_srt(result, 10, 13))

    def test_export_and_approval_use_saved_edits(self):
        candidate = {"candidate_id": "c1", "start": 10, "end": 13}
        for key in (approval_key, shorts_approval_key):
            self.assertNotEqual(key(candidate), key(dict(candidate, transcript_edits=edits("Привет друг снова"))))
        candidate["transcript_edits"] = edits("Привет друг снова")
        self.assertIn("Привет друг снова", clip_text(candidate, source()))
        corrected = apply_transcript_edits(source(), candidate["transcript_edits"])
        self.assertIn("Привет друг снова", build_subtitles_srt(corrected, 10, 13))
        with self.assertRaises(ValueError):
            apply_transcript_edits(source(), [{"sentence_index": 0, "start": 9, "end": 13, "text": "Текст"}])

    def test_final_shorts_render_receives_corrected_subtitles(self):
        candidate = {"candidate_id": "s1", "start": 10, "end": 13, "title": "Клип",
                     "status": "approved", "format": "short", "transcript_edits": edits("Привет друг снова")}
        candidate["approval_key"] = shorts_approval_key(candidate)
        report = {"signature": "test", "profiles": {"shorts": {"min_duration_sec": 1, "max_duration_sec": 30}},
                  "shorts_candidates": [candidate]}
        with tempfile.TemporaryDirectory() as directory, \
                patch("renderer.probe", return_value={"duration_sec": 30, "width": 1920, "height": 1080, "has_audio": True}), \
                patch("renderer._probe_basic", return_value={"duration_sec": 3, "width": 1080, "height": 1920, "has_audio": True}), \
                patch("renderer.subprocess.run", return_value=MagicMock(returncode=0)), \
                patch("pathlib.Path.rename"):
            render_shorts_approved({"source_path": "test.mp4"}, report, directory, lambda _: None, transcript=source())
            srt = next(Path(directory).rglob("*.srt")).read_text(encoding="utf-8-sig")
            self.assertIn("Привет друг снова", srt)
            self.assertNotIn("мир", srt)

    def test_dialog_draft_save_reload_and_approve(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        app = HighlightApp(root)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "report.json"
                candidate = {"candidate_id": "c1", "start": 10, "end": 13, "title": "Клип",
                             "reason": "Тема", "score": 80, "status": "pending"}
                app.context = {"report_path": path, "manifest": {"media": {"duration_sec": 30}},
                               "transcript": source(), "report": {"signature": "test", "candidates": [candidate],
                               "settings": {"min_duration_sec": 1, "max_duration_sec": 30}}}
                app.refresh_table()
                app.show_candidate()
                position = app.details.search("[00:00:10.000", "1.0", exact=True)
                self.assertTrue(position)
                with patch.object(app.details, "index", return_value=position):
                    app.edit_transcript_sentence(SimpleNamespace(x=0, y=0))
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                text = next(w for w in dialog.winfo_children() if isinstance(w, tk.Text))
                text.delete("1.0", "end")
                text.insert("1.0", "Привет друг снова")
                actions = dialog.winfo_children()[-1]
                actions.winfo_children()[0].invoke()
                self.assertTrue(app.has_unsaved_edits())
                self.assertFalse(path.exists())
                self.assertIn("Привет друг снова", app.details.get("1.0", "end"))
                app.decide("pending")
                app.show_candidate()
                self.assertFalse(app.has_unsaved_edits())
                saved = load_json(path)["candidates"][0]
                self.assertEqual(saved["transcript_edits"], edits("Привет друг снова"))
                self.assertEqual(app.context["transcript"], source())
                app.decide("approved")
                saved = load_json(path)["candidates"][0]
                self.assertEqual(saved["approval_key"], approval_key(saved))
                self.assertIn("Привет друг снова", clip_text(saved, source()))
        finally:
            app._close()


if __name__ == "__main__":
    unittest.main()
