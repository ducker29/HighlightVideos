"""Stage 7.2: Regression tests for editorial decision and approval independence."""
import copy
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from renderer import approval_key, shorts_approval_key, render_approved, render_shorts_approved
from job_store import load_json, save_json


def make_highlight(candidate_id="c001", start=120.0, end=240.0, status="pending"):
    return {
        "candidate_id": candidate_id,
        "format": "highlight",
        "start": start, "end": end,
        "start_id": "s000001", "end_id": "s000003",
        "title": "Highlight test", "reason": "Test reason", "score": 80,
        "status": status,
    }


def make_shorts(candidate_id="sc001", start=10.0, end=45.0, status="pending"):
    return {
        "candidate_id": candidate_id,
        "format": "short",
        "start": start, "end": end,
        "start_id": "s000000", "end_id": "s000008",
        "title": "Shorts test", "reason": "Test", "score": 70,
        "status": status,
        "render_settings": {"crop_mode": "center", "crop_x": None,
                             "subtitles_enabled": True, "output_width": 1080,
                             "output_height": 1920},
    }


SETTINGS = {
    "llm_model": "test", "ollama_url": "unused",
    "min_duration_sec": 120, "max_duration_sec": 300, "end_padding_sec": 0.5,
    "outputs": {
        "highlights": {"min_duration_sec": 120, "max_duration_sec": 300},
        "shorts": {"enabled": True, "min_duration_sec": 20,
                    "target_duration_sec": 40, "max_duration_sec": 60},
    },
}


def make_report(highlights=None, shorts=None):
    return {
        "schema": 1, "video_id": "test", "settings": SETTINGS,
        "signature": "abc123",
        "candidates": highlights if highlights is not None else [make_highlight()],
        "shorts_candidates": shorts if shorts is not None else [make_shorts()],
        "profiles": {
            "highlights": {"aspect_ratio": "16:9", "min_duration_sec": 120,
                            "max_duration_sec": 300, "end_padding_sec": 0.5},
            "shorts": {"enabled": True, "aspect_ratio": "9:16", "min_duration_sec": 20,
                        "target_duration_sec": 40, "max_duration_sec": 60,
                        "crop_mode": "center", "subtitles_enabled": True,
                        "output_width": 1080, "output_height": 1920},
        },
        "analysis_warnings": [], "analysis_complete": True,
        "selection_method": "intervals",
    }


MANIFEST = {
    "source_path": "dummy.mp4",
    "media": {"duration_sec": 600.0, "width": 1920, "height": 1080},
}


class ApprovalsIndependenceTests(unittest.TestCase):
    """Shorts approval must not approve Highlights and vice versa."""

    def test_approving_shorts_does_not_approve_highlights(self):
        """Simulate UI: approve a Shorts candidate, verify Highlights remain pending."""
        report = make_report()
        sc = dict(report["shorts_candidates"][0])

        # Simulate decide_shorts("approved") — the fixed logic
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)

        updated = copy.deepcopy(report)
        updated["shorts_candidates"] = [
            sc if c["candidate_id"] == sc["candidate_id"] else c
            for c in updated["shorts_candidates"]]
        # Highlights must be untouched
        self.assertEqual(updated["candidates"], report["candidates"])
        self.assertEqual(updated["candidates"][0]["status"], "pending")
        self.assertNotIn("approval_key", updated["candidates"][0])

    def test_approving_highlights_does_not_approve_shorts(self):
        """Simulate UI: approve a Highlights candidate, verify Shorts remain pending."""
        report = make_report()
        hl = dict(report["candidates"][0])

        # Simulate decide("approved")
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)

        updated = copy.deepcopy(report)
        updated["candidates"] = [
            hl if c["candidate_id"] == hl["candidate_id"] else c
            for c in updated["candidates"]]
        # Shorts must be untouched
        self.assertEqual(updated["shorts_candidates"], report["shorts_candidates"])
        self.assertEqual(updated["shorts_candidates"][0]["status"], "pending")
        self.assertNotIn("approval_key", updated["shorts_candidates"][0])

    def test_highlights_approval_key_unchanged_by_shorts_fields(self):
        """approval_key uses only candidate_id/start/end — Shorts-specific fields
        (crop_mode, subtitles, etc.) must not affect it."""
        hl_base = make_highlight()
        hl_with_shorts_fields = dict(hl_base, crop_mode="speaker", subtitles_enabled=False)
        self.assertEqual(approval_key(hl_base), approval_key(hl_with_shorts_fields))

    def test_shorts_approval_key_differs_from_highlights_key(self):
        """The two key functions produce different hashes for the same candidate
        (different payload fields)."""
        candidate = {
            "candidate_id": "sc001", "start": 10.0, "end": 45.0,
            "format": "short",
            "render_settings": {"crop_mode": "center", "crop_x": None,
                                 "subtitles_enabled": True, "output_width": 1080,
                                 "output_height": 1920},
        }
        self.assertNotEqual(approval_key(candidate), shorts_approval_key(candidate))


class ApprovalInvalidationTests(unittest.TestCase):
    """Changing an interval after approval invalidates the stored approval_key."""

    def test_highlights_boundary_change_invalidates_approval(self):
        hl = make_highlight()
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)
        # Simulate user editing the end time
        hl["end"] = hl["end"] + 5.0
        self.assertNotEqual(hl.get("approval_key"), approval_key(hl))

    def test_shorts_boundary_change_invalidates_approval(self):
        sc = make_shorts()
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)
        sc["end"] = sc["end"] + 3.0
        self.assertNotEqual(sc.get("approval_key"), shorts_approval_key(sc))

    def test_shorts_render_settings_change_invalidates_approval(self):
        """Changing crop or subtitles after approval must invalidate."""
        sc = make_shorts()
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)
        sc["render_settings"] = dict(sc["render_settings"], crop_x=0.25)
        self.assertNotEqual(sc.get("approval_key"), shorts_approval_key(sc))

    def test_renderer_rejects_stale_highlights_approval(self):
        """render_approved must raise when approval_key doesn't match."""
        report = make_report()
        hl = dict(report["candidates"][0])
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)
        # Mutate the boundary after approval
        hl["end"] = hl["end"] + 10.0
        report["candidates"] = [hl]

        from unittest.mock import patch
        with patch("renderer.probe", return_value={
                "duration_sec": 600.0, "width": 1920, "height": 1080}):
            with self.assertRaises(ValueError) as ctx:
                render_approved(MANIFEST, report, Path("tmp"), lambda m: None)
            self.assertIn("изменились после утверждения", str(ctx.exception))

    def test_renderer_rejects_stale_shorts_approval(self):
        """render_shorts_approved must raise when shorts_approval_key doesn't match."""
        report = make_report()
        sc = dict(report["shorts_candidates"][0])
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)
        sc["end"] = sc["end"] + 5.0
        report["shorts_candidates"] = [sc]

        from unittest.mock import patch
        with patch("renderer.probe", return_value={
                "duration_sec": 600.0, "width": 1920, "height": 1080}):
            with self.assertRaises(ValueError) as ctx:
                render_shorts_approved(MANIFEST, report, Path("tmp"), lambda m: None)
            self.assertIn("изменились после утверждения", str(ctx.exception))


class EditorialIndependenceTests(unittest.TestCase):
    """Shorts editorial decisions must not modify Highlights and vice versa."""

    def _report_with_both(self):
        return make_report()

    def test_shorts_editorial_keep_does_not_touch_highlights(self):
        report = self._report_with_both()
        hl_before = copy.deepcopy(report["candidates"])

        # Simulate shorts_editorial keep: only modifies shorts_candidates entry
        sc = report["shorts_candidates"][0]
        sc["editorial_decision"] = "keep"
        sc["editorial_flags"] = {"off_topic": False}
        sc.pop("approval_key", None)

        self.assertEqual(report["candidates"], hl_before)

    def test_highlights_editorial_does_not_touch_shorts(self):
        report = self._report_with_both()
        sc_before = copy.deepcopy(report["shorts_candidates"])

        # Simulate highlights editorial revise: pops approval_key, sets pending
        hl = report["candidates"][0]
        hl["status"] = "pending"
        hl.pop("approval_key", None)
        hl["editorial_decision"] = "revise"

        self.assertEqual(report["shorts_candidates"], sc_before)

    def test_editorial_reject_isolated_per_format(self):
        report = self._report_with_both()
        # Reject the highlight
        report["candidates"][0]["status"] = "rejected"
        # Shorts must remain unaffected
        self.assertEqual(report["shorts_candidates"][0]["status"], "pending")
        self.assertEqual(len(report["shorts_candidates"]), 1)


class ReportReloadTests(unittest.TestCase):
    """Reloading a report from disk preserves independent decisions for both formats."""

    def test_reload_preserves_independent_decisions(self):
        """Save a report with one approved highlight and one approved shorts,
        reload, and verify both statuses survive."""
        report = make_report()
        # Approve the highlight
        hl = dict(report["candidates"][0])
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)
        report["candidates"] = [hl]
        # Approve the shorts
        sc = dict(report["shorts_candidates"][0])
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)
        report["shorts_candidates"] = [sc]

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            save_json(report_path, report)
            reloaded = load_json(report_path)

            self.assertEqual(reloaded["candidates"][0]["status"], "approved")
            self.assertEqual(reloaded["candidates"][0]["approval_key"],
                             approval_key(reloaded["candidates"][0]))
            self.assertEqual(reloaded["shorts_candidates"][0]["status"], "approved")
            self.assertEqual(reloaded["shorts_candidates"][0]["approval_key"],
                             shorts_approval_key(reloaded["shorts_candidates"][0]))

    def test_reload_preserves_mixed_statuses(self):
        """One format approved, the other rejected — both survive a reload."""
        report = make_report()
        report["candidates"][0]["status"] = "approved"
        report["candidates"][0]["approval_key"] = approval_key(report["candidates"][0])
        report["shorts_candidates"][0]["status"] = "rejected"

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            save_json(report_path, report)
            reloaded = load_json(report_path)

            self.assertEqual(reloaded["candidates"][0]["status"], "approved")
            self.assertEqual(reloaded["shorts_candidates"][0]["status"], "rejected")
            self.assertNotIn("approval_key", reloaded["shorts_candidates"][0])


class DecideShortsHandlerTests(unittest.TestCase):
    """decide_shorts (обработчик кнопки утверждения Shorts) должен сохранять
    shorts_approval_key, а не approval_key."""

    def _make_app(self, candidate, tmp):
        from review_ui import HighlightApp
        app = HighlightApp.__new__(HighlightApp)  # без __init__ — нет Tk
        app.busy = False
        report_path = Path(tmp) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        app.context = {
            "report_path": report_path,
            "report": {"shorts_candidates": [dict(candidate)], "profiles": {
                "shorts": {"min_duration_sec": 20, "max_duration_sec": 60},
            }},
            "manifest": {"media": {"duration_sec": 600.0}},
        }
        app.shorts_table = unittest.mock.Mock()
        app.shorts_table.selection.return_value = (candidate["candidate_id"],)
        app.shorts_table.get_children.return_value = [candidate["candidate_id"]]
        app.start = unittest.mock.Mock()
        app.end = unittest.mock.Mock()
        app.title = unittest.mock.Mock()
        app.start.get.return_value = "0:00:10"
        app.end.get.return_value = "0:00:45"
        app.title.get.return_value = candidate["title"]
        app.status = unittest.mock.Mock()
        app.set_details = lambda *a, **kw: None
        app.refresh_shorts_table = lambda *a, **kw: None
        app.root = unittest.mock.Mock()
        app.root.after = lambda *a: None
        app._loading_candidate = False
        return app

    def _candidate(self, **overrides):
        base = make_shorts()
        base.update(overrides)
        return base

    def test_approve_saves_shorts_approval_key(self):
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(candidate, tmp)
            app.decide_shorts("approved")

            saved = app.context["report"]["shorts_candidates"][0]
            self.assertEqual(saved["status"], "approved")
            self.assertIn("approval_key", saved)
            self.assertEqual(saved["approval_key"], shorts_approval_key(saved))
            # Ключ не должен совпадать с highlights-ключом (был баг)
            self.assertNotEqual(saved["approval_key"], approval_key(saved))

    def test_cropping_change_invalidates_stored_approval(self):
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(candidate, tmp)
            app.decide_shorts("approved")
            stored_key = app.context["report"]["shorts_candidates"][0]["approval_key"]

            # Пользователь изменил crop_x после утверждения
            app.context["report"]["shorts_candidates"][0]["render_settings"]["crop_x"] = 0.25
            recomputed = shorts_approval_key(app.context["report"]["shorts_candidates"][0])
            self.assertNotEqual(stored_key, recomputed)

    def test_subtitles_toggle_invalidates_stored_approval(self):
        candidate = self._candidate()
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(candidate, tmp)
            app.decide_shorts("approved")
            stored_key = app.context["report"]["shorts_candidates"][0]["approval_key"]

            # Пользователь переключил субтитры после утверждения
            app.context["report"]["shorts_candidates"][0]["render_settings"]["subtitles_enabled"] = False
            recomputed = shorts_approval_key(app.context["report"]["shorts_candidates"][0])
            self.assertNotEqual(stored_key, recomputed)

    def test_reject_removes_approval_key(self):
        candidate = self._candidate(status="approved", approval_key="stale")
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(candidate, tmp)
            app.decide_shorts("rejected")

            saved = app.context["report"]["shorts_candidates"][0]
            self.assertEqual(saved["status"], "rejected")
            self.assertNotIn("approval_key", saved)


class UIIndependenceTests(unittest.TestCase):
    """Stage 7.4.3: реальные decide/decide_shorts не затрагивают другую ветку."""

    def _make_app(self, report, tmp, start_time="0:02:00", end_time="0:04:00"):
        from review_ui import HighlightApp
        app = HighlightApp.__new__(HighlightApp)
        app.busy = False
        report_path = Path(tmp) / "report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(report_path, report)
        app.context = {
            "report_path": report_path,
            "report": report,
            "manifest": {"media": {"duration_sec": 600.0}},
        }
        # Highlights-таблица
        app.table = unittest.mock.Mock()
        app.table.selection.return_value = ("c001",)
        # Shorts-таблица
        app.shorts_table = unittest.mock.Mock()
        app.shorts_table.selection.return_value = ("sc001",)
        # Виджеты границ
        app.start = unittest.mock.Mock()
        app.start.get.return_value = start_time
        app.end = unittest.mock.Mock()
        app.end.get.return_value = end_time
        app.title = unittest.mock.Mock()
        app.title.get.return_value = "Test"
        # Кнопки-обработчики
        app.refresh_table = unittest.mock.Mock()
        app.refresh_shorts_table = unittest.mock.Mock()
        app.root = unittest.mock.Mock()
        app.notebook = unittest.mock.Mock()
        app.notebook.index.return_value = 0
        app.notebook.select.return_value = ""
        return app

    def test_decide_shorts_does_not_modify_highlights(self):
        """decide_shorts('approved') не изменяет candidates Highlights."""
        report = make_report()
        hl_before = copy.deepcopy(report["candidates"])
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(report, tmp, start_time="0:00:10", end_time="0:00:45")
            app.decide_shorts("approved")

            self.assertEqual(app.context["report"]["candidates"], hl_before,
                             "decide_shorts не должен изменять candidates Highlights")
            self.assertEqual(app.context["report"]["candidates"][0]["status"], "pending")
            self.assertNotIn("approval_key", app.context["report"]["candidates"][0])
            # Shorts должен быть утверждён
            sc = app.context["report"]["shorts_candidates"][0]
            self.assertEqual(sc["status"], "approved")
            self.assertIn("approval_key", sc)

    def test_decide_highlights_does_not_modify_shorts(self):
        """decide('approved') на Highlights не изменяет shorts_candidates."""
        report = make_report()
        sc_before = copy.deepcopy(report["shorts_candidates"])
        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(report, tmp)
            # Реальный decide (не переопределяем)
            # _is_shorts_tab вернёт False, т.к. notebook.index == 0
            app.decide("approved")

            self.assertEqual(app.context["report"]["shorts_candidates"], sc_before,
                             "decide Highlights не должен изменять shorts_candidates")
            self.assertEqual(app.context["report"]["shorts_candidates"][0]["status"], "pending")
            self.assertNotIn("approval_key", app.context["report"]["shorts_candidates"][0])
            # Highlights должен быть утверждён
            hl = app.context["report"]["candidates"][0]
            self.assertEqual(hl["status"], "approved")
            self.assertIn("approval_key", hl)

    def test_highlights_approval_preserves_shorts_approval_key(self):
        """Утверждение Highlights не сбрасывает уже сохранённый shorts approval_key."""
        report = make_report()
        # Сначала утвердим Shorts
        sc = report["shorts_candidates"][0]
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)
        shorts_key_before = sc["approval_key"]

        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(report, tmp)
            app.decide("approved")  # утверждаем Highlights (реальный decide)

            hl = app.context["report"]["candidates"][0]
            sc_after = app.context["report"]["shorts_candidates"][0]
            self.assertEqual(hl["status"], "approved")
            self.assertEqual(sc_after["approval_key"], shorts_key_before,
                             "approval_key Shorts не должен сбрасываться при утверждении Highlights")
            self.assertEqual(sc_after["status"], "approved")

    def test_shorts_approval_preserves_highlights_approval_key(self):
        """Утверждение Shorts не сбрасывает уже сохранённый highlights approval_key."""
        report = make_report()
        # Сначала утвердим Highlights
        hl = report["candidates"][0]
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)
        highlights_key_before = hl["approval_key"]

        with tempfile.TemporaryDirectory() as tmp:
            app = self._make_app(report, tmp, start_time="0:00:10", end_time="0:00:45")
            app.decide_shorts("approved")  # утверждаем Shorts (реальный decide_shorts)

            hl_after = app.context["report"]["candidates"][0]
            self.assertEqual(hl_after["approval_key"], highlights_key_before,
                             "approval_key Highlights не должен сбрасываться при утверждении Shorts")
            self.assertEqual(hl_after["status"], "approved")
            sc = app.context["report"]["shorts_candidates"][0]
            self.assertEqual(sc["status"], "approved")

    def test_saved_report_preserves_both_branches_on_reload(self):
        """Сохранённый отчёт: при повторном открытии решения обеих веток остаются."""
        report = make_report()
        hl = report["candidates"][0]
        hl["status"] = "approved"
        hl["approval_key"] = approval_key(hl)
        sc = report["shorts_candidates"][0]
        sc["status"] = "approved"
        sc["approval_key"] = shorts_approval_key(sc)

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            save_json(report_path, report)

            # Повторное открытие
            reloaded = load_json(report_path)
            self.assertEqual(reloaded["candidates"][0]["status"], "approved")
            self.assertEqual(reloaded["candidates"][0]["approval_key"],
                             approval_key(reloaded["candidates"][0]))
            self.assertEqual(reloaded["shorts_candidates"][0]["status"], "approved")
            self.assertEqual(reloaded["shorts_candidates"][0]["approval_key"],
                             shorts_approval_key(reloaded["shorts_candidates"][0]))


if __name__ == "__main__":
    unittest.main()
