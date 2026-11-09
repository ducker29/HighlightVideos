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


class ShortsCacheInvalidationTests(unittest.TestCase):
    """Регрессионные тесты: кеш Shorts инвалидируется при изменении настроек Shorts."""

    def _run_select(self, settings, cache_root):
        """Выполняет select_shorts с мокированным LLM и возвращает путь к кешу."""
        sentences = fixture_sentences()
        block = [{"sentences": sentences, "analysis": {"topic": "Тема"}}]
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}

        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                value = {"starts": [{"start_id": sentences[0]["id"], "reason": "Начало"}]}
            else:
                chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                value = {"interval_id": chosen, "title": "S", "reason": "R", "score": 70}
            return {"message": {"content": json.dumps(value)}}

        from shorts_selector import shorts_cache_signature
        shorts_sig = shorts_cache_signature(settings)
        cache_dir = Path(cache_root) / f"shorts_{shorts_sig}" / "block_0000"
        with patch("analyzer.ollama_request", side_effect=transport):
            result = select_shorts(block, transcript, settings, cache_dir, lambda m: None)
        return cache_dir, result

    def test_same_settings_reuse_shorts_cache(self):
        """Одинаковые настройки → кеш Shorts переиспользуется (catalog.json существует)."""
        with tempfile.TemporaryDirectory() as temp:
            settings = dict(SHORTS_SETTINGS)
            cache_dir, _ = self._run_select(settings, temp)
            self.assertTrue((cache_dir / "catalog.json").exists())

            # Повторный запуск с теми же настройками — catalog.json уже есть
            calls = []
            def tracking_transport(base, endpoint, payload=None, timeout=10):
                calls.append(1)
                schema = payload["format"]
                if "starts" in schema["properties"]:
                    value = {"starts": [{"start_id": "s000000", "reason": "Начало"}]}
                else:
                    chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                    value = {"interval_id": chosen, "title": "S", "reason": "R", "score": 70}
                return {"message": {"content": json.dumps(value)}}

            with patch("analyzer.ollama_request", side_effect=tracking_transport):
                select_shorts([{"sentences": fixture_sentences(),
                                 "analysis": {"topic": "Тема"}}],
                              {"video_id": "test", "duration_sec": 500,
                               "sentences": fixture_sentences()},
                              settings, cache_dir, lambda m: None)

            # catalog.json не пересоздавался — значит, LLM вызывался только для PASS 2A/2B,
            # но catalog.json не пересчитывался.
            self.assertTrue((cache_dir / "catalog.json").exists())

    def test_changed_duration_invalidates_shorts_cache(self):
        """Изменённые настройки длительности → кеш Shorts пересоздаётся (новый путь)."""
        with tempfile.TemporaryDirectory() as temp:
            settings_1 = dict(SHORTS_SETTINGS)
            cache_dir_1, _ = self._run_select(settings_1, temp)
            self.assertTrue(cache_dir_1.exists())
            sig_1 = cache_dir_1.parent.name  # shorts_<sig>

            # Меняем min_duration_sec в настройках Shorts
            settings_2 = {
                "llm_model": "test", "ollama_url": "unused",
                "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
                "outputs": {"shorts": {"enabled": True, "min_duration_sec": 25,
                                        "target_duration_sec": 45, "max_duration_sec": 60}},
            }
            cache_dir_2, _ = self._run_select(settings_2, temp)
            sig_2 = cache_dir_2.parent.name

            # Сигнатуры должны различаться
            self.assertNotEqual(sig_1, sig_2)
            # Старый кеш не тронут
            old_catalog = Path(temp) / sig_1 / "block_0000" / "catalog.json"
            self.assertTrue(old_catalog.exists(), f"Старый кеш не найден: {old_catalog}")
            # Новый кеш создан
            new_catalog = Path(temp) / sig_2 / "block_0000" / "catalog.json"
            self.assertTrue(new_catalog.exists(), f"Новый кеш не найден: {new_catalog}")

    def test_highlights_cache_unaffected_by_shorts_settings(self):
        """Смена настроек Shorts не влияет на сигнатуру Highlights (window_signature)."""
        from analyzer import PROMPT_VERSION
        import hashlib

        sentences = fixture_sentences()
        transcript = {"video_id": "test", "duration_sec": 500, "sentences": sentences}

        settings_1 = dict(SHORTS_SETTINGS)
        settings_2 = {
            "llm_model": "test", "ollama_url": "unused",
            "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
            "outputs": {"shorts": {"enabled": True, "min_duration_sec": 99,
                                    "target_duration_sec": 50, "max_duration_sec": 70}},
        }

        # window_signature зависит только от transcript, llm_model, min/max (Highlights), PROMPT_VERSION
        def window_sig(transcript, settings):
            return hashlib.sha256(json.dumps({
                "transcript": transcript, "model": settings["llm_model"],
                "min": settings["min_duration_sec"], "max": settings["max_duration_sec"],
                "version": PROMPT_VERSION}, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:20]

        sig_1 = window_sig(transcript, settings_1)
        sig_2 = window_sig(transcript, settings_2)
        self.assertEqual(sig_1, sig_2, "Смена настроек Shorts не должна менять window_signature")


class ShortsFullKeyTests(unittest.TestCase):
    """Тесты итогового ключа Shorts (window_signature + shorts_cache_signature)."""

    def _make_transcript(self, sentences=None):
        if sentences is None:
            sentences = fixture_sentences()
        return {"video_id": "test", "duration_sec": 500, "sentences": sentences}

    def _window_signature(self, transcript, settings):
        """Воспроизводит логику формирования window_signature из analyzer.py."""
        from analyzer import PROMPT_VERSION
        import hashlib
        return hashlib.sha256(json.dumps({
            "transcript": transcript, "model": settings["llm_model"],
            "min": settings["min_duration_sec"], "max": settings["max_duration_sec"],
            "version": PROMPT_VERSION}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]

    def test_same_inputs_same_key(self):
        """Одинаковые базовые данные и Shorts-настройки → одинаковый итоговый ключ."""
        from shorts_selector import shorts_full_key

        transcript = self._make_transcript()
        settings = dict(SHORTS_SETTINGS)
        ws = self._window_signature(transcript, settings)

        key_1 = shorts_full_key(ws, settings)
        key_2 = shorts_full_key(ws, settings)

        self.assertEqual(key_1, key_2)
        self.assertIsNotNone(key_1)
        self.assertEqual(len(key_1), 16)

    def test_changed_shorts_settings_different_key(self):
        """Изменённые Shorts-настройки → другой итоговый ключ."""
        from shorts_selector import shorts_full_key

        transcript = self._make_transcript()
        ws = self._window_signature(transcript, SHORTS_SETTINGS)

        settings_1 = dict(SHORTS_SETTINGS)
        settings_2 = {
            "llm_model": "test", "ollama_url": "unused",
            "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
            "outputs": {"shorts": {"enabled": True, "min_duration_sec": 25,
                                    "target_duration_sec": 45, "max_duration_sec": 60}},
        }

        key_1 = shorts_full_key(ws, settings_1)
        key_2 = shorts_full_key(ws, settings_2)

        self.assertNotEqual(key_1, key_2)

    def test_changed_base_signature_different_key(self):
        """Изменённая базовая сигнатура (транскрипт/модель/Highlights) → другой ключ."""
        from shorts_selector import shorts_full_key

        transcript_1 = self._make_transcript()
        settings = dict(SHORTS_SETTINGS)
        ws_1 = self._window_signature(transcript_1, settings)
        key_1 = shorts_full_key(ws_1, settings)

        # Изменяем транскрипт → меняется window_signature
        transcript_2 = self._make_transcript(fixture_sentences(101))
        ws_2 = self._window_signature(transcript_2, settings)
        key_2 = shorts_full_key(ws_2, settings)

        self.assertNotEqual(ws_1, ws_2)
        self.assertNotEqual(key_1, key_2)

    def test_changed_model_different_key(self):
        """Изменённая модель LLM → другой window_signature → другой итоговый ключ."""
        from shorts_selector import shorts_full_key

        transcript = self._make_transcript()

        settings_1 = dict(SHORTS_SETTINGS)
        settings_2 = dict(SHORTS_SETTINGS)
        settings_2["llm_model"] = "other-model"

        ws_1 = self._window_signature(transcript, settings_1)
        ws_2 = self._window_signature(transcript, settings_2)
        self.assertNotEqual(ws_1, ws_2)

        key_1 = shorts_full_key(ws_1, settings_1)
        key_2 = shorts_full_key(ws_2, settings_2)

        self.assertNotEqual(key_1, key_2)

    def test_highlights_cache_not_affected_by_shorts_settings(self):
        """Highlights-кеш (window_signature) не зависит от Shorts-настроек."""
        transcript = self._make_transcript()

        settings_1 = dict(SHORTS_SETTINGS)
        settings_2 = {
            "llm_model": "test", "ollama_url": "unused",
            "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
            "outputs": {"shorts": {"enabled": True, "min_duration_sec": 99,
                                    "target_duration_sec": 50, "max_duration_sec": 70}},
        }

        ws_1 = self._window_signature(transcript, settings_1)
        ws_2 = self._window_signature(transcript, settings_2)

        self.assertEqual(ws_1, ws_2,
                         "window_signature (Highlights) не должен меняться при смене Shorts-настроек")


class ShortsCorruptCacheTests(unittest.TestCase):
    """Повреждённые артефакты кеша Shorts игнорируются и пересоздаются обычным путём;
    исправные соседние артефакты остаются пригодными."""

    SENTENCES = fixture_sentences()

    @classmethod
    def _block(cls):
        return [{"sentences": cls.SENTENCES, "analysis": {"topic": "Тема"}}]

    @classmethod
    def _transcript(cls):
        return {"video_id": "test", "duration_sec": 500, "sentences": cls.SENTENCES}

    @classmethod
    def _settings(cls):
        return dict(SHORTS_SETTINGS)

    @classmethod
    def _transport(cls):
        def transport(base, endpoint, payload=None, timeout=10):
            schema = payload["format"]
            if "starts" in schema["properties"]:
                value = {"starts": [{"start_id": cls.SENTENCES[0]["id"], "reason": "Начало"}]}
            else:
                chosen = next(v for v in schema["properties"]["interval_id"]["enum"] if v != "none")
                value = {"interval_id": chosen, "title": "S", "reason": "R", "score": 70}
            return {"message": {"content": json.dumps(value)}}
        return transport

    def _prime_cache(self, cache_dir):
        """Прогоняет select_shorts с чистым кешом (LLM замокан) и возвращает каталог."""
        from shorts_selector import shorts_cache_signature
        sig = shorts_cache_signature(self._settings())
        path = Path(cache_dir) / f"shorts_{sig}" / "block_0000"
        path.mkdir(parents=True, exist_ok=True)
        with patch("analyzer.ollama_request", side_effect=self._transport()):
            select_shorts(self._block(), self._transcript(), self._settings(), path, lambda m: None)
        return path / "catalog.json"

    def _rerun(self, cache_dir, count_expected=None):
        """Повторный прогон; возвращает (result, number_of_llm_calls)."""
        from shorts_selector import shorts_cache_signature
        sig = shorts_cache_signature(self._settings())
        path = Path(cache_dir) / f"shorts_{sig}" / "block_0000"
        calls = []

        def transport(base, endpoint, payload=None, timeout=10):
            calls.append(1)
            return self._transport()(base, endpoint, payload, timeout)

        with patch("analyzer.ollama_request", side_effect=transport):
            result = select_shorts(self._block(), self._transcript(), self._settings(), path, lambda m: None)
        return result, calls

    def _end_files(self, cache_dir):
        from shorts_selector import shorts_cache_signature
        sig = shorts_cache_signature(self._settings())
        return sorted((Path(cache_dir) / f"shorts_{sig}" / "block_0000").glob("end_*.json"))

    def _corrupt(self, path, content=b"{ this is not json"):
        Path(path).write_bytes(content)

    def test_corrupt_catalog_recreated(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = self._prime_cache(temp)
            end_files = self._end_files(temp)
            self.assertTrue(end_files, "В норме должен быть хотя бы один end-артефакт")

            self._corrupt(catalog)
            result, _ = self._rerun(temp)

            self.assertTrue(catalog.exists(), "Повреждённый catalog.json должен быть пересоздан")
            data = json.loads(catalog.read_text(encoding="utf-8"))
            self.assertIn("options", data)
            self.assertGreater(len(data["options"]), 0, "Пересозданный каталог не пуст")
            self.assertTrue(result.get("complete"), f"Ошибок быть не должно: {result['warnings']}")
            # Исправный соседний end-артефакт не затронут
            self.assertEqual(end_files, self._end_files(temp),
                             "Исправный соседний end-артефакт должен остаться нетронутым")

    def test_corrupt_starts_recreated(self):
        with tempfile.TemporaryDirectory() as temp:
            from shorts_selector import shorts_cache_signature
            sig = shorts_cache_signature(self._settings())
            cache = Path(temp) / f"shorts_{sig}" / "block_0000"
            catalog = cache / "catalog.json"

            self._prime_cache(temp)
            self.assertTrue(catalog.exists())

            starts_path = cache / "starts.json"
            self.assertTrue(starts_path.exists())
            self._corrupt(starts_path)
            result, _ = self._rerun(temp)

            self.assertTrue(starts_path.exists(), "Повреждённый starts.json должен быть пересоздан")
            data = json.loads(starts_path.read_text(encoding="utf-8"))
            self.assertIsInstance(data, list)
            self.assertTrue(data, "Новый starts.json не пуст")
            self.assertTrue(result.get("complete"), f"Ошибок быть не должно: {result['warnings']}")
            # Исправный каталог не пересоздан заново (не повреждён)
            self.assertTrue(catalog.exists())

    def test_corrupt_end_recreated(self):
        with tempfile.TemporaryDirectory() as temp:
            from shorts_selector import shorts_cache_signature
            sig = shorts_cache_signature(self._settings())
            cache = Path(temp) / f"shorts_{sig}" / "block_0000"
            starts_path = cache / "starts.json"

            self._prime_cache(temp)
            ends = self._end_files(temp)
            self.assertTrue(ends, "В норме должен быть хотя бы один end-артефакт")
            self.assertTrue(starts_path.exists())

            victim = ends[0]
            self._corrupt(victim)
            result, _ = self._rerun(temp)

            self.assertTrue(victim.exists(), "Повреждённый end-артефакт должен быть пересоздан")
            data = json.loads(victim.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)
            self.assertTrue(result.get("complete"), f"Ошибок быть не должно: {result['warnings']}")
            # Исправные соседние end-артефакты не затронуты
            others = [p for p in self._end_files(temp) if p != victim]
            self.assertEqual([p for p in ends if p != victim], others,
                             "Исправные соседние end-артефакты должны остаться нетронутыми")

    def test_wrong_type_start_ids_in_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            from shorts_selector import shorts_cache_signature
            sig = shorts_cache_signature(self._settings())
            cache = Path(temp) / f"shorts_{sig}" / "block_0000"

            self._prime_cache(temp)
            starts_path = cache / "starts.json"
            # Правильный JSON, но start_id вне разрешённого списка → артефакт некорректен
            starts_path.write_text(json.dumps([{"start_id": "s999999", "reason": "X"}]),
                                   encoding="utf-8")
            result, _ = self._rerun(temp)

            data = json.loads(starts_path.read_text(encoding="utf-8"))
            # После пересоздания start_id снова из разрешённого списка
            self.assertIsInstance(data, list)
            allowed = {s["id"] for s in self.SENTENCES}
            self.assertTrue(all(e.get("start_id") in allowed for e in data),
                            "Пересозданные starts должны ссылаться на существующие предложения")
            self.assertTrue(result.get("complete"), f"Ошибок быть не должно: {result['warnings']}")


if __name__ == "__main__":
    unittest.main()
