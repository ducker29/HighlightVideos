import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from job_store import load_json, save_json
from quality_state import QualityState as S
from quality_window import QualityDialog


ID = "l8qUNUTufU8"
URL = "https://www.youtube.com/watch?v=" + ID


class QualityWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.done = Mock()
        self.dialog = QualityDialog(self.root, URL, self.done, root=self.directory.name)
        self.addCleanup(self.cleanup_dialog)
        self.media = {"height": 720, "width": 1280,
                      "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        self.qualities = {720: {"format_id": "22", "fps": 30, "adaptive": False, "has_audio": True}}

    def cleanup_dialog(self):
        if not self.dialog.closed:
            self.dialog.busy = False
            self.dialog.close()

    def wait(self):
        deadline = time.monotonic() + 3
        while self.dialog.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertFalse(self.dialog.busy, "Worker did not finish")
        self.root.update()

    def check(self):
        with patch("quality_window.list_available_qualities", return_value=self.qualities):
            self.dialog.check_button.invoke()
            self.wait()

    def test_create_check_populate_and_close_once(self):
        self.assertIsNone(self.root.grab_current())
        self.check()
        self.assertEqual(self.dialog.table.get_children(), ("720",))
        button = self.dialog.controller.buttons[720]
        self.assertEqual(button.cget("text"), "не загружено")
        self.dialog.cancel_button.invoke()
        self.dialog.close()
        self.done.assert_called_once()
        self.assertIsNone(self.done.call_args.args[0])
        self.assertIsNone(self.dialog._after)

    def test_download_then_select_and_deselect_persist(self):
        self.check()
        d = self.dialog
        target = d.job_dir / "sources" / "720p.mkv"
        def download(*args):
            target.parent.mkdir(parents=True)
            target.write_bytes(b"fixture")
            return str(target), self.media
        with patch("quality_window.download_quality", side_effect=download), \
                patch("youtube_download.probe", return_value=self.media):
            d.controller.buttons[720].invoke()
            self.assertEqual(d.controller.states[720], S.DOWNLOADING)
            self.assertEqual(str(d.controller.buttons[720].cget("state")), "disabled")
            self.wait()
            self.assertEqual(d.controller.buttons[720].cget("text"), "загружено")
            self.assertIsNone(load_json(d.manifest_path)["active_height"])
            d.controller.buttons[720].invoke()
            self.wait()
            self.assertEqual(load_json(d.manifest_path)["active_height"], 720)
            self.assertEqual(d.controller.buttons[720].cget("text"), "выбрано")
            d.controller.buttons[720].invoke()
            self.wait()
            self.assertIsNone(load_json(d.manifest_path)["active_height"])
            d.controller.buttons[720].invoke()
            self.wait()
        d.ok_button.invoke()
        self.assertEqual(self.done.call_args.args[0], 720)
        self.assertEqual(self.done.call_args.args[1]["active_height"], 720)

    def test_download_error_retry_and_close_during_work(self):
        self.check()
        d = self.dialog
        with patch("quality_window.download_quality", side_effect=RuntimeError("network failed")):
            d.click(720)
            d.close()
            self.assertFalse(d.closed)
            self.wait()
        self.assertEqual(d.controller.states[720], S.NEUTRAL)
        self.assertIn("network failed", d.status.get())
        self.assertEqual(str(d.check_button.cget("state")), "normal")
        self.assertFalse(d.manifest_path.exists())

    def test_check_error_keeps_existing_rows(self):
        self.check()
        with patch("quality_window.list_available_qualities", side_effect=RuntimeError("offline")):
            self.dialog.check()
            self.wait()
        self.assertEqual(self.dialog.table.get_children(), ("720",))
        self.assertIn("offline", self.dialog.status.get())

    def reopen(self):
        self.dialog.close()
        self.dialog = QualityDialog(self.root, URL, self.done, root=self.directory.name)
        self.root.update()
        return self.dialog

    def test_checked_table_restores_offline_after_restart(self):
        self.check()
        self.assertTrue(self.dialog.table_cache_path.is_file())
        with patch("quality_window.list_available_qualities") as fetch, \
                patch("quality_window.download_quality") as download:
            d = self.reopen()
            self.assertEqual(d.table.get_children(), ("720",))
            self.assertEqual(d.qualities[720]["fps"], 30)
            self.assertEqual(d.controller.states[720], S.NEUTRAL)
            self.assertIn("восстановлена", d.status.get())
            fetch.assert_not_called()
            download.assert_not_called()

    def test_persisted_selection_and_downloaded_state_follow_files(self):
        self.check()
        d = self.dialog
        for height in (720, 1080):
            target = d.job_dir / "sources" / f"{height}p.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"fixture")
        manifest = {"video_id": ID, "active_height": 720,
                    "source_path": str(d.job_dir / "sources" / "720p.mkv"),
                    "qualities": {"720": self.qualities[720], "1080": {"format_id": "137", "fps": 60}}}
        save_json(d.manifest_path, manifest)
        d = self.reopen()
        self.assertEqual(d.controller.states[720], S.SELECTED)
        self.assertEqual(d.controller.states[1080], S.DOWNLOADED)
        # A removed file must not retain its saved selected state.
        (d.job_dir / "sources" / "720p.mkv").unlink()
        d = self.reopen()
        self.assertEqual(d.controller.states[720], S.NEUTRAL)
        self.assertIsNone(d.controller.selected())
        self.assertFalse(d.controller.blocked)

    def test_corrupt_table_cache_can_be_refreshed(self):
        self.dialog.table_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.dialog.table_cache_path.write_text("{", encoding="utf-8")
        d = self.reopen()
        self.assertIn("не восстановлена", d.status.get())
        self.check()
        self.assertEqual(load_json(d.table_cache_path)["video_id"], ID)
        self.assertEqual(d.table.get_children(), ("720",))

    def test_selection_write_failure_restores_previous_state(self):
        d = self.dialog
        target = d.job_dir / "sources" / "720p.mkv"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fixture")
        manifest = {"video_id": ID, "source_path": str(target), "active_height": 720,
                    "qualities": {"720": self.qualities[720]}, "media": self.media}
        save_json(d.manifest_path, manifest)
        d.populate(self.qualities, manifest)
        with patch("quality_window.save_json", side_effect=OSError("disk full")):
            d.click(720)
            self.wait()
        self.assertEqual(d.controller.selected(), 720)
        self.assertEqual(d.controller.buttons[720].cget("text"), "выбрано")
        self.assertEqual(load_json(d.manifest_path)["active_height"], 720)

    def test_buttons_follow_visible_cells_on_scroll_and_resize(self):
        self.root.deiconify()
        d = self.dialog
        qualities = {h: {"format_id": str(h), "fps": 30} for h in range(100, 3000, 100)}
        d.populate(qualities, d.manifest)
        self.root.update()
        d._position_buttons()
        self.assertEqual(d.controller.buttons[2900].winfo_manager(), "place")
        self.assertEqual(d.controller.buttons[100].winfo_manager(), "")
        d.table.yview_moveto(1)
        self.root.update()
        self.assertEqual(d.controller.buttons[100].winfo_manager(), "place")
        self.assertEqual(d.controller.buttons[2900].winfo_manager(), "")
        d.top.geometry("800x500")
        self.root.update()
        bbox = d.table.bbox("100", "action")
        button = d.controller.buttons[100]
        x, y, width, height = bbox
        padding = round(d.top.winfo_fpixels("10p"))
        self.assertEqual(d.row_padding, padding)
        self.assertEqual((button.winfo_x(), button.winfo_y(), button.winfo_width(), button.winfo_height()),
                         (x, y + padding, width, height - 2 * padding))

    def test_progress_fill_unknown_size_and_error_cleanup(self):
        self.root.deiconify()
        self.check()
        d = self.dialog
        d.controller.set_state(720, S.DOWNLOADING)
        d.busy = True
        d._refresh()
        self.root.update()
        canvas = d.progress_views[720]
        self.assertEqual(d.controller.buttons[720].winfo_manager(), "")
        d.events.put(("progress", 720, 45))
        deadline = time.monotonic() + 2
        while d.progress_values.get(720) != 45 and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertEqual(canvas.itemcget("label", "text"), "Загружается 45%")
        self.assertAlmostEqual(canvas.coords("fill")[2], canvas.winfo_width() * .45)
        d.events.put(("progress", 720, None))
        deadline = time.monotonic() + 2
        while d.progress_values.get(720) is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertEqual(canvas.itemcget("label", "text"), "Загружается…")
        before = canvas.coords("fill")
        deadline = time.monotonic() + .15
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertNotEqual(canvas.coords("fill"), before)
        d.events.put(("error", "download", "network failed"))
        self.wait()
        self.assertNotIn(720, d.progress_views)
        self.assertNotIn(720, d.progress_values)
        self.assertEqual(d.controller.buttons[720].winfo_manager(), "place")
        self.assertFalse(canvas.winfo_exists())


if __name__ == "__main__":
    unittest.main()
