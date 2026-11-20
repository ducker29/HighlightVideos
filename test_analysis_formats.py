"""Format routing and cache safety, using only synthetic media and model replies."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from analyzer import analyze
from app_backend import run_analysis, open_report
from job_store import load_json, normalize_profiles, save_json
from test_stage7_modes import make_transcript, settings_for, make_fake_http


class AnalysisFormatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name)
        self.transcript = make_transcript()
        save_json(self.job / "source.json", {
            "video_id": "test", "media": {"duration_sec": 100}, "source_path": "synthetic.mkv"})

    def analyze(self, mode):
        http, _ = make_fake_http()
        with patch("analyzer.ollama_request", side_effect=http), \
             patch("analyzer.list_models", return_value=["test"]):
            return analyze(self.job, self.transcript, settings_for(mode), lambda _: None)

    def test_modes_route_real_selectors_and_llm_calls(self):
        from interval_selector import select_intervals
        from shorts_selector import select_shorts
        from shorts_editorial import review_shorts_candidates
        from editorial_review import review_report
        from analyzer import ask

        shorts_inputs = []
        shorts_results = []
        shorts_counts = []
        for mode in ("both", "shorts_only", "highlights_only"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                job = Path(temp)
                manifest = {"video_id": "test", "media": {"duration_sec": 100}}
                settings = settings_for(mode)
                settings.update(device="cpu", youtube_url="synthetic", editorial_model="test")
                http, state = make_fake_http()
                calls = {"highlights": 0, "shorts": 0, "highlight_editorial": 0,
                         "shorts_editorial": 0}
                active = [None]

                def counted_ask(*args, **kwargs):
                    if active[0]:
                        calls[active[0]] += 1
                    return ask(*args, **kwargs)

                def wrap(function, branch):
                    def routed(*args, **kwargs):
                        previous = active[0]
                        active[0] = branch
                        try:
                            if branch == "shorts":
                                shorts_inputs.append(copy.deepcopy(args[:3]))
                            return function(*args, **kwargs)
                        finally:
                            active[0] = previous
                    return routed

                # Select a real catalog interval, then return valid editorial decisions.
                def transport(base, endpoint, payload=None, timeout=10):
                    props = payload.get("format", {}).get("properties", {}) if payload else {}
                    if "interval_id" in props:
                        import json
                        value = next(i for i in props["interval_id"]["enum"] if i != "none")
                        return {"message": {"content": json.dumps({
                            "interval_id": value, "title": "Synthetic", "reason": "Complete", "score": 80})}}
                    if "decision" in props:
                        import json
                        value = {k: True for k, v in props.items() if v.get("type") == "boolean"}
                        value.update(decision="keep", start_id="s0000", end_id="s0002",
                                     title="Synthetic", reason="Complete", score=80)
                        return {"message": {"content": json.dumps(value)}}
                    return http(base, endpoint, payload, timeout)

                with ExitStack() as stack:
                    for target in ("analyzer.ask", "interval_selector.ask", "shorts_selector.ask",
                                   "shorts_editorial.ask", "editorial_review.ask"):
                        stack.enter_context(patch(target, side_effect=counted_ask))
                    stack.enter_context(patch("analyzer.ollama_request", side_effect=transport))
                    stack.enter_context(patch("analyzer.list_models", return_value=["test"]))
                    stack.enter_context(patch("app_backend.list_models", return_value=["test"]))
                    stack.enter_context(patch("app_backend.check_runtime"))
                    stack.enter_context(patch("app_backend.download", return_value=(job, manifest)))
                    whisper = stack.enter_context(patch("app_backend.transcribe", return_value=self.transcript))
                    stack.enter_context(patch("app_backend.open_report", side_effect=lambda p: load_json(p)))
                    highlights = stack.enter_context(patch("interval_selector.select_intervals",
                        side_effect=wrap(select_intervals, "highlights")))
                    shorts = stack.enter_context(patch("shorts_selector.select_shorts",
                        side_effect=wrap(select_shorts, "shorts")))
                    stack.enter_context(patch("shorts_editorial.review_shorts_candidates",
                        side_effect=wrap(review_shorts_candidates, "shorts_editorial")))
                    editor = stack.enter_context(patch("app_backend.review_report",
                        side_effect=wrap(review_report, "highlight_editorial")))
                    padding = stack.enter_context(patch("app_backend.prepare_review_report", side_effect=lambda p, *a: p))
                    report = run_analysis(settings, lambda _: None)
                whisper.assert_called_once()
                self.assertEqual(state["window_count"], 1)
                if mode == "shorts_only":
                    highlights.assert_not_called()
                    editor.assert_not_called()
                    padding.assert_not_called()
                    self.assertEqual(calls["highlights"], 0)
                    self.assertEqual(calls["highlight_editorial"], 0)
                    self.assertEqual(report["candidates"], [])
                else:
                    self.assertGreater(calls["highlights"], 0)
                    self.assertGreater(calls["highlight_editorial"], 0)
                if mode == "highlights_only":
                    shorts.assert_not_called()
                    self.assertEqual(calls["shorts_editorial"], 0)
                else:
                    self.assertGreater(calls["shorts"], 0)
                    self.assertGreater(calls["shorts_editorial"], 0)
                    self.assertTrue(report["shorts_candidates"])
                    shorts_results.append(report["shorts_candidates"])
                    shorts_counts.append((calls["shorts"], calls["shorts_editorial"]))
        self.assertEqual(shorts_results[0], shorts_results[1])
        self.assertEqual(shorts_counts[0], shorts_counts[1])
        self.assertEqual(shorts_inputs[0][:2], shorts_inputs[1][:2])
        both_settings, only_settings = shorts_inputs[0][2], shorts_inputs[1][2]
        both_settings["outputs"]["highlights"]["enabled"] = False
        self.assertEqual(both_settings, only_settings)

    def test_disabled_formats_rejected_before_any_work(self):
        settings = settings_for("shorts_only")
        settings["outputs"]["shorts"]["enabled"] = False
        with patch("app_backend.list_models") as models:
            with self.assertRaisesRegex(ValueError, "хотя бы один формат"):
                run_analysis(settings, lambda _: None)
            models.assert_not_called()
        with self.assertRaises(ValueError):
            analyze(self.job, self.transcript, settings, lambda _: None)
        self.assertFalse((self.job / "analysis").exists())

    def test_cache_empty_results_and_mode_switch_preserve_decisions(self):
        path = self.analyze("highlights_only")
        report = load_json(path)
        self.assertEqual(report["analyzed_formats"], ["highlights"])
        report["candidates"] = [{"candidate_id": "c001", "status": "approved", "approval_key": "saved"}]
        save_json(path, report)
        original = path.read_bytes()
        both = self.analyze("both")
        self.assertNotEqual(path, both)
        self.assertEqual(path.read_bytes(), original)

        self.assertEqual(load_json(both)["analyzed_formats"], ["highlights", "shorts"])
        only = self.analyze("shorts_only")
        self.assertEqual(load_json(only)["candidates"], [])
        self.assertEqual(load_json(only)["shorts_candidates"], [])
        with patch("analyzer.list_models", side_effect=AssertionError("cache miss")):
            self.assertEqual(analyze(self.job, self.transcript, settings_for("shorts_only"), lambda _: None), only)
        self.assertEqual(self.analyze("highlights_only"), path)
        self.assertEqual(path.read_bytes(), original)

    def test_cache_marker_without_requested_result_list_is_not_sufficient(self):
        path = self.analyze("both")
        report = load_json(path)
        del report["shorts_candidates"]
        save_json(path, report)
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "всех запрошенных форматов"):
            self.analyze("both")
        self.assertEqual(path.read_bytes(), original)

    def test_incomplete_cache_is_not_returned_or_overwritten(self):
        path = self.analyze("both")
        report = load_json(path)
        report["analyzed_formats"] = ["highlights"]
        save_json(path, report)
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "всех запрошенных форматов"):
            self.analyze("both")
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_report_opens_without_rewriting(self):
        path = self.job / "analysis" / "legacy" / "report.json"
        save_json(path, {"schema": 1, "video_id": "test", "settings": {},
                         "candidates": [{"candidate_id": "c001", "status": "approved", "approval_key": "saved"}]})
        save_json(path.parent / "transcript.json", self.transcript)
        before = path.read_bytes()
        context = open_report(path)
        self.assertTrue(context["report"]["profiles"]["highlights"]["enabled"])
        self.assertEqual(context["report"]["candidates"][0]["approval_key"], "saved")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(normalize_profiles(None)["shorts"]["enabled"])

    def test_historical_cache_is_preserved_not_mistaken_for_requested_formats(self):
        import hashlib
        import json
        from interval_selector import SELECTION_VERSION
        from llm_context import CONTEXT_POLICY_VERSION
        settings = settings_for("both")
        window = hashlib.sha256(json.dumps({"transcript": self.transcript, "model": "test",
            "min": 20, "max": 60, "version": 1}, sort_keys=True,
            ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
        key = {"windows": window, "selection_version": SELECTION_VERSION,
               "padding": 0.5, "context": 32768, "context_policy": CONTEXT_POLICY_VERSION}
        old_signature = "iv1_" + hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:20]
        old = self.job / "analysis" / old_signature / "report.json"
        save_json(old, {"schema": 1, "settings": settings_for("highlights_only"),
                       "candidates": [{"status": "approved", "approval_key": "keep"}],
                       "shorts_candidates": []})
        before = old.read_bytes()
        new = self.analyze("both")
        self.assertNotEqual(old, new)
        self.assertEqual(load_json(new)["analyzed_formats"], ["highlights", "shorts"])
        self.assertEqual(old.read_bytes(), before)

    def test_ui_collects_and_restores_formats_without_loading_user_files(self):
        from review_ui import HighlightApp

        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        app = HighlightApp.__new__(HighlightApp)
        values = dict(youtube_url="synthetic", local_video_path="", asr_model="small",
                      llm_model="test", ollama_url="unused", device="cpu", language="ru",
                      min_duration_sec="20", max_duration_sec="60", end_padding_sec="0.5",
                      analyze_full_video=True, shorts_min_duration_sec="15",
                      shorts_target_duration_sec="30", shorts_max_duration_sec="60")
        app.variables = {key: Variable(value) for key, value in values.items()}
        app.highlights_enabled_var = Variable(False)
        app.shorts_enabled_var = Variable(True)
        app.sync_analysis_range = Mock()
        collected = app.read_settings()
        self.assertFalse(collected["outputs"]["highlights"]["enabled"])
        self.assertTrue(collected["outputs"]["shorts"]["enabled"])
        app.shorts_enabled_var.set(False)
        with self.assertRaisesRegex(ValueError, "хотя бы один формат"):
            app.read_settings()
        app.restore_report_settings({"report": {"settings": {"analyze_full_video": True}}})
        self.assertTrue(app.highlights_enabled_var.get())
        self.assertFalse(app.shorts_enabled_var.get())
        app.restore_report_settings({"report": {"settings": collected}})
        self.assertFalse(app.highlights_enabled_var.get())
        self.assertTrue(app.shorts_enabled_var.get())


if __name__ == "__main__":
    unittest.main()
