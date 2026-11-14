"""Application service: analysis never triggers final rendering."""
from pathlib import Path

from analyzer import analyze, list_models, ollama_request
from downloader import download
from local_source import local_source_path, prepare_local_source
from job_store import analysis_bounds, load_json, save_json, timecode
from transcriber import check_runtime, transcribe
from editorial_review import review_report
from clip_padding import padding_seconds, prepare_review_report
from job_store import migrate_report


def release_llm_for_asr(settings, log):
    """Avoid running our selected LLM and large Whisper concurrently on the GPU."""
    if settings["device"] != "cuda":
        return
    resident = ollama_request(settings["ollama_url"], "/api/ps", timeout=5).get("models", [])
    if any(model.get("name") == settings["llm_model"] for model in resident):
        log("Освобождение VRAM выбранной LLM перед Whisper…")
        ollama_request(settings["ollama_url"], "/api/generate",
                       {"model": settings["llm_model"], "keep_alive": 0, "stream": False}, timeout=30)


def run_analysis(settings, log):
    if not 1 <= settings["min_duration_sec"] <= settings["max_duration_sec"] <= 480:
        raise ValueError("Нужна длительность: 1 ≤ минимум ≤ максимум ≤ 480 секунд.")
    analysis_bounds(settings)
    padding_seconds(settings.get("end_padding_sec", 0.5))
    local_path = (settings.get("local_video_path") or "").strip()
    if local_path:
        local_path = str(local_source_path(local_path))
    # Fail before a large download if the selected local LLM is unavailable.
    models = list_models(settings["ollama_url"])
    required = {settings["llm_model"], settings.get("editorial_model") or settings["llm_model"]}
    missing = required.difference(models)
    if missing:
        raise ValueError("Не установлены LLM: " + ", ".join(sorted(missing)) + ". Обновите список моделей Ollama.")
    log("Проверка окружения Whisper…")
    check_runtime(settings)
    job_dir, manifest = (prepare_local_source(local_path, log) if local_path
                         else download(settings["youtube_url"], log))
    state = job_dir / "state.json"
    try:
        start_sec, end_sec = analysis_bounds(settings, manifest["media"]["duration_sec"])
        log("Анализ полного видео." if end_sec is None else
            f"Диапазон анализа: {timecode(start_sec)}–{timecode(end_sec)} исходного видео.")
        save_json(state, {"stage": "transcribing", "settings": settings})
        release_llm_for_asr(settings, log)
        transcript = transcribe(job_dir, manifest, settings, log, start_sec=start_sec, end_sec=end_sec)
        save_json(state, {"stage": "analyzing", "settings": settings})
        report_path = analyze(job_dir, transcript, settings, log)
        save_json(state, {"stage": "editorial_review", "settings": settings})
        report_path = review_report(report_path, log, model=settings.get("editorial_model"))
        save_json(state, {"stage": "end_padding", "settings": settings})
        report_path = prepare_review_report(report_path, settings.get("end_padding_sec", 0.5), log)
        save_json(state, {"stage": "awaiting_review", "report": str(report_path)})
    except Exception as exc:
        save_json(state, {"stage": "failed", "error": str(exc), "settings": settings})
        raise
    return open_report(report_path)


def open_report(path):
    path = Path(path).resolve()
    report = load_json(path)
    if report.get("schema") != 1 or "candidates" not in report:
        raise ValueError("Это не отчёт кандидатов Highlight Pipeline.")
    # Старым отчётам дополняем профили и поле формата, не трогая утверждения и границы.
    migrate_report(report)
    # Reports live at data/<video_id>/analysis/<signature>/report.json.
    job_dir = path.parent.parent.parent
    manifest = load_json(job_dir / "source.json")
    if manifest["video_id"] != report["video_id"]:
        raise ValueError("Отчёт относится к другому видео.")
    transcript = load_json(path.parent / "transcript.json")
    notes_path = job_dir / "quality_notes.json"
    quality_notes = load_json(notes_path) if notes_path.exists() else {}
    return {"job_dir": job_dir, "manifest": manifest, "report": report,
            "report_path": path, "transcript": transcript, "quality_notes": quality_notes}


def recalc_shorts_for_report(context, settings, log):
    """Recalculate Shorts only; reuse the transcript already tied to this report."""
    from analyzer import recompute_shorts
    report_path = recompute_shorts(context["job_dir"], context["transcript"],
                                   settings, context["report_path"], log)
    return open_report(report_path)
