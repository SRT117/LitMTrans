"""Persistence helpers for document-chat images and conversation history."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from pathlib import Path


CHAT_ASSET_PREFIX = "litmtrans-chat-asset://"
_DATA_IMAGE_RE = re.compile(
    r"^data:(image/[A-Za-z0-9.+-]+);base64,(.*)$",
    flags=re.IGNORECASE | re.DOTALL,
)
_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}


def chat_asset_dir(history_path: Path) -> Path:
    return Path(history_path).parent / "chat_attachments"


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def externalize_chat_images(value, asset_dir: Path):
    """Return a copy where image data URLs are content-addressed file refs."""
    if isinstance(value, list):
        return [externalize_chat_images(item, asset_dir) for item in value]
    if isinstance(value, dict):
        return {key: externalize_chat_images(item, asset_dir) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    match = _DATA_IMAGE_RE.match(value)
    if not match:
        return value
    mime_type = match.group(1).lower()
    extension = _MIME_EXTENSIONS.get(mime_type, ".img")
    try:
        payload = base64.b64decode(match.group(2), validate=False)
    except (ValueError, TypeError):
        return value
    if not payload:
        return value
    digest = hashlib.sha256(payload).hexdigest()
    filename = f"{digest}{extension}"
    target = Path(asset_dir) / filename
    if not target.exists():
        _atomic_write_bytes(target, payload)
    return f"{CHAT_ASSET_PREFIX}{filename}"


def hydrate_chat_images(value, asset_dir: Path):
    """Return a copy with internal refs restored to API-compatible data URLs."""
    if isinstance(value, list):
        return [hydrate_chat_images(item, asset_dir) for item in value]
    if isinstance(value, dict):
        return {key: hydrate_chat_images(item, asset_dir) for key, item in value.items()}
    if not isinstance(value, str) or not value.startswith(CHAT_ASSET_PREFIX):
        return value
    filename = value[len(CHAT_ASSET_PREFIX):]
    if not filename or Path(filename).name != filename:
        return value
    path = Path(asset_dir) / filename
    try:
        payload = path.read_bytes()
    except OSError:
        return value
    suffix = path.suffix.lower()
    mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }.get(suffix, "image/png")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def atomic_write_chat_sessions(path: Path, sessions: list[dict]) -> None:
    """Publish history atomically so interruption cannot truncate the JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(sessions, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def prune_unreferenced_chat_assets(asset_dir: Path, sessions: list[dict]) -> None:
    """Remove only content-addressed files no longer referenced by any session."""
    referenced: set[str] = set()

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, str) and value.startswith(CHAT_ASSET_PREFIX):
            filename = value[len(CHAT_ASSET_PREFIX):]
            if filename and Path(filename).name == filename:
                referenced.add(filename)

    visit(sessions)
    root = Path(asset_dir)
    if not root.is_dir():
        return
    for path in root.iterdir():
        if path.is_file() and path.name not in referenced and re.fullmatch(r"[0-9a-f]{64}\.[A-Za-z0-9]+", path.name):
            try:
                path.unlink()
            except OSError:
                pass
