"""Application service: analysis never triggers final rendering."""
from pathlib import Path

from analyzer import analyze, list_models, ollama_request
from downloader import download
from job_store import load_json, save_json
from transcriber import check_runtime, transcribe
from editorial_review import review_report


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
    # Fail before a large download if the selected local LLM is unavailable.
    models = list_models(settings["ollama_url"])
    required = {settings["llm_model"], settings.get("editorial_model") or settings["llm_model"]}
    missing = required.difference(models)
    if missing:
        raise ValueError("Не установлены LLM: " + ", ".join(sorted(missing)) + ". Обновите список моделей Ollama.")
    log("Проверка окружения Whisper…")
    check_runtime(settings)
    job_dir, manifest = download(settings["youtube_url"], log)
    state = job_dir / "state.json"
    try:
        save_json(state, {"stage": "transcribing", "settings": settings})
        release_llm_for_asr(settings, log)
        transcript = transcribe(job_dir, manifest, settings, log)
        save_json(state, {"stage": "analyzing", "settings": settings})
        report_path = analyze(job_dir, transcript, settings, log)
        save_json(state, {"stage": "editorial_review", "settings": settings})
        report_path = review_report(report_path, log, model=settings.get("editorial_model"))
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
