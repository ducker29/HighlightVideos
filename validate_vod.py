"""Resumable real-world validation on the complete VOD and its first 30 minutes."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import time
import traceback

from job_store import DATA, ROOT, load_json, save_json, validate_interval

VIDEO_ID = "AVbz4UzkeTw"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
RUN_DIR = DATA / "validation_runs" / f"{VIDEO_ID}_first30"


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def run(stage):
    if stage == "download":
        from downloader import download
        job_dir, manifest = download(URL, log)
        return {"job_dir": str(job_dir), "source": manifest,
                "size_bytes": Path(manifest["source_path"]).stat().st_size}
    if stage == "model":
        from faster_whisper.utils import download_model
        log("Downloading large-v3 weights into project cache…")
        path = download_model("large-v3", cache_dir=str(DATA / "models" / "whisper"))
        return {"model": "large-v3", "path": path}
    if stage == "analyze":
        from analyzer import analyze
        from transcriber import transcribe
        from app_backend import open_report, release_llm_for_asr
        from editorial_review import review_report
        source = load_json(DATA / VIDEO_ID / "source.json")
        job_dir = DATA / VIDEO_ID / "validation_first30"
        job_dir.mkdir(parents=True, exist_ok=True)
        save_json(job_dir / "source.json", source)
        settings = load_json(ROOT / "config.json")
        settings.update(youtube_url=URL, asr_model="large-v3", device="cuda", min_duration_sec=120, max_duration_sec=300)
        # Only unload the model selected by this application, not other resident models.
        transcript_path = job_dir / "transcript.json"
        expected = {"asr_model": "large-v3", "language": settings["language"], "device": "cuda",
                    "schema": 2, "start_sec": 0.0, "end_sec": 1800.0}
        cache_hit = transcript_path.exists() and load_json(transcript_path).get("settings") == expected
        if not cache_hit:
            log("Freeing this application's LLM VRAM before large-v3 ASR…")
            release_llm_for_asr(settings, log)
        start = time.perf_counter()
        transcript = transcribe(job_dir, source, settings, log, start_sec=0, end_sec=1800)
        asr_elapsed = time.perf_counter() - start
        words = [w for segment in transcript["segments"] for w in segment["words"]]
        if not words or any(not 0 <= w["start"] <= w["end"] <= 1800.5 for w in words):
            raise ValueError("Invalid word timestamps in 30-minute ASR")
        log(f"ASR done: {len(words)} words; {len(transcript['sentences'])} sentences; {asr_elapsed:.2f} seconds (cache included if reused)")
        if not cache_hit or not (RUN_DIR / "asr_result.json").exists():
            save_json(RUN_DIR / "asr_result.json", {"model": "large-v3", "range_start": 0, "range_end": 1800,
                      "elapsed_sec": round(asr_elapsed, 2), "cache_hit": cache_hit, "word_count": len(words),
                      "sentence_count": len(transcript["sentences"]), "transcript": str(job_dir / "transcript.json")})
        start = time.perf_counter()
        report_path = analyze(job_dir, transcript, settings, log)
        report_path = review_report(report_path, log, model=settings.get("editorial_model"))
        context = open_report(report_path)
        for candidate in context["report"]["candidates"]:
            validate_interval(candidate["start"], candidate["end"], 1800, 120, 300)
            if candidate["status"] != "pending":
                log(f"Existing review decision preserved: {candidate['candidate_id']} ({candidate['status']})")
        result = {"report_path": str(report_path.resolve()), "candidate_count": len(context["report"]["candidates"]),
                  "analysis_elapsed_sec": round(time.perf_counter() - start, 2),
                  "candidates": context["report"]["candidates"]}
        save_json(DATA / "last_review.json", {"path": str(report_path.resolve())})
        return result
    raise ValueError(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("download", "model", "analyze"), required=True)
    parser.add_argument("--wait", action="store_true", help="Wait for download/model workers before analysis")
    args = parser.parse_args()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    state = RUN_DIR / f"{args.stage}_state.json"
    if sys.stdout is not None:
        print(f"Stage {args.stage}; progress log: {RUN_DIR / (args.stage + '.log')}", flush=True)
    # A detached worker can outlive a terminal timeout; every stage has its own log/state.
    with (RUN_DIR / f"{args.stage}.log").open("a", encoding="utf-8", buffering=1) as stream:
        sys.stdout = stream
        sys.stderr = stream
        started = time.perf_counter()
        save_json(state, {"stage": args.stage, "status": "running", "pid": os.getpid(), "started_at": datetime.now().isoformat()})
        try:
            log(f"Starting stage: {args.stage}")
            if args.stage == "analyze" and args.wait:
                log("Waiting for complete video and large-v3 downloads…")
                deadline = time.monotonic() + 7200
                while True:
                    states = [load_json(RUN_DIR / f"{stage}_state.json") for stage in ("download", "model")]
                    if any(state["status"] == "failed" for state in states):
                        raise RuntimeError(f"Prerequisite failed: {states}")
                    if all(state["status"] == "completed" for state in states):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Downloads did not finish within 2 hours")
                    time.sleep(5)
            result = run(args.stage)
            save_json(RUN_DIR / f"{args.stage}_result.json", result)
            save_json(state, {"stage": args.stage, "status": "completed", "elapsed_sec": round(time.perf_counter() - started, 2)})
            log("Completed successfully")
        except Exception as exc:
            traceback.print_exc()
            save_json(state, {"stage": args.stage, "status": "failed", "error": str(exc)})
            raise SystemExit(1)


if __name__ == "__main__":
    main()
