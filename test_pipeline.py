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

from analyzer import analyze, ask, collect_clips, windows, CLIP_SCHEMA, ModelValidationError
from llm_context import MAX_PROMPT_BYTES, INITIAL_PROMPT_BYTES, byte_size, fit_sentence_groups, sentence_json
from app_backend import open_report
from downloader import probe
from job_store import analysis_bounds, load_json, save_json, validate_interval
from renderer import approval_key, output_filename, render_approved
from clip_padding import padding_seconds, prepare_review_report


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


class ModelRecoveryTests(unittest.TestCase):
    def test_mixed_replies_preserve_valid_clips_and_resume_saved_attempts(self):
        sentences = [{"id": f"s{i:06d}", "start": i * 5, "end": (i + 1) * 5, "text": "Речь."} for i in range(60)]
        good = {"start_id": "s000000", "end_id": "s000023", "title": "Тема", "reason": "Мысль", "score": 90}
        short = dict(good, start_id="s000024", end_id="s000025", score=85)
        unknown = dict(good, start_id="invented")
        response = {"message": {"content": json.dumps({"clips": [good, short, unknown]})}}
        settings = {"llm_model": "test", "ollama_url": "unused", "min_duration_sec": 120, "max_duration_sec": 300}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            audit = Path(temp) / "attempts.json"
            with patch("analyzer.ollama_request", return_value=response) as request:
                selected = collect_clips(settings, "Текст", sentences, 300, audit, lambda message: None, "Блок 1")
                self.assertEqual(request.call_count, 3)
            self.assertEqual(selected["clips"], [good])
            self.assertTrue(selected["validation_exhausted"])
            self.assertEqual(len(selected["rejected_candidates"]), 6)
            self.assertTrue(selected["selection_warnings"])
            before = audit.read_bytes()
            with patch("analyzer.ollama_request", side_effect=AssertionError("Completed attempts must not be repeated")):
                replay = collect_clips(settings, "Текст", sentences, 300, audit, lambda message: None, "Блок 1")
            self.assertEqual(replay["clips"], [good])
            self.assertEqual(audit.read_bytes(), before)

    def test_all_invalid_json_is_reported_but_network_failure_is_not_hidden(self):
        settings = {"llm_model": "test", "ollama_url": "unused", "min_duration_sec": 120, "max_duration_sec": 300}
        with patch("analyzer.ollama_request", return_value={"message": {"content": "not JSON"}}) as request:
            selected = collect_clips(settings, "Текст", [], 300, None, lambda message: None, "Блок 1")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(selected["clips"], [])
        self.assertTrue(selected["validation_exhausted"])
        with patch("analyzer.ollama_request", side_effect=RuntimeError("Server offline")):
            with self.assertRaisesRegex(RuntimeError, "Server offline"):
                collect_clips(settings, "Текст", [], 300, None, lambda message: None, "Блок 1")

    def test_bad_window_and_block_do_not_discard_later_results(self):
        sentences = [{"id": f"s{i:06d}", "start": i * 5, "end": (i + 1) * 5, "text": "Речь."} for i in range(100)]

        def transport(base, endpoint, payload=None, timeout=10):
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            prompt = payload["messages"][1]["content"]
            window = "topic" in payload["format"]["properties"]
            marker = "Предложения окна: " if window else "Полный текст с ID: "
            items = json.JSONDecoder().raw_decode(prompt.split(marker, 1)[1])[0]
            first = items[0]["id"] == "s000000"
            if window:
                value = {"topic": "Тема", "summary": "Резюме", "continues_previous": "bad" if first else False,
                         "continues_next": False, "interest_score": 80}
            else:
                value = {"clips": [{"start_id": items[0]["id"], "end_id": items[1 if first else 23]["id"],
                                    "title": "Тема", "reason": "Мысль", "score": 80}]} if len(items) >= 24 else {"clips": []}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                path = analyze(Path(temp), {"video_id": "test", "duration_sec": 500, "sentences": sentences},
                               {"llm_model": "test", "ollama_url": "unused", "min_duration_sec": 120,
                                "max_duration_sec": 300}, lambda message: None)
            report = load_json(path)
            self.assertTrue(report["candidates"])
            self.assertTrue(all(c["start"] >= 120 for c in report["candidates"]))
            self.assertFalse(report["analysis_complete"])
            self.assertTrue(report["analysis_warnings"])
            failed = load_json(path.parent / "block_0000.json")
            self.assertEqual(failed["clips"], [])
            self.assertTrue(failed["validation_exhausted"])


class ContextBudgetTests(unittest.TestCase):
    def test_repair_keeps_full_source_and_fits_budget_even_after_large_error(self):
        prompt = "я" * 13000  # 26000 UTF-8 bytes, close to the old hard limit.
        requests = []

        def transport(base, endpoint, payload=None, timeout=10):
            content = payload["messages"][1]["content"]
            self.assertTrue(content.startswith(prompt), "Never truncate the source to fit repair feedback")
            self.assertLessEqual(byte_size(content), MAX_PROMPT_BYTES)
            requests.append(content)
            value = ({"clips": [{"start_id": "s1", "end_id": "s2", "reason": "длинный ответ " * 3000}]}
                     if len(requests) == 1 else {"clips": []})
            return {"message": {"content": json.dumps(value, ensure_ascii=False)}}

        def validate(value):
            if value["clips"]:
                raise ValueError("Фактическая длина 101.92 с; нужно минимум 120. " + "Допустимые окончания: s3. " * 1000)

        with patch("analyzer.ollama_request", side_effect=transport):
            self.assertEqual(ask({"llm_model": "test", "ollama_url": "unused"}, prompt, CLIP_SCHEMA, validate), {"clips": []})
        self.assertEqual(len(requests), 2)
        self.assertIn("101.92", requests[1])
        self.assertIn("Предыдущий JSON", requests[1])

    def test_split_groups_preserve_every_sentence_and_overlap(self):
        sentences = [{"id": f"s{i}", "start": i * 5, "end": (i + 1) * 5, "text": f"{i}: " + "текст " * 50}
                     for i in range(80)]
        builder = lambda part: "Инструкции и контекст. " * 20 + sentence_json(part)
        groups = fit_sentence_groups(sentences, builder, overlap_seconds=30, budget=5000)
        self.assertGreater(len(groups), 1)
        self.assertEqual({s["id"] for group in groups for s in group}, {s["id"] for s in sentences})
        originals = {s["id"]: s for s in sentences}
        for group in groups:
            self.assertLessEqual(byte_size(builder(group)), 5000)
            self.assertTrue(all(s == originals[s["id"]] for s in group))
        self.assertTrue(all(a[-1]["end"] > b[0]["start"] for a, b in zip(groups, groups[1:])))

    def test_dense_transcript_splits_both_passes_without_losing_source(self):
        sentences = [{"id": f"s{i:06d}", "start": i * 3, "end": (i + 1) * 3,
                      "text": f"Предложение {i}: " + "слово " * 60} for i in range(100)]
        window_ids, clip_ids, sizes = set(), set(), []

        def transport(base, endpoint, payload=None, timeout=10):
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            prompt = payload["messages"][1]["content"]
            sizes.append(byte_size(prompt))
            self.assertLessEqual(sizes[-1], INITIAL_PROMPT_BYTES)
            if "topic" in payload["format"]["properties"]:
                items = json.loads(prompt.split("Предложения окна: ", 1)[1])
                window_ids.update(s["id"] for s in items)
                value = {"topic": "Тема", "summary": "Очень длинное резюме " * 2000,
                         "continues_previous": True, "continues_next": True, "interest_score": 80}
            else:
                items = json.loads(prompt.split("Полный текст с ID: ", 1)[1])
                clip_ids.update(s["id"] for s in items)
                value = {"clips": [{"start_id": items[0]["id"], "end_id": items[9]["id"],
                                    "title": "Тема", "reason": "Законченная мысль", "score": 80}]} if len(items) >= 10 else {"clips": []}
            return {"message": {"content": json.dumps(value, ensure_ascii=False)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                path = analyze(Path(temp), {"video_id": "test", "duration_sec": 300, "sentences": sentences},
                               {"llm_model": "test", "ollama_url": "unused", "min_duration_sec": 10,
                                "max_duration_sec": 30}, lambda message: None)
            self.assertTrue(load_json(path)["candidates"])
        self.assertEqual(window_ids, {s["id"] for s in sentences})
        self.assertEqual(clip_ids, window_ids)


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


class AnalysisScopeTests(unittest.TestCase):
    def test_minutes_validation_and_full_video_override(self):
        self.assertEqual(analysis_bounds({}), (0.0, None))
        self.assertEqual(analysis_bounds({"analyze_full_video": True, "analysis_start_min": "bad",
                                          "analysis_end_min": "nan"}, 60), (0.0, None))
        self.assertEqual(analysis_bounds({"analyze_full_video": False, "analysis_start_min": "1,5",
                                          "analysis_end_min": 3}, 180), (90, 180))
        for start, end in ((-1, 3), (3, 3), (4, 3), ("nan", 3), (0, "inf"), (0, "bad"), (0, 4)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                analysis_bounds({"analyze_full_video": False, "analysis_start_min": start, "analysis_end_min": end}, 180)

    def test_backend_passes_scope_to_asr_and_rejects_out_of_source_range(self):
        from app_backend import run_analysis

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            settings = {"min_duration_sec": 120, "max_duration_sec": 300, "llm_model": "test",
                        "device": "cpu", "ollama_url": "http://unused", "youtube_url": "http://unused",
                        "analyze_full_video": False, "analysis_start_min": 15, "analysis_end_min": 45}
            with patch("app_backend.list_models", return_value=["test"]), patch("app_backend.check_runtime"), \
                    patch("app_backend.download", return_value=(job, {"media": {"duration_sec": 3600}})), \
                    patch("app_backend.transcribe", return_value={}) as asr, \
                    patch("app_backend.analyze", return_value=job / "report.json"), \
                    patch("app_backend.review_report", return_value=job / "report.json"), \
                    patch("app_backend.prepare_review_report", return_value=job / "report.json") as tail, \
                    patch("app_backend.open_report", return_value={"ready": True}):
                self.assertEqual(run_analysis(settings, lambda message: None), {"ready": True})
                self.assertEqual(asr.call_args.kwargs, {"start_sec": 900.0, "end_sec": 2700.0})
                self.assertEqual(tail.call_args.args[1], 0.5)
                run_analysis(dict(settings, analyze_full_video=True), lambda message: None)
                self.assertEqual(asr.call_args.kwargs, {"start_sec": 0.0, "end_sec": None})
                asr.reset_mock()
                with self.assertRaisesRegex(ValueError, "превышает длительность"):
                    run_analysis(dict(settings, analysis_end_min=61), lambda message: None)
                asr.assert_not_called()


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
    def test_context_is_reduced_but_oversized_clip_requires_manual_review(self):
        from editorial_review import review_report

        sentences = [{"id": f"s{i:06d}", "start": i * 8, "end": (i + 1) * 8,
                      "text": ("Контекст " * 250 if i < 20 or i >= 35 else "Речь. " * 20)} for i in range(55)]
        clip = {"candidate_id": "c001", "start_id": "s000020", "end_id": "s000034", "start": 160, "end": 280,
                "title": "Тема", "reason": "Рассказ", "status": "pending", "score": 80}
        raw = {"signature": "raw", "settings": {"llm_model": "test", "min_duration_sec": 120, "max_duration_sec": 300},
               "candidates": [clip]}

        def check_prompt(settings, prompt, schema, validate, **kwargs):
            self.assertLessEqual(byte_size(prompt), INITIAL_PROMPT_BYTES)
            inside = json.JSONDecoder().raw_decode(prompt.split("КЛИП (именно его проверь): ", 1)[1])[0]
            self.assertEqual(inside, sentences[20:35])
            before = json.JSONDecoder().raw_decode(prompt.split("ДО КЛИПА (не входит в клип): ", 1)[1])[0]
            self.assertLess(len(before), 11)
            value = {"decision": "keep", "start_id": clip["start_id"], "end_id": clip["end_id"],
                     "title": "Тема", "reason": "Законченная мысль", "single_topic": True,
                     "self_contained": True, "complete_ending": True, "score": 80}
            validate(value)
            return value

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            path = Path(temp) / "analysis" / "raw" / "report.json"
            save_json(path, raw)
            save_json(path.parent / "transcript.json", {"duration_sec": 440, "sentences": sentences})
            with patch("editorial_review.ask", side_effect=check_prompt) as request:
                report_path = review_report(path, lambda message: None)
                self.assertEqual(request.call_count, 1)
            self.assertEqual(len(load_json(report_path)["candidates"]), 1)
            dense_path = path.parent.parent / "dense" / "report.json"
            dense = copy.deepcopy(sentences)
            for sentence in dense[20:35]:
                sentence["text"] = "Речь. " * 1000
            save_json(dense_path, dict(raw, signature="dense"))
            save_json(dense_path.parent / "transcript.json", {"duration_sec": 440, "sentences": dense})
            with patch("editorial_review.ask", side_effect=AssertionError("Never review a truncated clip")):
                report = load_json(review_report(dense_path, lambda message: None))
            self.assertTrue(report["editorial_warnings"])
            self.assertEqual(report["candidates"][0]["editorial_decision"], "needs_review")
            self.assertEqual(report["candidates"][0]["status"], "pending")
            self.assertEqual(report["candidates"][0]["end"], 280)
            invalid_path = path.parent.parent / "invalid_editor" / "report.json"
            save_json(invalid_path, dict(raw, signature="invalid_editor"))
            save_json(invalid_path.parent / "transcript.json", {"duration_sec": 440, "sentences": sentences})
            with patch("editorial_review.ask", side_effect=ModelValidationError("Three invalid replies", [])):
                report = load_json(review_report(invalid_path, lambda message: None))
            self.assertEqual(report["candidates"][0]["editorial_decision"], "needs_review")
            self.assertEqual(report["candidates"][0]["status"], "pending")
            self.assertTrue(report["editorial_warnings"])

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


class EndPaddingTests(unittest.TestCase):
    def test_visible_padding_limits_overlap_reapproval_and_idempotence(self):
        self.assertEqual(padding_seconds("0,5"), 0.5)
        for value in (-1, "nan", "inf", "bad", 6):
            with self.subTest(value=value), self.assertRaises(ValueError):
                padding_seconds(value)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            path = Path(temp) / "analysis" / "raw" / "report.json"
            clips = [{"candidate_id": f"c{i:03d}", "start": start, "end": end, "status": status}
                     for i, start, end, status in ((1, 10, 20, "approved"), (2, 20.25, 30, "pending"),
                                                   (3, 100, 119.8, "pending"), (4, 40, 70, "approved"))]
            for clip in clips:
                if clip["status"] == "approved":
                    clip["approval_key"] = approval_key(clip)
            original = {"signature": "raw", "settings": {"min_duration_sec": 1, "max_duration_sec": 30}, "candidates": clips}
            save_json(path, original)
            save_json(path.parent / "transcript.json", {"duration_sec": 120})
            padded_path = prepare_review_report(path, 0.5, lambda message: None)
            result = load_json(padded_path)
            first, second, last, capped = result["candidates"]
            self.assertEqual(first["end"], 20.5)
            self.assertEqual(first["status"], "pending")
            self.assertNotIn("approval_key", first)
            self.assertEqual(last["end"], 120)
            self.assertAlmostEqual(last["end_padding"]["applied_sec"], 0.2)
            self.assertEqual(capped["end"], 70)
            self.assertEqual(capped["status"], "approved")
            self.assertEqual(capped["approval_key"], approval_key(capped))
            self.assertTrue(capped["warnings"])
            self.assertEqual(result["padding_overlap_pairs"][0]["overlap_sec"], 0.25)
            self.assertEqual(load_json(path), original, "Original human decisions must be preserved")
            first["status"] = "approved"
            first["approval_key"] = approval_key(first)
            save_json(padded_path, result)
            self.assertEqual(prepare_review_report(padded_path, 0.5, lambda message: None), padded_path)
            self.assertEqual(load_json(padded_path)["candidates"][0]["status"], "approved")
            # Returning from the original source also restores the already reviewed cache.
            self.assertEqual(prepare_review_report(path, 0.5, lambda message: None), padded_path)
            longer = load_json(prepare_review_report(padded_path, 1, lambda message: None))
            self.assertEqual(longer["candidates"][0]["end"], 21, "Padding must not accumulate")
            self.assertEqual(longer["candidates"][0]["status"], "pending")
            removed = load_json(prepare_review_report(padded_path, 0, lambda message: None))
            self.assertEqual(removed["candidates"][0]["end"], 20)
            first["end"] = 22.75
            first["status"] = "pending"
            first.pop("approval_key", None)
            save_json(padded_path, result)
            manual = load_json(prepare_review_report(padded_path, 1, lambda message: None))
            self.assertEqual(manual["candidates"][0]["end"], 22.75, "Respect manually edited endings")


class OutputFilenameTests(unittest.TestCase):
    def test_title_cleanup_length_and_approval_independence(self):
        clip = {"candidate_id": "c002", "start": 10, "end": 130,
                "title": "Гиперинфляция и политика большевиков"}
        key = approval_key(clip)
        self.assertEqual(output_filename(clip), "c002_Гиперинфляция и политика большевиков.mp4")
        clip["title"] = '  Тема/Заголовок: "рост"?*<>|\\ тест.  '
        cleaned = output_filename(clip)
        self.assertTrue(cleaned.startswith("c002_Тема_Заголовок_"))
        self.assertFalse(any(character in cleaned for character in '<>:"/\\|?*'))
        self.assertFalse(Path(cleaned).stem.endswith((" ", ".")))
        self.assertEqual(approval_key(clip), key, "Titles must not change approval of exact time boundaries")
        clip["title"] = "Длинная тема 😀 " * 100
        long_name = output_filename(clip)
        self.assertLessEqual(len(long_name.encode("utf-16-le")) // 2, 124)
        self.assertTrue(long_name.endswith(".mp4"))
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            folder = Path(temp)
            base = output_filename(clip, folder)
            (folder / base).write_bytes(b"previous video")
            second = output_filename(clip, folder)
            self.assertTrue(second.endswith("_2.mp4"))
            (folder / second).with_suffix(".json").write_text("{}", encoding="utf-8")
            self.assertTrue(output_filename(clip, folder).endswith("_3.mp4"))
            self.assertEqual((folder / base).read_bytes(), b"previous video")
            deep_folder = folder / ("nested_" + "a" * 85)
            shortened = output_filename(clip, deep_folder)
            longest_path = deep_folder / (Path(shortened).stem + ".partial.mp4")
            self.assertLessEqual(len(str(longest_path.resolve()).encode("utf-16-le")) // 2, 259)


class DesktopReviewTests(unittest.TestCase):
    def test_padding_button_requests_reapproval_and_displays_final_end(self):
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
                job = Path(temp)
                path = job / "analysis" / "raw" / "report.json"
                clip = {"candidate_id": "c001", "start": 10, "end": 20, "title": "Тема", "score": 80,
                        "reason": "Законченная мысль", "status": "approved"}
                clip["approval_key"] = approval_key(clip)
                save_json(job / "source.json", {"video_id": "test", "title": "Тест", "media": {"duration_sec": 60}})
                save_json(path.parent / "transcript.json", {"duration_sec": 60, "sentences": []})
                save_json(path, {"schema": 1, "video_id": "test", "signature": "raw",
                                 "settings": {"min_duration_sec": 1, "max_duration_sec": 30}, "candidates": [clip]})
                app.context = open_report(path)
                app.refresh_table()
                root.update()
                app.variables["end_padding_sec"].set("0,5")
                self.assertEqual(app.read_settings()["end_padding_sec"], 0.5)
                with patch("review_ui.messagebox.askyesno", return_value=False), patch.object(app, "start_worker") as worker:
                    app.apply_end_padding()
                    worker.assert_not_called()

                def synchronous_worker(operation, function):
                    app.events.put(("result", (operation, function())))

                with patch("review_ui.messagebox.askyesno", return_value=True), \
                        patch.object(app, "start_worker", side_effect=synchronous_worker):
                    app.apply_end_padding()
                    app._poll()
                    root.update()
                revised = app.current_candidate()
                self.assertEqual(revised["end"], 20.5)
                self.assertEqual(revised["status"], "pending")
                self.assertEqual(app.table.set("c001", "end"), "00:00:20.500")
                self.assertEqual(load_json(path)["candidates"][0]["status"], "approved")
                output_dir = app.context["job_dir"] / "output" / app.context["report"]["signature"]
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / output_filename(revised, output_dir)).write_bytes(b"existing clip")
                app.show_candidate()
                preview_name = output_filename(revised, output_dir)
                self.assertTrue(preview_name.endswith("_2.mp4"))
                self.assertIn(preview_name, app.details.get("1.0", "end"))
                app.decide("approved")
                root.update()
                self.assertEqual(app.current_candidate()["end"], 20.5, "Approval must not add padding again")
        finally:
            root.destroy()

    def test_scope_controls_and_legacy_partial_report(self):
        import tkinter as tk
        from review_ui import HighlightApp

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            app = HighlightApp(root)
            app.variables["analyze_full_video"].set(True)
            app.variables["analysis_start_min"].set("ignored")
            self.assertTrue(app.range_start_entry.instate(["disabled"]))
            self.assertTrue(app.read_settings()["analyze_full_video"])
            app.variables["analyze_full_video"].set(False)
            self.assertFalse(app.range_end_entry.instate(["disabled"]))
            app.variables["analysis_start_min"].set("1,5")
            app.variables["analysis_end_min"].set("30")
            self.assertEqual(analysis_bounds(app.read_settings()), (90, 1800))
            app.busy = True
            app.sync_analysis_range()
            self.assertTrue(app.range_start_entry.instate(["disabled"]))
            app._finish()
            self.assertFalse(app.range_start_entry.instate(["disabled"]))
            app.restore_report_settings({"report": {"settings": {}},
                                         "transcript": {"analysis_range": {"start": 900, "end": 2700}},
                                         "manifest": {"media": {"duration_sec": 3600}}})
            self.assertFalse(app.variables["analyze_full_video"].get())
            self.assertEqual(analysis_bounds(app.read_settings()), (900, 2700))
            app.variables["analyze_full_video"].set(True)
            app._finish()
            self.assertTrue(app.range_end_entry.instate(["disabled"]))
        finally:
            root.destroy()

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
                self.assertEqual(app.table.set("c001", "candidate_id"), "c001")
                self.assertIn(output_filename(clip), app.details.get("1.0", "end"))
                self.assertIn("Regression QA note", app.details.get("1.0", "end"))
                app.decide("approved")
                root.update()
                approved = load_json(path)["candidates"][0]
                self.assertEqual(approved["status"], "approved")
                self.assertEqual(approved["approval_key"], approval_key(approved))
                self.assertFalse(app.busy, "Approval must not start rendering")
                app.end.set("4.5")
                app.title.set("Исправленный заголовок")
                self.assertTrue(app.has_unsaved_edits())
                app.decide("pending")
                root.update()
                changed = load_json(path)["candidates"][0]
                self.assertEqual(changed["end"], 4.5)
                self.assertEqual(output_filename(changed), "c001_Исправленный заголовок.mp4")
                self.assertEqual(changed["status"], "pending")
                self.assertNotIn("approval_key", changed)
                self.assertEqual(app.table.set("c001", "candidate_id"), "c001")
                self.assertIn(output_filename(changed), app.details.get("1.0", "end"))
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
            clip = {"candidate_id": "c001", "start": 0.8, "end": 3.8, "title": "Тема/Заголовок: тест?", "status": "pending"}
            report = {"signature": "test", "settings": {"min_duration_sec": 1, "max_duration_sec": 4}, "candidates": [clip]}
            with self.assertRaises(ValueError):
                render_approved(manifest, report, job, lambda message: None)
            report_path = job / "analysis" / "test" / "report.json"
            save_json(report_path, report)
            save_json(report_path.parent / "transcript.json", {"duration_sec": manifest["media"]["duration_sec"]})
            report = load_json(prepare_review_report(report_path, 0.5, lambda message: None))
            clip = report["candidates"][0]
            self.assertAlmostEqual(clip["end"], 4.3)
            clip["status"] = "approved"
            clip["approval_key"] = approval_key(clip)
            changed = copy.deepcopy(report)
            changed["candidates"][0]["end"] = 4.0
            with self.assertRaisesRegex(ValueError, "изменились"):
                render_approved(manifest, changed, job, lambda message: None)
            self.assertFalse((job / "output").exists())
            outputs = render_approved(manifest, report, job, lambda message: None)
            self.assertEqual(len(outputs), 1)
            self.assertEqual(Path(outputs[0]).name, output_filename(clip))
            self.assertTrue(Path(outputs[0]).name.startswith(clip["candidate_id"] + "_"))
            media = probe(outputs[0])
            self.assertEqual((media["width"], media["height"]), (320, 180))
            self.assertEqual(media["frame_rate"], manifest["media"]["frame_rate"])
            self.assertAlmostEqual(media["duration_sec"], 3.5, delta=0.15)
            self.assertTrue(Path(outputs[0]).with_suffix(".json").exists())
            self.assertEqual(load_json(Path(outputs[0]).with_suffix(".json"))["approved_candidate"]["candidate_id"], "c001")
            original_bytes = Path(outputs[0]).read_bytes()
            original_metadata = Path(outputs[0]).with_suffix(".json").read_bytes()
            next_name = output_filename(clip, Path(outputs[0]).parent)
            self.assertTrue(next_name.endswith("_2.mp4"))
            repeated = render_approved(manifest, report, job, lambda message: None)
            self.assertEqual(Path(repeated[0]).name, next_name)
            self.assertEqual(Path(outputs[0]).read_bytes(), original_bytes)
            self.assertEqual(Path(outputs[0]).with_suffix(".json").read_bytes(), original_metadata)


if __name__ == "__main__":
    unittest.main()
