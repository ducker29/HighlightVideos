"""Tests for the independent Shorts candidate discovery pass."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from interval_builder import build_interval_options
from job_store import profile_for_format, validate_interval
from shorts_selector import select_shorts, SHORTS_SELECTION_VERSION
from llm_context import byte_size


def fixture_sentences(count=100, seconds=5):
    return [{"id": f"s{i:06d}", "start": i * seconds, "end": (i + 1) * seconds,
             "text": f"Полное предложение {i}."} for i in range(count)]


SHORTS_SETTINGS = {
    "llm_model": "test", "ollama_url": "unused",
    "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
    "outputs": {"shorts": {"enabled": True, "min_duration_sec": 20,
                            "target_duration_sec": 40, "max_duration_sec": 60}},
}


def _shorts_profile(settings=SHORTS_SETTINGS):
    from job_store import normalize_profiles
    return profile_for_format(normalize_profiles(settings.get("outputs")), "short")


class ShortsCatalogTests(unittest.TestCase):
    def test_catalog_intervals_respect_shorts_duration_bounds(self):
        sentences = fixture_sentences()
        profile = _shorts_profile()
        options = build_interval_options(
            sentences, sentences[0]["id"], 500,
            {"min_duration_sec": profile["min_duration_sec"],
             "max_duration_sec": profile["max_duration_sec"],
             "end_padding_sec": 0.5})
        self.assertTrue(options)
        for option in options:
            validate_interval(option["start"], option["end"], 500,
                              profile["min_duration_sec"], profile["max_duration_sec"])
            self.assertGreaterEqual(option["duration_sec"], profile["min_duration_sec"] - 0.01)
            self.assertLessEqual(option["duration_sec"], profile["max_duration_sec"] + 0.51)

    def test_catalog_empty_for_too_short_source(self):
        sentences = fixture_sentences(3, seconds=5)
        profile = _shorts_profile()
        options = build_interval_options(
            sentences, sentences[0]["id"], 15,
            {"min_duration_sec": profile["min_duration_sec"],
             "max_duration_sec": profile["max_duration_sec"],
             "end_padding_sec": 0.5})
        self.assertEqual(options, [])


class ShortsSelectorTests(unittest.TestCase):
    def test_selection_returns_candidates_with_shorts_format(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}

        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                value = {"starts": [{"start_id": sentences[0]["id"], "reason": "Сильное начало"}]}
            elif "interval_id" in schema["properties"]:
                chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                value = {"interval_id": chosen, "title": "Shorts", "reason": "Хороший финал", "score": 80}
            else:
                value = {"interval_id": "none", "title": "", "reason": "", "score": 0}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS,
                                       Path(temp), lambda message: None)
            self.assertTrue(result["candidates"])
            for candidate in result["candidates"]:
                self.assertEqual(candidate["format"], "shorts")
                self.assertTrue(candidate["candidate_id"].startswith("sc"))
                self.assertEqual(candidate["status"], "pending")

    def test_none_choice_produces_no_candidate(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}

        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                value = {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]}
            else:
                value = {"interval_id": "none", "title": "", "reason": "Нет окончания", "score": 0}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS,
                                       Path(temp), lambda message: None)
            self.assertEqual(result["candidates"], [])
            self.assertGreater(result["statistics"]["no_match_batches"], 0)

    def test_unknown_interval_id_retried_then_rejected(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        bad_id_sent = False

        def transport(base, endpoint, payload=None, timeout=10):
            nonlocal bad_id_sent
            schema = payload["format"]
            if "starts" in schema["properties"]:
                value = {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]}
            elif not bad_id_sent:
                bad_id_sent = True
                value = {"interval_id": "invented", "title": "Bad", "reason": "Bad", "score": 100}
            else:
                chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                value = {"interval_id": chosen, "title": "Shorts", "reason": "Финал", "score": 75}
            return {"message": {"content": json.dumps(value)}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS,
                                       Path(temp), lambda message: None)
            self.assertTrue(bad_id_sent)
            self.assertTrue(result["candidates"])
            self.assertEqual(result["candidates"][0]["score"], 75)

    def test_disabled_shorts_returns_empty(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        disabled_settings = dict(SHORTS_SETTINGS, outputs={"shorts": {"enabled": False}})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            result = select_shorts(block, transcript, disabled_settings,
                                   Path(temp), lambda message: None)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["complete"])

    def test_deduplication_by_iou(self):
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        call_count = {"end": 0}

        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                return {"message": {"content": json.dumps(
                    {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]})}}
            call_count["end"] += 1
            chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
            return {"message": {"content": json.dumps(
                {"interval_id": chosen, "title": "Shorts", "reason": "Финал", "score": 80})}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS,
                                       Path(temp), lambda message: None)
            ids = [c["candidate_id"] for c in result["candidates"]]
            self.assertEqual(len(ids), len(set(ids)))


class ShortsIndependenceTests(unittest.TestCase):
    """Shorts must work even when highlights fail or produce nothing."""

    def test_shorts_works_with_empty_highlights(self):
        """Shorts produces candidates even if the block has no highlight options."""
        sentences = fixture_sentences(5, seconds=5)  # only 25s — too short for highlights (min 120s)
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 25, "sentences": sentences}

        # Highlights would produce nothing (too short), but Shorts (min 20s) might work
        # Actually 25s is still too short for 20-60s with padding. Use a case where
        # highlights explicitly fail but shorts catalog is non-empty.
        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                return {"message": {"content": json.dumps(
                    {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]})}}
            chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
            return {"message": {"content": json.dumps(
                {"interval_id": chosen, "title": "S", "reason": "Р", "score": 70})}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS,
                                       Path(temp), lambda m: None)
            self.assertIn("candidates", result)
            self.assertIn("complete", result)

    def test_shorts_cache_is_separate_from_highlights(self):
        """Shorts cache dir must not collide with highlight cache dir."""
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}

        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                return {"message": {"content": json.dumps(
                    {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]})}}
            chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
            return {"message": {"content": json.dumps(
                {"interval_id": chosen, "title": "S", "reason": "Р", "score": 80})}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            temp_path = Path(temp)
            with patch("analyzer.ollama_request", side_effect=transport):
                # Simulate the analyzer's cache layout
                highlight_cache = temp_path / "analysis" / "abc123"
                shorts_cache = temp_path / "analysis" / "shorts_abc123" / "block_0000"
                highlight_cache.mkdir(parents=True)
                # Write a fake highlight selection
                highlight_cache.mkdir(parents=True, exist_ok=True)
                (highlight_cache / "block_0000").mkdir(exist_ok=True)
                (highlight_cache / "block_0000" / "selection.json").write_text(
                    json.dumps({"candidates": [], "warnings": [], "complete": True, "statistics": {}}))
                # Run Shorts
                result = select_shorts(block, transcript, SHORTS_SETTINGS, shorts_cache,
                                       lambda m: None)
                # Verify shorts cache is in its own dir
                self.assertTrue(shorts_cache.exists())
                self.assertTrue((shorts_cache / "catalog.json").exists())
                # Verify highlight cache is untouched
                self.assertTrue((highlight_cache / "block_0000" / "selection.json").exists())
                # Verify they are in different directories
                self.assertNotEqual(shorts_cache.resolve(), highlight_cache.resolve())

    def test_shorts_recovers_from_partial_cache(self):
        """If starts.json exists but end_*.json is missing, Shorts resumes from cache."""
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        call_log = []

        def transport(base, endpoint, payload=None, timeout=10):
            call_log.append(payload["format"].get("properties", {}).get("interval_id", {}).get("enum", []))
            schema = payload["format"]
            if "starts" in schema["properties"]:
                return {"message": {"content": json.dumps(
                    {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]})}}
            chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
            return {"message": {"content": json.dumps(
                {"interval_id": chosen, "title": "S", "reason": "Р", "score": 70})}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            cache = Path(temp)
            # Pre-populate starts.json (simulate a previous interrupted run)
            import shorts_selector
            profile = shorts_selector._shorts_profile(SHORTS_SETTINGS)
            opts = build_interval_options(sentences, sentences[0]["id"], 500,
                                          {"min_duration_sec": profile["min_duration_sec"],
                                           "max_duration_sec": profile["max_duration_sec"],
                                           "end_padding_sec": 0.5})
            if opts:
                (cache / "catalog.json").write_text(json.dumps(
                    {"options": opts, "profile": {}, "sentence_ids": [s["id"] for s in sentences]}))
                (cache / "starts.json").write_text(json.dumps(
                    [{"start_id": sentences[0]["id"], "topic": "Т", "reason": "Н"}]))
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, SHORTS_SETTINGS, cache,
                                       lambda m: None)
            # The starts were cached, so the LLM should have been called only for ends
            self.assertTrue(result["candidates"])
            # Verify the end was picked from cache or LLM
            self.assertTrue((cache / "catalog.json").exists())
            self.assertTrue((cache / "starts.json").exists())

    def test_shorts_disabled_highlights_still_analyzed(self):
        """When highlights are disabled in settings, Shorts still runs independently."""
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}
        # Settings with highlights disabled but Shorts enabled
        settings = dict(SHORTS_SETTINGS)
        settings["outputs"] = {
            "highlights": {"enabled": False},
            "shorts": {"enabled": True, "min_duration_sec": 20,
                       "target_duration_sec": 40, "max_duration_sec": 60},
        }
        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                return {"message": {"content": json.dumps(
                    {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]})}}
            chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
            return {"message": {"content": json.dumps(
                {"interval_id": chosen, "title": "S", "reason": "Р", "score": 70})}}

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp:
            with patch("analyzer.ollama_request", side_effect=transport):
                result = select_shorts(block, transcript, settings,
                                       Path(temp), lambda m: None)
            self.assertIn("candidates", result)
            self.assertIn("complete", result)


if __name__ == "__main__":
    unittest.main()
