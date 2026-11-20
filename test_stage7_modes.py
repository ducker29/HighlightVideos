"""Stage 7.1: Regression tests for three operating modes (Highlights, Shorts, Both).

Verifies architectural independence:
- Highlights-only: shorts pass is skipped entirely
- Shorts-only: highlights PASS 2 is skipped, shorts pass runs
- Both: shared PASS 1, independent branches
- Error isolation: failure in one branch does not crash the other
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analyzer import analyze
from job_store import load_json, save_json, normalize_profiles, profile_for_format, candidate_format


def make_transcript(n=10, step=10.0):
    sentences = [{"id": f"s{i:04d}", "start": i * step, "end": (i + 1) * step,
                  "text": f"Предложение номер {i}."} for i in range(n)]
    return {"video_id": "test", "duration_sec": n * step, "sentences": sentences}


def settings_for(mode):
    base = {
        "llm_model": "test",
        "ollama_url": "http://unused",
        "min_duration_sec": 20,
        "max_duration_sec": 60,
        "llm_context_tokens": 32768,
    }
    if mode == "highlights_only":
        base["outputs"] = {"highlights": {"min_duration_sec": 20, "max_duration_sec": 60},
                           "shorts": {"enabled": False}}
    elif mode == "shorts_only":
        base["outputs"] = {"highlights": {"enabled": False, "min_duration_sec": 20, "max_duration_sec": 60},
                           "shorts": {"enabled": True, "min_duration_sec": 15,
                                      "target_duration_sec": 30, "max_duration_sec": 60}}
    else:
        base["outputs"] = {"highlights": {"min_duration_sec": 20, "max_duration_sec": 60},
                           "shorts": {"enabled": True, "min_duration_sec": 15,
                                      "target_duration_sec": 30, "max_duration_sec": 60}}
    return base


def make_fake_http():
    """Returns a fake ollama_request that serves window, clip, and shorts responses."""
    state = {"window_count": 0}

    def fake_http(base, endpoint, payload=None, timeout=10):
        if endpoint == "/api/tags":
            return {"models": [{"name": "test"}]}
        fmt = payload.get("format", {})
        props = fmt.get("properties", {})
        if "topic" in props:
            state["window_count"] += 1
            resp = {"topic": "Тема", "summary": "Резюме.",
                    "continues_previous": False, "continues_next": False, "interest_score": 50}
        elif "starts" in props:
            resp = {"starts": [{"start_id": "s0000", "topic": "Т", "reason": "Р"}]}
        elif "interval_id" in props:
            resp = {"interval_id": "none", "title": "", "reason": "Нет", "score": 0}
        else:
            resp = {"clips": []}
        return {"message": {"content": json.dumps(resp)}}

    return fake_http, state


class Stage7ModeTests(unittest.TestCase):

    def _run_analyze(self, mode, fake_http, selection_method="intervals"):
        settings = settings_for(mode)
        transcript = make_transcript()
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method=selection_method)
            report = load_json(path)
            return report, job
        finally:
            tmp.cleanup()

    def test_highlights_only_skips_shorts_pass(self):
        """When shorts.enabled=False, no shorts candidates and no shorts cache dirs."""
        fake_http, _ = make_fake_http()
        settings = settings_for("highlights_only")
        transcript = make_transcript()
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")
            report = load_json(path)
            self.assertEqual(report["shorts_candidates"], [],
                             "Shorts must be empty when disabled")
            self.assertIn("candidates", report)
            shorts_dirs = [d for d in job.iterdir() if d.is_dir() and "shorts_" in d.name]
            self.assertEqual(shorts_dirs, [],
                             f"No shorts_* cache dirs expected, found: {shorts_dirs}")
        finally:
            tmp.cleanup()

    def test_both_modes_produce_separate_candidate_lists(self):
        """In 'both' mode, highlights and shorts candidates are in separate lists with correct prefixes."""
        fake_http, _ = make_fake_http()
        report, _ = self._run_analyze("both", fake_http)

        self.assertIn("shorts_candidates", report)
        self.assertIn("candidates", report)
        for c in report.get("shorts_candidates", []):
            self.assertTrue(c["candidate_id"].startswith("sc"),
                            f"Shorts candidate ID should start with 'sc', got {c['candidate_id']}")
        for c in report.get("candidates", []):
            self.assertTrue(c["candidate_id"].startswith("c") and not c["candidate_id"].startswith("sc"),
                            f"Highlight candidate ID should start with 'c', got {c['candidate_id']}")

    def test_pass1_windows_are_cached(self):
        """PASS 1 window analyses are written to disk and reused on second call."""
        fake_http, state = make_fake_http()
        settings = settings_for("both")
        transcript = make_transcript()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")
            windows_cached = [f for f in (job / "analysis").rglob("window_*.json")
                              if f.name.endswith(".json") and not f.name.endswith("_attempts.json")]
            self.assertGreater(len(windows_cached), 0, "PASS 1 window analyses must be cached on disk")
            self.assertGreater(state["window_count"], 0, "Window LLM calls must have been made")

    def test_second_call_uses_cache_no_new_llm(self):
        """Second analyze() call with same signature returns cached report without new LLM calls."""
        fake_http, state = make_fake_http()
        settings = settings_for("both")
        transcript = make_transcript()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            job = Path(temp)
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]):
                path1 = analyze(job, transcript, settings, lambda m: None,
                                selection_method="intervals")
            calls_before = state["window_count"]
            with patch("analyzer.ollama_request", side_effect=AssertionError("LLM contacted on cached run")), \
                 patch("analyzer.list_models", return_value=["test"]):
                path2 = analyze(job, transcript, settings, lambda m: None,
                                selection_method="intervals")
            self.assertEqual(path1, path2)
            self.assertEqual(state["window_count"], calls_before,
                             "No new window LLM calls on cached run")

    def test_profiles_normalized_correctly(self):
        """normalize_profiles produces valid profiles for all three modes."""
        for mode in ("highlights_only", "shorts_only", "both"):
            settings = settings_for(mode)
            profiles = normalize_profiles(settings["outputs"])
            self.assertIn("highlights", profiles)
            self.assertIn("shorts", profiles)
            if mode == "highlights_only":
                self.assertFalse(profiles["shorts"]["enabled"])
            else:
                self.assertTrue(profiles["shorts"]["enabled"])

    def test_shorts_profile_for_format_raises_when_disabled(self):
        """profile_for_format raises ValueError for shorts when disabled."""
        settings = settings_for("highlights_only")
        profiles = normalize_profiles(settings["outputs"])
        with self.assertRaises(ValueError):
            profile_for_format(profiles, "short")

    def test_candidate_format_values(self):
        """candidate_format handles 'highlight', 'short', and None correctly."""
        self.assertEqual(candidate_format({"format": "highlight"}), "highlight")
        self.assertEqual(candidate_format({"format": "short"}), "short")
        self.assertEqual(candidate_format({}), "highlight")
        self.assertEqual(candidate_format({"format": None}), "highlight")
        with self.assertRaises(ValueError):
            candidate_format({"format": "invalid"})

    # ------------------------------------------------------------------ #
    # Stage 7.1 additional independence tests
    # ------------------------------------------------------------------ #

    def test_shorts_only_skips_highlights_pass2(self):
        """When only Shorts is requested, Highlights PASS 2 (select_intervals/collect_clips)
        must NOT be invoked, while Shorts PASS (select_shorts) MUST run."""
        fake_http, _ = make_fake_http()
        settings = settings_for("shorts_only")
        transcript = make_transcript()

        highlights_pass2_called = []
        shorts_pass_called = []

        original_select_intervals = None
        original_select_shorts = None

        import interval_selector
        import shorts_selector

        def spy_select_intervals(block, transcript_, settings_, directory, log):
            highlights_pass2_called.append(1)
            # Delegate to real logic but tag the result
            result = original_select_intervals(block, transcript_, settings_, directory, log)
            return result

        def spy_select_shorts(block, transcript_, settings_, cache_dir, log):
            shorts_pass_called.append(1)
            return original_select_shorts(block, transcript_, settings_, cache_dir, log)

        original_select_intervals = interval_selector.select_intervals
        original_select_shorts = shorts_selector.select_shorts

        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]), \
                 patch.object(interval_selector, "select_intervals", side_effect=spy_select_intervals), \
                 patch.object(shorts_selector, "select_shorts", side_effect=spy_select_shorts):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")
            report = load_json(path)

            self.assertGreater(len(shorts_pass_called), 0,
                               "Shorts PASS must be invoked when shorts are enabled")
            self.assertEqual(highlights_pass2_called, [],
                             "Highlights PASS 2 must be skipped in Shorts-only mode")
        finally:
            interval_selector.select_intervals = original_select_intervals
            shorts_selector.select_shorts = original_select_shorts
            tmp.cleanup()

    def test_highlights_pass2_failure_still_produces_shorts(self):
        """If Highlights PASS 2 raises an exception for a block, Shorts candidates
        must still be produced (error isolation)."""
        fake_http, _ = make_fake_http()
        settings = settings_for("both")
        transcript = make_transcript()

        import interval_selector
        import shorts_selector
        original_select_intervals = interval_selector.select_intervals

        def failing_select_intervals(block, transcript_, settings_, directory, log):
            raise RuntimeError("Simulated Highlights PASS 2 crash")

        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]), \
                 patch.object(interval_selector, "select_intervals",
                              side_effect=failing_select_intervals):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")
            report = load_json(path)

            # Highlights candidates should be empty or partial (error was swallowed)
            # Shorts candidates should still be present
            self.assertIn("shorts_candidates", report)
            self.assertIn("analysis_warnings", report)
            # The error from highlights should be logged as a warning
            self.assertTrue(
                any("PASS 2" in w or "блок" in w.lower() for w in report["analysis_warnings"]),
                f"Highlights failure should be recorded in warnings, got: {report['analysis_warnings']}")
        finally:
            interval_selector.select_intervals = original_select_intervals
            tmp.cleanup()

    def test_shorts_pass_failure_preserves_highlights_candidates(self):
        """If Shorts PASS raises an exception, already-computed Highlights candidates
        must be preserved in the report (error isolation in the other direction)."""
        fake_http, _ = make_fake_http()
        settings = settings_for("both")
        transcript = make_transcript()

        import interval_selector
        import shorts_selector
        original_select_shorts = shorts_selector.select_shorts

        def failing_select_shorts(block, transcript_, settings_, cache_dir, log):
            raise RuntimeError("Simulated Shorts PASS crash")

        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=fake_http), \
                 patch("analyzer.list_models", return_value=["test"]), \
                 patch.object(shorts_selector, "select_shorts",
                              side_effect=failing_select_shorts):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")
            report = load_json(path)

            # Highlights candidates must be preserved despite the Shorts crash
            self.assertIn("candidates", report)
            self.assertIn("shorts_candidates", report)
            # The error must be recorded in analysis_warnings
            self.assertTrue(
                any("PASS-Shorts" in w and "Simulated Shorts PASS crash" in w
                    for w in report["analysis_warnings"]),
                f"Shorts failure should be in warnings, got: {report['analysis_warnings']}")
            # Shorts candidates list exists (may be empty if all blocks failed)
            self.assertIsInstance(report["shorts_candidates"], list)
            # analysis_complete must be False after a crash
            self.assertFalse(report["analysis_complete"])
        finally:
            shorts_selector.select_shorts = original_select_shorts
            tmp.cleanup()

    def test_pass1_not_reexecuted_for_second_branch(self):
        """PASS 1 (window analysis) results are computed once and reused by both
        Highlights and Shorts branches — no duplicate LLM window calls."""
        window_llm_calls = []

        def counting_fake_http(base, endpoint, payload=None, timeout=10):
            if endpoint == "/api/tags":
                return {"models": [{"name": "test"}]}
            fmt = payload.get("format", {})
            props = fmt.get("properties", {})
            if "topic" in props:
                window_llm_calls.append(1)
                resp = {"topic": "Тема", "summary": "Резюме.",
                        "continues_previous": False, "continues_next": False, "interest_score": 50}
            elif "starts" in props:
                resp = {"starts": [{"start_id": "s0000", "topic": "Т", "reason": "Р"}]}
            elif "interval_id" in props:
                resp = {"interval_id": "none", "title": "", "reason": "Нет", "score": 0}
            else:
                resp = {"clips": []}
            return {"message": {"content": json.dumps(resp)}}

        settings = settings_for("both")
        transcript = make_transcript()
        tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        job = Path(tmp.name)
        try:
            save_json(job / "source.json", {"video_id": "test"})
            with patch("analyzer.ollama_request", side_effect=counting_fake_http), \
                 patch("analyzer.list_models", return_value=["test"]):
                path = analyze(job, transcript, settings, lambda m: None,
                               selection_method="intervals")

            # Count how many window JSON files were written (PASS 1 artifacts)
            window_files = [f for f in (job / "analysis").rglob("window_*.json")
                            if f.name.endswith(".json") and not f.name.endswith("_attempts.json")]
            n_windows = len(window_files)

            # The number of window LLM calls should equal the number of windows,
            # NOT 2x that (which would indicate PASS 1 ran twice).
            self.assertGreater(n_windows, 0)
            self.assertEqual(
                len(window_llm_calls), n_windows,
                f"PASS 1 LLM calls ({len(window_llm_calls)}) must equal window count "
                f"({n_windows}); more would mean duplicate execution")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
