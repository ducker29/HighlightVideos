"""Boundary and integration checks; no YouTube download or model weights required."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from analyzer import analyze, windows
from app_backend import open_report
from downloader import probe
from job_store import load_json, save_json, validate_interval
from renderer import approval_key, render_approved


class CandidateTests(unittest.TestCase):
    def test_entire_transcript_covered_and_no_invented_boundaries(self):
        sentences = [{"id": f"s{i:04d}", "start": i * 5.0, "end": (i + 1) * 5.0,
                      "text": f"Предложение {i}."} for i in range(140)]
        covered = {s["id"] for group in windows(sentences) for s in group}
        self.assertEqual(covered, {s["id"] for s in sentences})
        settings = {"llm_model": "test", "ollama_url": "http://unused", "min_duration_sec": 120,
                    "max_duration_sec": 300}
        invalid_sent = False
        duration_repair_sent = False
        checked_repair = False

        def fake_http(base, endpoint, payload=None, timeout=10):
            nonlocal invalid_sent, duration_repair_sent, checked_repair
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            if "topic" in payload["format"]["properties"]:
                response = {"topic": "Тестовая тема", "summary": "Завершённое рассуждение.",
                            "continues_previous": False, "continues_next": False, "interest_score": 80}
            else:
                prompt = payload["messages"][1]["content"]
                items = json.JSONDecoder().raw_decode(prompt.split("Полный текст с ID: ", 1)[1])[0]
                if not invalid_sent:
                    invalid_sent = True
                    response = {"clips": [{"start_id": "invented", "end_id": "invented", "title": "Bad", "reason": "Bad", "score": 100}]}
                elif not duration_repair_sent:
                    self.assertIn("Предыдущий JSON:", prompt)
                    self.assertIn("invented", prompt)
                    duration_repair_sent = True
                    response = {"clips": [{"start_id": items[0]["id"], "end_id": items[3]["id"],
                                           "title": "Short", "reason": "Too short", "score": 90}]}
                elif len(items) >= 24:
                    if not checked_repair:
                        self.assertIn("фактическая длина 20.00", prompt)
                        self.assertIn("Допустимые окончания", prompt)
                        checked_repair = True
                    response = {"clips": [{"start_id": items[0]["id"], "end_id": items[23]["id"],
                                           "title": "Тема", "reason": "Самодостаточная мысль", "score": 80}]}
                else:
                    response = {"clips": []}
            return {"message": {"content": json.dumps(response)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            transcript = {"video_id": "test", "duration_sec": 700, "sentences": sentences}
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http):
                path = analyze(job, transcript, settings, lambda message: None)
            report = load_json(path)
            self.assertTrue(invalid_sent)
            self.assertTrue(checked_repair)
            audit = load_json(path.parent / "block_0000_attempts.json")
            self.assertEqual([a["valid"] for a in audit], [False, False, True])
            self.assertGreater(len(report["candidates"]), 0)
            for clip in report["candidates"]:
                self.assertEqual(clip["status"], "pending")
                self.assertIn(clip["start"], [s["start"] for s in sentences])
                self.assertIn(clip["end"], [s["end"] for s in sentences])
                validate_interval(clip["start"], clip["end"], 700, 120, 300)
            report["candidates"][0]["status"] = "rejected"
            save_json(path, report)
            with patch("analyzer.ollama_request", side_effect=AssertionError("Cache contacted LLM")):
                self.assertEqual(analyze(job, transcript, settings, lambda message: None), path)
            # Reprocessing ASR must not change the transcript shown in an older review.
            save_json(job / "transcript.json", {"sentences": []})
            loaded = open_report(path)
            self.assertEqual(len(loaded["transcript"]["sentences"]), 140)
            self.assertEqual(loaded["report"]["candidates"][0]["status"], "rejected")

    def test_bad_intervals_rejected(self):
        for start, end in ((-1, 120), (120, 0), (0, 1000), (float("nan"), 120), (0, float("inf"))):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate_interval(start, end, 500, 120, 300)


class TranscriptionRangeTests(unittest.TestCase):
    def test_range_keeps_source_timecodes_and_separate_cache_signature(self):
        from transcriber import transcribe

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            for end in (40000, 50000):
                (job / f"audio_10000_{end}.wav").write_bytes(b"fixture")
            word = SimpleNamespace(word="Тест.", start=1.0, end=2.0)
            segment = SimpleNamespace(id=0, start=1.0, end=2.0, text="Тест.", words=[word])
            info = SimpleNamespace(language="ru")
            settings = {"asr_model": "large-v3", "language": "ru", "device": "cuda"}
            manifest = {"video_id": "test", "media": {"duration_sec": 60}, "source_path": "unused"}
            with patch("transcriber.check_runtime"), patch("faster_whisper.WhisperModel") as model:
                model.return_value.transcribe.side_effect = lambda *args, **kwargs: (iter([segment]), info)
                first = transcribe(job, manifest, settings, lambda message: None, start_sec=10, end_sec=40)
                self.assertEqual(first["segments"][0]["words"][0]["start"], 11.0)
                self.assertEqual(first["sentences"][0]["end"], 12.0)
                self.assertEqual(first["analysis_range"], {"start": 10.0, "end": 40.0})
                transcribe(job, manifest, settings, lambda message: None, start_sec=10, end_sec=40)
                self.assertEqual(model.call_count, 1)
                transcribe(job, manifest, settings, lambda message: None, start_sec=10, end_sec=50)
                self.assertEqual(model.call_count, 2)


class GPUHandoffTests(unittest.TestCase):
    def test_only_selected_resident_llm_is_unloaded(self):
        from app_backend import release_llm_for_asr

        settings = {"device": "cuda", "ollama_url": "http://unused", "llm_model": "selected"}
        with patch("app_backend.ollama_request", return_value={"models": [{"name": "other"}]}) as request:
            release_llm_for_asr(settings, lambda message: None)
            self.assertEqual(request.call_count, 1)
        with patch("app_backend.ollama_request", side_effect=[{"models": [{"name": "selected"}]}, {}]) as request:
            release_llm_for_asr(settings, lambda message: None)
            self.assertEqual(request.call_args.args[2]["keep_alive"], 0)
            self.assertEqual(request.call_args.args[2]["model"], "selected")
        with patch("app_backend.ollama_request") as request:
            release_llm_for_asr(dict(settings, device="cpu"), lambda message: None)
            request.assert_not_called()


class EditorialReviewTests(unittest.TestCase):
    def test_filter_revise_deduplicate_and_never_auto_approve(self):
        from editorial_review import review_report

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            path = job / "analysis" / "raw" / "report.json"
            sentences = [{"id": f"s{i:06d}", "start": i * 5.0, "end": (i + 1) * 5.0,
                          "text": "Законченное предложение."} for i in range(90)]
            save_json(path.parent / "transcript.json", {"duration_sec": 450, "sentences": sentences})
            candidates = [{"candidate_id": f"c{i + 1:03d}", "start_id": f"s{start:06d}", "end_id": f"s{start + 23:06d}",
                           "start": start * 5.0, "end": (start + 24) * 5.0, "score": 85,
                           "title": "Тест", "reason": "Исходный отбор", "status": "pending"}
                          for i, start in enumerate((0, 24, 36))]
            raw = {"signature": "raw", "settings": {"min_duration_sec": 120, "max_duration_sec": 300,
                                                    "llm_model": "test"}, "candidates": candidates}
            save_json(path, raw)
            decisions = iter([
                {"decision": "reject", "start_id": "", "end_id": "", "title": "Анонс", "reason": "Только приветствия и анонсы.",
                 "single_topic": False, "self_contained": False, "complete_ending": False, "score": 20},
                {"decision": "revise", "start_id": "s000025", "end_id": "s000049", "title": "История", "reason": "Уточнены смысловые границы.",
                 "single_topic": True, "self_contained": True, "complete_ending": True, "score": 90},
                {"decision": "revise", "start_id": "s000026", "end_id": "s000050", "title": "Дубль", "reason": "Та же история.",
                 "single_topic": True, "self_contained": True, "complete_ending": True, "score": 80},
            ])
            decision_index = 0

            def fake_ask(settings, prompt, schema, validate, **kwargs):
                nonlocal decision_index
                # Adjacent context must not be presented as part of the selected clip.
                inside = json.JSONDecoder().raw_decode(prompt.split("КЛИП (именно его проверь): ", 1)[1])[0]
                self.assertEqual(inside[0]["id"], candidates[decision_index]["start_id"])
                self.assertEqual(inside[-1]["id"], candidates[decision_index]["end_id"])
                self.assertIn("ДО КЛИПА (не входит в клип)", prompt)
                self.assertIn("ПОСЛЕ КЛИПА (не входит в клип)", prompt)
                decision_index += 1
                result = next(decisions)
                validate(result)
                return result

            with patch("editorial_review.ask", side_effect=fake_ask):
                reviewed_path = review_report(path, lambda message: None)
            report = load_json(reviewed_path)
            self.assertEqual(len(report["candidates"]), 1)
            self.assertEqual(len(report["editorial_rejections"]), 2)
            candidate = report["candidates"][0]
            self.assertEqual(candidate["candidate_id"], "c002")
            self.assertEqual(candidate["start"], 125)
            self.assertEqual(candidate["status"], "pending")
            self.assertNotIn("approval_key", candidate)
            self.assertEqual(load_json(path), raw)
            candidate["status"] = "approved"
            candidate["approval_key"] = approval_key(candidate)
            save_json(reviewed_path, report)
            with patch("editorial_review.ask", side_effect=AssertionError("Must preserve existing review")):
                self.assertEqual(review_report(path, lambda message: None), reviewed_path)
            self.assertEqual(load_json(reviewed_path)["candidates"][0]["status"], "approved")


class DesktopReviewTests(unittest.TestCase):
    def test_review_persists_and_edited_bounds_lose_approval(self):
        import tkinter as tk
        from review_ui import HighlightApp

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            app = HighlightApp(root)
            with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
                path = Path(temp) / "report.json"
                clip = {"candidate_id": "c001", "start": 1.0, "end": 4.0, "title": "Тема",
                        "reason": "Законченная мысль", "score": 80, "status": "pending"}
                report = {"settings": {"min_duration_sec": 1, "max_duration_sec": 4}, "candidates": [clip]}
                app.context = {"report": report, "report_path": path,
                               "manifest": {"media": {"duration_sec": 10}}, "transcript": {"sentences": []},
                               "quality_notes": {"c001": {"start": 1.0, "end": 4.0, "notes": ["Regression QA note"]}}}
                app.refresh_table()
                root.update()
                self.assertIn("Regression QA note", app.details.get("1.0", "end"))
                app.decide("approved")
                root.update()
                approved = load_json(path)["candidates"][0]
                self.assertEqual(approved["status"], "approved")
                self.assertEqual(approved["approval_key"], approval_key(approved))
                self.assertFalse(app.busy, "Approval must not start rendering")
                app.end.set("4.5")
                self.assertTrue(app.has_unsaved_edits())
                app.decide("pending")
                root.update()
                changed = load_json(path)["candidates"][0]
                self.assertEqual(changed["end"], 4.5)
                self.assertEqual(changed["status"], "pending")
                self.assertNotIn("approval_key", changed)
                self.assertNotIn("Regression QA note", app.details.get("1.0", "end"))
        finally:
            root.destroy()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RenderIntegrationTests(unittest.TestCase):
    def test_real_render_preserves_frame_and_requires_exact_approval(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            source = job / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "5",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source)], check=True)
            # Exercise the same PyAV call used by ASR; PyAV 19 broke this API.
            from faster_whisper.audio import decode_audio
            decoded = decode_audio(str(source), sampling_rate=16000)
            self.assertAlmostEqual(len(decoded) / 16000, 5.0, delta=0.05)
            manifest = {"source_path": str(source), "video_id": "synthetic", "media": probe(source)}
            clip = {"candidate_id": "c001", "start": 0.8, "end": 3.8, "title": "Test", "status": "pending"}
            report = {"signature": "test", "settings": {"min_duration_sec": 1, "max_duration_sec": 4}, "candidates": [clip]}
            with self.assertRaises(ValueError):
                render_approved(manifest, report, job, lambda message: None)
            clip["status"] = "approved"
            clip["approval_key"] = approval_key(clip)
            changed = copy.deepcopy(report)
            changed["candidates"][0]["end"] = 4.0
            with self.assertRaisesRegex(ValueError, "изменились"):
                render_approved(manifest, changed, job, lambda message: None)
            self.assertFalse((job / "output").exists())
            outputs = render_approved(manifest, report, job, lambda message: None)
            self.assertEqual(len(outputs), 1)
            media = probe(outputs[0])
            self.assertEqual((media["width"], media["height"]), (320, 180))
            self.assertEqual(media["frame_rate"], manifest["media"]["frame_rate"])
            self.assertAlmostEqual(media["duration_sec"], 3.0, delta=0.15)
            self.assertTrue(Path(outputs[0]).with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
