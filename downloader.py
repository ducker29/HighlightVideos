"""Download source media without cropping or changing its resolution."""
import shutil
import subprocess
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from job_store import DATA, load_json, save_json


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    info = json.loads(result.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None or not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise ValueError("В источнике должны быть видео и аудио.")
    duration = float(info["format"]["duration"])
    sar = video.get("sample_aspect_ratio", "1:1")
    num, den = (float(x) for x in (sar if sar not in ("N/A", "0:1") else "1:1").split(":"))
    ratio = video["width"] * num / (video["height"] * den)
    if abs(ratio - 16 / 9) > 0.02:
        raise ValueError(f"Источник имеет формат {ratio:.3f}, ожидается 16:9. Автокроп отключён.")
    return {"duration_sec": duration, "width": video["width"], "height": video["height"],
            "sample_aspect_ratio": sar, "frame_rate": video.get("avg_frame_rate"), "streams": info["streams"]}


def video_id_from_url(url):
    """Локальный разбор YouTube-ссылки без сети; None для нестандартных форм."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    query = parse_qs(parts.query)
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        path = parts.path
        if path == "/watch" and query.get("v"):
            candidate = query["v"][0]
        elif len(path) >= 2 and path[1:2] == "/":
            segments = [s for s in path.split("/") if s]
            if segments and segments[0] in {"embed", "live", "shorts", "v"}:
                candidate = segments[1] if len(segments) > 1 else None
        else:
            candidate = None
        if candidate:
            if len(candidate) == 11 and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in candidate):
                return candidate
        return None
    if host == "youtu.be":
        candidate = next((s for s in parts.path.split("/") if s), None)
        if candidate and len(candidate) == 11 and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in candidate):
            return candidate
    return None


def download(url, log):
    host = (urlparse(url).hostname or "").lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Укажите ссылку на YouTube VOD.")
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} не найден в PATH.")
    local_id = video_id_from_url(url)
    if local_id is not None:
        job_dir = DATA / local_id
        source = job_dir / "source.mkv"
        manifest_path = job_dir / "source.json"
        if source.exists() and manifest_path.exists():
            try:
                manifest = load_json(manifest_path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Локальный манифест повреждён: {manifest_path}. "
                                   f"Удалите файл вручную, чтобы скачать заново.") from exc
            if manifest.get("video_id") != local_id:
                raise RuntimeError(f"Локальный источник {local_id} не совпадает с манифестом "
                                   f"({manifest.get('video_id')!r}). Проверьте файл вручную.")
            try:
                manifest["media"] = probe(source)
            except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Локальный источник повреждён: {source} ({exc}). "
                                   f"Удалите файл вручную, чтобы скачать заново.") from exc
            log("Найден ранее скачанный источник — YouTube не запрашивается.")
            return job_dir, manifest
    from yt_dlp import YoutubeDL
    log("Получение метаданных YouTube…")
    base = {"noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False, "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024, "retries": 3, "extractor_retries": 2}
    # This PC has Node, not Deno. yt-dlp only enables Deno by default.
    if shutil.which("node"):
        base["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    with YoutubeDL(base) as ydl:
        meta = ydl.extract_info(url, download=False)
    if meta.get("is_live") or meta.get("live_status") == "is_upcoming":
        raise ValueError("Нужна завершённая запись, а не текущая или запланированная трансляция.")
    video_id = meta["id"]
    if not video_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in video_id):
        raise ValueError("Некорректный ID видео.")
    job_dir = DATA / video_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "source.mkv"
    manifest_path = job_dir / "source.json"
    if source.exists() and manifest_path.exists():
        manifest = load_json(manifest_path)
        manifest["media"] = probe(source)
        log("Найден ранее скачанный источник.")
        return job_dir, manifest

    last_bucket = [-1]

    def progress(event):
        if event["status"] == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            if total:
                percent = int(100 * event.get("downloaded_bytes", 0) / total)
                if percent // 10 != last_bucket[0]:
                    last_bucket[0] = percent // 10
                    log(f"Скачивание потока: {percent}%")

    options = dict(base, format="bv*+ba/b", merge_output_format="mkv",
                   outtmpl=str(job_dir / "source.%(ext)s"), progress_hooks=[progress],
                   postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}])
    log("Скачивание исходного видео и аудио…")
    with YoutubeDL(options) as ydl:
        ydl.download([url])
    if not source.exists():
        raise RuntimeError("yt-dlp не создал ожидаемый source.mkv.")
    manifest = {"video_id": video_id, "url": url, "title": meta.get("title", video_id),
                "upload_date": meta.get("upload_date"), "source_path": str(source.resolve()),
                "media": probe(source)}
    save_json(manifest_path, manifest)
    return job_dir, manifest
