"""Word-level ASR plus stable, timestamp-backed sentence identifiers."""
import gc
import re
import subprocess

from job_store import DATA, load_json, save_json
from gpu_runtime import check_cuda_libraries, prepare_cuda


def check_runtime(settings):
    """Fail before downloading hours of video when the selected ASR cannot start."""
    if settings["device"] == "cuda":
        prepare_cuda()
    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Установите зависимости: .venv\\Scripts\\python.exe -m pip install -r requirements.txt") from exc
    if settings["device"] == "cuda":
        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("CUDA-устройство не найдено. Выберите cpu в интерфейсе.")
        check_cuda_libraries()


def build_sentences(words):
    sentences, group = [], []

    def flush():
        if group:
            sentences.append({"id": f"s{len(sentences):06d}", "start": group[0]["start"],
                              "end": group[-1]["end"], "text": " ".join(w["word"].strip() for w in group),
                              "boundary": "punctuation" if re.search(r'[.!?…][»"\)]*$', group[-1]["word"].strip()) else "pause_or_limit"})
            group.clear()

    for word in words:
        if group and word["start"] - group[-1]["end"] >= 1.5:
            flush()
        group.append(word)
        if re.search(r'[.!?…][»"\)]*$', word["word"].strip()) or len(group) >= 120:
            flush()
    flush()
    return sentences


def transcribe(job_dir, manifest, settings, log, force=False, start_sec=0, end_sec=None):
    source_duration = manifest["media"]["duration_sec"]
    range_end = source_duration if end_sec is None else float(end_sec)
    start_sec = float(start_sec)
    if not 0 <= start_sec < range_end <= source_duration:
        raise ValueError("Диапазон ASR должен находиться внутри исходного видео.")
    path = job_dir / "transcript.json"
    signature = {k: settings[k] for k in ("asr_model", "language", "device")}
    signature.update(schema=2, start_sec=start_sec, end_sec=range_end)
    if path.exists() and not force:
        cached = load_json(path)
        if cached.get("settings") == signature:
            log("Используется сохранённый транскрипт.")
            return cached
    check_runtime(settings)
    from faster_whisper import WhisperModel

    audio = job_dir / ("audio.wav" if start_sec == 0 and end_sec is None else
                       f"audio_{round(start_sec * 1000)}_{round(range_end * 1000)}.wav")
    if not audio.exists():
        log("Извлечение моно-аудио 16 kHz…")
        temporary = audio.with_suffix(".partial.wav")
        result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start_sec),
                                 "-i", manifest["source_path"], "-t", str(range_end - start_sec),
                                 "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(temporary)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(result.stderr[-4000:])
        temporary.replace(audio)
    device = settings["device"]
    log(f"Загрузка Whisper {settings['asr_model']} ({device}). При первом запуске загружаются веса модели…")
    model = WhisperModel(settings["asr_model"], device=device,
                         compute_type="float16" if device == "cuda" else "int8",
                         download_root=str(DATA / "models" / "whisper"))
    segments, info = model.transcribe(str(audio), language=settings["language"] or None,
                                     word_timestamps=True, vad_filter=True)
    rows, words = [], []
    last_minute = -1
    try:
        for segment in segments:
            segment_words = [{"word": w.word, "start": w.start + start_sec, "end": w.end + start_sec} for w in (segment.words or [])]
            words.extend(segment_words)
            rows.append({"id": segment.id, "start": segment.start + start_sec, "end": segment.end + start_sec,
                         "text": segment.text, "words": segment_words})
            minute = int(segment.end // 60)
            if minute != last_minute:
                log(f"Транскрипция: {minute} / {int((range_end - start_sec) // 60)} мин диапазона")
                last_minute = minute
    finally:
        del model
        gc.collect()
    if not words:
        raise ValueError("В записи не распознана речь. Смысловой анализ не запущен.")
    transcript = {"video_id": manifest["video_id"], "duration_sec": manifest["media"]["duration_sec"],
                  "analysis_range": {"start": start_sec, "end": range_end},
                  "language": info.language, "settings": signature, "segments": rows,
                  "sentences": build_sentences(words)}
    save_json(path, transcript)
    return transcript
