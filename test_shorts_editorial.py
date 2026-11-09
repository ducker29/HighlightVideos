"""Tests for independent Shorts editorial review."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from shorts_editorial import review_shorts_candidates, SHORTS_SCHEMA


def make_transcript(count=30, seconds=5):
    sentences = [{"id": f"s{i:06d}", "start": i * seconds, "end": (i + 1) * seconds,
                  "text": f"Предложение {i}."} for i in range(count)]
    return {"video_id": "test", "duration_sec": count * seconds, "sentences": sentences}


SHORTS_CANDIDATE = {
    "candidate_id": "sc001",
    "format": "shorts",
    "start": 10.0, "end": 50.0,
    "start_id": "s000002", "end_id": "s000009",
    "title": "Тестовый Short", "score": 75,
    "status": "pending",
    "block_id": 0,
}

SETTINGS = {
    "llm_model": "test", "ollama_url": "unused",
    "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
    "outputs": {"shorts": {"enabled": True, "min_duration_sec": 20,
                            "target_duration_sec": 40, "max_duration_sec": 60}},
}


class ShortsEditorialTests(unittest.TestCase):

    def _make_report(self, tmp, transcript, candidates=None):
        report = {
            "schema": 1, "video_id": "test", "settings": SETTINGS,
            "signature": "abc123", "candidates": [],
            "shorts_candidates": candidates or [dict(SHORTS_CANDIDATE)],
        }
        job_dir = Path(tmp) / "job"
        job_dir.mkdir(parents=True, exist_ok=True)
        report_path = job_dir / "report.json"
        report_path.write_text(json.dumps(report))
        (job_dir / "transcript.json").write_text(json.dumps(transcript))
        # Clear any existing editorial cache
        cache_dir = job_dir / "analysis" / f"shorts_abc123_editorial_v1"
        if cache_dir.exists():
            import shutil
            shutil.rmtree(cache_dir)
        return report_path

    def test_keep_decision_preserves_candidate(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)
            decision = {
                "decision": "keep", "start_id": "s000002", "end_id": "s000009",
                "title": "Хороший Short", "reason": "Все критерии выполнены",
                "strong_opening": True, "self_contained": True,
                "complete_ending": True, "single_topic": True,
                "no_trailing_pause": True, "not_a_fragment": True,
                "score": 85
            }

            with patch("shorts_editorial.ask", return_value=decision):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(Path(result_path).read_text())
            self.assertEqual(len(report["shorts_candidates"]), 1)
            c = report["shorts_candidates"][0]
            self.assertEqual(c["editorial_decision"], "keep")
            self.assertEqual(c["score"], 85)
            self.assertEqual(c["status"], "pending")

    def test_reject_removes_candidate(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)
            decision = {
                "decision": "reject", "start_id": "s000002", "end_id": "s000009",
                "title": "Слабый Short", "reason": "Нет самостоятельности",
                "strong_opening": False, "self_contained": False,
                "complete_ending": True, "single_topic": True,
                "no_trailing_pause": True, "not_a_fragment": True,
                "score": 30
            }

            with patch("shorts_editorial.ask", return_value=decision):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(Path(result_path).read_text())
            self.assertEqual(len(report["shorts_candidates"]), 0)
            self.assertEqual(len(report["shorts_editorial_rejections"]), 1)

    def test_revise_updates_bounds(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)
            decision = {
                "decision": "revise", "start_id": "s000003", "end_id": "s000010",
                "title": "Исправленный Short", "reason": "Сдвинул границы",
                "strong_opening": True, "self_contained": True,
                "complete_ending": True, "single_topic": True,
                "no_trailing_pause": True, "not_a_fragment": True,
                "score": 80
            }

            with patch("shorts_editorial.ask", return_value=decision):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(Path(result_path).read_text(encoding="utf-8"))
            c = report["shorts_candidates"][0]
            self.assertEqual(c["start_id"], "s000003")
            self.assertEqual(c["end_id"], "s000010")
            self.assertIn("Границы скорректированы", c.get("warnings", [""])[0] if c.get("warnings") else "")

    def test_invalid_json_retried_then_needs_review(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)

            with patch("shorts_editorial.ask", side_effect=Exception("Invalid JSON")):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(Path(result_path).read_text())
            c = report["shorts_candidates"][0]
            self.assertEqual(c.get("editorial_decision"), "needs_review")

    def test_already_approved_not_modified(self):
        transcript = make_transcript()
        candidate = dict(SHORTS_CANDIDATE, status="approved", approval_key="abc")
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript, [candidate])

            def transport(base, endpoint, payload=None, timeout=10):
                self.fail("LLM should not be called for approved candidates")

            with patch("analyzer.ollama_request", side_effect=transport):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(Path(result_path).read_text())
            c = report["shorts_candidates"][0]
            self.assertEqual(c["status"], "approved")
            self.assertEqual(c.get("editorial_decision"), None)

    def test_shorts_disabled_returns_early(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)
            report = json.loads(report_path.read_text())
            report["settings"]["outputs"]["shorts"]["enabled"] = False
            report_path.write_text(json.dumps(report))

            def transport(base, endpoint, payload=None, timeout=10):
                self.fail("LLM should not be called when shorts disabled")

            with patch("analyzer.ollama_request", side_effect=transport):
                result_path = review_shorts_candidates(report_path, lambda m: None)
            self.assertEqual(str(result_path), str(report_path))

    def test_independent_from_highlights(self):
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript)
            report = json.loads(report_path.read_text())
            report["candidates"] = [
                {"candidate_id": "c001", "start": 100.0, "end": 200.0, "status": "approved",
                 "approval_key": "xyz", "format": "highlight"}
            ]
            report_path.write_text(json.dumps(report))
            decision = {
                "decision": "keep", "start_id": "s000002", "end_id": "s000009",
                "title": "Short", "reason": "OK",
                "strong_opening": True, "self_contained": True,
                "complete_ending": True, "single_topic": True,
                "no_trailing_pause": True, "not_a_fragment": True,
                "score": 80
            }

            with patch("shorts_editorial.ask", return_value=decision):
                review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(report_path.read_text())
            highlight = report["candidates"][0]
            shorts = report["shorts_candidates"][0]
            self.assertEqual(highlight["status"], "approved")
            self.assertEqual(highlight["approval_key"], "xyz")
            self.assertNotEqual(shorts.get("approval_key"), highlight.get("approval_key"))

    def test_deduplication_by_iou(self):
        transcript = make_transcript()
        candidates = [
            dict(SHORTS_CANDIDATE, candidate_id="sc001", start=10.0, end=50.0),
            dict(SHORTS_CANDIDATE, candidate_id="sc002", start=12.0, end=52.0, score=70),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._make_report(tmp, transcript, candidates)
            decision = {
                "decision": "keep", "start_id": "s000002", "end_id": "s000009",
                "title": "T", "reason": "R",
                "strong_opening": True, "self_contained": True,
                "complete_ending": True, "single_topic": True,
                "no_trailing_pause": True, "not_a_fragment": True,
                "score": 80
            }

            with patch("shorts_editorial.ask", return_value=decision):
                review_shorts_candidates(report_path, lambda m: None)
            report = json.loads(report_path.read_text())
            self.assertLessEqual(len(report["shorts_candidates"]), 1)
            self.assertGreaterEqual(len(report["shorts_editorial_rejections"]), 1)


if __name__ == "__main__":
    unittest.main()
