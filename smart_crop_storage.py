"""Storage and manipulation for Smart Crop settings per approved Short.

Segments use normalized crop coordinates (0.0-1.0) so the same settings
work for 720p and 4K sources. Time values are relative to the Short start.
"""
import copy
import json
import math
import os
from pathlib import Path

SCHEMA_VERSION = 1
TARGETS = {"HOST", "GUEST", "AUTO"}
SHOT_TYPES = {
    "HOST_MEDIUM", "HOST_CLOSE", "GUEST_MEDIUM", "GUEST_CLOSE",
    "SPLIT", "INSERT", "UNKNOWN",
}
EPS = 1e-6


class SmartCropError(ValueError):
    """Raised for invalid Smart Crop settings."""


def _finite_number(value, name):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SmartCropError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise SmartCropError(f"{name} must be finite")
    return number


def _normalize_crop(crop):
    if not isinstance(crop, dict):
        raise SmartCropError("crop must be a dict")
    required = {"x", "y", "width", "height"}
    if not required.issubset(crop):
        raise SmartCropError(f"crop missing keys: {required - set(crop)}")
    values = {}
    for key in required:
        value = _finite_number(crop[key], f"crop.{key}")
        if key in ("width", "height"):
            if not (0 < value <= 1.0 + EPS):
                raise SmartCropError(f"crop.{key} must be in (0, 1]")
        else:
            if not (0 <= value <= 1.0 + EPS):
                raise SmartCropError(f"crop.{key} must be in [0, 1]")
        # Clamp tiny float overshoots so round-trips stay stable.
        if key in ("x", "y"):
            value = min(max(value, 0.0), 1.0)
        else:
            value = min(max(value, 0.0), 1.0)
        values[key] = value
    if values["x"] + values["width"] > 1.0 + EPS:
        raise SmartCropError("crop x + width exceeds 1")
    if values["y"] + values["height"] > 1.0 + EPS:
        raise SmartCropError("crop y + height exceeds 1")
    return {key: round(values[key], 6) for key in ("x", "y", "width", "height")}


def _normalize_segment(segment, index):
    if not isinstance(segment, dict):
        raise SmartCropError(f"segments[{index}] must be a dict")
    start = _finite_number(segment.get("start"), f"segments[{index}].start")
    end = _finite_number(segment.get("end"), f"segments[{index}].end")
    if not (start < end):
        raise SmartCropError(f"segments[{index}] requires start < end")
    shot_type = segment.get("shot_type", "UNKNOWN")
    if shot_type not in SHOT_TYPES:
        raise SmartCropError(
            f"segments[{index}].shot_type must be one of {sorted(SHOT_TYPES)}")
    target = segment.get("target", "AUTO")
    if target not in TARGETS:
        raise SmartCropError(
            f"segments[{index}].target must be one of {sorted(TARGETS)}")
    crop = _normalize_crop(segment.get("crop"))
    manual_override = bool(segment.get("manual_override", False))
    return {
        "start": round(start, 6),
        "end": round(end, 6),
        "shot_type": shot_type,
        "target": target,
        "crop": crop,
        "manual_override": manual_override,
    }


def validate_segments(segments, duration):
    """Validate and normalize a full segment list covering [0, duration]."""
    duration = _finite_number(duration, "duration")
    if duration <= 0:
        raise SmartCropError("duration must be positive")
    if not segments:
        raise SmartCropError("segments must not be empty")
    normalized = [_normalize_segment(segment, i) for i, segment in enumerate(segments)]
    normalized.sort(key=lambda item: item["start"])
    if normalized[0]["start"] > EPS:
        raise SmartCropError("first segment must start at 0")
    for previous, current in zip(normalized, normalized[1:]):
        if current["start"] < previous["end"] - EPS:
            raise SmartCropError(
                f"segments overlap: {previous['end']} > {current['start']}")
        gap = current["start"] - previous["end"]
        if gap > EPS:
            raise SmartCropError(
                f"gap between segments: {previous['end']} .. {current['start']}")
    if normalized[-1]["end"] < duration - EPS:
        raise SmartCropError("segments must cover the whole Short")
    # Snap the final boundary to the exact duration so consumers see full coverage.
    normalized[-1]["end"] = round(duration, 6)
    return normalized


def create_settings(candidate_id, source_start, source_end,
                    source_width, source_height, segments=None):
    """Create a new settings dict covering the whole Short."""
    source_start = _finite_number(source_start, "source_start")
    source_end = _finite_number(source_end, "source_end")
    if source_start < 0 or source_end <= source_start:
        raise SmartCropError("need 0 <= source_start < source_end")
    source_width = int(_finite_number(source_width, "source_width"))
    source_height = int(_finite_number(source_height, "source_height"))
    if source_width <= 0 or source_height <= 0:
        raise SmartCropError("source dimensions must be positive")
    duration = round(source_end - source_start, 6)
    if segments is None:
        # Largest centered 9:16 crop in source-pixel coordinates.
        width_per_height = (9.0 / 16.0) * source_height / source_width
        crop_width = min(1.0, width_per_height)
        crop_height = min(1.0, 1.0 / width_per_height)
        segments = [{
            "start": 0.0,
            "end": duration,
            "shot_type": "UNKNOWN",
            "target": "AUTO",
            "crop": {
                "x": (1.0 - crop_width) / 2.0,
                "y": (1.0 - crop_height) / 2.0,
                "width": crop_width,
                "height": crop_height,
            },
            "manual_override": False,
        }]
    normalized = validate_segments(segments, duration)
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": str(candidate_id),
        "source_start": round(source_start, 6),
        "source_end": round(source_end, 6),
        "source_width": source_width,
        "source_height": source_height,
        "segments": normalized,
    }


def load_settings(path):
    """Load settings from disk; returns (settings, warnings)."""
    path = Path(path)
    if not path.exists():
        raise SmartCropError(f"Smart Crop file not found: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SmartCropError(f"Cannot read Smart Crop file: {exc}") from exc
    return _load_from_dict(data)


def _load_from_dict(data):
    """Load settings from a parsed JSON dict; returns (settings, warnings)."""
    if not isinstance(data, dict):
        raise SmartCropError("Smart Crop root must be an object")
    warnings = []
    schema_version = data.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        warnings.append(
            f"schema_version {schema_version} != current {SCHEMA_VERSION}; "
            "attempting compatibility load")
    candidate_id = data.get("candidate_id")
    if not candidate_id:
        raise SmartCropError("candidate_id is required")
    source_start = _finite_number(data.get("source_start"), "source_start")
    source_end = _finite_number(data.get("source_end"), "source_end")
    source_width = int(_finite_number(data.get("source_width"), "source_width"))
    source_height = int(_finite_number(data.get("source_height"), "source_height"))
    duration = round(source_end - source_start, 6)
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise SmartCropError("segments must be a list")
    normalized = validate_segments(segments, duration)
    settings = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": str(candidate_id),
        "source_start": round(source_start, 6),
        "source_end": round(source_end, 6),
        "source_width": source_width,
        "source_height": source_height,
        "segments": normalized,
    }
    return settings, warnings


def save_settings(settings, path):
    """Atomically write settings to disk."""
    _ = validate_settings(settings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    payload = copy.deepcopy(settings)
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    os.replace(temp, path)
    return path


def validate_settings(settings):
    """Validate a settings dict and return a normalized deep copy."""
    if not isinstance(settings, dict):
        raise SmartCropError("settings must be a dict")
    schema_version = settings.get("schema_version", SCHEMA_VERSION)
    if schema_version != SCHEMA_VERSION:
        # Allow forward/backward tolerance; callers may want to warn.
        pass
    candidate_id = settings.get("candidate_id")
    if not candidate_id:
        raise SmartCropError("candidate_id is required")
    source_start = _finite_number(settings.get("source_start"), "source_start")
    source_end = _finite_number(settings.get("source_end"), "source_end")
    if source_start < 0 or source_end <= source_start:
        raise SmartCropError("need 0 <= source_start < source_end")
    source_width = int(_finite_number(settings.get("source_width"), "source_width"))
    source_height = int(_finite_number(settings.get("source_height"), "source_height"))
    if source_width <= 0 or source_height <= 0:
        raise SmartCropError("source dimensions must be positive")
    duration = round(source_end - source_start, 6)
    segments = settings.get("segments")
    if not isinstance(segments, list):
        raise SmartCropError("segments must be a list")
    normalized = validate_segments(segments, duration)
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": str(candidate_id),
        "source_start": round(source_start, 6),
        "source_end": round(source_end, 6),
        "source_width": source_width,
        "source_height": source_height,
        "segments": normalized,
    }


def split_segment(settings, time, split_target=None, split_shot_type=None):
    """Split the segment containing time. Returns a new settings dict."""
    settings = copy.deepcopy(validate_settings(settings))
    duration = settings["source_end"] - settings["source_start"]
    time = _finite_number(time, "time")
    if time <= EPS or time >= duration - EPS:
        raise SmartCropError("split time must be inside (0, duration)")
    for i, segment in enumerate(settings["segments"]):
        if segment["start"] + EPS < time < segment["end"] - EPS:
            left = {
                "start": segment["start"],
                "end": round(time, 6),
                "shot_type": segment["shot_type"],
                "target": segment["target"],
                "crop": dict(segment["crop"]),
                "manual_override": segment["manual_override"],
            }
            right = {
                "start": round(time, 6),
                "end": segment["end"],
                "shot_type": split_shot_type or segment["shot_type"],
                "target": split_target or segment["target"],
                "crop": dict(segment["crop"]),
                "manual_override": segment["manual_override"],
            }
            settings["segments"][i:i + 1] = [left, right]
            settings["segments"] = validate_segments(settings["segments"], duration)
            return settings
    raise SmartCropError("no segment contains the split time")


def merge_segments(settings, index):
    """Merge segment at index with the next one. Returns a new settings dict."""
    settings = copy.deepcopy(validate_settings(settings))
    segments = settings["segments"]
    if index < 0 or index >= len(segments) - 1:
        raise SmartCropError("cannot merge: not an internal boundary")
    left = segments[index]
    right = segments[index + 1]
    if abs(left["end"] - right["start"]) > EPS:
        raise SmartCropError("cannot merge non-adjacent segments")
    # The merged segment inherits crop and manual_override from the selected
    # (left) segment; a merge is still a manual action, so the flag is kept.
    merged = {
        "start": left["start"],
        "end": right["end"],
        "shot_type": right["shot_type"],
        "target": right["target"],
        "crop": dict(left["crop"]),
        "manual_override": left["manual_override"],
    }
    segments[index:index + 2] = [merged]
    settings["segments"] = validate_segments(segments, settings["source_end"] - settings["source_start"])
    return settings


def set_segment_target(settings, index, target):
    """Change target of a segment and mark it manually overridden."""
    settings = copy.deepcopy(validate_settings(settings))
    segments = settings["segments"]
    if index < 0 or index >= len(segments):
        raise SmartCropError("segment index out of range")
    if target not in TARGETS:
        raise SmartCropError(f"target must be one of {sorted(TARGETS)}")
    segments[index]["target"] = target
    segments[index]["manual_override"] = True
    return settings


def clear_manual_override(settings, index):
    """Remove the manual_override flag from a segment."""
    settings = copy.deepcopy(validate_settings(settings))
    segments = settings["segments"]
    if index < 0 or index >= len(segments):
        raise SmartCropError("segment index out of range")
    segments[index]["manual_override"] = False
    return settings


def set_segment_crop(settings, index, crop):
    """Replace crop of a segment and mark it manually overridden."""
    settings = copy.deepcopy(validate_settings(settings))
    segments = settings["segments"]
    if index < 0 or index >= len(segments):
        raise SmartCropError("segment index out of range")
    normalized = _normalize_crop(crop)
    segments[index]["crop"] = normalized
    segments[index]["manual_override"] = True
    return settings


def segment_at(settings, time):
    """Return the index of the segment containing time, or -1."""
    settings = validate_settings(settings)
    time = _finite_number(time, "time")
    for i, segment in enumerate(settings["segments"]):
        if segment["start"] - EPS <= time < segment["end"] - EPS:
            return i
        if i == len(settings["segments"]) - 1 and abs(time - segment["end"]) <= EPS:
            return i
    return -1


def settings_path(job_dir, signature, candidate_id):
    """Return the conventional path for a Short's Smart Crop file."""
    job_dir = Path(job_dir)
    return job_dir / "analysis" / signature / f"smart_crop_{candidate_id}.json"
