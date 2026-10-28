"""Tests of code-owned interval durations and constrained LLM selection."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analyzer import analyze
from clip_padding import prepare_review_report
from editorial_review import review_report
from interval_builder import build_interval_options, make_interval
from interval_selector import end_prompt, select_intervals
from job_store import load_json, save_json, validate_interval
from llm_context import INITIAL_PROMPT_BYTES, byte_size, fit_option_batches


def fixture_sentences(count=100, seconds=5):
    return [{"id": f"s{i:06d}", "start": i * seconds, "end": (i + 1) * seconds,
             "text": f"Полное предложение {i}."} for i in range(count)]


SETTINGS = {"llm_model": "test", "ollama_url": "unused", "min_duration_sec": 120,
            "max_duration_sec": 300, "end_padding_sec": 0.5}


class IntervalBuilderTests(unittest.TestCase):
    def test_all_options_include_padding_and_valid_source_bounds(self):
        sentences = fixture_sentences()
        options = build_interval_options(sentences, "s000000", 500, SETTINGS)
        self.assertTrue(options)
        self.assertEqual(len({o["interval_id"] for o in options}), len(options))
        for option in options:
            validate_interval(option["start"], option["end"], 500, 120, 300)
            self.assertLessEqual(option["end_padding"]["base_end"], option["end"])
            self.assertEqual(option["end"], min(option["end_padding"]["base_end"] + 0.5, 300))
        at_maximum = next(o for o in options if o["end_padding"]["base_end"] == 300)
        self.assertEqual(at_maximum["end_padding"]["applied_sec"], 0)
        near_source_end = build_interval_options(sentences, "s000050", 500, SETTINGS)
        self.assertEqual(near_source_end[-1]["end"], 500)
        self.assertEqual(near_source_end[-1]["end_padding"]["applied_sec"], 0)
        self.assertEqual(build_interval_options(sentences, "s000090", 500, SETTINGS), [])
        # Padding is part of final duration, but content can never be cut to fit max.
        first = {"id": "begin", "start": 0, "end": 1, "text": "Начало"}
        last = {"id": "end", "start": 118, "end": 119.7, "text": "Вывод"}
        self.assertAlmostEqual(make_interval(first, last, 500, SETTINGS)["duration_sec"], 120.2)
        with self.assertRaises(ValueError):
            make_interval(first, dict(last, end=300.2), 500, SETTINGS)

    def test_option_batches_are_exhaustive_and_do_not_truncate_content(self):
        sentences = fixture_sentences(140)
        options = build_interval_options(sentences, "s000000", 700, SETTINGS)
        builder = lambda batch: end_prompt(batch, sentences, "Тема")
        batches, oversized = fit_option_batches(options, builder, budget=6500)
        self.assertGreater(len(batches), 1)
        presented = [o["interval_id"] for batch in batches for o in batch]
        self.assertEqual(len(presented), len(set(presented)))
        self.assertEqual(set(presented) | {o["interval_id"] for o in oversized}, {o["interval_id"] for o in options})
        for batch in batches:
            self.assertLessEqual(byte_size(builder(batch)), 6500)
            text = json.JSONDecoder().raw_decode(builder(batch).split("Полный текст для сравнения окончаний: ", 1)[1])[0]
            self.assertEqual(text[-1]["id"], batch[-1]["end_id"])


class IntervalSelectorTests(unittest.TestCase):
    def test_unknown_id_retried_then_code_times_used_and_cache_preserved(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        bad_id_sent = False
        calls = []

        def transport(base, endpoint, payload=None, timeout=10):
            nonlocal bad_id_sent
            calls.append(payload)
            self.assertLessEqual(byte_size(payload["messages"][1]["content"]), 27000)
            schema = payload["format"]
            if "starts" in schema["properties"]:
                ids = schema["properties"]["starts"]["items"]["properties"]["start_id"]["enum"]
                value = {"starts": [{"start_id": ids[0], "topic": "Тема", "reason": "Понятное начало"}]}
            elif not bad_id_sent:
                bad_id_sent = True
                value = {"interval_id": "invented", "title": "Bad", "reason": "Bad", "score": 100}
            else:
                chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                # Any invented time fields must never replace catalog values.
                value = {"interval_id": chosen, "title": "Тема", "reason": "Законченная мысль", "score": 85,
                         "start": -100, "end": 99999}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            directory = Path(temp)
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_intervals(block, transcript, SETTINGS, directory, lambda message: None)
            self.assertTrue(result["candidates"])
            for candidate in result["candidates"]:
                validate_interval(candidate["start"], candidate["end"], 500, 120, 300)
                self.assertEqual(candidate["status"], "pending")
                self.assertNotIn("approval_key", candidate)
                self.assertEqual(candidate["end"], candidate["end_padding"]["padded_end"])
            self.assertTrue(bad_id_sent)
            self.assertEqual(result["statistics"]["options"], result["statistics"]["presented_options"] + result["statistics"]["oversized_options"])
            with patch("analyzer.ollama_request", side_effect=AssertionError("Cached selection must not call LLM")):
                self.assertEqual(select_intervals(block, transcript, SETTINGS, directory, lambda message: None), result)

    def test_none_is_valid_without_forced_clip(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]

        def transport(base, endpoint, payload=None, timeout=10):
            if "starts" in payload["format"]["properties"]:
                value = {"starts": [{"start_id": sentences[0]["id"], "topic": "Тема", "reason": "Начало"}]}
            else:
                value = {"interval_id": "none", "title": "", "reason": "Нет завершённого окончания", "score": 0}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_intervals(block, {"duration_sec": 500, "sentences": sentences}, SETTINGS,
                                          Path(temp), lambda message: None)
            self.assertEqual(result["candidates"], [])
            self.assertTrue(result["complete"])
            self.assertGreater(result["statistics"]["no_match_batches"], 0)

    def test_new_report_reuses_old_pass1_cache_and_does_not_double_pad(self):
        sentences = fixture_sentences(60)
        transcript = {"video_id": "test", "duration_sec": 300, "sentences": sentences}

        def legacy_transport(base, endpoint, payload=None, timeout=10):
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            if "topic" in payload["format"]["properties"]:
                value = {"topic": "Тема", "summary": "Резюме", "continues_previous": True,
                         "continues_next": True, "interest_score": 80}
            else:
                value = {"clips": []}
            return {"message": {"content": json.dumps(value)}}

        def new_transport(base, endpoint, payload=None, timeout=10):
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            schema = payload["format"]["properties"]
            self.assertNotIn("topic", schema, "PASS 1 must come from existing cache")
            if "starts" in schema:
                first = schema["starts"]["items"]["properties"]["start_id"]["enum"][0]
                value = {"starts": [{"start_id": first, "topic": "Тема", "reason": "Начало"}]}
            else:
                chosen = schema["interval_id"]["enum"][1]
                value = {"interval_id": chosen, "title": "Тема", "reason": "Вывод", "score": 80}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            with patch("analyzer.ollama_request", side_effect=legacy_transport):
                old_path = analyze(job, transcript, SETTINGS, lambda message: None, selection_method="legacy")
            old_bytes = old_path.read_bytes()
            with patch("analyzer.ollama_request", side_effect=new_transport):
                new_path = analyze(job, transcript, SETTINGS, lambda message: None)
            self.assertNotEqual(new_path, old_path)
            self.assertEqual(old_path.read_bytes(), old_bytes)
            report = load_json(new_path)
            self.assertEqual(report["selection_method"], "intervals")
            original_ends = [c["end"] for c in report["candidates"]]
            padded = load_json(prepare_review_report(new_path, 0.5, lambda message: None))
            self.assertEqual([c["end"] for c in padded["candidates"]], original_ends)
            # Editor sees semantic boundaries and recomputes padding after revisions.
            def editor(settings, prompt, schema, validate, **kwargs):
                c = report["candidates"][0]
                value = {"decision": "keep", "start_id": c["start_id"], "end_id": c["end_id"],
                         "title": "Тема", "reason": "Вывод", "single_topic": True, "self_contained": True,
                         "complete_ending": True, "score": 80}
                validate(value)
                return value

            with patch("editorial_review.ask", side_effect=editor):
                reviewed = load_json(review_report(new_path, lambda message: None))
            self.assertEqual(reviewed["candidates"][0]["end"], report["candidates"][0]["end"])
            revised_source = job / "analysis" / "revision" / "report.json"
            save_json(revised_source, dict(report, signature="revision"))
            save_json(revised_source.parent / "transcript.json", transcript)

            def revising_editor(settings, prompt, schema, validate, **kwargs):
                c = report["candidates"][0]
                next_index = next(i for i, s in enumerate(sentences) if s["id"] == c["end_id"]) + 1
                value = {"decision": "revise", "start_id": c["start_id"], "end_id": sentences[next_index]["id"],
                         "title": "Тема", "reason": "Более завершённое окончание", "single_topic": True,
                         "self_contained": True, "complete_ending": True, "score": 85}
                validate(value)
                return value

            with patch("editorial_review.ask", side_effect=revising_editor):
                revised_path = review_report(revised_source, lambda message: None)
            revised = load_json(revised_path)["candidates"][0]
            self.assertAlmostEqual(revised["end"], report["candidates"][0]["end"] + 5)
            self.assertEqual(revised["end_padding"]["padded_end"], revised["end"])
            repadded = load_json(prepare_review_report(revised_path, 0.5, lambda message: None))["candidates"][0]
            self.assertEqual(repadded["end"], revised["end"])

    def test_manual_bounds_update_duration_without_changing_candidate_id(self):
        import tkinter as tk
        from review_ui import HighlightApp

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        try:
            app = HighlightApp(root)
            sentences = fixture_sentences()
            option = build_interval_options(sentences, "s000000", 500, SETTINGS)[0]
            candidate = dict(option, candidate_id="c001", title="Тема", reason="Вывод", score=85, status="pending")
            app.context = {"report": {"settings": SETTINGS, "candidates": [candidate]},
                           "manifest": {"media": {"duration_sec": 500}}, "transcript": {"sentences": sentences}}
            app.refresh_table()
            root.update()
            app.end.set(str(candidate["end"] + 1))
            edited = app.edited_candidate()
            self.assertEqual(edited["candidate_id"], "c001")
            self.assertEqual(edited["duration_sec"], edited["end"] - edited["start"])
            self.assertTrue(edited["manual_bounds"])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
