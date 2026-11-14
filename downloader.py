"""Download source media without cropping or changing its resolution."""
import subprocess
import json
from urllib.parse import parse_qs, urlparse

from job_store import DATA


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
    # Lazy import keeps local-file probing independent of YouTube dependencies.
    from youtube_download import download as download_youtube
    return download_youtube(url, log, DATA)
