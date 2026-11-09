"""Render approved review intervals: 16:9 highlights and 9:16 Shorts."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from downloader import probe
from job_store import load_json, save_json, validate_interval
from transcript_review import review_sections, transcript_chunks


def approval_key(candidate):
    return hashlib.sha256(json.dumps({k: candidate[k] for k in ("candidate_id", "start", "end")},
                                     sort_keys=True).encode()).hexdigest()


def shorts_approval_key(candidate):
    """Отдельная сигнатура утверждения Shorts: учитывает всё, что меняет итоговый MP4.

    Изменение любого полей ниже снимает утверждение Short. Заголовок намеренно не входит.
    """
    render = candidate.get("render_settings") or {}
    payload = {
        "candidate_id": candidate["candidate_id"],
        "start": candidate["start"],
        "end": candidate["end"],
        "format": candidate.get("format", "short"),
        "crop_mode": render.get("crop_mode", "center"),
        "crop_x": render.get("crop_x"),
        "subtitles_enabled": render.get("subtitles_enabled", True),
        "output_width": render.get("output_width", 1080),
        "output_height": render.get("output_height", 1920),
        "end_padding": candidate.get("end_padding"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _filename_part(value, fallback):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value or ""))
    return re.sub(r"\s+", " ", value).strip(" .") or fallback


def _shorten(value, units):
    # Windows filename/path limits count UTF-16 units, not Python characters.
    return value.encode("utf-16-le")[:units * 2].decode("utf-16-le", errors="ignore").rstrip(" .")


def output_filename(candidate, output_dir=None):
    """Readable ID_title.mp4; optional directory resolves collisions without overwriting."""
    identifier = _shorten(_filename_part(candidate["candidate_id"], "clip"), 40)
    title = _filename_part(candidate.get("title"), "Без названия")
    stem_limit = 120
    if output_dir is not None:
        output_dir = Path(output_dir)
        directory_units = len(str(output_dir.resolve()).encode("utf-16-le")) // 2
        # Leave room for separator, extension and the longest companion suffix.
        stem_limit = min(stem_limit, 259 - directory_units - 1 - len(".partial.mp4"))
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        title_units = stem_limit - len((identifier + "_" + suffix).encode("utf-16-le")) // 2
        if title_units < 1:
            raise ValueError("Слишком длинный путь папки экспорта. Выберите более короткую папку проекта.")
        stem = f"{identifier}_{_shorten(title, title_units) or '_'}{suffix}"
        if output_dir is None or not any((output_dir / (stem + extension)).exists()
                                         for extension in (".mp4", ".txt", ".json", ".ffmpeg.log", ".partial.mp4", ".json.tmp")):
            return stem + ".mp4"
        index += 1


def clip_text(clip, transcript):
    sections = review_sections(transcript, clip["start"], clip["end"])
    return (f"Название клипа: {clip.get('title') or 'Без названия'}\n\n"
            f"Почему выбран: {clip.get('reason') or 'Не указано.'}\n\n"
            + "".join(text for _, text in transcript_chunks(sections, include_technical=False)))


def render_approved(manifest, report, job_dir, log, transcript=None):
    clips = [c for c in report["candidates"] if c["status"] == "approved"]
    if not clips:
        raise ValueError("Нет утверждённых клипов.")
    media = probe(manifest["source_path"])
    for clip in clips:
        validate_interval(clip["start"], clip["end"], media["duration_sec"],
                          report["settings"]["min_duration_sec"], report["settings"]["max_duration_sec"])
        if clip.get("approval_key") != approval_key(clip):
            raise ValueError(f"Границы {clip['candidate_id']} изменились после утверждения. Утвердите их заново.")
    if transcript is None:
        transcript = load_json(Path(job_dir) / "transcript.json")
    texts = [clip_text(clip, transcript) for clip in clips]
    output_dir = Path(job_dir) / "output" / report["signature"]
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for clip, text in zip(clips, texts):
        log(f"Рендер {clip['candidate_id']}: {clip['title']}")
        target = output_dir / output_filename(clip, output_dir)
        temporary = target.with_suffix(".partial.mp4")
        command = ["ffmpeg", "-v", "error", "-y", "-ss", str(clip["start"]),
                   "-i", manifest["source_path"], "-t", str(clip["end"] - clip["start"]),
                   "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-crf", "18",
                   "-preset", "slow", "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "192k",
                   "-movflags", "+faststart", str(temporary)]
        log_path = target.with_suffix(".ffmpeg.log")
        with log_path.open("w", encoding="utf-8") as stderr:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr)
        if result.returncode:
            raise RuntimeError(f"FFmpeg завершился с кодом {result.returncode}. Подробности: {log_path}")
        rendered = probe(temporary)
        if (rendered["width"], rendered["height"]) != (media["width"], media["height"]):
            raise RuntimeError("Рендер изменил разрешение источника.")
        if abs(rendered["duration_sec"] - (clip["end"] - clip["start"])) > 0.3:
            raise RuntimeError("Длительность выходного клипа не соответствует утверждённому интервалу.")
        temporary.rename(target)
        save_json(target.with_suffix(".json"), {"source": manifest, "approved_candidate": clip,
                                               "output": str(target), "media": rendered})
        target.with_suffix(".txt").write_text(text, encoding="utf-8")
        outputs.append(str(target))
        log(f"Сохранено: {target}")
    return outputs


# --- Shorts 9:16 rendering -------------------------------------------------

SHORTS_OUTPUT_W = 1080
SHORTS_OUTPUT_H = 1920
SUBTITLE_SAFE_BOTTOM = 240
SUBTITLE_SAFE_SIDE = 60
SUBTITLE_MAX_CHARS_PER_LINE = 36
SUBTITLE_MAX_WORDS_PER_CUE = 6


def _filtergraph_escape_path(path):
    """Экранирует Windows-путь для вставки в аргумент фильтра subtitles.

    Фильтр subtitles в filtergraph использует собственные правила экранирования:
    уровень 0 (значение аргумента фильтра) экранирует \\, ', :, ;, [, ], и пробел
    обратным слешем. Двоеточие после буквы диска (D:) обязано остаться
    экранированным как D\:, иначе FFmpeg потеряет его.

    Кавычки в filtergraph не используются — весь путь передаётся внутри
    одиночных кавычек Python-строки vf, поэтому двойное экранирование не нужно.
    """
    result = str(path).replace("\\", "/")
    for ch in ("\\", "'", ":", ";", "[", "]"):
        result = result.replace(ch, "\\" + ch)
    return result


def _crop_params(source_width, source_height, crop_x=None):
    """Вычисляет параметры crop= для 9:16 области в источнике.

    crop_x: 0.0–1.0 (доля ширины) или None для центрирования.
    Возвращает (crop_w, crop_h, x, y).
    """
    target_ratio = 9 / 16
    crop_h = source_height
    crop_w = int(round(source_height * target_ratio))
    if crop_w >= source_width:
        crop_w = source_width
        crop_h = int(round(source_width / target_ratio))
    if crop_x is None:
        x = (source_width - crop_w) // 2
    else:
        x = int(round(crop_x * (source_width - crop_w)))
        x = max(0, min(x, source_width - crop_w))
    y = (source_height - crop_h) // 2
    return crop_w, crop_h, x, y


def validate_crop_x(crop_x):
    """Проверяет crop_x: None или float в [0, 1]."""
    if crop_x is None:
        return None
    try:
        value = float(str(crop_x).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError("crop_x должен быть числом от 0 до 1 или пустым.") from exc
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("crop_x должен быть в диапазоне от 0 до 1.")
    return round(value, 4)


def _srt_timecode(seconds):
    """Преобразует секунды в SRT-формат ЧЧ:ММ:СС,ммм."""
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _group_words_into_cues(words, max_words=SUBTITLE_MAX_WORDS_PER_CUE):
    """Группирует слова в подзаголовки по max_words слов."""
    cues = []
    current = []
    for w in words:
        current.append(w)
        if len(current) >= max_words:
            cues.append(current)
            current = []
    if current:
        cues.append(current)
    return cues


def build_subtitles_srt(transcript, clip_start, clip_end):
    """Строит SRT-текст из word timestamps, попадающих в [clip_start, clip_end].

    Таймкоды пересчитываются относительно начала клипа (0.0).
    """
    eps = 0.000001
    words = []
    for segment in transcript.get("segments", []):
        if segment.get("end", 0) <= clip_start - eps or segment.get("start", 0) >= clip_end + eps:
            continue
        for w in segment.get("words", []):
            w_start = max(w["start"], clip_start)
            w_end = min(w["end"], clip_end)
            if w_end > w_start + eps and w.get("word", "").strip():
                words.append({"start": w_start - clip_start, "end": w_end - clip_start,
                              "word": w["word"].strip()})
    words.sort(key=lambda x: x["start"])
    if not words:
        return ""
    cues = _group_words_into_cues(words)
    lines = []
    for i, cue in enumerate(cues, 1):
        start = cue[0]["start"]
        end = cue[-1]["end"]
        text = " ".join(w["word"] for w in cue)
        # Перенос длинных строк
        if len(text) > SUBTITLE_MAX_CHARS_PER_LINE:
            mid = len(text) // 2
            space = text.find(" ", mid - 5, mid + 5)
            if space == -1:
                space = mid
            text = text[:space] + "\n" + text[space + 1:]
        lines.append(f"{i}\n{_srt_timecode(start)} --> {_srt_timecode(end)}\n{text}\n")
    return "\n".join(lines)


def _probe_basic(path):
    """Минимальный ffprobe без проверки aspect ratio (для 9:16 результата)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    info = json.loads(result.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return {
        "duration_sec": float(info["format"]["duration"]),
        "width": video["width"],
        "height": video["height"],
        "has_audio": has_audio,
    }


def render_shorts_approved(manifest, report, job_dir, log, transcript=None, candidate_id=None):
    """Рендер утверждённых Shorts 9:16 с crop и необязательными субтитрами."""
    shorts = report.get("shorts_candidates", [])
    if candidate_id is not None:
        shorts = [c for c in shorts if c["candidate_id"] == candidate_id]
    approved = [c for c in shorts if c["status"] == "approved"]
    if not approved:
        raise ValueError("Нет утверждённых Shorts.")
    media = probe(manifest["source_path"])
    profiles = report.get("profiles", {})
    shorts_profile = profiles.get("shorts", {})
    min_dur = shorts_profile.get("min_duration_sec", 20)
    max_dur = shorts_profile.get("max_duration_sec", 60)
    for clip in approved:
        validate_interval(clip["start"], clip["end"], media["duration_sec"], min_dur, max_dur)
        if clip.get("approval_key") != shorts_approval_key(clip):
            raise ValueError(
                f"Параметры {clip['candidate_id']} изменились после утверждения. Утвердите заново.")
    if transcript is None:
        transcript = load_json(Path(job_dir) / "transcript.json")

    output_dir = Path(job_dir) / "output" / report["signature"] / "shorts"
    output_dir.mkdir(parents=True, exist_ok=True)

    crop_w, crop_h, cx, cy = _crop_params(media["width"], media["height"])
    outputs = []
    for clip in approved:
        log(f"Рендер Shorts {clip['candidate_id']}: {clip['title']}")
        render_settings = clip.get("render_settings") or {}
        crop_mode = render_settings.get("crop_mode", "center")
        crop_x_raw = render_settings.get("crop_x")
        crop_x = validate_crop_x(crop_x_raw)
        subtitles_on = render_settings.get("subtitles_enabled", True)

        crop_w, crop_h, cx, cy = _crop_params(media["width"], media["height"], crop_x)

        target = output_dir / output_filename(clip, output_dir)
        temporary = target.with_suffix(".partial.mp4")

        vf_parts = [
            f"crop={crop_w}:{crop_h}:{cx}:{cy}",
            f"scale={SHORTS_OUTPUT_W}:{SHORTS_OUTPUT_H}",
        ]
        srt_keep_path = None
        tmp_srt_path = None
        if subtitles_on:
            srt_content = build_subtitles_srt(transcript, clip["start"], clip["end"])
            if srt_content:
                srt_keep_path = output_dir / (target.stem + ".srt")
                srt_keep_path.write_text(srt_content, encoding="utf-8-sig")
                if not srt_keep_path.exists() or not srt_keep_path.stat().st_size:
                    raise RuntimeError(f"SRT не создан или пуст: {srt_keep_path}")
                # Временный SRT с коротким ASCII-именем в том же каталоге,
                # чтобы FFmpeg subtitles-фильтр точно находил файл.
                fd, tmp_srt_path = tempfile.mkstemp(
                    prefix="subs_", suffix=".srt", dir=str(output_dir), text=True)
                with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                    f.write(srt_content)
                escaped = _filtergraph_escape_path(tmp_srt_path)
                vf_parts.append(
                    f"subtitles='{escaped}':force_style='FontName=Arial,FontSize=18,"
                    f"PrimaryColour=&HFFFFFF&,OutlineColour=&H00000000&,Outline=1,Alignment=2,"
                    f"MarginV={SUBTITLE_SAFE_BOTTOM},MarginL={SUBTITLE_SAFE_SIDE},MarginR={SUBTITLE_SAFE_SIDE}'"
                )

        vf = ",".join(vf_parts)
        duration = clip["end"] - clip["start"]
        command = [
            "ffmpeg", "-v", "error", "-y",
            "-ss", str(clip["start"]), "-i", manifest["source_path"],
            "-t", str(duration),
            "-map", "0:v:0", "-map", "0:a:0",
            "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "slow",
            "-fps_mode", "passthrough",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(temporary),
        ]
        log_path = target.with_suffix(".ffmpeg.log")
        try:
            with log_path.open("w", encoding="utf-8") as stderr:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr)
            if result.returncode:
                raise RuntimeError(f"FFmpeg (Shorts {clip['candidate_id']}) завершился с кодом "
                                   f"{result.returncode}. Подробности: {log_path}")
            rendered = _probe_basic(temporary)
            if (rendered["width"], rendered["height"]) != (SHORTS_OUTPUT_W, SHORTS_OUTPUT_H):
                raise RuntimeError(
                    f"Разрешение Shorts {clip['candidate_id']}: "
                    f"{rendered['width']}x{rendered['height']}, ожидается {SHORTS_OUTPUT_W}x{SHORTS_OUTPUT_H}.")
            if abs(rendered["duration_sec"] - duration) > 0.3:
                raise RuntimeError(
                    f"Длительность Shorts {clip['candidate_id']} не соответствует утверждённому интервалу.")
            temporary.rename(target)
            save_json(target.with_suffix(".json"), {
                "source": manifest,
                "approved_candidate": clip,
                "output": str(target),
                "media": rendered,
                "crop": {"width": crop_w, "height": crop_h, "x": cx, "y": cy,
                         "mode": crop_mode, "crop_x": crop_x},
                "subtitles": {"enabled": subtitles_on},
            })
            outputs.append(str(target))
            log(f"Сохранено: {target}")
        finally:
            if tmp_srt_path and Path(tmp_srt_path).exists():
                Path(tmp_srt_path).unlink()
    return outputs
