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
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout

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


MTRAN_RUNTIME_ZIP_URL = (
    "https://github.com/SRT117/LitMTrans/releases/download/"
    "runtime-windows-v1/LitMTrans-runtime-windows-v1.zip"
)
MTRAN_RUNTIME_ZIP_SHA256 = "fc2c732573717db29e406c5164bd5687b1f7d1cb7908e6d4a49051736c68e406"
MTRAN_RUNTIME_ESTIMATED_SIZE = 180 * 1024 * 1024
MTRAN_RUNTIME_MAX_SIZE = 350 * 1024 * 1024


class MTranModelDownloadWorker(QThread):
    """用于下载并解压 MTranServer 离线翻译模型包的后台工作线程。"""

    succeeded = Signal(str)
    failed = Signal(str)
    progress = Signal(int, int, str)
    cancelled = Signal()

    def __init__(self, target_dir: Path | None = None, use_mirror: bool = True, parent=None):
        super().__init__(parent)
        self.target_dir = target_dir
        self.use_mirror = bool(use_mirror)
        self._is_cancelled = False

    def cancel(self) -> None:
        self._is_cancelled = True

    def run(self) -> None:
        import shutil
        import uuid
        import zipfile
        from machine_translate import _is_valid_resource_root, mtran_persistent_root

        try:
            target_root = (self.target_dir or mtran_persistent_root()).resolve()
            parent_dir = target_root.parent
            parent_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.failed.emit(f"无法创建离线模型存储目录：{exc}")
            return

        temp_zip = parent_dir / f".mtran_download_{uuid.uuid4().hex}.zip"
        temp_extract = parent_dir / f".mtran_extract_{uuid.uuid4().hex}"
        candidates = get_candidate_urls(MTRAN_RUNTIME_ZIP_URL, use_mirror=self.use_mirror)
        last_error: Exception | None = None

        for url in candidates:
            if self._is_cancelled:
                self._cleanup_paths(temp_zip, temp_extract)
                self.cancelled.emit()
                return
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
                )
                digest = hashlib.sha256()
                written = 0
                self.progress.emit(0, MTRAN_RUNTIME_ESTIMATED_SIZE, "正在连接下载节点…")

                with urllib.request.urlopen(request, timeout=25) as response, temp_zip.open("wb") as output:
                    header_len = response.headers.get("Content-Length")
                    total_size = int(header_len) if header_len and header_len.isdigit() else MTRAN_RUNTIME_ESTIMATED_SIZE
                    if total_size > MTRAN_RUNTIME_MAX_SIZE:
                        raise ValueError(f"离线模型包声明大小 ({format_size(total_size)}) 超出安全限制")

                    while True:
                        if self._is_cancelled:
                            self._cleanup_paths(temp_zip, temp_extract)
                            self.cancelled.emit()
                            return
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MTRAN_RUNTIME_MAX_SIZE:
                            raise ValueError(f"离线模型包下载大小超出安全限制 ({format_size(MTRAN_RUNTIME_MAX_SIZE)})")
                        digest.update(chunk)
                        output.write(chunk)
                        self.progress.emit(
                            written,
                            total_size,
                            f"正在下载离线模型包（{format_size(written)} / {format_size(total_size)}）…",
                        )

                if self._is_cancelled:
                    self._cleanup_paths(temp_zip, temp_extract)
                    self.cancelled.emit()
                    return

                if digest.hexdigest().lower() != MTRAN_RUNTIME_ZIP_SHA256:
                    raise ValueError("离线模型包 SHA-256 安全校验不匹配，文件可能已损坏。")

                self.progress.emit(written, written, "正在解压并安装离线机翻引擎…")
                temp_extract.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(temp_zip) as archive:
                    for member in archive.infolist():
                        if self._is_cancelled:
                            self._cleanup_paths(temp_zip, temp_extract)
                            self.cancelled.emit()
                            return
                        parts = Path(member.filename).parts
                        if not parts:
                            continue
                        if parts[0] == "mtranserver":
                            sub_path = Path(*parts[1:])
                        elif parts[0] == "resources" and len(parts) > 1 and parts[1] == "mtranserver":
                            sub_path = Path(*parts[2:])
                        elif parts[0] in {"bin", "config", "models", "README.md"}:
                            sub_path = Path(*parts)
                        else:
                            continue
                        if not str(sub_path):
                            continue
                        dest_file = temp_extract / sub_path
                        if member.is_dir():
                            dest_file.mkdir(parents=True, exist_ok=True)
                        else:
                            dest_file.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(member) as src, dest_file.open("wb") as dst:
                                shutil.copyfileobj(src, dst)

                if self._is_cancelled:
                    self._cleanup_paths(temp_zip, temp_extract)
                    self.cancelled.emit()
                    return

                if not _is_valid_resource_root(temp_extract):
                    raise ValueError("离线模型包解压后内容不完整或缺少必要的可执行文件与模型。")

                if target_root.exists():
                    backup_root = parent_dir / f".mtran_old_{uuid.uuid4().hex}"
                    try:
                        target_root.rename(backup_root)
                        temp_extract.rename(target_root)
                        shutil.rmtree(backup_root, ignore_errors=True)
                    except Exception:
                        if not target_root.exists() and backup_root.exists():
                            backup_root.rename(target_root)
                        shutil.copytree(temp_extract, target_root, dirs_exist_ok=True)
                        shutil.rmtree(temp_extract, ignore_errors=True)
                else:
                    temp_extract.rename(target_root)

                self._cleanup_paths(temp_zip, temp_extract)
                self.succeeded.emit(str(target_root))
                return
            except Exception as exc:
                self._cleanup_paths(temp_zip, temp_extract)
                last_error = exc
                continue

        if self._is_cancelled:
            self._cleanup_paths(temp_zip, temp_extract)
            self.cancelled.emit()
        else:
            self.failed.emit(str(last_error or "所有下载节点均连接失败"))

    def _cleanup_paths(self, temp_zip: Path, temp_extract: Path) -> None:
        try:
            temp_zip.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if temp_extract.exists():
                import shutil
                shutil.rmtree(temp_extract, ignore_errors=True)
        except OSError:
            pass


class MTranModelDownloadDialog(QDialog):
    """MTranServer 离线模型包下载与安装对话框。"""

    def __init__(self, parent=None, use_mirror: bool = True):
        super().__init__(parent)
        self.setWindowTitle("下载本地离线机翻模型")
        self.setMinimumWidth(440)
        self.setModal(True)
        self.worker = MTranModelDownloadWorker(use_mirror=use_mirror, parent=self)
        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)

        self.title_label = QLabel("正在下载 MTranServer 离线翻译模型包…")
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.title_label)

        self.hint_label = QLabel("本地机翻模型包约 180 MB，下载后永久保存在本机。\n提示：该离线小模型翻译效果比较基础，堪堪用于快速预览；学术论文建议使用联网机翻或 AI 大模型以获得更佳效果。")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #666; font-size: 12px;")
        layout.addWidget(self.hint_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("准备下载…")
        self.status_label.setStyleSheet("font-size: 12px; color: #555;")
        layout.addWidget(self.status_label)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._on_cancel)
        button_row.addWidget(self.cancel_button)
        layout.addLayout(button_row)

    def _connect_signals(self) -> None:
        self.worker.progress.connect(self._on_progress)
        self.worker.succeeded.connect(self._on_succeeded)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(super().reject)

    def exec(self) -> int:
        self.worker.start()
        return super().exec()

    def reject(self) -> None:
        """用户按 Esc 或调用 reject 时立即取消下载线程并安全等待。"""
        if self.worker.isRunning():
            self.cancel_button.setEnabled(False)
            self.worker.cancel()
            self.worker.wait(1000)
        super().reject()

    def closeEvent(self, event) -> None:
        """用户点击右上角关闭按钮时立即取消下载线程并安全等待。"""
        if self.worker.isRunning():
            self.cancel_button.setEnabled(False)
            self.worker.cancel()
            self.worker.wait(1000)
        super().closeEvent(event)

    def _on_progress(self, current: int, total: int, status_text: str) -> None:
        if total > 0:
            percent = int((current / total) * 100)
            self.progress_bar.setValue(min(100, max(0, percent)))
        self.status_label.setText(status_text)

    def _on_succeeded(self, target_path: str) -> None:
        if not self.isVisible():
            return
        self.title_label.setText("离线机翻模型已就绪！")
        self.status_label.setText("安装完成，本地机翻引擎已成功就绪。")
        self.progress_bar.setValue(100)
        QMessageBox.information(self, "下载完成", "本地离线机翻模型包已成功安装，现在可以使用本地机翻进行翻译。")
        self.accept()

    def _on_failed(self, error_msg: str) -> None:
        if not self.isVisible():
            return
        QMessageBox.critical(self, "下载失败", f"离线模型包下载失败：\n{error_msg}\n\n您可以稍后重试，或切换为【联网免费机翻】直接翻译。")
        super().reject()

    def _on_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消…")
        self.reject()
