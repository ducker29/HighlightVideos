"""Local Shorts composition intervals; no decoding, rendering or implicit writes."""
import copy
import math


class FramingError(ValueError):
    """Invalid or stale Shorts framing settings."""


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FramingError(f"{name}: требуется конечное число.")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise FramingError(f"{name}: требуется конечное число.") from exc
    if not math.isfinite(result):
        raise FramingError(f"{name}: требуется конечное число.")
    return result


def normalize_framing(framing, duration):
    """Return a fresh canonical document. CROP is implicit between overrides.

    Validate all intervals (including explicit CROP) before discarding redundant
    CROP entries. Boundaries are half-open and are not rounded or clamped.
    """
    duration = _number(duration, "duration")
    if duration <= 0:
        raise FramingError("Длительность Shorts должна быть положительной.")
    if framing is None:
        framing = {"schema_version": 1, "default_mode": "CROP", "intervals": []}
    if not isinstance(framing, dict):
        raise FramingError("framing должен быть объектом.")
    if type(framing.get("schema_version")) is not int or framing["schema_version"] != 1:
        raise FramingError("Поддерживается только framing.schema_version = 1.")
    if framing.get("default_mode") != "CROP":
        raise FramingError("framing.default_mode должен быть CROP.")
    intervals = framing.get("intervals")
    if not isinstance(intervals, list):
        raise FramingError("framing.intervals должен быть списком.")
    ordered = []
    for item in intervals:
        if not isinstance(item, dict):
            raise FramingError("Интервал framing должен быть объектом.")
        start = _number(item.get("start"), "start")
        end = _number(item.get("end"), "end")
        mode = item.get("mode")
        if mode not in ("CROP", "FIT_BLUR"):
            raise FramingError("Допустимы только CROP и FIT_BLUR.")
        if not 0 <= start < end <= duration:
            raise FramingError("Нужны границы 0 ≤ start < end ≤ duration Shorts.")
        ordered.append({"start": start if start else 0.0, "end": end, "mode": mode})
    ordered.sort(key=lambda item: (item["start"], item["end"]))
    for left, right in zip(ordered, ordered[1:]):
        if right["start"] < left["end"]:
            raise FramingError("Интервалы framing пересекаются.")
    result = []
    for item in ordered:
        if item["mode"] == "CROP":
            continue
        if result and result[-1]["end"] == item["start"]:
            result[-1]["end"] = item["end"]
        else:
            result.append(item)
    return {"schema_version": 1, "default_mode": "CROP", "intervals": result}


def _bounds(candidate):
    start = _number(candidate.get("start"), "Shorts.start")
    end = _number(candidate.get("end"), "Shorts.end")
    if not 0 <= start < end:
        raise FramingError("Некорректные границы Shorts.")
    return start, end


def candidate_framing(candidate):
    """Read/validate without modifying the candidate or writing a report."""
    start, end = _bounds(candidate)
    render = candidate.get("render_settings") or {}
    normalized = normalize_framing(render.get("framing"), end - start)
    anchor = render.get("framing_source_bounds")
    if normalized["intervals"] and anchor is not None:
        if not isinstance(anchor, dict) or _bounds(anchor) != (start, end):
            raise FramingError("Границы Shorts изменились. Явно сбросьте устаревшую разметку framing.")
    return normalized


def validate_report_framing(report, previous=None):
    """Guard explicit report writes, including existing boundary-editing paths."""
    old = {c["candidate_id"]: c for c in (previous or {}).get("shorts_candidates", [])}
    for candidate in report.get("shorts_candidates", []):
        render = candidate.get("render_settings") or {}
        if "framing" not in render:
            continue
        normalized = candidate_framing(candidate)
        before = old.get(candidate["candidate_id"])
        if normalized["intervals"] and before is not None and _bounds(before) != _bounds(candidate):
            raise FramingError("Границы Shorts изменились. Сначала явно сбросьте framing.")


def save_candidate_framing(report_path, candidate_id, framing, *, expected_start, expected_end):
    """Explicit atomic save for one Short; return the freshly saved report.

    Callers must supply the bounds used to edit the draft, protecting against a
    stale editor even when the new Short has the same duration. Passing None
    explicitly resets framing to CROP, including stale framing.
    """
    from job_store import load_json, save_json

    report = load_json(report_path)
    matches = [c for c in report.get("shorts_candidates", []) if c["candidate_id"] == candidate_id]
    if len(matches) != 1:
        raise FramingError("Нужен единственный Shorts с указанным candidate_id.")
    candidate = matches[0]
    bounds = _bounds(candidate)
    if bounds != _bounds({"start": expected_start, "end": expected_end}):
        raise FramingError("Границы Shorts изменились после начала редактирования framing.")
    normalized = normalize_framing(framing, bounds[1] - bounds[0])
    render = candidate.get("render_settings") or {}
    try:
        old = candidate_framing(candidate)
    except FramingError:
        if normalized["intervals"]:
            raise
        old = None  # An explicit reset may recover invalid/stale settings.
    updated = copy.deepcopy(report)
    target = next(c for c in updated["shorts_candidates"] if c["candidate_id"] == candidate_id)
    settings = target.setdefault("render_settings", {})
    if settings is None:
        settings = target["render_settings"] = {}
    settings["framing"] = normalized
    settings["framing_source_bounds"] = {"start": bounds[0], "end": bounds[1]}
    # Adding an explicit framing document changes the approval payload even if
    # it describes all-CROP; existing candidates without framing remain legacy.
    if old != normalized or "framing" not in render:
        if target.get("status") == "approved":
            target["status"] = "pending"
        target.pop("approval_key", None)
    save_json(report_path, updated)
    return updated
