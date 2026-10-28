"""Compare constrained PASS 2 against saved problematic blocks; no ASR or render."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from analyzer import interval_iou, merge_windows, windows
from interval_selector import SELECTION_VERSION, select_intervals
from job_store import DATA, load_json, save_json, validate_interval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--blocks", type=int, nargs="+", default=[1, 8, 24], help="Zero-based block indices")
    args = parser.parse_args()
    source = args.cache.resolve()
    transcript = load_json(source / "transcript.json")
    settings = load_json(source / "report.json")["settings"]
    analyzed = [{"sentences": part, "analysis": load_json(source / f"window_{i:04d}.json")}
                for i, part in enumerate(windows(transcript["sentences"]))]
    blocks = merge_windows(analyzed, max(900, settings["max_duration_sec"] + 300))
    key = hashlib.sha256(json.dumps({"settings": settings, "blocks": args.blocks,
                                    "version": SELECTION_VERSION}, sort_keys=True).encode()).hexdigest()[:10]
    directory = DATA / "validation_runs" / f"intervals_v{SELECTION_VERSION}_{source.name}_{key}"
    directory.mkdir(parents=True, exist_ok=True)
    state_path = directory / "state.json"
    if sys.stdout is not None:
        print(f"Log: {directory / 'run.log'}", flush=True)
    with (directory / "run.log").open("a", encoding="utf-8", buffering=1) as stream:
        with redirect_stdout(stream), redirect_stderr(stream):
            try:
                save_json(state_path, {"status": "running", "blocks": args.blocks})
                results = []
                all_candidates = []
                prior_results = (load_json(directory / "results.json").get("results", [])
                                 if (directory / "results.json").exists() else [])
                for index in args.blocks:
                    print(f"[{datetime.now():%H:%M:%S}] Testing block {index + 1}/{len(blocks)}", flush=True)
                    part_dir = directory / f"block_{index:04d}"
                    cache_hit = (part_dir / "selection.json").exists()
                    requests_before = sum(len(load_json(p)) for p in part_dir.glob("*_attempts.json"))
                    began = time.perf_counter()
                    selected = select_intervals(blocks[index], transcript, settings, part_dir,
                                                lambda text: print(f"[{datetime.now():%H:%M:%S}] {text}", flush=True))
                    elapsed = time.perf_counter() - began
                    for candidate in selected["candidates"]:
                        validate_interval(candidate["start"], candidate["end"], transcript["duration_sec"],
                                          settings["min_duration_sec"], settings["max_duration_sec"])
                        if candidate["status"] != "pending":
                            raise ValueError("Probe must never approve candidates")
                        all_candidates.append(dict(candidate, block_id=index))
                    old_path = source / f"block_{index:04d}.json"
                    old = load_json(old_path) if old_path.exists() else {}
                    audits = [load_json(p) for p in part_dir.glob("*_attempts.json")]
                    prior = next((r for r in prior_results if r["block_index"] == index), {})
                    deduplicated = []
                    for candidate in sorted(selected["candidates"], key=lambda c: c["score"], reverse=True):
                        if not any(interval_iou(candidate, other) >= 0.35 for other in deduplicated):
                            deduplicated.append(candidate)
                    result = {"block_index": index, "old_validation_exhausted": old.get("validation_exhausted"),
                              "old_invalid_proposals": len(old.get("rejected_candidates", [])),
                              "old_candidates": len(old.get("clips", [])),
                              "new_candidates": len(selected["candidates"]), "new_complete": selected["complete"],
                              "new_after_iou": len(deduplicated),
                              "new_invalid_replies": sum(not attempt["valid"] for audit in audits for attempt in audit),
                              "new_total_requests": sum(len(audit) for audit in audits),
                              "requests_this_run": sum(len(audit) for audit in audits) - requests_before,
                              "cache_hit": cache_hit,
                              "first_recorded_elapsed_sec": prior.get("first_recorded_elapsed_sec",
                                                                    prior.get("elapsed_sec_including_cache", round(elapsed, 2))),
                              "elapsed_sec_including_cache": round(elapsed, 2), "statistics": selected["statistics"],
                              "durations": [round(c["duration_sec"], 3) for c in selected["candidates"]]}
                    results.append(result)
                    save_json(directory / "results.json", {"source_cache": str(source), "results": results})
                    print(json.dumps(result, ensure_ascii=True), flush=True)
                ranked = []
                for candidate in sorted(all_candidates, key=lambda c: c["score"], reverse=True):
                    if not any(interval_iou(candidate, other) >= 0.35 for other in ranked):
                        ranked.append(candidate)
                for index, candidate in enumerate(ranked, 1):
                    candidate["candidate_id"] = f"c{index:03d}"
                    candidate["warnings"].append("Тест PASS 2 на выбранных блоках; отдельный редакционный проход ещё не выполнен.")
                label = "Тест PASS 2: только блоки " + ", ".join(str(i + 1) for i in args.blocks)
                preview_dir = source.parent / f"iv_probe_v{SELECTION_VERSION}_{key}"
                preview_path = preview_dir / "report.json"
                if not preview_path.exists():
                    save_json(preview_dir / "transcript.json", transcript)
                    save_json(preview_path, {"schema": 1, "video_id": transcript["video_id"], "settings": settings,
                              "signature": preview_dir.name, "selection_method": "intervals", "candidates": ranked,
                              "analysis_complete": False, "analysis_warnings": [label + ". Полный VOD новым методом не обработан."],
                              "analysis_scope": {"label": label, "block_indices": args.blocks}, "probe_only": True})
                save_json(directory / "results.json", {"source_cache": str(source), "results": results,
                          "preview_report_path": str(preview_path), "preview_candidates": len(ranked)})
                save_json(state_path, {"status": "completed", "blocks": args.blocks})
            except Exception as exc:
                traceback.print_exc()
                save_json(state_path, {"status": "failed", "error": str(exc)})
                raise


if __name__ == "__main__":
    main()
