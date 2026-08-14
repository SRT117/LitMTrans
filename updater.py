"""Windows 客户端版本更新检查与安装包下载校验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app_version import APP_NAME, APP_VERSION, GITHUB_REPOSITORY, UPDATE_MANIFEST_URL


_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_ALLOWED_DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com"}
DEFAULT_GITHUB_MIRRORS = [
    "https://ghproxy.net/",
    "https://mirror.ghproxy.com/",
]


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    notes: str
    installer_url: str
    installer_sha256: str
    installer_size: int
    release_page_url: str = ""

    @property
    def formatted_size(self) -> str:
        return format_size(self.installer_size)


def format_size(size_bytes: int) -> str:
    """将字节大小格式化为易读字符串（B、KB、MB、GB）。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def wrap_mirror_url(url: str, mirror_prefix: str) -> str:
    """为 GitHub 下载地址添加镜像加速代理前缀。"""
    prefix = mirror_prefix.rstrip("/") + "/"
    clean = str(url).strip()
    return f"{prefix}{clean}"


def get_candidate_urls(raw_url: str, use_mirror: bool = True) -> list[str]:
    """返回有序的候选下载地址列表（根据配置优先使用镜像或直连）。"""
    clean = str(raw_url).strip()
    if not clean:
        return []
    mirrored = [wrap_mirror_url(clean, mirror) for mirror in DEFAULT_GITHUB_MIRRORS]
    if use_mirror:
        return [*mirrored, clean]
    return [clean, *mirrored]


def version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError(f"无效版本号：{value}")
    return tuple(int(part) for part in match.groups())


def is_newer_version(candidate: str, current: str = APP_VERSION) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def parse_release_manifest(payload: bytes | str) -> ReleaseInfo:
    data = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
    installer = data.get("installer") if isinstance(data, dict) else None
    if not isinstance(installer, dict):
        raise ValueError("更新清单缺少 installer")
    version = str(data.get("version") or "").strip()
    version_tuple(version)
    url = str(installer.get("url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError("安装包必须来自 GitHub HTTPS 地址")
    digest = str(installer.get("sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("安装包 SHA-256 无效")
    size = int(installer.get("size") or 0)
    if size <= 0 or size > 2 * 1024 * 1024 * 1024:
        raise ValueError("安装包大小无效")
    return ReleaseInfo(
        version=version,
        notes=str(data.get("notes") or "").strip(),
        installer_url=url,
        installer_sha256=digest,
        installer_size=size,
        release_page_url=f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/v{version}",
    )


def cleanup_old_installers(updates_dir: Path, keep_version: str = "") -> None:
    """清理历史版本的更新安装包和未完成的残片，避免磁盘空间积累。"""
    if not updates_dir.exists():
        return
    keep_target = f"LitMTrans-{keep_version}-setup.exe" if keep_version else ""
    for candidate in updates_dir.glob("LitMTrans-*-setup.exe*"):
        if candidate.name != keep_target:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass


def updates_directory() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / APP_NAME / "updates"
    root.mkdir(parents=True, exist_ok=True)
    return root


class UpdateCheckWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, use_mirror: bool = True, parent=None):
        super().__init__(parent)
        self.use_mirror = bool(use_mirror)

    def run(self) -> None:
        candidates = get_candidate_urls(UPDATE_MANIFEST_URL, use_mirror=self.use_mirror)
        last_error: Exception | None = None
        for url in candidates:
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
                )
                with urllib.request.urlopen(request, timeout=12) as response:
                    payload = response.read(1024 * 1024 + 1)
                if len(payload) > 1024 * 1024:
                    raise ValueError("更新清单过大")
                self.succeeded.emit(parse_release_manifest(payload))
                return
            except Exception as exc:
                last_error = exc
                continue
        self.failed.emit(str(last_error or "无法连接到更新服务器"))


class UpdateDownloadWorker(QThread):
    succeeded = Signal(str)
    failed = Signal(str)
    progress = Signal(int, int)  # (已下载字节数, 总字节数)
    cancelled = Signal()

    def __init__(self, release: ReleaseInfo, use_mirror: bool = True, parent=None):
        super().__init__(parent)
        self.release = release
        self.use_mirror = bool(use_mirror)
        self._is_cancelled = False

    def cancel(self) -> None:
        self._is_cancelled = True

    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def run(self) -> None:
        updates_dir = updates_directory()
        cleanup_old_installers(updates_dir, keep_version=self.release.version)
        target = updates_dir / f"LitMTrans-{self.release.version}-setup.exe"
        partial = target.with_suffix(".exe.part")
        candidates = get_candidate_urls(self.release.installer_url, use_mirror=self.use_mirror)
        last_error: Exception | None = None

        for url in candidates:
            if self._is_cancelled:
                self._cleanup_partial(partial)
                self.cancelled.emit()
                return

            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
                )
                digest = hashlib.sha256()
                written = 0
                self.progress.emit(0, self.release.installer_size)

                with urllib.request.urlopen(request, timeout=25) as response, partial.open("wb") as output:
                    while True:
                        if self._is_cancelled:
                            self._cleanup_partial(partial)
                            self.cancelled.emit()
                            return

                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.release.installer_size:
                            raise ValueError("安装包大小超出更新清单限制")
                        digest.update(chunk)
                        output.write(chunk)
                        self.progress.emit(written, self.release.installer_size)

                if self._is_cancelled:
                    self._cleanup_partial(partial)
                    self.cancelled.emit()
                    return

                if written != self.release.installer_size:
                    raise ValueError(f"安装包下载不完整（预期 {self.release.installer_size} 字节，实际 {written} 字节）")
                if digest.hexdigest().lower() != self.release.installer_sha256:
                    raise ValueError("安装包 SHA-256 安全校验失败，文件可能已损坏")

                os.replace(partial, target)
                self.succeeded.emit(str(target))
                return
            except Exception as exc:
                self._cleanup_partial(partial)
                last_error = exc
                continue

        if self._is_cancelled:
            self._cleanup_partial(partial)
            self.cancelled.emit()
        else:
            self.failed.emit(str(last_error or "所有下载节点均连接失败"))

    def _cleanup_partial(self, partial_path: Path) -> None:
        try:
            partial_path.unlink(missing_ok=True)
        except OSError:
            pass


def launch_installer(path: str) -> None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise RuntimeError("只能从已安装的 Windows 发行版启动更新")
    subprocess.Popen([str(Path(path).resolve()), "/SP-", "/CLOSEAPPLICATIONS"])
