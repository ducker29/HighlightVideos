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
from smart_crop_storage import (
    SmartCropError,
    load_settings as load_smart_crop_settings,
    settings_path as smart_crop_settings_path,
)
from transcript_review import review_sections, transcript_chunks
from transcript_editing import apply_transcript_edits
from subtitle_settings import (DEFAULTS as SUBTITLE_DEFAULTS, LEGACY_STYLE,
                               document as subtitle_document, uses_legacy_srt_style)
from subtitle_background import MeasurementCache, prepare_ass
from shorts_settings import DEFAULTS as SHORTS_DEFAULTS, document as shorts_export_document
from local_source import verify_local_source
from shorts_framing import candidate_framing
from shorts_filtergraph import build_shorts_filtergraph


def approval_key(candidate):
    payload = {k: candidate[k] for k in ("candidate_id", "start", "end")}
    if candidate.get("transcript_edits"):
        payload["transcript_edits"] = candidate["transcript_edits"]
    return hashlib.sha256(json.dumps(payload,
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
    if candidate.get("transcript_edits"):
        payload["transcript_edits"] = candidate["transcript_edits"]
    if "framing" in render:
        payload["framing"] = candidate_framing(candidate)
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
    transcript = apply_transcript_edits(transcript, clip.get("transcript_edits"))
    sections = review_sections(transcript, clip["start"], clip["end"])
    return (f"Название клипа: {clip.get('title') or 'Без названия'}\n\n"
            f"Почему выбран: {clip.get('reason') or 'Не указано.'}\n\n"
            + "".join(text for _, text in transcript_chunks(sections, include_technical=False)))


def render_approved(manifest, report, job_dir, log, transcript=None):
    clips = [c for c in report["candidates"] if c["status"] == "approved"]
    if not clips:
        raise ValueError("Нет утверждённых клипов.")
    verify_local_source(manifest, log)
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


def _smart_crop_file(job_dir, report, candidate):
    """Возвращает путь к Smart Crop-файлу кандидата, если он существует."""
    signature = report.get("signature")
    if not signature:
        return None
    path = smart_crop_settings_path(job_dir, signature, candidate["candidate_id"])
    return path if path.exists() else None


def _crop_switch_exprs(segments, source_width, source_height):
    """Строит crop-выражения c временным переключением по сегментам.

    FFmpeg crop поддерживает таймлайн-выражения с t в этой сборке, поэтому
    каждый сегмент задаётся ветвью if(lt(t, end_i), ...). Для последнего
    сегмента используется else. Возвращает (w_expr, h_expr, x_expr, y_expr)
    в виде Python-строк, готовых к подстановке в фильтр crop.

    t здесь — время относительно начала Short (после -ss), что совпадает
    с локальными start/end сегментов.
    """
    branches = []
    for i, seg in enumerate(segments):
        cw, ch, cx, cy = _crop_to_pixels(seg["crop"], source_width, source_height)
        branches.append((cw, ch, cx, cy))

    def branch_exprs(i):
        cw, ch, cx, cy = branches[i]
        return (str(cw), str(ch), str(cx), str(cy))

    def build(i):
        if i == len(segments) - 1:
            return branch_exprs(i)
        else_val = build(i + 1)
        w, h, x, y = branch_exprs(i)
        t = f"{segments[i]['end']:.6f}"
        return tuple(
            f"if(lt(t\\,{t}),{p},{e})" for p, e in zip((w, h, x, y), else_val)
        )

    return build(0)


def _crop_to_pixels(crop, source_width, source_height):
    """Переводит нормализованный crop (0..1) в пиксели исходного кадра.

    Гарантирует границы кадра, корректную 9:16 геометрию (нечётные размеры
    вырезаются по 9:16) и чётные x/y/ширина/высота для H.264 yuv420p.
    Возвращает (crop_w, crop_h, x, y).
    """
    x = crop.get("x", 0.0)
    y = crop.get("y", 0.0)
    width = crop.get("width", 1.0)
    height = crop.get("height", 1.0)

    def even(n):
        return max(0, int(n) // 2 * 2)

    px = even(x * source_width)
    py = even(y * source_height)
    pw = even(width * source_width)
    ph = even(height * source_height)
    if pw < 2:
        pw = 2
    if ph < 2:
        ph = 2
    # Укладываем область в границы кадра.
    if px + pw > source_width:
        px = max(0, source_width - pw)
    if py + ph > source_height:
        py = max(0, source_height - ph)
    px = even(px)
    py = even(py)
    pw = even(min(pw, source_width - px))
    ph = even(min(ph, source_height - py))

    # Корректируем размер, чтобы сохранить соотношение 9:16:
    # берём высоту, которая даёт 9:16 при текущей ширине, и вписываем
    # результат в оставшееся место кадра.
    target_ratio = 9 / 16
    crop_h = even(min(round(pw / target_ratio), source_height - py))
    crop_w = even(min(round(crop_h * target_ratio), source_width - px))
    if crop_w < 2:
        crop_w = 2
    if crop_h < 2:
        crop_h = 2
    return crop_w, crop_h, px, py


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


def build_subtitles_srt(transcript, clip_start, clip_end, *, return_cues=False):
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
        return ("", []) if return_cues else ""
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
                space = min((i for i, char in enumerate(text) if char == " "),
                            key=lambda i: abs(i - mid), default=-1)
            if space >= 0:
                text = text[:space] + "\n" + text[space + 1:]
        lines.append(f"{i}\n{_srt_timecode(start)} --> {_srt_timecode(end)}\n{text}\n")
    result = "\n".join(lines)
    return (result, cues) if return_cues else result


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


def _shorts_source_sar(media):
    """ffprobe uses N/A or 0:1 for unspecified SAR; match probe's 1:1 fallback."""
    raw = media.get("sample_aspect_ratio")
    if raw in (None, "", "N/A", "0:1"):
        return (1, 1)
    try:
        numerator, denominator = (int(part) for part in raw.split(":"))
        if numerator <= 0 or denominator <= 0:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Некорректный SAR исходника: {raw!r}.") from exc
    return numerator, denominator


def render_shorts_approved(manifest, report, job_dir, log, transcript=None, candidate_id=None,
                           subtitle_settings=None, export_settings=None):
    """Рендер утверждённых Shorts 9:16 с crop и необязательными субтитрами."""
    subtitle_settings = subtitle_settings or SUBTITLE_DEFAULTS
    export_settings = export_settings or SHORTS_DEFAULTS
    shorts = report.get("shorts_candidates", [])
    if candidate_id is not None:
        shorts = [c for c in shorts if c["candidate_id"] == candidate_id]
    approved = [c for c in shorts if c["status"] == "approved"]
    if not approved:
        raise ValueError("Нет утверждённых Shorts.")
    verify_local_source(manifest, log)
    media = probe(manifest["source_path"])
    output_w, output_h = export_settings.output_size(media["width"], media["height"])
    log(f"Экспорт Shorts: {export_settings.resolution}, {output_w}x{output_h}, "
        f"{export_settings.compression}, CRF {export_settings.crf}.")
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

    outputs = []
    measurement_cache = None
    for clip in approved:
        log(f"Рендер Shorts {clip['candidate_id']}: {clip['title']}")
        render_settings = clip.get("render_settings") or {}
        crop_mode = render_settings.get("crop_mode", "center")
        crop_x_raw = render_settings.get("crop_x")
        crop_x = validate_crop_x(crop_x_raw)
        subtitles_on = render_settings.get("subtitles_enabled", True)
        framing = candidate_framing(clip)
        mixed = bool(framing["intervals"])
        source_sar = _shorts_source_sar(media) if mixed else None

        # Приоритет: сохранённый Smart Crop (один или несколько сегментов),
        # иначе — стандартное центрированное кадрирование по crop_mode/crop_x.
        smart_crop_file = _smart_crop_file(job_dir, report, clip)
        segments = None
        crop_meta = None
        settings = None
        if smart_crop_file is not None:
            try:
                settings, warnings = load_smart_crop_settings(smart_crop_file)
            except (SmartCropError, OSError, ValueError) as exc:
                raise ValueError(
                    f"Smart Crop {clip['candidate_id']} не удалось загрузить из {smart_crop_file}: {exc}. "
                    "Откройте редактор Smart Crop и сохраните настройки заново."
                ) from exc
            if settings is not None:
                if (settings["candidate_id"] != clip["candidate_id"]
                        or abs(settings["source_start"] - clip["start"]) > 0.001
                        or abs(settings["source_end"] - clip["end"]) > 0.001
                        or settings["source_width"] != media["width"]
                        or settings["source_height"] != media["height"]):
                    raise ValueError(
                        f"Smart Crop {clip['candidate_id']} не соответствует текущему Short "
                        f"({clip['start']:.2f}–{clip['end']:.2f}). "
                        "Откройте редактор Smart Crop и настройте кадр заново. "
                        f"Старый файл сохранён: {smart_crop_file}"
                    )
                else:
                    segments = settings["segments"]
                    crop_meta = []
                    for seg in segments:
                        crop_meta.append(_crop_to_pixels(seg["crop"], media["width"], media["height"]))
                    if len(segments) == 1:
                        cw, ch, cx, cy = crop_meta[0]
                        log(f"Smart Crop {clip['candidate_id']}: один сегмент, crop "
                            f"{cw}x{ch} @ ({cx},{cy}).")
                    else:
                        log(f"Smart Crop {clip['candidate_id']}: {len(segments)} сегментов, "
                            f"crop переключается по таймлайну.")

        target = output_dir / output_filename(clip, output_dir)
        temporary = target.with_suffix(".partial.mp4")

        if mixed and segments is not None:
            # Static branches in the generator handle changing zoom. Never
            # invoke the legacy dynamic crop.w/h expressions on this path.
            vf_parts = []
        elif segments is not None:
            if len(segments) == 1:
                cw, ch, cx, cy = crop_meta[0]
                crop_part = f"crop={cw}:{ch}:{cx}:{cy}"
            else:
                w_expr, h_expr, x_expr, y_expr = _crop_switch_exprs(
                    segments, media["width"], media["height"])
                crop_part = (f"crop=w='{w_expr}':h='{h_expr}'"
                             f":x='{x_expr}':y='{y_expr}'")
            vf_parts = [crop_part, f"scale={output_w}:{output_h}"]
        else:
            crop_w, crop_h, cx, cy = _crop_params(media["width"], media["height"], crop_x)
            vf_parts = [
                f"crop={crop_w}:{crop_h}:{cx}:{cy}",
                f"scale={output_w}:{output_h}",
            ]
        srt_keep_path = None
        tmp_srt_path = None
        try:
            subtitles_filter = None
            if subtitles_on:
                srt_content, word_cues = build_subtitles_srt(
                    apply_transcript_edits(transcript, clip.get("transcript_edits")),
                    clip["start"], clip["end"], return_cues=True)
                if srt_content:
                    srt_keep_path = output_dir / (target.stem + ".srt")
                    srt_keep_path.write_text(srt_content, encoding="utf-8-sig")
                    if not srt_keep_path.exists() or not srt_keep_path.stat().st_size:
                        raise RuntimeError(f"SRT не создан или пуст: {srt_keep_path}")
                    # Keep the existing ASCII temporary filename and Windows
                    # filtergraph escaping for both -vf and -filter_complex.
                    custom_style = not uses_legacy_srt_style(subtitle_settings)
                    fd, tmp_srt_path = tempfile.mkstemp(
                        prefix="subs_", suffix=".ass" if custom_style else ".srt", dir=str(output_dir), text=True)
                    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                        if custom_style:
                            if (subtitle_settings.background_enabled
                                    and subtitle_settings.background_geometry_mode == "custom"
                                    and not (subtitle_settings.area_enabled and subtitle_settings.background_fill_region)
                                    and measurement_cache is None):
                                measurement_cache = MeasurementCache()
                            try:
                                content = prepare_ass(srt_content, subtitle_settings, output_w, output_h,
                                                      word_cues, cache=measurement_cache)
                            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                                raise ValueError(f"Подготовка субтитров {clip['candidate_id']} не удалась: {exc}") from exc
                        else:
                            content = srt_content
                        f.write(content)
                    escaped = _filtergraph_escape_path(tmp_srt_path)
                    subtitles_filter = (f"subtitles='{escaped}'" if custom_style else
                                        f"subtitles='{escaped}':force_style='{LEGACY_STYLE}'")
                    vf_parts.append(subtitles_filter)

            duration = clip["end"] - clip["start"]
            # Input -ss uses FFmpeg's default accurate seek, with copyts OFF:
            # timestamps of BOTH streams are translated to the selected input
            # origin. Keep any first-frame/audio offset; no independent setpts,
            # asetpts, concat or shortest. The graph inherits these local PTS.
            if mixed:
                plan = build_shorts_filtergraph(
                    clip, media["width"], media["height"], export_settings=export_settings,
                    smart_crop=settings, source_sar=source_sar, subtitles_filter=subtitles_filter)
                video_options = ["-map", f"[{plan.output_label}]", "-map", "0:a:0",
                                 "-filter_complex", plan.filter_complex]
            else:
                video_options = ["-map", "0:v:0", "-map", "0:a:0", "-vf", ",".join(vf_parts)]
            command = [
                "ffmpeg", "-v", "error", "-y",
                "-ss", str(clip["start"]), "-i", manifest["source_path"],
                "-t", str(duration),
                *video_options,
                "-c:v", "libx264", "-crf", str(export_settings.crf), "-preset", "slow",
                "-fps_mode", "passthrough",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                str(temporary),
            ]
            log_path = target.with_suffix(".ffmpeg.log")
            with log_path.open("w", encoding="utf-8") as stderr:
                result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr)
            if result.returncode:
                raise RuntimeError(f"FFmpeg (Shorts {clip['candidate_id']}) завершился с кодом "
                                   f"{result.returncode}. Подробности: {log_path}")
            rendered = _probe_basic(temporary)
            if (rendered["width"], rendered["height"]) != (output_w, output_h):
                raise RuntimeError(
                    f"Разрешение Shorts {clip['candidate_id']}: "
                    f"{rendered['width']}x{rendered['height']}, ожидается {output_w}x{output_h}.")
            if abs(rendered["duration_sec"] - duration) > 0.3:
                raise RuntimeError(
                    f"Длительность Shorts {clip['candidate_id']} не соответствует утверждённому интервалу.")
            if mixed and not rendered.get("has_audio"):
                raise RuntimeError(f"Смешанный Shorts {clip['candidate_id']} получился без аудиопотока.")
            temporary.rename(target)
            if segments is not None:
                crop_meta_out = {
                    "mode": "smart",
                    "smart_crop": True,
                    "segments": [
                        {"start": seg["start"], "end": seg["end"],
                         "crop_pixels": {"width": cw, "height": ch, "x": cx, "y": cy},
                         "manual_override": seg.get("manual_override", False)}
                        for seg, (cw, ch, cx, cy) in zip(segments, crop_meta)
                    ],
                }
            else:
                crop_meta_out = {
                    "width": crop_w, "height": crop_h, "x": cx, "y": cy,
                    "mode": crop_mode, "crop_x": crop_x, "smart_crop": False,
                }
            save_json(target.with_suffix(".json"), {
                "source": manifest,
                "approved_candidate": clip,
                "output": str(target),
                "media": rendered,
                "crop": crop_meta_out,
                "subtitles": {"enabled": subtitles_on, "style": subtitle_document(subtitle_settings)},
                "export_settings": {**shorts_export_document(export_settings), "crf": export_settings.crf,
                                     "output_width": output_w, "output_height": output_h},
                **({"framing": framing} if "framing" in render_settings else {}),
            })
            outputs.append(str(target))
            log(f"Сохранено: {target}")
        finally:
            if tmp_srt_path and Path(tmp_srt_path).exists():
                Path(tmp_srt_path).unlink()
            if temporary.exists():
                temporary.unlink()
    return outputs
