import copy
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from review_ui import HighlightApp


ID = "l8qUNUTufU8"
URL = "https://www.youtube.com/watch?v=" + ID


class QualityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.data = patch("review_ui.DATA", Path(self.directory.name))
        self.data.start()
        self.addCleanup(self.data.stop)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = HighlightApp(self.root)
        self.app.variables["youtube_url"].set(URL)

    def test_button_next_to_paste_and_autonomous_while_busy(self):
        app = self.app
        self.assertEqual(app.quality_button.cget("text"), "Загрузить")
        self.assertEqual(int(app.quality_button.grid_info()["column"]), 6)
        self.assertNotIn(app.quality_button, app.controls)
        app.busy = True
        with patch.object(app, "start_worker") as start:
            app.quality_button.invoke()
            first = app.quality_window
            self.assertIsNotNone(first)
            app.open_quality_window()
            self.assertIs(app.quality_window, first)
            start.assert_not_called()
        self.assertTrue(app.busy)
        first.close()
        self.assertIsNone(app.quality_window)
        app.open_quality_window()
        self.assertIsNot(app.quality_window, first)

    def test_empty_and_invalid_url(self):
        with patch("review_ui.messagebox.showinfo") as info:
            self.app.variables["youtube_url"].set("")
            self.app.open_quality_window()
            info.assert_called_once()
        with patch("review_ui.messagebox.showerror") as error:
            self.app.variables["youtube_url"].set("invalid")
            self.app.open_quality_window()
            error.assert_called_once()
        self.assertIsNone(self.app.quality_window)

    def test_manifest_update_preserves_report_and_other_video_context(self):
        app = self.app
        report = {"candidates": [{"status": "approved", "approval_key": "keep"}]}
        old = {"video_id": ID, "source_path": "old"}
        new = {"video_id": ID, "source_path": "new", "active_height": 1080,
               "media": {"height": 1080}}
        app.context = {"manifest": old, "report": report}
        app._apply_quality_manifest(1080, new)
        self.assertEqual(app.context["manifest"], new)
        self.assertIs(app.context["report"], report)
        new["media"]["height"] = 720
        self.assertEqual(app.context["manifest"]["media"]["height"], 1080)
        before = copy.deepcopy(app.context)
        app._apply_quality_manifest(720, {"video_id": "aaaaaaaaaaa", "source_path": "other"})
        self.assertEqual(app.context, before)
        app.context["manifest"]["source_type"] = "local"
        before = copy.deepcopy(app.context)
        app._apply_quality_manifest(720, new)
        self.assertEqual(app.context, before)

    def test_dialog_change_updates_context_before_close(self):
        app = self.app
        app.context = {"manifest": {"video_id": ID}, "report": {}}
        app.open_quality_window()
        dialog = app.quality_window
        manifest = {"video_id": ID, "active_height": 720, "source_path": "new"}
        dialog.busy = True
        dialog.events.put(("done", "select", manifest))
        # Invoke the scheduled poll once through Tk, avoiding an extra timer.
        import time
        deadline = time.monotonic() + 2
        while dialog.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertEqual(app.context["manifest"], manifest)
        self.assertFalse(dialog.closed)

    def test_app_close_waits_for_quality_worker(self):
        self.app.open_quality_window()
        self.app.quality_window.busy = True
        with patch("review_ui.messagebox.showinfo") as info:
            self.app._close()
            info.assert_called_once()
        self.assertTrue(self.root.winfo_exists())
        self.app.quality_window.busy = False


if __name__ == "__main__":
    unittest.main()
