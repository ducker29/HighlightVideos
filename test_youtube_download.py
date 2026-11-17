import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from downloader import download, probe
from job_store import load_json, save_json
from youtube_download import (SourcePolicyError, download_quality, list_available_qualities, migrate_legacy_source,
                              publish_quality, quality_path, set_active_quality,
                              select_combined_format, select_progressive_stream)


ID = "l8qUNUTufU8"
URL = "https://www.youtube.com/watch?v=" + ID


def fmt(height, identifier=None, **changes):
    return {"height": height, "format_id": identifier or str(height), "vcodec": "avc1", "acodec": "mp4a",
            "fps": 30, "tbr": 1000, **changes}


def stream(height, **changes):
    return SimpleNamespace(resolution=f"{height}p", fps=30, bitrate=1000, is_progressive=True,
                           **changes)


class SelectionTests(unittest.TestCase):
    def test_1080_before_higher_before_lower(self):
        for heights, expected in (([720, 2160, 1080, 1440], 1080), ([720, 2160, 1440], 1440),
                                   ([480, 720, 360], 720), ([2160, 4320], 2160)):
            with self.subTest(heights=heights):
                self.assertEqual(select_combined_format([fmt(h) for h in heights])["height"], expected)
                self.assertEqual(select_progressive_stream([stream(h) for h in heights]).resolution, f"{expected}p")

    def test_never_select_video_only_audio_only_drm_or_composite(self):
        options = [fmt(1080, acodec="none"), fmt(2160, vcodec="none"), fmt(1080, has_drm=True),
                   fmt(1080, "137+140"), fmt(1080, "137/140"), fmt(720)]
        self.assertEqual(select_combined_format(options)["height"], 720)
        adaptive = stream(1080)
        adaptive.is_progressive = False
        self.assertEqual(select_progressive_stream([adaptive, stream(360)]).resolution, "360p")
        with self.assertRaisesRegex(RuntimeError, "Раздельная загрузка отключена"):
            select_combined_format(options[:-1])
        with self.assertRaisesRegex(RuntimeError, "Раздельная загрузка отключена"):
            select_progressive_stream([adaptive])

    def test_same_resolution_prefers_higher_fps(self):
        self.assertEqual(select_combined_format([fmt(1080, "30", fps=30), fmt(1080, "60", fps=60)])["format_id"], "60")


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.job = self.root / ID
        self.job.mkdir()
        self.media = {"height": 720, "width": 1280,
                      "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}

    def test_grouping_and_adaptive(self):
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.extract_info.return_value = {"formats": [fmt(720), fmt(1080, "137", acodec="none"),
            fmt(1080, "138", acodec="none", fps=60), fmt(2160, has_drm=True), fmt(360, vcodec="none")]}
        with patch("yt_dlp.YoutubeDL", return_value=fake):
            qualities = list_available_qualities(URL, lambda _: None)
        self.assertEqual(list(qualities), [1080, 720])
        self.assertTrue(qualities[1080]["adaptive"])
        self.assertFalse(qualities[1080]["has_audio"])
        self.assertEqual(qualities[1080]["format_id"], "138")
        self.assertFalse(qualities[720]["adaptive"])

    def test_publish_and_switch_preserve_files_and_fields(self):
        for h in (720, 1080):
            path = quality_path(self.job, h)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(str(h).encode())
        manifest = {"video_id": ID, "source_path": str(self.job / "source.mkv"), "custom": "keep"}
        with patch("youtube_download.probe", return_value=self.media):
            publish_quality(manifest, 720, self.media, "yt-dlp", "22")
        self.assertEqual(load_json(self.job / "source.json"), manifest)
        with patch("youtube_download.probe", return_value={**self.media, "height": 1080}):
            switched = set_active_quality(self.job / "source.json", 1080)
        self.assertEqual(switched["custom"], "keep")
        self.assertEqual(switched["active_height"], 1080)
        self.assertEqual(Path(switched["source_path"]), quality_path(self.job, 1080).resolve())
        self.assertEqual(quality_path(self.job, 720).read_bytes(), b"720")
        saved = (self.job / "source.json").read_bytes()
        with self.assertRaises(FileNotFoundError):
            set_active_quality(self.job / "source.json", 2160)
        self.assertEqual((self.job / "source.json").read_bytes(), saved)

    def test_migration_preserves_legacy_report_path_and_rolls_back_save_failure(self):
        legacy = self.job / "source.mkv"
        legacy.write_bytes(b"legacy")
        original = {"video_id": ID, "source_path": str(legacy), "custom": "keep"}
        save_json(self.job / "source.json", original)
        with patch("youtube_download.probe", return_value=self.media):
            with patch("youtube_download.save_json", side_effect=OSError("disk full")):
                self.assertIsNone(migrate_legacy_source(self.job, lambda _: None))
            self.assertFalse(quality_path(self.job, 720).exists())
            self.assertEqual(load_json(self.job / "source.json"), original)
            self.assertEqual(migrate_legacy_source(self.job, lambda _: None), quality_path(self.job, 720))
        self.assertEqual(legacy.read_bytes(), b"legacy")
        self.assertEqual(quality_path(self.job, 720).read_bytes(), b"legacy")
        self.assertEqual(load_json(self.job / "source.json")["active_height"], 720)

    def test_download_stages_exact_quality_and_rejects_wrong_height_or_no_audio(self):
        for media in (self.media, {**self.media, "height": 360}, {**self.media, "streams": [{"codec_type": "video"}]}):
            with self.subTest(media=media):
                target = quality_path(self.job, 720)
                target.unlink(missing_ok=True)
                fake = Mock()
                fake.__enter__ = Mock(return_value=fake)
                fake.__exit__ = Mock(return_value=False)
                fake.extract_info.return_value = {"id": ID}
                options = {}
                def factory(opts):
                    options.update(opts)
                    return fake
                fake.download.side_effect = lambda urls: (Path(options["outtmpl"]).parent / "source.mkv").write_bytes(b"download")
                with patch("yt_dlp.YoutubeDL", side_effect=factory), patch("youtube_download.probe", return_value=media):
                    if media == self.media:
                        path, actual = download_quality(URL, ID, 720, self.root, lambda _: None)
                        self.assertEqual(Path(path), target.resolve())
                        self.assertEqual(actual, media)
                        fake.download.reset_mock()
                        download_quality(URL, ID, 720, self.root, lambda _: None)
                        fake.download.assert_not_called()
                    else:
                        with self.assertRaises(SourcePolicyError):
                            download_quality(URL, ID, 720, self.root, lambda _: None)
                        self.assertFalse(target.exists())
                self.assertNotIn("height<=", options["format"])
                self.assertEqual(list(self.job.glob("quality_*")), [])

    def test_download_failure_preserves_other_quality_and_manifest(self):
        source = quality_path(self.job, 720)
        source.parent.mkdir()
        source.write_bytes(b"keep")
        save_json(self.job / "source.json", {"video_id": ID, "source_path": str(source)})
        saved = (self.job / "source.json").read_bytes()
        fake = Mock()
        fake.__enter__ = Mock(return_value=fake)
        fake.__exit__ = Mock(return_value=False)
        fake.extract_info.return_value = {"id": ID}
        fake.download.side_effect = ConnectionResetError("reset")
        with patch("yt_dlp.YoutubeDL", return_value=fake):
            with self.assertRaises(ConnectionResetError):
                download_quality(URL, ID, 1080, self.root, lambda _: None)
        self.assertEqual(source.read_bytes(), b"keep")
        self.assertEqual((self.job / "source.json").read_bytes(), saved)
        self.assertEqual(list(self.job.glob("quality_*")), [])

    def test_migration_conflict_does_not_remove_either_source(self):
        legacy = self.job / "source.mkv"
        legacy.write_bytes(b"legacy")
        target = quality_path(self.job, 720)
        target.parent.mkdir()
        target.write_bytes(b"different")
        save_json(self.job / "source.json", {"video_id": ID, "source_path": str(legacy)})
        saved = (self.job / "source.json").read_bytes()
        with patch("youtube_download.probe", return_value=self.media):
            self.assertIsNone(migrate_legacy_source(self.job, lambda _: None))
        self.assertEqual(target.read_bytes(), b"different")
        self.assertEqual(legacy.read_bytes(), b"legacy")
        self.assertEqual((self.job / "source.json").read_bytes(), saved)

    def test_publish_without_activation_keeps_previous_source(self):
        target = quality_path(self.job, 1080)
        target.parent.mkdir()
        target.write_bytes(b"new quality")
        original = {"active_height": 720, "source_path": str(quality_path(self.job, 720)),
                    "media": self.media}
        manifest = {"video_id": ID, **original}
        with patch("youtube_download.probe", return_value={**self.media, "height": 1080}):
            publish_quality(manifest, 1080, {}, "yt-dlp", "137",
                            self.job / "source.json", activate=False)
        for key, value in original.items():
            self.assertEqual(manifest[key], value)
        self.assertIn("1080", manifest["qualities"])
        self.assertEqual(load_json(self.job / "source.json"), manifest)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_patch = patch("downloader.DATA", self.root)
        self.data_patch.start()
        self.tools = patch("youtube_download.shutil.which", return_value="available")
        self.tools.start()

    def tearDown(self):
        self.tools.stop()
        self.data_patch.stop()
        self.directory.cleanup()

    def test_network_reset_uses_fallback_and_reports_both_failures(self):
        expected = (self.root / ID, {"video_id": ID})
        with patch("youtube_download._download_ytdlp", side_effect=ConnectionResetError(10054, "reset")), \
                patch("youtube_download._download_pytubefix", return_value=expected) as backup:
            self.assertEqual(download(URL, lambda _: None), expected)
            backup.assert_called_once_with(URL, ID, unittest.mock.ANY, self.root)
        with patch("youtube_download._download_ytdlp", side_effect=RuntimeError("primary error")), \
                patch("youtube_download._download_pytubefix", side_effect=RuntimeError("backup error")):
            with self.assertRaisesRegex(RuntimeError, "(?s)primary error.*backup error"):
                download(URL, lambda _: None)

    def test_cache_is_offline_and_does_not_change_existing_quality(self):
        job = self.root / ID
        sources = job / "sources"
        sources.mkdir(parents=True)
        source = sources / "2160p.mkv"
        source.write_bytes(b"original source")
        manifest = {"video_id": ID, "active_height": 2160, "source_path": str(source.resolve()),
                    "media": {"width": 3840, "height": 2160}, "qualities": {}}
        save_json(job / "source.json", manifest)
        with patch("youtube_download.probe", return_value=manifest["media"]), \
                patch("youtube_download._download_ytdlp") as primary, \
                patch("youtube_download._download_pytubefix") as backup:
            _, reused = download(URL, lambda _: None)
            self.assertEqual(reused["media"]["height"], 2160)
            primary.assert_not_called()
            backup.assert_not_called()
        self.assertEqual(source.read_bytes(), b"original source")
        self.assertTrue((sources / "2160p.mkv").exists())

    def test_broken_or_orphaned_cache_never_overwritten(self):
        job = self.root / ID
        job.mkdir()
        source = job / "source.mkv"
        source.write_bytes(b"keep me")
        for content in (None, "{", '{"video_id":"wrong"}'):
            if content is not None:
                (job / "source.json").write_text(content)
            with self.subTest(content=content), patch("youtube_download._download_ytdlp") as primary, \
                    patch("youtube_download._download_pytubefix") as backup:
                with self.assertRaises(SourcePolicyError):
                    download(URL, lambda _: None)
                primary.assert_not_called()
                backup.assert_not_called()
                self.assertEqual(source.read_bytes(), b"keep me")

    def test_live_and_mismatched_identity_do_not_trigger_backup(self):
        for metadata in ({"id": ID, "is_live": True}, {"id": "aaaaaaaaaaa", "formats": [fmt(1080)]}):
            fake = Mock()
            fake.__enter__ = Mock(return_value=fake)
            fake.__exit__ = Mock(return_value=False)
            fake.extract_info.return_value = metadata
            with patch("yt_dlp.YoutubeDL", return_value=fake), patch("youtube_download._download_pytubefix") as backup:
                with self.assertRaises(SourcePolicyError):
                    download(URL, lambda _: None)
                backup.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
class SyntheticDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.directory.name) / "combined.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=0.4",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "0.4", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(cls.fixture)], check=True, timeout=20)
        cls.mkv = cls.fixture.with_suffix(".mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(cls.fixture), "-c", "copy", str(cls.mkv)], check=True, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_download(self, fail_download=False):
        options_seen = []
        fixture, mkv = self.fixture, self.mkv
        metadata = {"id": ID, "title": "Combined test", "formats": [fmt(1080, "137", acodec="none"), fmt(360, "18")]}

        class FakeYDL:
            def __init__(self, options):
                self.options = options
                options_seen.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download):
                return copy.deepcopy(metadata)

            def download(self, urls):
                output = Path(self.options["outtmpl"]).parent / "source.mkv"
                if fail_download:
                    output.with_suffix(".mp4.part").write_bytes(b"incomplete primary")
                    raise ConnectionResetError(10054, "reset during download")
                shutil.copyfile(mkv, output)

        combined = stream(360, subtype="mp4", itag=18, filesize=fixture.stat().st_size)

        def fetch(**kwargs):
            self.assertEqual(kwargs["max_retries"], 3)
            target = Path(kwargs["output_path"]) / kwargs["filename"]
            shutil.copyfile(fixture, target)
            return str(target)

        combined.download = Mock(side_effect=fetch)
        query = Mock()
        query.filter.return_value = [combined]
        video = SimpleNamespace(video_id=ID, title="Fallback test", vid_info={"videoDetails": {}}, streams=query)
        with tempfile.TemporaryDirectory() as root, patch("downloader.DATA", Path(root)), \
                patch("yt_dlp.YoutubeDL", FakeYDL), patch("pytubefix.YouTube", return_value=video) as backup:
            job, manifest = download(URL, lambda _: None)
            self.assertEqual(manifest["media"]["height"], 360)
            self.assertEqual(manifest["download_info"]["backend"], "pytubefix" if fail_download else "yt-dlp")
            self.assertTrue(manifest["download_info"]["combined_stream"])
            self.assertEqual(manifest["active_height"], 360)
            self.assertEqual(load_json(job / "source.json")["video_id"], ID)
            self.assertCountEqual(sorted(p.name for p in job.iterdir()), ["sources", "source.json"])
            self.assertTrue((job / "sources" / "360p.mkv").exists())
            self.assertNotIn("source.mkv", [p.name for p in job.iterdir()])
            self.assertEqual(options_seen[-1]["format"], "18")
            self.assertNotIn("merge_output_format", options_seen[-1])
            actual = probe(job / "sources" / "360p.mkv")
            self.assertEqual({s["codec_type"] for s in actual["streams"]}, {"video", "audio"})
            self.assertTrue(Path(manifest["source_path"]).is_file())
            if fail_download:
                backup.assert_called_once()
                combined.download.assert_called_once()
                query.filter.assert_called_once_with(progressive=True)
            else:
                backup.assert_not_called()

    def test_single_ytdlp_format_and_old_manifest_compatibility(self):
        self.run_download()

    def test_interrupted_primary_uses_fresh_combined_fallback_and_real_remux(self):
        self.run_download(fail_download=True)

    def test_quality_download_has_real_audio_and_publishes_manifest(self):
        fixture = self.mkv
        class FakeYDL:
            def __init__(self, options):
                self.options = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download):
                return {"id": ID}
            def download(self, urls):
                shutil.copyfile(fixture, Path(self.options["outtmpl"]).parent / "source.mkv")
        with tempfile.TemporaryDirectory() as root, patch("yt_dlp.YoutubeDL", FakeYDL):
            path, media = download_quality(URL, ID, 360, root, lambda _: None)
            manifest = {"video_id": ID, "url": URL, "title": "Quality fixture"}
            manifest_path = Path(root) / ID / "source.json"
            publish_quality(manifest, 360, media, "yt-dlp", "18", manifest_path=manifest_path)
            self.assertEqual(load_json(manifest_path)["source_path"], path)
            self.assertEqual({s["codec_type"] for s in probe(path)["streams"]}, {"video", "audio"})


if __name__ == "__main__":
    unittest.main()
