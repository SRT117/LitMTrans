"""工作目录解析与旧配置兼容。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Iterable


def configured_work_dir(candidates: Iterable[Path]) -> str:
    """返回候选配置中第一个仍然存在的工作目录。"""
    for candidate in candidates:
        try:
            data = json.loads(Path(candidate).read_text(encoding="utf-8", errors="replace"))
            value = str(data.get("work_dir") or "").strip() if isinstance(data, dict) else ""
            if value and Path(value).expanduser().exists():
                return value
        except (OSError, ValueError, TypeError):
            continue
    return ""


def default_workspace_path(app_dir: Path, legacy_app_dirs: Iterable[Path], source_dir: Path) -> Path:
    """已有数据优先沿用；真正的新安装默认使用安装目录。"""
    historical = (Path(app_dir) / "workspace", *(Path(item) / "workspace" for item in legacy_app_dirs))
    for workspace in historical:
        try:
            if workspace.is_dir() and any(workspace.iterdir()):
                return workspace
        except OSError:
            continue

    install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(source_dir).resolve()
    preferred = install_dir / "workspace"
    if _directory_is_writable(preferred):
        return preferred
    return Path(app_dir) / "workspace"


def _directory_is_writable(directory: Path) -> bool:
    probe = Path(directory) / f".litmtrans-write-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.touch(exist_ok=False)
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
