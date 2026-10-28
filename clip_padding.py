"""Prepare visible, reviewable end padding; the renderer never adds hidden time."""
import copy
import hashlib
import json
import math
from pathlib import Path

from analyzer import interval_iou
from job_store import load_json, padding_seconds, save_json, validate_interval

PADDING_VERSION = 1


def set_candidate_padding(candidate, enabled, seconds, duration, settings):
    """Change one visible interval, always relative to its unpadded end."""
    seconds = padding_seconds(seconds)
    result = copy.deepcopy(candidate)
    previous = result.get("end_padding")
    old_end = result["end"]
    base_end = (previous["base_end"] if previous and
                math.isclose(old_end, previous["padded_end"], rel_tol=0, abs_tol=0.000001)
                else old_end)
    end = min(base_end + (seconds if enabled else 0), duration,
              result["start"] + settings["max_duration_sec"])
    validate_interval(result["start"], end, duration,
                      settings["min_duration_sec"], settings["max_duration_sec"])
    result["end_padding_enabled"] = enabled
    result["end_padding"] = {"base_end": base_end, "padded_end": end,
                             "requested_sec": seconds if enabled else 0,
                             "applied_sec": round(end - base_end, 6)}
    result["warnings"] = [w for w in result.get("warnings", [])
                          if not w.startswith("Запас в конце:")]
    if enabled and end - base_end < seconds - 0.001:
        result["warnings"].append("Запас в конце: добавлен не полностью из-за конца источника или лимита длительности; проверьте последнее слово.")
    if end != old_end:
        result["end"] = end
        if result["status"] == "approved":
            result["status"] = "pending"
        result.pop("approval_key", None)
    if "duration_sec" in result:
        result["duration_sec"] = result["end"] - result["start"]
    return result


def refresh_padding_overlaps(report):
    pairs = []
    for candidate in report["candidates"]:
        candidate["warnings"] = [w for w in candidate.get("warnings", [])
                                 if not w.startswith("После запаса:")]
    active = [c for c in report["candidates"] if c["status"] != "rejected"]
    for index, first in enumerate(active):
        for second in active[index + 1:]:
            overlap = min(first["end"], second["end"]) - max(first["start"], second["start"])
            if overlap > 0:
                pairs.append({"first": first["candidate_id"], "second": second["candidate_id"],
                              "overlap_sec": round(overlap, 3), "iou": interval_iou(first, second)})
                for candidate, other in ((first, second), (second, first)):
                    candidate["warnings"].append(f"После запаса: пересечение с {other['candidate_id']} — {overlap:.3f} с; проверьте интервалы.")
    report["padding_overlap_pairs"] = pairs


def prepare_review_report(source_path, seconds, log):
    source_path = Path(source_path).resolve()
    seconds = padding_seconds(seconds)
    source = load_json(source_path)
    if source.get("padding_version") == PADDING_VERSION and source["settings"].get("end_padding_sec") == seconds:
        log("Запас уже учтён. Ручные границы и решения сохранены.")
        return source_path
    key = hashlib.sha256(json.dumps({"source": source, "seconds": seconds, "version": PADDING_VERSION},
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    directory = source_path.parent.parent / f"tail_v{PADDING_VERSION}_{key}"
    result_path = directory / "report.json"
    if result_path.exists():
        log("Используется сохранённый отчёт с запасом и решениями ревью.")
        return result_path
    transcript = load_json(source_path.parent / "transcript.json")
    duration = transcript["duration_sec"]
    report = copy.deepcopy(source)
    settings = report["settings"]
    changed = 0
    for candidate in report["candidates"]:
        start, old_end = validate_interval(candidate["start"], candidate["end"], duration,
                                           settings["min_duration_sec"], settings["max_duration_sec"])
        candidate["warnings"] = [w for w in candidate.get("warnings", [])
                                 if not w.startswith(("Запас в конце:", "После запаса:"))]
        previous = candidate.get("end_padding")
        if previous and not math.isclose(old_end, previous["padded_end"], rel_tol=0, abs_tol=0.000001):
            candidate["warnings"].append("Запас в конце: сохранена вручную изменённая граница; повторное добавление не выполнено.")
            continue
        base_end = previous["base_end"] if previous else old_end
        requested = seconds if candidate.get("end_padding_enabled", True) else 0
        end = min(base_end + requested, duration, start + settings["max_duration_sec"])
        validate_interval(start, end, duration, settings["min_duration_sec"], settings["max_duration_sec"])
        # Preserve the exact stored number when unchanged: approval hashes distinguish
        # JSON integers from floats (70 versus 70.0).
        if end != old_end:
            candidate["end"] = end
        if "duration_sec" in candidate:
            candidate["duration_sec"] = candidate["end"] - candidate["start"]
        candidate["end_padding"] = {"base_end": base_end, "padded_end": end, "requested_sec": requested,
                                    "applied_sec": round(end - base_end, 6)}
        if end - base_end < requested - 0.001:
            candidate["warnings"].append("Запас в конце: добавлен не полностью из-за конца источника или лимита длительности; проверьте последнее слово.")
        if end != old_end:
            changed += 1
            if candidate["status"] == "approved":
                candidate["status"] = "pending"
            candidate.pop("approval_key", None)
    refresh_padding_overlaps(report)
    report.update(signature=directory.name, padding_version=PADDING_VERSION,
                  padding_source_report=str(source_path))
    settings["end_padding_sec"] = seconds
    save_json(directory / "transcript.json", transcript)
    save_json(result_path, report)
    job_dir = source_path.parent.parent.parent
    save_json(job_dir / "latest_report.json", {"path": str(result_path)})
    log(f"Запас {seconds:g} с учтён до ревью; изменено интервалов: {changed}. Изменённые утверждённые клипы ожидают повторного утверждения.")
    return result_path
