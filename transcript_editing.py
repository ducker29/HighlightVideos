"""Candidate-local transcript corrections, leaving the ASR cache intact."""
import copy
from difflib import SequenceMatcher


def apply_transcript_edits(transcript, edits):
    if not edits:
        return transcript
    result = copy.deepcopy(transcript)
    sentences = result.get("sentences", [])
    for edit in edits:
        index = edit.get("sentence_index")
        if type(index) is not int or not 0 <= index < len(sentences):
            raise ValueError("Исправление ссылается на отсутствующую реплику.")
        sentence = sentences[index]
        if edit.get("start") != sentence["start"] or edit.get("end") != sentence["end"]:
            raise ValueError("Тайминги исходной реплики изменились; повторите правку.")
        text = edit.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Текст реплики не может быть пустым.")
        text = " ".join(text.split())
        start, end = sentence["start"], sentence["end"]
        segments = result.setdefault("segments", [])
        owned = sorted([(segment, word) for segment in segments for word in segment.get("words", [])
                        if start <= (word["start"] + word["end"]) / 2 < end],
                       key=lambda pair: pair[1]["start"])
        old_words = [word for _, word in owned]
        tokens = text.split()
        new_words = []
        matcher = SequenceMatcher(a=[w["word"].strip() for w in old_words], b=tokens, autojunk=False)
        for kind, a, b, c, d in matcher.get_opcodes():
            if kind == "equal":
                new_words.extend(copy.deepcopy(old_words[a:b]))
                continue
            if c == d:
                continue
            left = max(start, old_words[a]["start"]) if a < b else (
                max(start, old_words[a - 1]["end"]) if a else start)
            right = min(end, old_words[b - 1]["end"]) if a < b else (
                min(end, old_words[a]["start"]) if a < len(old_words) else end)
            if right <= left:
                # Adjacent ASR words have no insertion gap: share the neighbour's slot.
                if new_words:
                    left = max(start, new_words[-1]["start"])
                    right = min(end, new_words[-1]["end"])
                    split = left + (right - left) / (d - c + 1)
                    new_words[-1]["end"] = split
                    left = split
                elif a < len(old_words):
                    left = max(start, old_words[a]["start"])
                    right = left + (min(end, old_words[a]["end"]) - left) / 2
                    old_words[a]["start"] = right
            step = (right - left) / (d - c)
            new_words.extend({"word": tokens[j], "start": left + (j - c) * step,
                              "end": left + (j - c + 1) * step} for j in range(c, d))
        for segment, word in owned:
            segment["words"].remove(word)
        if owned:
            target = owned[0][0]
        else:
            target = next((s for s in segments if s.get("start", end) < end
                           and s.get("end", start) > start), None)
            if target is None:
                target = {"start": start, "end": end, "words": []}
                segments.append(target)
        target.setdefault("words", []).extend(new_words)
        target["words"].sort(key=lambda w: w["start"])
        target["start"] = min(target["start"], start)
        target["end"] = max(target["end"], end)
        for segment in segments:
            if "words" in segment:
                segment["text"] = " ".join(w["word"].strip() for w in segment["words"])
        sentence["text"] = text
    return result
