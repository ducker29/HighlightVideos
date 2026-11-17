"""YouTube source cache, legacy combined downloads and explicit quality downloads."""
import math
import copy
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from downloader import probe, video_id_from_url
from job_store import load_json, save_json


class SourcePolicyError(ValueError):
    """Invalid identity, live broadcast or damaged cache, not a transport failure."""


def combined_quality_key(height, fps=0, bitrate=0):
    """1080p, nearest higher resolution, then highest lower resolution."""
    height = int(height)
    if height <= 0:
        raise ValueError("Неизвестное разрешение потока.")
    tier = 0 if height == 1080 else 1 if height > 1080 else 2
    return tier, height if tier == 1 else -height, -(fps or 0), -(bitrate or 0)


def select_combined_format(formats):
    formats = [f for f in formats if f.get("vcodec") not in (None, "none")
               and f.get("acodec") not in (None, "none") and not f.get("has_drm")
               and type(f.get("height")) in (int, float) and math.isfinite(f["height"]) and f["height"] > 0
               and isinstance(f.get("format_id"), str)
               and re.fullmatch(r"[A-Za-z0-9_.-]+", f["format_id"])]
    if not formats:
        raise RuntimeError("Нет совмещённого потока с видео и звуком. Раздельная загрузка отключена.")
    return min(formats, key=lambda f: combined_quality_key(f["height"], f.get("fps"), f.get("tbr")))


def select_progressive_stream(streams):
    streams = [s for s in streams if s.is_progressive and re.fullmatch(r"\d+p", s.resolution or "")]
    if not streams:
        raise RuntimeError("pytubefix: нет готового потока с видео и звуком. Раздельная загрузка отключена.")
    return min(streams, key=lambda s: combined_quality_key(int(s.resolution[:-1]), s.fps, s.bitrate))


def _job_dir(root, video_id, expected_id=None):
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise SourcePolicyError("Некорректный ID видео.")
    if expected_id is not None and video_id != expected_id:
        raise SourcePolicyError("ID полученного видео не совпадает с YouTube-ссылкой.")
    return Path(root) / video_id


def _cached_source(root, video_id, log):
    job_dir = _job_dir(root, video_id)
    source = job_dir / "source.mkv"
    manifest_path = job_dir / "source.json"
    if not source.exists() and not manifest_path.exists():
        if (job_dir / "sources").exists() and any((job_dir / "sources").glob("*.mkv")):
            raise SourcePolicyError("Источники существуют без манифеста; автоматическая перезапись отключена.")
        return None
    if not manifest_path.exists():
        raise SourcePolicyError(f"Источник уже существует без манифеста: {source}. "
                                "Проверьте файлы вручную; автоматическая перезапись отключена.")
    try:
        manifest = load_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("video_id") != video_id:
            raise ValueError("ID источника не совпадает с манифестом.")
        source = Path(manifest.get("source_path") or source)
        manifest["media"] = probe(source)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SourcePolicyError(f"Сохранённый источник или манифест повреждён: {source} ({exc}). "
                                "Проверьте файлы вручную; автоматическая перезапись отключена.") from exc
    if source == job_dir / "source.mkv":
        if migrate_legacy_source(job_dir, log):
            manifest = load_json(manifest_path)
    log("Найден ранее скачанный источник — YouTube не запрашивается. Его разрешение сохраняется.")
    return job_dir, manifest


def _publish_source(staged, meta, job_dir, backend, selection, log):
    media = probe(staged)
    height = int(media["height"])
    sources = Path(job_dir) / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    source = quality_path(job_dir, height)
    if source.exists():
        raise SourcePolicyError(f"Источник появился во время загрузки: {source}. Перезапись отключена.")
    staged.rename(source)
    manifest = {"video_id": meta["id"], "url": meta["url"], "title": meta.get("title") or meta["id"],
                "upload_date": meta.get("upload_date"), "active_height": height,
                "source_path": str(source.resolve()), "media": media, "qualities": {},
                "download_info": {"backend": backend, "combined_stream": True,
                                  "selection_policy": "1080_then_nearest_higher_then_best_lower", **selection}}
    manifest["qualities"][str(height)] = {"format_id": selection.get("format_id", "unknown"),
                                          "fps": int(selection.get("fps") or 0), "tbr": 0,
                                          "adaptive": False, "has_audio": True, "backend": backend}
    save_json(Path(job_dir) / "source.json", manifest)
    log(f"Источник готов: {media['width']}×{height}, видео со звуком, загрузчик {backend}.")
    return job_dir, manifest


def _download_ytdlp(url, expected_id, log, root):
    from yt_dlp import YoutubeDL
    log("Получение метаданных YouTube…")
    base = {"noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False, "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024, "retries": 3, "extractor_retries": 2,
            "format": "best[vcodec!=none][acodec!=none]", "skip_unavailable_fragments": False}
    if shutil.which("node"):
        base["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    with YoutubeDL(base) as ydl:
        meta = ydl.extract_info(url, download=False)
    if meta.get("is_live") or meta.get("live_status") == "is_upcoming":
        raise SourcePolicyError("Нужна завершённая запись, а не текущая или запланированная трансляция.")
    video_id = meta["id"]
    job_dir = _job_dir(root, video_id, expected_id)
    cached = _cached_source(root, video_id, log)
    if cached:
        return cached
    selected = select_combined_format(meta.get("formats") or [])
    height = int(selected["height"])
    log(f"yt-dlp: выбран готовый поток {height}p с видео и звуком (приоритет 1080p → выше → ниже).")
    job_dir.mkdir(parents=True, exist_ok=True)
    last_bucket = [-1]

    def progress(event):
        if event["status"] == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            if total:
                percent = int(100 * event.get("downloaded_bytes", 0) / total)
                if percent // 10 != last_bucket[0]:
                    last_bucket[0] = percent // 10
                    log(f"Скачивание потока: {percent}%")

    with tempfile.TemporaryDirectory(prefix="download_ytdlp_", dir=job_dir) as directory:
        options = dict(base, format=str(selected["format_id"]),
                       outtmpl=str(Path(directory) / "source.%(ext)s"), progress_hooks=[progress],
                       postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}])
        log("Скачивание одного потока с видео и звуком…")
        with YoutubeDL(options) as ydl:
            ydl.download([url])
        staged = Path(directory) / "source.mkv"
        if not staged.exists():
            raise RuntimeError("yt-dlp не создал ожидаемый source.mkv.")
        return _publish_source(staged, {**meta, "url": url}, job_dir, "yt-dlp",
                               {"format_id": selected["format_id"], "selected_height": height}, log)


def _download_pytubefix(url, expected_id, log, root):
    try:
        from pytubefix import YouTube
    except ImportError as exc:
        raise RuntimeError("Резервный pytubefix не установлен. Установите зависимости из requirements.txt.") from exc
    last_bucket = [-1]

    def progress(stream, chunk, remaining):
        total = stream.filesize
        if total:
            percent = int(100 * (total - remaining) / total)
            if percent // 10 != last_bucket[0]:
                last_bucket[0] = percent // 10
                log(f"pytubefix: скачивание потока {percent}%")

    video = YouTube(url, on_progress_callback=progress)
    job_dir = _job_dir(root, video.video_id, expected_id)
    details = video.vid_info.get("videoDetails", {})
    if (details.get("isLive") or details.get("isUpcoming")
            or video.vid_info.get("playabilityStatus", {}).get("status") == "LIVE_STREAM_OFFLINE"):
        raise SourcePolicyError("Нужна завершённая запись, а не текущая или запланированная трансляция.")
    cached = _cached_source(root, video.video_id, log)
    if cached:
        return cached
    stream = select_progressive_stream(video.streams.filter(progressive=True))
    log(f"pytubefix: выбран готовый поток {stream.resolution} с видео и звуком.")
    job_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download_pytubefix_", dir=job_dir) as directory:
        result = stream.download(output_path=directory, filename="combined." + stream.subtype,
                                 skip_existing=False, timeout=30, max_retries=3)
        if not result or not Path(result).is_file():
            raise RuntimeError("pytubefix не создал видеофайл.")
        staged = Path(directory) / "source.mkv"
        # One input already contains both tracks. Remux only; no merging or encoding.
        subprocess.run(["ffmpeg", "-v", "error", "-n", "-i", str(result), "-map", "0:v:0", "-map", "0:a:0",
                        "-c", "copy", str(staged)], capture_output=True, check=True)
        return _publish_source(staged, {"id": video.video_id, "url": url, "title": video.title},
                               job_dir, "pytubefix", {"format_id": str(stream.itag),
                                                     "selected_height": int(stream.resolution[:-1])}, log)


def download(url, log, root):
    host = (urlparse(url).hostname or "").lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        raise ValueError("Укажите ссылку на YouTube VOD.")
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} не найден в PATH.")
    expected_id = video_id_from_url(url)
    if expected_id is not None:
        cached = _cached_source(root, expected_id, log)
        if cached:
            return cached
    try:
        job_dir, manifest = _download_ytdlp(url, expected_id, log, root)
    except SourcePolicyError:
        raise
    except Exception as primary_error:
        log(f"yt-dlp не завершил загрузку: {primary_error}")
        log("Переход к резервному pytubefix. Только готовые потоки с видео и звуком.")
        try:
            job_dir, manifest = _download_pytubefix(url, expected_id, log, root)
        except SourcePolicyError:
            raise
        except Exception as fallback_error:
            raise RuntimeError(f"Не удалось скачать видео ни одним загрузчиком.\nyt-dlp: {primary_error}\n"
                                f"pytubefix: {fallback_error}") from fallback_error
    return job_dir, manifest


def quality_path(job_dir, height):
    return Path(job_dir) / "sources" / f"{int(height)}p.mkv"


def ensure_quality_manifest(manifest, job_dir, log):
    """Дописывает новые поля схемы (sources/, active_height, qualities) в существующий манифест."""
    job_dir = Path(job_dir)
    if "active_height" not in manifest:
        height = int(probe(Path(manifest["source_path"]))["height"])
        manifest["active_height"] = height
    manifest.setdefault("qualities", {})
    return manifest


def migrate_legacy_source(job_dir, log):
    """Копирует старый источник в новую схему, сохраняя путь для старых отчётов."""
    job_dir = Path(job_dir)
    legacy = job_dir / "source.mkv"
    manifest_path = job_dir / "source.json"
    if not legacy.exists():
        return None
    try:
        media = probe(legacy)
        height = int(media["height"])
    except Exception as exc:
        log(f"Миграция пропущена, источник не читается: {exc}")
        return None
    target = quality_path(job_dir, height)
    created = False
    try:
        manifest = load_json(manifest_path)
        if target.exists():
            raise SourcePolicyError(f"Целевой файл уже существует: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        created = True
        shutil.copy2(legacy, target)
        manifest["active_height"] = height
        manifest["source_path"] = str(target.resolve())
        manifest.setdefault("qualities", {})
        manifest["qualities"][str(height)] = {
            "format_id": "legacy", "fps": 0, "tbr": 0, "adaptive": False, "has_audio": True}
        manifest["media"] = media
        save_json(manifest_path, manifest)
    except Exception as exc:
        if created:
            target.unlink(missing_ok=True)
        log(f"Миграция источника не удалась (файл не тронут): {exc}")
        return None
    # Keep the legacy path readable for previously saved analysis reports.
    log(f"Источник добавлен в sources/{height}p.mkv; старый путь сохранён для отчётов.")
    return target


def list_available_qualities(url, log):
    """Группирует потоки YouTube по высоте: {height: {"format_id","fps","tbr","adaptive","has_audio"}}."""
    from yt_dlp import YoutubeDL
    log("Запрос доступных качеств…")
    base = {"noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False, "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024}
    if shutil.which("node"):
        base["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    with YoutubeDL(base) as ydl:
        meta = ydl.extract_info(url, download=False)
    if meta.get("is_live") or meta.get("live_status") == "is_upcoming":
        raise SourcePolicyError("Нужна завершённая запись, а не текущая или запланированная трансляция.")
    qualities = {}
    for f in meta.get("formats") or []:
        if f.get("vcodec") in (None, "none") or f.get("has_drm"):
            continue
        h = f.get("height")
        if type(h) is not int or h <= 0:
            continue
        fmt_id = f.get("format_id")
        if not isinstance(fmt_id, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", fmt_id):
            continue
        fps = f.get("fps") or 0
        tbr = f.get("tbr") or 0
        has_audio = f.get("acodec") not in (None, "none")
        adaptive = not has_audio
        existing = qualities.get(h)
        # При равных fps/tbr комбинированный (аудио в одном файле) предпочтительнее.
        if existing is None or (fps, tbr, has_audio) > (existing["fps"], existing["tbr"], existing["has_audio"]):
            qualities[h] = {"format_id": fmt_id, "fps": fps, "tbr": tbr,
                            "adaptive": adaptive, "has_audio": has_audio}
    if not qualities:
        raise RuntimeError("Не найдено ни одного доступного видеокачества.")
    return dict(sorted(qualities.items(), reverse=True))


def download_quality(url, expected_id, height, root, log, on_progress=None):
    """Скачивает bestvideo[height=H][vcodec!=none]+bestaudio в sources/<H>p.mkv."""
    from yt_dlp import YoutubeDL
    if type(height) is not int or height <= 0:
        raise ValueError("Некорректная высота качества.")
    video_id = video_id_from_url(url)
    if expected_id is not None and video_id != expected_id:
        raise SourcePolicyError("ID полученного видео не совпадает с YouTube-ссылкой.")
    if video_id is None:
        raise SourcePolicyError("Не удалось определить ID видео из ссылки.")
    job_dir = _job_dir(root, video_id, expected_id)
    sources = job_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    target = quality_path(job_dir, height)
    if target.exists():
        media = probe(target)
        _validate_quality(media, height)
        return str(target.resolve()), media
    if (job_dir / "source.mkv").exists():
        migrate_legacy_source(job_dir, log)
        if target.exists():
            media = probe(target)
            _validate_quality(media, height)
            return str(target.resolve()), media
    base = {"noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False, "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024, "retries": 3, "extractor_retries": 2,
            "format": f"bestvideo[height={height}][vcodec!=none]+bestaudio/best[height={height}][vcodec!=none][acodec!=none]",
            "merge_output_format": "mkv", "skip_unavailable_fragments": False,
            "progress_hooks": [on_progress] if on_progress else []}
    if shutil.which("node"):
        base["js_runtimes"] = {"node": {"path": shutil.which("node")}}
    log(f"Скачивание качества {height}p…")
    with tempfile.TemporaryDirectory(prefix="quality_", dir=job_dir) as directory:
        staged = Path(directory) / "source.mkv"
        options = dict(base, outtmpl=str(Path(directory) / "source.%(ext)s"),
                       postprocessors=[{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}])
        with YoutubeDL(options) as ydl:
            meta = ydl.extract_info(url, download=False)
            if meta.get("id") != video_id or meta.get("is_live") or meta.get("live_status") == "is_upcoming":
                raise SourcePolicyError("Неверный ID или незавершённая трансляция.")
            ydl.download([url])
        media = probe(staged)
        _validate_quality(media, height)
        if target.exists():
            raise SourcePolicyError(f"Источник появился во время загрузки: {target}")
        staged.rename(target)
    return str(target.resolve()), media


def _validate_quality(media, height):
    if media.get("height") != height or not {"video", "audio"}.issubset(
            {s.get("codec_type") for s in media.get("streams", [])}):
        raise SourcePolicyError(f"Ожидалось видео {height}p со звуком.")


def _merge_quality_entry(height, entry, backend, fmt_id):
    entry = entry or {}
    return {"format_id": fmt_id, "fps": int(entry.get("fps") or 0), "tbr": int(entry.get("tbr") or 0),
            "adaptive": bool(entry.get("adaptive")), "has_audio": bool(entry.get("has_audio")),
            "backend": backend}


def publish_quality(manifest, height, media, backend, fmt_id, manifest_path=None, activate=True):
    """Обновляет манифест: qualities[height], active_height, source_path, media."""
    h = int(height)
    if manifest_path is None:
        source = Path(manifest["source_path"])
        job_dir = source.parent.parent if source.parent.name == "sources" else source.parent
        manifest_path = job_dir / "source.json"
    job_dir = Path(manifest_path).parent
    target = quality_path(job_dir, h)
    actual = probe(target)
    _validate_quality(actual, h)
    updated = copy.deepcopy(manifest)
    entry = _merge_quality_entry(h, updated.get("qualities", {}).get(str(h)), backend, fmt_id)
    entry["has_audio"] = True
    updated.setdefault("qualities", {})[str(h)] = entry
    if activate:
        updated.update(active_height=h, source_path=str(target.resolve()), media=actual)
    else:
        updated.setdefault("active_height", None)
        updated.setdefault("source_path", str(target.resolve()))
        updated.setdefault("media", actual)
    save_json(manifest_path, updated)
    manifest.clear()
    manifest.update(updated)
    return manifest


def set_active_quality(manifest_path, height):
    """Переключает active_height; источник должен существовать на диске."""
    manifest = load_json(manifest_path)
    manifest = ensure_quality_manifest(manifest, Path(manifest_path).parent, lambda _: None)
    h = int(height)
    target = quality_path(Path(manifest_path).parent, h)
    if not target.exists():
        raise FileNotFoundError(f"Файл качества {h}p не найден: {target}")
    manifest["active_height"] = h
    manifest["source_path"] = str(target.resolve())
    media = probe(target)
    _validate_quality(media, h)
    manifest["media"] = media
    manifest.setdefault("qualities", {}).setdefault(str(h), {"format_id": "unknown", "fps": 0, "tbr": 0,
                                                             "adaptive": False, "has_audio": True})
    save_json(manifest_path, manifest)
    return manifest
