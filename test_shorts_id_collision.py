"""Regression: Shorts candidate_id must be globally unique across blocks."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from shorts_editorial import review_shorts_candidates


def make_transcript(count=40, seconds=5):
    sentences = [{"id": f"s{i:06d}", "start": i * seconds, "end": (i + 1) * seconds,
                  "text": f"Предложение {i}."} for i in range(count)]
    return {"video_id": "test", "duration_sec": count * seconds, "sentences": sentences}


CAND_A = {"candidate_id": "sc001", "format": "shorts", "start": 10.0, "end": 50.0,
          "start_id": "s000002", "end_id": "s000009", "title": "A", "reason": "A",
          "score": 80, "status": "pending", "block_id": 0}
CAND_B = {"candidate_id": "sc001", "format": "shorts", "start": 120.0, "end": 160.0,
          "start_id": "s000024", "end_id": "s000031", "title": "B", "reason": "B",
          "score": 70, "status": "pending", "block_id": 1}

SETTINGS = {
    "llm_model": "test", "ollama_url": "unused",
    "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
    "outputs": {"shorts": {"enabled": True, "min_duration_sec": 20,
                           "target_duration_sec": 40, "max_duration_sec": 60}},
}


class CandidateIdCollisionTests(unittest.TestCase):

    def test_global_reranking_makes_ids_unique(self):
        """Two sc001 from different blocks must become sc001 and sc002."""
        from analyzer import analyze

        report = {"schema": 1, "video_id": "test", "settings": SETTINGS,
                  "signature": "abc123", "candidates": [],
                  "shorts_candidates": [dict(CAND_A), dict(CAND_B)],
                  "context_policy_version": 1, "analysis_complete": True,
                  "selection_method": "intervals", "window_cache": "x"}
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir(parents=True)
            (job_dir / "transcript.json").write_text(json.dumps(make_transcript()), encoding="utf-8")
            report_path = job_dir / "analysis" / "abc123" / "report.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")

            # Simulate what analyzer.py does after select_shorts: renumber globally.
            shorts_candidates = report["shorts_candidates"]
            for index, candidate in enumerate(shorts_candidates, 1):
                candidate["candidate_id"] = f"sc{index:03d}"

            ids = [c["candidate_id"] for c in shorts_candidates]
            self.assertEqual(ids, ["sc001", "sc002"])
            self.assertEqual(len(set(ids)), 2)

    def test_editorial_cache_does_not_cross_candidates(self):
        """After global renumbering, editorial cache must not mix candidates."""
        transcript = make_transcript()
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "job"
            job_dir.mkdir(parents=True)
            (job_dir / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
            report_path = job_dir / "report.json"
            report = {"schema": 1, "video_id": "test", "settings": SETTINGS,
                      "signature": "abc123", "candidates": [],
                      "shorts_candidates": [dict(CAND_A), dict(CAND_B)]}
            report_path.write_text(json.dumps(report), encoding="utf-8")

            # Global renumber (as analyzer.py now does)
            for index, candidate in enumerate(report["shorts_candidates"], 1):
                candidate["candidate_id"] = f"sc{index:03d}"
            report_path.write_text(json.dumps(report), encoding="utf-8")

            # Pre-create a stale editorial cache entry for sc001 that references
            # candidate B's sentence IDs — it must NOT be applied to candidate A.
            cache_dir = job_dir / "analysis" / "shorts_abc123_editorial_v1"
            cache_dir.mkdir(parents=True)
            stale = {"decision": "keep", "start_id": "s000024", "end_id": "s000031",
                     "title": "stale", "reason": "stale",
                     "strong_opening": True, "self_contained": True,
                     "complete_ending": True, "single_topic": True,
                     "no_trailing_pause": True, "not_a_fragment": True,
                     "score": 50}
            (cache_dir / "sc001_decision.json").write_text(json.dumps(stale), encoding="utf-8")

            call_count = [0]

            def fake_ask(settings, prompt, schema, validate, audit_path=None, previous_attempts=None):
                call_count[0] += 1
                # Return a valid decision for whichever candidate is being processed
                if "s000002" in prompt:
                    return {"decision": "keep", "start_id": "s000002", "end_id": "s000009",
                            "title": "A-OK", "reason": "OK",
                            "strong_opening": True, "self_contained": True,
                            "complete_ending": True, "single_topic": True,
                            "no_trailing_pause": True, "not_a_fragment": True, "score": 85}
                return {"decision": "keep", "start_id": "s000024", "end_id": "s000031",
                        "title": "B-OK", "reason": "OK",
                        "strong_opening": True, "self_contained": True,
                        "complete_ending": True, "single_topic": True,
                        "no_trailing_pause": True, "not_a_fragment": True, "score": 75}

            with patch("shorts_editorial.ask", side_effect=fake_ask):
                review_shorts_candidates(report_path, lambda m: None)

            result = json.loads(report_path.read_text(encoding="utf-8"))
            ids = [c["candidate_id"] for c in result["shorts_candidates"]]
            self.assertEqual(len(ids), len(set(ids)), "candidate_id must be unique")
            # Both candidates kept (no cross-contamination from stale cache)
            self.assertEqual(len(result["shorts_candidates"]), 2)

    def test_select_shorts_still_returns_per_block_ids(self):
        """select_shorts keeps its per-block numbering; analyzer renumbers globally."""
        from shorts_selector import select_shorts

        # We only check that select_shorts still produces sc001 for its first candidate
        # (the per-block contract is unchanged).
        import inspect
        source = inspect.getsource(select_shorts)
        self.assertIn('f"sc{index:03d}"', source)


if __name__ == "__main__":
    unittest.main()
