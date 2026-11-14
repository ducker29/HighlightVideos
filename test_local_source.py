import os
import shutil
import subprocess
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

from job_store import load_json, save_json
from local_source import content_fingerprint, local_source_path, prepare_local_source, verify_local_source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.source = self.root / "Видео с пробелом.mp4"
        self.source.write_bytes(b"source one")
        self.data_patch = patch("local_source.DATA", self.data)
        self.data_patch.start()
        self.tools = patch("local_source.shutil.which", return_value="available")
        self.tools.start()
        self.probe = patch("local_source.probe", return_value={"width": 1920, "height": 1080, "duration_sec": 10})
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.tools.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_path_and_media_validation(self):
        self.assertEqual(local_source_path(f'  "{self.source}"  '), self.source.resolve())
        for invalid in ("", str(self.root), str(self.root / "missing.mp4")):
            with self.assertRaises(ValueError):
                local_source_path(invalid)
        with patch("local_source.probe", side_effect=ValueError("Ожидается 16:9")), self.assertRaisesRegex(ValueError, "16:9"):
            prepare_local_source(str(self.source), lambda _: None)
        self.assertFalse(self.data.exists())

    def test_identity_cache_reuse_and_changed_content_with_preserved_mtime(self):
        from transcriber import transcribe
        job, manifest = prepare_local_source(str(self.source), lambda _: None)
        self.assertEqual(Path(manifest["source_path"]), self.source.resolve())
        self.assertEqual(manifest["source_type"], "local")
        self.assertFalse((job / "source.mkv").exists())
        cached = {"settings": {"asr_model": "test", "language": "ru", "device": "cpu", "schema": 2,
                               "start_sec": 0., "end_sec": 10}, "video_id": manifest["video_id"]}
        save_json(job / "transcript.json", cached)
        repeated_job, repeated_manifest = prepare_local_source(str(self.source), lambda _: None)
        self.assertEqual(repeated_job, job)
        with patch("transcriber.check_runtime") as runtime:
            self.assertEqual(transcribe(job, repeated_manifest, {"asr_model": "test", "language": "ru", "device": "cpu"},
                                        lambda _: None), cached)
            runtime.assert_not_called()
        stamp = self.source.stat()
        self.source.write_bytes(b"source two")  # same length, same name, same mtime
        os.utime(self.source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        changed_job, changed_manifest = prepare_local_source(str(self.source), lambda _: None)
        self.assertNotEqual(changed_job, job)
        self.assertNotEqual(changed_manifest["video_id"], manifest["video_id"])
        self.assertEqual(load_json(job / "transcript.json"), cached)
        self.assertFalse((changed_job / "transcript.json").exists())
        with self.assertRaisesRegex(ValueError, "изменилось"):
            verify_local_source(manifest, lambda _: None)

    def test_same_filename_in_different_folders_is_separate(self):
        other = self.root / "other"
        other.mkdir()
        second = other / self.source.name
        second.write_bytes(self.source.read_bytes())
        job, _ = prepare_local_source(str(self.source), lambda _: None)
        other_job, _ = prepare_local_source(str(second), lambda _: None)
        self.assertNotEqual(job, other_job)

    def test_file_changing_during_hash_is_rejected(self):
        with patch("local_source._stamp", side_effect=[(10, 1, 1, 1), (10, 2, 2, 1)]):
            with self.assertRaisesRegex(ValueError, "изменилось"):
                content_fingerprint(self.source)

    def test_changed_source_blocked_before_either_renderer(self):
        from renderer import render_approved, render_shorts_approved
        job, manifest = prepare_local_source(str(self.source), lambda _: None)
        self.source.write_bytes(b"replacement")
        clip = {"candidate_id": "s001", "status": "approved"}
        report = {"candidates": [clip], "shorts_candidates": [clip]}
        with patch("renderer.subprocess.run") as run:
            for function in (render_approved, render_shorts_approved):
                with self.assertRaisesRegex(ValueError, "изменилось"):
                    function(manifest, report, job, lambda _: None)
            run.assert_not_called()


class WindowTests(unittest.TestCase):
    def setUp(self):
        from review_ui import HighlightApp
        self.directory = tempfile.TemporaryDirectory()
        self.data = Path(self.directory.name)
        self.patch = patch("review_ui.DATA", self.data / "data")
        self.patch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = HighlightApp(self.root)
        self.source = self.data / "Локальное видео.mp4"
        self.source.write_bytes(b"placeholder")

    def tearDown(self):
        self.root.destroy()
        self.patch.stop()
        self.directory.cleanup()

    def test_field_below_url_and_collapse_preserves_values(self):
        app = self.app
        app.variables["local_video_path"].set(str(self.source))
        original = app.read_settings()
        self.assertEqual(int(app.local_video_entry.grid_info()["row"]), int(app.url_entry.grid_info()["row"]) + 1)
        self.assertTrue(app.source_settings_expanded)
        for _ in range(2):
            app.source_settings_toggle.invoke()
            self.assertFalse(app.source_settings_expanded)
            self.assertEqual(app.source_settings_body.winfo_manager(), "")
            self.assertEqual(app.read_settings(), original)
            app.source_settings_toggle.invoke()
            self.assertTrue(app.source_settings_expanded)
            self.assertEqual(app.source_settings_body.winfo_manager(), "pack")
            self.assertEqual(app.read_settings(), original)

    def test_picker_settings_persistence_and_optional_url(self):
        app = self.app
        app.variables["youtube_url"].set("")
        with patch("review_ui.filedialog.askopenfilename", return_value=str(self.source)):
            app.browse_local_video()
        self.assertEqual(app.read_settings()["local_video_path"], str(self.source.resolve()))
        with patch("review_ui.filedialog.askopenfilename", return_value=""):
            app.browse_local_video()
        self.assertEqual(app.read_settings()["local_video_path"], str(self.source.resolve()))
        with patch.object(app, "start_worker"):
            app.analyze()
        saved = load_json(self.data / "data" / "ui_settings.json")
        self.assertEqual(saved["local_video_path"], str(self.source.resolve()))
        from review_ui import HighlightApp
        second_root = tk.Toplevel(self.root)
        try:
            second = HighlightApp(second_root)
            self.assertEqual(second.variables["local_video_path"].get(), str(self.source.resolve()))
            self.assertTrue(second.source_settings_expanded)
        finally:
            second_root.destroy()
        app.variables["youtube_url"].set("invalid URL ignored for local source")
        self.assertEqual(app.read_settings()["local_video_path"], str(self.source.resolve()))
        app.variables["local_video_path"].set(str(self.data / "missing.mp4"))
        with self.assertRaisesRegex(ValueError, "недоступен"):
            app.read_settings()  # do not silently fall back to URL
        app.variables["local_video_path"].set("")
        app.variables["youtube_url"].set("")
        with self.assertRaisesRegex(ValueError, "YouTube"):
            app.read_settings()


class BackendTests(unittest.TestCase):
    def test_invalid_local_path_fails_before_services(self):
        from app_backend import run_analysis
        with patch("app_backend.list_models") as models, patch("app_backend.download") as download:
            with self.assertRaisesRegex(ValueError, "недоступен"):
                run_analysis({"min_duration_sec": 1, "max_duration_sec": 2,
                              "local_video_path": "Z:/__missing_local_source__/missing.mp4"}, lambda _: None)
            models.assert_not_called()
            download.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_local_source_backend_report_and_both_exports(self):
        from app_backend import run_analysis
        from renderer import approval_key, shorts_approval_key, render_approved, render_shorts_approved
        from shorts_settings import ShortsSettings
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Видео с пробелами.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=2",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2", "-c:v", "libx264",
                            "-pix_fmt", "yuv420p", str(source)], check=True, timeout=20)
            settings = {"local_video_path": str(source), "youtube_url": "not a URL", "min_duration_sec": 1,
                        "max_duration_sec": 2, "device": "cpu", "llm_model": "test", "ollama_url": "unused",
                        "outputs": {"highlights": {"min_duration_sec": 1, "max_duration_sec": 2},
                                    "shorts": {"enabled": True, "min_duration_sec": 1,
                                               "target_duration_sec": 1, "max_duration_sec": 2}}}

            def asr(job, manifest, _, log, **bounds):
                self.assertEqual(manifest["source_path"], str(source.resolve()))
                self.assertEqual(bounds, {"start_sec": 0., "end_sec": None})
                return {"video_id": manifest["video_id"], "duration_sec": 2, "segments": [], "sentences": []}

            def analyze(job, transcript, settings, log):
                path = job / "analysis" / "test" / "report.json"
                clip = {"candidate_id": "c001", "start": 0, "end": 1, "title": "Local test", "status": "pending"}
                save_json(path, {"schema": 1, "video_id": transcript["video_id"], "signature": "test",
                                 "settings": settings, "candidates": [clip], "shorts_candidates": [
                                     {**clip, "candidate_id": "s001", "format": "short"}]})
                save_json(path.parent / "transcript.json", transcript)
                return path

            with patch("local_source.DATA", root / "data"), patch("app_backend.download") as download, \
                    patch("app_backend.check_runtime"), patch("app_backend.list_models", return_value=["test"]), \
                    patch("app_backend.transcribe", side_effect=asr), patch("app_backend.analyze", side_effect=analyze), \
                    patch("app_backend.review_report", side_effect=lambda path, *a, **kw: path), \
                    patch("app_backend.prepare_review_report", side_effect=lambda path, *a, **kw: path):
                context = run_analysis(settings, lambda _: None)
                download.assert_not_called()
            self.assertFalse(list((root / "data").rglob("*.mp4")))  # analysis never auto-renders or copies source
            report = context["report"]
            report["candidates"][0].update(status="approved", approval_key=approval_key(report["candidates"][0]))
            report["shorts_candidates"][0].update(status="approved", approval_key=shorts_approval_key(report["shorts_candidates"][0]))
            high = render_approved(context["manifest"], report, context["job_dir"], lambda _: None, transcript=context["transcript"])
            short = render_shorts_approved(context["manifest"], report, context["job_dir"], lambda _: None,
                                           transcript=context["transcript"], export_settings=ShortsSettings("720p", "compact"))
            self.assertTrue(Path(high[0]).exists())
            self.assertTrue(Path(short[0]).exists())
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
