import copy
import json
import shutil
import subprocess
import tempfile
import tkinter as tk
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

from shorts_settings import (COMPRESSIONS, DEFAULTS, RESOLUTIONS, ShortsSettings,
                             document, from_document, load_settings, save_settings)


class SettingsTests(unittest.TestCase):
    def test_resolution_and_compression_presets(self):
        self.assertEqual(DEFAULTS.output_size(3840, 2160), (1080, 1920))
        self.assertEqual(DEFAULTS.crf, 18)
        self.assertEqual(ShortsSettings("720p", "balanced").output_size(3840, 2160), (720, 1280))
        self.assertEqual(ShortsSettings("720p", "balanced").crf, 23)
        self.assertEqual(ShortsSettings("original", "compact").crf, 28)
        original = ShortsSettings("original")
        self.assertEqual(original.output_size(3840, 2160), (1216, 2160))
        self.assertEqual(original.output_size(1920, 1080), (608, 1080))
        self.assertEqual(original.output_size(1080, 1920), (1080, 1920))
        for source_w, source_h in ((1001, 721), (720, 1920), (2, 2)):
            width, height = original.output_size(source_w, source_h)
            self.assertLessEqual(width, source_w)
            self.assertLessEqual(height, source_h)
            self.assertEqual((width % 2, height % 2), (0, 0))
        with self.assertRaises(FrozenInstanceError):
            DEFAULTS.resolution = "720p"

    def test_invalid_settings_and_roundtrip(self):
        for args in (("4k", "high"), ("720p", "lossless"), (None, "high"), ([], "high")):
            with self.assertRaises(ValueError):
                ShortsSettings(*args)
        for invalid in ({}, [], {"schema_version": True, "shorts_export": {}},
                        {"schema_version": 2, "shorts_export": {}},
                        {"schema_version": 1, "shorts_export": {"resolution": "720p"}}):
            with self.assertRaises(ValueError):
                from_document(invalid)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shorts_render_settings.json"
            settings = ShortsSettings("720p", "compact")
            save_settings(path, settings)
            self.assertEqual(load_settings(path), settings)
            self.assertEqual(json.loads(path.read_text()), document(settings))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(path)


class WindowTests(unittest.TestCase):
    def setUp(self):
        import review_ui
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name)
        self.data_patch = patch.object(review_ui, "DATA", self.data)
        self.data_patch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = review_ui.HighlightApp(self.root)

    def tearDown(self):
        self.root.destroy()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_singleton_draft_apply_reopen_and_restart(self):
        import review_ui
        self.app.open_shorts_settings()
        window = self.app.shorts_settings_window
        self.app.open_shorts_settings()
        self.assertIs(self.app.shorts_settings_window, window)
        self.assertIsNone(window.top.grab_current())  # non-modal
        window.resolution.set(RESOLUTIONS["720p"])
        window.compression.set(COMPRESSIONS["compact"][0])
        self.assertEqual(self.app.shorts_export_settings, DEFAULTS)
        self.assertFalse((self.data / "shorts_render_settings.json").exists())
        window.apply()
        expected = ShortsSettings("720p", "compact")
        self.assertEqual(self.app.shorts_export_settings, expected)
        self.assertEqual(load_settings(self.data / "shorts_render_settings.json"), expected)
        window.populate(DEFAULTS)
        self.assertEqual(self.app.shorts_export_settings, expected)  # draft only
        window.close()
        self.app.open_shorts_settings()
        self.assertEqual(self.app.shorts_settings_window.current(), expected)
        second_root = tk.Toplevel(self.root)
        try:
            second = review_ui.HighlightApp(second_root)
            self.assertEqual(second.shorts_export_settings, expected)
        finally:
            second_root.destroy()

    def test_failed_save_keeps_active_settings(self):
        self.app.open_shorts_settings()
        window = self.app.shorts_settings_window
        window.populate(ShortsSettings("720p", "compact"))
        with patch("review_ui.save_shorts_settings", side_effect=OSError("disk full")), \
                patch("shorts_settings_window.messagebox.showerror") as error:
            window.apply()
            error.assert_called_once()
        self.assertEqual(self.app.shorts_export_settings, DEFAULTS)

    def test_corrupt_config_kept_with_warning(self):
        import review_ui
        path = self.data / "shorts_render_settings.json"
        path.write_text("broken", encoding="utf-8")
        second_root = tk.Toplevel(self.root)
        try:
            with patch("review_ui.messagebox.showwarning") as warning:
                second = review_ui.HighlightApp(second_root)
                warning.assert_called_once()
            self.assertEqual(second.shorts_export_settings, DEFAULTS)
            self.assertEqual(path.read_text(), "broken")
        finally:
            second_root.destroy()

    def test_render_single_and_batch_take_immutable_active_snapshot(self):
        self.app.context = {"manifest": {"source_path": "source.mp4"}, "job_dir": str(self.data),
                            "report": {"shorts_candidates": [{"candidate_id": "s001", "status": "approved"}]},
                            "transcript": {"segments": []}}
        initial = ShortsSettings("720p", "balanced")
        for candidate_id in ("s001", None):
            self.app.shorts_export_settings = initial
            queued = []
            with patch.object(self.app, "has_unsaved_edits", return_value=False), \
                    patch.object(self.app, "start_worker", side_effect=lambda label, task: queued.append(task)):
                self.app.render_shorts(candidate_id)
            self.app.shorts_export_settings = ShortsSettings("original", "compact")
            with patch("review_ui.render_shorts_approved") as renderer:
                queued[0]()
                self.assertEqual(renderer.call_args.kwargs["export_settings"], initial)
                self.assertEqual(renderer.call_args.kwargs["candidate_id"], candidate_id)


class RendererTests(unittest.TestCase):
    def test_both_crop_branches_use_export_settings(self):
        import renderer
        from smart_crop_storage import create_settings, save_settings as save_crop, settings_path
        source = {"width": 1920, "height": 1080, "duration_sec": 2, "has_audio": True}
        for segment_count in (0, 1, 2):
            with self.subTest(segment_count=segment_count), tempfile.TemporaryDirectory() as directory:
                clip = {"candidate_id": "s001", "start": 0, "end": 1, "title": "test", "status": "approved",
                        "render_settings": {"subtitles_enabled": False}}
                clip["approval_key"] = renderer.shorts_approval_key(clip)
                report = {"signature": "test", "shorts_candidates": [clip],
                          "profiles": {"shorts": {"min_duration_sec": .1, "max_duration_sec": 2}}}
                if segment_count:
                    crop = create_settings("s001", source_start=0, source_end=1,
                                           source_width=1920, source_height=1080)
                    if segment_count == 2:
                        first = crop["segments"][0]
                        crop["segments"] = [{**first, "end": .5}, {**first, "start": .5}]
                    save_crop(crop, settings_path(directory, "test", "s001"))
                expected = ShortsSettings("720p", "compact")
                with patch("renderer.probe", return_value=source), \
                        patch("renderer._probe_basic", return_value={**source, "width": 720, "height": 1280, "duration_sec": 1}), \
                        patch("renderer.subprocess.run", return_value=Mock(returncode=0)) as run, \
                        patch("pathlib.Path.rename"):
                    renderer.render_shorts_approved({"source_path": "source.mp4"}, report, directory, lambda _: None,
                                                    transcript={"segments": []}, export_settings=expected)
                command = run.call_args.args[0]
                self.assertIn("scale=720:1280", command[command.index("-vf") + 1])
                self.assertEqual(command[command.index("-crf") + 1], "28")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_all_resolution_and_compression_combinations(self):
        import renderer
        from subtitle_settings import DEFAULTS as SUBS_DEFAULTS
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=0.6",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.6", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", str(source)], check=True, timeout=20)
            clip = {"candidate_id": "s001", "start": 0, "end": .6, "title": "resolution test", "status": "approved"}
            clip["approval_key"] = renderer.shorts_approval_key(clip)
            report = {"signature": "test", "shorts_candidates": [clip],
                      "profiles": {"shorts": {"min_duration_sec": .1, "max_duration_sec": 2}}}
            original_report = copy.deepcopy(report)
            transcript = {"segments": [{"start": 0, "end": .6, "words": [{"start": 0, "end": .6, "word": "Субтитры"}]}]}
            style = replace(SUBS_DEFAULTS, position_mode="free", x=50, y=60, area_enabled=True,
                            width_percent=70, height_percent=25, outline_color="#00FF00", background_enabled=True)
            sizes = {}
            for resolution in RESOLUTIONS:
                for compression in COMPRESSIONS:
                    settings = ShortsSettings(resolution, compression)
                    with self.subTest(resolution=resolution, compression=compression):
                        outputs = renderer.render_shorts_approved({"source_path": str(source)}, report, directory,
                                                                 lambda _: None, transcript=transcript,
                                                                 subtitle_settings=style, export_settings=settings)
                        output = Path(outputs[0])
                        media = renderer._probe_basic(output)
                        self.assertEqual((media["width"], media["height"]), settings.output_size(640, 360))
                        self.assertTrue(media["has_audio"])
                        metadata = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
                        self.assertEqual(metadata["export_settings"]["shorts_export"], document(settings)["shorts_export"])
                        self.assertEqual(metadata["export_settings"]["crf"], settings.crf)
                        sizes[(resolution, compression)] = output.stat().st_size
            self.assertLess(sizes[("720p", "compact")], sizes[("720p", "high")])
            self.assertEqual(report, original_report)
            self.assertEqual(len(list(Path(directory).rglob("*.mp4"))), 10)  # source + nine new exports


if __name__ == "__main__":
    unittest.main()
