"""Read-only transcript slices and explicit source context around exact clip bounds."""
import math

from job_store import timecode

START_MARKER = "-----НАЧАЛО-КЛИПА-----"
END_MARKER = "-----КОНЕЦ-КЛИПА-----"
EPSILON = 0.000001


def review_sections(transcript, start, end, context_count=3):
    start, end = float(start), float(end)
    if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
        raise ValueError("Нужны корректные границы начала и конца клипа.")
    sentences = sorted(transcript.get("sentences", []), key=lambda s: (s["start"], s["end"]))
    overlapping = [s for s in sentences if s["end"] > start + EPSILON and s["start"] < end - EPSILON]
    before = [s for s in sentences if s["end"] <= start + EPSILON]
    after = [s for s in sentences if s["start"] >= end - EPSILON]
    left = overlapping[0] if overlapping and overlapping[0]["start"] < start - EPSILON else None
    right = overlapping[-1] if overlapping and overlapping[-1]["end"] > end + EPSILON else None
    # Full text of a boundary-crossing sentence belongs to the clearly marked context.
    # If a single long sentence crosses both edges, show its full text only once, after.
    if left is not None and left is not right:
        before.append(left)
    if right is not None:
        after.insert(0, right)
    before = before[-context_count:] if context_count else []
    after = after[:context_count]
    words = [w for segment in transcript.get("segments", [])
             if segment.get("end", math.inf) > start and segment.get("start", -math.inf) < end
             for w in segment.get("words", [])
             if w["end"] > start + EPSILON and w["start"] < end - EPSILON and w["end"] > w["start"]]
    rows = []
    for sentence in overlapping:
        complete = sentence["start"] >= start - EPSILON and sentence["end"] <= end + EPSILON
        row = {"sentence": sentence, "kind": "full" if complete else "partial",
               "start": max(start, sentence["start"]), "end": min(end, sentence["end"])}
        if complete:
            row["text"] = sentence["text"]
        else:
            owned_words = [w for w in words if w["start"] >= sentence["start"] - EPSILON
                           and w["end"] <= sentence["end"] + EPSILON]
            full_words = [w for w in owned_words if w["start"] >= start - EPSILON and w["end"] <= end + EPSILON]
            fragments = [w["word"].strip() for w in owned_words
                         if w["start"] < start - EPSILON or w["end"] > end + EPSILON]
            row["text"] = " ".join(w["word"].strip() for w in full_words)
            row["cut_words"] = fragments
            row["word_timestamps_available"] = bool(owned_words)
        rows.append(row)
    return {"start": start, "end": end, "before": before, "rows": rows, "after": after}


def transcript_chunks(sections):
    """Return tagged text chunks: context/full/partial/marker, for both GUI and tests."""
    start, end = sections["start"], sections["end"]
    result = [("context", "КОНТЕКСТ ДО — НЕ ВХОДИТ ЦЕЛИКОМ В КЛИП\n")]

    def context_line(sentence):
        overlap = max(0, min(end, sentence["end"]) - max(start, sentence["start"]))
        label = "НЕ ВХОДИТ В КЛИП" if overlap <= EPSILON else f"НЕ ВХОДИТ ЦЕЛИКОМ: попало только {overlap:.3f} с"
        return (f"[{timecode(sentence['start'])}–{timecode(sentence['end'])}] [{label}] "
                f"{sentence['text']}\n")

    if sections["before"]:
        result.extend(("context", context_line(sentence)) for sentence in sections["before"])
    else:
        result.append(("context", "(Предыдущих реплик в сохранённом транскрипте нет.)\n"))
    result.append(("marker", f"\n{START_MARKER}\n"))
    result.append(("clip", f"Границы исходного видео: {timecode(start)}–{timecode(end)}. Разметка ASR приблизительная.\n"))
    if not sections["rows"]:
        result.append(("clip", "(В этом интервале нет распознанных реплик.)\n"))
    for row in sections["rows"]:
        prefix = f"[{timecode(row['start'])}–{timecode(row['end'])}] "
        if row["kind"] == "full":
            result.append(("clip", prefix + row["text"] + "\n"))
        else:
            details = row["text"] or "(Полных слов по разметке не попало.)"
            if row["cut_words"]:
                details += " Граница проходит внутри слов: «" + "», «".join(row["cut_words"]) + "»."
            if not row["word_timestamps_available"]:
                details = "(Нет word-level разметки для точного отображения этой части.)"
            result.append(("partial", prefix + "[НЕПОЛНАЯ РЕПЛИКА] " + details +
                           " Полный текст показан отдельно в контексте.\n"))
    result.append(("marker", f"{END_MARKER}\n\n"))
    result.append(("context", "КОНТЕКСТ ПОСЛЕ — НЕ ВХОДИТ ЦЕЛИКОМ В КЛИП\n"))
    if sections["after"]:
        result.extend(("context", context_line(sentence)) for sentence in sections["after"])
    else:
        result.append(("context", "(Следующих реплик в сохранённом транскрипте нет.)\n"))
    return result
