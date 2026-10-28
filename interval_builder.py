"""Authoritative sentence-aligned intervals: duration and padding are code-owned."""
import hashlib
import json

from job_store import padding_seconds, validate_interval

INTERVAL_VERSION = 1


def make_interval(first, last, source_duration, settings):
    if last["start"] < first["start"]:
        raise ValueError("Конец выбран раньше начала.")
    start, content_end = validate_interval(first["start"], last["end"], source_duration)
    maximum = settings["max_duration_sec"]
    if content_end - start > maximum:
        raise ValueError("Смысловой интервал длиннее максимума; обрезать последнее предложение нельзя.")
    padding = padding_seconds(settings.get("end_padding_sec", 0.5))
    end = min(content_end + padding, source_duration, start + maximum)
    validate_interval(start, end, source_duration, settings["min_duration_sec"], maximum)
    identity = {"start_id": first["id"], "end_id": last["id"], "start": start,
                "content_end": content_end, "end": end, "requested_padding": padding,
                "version": INTERVAL_VERSION}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
    return {"interval_id": f"iv_{key}", "start_id": first["id"], "end_id": last["id"],
            "start": start, "end": end, "duration_sec": end - start,
            "end_padding": {"base_end": content_end, "padded_end": end,
                            "requested_sec": padding, "applied_sec": round(end - content_end, 6)}}


def build_interval_options(sentences, start_id, source_duration, settings):
    ordered = sorted(sentences, key=lambda sentence: sentence["start"])
    positions = {s["id"]: i for i, s in enumerate(ordered)}
    if start_id not in positions:
        raise ValueError("Неизвестное начало интервала.")
    index = positions[start_id]
    first = ordered[index]
    result = []
    for end_index in range(index, len(ordered)):
        last = ordered[end_index]
        try:
            option = make_interval(first, last, source_duration, settings)
        except ValueError:
            continue
        # Full neighboring sentences, not a generated summary of the ending.
        option["ending_text"] = " ".join(s["text"] for s in ordered[max(index, end_index - 1):end_index + 1])
        result.append(option)
    return result
