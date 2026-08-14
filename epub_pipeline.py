"""Local EPUB parsing, export, and structural validation for LitMTrans."""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

from PySide6.QtCore import QThread, Signal


EPUB_EXTENSION = ".epub"
EPUB_MIMETYPE = b"application/epub+zip"
EPUB_PARSE_PROTOCOL = "litmtrans-epub-v2"
CHAPTER_MARKER_RE = re.compile(r"<!--\s*LITMTRANS_EPUB_CHAPTER\s+(\{.*?\})\s*-->")
EPUB_MARKER_RE = re.compile(r"<!--\s*LITMTRANS_EPUB_CHAPTER\b")
EPUB_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"}
EPUB_IMAGE_WRAPPER_HREF_RE = re.compile(
    r"^(?P<image>[^/\\()\s]+\.(?:jpe?g|png|gif|webp|svg))\.id-[^/\\()\s]+\.wrap[^/\\()\s]*\.html(?:\.html)?$",
    re.IGNORECASE,
)
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_RATIO = 500
SAFE_FONT_OBFUSCATION_ALGORITHMS = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}


class EpubError(RuntimeError):
    pass


@dataclass(frozen=True)
class EpubBook:
    source: Path
    rootfile: str
    root_dir: str
    metadata: dict
    manifest: dict[str, dict]
    spine: list[str]
    chapters: list[dict]
    styles: list[str]
    cover_href: str


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _archive_name(value: str) -> str:
    value = unquote(str(value or "")).replace("\\", "/")
    normalized = posixpath.normpath(value).lstrip("/")
    if not normalized or normalized == "." or normalized == ".." or normalized.startswith("../"):
        raise EpubError(f"EPUB 包含不安全路径: {value}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise EpubError(f"EPUB 包含绝对路径: {value}")
    return normalized


def _resolve_href(base_dir: str, href: str) -> str:
    clean, _fragment = urldefrag(str(href or ""))
    return _archive_name(posixpath.join(base_dir, clean))


def _check_archive_limits(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise EpubError(f"EPUB 文件数量异常（{len(infos)} 个），已拒绝解析。")
    total = sum(max(0, item.file_size) for item in infos)
    if total > MAX_ARCHIVE_UNCOMPRESSED:
        raise EpubError("EPUB 解压后体积超过 2 GB，已拒绝解析。")
    for item in infos:
        _archive_name(item.filename)
        if item.flag_bits & 0x1:
            raise EpubError(f"EPUB 包含加密成员，不支持受 DRM 保护的电子书: {item.filename}")
        if item.file_size > 32 * 1024 * 1024 and item.compress_size > 0:
            if item.file_size / item.compress_size > MAX_ARCHIVE_RATIO:
                raise EpubError(f"EPUB 成员压缩率异常: {item.filename}")


def _reject_drm(archive: zipfile.ZipFile, names: set[str]) -> None:
    if "META-INF/rights.xml" in names:
        raise EpubError("不支持受 DRM 保护的 EPUB。")
    if "META-INF/encryption.xml" not in names:
        return
    try:
        root = ET.fromstring(archive.read("META-INF/encryption.xml"))
    except ET.ParseError as exc:
        raise EpubError("EPUB encryption.xml 无法解析。") from exc
    algorithms = {
        node.attrib.get("Algorithm", "")
        for node in root.iter()
        if _local_name(node.tag) == "EncryptionMethod"
    }
    unsupported = sorted(value for value in algorithms if value and value not in SAFE_FONT_OBFUSCATION_ALGORITHMS)
    if unsupported:
        raise EpubError("不支持受 DRM 保护或内容加密的 EPUB。")


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    for info in archive.infolist():
        name = _archive_name(info.filename)
        destination = (target / Path(*PurePosixPath(name).parts)).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise EpubError(f"EPUB 成员越过解压目录: {info.filename}") from exc
        if info.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _find_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _metadata_values(metadata_node: ET.Element, name: str) -> list[str]:
    return [value for node in metadata_node if _local_name(node.tag) == name if (value := _text(node))]


def _toc_details(extracted: Path, book_root: str, manifest: dict[str, dict]) -> dict[str, dict]:
    details: dict[str, dict] = {}
    nav_items = [item for item in manifest.values() if "nav" in item.get("properties", [])]
    for item in nav_items:
        nav_path = extracted / Path(*PurePosixPath(item["path"]).parts)
        try:
            root = ET.parse(nav_path).getroot()
        except (OSError, ET.ParseError):
            continue
        def walk_list(list_node: ET.Element, depth: int = 0) -> None:
            for li in [node for node in list_node if _local_name(node.tag) == "li"]:
                anchor = next((node for node in li if _local_name(node.tag) == "a"), None)
                if anchor is not None and anchor.attrib.get("href"):
                    try:
                        resolved = _resolve_href(posixpath.dirname(item["path"]), anchor.attrib["href"])
                        details.setdefault(urldefrag(resolved)[0], {"title": _text(anchor), "depth": depth})
                    except EpubError:
                        pass
                for child_list in [node for node in li if _local_name(node.tag) in {"ol", "ul"}]:
                    walk_list(child_list, depth + 1)

        nav = next((node for node in root.iter() if _local_name(node.tag) == "nav"), None)
        if nav is not None:
            for top_list in [node for node in nav if _local_name(node.tag) in {"ol", "ul"}]:
                walk_list(top_list)

    for item in manifest.values():
        if item.get("media_type") != "application/x-dtbncx+xml":
            continue
        ncx_path = extracted / Path(*PurePosixPath(item["path"]).parts)
        try:
            root = ET.parse(ncx_path).getroot()
        except (OSError, ET.ParseError):
            continue
        def walk_points(parent: ET.Element, depth: int = 0) -> None:
            for nav_point in [node for node in parent if _local_name(node.tag) == "navPoint"]:
                content = next((node for node in nav_point if _local_name(node.tag) == "content"), None)
                label = next((node for node in nav_point if _local_name(node.tag) == "navLabel"), None)
                src = content.attrib.get("src", "") if content is not None else ""
                if src:
                    try:
                        resolved = _resolve_href(posixpath.dirname(item["path"]), src)
                        details.setdefault(urldefrag(resolved)[0], {"title": _text(label), "depth": depth})
                    except EpubError:
                        pass
                walk_points(nav_point, depth + 1)
        walk_points(root)
    return details


def inspect_epub(source: Path, extract_to: Path | None = None) -> EpubBook:
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise EpubError(f"EPUB 文件不存在: {source}")
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise EpubError("文件不是有效的 EPUB/ZIP 容器。") from exc
    with archive:
        _check_archive_limits(archive)
        names = {_archive_name(item.filename): item for item in archive.infolist()}
        _reject_drm(archive, set(names))
        if "mimetype" not in names or archive.read("mimetype").strip() != EPUB_MIMETYPE:
            raise EpubError("EPUB 缺少有效的 mimetype 文件。")
        if "META-INF/container.xml" not in names:
            raise EpubError("EPUB 缺少 META-INF/container.xml。")
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except ET.ParseError as exc:
            raise EpubError("EPUB container.xml 无法解析。") from exc
        rootfile_node = next((node for node in container.iter() if _local_name(node.tag) == "rootfile"), None)
        if rootfile_node is None:
            raise EpubError("EPUB container.xml 没有 rootfile。")
        rootfile = _archive_name(rootfile_node.attrib.get("full-path", ""))
        if rootfile not in names:
            raise EpubError(f"EPUB OPF 文件不存在: {rootfile}")
        try:
            package = ET.fromstring(archive.read(rootfile))
        except ET.ParseError as exc:
            raise EpubError("EPUB OPF 文件无法解析。") from exc

        metadata_node = next((node for node in package if _local_name(node.tag) == "metadata"), ET.Element("metadata"))
        manifest_node = next((node for node in package if _local_name(node.tag) == "manifest"), ET.Element("manifest"))
        spine_node = next((node for node in package if _local_name(node.tag) == "spine"), ET.Element("spine"))
        root_dir = posixpath.dirname(rootfile)
        manifest: dict[str, dict] = {}
        for item in manifest_node:
            if _local_name(item.tag) != "item" or not item.attrib.get("id") or not item.attrib.get("href"):
                continue
            path = _resolve_href(root_dir, item.attrib["href"])
            manifest[item.attrib["id"]] = {
                "id": item.attrib["id"],
                "href": item.attrib["href"],
                "path": path,
                "media_type": item.attrib.get("media-type", ""),
                "properties": item.attrib.get("properties", "").split(),
            }
        spine = [item.attrib.get("idref", "") for item in spine_node if _local_name(item.tag) == "itemref"]
        spine = [item for item in spine if item in manifest]
        if not spine:
            raise EpubError("EPUB 没有可阅读的 spine 章节。")

        identifiers = _metadata_values(metadata_node, "identifier")
        metadata = {
            "title": (_metadata_values(metadata_node, "title") or [source.stem])[0],
            "authors": _metadata_values(metadata_node, "creator"),
            "language": (_metadata_values(metadata_node, "language") or [""])[0],
            "identifier": identifiers[0] if identifiers else "",
            "publisher": (_metadata_values(metadata_node, "publisher") or [""])[0],
            "rights": (_metadata_values(metadata_node, "rights") or [""])[0],
            "description": (_metadata_values(metadata_node, "description") or [""])[0],
            "subjects": _metadata_values(metadata_node, "subject"),
        }
        cover_id = ""
        for node in metadata_node:
            if _local_name(node.tag) == "meta" and node.attrib.get("name") == "cover":
                cover_id = node.attrib.get("content", "")
        cover_item = next((item for item in manifest.values() if "cover-image" in item["properties"]), None)
        cover_item = cover_item or manifest.get(cover_id)
        cover_href = cover_item["path"] if cover_item else ""
        styles = [item["path"] for item in manifest.values() if item["media_type"] == "text/css"]

        temp_extract = extract_to or Path(tempfile.mkdtemp(prefix="litmtrans_epub_inspect_"))
        cleanup = extract_to is None
        try:
            _safe_extract(archive, temp_extract)
            toc_details = _toc_details(temp_extract, root_dir, manifest)
        finally:
            if cleanup:
                shutil.rmtree(temp_extract, ignore_errors=True)
        chapters = []
        for index, idref in enumerate(spine, start=1):
            item = manifest[idref]
            toc = toc_details.get(item["path"], {})
            chapters.append({
                "index": index,
                "idref": idref,
                "href": item["href"],
                "path": item["path"],
                "title": toc.get("title", ""),
                "toc_depth": int(toc.get("depth", 0)),
                "media_type": item["media_type"],
            })
        return EpubBook(source, rootfile, root_dir, metadata, manifest, spine, chapters, styles, cover_href)


def _pandoc_path() -> Path:
    bundled = Path(__file__).resolve().parent / "resources" / "pandoc.exe"
    if bundled.is_file():
        return bundled
    found = shutil.which("pandoc")
    if found:
        return Path(found)
    raise EpubError("没有找到 Pandoc，无法解析或导出 EPUB。")


def _run_pandoc(command: list[str], cwd: Path, timeout: int = 300) -> None:
    startup = None
    creationflags = 0
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.run(
            command,
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            startupinfo=startup,
            creationflags=creationflags,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise EpubError(f"Pandoc 处理 EPUB 失败: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EpubError("Pandoc 处理 EPUB 超时。") from exc


def _chapter_marker(chapter: dict) -> str:
    payload = {key: chapter.get(key) for key in ("index", "idref", "href", "path", "title", "toc_depth")}
    return f"<!-- LITMTRANS_EPUB_CHAPTER {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))} -->"


def normalize_epub_image_wrapper_links(markdown: str) -> str:
    """Turn Gutenberg image-wrapper XHTML links into direct image links.

    EPUB producers commonly wrap a thumbnail in a generated XHTML page whose
    filename contains `.id-...wrap-0.html`. Those pages are not part of the
    normalized Markdown export, while the corresponding full-size image is.
    """
    link_re = re.compile(r"(\]\()(?P<href>[^)\s]+)(?P<title>\s+[\"'][^)]*[\"'])?(\))")

    def replace(match: re.Match) -> str:
        href = match.group("href")
        image_match = EPUB_IMAGE_WRAPPER_HREF_RE.match(posixpath.basename(href.replace("\\", "/")))
        if not image_match:
            return match.group(0)
        return f"{match.group(1)}images/{image_match.group('image')}{match.group(4)}"

    return link_re.sub(replace, markdown)


def is_epub_markdown(markdown: str) -> bool:
    """Return whether Markdown contains the structural markers emitted by EPUB parsing."""
    return bool(EPUB_MARKER_RE.search(str(markdown or "")))


def is_epub_markdown_path(path: Path | str | None) -> bool:
    """Return whether a parsed-document folder was produced from an EPUB."""
    if not path:
        return False
    candidate = Path(path)
    if candidate.suffix.lower() == EPUB_EXTENSION:
        return True
    folder = candidate if candidate.is_dir() else candidate.parent
    metadata_path = folder / "epub_source.json"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(payload, dict) and str(payload.get("format") or "").lower() == "epub"


def repair_epub_markdown_attributes(markdown: str) -> str:
    """Repair a common translation error in Gutenberg EPUB title spans.

    The source uses ``[title]{#pg-title-no-subtitle ...}``. Some translators
    drop the brackets but leave the attribute block, which Pandoc then renders
    visibly as ordinary text. Reattach only this known EPUB span attribute.
    """
    # Attribute-only headings are hidden Calibre/page-break metadata. If a
    # translator exposes them as visible text, remove the whole heading.
    cleaned = re.sub(r"(?m)^\s*#{1,6}\s+\{#[^}\n]*\}\s*$", "", str(markdown or ""))
    heading_re = re.compile(
        r"^(?P<prefix>\s*#{1,6}\s+)(?P<title>.+?)\s+"
        r"(?P<span>\{#pg-title-no-subtitle\b[^}\n]*\})"
        r"(?P<rest>\s+\{#pg-header-heading\b[^}\n]*\})?\s*$",
        re.MULTILINE,
    )

    def repair(match: re.Match) -> str:
        title = match.group("title").strip()
        # Do not double-wrap already valid bracketed spans.
        if title.startswith("[") and title.endswith("]"):
            return match.group(0)
        return (
            f"{match.group('prefix')}[{title}]"
            f"{match.group('span')}{match.group('rest') or ''}"
        )

    return heading_re.sub(repair, cleaned)


def _sanitize_xhtml_for_conversion(path: Path) -> None:
    """Prevent active content and remote media fetches during local conversion."""
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", text, flags=re.IGNORECASE | re.DOTALL)
    remote_media = re.compile(
        r"(<(?:img|image|audio|video|source)\b[^>]*?\s(?:src|href|xlink:href)\s*=\s*)(['\"])https?://.*?\2",
        flags=re.IGNORECASE | re.DOTALL,
    )
    path.write_text(remote_media.sub(r'\1""', text), encoding="utf-8")


def _copy_epub_embedded_media(extracted: Path, images_dir: Path) -> None:
    """Copy media referenced indirectly by extracted SVGs (for example cover.jpeg)."""
    images_dir.mkdir(parents=True, exist_ok=True)
    for asset in extracted.rglob("*"):
        if not asset.is_file() or asset.suffix.lower() not in EPUB_MEDIA_SUFFIXES:
            continue
        target = images_dir / asset.name
        if target.exists():
            continue
        try:
            shutil.copyfile(asset, target)
        except OSError:
            continue


def _normalize_epub_svg_cover_links(markdown: str, images_dir: Path) -> str:
    """Use the concrete raster asset when an EPUB SVG is only an image wrapper."""
    image_re = re.compile(r"(!\[[^\]]*\]\()(?P<target>images/[^)]+\.svg)(\))", re.IGNORECASE)

    def replace(match: re.Match) -> str:
        svg_path = images_dir / Path(match.group("target")).name
        try:
            svg_text = svg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return match.group(0)
        ref_match = re.search(r"(?:xlink:href|href)\s*=\s*[\"']([^\"']+)[\"']", svg_text, re.IGNORECASE)
        if not ref_match:
            return match.group(0)
        raster = images_dir / Path(ref_match.group(1)).name
        if raster.suffix.lower() not in EPUB_MEDIA_SUFFIXES or not raster.is_file():
            return match.group(0)
        return f"{match.group(1)}images/{raster.name}{match.group(3)}"

    return image_re.sub(replace, markdown)


def convert_epub_to_markdown(source: Path, output_dir: Path, log=None, should_stop=None) -> Path:
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted = output_dir / "epub_source"
    if extracted.exists():
        shutil.rmtree(extracted)
    with zipfile.ZipFile(source) as archive:
        _check_archive_limits(archive)
        _safe_extract(archive, extracted)
    book = inspect_epub(source, extracted)
    pandoc = _pandoc_path()
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    converted_chapters: list[dict] = []
    for index, chapter in enumerate(book.chapters, start=1):
        if should_stop and should_stop():
            raise EpubError("用户已停止解析。")
        chapter_path = extracted / Path(*PurePosixPath(chapter["path"]).parts)
        if not chapter_path.is_file():
            continue
        _sanitize_xhtml_for_conversion(chapter_path)
        temp_md = output_dir / f".epub_chapter_{index:04d}.md"
        command = [
            str(pandoc), str(chapter_path), "-f", "html", "-t",
            "markdown+raw_html+tex_math_dollars+footnotes+pipe_tables+link_attributes",
            # Use a relative extraction path and run from the output directory so
            # Pandoc emits portable `images/...` Markdown targets. Resources in
            # EPUB XHTML are resolved relative to the chapter, not the archive root.
            "--wrap=none", "--extract-media", "images", "--resource-path", str(chapter_path.parent), "-o", str(temp_md),
        ]
        _run_pandoc(command, output_dir)
        text = temp_md.read_text(encoding="utf-8", errors="replace").strip()
        temp_md.unlink(missing_ok=True)
        title = str(chapter.get("title") or "").strip()
        desired_level = max(1, min(6, int(chapter.get("toc_depth", 0)) + 1))
        first_heading = re.search(r"(?m)^#{1,6}\s+", text)
        if first_heading:
            text = text[:first_heading.start()] + ("#" * desired_level) + " " + text[first_heading.end():]
        elif title:
            text = f"{'#' * desired_level} {title}\n\n{text}"
        parts.extend([_chapter_marker(chapter), text])
        converted_chapters.append(chapter)
        if log:
            log(f"正在解析 EPUB 章节（{index}/{len(book.chapters)}）{f' · {title}' if title else ''}…")

    if not converted_chapters:
        raise EpubError("EPUB 没有可转换的 XHTML 章节。")
    _copy_epub_embedded_media(extracted, images_dir)
    markdown = normalize_epub_image_wrapper_links("\n\n".join(part for part in parts if part).strip() + "\n")
    markdown = _normalize_epub_svg_cover_links(markdown, images_dir)
    raw_path = output_dir / "full.md"
    clean_path = output_dir / "full.cleaned.md"
    raw_path.write_text(markdown, encoding="utf-8")
    clean_path.write_text(markdown, encoding="utf-8")
    (output_dir / "image_map.json").write_text("[]\n", encoding="utf-8")
    copied_source = output_dir / source.name
    if copied_source.resolve() != source:
        shutil.copy2(source, copied_source)
    payload = {
        "protocol": EPUB_PARSE_PROTOCOL,
        "parser": "epub-local-pandoc",
        "source_file": str(copied_source),
        "source_display_name": source.name,
        "format": "epub",
        "rootfile": book.rootfile,
        "metadata": book.metadata,
        "manifest": list(book.manifest.values()),
        "spine": book.spine,
        "chapters": converted_chapters,
        "styles": book.styles,
        "cover": book.cover_href,
    }
    for name in ("document_task.json", "mineru_task.json", "epub_source.json"):
        (output_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    marker = {"generated_by": "LitMTrans", "parser": "epub", "source_file": str(source)}
    marker_text = json.dumps(marker, ensure_ascii=False, indent=2)
    (output_dir / ".litmtrans-output.json").write_text(marker_text, encoding="utf-8")
    # Existing libraries use this marker name to recognize managed document folders.
    (output_dir / ".mineru_generated").write_text(marker_text, encoding="utf-8")
    return clean_path


def _load_epub_metadata(markdown_path: Path) -> dict:
    for name in ("epub_source.json", "document_task.json", "mineru_task.json"):
        path = markdown_path.parent / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("format") == "epub":
            return value
    return {}


def _translated_identifier(identifier: str, markdown_path: Path, target_language: str) -> str:
    seed = f"{identifier}|{markdown_path.resolve()}|{target_language}".encode("utf-8", errors="replace")
    return f"urn:uuid:{uuid.UUID(hashlib.md5(seed).hexdigest())}"


def restore_epub_chapter_markers(source_markdown: str, translated_markdown: str) -> str:
    """Restore protected chapter boundaries if a translation provider dropped comments."""
    marker_payloads = [json.loads(value) for value in CHAPTER_MARKER_RE.findall(source_markdown)]
    markers = [_chapter_marker(value) for value in marker_payloads]
    if not markers or len(CHAPTER_MARKER_RE.findall(translated_markdown)) == len(markers):
        return translated_markdown
    clean = CHAPTER_MARKER_RE.sub("", translated_markdown)
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+.+$", clean))
    selected = []
    cursor_index = 0
    for payload in marker_payloads:
        expected = max(1, min(6, int(payload.get("toc_depth", 0) or 0) + 1))
        match_index = next(
            (index for index in range(cursor_index, len(headings)) if len(headings[index].group(1)) == expected),
            None,
        )
        if match_index is None:
            return translated_markdown
        selected.append(headings[match_index])
        cursor_index = match_index + 1
    pieces: list[str] = []
    cursor = 0
    for marker, heading in zip(markers, selected):
        pieces.append(clean[cursor:heading.start()])
        pieces.append(marker + "\n\n")
        cursor = heading.start()
    pieces.append(clean[cursor:])
    return "".join(pieces)


def export_markdown_to_epub(
    markdown_path: Path,
    output_path: Path,
    *,
    target_language: str = "",
    translated: bool = False,
    title: str = "",
    log=None,
) -> list[str]:
    markdown_path = Path(markdown_path).resolve()
    output_path = Path(output_path).resolve()
    metadata_bundle = _load_epub_metadata(markdown_path)
    metadata = dict(metadata_bundle.get("metadata") or {})
    source_root = markdown_path.parent / "epub_source"
    work_root = Path(tempfile.mkdtemp(prefix="litmtrans_epub_export_"))
    warnings: list[str] = []
    try:
        export_md = work_root / "book.md"
        raw = normalize_epub_image_wrapper_links(markdown_path.read_text(encoding="utf-8", errors="replace"))
        markers = CHAPTER_MARKER_RE.findall(raw)
        export_md.write_text(raw, encoding="utf-8")
        book_title = title.strip() or str(metadata.get("title") or markdown_path.parent.name)
        if translated and not title.strip():
            book_title = f"{book_title}（{target_language or '译本'}）"
        language = (target_language if translated else str(metadata.get("language") or "")).strip() or "zh-CN"
        identifier = str(metadata.get("identifier") or "")
        if translated:
            identifier = _translated_identifier(identifier, markdown_path, language)
        command = [
            str(_pandoc_path()), str(export_md), "-f",
            "markdown+raw_html+tex_math_dollars+footnotes+pipe_tables+link_attributes",
            "-t", "epub3", "--standalone", "--toc", "--split-level=1",
            "--resource-path", os.pathsep.join([str(markdown_path.parent), str(source_root)]),
            "--metadata", f"title={book_title}", "--metadata", f"lang={language}",
            "--metadata", f"identifier={identifier}", "-o", str(output_path),
        ]
        for author in metadata.get("authors") or []:
            command.extend(["--metadata", f"author={author}"])
        for key in ("publisher", "rights", "description"):
            if metadata.get(key):
                command.extend(["--metadata", f"{key}={metadata[key]}"])
        cover = str(metadata_bundle.get("cover") or "")
        if cover:
            cover_path = source_root / Path(*PurePosixPath(cover).parts)
            if cover_path.is_file():
                command.extend(["--epub-cover-image", str(cover_path)])
        for style in metadata_bundle.get("styles") or []:
            css = source_root / Path(*PurePosixPath(str(style)).parts)
            if css.is_file():
                command.extend(["--css", str(css)])
        if log:
            log(f"正在生成 EPUB 电子书：{book_title}…")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run_pandoc(command, markdown_path.parent, timeout=600)
        report = validate_epub(output_path)
        if not report["valid"]:
            raise EpubError("导出的 EPUB 校验失败：" + "；".join(report["errors"]))
        warnings.extend(report["warnings"])
        if metadata_bundle.get("chapters") and not markers:
            warnings.append("译文中的章节标记缺失，已根据标题重新生成目录，章节拆分可能与原书不同。")
        return warnings
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def validate_epub(path: Path) -> dict:
    path = Path(path)
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _check_archive_limits(archive)
            names = {_archive_name(item.filename) for item in infos}
            _reject_drm(archive, names)
            if not infos or infos[0].filename != "mimetype":
                errors.append("mimetype 不是 ZIP 中的第一个成员")
            else:
                if infos[0].compress_type != zipfile.ZIP_STORED:
                    errors.append("mimetype 被压缩")
                if archive.read(infos[0]).strip() != EPUB_MIMETYPE:
                    errors.append("mimetype 内容不正确")
            if "META-INF/container.xml" not in names:
                errors.append("缺少 container.xml")
                return {"valid": False, "errors": errors, "warnings": warnings}
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile_node = next((node for node in container.iter() if _local_name(node.tag) == "rootfile"), None)
            rootfile = _archive_name(rootfile_node.attrib.get("full-path", "")) if rootfile_node is not None else ""
            if not rootfile or rootfile not in names:
                errors.append("OPF rootfile 不存在")
                return {"valid": False, "errors": errors, "warnings": warnings}
            package = ET.fromstring(archive.read(rootfile))
            base = posixpath.dirname(rootfile)
            manifest: dict[str, str] = {}
            for node in _find_children(package, "item"):
                item_id, href = node.attrib.get("id", ""), node.attrib.get("href", "")
                if not item_id or not href:
                    continue
                resolved = _resolve_href(base, href)
                manifest[item_id] = resolved
                if resolved not in names:
                    errors.append(f"manifest 资源不存在: {href}")
            spine_refs = [node.attrib.get("idref", "") for node in _find_children(package, "itemref")]
            missing_refs = [item for item in spine_refs if item not in manifest]
            if missing_refs:
                errors.append("spine 引用了不存在的 manifest id: " + ", ".join(missing_refs[:5]))
            if not spine_refs:
                errors.append("spine 为空")
            if not any("nav" in node.attrib.get("properties", "").split() for node in _find_children(package, "item")):
                warnings.append("EPUB3 导航文档未找到。")
            external_schemes = ("http://", "https://", "mailto:", "tel:", "data:", "javascript:")
            for document in manifest.values():
                if not document.lower().endswith((".xhtml", ".html", ".htm")) or document not in names:
                    continue
                try:
                    root = ET.fromstring(archive.read(document))
                except ET.ParseError:
                    errors.append(f"XHTML 无法解析: {document}")
                    continue
                base_dir = posixpath.dirname(document)
                for node in root.iter():
                    for attribute in ("href", "src", "{http://www.w3.org/1999/xlink}href"):
                        target = str(node.attrib.get(attribute) or "").strip()
                        if not target or target.startswith("#") or target.lower().startswith(external_schemes):
                            continue
                        try:
                            resolved = _resolve_href(base_dir, target)
                        except EpubError:
                            errors.append(f"XHTML 含不安全链接: {document} -> {target}")
                            continue
                        if resolved not in names:
                            # A translated book may retain links to source-only
                            # Gutenberg wrapper pages. They are non-critical
                            # anchor targets; missing image/media sources remain
                            # hard validation errors below.
                            local = _local_name(node.tag).lower()
                            if attribute == "href" and local in {"a", "area"}:
                                warnings.append(f"XHTML 锚点链接失效（已保留正文）: {document} -> {target}")
                            else:
                                errors.append(f"XHTML 资源链接失效: {document} -> {target}")
    except (OSError, zipfile.BadZipFile, ET.ParseError, EpubError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "warnings": warnings}


class EpubParseWorker(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(int)
    finished_signal = Signal(bool, str, str)

    def __init__(self, source_path: str, output_dir: str):
        super().__init__()
        self.source_path = Path(source_path)
        self.output_dir = Path(output_dir)

    def run(self) -> None:
        try:
            self.progress_signal.emit(5)
            self.log_signal.emit(f"正在本地解析 EPUB：{self.source_path.name}")
            path = convert_epub_to_markdown(
                self.source_path,
                self.output_dir,
                log=self.log_signal.emit,
                should_stop=self.isInterruptionRequested,
            )
            self.progress_signal.emit(100)
            self.finished_signal.emit(True, f"EPUB 解析完成: {path}", str(path))
        except Exception as exc:
            if self.output_dir.exists() and not (self.output_dir / "full.cleaned.md").exists():
                shutil.rmtree(self.output_dir, ignore_errors=True)
            self.finished_signal.emit(False, str(exc), "")
