"""Individual padding and single-row rendering regressions."""
import copy
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_padding import prepare_review_report, set_candidate_padding
from job_store import load_json, save_json
from renderer import approval_key
from review_ui import HighlightApp


class ClipControlsTests(unittest.TestCase):
    def clip(self, identifier="c001"):
        clip = {"candidate_id": identifier, "start": 10, "end": 20, "title": "Тема",
                "score": 80, "reason": "Мысль", "status": "approved"}
        clip["approval_key"] = approval_key(clip)
        return clip

    def test_toggle_limits_no_accumulation_and_manual_end(self):
        settings = {"min_duration_sec": 1, "max_duration_sec": 30}
        original = self.clip()
        padded = set_candidate_padding(original, True, 1, 20.5, settings)
        self.assertEqual(padded["end"], 20.5)
        self.assertEqual(padded["status"], "pending")
        self.assertNotIn("approval_key", padded)
        self.assertEqual(original["end"], 20)
        disabled = set_candidate_padding(padded, False, 1, 60, settings)
        self.assertEqual(disabled["end"], 20)
        enabled = set_candidate_padding(disabled, True, 1, 60, settings)
        self.assertEqual(enabled["end"], 21)
        self.assertEqual(set_candidate_padding(enabled, True, 2, 60, settings)["end"], 22)
        enabled["end"] = 25
        self.assertEqual(set_candidate_padding(enabled, True, 1, 60, settings)["end"], 26)
        unchanged = set_candidate_padding(original, False, 1, 60, settings)
        self.assertEqual(unchanged["approval_key"], approval_key(unchanged))

    def test_ui_toggle_persists_and_render_only_one_clip(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            app = HighlightApp(root)
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "report.json"
                clips = [self.clip(), self.clip("c002")]
                app.context = {"report_path": path, "job_dir": Path(temp),
                               "manifest": {"media": {"duration_sec": 60}},
                               "transcript": {"sentences": []},
                               "report": {"signature": "test", "settings": {
                                   "min_duration_sec": 1, "max_duration_sec": 30}, "candidates": clips}}
                app.refresh_table()
                app.show_candidate()
                app.variables["end_padding_sec"].set("1")
                app.toggle_candidate_padding()  # Disable the default checked state.
                app.toggle_candidate_padding()
                self.assertEqual(app.current_candidate()["end"], 21)
                self.assertEqual(app.current_candidate()["status"], "pending")
                self.assertEqual(load_json(path)["candidates"][0]["end"], 21)
                other = app.context["report"]["candidates"][1]
                self.assertEqual(other["end"], 20)
                self.assertEqual(other["approval_key"], clips[1]["approval_key"])
                with patch("review_ui.messagebox.showinfo") as info, patch.object(app, "start_worker") as worker:
                    app.render("c001")
                    info.assert_called_once()
                    worker.assert_not_called()
                app.decide("approved")
                app.show_candidate()
                before = copy.deepcopy(app.context["report"])
                with patch("review_ui.messagebox.askyesno", return_value=True), \
                        patch("review_ui.render_approved", return_value=[]) as render, \
                        patch.object(app, "start_worker", side_effect=lambda op, function: function()):
                    app.render("c001")
                    self.assertEqual([c["candidate_id"] for c in render.call_args.args[1]["candidates"]], ["c001"])
                self.assertEqual(app.context["report"], before)
        finally:
            root.destroy()

    def test_shared_padding_preserves_disabled_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "analysis" / "raw" / "report.json"
            settings = {"min_duration_sec": 1, "max_duration_sec": 30}
            disabled = set_candidate_padding(self.clip(), False, 1, 60, settings)
            save_json(path, {"settings": settings, "candidates": [disabled, self.clip("c002")]})
            save_json(path.parent / "transcript.json", {"duration_sec": 60})
            report = load_json(prepare_review_report(path, 2, lambda message: None))
            self.assertEqual(report["candidates"][0]["end"], 20)
            self.assertFalse(report["candidates"][0]["end_padding_enabled"])
            self.assertEqual(report["candidates"][0]["status"], "approved")
            self.assertEqual(report["candidates"][1]["end"], 22)
