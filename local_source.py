"""Register a local source in the existing pipeline without copying its video."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

from downloader import probe
from job_store import DATA, load_json, save_json


def local_source_path(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Укажите путь к локальному видеофайлу.")
    value = value.strip()
    # Windows Explorer's Copy as path surrounds the pathname with quotes.
    if len(value) > 1 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"Укажите видеофайл, а не папку: {path}")
    except OSError as exc:
        raise ValueError(f"Локальный видеофайл недоступен: {value} ({exc})") from exc
    return path


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def content_fingerprint(path):
    before = _stamp(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if _stamp(path) != before:
        raise ValueError("Видео изменилось во время чтения. Дождитесь окончания записи файла и повторите анализ.")
    return {"sha256": digest.hexdigest(), "size_bytes": before[0]}


def prepare_local_source(value, log):
    path = local_source_path(value)
    for command in ("ffmpeg", "ffprobe"):
        if not shutil.which(command):
            raise RuntimeError(f"{command} не найден в PATH.")
    log(f"Локальный источник (без скачивания и копирования): {path}")
    before = _stamp(path)
    try:
        media = probe(path)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        raise ValueError(f"Не удалось проверить локальное видео: {path}\n{exc}") from exc
    log("Проверка содержимого файла для отдельного кеша (SHA-256)…")
    fingerprint = content_fingerprint(path)
    if _stamp(path) != before:
        raise ValueError("Видео изменилось во время подготовки. Повторите анализ после окончания записи.")
    identity = os.path.normcase(str(path)) + "\0" + fingerprint["sha256"]
    video_id = "local_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    job_dir = DATA / video_id
    manifest_path = job_dir / "source.json"
    if manifest_path.exists():
        manifest = load_json(manifest_path)
        if (not isinstance(manifest, dict) or manifest.get("video_id") != video_id
                or manifest.get("source_type") != "local"
                or manifest.get("source_fingerprint") != fingerprint
                or not isinstance(manifest.get("source_path"), str)
                or os.path.normcase(manifest.get("source_path", "")) != os.path.normcase(str(path))):
            raise ValueError(f"Манифест локального источника не соответствует файлу: {manifest_path}")
        log("Найден кеш этой версии локального видео.")
        return job_dir, manifest
    manifest = {"video_id": video_id, "source_type": "local", "url": None,
                "title": path.stem, "upload_date": None, "source_path": str(path),
                "source_fingerprint": fingerprint, "media": media}
    save_json(manifest_path, manifest)
    return job_dir, manifest


def verify_local_source(manifest, log):
    """Old reports must not silently render a different file at the saved path."""
    if manifest.get("source_type") != "local":
        return
    path = local_source_path(manifest["source_path"])
    log("Проверка локального исходника перед рендером (SHA-256)…")
    if content_fingerprint(path) != manifest.get("source_fingerprint"):
        raise ValueError("Локальное исходное видео изменилось после анализа. "
                         "Верните исходный файл или выполните новый анализ; старый отчёт сохранён.")
