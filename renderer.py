"""Render only approved, unchanged review intervals; preserve the full 16:9 frame."""
import hashlib
import json
import subprocess
from pathlib import Path

from downloader import probe
from job_store import save_json, validate_interval


def approval_key(candidate):
    return hashlib.sha256(json.dumps({k: candidate[k] for k in ("candidate_id", "start", "end")},
                                    sort_keys=True).encode()).hexdigest()


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
        target = output_dir / f"{clip['candidate_id']}_{clip['approval_key'][:10]}.mp4"
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
        temporary.replace(target)
        save_json(target.with_suffix(".json"), {"source": manifest, "approved_candidate": clip,
                                               "output": str(target), "media": rendered})
        outputs.append(str(target))
        log(f"Сохранено: {target}")
    return outputs
