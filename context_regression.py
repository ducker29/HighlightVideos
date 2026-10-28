"""Check prompt plans against a saved failed run; --live tests one failed block only."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import json
from pathlib import Path

from analyzer import CLIP_SCHEMA, ask, clip_prompt, collect_clips, merge_windows, windows
from job_store import load_json, save_json, validate_interval
from llm_context import INITIAL_PROMPT_BYTES, MAX_PROMPT_BYTES, byte_size, fit_sentence_groups, repair_feedback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--block", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay-invalid", action="store_true", help="Recover valid clips from three saved attempts without LLM requests")
    mode.add_argument("--resume", action="store_true", help="Resume the complete saved job without rendering")
    args = parser.parse_args()
    cache = args.cache.resolve()
    transcript = load_json(cache / "transcript.json")
    state = load_json(cache.parent.parent / "state.json")
    settings = state.get("settings") or load_json(cache / "report.json")["settings"]
    analyzed = [{"sentences": part, "analysis": load_json(cache / f"window_{i:04d}.json")}
                for i, part in enumerate(windows(transcript["sentences"]))]
    blocks = merge_windows(analyzed, max(900, settings["max_duration_sec"] + 300))
    seen, sizes, planned = set(), [], []
    for block in blocks:
        unique = {s["id"]: s for window in block for s in window["sentences"]}
        sentences = sorted(unique.values(), key=lambda s: s["start"])
        builder = lambda part: clip_prompt(part, block, settings)
        groups = fit_sentence_groups(sentences, builder, overlap_seconds=settings["max_duration_sec"])
        planned.append(groups)
        for group in groups:
            seen.update(s["id"] for s in group)
            sizes.append(byte_size(builder(group)))
    if seen != {s["id"] for s in transcript["sentences"]}:
        raise ValueError("The prompt plan lost transcript sentences")
    result = {"windows_cached": len(analyzed), "topic_blocks": len(blocks), "planned_parts": len(sizes),
              "source_sentences_covered": len(seen), "largest_initial_prompt_bytes": max(sizes, default=0),
              "initial_budget_bytes": INITIAL_PROMPT_BYTES, "hard_budget_bytes": MAX_PROMPT_BYTES}
    block = blocks[args.block]
    prior_attempt = load_json(cache / f"block_{args.block:04d}_attempts.json")[0]
    group = planned[args.block][0]
    prompt = clip_prompt(group, block, settings)
    repair = repair_feedback(prompt, prior_attempt["reply"], prior_attempt["error"],
                             "Исправь длительность; если законченного клипа нет, верни clips=[].")
    result["failed_block_repair_bytes"] = byte_size(prompt + repair)
    if args.replay_invalid:
        audit_path = cache / f"block_{args.block:04d}_attempts.json"
        if len(load_json(audit_path)) != 3:
            raise ValueError("Replay requires exactly three saved attempts; no requests were sent")
        selection = collect_clips(settings, prompt, group, transcript["duration_sec"], audit_path,
                                  lambda message: None, f"Блок {args.block + 1}")
        save_json(cache / "selection_fix_probe.json", selection)
        allowed = {s["id"]: s for s in group}
        result["replayed_selection"] = {"validation_exhausted": selection["validation_exhausted"],
            "rejected_count": len(selection["rejected_candidates"]),
            "valid_candidates": [{"start_id": c["start_id"], "end_id": c["end_id"],
                                  "duration_sec": round(allowed[c["end_id"]]["end"] - allowed[c["start_id"]]["start"], 2)}
                                 for c in selection["clips"]]}
    if args.live:
        allowed = {s["id"]: s for s in group}

        def validate(value):
            if not isinstance(value, dict) or not isinstance(value.get("clips"), list):
                raise ValueError("Expected clips array")
            for candidate in value["clips"]:
                if candidate["start_id"] not in allowed or candidate["end_id"] not in allowed:
                    raise ValueError("Unknown sentence ID")
                start, end = allowed[candidate["start_id"]]["start"], allowed[candidate["end_id"]]["end"]
                validate_interval(start, end, transcript["duration_sec"], settings["min_duration_sec"], settings["max_duration_sec"])

        result["live_reply"] = ask(settings, prompt + repair, CLIP_SCHEMA, validate,
                                   audit_path=cache / "context_fix_probe_attempts.json")
    if args.resume:
        import traceback
        from app_backend import run_analysis
        from job_store import DATA

        with (cache / "resume_analysis.log").open("a", encoding="utf-8", buffering=1) as stream:
            with redirect_stdout(stream), redirect_stderr(stream):
                try:
                    context = run_analysis(settings, lambda message: print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True))
                    result["resumed_report_path"] = str(context["report_path"])
                    result["resumed_candidate_count"] = len(context["report"]["candidates"])
                    result["analysis_complete"] = context["report"].get("analysis_complete", True)
                    result["analysis_warnings"] = context["report"].get("analysis_warnings", [])
                    save_json(DATA / "last_review.json", {"path": str(context["report_path"])})
                    save_json(cache / "resume_result.json", result)
                    print("Resume completed; awaiting human review.", flush=True)
                except Exception:
                    traceback.print_exc()
                    raise
    save_json(cache / "context_fix_probe.json", result)
    import sys
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
