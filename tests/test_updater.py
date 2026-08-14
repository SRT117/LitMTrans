import hashlib
import json
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


if __name__ == "__main__":
    unittest.main()
