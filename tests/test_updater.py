import hashlib
import json
from pathlib import Path
import unittest

from app_version import APP_VERSION
from updater import (
    ReleaseInfo,
    format_size,
    get_candidate_urls,
    is_newer_version,
    parse_release_manifest,
    version_tuple,
    wrap_mirror_url,
)


class UpdaterTests(unittest.TestCase):
    def test_version_comparison_is_numeric(self):
        self.assertEqual(version_tuple("1.10.0"), (1, 10, 0))
        self.assertTrue(is_newer_version("1.10.0", "1.9.9"))
        self.assertFalse(is_newer_version("1.0.0", "1.0.0"))

    def test_manifest_requires_verified_github_installer(self):
        digest = hashlib.sha256(b"installer").hexdigest()
        release = parse_release_manifest(json.dumps({
            "version": "1.2.3",
            "notes": "修复导出问题",
            "installer": {
                "url": "https://github.com/SRT117/LitMTrans/releases/download/v1.2.3/LitMTrans-1.2.3-setup.exe",
                "sha256": digest,
                "size": 9,
            },
        }))
        self.assertEqual(release.version, "1.2.3")
        self.assertEqual(release.installer_sha256, digest)
        self.assertIn("v1.2.3", release.release_page_url)
        self.assertEqual(release.formatted_size, "9 B")

    def test_manifest_rejects_non_github_download(self):
        with self.assertRaises(ValueError):
            parse_release_manifest(json.dumps({
                "version": "1.2.3",
                "installer": {
                    "url": "https://example.com/setup.exe",
                    "sha256": "0" * 64,
                    "size": 1,
                },
            }))

    def test_format_size_helper(self):
        self.assertEqual(format_size(500), "500 B")
        self.assertEqual(format_size(1500), "1.5 KB")
        self.assertEqual(format_size(15 * 1024 * 1024), "15.0 MB")
        self.assertEqual(format_size(int(2.5 * 1024 * 1024 * 1024)), "2.50 GB")

    def test_mirror_urls_generation(self):
        direct_url = "https://github.com/SRT117/LitMTrans/releases/download/v1.0.0/setup.exe"
        mirrored = wrap_mirror_url(direct_url, "https://ghproxy.net/")
        self.assertEqual(mirrored, f"https://ghproxy.net/{direct_url}")

        # 启用镜像加速时，镜像地址排在前面，直连地址作为兜底
        candidates_mirror = get_candidate_urls(direct_url, use_mirror=True)
        self.assertTrue(len(candidates_mirror) >= 2)
        self.assertEqual(candidates_mirror[-1], direct_url)
        self.assertTrue(candidates_mirror[0].startswith("https://ghproxy.net/"))

        # 未启用镜像加速时，直连地址排在首位
        candidates_direct = get_candidate_urls(direct_url, use_mirror=False)
        self.assertEqual(candidates_direct[0], direct_url)

    def test_cleanup_old_installers(self):
        import tempfile
        from pathlib import Path
        from updater import cleanup_old_installers

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            old1 = tmp_path / "LitMTrans-1.0.0-setup.exe"
            old2 = tmp_path / "LitMTrans-0.9.0-setup.exe.part"
            keep = tmp_path / "LitMTrans-1.0.1-setup.exe"
            other = tmp_path / "unrelated.txt"

            for p in (old1, old2, keep, other):
                p.write_text("test", encoding="utf-8")

            cleanup_old_installers(tmp_path, keep_version="1.0.1")

            self.assertFalse(old1.exists())
            self.assertFalse(old2.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(other.exists())

    def test_cleanup_on_startup(self):
        import tempfile
        from pathlib import Path
        from updater import cleanup_on_startup

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            installed = tmp_path / "LitMTrans-1.0.3-setup.exe"
            part = tmp_path / "LitMTrans-1.0.4-setup.exe.part"
            unrelated = tmp_path / "data.json"

            for p in (installed, part, unrelated):
                p.write_text("dummy", encoding="utf-8")

            cleanup_on_startup(tmp_path)

            self.assertFalse(installed.exists())
            self.assertFalse(part.exists())
            self.assertTrue(unrelated.exists())

    def test_update_manifest_url_points_to_raw_json(self):
        from app_version import GITHUB_REPOSITORY, UPDATE_MANIFEST_URL

        expected = f"https://raw.githubusercontent.com/{GITHUB_REPOSITORY}/main/update.json"
        self.assertEqual(UPDATE_MANIFEST_URL, expected)

    def test_bundled_update_json_is_valid(self):
        from pathlib import Path
        root_update_json = Path(__file__).resolve().parents[1] / "update.json"
        self.assertTrue(root_update_json.is_file())
        content = root_update_json.read_text(encoding="utf-8")
        release = parse_release_manifest(content)
        self.assertEqual(release.version, APP_VERSION)
        self.assertTrue(release.installer_url.startswith("https://github.com/"))
        self.assertEqual(len(release.installer_sha256), 64)
        self.assertGreater(release.installer_size, 0)


    def test_mtran_installed_detection_and_multi_path_discovery(self):
        import tempfile
        from unittest.mock import patch
        import machine_translate

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_root = Path(tmpdir).resolve()
            # 空目录时未安装
            with patch.object(machine_translate, "mtran_persistent_root", return_value=fake_root):
                with patch.object(machine_translate, "_candidate_resource_roots", return_value=[fake_root]):
                    self.assertFalse(machine_translate.is_mtran_installed())

                    # 创建结构但无模型文件
                    bin_dir = fake_root / "bin"
                    bin_dir.mkdir()
                    (bin_dir / "mtranserver.exe").write_bytes(b"exe")
                    config_dir = fake_root / "config"
                    config_dir.mkdir()
                    (config_dir / "records.json").write_text("{}", encoding="utf-8")
                    models_dir = fake_root / "models"
                    models_dir.mkdir()
                    self.assertFalse(machine_translate.is_mtran_installed())

                    # 放入有效模型
                    pair_dir = models_dir / "en_zh-Hans"
                    pair_dir.mkdir()
                    (pair_dir / "model.enzh.intgemm.alphas.bin").write_bytes(b"bin")
                    self.assertTrue(machine_translate.is_mtran_installed())


    def test_mtran_download_worker_enforces_size_limit(self):
        import io
        import tempfile
        from unittest.mock import MagicMock, patch
        from updater import MTRAN_RUNTIME_MAX_SIZE, MTranModelDownloadWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / "mtranserver"
            worker = MTranModelDownloadWorker(target_dir=target_dir)

            # 模拟返回过大流
            oversized_data = b"x" * (64 * 1024)
            mock_response = MagicMock()
            mock_response.headers.get.return_value = str(MTRAN_RUNTIME_MAX_SIZE + 1024)
            mock_response.read.side_effect = [oversized_data, b""]
            mock_response.__enter__.return_value = mock_response

            failed_msgs = []
            worker.failed.connect(failed_msgs.append)

            with patch("urllib.request.urlopen", return_value=mock_response):
                worker.run()

            self.assertTrue(len(failed_msgs) > 0)
            self.assertIn("超出安全限制", failed_msgs[0])

    def test_mtran_download_worker_handles_cancel_and_clean_temp(self):
        import tempfile
        from unittest.mock import MagicMock, patch
        from updater import MTranModelDownloadWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / "mtranserver"
            worker = MTranModelDownloadWorker(target_dir=target_dir)
            worker.cancel()

            cancelled_fired = []
            worker.cancelled.connect(lambda: cancelled_fired.append(True))

            worker.run()
            self.assertTrue(len(cancelled_fired) > 0)
            # 确认临时文件已被彻底清理
            leftover = list(Path(tmpdir).glob(".mtran_*"))
            self.assertEqual(leftover, [])

    def test_mtran_download_worker_atomic_extraction_rejects_invalid_archive(self):
        import hashlib
        import tempfile
        import zipfile
        from unittest.mock import MagicMock, patch
        from updater import MTRAN_RUNTIME_ZIP_SHA256, MTranModelDownloadWorker

        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = Path(tmpdir) / "mtranserver"
            worker = MTranModelDownloadWorker(target_dir=target_dir)

            # 制作一个结构残缺的 zip（例如缺少 bin 和 models）
            zip_buf = tempfile.NamedTemporaryFile(delete=False)
            with zipfile.ZipFile(zip_buf.name, "w") as zf:
                zf.writestr("mtranserver/README.md", "incomplete")
            zip_bytes = Path(zip_buf.name).read_bytes()
            zip_buf.close()
            Path(zip_buf.name).unlink(missing_ok=True)

            def make_resp(*args, **kwargs):
                resp = MagicMock()
                resp.headers.get.return_value = str(len(zip_bytes))
                resp.read.side_effect = [zip_bytes, b""]
                resp.__enter__.return_value = resp
                return resp

            failed_msgs = []
            worker.failed.connect(failed_msgs.append)

            # 模拟 SHA-256 匹配但内容残缺
            with patch("urllib.request.urlopen", side_effect=make_resp):
                with patch("updater.MTRAN_RUNTIME_ZIP_SHA256", hashlib.sha256(zip_bytes).hexdigest()):
                    worker.run()

            self.assertTrue(len(failed_msgs) > 0)
            self.assertIn("不完整", failed_msgs[0])
            # 目标正式目录不应被创建
            self.assertFalse(target_dir.exists())

    def test_mtran_download_dialog_reject_and_close_cancels_worker(self):
        from unittest.mock import patch
        from updater import MTranModelDownloadDialog

        with patch("updater.MTranModelDownloadWorker.start"):
            dialog = MTranModelDownloadDialog()
            dialog.worker.isRunning = lambda: True

            cancelled = []
            dialog.worker.cancel = lambda: cancelled.append(True)
            dialog.worker.wait = lambda timeout=1000: True

            dialog.reject()
            self.assertTrue(len(cancelled) > 0)


if __name__ == "__main__":
    unittest.main()
