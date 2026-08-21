"""工作目录数据的非破坏性复制迁移。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Callable

from AI_history import atomic_write_chat_sessions


CHAT_HISTORY_NAME = "chat_conversations.json"
GENERATED_MARKER = ".mineru_generated"
SPECIAL_DIRECTORIES = {"chat_attachments", "parsed_documents"}
_SOURCE_LINE = re.compile(r"(?m)^(\s*来源\s*:\s*)([^\r\n]+)(\r?$)")


def should_copy_workspace_item(item: Path) -> bool:
    if item.is_dir():
        return item.name in SPECIAL_DIRECTORIES or (item / GENERATED_MARKER).is_file()
    return item.name == CHAT_HISTORY_NAME


def copy_workspace_data(
    old_dir: Path,
    new_dir: Path,
    cancelled: Callable[[], bool] | None = None,
) -> dict[Path, Path]:
    """复制程序数据到空目录；任何失败都不会改动旧目录。"""
    old_dir = Path(old_dir).expanduser().resolve()
    new_dir = Path(new_dir).expanduser().resolve()
    if old_dir == new_dir:
        return {}
    if new_dir.is_relative_to(old_dir) or old_dir.is_relative_to(new_dir):
        raise ValueError("新旧工作文件夹不能互相包含")
    if not old_dir.is_dir():
        raise FileNotFoundError(f"旧工作文件夹不存在：{old_dir}")

    new_dir.mkdir(parents=True, exist_ok=True)
    if any(new_dir.iterdir()):
        raise ValueError("自动迁移只能使用空的新工作文件夹")

    selected = [item for item in old_dir.iterdir() if should_copy_workspace_item(item)]
    history = old_dir / CHAT_HISTORY_NAME
    sessions = _read_sessions(history) if history.is_file() else None
    stage = new_dir / f".litmtrans-migration-{uuid.uuid4().hex}"
    stage.mkdir()
    final_map = {
        item.resolve(): (new_dir / item.name).resolve()
        for item in selected
        if item.is_dir() and (item / GENERATED_MARKER).is_file()
    }

    try:
        for item in selected:
            _raise_if_cancelled(cancelled)
            if item.name == CHAT_HISTORY_NAME:
                continue
            target = stage / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

        if sessions is not None:
            atomic_write_chat_sessions(stage / CHAT_HISTORY_NAME, remap_chat_sessions(sessions, final_map))

        _rewrite_cached_workspace_paths(stage, final_map, old_dir, new_dir)
        _verify_copied_items(selected, stage)
        _raise_if_cancelled(cancelled)
        for item in list(stage.iterdir()):
            os.replace(item, new_dir / item.name)
        stage.rmdir()
        return final_map
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def remap_chat_sessions(sessions: list[dict], migrated_map: dict[Path, Path]) -> list[dict]:
    id_map: dict[str, str] = {}
    normalized_map = {Path(old).resolve(): Path(new).resolve() for old, new in migrated_map.items()}
    for old_folder, new_folder in normalized_map.items():
        old_hash = hashlib.sha1(str(old_folder).encode("utf-8", errors="replace")).hexdigest()
        new_hash = hashlib.sha1(str(new_folder).encode("utf-8", errors="replace")).hexdigest()
        id_map[f"doc-chat-{old_hash}"] = f"doc-chat-{new_hash}"

    result: list[dict] = []
    for original in sessions:
        if not isinstance(original, dict):
            continue
        record = copy.deepcopy(original)
        session_id = str(record.get("id") or "")
        for old_id, new_id in id_map.items():
            if session_id == old_id or session_id.startswith(f"{old_id}-revision-"):
                record["id"] = session_id.replace(old_id, new_id, 1)
                break

        source = str(record.get("document_source_path") or "").strip()
        remapped_source = _remap_path_text(source, normalized_map)
        if remapped_source:
            record["document_source_path"] = remapped_source

        for message in record.get("messages") or []:
            if isinstance(message, dict) and "content" in message:
                message["content"] = _remap_message_content(message["content"], normalized_map)
        result.append(record)
    return result


def _remap_message_content(content, migrated_map: dict[Path, Path]):
    if isinstance(content, str):
        def replace_source(match: re.Match) -> str:
            remapped = _remap_path_text(match.group(2).strip(), migrated_map)
            return f"{match.group(1)}{remapped or match.group(2)}{match.group(3)}"

        return _SOURCE_LINE.sub(replace_source, content)
    if isinstance(content, list):
        return [
            {**part, "text": _remap_message_content(part.get("text"), migrated_map)}
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            else part
            for part in content
        ]
    return content


def _remap_path_text(value: str, migrated_map: dict[Path, Path]) -> str:
    if not value:
        return ""
    try:
        path = Path(value).expanduser().resolve()
    except (OSError, ValueError):
        return ""
    for old_folder, new_folder in migrated_map.items():
        try:
            relative = path.relative_to(old_folder)
        except ValueError:
            continue
        return str(new_folder / relative)
    return ""


def _read_sessions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(data, list):
        raise ValueError("对话历史文件格式无效")
    return data


REWRITE_EXTENSIONS = {".html", ".htm", ".json", ".md", ".txt"}


def _verify_copied_items(selected: list[Path], stage: Path) -> None:
    for source in selected:
        if source.name == CHAT_HISTORY_NAME:
            if not (stage / CHAT_HISTORY_NAME).is_file():
                raise OSError("对话历史未复制完成")
            continue
        target = stage / source.name
        if source.is_file():
            if not target.is_file() or source.stat().st_size != target.stat().st_size:
                raise OSError(f"文件复制校验失败：{source.name}")
            continue
        source_files = {path.relative_to(source): path.stat().st_size for path in source.rglob("*") if path.is_file()}
        target_files = {path.relative_to(target): path.stat().st_size for path in target.rglob("*") if path.is_file()}
        if source_files.keys() != target_files.keys() or any(
            source_size != target_files[relative]
            for relative, source_size in source_files.items()
            if relative.suffix.lower() not in REWRITE_EXTENSIONS
        ):
            raise OSError(f"文件夹复制校验失败：{source.name}")


def _remap_json_obj(obj, normalized_map: dict[Path, Path]):
    if isinstance(obj, dict):
        return {k: _remap_json_obj(v, normalized_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_remap_json_obj(item, normalized_map) for item in obj]
    if isinstance(obj, str):
        remapped = _remap_path_text(obj, normalized_map)
        return remapped if remapped else obj
    return obj


def _rewrite_cached_workspace_paths(stage: Path, migrated_map: dict[Path, Path], old_dir: Path, new_dir: Path) -> None:
    """全面修正迁移目录中元数据（mineru_task.json 等）与 HTML 缓存中的旧路径引用。"""
    normalized_map = {Path(old).resolve(): Path(new).resolve() for old, new in migrated_map.items()}
    normalized_map[Path(old_dir).resolve()] = Path(new_dir).resolve()

    replacements: list[tuple[str, str]] = []
    for old_folder, new_folder in normalized_map.items():
        replacements.extend([
            (old_folder.as_uri(), new_folder.as_uri()),
            (str(old_folder), str(new_folder)),
            (old_folder.as_posix(), new_folder.as_posix()),
            (json.dumps(str(old_folder))[1:-1], json.dumps(str(new_folder))[1:-1]),
        ])

    for old_folder, new_folder in migrated_map.items():
        staged_folder = stage / new_folder.name
        if not staged_folder.is_dir():
            continue
        for file_path in staged_folder.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in REWRITE_EXTENSIONS:
                continue
            try:
                if file_path.suffix.lower() == ".json":
                    try:
                        data = json.loads(file_path.read_text(encoding="utf-8", errors="strict"))
                        updated_data = _remap_json_obj(data, normalized_map)
                        file_path.write_text(json.dumps(updated_data, ensure_ascii=False, indent=2), encoding="utf-8")
                        continue
                    except (json.JSONDecodeError, UnicodeError):
                        pass

                original = file_path.read_text(encoding="utf-8", errors="strict")
                updated = original
                for old_text, new_text in replacements:
                    if old_text:
                        updated = updated.replace(old_text, new_text)
                if updated != original:
                    file_path.write_text(updated, encoding="utf-8")
            except (OSError, UnicodeError):
                continue


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise RuntimeError("迁移已取消")

