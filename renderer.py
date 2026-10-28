"""Render only approved, unchanged review intervals; preserve the full 16:9 frame."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

from downloader import probe
from job_store import save_json, validate_interval


def approval_key(candidate):
    return hashlib.sha256(json.dumps({k: candidate[k] for k in ("candidate_id", "start", "end")},
                                    sort_keys=True).encode()).hexdigest()


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
                                         for extension in (".mp4", ".json", ".ffmpeg.log", ".partial.mp4", ".json.tmp")):
            return stem + ".mp4"
        index += 1


def render_approved(manifest, report, job_dir, log):
    clips = [c for c in report["candidates"] if c["status"] == "approved"]
    if not clips:
        raise ValueError("Нет утверждённых клипов.")
    media = probe(manifest["source_path"])
    for clip in clips:
        validate_interval(clip["start"], clip["end"], media["duration_sec"],
                          report["settings"]["min_duration_sec"], report["settings"]["max_duration_sec"])
        if clip.get("approval_key") != approval_key(clip):
            raise ValueError(f"Границы {clip['candidate_id']} изменились после утверждения. Утвердите их заново.")
    output_dir = Path(job_dir) / "output" / report["signature"]
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for clip in clips:
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
        outputs.append(str(target))
        log(f"Сохранено: {target}")
    return outputs
