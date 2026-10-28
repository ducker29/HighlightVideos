"""Download VOD audio and transcribe only a short range, never a final highlight."""
import argparse
import json
import shutil
import subprocess
import time

from job_store import DATA, save_json
from transcriber import check_runtime, transcribe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://www.youtube.com/watch?v=AVbz4UzkeTw")
    parser.add_argument("--start", type=float, default=300)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--model", default="small")
    args = parser.parse_args()
    if args.start < 0 or not 5 <= args.duration <= 180:
        parser.error("start must be >= 0; duration must be 5–180 seconds")
    settings = {"device": "cuda", "asr_model": args.model, "language": "ru"}
    check_runtime(settings)
    job_dir = DATA / "gpu_smoke" / f"start_{args.start:g}_duration_{args.duration:g}"
    job_dir.mkdir(parents=True, exist_ok=True)
    audio = job_dir / "audio.wav"
    provenance = job_dir / "source.json"
    if provenance.exists():
        from job_store import load_json
        if load_json(provenance)["url"] != args.url:
            raise ValueError("This test directory belongs to another URL. Use a different start/duration.")
    save_json(provenance, {"url": args.url, "source_start_sec": args.start, "duration_sec": args.duration})
    if not audio.exists():
        from yt_dlp import YoutubeDL
        options = {"format": "140/bestaudio/best", "noplaylist": True, "socket_timeout": 20,
                   "retries": 2, "extractor_retries": 1, "outtmpl": str(job_dir / "sample.%(ext)s"),
                   "http_chunk_size": 10 * 1024 * 1024}
        if shutil.which("node"):
            options["js_runtimes"] = {"node": {"path": shutil.which("node")}}
        print(f"Downloading audio only; extracting range {args.start:g}–{args.start + args.duration:g} seconds…", flush=True)
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(args.url, download=True)
            source = info["requested_downloads"][0]["filepath"]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(args.start), "-i", source, "-t", str(args.duration),
                        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)], check=True)
    duration = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio)], text=True).strip())
    manifest = {"video_id": "AVbz4UzkeTw_gpu_smoke", "source_path": str(audio), "media": {"duration_sec": duration},
                "url": args.url, "source_start_sec": args.start}
    started = time.perf_counter()
    transcript = transcribe(job_dir, manifest, settings, lambda message: print(message, flush=True), force=True)
    elapsed = time.perf_counter() - started
    words = [word for segment in transcript["segments"] for word in segment["words"]]
    if not words:
        raise RuntimeError("No words returned by GPU ASR")
    if any(not 0 <= w["start"] <= w["end"] <= duration + 0.5 for w in words):
        raise RuntimeError("Word timestamps are outside the test audio")
    result = {"url": args.url, "source_start_sec": args.start, "audio_duration_sec": duration,
              "device": "cuda", "compute_type": "float16", "model": args.model,
              "elapsed_sec_including_model_load": round(elapsed, 2), "word_count": len(words),
              "sentence_count": len(transcript["sentences"]),
              "text": " ".join(s["text"].strip() for s in transcript["segments"])}
    save_json(job_dir / f"result_{args.model}.json", result)
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
