"""Page-based reading preview, positioned rendering, and export helpers."""

from __future__ import annotations

import math
import os

from OT_common import *
from layout_single_column import (
    SingleColumnProfile,
    inherit_stable_single_column_short_items,
    infer_single_column_profile,
    promote_stable_single_column_items,
)

# 阅读预览加入内置字体声明后升级标记，强制旧的普通阅读预览重新生成。
READER_POLISH_MARKER = "mineru-reader-polish-v6-formula-lightbox"
SERIF_READING_FONT_STACK = BUNDLED_READER_FONT_STACK
# Public cache protocol identifier. Change this single value only when a
# layout-rule or cache-schema change makes every completed fit incompatible.
LAYOUT_FIT_CACHE_VERSION = "layout-fit-cache-v12-logical-span-lines"


def single_column_body_promotion_enabled() -> bool:
    """Allow a one-variable rollback of the optional single-column path.

    Set ``LITMTRANS_SINGLE_COLUMN_BODY_PROMOTION=0`` before starting the app to
    preserve the exact legacy promotion behavior.  The default is enabled only
    after the geometry module independently proves a repeated full-width lane.
    """

    value = os.environ.get("LITMTRANS_SINGLE_COLUMN_BODY_PROMOTION", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}

def html_preview_cache_is_fresh(html_path: Path, source_path: Path) -> bool:
    try:
        return html_path.exists() and html_path.stat().st_mtime >= source_path.stat().st_mtime
    except OSError:
        return False


def html_contains_marker(html_path: Path, marker: str) -> bool:
    try:
        return marker in html_path.read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return False


def polished_preview_cache_is_fresh(output_path: Path, dependencies: list[Path]) -> bool:
    return multi_file_cache_is_fresh(output_path, dependencies) and html_contains_marker(output_path, READER_POLISH_MARKER)


def block_id_for_index(index: int) -> str:
    return f"doc-block-{index:04d}"


def inject_markdown_block_anchors(markdown: str) -> str:
    lines = markdown.splitlines()
    result: list[str] = []
    block_index = 0
    in_fence = False
    pending_anchor = True
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if pending_anchor:
                block_index += 1
                result.append(f'\n<a id="{block_id_for_index(block_index)}" class="sync-anchor"></a>\n')
                pending_anchor = False
            in_fence = not in_fence
            result.append(line)
            continue
        if not in_fence and stripped and pending_anchor:
            block_index += 1
            result.append(f'\n<a id="{block_id_for_index(block_index)}" class="sync-anchor"></a>\n')
            pending_anchor = False
        result.append(line)
        if not in_fence and not stripped:
            pending_anchor = True
    return "\n".join(result) + ("\n" if markdown.endswith("\n") else "")


def slugify_heading(text: str) -> str:
    slug = re.sub(r"\s+", "-", text.strip().lower())
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]+", "", slug, flags=re.UNICODE).strip("-")
    return slug or "heading"


def inject_heading_sync_anchors(markdown: str) -> str:
    lines = markdown.splitlines()
    result: list[str] = []
    seen: dict[str, int] = {}
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            result.append(line)
            continue
        if not in_fence:
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match:
                level = len(match.group(1))
                title = re.sub(r"\s+#+\s*$", "", match.group(2)).strip()
                base = slugify_heading(title)
                seen[base] = seen.get(base, 0) + 1
                anchor_id = f"heading-{base}" if seen[base] == 1 else f"heading-{base}-{seen[base]}"
                safe_title = html.escape(title, quote=True)
                result.append(
                    f'\n<a id="{anchor_id}" class="sync-heading" data-level="{level}" data-title="{safe_title}"></a>\n'
                )
        result.append(line)
    return "\n".join(result) + ("\n" if markdown.endswith("\n") else "")


def repair_fragmented_inline_math(markdown: str) -> str:
    inline_fragment = r"\$(?:[_^][^$]*|\\[A-Za-z]+[^$]*?)\$"
    # Fix OCR/Markdown fragments like `H $_2$ O` or `Mg/H $_2$ O(l)` before export.
    markdown = re.sub(
        rf"(?<=[A-Za-z0-9\)/\]])\s+({inline_fragment})(?=\s*[A-Za-z0-9\(])",
        r"\1",
        markdown,
    )
    markdown = re.sub(
        rf"(?<=[A-Za-z0-9\)/\]])({inline_fragment})\s+(?=[A-Za-z0-9\(])",
        r"\1",
        markdown,
    )
    return markdown


def _normalized_image_key(value: str) -> str:
    raw = urllib.parse.unquote(str(value or "").strip().strip("<>"))
    raw = raw.replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    return raw.lower().lstrip("./")


def _layout_bbox(value) -> tuple[float, float, float, float] | None:
    if isinstance(value, str):
        parts = value.replace(",", " ").split()
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        return None
    if len(parts) < 4:
        return None
    try:
        left, top, right, bottom = (float(part) for part in parts[:4])
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def layout_image_width_percentages(markdown_path: Path) -> dict[str, float]:
    """Map Markdown image targets to their placed width on the source PDF page.

    Raster pixel dimensions are not reliable layout dimensions: MinerU may
    extract a small logo at a high DPI or a full-width figure at a low DPI.
    ``layout.json`` stores both the image bbox and the real PDF page width, so
    their ratio is the stable value needed by responsive readers and exports.
    """
    markdown_path = Path(markdown_path).resolve()
    asset_dir = layout_preview_asset_dir(markdown_path)
    layout_path = asset_dir / "layout.json" if asset_dir else None
    if not layout_path or not layout_path.exists():
        return {}
    try:
        payload = json.loads(layout_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    pages = payload.get("pdf_info") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return {}

    source_widths: dict[str, float] = {}

    def visit(node, page_width: float, inherited_bbox=None) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child, page_width, inherited_bbox)
            return
        if not isinstance(node, dict):
            return
        bbox = _layout_bbox(node.get("bbox")) or inherited_bbox
        image_path = node.get("image_path") or node.get("img_path")
        node_type = str(node.get("type") or "").lower()
        if image_path and bbox and node_type in {"image", "image_body", "chart", "table"}:
            percentage = max(0.1, min(100.0, ((bbox[2] - bbox[0]) / page_width) * 100.0))
            key = _normalized_image_key(str(image_path))
            if key:
                # The same image can occur more than once. Preserve the largest
                # actual placement so no occurrence becomes unreadably small.
                for candidate in {key, Path(key).name}:
                    source_widths[candidate] = max(percentage, source_widths.get(candidate, 0.0))
        for value in node.values():
            if isinstance(value, (dict, list)):
                visit(value, page_width, bbox)

    for page in pages:
        if not isinstance(page, dict):
            continue
        page_size = page.get("page_size")
        try:
            page_width = float(page_size[0])
        except (TypeError, ValueError, IndexError):
            continue
        if page_width > 0:
            visit(page, page_width)

    result = dict(source_widths)
    image_map_path = markdown_path.parent / "image_map.json"
    try:
        records = json.loads(image_map_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        records = []
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            original = _normalized_image_key(str(record.get("original_target") or ""))
            percentage = source_widths.get(original) or source_widths.get(Path(original).name)
            if not percentage:
                continue
            for field in ("clean_target", "saved_file", "original_target"):
                key = _normalized_image_key(str(record.get(field) or ""))
                if key:
                    result[key] = percentage
                    result[Path(key).name] = percentage
    return result


def image_width_for_target(target: str, image_widths: dict[str, float] | None) -> str | None:
    if not image_widths:
        return None
    key = _normalized_image_key(target)
    percentage = image_widths.get(key) or image_widths.get(Path(key).name)
    if not percentage:
        return None
    return f"{percentage:.3f}".rstrip("0").rstrip(".") + "%"


def normalize_markdown_for_export(
    markdown: str,
    image_width: str | None = DEFAULT_EXPORT_IMAGE_WIDTH,
    image_widths: dict[str, float] | None = None,
) -> str:
    markdown = repair_malformed_pipe_tables(markdown)
    markdown = repair_fragmented_inline_math(markdown)
    markdown = re.sub(r"<details\b[^>]*>.*?</details>\s*", "\n\n", markdown, flags=re.IGNORECASE | re.DOTALL)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    image_pattern = re.compile(
        r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?P<title>\s+['\"][^'\"]*['\"])?\s*\)(?!\{)",
        re.DOTALL,
    )

    def replace_image(match: re.Match) -> str:
        alt = match.group("alt")
        if re.fullmatch(r"IMAGE_\d+", alt.strip(), flags=re.IGNORECASE):
            alt = ""
        target = match.group("target")
        title = match.group("title") or ""
        # Prefer the physical placement recorded in the source PDF. The same
        # percentage then responds to the reader pane and to export page width.
        resolved_width = image_width_for_target(target, image_widths) or image_width
        width_attribute = f"{{width={resolved_width}}}" if resolved_width else ""
        return f"![{alt}]({target}{title}){width_attribute}"

    normalized = image_pattern.sub(replace_image, markdown)
    return normalized


def separate_markdown_images_for_word(markdown: str) -> str:
    """Place document images in their own Markdown paragraphs for Word.

    A MinerU text block can occasionally put an image reference and the next
    sentence on one physical line.  Pandoc then produces one Word paragraph,
    leaving the following text beside the inline image.  In research papers the
    extracted images are figures, not inline icons, so make every Markdown
    image outside a table a block-level paragraph before DOCX conversion.
    """
    image_pattern = re.compile(
        r"!\[[^\]\n]*\]\(\s*(?:<[^>\n]+>|[^\s)]+)(?:\s+['\"][^'\"]*['\"])?\s*\)(?:\{[^}\n]*\})?",
        re.DOTALL,
    )
    separated = image_pattern.sub(lambda match: f"\n\n{match.group(0).strip()}\n\n", markdown)
    return re.sub(r"\n{3,}", "\n\n", separated)


def inline_local_images_in_html(html_path: Path, asset_root: Path) -> int:
    """Embed local ``<img>`` resources so an exported HTML is portable.

    Pandoc deliberately preserves Markdown image URLs as relative paths.  That
    works for the preview beside ``images/``, but the UI subsequently copies the
    HTML to a user-selected directory.  Convert only images that resolve under
    the document's asset directory; remote URLs and unresolved resources remain
    untouched.
    """
    try:
        text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0

    root = asset_root.resolve()
    embedded = 0
    image_src_pattern = re.compile(
        r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(?P<src>.*?)(\2)',
        flags=re.IGNORECASE | re.DOTALL,
    )

    def replace_source(match: re.Match) -> str:
        nonlocal embedded
        source = html.unescape(match.group("src")).strip()
        parsed = urllib.parse.urlparse(source)
        if not source or parsed.scheme or source.startswith("//"):
            return match.group(0)
        relative_path = urllib.parse.unquote(parsed.path).replace("/", os.sep)
        try:
            image_path = (root / relative_path).resolve()
            image_path.relative_to(root)
        except (OSError, ValueError):
            return match.group(0)
        if not image_path.is_file():
            return match.group(0)
        mime_type = mimetypes.guess_type(str(image_path))[0] or ""
        if not mime_type.startswith("image/"):
            return match.group(0)
        try:
            payload = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError:
            return match.group(0)
        embedded += 1
        return f'{match.group(1)}{match.group(2)}data:{mime_type};base64,{payload}{match.group(2)}'

    # Do not build a second giant HTML string containing every base64 image.
    # Keep the source text plus at most one encoded image in memory while
    # publishing a replacement file atomically.
    temporary = html_path.with_name(f".{html_path.name}.{uuid.uuid4().hex}.tmp")
    last_end = 0
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            for match in image_src_pattern.finditer(text):
                handle.write(text[last_end:match.start()])
                handle.write(replace_source(match))
                last_end = match.end()
            handle.write(text[last_end:])
        if embedded:
            os.replace(temporary, html_path)
        else:
            temporary.unlink(missing_ok=True)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return embedded


class RawHtmlTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[HtmlTableCell]] = []
        self._current_row: list[HtmlTableCell] | None = None
        self._current_cell: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        tag = tag.lower()
        if tag == "tr":
            self._current_row = []
            return
        if tag not in {"td", "th"}:
            if self._current_cell is not None and tag in {"br", "p", "div"}:
                parts = self._current_cell.setdefault("parts", [])
                if parts and str(parts[-1]) != "\n":
                    parts.append("\n")
            return
        if self._current_row is None:
            self._current_row = []
        attr_map = {key.lower(): value for key, value in attrs}
        colspan = attr_map.get("colspan") or "1"
        rowspan = attr_map.get("rowspan") or "1"
        try:
            colspan_value = max(1, int(colspan))
        except ValueError:
            colspan_value = 1
        try:
            rowspan_value = max(1, int(rowspan))
        except ValueError:
            rowspan_value = 1
        self._current_cell = {
            "parts": [],
            "colspan": colspan_value,
            "rowspan": rowspan_value,
            "is_header": tag == "th",
        }

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in {"td", "th"}:
            if self._current_row is None or self._current_cell is None:
                return
            raw_text = "".join(str(part) for part in self._current_cell.get("parts", []))
            text = normalize_html_table_cell_text(raw_text)
            self._current_row.append(
                HtmlTableCell(
                    text=text,
                    colspan=int(self._current_cell.get("colspan", 1)),
                    rowspan=int(self._current_cell.get("rowspan", 1)),
                    is_header=bool(self._current_cell.get("is_header")),
                )
            )
            self._current_cell = None
            return
        if tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str):
        if self._current_cell is not None:
            parts = self._current_cell.setdefault("parts", [])
            parts.append(data)


def normalize_html_table_cell_text(text: str) -> str:
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\\\((.*?)\\\)", lambda match: match.group(1).strip(), text, flags=re.DOTALL)
    text = re.sub(r"(?<!\\)\$(?![\s$])(.+?)(?<!\\)\$(?!\d)", lambda match: match.group(1).strip(), text, flags=re.DOTALL)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def strip_tex_wrappers(text: str) -> str:
    command_with_group = re.compile(r"\\([A-Za-z]+)\s*\{")
    supported_wrappers = {"mathrm", "text", "mathit", "mathbf", "operatorname", "mathsf", "mathtt"}

    def extract_braced(source: str, start: int) -> tuple[str, int]:
        if start >= len(source) or source[start] != "{":
            return "", start
        depth = 0
        pieces: list[str] = []
        index = start
        while index < len(source):
            char = source[index]
            if char == "{":
                depth += 1
                if depth > 1:
                    pieces.append(char)
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return "".join(pieces), index + 1
                pieces.append(char)
            else:
                pieces.append(char)
            index += 1
        return "".join(pieces), index

    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            result.append(text[index])
            index += 1
            continue
        match = command_with_group.match(text, index)
        if match:
            command = match.group(1)
            if command in supported_wrappers:
                inner, next_index = extract_braced(text, match.end() - 1)
                result.append(strip_tex_wrappers(inner))
                index = next_index
                continue
        simple_match = re.match(r"\\([A-Za-z]+)", text[index:])
        if simple_match:
            command = simple_match.group(1)
            result.append(tex_command_to_text(command))
            index += len(command) + 1
            continue
        if index + 1 < len(text):
            result.append(text[index + 1])
            index += 2
        else:
            index += 1
    return result and "".join(result) or ""


@dataclass
class InlineTextSegment:
    text: str
    vertical_align: str | None = None


TEX_COMMAND_TEXT_MAP = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "ell": "ℓ",
    "cdot": "·",
    "times": "×",
    "pm": "±",
    "mp": "∓",
    "sim": "∼",
    "approx": "≈",
    "leq": "≤",
    "geq": "≥",
    "neq": "≠",
    "rightarrow": "→",
    "leftarrow": "←",
    "to": "→",
    "degree": "°",
    "circ": "°",
    "infty": "∞",
}


def tex_command_to_text(command: str) -> str:
    return TEX_COMMAND_TEXT_MAP.get(command, command)


def split_tex_group(text: str, start: int) -> tuple[str, int]:
    if start >= len(text):
        return "", start
    if text[start] == "{":
        depth = 0
        pieces: list[str] = []
        index = start
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
                if depth > 1:
                    pieces.append(char)
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return "".join(pieces), index + 1
                pieces.append(char)
            else:
                pieces.append(char)
            index += 1
        return "".join(pieces), index
    if text[start] == "\\":
        command_match = re.match(r"\\([A-Za-z]+)", text[start:])
        if command_match:
            command = command_match.group(1)
            next_index = start + len(command) + 1
            if next_index < len(text) and text[next_index] == "{":
                group, final_index = split_tex_group(text, next_index)
                return f"\\{command}{{{group}}}", final_index
            return f"\\{command}", next_index
        if start + 1 < len(text):
            return text[start + 1], start + 2
    return text[start], start + 1


def parse_texish_segments(text: str) -> list[InlineTextSegment]:
    text = strip_tex_wrappers(text)
    segments: list[InlineTextSegment] = []
    plain_buffer: list[str] = []

    def flush_plain():
        if plain_buffer:
            segments.append(InlineTextSegment("".join(plain_buffer)))
            plain_buffer.clear()

    index = 0
    while index < len(text):
        char = text[index]
        if char in "_^":
            flush_plain()
            atom, next_index = split_tex_group(text, index + 1)
            atom_text = strip_tex_wrappers(atom).strip("{}").strip()
            if atom_text:
                segments.append(
                    InlineTextSegment(
                        atom_text,
                        "subscript" if char == "_" else "superscript",
                    )
                )
            index = next_index
            continue
        if char in "{}":
            index += 1
            continue
        if char == "\\":
            command_match = re.match(r"\\([A-Za-z]+)", text[index:])
            if command_match:
                plain_buffer.append(tex_command_to_text(command_match.group(1)))
                index += len(command_match.group(1)) + 1
                continue
            if index + 1 < len(text):
                escaped_map = {"%": "%", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", "~": "~"}
                plain_buffer.append(escaped_map.get(text[index + 1], text[index + 1]))
                index += 2
                continue
        plain_buffer.append(char)
        index += 1

    flush_plain()
    return [segment for segment in segments if segment.text]


def parse_raw_html_table(table_html: str) -> list[list[HtmlTableCell]]:
    parser = RawHtmlTableParser()
    parser.feed(table_html)
    parser.close()
    return parser.rows


def extract_word_export_tables(markdown: str) -> tuple[str, list[HtmlTablePlaceholder]]:
    placeholders: list[HtmlTablePlaceholder] = []
    table_pattern = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)

    def replace_table(match: re.Match) -> str:
        rows = parse_raw_html_table(match.group(0))
        if not rows:
            return match.group(0)
        marker = f"MINERU_HTML_TABLE_PLACEHOLDER_{len(placeholders) + 1:04d}"
        placeholders.append(HtmlTablePlaceholder(marker=marker, rows=rows))
        return f"\n\n{marker}\n\n"

    return table_pattern.sub(replace_table, markdown), placeholders


def make_word_export_markdown(
    markdown_path: Path,
    style: ExportStyleSettings | None = None,
) -> tuple[Path, list[HtmlTablePlaceholder]]:
    raw = markdown_path.read_text(encoding="utf-8", errors="replace")
    raw = normalize_markdown_for_export(
        raw,
        export_style_markdown_image_width(style),
        layout_image_width_percentages(markdown_path),
    )
    raw, placeholders = extract_word_export_tables(raw)
    raw = separate_markdown_images_for_word(raw)
    temp_path = markdown_path.with_name(f".{markdown_path.stem}.word-export.md")
    temp_path.write_text(raw, encoding="utf-8")
    return temp_path, placeholders


def split_markdown_table_cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in row:
        if char == "\\" and not escaped:
            escaped = True
            current.append(char)
            continue
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        escaped = False
    cells.append("".join(current).strip())
    return cells


def is_markdown_separator_row(row: str) -> bool:
    cells = split_markdown_table_cells(row)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells if cell.strip())


def markdown_table_row(cells: list[str], width: int) -> str:
    padded = [cell.strip() for cell in cells[:width]]
    padded.extend([""] * (width - len(padded)))
    return "| " + " | ".join(padded) + " |"


def repair_pipe_table_block(lines: list[str]) -> list[str] | None:
    rows: list[list[str]] = []
    for raw_line in lines:
        pieces = [piece for piece in re.split(r"\s*\|\|\s*", raw_line.strip()) if piece.strip()]
        for piece in pieces:
            if "|" not in piece:
                continue
            piece = piece.strip()
            if not piece.startswith("|"):
                piece = "| " + piece
            if not piece.endswith("|"):
                piece = piece + " |"
            if is_markdown_separator_row(piece):
                continue
            cells = split_markdown_table_cells(piece)
            if len(cells) >= 2:
                rows.append(cells)
    if len(rows) < 2:
        return None
    width = max(len(row) for row in rows)
    if width < 2:
        return None
    merged_rows: list[list[str]] = []
    for row in rows:
        if merged_rows and len(merged_rows[-1]) < width and len(row) < width:
            merged_rows[-1].extend(row)
        else:
            merged_rows.append(row)
    rows = merged_rows
    repaired = [markdown_table_row(rows[0], width)]
    repaired.append("| " + " | ".join(["---"] * width) + " |")
    repaired.extend(markdown_table_row(row, width) for row in rows[1:])
    return repaired


def repair_malformed_pipe_tables(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    block: list[str] = []
    in_fence = False

    def flush_block() -> None:
        nonlocal block
        if not block:
            return
        candidate = repair_pipe_table_block(block)
        output.extend(candidate if candidate else block)
        block = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            flush_block()
            in_fence = not in_fence
            output.append(line)
            continue
        if in_fence:
            output.append(line)
            continue
        is_pipeish = "|" in line and not stripped.startswith("![")
        is_likely_table = is_pipeish and (
            stripped.startswith("|")
            or stripped.endswith("|")
            or "||" in stripped
            or is_markdown_separator_row(stripped)
        )
        if is_likely_table:
            block.append(line)
            continue
        flush_block()
        output.append(line)
    flush_block()
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(output) + suffix


def research_css(
    style: ExportStyleSettings | None = None,
    use_bundled_reader_font: bool = True,
) -> str:
    style = resolve_export_style(style)

    # 阅读预览使用程序内置思源宋体；导出 HTML 仍尊重用户的导出字体设置，
    # 不引用程序私有运行时目录，避免生成不可移植的导出文件。
    font_face_css = (
        bundled_reader_font_face_css()
        if use_bundled_reader_font
        else ""
    )
    body_font_stack = (
        f'"{BUNDLED_READER_FONT_CSS_FAMILY}", '
        f'"{style.body_font_latin}", "{style.body_font_cjk}", serif'
        if use_bundled_reader_font
        else f'"{style.body_font_latin}", "{style.body_font_cjk}", serif'
    )

    return """
%(font_face_css)s
body {
  max-width: 980px;
  margin: 0 auto;
  padding: 24px 32px 42px;
  color: #1f2933;
  background: #fbfcfd;
  line-height: var(--reader-line-height, %(line_spacing_pt)spt);
  font-family: %(body_font_stack)s;
  font-size: var(--reader-font-size, %(body_font_pt)spt);
}
h1, h2, h3, h4 { color: #123044; letter-spacing: 0; line-height: 1.25; }
h1, h2, h3, h4, h5, h6 {
  font-family: %(heading_font_stack)s;
  font-weight: 700;
}
h1 { font-size: %(heading1_pt)spt; border-bottom: 1px solid #d8e2ea; padding-bottom: 8px; margin: 0.7em 0 0.45em; }
h2 { font-size: %(heading2_pt)spt; margin: 1.1em 0 0.45em; }
h3, h4, h5, h6 { font-size: %(heading3_pt)spt; margin: 0.95em 0 0.35em; }
p {
  margin: 0.45em 0;
  text-indent: %(indent_cm).2fcm;
  text-align: justify;
}
/* 只对图片段落、列表内段落、引用段落取消首行缩进。
   不再对 p:has(> .math) 全局取消缩进，避免含公式的普通正文段落丢失首行缩进。 */
p:has(img), li p, blockquote p { text-indent: 0; text-align: left; }
a { color: #176b87; }
table {
  border-collapse: collapse;
  width: auto;
  min-width: min(100%%, 560px);
  max-width: 100%%;
  margin: 0.75em auto;
  font-size: 11pt;
  line-height: 1.18;
  background: #ffffff;
  border-top: 2px solid #303840;
  border-bottom: 2px solid #303840;
  box-shadow: none;
}
th, td {
  border: none;
  padding: 3px 7px;
  vertical-align: middle;
  text-align: center;
}
th {
  background: transparent;
  color: #123044;
  font-weight: 650;
  border-bottom: 1.6px solid #5d6872;
}
td:first-child, th:first-child { text-align: left; }
tr:nth-child(even) td { background: transparent; }
pre, code { font-family: Consolas, "Cascadia Mono", monospace; }
pre {
  background: #f4f7f9;
  border: 1px solid #dce5eb;
  border-radius: 6px;
  padding: 12px;
  overflow: auto;
}
img {
  display: block;
  /* Layout-backed images receive an inline source-page percentage. Documents
     without layout metadata retain the bitmap's intrinsic size. In both cases
     this max-width only prevents overflow when the reading pane narrows. */
  max-width: 100%%;
  width: auto;
  height: auto;
  object-fit: contain;
  margin: 0.75em auto;
  cursor: zoom-in;
}
.math, mjx-container { overflow-x: auto; overflow-y: hidden; max-width: 100%%; }
.sync-anchor { display: block; scroll-margin-top: 18px; height: 1px; }
.table-wrap {
  overflow-x: auto;
  max-width: 100%%;
  margin: 0.75em auto;
  text-align: center;
  display: block;
}
.table-wrap table {
  display: inline-table;
  margin-left: auto;
  margin-right: auto;
}
.caption-like {
  text-align: center;
  text-indent: 0;
  /* 题注不再默认强加粗，避免图题/表题识别边界情况下出现大段正文加粗。 */
  font-weight: 500;
  color: #25313a;
  margin: 0.7em auto 0.25em;
  font-size: %(caption_font_pt)spt;
}
#image-lightbox {
  position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
  background: rgba(16, 24, 32, 0.76); z-index: 9999; cursor: grab;
}
#image-lightbox.open { display: flex; }
#image-lightbox img { max-width: none; max-height: none; margin: 0; cursor: grab; transform-origin: 0 0; }
#image-lightbox .hint {
  position: fixed; left: 18px; bottom: 14px; color: #eef5f7; font-size: 12px;
  background: rgba(10, 20, 30, 0.54); padding: 6px 9px; border-radius: 5px;
}
mjx-container { cursor: zoom-in; }
#reader-formula-lightbox {
  position: fixed; inset: 0; display: none; align-items: center; justify-content: center;
  background: rgba(16, 24, 32, 0.78); z-index: 9998; cursor: grab; overflow: hidden;
  touch-action: none; user-select: none;
}
#reader-formula-lightbox.open { display: flex; }
#reader-formula-lightbox .formula-stage {
  display: flex; align-items: center; justify-content: center;
  padding: 24px; color: #111827; background: #ffffff; border-radius: 8px;
  box-shadow: 0 14px 42px rgba(0, 0, 0, 0.34); transform-origin: center center;
}
#reader-formula-lightbox .formula-stage mjx-container {
  margin: 0 !important; overflow: visible !important; cursor: grab;
}
#reader-formula-lightbox .hint {
  position: fixed; left: 18px; bottom: 14px; color: #eef5f7; font-size: 12px;
  background: rgba(10, 20, 30, 0.54); padding: 6px 9px; border-radius: 5px;
}
.reader-formula-placeholder { display: inline-block; visibility: hidden; }
@page {
  size: A4;
  margin: 25.4mm 31.8mm 25.4mm 31.8mm;
}
@media print {
  html, body {
    background: #ffffff !important;
  }
  body {
    max-width: none;
    padding: 0;
    color: #111827;
    font-size: 9pt;
    line-height: 1.25;
  }
  h1, h2, h3, h4 {
    break-after: avoid;
    page-break-after: avoid;
  }
  h1 {
    font-size: 19pt;
  }
  h2 {
    font-size: 15pt;
  }
  h3 {
    font-size: 12.5pt;
  }
  p, li, blockquote {
    orphans: 2;
    widows: 2;
  }
  img {
    /* Per-image source-page ratios are emitted inline. Printing must only
       constrain overflow, not replace those ratios with one global size. */
    max-width: 100%%;
    max-height: 95mm;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  table {
    width: auto;
    max-width: 100%%;
    min-width: 0;
    font-size: 8.5pt;
    break-inside: auto;
    page-break-inside: auto;
  }
  tr {
    break-inside: avoid;
    page-break-inside: avoid;
  }
  th, td {
    padding: 2px 4px;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .table-wrap {
    overflow: visible;
    max-width: 100%%;
    display: flex;
    justify-content: center;
  }
  pre {
    white-space: pre-wrap;
    break-inside: avoid;
    page-break-inside: avoid;
  }
  #image-lightbox,
  #reader-formula-lightbox,
  .sync-anchor {
    display: none !important;
  }
}
""" % {
        "font_face_css": font_face_css,
        "body_font_stack": body_font_stack,
        "heading_font_stack": f'"{style.heading_font_latin}", "{style.heading_font_cjk}", sans-serif',
        "body_font_pt": clamp_int(style.body_font_pt, 10, 16, 12),
        "heading1_pt": clamp_int(style.heading1_pt, 12, 22, 15),
        "heading2_pt": clamp_int(style.heading2_pt, 12, 20, 14),
        "heading3_pt": clamp_int(style.heading3_pt, 11, 18, 13),
        "caption_font_pt": clamp_int(style.caption_font_pt, 9, 14, 11),
        "line_spacing_pt": clamp_int(style.line_spacing_pt, 15, 30, 20),
        "indent_cm": clamp_float(style.first_line_indent_cm, 0.0, 2.0, 0.8),
    }


def research_script() -> str:
    return r"""
(() => {
  const boot = () => {
    if (!document.body) return;
    if (window.__mineruReaderBooted) return;
    window.__mineruReaderBooted = true;
    // 图题/表题识别要保守：避免把 “Figure 1 shows ...” / “Table 1 shows ...” 正文句误判为 caption。
    const captionLeadRe = /^(?:图|表)\s*[\d一二三四五六七八九十IVXivx]+(?=\s*[（(.:：．、\-—]|$)|^(?:Figure|Fig\.?|Table)\s*\d+[A-Za-z]?(?=\s*[\(\[.:：．、\-—]|$)/i;
    const captionDescRe = /^(?:As shown|Figure|Fig\.?|Table|图|表)\b/i;
    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const isCaptionLead = (text) => captionLeadRe.test(normalizeText(text));
    const isCaptionContinuation = (text) => {
      const normalized = normalizeText(text);
      if (!normalized || normalized.length > 120) return false;
      if (isCaptionLead(normalized) || captionDescRe.test(normalized)) return false;
      return /^(?:[\(\[]?[a-z0-9]|at\s+\d|and\s+|or\s+|with\s+|where\s+|when\s+|[（(]?[a-z0-9])/i.test(normalized)
        || /[。.]\s*$/u.test(normalized);
    };

    document.querySelectorAll('details').forEach((node) => { node.open = true; });
    document.querySelectorAll('[aria-expanded="false"]').forEach((node) => {
      node.setAttribute('aria-expanded', 'true');
    });
    document.querySelectorAll('.collapsed,.collapse,.folded,[hidden]').forEach((node) => {
      node.hidden = false;
      node.classList.remove('collapsed', 'folded');
      node.style.display = '';
      node.style.maxHeight = 'none';
      node.style.height = 'auto';
      node.style.overflow = 'visible';
    });

    document.querySelectorAll('table').forEach((table) => {
      if (!table.parentNode) return;
      if (table.parentElement && table.parentElement.classList.contains('table-wrap')) return;
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    });

    const blocks = Array.from(document.querySelectorAll('p, figcaption'));
    let recentVisualAnchor = false;
    let previousCaption = false;
    blocks.forEach((node) => {
      const text = normalizeText(node.textContent || '');
      const html = node.innerHTML || '';
      const isAnchorOnly = /sync-anchor/.test(html) && !text;
      const isVisualAnchor = /<img\b/i.test(html) || /<table\b/i.test(html) || /class=["'][^"']*table-wrap/.test(html);
      if (!isAnchorOnly && text) {
        if ((isCaptionLead(text) && (recentVisualAnchor || previousCaption)) || (previousCaption && isCaptionContinuation(text))) {
          node.classList.add('caption-like');
          previousCaption = true;
          recentVisualAnchor = false;
          return;
        }
        previousCaption = node.classList.contains('caption-like');
        recentVisualAnchor = false;
        return;
      }
      if (!isAnchorOnly) {
        recentVisualAnchor = isVisualAnchor;
        previousCaption = false;
      }
    });

  let box = document.getElementById('image-lightbox');
  if (!box) {
    box = document.createElement('div');
    box.id = 'image-lightbox';
    box.innerHTML = '<img alt=""><div class="hint">滚轮缩放 · 左键拖动 · 单击退出</div>';
    document.body.appendChild(box);
  }
  const img = box.querySelector('img');
  if (!img) return;
  let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0, moved = false;
  const apply = () => { img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; };
  const stopDrag = () => { dragging = false; box.style.cursor = 'grab'; };
  const close = () => { stopDrag(); box.classList.remove('open'); img.removeAttribute('src'); };
  const reset = () => { scale = 1; tx = 0; ty = 0; moved = false; apply(); };
  document.querySelectorAll('img').forEach((node) => {
    if (node.closest('#image-lightbox')) return;
    if (node.__mineruLightboxBound) return;
    node.__mineruLightboxBound = true;
    node.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      img.src = node.currentSrc || node.src;
      box.classList.add('open');
      reset();
    });
  });
  box.addEventListener('click', (event) => {
    if (!moved) close();
    moved = false;
  });
  box.addEventListener('dblclick', (event) => { event.preventDefault(); reset(); });
  box.addEventListener('wheel', (event) => {
    if (!box.classList.contains('open')) return;
    event.preventDefault();
    const previous = scale;
    const factor = event.deltaY < 0 ? 1.12 : 0.89;
    scale = Math.min(8, Math.max(0.25, scale * factor));
    const rect = img.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    tx -= px * (scale / previous - 1);
    ty -= py * (scale / previous - 1);
    apply();
  }, { passive: false });
  img.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || !box.classList.contains('open')) return;
    event.preventDefault();
    dragging = true; moved = false; sx = event.clientX - tx; sy = event.clientY - ty; box.style.cursor = 'grabbing';
  });
  window.addEventListener('mousemove', (event) => {
    if (!dragging) return;
    const nextX = event.clientX - sx;
    const nextY = event.clientY - sy;
    if (Math.abs(nextX - tx) > 2 || Math.abs(nextY - ty) > 2) moved = true;
    tx = nextX; ty = nextY; apply();
  });
  window.addEventListener('mouseup', stopDrag);
  window.addEventListener('blur', stopDrag);
  window.addEventListener('keydown', (event) => { if (event.key === 'Escape') close(); });

  // MathJax creates formula nodes asynchronously, so event delegation keeps the
  // viewer available without binding each formula during page startup.
  (() => {
    let fbox = document.getElementById('reader-formula-lightbox');
    if (!fbox) {
      fbox = document.createElement('div');
      fbox.id = 'reader-formula-lightbox';
      fbox.innerHTML = '<div class="formula-stage"></div><div class="hint">滚轮缩放 · 左键拖动 · 单击退出</div>';
      document.body.appendChild(fbox);
    }
    const stage = fbox.querySelector('.formula-stage');
    if (!stage) return;
    let active = null, placeholder = null, originalStyle = '';
    let fscale = 2.2, ftx = 0, fty = 0, fdrag = false, fmoved = false, fsx = 0, fsy = 0;
    const fapply = () => { stage.style.transform = `translate(${ftx}px, ${fty}px) scale(${fscale})`; };
    const restore = () => {
      if (active && placeholder && placeholder.parentNode) {
        placeholder.replaceWith(active);
        active.style.cssText = originalStyle;
      }
      active = null;
      placeholder = null;
    };
    const fclose = () => {
      fdrag = false;
      fbox.classList.remove('open');
      restore();
      stage.style.transform = '';
      stage.textContent = '';
    };
    const fopen = (container) => {
      if (!container || fbox.classList.contains('open')) return;
      const rect = container.getBoundingClientRect();
      placeholder = document.createElement(container.tagName.toLowerCase() === 'mjx-container' ? 'span' : 'div');
      placeholder.className = 'reader-formula-placeholder';
      placeholder.style.width = `${Math.max(1, rect.width)}px`;
      placeholder.style.height = `${Math.max(1, rect.height)}px`;
      container.before(placeholder);
      active = container;
      originalStyle = container.style.cssText;
      stage.appendChild(container);
      container.style.transform = '';
      container.style.position = 'relative';
      container.style.left = '0';
      fscale = Math.max(0.2, Math.min(4, Math.min(
        (window.innerWidth * 0.86) / Math.max(1, rect.width),
        (window.innerHeight * 0.76) / Math.max(1, rect.height)
      )));
      ftx = 0; fty = 0; fmoved = false; fapply();
      fbox.classList.add('open');
    };
    document.addEventListener('click', (event) => {
      const container = event.target && event.target.closest ? event.target.closest('mjx-container') : null;
      if (!container || container.closest('#reader-formula-lightbox') || event.ctrlKey || event.metaKey || event.shiftKey) return;
      event.preventDefault();
      event.stopPropagation();
      fopen(container);
    }, true);
    fbox.addEventListener('click', () => { if (!fmoved) fclose(); });
    fbox.addEventListener('wheel', (event) => {
      if (!fbox.classList.contains('open')) return;
      event.preventDefault();
      const previous = fscale;
      fscale = Math.max(0.4, Math.min(10, fscale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
      ftx -= (event.clientX - window.innerWidth / 2 - ftx) * (fscale / previous - 1);
      fty -= (event.clientY - window.innerHeight / 2 - fty) * (fscale / previous - 1);
      fapply();
    }, { passive: false });
    stage.addEventListener('mousedown', (event) => {
      if (event.button !== 0 || !fbox.classList.contains('open')) return;
      event.preventDefault();
      fdrag = true; fmoved = false; fsx = event.clientX - ftx; fsy = event.clientY - fty;
    });
    window.addEventListener('mousemove', (event) => {
      if (!fdrag) return;
      const nextX = event.clientX - fsx, nextY = event.clientY - fsy;
      if (Math.abs(nextX - ftx) + Math.abs(nextY - fty) > 3) fmoved = true;
      ftx = nextX; fty = nextY; fapply();
    });
    window.addEventListener('mouseup', () => { fdrag = false; });
    window.addEventListener('blur', () => { fdrag = false; });
    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && fbox.classList.contains('open')) fclose();
    });
  })();

  const syncRoot = () => document.scrollingElement || document.documentElement;
  const syncMaxScroll = () => Math.max(1, syncRoot().scrollHeight - window.innerHeight);
  const syncPageY = (node) => syncRoot().scrollTop + node.getBoundingClientRect().top;
  const SYNC_PROGRAMMATIC_SCROLL_MS = 64;
  let syncAnchorCache = null;
  let syncAnchorCacheHeight = 0;

  const invalidateSyncAnchorCache = () => {
    syncAnchorCache = null;
    syncAnchorCacheHeight = 0;
  };

  const buildSyncAnchors = (force = false) => {
    const root = syncRoot();
    const cacheHeight = root.scrollHeight;
    if (!force && syncAnchorCache && syncAnchorCacheHeight === cacheHeight) {
      return syncAnchorCache;
    }

    const anchors = [];
    anchors.push({ key: '__top__', kind: 'edge', y: 0, node: null });

    document.querySelectorAll('img').forEach((img, index) => {
      if (img.closest('#image-lightbox')) return;
      anchors.push({
        key: `image:${String(index + 1).padStart(4, '0')}`,
        kind: 'image',
        y: syncPageY(img),
        node: img
      });
    });

    document.querySelectorAll('table').forEach((table, index) => {
      anchors.push({
        key: `table:${String(index + 1).padStart(4, '0')}`,
        kind: 'table',
        y: syncPageY(table),
        node: table
      });
    });

    const headingCounts = new Map();
    document.querySelectorAll('.sync-heading[id]').forEach((heading) => {
      const level = Number(heading.dataset.level || 0) || 0;
      const nextIndex = (headingCounts.get(level) || 0) + 1;
      headingCounts.set(level, nextIndex);
      anchors.push({
        key: `heading:${level}:${String(nextIndex).padStart(4, '0')}`,
        kind: 'heading',
        y: syncPageY(heading),
        node: heading
      });
    });

    document.querySelectorAll('.sync-anchor[id]').forEach((anchor) => {
      anchors.push({
        key: `block:${anchor.id}`,
        kind: 'block',
        y: syncPageY(anchor),
        node: anchor
      });
    });

    anchors.push({ key: '__bottom__', kind: 'edge', y: syncMaxScroll(), node: null });
    anchors.sort((a, b) => a.y - b.y);

    const deduped = [];
    const seen = new Set();
    for (const anchor of anchors) {
      const token = `${anchor.key}@${Math.round(anchor.y)}`;
      if (seen.has(token)) continue;
      seen.add(token);
      deduped.push(anchor);
    }

    syncAnchorCache = deduped;
    syncAnchorCacheHeight = cacheHeight;
    return deduped;
  };

  const currentFocusImage = () => {
    const viewportCenter = window.innerHeight * 0.42;
    let best = null;
    document.querySelectorAll('img').forEach((img, index) => {
      if (img.closest('#image-lightbox')) return;
      const rect = img.getBoundingClientRect();
      const visible = rect.bottom > 0 && rect.top < window.innerHeight;
      const center = (rect.top + rect.bottom) / 2;
      const distance = Math.abs(center - viewportCenter);
      if (!best || distance < best.distance) {
        best = {
          key: `image:${String(index + 1).padStart(4, '0')}`,
          top: rect.top,
          bottom: rect.bottom,
          distance,
          visible
        };
      }
    });
    if (!best) return null;
    if (!best.visible && best.distance > Math.max(180, window.innerHeight * 0.3)) return null;
    return {
      key: best.key,
      viewportTop: best.top
    };
  };

  const syncPositionPayload = () => {
    const y = syncRoot().scrollTop;
    const anchors = buildSyncAnchors();
    let previous = anchors[0];
    let next = anchors[anchors.length - 1];

    for (let i = 0; i < anchors.length; i += 1) {
      if (anchors[i].y <= y) previous = anchors[i];
      if (anchors[i].y >= y) {
        next = anchors[i];
        break;
      }
    }

    const span = Math.max(1, next.y - previous.y);
    const focusImage = currentFocusImage();
    return {
      ratio: Math.max(0, Math.min(1, y / syncMaxScroll())),
      previousKey: previous.key,
      nextKey: next.key,
      localRatio: Math.max(0, Math.min(1, (y - previous.y) / span)),
      offset: y - previous.y,
      focusImageKey: focusImage ? focusImage.key : '',
      focusImageViewportTop: focusImage ? focusImage.viewportTop : null
    };
  };

  const scrollToSyncPayload = (payload, smooth = false) => {
    if (!payload) return false;
    const anchors = buildSyncAnchors();
    const byKey = new Map(anchors.map((anchor) => [anchor.key, anchor]));
    const previous = byKey.get(payload.previousKey);
    const next = byKey.get(payload.nextKey);
    const focusImageAnchor = payload.focusImageKey ? byKey.get(payload.focusImageKey) : null;
    let top = null;

    if (focusImageAnchor && focusImageAnchor.kind === 'image' && focusImageAnchor.node && payload.focusImageViewportTop !== null && payload.focusImageViewportTop !== undefined) {
      top = focusImageAnchor.y - Number(payload.focusImageViewportTop || 0);
    } else if (previous && next && next.y >= previous.y) {
      top = previous.y + (next.y - previous.y) * Number(payload.localRatio || 0);
    } else if (previous) {
      top = previous.y + Number(payload.offset || 0);
    }

    if (top === null || Number.isNaN(top)) {
      top = Number(payload.ratio || 0) * syncMaxScroll();
    }

    top = Math.max(0, Math.min(syncMaxScroll(), top));
    window.__mineruProgrammaticScrollUntil = Date.now() + SYNC_PROGRAMMATIC_SCROLL_MS;
    window.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
    return true;
  };

  window.addEventListener('resize', () => {
    invalidateSyncAnchorCache();
    requestAnimationFrame(() => buildSyncAnchors(true));
  }, { passive: true });
  document.querySelectorAll('img').forEach((img) => {
    const refreshAnchors = () => {
      invalidateSyncAnchorCache();
      requestAnimationFrame(() => buildSyncAnchors(true));
    };
    img.addEventListener('load', refreshAnchors, { once: true });
    img.addEventListener('error', refreshAnchors, { once: true });
  });
  window.addEventListener('load', () => buildSyncAnchors(true), { once: true });
  setTimeout(() => buildSyncAnchors(true), 80);
  setTimeout(() => buildSyncAnchors(true), 600);

  window.syncScrollApi = {
    scrollRatio: () => {
      const root = syncRoot();
      return Math.max(0, Math.min(1, root.scrollTop / syncMaxScroll()));
    },
    scrollToRatio: (ratio, smooth = true) => {
      const top = Math.max(0, Math.min(syncMaxScroll(), Number(ratio || 0) * syncMaxScroll()));
      window.__mineruProgrammaticScrollUntil = Date.now() + SYNC_PROGRAMMATIC_SCROLL_MS;
      window.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
    },
    syncPayload: syncPositionPayload,
    scrollToSyncPayload,
    currentHeadingPosition: () => {
      const headings = [...document.querySelectorAll('.sync-heading[id]')];
      if (!headings.length) return null;
      let best = headings[0];
      let bestTop = -Infinity;
      for (const heading of headings) {
        const top = heading.getBoundingClientRect().top;
        if (top <= 80 && top > bestTop) { best = heading; bestTop = top; }
      }
      if (bestTop === -Infinity) {
        best = headings.reduce((current, heading) => {
          return Math.abs(heading.getBoundingClientRect().top) < Math.abs(current.getBoundingClientRect().top) ? heading : current;
        }, headings[0]);
      }
      return {
        id: best.id,
        level: Number(best.dataset.level || 0),
        title: best.dataset.title || '',
        top: best.getBoundingClientRect().top
      };
    },
    scrollToHeadingPosition: (position, smooth = true) => {
      if (!position) return false;
      const headings = [...document.querySelectorAll('.sync-heading[id]')];
      if (!headings.length) return false;
      let target = document.getElementById(position.id);
      if (!target && position.title) {
        target = headings.find((heading) => heading.dataset.title === position.title && Number(heading.dataset.level || 0) === Number(position.level || 0));
      }
      if (!target && position.title) {
        target = headings.find((heading) => heading.dataset.title === position.title);
      }
      if (!target) return false;
      const root = syncRoot();
      const targetTop = root.scrollTop + target.getBoundingClientRect().top - Number(position.top || 0);
      window.__mineruProgrammaticScrollUntil = Date.now() + SYNC_PROGRAMMATIC_SCROLL_MS;
      window.scrollTo({ top: Math.max(0, targetTop), behavior: smooth ? 'smooth' : 'auto' });
      return true;
    },
    nearestAnchor: () => {
      const anchors = [...document.querySelectorAll('.sync-anchor[id]')];
      let best = anchors[0] ? anchors[0].id : '';
      let bestDistance = Number.POSITIVE_INFINITY;
      for (const anchor of anchors) {
        const distance = Math.abs(anchor.getBoundingClientRect().top);
        if (distance < bestDistance) { best = anchor.id; bestDistance = distance; }
      }
      return best;
    },
    scrollToAnchor: (id) => {
      const anchor = document.getElementById(id);
      if (anchor) anchor.scrollIntoView({ block: 'start', behavior: 'auto' });
    },
    selectedText: () => window.getSelection ? String(window.getSelection()) : ''
  };
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
"""


def normalize_raw_html_table_math(text: str) -> str:
    cell_pattern = re.compile(r"(<t[dh]\b[^>]*>)(.*?)(</t[dh]>)", re.IGNORECASE | re.DOTALL)
    math_pattern = re.compile(r"(?<!\\)\$(?![\s$])(.+?)(?<!\\)\$(?!\d)", re.DOTALL)

    def replace_cell(match: re.Match) -> str:
        start, body, end = match.groups()
        if "$" not in body:
            return match.group(0)
        body = math_pattern.sub(lambda math: r"\(" + math.group(1).strip() + r"\)", body)
        return start + body + end

    return cell_pattern.sub(replace_cell, text)


def normalize_html_image_width_attributes(text: str) -> str:
    image_width_pattern = re.compile(
        r"(<img\b[^>]*?)(/?>)\s*\{width=(?P<width>\d+(?:\.\d+)?)%\}",
        re.IGNORECASE,
    )

    def replace_image(match: re.Match) -> str:
        prefix = match.group(1)
        closing = match.group(2)
        width = match.group("width")
        if re.search(r"\bstyle\s*=", prefix, flags=re.IGNORECASE):
            return match.group(1) + closing
        return f'{prefix} style="width:{width}%" {closing}'

    return image_width_pattern.sub(replace_image, text)


def is_caption_lead_text(text: str) -> bool:
    stripped = re.sub(r"\s+", " ", str(text or "")).strip()
    if not stripped:
        return False
    return bool(
        re.match(
            r"^(?:(?:图|表)\s*[\d一二三四五六七八九十IVXivx]+(?=\s*[（(.:：．、\-—]|$)|(?:Figure|Fig\.?|Table)\s*\d+[A-Za-z]?(?=\s*[\(\[.:：．、\-—]|$))",
            stripped,
            re.I,
        )
    )


def strip_html_text(text: str) -> str:
    cleaned = re.sub(r"<br\s*/?>", "\n", str(text or ""), flags=re.I)
    cleaned = re.sub(r"</p\s*>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return html.unescape(re.sub(r"\s+", " ", cleaned)).strip()


def looks_like_caption_continuation(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", " ", stripped)
    if len(compact) > 120:
        return False
    if is_caption_lead_text(compact):
        return False
    if re.match(r"^(?:As shown|Figure|Fig\.?|Table|图|表)\b", compact, re.I):
        return False
    return bool(
        re.match(r"^(?:[\(\[]?[a-z0-9]|at\s+\d|and\s+|or\s+|with\s+|where\s+|when\s+|[（(]?[a-z0-9])", compact, re.I)
        or compact.endswith((".", "。"))
    )


def mark_caption_like_paragraphs(text: str) -> str:
    paragraph_pattern = re.compile(r"<p(?P<attrs>[^>]*)>(?P<body>.*?)</p>", re.I | re.S)
    image_only_pattern = re.compile(r"^\s*(?:<a\b[^>]*>\s*</a>\s*)*(?:<img\b[^>]*>\s*)+\s*$", re.I | re.S)
    table_only_pattern = re.compile(
        r'^\s*(?:<a\b[^>]*>\s*</a>\s*)*(?:<div\b[^>]*class="table-wrap"[^>]*>.*?</div>|<table\b.*?</table>)\s*$',
        re.I | re.S,
    )
    anchor_only_pattern = re.compile(
        r'^\s*(?:<a\b[^>]*class="sync-anchor"[^>]*>\s*</a>|<span\b[^>]*class="sync-anchor"[^>]*>\s*</span>|<a\b[^>]*id="doc-block-[^"]*"[^>]*>\s*</a>)+\s*$',
        re.I | re.S,
    )
    parts: list[str] = []
    last_index = 0
    recent_visual_anchor = False
    previous_caption = False

    for match in paragraph_pattern.finditer(text):
        parts.append(text[last_index:match.start()])
        attrs = match.group("attrs") or ""
        body = match.group("body") or ""
        raw_text = strip_html_text(body)
        attrs_has_caption = re.search(r'\bclass\s*=\s*"[^"]*\bcaption-like\b', attrs, re.I) is not None
        is_anchor_only = bool(anchor_only_pattern.fullmatch(body))
        is_visual_anchor = bool(image_only_pattern.fullmatch(body) or table_only_pattern.fullmatch(body))
        mark_caption = attrs_has_caption

        if not mark_caption and raw_text:
            if is_caption_lead_text(raw_text) and (recent_visual_anchor or previous_caption):
                mark_caption = True
            elif previous_caption and looks_like_caption_continuation(raw_text):
                mark_caption = True

        if mark_caption and not attrs_has_caption:
            class_match = re.search(r'(\bclass\s*=\s*")([^"]*)(")', attrs, re.I)
            if class_match:
                attrs = attrs[:class_match.start(2)] + class_match.group(2).strip() + " caption-like" + attrs[class_match.end(2):]
            else:
                attrs += ' class="caption-like"'

        parts.append(f"<p{attrs}>{body}</p>")
        last_index = match.end()

        if is_anchor_only:
            continue
        recent_visual_anchor = is_visual_anchor
        previous_caption = mark_caption
        if raw_text and not mark_caption:
            recent_visual_anchor = False

    parts.append(text[last_index:])
    return "".join(parts)


def polish_html(
    html_path: Path,
    style: ExportStyleSettings | None = None,
    use_bundled_reader_font: bool = True,
) -> None:
    text = html_path.read_text(encoding="utf-8", errors="replace")
    text = normalize_raw_html_table_math(text)
    text = normalize_html_image_width_attributes(text)
    text = mark_caption_like_paragraphs(text)
    text = re.sub(
        r"<!-- mineru-reader-polish -->.*?<!-- /mineru-reader-polish -->",
        "",
        text,
        flags=re.DOTALL,
    )
    extra = (
        f"<!-- {READER_POLISH_MARKER} -->\n"
        "<!-- mineru-reader-polish -->\n"
        f"<style>{research_css(style, use_bundled_reader_font)}</style>\n"
        f"<script>{research_script()}</script>\n"
        "<!-- /mineru-reader-polish -->\n"
    )
    if "</head>" in text:
        text = text.replace("</head>", extra + "</head>", 1)
    else:
        text = extra + text
    html_path.write_text(text, encoding="utf-8")


_GARBLED_EQUATION_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:\\?[~～〜]\s*)([A-Za-z]?\d+[A-Za-z]?|[a-z])\s*[!！](?![A-Za-z0-9])"
)


def normalize_garbled_equation_references_for_display(markdown: str) -> str:
    """Repair MinerU's `\\~2!` OCR artifact in an in-memory preview copy."""
    return _GARBLED_EQUATION_REFERENCE_RE.sub(
        lambda match: f"({match.group(1)})",
        str(markdown or ""),
    )


def render_preview_html_internal(
    markdown_path: Path,
    workspace: Path,
    log=None,
    anchor_blocks: bool = True,
    export_mode: bool = False,
    style: ExportStyleSettings | None = None,
) -> Path | None:
    markdown_path = markdown_path.resolve()
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    prefix = "export" if export_mode else "preview"
    html_path = markdown_path.with_name(f"{prefix}.{safe_stem}.html")
    if not export_mode and polished_preview_cache_is_fresh(html_path, preview_html_dependencies(markdown_path)):
        return html_path

    pandoc = find_pandoc_for_workspace(workspace)
    if not pandoc:
        # When Pandoc is unavailable, keep using a previously rendered preview
        # instead of forcing the UI to fall back to raw Markdown forever.
        if html_preview_cache_is_fresh(html_path, markdown_path):
            if log:
                log("未检测到 Pandoc 环境，正在复用已有 HTML 预览…")
            return html_path
        return None

    input_path = markdown_path
    temp_path = None
    epub_marked = False
    if anchor_blocks or export_mode:
        raw = markdown_path.read_text(encoding="utf-8", errors="replace")
        # Do not alter the parsed Markdown artifact. This is only the temporary
        # Pandoc input used for preview/export rendering.
        raw = normalize_garbled_equation_references_for_display(raw)
        raw = repair_malformed_pipe_tables(raw)
        epub_marked = "LITMTRANS_EPUB_CHAPTER" in raw
        # Both the responsive reader and fixed-page exports use the image's
        # physical width ratio from the source PDF. The configured export width
        # remains a fallback for documents without MinerU layout metadata.
        image_width = export_style_markdown_image_width(style) if export_mode else None
        image_widths = layout_image_width_percentages(markdown_path)
        raw = normalize_markdown_for_export(raw, image_width, image_widths)
        if anchor_blocks:
            raw = inject_heading_sync_anchors(raw)
            raw = inject_markdown_block_anchors(raw)
        temp_suffix = "export" if export_mode else "anchored"
        temp_path = markdown_path.with_name(f".{markdown_path.stem}.{temp_suffix}.md")
        temp_path.write_text(raw, encoding="utf-8")
        input_path = temp_path
    input_format = "markdown+pipe_tables+raw_html+tex_math_dollars+link_attributes+table_captions+implicit_figures"
    # EPUBs use Pandoc fenced divs and bracketed spans for chapter metadata and
    # Calibre classes. Enable these extensions only for EPUB-marked documents so
    # the existing PDF/Word/HTML preview behavior remains unchanged.
    if epub_marked:
        input_format += "+fenced_divs+bracketed_spans"
    cmd = [
        str(pandoc),
        str(input_path),
        "-f",
        input_format,
        "-t",
        "html",
        "-s",
        "--mathjax",
        "--resource-path",
        str(markdown_path.parent),
        "-o",
        str(html_path),
    ]
    try:
        subprocess.run(
            cmd,
            cwd=str(markdown_path.parent),
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
            **hidden_subprocess_kwargs(),
        )
        if html_path.exists():
            polish_html(
                html_path,
                style,
                use_bundled_reader_font=not export_mode,
            )
            if export_mode:
                inline_local_images_in_html(html_path, markdown_path.parent)
        return html_path if html_path.exists() else None
    except Exception as exc:
        if log:
            log(f"外部预览生成遇到问题，已自动切换为内置预览：{exc}")
        return None
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def render_export_html_internal(
    markdown_path: Path,
    workspace: Path,
    log=None,
    style: ExportStyleSettings | None = None,
) -> Path | None:
    return render_preview_html_internal(
        markdown_path,
        workspace,
        log,
        anchor_blocks=False,
        export_mode=True,
        style=style,
    )


def simple_file_html(path: Path, style: ExportStyleSettings | None = None) -> str:
    mime = mimetypes.guess_type(str(path))[0] or ""
    escaped = html.escape(str(path.resolve()))
    if path.suffix.lower() in IMAGE_SUFFIXES:
        return f"""<!doctype html><meta charset="utf-8"><style>{research_css(style)}</style><body><img src="{path.resolve().as_uri()}" alt="{html.escape(path.name)}"><script>{research_script()}</script></body>"""
    return f"""<!doctype html><meta charset="utf-8"><style>{research_css(style)}</style><body><h2>原始文件</h2><p>{html.escape(path.name)}</p><p>类型: {html.escape(mime or path.suffix)}</p><p>当前文件格式无法稳定内嵌预览，请使用“系统打开原文件”，或切换到解析文件查看。</p><p>{escaped}</p></body>"""


def original_pdf_preview_html_path(pdf_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem)
    return pdf_path.with_name(f"original_pdf_preview.{safe_stem}.html")


def original_pdf_debug_preview_html_path(pdf_path: Path, markdown_path: Path) -> Path:
    pdf_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem)
    md_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    return pdf_path.with_name(f"original_pdf_debug_preview.{pdf_stem}.{md_stem}.html")


def preview_html_dependencies(markdown_path: Path) -> list[Path]:
    dependencies = [markdown_path.resolve(), Path(__file__).resolve()]
    image_map_path = markdown_path.parent / "image_map.json"
    if image_map_path.exists():
        dependencies.append(image_map_path.resolve())
    asset_dir = layout_preview_asset_dir(markdown_path)
    layout_path = asset_dir / "layout.json" if asset_dir else None
    if layout_path and layout_path.exists():
        dependencies.append(layout_path.resolve())
    return dependencies


def original_document_preview_dependencies(source_path: Path) -> list[Path]:
    return [source_path.resolve(), Path(__file__).resolve()]


def collect_debug_overlay_boxes(page: dict, pdf_width: float, pdf_height: float) -> list[str]:
    raw_page_size = page.get("page_size") or [pdf_width, pdf_height]
    if not isinstance(raw_page_size, list) or len(raw_page_size) < 2:
        raw_page_size = [pdf_width, pdf_height]
    layout_width = max(1.0, float(raw_page_size[0]))
    layout_height = max(1.0, float(raw_page_size[1]))
    scale_x = pdf_width / layout_width
    scale_y = pdf_height / layout_height
    overlays: list[str] = []

    def visit(block) -> None:
        if not isinstance(block, dict):
            return
        bbox = block.get("bbox")
        block_type = re.sub(r"[^a-z0-9_-]+", "_", str(block.get("type") or "unknown").lower())
        if isinstance(bbox, list) and len(bbox) >= 4:
            left = float(bbox[0]) * scale_x
            top = float(bbox[1]) * scale_y
            right = float(bbox[2]) * scale_x
            bottom = float(bbox[3]) * scale_y
            overlays.append(
                f"""<div class="pdf-debug-box pdf-debug-{html.escape(block_type)}" """
                f"""style="left:{left:.3f}px;top:{top:.3f}px;width:{max(1.0, right - left):.3f}px;height:{max(1.0, bottom - top):.3f}px;"></div>"""
            )
        for child in block.get("blocks") or []:
            visit(child)

    for block in page.get("preproc_blocks") or []:
        visit(block)
    return overlays


def render_original_pdf_debug_preview_html(pdf_path: Path, markdown_path: Path) -> Path | None:
    if not pdf_path or not pdf_path.exists() or not markdown_path or not markdown_path.exists():
        return None
    bundle = load_layout_preview_bundle(markdown_path)
    if not bundle:
        return None
    base_path = render_original_pdf_preview_html(pdf_path)
    if not base_path or not base_path.exists():
        return None
    out_path = original_pdf_debug_preview_html_path(pdf_path, markdown_path)
    dependencies = [pdf_path, markdown_path, bundle["layout_path"], Path(__file__).resolve()]
    if multi_file_cache_is_fresh(out_path, dependencies):
        return out_path
    html_text = base_path.read_text(encoding="utf-8", errors="replace")
    page_info = bundle.get("page_info") or []
    overlays_by_page: dict[int, str] = {}
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            continue
        match = re.search(
            rf'<section class="pdf-page-wrap"[^>]*data-sync-page-index="{page_index}"[^>]*data-page-width="([^"]+)"[^>]*data-page-height="([^"]+)"',
            html_text,
            flags=re.I,
        )
        if not match:
            continue
        pdf_width = max(1.0, float(match.group(1)))
        pdf_height = max(1.0, float(match.group(2)))
        overlays_by_page[page_index] = "".join(collect_debug_overlay_boxes(page, pdf_width, pdf_height))
    for page_index, overlay_html in overlays_by_page.items():
        if not overlay_html:
            continue
        pattern = re.compile(
            rf'(<section class="pdf-page-wrap"[^>]*data-sync-page-index="{page_index}"[^>]*>.*?<div class="pdf-page"[^>]*>)(.*?)(</div></div></section>)',
            flags=re.S | re.I,
        )
        html_text = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}{overlay_html}{match.group(3)}", html_text, count=1)
    debug_css = """
<style>
.pdf-debug-box {
  position: absolute;
  box-sizing: border-box;
  pointer-events: none;
  z-index: 8;
  outline: 2px solid rgba(245, 158, 11, 0.98);
  outline-offset: -1px;
}
.pdf-debug-title { outline-color: rgba(37, 99, 235, 0.98); }
.pdf-debug-text { outline-color: rgba(245, 158, 11, 0.98); }
.pdf-debug-ref_text { outline-color: rgba(147, 51, 234, 0.98); }
.pdf-debug-interline_equation { outline-color: rgba(99, 102, 241, 0.98); }
.pdf-debug-table,
.pdf-debug-chart,
.pdf-debug-image { outline-color: rgba(34, 197, 94, 0.98); }
.pdf-debug-page_header,
.pdf-debug-header,
.pdf-debug-page_footer,
.pdf-debug-footer,
.pdf-debug-page_number { outline-color: rgba(20, 184, 166, 0.98); }
</style>
"""
    html_text = html_text.replace("</head>", f"{debug_css}</head>", 1)
    html_text = html_text.replace("original-pdf-preview", "original-pdf-debug-preview", 1)
    out_path.write_text(html_text, encoding="utf-8")
    return out_path


def render_original_pdf_preview_html(pdf_path: Path) -> Path | None:
    if not pdf_path or not pdf_path.exists():
        return None
    out_path = original_pdf_preview_html_path(pdf_path)
    try:
        if (
            html_preview_cache_is_fresh(out_path, pdf_path)
            and f"original-pdf-preview version={ORIGINAL_PDF_PREVIEW_VERSION}" in out_path.read_text(encoding="utf-8", errors="ignore")[:500]
        ):
            return out_path
    except Exception:
        pass
    try:
        import pypdfium2 as pdfium

        # Keep the page-shell HTML because it provides page metrics and the
        # scroll bridge used by the main reader and pure-reading mode. PDFium
        # creates portable page images while its text geometry provides a
        # transparent, selectable text layer above each page.
        asset_dir = pdf_path.with_name(
            f"original_pdf_preview_assets.v{ORIGINAL_PDF_PREVIEW_VERSION}."
            f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', pdf_path.stem)}"
        )
        asset_dir.mkdir(exist_ok=True)
        title = html.escape(pdf_path.name)
        rendered_pages: list[str] = []
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    page_width, page_height = page.get_size()
                    page_width = max(1.0, float(page_width))
                    page_height = max(1.0, float(page_height))
                    image_path = asset_dir / f"page_{page_index + 1:04d}.png"
                    if not image_path.exists() or image_path.stat().st_mtime < pdf_path.stat().st_mtime:
                        bitmap = page.render(scale=2.0)
                        try:
                            bitmap.to_pil().save(image_path, format="PNG")
                        finally:
                            bitmap.close()

                    spans: list[str] = []
                    text_page = page.get_textpage()
                    try:
                        for char_index in range(text_page.count_chars()):
                            text = text_page.get_text_range(char_index, 1)
                            if not text or text.isspace():
                                continue
                            left, bottom, right, top = [float(value) for value in text_page.get_charbox(char_index)]
                            width = max(1.0, right - left)
                            height = max(1.0, top - bottom)
                            spans.append(
                                f"""<span class="pdf-text" style="left:{left:.3f}px;top:{max(0.0, page_height - top):.3f}px;width:{width:.3f}px;height:{height:.3f}px;font-size:{height:.3f}px;">{html.escape(text)}</span>"""
                            )
                    finally:
                        text_page.close()
                finally:
                    page.close()
                image_url = image_path.resolve().as_uri()
                rendered_pages.append(
                    f"""<section class="pdf-page-wrap" data-sync-page-index="{page_index}" data-page-width="{page_width:.3f}" data-page-height="{page_height:.3f}">"""
                    f"""<div class="pdf-page-shell" data-page-width="{page_width:.3f}" data-page-height="{page_height:.3f}" style="width:{page_width:.3f}px;height:{page_height:.3f}px;">"""
                    f"""<div class="pdf-page" style="width:{page_width:.3f}px;height:{page_height:.3f}px;">"""
                    f"""<img class="pdf-page-image" src="{image_url}" alt="" loading="lazy" decoding="async">{"".join(spans)}</div></div></section>"""
                )
        finally:
            document.close()
        if not rendered_pages:
            raise RuntimeError("empty PDF")
        html_text = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<!-- original-pdf-preview version={ORIGINAL_PDF_PREVIEW_VERSION} -->
<style>
html, body {{
  margin: 0;
  min-height: 100%;
  background: #f6f3ee;
}}
body {{
  padding: 10px 10px 28px;
  color: #111827;
  font-family: {SERIF_READING_FONT_STACK};
}}
.pdf-doc {{
  width: 100%;
  max-width: none;
  margin: 0 auto;
}}
.pdf-page-wrap {{
  margin: 0 auto 18px;
}}
.pdf-page-shell {{
  position: relative;
  margin: 0 auto;
  background: #ffffff;
  max-width: 100%;
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.10);
  overflow: hidden;
}}
.pdf-page {{
  position: relative;
  overflow: hidden;
  background: #ffffff;
  transform-origin: top left;
}}
.pdf-page-image {{
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: fill;
  user-select: none;
  pointer-events: none;
}}
.pdf-text {{
  position: absolute;
  display: block;
  color: transparent;
  line-height: 1;
  white-space: pre;
  overflow: visible;
  user-select: text;
  -webkit-user-select: text;
}}
.pdf-text::selection {{
  color: transparent;
  background: rgba(59, 130, 246, 0.32);
}}
</style>
</head>
<body><main class="pdf-doc">{''.join(rendered_pages)}</main>
<script>
(() => {{
  function fitPdfPages() {{
    const doc = document.querySelector('.pdf-doc');
    const available = Math.max(240, (doc ? doc.clientWidth : window.innerWidth) - 2);
    for (const shell of document.querySelectorAll('.pdf-page-shell')) {{
      const page = shell.querySelector('.pdf-page');
      if (!page) continue;
      const pageWidth = parseFloat(shell.dataset.pageWidth || page.style.width || page.offsetWidth || '1');
      const pageHeight = parseFloat(shell.dataset.pageHeight || page.style.height || page.offsetHeight || '1');
      const wrap = shell.closest('.pdf-page-wrap');
      const pageIndex = wrap ? Number(wrap.dataset.syncPageIndex || 0) : 0;
      const forced = window.__mineruForcedPageMetrics && window.__mineruForcedPageMetrics.get(pageIndex);
      const scale = forced && forced.renderedHeight > 0
        ? forced.renderedHeight / Math.max(1, pageHeight)
        : available / Math.max(1, pageWidth);
      shell.style.width = `${{pageWidth * scale}}px`;
      shell.style.height = `${{pageHeight * scale}}px`;
      page.style.transform = `scale(${{scale}})`;
    }}
  }}

  function syncRoot() {{
    return document.scrollingElement || document.documentElement;
  }}

  function maxScroll() {{
    return Math.max(1, syncRoot().scrollHeight - window.innerHeight);
  }}

  function pageMetrics() {{
    const root = syncRoot();
    const y = root.scrollTop;
    return Array.from(document.querySelectorAll('.pdf-page-wrap')).map((wrap, fallbackIndex) => {{
      const shell = wrap.querySelector('.pdf-page-shell') || wrap;
      const rect = shell.getBoundingClientRect();
      const pageWidth = parseFloat(shell.dataset.pageWidth || wrap.dataset.pageWidth || '1');
      const pageHeight = parseFloat(shell.dataset.pageHeight || wrap.dataset.pageHeight || '1');
      return {{
        index: Number(wrap.dataset.syncPageIndex || fallbackIndex),
        top: y + rect.top,
        bottom: y + rect.bottom,
        renderedWidth: Math.max(1, rect.width),
        renderedHeight: Math.max(1, rect.height),
        pageWidth,
        pageHeight,
      }};
    }});
  }}

  function applyForcedPageMetrics(pages) {{
    if (!Array.isArray(pages) || !pages.length) return false;
    const next = new Map();
    for (const page of pages) {{
      const index = Number(page && page.index);
      const renderedHeight = Number(page && page.renderedHeight);
      const renderedWidth = Number(page && page.renderedWidth);
      if (!Number.isFinite(index) || renderedHeight <= 0) continue;
      next.set(index, {{ renderedHeight, renderedWidth }});
    }}
    if (!next.size) return false;
    window.__mineruForcedPageMetrics = next;
    fitPdfPages();
    return true;
  }}

  function syncPayload() {{
    const root = syncRoot();
    const pages = pageMetrics();
    const y = root.scrollTop;
    const viewportAnchorRatio = 0.5;
    const anchorY = y + window.innerHeight * viewportAnchorRatio;
    if (!pages.length) {{
      return {{ ratio: Math.max(0, Math.min(1, y / maxScroll())) }};
    }}
    let best = pages[0];
    let bestDistance = Infinity;
    for (const page of pages) {{
      if (anchorY >= page.top && anchorY <= page.bottom) {{
        best = page;
        break;
      }}
      const distance = Math.min(Math.abs(page.top - anchorY), Math.abs(page.bottom - anchorY));
      if (distance < bestDistance) {{
        best = page;
        bestDistance = distance;
      }}
    }}
    const pageOffsetRatio = Math.max(0, Math.min(1, (anchorY - best.top) / best.renderedHeight));
    return {{
      layoutPage: best.index,
      pageOffsetRatio,
      viewportAnchorRatio,
      ratio: Math.max(0, Math.min(1, y / maxScroll())),
      pages: pages.map((page) => ({{
        index: page.index,
        pageWidth: page.pageWidth,
        pageHeight: page.pageHeight,
        renderedWidth: page.renderedWidth,
        renderedHeight: page.renderedHeight,
      }})),
    }};
  }}

  function scrollToSyncPayload(payload, smooth) {{
    if (!payload) return false;
    applyForcedPageMetrics(payload.pages);
    const pages = pageMetrics();
    if (!pages.length || payload.layoutPage === undefined) return false;
    const requestedIndex = Number(payload.layoutPage) || 0;
    const page = pages.find((candidate) => candidate.index === requestedIndex) || pages[Math.max(0, Math.min(pages.length - 1, requestedIndex))];
    if (!page) return false;
    const offsetRatio = Math.max(0, Math.min(1, Number(payload.pageOffsetRatio || 0)));
    const anchorRatio = Math.max(0, Math.min(1, Number(payload.viewportAnchorRatio ?? 0.5)));
    const nextTop = Math.max(0, Math.min(maxScroll(), page.top + page.renderedHeight * offsetRatio - window.innerHeight * anchorRatio));
    window.__mineruProgrammaticScrollUntil = Date.now() + 120;
    window.scrollTo({{ top: nextTop, behavior: smooth ? 'smooth' : 'auto' }});
    return true;
  }}

  window.syncScrollApi = {{
    scrollRatio() {{
      return Math.max(0, Math.min(1, syncRoot().scrollTop / maxScroll()));
    }},
    scrollToRatio(ratio, smooth) {{
      const top = Math.max(0, Math.min(maxScroll(), Number(ratio || 0) * maxScroll()));
      window.__mineruProgrammaticScrollUntil = Date.now() + 120;
      window.scrollTo({{ top, behavior: smooth ? 'smooth' : 'auto' }});
      return true;
    }},
    syncPayload,
    scrollToSyncPayload,
    selectedText() {{
      return String(window.getSelection ? window.getSelection() : '');
    }},
  }};
  window.__mineruFitLayoutPages = fitPdfPages;
  window.addEventListener('resize', () => requestAnimationFrame(fitPdfPages));
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', fitPdfPages, {{ once: true }});
  }} else {{
    fitPdfPages();
  }}
}})();
</script>
</body>
</html>"""
        out_path.write_text(html_text, encoding="utf-8")
        return out_path
    except Exception:
        pass
    if html_preview_cache_is_fresh(out_path, pdf_path):
        return out_path
    pdf_url = pdf_path.resolve().as_uri()
    title = html.escape(pdf_path.name)
    html_text = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
html, body {{
  margin: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #2f3338;
}}
.pdf-frame {{
  display: block;
  width: 100vw;
  height: 100vh;
  border: 0;
  background: #2f3338;
}}
</style>
</head>
<body>
<embed class="pdf-frame" src="{pdf_url}#toolbar=0&navpanes=0&scrollbar=1&view=FitH" type="application/pdf">
</body>
</html>"""
    try:
        out_path.write_text(html_text, encoding="utf-8")
        return out_path
    except Exception:
        return None


def layout_preview_html_path(markdown_path: Path, strict_fit: bool = False, debug_overlay: bool = False) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    if strict_fit:
        prefix = "preview_layout_source_strict_debug" if debug_overlay else "preview_layout_source_strict"
    else:
        prefix = "preview_layout_current_debug" if debug_overlay else "preview_layout_current"
    return markdown_path.with_name(f"{prefix}.{safe_stem}.html")


def layout_translation_preview_html_path(markdown_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    return markdown_path.with_name(f"preview_layout_translated_current.{safe_stem}.html")


def layout_translation_bundle_path(markdown_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    return markdown_path.with_name(f"layout_translated_current.{safe_stem}.json")


def layout_fit_cache_bootstrap_html(cache_fingerprint: str, cache_scope: str) -> str:
    """Restore the warm-cache state before a long layout document is parsed.

    The full style payload is applied by the runtime after the nodes exist.  This
    early probe only suppresses the expensive-fit overlay for a known completed
    artifact. The fingerprint includes the source layout and the unique
    translation-run revision, so every retranslation rejects the older fit.
    """
    fingerprint_json = json.dumps(str(cache_fingerprint), ensure_ascii=False)
    scope_json = json.dumps(str(cache_scope), ensure_ascii=False)
    version_json = json.dumps(LAYOUT_FIT_CACHE_VERSION, ensure_ascii=False)
    return f"""
<script data-layout-fit-cache-bootstrap>
(() => {{
  const fingerprint = {fingerprint_json};
  const scope = {scope_json};
  const version = {version_json};
  const key = `${{version}}:${{scope}}:${{fingerprint}}`;
  try {{
    const payload = JSON.parse(localStorage.getItem(key) || 'null');
    const diskPayload = window.__mineruDiskFitCache;
    const restored = payload && payload.version === version && payload.complete === true && Array.isArray(payload.styles)
      ? payload
      : diskPayload;
    if (restored && restored.version === version && restored.complete === true && Array.isArray(restored.styles)) {{
      window.__mineruInitialFitCache = {{ key, payload: restored }};
      document.body.classList.add('layout-fit-cache-hit');
      document.body.dataset.layoutFitState = 'restoring-cache';
      document.body.dataset.layoutProgress = (document.body.dataset.layoutProgress || '正在载入已排版全文…')
        .replace('正在准备全文排版', '正在载入已排版全文');
    }}
  }} catch (_error) {{}}
}})();
</script>"""


def paired_layout_preview_html_path(markdown_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    return markdown_path.with_name(f"preview_layout_paired_current.{safe_stem}.html")


def extract_page_preview_parts(layout_html: str) -> tuple[str, str, list[str]]:
    styles = "\n".join(re.findall(r"<style>(.*?)</style>", layout_html, flags=re.S | re.I))
    scripts = "\n".join(re.findall(r"<script>(.*?)</script>", layout_html, flags=re.S | re.I))
    sections = re.findall(r'<section class="(?:layout-page-wrap|pdf-page-wrap)"[^>]*>.*?</section>', layout_html, flags=re.S | re.I)
    return styles, scripts, sections


def render_paired_layout_preview_html(source_page_path: Path, translation_layout_path: Path, markdown_path: Path, debug: bool = False) -> Path | None:
    if not source_page_path.exists() or not translation_layout_path.exists():
        return None
    out_path = paired_layout_preview_html_path(markdown_path)
    dependencies = [source_page_path, translation_layout_path, Path(__file__).resolve()]
    cache_marker = (
        f'paired-layout-preview source="{html.escape(str(markdown_path.resolve()))}" '
        f'source_page="{html.escape(str(source_page_path.resolve()))}" '
        f'translation="{html.escape(str(translation_layout_path.resolve()))}" '
        f'debug="{1 if debug else 0}"'
    )
    if multi_file_cache_is_fresh(out_path, dependencies):
        try:
            if cache_marker in out_path.read_text(encoding="utf-8", errors="ignore")[:1200]:
                return out_path
        except Exception:
            pass
    source_html = source_page_path.read_text(encoding="utf-8", errors="replace")
    translation_html = translation_layout_path.read_text(encoding="utf-8", errors="replace")
    source_styles, _, source_sections = extract_page_preview_parts(source_html)
    translation_styles, translation_scripts, translation_sections = extract_page_preview_parts(translation_html)
    if not source_sections or not translation_sections:
        return None
    page_count = max(len(source_sections), len(translation_sections))
    paired_rows: list[str] = []
    for index in range(page_count):
        source_section = source_sections[index] if index < len(source_sections) else '<section class="layout-page-wrap missing-page"></section>'
        translation_section = translation_sections[index] if index < len(translation_sections) else '<section class="layout-page-wrap missing-page"></section>'
        paired_rows.append(
            f"""<section class="paired-page-row" data-pair-page="{index}">"""
            f"""<div class="paired-pane paired-source">{source_section}</div>"""
            f"""<div class="paired-pane paired-translation">{translation_section}</div>"""
            f"""</section>"""
        )
    title = html.escape(markdown_path.stem)
    html_text = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title} - paired layout</title>
<!-- {cache_marker} -->
{mathjax_script_html()}
<style>
{source_styles}
{translation_styles}
body {{
  margin: 0;
  padding: 10px 10px 28px;
  background: #f6f3ee;
}}
.paired-layout-doc {{
  width: 100%;
  max-width: none;
  margin: 0 auto;
}}
.paired-page-row {{
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(240px, 1fr);
  gap: 14px;
  align-items: start;
  margin: 0 0 20px;
}}
.paired-pane {{
  min-width: 0;
  overflow: visible;
}}
.paired-pane .layout-page-wrap {{
  margin: 0 auto;
}}
.paired-pane .pdf-page-wrap {{
  margin: 0 auto;
}}
.paired-pane .layout-page-shell,
.paired-pane .pdf-page-shell {{
  max-width: 100%;
}}
.paired-pane .layout-note,
.paired-pane .layout-build-badge,
.paired-pane .layout-page-label {{
  display: none !important;
}}
.missing-page {{
  min-height: 120px;
  border: 1px dashed #cbd5e1;
  background: rgba(255,255,255,0.6);
}}
@media (max-width: 920px) {{
  .paired-page-row {{
    grid-template-columns: 1fr;
  }}
}}
</style></head><body class="{'layout-debug' if debug else 'layout-production'} layout-paired layout-translated"><main class="paired-layout-doc">
{''.join(paired_rows)}
</main><script>
{translation_scripts}
</script><script>
(() => {{
  function fitPairedLayoutPages() {{
    for (const pane of document.querySelectorAll('.paired-pane')) {{
      const available = Math.max(220, pane.clientWidth - 2);
      for (const shell of pane.querySelectorAll('.layout-page-shell')) {{
        const page = shell.querySelector('.layout-page');
        if (!page) continue;
        const pageWidth = parseFloat(shell.dataset.pageWidth || page.style.width || page.offsetWidth || '1');
        const pageHeight = parseFloat(shell.dataset.pageHeight || page.style.height || page.offsetHeight || '1');
        const scale = available / Math.max(1, pageWidth);
        shell.style.width = `${{pageWidth * scale}}px`;
        shell.style.height = `${{pageHeight * scale}}px`;
        page.style.transform = 'none';
        page.style.zoom = String(scale);
      }}
      for (const shell of pane.querySelectorAll('.pdf-page-shell')) {{
        const page = shell.querySelector('.pdf-page');
        if (!page) continue;
        const pageWidth = parseFloat(shell.dataset.pageWidth || page.style.width || page.offsetWidth || '1');
        const pageHeight = parseFloat(shell.dataset.pageHeight || page.style.height || page.offsetHeight || '1');
        const scale = available / Math.max(1, pageWidth);
        shell.style.width = `${{pageWidth * scale}}px`;
        shell.style.height = `${{pageHeight * scale}}px`;
        page.style.transform = `scale(${{scale}})`;
        page.style.transformOrigin = 'top left';
      }}
    }}
  }}
  function fitPairedEquations() {{
    for (const block of document.querySelectorAll('.layout-block.type-interline_equation')) {{
      const host = block.querySelector('.layout-equation-text') || block;
      const formulaHost = block.querySelector('.layout-equation-formula') || host;
      if (!host || !formulaHost) continue;
      const number = block.querySelector('.layout-equation-number');
      const math = formulaHost.querySelector('mjx-container');
      if (!math) continue;
      math.style.display = 'inline-block';
      math.style.width = 'auto';
      math.style.minWidth = '0';
      math.style.margin = '0';
      math.style.textAlign = 'left';
      math.style.position = 'relative';
      math.style.left = '0px';
      math.style.transform = '';
      math.style.transformOrigin = 'left center';
      const hostRect = host.getBoundingClientRect();
      const fittedRect = math.getBoundingClientRect();
      const finalRightLimit = hostRect.right - 1;
      const availableWidth = Math.max(1, finalRightLimit - math.getBoundingClientRect().left);
      const remainingOverflow = Math.max(0, fittedRect.right - finalRightLimit, hostRect.left - fittedRect.left);
      const scale = remainingOverflow > 0.5 ? Math.min(1, availableWidth / Math.max(1, fittedRect.width)) : 1;
      math.style.transform = scale < 1 ? `scale(${{scale.toFixed(4)}})` : '';
    }}
  }}
  function runPairedLayout() {{
    fitPairedLayoutPages();
  }}
  function refitPairedAfterMathJax() {{
    if (!window.MathJax || !window.MathJax.typesetPromise) return;
    window.MathJax.typesetPromise()
      .then(() => requestAnimationFrame(() => requestAnimationFrame(() => {{
        fitPairedEquations();
        fitPairedLayoutPages();
      }})))
      .catch(() => {{}});
  }}
  window.syncScrollApi = {{
    scrollRatio() {{
      const root = document.scrollingElement || document.documentElement;
      const maxScroll = Math.max(1, root.scrollHeight - window.innerHeight);
      return Math.max(0, Math.min(1, root.scrollTop / maxScroll));
    }},
    scrollToRatio(ratio, smooth) {{
      const root = document.scrollingElement || document.documentElement;
      const maxScroll = Math.max(1, root.scrollHeight - window.innerHeight);
      const top = Math.max(0, Math.min(maxScroll, Number(ratio || 0) * maxScroll));
      window.scrollTo({{ top, behavior: smooth ? 'smooth' : 'auto' }});
      return true;
    }},
    selectedText() {{
      return String(window.getSelection ? window.getSelection() : '');
    }}
  }};
  window.__mineruFitLayoutPages = fitPairedLayoutPages;
  window.__mineruFitLayoutEquations = fitPairedEquations;
  window.addEventListener('resize', () => requestAnimationFrame(fitPairedLayoutPages));
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', () => {{ runPairedLayout(); refitPairedAfterMathJax(); }}, {{ once: true }});
  }} else {{
    runPairedLayout();
    refitPairedAfterMathJax();
  }}
}})();
</script></body></html>"""
    try:
        out_path.write_text(html_text, encoding="utf-8")
        return out_path
    except Exception:
        return None


def layout_audit_report_path(markdown_path: Path) -> Path:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    return markdown_path.with_name(f"layout_audit_current.{safe_stem}.json")


def cleanup_stale_layout_artifacts(markdown_path: Path, keep_paths: list[Path]) -> None:
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    keep_resolved = {path.resolve() for path in keep_paths}
    patterns = [
        f"preview_layout_v*.{safe_stem}.html",
        f"layout_audit_v*.{safe_stem}.json",
        f"preview_layout_current.{safe_stem}.html",
        f"layout_audit_current.{safe_stem}.json",
    ]
    for pattern in patterns:
        for candidate in markdown_path.parent.glob(pattern):
            try:
                if candidate.resolve() in keep_resolved:
                    continue
                candidate.unlink()
            except Exception:
                continue


def layout_preview_asset_dir(markdown_path: Path) -> Path | None:
    folder = markdown_path.parent
    candidates = [folder / "mineru_result", folder]
    for candidate in candidates:
        if (candidate / "layout.json").exists():
            return candidate
    return None


def layout_preview_scale(page_width: float) -> float:
    return 1.45 if page_width <= 0 else (920.0 / float(page_width))


def layout_pdf_page_size_from_html(html_path: Path) -> tuple[float, float] | None:
    """Read the source-PDF page size encoded in a rendered layout HTML file."""
    try:
        html_text = html_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    sizes: list[tuple[float, float]] = []
    section_pattern = re.compile(
        r'<section\b[^>]*class=["\'][^"\']*\blayout-page-wrap\b[^"\']*["\'][^>]*>',
        flags=re.IGNORECASE,
    )
    attribute_pattern = re.compile(r'\bdata-page-(width|height)=["\']([^"\']+)["\']', flags=re.IGNORECASE)
    for section in section_pattern.finditer(html_text):
        attributes = {match.group(1).lower(): match.group(2) for match in attribute_pattern.finditer(section.group(0))}
        try:
            width = float(attributes.get("width", "0"))
            height = float(attributes.get("height", "0"))
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            sizes.append((width, height))
    if not sizes:
        return None
    # QPageLayout accepts one page size for a PDF. Use the largest source
    # dimensions so documents with tiny per-page variations are not clipped.
    return max(width for width, _ in sizes), max(height for _, height in sizes)


def multi_file_cache_is_fresh(output_path: Path, dependencies: list[Path]) -> bool:
    try:
        if not output_path.exists():
            return False
        output_mtime = output_path.stat().st_mtime
        for dependency in dependencies:
            if dependency.exists() and dependency.stat().st_mtime > output_mtime:
                return False
        return True
    except OSError:
        return False


def mathjax_script_html() -> str:
    local_mathjax = Path(__file__).resolve().parent / "node_modules" / "mathjax" / "es5" / "tex-chtml-full.js"
    source = local_mathjax.resolve().as_uri() if local_mathjax.exists() else "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml-full.js"
    return f"""<script>
window.MathJax = {{ tex: {{ inlineMath: [['\\\\(', '\\\\)'], ['$', '$']], displayMath: [['\\\\[', '\\\\]'], ['$$', '$$']], tags: 'ams' }}, svg: {{ fontCache: 'none' }} }};
</script><script defer src="{html.escape(source, quote=True)}"></script>"""


def qt_webchannel_script_html() -> str:
    """Load Qt's bridge only inside QWebEngine; ordinary browsers ignore it."""
    return '<script src="qrc:///qtwebchannel/qwebchannel.js"></script>'


def normalize_tex_delimiters(text: str) -> tuple[str, bool]:
    raw = str(text or "").strip()
    display = False
    wrappers = [
        (r"\\\[(.*?)\\\]", True),
        (r"\\\((.*?)\\\)", False),
        (r"\$\$(.*?)\$\$", True),
        (r"\$(.*?)\$", False),
    ]
    for pattern, is_display in wrappers:
        match = re.fullmatch(pattern, raw, flags=re.S)
        if match:
            raw = match.group(1).strip()
            display = is_display
            break
    raw = re.sub(r"\\tag\s*\{([^{}]+)\}", r"\\tag{\1}", raw)
    if re.search(r"\\tag\s*\{", raw):
        display = True
    return raw, display


def tex_inline_to_html(text: str, display: bool = False) -> str:
    raw, forced_display = normalize_tex_delimiters(text)
    if forced_display:
        display = True
    if not raw:
        return ""
    escaped = html.escape(raw)
    if display:
        return f"""<span class="layout-math layout-math-display">\\[{escaped}\\]</span>"""
    return f"""<span class="layout-math layout-math-inline">\\({escaped}\\)</span>"""


def split_tex_equation_tag(text: str) -> tuple[str, str]:
    raw, _ = normalize_tex_delimiters(text)
    match = re.search(r"\\tag\s*\{([^{}]+)\}\s*,?\s*$", raw)
    if not match:
        return raw, ""
    body = raw[: match.start()].rstrip()
    body = re.sub(r",\s*$", "", body).rstrip()
    number = match.group(1).strip()
    return body, number


def equation_display_html(text: str, number_right_offset: float | None = None) -> str:
    body, number = split_tex_equation_tag(text)
    if number:
        number_style = (
            f' style="--equation-number-right:{float(number_right_offset):.2f}px"'
            if number_right_offset is not None else ""
        )
        return (
            f"""<div class="layout-equation-text has-number"{number_style}>"""
            f"""<div class="layout-equation-formula">{tex_inline_to_html(body, display=True)}</div>"""
            f"""<div class="layout-equation-number">({html.escape(number)})</div>"""
            f"""</div>"""
        )
    return f"""<div class="layout-equation-text">{tex_inline_to_html(text, display=True)}</div>"""


def load_layout_preview_bundle(markdown_path: Path) -> dict | None:
    asset_dir = layout_preview_asset_dir(markdown_path)
    if not asset_dir:
        return None
    layout_path = asset_dir / "layout.json"
    if not layout_path.exists():
        return None
    model_path = next(asset_dir.glob("*_model.json"), None)
    content_path = next(asset_dir.glob("*_content_list_v2.json"), None)
    if content_path is None:
        content_path = next(asset_dir.glob("*_content_list.json"), None)
    try:
        layout_data = json.loads(layout_path.read_text(encoding="utf-8", errors="replace"))
        page_info = layout_data.get("pdf_info") if isinstance(layout_data, dict) else None
        model_pages = json.loads(model_path.read_text(encoding="utf-8", errors="replace")) if model_path else None
        content_pages = json.loads(content_path.read_text(encoding="utf-8", errors="replace")) if content_path else None
    except Exception:
        return None
    if not isinstance(page_info, list):
        return None
    return {
        "asset_dir": asset_dir,
        "layout_path": layout_path,
        "model_path": model_path,
        "content_path": content_path,
        "page_info": page_info,
        "model_pages": model_pages if isinstance(model_pages, list) else [],
        "content_pages": content_pages,
    }


def load_layout_translation_bundle(markdown_path: Path) -> dict | None:
    translated_path = layout_translation_bundle_path(markdown_path)
    if not translated_path.exists():
        return None
    try:
        data = json.loads(translated_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("page_info"), list):
        return None
    asset_dir = data.get("asset_dir")
    data["asset_dir"] = Path(asset_dir) if asset_dir else layout_preview_asset_dir(markdown_path)
    if not isinstance(data.get("model_pages"), list):
        data["model_pages"] = []
    return data


def layout_docx_pt(value: float) -> float:
    """Return MinerU/PDF layout coordinates as Word points.

    MinerU page sizes and bboxes use PDF points (A4 is about 595 x 842),
    rather than CSS pixels. Converting by 72/96 shrinks the exported page
    and every anchored object to 75 percent of the intended size.
    """
    return max(0.0, float(value))


def layout_docx_escape_text(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def layout_docx_html_from_block(block: dict) -> str:
    block_type = str(block.get("type") or "").lower()
    spans: list[dict] = []
    for line in block.get("lines") or []:
        if isinstance(line, dict):
            spans.extend(span for span in (line.get("spans") or []) if isinstance(span, dict))
    if block_type == "table_body":
        for span in spans:
            if span.get("html"):
                return normalize_layout_html_snippet(str(span.get("html") or ""))
    if block_type in {"interline_equation", "equation"}:
        for span in spans:
            if span.get("content"):
                return str(span.get("content") or "")
    return layout_lines_to_html(block.get("lines"))


def layout_docx_plain_text_from_html(fragment_html: str) -> str:
    text = body_text_from_html(fragment_html)
    return re.sub(r"\s+", " ", text).strip()


def layout_docx_plain_text_from_block(block: dict) -> str:
    return layout_docx_plain_text_from_html(layout_docx_html_from_block(block))


def layout_docx_image_path(block: dict, asset_dir: Path) -> Path | None:
    for line in block.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if not isinstance(span, dict):
                continue
            image_name = str(span.get("image_path") or "")
            if not image_name:
                continue
            image_path = asset_dir / "images" / image_name
            if image_path.exists():
                return image_path
    return None


def layout_docx_collect_blocks(blocks: list[dict], asset_dir: Path) -> list[dict]:
    items: list[dict] = []

    def visit(block: dict) -> None:
        if not isinstance(block, dict):
            return
        bbox = block.get("bbox")
        block_type = str(block.get("type") or "text").lower()
        if isinstance(bbox, list) and len(bbox) >= 4:
            image_path = layout_docx_image_path(block, asset_dir)
            body_html = "" if image_path else layout_docx_html_from_block(block)
            table_rows = parse_raw_html_table(body_html) if block_type == "table_body" and "<table" in body_html.lower() else []
            text = "" if image_path or table_rows else layout_docx_plain_text_from_html(body_html)
            if image_path or table_rows or text:
                items.append(
                    {
                        "bbox": [float(part) for part in bbox[:4]],
                        "type": block_type,
                        "text": text,
                        "html": body_html,
                        "table_rows": table_rows,
                        "image_path": image_path,
                    }
                )
        for child in block.get("blocks") or []:
            if isinstance(child, dict):
                visit(child)

    for block in blocks or []:
        visit(block)
    return items


LAYOUT_DOCX_FONT_BOOST_PT = 0.0
LAYOUT_DOCX_MATH_POINT_SCALE = 1.0


def layout_docx_output_font_size(font_size: float) -> float:
    """Return the font size written to layout-mode Word output.

    Keep the final browser/runtime size unchanged. A previous Word-only 1 pt
    increase made exported paragraphs visibly taller than the reading view.
    """
    try:
        source_size = float(font_size)
    except (TypeError, ValueError):
        source_size = 1.0
    return max(1.0, source_size) + LAYOUT_DOCX_FONT_BOOST_PT


def layout_docx_formula_point_scale(formula_text: str) -> float:
    """Return the scale multiplier for Word math formulas matching surrounding text."""
    return 1.0


def layout_docx_content_ratio(item: dict) -> float:
    """Return the overflow ratio captured from the final browser layout."""
    try:
        ratio = float(item.get("runtime_content_ratio") or 1.0)
    except (TypeError, ValueError):
        ratio = 1.0
    return max(1.0, ratio)


def layout_docx_safe_text_height(
    item: dict,
    source_height: float,
    source_font_size: float,
    *,
    formula_text: str = "",
) -> float:
    """Preserve block boundaries while allowing for native math and wrapped text."""
    height = max(1.0, float(source_height))
    content_ratio = layout_docx_content_ratio(item)
    if formula_text:
        # The source layout records a browser pixel font size.  Word's native
        # math uses points, so retaining the raw number makes every formula
        # about one third taller and causes neighboring absolute anchors to
        # overlap.  Keep it within the source bbox, with only the room truly
        # required by multi-level math.
        output_size = layout_docx_output_font_size(max(1.0, float(source_font_size))) * layout_docx_formula_point_scale(formula_text)
        try:
            output_size *= float(item.get("runtime_math_scale") or 1.0)
        except (TypeError, ValueError):
            pass
        # Word's OMML ascent/descent is larger than the source MathJax box.
        # Keep a conservative reservation so its auto-expanding textbox never
        # grows into the following same-column anchor.
        height = max(height * content_ratio, 30.0, output_size * 2.8)
        if r"\begin" in formula_text:
            height = max(height, 82.0)
        elif re.search(r"\\(?:frac|dfrac|tfrac|sqrt|sum|int|left|right)\b", formula_text):
            height = max(height, 42.0)
        return height

    node = item.get("node") or {}
    text = layout_docx_dom_text(node).strip()
    if text:
        block_type = str(item.get("type") or "").lower()
        font_size = layout_docx_output_font_size(max(1.0, float(source_font_size)))
        line_ratio = (
            max(1.18, float(item.get("line_ratio") or 1.18))
            if block_type == "title" or float(item.get("line_ratio") or 1.0) < 1.15
            else float(item.get("line_ratio") or 1.18)
        )
        bbox = item.get("bbox") or [0, 0, 100, source_height]
        width = max(10.0, float(bbox[2]))
        cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", text))
        other_count = len(text) - cjk_count
        total_text_width = (cjk_count * 1.05 + other_count * 0.55) * font_size
        lines = max(1, math.ceil(total_text_width / width))
        explicit_lines = max(1, text.count("\n") + 1)
        effective_lines = max(lines, explicit_lines)
        rendered_h = effective_lines * (font_size * line_ratio)
        height = max(height, rendered_h, height * content_ratio)
    return height


def layout_docx_font_size(block_type: str, bbox: list[float], text: str) -> float:
    # Match the fixed-size typography used by the on-screen layout renderer.
    # A character-count density estimate is unsafe for translated CJK text:
    # it has fewer code points than English but each glyph is roughly twice as
    # wide, which previously selected 11 pt and overflowed neighboring boxes.
    kind = str(block_type or "").lower()
    if kind == "title":
        return min(13.0, max(8.2, bbox_height(bbox) * 0.75))
    if kind in {"interline_equation", "equation"}:
        return 8.0
    if "footnote" in kind or kind in {"page_footer", "page_header", "ref_text"}:
        return 7.2
    if "caption" in kind or kind == "text":
        return 7.6
    return 7.6


def layout_docx_run_properties(font_size: float, *, bold: bool = False, vertical_align: str = "") -> str:
    half_points = max(2, int(round(layout_docx_output_font_size(font_size) * 2.0)))
    parts = [
        '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" '
        'w:eastAsia="SimSun" w:cs="Times New Roman"/>',
        f'<w:sz w:val="{half_points}"/><w:szCs w:val="{half_points}"/>',
        '<w:noProof/>',
    ]
    if bold:
        parts.append('<w:b/><w:bCs/>')
    if vertical_align:
        parts.append(f'<w:vertAlign w:val="{vertical_align}"/>')
    return "".join(parts)


def layout_docx_text_runs_from_html(fragment_html: str, font_size: float, *, bold: bool = False) -> str:
    normalized = str(fragment_html or "")
    normalized = re.sub(r"<\s*br\s*/?\s*>", "\n", normalized, flags=re.I)
    normalized = re.sub(r"</\s*p\s*>", "\n", normalized, flags=re.I)
    normalized = re.sub(r"<\s*p\b[^>]*>", "", normalized, flags=re.I)
    normalized = re.sub(r"<\s*sup\b[^>]*>(.*?)</\s*sup\s*>", r"[[SUP:\1]]", normalized, flags=re.I | re.S)
    normalized = re.sub(r"<\s*sub\b[^>]*>(.*?)</\s*sub\s*>", r"[[SUB:\1]]", normalized, flags=re.I | re.S)
    normalized = re.sub(r"<[^>]+>", " ", normalized)
    normalized = html.unescape(normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    parts = re.split(r"(\[\[(?:SUP|SUB):.*?\]\]|\n+)", normalized, flags=re.S)
    runs: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("[[SUP:") and part.endswith("]]"):
            text = layout_docx_escape_text(part[6:-2])
            if text:
                props = layout_docx_run_properties(font_size, bold=bold, vertical_align="superscript")
                runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t>{text}</w:t></w:r>')
            continue
        if part.startswith("[[SUB:") and part.endswith("]]"):
            text = layout_docx_escape_text(part[6:-2])
            if text:
                props = layout_docx_run_properties(font_size, bold=bold, vertical_align="subscript")
                runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t>{text}</w:t></w:r>')
            continue
        if part.startswith("\n"):
            for _ in range(max(1, part.count("\n"))):
                props = layout_docx_run_properties(font_size, bold=bold)
                runs.append(f"<w:r><w:rPr>{props}</w:rPr><w:br/></w:r>")
            continue
        text = layout_docx_escape_text(part)
        if text:
            props = layout_docx_run_properties(font_size, bold=bold)
            runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>')
    return "".join(runs) or "<w:r><w:t></w:t></w:r>"


def layout_docx_paragraph_xml(inner_runs: str, *, align: str = "left", font_size: float = 8.0) -> str:
    jc = {"center": "center", "justify": "both", "right": "right"}.get(align, "left")
    line_twips = max(20, int(round(float(font_size) * 20.0 * 1.08)))
    return (
        f'<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="{line_twips}" w:lineRule="exact"/>'
        f'<w:jc w:val="{jc}"/></w:pPr>{inner_runs}</w:p>'
    )


def layout_docx_table_xml(rows: list[list[HtmlTableCell]]) -> str:
    if not rows:
        return ""
    table_rows: list[str] = []
    for row in rows:
        cells: list[str] = []
        for cell in row:
            cell_text = layout_docx_escape_text(cell.text)
            tc_pr = '<w:tcPr>'
            if cell.colspan > 1:
                tc_pr += f'<w:gridSpan w:val="{cell.colspan}"/>'
            tc_pr += '<w:vAlign w:val="center"/></w:tcPr>'
            table_font = layout_docx_run_properties(7.2, bold=cell.is_header)
            cells.append(
                f'<w:tc>{tc_pr}<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
                f'<w:r><w:rPr>{table_font}</w:rPr><w:t>{cell_text}</w:t></w:r></w:p></w:tc>'
            )
        table_rows.append(f'<w:tr>{"".join(cells)}</w:tr>')
    return (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="0" w:type="auto"/><w:tblLayout w:type="autofit"/>'
        '<w:jc w:val="center"/></w:tblPr>'
        + "".join(table_rows)
        + '</w:tbl>'
    )


def layout_docx_text_anchor(item: dict, shape_id: str) -> str:
    bbox = item["bbox"]
    block_type = str(item.get("type") or "")
    body_html = str(item.get("html") or item.get("text") or "")
    x = layout_docx_pt(bbox[0])
    y = layout_docx_pt(bbox[1])
    width = max(1.0, layout_docx_pt(bbox[2] - bbox[0]))
    source_height = max(1.0, layout_docx_pt(bbox[3] - bbox[1]))
    font_size = layout_docx_font_size(block_type, bbox, item.get("text") or "")
    output_font_size = layout_docx_output_font_size(font_size)
    height = layout_docx_safe_text_height(item, source_height, font_size)
    is_bold = block_type == "title"
    weight = "font-weight:bold;" if is_bold else ""
    align = "center" if block_type == "title" or "caption" in block_type else "justify"
    shape_style = (
        f"position:absolute;margin-left:{x:.2f}pt;margin-top:{y:.2f}pt;"
        f"width:{width:.2f}pt;height:{height:.2f}pt;z-index:1;v-text-anchor:top;"
        "mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
        f"font-family:'Times New Roman','SimSun';font-size:{output_font_size:.2f}pt;line-height:1.08;{weight}"
    )
    runs = layout_docx_text_runs_from_html(body_html, font_size, bold=is_bold)
    paragraph = layout_docx_paragraph_xml(runs, align=align, font_size=output_font_size)
    return (
        f'<w:r><w:pict><v:rect id="{shape_id}" style="{shape_style}" stroked="f" filled="f">'
        f'<v:textbox style="mso-fit-shape-to-text:t" inset="0,0,0,0"><w:txbxContent>'
        f'{paragraph}</w:txbxContent></v:textbox></v:rect></w:pict></w:r>'
    )


def layout_docx_image_anchor(item: dict, rel_id: str, shape_id: str) -> str:
    bbox = item["bbox"]
    x = layout_docx_pt(bbox[0])
    y = layout_docx_pt(bbox[1])
    width = max(1.0, layout_docx_pt(bbox[2] - bbox[0]))
    height = max(1.0, layout_docx_pt(bbox[3] - bbox[1]))
    shape_style = (
        f"position:absolute;margin-left:{x:.2f}pt;margin-top:{y:.2f}pt;"
        f"width:{width:.2f}pt;height:{height:.2f}pt;z-index:2;"
        "mso-position-horizontal-relative:page;mso-position-vertical-relative:page"
    )
    match = re.search(r"(\d+)$", str(shape_id))
    shape_number = int(match.group(1)) if match else 1
    office_shape_id = f"_x0000_s{1024 + max(1, shape_number)}"
    return (
        f'<w:r><w:pict><v:shape id="{shape_id}" type="#_x0000_t75" o:spid="{office_shape_id}" '
        f'o:allowincell="f" style="{shape_style}" stroked="f">'
        f'<v:imagedata r:id="{rel_id}" o:title="layout-image"/>'
        f'</v:shape></w:pict></w:r>'
    )


def layout_docx_settings_xml() -> str:
    """Return modern Word compatibility settings for generated DOCX files."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:compat><w:compatSetting w:name="compatibilityMode" '
        'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
        '</w:compat></w:settings>'
    )


def layout_docx_table_anchor(item: dict, shape_id: str) -> str:
    bbox = item["bbox"]
    x = layout_docx_pt(bbox[0])
    y = layout_docx_pt(bbox[1])
    width = max(1.0, layout_docx_pt(bbox[2] - bbox[0]))
    height = max(1.0, layout_docx_pt(bbox[3] - bbox[1]))
    shape_style = (
        f"position:absolute;margin-left:{x:.2f}pt;margin-top:{y:.2f}pt;"
        f"width:{width:.2f}pt;height:{height:.2f}pt;z-index:1;v-text-anchor:top;"
        "mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
        f"font-family:'Times New Roman','SimSun';font-size:{layout_docx_output_font_size(7.2):.2f}pt;line-height:1.08;"
    )
    table_xml = layout_docx_table_xml(item.get("table_rows") or [])
    return (
        f'<w:r><w:pict><v:rect id="{shape_id}" style="{shape_style}" stroked="f" filled="f">'
        f'<v:textbox inset="0,0,0,0"><w:txbxContent>'
        f'{table_xml}</w:txbxContent></v:textbox></v:rect></w:pict></w:r>'
    )


def render_layout_docx(markdown_path: Path, out_path: Path, *, translated: bool = False, log=None) -> Path:
    bundle = load_layout_translation_bundle(markdown_path) if translated else None
    if translated and bundle is None:
        raise RuntimeError("当前文档还没有排版译文数据，请先在排版阅读模式下完成翻译后再导出排版译文 Word。")
    if bundle is None:
        bundle = load_layout_preview_bundle(markdown_path)
    if not bundle:
        raise RuntimeError("当前文档缺少 MinerU layout.json，无法导出排版 Word。")

    page_info = bundle["page_info"]
    asset_dir = Path(bundle.get("asset_dir") or layout_preview_asset_dir(markdown_path) or markdown_path.parent)
    media_files: list[Path] = []
    rel_entries: list[str] = []
    document_parts: list[str] = []
    shape_count = 0

    valid_pages = [page for page in page_info if isinstance(page, dict)]
    if not valid_pages:
        raise RuntimeError("当前文档的 layout.json 中没有可导出的页面。")
    last_section_xml = ""
    for page_index, page in enumerate(valid_pages):
        raw_page_size = page.get("page_size") or [612, 792]
        page_width = max(1.0, float(raw_page_size[0]))
        page_height = max(1.0, float(raw_page_size[1]))
        page_width_twip = int(layout_docx_pt(page_width) * 20)
        page_height_twip = int(layout_docx_pt(page_height) * 20)
        items = sorted(
            layout_docx_collect_blocks(page.get("preproc_blocks") or [], asset_dir),
            key=lambda value: (value["bbox"][1], value["bbox"][0]),
        )
        page_runs: list[str] = []
        for item in items:
            shape_count += 1
            shape_id = f"layout_shape_{shape_count}"
            image_path = item.get("image_path")
            if item.get("table_rows"):
                page_runs.append(layout_docx_table_anchor(item, shape_id))
            elif image_path:
                media_files.append(Path(image_path))
                extension = Path(image_path).suffix.lower().lstrip(".") or "png"
                rel_id = f"rId{len(media_files)}"
                rel_entries.append(
                    f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image{len(media_files)}.{extension}"/>'
                )
                page_runs.append(layout_docx_image_anchor(item, rel_id, shape_id))
            else:
                page_runs.append(layout_docx_text_anchor(item, shape_id))
        # Keep every page's absolutely positioned objects in one zero-height
        # carrier paragraph. Separate carrier paragraphs make VML positions
        # relative to successively lower paragraph anchors in Word.
        document_parts.append(
            '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="1" w:lineRule="exact"/>'
            '<w:keepNext/><w:keepLines/></w:pPr>' + "".join(page_runs) + '</w:p>'
        )
        section_xml = (
            f'<w:sectPr><w:type w:val="nextPage"/><w:pgSz w:w="{page_width_twip}" w:h="{page_height_twip}"/>'
            f'<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/>'
            '</w:sectPr>'
        )
        if page_index < len(valid_pages) - 1:
            document_parts.append(f'<w:p><w:pPr>{section_xml}</w:pPr></w:p>')
        else:
            last_section_xml = section_xml

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        + "".join(document_parts)
        + last_section_xml
        + '</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        '<Default Extension="jpg" ContentType="image/jpeg"/>'
        '<Default Extension="jpeg" ContentType="image/jpeg"/>'
        '<Default Extension="webp" ContentType="image/webp"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '</Types>'
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + '<Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
        + "".join(rel_entries)
        + '</Relationships>'
    )

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/settings.xml", layout_docx_settings_xml())
        for index, image_path in enumerate(media_files, start=1):
            extension = image_path.suffix.lower().lstrip(".") or "png"
            archive.write(image_path, f"word/media/image{index}.{extension}")
    tmp_path.replace(out_path)
    if log:
        log(f"{'排版译文' if translated else '排版原文'}可编辑 Word 文档已生成：{out_path}")
    return out_path


_LAYOUT_DOCX_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class LayoutDocxDomParser(HTMLParser):
    """Small dependency-free HTML tree builder for generated layout previews."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "root", "attrs": {}, "children": []}
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {
            "tag": str(tag or "").lower(),
            "attrs": {str(key): str(value or "") for key, value in attrs},
            "children": [],
        }
        self.stack[-1]["children"].append(node)
        if node["tag"] not in _LAYOUT_DOCX_VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].get("tag") == str(tag or "").lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        wanted = str(tag or "").lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].get("tag") == wanted:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1]["children"].append({"tag": "", "text": data, "attrs": {}, "children": []})


def layout_docx_dom_classes(node: dict) -> set[str]:
    return {part for part in str((node.get("attrs") or {}).get("class") or "").split() if part}


def layout_docx_dom_find(node: dict, predicate) -> list[dict]:
    found: list[dict] = []
    for child in node.get("children") or []:
        if not isinstance(child, dict) or not child.get("tag"):
            continue
        if predicate(child):
            found.append(child)
        found.extend(layout_docx_dom_find(child, predicate))
    return found


def layout_docx_dom_text(node: dict) -> str:
    if not node.get("tag"):
        return str(node.get("text") or "")
    return "".join(layout_docx_dom_text(child) for child in (node.get("children") or []) if isinstance(child, dict))


def layout_docx_dom_html(node: dict, *, inner: bool = False) -> str:
    def serialize(value: dict) -> str:
        if not value.get("tag"):
            return html.escape(str(value.get("text") or ""), quote=False)
        attrs = "".join(
            f' {html.escape(str(key), quote=True)}="{html.escape(str(attr), quote=True)}"'
            for key, attr in (value.get("attrs") or {}).items()
        )
        content = "".join(serialize(child) for child in (value.get("children") or []) if isinstance(child, dict))
        if value.get("tag") in _LAYOUT_DOCX_VOID_TAGS:
            return f'<{value["tag"]}{attrs}>'
        return f'<{value["tag"]}{attrs}>{content}</{value["tag"]}>'

    if inner:
        return "".join(serialize(child) for child in (node.get("children") or []) if isinstance(child, dict))
    return serialize(node)


def layout_docx_css_values(style_text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for declaration in str(style_text or "").split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        values[name.strip().lower()] = value.strip()
    return values


def layout_docx_css_number(values: dict[str, str], name: str, default: float = 0.0) -> float:
    match = re.search(r"-?[0-9]+(?:\.[0-9]+)?", str(values.get(name) or ""))
    return float(match.group(0)) if match else float(default)


def layout_docx_default_line_ratio(block_type: str) -> float:
    """Return the line-height ratio used by the layout preview CSS."""
    kind = str(block_type or "").lower()
    if kind == "ref_text":
        return 1.06
    if "caption" in kind or kind == "table_footnote":
        return 1.2
    if kind in {"interline_equation", "equation"}:
        return 1.15
    return 1.12


def layout_docx_runtime_state_for_item(
    runtime_state: dict | None,
    page_index: int,
    item_index: int,
) -> dict:
    """Look up styles captured from the final in-browser layout DOM."""
    if not isinstance(runtime_state, dict):
        return {}
    pages = runtime_state.get("pages")
    if not isinstance(pages, list) and isinstance(runtime_state.get("state"), dict):
        pages = runtime_state["state"].get("pages")
    if not isinstance(pages, list) or page_index >= len(pages):
        return {}
    page_state = pages[page_index]
    if not isinstance(page_state, dict):
        return {}
    items = page_state.get("items")
    if not isinstance(items, list) or item_index >= len(items):
        return {}
    item_state = items[item_index]
    return item_state if isinstance(item_state, dict) else {}


def layout_docx_alignment_for_item(item: dict) -> str:
    """Resolve the same alignment that the layout HTML actually displays."""
    runtime_align = str(item.get("runtime_align") or "").strip().lower()
    runtime_align = {"start": "left", "end": "right"}.get(runtime_align, runtime_align)
    if runtime_align in {"left", "center", "right", "justify"}:
        return runtime_align

    node = item.get("node") or {}
    attrs = node.get("attrs") or {}
    classes = layout_docx_dom_classes(node)
    block_type = str(item.get("type") or "").lower()
    if "layout-flow-stream" in classes:
        if block_type == "ref_text" or "refs" in classes:
            return "left"
        if str(attrs.get("data-original-lines") or "").lower() == "single":
            single_align = str(attrs.get("data-single-line-align") or "").lower()
            if single_align in {"left", "center", "right"}:
                return single_align
        return "justify"
    if "main-title" in classes:
        return "center"
    if block_type == "title":
        return "left"
    if block_type in {"header", "page_header", "footer", "page_footer", "page_footnote"}:
        return "center"
    if "caption" in block_type or block_type == "table_footnote":
        return "left"
    if block_type in {"page_number", "ref_text"}:
        return "left"
    if block_type in {"interline_equation", "equation"}:
        return "center"
    return "justify"


def layout_docx_runtime_state_script() -> str:
    """Collect final layout styles without re-typesetting or walking descendants."""
    return r"""
    (() => {
      if (!document.body || document.body.dataset.layoutFitState !== 'ready') {
        return JSON.stringify({version: 5, error: 'layout-not-ready'});
      }
      const pages = Array.from(document.querySelectorAll('.layout-page')).map((page) => {
        const shell = page.closest('.layout-page-shell');
        const pageWidth = Number.parseFloat((shell && shell.dataset.pageWidth) || page.style.width) || 1;
        const renderedWidth = Math.max(1, (shell && shell.getBoundingClientRect().width) || pageWidth);
        const pageRect = page.getBoundingClientRect();
        const pageScale = renderedWidth / pageWidth;
        const nodes = Array.from(page.children).filter((node) => (
          node.classList && (
            node.classList.contains('layout-flow-stream') ||
            node.classList.contains('layout-block')
          )
        ));
        return {
          // The reader displays the PDF-point coordinate page through this
          // scale.  Keep it with the finalized state so PDF export can retain
          // the same apparent glyph size instead of silently normalizing it.
          page_scale: renderedWidth / pageWidth,
          items: nodes.map((node) => {
            const computed = getComputedStyle(node);
            const fontSize = Number.parseFloat(computed.fontSize) || 8;
            const lineHeight = Number.parseFloat(computed.lineHeight) || (fontSize * 1.12);
            const weight = Number.parseInt(computed.fontWeight, 10);
            const boxHeight = Math.max(1, node.clientHeight || node.getBoundingClientRect().height || 1);
            const contentHeight = Math.max(boxHeight, node.scrollHeight || 0);
            const math = node.querySelector('.layout-equation-formula mjx-container, .layout-math-display mjx-container');
            const transform = math ? (math.style.transform || getComputedStyle(math).transform || '') : '';
            const scaleMatch = transform.match(/scale\(\s*([0-9.]+)\s*\)/) || transform.match(/matrix\(\s*([0-9.]+)/);
            const mathScale = scaleMatch ? Number.parseFloat(scaleMatch[1]) : 1;
            const rectInPage = (element) => {
              if (!element) return null;
              const rect = element.getBoundingClientRect();
              if (!(rect.width > 0) || !(rect.height > 0)) return null;
              return {
                x: (rect.left - pageRect.left) / pageScale,
                y: (rect.top - pageRect.top) / pageScale,
                width: rect.width / pageScale,
                height: rect.height / pageScale
              };
            };
            const equationNumber = node.querySelector('.layout-equation-number');
            return {
              // Layout CSS uses PDF-point coordinate numbers as pixels
              // (a 612-wide page is represented as 612px).  DOCX anchors
              // preserve that same 612-point page, so converting only text
              // by 72/96 shrinks every glyph by 25% relative to its bbox.
              font_size: fontSize,
              line_ratio: lineHeight / Math.max(0.1, fontSize),
              align: computed.textAlign,
              bold: Number.isFinite(weight) ? weight >= 600 : computed.fontWeight === 'bold',
              content_ratio: contentHeight / boxHeight,
              // Long formulas are scaled by the reader to fit their source
              // box; use that exact scale for Word's native math too.
              math_scale: Number.isFinite(mathScale) ? Math.max(0.1, Math.min(1, mathScale)) : 1,
              math_bbox: rectInPage(math),
              equation_number_bbox: rectInPage(equationNumber)
            };
          })
        };
      });
      const fitNodes = Array.from(document.querySelectorAll('.layout-flow-stream, .layout-block'));
      const fitCache = {
        version: document.body.dataset.layoutCacheVersion || '',
        complete: true,
        count: fitNodes.length,
        styles: fitNodes.map((node) => ({
          f: node.style.fontSize || '',
          l: node.style.lineHeight || '',
          o: node.dataset.originalLines || ''
        }))
      };
      return JSON.stringify({version: 5, pages, fit_cache: fitCache});
    })();
    """


def layout_docx_tex_source(value: str) -> str:
    text = html.unescape(str(value or "")).strip()
    if (text.startswith(r"\(") and text.endswith(r"\)")) or (text.startswith(r"\[") and text.endswith(r"\]")):
        text = text[2:-2]
    return re.sub(r"\s+", " ", text).strip()


def layout_docx_collect_tex(html_text: str) -> list[str]:
    formulas: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\\\((.+?)\\\)|\\\[(.+?)\\\]", html.unescape(str(html_text or "")), flags=re.S):
        tex = layout_docx_tex_source(match.group(0))
        if tex and tex not in seen:
            seen.add(tex)
            formulas.append(tex)
    return formulas


def layout_docx_build_omml_map(formulas: list[str], *, log=None) -> dict[str, str]:
    if not formulas:
        return {}
    pandoc = find_pandoc_for_workspace(WORKSPACE)
    if not pandoc:
        raise RuntimeError("未找到 pandoc.exe，无法将排版 Word 中的公式转为可编辑的 Word 原生公式。")
    with tempfile.TemporaryDirectory(prefix="mineru-layout-omml-") as temp_name:
        temp_dir = Path(temp_name)
        markdown_path = temp_dir / "formulas.md"
        docx_path = temp_dir / "formulas.docx"
        markdown = "\n\n".join(f"FORMULA_{index:04d} $\\displaystyle {tex}$" for index, tex in enumerate(formulas))
        markdown_path.write_text(markdown, encoding="utf-8")
        result = subprocess.run(
            [str(pandoc), str(markdown_path), "--from", "markdown+tex_math_dollars", "--output", str(docx_path)],
            cwd=str(temp_dir),
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not docx_path.exists():
            detail = (result.stderr or result.stdout or "Pandoc 公式转换失败").strip()
            raise RuntimeError(detail)
        with zipfile.ZipFile(docx_path, "r") as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    namespaces = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    }
    converted: dict[str, str] = {}
    for paragraph in root.findall(".//w:p", namespaces):
        marker = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespaces))
        marker_match = re.search(r"FORMULA_(\d{4})", marker)
        math_node = paragraph.find(".//m:oMath", namespaces)
        if not marker_match or math_node is None:
            continue
        formula_index = int(marker_match.group(1))
        if formula_index < len(formulas):
            converted[formulas[formula_index]] = ET.tostring(math_node, encoding="unicode")
    if log:
        log(f"Word 原生数学公式已转换：{len(converted)}/{len(formulas)} 个。")
    return converted


def layout_docx_mixed_runs_from_html(
    fragment_html: str,
    font_size: float,
    omml_map: dict[str, str],
    *,
    bold: bool = False,
) -> str:
    normalized = str(fragment_html or "")
    normalized = re.sub(r"<\s*br\s*/?\s*>", "\n", normalized, flags=re.I)
    normalized = re.sub(r"<\s*sup\b[^>]*>(.*?)</\s*sup\s*>", r"[[SUP:\1]]", normalized, flags=re.I | re.S)
    normalized = re.sub(r"<\s*sub\b[^>]*>(.*?)</\s*sub\s*>", r"[[SUB:\1]]", normalized, flags=re.I | re.S)
    normalized = re.sub(r"<[^>]+>", "", normalized)
    normalized = html.unescape(normalized)
    token_pattern = re.compile(r"(\\\(.+?\\\)|\\\[.+?\\\]|\[\[(?:SUP|SUB):.*?\]\]|\n+)", flags=re.S)
    parts = token_pattern.split(normalized)
    runs: list[str] = []
    for part in parts:
        if not part:
            continue
        if (part.startswith(r"\(") and part.endswith(r"\)")) or (part.startswith(r"\[") and part.endswith(r"\]")):
            tex = layout_docx_tex_source(part)
            formula_xml = omml_map.get(tex)
            if formula_xml:
                runs.append(layout_docx_omml_with_font_size(formula_xml, layout_docx_output_font_size(font_size)))
            else:
                props = layout_docx_run_properties(font_size, bold=bold)
                runs.append(f'<w:r><w:rPr>{props}</w:rPr><w:t>{layout_docx_escape_text(tex)}</w:t></w:r>')
            continue
        vertical_align = ""
        text = part
        if part.startswith("[[SUP:") and part.endswith("]]" ):
            vertical_align, text = "superscript", part[6:-2]
        elif part.startswith("[[SUB:") and part.endswith("]]" ):
            vertical_align, text = "subscript", part[6:-2]
        if part.startswith("\n"):
            props = layout_docx_run_properties(font_size, bold=bold)
            runs.extend(f"<w:r><w:rPr>{props}</w:rPr><w:br/></w:r>" for _ in range(max(1, part.count("\n"))))
            continue
        if text:
            props = layout_docx_run_properties(font_size, bold=bold, vertical_align=vertical_align)
            runs.append(
                f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{layout_docx_escape_text(text)}</w:t></w:r>'
            )
    return "".join(runs) or "<w:r><w:t></w:t></w:r>"


def layout_docx_omml_with_font_size(formula_xml: str, font_size: float) -> str:
    """Apply Word-point sizing and solid operator strokes to an OMML formula."""
    namespaces = {
        "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }
    try:
        root = ET.fromstring(formula_xml)
    except ET.ParseError:
        return formula_xml
    word_ns = namespaces["w"]
    half_points = str(max(2, int(round(max(1.0, float(font_size)) * 2.0))))
    math_ns = namespaces["m"]
    for math_run in root.findall(".//m:r", namespaces):
        math_props = math_run.find("m:rPr", namespaces)
        if math_props is None:
            math_props = ET.Element(f"{{{math_ns}}}rPr")
            math_run.insert(0, math_props)
        word_props = math_props.find("w:rPr", namespaces)
        if word_props is None:
            word_props = ET.SubElement(math_props, f"{{{word_ns}}}rPr")
        for size in list(word_props.findall("w:sz", namespaces)):
            word_props.remove(size)
        for color in list(word_props.findall("w:color", namespaces)):
            word_props.remove(color)
        ET.SubElement(
            word_props,
            f"{{{word_ns}}}rFonts",
            {
                f"{{{word_ns}}}ascii": "Cambria Math",
                f"{{{word_ns}}}hAnsi": "Cambria Math",
                f"{{{word_ns}}}cs": "Cambria Math",
            },
        )
        ET.SubElement(word_props, f"{{{word_ns}}}color", {f"{{{word_ns}}}val": "000000"})
        ET.SubElement(word_props, f"{{{word_ns}}}sz", {f"{{{word_ns}}}val": half_points})
    return ET.tostring(root, encoding="unicode")


def layout_docx_dom_paragraphs(node: dict) -> list[dict]:
    direct = [
        child
        for child in (node.get("children") or [])
        if isinstance(child, dict) and layout_docx_dom_classes(child).intersection({"flow-para", "flow-ref"})
    ]
    return direct or [node]


def layout_docx_dom_text_anchor(item: dict, shape_id: str, omml_map: dict[str, str]) -> str:
    node = item["node"]
    font_size = max(1.0, float(item["font_size"]))
    output_font_size = layout_docx_output_font_size(font_size)
    block_type = str(item.get("type") or "")
    runtime_bold = item.get("runtime_bold")
    bold = bool(runtime_bold) if runtime_bold is not None else block_type == "title"
    align = layout_docx_alignment_for_item(item)
    paragraphs: list[str] = []
    for paragraph_node in layout_docx_dom_paragraphs(node):
        paragraph_style = layout_docx_css_values(str((paragraph_node.get("attrs") or {}).get("style") or ""))
        first_line = layout_docx_css_number(paragraph_style, "text-indent", 0.0)
        runs = layout_docx_mixed_runs_from_html(layout_docx_dom_html(paragraph_node, inner=True), font_size, omml_map, bold=bold)
        effective_line_ratio = (
            max(1.18, float(item.get("line_ratio") or 1.18))
            if block_type == "title" or float(item.get("line_ratio") or 1.0) < 1.15
            else float(item["line_ratio"])
        )
        line_twips = max(20, int(round(output_font_size * 20.0 * effective_line_ratio)))
        after_twips = max(0, int(round(output_font_size * 20.0 * float(item["para_gap"]))))
        indent_xml = f'<w:ind w:firstLine="{int(round(first_line * 20.0))}"/>' if abs(first_line) >= 0.05 else ""
        jc = {"center": "center", "right": "right", "justify": "both"}.get(align, "left")
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:before="0" w:after="{after_twips}" w:line="{line_twips}" w:lineRule="exact"/>'
            f'{indent_xml}<w:jc w:val="{jc}"/></w:pPr>{runs}</w:p>'
        )
    x, y, width, source_height = item["bbox"]
    height = layout_docx_safe_text_height(item, source_height, font_size)
    weight = "font-weight:bold;" if bold else ""
    shape_style = (
        f"position:absolute;margin-left:{x:.2f}pt;margin-top:{y:.2f}pt;width:{width:.2f}pt;height:{height:.2f}pt;"
        "z-index:1;v-text-anchor:top;mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
        f"font-family:'Times New Roman','SimSun';font-size:{output_font_size:.2f}pt;{weight}"
    )
    return (
        f'<w:r><w:pict><v:rect id="{shape_id}" style="{shape_style}" stroked="f" filled="f">'
        f'<v:textbox style="mso-fit-shape-to-text:t" inset="0,0,0,0"><w:txbxContent>{"".join(paragraphs)}</w:txbxContent></v:textbox>'
        '</v:rect></w:pict></w:r>'
    )


def layout_docx_dom_equation_anchor(item: dict, shape_id: str, omml_map: dict[str, str]) -> str:
    formula_nodes = layout_docx_dom_find(item["node"], lambda value: "layout-math" in layout_docx_dom_classes(value))
    tex = layout_docx_tex_source(layout_docx_dom_text(formula_nodes[0])) if formula_nodes else ""
    formula_xml = omml_map.get(tex)
    if not formula_xml:
        return layout_docx_dom_text_anchor(item, shape_id, omml_map)
    try:
        math_scale = float(item.get("runtime_math_scale") or 1.0)
    except (TypeError, ValueError):
        math_scale = 1.0
    formula_xml = layout_docx_omml_with_font_size(
        formula_xml,
        layout_docx_output_font_size(float(item["font_size"])) * layout_docx_formula_point_scale(tex) * max(0.1, min(1.0, math_scale)),
    )
    number_nodes = layout_docx_dom_find(item["node"], lambda value: "layout-equation-number" in layout_docx_dom_classes(value))
    number = layout_docx_dom_text(number_nodes[0]).strip() if number_nodes else ""
    x, y, width, source_height = item["bbox"]
    height = layout_docx_safe_text_height(item, source_height, float(item["font_size"]), formula_text=tex)

    # In HTML, the equation text container can span to the column right margin.
    # Extract the --equation-number-right CSS variable from .layout-equation-text
    # so the formula textbox in Word is wide enough to avoid artificial wrapping,
    # and the equation number aligns to the actual column right edge.
    txt_nodes = layout_docx_dom_find(item["node"], lambda n: "layout-equation-text" in layout_docx_dom_classes(n))
    style_str = str((txt_nodes[0].get("attrs") or {}).get("style") or "") if txt_nodes else ""
    style_values = layout_docx_css_values(style_str)
    number_right_offset = layout_docx_css_number(style_values, "--equation-number-right", None)

    number_width = 34.0
    number_bbox = item.get("runtime_equation_number_bbox") or {}
    try:
        number_x = float(number_bbox.get("x"))
        number_width = max(12.0, float(number_bbox.get("width")) + 1.0)
    except (AttributeError, TypeError, ValueError):
        if number_right_offset is not None and float(number_right_offset) > 0:
            number_x = x + float(number_right_offset) - number_width
        else:
            number_x = x + width - number_width

    if number:
        formula_width = max(width, number_x - x - 2.0)
    else:
        if number_right_offset is not None and float(number_right_offset) > 0:
            formula_width = max(width, float(number_right_offset))
        else:
            formula_width = width

    shape_style = (
        f"position:absolute;margin-left:{x:.2f}pt;margin-top:{y:.2f}pt;width:{formula_width:.2f}pt;height:{height:.2f}pt;"
        "z-index:1;v-text-anchor:top;mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
    )
    paragraph = f'<w:p><w:pPr><w:jc w:val="left"/></w:pPr>{formula_xml}</w:p>'
    formula_anchor = (
        f'<w:r><w:pict><v:rect id="{shape_id}" style="{shape_style}" stroked="f" filled="f">'
        f'<v:textbox style="mso-fit-shape-to-text:t" inset="0,0,0,0"><w:txbxContent>{paragraph}</w:txbxContent></v:textbox>'
        '</v:rect></w:pict></w:r>'
    )
    if not number:
        return formula_anchor

    number_props = layout_docx_run_properties(layout_docx_output_font_size(float(item["font_size"])))
    number_style = (
        f"position:absolute;margin-left:{number_x:.2f}pt;margin-top:{y:.2f}pt;width:{number_width:.2f}pt;height:{height:.2f}pt;"
        "z-index:1;v-text-anchor:top;mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
    )
    number_paragraph = (
        f'<w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:rPr>{number_props}</w:rPr>'
        f'<w:t>{layout_docx_escape_text(number)}</w:t></w:r></w:p>'
    )
    return (
        formula_anchor
        + f'<w:r><w:pict><v:rect id="{shape_id}_number" style="{number_style}" stroked="f" filled="f">'
        + f'<v:textbox style="mso-fit-shape-to-text:t" inset="0,0,0,0"><w:txbxContent>{number_paragraph}</w:txbxContent></v:textbox>'
        + '</v:rect></w:pict></w:r>'
    )


def layout_docx_file_uri_path(uri: str) -> Path | None:
    parsed = urllib.parse.urlparse(str(uri or ""))
    if parsed.scheme and parsed.scheme.lower() != "file":
        return None
    raw_path = urllib.parse.unquote(parsed.path if parsed.scheme else str(uri or ""))
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", raw_path):
        raw_path = raw_path[1:]
    path = Path(raw_path)
    return path if path.exists() else None


def layout_docx_equation_tex(item: dict) -> str:
    """Return the TeX represented by one parsed positioned equation item."""
    formula_nodes = layout_docx_dom_find(
        item.get("node") or {},
        lambda value: "layout-math" in layout_docx_dom_classes(value),
    )
    return layout_docx_tex_source(layout_docx_dom_text(formula_nodes[0])) if formula_nodes else ""


def layout_docx_apply_equation_clearance(items: list[dict]) -> None:
    """Move anchors down when an expanded native equation or shifted block needs space.

    The source HTML has initial coordinates. When multi-level native equations
    grow vertically, both the equations and any subsequent text/equation blocks
    in the same column must shift downwards smoothly without colliding or overlapping.
    Subfigure label markers that overlap chart edges by design are excluded from
    triggering artificial shifts.
    """
    placed: list[tuple[float, float, float, float, str, bool]] = []
    for item in sorted(items, key=lambda value: (float(value["bbox"][1]), float(value["bbox"][0]))):
        if item.get("skip_docx_export"):
            continue
        x, y, width, source_height = (float(value) for value in item["bbox"])
        block_type = str(item.get("type") or "").lower()
        is_equation = block_type in {"interline_equation", "equation"}
        is_subfig_caption = (
            block_type in {"chart_caption", "image_caption"}
            and bool(re.fullmatch(r"\([a-z]\)", re.sub(r"\s+", "", layout_docx_dom_text(item.get("node") or "")), flags=re.I))
        )
        required_y = y
        for previous_x, previous_y, previous_width, previous_bottom, previous_type, previous_is_subfig in placed:
            horizontal_overlap = max(0.0, min(x + width, previous_x + previous_width) - max(x, previous_x))
            if horizontal_overlap >= min(width, previous_width) * 0.30:
                if is_subfig_caption and previous_type in {"chart_body", "image_body", "image"}:
                    continue
                if previous_is_subfig and block_type in {"chart_body", "image_body", "image"}:
                    continue
                if previous_bottom + 1.0 > required_y:
                    required_y = previous_bottom + 1.0
        if required_y > y:
            item["bbox"][1] = required_y
            y = required_y
        if is_equation:
            expected_height = layout_docx_safe_text_height(
                item,
                source_height,
                float(item.get("font_size") or 8.0),
                formula_text=layout_docx_equation_tex(item),
            )
        elif block_type in {"chart_body", "image_body", "image"}:
            expected_height = source_height
        elif is_subfig_caption:
            expected_height = source_height
        else:
            expected_height = layout_docx_safe_text_height(
                item,
                source_height,
                float(item.get("font_size") or 8.0),
            )
        placed.append((x, y, width, y + expected_height, block_type, is_subfig_caption))


def layout_docx_subfigure_caption_pairs(items: list[dict]) -> list[tuple[dict, dict]]:
    """Pair short subfigure labels with the image whose edge they annotate."""
    pairs: list[tuple[dict, dict]] = []
    used_images: set[int] = set()
    for caption in items:
        block_type = str(caption.get("type") or "").lower()
        if block_type not in {"chart_caption", "image_caption"}:
            continue
        text = re.sub(r"\s+", "", layout_docx_dom_text(caption.get("node") or {}))
        if not re.fullmatch(r"\([a-z]\)", text, flags=re.IGNORECASE):
            continue
        caption_bbox = caption.get("bbox") or []
        if len(caption_bbox) < 4:
            continue
        caption_x, caption_y, caption_width, caption_height = (float(value) for value in caption_bbox[:4])
        caption_right = caption_x + caption_width
        caption_bottom = caption_y + caption_height
        nearest: tuple[float, dict] | None = None
        for image in items:
            if id(image) in used_images or not image.get("image_path"):
                continue
            image_bbox = image.get("bbox") or []
            if len(image_bbox) < 4:
                continue
            image_x, image_y, image_width, image_height = (float(value) for value in image_bbox[:4])
            image_right = image_x + image_width
            image_bottom = image_y + image_height
            horizontal_gap = max(0.0, max(image_x - caption_right, caption_x - image_right))
            vertical_gap = max(0.0, max(image_y - caption_bottom, caption_y - image_bottom))
            if horizontal_gap > 12.0 or vertical_gap > 16.0:
                continue
            distance = vertical_gap * 4.0 + horizontal_gap
            if nearest is None or distance < nearest[0]:
                nearest = (distance, image)
        if nearest is not None:
            image = nearest[1]
            pairs.append((caption, image))
            used_images.add(id(image))
    return pairs


def layout_docx_font_for_subfigure_label(size: int):
    """Return a small Windows-compatible raster font for a chart label."""
    from PIL import ImageFont

    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"
    try:
        return ImageFont.truetype(str(font_path), size=size)
    except OSError:
        return ImageFont.load_default()


def layout_docx_clear_subfigure_marker_pixels(image, scale: int) -> None:
    """Erase only dark connected components belonging to a clipped edge label."""
    width, height = image.size
    region_width = min(width, max(1, int(round(28.0 * scale))))
    region_height = min(height, max(1, int(round(22.0 * scale))))
    component_limit_x = int(round(24.0 * scale))
    component_limit_y = int(round(18.0 * scale))
    pixels = image.load()
    visited: set[tuple[int, int]] = set()
    for start_y in range(region_height):
        for start_x in range(region_width):
            if (start_x, start_y) in visited:
                continue
            red, green, blue = pixels[start_x, start_y]
            if min(red, green, blue) >= 245:
                continue
            stack = [(start_x, start_y)]
            visited.add((start_x, start_y))
            component: list[tuple[int, int]] = []
            min_x = region_width
            min_y = region_height
            while stack:
                x, y = stack.pop()
                component.append((x, y))
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                for next_x, next_y in (
                    (x - 1, y),
                    (x + 1, y),
                    (x, y - 1),
                    (x, y + 1),
                    (x - 1, y - 1),
                    (x + 1, y - 1),
                    (x - 1, y + 1),
                    (x + 1, y + 1),
                ):
                    if not (0 <= next_x < region_width and 0 <= next_y < region_height):
                        continue
                    if (next_x, next_y) in visited:
                        continue
                    next_red, next_green, next_blue = pixels[next_x, next_y]
                    if min(next_red, next_green, next_blue) >= 245:
                        continue
                    visited.add((next_x, next_y))
                    stack.append((next_x, next_y))
            if min_x <= component_limit_x and min_y <= component_limit_y:
                for x, y in component:
                    for clear_x in range(max(0, x - 1), min(width, x + 2)):
                        for clear_y in range(max(0, y - 1), min(height, y + 2)):
                            pixels[clear_x, clear_y] = (255, 255, 255)


def layout_docx_compose_subfigure_labels(pages: list[dict], output_dir: Path) -> None:
    """Rasterize short labels beneath their image using the browser paint order.

    MinerU often extracts a subfigure marker separately even though part of the
    marker remains in the cropped raster. Chromium paints the image after that
    text, hiding the duplicated part. Word VML does not reproduce that overlap
    consistently, so package the two layers as one image for Word only.
    """
    from PIL import Image, ImageDraw

    output_dir.mkdir(parents=True, exist_ok=True)
    image_index = 0
    for page in pages:
        pairs = layout_docx_subfigure_caption_pairs(page.get("items") or [])
        for caption, image_item in pairs:
            image_path = Path(image_item["image_path"])
            if not image_path.is_file():
                continue
            try:
                caption_x, caption_y, caption_width, caption_height = (
                    float(value) for value in caption["bbox"][:4]
                )
                image_x, image_y, image_width, image_height = (
                    float(value) for value in image_item["bbox"][:4]
                )
                left = min(caption_x, image_x)
                top = min(caption_y, image_y)
                right = max(caption_x + caption_width, image_x + image_width)
                bottom = max(caption_y + caption_height, image_y + image_height)
                scale = 4
                canvas_width = max(1, int(round((right - left) * scale)))
                canvas_height = max(1, int(round((bottom - top) * scale)))
                canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
                with Image.open(image_path) as source:
                    resized = source.convert("RGB").resize(
                        (
                            max(1, int(round(image_width * scale))),
                            max(1, int(round(image_height * scale))),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                # The source crop may retain a partial ``a)``/``)`` marker at
                # its upper-left edge. Clear only connected edge glyphs;
                # axis ticks and legends remain intact.
                layout_docx_clear_subfigure_marker_pixels(resized, scale)
                canvas.paste(
                    resized,
                    (int(round((image_x - left) * scale)), int(round((image_y - top) * scale))),
                )
                draw = ImageDraw.Draw(canvas)
                label = layout_docx_dom_text(caption.get("node") or {}).strip()
                label_size = max(1, int(round(float(caption.get("font_size") or 7.6) * scale)))
                draw.text(
                    (int(round((caption_x - left) * scale)), int(round((caption_y - top) * scale))),
                    label,
                    fill="black",
                    font=layout_docx_font_for_subfigure_label(label_size),
                    anchor="lt",
                )
                image_index += 1
                composite_path = output_dir / f"subfigure-{image_index:03d}.png"
                canvas.save(composite_path, format="PNG")
            except (OSError, ValueError):
                continue
            caption["skip_docx_export"] = True
            image_item["image_path"] = composite_path
            image_item["bbox"] = [left, top, right - left, bottom - top]


def layout_docx_items_from_html(
    html_path: Path,
    runtime_state: dict | None = None,
) -> list[dict]:
    parser = LayoutDocxDomParser()
    html_text = html_path.read_text(encoding="utf-8", errors="replace")
    parser.feed(html_text)
    pages = layout_docx_dom_find(parser.root, lambda value: value.get("tag") == "section" and "layout-page-wrap" in layout_docx_dom_classes(value))
    result: list[dict] = []
    for page_index, page in enumerate(pages):
        attrs = page.get("attrs") or {}
        page_width = max(1.0, float(attrs.get("data-page-width") or 612.0))
        page_height = max(1.0, float(attrs.get("data-page-height") or 792.0))
        page_nodes = layout_docx_dom_find(page, lambda value: "layout-page" in layout_docx_dom_classes(value))
        if not page_nodes:
            continue
        items: list[dict] = []
        item_index = 0
        for node in page_nodes[0].get("children") or []:
            if not isinstance(node, dict):
                continue
            classes = layout_docx_dom_classes(node)
            if not classes.intersection({"layout-flow-stream", "layout-block"}):
                continue
            node_attrs = node.get("attrs") or {}
            css = layout_docx_css_values(str(node_attrs.get("style") or ""))
            x = layout_docx_css_number(css, "left")
            y = layout_docx_css_number(css, "top")
            width = max(1.0, layout_docx_css_number(css, "width", 1.0))
            height = max(1.0, layout_docx_css_number(css, "height", 1.0))
            block_type = str(node_attrs.get("data-flow-kind") or "")
            if not block_type:
                block_type = next((value[5:] for value in classes if value.startswith("type-")), "text")
            runtime_item = layout_docx_runtime_state_for_item(runtime_state, page_index, item_index)
            runtime_font_size = runtime_item.get("font_size")
            try:
                runtime_font_size = float(runtime_font_size)
            except (TypeError, ValueError):
                runtime_font_size = 0.0
            font_size = runtime_font_size if runtime_font_size > 0 else layout_docx_css_number(
                css,
                "font-size",
                float(node_attrs.get("data-base-font") or 8.0),
            )
            runtime_line_ratio = runtime_item.get("line_ratio")
            try:
                runtime_line_ratio = float(runtime_line_ratio)
            except (TypeError, ValueError):
                runtime_line_ratio = 0.0
            inline_line_ratio = layout_docx_css_number(css, "line-height", 0.0)
            line_ratio = runtime_line_ratio if runtime_line_ratio > 0 else float(
                node_attrs.get("data-line-ratio") or inline_line_ratio or layout_docx_default_line_ratio(block_type)
            )
            para_gap = float(node_attrs.get("data-para-gap") or 0.0)
            runtime_content_ratio = runtime_item.get("content_ratio")
            try:
                runtime_content_ratio = float(runtime_content_ratio)
            except (TypeError, ValueError):
                runtime_content_ratio = 1.0
            runtime_math_scale = runtime_item.get("math_scale")
            try:
                runtime_math_scale = float(runtime_math_scale)
            except (TypeError, ValueError):
                runtime_math_scale = 1.0
            runtime_equation_number_bbox = runtime_item.get("equation_number_bbox")
            if not isinstance(runtime_equation_number_bbox, dict):
                runtime_equation_number_bbox = None
            images = layout_docx_dom_find(node, lambda value: value.get("tag") == "img")
            image_path = layout_docx_file_uri_path(str((images[0].get("attrs") or {}).get("src") or "")) if images else None
            tables = layout_docx_dom_find(node, lambda value: value.get("tag") == "table")
            table_rows = parse_raw_html_table(layout_docx_dom_html(tables[0])) if tables and not image_path else []
            items.append(
                {
                    "node": node,
                    "bbox": [x, y, width, height],
                    "page_width": page_width,
                    "type": block_type,
                    "font_size": max(1.0, font_size),
                    "line_ratio": max(0.5, line_ratio),
                    "para_gap": max(0.0, para_gap),
                    "runtime_align": str(runtime_item.get("align") or "").strip().lower(),
                    "runtime_bold": runtime_item.get("bold"),
                    "runtime_content_ratio": max(1.0, runtime_content_ratio),
                    "runtime_math_scale": max(0.1, min(1.0, runtime_math_scale)),
                    "runtime_equation_number_bbox": runtime_equation_number_bbox,
                    "image_path": image_path,
                    "table_rows": table_rows,
                }
            )
            item_index += 1
        layout_docx_apply_equation_clearance(items)
        result.append({"page_width": page_width, "page_height": page_height, "items": items, "html": html_text})
    return result


def render_layout_editable_html_docx(
    html_path: Path,
    out_path: Path,
    *,
    runtime_state: dict | None = None,
    log=None,
) -> Path:
    """Export the final optimized layout HTML as editable positioned Word objects."""
    if not html_path or not html_path.exists():
        raise RuntimeError("排版 HTML 不存在，无法生成可编辑的所见排版 Word。")
    pages = layout_docx_items_from_html(html_path, runtime_state=runtime_state)
    if not pages:
        raise RuntimeError("排版 HTML 中没有可导出的页面。")
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    composite_dir = Path(tempfile.mkdtemp(prefix=".layout-docx-", dir=str(out_path.parent)))
    try:
        layout_docx_compose_subfigure_labels(pages, composite_dir)
        editable_html = "\n".join(
            layout_docx_dom_html(item["node"], inner=True)
            for page in pages
            for item in page["items"]
            if not item.get("image_path") and not item.get("skip_docx_export")
        )
        formulas = layout_docx_collect_tex(editable_html)
        omml_map = layout_docx_build_omml_map(formulas, log=log)
        media_files: list[Path] = []
        rel_entries: list[str] = []
        document_parts: list[str] = []
        last_section_xml = ""
        shape_count = 0
        for page_index, page in enumerate(pages):
            page_runs: list[str] = []
            for item in page["items"]:
                if item.get("skip_docx_export"):
                    continue
                shape_count += 1
                shape_id = f"layout_html_shape_{shape_count}"
                image_path = item.get("image_path")
                if image_path:
                    media_files.append(Path(image_path))
                    extension = image_path.suffix.lower().lstrip(".") or "png"
                    rel_id = f"rId{len(media_files)}"
                    rel_entries.append(
                        f'<Relationship Id="{rel_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                        f'Target="media/image{len(media_files)}.{extension}"/>'
                    )
                    x, y, width, height = item["bbox"]
                    page_runs.append(layout_docx_image_anchor({"bbox": [x, y, x + width, y + height]}, rel_id, shape_id))
                elif item.get("table_rows"):
                    x, y, width, height = item["bbox"]
                    page_runs.append(
                        layout_docx_table_anchor(
                            {"bbox": [x, y, x + width, y + height], "table_rows": item["table_rows"]},
                            shape_id,
                        )
                    )
                elif item["type"] == "interline_equation":
                    page_runs.append(layout_docx_dom_equation_anchor(item, shape_id, omml_map))
                else:
                    page_runs.append(layout_docx_dom_text_anchor(item, shape_id, omml_map))
            document_parts.append(
                '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="1" w:lineRule="exact"/>'
                '<w:keepNext/><w:keepLines/></w:pPr>' + "".join(page_runs) + '</w:p>'
            )
            section_xml = (
                f'<w:sectPr><w:type w:val="nextPage"/><w:pgSz w:w="{int(round(page["page_width"] * 20))}" '
                f'w:h="{int(round(page["page_height"] * 20))}"/><w:pgMar w:top="0" w:right="0" w:bottom="0" '
                'w:left="0" w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
            )
            if page_index < len(pages) - 1:
                document_parts.append(f'<w:p><w:pPr>{section_xml}</w:pPr></w:p>')
            else:
                last_section_xml = section_xml
        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
            'xmlns:o="urn:schemas-microsoft-com:office:office" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            'xmlns:v="urn:schemas-microsoft-com:vml" '
            'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + "".join(document_parts) + last_section_xml + '</w:body></w:document>'
        )
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/><Default Extension="jpg" ContentType="image/jpeg"/>'
            '<Default Extension="jpeg" ContentType="image/jpeg"/><Default Extension="webp" ContentType="image/webp"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
            '</Types>'
        )
        package_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        )
        document_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + '<Relationship Id="rIdSettings" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
            + "".join(rel_entries) + '</Relationships>'
        )
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", package_rels)
            archive.writestr("word/_rels/document.xml.rels", document_rels)
            archive.writestr("word/document.xml", document_xml)
            archive.writestr("word/settings.xml", layout_docx_settings_xml())
            for index, image_path in enumerate(media_files, start=1):
                extension = image_path.suffix.lower().lstrip(".") or "png"
                archive.write(image_path, f"word/media/image{index}.{extension}")
        tmp_path.replace(out_path)
    finally:
        shutil.rmtree(composite_dir, ignore_errors=True)
    if log:
        log(f"可编辑排版 Word 文档已按精确排版生成：{out_path}")
    return out_path


def collect_layout_audit(bundle: dict) -> dict:
    page_info = bundle.get("page_info") or []
    model_pages = bundle.get("model_pages") or []
    content_pages = bundle.get("content_pages") or []
    preproc_counter: Counter[str] = Counter()
    nested_counter: Counter[str] = Counter()
    model_counter: Counter[str] = Counter()
    content_counter: Counter[str] = Counter()
    pages_summary: list[dict] = []
    for index, page in enumerate(page_info, start=1):
        preproc_types = Counter()
        nested_types = Counter()
        preproc_blocks = page.get("preproc_blocks") or []
        for block in preproc_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "unknown")
            preproc_types[block_type] += 1
            preproc_counter[block_type] += 1
            for child in block.get("blocks") or []:
                if not isinstance(child, dict):
                    continue
                child_type = str(child.get("type") or "unknown")
                nested_types[child_type] += 1
                nested_counter[child_type] += 1
        model_types = Counter(str(item.get("type") or "unknown") for item in (model_pages[index - 1] if index - 1 < len(model_pages) else []) if isinstance(item, dict))
        content_types = Counter(str(item.get("type") or "unknown") for item in (content_pages[index - 1] if index - 1 < len(content_pages) else []) if isinstance(item, dict))
        model_counter.update(model_types)
        content_counter.update(content_types)
        pages_summary.append(
            {
                "page": index,
                "preproc_types": dict(preproc_types),
                "nested_types": dict(nested_types),
                "model_types": dict(model_types),
                "content_types": dict(content_types),
            }
        )
    # Unknown layout types are now rendered through a generic fallback, so audit
    # should list them as covered instead of silently assuming a fixed whitelist.
    used_preproc = set(preproc_counter)
    used_nested = set(nested_counter)
    used_model = set(model_counter)
    return {
        "layout_preview_version": LAYOUT_PREVIEW_VERSION,
        "files": {
            "layout": str(bundle.get("layout_path") or ""),
            "model": str(bundle.get("model_path") or ""),
            "content": str(bundle.get("content_path") or ""),
        },
        "totals": {
            "preproc_types": dict(preproc_counter),
            "nested_types": dict(nested_counter),
            "model_types": dict(model_counter),
            "content_types": dict(content_counter),
        },
        "coverage": {
            "preproc_types_used_by_renderer": sorted(used_preproc),
            "preproc_types_present_but_not_rendered": [],
            "nested_types_used_by_renderer": sorted(used_nested),
            "nested_types_present_but_not_rendered": [],
            "model_types_used_by_renderer": sorted(used_model),
            "model_types_present_but_not_rendered": sorted(set(model_counter) - used_model),
        },
        "pages": pages_summary,
    }


def layout_spans_to_html(spans) -> str:
    if not isinstance(spans, list):
        return ""
    parts: list[str] = []
    for fragment in spans:
        if not isinstance(fragment, dict):
            continue
        fragment_type = str(fragment.get("type") or "").lower()
        content = str(fragment.get("content") or "")
        if not content:
            continue
        if fragment_type in {"equation_inline", "inline_equation"}:
            parts.append(tex_inline_to_html(content, display=False))
        elif fragment_type in {"equation_block", "block_equation"}:
            parts.append(tex_inline_to_html(content, display=True))
        else:
            parts.append(safe_layout_text_to_html(content))
    return "".join(parts)


def safe_layout_text_to_html(text: str) -> str:
    escaped = html.escape(str(text or "")).replace("\n", "<br>")
    escaped = re.sub(r"&amp;lt;(\/?)(sup|sub|br)&amp;gt;", r"&lt;\1\2&gt;", escaped, flags=re.I)
    escaped = re.sub(r"&amp;lt;(br)\s*/&amp;gt;", r"&lt;\1/&gt;", escaped, flags=re.I)
    for tag in ("sup", "sub", "br"):
        escaped = re.sub(rf"&lt;{tag}&gt;", f"<{tag}>", escaped, flags=re.I)
        escaped = re.sub(rf"&lt;/{tag}&gt;", f"</{tag}>", escaped, flags=re.I)
        escaped = re.sub(rf"&lt;{tag}\s*/&gt;", f"<{tag}>", escaped, flags=re.I)
    return escaped


def layout_lines_to_html(lines) -> str:
    if not isinstance(lines, list):
        return ""
    rendered_lines: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        rendered_lines.append(layout_spans_to_html(line.get("spans")))
    return "<br>".join(part for part in rendered_lines if part)


def layout_lines_to_reflow_html(lines) -> str:
    if not isinstance(lines, list):
        return ""
    rendered_lines: list[str] = []
    plain_lines: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        fragment_html = layout_spans_to_html(line.get("spans")).strip()
        if not fragment_html:
            continue
        rendered_lines.append(fragment_html)
        plain_lines.append(body_text_from_html(fragment_html))
    if not rendered_lines:
        return ""
    parts: list[str] = [rendered_lines[0]]
    for index, fragment_html in enumerate(rendered_lines[1:], start=1):
        previous_plain = plain_lines[index - 1].rstrip()
        current_plain = plain_lines[index].lstrip()
        if previous_plain.endswith(("-", "−", "–")):
            if current_plain[:1].islower():
                parts[-1] = re.sub(r"[-−–](\s*(?:</(?:span|sup|sub|em|strong|i|b)>)*\s*)$", r"\1", parts[-1])
            separator = ""
        else:
            separator = " "
        parts.append(separator)
        parts.append(fragment_html)
    return "".join(parts)


def is_symbol_glossary_block(block: dict) -> bool:
    """Whether a text block is a dense ``symbol → definition`` glossary.

    MinerU commonly labels nomenclature panels as ordinary text.  Do not use
    lexical clues such as "Nomenclature": the reliable signal is the repeated
    geometry of a short leading formula and a consistently aligned definition.
    The deliberately high thresholds keep normal formula-heavy prose on the
    established reflow path.
    """
    if not isinstance(block, dict) or str(block.get("type") or "").lower() != "text":
        return False
    if block.get("_layout_symbol_glossary") is True:
        return True
    lines = [line for line in (block.get("lines") or []) if isinstance(line, dict)]
    if len(lines) < 8:
        return False
    matched_rows: list[tuple[float, float]] = []
    nonempty_rows = 0
    for line in lines:
        spans = [span for span in (line.get("spans") or []) if isinstance(span, dict) and str(span.get("content") or "").strip()]
        if not spans:
            continue
        nonempty_rows += 1
        first = spans[0]
        first_type = str(first.get("type") or "").lower()
        first_bbox = first.get("bbox")
        if "equation" not in first_type and "formula" not in first_type:
            continue
        if not isinstance(first_bbox, list) or len(first_bbox) < 4 or len(str(first.get("content") or "").strip()) > 64:
            continue
        definition_bbox = None
        for span in spans[1:]:
            if str(span.get("type") or "").lower() != "text":
                continue
            candidate_bbox = span.get("bbox")
            if isinstance(candidate_bbox, list) and len(candidate_bbox) >= 4:
                definition_bbox = candidate_bbox
                break
        if definition_bbox is None or float(definition_bbox[0]) - float(first_bbox[0]) < 16.0:
            continue
        matched_rows.append((float(first_bbox[0]), float(definition_bbox[0])))
    if nonempty_rows < 8 or len(matched_rows) * 5 < nonempty_rows * 4:
        return False
    symbol_lefts = [row[0] for row in matched_rows]
    definition_lefts = [row[1] for row in matched_rows]
    return max(symbol_lefts) - min(symbol_lefts) <= 14.0 and max(definition_lefts) - min(definition_lefts) <= 20.0


# MinerU does not currently expose a dedicated ``toc`` block type.  In many
# manuals it emits the entire contents page as one ``text`` span with embedded
# newlines, so the normal prose reflow path cannot retain its row structure.
TOC_ENTRY_RE = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)*\.?)\s+(?P<title>.+?)\s*"
    r"(?:\.{2,}|…{2,}|·{2,}|-{3,})\s*(?P<page>\d+|[ivxlcdm]+)\s*$",
    flags=re.IGNORECASE,
)


def layout_logical_lines(lines) -> list[str]:
    """Return newline-delimited logical rows, including rows embedded in spans."""
    if not isinstance(lines, list):
        return []
    logical_lines: list[str] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        fragments = [
            str(span.get("content") or "")
            for span in (line.get("spans") or [])
            if isinstance(span, dict) and str(span.get("content") or "")
        ]
        if not fragments:
            continue
        # A parser line may itself contain all visual rows of a contents page.
        logical_lines.extend("".join(fragments).replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    return logical_lines


def layout_visual_line_count(lines) -> int:
    """Count visual source rows even when MinerU embeds newlines in one span."""
    parser_line_count = len([line for line in (lines or []) if isinstance(line, dict)]) if isinstance(lines, list) else 0
    logical_line_count = len([line for line in layout_logical_lines(lines) if str(line or "").strip()])
    return max(1, parser_line_count, logical_line_count)


def parse_toc_rows(lines) -> list[dict] | None:
    """Recognize a dense numbered contents list without relying on MinerU type."""
    logical_lines = layout_logical_lines(lines)
    entries: list[dict] = []
    nonblank = 0
    for raw_line in logical_lines:
        text = re.sub(r"\s+", " ", raw_line or "").strip()
        if not text:
            entries.append({"gap": True})
            continue
        nonblank += 1
        match = TOC_ENTRY_RE.match(text)
        if not match:
            entries.append({"text": text})
            continue
        number = match.group("number")
        entries.append(
            {
                "number": number,
                "title": match.group("title").strip(),
                "page": match.group("page"),
                "level": max(0, len([part for part in number.rstrip(".").split(".") if part]) - 1),
            }
        )
    matched = sum(1 for entry in entries if entry.get("page"))
    # Requiring several leader/page pairs makes this conservative: an ordinary
    # numbered list is never promoted merely because it has a few dotted lines.
    if matched < 6 or matched / max(1, nonblank) < 0.70:
        return None
    return entries


def render_toc_rows(rows: list[dict]) -> str:
    """Render a parsed contents list with stable leaders and page alignment."""
    parts: list[str] = []
    for row in rows:
        if row.get("gap"):
            parts.append('<div class="toc-gap" aria-hidden="true"></div>')
            continue
        if not row.get("page"):
            parts.append(f'<div class="toc-unparsed">{html.escape(str(row.get("text") or ""))}</div>')
            continue
        level = int(row.get("level") or 0)
        number = html.escape(str(row.get("number") or ""))
        title = html.escape(str(row.get("title") or ""))
        page = html.escape(str(row.get("page") or ""))
        parts.append(
            f'<div class="toc-row toc-level-{level}" style="--toc-level:{level}">'
            f'<span class="toc-label"><span class="toc-number">{number}</span> '
            f'<span class="toc-title">{title}</span></span><span class="toc-leader" aria-hidden="true"></span>'
            f'<span class="toc-page">{page}</span></div>'
        )
    return "".join(parts)


def layout_line_debug_overlays(lines, block_bbox: list[float] | None = None) -> str:
    if not isinstance(lines, list):
        return ""
    parts: list[str] = []
    block_left = float(block_bbox[0]) if isinstance(block_bbox, list) and len(block_bbox) >= 4 else 0.0
    block_top = float(block_bbox[1]) if isinstance(block_bbox, list) and len(block_bbox) >= 4 else 0.0
    for line in lines:
        if not isinstance(line, dict):
            continue
        bbox = line.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            span_boxes = [
                span.get("bbox")
                for span in (line.get("spans") or [])
                if isinstance(span, dict) and isinstance(span.get("bbox"), list) and len(span.get("bbox")) >= 4
            ]
            if not span_boxes:
                continue
            bbox = bbox_union(span_boxes)
        left = float(bbox[0]) - block_left
        top = float(bbox[1]) - block_top
        width = max(1.0, float(bbox[2]) - float(bbox[0]))
        height = max(1.0, float(bbox[3]) - float(bbox[1]))
        parts.append(
            f"""<span class="layout-line-debug-box" style="left:{left:.2f}px;top:{top:.2f}px;width:{width:.2f}px;height:{height:.2f}px;"></span>"""
        )
    return "".join(parts)


def normalize_layout_html_snippet(html_text: str) -> str:
    snippet = str(html_text or "")
    if not snippet:
        return ""
    snippet = re.sub(r"<eq>(.*?)</eq>", lambda m: tex_inline_to_html(m.group(1), display=False), snippet, flags=re.DOTALL)
    return snippet


def model_bbox_to_page_bbox(bbox: list[float], page_width: float, page_height: float) -> list[float]:
    return [
        float(bbox[0]) * page_width,
        float(bbox[1]) * page_height,
        float(bbox[2]) * page_width,
        float(bbox[3]) * page_height,
    ]


def layout_css_class_for_type(block_type: str) -> str:
    safe_type = re.sub(r"[^a-z0-9_-]+", "-", str(block_type or "").lower())
    return f"layout-block type-{safe_type}"


def estimate_layout_font_size(block_type: str, bbox: list[float], plain_text: str) -> float | None:
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    if not text:
        return None
    width = max(18.0, float(bbox[2]) - float(bbox[0]))
    height = max(10.0, float(bbox[3]) - float(bbox[1]))
    length = max(1, len(text))
    base = ((width * height) / max(1.0, length * 0.62)) ** 0.5
    kind = str(block_type or "").lower()
    multiplier = {
        "title": 0.9,
        "text": 0.94,
        "header": 0.92,
        "page_header": 0.92,
        "footer": 0.9,
        "page_footer": 0.9,
        "page_number": 0.88,
        "ref_text": 0.98,
        "table_caption": 0.95,
        "chart_caption": 0.95,
        "image_caption": 0.95,
    }.get(kind, 0.92)
    min_size, max_size = {
        "title": (9.5, 13.0),
        "text": (7.6, 11.0),
        "header": (7.8, 11.0),
        "page_header": (7.8, 11.0),
        "footer": (7.2, 10.0),
        "page_footer": (7.2, 10.0),
        "page_number": (7.2, 10.0),
        "ref_text": (7.0, 9.4),
        "table_caption": (7.0, 10.0),
        "chart_caption": (7.0, 10.0),
        "image_caption": (7.0, 10.0),
    }.get(kind, (7.0, 10.0))
    return max(min_size, min(max_size, base * multiplier))


def fixed_layout_font_size(block_type: str) -> float | None:
    kind = str(block_type or "").lower()
    return {
        "table_caption": 7.6,
        "table_footnote": 7.2,
        "chart_caption": 7.6,
        "image_caption": 7.6,
        "image_footnote": 7.2,
        "text": 7.6,
    }.get(kind)


def render_layout_positioned_block(
    bbox: list[float],
    block_type: str,
    body_html: str,
    page_width: float,
    page_height: float,
    font_size: float | None = None,
    extra_class: str = "",
    data_attrs: dict[str, str] | None = None,
) -> str:
    left = float(bbox[0])
    top = float(bbox[1])
    width = max(4.0, float(bbox[2]) - float(bbox[0]))
    height = max(4.0, float(bbox[3]) - float(bbox[1]))
    font_style = f"font-size:{font_size:.2f}px;" if font_size else ""
    classes = f"{layout_css_class_for_type(block_type)} {extra_class}".strip()
    block_kind = str(block_type or "").lower()
    merged_attrs = dict(data_attrs or {})
    code_block_style = ""
    if block_kind in {"code", "code_body"}:
        # Code blocks keep their source bbox, but translated text may wrap to
        # more lines than the original.  The browser runtime can then reduce
        # this block's own line-height/font-size without affecting neighbours.
        code_block_style = "line-height:1.180;"
        merged_attrs.setdefault("base-font", f"{(font_size or 10.0):.2f}")
        merged_attrs.setdefault("line-ratio", "1.180")
        merged_attrs.setdefault("page-height", f"{page_height:.2f}")
        merged_attrs.setdefault("fit-label", "")
    if block_kind in {"table_caption", "table_footnote", "chart_caption", "image_caption", "image_footnote"}:
        merged_attrs.setdefault("block-kind", block_kind)
        merged_attrs.setdefault("base-font", f"{(font_size or (7.2 if block_kind == 'image_footnote' else 7.6)):.2f}")
        merged_attrs.setdefault("line-ratio", "1.200")
        merged_attrs.setdefault("page-height", f"{page_height:.2f}")
        merged_attrs.setdefault("fit-band-ratio", "0.120")
        merged_attrs.setdefault("fit-label", "")
    attrs = ""
    if merged_attrs:
        attrs = " " + " ".join(
            f"""data-{html.escape(str(key), quote=True)}="{html.escape(str(value), quote=True)}" """
            for key, value in merged_attrs.items()
        ).strip()
    return (
        f"""<div class="{classes}"{attrs} """
        f"""style="left:{left:.2f}px;top:{top:.2f}px;width:{width:.2f}px;height:{height:.2f}px;"""
        f"""{code_block_style}"""
        f"""{font_style}--page-width:{page_width:.2f}px;--page-height:{page_height:.2f}px;">{body_html}</div>"""
    )


def caption_like_block_type(block_type: str) -> bool:
    kind = str(block_type or "").lower()
    return kind in {"title", "table_caption", "table_footnote", "chart_caption", "image_caption", "image_footnote"}


def narrow_block_type_can_expand(block_type: str) -> bool:
    kind = str(block_type or "").lower()
    return kind not in {"table_body", "chart_body", "image_body", "interline_equation"}


def expand_bbox_to_column_right(
    bbox: list[float],
    block_type: str,
    page_width: float,
    column_rights: dict[str, float] | None = None,
) -> list[float]:
    if not column_rights:
        return bbox
    kind = str(block_type or "").lower()
    expanded = [float(value) for value in bbox[:4]]
    side = stream_side_for_bbox(expanded, page_width)
    target_right = column_rights.get(side)
    target_left = column_rights.get(f"{side}_left")
    if target_right is None and side == "full":
        if kind == "title" and "left_left" in column_rights and "right" in column_rights:
            target_left = column_rights.get("left_left")
            target_right = column_rights.get("right")
        else:
            center = (expanded[0] + expanded[2]) / 2.0
            chosen_side = "left" if center < page_width / 2.0 else "right"
            target_right = column_rights.get(chosen_side)
            target_left = column_rights.get(f"{chosen_side}_left")
    if target_right is None:
        return bbox
    target_width = max(1.0, float(target_right) - float(target_left or expanded[0]))
    is_narrow = (expanded[2] - expanded[0]) < target_width / 5.0
    should_expand = caption_like_block_type(block_type) or (is_narrow and narrow_block_type_can_expand(block_type))
    if not should_expand:
        return bbox
    if is_narrow and not caption_like_block_type(block_type):
        margin_space = max(0.0, float(page_width) - float(target_right))
        if expanded[0] <= float(target_right) + margin_space / 2.0:
            expanded[2] = max(expanded[2], float(page_width) - 4.0)
        return expanded
    if kind == "title" and target_left is not None:
        expanded[0] = min(expanded[0], max(0.0, float(target_left)))
    expanded[2] = max(expanded[2], min(float(page_width) - 12.0, float(target_right)))
    return expanded


def layout_block_image_or_table_html(block: dict, block_type: str, asset_dir: Path) -> str:
    spans: list[dict] = []
    for line in block.get("lines") or []:
        if isinstance(line, dict):
            spans.extend(span for span in (line.get("spans") or []) if isinstance(span, dict))
    span = spans[0] if spans else {}
    image_name = str(span.get("image_path") or "")
    image_path = asset_dir / "images" / image_name if image_name else None
    if block_type == "interline_equation" and span.get("content"):
        return equation_display_html(str(span.get("content") or ""))
    if block_type in {"code", "code_body"}:
        code_lines: list[str] = []

        def collect_code_text(node: dict) -> None:
            for line in node.get("lines") or []:
                if not isinstance(line, dict):
                    continue
                line_text = "".join(
                    str(item.get("content") or "")
                    for item in (line.get("spans") or [])
                    if isinstance(item, dict)
                )
                if line_text:
                    code_lines.append(line_text)
            for child in node.get("blocks") or []:
                if isinstance(child, dict):
                    collect_code_text(child)

        collect_code_text(block)
        code_text = "\n".join(code_lines).strip("\n")
        if code_text:
            language = html.escape(str(block.get("guess_lang") or "text"))
            return f"""<pre class="layout-code" data-code-language="{language}"><code>{html.escape(code_text)}</code></pre>"""
    if image_path is not None and image_path.is_file():
        media_class = "layout-equation-media" if block_type == "interline_equation" else "layout-media"
        return (
            f"""<img class="{media_class}" src="{image_path.resolve().as_uri()}" """
            f"""alt="{html.escape(block_type)}" loading="lazy" decoding="async">"""
        )
    if block_type == "table_body" and span.get("html"):
        return f"""<div class="layout-table-wrap">{normalize_layout_html_snippet(str(span.get("html") or ""))}</div>"""
    if block_type == "chart_body" and span.get("content"):
        return f"""<pre class="layout-chart-data">{html.escape(str(span.get("content") or ""))}</pre>"""
    return ""


def render_layout_generic_block(
    block: dict,
    asset_dir: Path,
    page_width: float,
    page_height: float,
    column_rights: dict[str, float] | None = None,
) -> str:
    bbox = block.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return ""
    block_type = str(block.get("type") or "unknown").lower()
    body_html = layout_block_image_or_table_html(block, block_type, asset_dir)
    if not body_html:
        body_html = layout_lines_to_html(block.get("lines"))
        if body_html:
            body_html += layout_line_debug_overlays(layout_debug_lines_for_block(block), bbox)
    if not body_html:
        child_parts = render_layout_block_children(block, asset_dir, page_width, page_height, column_rights)
        return "".join(child_parts)
    plain_text = re.sub(r"<[^>]+>", "", body_html)
    font_size = fixed_layout_font_size(block_type) or estimate_layout_font_size(block_type, bbox, plain_text)
    draw_bbox = expand_bbox_to_column_right(bbox, block_type, page_width, column_rights)
    return render_layout_positioned_block(draw_bbox, block_type, body_html, page_width, page_height, font_size)


def render_layout_block_children(
    block: dict,
    asset_dir: Path,
    page_width: float,
    page_height: float,
    column_rights: dict[str, float] | None = None,
) -> list[str]:
    rendered: list[str] = []
    for child in block.get("blocks") or []:
        if not isinstance(child, dict):
            continue
        child_type = str(child.get("type") or "").lower()
        bbox = child.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        body_html = ""
        if child_type in {"table_caption", "table_footnote", "chart_caption", "image_caption", "image_footnote"}:
            body_html = layout_lines_to_html(child.get("lines"))
            if body_html:
                body_html += layout_line_debug_overlays(layout_debug_lines_for_block(child), bbox)
            font_size = fixed_layout_font_size(child_type) or estimate_layout_font_size(child_type, bbox, re.sub(r"<[^>]+>", "", body_html))
        elif child_type in {"table_body", "chart_body", "image_body"}:
            body_html = layout_block_image_or_table_html(child, child_type, asset_dir)
            font_size = None
        if body_html:
            draw_bbox = expand_bbox_to_column_right(bbox, child_type, page_width, column_rights)
            rendered.append(render_layout_positioned_block(draw_bbox, child_type, body_html, page_width, page_height, font_size))
        else:
            fallback = render_layout_generic_block(child, asset_dir, page_width, page_height, column_rights)
            if fallback:
                rendered.append(fallback)
    return rendered


def render_layout_equation_block(
    block: dict,
    asset_dir: Path,
    page_width: float,
    page_height: float,
    column_rights: dict | None = None,
) -> str:
    bbox = block.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return ""
    spans: list[dict] = []
    for line in block.get("lines") or []:
        if isinstance(line, dict):
            spans.extend(span for span in (line.get("spans") or []) if isinstance(span, dict))
    image_name = ""
    content = ""
    for span in spans:
        image_name = image_name or str(span.get("image_path") or "")
        content = content or str(span.get("content") or "")
    image_path = asset_dir / "images" / image_name if image_name else Path()
    _, number = split_tex_equation_tag(content) if content else ("", "")
    number_right = equation_number_right_for_bbox(bbox, page_width, column_rights) if number else None
    if content:
        body_html = equation_display_html(
            content,
            None if number_right is None else number_right - float(bbox[0]),
        )
    elif image_path and image_path.exists():
        body_html = f"""<img class="layout-equation-media" src="{image_path.resolve().as_uri()}" alt="equation">"""
    else:
        body_html = layout_lines_to_html(block.get("lines"))
        if body_html:
            body_html += layout_line_debug_overlays(layout_debug_lines_for_block(block), bbox)
    if not body_html:
        return ""
    return render_layout_positioned_block(bbox, "interline_equation", body_html, page_width, page_height)


def render_layout_reference_block(block: dict, page_width: float, page_height: float) -> str:
    bbox = block.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return ""
    body_html = layout_lines_to_html(block.get("lines"))
    if not body_html:
        return ""
    body_html += layout_line_debug_overlays(layout_debug_lines_for_block(block), bbox)
    font_size = estimate_layout_font_size("ref_text", bbox, re.sub(r"<[^>]+>", "", body_html))
    return render_layout_positioned_block(bbox, "ref_text", body_html, page_width, page_height, font_size)


def render_layout_reference_list(block: dict, page_width: float, page_height: float) -> list[str]:
    rendered: list[str] = []
    for child in block.get("blocks") or []:
        if not isinstance(child, dict):
            continue
        if str(child.get("type") or "").lower() != "ref_text":
            continue
        item_html = render_layout_reference_block(child, page_width, page_height)
        if item_html:
            rendered.append(item_html)
    return rendered


def body_text_from_html(fragment_html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment_html or "")).strip()


def collect_layout_media_carrier_boxes(blocks) -> list[list[float]]:
    boxes: list[list[float]] = []

    def add_box(value: dict) -> None:
        bbox = value.get("bbox")
        if isinstance(bbox, list) and len(bbox) >= 4:
            boxes.append([float(part) for part in bbox[:4]])

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        block_type = str(value.get("type") or "").lower()
        children = [child for child in (value.get("blocks") or []) if isinstance(child, dict)]
        if block_type in {"table_body", "chart_body", "image_body", "interline_equation", "equation"}:
            add_box(value)
        elif block_type in {"table", "chart", "image"} and not children:
            add_box(value)
        for child in children:
            visit(child)

    visit(blocks)
    return boxes


def bbox_covered_by_any(bbox: list[float], boxes: list[list[float]], threshold: float = 0.72) -> bool:
    if not isinstance(bbox, list) or len(bbox) < 4:
        return False
    for box in boxes:
        if bbox_contained_overlap_ratio([float(part) for part in bbox[:4]], box) >= threshold:
            return True
    return False


def layout_role_text(block: dict, fallback: str) -> str:
    return str(block.get("_layout_original_plain_text") or fallback or "")


def layout_original_line_count(block: dict) -> int:
    explicit_count = 0
    value = block.get("_layout_original_line_count")
    try:
        if value is not None:
            explicit_count = int(value)
    except (TypeError, ValueError):
        pass
    return max(explicit_count, layout_visual_line_count(block.get("lines")))


def layout_text_line_group(text: str, original_line_count: int, item_count: int, paragraph_count: int) -> str:
    if item_count > 1 or paragraph_count > 1:
        return "multi"
    if original_line_count > 1:
        return "multi"
    return "single"


def layout_single_line_text_is_axis_symmetric(
    bbox: list[float],
    page_width: float,
    threshold: float = 0.07,
) -> bool:
    # 判断单行 text 框是否关于页面竖向中轴近似对称。
    # 做法：把右侧留白折到左侧后，比较左右留白差异；
    # 差异除以框自身宽度，比例不超过阈值时认为该框是居中型文本框。
    if not isinstance(bbox, list) or len(bbox) < 4:
        return False
    try:
        left = float(bbox[0])
        right = float(bbox[2])
        page_width = float(page_width)
    except (TypeError, ValueError):
        return False
    box_width = max(1.0, right - left)
    folded_right_margin = page_width - right
    symmetry_ratio = abs(left - folded_right_margin) / box_width
    return symmetry_ratio <= threshold


def layout_debug_lines_for_block(block: dict) -> list | None:
    lines = block.get("_layout_debug_lines")
    if isinstance(lines, list):
        return lines
    lines = block.get("_layout_original_lines")
    if isinstance(lines, list):
        return lines
    lines = block.get("lines")
    return lines if isinstance(lines, list) else None


def looks_like_continuation(previous_text: str, current_text: str) -> bool:
    prev = (previous_text or "").strip()
    curr = (current_text or "").strip()
    if not prev or not curr:
        return False
    if prev.endswith((".", "!", "?", ":", ";")):
        return False
    if re.match(r"^[a-z(\[]", curr):
        return True
    if re.match(r"^(and|or|but|with|which|that|where|when|while|whose|who|of|to)\b", curr, flags=re.IGNORECASE):
        return True
    if re.match(r"^[,.)\\]}]", curr):
        return True
    return False


def is_body_text_candidate(plain_text: str, bbox: list[float], page_width: float, page_height: float) -> bool:
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    lowered = text.lower()
    if not text:
        return False
    if re.match(r"^(pacs|keywords?|doi)\s*[:：]", lowered):
        return False
    if re.match(r"^pacs\s*(编号|号|分类号|代码|编码)?\s*[:：]", lowered):
        return False
    if re.match(r"^(关键词|关键字|doi|数字对象标识符)\s*[:：]", lowered):
        return False
    if lowered.startswith("pacs numbers"):
        return False
    if lowered.startswith("published under an exclusive license"):
        return False
    if lowered.startswith("received "):
        return False
    if lowered.startswith("(received "):
        return False
    side = stream_side_for_bbox(bbox, page_width)
    # Full-width text is usually abstract/top matter rather than column body.
    if side == "full":
        return False
    # Strong seed rule: clearly columnar text below the title/abstract region.
    if float(bbox[1]) < page_height * 0.33:
        return False
    return True


def is_layout_metadata_text(plain_text: str) -> bool:
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    lowered = text.lower()
    if not text:
        return False
    return bool(
        re.match(r"^(pacs|keywords?|doi)\s*[:：]", lowered)
        or re.match(r"^pacs\s*(编号|号|分类号|代码|编码)?\s*[:：]", lowered)
        or re.match(r"^(关键词|关键字|数字对象标识符)\s*[:：]", lowered)
        or lowered.startswith("pacs numbers")
        or lowered.startswith("published under an exclusive license")
        or lowered.startswith(("cite as:", "citation:", "submitted:", "accepted:", "published online:", "online publication:"))
        or lowered.startswith(("authors to whom correspondence", "author to whom correspondence"))
        or "all rights reserved" in lowered
        or "elsevier" in lowered
        or "copyright" in lowered
        or text.startswith(("©", "(c)", "Ⓒ"))
    )


def looks_like_body_prose_evidence(plain_text: str, item: dict, page_width: float, page_height: float) -> bool:
    text = re.sub(r"\s+", " ", plain_text or "").strip()
    if not text or is_layout_metadata_text(text):
        return False
    lowered = text.lower()
    if lowered.startswith(
        (
            "articles you may be interested in",
            "citation:",
            "view online:",
            "view table of contents:",
            "published by ",
        )
    ):
        return False
    if re.search(r"https?://|www\.", lowered):
        return False
    bbox = item.get("bbox") or [0, 0, 0, 0]
    if stream_side_for_bbox(bbox, page_width) not in {"left", "right"}:
        return False
    if float(bbox[1]) < page_height * 0.25:
        return False
    original_lines = int(item.get("original_line_count") or 0)
    if original_lines < 2 and bbox_height(bbox) <= page_height * 0.025:
        return False
    latin_words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
    cjk_chars = re.findall(r"[\u3400-\u9fff]", text)
    sentence_marks = len(re.findall(r"[.!?。！？]", text))
    if len(latin_words) >= 32 and len(text) >= 180 and sentence_marks >= 2:
        return True
    if len(cjk_chars) >= 80 and sentence_marks >= 2:
        return True
    return False


def body_column_profiles(
    flow_items: list[dict],
    page_width: float,
    page_height: float,
) -> dict[str, list[tuple[float, float]]]:
    """Return normalized column edges from locally unambiguous body prose."""
    profiles: dict[str, list[tuple[float, float]]] = {}
    for item in flow_items:
        if item.get("kind") != "text" or item.get("from_list"):
            continue
        text = str(item.get("role_text") or item.get("plain_text") or "")
        if not looks_like_body_prose_evidence(text, item, page_width, page_height):
            continue
        bbox = item.get("bbox") or []
        if len(bbox) < 4:
            continue
        column = layout_column_key(item, page_width)
        if column == "full":
            continue
        profiles.setdefault(column, []).append((float(bbox[0]) / page_width, float(bbox[2]) / page_width))
    return profiles


def matches_body_column_profile(
    item: dict,
    page_width: float,
    profiles: dict[str, list[tuple[float, float]]] | None,
) -> bool:
    """Whether a short text block sits in a previously established column."""
    bbox = item.get("bbox") or []
    if len(bbox) < 4 or not profiles:
        return False
    column = layout_column_key(item, page_width)
    if column == "full":
        return False
    candidates = profiles.get(column) or []
    if not candidates:
        return False
    left = float(bbox[0]) / page_width
    right = float(bbox[2]) / page_width
    width = max(0.001, right - left)
    for ref_left, ref_right in candidates:
        ref_width = max(0.001, ref_right - ref_left)
        if abs(left - ref_left) <= 0.10 and right <= ref_right + 0.10 and width >= ref_width * 0.42:
            return True
    return False


def bbox_edge_match_tolerance(anchor_bbox: list[float]) -> float:
    anchor_width = max(1.0, float(anchor_bbox[2]) - float(anchor_bbox[0]))
    return max(18.0, anchor_width * 0.08)


def bbox_matches_column(anchor_bbox: list[float], bbox: list[float]) -> bool:
    tolerance = bbox_edge_match_tolerance(anchor_bbox)
    left_delta = abs(float(bbox[0]) - float(anchor_bbox[0]))
    right_delta = abs(float(bbox[2]) - float(anchor_bbox[2]))
    if left_delta <= tolerance and right_delta <= tolerance:
        return True
    anchor_width = max(1.0, float(anchor_bbox[2]) - float(anchor_bbox[0]))
    width = max(1.0, float(bbox[2]) - float(bbox[0]))
    # A normal paragraph's last/short line often has the same left edge but a
    # much shorter right edge. Treat it as same-column text as long as it does
    # not spill beyond the column and is not a tiny metadata fragment.
    return (
        left_delta <= tolerance
        and float(bbox[2]) <= float(anchor_bbox[2]) + tolerance
        and width >= anchor_width * 0.45
    )


def promote_text_items_to_body(
    flow_items: list[dict],
    page_width: float,
    page_height: float,
    body_context: dict | None = None,
) -> list[dict]:
    promoted: list[dict] = []
    seeds_by_column: dict[str, list[list[float]]] = {}
    body_context = body_context or {}
    has_previous_body = bool(body_context.get("has_previous_body"))
    neighbor_profiles = body_context.get("neighbor_column_profiles") or {}

    page_has_body_prose = any(
        item.get("kind") == "text"
        and not item.get("from_list")
        and looks_like_body_prose_evidence(
            str(item.get("role_text") or item.get("plain_text") or ""),
            item,
            page_width,
            page_height,
        )
        for item in flow_items
    )
    # Figure/equation-heavy pages can contain only short continuation text.
    # A proven preceding body page opens this pass; a later page alone never
    # retroactively promotes author/citation front matter.
    if not page_has_body_prose and not has_previous_body:
        return flow_items[:]

    def body_candidate_for_item(item: dict, text: str, bbox: list[float]) -> bool:
        if is_body_text_candidate(text, bbox, page_width, page_height):
            return True
        # The title/abstract exclusion only makes sense near the front matter.
        # On later pages, column text can legitimately start at the page top.
        page_index = int(item.get("page_index") or 0)
        column = layout_column_key(item, page_width)
        return page_index >= 2 and column != "full" and not is_layout_metadata_text(text)

    def seed_eligible(item: dict, bbox: list[float]) -> bool:
        original_lines = int(item.get("original_line_count") or 0)
        width = bbox_width(bbox)
        height = bbox_height(bbox)
        # Isolated one-line snippets such as metadata, PACS, page links, and
        # "articles you may be interested in" citations must not create a body
        # column by themselves. They may still join an already-detected body
        # column through the edge-matching pass below.
        return original_lines > 1 or height > page_height * 0.035 or width > page_width * 0.48

    for item in flow_items:
        if item.get("kind") != "text":
            continue
        if item.get("symbol_glossary"):
            continue
        if item.get("from_list"):
            continue
        bbox = item.get("bbox") or [0, 0, 0, 0]
        text = str(item.get("role_text") or item.get("plain_text") or "")
        if is_layout_metadata_text(text) or is_early_front_matter_item(item, page_width, page_height, has_previous_body):
            continue
        if body_candidate_for_item(item, text, bbox) and seed_eligible(item, bbox):
            column = layout_column_key(item, page_width)
            if column != "full":
                seeds_by_column.setdefault(column, []).append(bbox)
    for item in flow_items:
        if item.get("kind") != "text":
            promoted.append(item)
            continue
        if item.get("symbol_glossary"):
            promoted.append(item)
            continue
        if item.get("from_list"):
            promoted.append(item)
            continue
        bbox = item.get("bbox") or [0, 0, 0, 0]
        text = str(item.get("role_text") or item.get("plain_text") or "")
        column = layout_column_key(item, page_width)
        if is_layout_metadata_text(text) or is_early_front_matter_item(item, page_width, page_height, has_previous_body):
            promoted.append(item)
            continue
        if body_candidate_for_item(item, text, bbox) and seed_eligible(item, bbox):
            item["debug_role"] = "body_candidate"
            promoted.append(item)
            continue
        if column in seeds_by_column and seeds_by_column[column]:
            if any(bbox_matches_column(anchor_bbox, bbox) for anchor_bbox in seeds_by_column[column]):
                item = dict(item)
                item["debug_role"] = "body_candidate"
                promoted.append(item)
                continue
        # No local long paragraph is required when a preceding body page has
        # already established this column. This recovers short prose around
        # figures/equations without using lexical cues such as "where".
        if has_previous_body and body_candidate_for_item(item, text, bbox) and matches_body_column_profile(item, page_width, neighbor_profiles):
            item = dict(item)
            item["debug_role"] = "body_candidate"
            promoted.append(item)
            continue
        promoted.append(item)
    return promoted


def layout_barrier_boxes(blocks: list[dict]) -> list[list[float]]:
    barrier_boxes: list[list[float]] = []
    queue = list(blocks or [])
    while queue:
        block = queue.pop(0)
        if not isinstance(block, dict):
            continue
        bbox = block.get("bbox")
        if isinstance(bbox, list) and len(bbox) >= 4:
            barrier_boxes.append(bbox[:])
        for child in block.get("blocks") or []:
            if isinstance(child, dict):
                queue.append(child)
    return barrier_boxes


def horizontal_overlap_ratio(a: list[float], b: list[float]) -> float:
    left = max(float(a[0]), float(b[0]))
    right = min(float(a[2]), float(b[2]))
    overlap = max(0.0, right - left)
    base = max(1.0, min(bbox_width(a), bbox_width(b)))
    return overlap / base


def body_merge_blocked_by_barrier(
    merged_bbox: list[float],
    next_bbox: list[float],
    barrier_boxes: list[list[float]],
    blocking_items: list[dict] | None = None,
) -> bool:
    upper_bottom = float(merged_bbox[3])
    lower_top = float(next_bbox[1])
    if lower_top <= upper_bottom:
        return False
    column_bbox = [
        min(float(merged_bbox[0]), float(next_bbox[0])),
        float(merged_bbox[1]),
        max(float(merged_bbox[2]), float(next_bbox[2])),
        float(next_bbox[3]),
    ]
    for barrier_bbox in barrier_boxes:
        if horizontal_overlap_ratio(column_bbox, barrier_bbox) < 0.18:
            continue
        if float(barrier_bbox[1]) < lower_top and float(barrier_bbox[3]) > upper_bottom:
            return True
    for item in blocking_items or []:
        item_bbox = item.get("bbox")
        if not isinstance(item_bbox, list) or len(item_bbox) < 4:
            continue
        if horizontal_overlap_ratio(column_bbox, item_bbox) < 0.18:
            continue
        if float(item_bbox[1]) < lower_top and float(item_bbox[3]) > upper_bottom:
            return True
    return False


def body_merge_creates_barrier_intrusion(
    current_items: list[dict],
    next_bbox: list[float],
    barrier_boxes: list[list[float]],
) -> bool:
    """Reject a merge that would widen otherwise safe text into a barrier.

    MinerU frequently emits narrow prose beside a figure followed by a
    full-width fragment below it.  Taking the union of those boxes makes the
    merged stream cross the figure even though neither source fragment does.
    This is a merge-time geometry error, not a typography problem: keep a
    stream boundary at that transition and preserve the source reading lanes.
    """
    part_boxes = [
        item.get("bbox") for item in current_items
        if isinstance(item.get("bbox"), list) and len(item.get("bbox") or []) >= 4
    ]
    if not part_boxes or len(next_bbox) < 4:
        return False
    merged_bbox = bbox_union([*part_boxes, next_bbox])
    for barrier_bbox in barrier_boxes:
        if not isinstance(barrier_bbox, list) or len(barrier_bbox) < 4:
            continue
        overlap_left = max(float(merged_bbox[0]), float(barrier_bbox[0]))
        overlap_top = max(float(merged_bbox[1]), float(barrier_bbox[1]))
        overlap_right = min(float(merged_bbox[2]), float(barrier_bbox[2]))
        overlap_bottom = min(float(merged_bbox[3]), float(barrier_bbox[3]))
        if overlap_right - overlap_left < 8.0 or overlap_bottom - overlap_top < 3.0:
            continue
        # Only reject newly introduced crossings.  A source text rectangle
        # that already overlaps a barrier needs a separate parser/layout
        # repair and must not make every neighbouring fragment unmergeable.
        source_boxes = [*part_boxes, next_bbox]
        source_intrudes = any(
            min(float(box[2]), float(barrier_bbox[2])) - max(float(box[0]), float(barrier_bbox[0])) >= 8.0
            and min(float(box[3]), float(barrier_bbox[3])) - max(float(box[1]), float(barrier_bbox[1])) >= 3.0
            for box in source_boxes
        )
        if not source_intrudes:
            return True
    return False


def is_single_line_body_item(item: dict) -> bool:
    """Whether a body item came from exactly one source-layout line."""
    try:
        return int(item.get("original_line_count") or 0) == 1
    except (TypeError, ValueError):
        return False


def single_line_body_items_are_adjacent(previous: dict, current: dict) -> bool:
    """Allow a short one-line body fragment to join its direct column neighbor.

    MinerU often gives the last/first sentence around an equation a narrower
    bbox than the adjacent paragraph.  Requiring both right edges to match
    turns that harmless split into a standalone, shallow text box, which then
    becomes the global font-size limiter after translation.  A shared left
    edge and substantial horizontal overlap are enough for this narrowly
    scoped exception; barrier checks remain the authority on whether another
    content type sits between the two items.
    """
    if not (is_single_line_body_item(previous) or is_single_line_body_item(current)):
        return False
    previous_bbox = previous.get("bbox") or []
    current_bbox = current.get("bbox") or []
    if len(previous_bbox) < 4 or len(current_bbox) < 4:
        return False
    vertical_gap = float(current_bbox[1]) - float(previous_bbox[3])
    # "Nearest" means the two source boxes must be physically contiguous.
    # Do not bridge a large blank area merely because MinerU did not label the
    # intervening space as another block.
    max_gap = max(14.0, min(bbox_height(previous_bbox), bbox_height(current_bbox)) * 1.5)
    if vertical_gap > max_gap:
        return False
    tolerance = max(bbox_edge_match_tolerance(previous_bbox), bbox_edge_match_tolerance(current_bbox))
    left_delta = abs(float(previous_bbox[0]) - float(current_bbox[0]))
    right_delta = abs(float(previous_bbox[2]) - float(current_bbox[2]))
    return (
        (left_delta <= tolerance or right_delta <= tolerance)
        and horizontal_overlap_ratio(previous_bbox, current_bbox) >= 0.45
    )


def is_early_front_matter_item(
    item: dict,
    page_width: float,
    page_height: float,
    has_previous_body: bool,
) -> bool:
    """Keep author/affiliation metadata from becoming the first body seed."""
    if has_previous_body:
        return False
    bbox = item.get("bbox") or []
    if len(bbox) < 4 or float(bbox[1]) > page_height * 0.55:
        return False
    original_lines = int(item.get("original_line_count") or 0)
    return original_lines <= 1


def merged_part_indent_px(part: dict, merged_bbox: list[float]) -> float:
    """Preserve a fragment's visual first-line indent after bbox merging.

    Multi-line fragments already carry an indent estimated from their own
    later lines.  A one-line fragment cannot be measured that way: before a
    merge its cropped text position supplied the visual offset, but that
    offset disappears when the fragment is placed in the wider merged bbox.
    Recover it from the first OCR/debug line relative to the merged column.
    """
    explicit_indent = float(part.get("indent_px") or 0.0)
    if explicit_indent > 0.0:
        return explicit_indent
    first_line_left: float | None = None
    debug_lines = [
        line for line in (part.get("debug_lines") or [])
        if isinstance(line, dict) and isinstance(line.get("bbox"), list) and len(line.get("bbox") or []) >= 4
    ]
    if debug_lines:
        first_top = min(float(line["bbox"][1]) for line in debug_lines)
        first_line_boxes = [line["bbox"] for line in debug_lines if abs(float(line["bbox"][1]) - first_top) <= 3.0]
        first_line_left = min(float(line_bbox[0]) for line_bbox in first_line_boxes)
    else:
        part_bbox = part.get("bbox") or []
        if len(part_bbox) >= 4:
            first_line_left = float(part_bbox[0])
    if first_line_left is None:
        return 0.0

    indent = first_line_left - float(merged_bbox[0])
    if indent < 5.0:
        return 0.0
    max_reasonable_indent = max(10.0, bbox_width(merged_bbox) * 0.18)
    return round(min(indent, max_reasonable_indent), 2)


def merged_body_paragraphs(parts: list[dict], merged_bbox: list[float] | None = None) -> list[dict]:
    if merged_bbox is None:
        valid_boxes = [part.get("bbox") for part in parts if isinstance(part.get("bbox"), list) and len(part.get("bbox") or []) >= 4]
        merged_bbox = bbox_union(valid_boxes) if valid_boxes else [0.0, 0.0, 0.0, 0.0]
    paragraphs: list[dict] = []
    html_parts: list[str] = []
    plain_parts: list[str] = []
    current_indent = 0.0

    def flush() -> None:
        nonlocal html_parts, plain_parts, current_indent
        html_text = "".join(html_parts).strip()
        plain_text = "".join(plain_parts).strip()
        if html_text or plain_text:
            paragraphs.append(
                {
                    "html": html_text,
                    "plain_text": plain_text,
                    "indent_px": current_indent,
                }
            )
        html_parts = []
        plain_parts = []
        current_indent = 0.0

    for part in parts:
        fragment_html = str(part.get("html") or "").strip()
        plain_text = str(part.get("plain_text") or "").strip()
        if not fragment_html and not plain_text:
            continue
        previous_plain = "".join(plain_parts).strip()
        should_join = bool(previous_plain) and (
            previous_plain.rstrip().endswith(("-", "−", "–"))
            or looks_like_continuation(previous_plain, plain_text)
        )
        if html_parts and not should_join:
            flush()
        if not html_parts:
            current_indent = merged_part_indent_px(part, merged_bbox)
        if html_parts and not previous_plain.rstrip().endswith(("-", "−", "–")):
            html_parts.append(" ")
        if plain_parts and not previous_plain.rstrip().endswith(("-", "−", "–")):
            plain_parts.append(" ")
        if fragment_html:
            html_parts.append(fragment_html)
        if plain_text:
            plain_parts.append(plain_text)
    flush()
    return paragraphs


def merged_debug_lines(parts: list[dict]) -> list[dict]:
    lines: list[dict] = []
    for part in parts:
        for line in part.get("debug_lines") or []:
            if isinstance(line, dict):
                lines.append(line)
    return lines


def merge_vertical_body_items(flow_items: list[dict], absolute_blocks: list[dict]) -> list[dict]:
    body_items = [item for item in flow_items if item.get("kind") == "text" and item.get("debug_role") == "body_candidate"]
    other_items = [item for item in flow_items if not (item.get("kind") == "text" and item.get("debug_role") == "body_candidate")]
    merged: list[dict] = []
    barrier_boxes = layout_barrier_boxes(absolute_blocks)
    # A non-body text item is a semantic barrier just like an equation, image,
    # or table.  This keeps the single-line repair below from crossing labels,
    # captions, list entries, or other content types.
    blocking_items = [item for item in other_items if item.get("kind") == "text"]
    item_column = lambda item: str(item.get("column_key") or item.get("side") or "full")
    non_column_body_items = [item for item in body_items if item_column(item) == "full"]
    column_keys = sorted({item_column(item) for item in body_items if item_column(item) != "full"})
    for column_key in column_keys:
        side_items = sorted(
            [item for item in body_items if item_column(item) == column_key],
            key=lambda item: (float((item.get("bbox") or [0, 0, 0, 0])[1]), float((item.get("bbox") or [0, 0, 0, 0])[0])),
        )
        if not side_items:
            continue
        current_items = [side_items[0]]
        for item in side_items[1:]:
            bbox = (item.get("bbox") or [0, 0, 0, 0])[:]
            current_bbox = [
                min(float((part.get("bbox") or [0, 0, 0, 0])[0]) for part in current_items),
                min(float((part.get("bbox") or [0, 0, 0, 0])[1]) for part in current_items),
                max(float((part.get("bbox") or [0, 0, 0, 0])[2]) for part in current_items),
                max(float((part.get("bbox") or [0, 0, 0, 0])[3]) for part in current_items),
            ]
            current_width = max(1.0, bbox_width(current_bbox))
            left_delta = abs(float(bbox[0]) - float(current_bbox[0]))
            right_delta = abs(float(bbox[2]) - float(current_bbox[2]))
            # Neighbor fragments may have different right edges because MinerU
            # crops a short sentence to its ink width.  Same-column adjacency
            # is therefore enough; the merged bbox keeps the larger width.
            tolerance = max(18.0, current_width * 0.10)
            vertical_gap = float(bbox[1]) - float(current_bbox[3])
            max_merge_gap = max(18.0, min(bbox_height(current_bbox), bbox_height(bbox)) * 2.0)
            blocked = body_merge_blocked_by_barrier(current_bbox, bbox, barrier_boxes, blocking_items)
            creates_intrusion = body_merge_creates_barrier_intrusion(current_items, bbox, barrier_boxes)
            previous_item = current_items[-1]
            normal_column_match = vertical_gap <= max_merge_gap and (
                left_delta <= tolerance
                or right_delta <= tolerance
                or horizontal_overlap_ratio(current_bbox, bbox) >= 0.55
            )
            single_line_neighbor_match = single_line_body_items_are_adjacent(previous_item, item)
            if (normal_column_match or single_line_neighbor_match) and not blocked and not creates_intrusion:
                current_items.append(item)
                continue
            merged.append(
                {
                    "kind": "text",
                    "side": current_items[0].get("side"),
                    "column_key": column_key,
                    "bbox": current_bbox[:],
                    "html": "<br><br>".join(str(part.get("html") or "") for part in current_items),
                    "plain_text": "\n\n".join(str(part.get("plain_text") or "") for part in current_items),
                    "original_line_count": sum(int(part.get("original_line_count") or 0) for part in current_items),
                    "indent_px": float(current_items[0].get("indent_px") or 0.0),
                    "page_index": int(current_items[0].get("page_index") or 0),
                    "debug_role": "merged_body",
                    "paragraphs": merged_body_paragraphs(current_items, current_bbox),
                    "debug_lines": merged_debug_lines(current_items),
                }
            )
            current_items = [item]
        current_bbox = [
            min(float((part.get("bbox") or [0, 0, 0, 0])[0]) for part in current_items),
            min(float((part.get("bbox") or [0, 0, 0, 0])[1]) for part in current_items),
            max(float((part.get("bbox") or [0, 0, 0, 0])[2]) for part in current_items),
            max(float((part.get("bbox") or [0, 0, 0, 0])[3]) for part in current_items),
        ]
        merged.append(
            {
                "kind": "text",
                "side": current_items[0].get("side"),
                "column_key": column_key,
                "bbox": current_bbox[:],
                "html": "<br><br>".join(str(part.get("html") or "") for part in current_items),
                "plain_text": "\n\n".join(str(part.get("plain_text") or "") for part in current_items),
                "original_line_count": sum(int(part.get("original_line_count") or 0) for part in current_items),
                "indent_px": float(current_items[0].get("indent_px") or 0.0),
                "page_index": int(current_items[0].get("page_index") or 0),
                "debug_role": "merged_body",
                "paragraphs": merged_body_paragraphs(current_items, current_bbox),
                "debug_lines": merged_debug_lines(current_items),
            }
        )
    merged.extend(non_column_body_items)
    merged.extend(other_items)
    return merged


def equation_barrier_boxes(blocks: list[dict]) -> list[list[float]]:
    """Collect display-equation boxes from nested absolute layout blocks."""
    boxes: list[list[float]] = []
    queue = list(blocks or [])
    while queue:
        block = queue.pop(0)
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        bbox = block.get("bbox")
        if block_type in {"interline_equation", "equation"} and isinstance(bbox, list) and len(bbox) >= 4:
            boxes.append([float(value) for value in bbox[:4]])
        for child in block.get("blocks") or []:
            if isinstance(child, dict):
                queue.append(child)
    return boxes


def mark_equation_dense_body_items(
    flow_items: list[dict],
    absolute_blocks: list[dict],
    page_height: float,
) -> list[dict]:
    """Mark short body fragments tightly sandwiched by display equations.

    They remain ordinary body text with the document-wide shared font; the
    marker only permits a tighter local line ratio if formula spacing demands
    it.
    """
    equations = equation_barrier_boxes(absolute_blocks)
    if not equations:
        return flow_items
    for item in flow_items:
        if item.get("debug_role") not in {"body_candidate", "merged_body"}:
            continue
        bbox = item.get("bbox") or []
        if len(bbox) < 4:
            continue
        line_count = int(item.get("original_line_count") or 0)
        if line_count <= 0:
            line_count = len(item.get("debug_lines") or [])
        if line_count > 4 or bbox_height(bbox) > max(54.0, page_height * 0.07):
            continue
        horizontally_related = [
            equation for equation in equations
            if horizontal_overlap_ratio(bbox, equation) >= 0.35
        ]
        has_upper_equation = any(
            0 <= float(bbox[1]) - float(equation[3]) <= 28.0
            for equation in horizontally_related
        )
        has_lower_equation = any(
            0 <= float(equation[1]) - float(bbox[3]) <= 28.0
            for equation in horizontally_related
        )
        if has_upper_equation and has_lower_equation:
            item["equation_dense"] = True
    return flow_items


def merge_reference_items(flow_items: list[dict]) -> list[dict]:
    ref_items = [item for item in flow_items if item.get("kind") == "ref_text"]
    other_items = [item for item in flow_items if item.get("kind") != "ref_text"]
    merged: list[dict] = []
    column_keys = sorted({str(item.get("column_key") or item.get("side") or "full") for item in ref_items})
    for column_key in column_keys:
        side_items = sorted(
            [item for item in ref_items if str(item.get("column_key") or item.get("side") or "full") == column_key],
            key=lambda item: (float((item.get("bbox") or [0, 0, 0, 0])[1]), float((item.get("bbox") or [0, 0, 0, 0])[0])),
        )
        if not side_items:
            continue
        first_bbox = (side_items[0].get("bbox") or [0, 0, 0, 0])[:]
        merged_bbox = [
            min(float((item.get("bbox") or [0, 0, 0, 0])[0]) for item in side_items),
            min(float((item.get("bbox") or [0, 0, 0, 0])[1]) for item in side_items),
            max(float((item.get("bbox") or [0, 0, 0, 0])[2]) for item in side_items),
            max(float((item.get("bbox") or [0, 0, 0, 0])[3]) for item in side_items),
        ]
        merged.append(
            {
                "kind": "ref_text",
                "side": side_items[0].get("side"),
                "column_key": column_key,
                "bbox": merged_bbox,
                "html": "<br>".join(str(part.get("html") or "") for part in side_items),
                "plain_text": "\n".join(str(part.get("plain_text") or "") for part in side_items),
                "indent_px": 0.0,
                "page_index": int(side_items[0].get("page_index") or 0),
                "debug_role": "merged_reference",
                "debug_lines": merged_debug_lines(side_items),
                # Keep the source records temporarily so the conservative
                # repair below can restore their original reading order.
                "_reference_parts": side_items,
                "paragraphs": [
                    {
                        "html": str(part.get("html") or ""),
                        "plain_text": str(part.get("plain_text") or ""),
                        "indent_px": 0.0,
                    }
                    for part in side_items
                ],
            }
        )

    # A reference entry's bbox is only as wide as its visible glyphs.  On a
    # one-column bibliography, short one-line entries can therefore be put in
    # a different column bucket from their long neighbours.  Rendering both
    # buckets as independent full-height flows overlays the text.  Repair only
    # this unmistakable geometry: same left edge and largely the same vertical
    # span.  Genuine multi-column bibliographies have distinct left edges, and
    # vertically separate reference sections do not meet the overlap rule.
    repaired: list[dict] = []
    pending = sorted(merged, key=lambda item: float((item.get("bbox") or [0, 0, 0, 0])[1]))
    while pending:
        current = pending.pop(0)
        current_bbox = current.get("bbox") or [0, 0, 0, 0]
        compatible_index = None
        for index, candidate in enumerate(pending):
            candidate_bbox = candidate.get("bbox") or [0, 0, 0, 0]
            if int(candidate.get("page_index") or 0) != int(current.get("page_index") or 0):
                continue
            if abs(float(candidate_bbox[0]) - float(current_bbox[0])) > 8.0:
                continue
            overlap = max(
                0.0,
                min(float(current_bbox[3]), float(candidate_bbox[3]))
                - max(float(current_bbox[1]), float(candidate_bbox[1])),
            )
            shorter_height = min(bbox_height(current_bbox), bbox_height(candidate_bbox))
            if shorter_height > 0.0 and overlap / shorter_height >= 0.75:
                compatible_index = index
                break
        if compatible_index is None:
            repaired.append(current)
            continue

        candidate = pending.pop(compatible_index)
        parts = [
            *[part for part in (current.get("_reference_parts") or []) if isinstance(part, dict)],
            *[part for part in (candidate.get("_reference_parts") or []) if isinstance(part, dict)],
        ]
        parts.sort(key=lambda part: (
            float((part.get("bbox") or [0, 0, 0, 0])[1]),
            float((part.get("bbox") or [0, 0, 0, 0])[0]),
        ))
        merged_bbox = bbox_union([part["bbox"] for part in parts])
        pending.insert(
            0,
            {
                "kind": "ref_text",
                "side": current.get("side"),
                "column_key": current.get("column_key"),
                "bbox": merged_bbox,
                "html": "<br>".join(str(part.get("html") or "") for part in parts),
                "plain_text": "\n".join(str(part.get("plain_text") or "") for part in parts),
                "indent_px": 0.0,
                "page_index": int(current.get("page_index") or 0),
                "debug_role": "merged_reference",
                "debug_lines": merged_debug_lines(parts),
                "_reference_parts": parts,
                "paragraphs": [
                    {
                        "html": str(part.get("html") or ""),
                        "plain_text": str(part.get("plain_text") or ""),
                        "indent_px": 0.0,
                    }
                    for part in parts
                ],
            },
        )

    for item in repaired:
        item.pop("_reference_parts", None)
    merged = repaired
    merged.extend(other_items)
    return merged


def bbox_width(bbox: list[float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0]))


def bbox_height(bbox: list[float]) -> float:
    return max(0.0, float(bbox[3]) - float(bbox[1]))


def bbox_center(bbox: list[float]) -> tuple[float, float]:
    return ((float(bbox[0]) + float(bbox[2])) / 2.0, (float(bbox[1]) + float(bbox[3])) / 2.0)


def bbox_union(boxes: list[list[float]]) -> list[float]:
    return [
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    ]


def bbox_vertical_gap(upper: list[float], lower: list[float]) -> float:
    return float(lower[1]) - float(upper[3])


def collect_model_ocr_boxes(model_page, page_width: float, page_height: float) -> list[list[float]]:
    boxes: list[list[float]] = []
    if not isinstance(model_page, list):
        return boxes
    for item in model_page:
        if not isinstance(item, dict) or str(item.get("type") or "").lower() != "ocr_text":
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        boxes.append(model_bbox_to_page_bbox(bbox, page_width, page_height))
    return boxes


def ocr_boxes_in_region(ocr_boxes: list[list[float]], bbox: list[float], padding: float = 2.0) -> list[list[float]]:
    x0 = float(bbox[0]) - padding
    y0 = float(bbox[1]) - padding
    x1 = float(bbox[2]) + padding
    y1 = float(bbox[3]) + padding
    selected: list[list[float]] = []
    for box in ocr_boxes:
        cx, cy = bbox_center(box)
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            selected.append(box)
    return selected


def median_value(values: list[float], default: float) -> float:
    numbers = sorted(float(value) for value in values if value is not None)
    if not numbers:
        return default
    middle = len(numbers) // 2
    if len(numbers) % 2:
        return numbers[middle]
    return (numbers[middle - 1] + numbers[middle]) / 2.0


def refined_text_bbox_from_ocr(bbox: list[float], ocr_boxes: list[list[float]]) -> list[float]:
    hits = ocr_boxes_in_region(ocr_boxes, bbox, padding=1.5)
    if not hits:
        return bbox[:]
    content_box = bbox_union(hits)
    return [
        min(float(bbox[0]), float(content_box[0]) - 1.0),
        float(bbox[1]),
        max(float(bbox[2]), float(content_box[2]) + 1.0),
        min(float(bbox[3]), float(content_box[3]) + 2.0),
    ]


def estimate_first_line_indent(bbox: list[float], ocr_boxes: list[list[float]]) -> float:
    hits = ocr_boxes_in_region(ocr_boxes, bbox, padding=1.5)
    if len(hits) < 3:
        return 0.0
    # One PDF line may be represented by several OCR boxes (text, inline
    # formula and a citation).  Sorting boxes alone can select a formula in
    # the middle of the first line as its "start", producing a giant fake
    # indent.  Treat boxes with the same vertical centre as one visual line
    # and use that line's leftmost box.
    hits = sorted(hits, key=lambda box: (bbox_center(box)[1], float(box[0])))
    first_center = bbox_center(hits[0])[1]
    typical_height = median_value([bbox_height(box) for box in hits], 8.0)
    same_line_tolerance = max(2.5, min(6.0, typical_height * 0.55))
    first_line = [box for box in hits if abs(bbox_center(box)[1] - first_center) <= same_line_tolerance]
    later_lines = [box for box in hits if box not in first_line]
    if not later_lines:
        return 0.0
    first_line_left = min(float(box[0]) for box in first_line)
    remainder_lefts = [float(box[0]) for box in later_lines]
    base_left = median_value(remainder_lefts, float(bbox[0]))
    indent = first_line_left - base_left
    if indent < 5.0:
        return 0.0
    max_reasonable_indent = max(10.0, bbox_width(bbox) * 0.18)
    return round(min(indent, max_reasonable_indent), 2)


def normalized_indent_px(items: list[dict]) -> float:
    indents = [float(item.get("indent_px") or 0.0) for item in items if float(item.get("indent_px") or 0.0) > 0.0]
    if not indents:
        return 0.0
    return round(median_value(indents, 0.0), 2)


def stream_side_for_bbox(bbox: list[float], page_width: float) -> str:
    width = float(bbox[2]) - float(bbox[0])
    center = (float(bbox[0]) + float(bbox[2])) / 2.0
    if width > page_width * 0.55:
        return "full"
    return "left" if center < page_width / 2.0 else "right"


def assign_layout_column_keys(flow_items: list[dict], page_width: float) -> None:
    """Give text streams a stable physical-column key.

    ``left``/``right`` is useful for a simple two-column page, but it is not a
    column identity: on a three-column page the middle column is also ``left``.
    Keep that coarse value for older presentation rules and add a key based on
    the actual left column edge.  A left-edge key deliberately survives a short
    final line, while the later bbox checks still prevent merging text from a
    two-column region with a narrower three-column region on the same page.
    """
    candidates: list[tuple[float, float]] = []
    for item in flow_items:
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        width = bbox_width(bbox)
        if width < page_width * 0.12 or width > page_width * 0.82:
            continue
        candidates.append((float(bbox[0]), width))
    if not candidates:
        for item in flow_items:
            item["column_key"] = str(item.get("side") or "full")
        return

    # PDF coordinates vary a little between blocks in one physical column.
    # The tolerance is capped so adjacent narrow columns never coalesce.
    tolerance = max(8.0, min(20.0, median_value([width for _, width in candidates], page_width * 0.3) * 0.08))
    anchors: list[float] = []
    for left, _ in sorted(candidates):
        if not anchors or abs(left - anchors[-1]) > tolerance:
            anchors.append(left)
        else:
            anchors[-1] = (anchors[-1] + left) / 2.0
    for item in flow_items:
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4 or bbox_width(bbox) > page_width * 0.82:
            item["column_key"] = "full"
            continue
        left = float(bbox[0])
        nearest = min(range(len(anchors)), key=lambda index: abs(left - anchors[index]))
        item["column_key"] = f"column-{nearest}"


def layout_column_key(item: dict, page_width: float) -> str:
    """Read the physical-column key with a safe fallback for older bundles."""
    key = str(item.get("column_key") or "")
    if key:
        return key
    bbox = item.get("bbox") or [0, 0, page_width, 0]
    return stream_side_for_bbox(bbox, page_width)


def streamable_layout_items(page: dict, page_width: float, page_height: float, ocr_boxes: list[list[float]] | None = None) -> tuple[list[dict], list[dict]]:
    flow_items: list[dict] = []
    absolute_blocks: list[dict] = []
    ocr_boxes = ocr_boxes or []
    media_carrier_boxes = collect_layout_media_carrier_boxes(page.get("preproc_blocks") or [])
    for block in page.get("preproc_blocks") or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        bbox = block.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        if block_type in {"table", "chart", "image", "title"}:
            absolute_blocks.append(block)
            continue
        if block_type == "interline_equation":
            absolute_blocks.append(block)
            continue
        if block_type == "text":
            if bbox_covered_by_any(bbox, media_carrier_boxes):
                continue
            symbol_glossary = is_symbol_glossary_block(block)
            # Preserve the entries of a recognised nomenclature panel.  All
            # other text remains on the established prose-reflow path.
            body_html = layout_lines_to_html(block.get("lines")) if symbol_glossary else layout_lines_to_reflow_html(block.get("lines"))
            if not body_html:
                continue
            side = stream_side_for_bbox(bbox, page_width)
            plain_text = body_text_from_html(body_html)
            role_text = layout_role_text(block, plain_text)
            toc_rows = parse_toc_rows(block.get("lines"))
            flow_items.append(
                {
                    "kind": "text",
                    "bbox": bbox[:],
                    "html": body_html,
                    "plain_text": plain_text,
                    "role_text": role_text,
                    "debug_lines": layout_debug_lines_for_block(block),
                    # MinerU can put dozens of directory rows into one span.
                    # Count logical rows so it never takes the single-line
                    # centering path even before the dedicated renderer runs.
                    "original_line_count": len(layout_logical_lines(block.get("lines"))) if toc_rows else layout_original_line_count(block),
                    "font_estimate": estimate_layout_font_size("text", bbox, plain_text) or 8.5,
                    "side": side,
                    "indent_px": estimate_first_line_indent(bbox, ocr_boxes),
                    "page_index": int(page.get("page_idx") or 0),
                    "debug_role": "toc" if toc_rows else "text",
                    "toc_rows": toc_rows,
                    "symbol_glossary": symbol_glossary,
                }
            )
            continue
        if block_type == "ref_text":
            if bbox_covered_by_any(bbox, media_carrier_boxes):
                continue
            body_html = layout_lines_to_reflow_html(block.get("lines"))
            if not body_html:
                continue
            plain_text = body_text_from_html(body_html)
            flow_items.append(
                {
                    "kind": "ref_text",
                    "bbox": bbox[:],
                    "html": body_html,
                    "plain_text": plain_text,
                    "role_text": layout_role_text(block, plain_text),
                    "debug_lines": layout_debug_lines_for_block(block),
                    "original_line_count": layout_original_line_count(block),
                    "font_estimate": estimate_layout_font_size("ref_text", bbox, plain_text) or 8.2,
                    "side": stream_side_for_bbox(bbox, page_width),
                    "indent_px": 0.0,
                    "page_index": int(page.get("page_idx") or 0),
                    "debug_role": "reference",
                }
            )
            continue
        if block_type == "list":
            child_items: list[dict] = []
            text_child_items: list[dict] = []
            for child in block.get("blocks") or []:
                if not isinstance(child, dict):
                    continue
                child_type = str(child.get("type") or "").lower()
                child_bbox = child.get("bbox")
                body_html = layout_lines_to_html(child.get("lines"))
                if not isinstance(child_bbox, list) or len(child_bbox) < 4 or not body_html:
                    continue
                if bbox_covered_by_any(child_bbox, media_carrier_boxes):
                    continue
                if child_type == "ref_text":
                    body_html = layout_lines_to_reflow_html(child.get("lines"))
                    plain_text = body_text_from_html(body_html)
                    child_items.append(
                        {
                            "kind": "ref_text",
                            "bbox": child_bbox[:],
                            "html": body_html,
                            "plain_text": plain_text,
                            "role_text": layout_role_text(child, plain_text),
                            "debug_lines": layout_debug_lines_for_block(child),
                            "original_line_count": layout_original_line_count(child),
                            "font_estimate": estimate_layout_font_size("ref_text", child_bbox, body_text_from_html(body_html)) or 8.2,
                            "side": stream_side_for_bbox(child_bbox, page_width),
                            "indent_px": 0.0,
                            "page_index": int(page.get("page_idx") or 0),
                            "debug_role": "reference",
                            "from_list": True,
                        }
                    )
                elif child_type == "text":
                    body_html = layout_lines_to_reflow_html(child.get("lines"))
                    plain_text = body_text_from_html(body_html)
                    text_child_items.append(
                        {
                            "kind": "text",
                            "bbox": child_bbox[:],
                            "html": body_html,
                            "plain_text": plain_text,
                            "role_text": layout_role_text(child, plain_text),
                            "debug_lines": layout_debug_lines_for_block(child),
                            "original_line_count": layout_original_line_count(child),
                            "font_estimate": estimate_layout_font_size("text", child_bbox, plain_text) or 8.5,
                            "side": stream_side_for_bbox(child_bbox, page_width),
                            "indent_px": estimate_first_line_indent(child_bbox, ocr_boxes),
                            "page_index": int(page.get("page_idx") or 0),
                            "debug_role": "text",
                            "from_list": True,
                        }
                    )
            if child_items:
                flow_items.extend(child_items)
            if text_child_items:
                flow_items.extend(text_child_items)
            if child_items or text_child_items:
                continue
            else:
                absolute_blocks.append(block)
            continue
        absolute_blocks.append(block)
    assign_layout_column_keys(flow_items, page_width)
    return flow_items, absolute_blocks


def group_flow_streams(flow_items: list[dict], absolute_blocks: list[dict], page_width: float) -> list[dict]:
    if not flow_items:
        return []
    media_boxes: list[list[float]] = []
    for block in absolute_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        bbox = block.get("bbox")
        if block_type in {"table", "chart", "image"} and isinstance(bbox, list) and len(bbox) >= 4:
            media_boxes.append(bbox)
    streams: list[dict] = []
    for item in sorted(flow_items, key=lambda value: (value["side"], value["bbox"][1], value["bbox"][0])):
        bbox = item["bbox"]
        if not streams:
            streams.append({"side": item["side"], "items": [item], "bbox": bbox[:], "page_index": int(item.get("page_index") or 0)})
            continue
        current = streams[-1]
        same_side = current["side"] == item["side"]
        prev_bbox = current["bbox"]
        gap = float(bbox[1]) - float(prev_bbox[3])
        overlap = min(float(prev_bbox[2]), float(bbox[2])) - max(float(prev_bbox[0]), float(bbox[0]))
        min_width = max(1.0, min(float(prev_bbox[2]) - float(prev_bbox[0]), float(bbox[2]) - float(bbox[0])))
        overlap_ratio = overlap / min_width
        prev_items = current["items"]
        prev_kind = str(prev_items[-1].get("kind") or "")
        same_kind = prev_kind == str(item.get("kind") or "")
        prev_role = str(prev_items[-1].get("debug_role") or "")
        item_role = str(item.get("debug_role") or "")
        # Do not let a metadata/text stream absorb a body stream merely
        # because their boxes have matching column edges and a small gap.
        # Besides losing the role used by later width logic, that union can
        # inherit a wide license/header bbox into the reading column.
        same_layout_role = prev_role == item_role
        continuation = looks_like_continuation(
            str(prev_items[-1].get("role_text") or prev_items[-1].get("plain_text") or ""),
            str(item.get("role_text") or item.get("plain_text") or ""),
        )
        has_media_barrier = False
        side_center = page_width / 2.0
        for media_bbox in media_boxes:
            media_cx, _ = bbox_center(media_bbox)
            if item["side"] == "left" and media_cx >= side_center:
                continue
            if item["side"] == "right" and media_cx < side_center:
                continue
            if float(media_bbox[1]) < float(bbox[1]) and float(media_bbox[3]) > float(prev_bbox[3]):
                has_media_barrier = True
                break
        should_merge = False
        if same_side and same_kind and same_layout_role and not has_media_barrier:
            max_gap = 18 if prev_kind == "ref_text" else 54
            if overlap_ratio >= 0.4 and gap <= max_gap:
                should_merge = True
            elif prev_kind != "ref_text" and continuation and gap <= 20:
                should_merge = True
        if should_merge:
            current["items"].append(item)
            current["bbox"] = [
                min(float(prev_bbox[0]), float(bbox[0])),
                min(float(prev_bbox[1]), float(bbox[1])),
                max(float(prev_bbox[2]), float(bbox[2])),
                max(float(prev_bbox[3]), float(bbox[3])),
            ]
        else:
            streams.append({"side": item["side"], "items": [item], "bbox": bbox[:], "page_index": int(item.get("page_index") or 0)})
    return streams


def column_right_edges_from_streams(streams: list[dict], page_width: float) -> dict:
    # ``_body_boxes`` remains the authority for text fitting.  Formula-number
    # gutters also need the wider set below: a valid single-column paragraph
    # can deliberately stay out of body fitting (front matter, a short
    # derivation transition, etc.) while still being decisive evidence of the
    # page's physical reading lane.
    edges: dict = {"_body_boxes": [], "_text_boxes": []}
    for stream in streams:
        bbox = stream.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        items = stream.get("items") or []
        if not items or all(item.get("kind") == "ref_text" for item in items):
            continue
        column_key = str(stream.get("column_key") or (items[0].get("column_key") if items else "") or stream_side_for_bbox(bbox, page_width))
        left = float(bbox[0])
        right = float(bbox[2])
        top = float(bbox[1])
        bottom = float(bbox[3])
        role = str(stream.get("debug_role") or (items[0].get("debug_role") if items else "") or "")
        box = {"column_key": column_key, "left": left, "right": right, "top": top, "bottom": bottom, "role": role}
        edges["_text_boxes"].append(box)
        if role in {"body_candidate", "merged_body"}:
            edges["_body_boxes"].append(box)
        if role not in {"body_candidate", "merged_body"}:
            continue
        left_key = f"{column_key}_left"
        if left_key not in edges:
            edges[left_key] = left
        else:
            edges[left_key] = min(edges[left_key], left)
        edges[column_key] = max(edges.get(column_key, 0.0), right)
    return edges


def local_column_right_for_bbox(bbox: list[float], column_key: str, page_width: float, column_rights: dict) -> float | None:
    body_boxes = [box for box in column_rights.get("_body_boxes", []) if isinstance(box, dict)]
    if not body_boxes:
        return column_rights.get(column_key)
    top = float(bbox[1])
    bottom = float(bbox[3])
    left = float(bbox[0])
    right = float(bbox[2])
    center_y = (top + bottom) / 2.0
    # Width authority must come from the same local reading region.  A tall
    # paragraph used to expand this band by up to eight times its height,
    # making a lower column borrow an affiliation/header width hundreds of
    # pixels away on the same page.
    band = max(36.0, min(96.0, (bottom - top) * 0.50))

    def near(box: dict) -> bool:
        box_top = float(box.get("top", 0.0))
        box_bottom = float(box.get("bottom", 0.0))
        box_center = (box_top + box_bottom) / 2.0
        overlaps = box_bottom >= top - 8.0 and box_top <= bottom + 8.0
        return overlaps or abs(box_center - center_y) <= band

    def not_self(box: dict) -> bool:
        return not (
            abs(float(box.get("left", 0.0)) - left) < 1.0
            and abs(float(box.get("right", 0.0)) - right) < 1.0
            and abs(float(box.get("top", 0.0)) - top) < 1.0
            and abs(float(box.get("bottom", 0.0)) - bottom) < 1.0
        )

    same_column = [box for box in body_boxes if box.get("column_key") == column_key and near(box) and not_self(box)]
    if same_column:
        return max(float(box.get("right", 0.0)) for box in same_column)
    # Never borrow a column edge from a distant same-page region.  Headers,
    # affiliations, and wide metadata can be classified as text but are not a
    # safe width authority for a lower body column.
    full_column = [box for box in body_boxes if box.get("column_key") == "full" and near(box) and not_self(box)]
    if full_column:
        return max(float(box.get("right", 0.0)) for box in full_column)
    return None


def equation_number_right_for_bbox(
    bbox: list[float],
    page_width: float,
    column_rights: dict | None,
) -> float:
    """Return the right-edge anchor for a numbered equation without altering its bbox.

    MinerU's equation bbox describes formula ink, not the equation number. The
    surrounding text streams already carry stable physical column keys (including
    three or more columns), so use their nearby geometry only to place the
    number. Formula sizing and collision geometry keep the source bbox.
    """
    source = [float(value) for value in bbox[:4]]
    _, top, right, bottom = source
    if not column_rights:
        return right
    geometry_boxes = [
        box for collection in ("_body_boxes", "_text_boxes")
        for box in column_rights.get(collection, [])
        if isinstance(box, dict)
    ]
    if not geometry_boxes:
        return right

    left = source[0]
    width = max(1.0, right - left)
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    band = max(40.0, min(112.0, (bottom - top) * 1.5))
    by_column: dict[str, list[tuple[dict, float]]] = {}
    seen_boxes: set[tuple[str, float, float, float, float]] = set()
    for box in geometry_boxes:
        box_left = float(box.get("left", 0.0))
        box_right = float(box.get("right", 0.0))
        box_top = float(box.get("top", 0.0))
        box_bottom = float(box.get("bottom", 0.0))
        identity = (str(box.get("column_key") or ""), box_left, box_right, box_top, box_bottom)
        if identity in seen_boxes:
            continue
        seen_boxes.add(identity)
        box_center_y = (box_top + box_bottom) / 2.0
        if not (box_bottom >= top - 10.0 and box_top <= bottom + 10.0) and abs(box_center_y - center_y) > band:
            continue
        overlap = max(0.0, min(right, box_right) - max(left, box_left))
        contains_center = box_left - 8.0 <= center_x <= box_right + 8.0
        if not contains_center and overlap < max(14.0, width * 0.15):
            continue
        key = str(box.get("column_key") or "")
        if key:
            by_column.setdefault(key, []).append((box, overlap))
    if not by_column:
        return right

    def column_score(entries: list[tuple[dict, float]]) -> float:
        score = 0.0
        for box, overlap in entries:
            box_left = float(box.get("left", 0.0))
            box_right = float(box.get("right", 0.0))
            score = max(score, overlap + (page_width if box_left - 8.0 <= center_x <= box_right + 8.0 else 0.0))
        return score

    ranked = sorted(by_column.items(), key=lambda entry: column_score(entry[1]), reverse=True)
    chosen_key, chosen_entries = ranked[0]
    chosen_right = max(float(box.get("right", right)) for box, _ in chosen_entries)
    chosen_left = min(float(box.get("left", left)) for box, _ in chosen_entries)

    # A formula spanning more than one locally visible column should place its
    # number at the outer edge of the span, rather than in an arbitrary middle
    # column.  This works for two, three, and mixed-column pages alike.
    overlapping_columns = [
        entries for entries in by_column.values()
        if max(overlap for _, overlap in entries) >= max(14.0, width * 0.15)
    ]
    chosen_column_width = max(1.0, chosen_right - chosen_left)
    if len(overlapping_columns) >= 2 and width >= chosen_column_width * 1.25:
        chosen_right = max(
            float(box.get("right", right))
            for entries in overlapping_columns
            for box, _ in entries
        )
    return max(right, chosen_right)


def expand_narrow_text_stream_to_column(
    stream: dict,
    page_width: float,
    column_rights: dict | None,
    barrier_boxes: list[list[float]] | None = None,
) -> None:
    if not column_rights:
        return
    items = stream.get("items") or []
    if not items or all(item.get("kind") == "ref_text" for item in items):
        return
    role = str(stream.get("debug_role") or (items[0].get("debug_role") if items else "") or "")
    if role not in {"body_candidate", "merged_body"}:
        return
    bbox = stream.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return
    column_key = str(stream.get("column_key") or (items[0].get("column_key") if items else "") or stream_side_for_bbox(bbox, page_width))
    if column_key == "full":
        return
    target_right = local_column_right_for_bbox(bbox, column_key, page_width, column_rights)
    target_left = column_rights.get(f"{column_key}_left")
    if target_right is None:
        return
    current_width = max(1.0, float(bbox[2]) - float(bbox[0]))
    column_width = max(1.0, float(target_right) - float(target_left if target_left is not None else bbox[0]))
    if current_width >= column_width * 0.94:
        return
    if float(bbox[0]) > float(target_right):
        return
    # Do not "repair" a deliberately narrow source fragment into the full
    # column when a visual, caption, equation, or other positioned block is
    # occupying the missing right-hand band at the same vertical position.
    # This is common for prose that wraps around a right-aligned figure.
    prospective_bbox = [float(bbox[0]), float(bbox[1]), float(target_right), float(bbox[3])]
    for barrier_bbox in barrier_boxes or []:
        if not isinstance(barrier_bbox, list) or len(barrier_bbox) < 4:
            continue
        horizontal_overlap = min(prospective_bbox[2], float(barrier_bbox[2])) - max(prospective_bbox[0], float(barrier_bbox[0]))
        vertical_overlap = min(prospective_bbox[3], float(barrier_bbox[3])) - max(prospective_bbox[1], float(barrier_bbox[1]))
        if horizontal_overlap >= 8.0 and vertical_overlap >= 3.0 and float(barrier_bbox[0]) > float(bbox[0]) + 24.0:
            return
    bbox[2] = max(float(bbox[2]), float(target_right))
    stream["bbox"] = bbox


def retreat_intruding_column_boundaries(streams: list[dict]) -> int:
    """Move overlapping text-column edges back to their shared boundary.

    MinerU normally keeps neighbouring text columns apart, but a malformed
    layout box (or a later width expansion) can make two simultaneously
    visible columns overlap.  Keep their vertical positions intact and split
    only the horizontal overlap at its midpoint.  This is deliberately a
    last-line safeguard: boxes from different vertical regions are not
    touched, so full-width front matter remains unaffected.
    """
    repaired = 0
    candidates: list[tuple[dict, list[float], str]] = []
    for stream in streams:
        bbox = stream.get("bbox")
        items = stream.get("items") or []
        if not isinstance(bbox, list) or len(bbox) < 4 or not items:
            continue
        column_key = str(stream.get("column_key") or items[0].get("column_key") or "")
        if not column_key or column_key == "full":
            continue
        candidates.append((stream, bbox, column_key))

    for index, (left_stream, left_bbox, left_key) in enumerate(candidates):
        for right_stream, right_bbox, right_key in candidates[index + 1 :]:
            if left_key == right_key:
                continue
            first_stream, first_bbox = left_stream, left_bbox
            second_stream, second_bbox = right_stream, right_bbox
            if float(first_bbox[0]) > float(second_bbox[0]):
                first_stream, second_stream = second_stream, first_stream
                first_bbox, second_bbox = second_bbox, first_bbox
            vertical_overlap = min(float(first_bbox[3]), float(second_bbox[3])) - max(float(first_bbox[1]), float(second_bbox[1]))
            if vertical_overlap <= 2.0:
                continue
            if float(first_bbox[2]) <= float(second_bbox[0]):
                continue
            boundary = (float(first_bbox[2]) + float(second_bbox[0])) / 2.0
            if boundary - float(first_bbox[0]) < 20.0 or float(second_bbox[2]) - boundary < 20.0:
                continue
            first_bbox[2] = boundary
            second_bbox[0] = boundary
            first_stream["bbox"] = first_bbox
            second_stream["bbox"] = second_bbox
            repaired += 1
    return repaired


def stream_line_metrics(items: list[dict], ocr_boxes: list[list[float]]) -> tuple[float, float]:
    if not items:
        return 8.5, 1.14
    bbox = bbox_union([item["bbox"] for item in items])
    refs_only = all(item.get("kind") == "ref_text" for item in items)
    boxes = ocr_boxes_in_region(ocr_boxes, bbox, padding=4.0)
    heights = [bbox_height(box) for box in boxes]
    centers = sorted(bbox_center(box)[1] for box in boxes)
    gaps = [centers[index + 1] - centers[index] for index in range(len(centers) - 1) if centers[index + 1] - centers[index] > 1.0]
    median_height = median_value(heights, 10.0 if refs_only else 10.8)
    median_gap = median_value(gaps, median_height * (1.14 if refs_only else 1.18))
    target_font = median_height * (0.92 if refs_only else 0.94)
    target_font = max(7.1 if refs_only else 7.6, min(9.0 if refs_only else 10.4, target_font))
    line_height_ratio = median_gap / max(1.0, target_font)
    line_height_ratio = max(1.02 if refs_only else 1.05, min(1.24 if refs_only else 1.28, line_height_ratio))
    return round(target_font, 2), round(line_height_ratio, 3)


def stream_text_length(items: list[dict]) -> int:
    text = " ".join(str(item.get("plain_text") or "") for item in items).strip()
    return max(1, len(re.sub(r"\s+", " ", text)))


def stream_paragraphs(items: list[dict]) -> list[dict]:
    paragraphs: list[dict] = []
    for item in items:
        if isinstance(item.get("paragraphs"), list) and item.get("paragraphs"):
            for paragraph in item.get("paragraphs") or []:
                if isinstance(paragraph, dict):
                    paragraphs.append(paragraph)
            continue
        paragraphs.append(
            {
                "html": str(item.get("html") or ""),
                "plain_text": str(item.get("plain_text") or ""),
                "indent_px": float(item.get("indent_px") or 0.0),
            }
        )
    return paragraphs


def stream_inner_bbox(bbox: list[float], refs_only: bool) -> list[float]:
    if refs_only:
        left_pad = 2.0
        right_pad = 2.0
        top_pad = 1.0
        bottom_pad = 1.0
    else:
        left_pad = 2.0
        right_pad = 4.0
        top_pad = 0.0
        bottom_pad = 0.0
    return [
        float(bbox[0]) + left_pad,
        float(bbox[1]) + top_pad,
        max(float(bbox[0]) + left_pad + 4.0, float(bbox[2]) - right_pad),
        max(float(bbox[1]) + top_pad + 4.0, float(bbox[3]) - bottom_pad),
    ]


def capacity_for_bbox(bbox: list[float], font_size: float, line_height_ratio: float, refs_only: bool) -> float:
    inner_bbox = stream_inner_bbox(bbox, refs_only)
    width = max(12.0, bbox_width(inner_bbox))
    height = max(12.0, bbox_height(inner_bbox))
    line_height = font_size * line_height_ratio
    usable_width = width * (0.965 if refs_only else 0.985)
    chars_per_line = max(6.0, usable_width / max(1.0, font_size * (0.5 if refs_only else 0.49)))
    line_count = max(1.0, height / max(1.0, line_height))
    return chars_per_line * line_count


def fill_ratio_for_stream(stream: dict, font_size: float, line_height_ratio: float, refs_only: bool) -> float:
    bbox = stream.get("bbox") or [0, 0, 0, 0]
    used_height = estimate_stream_used_height(stream, font_size, line_height_ratio, 0.18 if not refs_only else 0.10, refs_only)
    return min(1.0, used_height / max(1.0, bbox_height(bbox)))


def estimate_stream_used_height(
    stream: dict,
    font_size: float,
    line_height_ratio: float,
    paragraph_gap_em: float,
    refs_only: bool,
) -> float:
    bbox = stream.get("bbox") or [0, 0, 0, 0]
    inner_bbox = stream_inner_bbox(bbox, refs_only)
    width = max(12.0, bbox_width(inner_bbox))
    line_height = font_size * line_height_ratio
    paragraphs = stream_paragraphs(stream.get("items") or [])
    char_factor = 0.47 if refs_only else 0.45
    total_lines = 0
    for paragraph in paragraphs:
        plain_text = re.sub(r"\s+", " ", str(paragraph.get("plain_text") or "")).strip()
        if not plain_text:
            continue
        indent_px = 0.0 if refs_only else float(paragraph.get("indent_px") or 0.0)
        full_chars = max(6.0, width / max(1.0, font_size * char_factor))
        first_line_chars = max(4.0, (width - indent_px) / max(1.0, font_size * char_factor))
        text_len = len(plain_text)
        if text_len <= first_line_chars:
            para_lines = 1
        else:
            remaining = text_len - first_line_chars
            para_lines = 1 + int((remaining + full_chars - 1) // full_chars)
        total_lines += max(1, para_lines)
    if total_lines <= 0:
        return 0.0
    paragraph_gaps = max(0, len([p for p in paragraphs if str(p.get("plain_text") or "").strip()]) - 1)
    return total_lines * line_height + paragraph_gaps * paragraph_gap_em * font_size


def stream_bottom_gap(stream: dict, font_size: float, line_height_ratio: float, paragraph_gap_em: float, refs_only: bool) -> float:
    bbox = stream.get("bbox") or [0, 0, 0, 0]
    return max(0.0, bbox_height(bbox) - estimate_stream_used_height(stream, font_size, line_height_ratio, paragraph_gap_em, refs_only))


def solve_uniform_stream_style(streams: list[dict], ocr_boxes_by_page: dict[int, list[list[float]]], refs_only: bool) -> tuple[float, float]:
    if not streams:
        return (7.8, 1.12) if refs_only else (8.4, 1.16)
    base_fonts: list[float] = []
    line_ratios: list[float] = []
    min_font, max_font = ((6.9, 8.8) if refs_only else (7.2, 10.6))
    for stream in streams:
        metrics = stream_line_metrics(stream.get("items") or [], ocr_boxes_by_page.get(int(stream.get("page_index") or 0), []))
        base_fonts.append(metrics[0])
        line_ratios.append(metrics[1])
    base_font = median_value(base_fonts, 7.8 if refs_only else 8.4)
    line_height_ratio = median_value(line_ratios, 1.10 if refs_only else 1.16)
    line_candidates = [line_height_ratio]
    paragraph_gap_candidates = [0.10] if refs_only else [0.12, 0.16, 0.20]
    if refs_only:
        line_candidates.extend([max(1.0, line_height_ratio * 0.96), min(1.18, line_height_ratio * 1.02)])
    else:
        line_candidates.extend([max(1.03, line_height_ratio * 0.95), min(1.18, line_height_ratio * 1.02), max(1.02, line_height_ratio * 0.98)])
    best_score = -10**18
    best_style = (min(max_font, max(min_font, base_font)), line_height_ratio, paragraph_gap_candidates[0])
    for candidate_ratio in sorted(set(round(value, 3) for value in line_candidates)):
        for paragraph_gap_em in paragraph_gap_candidates:
            lo = min_font
            hi = max(base_font, min(max_font, base_font * (1.40 if refs_only else 1.50)))
            candidate_font = min(max_font, max(min_font, base_font))
            for _ in range(22):
                mid = (lo + hi) / 2.0
                fits_all = True
                for stream in streams:
                    bbox = stream.get("bbox") or [0, 0, 0, 0]
                    used_height = estimate_stream_used_height(stream, mid, candidate_ratio, paragraph_gap_em, refs_only)
                    if used_height > bbox_height(bbox):
                        fits_all = False
                        break
                if fits_all:
                    candidate_font = mid
                    lo = mid
                else:
                    hi = mid
            # Bottom-band check: if every block still leaves a large blank band near the bottom,
            # keep pushing the font upward until one block reaches the band or we hit overflow.
            if streams:
                sample_page_index = int((streams[0].get("page_index") or 0))
                sample_boxes = ocr_boxes_by_page.get(sample_page_index) or []
                sample_page_height = 792.0
                if sample_boxes:
                    sample_page_height = max(float(box[3]) for box in sample_boxes) / 0.92
                check_band = max(6.0, sample_page_height * 0.02)
                while candidate_font < max_font:
                    gaps = [stream_bottom_gap(stream, candidate_font, candidate_ratio, paragraph_gap_em, refs_only) for stream in streams]
                    if not gaps or min(gaps) <= check_band:
                        break
                    next_font = min(max_font, candidate_font + 0.2)
                    overflow = False
                    for stream in streams:
                        bbox = stream.get("bbox") or [0, 0, 0, 0]
                        used_height = estimate_stream_used_height(stream, next_font, candidate_ratio, paragraph_gap_em, refs_only)
                        if used_height > bbox_height(bbox):
                            overflow = True
                            break
                    if overflow or next_font <= candidate_font:
                        break
                    candidate_font = next_font
            fill_ratios = [
                min(1.0, estimate_stream_used_height(stream, candidate_font, candidate_ratio, paragraph_gap_em, refs_only) / max(1.0, bbox_height(stream.get("bbox") or [0, 0, 0, 0])))
                for stream in streams
            ]
            min_fill = min(fill_ratios) if fill_ratios else 0.0
            avg_fill = sum(fill_ratios) / max(1, len(fill_ratios))
            score = min_fill * 2000.0 + avg_fill * 260.0 + candidate_font * 20.0 - candidate_ratio * 4.0 - paragraph_gap_em * 3.0
            if score > best_score:
                best_score = score
                best_style = (candidate_font, candidate_ratio, paragraph_gap_em)
    best_font, best_ratio, _ = best_style
    return round(best_font, 2), round(best_ratio, 3)


def render_flow_stream(
    stream: dict,
    page_width: float,
    page_height: float,
    ocr_boxes: list[list[float]],
    uniform_styles: dict[str, tuple[float, float]] | None = None,
) -> str:
    bbox = stream["bbox"]
    items = stream["items"]
    if not items:
        return ""
    refs_only = all(item.get("kind") == "ref_text" for item in items)
    debug_role = str(stream.get("debug_role") or (items[0].get("debug_role") if items else "") or "")
    from_list = any(bool(item.get("from_list")) for item in items)
    toc_rows = [row for item in items for row in (item.get("toc_rows") or [])]
    is_toc = bool(toc_rows)
    is_body_stream = (not refs_only) and debug_role in {"body_candidate", "merged_body", "body_inherited"}
    is_inherited_body_stream = debug_role == "body_inherited"
    is_equation_dense = is_body_stream and any(bool(item.get("equation_dense")) for item in items)
    style_key = "ref_text" if refs_only else ("body_text" if is_body_stream else "text")
    if is_toc:
        # Contents pages are dense, but should remain comfortably larger than
        # ordinary labels.  Their row grid, rather than prose wrapping, owns
        # the available height.
        font_size = 8.2
        line_height_ratio = 1.22
    elif not refs_only and not is_body_stream:
        font_size = fixed_layout_font_size("text") or 7.6
        line_height_ratio = 1.28
    elif uniform_styles and style_key in uniform_styles:
        font_size, line_height_ratio = uniform_styles[style_key]
    else:
        font_size, line_height_ratio = solve_uniform_stream_style(
            [{"bbox": bbox, "items": items, "page_index": int(stream.get("page_index") or 0)}],
            {int(stream.get("page_index") or 0): ocr_boxes},
            refs_only,
        )
    item_html_parts: list[str] = []
    overlay_parts: list[str] = []
    current_text_buffer = ""
    current_plain = ""
    current_kind = ""
    current_indent = 0.0
    paragraph_gap = 0.10 if refs_only else 0.16
    stream_indent = 0.0 if refs_only else normalized_indent_px(items)
    def flush_current():
        nonlocal current_text_buffer, current_plain, current_kind, current_indent
        if not current_text_buffer:
            return
        css_class = "flow-ref" if current_kind == "ref_text" else "flow-para"
        style_attr = f""" style="text-indent:{current_indent:.2f}px;" """ if current_kind != "ref_text" and current_indent > 0.0 else ""
        item_html_parts.append(f"""<div class="{css_class}"{style_attr}>{current_text_buffer}</div>""")
        current_text_buffer = ""
        current_plain = ""
        current_kind = ""
        current_indent = 0.0
    if is_toc:
        item_html_parts.append(render_toc_rows(toc_rows))
    else:
        for paragraph in stream_paragraphs(items):
            kind = "ref_text" if refs_only else "text"
            fragment_html = str(paragraph.get("html") or "")
            plain = str(paragraph.get("plain_text") or "")
            flush_current()
            current_text_buffer = fragment_html
            current_plain = plain
            current_kind = kind
            current_indent = 0.0 if refs_only else float(paragraph.get("indent_px") or stream_indent)
            flush_current()
    stream_left = float(bbox[0])
    stream_top = float(bbox[1])
    for item in items:
        item_bbox = item.get("bbox")
        item_lines = item.get("debug_lines")
        if not isinstance(item_lines, list):
            continue
        for line in item_lines:
            if not isinstance(line, dict):
                continue
            line_bbox = line.get("bbox")
            if not isinstance(line_bbox, list) or len(line_bbox) < 4:
                continue
            overlay_parts.append(
                f"""<span class="layout-line-debug-box" style="left:{float(line_bbox[0]) - stream_left:.2f}px;top:{float(line_bbox[1]) - stream_top:.2f}px;width:{max(1.0, float(line_bbox[2]) - float(line_bbox[0])):.2f}px;height:{max(1.0, float(line_bbox[3]) - float(line_bbox[1])):.2f}px;"></span>"""
            )
    body_html = "".join(item_html_parts)
    if overlay_parts:
        body_html += "".join(overlay_parts)
    left = float(bbox[0])
    top = float(bbox[1])
    width = max(4.0, float(bbox[2]) - float(bbox[0]))
    height = max(4.0, float(bbox[3]) - float(bbox[1]))
    paragraphs = stream_paragraphs(items)
    original_line_count = sum(int(item.get("original_line_count") or 0) for item in items)
    stream_plain_text = " ".join(str(paragraph.get("plain_text") or "") for paragraph in paragraphs)
    role_plain_text = " ".join(str(item.get("role_text") or item.get("plain_text") or "") for item in items)
    original_line_group = "multi" if is_toc else layout_text_line_group(role_plain_text or stream_plain_text, original_line_count, len(items), len(paragraphs))
    # 仅对未升格正文的单行 text 做轴线对称判断：
    # 对称框保持居中并允许左右溢出；非对称框左对齐并允许向右溢出。
    single_line_align = "left"
    if original_line_group == "single" and debug_role == "text":
        single_line_align = "center" if layout_single_line_text_is_axis_symmetric(bbox, page_width) else "left"
    stream_class = "layout-flow-stream refs" if all(item.get("kind") == "ref_text" for item in items) else "layout-flow-stream"
    if is_toc:
        stream_class += " toc-stream"
    if debug_role:
        stream_class += f" debug-{debug_role}"
    if from_list:
        stream_class += " from-list"
    return (
        f"""<div class="{stream_class}" style="left:{left:.2f}px;top:{top:.2f}px;width:{width:.2f}px;height:{height:.2f}px;"""
        f"""font-size:{font_size:.2f}px;line-height:{line_height_ratio:.3f};--para-gap:{paragraph_gap:.3f}em;"""
        f"""--page-width:{page_width:.2f}px;--page-height:{page_height:.2f}px;" """
        f"""data-flow-kind="{'ref_text' if refs_only else 'text'}" data-style-kind="{style_key}" data-base-font="{font_size:.2f}" data-line-ratio="{line_height_ratio:.3f}" """
        f"""data-para-gap="{paragraph_gap:.3f}" data-page-height="{page_height:.2f}" data-original-lines="{original_line_group}" data-single-line-align="{single_line_align}" data-from-list="{1 if from_list else 0}" data-body-inherited="{1 if is_inherited_body_stream else 0}" data-equation-dense="{1 if is_equation_dense else 0}" data-toc="{1 if is_toc else 0}" data-fit-label="">{body_html}</div>"""
    )


def render_layout_text_block(
    block: dict,
    page_width: float,
    page_height: float,
    column_rights: dict[str, float] | None = None,
    force_main_title: bool = False,
) -> str:
    bbox = block.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return ""
    body_html = layout_lines_to_html(block.get("lines"))
    if not body_html:
        return ""
    body_html += layout_line_debug_overlays(layout_debug_lines_for_block(block), bbox)
    block_type = str(block.get("type") or "text")
    font_size = estimate_layout_font_size(block_type, bbox, re.sub(r"<[^>]+>", "", body_html))
    draw_bbox = expand_bbox_to_column_right(bbox, block_type, page_width, column_rights)
    title_bbox_width = bbox_width(bbox)
    title_bbox_height = bbox_height(bbox)
    geometry_main_title = (
        block_type.lower() == "title"
        and float(bbox[1]) <= page_height * 0.35
        and title_bbox_width >= page_width * 0.45
        and title_bbox_height >= max(24.0, page_height * 0.035)
    )
    is_main_title = force_main_title or bool(block.get("_layout_main_title")) or geometry_main_title
    if is_main_title and column_rights:
        left_edge = column_rights.get("left_left")
        right_edge = column_rights.get("right") or column_rights.get("left")
        if left_edge is not None and right_edge is not None:
            draw_bbox = [float(left_edge), float(draw_bbox[1]), float(right_edge), float(draw_bbox[3])]
    extra_class = "main-title" if is_main_title else ""
    original_line_count = layout_original_line_count(block)
    data_attrs: dict[str, str] = {}
    if block_type.lower() == "text":
        plain_text = re.sub(r"<[^>]+>", "", body_html)
        line_group = layout_text_line_group(plain_text, original_line_count, 1, 1)
        line_ratio = 1.28
        data_attrs = {
            "block-kind": "text",
            "original-lines": line_group,
            "base-font": f"{(font_size or 7.6):.2f}",
            "line-ratio": f"{line_ratio:.3f}",
            "page-height": f"{page_height:.2f}",
            "fit-label": "",
        }
    elif block_type.lower() == "title":
        # Keep the title's geometry-derived estimate as its own starting point.
        # Journals and heading levels differ, so the browser fitter must not
        # fall back to its generic 8px default or a document-wide fixed size.
        data_attrs = {
            "block-kind": "title",
            "base-font": f"{(font_size or 10.4):.2f}",
            "line-ratio": "1.120",
            "page-height": f"{page_height:.2f}",
            "fit-label": "",
        }
    return render_layout_positioned_block(draw_bbox, block_type, body_html, page_width, page_height, font_size, extra_class, data_attrs)


def model_item_text_html(item: dict) -> str:
    content = item.get("content")
    if content is None:
        content = item.get("text")
    if content is not None and str(content).strip():
        return html.escape(str(content).strip()).replace("\n", "<br>")
    body_html = layout_lines_to_html(item.get("lines"))
    if body_html:
        return body_html
    spans = item.get("spans")
    if isinstance(spans, list):
        return layout_spans_to_html(spans)
    return ""


def normalize_model_bbox(bbox: list[float], page_width: float, page_height: float) -> list[float]:
    values = [float(value) for value in bbox[:4]]
    if max(abs(value) for value in values) <= 1.5:
        return model_bbox_to_page_bbox(values, page_width, page_height)
    return values


def collect_model_positioned_items(model_page) -> list[dict]:
    candidates: list[dict] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        bbox = value.get("bbox")
        if isinstance(bbox, list) and len(bbox) >= 4 and model_item_text_html(value):
            candidates.append(value)
        for key in ("blocks", "lines", "spans", "children"):
            nested = value.get(key)
            if isinstance(nested, (list, dict)):
                visit(nested)

    visit(model_page)
    return candidates


def collect_layout_occupied_boxes(blocks) -> list[list[float]]:
    boxes: list[list[float]] = []

    def visit(value) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        bbox = value.get("bbox")
        if isinstance(bbox, list) and len(bbox) >= 4:
            boxes.append([float(part) for part in bbox[:4]])
        for key in ("blocks", "preproc_blocks", "children"):
            nested = value.get(key)
            if isinstance(nested, (list, dict)):
                visit(nested)

    visit(blocks)
    return boxes


def bbox_area(bbox: list[float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def bbox_contained_overlap_ratio(inner: list[float], outer: list[float]) -> float:
    left = max(float(inner[0]), float(outer[0]))
    top = max(float(inner[1]), float(outer[1]))
    right = min(float(inner[2]), float(outer[2]))
    bottom = min(float(inner[3]), float(outer[3]))
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    return overlap / max(1.0, bbox_area(inner))


def model_item_duplicates_layout(page_bbox: list[float], occupied_boxes: list[list[float]]) -> bool:
    item_area = bbox_area(page_bbox)
    if item_area <= 1.0:
        return True
    for occupied in occupied_boxes:
        if bbox_contained_overlap_ratio(page_bbox, occupied) >= 0.72:
            return True
    return False


def layout_model_draw_type(item_type: str) -> str:
    kind = re.sub(r"[\s-]+", "_", str(item_type or "model_item").lower())
    if kind == "footer":
        return "page_footer"
    if kind == "header":
        return "page_header"
    if kind == "footnote":
        return "page_footnote"
    return kind


def model_item_looks_like_page_metadata(draw_type: str, body_html: str, page_bbox: list[float], page_height: float) -> bool:
    if draw_type in {"page_header", "page_footer", "page_footnote", "page_number", "header", "footer", "footnote"}:
        return True
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body_html or "")).strip()
    if not plain:
        return False
    top = float(page_bbox[1])
    bottom = float(page_bbox[3])
    near_top = top <= float(page_height) * 0.07
    near_bottom = bottom >= float(page_height) * 0.93
    if (near_top or near_bottom) and re.fullmatch(r"(?:[-–—]?\s*)?\d{1,4}(?:\s*[-–—])?", plain):
        return True
    return False


def render_layout_model_items(
    model_page,
    page_width: float,
    page_height: float,
    column_rights: dict[str, float] | None = None,
    occupied_boxes: list[list[float]] | None = None,
    excluded_types: set[str] | None = None,
) -> list[str]:
    rendered: list[str] = []
    if not isinstance(model_page, (list, dict)):
        return rendered
    occupied_boxes = occupied_boxes or []
    seen: set[tuple[str, str, tuple[int, int, int, int]]] = set()
    for item in collect_model_positioned_items(model_page):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or item.get("category") or "model_item").lower()
        if item_type in (excluded_types or set()):
            continue
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        body_html = model_item_text_html(item)
        if not body_html:
            continue
        page_bbox = normalize_model_bbox(bbox, page_width, page_height)
        if page_bbox[2] <= page_bbox[0] or page_bbox[3] <= page_bbox[1]:
            continue
        draw_type = layout_model_draw_type(item_type)
        is_page_metadata = model_item_looks_like_page_metadata(draw_type, body_html, page_bbox, page_height)
        if not is_page_metadata and model_item_duplicates_layout(page_bbox, occupied_boxes):
            continue
        dedupe_key = (
            draw_type,
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body_html)).strip(),
            tuple(round(value) for value in page_bbox),
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        draw_bbox = expand_bbox_to_column_right(page_bbox, draw_type, page_width, column_rights)
        font_size = estimate_layout_font_size(draw_type, page_bbox, re.sub(r"<[^>]+>", "", body_html))
        rendered.append(
            render_layout_positioned_block(
                draw_bbox,
                draw_type,
                body_html,
                page_width,
                page_height,
                font_size,
            )
        )
    return rendered


def render_layout_preview_html(
    markdown_path: Path,
    log=None,
    style: ExportStyleSettings | None = None,
    strict_fit: bool = False,
    debug_overlay: bool | None = None,
) -> Path | None:
    bundle = load_layout_preview_bundle(markdown_path)
    if not bundle:
        return None
    single_column_promotion = single_column_body_promotion_enabled()
    debug_enabled = LAYOUT_PREVIEW_DEBUG if debug_overlay is None else bool(debug_overlay)
    out_path = layout_preview_html_path(markdown_path, strict_fit=strict_fit, debug_overlay=debug_enabled)
    audit_path = layout_audit_report_path(markdown_path)
    dependencies = [markdown_path, bundle["layout_path"]]
    dependencies.extend([Path(__file__).resolve(), Path(infer_single_column_profile.__code__.co_filename).resolve()])
    if bundle.get("model_path"):
        dependencies.append(bundle["model_path"])
    if (
        multi_file_cache_is_fresh(out_path, dependencies)
        and multi_file_cache_is_fresh(audit_path, dependencies)
        and html_contains_marker(
            out_path,
            f"layout-preview version={LAYOUT_PREVIEW_VERSION} single-column-body={int(single_column_promotion)} ",
        )
    ):
        return out_path

    style = resolve_export_style(style)
    audit_payload = collect_layout_audit(bundle)
    try:
        audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        if log:
            log(f"保存排版诊断记录遇到问题：{exc}")
    page_info = bundle["page_info"]
    model_pages = bundle["model_pages"]
    asset_dir = bundle["asset_dir"]
    single_column_profile: SingleColumnProfile | None = (
        infer_single_column_profile(page_info) if single_column_promotion else None
    )
    page_contexts: list[dict] = []
    ocr_boxes_by_page: dict[int, list[list[float]]] = {}
    body_streams: list[dict] = []
    ref_streams: list[dict] = []

    # Promotion used to be page-local.  Build a lightweight document context
    # first so a short paragraph on a figure/equation page can borrow only the
    # column geometry (never wording) of nearby proven body pages.
    provisional_pages: list[dict] = []
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            provisional_pages.append({"profiles": {}})
            continue
        page["page_idx"] = page_index
        raw_page_size = page.get("page_size") or [612, 792]
        if not isinstance(raw_page_size, list) or len(raw_page_size) < 2:
            raw_page_size = [612, 792]
        page_width = max(1.0, float(raw_page_size[0]))
        page_height = max(1.0, float(raw_page_size[1]))
        model_page = model_pages[page_index] if page_index < len(model_pages) else []
        ocr_boxes = collect_model_ocr_boxes(model_page, page_width, page_height)
        flow_items, _absolute_blocks = streamable_layout_items(page, page_width, page_height, ocr_boxes)
        provisional_pages.append({
            "profiles": body_column_profiles(flow_items, page_width, page_height),
        })

    promotion_contexts: dict[int, dict] = {}
    for page_index in range(len(page_info)):
        previous_indexes = [index for index in range(page_index - 1, -1, -1) if provisional_pages[index].get("profiles")]
        following_indexes = [index for index in range(page_index + 1, len(page_info)) if provisional_pages[index].get("profiles")]
        neighbor_profiles: dict[str, list[tuple[float, float]]] = {}
        for neighbor_index in (previous_indexes[:1] + following_indexes[:1]):
            for column, profiles in (provisional_pages[neighbor_index].get("profiles") or {}).items():
                neighbor_profiles.setdefault(column, []).extend(profiles)
        promotion_contexts[page_index] = {
            "has_previous_body": bool(previous_indexes),
            "neighbor_column_profiles": neighbor_profiles,
        }

    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            continue
        page["page_idx"] = page_index
        raw_page_size = page.get("page_size") or [612, 792]
        if not isinstance(raw_page_size, list) or len(raw_page_size) < 2:
            raw_page_size = [612, 792]
        page_width = max(1.0, float(raw_page_size[0]))
        page_height = max(1.0, float(raw_page_size[1]))
        model_page = model_pages[page_index] if page_index < len(model_pages) else []
        ocr_boxes = collect_model_ocr_boxes(model_page, page_width, page_height)
        ocr_boxes_by_page[page_index] = ocr_boxes
        flow_items, absolute_blocks = streamable_layout_items(page, page_width, page_height, ocr_boxes)
        flow_items = promote_text_items_to_body(
            flow_items,
            page_width,
            page_height,
            promotion_contexts.get(page_index),
        )
        flow_items = promote_stable_single_column_items(
            flow_items,
            page_width,
            page_height,
            single_column_profile,
        )
        flow_items = inherit_stable_single_column_short_items(
            flow_items,
            page_width,
            page_height,
            single_column_profile,
        )
        flow_items = merge_vertical_body_items(flow_items, absolute_blocks)
        flow_items = mark_equation_dense_body_items(flow_items, absolute_blocks, page_height)
        flow_items = merge_reference_items(flow_items)
        streams = [
            {
                "side": item.get("side"),
                "column_key": item.get("column_key"),
                "items": [item],
                "bbox": (item.get("bbox") or [0, 0, 0, 0])[:],
                "page_index": page_index,
            }
            for item in sorted(flow_items, key=lambda value: (value.get("side"), value.get("bbox", [0, 0, 0, 0])[1], value.get("bbox", [0, 0, 0, 0])[0]))
        ]
        for stream in streams:
            stream["page_index"] = page_index
            if not stream.get("debug_role"):
                roles = [str(item.get("debug_role") or "") for item in stream.get("items") or []]
                if roles and all(role == roles[0] for role in roles):
                    stream["debug_role"] = roles[0]
            if all(item.get("kind") == "ref_text" for item in stream.get("items") or []):
                ref_streams.append(stream)
            elif str(stream.get("debug_role") or "") in {"body_candidate", "merged_body"}:
                body_streams.append(stream)
        page_contexts.append(
            {
                "page_index": page_index,
                "page": page,
                "page_width": page_width,
                "page_height": page_height,
                "model_page": model_page,
                "ocr_boxes": ocr_boxes,
                "streams": streams,
                "absolute_blocks": absolute_blocks,
                "column_rights": column_right_edges_from_streams(streams, page_width),
            }
        )
    inferred_body_style = solve_uniform_stream_style(body_streams, ocr_boxes_by_page, refs_only=False)
    # Chinese translated prose benefits from a slightly more open baseline than
    # the source-PDF geometry alone suggests. Keep it bounded; the layout fit
    # pass can still tighten a genuinely crowded text box.
    uniform_styles = {
        "body_text": (inferred_body_style[0], min(1.22, inferred_body_style[1] + 0.04)),
        "ref_text": solve_uniform_stream_style(ref_streams, ocr_boxes_by_page, refs_only=True),
    }
    rendered_pages: list[str] = []
    main_title_seen = False
    for context in page_contexts:
        page_index = context["page_index"]
        page = context["page"]
        page_width = context["page_width"]
        page_height = context["page_height"]
        scale = layout_preview_scale(page_width)
        blocks: list[str] = []
        model_page = context["model_page"]
        ocr_boxes = context["ocr_boxes"]
        absolute_blocks = context["absolute_blocks"]
        column_rights = context.get("column_rights") or {}
        occupied_boxes: list[list[float]] = []
        barrier_boxes = layout_barrier_boxes(absolute_blocks)
        for stream in context["streams"]:
            expand_narrow_text_stream_to_column(stream, page_width, column_rights, barrier_boxes)
        retreat_intruding_column_boundaries(context["streams"])
        for stream in context["streams"]:
            stream_bbox = stream.get("bbox")
            if isinstance(stream_bbox, list) and len(stream_bbox) >= 4:
                occupied_boxes.append([float(part) for part in stream_bbox[:4]])
            rendered_stream = render_flow_stream(stream, page_width, page_height, ocr_boxes, uniform_styles)
            if rendered_stream:
                blocks.append(rendered_stream)
        for block in absolute_blocks:
            if not isinstance(block, dict):
                continue
            occupied_boxes.extend(collect_layout_occupied_boxes(block))
            block_type = str(block.get("type") or "").lower()
            if block_type in {"title", "text"}:
                force_main_title = block_type == "title" and not main_title_seen
                rendered = render_layout_text_block(block, page_width, page_height, column_rights, force_main_title=force_main_title)
                if force_main_title and rendered:
                    main_title_seen = True
                if rendered:
                    blocks.append(rendered)
            elif block_type in {"table", "chart", "image"}:
                blocks.extend(render_layout_block_children(block, asset_dir, page_width, page_height, column_rights))
            elif block_type == "interline_equation":
                rendered = render_layout_equation_block(block, asset_dir, page_width, page_height, column_rights)
                if rendered:
                    blocks.append(rendered)
            else:
                rendered = render_layout_generic_block(block, asset_dir, page_width, page_height, column_rights)
                if rendered:
                    blocks.append(rendered)
        blocks.extend(render_layout_model_items(model_page, page_width, page_height, column_rights, occupied_boxes))
        rendered_pages.append(
            (
                f"""<section class="layout-page-wrap" data-sync-page-index="{page_index}" data-page-width="{page_width:.2f}" data-page-height="{page_height:.2f}"><div class="layout-page-label">Page {page_index + 1}</div>"""
                f"""<div class="layout-page-shell" data-page-width="{page_width:.2f}" data-page-height="{page_height:.2f}" """
                f"""style="width:{page_width * scale:.2f}px;height:{page_height * scale:.2f}px;">"""
                f"""<div class="layout-page" style="width:{page_width:.2f}px;height:{page_height:.2f}px;zoom:{scale:.6f};">"""
                f"""{"".join(blocks)}</div></div></section>"""
            )
        )

    if not rendered_pages:
        return None

    layout_css = """
body {
  margin: 0;
  padding: 10px 10px 28px;
  background: #f6f3ee;
  color: #1f2933;
  font-family: {SERIF_READING_FONT_STACK};
}
.layout-doc {
  width: 100%;
  max-width: none;
  margin: 0 auto;
}
/* A layout page must never be readable while its body typography is still a
   page-local provisional fit.  The final pass deliberately works on the
   whole document, then reveals every page together. */
body.layout-fit-pending .layout-page-shell {
  opacity: 0;
  pointer-events: none;
}
body.layout-fit-pending::before {
  content: attr(data-layout-progress);
  position: fixed;
  z-index: 1000;
  top: 14px;
  right: 14px;
  left: auto;
  transform: none;
  max-width: min(360px, calc(100vw - 48px));
  padding: 0;
  color: #334e68;
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  font: 600 11px/1.35 "Cascadia Mono", "Microsoft YaHei UI", monospace;
  letter-spacing: 0.02em;
}
.layout-build-badge {
  position: sticky;
  top: 6px;
  z-index: 20;
  width: fit-content;
  margin: 0 0 10px auto;
  padding: 3px 8px;
  font: 600 11px/1.2 "Segoe UI", "Microsoft YaHei UI", sans-serif;
  color: #7f1d1d;
  background: rgba(254, 242, 242, 0.95);
  border: 1px solid rgba(220, 38, 38, 0.55);
  border-radius: 999px;
}
.layout-note {
  max-width: 980px;
  margin: 0 auto 16px;
  color: #334e68;
  font-size: 13px;
  line-height: 1.5;
}
body:not(.layout-debug) .layout-build-badge,
body:not(.layout-debug) .layout-note,
body:not(.layout-debug) .layout-page-label {
  display: none;
}
.layout-page-wrap {
  margin: 0 auto 18px;
}
.layout-page-label {
  max-width: 980px;
  margin: 0 auto 8px;
  color: #486581;
  font: 600 12px/1.2 "Segoe UI", "Microsoft YaHei UI", sans-serif;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.layout-page-shell {
  position: relative;
  margin: 0 auto;
  background: #ffffff;
  max-width: 100%;
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.10);
  overflow: hidden;
}
.layout-page {
  position: relative;
  background: #ffffff;
  overflow: hidden;
}
.layout-code {
  box-sizing: border-box;
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 5px 7px;
  overflow: hidden;
  color: #4b5563;
  background: #f1f1f1;
  border: 0;
  font: inherit;
  font-family: "Cascadia Mono", "Consolas", "SFMono-Regular", monospace;
  line-height: inherit;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.layout-code code {
  font: inherit;
  white-space: inherit;
}
.layout-flow-stream {
  position: absolute;
  z-index: 4;
  box-sizing: border-box;
  overflow: visible;
  font-size: 9px;
  line-height: 1.1;
  text-align: justify;
  text-justify: auto;
  hyphens: auto;
  overflow-wrap: break-word;
  word-break: normal;
  padding: 0 4px 0 2px;
  border: 0 solid transparent;
  background: transparent;
}
/* 正文迭代检查逻辑：诊断模式仅绘制正文框，并将迭代结果写在框标签中。 */
body.layout-debug .layout-flow-stream,
body.layout-debug .layout-block {
  outline: none !important;
}
body.layout-debug .layout-flow-stream.fit-limiter,
body.layout-debug .layout-flow-stream.fit-blocker,
body.layout-debug .layout-block.fit-limiter,
body.layout-debug .layout-block.fit-blocker {
  border-color: transparent !important;
  background: transparent !important;
  box-shadow: none !important;
}
body.layout-debug .layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"] {
  outline: 2px solid rgba(37, 99, 235, 0.96) !important;
  outline-offset: -1px;
  background: rgba(59, 130, 246, 0.05) !important;
}
body.layout-debug .layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"].body-iteration-collision {
  outline: 3px solid rgba(220, 38, 38, 0.98) !important;
  background: rgba(239, 68, 68, 0.13) !important;
  box-shadow: inset 0 0 0 2px rgba(254, 202, 202, 0.95) !important;
}
.layout-flow-stream.debug-text[data-original-lines="single"] {
  text-align: left;
  overflow: visible;
}
.layout-flow-stream.debug-text[data-original-lines="single"] .flow-para {
  text-indent: 0 !important;
  white-space: nowrap;
  width: max-content;
  max-width: none;
}
.layout-flow-stream.debug-text[data-original-lines="single"][data-single-line-align="center"] .flow-para {
  position: relative;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
}
.layout-flow-stream.debug-text[data-original-lines="single"][data-single-line-align="left"] .flow-para {
  text-align: left;
}
body.layout-debug .layout-flow-stream.debug-reference {
  outline: 2px solid rgba(147, 51, 234, 0.98);
  outline-offset: -1px;
}
body.layout-debug .layout-flow-stream.debug-merged_reference {
  outline: 2px solid rgba(147, 51, 234, 0.98);
  outline-offset: -1px;
}
.layout-flow-stream.refs {
  text-align: left;
}
.layout-flow-stream.fit-limiter,
.layout-block.fit-limiter {
  border-color: rgba(0, 0, 0, 0.98) !important;
  background:
    repeating-linear-gradient(
      135deg,
      rgba(250, 204, 21, 0.20) 0,
      rgba(250, 204, 21, 0.20) 6px,
      rgba(0, 0, 0, 0.08) 6px,
      rgba(0, 0, 0, 0.08) 12px
    ),
    rgba(220, 38, 38, 0.05) !important;
  box-shadow: inset 0 0 0 4px rgba(250, 204, 21, 0.98), 0 0 0 2px rgba(0, 0, 0, 0.95);
}
.layout-flow-stream.fit-blocker,
.layout-block.fit-blocker {
  outline: 3px dashed rgba(14, 165, 233, 0.98) !important;
  outline-offset: -2px;
  box-shadow: inset 0 0 0 3px rgba(14, 165, 233, 0.35), 0 0 0 2px rgba(2, 132, 199, 0.85) !important;
}
.layout-flow-stream::after,
.layout-block::after {
  content: attr(data-fit-label);
  position: absolute;
  left: 2px;
  top: 2px;
  z-index: 20;
  padding: 2px 5px;
  font: 800 10px/1.15 "Segoe UI", "Microsoft YaHei UI", sans-serif;
  color: #111827;
  background: rgba(255, 255, 255, 0.96);
  border: 2px solid rgba(17, 24, 39, 0.75);
  pointer-events: none;
}
body:not(.layout-debug) .layout-flow-stream::after,
body:not(.layout-debug) .layout-block::after {
  display: none;
}
/* 正文迭代检查逻辑只显示正文的状态标签，避免非正文块干扰检查。 */
body.layout-debug .layout-flow-stream::after,
body.layout-debug .layout-block::after {
  display: none !important;
}
body.layout-debug .layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]::after {
  display: block !important;
  content: attr(data-fit-label);
  color: #172554;
  background: rgba(239, 246, 255, 0.97);
  border-color: rgba(37, 99, 235, 0.95);
  white-space: nowrap;
}
body.layout-debug .layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"].body-iteration-collision::after {
  color: #7f1d1d;
  background: rgba(254, 242, 242, 0.98);
  border-color: rgba(220, 38, 38, 0.98);
}
.layout-flow-stream.fit-limiter::after,
.layout-block.fit-limiter::after {
  color: #000000;
  background: rgba(250, 204, 21, 0.96);
  border-color: rgba(0, 0, 0, 0.98);
}
.layout-flow-stream.fit-blocker::after,
.layout-block.fit-blocker::after {
  content: attr(data-fit-label);
  position: absolute;
  left: 2px;
  top: 2px;
  z-index: 21;
  padding: 2px 5px;
  font: 800 10px/1.15 "Segoe UI", "Microsoft YaHei UI", sans-serif;
  color: #ffffff;
  background: rgba(2, 132, 199, 0.96);
  border: 2px solid rgba(12, 74, 110, 0.98);
  pointer-events: none;
}
.layout-flow-stream[data-fit-label=""]::after,
.layout-block[data-fit-label=""]::after {
  display: none;
}
body:not(.layout-debug) .layout-flow-stream.fit-blocker::after,
body:not(.layout-debug) .layout-block.fit-blocker::after {
  display: none;
}
.layout-line-debug-box {
  display: none;
  position: absolute;
  z-index: 24;
  box-sizing: border-box;
  border: 1px dashed rgba(6, 182, 212, 0.95);
  background: rgba(6, 182, 212, 0.04);
  pointer-events: none;
}
/* 诊断模式同时显示 MinerU 的原始 line 块，便于逐行核对块级布局。 */
body.layout-debug .layout-line-debug-box { display: block; }
body.layout-debug .layout-line-debug-box::after {
  content: "line";
  position: absolute;
  right: 0;
  top: -10px;
  font: 700 7px/1 "Segoe UI", sans-serif;
  color: rgba(8, 145, 178, 0.95);
  background: rgba(255, 255, 255, 0.85);
}
.layout-collision-debug-layer,
.layout-collision-debug-box,
.layout-collision-debug-line {
  display: none;
  position: absolute;
  pointer-events: none;
}
body.layout-debug .layout-collision-debug-layer,
body.layout-debug .layout-collision-debug-box,
body.layout-debug .layout-collision-debug-line { display: none; }
.layout-collision-debug-layer {
  inset: 0;
  z-index: 36;
  overflow: hidden;
}
.layout-collision-debug-line {
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}
.layout-collision-debug-box {
  box-sizing: border-box;
  z-index: 37;
  border: 2px solid rgba(239, 68, 68, 0.98);
  background: rgba(239, 68, 68, 0.08);
}
.layout-collision-debug-box::after {
  content: attr(data-debug-label);
  position: absolute;
  left: 0;
  top: -14px;
  padding: 1px 4px;
  font: 800 8px/1.1 "Segoe UI", "Microsoft YaHei UI", sans-serif;
  color: #ffffff;
  background: rgba(185, 28, 28, 0.95);
  white-space: nowrap;
}
.layout-flow-stream .flow-para {
  margin: 0 0 var(--para-gap, 0.22em) 0;
}
.layout-flow-stream .flow-ref {
  margin: 0 0 var(--para-gap, 0.14em) 0;
  padding-left: 1.1em;
  text-indent: -1.1em;
}
.layout-flow-stream .flow-para:last-child,
.layout-flow-stream .flow-ref:last-child {
  margin-bottom: 0;
}
.layout-flow-stream.toc-stream {
  text-align: left;
  overflow: hidden;
  font-variant-numeric: tabular-nums;
}
.toc-row {
  display: grid;
  grid-template-columns: minmax(0, auto) minmax(12px, 1fr) auto;
  align-items: baseline;
  min-width: 0;
  padding-left: calc(var(--toc-level, 0) * 0.82em);
  white-space: nowrap;
}
.toc-row.toc-level-0 { font-weight: 700; }
.toc-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: clip;
}
.toc-number { white-space: pre; }
.toc-leader {
  min-width: 12px;
  margin: 0 0.34em 0.20em 0.40em;
  border-bottom: 1px dotted currentColor;
  opacity: 0.78;
}
.toc-page {
  min-width: 2.1em;
  text-align: right;
  font-weight: 400;
}
.toc-gap { height: 0.40em; }
.toc-unparsed { white-space: nowrap; overflow: hidden; }
.layout-block {
  position: absolute;
  z-index: 3;
  overflow: visible;
  white-space: normal;
  word-break: break-word;
  box-sizing: border-box;
  line-height: 1.12;
  font-size: 10.4px;
  text-align: justify;
  border: 0 solid transparent;
  background: transparent;
}
/* 其他版面块仍参与碰撞计算，但诊断模式不再为它们绘制框。 */
.layout-block.type-title,
.layout-block.type-header,
.layout-block.type-page_header,
.layout-block.type-footer,
.layout-block.type-page_footer,
.layout-block.type-page_number {
  font-family: "Arial", "Microsoft YaHei UI", sans-serif;
}
.layout-block.type-title {
  font-size: 14px;
  font-weight: 700;
  text-align: left;
  line-height: 1.12;
}
.layout-block.type-title.main-title {
  text-align: center;
}
.layout-block.type-page_header {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-align: center;
}
.layout-block.type-header {
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-align: center;
}
.layout-block.type-footer,
.layout-block.type-page_footer,
.layout-block.type-page_footnote {
  font-size: 8.5px;
  text-align: center;
}
.layout-block.type-page_number {
  font-size: 9px;
  text-align: left;
}
.layout-block.type-text {
  font-size: 10.4px;
  text-align: justify;
}
.layout-block.type-ref_text {
  font-size: 8.2px;
  line-height: 1.06;
  text-align: left;
}
.layout-block.type-table_caption,
.layout-block.type-table_footnote,
.layout-block.type-chart_caption,
.layout-block.type-image_caption {
  font: 9px/1.2 "Arial", "Microsoft YaHei UI", sans-serif;
  text-align: left;
}
.layout-block.type-table_body,
.layout-block.type-chart_body,
.layout-block.type-image_body {
  padding: 0;
}
.layout-media {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  cursor: zoom-in;
}
#layout-image-lightbox {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: none;
  align-items: center;
  justify-content: center;
  background: rgba(16, 24, 32, 0.76);
  cursor: grab;
  touch-action: none;
  user-select: none;
}
#layout-image-lightbox.open {
  display: flex;
}
#layout-image-lightbox img {
  max-width: none;
  max-height: none;
  margin: 0;
  cursor: grab;
  transform-origin: 0 0;
  -webkit-user-drag: none;
  user-select: none;
}
#layout-image-lightbox .hint {
  position: fixed;
  left: 18px;
  bottom: 14px;
  color: #eef5f7;
  font-size: 12px;
  background: rgba(10, 20, 30, 0.54);
  padding: 6px 9px;
  border-radius: 5px;
}
#layout-formula-lightbox {
  position: fixed;
  inset: 0;
  z-index: 9998;
  display: none;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: rgba(16, 24, 32, 0.78);
  cursor: grab;
  touch-action: none;
  user-select: none;
}
#layout-formula-lightbox.open { display: flex; }
#layout-formula-lightbox .formula-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  min-width: 1px;
  min-height: 1px;
  padding: 24px;
  color: #111827;
  background: #ffffff;
  border-radius: 8px;
  box-shadow: 0 14px 42px rgba(0, 0, 0, 0.34);
  transform-origin: center center;
}
#layout-formula-lightbox .formula-stage mjx-container {
  margin: 0 !important;
  overflow: visible !important;
  cursor: grab;
}
#layout-formula-lightbox .hint {
  position: fixed;
  left: 18px;
  bottom: 14px;
  padding: 6px 9px;
  color: #eef5f7;
  background: rgba(10, 20, 30, 0.58);
  border-radius: 5px;
  font: 12px/1.35 "Segoe UI", "Microsoft YaHei UI", sans-serif;
}
body.layout-production mjx-container { cursor: zoom-in; }
.layout-formula-placeholder { display: inline-block; visibility: hidden; }
/* MathJax's own contextual menu must remain clickable above our formula viewer. */
.CtxtMenu_MenuFrame,
.CtxtMenu_ContextMenu { z-index: 10020 !important; }
.layout-equation-media {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.layout-equation-text {
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  width: 100%;
  height: 100%;
  font: 10px/1.15 "Cambria Math", "Times New Roman", serif;
  overflow: visible;
}
.layout-equation-formula {
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  overflow: visible;
}
.layout-equation-formula .layout-math-display {
  display: inline-block;
  width: auto;
  max-width: none;
  text-align: left;
}
.layout-equation-formula mjx-container {
  display: inline-block !important;
  width: auto !important;
  min-width: 0 !important;
  margin: 0 !important;
  text-align: left !important;
}
.layout-equation-number {
  position: absolute;
  left: var(--equation-number-right, 100%);
  right: auto;
  top: 50%;
  transform: translate(-100%, -50%);
  white-space: nowrap;
  font: 10px/1.15 "Times New Roman", serif;
}
.layout-block.type-interline_equation {
  overflow: visible;
  z-index: 5;
  /* 诊断模式框图不应改变公式本身的容器排版。 */
  padding: 0;
  text-align: center;
}
.layout-table-wrap table {
  width: 100%;
  border-collapse: collapse;
  font-size: 8.6px;
  line-height: 1.15;
}
.layout-table-wrap td,
.layout-table-wrap th {
  border: 1px solid #b8c4ce;
  padding: 1px 3px;
  text-align: center;
}
.layout-chart-data {
  margin: 0;
  padding: 0.4em;
  font: 8px/1.2 Consolas, "Cascadia Mono", monospace;
  background: #f8fafc;
  border: 1px solid #d9e2ec;
  overflow: auto;
}
.layout-missing {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #7b8794;
  border: 1px dashed #cbd2d9;
  background: #f8fafc;
}
.layout-math-inline {
  white-space: nowrap;
}
.layout-math-display {
  display: block;
  width: 100%;
  text-align: center;
}
.layout-flow-stream mjx-container,
.layout-block mjx-container {
  max-width: 100%;
  overflow: hidden;
  font-size: 100% !important;
}

/* MathJax 数学字体与思源宋体的视觉字面高度不同。
   正文已经参与统一字号拟合，只需轻度缩小行内公式。 */
body.layout-translated
.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]
mjx-container:not([display="true"]) {
  font-size: 92% !important;
}

/* 摘要等首页前置信息不会被归类为 body_text，而是普通的多行 text，
   并且其正文基础字号通常更小，因此需要更强的光学补偿。
   排除目录和列表，避免影响其他结构化内容；独立块级公式不受影响。 */
body.layout-translated
.layout-flow-stream.debug-text[data-style-kind="text"][data-flow-kind="text"][data-original-lines="multi"]:not(.toc-stream):not(.from-list)
mjx-container:not([display="true"]) {
  font-size: 88% !important;
}

.layout-block.type-interline_equation mjx-container {
  max-width: none !important;
  overflow: visible !important;
  transform-origin: center center;
}
@page {
  size: auto;
  margin: 0;
}
@media print {
  body {
    background: #ffffff;
    padding: 0;
  }
  .layout-note,
  .layout-page-label {
    display: none;
  }
  .layout-page-wrap {
    break-after: page;
    page-break-after: always;
    margin: 0;
  }
  .layout-page-shell {
    max-width: none;
    box-shadow: none;
  }
}
"""
    # 字体声明必须位于排版 HTML 内部，使 Chromium 直接读取程序内置字体。
    # 排版脚本会等待 document.fonts.ready，字体加载完成后才进行最终尺寸拟合。
    layout_css = (
        bundled_reader_font_face_css()
        + "\n"
        + layout_css.replace(
            "{SERIF_READING_FONT_STACK}",
            SERIF_READING_FONT_STACK,
        )
    )
    # A cold artifact remains visually blocked until one eager, document-wide
    # pass finishes. Warm artifacts restore their completed style cache before
    # the long page DOM is parsed, then reveal only after fonts/formulas settle.
    body_classes = ["layout-debug" if debug_enabled else "layout-production", "layout-fit-pending"]
    if strict_fit:
        body_classes.append("layout-source-strict-fit")
    body_class = " ".join(body_classes)
    note_html = (
        "<p class=\"layout-note\">此视图使用 MinerU 返回的版面坐标和块级内容还原版面，"
        "目标是尽量接近期刊原始双栏排版；它保留了版面关系，但仍可能和原 PDF 有局部行距、字号、题注位置偏差。</p>"
    )
    dependency_fingerprint = hashlib.sha1(
        "|".join(
            f"{path.resolve()}::{path.stat().st_mtime_ns}::{path.stat().st_size}"
            for path in dependencies
            if path and path.exists()
        ).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    fit_cache_fingerprint = hashlib.sha1(
        f"{dependency_fingerprint}|strict={int(strict_fit)}|debug={int(debug_enabled)}".encode("utf-8")
    ).hexdigest()[:16]
    fit_cache_scope = hashlib.sha1(str(out_path.resolve()).encode("utf-8", errors="replace")).hexdigest()[:12]
    html_text = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<!-- layout-preview version={LAYOUT_PREVIEW_VERSION} single-column-body={int(single_column_promotion)} fingerprint={dependency_fingerprint} -->"
        f"<style>{layout_css}</style>"
        f"{mathjax_script_html()}"
        f"{qt_webchannel_script_html()}"
        f"</head><body class=\"{body_class}\" data-layout-cache-version=\"{LAYOUT_FIT_CACHE_VERSION}\" "
        f"data-layout-cache-key=\"{fit_cache_fingerprint}\" "
        f"data-layout-cache-scope=\"{fit_cache_scope}\" "
        f"data-layout-progress=\"正在准备全文排版（共 {len(rendered_pages)} 页）…\">"
        '<script data-layout-fit-disk-cache></script>'
        f"{layout_fit_cache_bootstrap_html(fit_cache_fingerprint, fit_cache_scope)}<main class=\"layout-doc\">"
        f"""<div class="layout-build-badge">build {dependency_fingerprint}</div>{note_html}{''.join(rendered_pages)}</main>"""
        """
<script>
(() => {
  function fitLayoutPages() {
    if (window.__mineruPdfExportMode) return;
    const doc = document.querySelector('.layout-doc');
    const available = Math.max(240, (doc ? doc.clientWidth : window.innerWidth) - 2);
    for (const shell of document.querySelectorAll('.layout-page-shell')) {
      const page = shell.querySelector('.layout-page');
      if (!page) continue;
      const pageWidth = parseFloat(shell.dataset.pageWidth || page.style.width || page.offsetWidth || '1');
      const pageHeight = parseFloat(shell.dataset.pageHeight || page.style.height || page.offsetHeight || '1');
      const wrap = shell.closest('.layout-page-wrap');
      const pageIndex = wrap ? Number(wrap.dataset.syncPageIndex || 0) : 0;
      const forced = window.__mineruForcedPageMetrics && window.__mineruForcedPageMetrics.get(pageIndex);
      const scale = forced && forced.renderedHeight > 0
        ? forced.renderedHeight / Math.max(1, pageHeight)
        : available / Math.max(1, pageWidth);
      shell.style.width = `${pageWidth * scale}px`;
      shell.style.height = `${pageHeight * scale}px`;
      // CSS zoom lays out and rasterizes the CJK glyphs at their final size.
      // transform: scale() downsamples a painted page and makes fine serif
      // strokes uneven at fractional scales.
      page.style.transform = 'none';
      page.style.zoom = String(scale);
    }
  }

  function fitLayoutEquations() {
    for (const block of document.querySelectorAll('.layout-block.type-interline_equation')) {
      const host = block.querySelector('.layout-equation-text') || block;
      const formulaHost = block.querySelector('.layout-equation-formula') || host;
      if (!host || !formulaHost) continue;
      const number = block.querySelector('.layout-equation-number');
      const math = formulaHost.querySelector('mjx-container');
      if (!math) continue;
      math.style.display = 'inline-block';
      math.style.width = 'auto';
      math.style.minWidth = '0';
      math.style.margin = '0';
      math.style.textAlign = 'left';
      math.style.position = 'relative';
      math.style.left = '0px';
      math.style.transform = '';
      math.style.transformOrigin = 'left center';
      const hostRect = host.getBoundingClientRect();
      const fittedRect = math.getBoundingClientRect();
      const finalRightLimit = hostRect.right - 1;
      const availableWidth = Math.max(1, finalRightLimit - math.getBoundingClientRect().left);
      const remainingOverflow = Math.max(0, fittedRect.right - finalRightLimit, hostRect.left - fittedRect.left);
      const scale = remainingOverflow > 0.5 ? Math.min(1, availableWidth / Math.max(1, fittedRect.width)) : 1;
      math.style.transform = scale < 1 ? `scale(${scale.toFixed(4)})` : '';
    }
  }

  function layoutScrollRoot() {
    return document.scrollingElement || document.documentElement;
  }

  function layoutMaxScroll() {
    return Math.max(1, layoutScrollRoot().scrollHeight - window.innerHeight);
  }

  function layoutPageMetrics() {
    const root = layoutScrollRoot();
    const y = root.scrollTop;
    return Array.from(document.querySelectorAll('.layout-page-wrap')).map((wrap, fallbackIndex) => {
      const shell = wrap.querySelector('.layout-page-shell') || wrap;
      const rect = shell.getBoundingClientRect();
      const pageWidth = parseFloat(shell.dataset.pageWidth || wrap.dataset.pageWidth || '1');
      const pageHeight = parseFloat(shell.dataset.pageHeight || wrap.dataset.pageHeight || '1');
      return {
        index: Number(wrap.dataset.syncPageIndex || fallbackIndex),
        top: y + rect.top,
        bottom: y + rect.bottom,
        renderedWidth: Math.max(1, rect.width),
        renderedHeight: Math.max(1, rect.height),
        pageWidth,
        pageHeight,
      };
    });
  }

  function applyForcedPageMetrics(pages) {
    if (!Array.isArray(pages) || !pages.length) return false;
    const next = new Map();
    for (const page of pages) {
      const index = Number(page && page.index);
      const renderedHeight = Number(page && page.renderedHeight);
      const renderedWidth = Number(page && page.renderedWidth);
      if (!Number.isFinite(index) || renderedHeight <= 0) continue;
      next.set(index, { renderedHeight, renderedWidth });
    }
    if (!next.size) return false;
    window.__mineruForcedPageMetrics = next;
    fitLayoutPages();
    return true;
  }

  function pageSyncPayload() {
    const root = layoutScrollRoot();
    const pages = layoutPageMetrics();
    const y = root.scrollTop;
    const viewportAnchorRatio = 0.5;
    const anchorY = y + window.innerHeight * viewportAnchorRatio;
    if (!pages.length) {
      return { ratio: Math.max(0, Math.min(1, y / layoutMaxScroll())) };
    }
    let best = pages[0];
    let bestDistance = Infinity;
    for (const page of pages) {
      if (anchorY >= page.top && anchorY <= page.bottom) {
        best = page;
        break;
      }
      const distance = Math.min(Math.abs(page.top - anchorY), Math.abs(page.bottom - anchorY));
      if (distance < bestDistance) {
        best = page;
        bestDistance = distance;
      }
    }
    const pageOffsetPx = anchorY - best.top;
    return {
      layoutPage: best.index,
      pageOffsetPx,
      pageOffsetRatio: Math.max(0, Math.min(1, pageOffsetPx / best.renderedHeight)),
      viewportAnchorRatio,
      ratio: Math.max(0, Math.min(1, y / layoutMaxScroll())),
      pages: pages.map((page) => ({
        index: page.index,
        pageWidth: page.pageWidth,
        pageHeight: page.pageHeight,
        renderedWidth: page.renderedWidth,
        renderedHeight: page.renderedHeight,
      })),
    };
  }

  function scrollToPagePayload(payload, smooth) {
    if (!payload) return false;
    applyForcedPageMetrics(payload.pages);
    const pages = layoutPageMetrics();
    const root = layoutScrollRoot();
    if (!pages.length || payload.layoutPage === undefined) return false;
    const requestedIndex = Number(payload.layoutPage) || 0;
    const page = pages.find((candidate) => candidate.index === requestedIndex) || pages[Math.max(0, Math.min(pages.length - 1, requestedIndex))];
    if (!page) return false;
    const offsetRatio = Math.max(0, Math.min(1, Number(payload.pageOffsetRatio || 0)));
    const anchorRatio = Math.max(0, Math.min(1, Number(payload.viewportAnchorRatio ?? 0.5)));
    const nextTop = Math.max(0, Math.min(layoutMaxScroll(), page.top + page.renderedHeight * offsetRatio - window.innerHeight * anchorRatio));
    window.__mineruProgrammaticScrollUntil = Date.now() + 120;
    window.scrollTo({ top: nextTop, behavior: smooth ? 'smooth' : 'auto' });
    return true;
  }

  window.syncScrollApi = {
    scrollRatio() {
      return Math.max(0, Math.min(1, layoutScrollRoot().scrollTop / layoutMaxScroll()));
    },
    scrollToRatio(ratio, smooth) {
      const top = Math.max(0, Math.min(layoutMaxScroll(), Number(ratio || 0) * layoutMaxScroll()));
      window.__mineruProgrammaticScrollUntil = Date.now() + 120;
      window.scrollTo({ top, behavior: smooth ? 'smooth' : 'auto' });
      return true;
    },
    syncPayload: pageSyncPayload,
    scrollToSyncPayload: scrollToPagePayload,
    selectedText() {
      return String(window.getSelection ? window.getSelection() : '');
    }
  };
  window.__mineruFitLayoutPages = fitLayoutPages;
  window.__mineruFitLayoutEquations = fitLayoutEquations;

  function initLayoutImageLightbox() {
    let box = document.getElementById('layout-image-lightbox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'layout-image-lightbox';
      box.innerHTML = '<img alt=""><div class="hint">滚轮缩放 · 左键拖动 · 单击退出</div>';
      document.body.appendChild(box);
    }
    // Mark the generated page's viewer so the UI compatibility injector does
    // not attach a second, competing set of drag handlers.
    box.dataset.runtimeBound = '1';
    const image = box.querySelector('img');
    if (!image) return;
    image.draggable = false;
    image.addEventListener('dragstart', (event) => event.preventDefault());
    let scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0, moved = false;
    const apply = () => { image.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; };
    const reset = () => { scale = 1; tx = 0; ty = 0; moved = false; apply(); };
    const stopDrag = () => { dragging = false; box.style.cursor = 'grab'; };
    const close = () => { stopDrag(); box.classList.remove('open'); image.removeAttribute('src'); };

    for (const node of document.querySelectorAll('img.layout-media')) {
      if (node.dataset.layoutLightboxBound) continue;
      node.dataset.layoutLightboxBound = '1';
      node.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        image.src = node.currentSrc || node.src;
        box.classList.add('open');
        reset();
      });
    }
    // `moved` only suppresses the click generated by the same drag gesture.
    // Reset it for every new press, including presses on the backdrop; without
    // this, a previous drag made all later backdrop clicks unable to close.
    box.addEventListener('click', () => { if (!moved) close(); });
    box.addEventListener('wheel', (event) => {
      event.preventDefault();
      const previous = scale;
      const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      scale = Math.max(0.2, Math.min(8, scale * factor));
      const rect = image.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      tx -= px * (scale / previous - 1);
      ty -= py * (scale / previous - 1);
      apply();
    }, { passive: false });
    box.addEventListener('pointerdown', (event) => {
      moved = false;
      if (event.target !== image) return;
      dragging = true;
      sx = event.clientX - tx;
      sy = event.clientY - ty;
      box.setPointerCapture(event.pointerId);
    });
    box.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const nextTx = event.clientX - sx;
      const nextTy = event.clientY - sy;
      if (Math.abs(nextTx - tx) + Math.abs(nextTy - ty) > 3) moved = true;
      tx = nextTx;
      ty = nextTy;
      apply();
    });
    box.addEventListener('pointerup', stopDrag);
    box.addEventListener('pointercancel', stopDrag);
  }

  function initLayoutFormulaInteractions() {
    if (window.__mineruFormulaInteractionsInstalled) return;
    window.__mineruFormulaInteractionsInstalled = true;
    let activeFormula = null;
    let bridge = null;
    const pendingBridgePayloads = [];

    const connectBridge = () => {
      if (bridge || !window.qt || !window.qt.webChannelTransport || !window.QWebChannel) return;
      try {
        new QWebChannel(window.qt.webChannelTransport, (channel) => {
          bridge = channel.objects.layoutFormulaBridge || null;
          if (!bridge) return;
          while (pendingBridgePayloads.length) bridge.askFormula(pendingBridgePayloads.shift());
        });
      } catch (_error) {}
    };
    connectBridge();

    const formulaPayload = (container) => {
      if (!container) return null;
      let item = null;
      try {
        const mathDocument = window.MathJax && window.MathJax.startup && window.MathJax.startup.document;
        const items = mathDocument && mathDocument.getMathItemsWithin
          ? mathDocument.getMathItemsWithin(container)
          : [];
        item = items && items.length ? items[0] : null;
      } catch (_error) {}
      const tex = String(item && item.math ? item.math : container.getAttribute('aria-label') || '').trim();
      if (!tex) return null;
      const savedAnchor = container.__mineruReferenceAnchor || null;
      const page = container.closest('.layout-page-wrap');
      const block = container.closest('.layout-flow-stream, .layout-block');
      const pageRect = page ? page.getBoundingClientRect() : null;
      const formulaRect = container.getBoundingClientRect();
      return {
        type: 'formula',
        tex,
        display: Boolean(item && item.display),
        page: page ? Number(page.dataset.syncPageIndex || 0) + 1 : (savedAnchor ? savedAnchor.page : null),
        anchor_page: page ? Number(page.dataset.syncPageIndex || 0) + 1 : (savedAnchor ? savedAnchor.page : null),
        anchor_ratio: pageRect && pageRect.height ? Math.max(0, Math.min(1, (formulaRect.top - pageRect.top) / pageRect.height)) : (savedAnchor ? savedAnchor.ratio : null),
        anchor_rect: pageRect && pageRect.width && pageRect.height ? {
          x: Math.max(0, Math.min(1, (formulaRect.left - pageRect.left) / pageRect.width)),
          y: Math.max(0, Math.min(1, (formulaRect.top - pageRect.top) / pageRect.height)),
          width: Math.max(0.01, Math.min(1, formulaRect.width / pageRect.width)),
          height: Math.max(0.01, Math.min(1, formulaRect.height / pageRect.height))
        } : (savedAnchor ? savedAnchor.rect : null),
        anchor_point: savedAnchor ? savedAnchor.point || null : null,
        blockType: block ? String(block.dataset.flowKind || block.dataset.styleKind || '') : '',
      };
    };

    const askFormula = (container) => {
      const payload = formulaPayload(container);
      if (!payload) return;
      const serialized = JSON.stringify(payload);
      window.__mineruLastFormulaQuote = payload;
      connectBridge();
      if (bridge && bridge.askFormula) bridge.askFormula(serialized);
      else pendingBridgePayloads.push(serialized);
    };

    let box = document.getElementById('layout-formula-lightbox');
    if (!box) {
      box = document.createElement('div');
      box.id = 'layout-formula-lightbox';
      box.innerHTML = '<div class="formula-stage"></div><div class="hint">滚轮缩放 · 左键拖动 · 单击退出</div>';
      document.body.appendChild(box);
    }
    // New previews already contain the current interaction implementation;
    // old retained previews receive a runtime compatibility shim in OT_ui.
    box.dataset.formulaViewerVersion = '2';
    const stage = box.querySelector('.formula-stage');
    let placeholder = null;
    let originalStyle = '';
    let scale = 2.2, tx = 0, ty = 0, dragging = false, moved = false, sx = 0, sy = 0;
    const apply = () => { if (stage) stage.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`; };
    const restoreFormula = () => {
      if (activeFormula && placeholder && placeholder.parentNode) {
        placeholder.replaceWith(activeFormula);
        activeFormula.style.cssText = originalStyle;
      }
      activeFormula = null;
      placeholder = null;
    };
    const close = () => {
      dragging = false;
      box.classList.remove('open');
      restoreFormula();
      if (stage) { stage.style.transform = ''; stage.textContent = ''; }
    };
    const open = (container) => {
      if (!container || !stage || box.classList.contains('open')) return;
      const rect = container.getBoundingClientRect();
      const sourcePage = container.closest('.layout-page-wrap');
      const sourcePageRect = sourcePage ? sourcePage.getBoundingClientRect() : null;
      if (sourcePage && sourcePageRect && sourcePageRect.width && sourcePageRect.height) {
        container.__mineruReferenceAnchor = {
          page: Number(sourcePage.dataset.syncPageIndex || 0) + 1,
          ratio: Math.max(0, Math.min(1, (rect.top - sourcePageRect.top) / sourcePageRect.height)),
          rect: {
            x: Math.max(0, Math.min(1, (rect.left - sourcePageRect.left) / sourcePageRect.width)),
            y: Math.max(0, Math.min(1, (rect.top - sourcePageRect.top) / sourcePageRect.height)),
            width: Math.max(0.01, Math.min(1, rect.width / sourcePageRect.width)),
            height: Math.max(0.01, Math.min(1, rect.height / sourcePageRect.height))
          }
        };
      }
      placeholder = document.createElement(container.tagName.toLowerCase() === 'mjx-container' ? 'span' : 'div');
      placeholder.className = 'layout-formula-placeholder';
      placeholder.style.width = `${Math.max(1, rect.width)}px`;
      placeholder.style.height = `${Math.max(1, rect.height)}px`;
      container.before(placeholder);
      activeFormula = container;
      originalStyle = container.style.cssText;
      stage.appendChild(container);
      container.style.transform = '';
      container.style.position = 'relative';
      container.style.left = '0';
      // Fit the first view inside the viewport.  The old minimum of 1.5 made
      // long equations overflow before the reader had a chance to zoom out.
      scale = Math.max(0.2, Math.min(4, Math.min(
        (window.innerWidth * 0.86) / Math.max(1, rect.width),
        (window.innerHeight * 0.76) / Math.max(1, rect.height)
      )));
      tx = 0; ty = 0; moved = false; apply();
      box.classList.add('open');
    };

    // 从 MathJax 右键菜单提问时，公式往往已被移入放大层。
    // 将放大层中的右键落点按公式自身比例映射回原页面，保存的是用户实际
    // 指向的位置，而不是笼统的公式容器中心。
    document.addEventListener('contextmenu', (event) => {
      const container = event.target && event.target.closest ? event.target.closest('mjx-container') : null;
      const anchor = container && container.__mineruReferenceAnchor;
      if (!container || !anchor || !anchor.rect) return;
      const rect = container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const localX = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      const localY = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
      anchor.point = {
        x: Math.max(0, Math.min(1, Number(anchor.rect.x || 0) + localX * Number(anchor.rect.width || 0))),
        y: Math.max(0, Math.min(1, Number(anchor.rect.y || 0) + localY * Number(anchor.rect.height || 0)))
      };
    }, true);

    document.addEventListener('click', (event) => {
      const container = event.target && event.target.closest ? event.target.closest('mjx-container') : null;
      if (!container || container.closest('#layout-formula-lightbox') || event.ctrlKey || event.metaKey || event.shiftKey) return;
      event.preventDefault();
      event.stopPropagation();
      open(container);
    }, true);
    box.addEventListener('click', () => { if (!moved) close(); });
    box.addEventListener('wheel', (event) => {
      event.preventDefault();
      const previous = scale;
      scale = Math.max(0.4, Math.min(10, scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
      tx -= (event.clientX - window.innerWidth / 2 - tx) * (scale / previous - 1);
      ty -= (event.clientY - window.innerHeight / 2 - ty) * (scale / previous - 1);
      apply();
    }, { passive: false });
    box.addEventListener('pointerdown', (event) => {
      moved = false;
      if (!stage || !stage.contains(event.target)) return;
      dragging = true; sx = event.clientX - tx; sy = event.clientY - ty;
      box.setPointerCapture(event.pointerId);
    });
    box.addEventListener('pointermove', (event) => {
      if (!dragging) return;
      const nextX = event.clientX - sx, nextY = event.clientY - sy;
      if (Math.abs(nextX - tx) + Math.abs(nextY - ty) > 3) moved = true;
      tx = nextX; ty = nextY; apply();
    });
    box.addEventListener('pointerup', () => { dragging = false; });
    box.addEventListener('pointercancel', () => { dragging = false; });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && box.classList.contains('open')) close();
    });

    const appendAskAiItem = (menu) => {
      if (!menu || menu.querySelector('[data-mineru-ask-formula]') || !activeFormula) return;
      const isTopLevelMathMenu = Array.from(menu.children).some(
        (child) => String(child.textContent || '').trim() === 'MathJax Help'
      );
      if (!isTopLevelMathMenu) return;
      const rule = document.createElement('div');
      rule.className = 'CtxtMenu_MenuItem CtxtMenu_MenuRule';
      rule.setAttribute('role', 'separator');
      rule.setAttribute('aria-orientation', 'vertical');
      rule.dataset.mineruAskFormula = 'rule';
      const item = document.createElement('div');
      item.className = 'CtxtMenu_MenuItem';
      item.setAttribute('role', 'menuitem');
      item.setAttribute('aria-disabled', 'false');
      item.dataset.mineruAskFormula = '1';
      item.textContent = '提问';
      item.addEventListener('mousedown', (event) => { event.preventDefault(); event.stopPropagation(); });
      item.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        askFormula(activeFormula);
        menu.remove();
      });
      menu.appendChild(rule);
      menu.appendChild(item);
    };
    const menuObserver = new MutationObserver(() => {
      for (const menu of document.querySelectorAll('.CtxtMenu_ContextMenu[role="menu"]')) appendAskAiItem(menu);
    });
    menuObserver.observe(document.body, { childList: true, subtree: true });
    document.addEventListener('contextmenu', (event) => {
      const container = event.target && event.target.closest ? event.target.closest('mjx-container') : null;
      if (!container) return;
      activeFormula = container;
      window.setTimeout(() => {
        for (const menu of document.querySelectorAll('.CtxtMenu_ContextMenu[role="menu"]')) appendAskAiItem(menu);
      }, 0);
    }, true);
  }

  let activeFitPages = null;
  const fitCacheVersion = document.body ? document.body.dataset.layoutCacheVersion : '';
  // Short single-column transitions first inherit the final shared body font.
  // If their own source band still collides in the final audit, only that
  // transition may reduce its font; it never changes the shared body font.
  const ALLOW_INHERITED_BODY_FONT_BACKOFF = true;
  let bodyIterationInspectionRound = 0;

  // 正文迭代检查逻辑：记录正文在每次候选样式探测中首次触发的碰撞，
  // 并在整轮排版结束后将碰撞轮次、最终字号和行距显示在正文框上。
  function isBodyIterationNode(node) {
    return Boolean(node && node.matches && node.matches(
      '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])'
    ));
  }

  function bodyIterationNodes(nodes) {
    return (nodes || []).filter(isBodyIterationNode);
  }

  // Native `title` popups are useful while inspecting the fitting algorithm,
  // but they leak internal convergence details into the normal reading view.
  // Keep the diagnostic data attributes in both modes for the fit/cache logic;
  // expose them to the browser only when the user explicitly opens debug mode.
  function setDiagnosticTitle(node, text) {
    if (!node) return;
    node.title = document.body.classList.contains('layout-debug') ? (text || '') : '';
  }

  function resetBodyIterationInspection(nodes) {
    for (const node of bodyIterationNodes(nodes)) {
      node.classList.remove('body-iteration-collision');
      node.dataset.bodyIterationCollisionRound = '';
      node.dataset.bodyIterationCollisionPhase = '';
      node.dataset.bodyIterationLastRound = '0';
    }
  }

  function beginBodyIterationProbe(nodes, phase) {
    const bodyNodes = bodyIterationNodes(nodes);
    if (!bodyNodes.length) return null;
    const round = ++bodyIterationInspectionRound;
    for (const node of bodyNodes) {
      node.dataset.bodyIterationLastRound = String(round);
    }
    return { round, phase };
  }

  function recordBodyIterationCollision(collision, probe) {
    if (!probe || !collision || !isBodyIterationNode(collision.source)) return;
    const source = collision.source;
    if (!source.dataset.bodyIterationCollisionRound) {
      source.dataset.bodyIterationCollisionRound = String(probe.round);
      source.dataset.bodyIterationCollisionPhase = probe.phase;
    }
    source.classList.add('body-iteration-collision');
  }

  function publishBodyIterationInspection(nodes) {
    const bodyNodes = bodyIterationNodes(nodes);
    const globalLimiter = bodyNodes.find((node) => node.dataset.bodyIterationCollisionRound);
    const globalLimiterRound = globalLimiter ? globalLimiter.dataset.bodyIterationCollisionRound : '';
    const globalLimiterPhase = globalLimiter ? globalLimiter.dataset.bodyIterationCollisionPhase : '';
    for (const node of bodyNodes) {
      const fontSize = parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0;
      const lineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '0') || 0;
      const collisionRound = node.dataset.bodyIterationCollisionRound;
      const lastRound = node.dataset.bodyIterationLastRound || '0';
      const globalText = globalLimiterRound
        ? `全文已收敛 R${lastRound} · 全局限制 R${globalLimiterRound}（${globalLimiterPhase || 'probe'}）`
        : `全文已收敛 R${lastRound} · 未发现碰撞`;
      const localText = collisionRound ? '当前框为限制源' : '当前框无碰撞';
      node.dataset.fitLabel = `正文迭代 · ${globalText} · ${localText} · 字号 ${fontSize.toFixed(2)}px · 行距 ${lineRatio.toFixed(3)}`;
      node.dataset.fitDebug = node.dataset.fitLabel;
      setDiagnosticTitle(node, node.dataset.fitLabel);
    }
  }

  function publishCachedBodyIterationInspection() {
    const nodes = Array.from(document.querySelectorAll(
      '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])'
    ));
    const bodyNodes = bodyIterationNodes(nodes);
    const globalLimiter = bodyNodes.find((node) => node.dataset.bodyIterationCollisionRound);
    const globalText = globalLimiter
      ? `全文已收敛（完整缓存） · 全局限制 R${globalLimiter.dataset.bodyIterationCollisionRound || '?'}（${globalLimiter.dataset.bodyIterationCollisionPhase || 'probe'}）`
      : '全文已收敛（完整缓存）';
    for (const node of bodyNodes) {
      const fontSize = parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0;
      const lineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '0') || 0;
      const localText = node === globalLimiter ? '当前框为限制源' : '当前框无碰撞';
      const label = `正文迭代 · ${globalText} · ${localText} · 字号 ${fontSize.toFixed(2)}px · 行距 ${lineRatio.toFixed(3)}`;
      node.dataset.fitLabel = label;
      node.dataset.fitDebug = label;
      setDiagnosticTitle(node, label);
    }
  }

  function scopedNodes(selector) {
    const nodes = Array.from(document.querySelectorAll(selector));
    if (!activeFitPages) return nodes;
    return nodes.filter((node) => activeFitPages.has(node.closest('.layout-page-wrap')));
  }

  function fitCacheKey() {
    const fingerprint = document.body ? document.body.dataset.layoutCacheKey : '';
    const scope = document.body ? document.body.dataset.layoutCacheScope : '';
    if (!fingerprint || !scope) return '';
    return `${fitCacheVersion}:${scope}:${fingerprint}`;
  }

  function fitCacheNodes() {
    return Array.from(document.querySelectorAll('.layout-flow-stream, .layout-block'));
  }

  function restoreFitCache() {
    const key = fitCacheKey();
    if (!key) return false;
    try {
      const warm = window.__mineruInitialFitCache;
      const payload = warm && warm.key === key
        ? warm.payload
        : JSON.parse(localStorage.getItem(key) || 'null');
      const nodes = fitCacheNodes();
      const completeStyles = Array.isArray(payload && payload.styles)
        && payload.styles.length === nodes.length
        && payload.styles.every((value) => value && typeof value === 'object'
          && Object.prototype.hasOwnProperty.call(value, 'f')
          && Object.prototype.hasOwnProperty.call(value, 'l')
          && Object.prototype.hasOwnProperty.call(value, 'o'));
      if (!payload || payload.version !== fitCacheVersion || payload.complete !== true
          || payload.count !== nodes.length || !completeStyles) return false;
      for (let index = 0; index < nodes.length; index += 1) {
        const value = payload.styles[index];
        if (!value) continue;
        const node = nodes[index];
        if (value.f) node.style.fontSize = value.f;
        if (value.l) node.style.lineHeight = value.l;
        if (value.o) node.dataset.originalLines = value.o;
        if (value.n === '1' && value.w) {
          node.style.width = value.w;
          node.style.whiteSpace = 'nowrap';
          node.dataset.shortTitleNoWrap = '1';
        }
      }
      const inspection = payload.bodyInspection || {};
      const cachedRound = String(inspection.lastRound || '0');
      for (const node of bodyIterationNodes(nodes)) node.dataset.bodyIterationLastRound = cachedRound;
      const limiterIndex = Number(inspection.limiterIndex);
      const limiter = Number.isInteger(limiterIndex) ? nodes[limiterIndex] : null;
      if (isBodyIterationNode(limiter)) {
        limiter.dataset.bodyIterationCollisionRound = String(inspection.limiterRound || '?');
        limiter.dataset.bodyIterationCollisionPhase = String(inspection.limiterPhase || 'probe');
        limiter.classList.add('body-iteration-collision');
      }
      document.body.dataset.layoutFitCached = '1';
      publishCachedBodyIterationInspection();
      return true;
    } catch (_error) {
      return false;
    }
  }

  function saveFitCache() {
    const key = fitCacheKey();
    if (!key) return;
    try {
      const nodes = fitCacheNodes();
      const styles = nodes.map((node) => ({
        f: node.style.fontSize || '',
        l: node.style.lineHeight || '',
        o: node.dataset.originalLines || '',
        n: node.dataset.shortTitleNoWrap === '1' ? '1' : '',
        w: node.dataset.shortTitleNoWrap === '1' ? (node.style.width || '') : '',
      }));
      const bodyNodes = bodyIterationNodes(nodes);
      const limiter = bodyNodes.find((node) => node.dataset.bodyIterationCollisionRound);
      const bodyInspection = {
        lastRound: Math.max(0, ...bodyNodes.map((node) => Number(node.dataset.bodyIterationLastRound || 0))),
        limiterIndex: limiter ? nodes.indexOf(limiter) : -1,
        limiterRound: limiter ? String(limiter.dataset.bodyIterationCollisionRound || '') : '',
        limiterPhase: limiter ? String(limiter.dataset.bodyIterationCollisionPhase || '') : '',
      };
      const scope = document.body ? document.body.dataset.layoutCacheScope : '';
      const stalePrefix = scope ? `${fitCacheVersion}:${scope}:` : '';
      if (stalePrefix) {
        const staleKeys = [];
        for (let index = 0; index < localStorage.length; index += 1) {
          const candidate = localStorage.key(index) || '';
          if (candidate !== key && candidate.startsWith(stalePrefix)) staleKeys.push(candidate);
        }
        for (const staleKey of staleKeys) localStorage.removeItem(staleKey);
      }
      localStorage.setItem(key, JSON.stringify({
        version: fitCacheVersion,
        complete: true,
        count: nodes.length,
        styles,
        bodyInspection
      }));
    } catch (_error) {
      // file:// storage availability and quota vary by WebEngine version.
    }
  }

  function measureTextBand(el) {
    const hostRect = el.getBoundingClientRect();
    const scaleY = hostRect.height > 0 && el.offsetHeight > 0 ? hostRect.height / el.offsetHeight : 1;
    const walker = document.createTreeWalker(
      el,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (!node.textContent || !node.textContent.trim()) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          if (parent.closest(
            '.mjx-assistive-mml, .layout-line-debug-box, .layout-collision-debug-layer, [aria-hidden="true"]'
          )) return NodeFilter.FILTER_REJECT;
          const style = getComputedStyle(parent);
          return style.display !== 'none' && style.visibility !== 'hidden'
            ? NodeFilter.FILTER_ACCEPT
            : NodeFilter.FILTER_REJECT;
        }
      }
    );
    const range = document.createRange();
    let firstTop = null;
    let lastBottom = 0;
    let hasText = false;
    let node;
    while ((node = walker.nextNode())) {
      range.selectNodeContents(node);
      const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0.5 && rect.height > 0.5);
      for (const rect of rects) {
        const top = (rect.top - hostRect.top) / scaleY;
        const bottom = (rect.bottom - hostRect.top) / scaleY;
        if (firstTop === null || top < firstTop) firstTop = top;
        if (bottom > lastBottom) lastBottom = bottom;
        hasText = true;
      }
    }
    return {
      hasText,
      firstTop: firstTop ?? 0,
      lastBottom,
    };
  }

  function textRectsInPage(el) {
    if (window.__layoutPerfEnabled) {
      const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
      c.textRectsCalls = (c.textRectsCalls || 0) + 1;
    }
    const page = el.closest('.layout-page');
    if (!page) return [];
    const pageRect = page.getBoundingClientRect();
    const scaleX = page.offsetWidth > 0 ? pageRect.width / page.offsetWidth : 1;
    const scaleY = page.offsetHeight > 0 ? pageRect.height / page.offsetHeight : 1;
    const walker = document.createTreeWalker(
      el,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          if (!node.textContent || !node.textContent.trim()) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          if (parent.closest(
            '.mjx-assistive-mml, .layout-line-debug-box, .layout-collision-debug-layer, [aria-hidden="true"]'
          )) return NodeFilter.FILTER_REJECT;
          const style = getComputedStyle(parent);
          return style.display !== 'none' && style.visibility !== 'hidden'
            ? NodeFilter.FILTER_ACCEPT
            : NodeFilter.FILTER_REJECT;
        }
      }
    );
    const range = document.createRange();
    const rects = [];
    let node;
    while ((node = walker.nextNode())) {
      range.selectNodeContents(node);
      for (const rect of Array.from(range.getClientRects())) {
        if (rect.width <= 0.5 || rect.height <= 0.5) continue;
        rects.push({
          left: (rect.left - pageRect.left) / scaleX,
          top: (rect.top - pageRect.top) / scaleY,
          right: (rect.right - pageRect.left) / scaleX,
          bottom: (rect.bottom - pageRect.top) / scaleY,
        });
      }
    }
    return rects;
  }

  function viewportRectInPage(page, rect) {
    const pageRect = page.getBoundingClientRect();
    const scaleX = page.offsetWidth > 0 ? pageRect.width / page.offsetWidth : 1;
    const scaleY = page.offsetHeight > 0 ? pageRect.height / page.offsetHeight : 1;
    return {
      left: (rect.left - pageRect.left) / scaleX,
      top: (rect.top - pageRect.top) / scaleY,
      right: (rect.right - pageRect.left) / scaleX,
      bottom: (rect.bottom - pageRect.top) / scaleY,
    };
  }

  // 正文专用的内容几何：文字取每个可见行的 Range rect；公式取 MathJax
  // 完成排版后的容器；图片和表格取实际元素的渲染矩形。没有可测内容时回退到块框。
  function renderedContentRectsInPage(el) {
    const page = el && el.closest ? el.closest('.layout-page') : null;
    if (!page) return [];
    const rects = textRectsInPage(el);
    const visualNodes = el.querySelectorAll
      ? el.querySelectorAll('mjx-container, img, table, svg, canvas')
      : [];
    const seen = new Set();
    for (const node of visualNodes) {
      if (!node || seen.has(node)) continue;
      seen.add(node);
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0.5 || rect.height <= 0.5) continue;
      rects.push(viewportRectInPage(page, rect));
    }
    return rects.length ? rects : [elementBoxInPage(el)];
  }

  function elementBoxInPage(el) {
    return {
      left: el.offsetLeft,
      top: el.offsetTop,
      right: el.offsetLeft + el.offsetWidth,
      bottom: el.offsetTop + el.offsetHeight,
    };
  }

  function singleLineTextExceedsPage(node, tolerance = 1.5) {
    const page = node.closest('.layout-page');
    if (!page) return false;
    const pageWidth = page.offsetWidth || 0;
    if (pageWidth <= 0) return false;
    const textRects = textRectsInPage(node);
    if (!textRects.length) return false;
    return textRects.some((rect) => rect.left < -tolerance || rect.right > pageWidth + tolerance);
  }

  function demoteFalseSingleLineText() {
    const selector = [
      '.layout-flow-stream.debug-text[data-flow-kind="text"][data-original-lines="single"]',
      '.layout-block.type-text[data-original-lines="single"]',
    ].join(', ');
    for (const node of scopedNodes(selector)) {
      if (!singleLineTextExceedsPage(node)) continue;
      node.dataset.originalLines = 'multi';
      node.dataset.singleLineAlign = 'left';
      node.dataset.fitLabel = node.dataset.fitLabel || 'DEMOTED single->multi';
      node.dataset.fitDebug = [
        'single line exceeded page bounds',
        `self=${blockDebugName(node)}`,
      ].join(' ');
      setDiagnosticTitle(node, node.dataset.fitDebug);
    }
  }

  function rectsOverlap(a, b, padding) {
    return a.left < b.right - padding &&
      a.right > b.left + padding &&
      a.top < b.bottom - padding &&
      a.bottom > b.top + padding;
  }

  // A cheap, conservative broad-phase envelope for a set of rendered-content
  // rectangles.  It may admit whitespace between lines, but can never reject a
  // real glyph collision; candidates still go through the exact rect test.
  function rectUnion(rects) {
    if (!rects || !rects.length) return null;
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const rect of rects) {
      if (!rect) continue;
      left = Math.min(left, rect.left);
      top = Math.min(top, rect.top);
      right = Math.max(right, rect.right);
      bottom = Math.max(bottom, rect.bottom);
    }
    return Number.isFinite(left) ? { left, top, right, bottom } : null;
  }

  function horizontalBoxesOverlap(a, b, padding = 0) {
    return a.left < b.right - padding && a.right > b.left + padding;
  }

  function blockDebugName(el) {
    if (!el) return 'unknown';
    const role = el.dataset.styleKind || el.dataset.flowKind || '';
    const classes = Array.from(el.classList || []).filter((name) => (
      name.startsWith('debug-') || name.startsWith('type-') || name === 'from-list' || name === 'refs'
    ));
    const box = elementBoxInPage(el);
    const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 42);
    return [
      role || 'block',
      classes.join('.'),
      `@${Math.round(box.left)},${Math.round(box.top)},${Math.round(box.right - box.left)}x${Math.round(box.bottom - box.top)}`,
      text
    ].filter(Boolean).join(' ');
  }

  function textCollisionDetails(nodes, options = {}) {
    const __tCd = window.__layoutPerfEnabled ? performance.now() : 0;
    if (window.__layoutPerfEnabled) {
      const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
      c.collisionProbes = (c.collisionProbes || 0) + 1;
      c.collisionSourceNodes = (c.collisionSourceNodes || 0) + nodes.length;
    }
    const nodeSet = new Set(nodes);
    const renderedRectCache = new Map();
    const includeGroupPeers = Boolean(options.includeGroupPeers);
    const ignoreTopOverflow = Boolean(options.ignoreTopOverflow);
    const checkAllTextForCollisions = Boolean(options.checkAllTextForCollisions);
    const avoidPageOverflow = Boolean(options.avoidPageOverflow);
    const bodyColumnIndependentFit = Boolean(options.bodyColumnIndependentFit);
    for (const node of nodes) {
      const page = node.closest('.layout-page');
      if (!page) continue;
      const own = elementBoxInPage(node);
      const sharedEdgeTolerance = Number.isFinite(options.sharedEdgeTolerance)
        ? options.sharedEdgeTolerance
        : 0;
      const sharedHorizontalEdgeTolerance = Number.isFinite(options.sharedHorizontalEdgeTolerance)
        ? options.sharedHorizontalEdgeTolerance
        : 0;
      const ignoreNodeTopOverflow = ignoreTopOverflow || (
        Boolean(options.ignoreBodyTopOverflow) && node.dataset.styleKind === 'body_text'
      );
      const sourceIsBodyText = node.dataset.styleKind === 'body_text';
      const __tSrc = window.__layoutPerfEnabled ? performance.now() : 0;
      // 每个文本源都逐行检测自身实际文字。区别仅在障碍物：正文迭代以实际
      // 内容为障碍；其它文本迭代以布局边框为障碍。
      const sourceRects = textRectsInPage(node).filter((rect) => {
        return checkAllTextForCollisions || rect.bottom > own.bottom + 1 ||
          (!ignoreNodeTopOverflow && rect.top < own.top - 1) ||
          rect.left < own.left - 1 ||
          rect.right > own.right + 1;
      });
      if (window.__layoutPerfEnabled) __perfMark('probe_sourceRects', __tSrc);
      if (!sourceRects.length) continue;
      const __tBar = window.__layoutPerfEnabled ? performance.now() : 0;
      if (window.__layoutPerfEnabled) {
        const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
        c.barrierQueries = (c.barrierQueries || 0) + 1;
      }
      const barriers = Array.from(page.querySelectorAll('.layout-flow-stream, .layout-block'))
        .filter((candidate) => candidate !== node && (includeGroupPeers || !nodeSet.has(candidate)))
        .map((element) => {
          const box = elementBoxInPage(element);
          // 正文迭代：正文实际文字对所有块的实际内容；其它文本迭代：自己的
          // 实际文字对所有块的边框。这样正文不受空白边框的保守限制，而题注、
          // 标题等仍以稳定的布局边框作为外部约束。
          const barrierUsesTextGeometry = Boolean(options.bodyTextCollisionGeometry) &&
            sourceIsBodyText;
          const contentRects = barrierUsesTextGeometry
            ? (renderedRectCache.get(element) || (() => {
              const rects = renderedContentRectsInPage(element);
              const geometry = { rects, bounds: rectUnion(rects) || box };
              renderedRectCache.set(element, geometry);
              return geometry;
            })())
            : { rects: [box], bounds: box };
          return { element, box, contentRects: contentRects.rects, contentBounds: contentRects.bounds };
        });
      if (window.__layoutPerfEnabled) __perfMark('probe_barriers', __tBar);
      for (const rect of sourceRects) {
        if (avoidPageOverflow && (
          rect.left < -1.5 || rect.top < -1.5 ||
          rect.right > page.offsetWidth + 1.5 || rect.bottom > page.offsetHeight + 1.5
        )) {
          return {
            source: node,
            blocker: null,
            rect,
            sourceName: blockDebugName(node),
            blockerName: 'page-boundary',
          };
        }
        const hit = barriers.find((barrier) => {
          // 正文的字号填充只受同列（或原始框已相互侵入）的块约束。
          // 并列栏即使处于同一高度，也不应互相压低字号；页面左右越界仍由
          // avoidPageOverflow 单独保护。这里按源框投影而非文字墨迹判断，故仍能
          // 捕获错误扩宽的正文框和向下增长压到同列块的情形。
          if (bodyColumnIndependentFit && node.dataset.styleKind === 'body_text' &&
              !horizontalBoxesOverlap(own, barrier.box, 1.5)) {
            return false;
          }
          // Broad phase: a union of the barrier's actual rendered rects avoids
          // scanning every line/glyph rectangle for distant text.  Do not use
          // the layout block box here: visible content may legitimately extend
          // beyond it, and that overflow must remain detectable.
          if (!rectsOverlap(rect, barrier.contentBounds, 1.5)) return false;
          const contentHit = barrier.contentRects.some((contentRect) => rectsOverlap(rect, contentRect, 1.5));
          if (!contentHit) return false;
          // Adjacent source bboxes can overlap by a rounding pixel. Ignore only
          // that upper shared edge for body text; horizontal and lower-edge
          // collisions from the same first line are still enforced.
          if (ignoreNodeTopOverflow && rect.top < own.top && barrier.box.bottom <= own.top + 1.5) {
            return false;
          }
          // Inline math and justified CJK glyphs can overhang a column edge by
          // a few pixels even though the line box itself remains in-column.
          // Treat only a shallow overhang across an exactly shared vertical
          // edge as optical ink; any deeper intrusion remains a collision.
          const sharesRightEdge = Math.abs(own.right - barrier.box.left) <= 1.5;
          const sharesLeftEdge = Math.abs(own.left - barrier.box.right) <= 1.5;
          if (sharedEdgeTolerance > 0 && (
            (sharesRightEdge && rect.right <= own.right + sharedEdgeTolerance) ||
            (sharesLeftEdge && rect.left >= own.left - sharedEdgeTolerance)
          )) {
            return false;
          }
          const sharesBottomEdge = Math.abs(own.bottom - barrier.box.top) <= 1.5;
          const sharesTopEdge = Math.abs(own.top - barrier.box.bottom) <= 1.5;
          if (sharedHorizontalEdgeTolerance > 0 && (
            (sharesBottomEdge && rect.bottom <= own.bottom + sharedHorizontalEdgeTolerance) ||
            (sharesTopEdge && rect.top >= own.top - sharedHorizontalEdgeTolerance)
          )) {
            return false;
          }
          return true;
        });
        if (hit) {
          if (window.__layoutPerfEnabled) __perfMark('collisionTotal', __tCd);
          return {
            source: node,
            blocker: hit.element,
            rect,
            sourceName: blockDebugName(node),
            blockerName: blockDebugName(hit.element),
          };
        }
      }
    }
    if (window.__layoutPerfEnabled) __perfMark('collisionTotal', __tCd);
    return null;
  }

  function applyGroup(nodes, fontSize, lineRatio) {
    if (window.__layoutPerfEnabled) {
      const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
      c.applyGroupCalls = (c.applyGroupCalls || 0) + 1;
    }
    for (const node of nodes) {
      if (!node || !node.style) continue;
      node.style.fontSize = `${fontSize.toFixed(2)}px`;
      node.style.lineHeight = lineRatio.toFixed(3);
    }
  }

  function measureGroup(nodes) {
    const __tMg = window.__layoutPerfEnabled ? performance.now() : 0;
    if (window.__layoutPerfEnabled) {
      const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
      c.measureGroupCalls = (c.measureGroupCalls || 0) + 1;
    }
    let overflow = false;
    let allReachedBand = true;
    let maxBottomGap = 0;
    let details = [];
    for (const node of nodes) {
      if (!node || !node.style) continue;
      const pageHeight = parseFloat(node.dataset.pageHeight || "792");
      const fitBandRatio = parseFloat(node.dataset.fitBandRatio || "");
      const band = Number.isFinite(fitBandRatio)
        ? Math.max(1.0, node.clientHeight * fitBandRatio)
        : pageHeight * 0.02;
      const metrics = measureTextBand(node);
      const bottomGap = Math.max(0, node.clientHeight - metrics.lastBottom);
      const overflowAmount = Math.max(
        0,
        metrics.lastBottom - node.clientHeight - 1.5,
        node.dataset.styleKind === 'body_text' ? 0 : -metrics.firstTop - 1.5
      );
      maxBottomGap = Math.max(maxBottomGap, bottomGap);
      if (overflowAmount > 0.5) {
        overflow = true;
      }
      if (!metrics.hasText || bottomGap > band) {
        allReachedBand = false;
      }
      details.push({
        node,
        hasText: metrics.hasText,
        bottomGap,
        band,
        overflowAmount,
        reachedBand: metrics.hasText && bottomGap <= band,
      });
    }
    if (window.__layoutPerfEnabled) __perfMark('measureGroup', __tMg);
    return { overflow, allReachedBand, maxBottomGap, details };
  }

  function measureAt(nodes, fontSize, lineRatio) {
    applyGroup(nodes, fontSize, lineRatio);
    return measureGroup(nodes);
  }

  function wouldCollideWithBlocks(nodes, options) {
    if (!options.avoidBlockOverlap) return false;
    return Boolean(textCollisionDetails(nodes, options));
  }

  function clearFitMarks(nodes) {
    for (const node of nodes) {
      if (!node || !node.classList) continue;
      node.classList.remove('fit-limiter');
      node.classList.remove('fit-blocker');
      node.dataset.fitLabel = '';
      node.dataset.fitDebug = '';
      setDiagnosticTitle(node, '');
    }
  }

  function ensureCollisionDebugLayer(page) {
    let layer = page.querySelector(':scope > .layout-collision-debug-layer');
    if (layer) return layer;
    layer = document.createElement('div');
    layer.className = 'layout-collision-debug-layer';
    layer.setAttribute('aria-hidden', 'true');
    page.appendChild(layer);
    return layer;
  }

  function clearCollisionDebugLayer(page) {
    const layer = page && page.querySelector(':scope > .layout-collision-debug-layer');
    if (layer) layer.replaceChildren();
  }

  function clearAllCollisionDebugLayers() {
    for (const page of document.querySelectorAll('.layout-page')) {
      clearCollisionDebugLayer(page);
    }
  }

  function drawCollisionDebug(collision) {
    if (!collision || !collision.source || !collision.blocker) return;
    if (!document.body.classList.contains('layout-debug')) return;
    const page = collision.source.closest('.layout-page');
    if (!page) return;
    const layer = ensureCollisionDebugLayer(page);
    const rect = collision.rect;
    const blockerBox = elementBoxInPage(collision.blocker);
    const sourceBox = elementBoxInPage(collision.source);
    const hitBox = document.createElement('div');
    hitBox.className = 'layout-collision-debug-box';
    hitBox.dataset.debugLabel = 'TEXT HIT';
    hitBox.style.left = `${rect.left.toFixed(2)}px`;
    hitBox.style.top = `${rect.top.toFixed(2)}px`;
    hitBox.style.width = `${Math.max(1, rect.right - rect.left).toFixed(2)}px`;
    hitBox.style.height = `${Math.max(1, rect.bottom - rect.top).toFixed(2)}px`;
    hitBox.title = `text overflow hit blocker\nsource=${collision.sourceName}\nblocker=${collision.blockerName}`;
    layer.appendChild(hitBox);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('layout-collision-debug-line');
    svg.setAttribute('viewBox', `0 0 ${Math.max(1, page.offsetWidth)} ${Math.max(1, page.offsetHeight)}`);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', ((sourceBox.left + sourceBox.right) / 2).toFixed(2));
    line.setAttribute('y1', ((sourceBox.top + sourceBox.bottom) / 2).toFixed(2));
    line.setAttribute('x2', ((blockerBox.left + blockerBox.right) / 2).toFixed(2));
    line.setAttribute('y2', ((blockerBox.top + blockerBox.bottom) / 2).toFixed(2));
    line.setAttribute('stroke', 'rgba(185, 28, 28, 0.95)');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-dasharray', '5 3');
    svg.appendChild(line);
    layer.appendChild(svg);
  }

  function markLimiter(nodes, fontSize, lineRatio, options, stopReason) {
    const finalState = measureAt(nodes, fontSize, lineRatio);
    const collision = options.avoidBlockOverlap ? textCollisionDetails(nodes, options) : null;
    const nextFontState = fontSize + options.step <= options.maxFont
      ? measureAt(nodes, fontSize + options.step, lineRatio)
      : null;
    const nextLineState = lineRatio + options.lineStep <= options.maxLineRatio
      ? measureAt(nodes, fontSize, lineRatio + options.lineStep)
      : null;
    applyGroup(nodes, fontSize, lineRatio);
    const fontLimiters = new Set(
      (nextFontState?.details || [])
        .filter((detail) => detail.overflowAmount > 0.5)
        .map((detail) => detail.node)
    );
    const lineLimiters = new Set(
      (nextLineState?.details || [])
        .filter((detail) => detail.overflowAmount > 0.5)
        .map((detail) => detail.node)
    );
    const candidates = finalState.details
      .slice()
      .sort((a, b) => {
        const aNextOverflow = (fontLimiters.has(a.node) ? 1 : 0) + (lineLimiters.has(a.node) ? 1 : 0);
        const bNextOverflow = (fontLimiters.has(b.node) ? 1 : 0) + (lineLimiters.has(b.node) ? 1 : 0);
        if (aNextOverflow !== bNextOverflow) return bNextOverflow - aNextOverflow;
        return a.bottomGap - b.bottomGap;
      });
    const limiter = candidates.find((detail) => fontLimiters.has(detail.node) || lineLimiters.has(detail.node))
      || (collision ? candidates.find((detail) => detail.node === collision.source) : null)
      || candidates.find((detail) => !detail.reachedBand)
      || candidates[0];
    for (const detail of finalState.details) {
      const gap = detail.bottomGap.toFixed(1);
      const band = detail.band.toFixed(1);
      const overflow = detail.overflowAmount.toFixed(1);
      const isLimiter = limiter && detail.node === limiter.node;
      if (detail.node.classList) detail.node.classList.toggle('fit-limiter', isLimiter);
      const labelPrefix = options.labelPrefix || 'LIMIT';
      const labelReason = collision && isLimiter ? `${labelPrefix} ${stopReason} hit` : `${labelPrefix} ${stopReason} gap ${gap}`;
      detail.node.dataset.fitLabel = (isLimiter || options.markAll) ? labelReason : '';
      detail.node.dataset.fitDebug = [
        `font=${fontSize.toFixed(2)}`,
        `line=${lineRatio.toFixed(3)}`,
        `gap=${gap}`,
        `band=${band}`,
        `overflow=${overflow}`,
        `stop=${stopReason}`,
        `self=${blockDebugName(detail.node)}`,
        collision ? `collisionSource=${collision.sourceName}` : '',
        collision ? `collisionBlocker=${collision.blockerName}` : '',
        `nextFontOverflow=${nextFontState ? fontLimiters.has(detail.node) : 'max'}`,
        `nextLineOverflow=${nextLineState ? lineLimiters.has(detail.node) : 'max'}`
      ].filter(Boolean).join(' ');
      setDiagnosticTitle(detail.node, detail.node.dataset.fitDebug);
    }
    if (collision && collision.blocker && document.body.classList.contains('layout-debug')) {
      collision.blocker.classList.add('fit-blocker');
      collision.blocker.dataset.fitLabel = 'BLOCKER';
      collision.blocker.dataset.fitDebug = [
        'collision blocker',
        `source=${collision.sourceName}`,
        `blocker=${collision.blockerName}`,
      ].join(' ');
      setDiagnosticTitle(collision.blocker, collision.blocker.dataset.fitDebug);
      drawCollisionDebug(collision);
    }
  }

  function tuneGroup(selector, options) {
    const nodes = scopedNodes(selector);
    if (!nodes.length) return;
    tuneNodes(nodes, options);
    if (options.continueUnderfilledNodes) {
      continueUnderfilledNodes(nodes, options);
    }
  }

  // Short transitions in a stable single-column prose lane must follow the
  // final shared body font. They deliberately stay out of tuneGroup: a
  // one-line derivation can be much narrower than its paragraph lane and
  // must not shrink the body font for the rest of the document. The final
  // collision audit owns any necessary, source-local backoff afterwards.
  function syncInheritedBodyFontToBodyGroup() {
    const bodyNodes = scopedNodes(
      '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])'
    );
    const inheritedNodes = scopedNodes(
      '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"][data-body-inherited="1"]'
    );
    if (!bodyNodes.length || !inheritedNodes.length) return;
    const fontSize = Math.min(...bodyNodes
      .map((node) => parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0)
      .filter((value) => Number.isFinite(value) && value > 0));
    if (!Number.isFinite(fontSize) || fontSize <= 0) return;
    for (const node of inheritedNodes) {
      const lineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '1.1') || 1.1;
      applyGroup([node], fontSize, lineRatio);
    }
  }

  function tuneEach(selector, options) {
    const nodes = scopedNodes(selector);
    for (const node of nodes) {
      tuneNodes([node], options);
    }
  }

  function titleFrameFill(node) {
    const own = elementBoxInPage(node);
    const ownWidth = Math.max(1, own.right - own.left);
    const ownHeight = Math.max(1, own.bottom - own.top);
    const ink = rectUnion(textRectsInPage(node));
    if (!ink) return { area: 0, width: 0, height: 0 };
    // Only count the portion that occupies the original source frame.  A
    // translated title may legitimately overhang that frame, but the purpose
    // of this post-pass is to detect titles that leave most of it unused.
    const usedWidth = Math.max(0, Math.min(own.right, ink.right) - Math.max(own.left, ink.left));
    const usedHeight = Math.max(0, Math.min(own.bottom, ink.bottom) - Math.max(own.top, ink.top));
    const width = Math.min(1, usedWidth / ownWidth);
    const height = Math.min(1, usedHeight / ownHeight);
    return { area: width * height, width, height };
  }

  // The normal title pass deliberately uses one shared scale for visual
  // consistency.  A small title box can therefore become the group's limiter.
  // After that stable pass, let only titles that visibly under-use their own
  // source frame grow independently.  This is an exception path, not the
  // default title policy.
  function expandUnderfilledTitles(selector, options) {
    const areaThreshold = Number.isFinite(options.titleFillAreaThreshold)
      ? options.titleFillAreaThreshold
      : 0.42;
    const dimensionThreshold = Number.isFinite(options.titleFillDimensionThreshold)
      ? options.titleFillDimensionThreshold
      : 0.72;
    for (const node of scopedNodes(selector)) {
      const initialFill = titleFrameFill(node);
      if (initialFill.area >= areaThreshold || (
        initialFill.width >= dimensionThreshold && initialFill.height >= dimensionThreshold
      )) continue;

      let fontSize = parseFloat(node.style.fontSize || node.dataset.baseFont || '8') || 8;
      const lineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '1.12') || 1.12;
      for (let safety = 0; safety < 136 && fontSize + options.step <= options.maxFont; safety += 1) {
        const nextFont = fontSize + options.step;
        applyGroup([node], nextFont, lineRatio);
        const collision = textCollisionDetails([node], options);
        if (collision) {
          applyGroup([node], fontSize, lineRatio);
          break;
        }
        fontSize = nextFont;
        const fill = titleFrameFill(node);
        if (fill.area >= areaThreshold || (
          fill.width >= dimensionThreshold && fill.height >= dimensionThreshold
        )) break;
      }
    }
  }

  // Independent fill recovery can leave two headings that are visually the
  // same level a fraction of a font size apart.  Cluster only near-neighbours
  // (a cluster may span at most one px from its smallest member), then use the
  // smaller size.  Reducing text cannot create a new collision.
  function clusterTitleFontSizes(selector, maxDifference = 1.0) {
    const nodes = scopedNodes(selector)
      .filter((node) => node && node.style)
      .map((node) => ({
        node,
        fontSize: parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0,
      }))
      .filter((entry) => entry.fontSize > 0)
      .sort((left, right) => left.fontSize - right.fontSize);
    let cluster = [];
    let clusterMinimum = 0;
    const applyCluster = () => {
      if (cluster.length < 2) return;
      for (const entry of cluster) {
        entry.node.style.fontSize = `${clusterMinimum.toFixed(2)}px`;
      }
    };
    for (const entry of nodes) {
      if (!cluster.length || entry.fontSize - clusterMinimum <= maxDifference + 0.001) {
        cluster.push(entry);
        if (cluster.length === 1) clusterMinimum = entry.fontSize;
        continue;
      }
      applyCluster();
      cluster = [entry];
      clusterMinimum = entry.fontSize;
    }
    applyCluster();
  }

  function renderedTextLineCount(node, topTolerance = 1.5) {
    const tops = [];
    const rects = textRectsInPage(node).sort((left, right) => (
      left.top - right.top || left.left - right.left
    ));
    for (const rect of rects) {
      if (!tops.some((top) => Math.abs(top - rect.top) <= topTolerance)) {
        tops.push(rect.top);
      }
    }
    return tops.length;
  }

  // MinerU title bboxes are often tight ink boxes rather than the available
  // heading lane. A compact English heading can therefore become a two-line
  // CJK heading by only a few pixels after translation. Repair that narrow
  // case after shared title sizing: use the browser's actual rendered lines,
  // not MinerU's occasionally unreliable single-line label, and retain the
  // no-wrap result only when its small rightward borrow is collision-free.
  function keepShortTitlesOnOneLine(selector, options = {}) {
    const maxCharacters = Number.isFinite(options.maxCharacters) ? options.maxCharacters : 12;
    const maxBorrowPx = Number.isFinite(options.maxBorrowPx) ? options.maxBorrowPx : 18;
    const maxWidthRatio = Number.isFinite(options.maxWidthRatio) ? options.maxWidthRatio : 1.35;
    for (const node of scopedNodes(selector)) {
      const text = (node.innerText || '').replace(/\\s+/g, ' ').trim();
      if (Array.from(text).length < 2 || Array.from(text).length > maxCharacters) continue;
      if (renderedTextLineCount(node) <= 1) continue;

      const originalWidth = node.style.width;
      const originalWhiteSpace = node.style.whiteSpace;
      const own = elementBoxInPage(node);
      const ownWidth = Math.max(1, own.right - own.left);
      node.style.whiteSpace = 'nowrap';

      const nowrapRects = textRectsInPage(node);
      const ink = rectUnion(nowrapRects);
      if (!ink || renderedTextLineCount(node) !== 1) {
        node.style.width = originalWidth;
        node.style.whiteSpace = originalWhiteSpace;
        continue;
      }
      const requiredWidth = Math.max(ownWidth, ink.right - own.left + 0.75);
      const borrowedWidth = requiredWidth - ownWidth;
      if (ink.left < own.left - 1.5 || borrowedWidth > maxBorrowPx || requiredWidth / ownWidth > maxWidthRatio) {
        node.style.width = originalWidth;
        node.style.whiteSpace = originalWhiteSpace;
        continue;
      }

      node.style.width = `${requiredWidth.toFixed(2)}px`;
      const collision = textCollisionDetails([node], {
        avoidBlockOverlap: true,
        avoidPageOverflow: true,
        checkAllTextForCollisions: true,
      });
      if (collision) {
        node.style.width = originalWidth;
        node.style.whiteSpace = originalWhiteSpace;
        continue;
      }
      node.dataset.shortTitleNoWrap = '1';
    }
  }

  // Monotone collision-constrained growth: find the largest value in
  // [start, max] (stepped by `step`) at which `collides(value)` is still false.
  // The linear scan probes every tick; this gallops up to bracket the first
  // colliding tick, then bisects, cutting a ~25-probe climb to ~log2 probes.
  //
  // Chromium line-breaking makes collision only *weakly* monotone in font size:
  // a larger size almost always collides at least as much, but a reflow can
  // shift a break by one tick. After bisecting we therefore re-probe a small
  // neighbourhood below the boundary and keep the largest non-colliding tick,
  // reproducing what the linear scan would have stopped at. `collides` must
  // leave the node styled at the probed value on return (callers re-apply the
  // chosen value afterwards).
  function gallopingGrow(start, max, step, collides) {
    if (start >= max) return { value: start, probes: 0 };
    const ticks = Math.floor((max - start) / step + 1e-6);
    if (ticks <= 0) return { value: start, probes: 0 };
    const at = (t) => Math.min(max, start + t * step);
    let probes = 0;
    // Start tick 0 is assumed feasible (caller applied it and it held); gallop
    // over the colliding boundary with doubling jumps.
    let lastOk = 0;
    let firstBad = -1;
    let jump = 1;
    while (lastOk + jump <= ticks) {
      const t = lastOk + jump;
      probes += 1;
      if (collides(at(t))) { firstBad = t; break; }
      lastOk = t;
      jump *= 2;
    }
    // A doubling jump can pass the final tick (for example 1, 3, 7, 15 on a
    // 23-tick search).  In that case the old code returned max without ever
    // testing the remaining interval, allowing a real collision near max to
    // bypass the solver entirely.  Probe the endpoint to complete the
    // bracket; the ordinary bisection below then finds the safe predecessor.
    if (firstBad === -1 && lastOk < ticks) {
      probes += 1;
      if (collides(at(ticks))) firstBad = ticks;
      else lastOk = ticks;
    }
    if (firstBad === -1) {
      // No collision up to max: the top tick is feasible.
      return { value: at(ticks), probes, lastProbed: at(ticks) };
    }
    // Bisect the open interval (lastOk, firstBad).
    let lo = lastOk;
    let hi = firstBad;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      probes += 1;
      if (collides(at(mid))) hi = mid; else lo = mid;
    }
    // Neighbourhood re-probe above lo to recover a tick a reflow made feasible
    // just past the bisected boundary (weak-monotonicity guard). One tick has
    // matched the linear scan's stop point on every measured fixture; keep it
    // minimal so an early boundary stays as cheap as the linear scan.
    let best = lo;
    for (let t = lo + 1; t <= Math.min(ticks, lo + 1); t += 1) {
      probes += 1;
      if (collides(at(t))) break;
      best = t;
    }
    return { value: at(best), probes, lastProbed: at(Math.min(ticks, best + 1)) };
  }

  function tuneNodes(nodes, options) {
    if (!nodes.length) return null;
    if (window.__layoutPerfEnabled) {
      const c = (window.__layoutPerfCounters = window.__layoutPerfCounters || {});
      c.tuneNodesCalls = (c.tuneNodesCalls || 0) + 1;
    }
    clearFitMarks(nodes);
    const baseFont = Math.max(...nodes.map((node) => parseFloat(node.dataset.baseFont || "8")));
    const baseLineRatio = Math.max(...nodes.map((node) => parseFloat(node.dataset.lineRatio || "1.1")));
    let fontSize = baseFont;
    let lineRatio = baseLineRatio;
    const minFont = Number.isFinite(options.minFont) ? options.minFont : Math.max(4.8, fontSize * 0.55);
    const minLineRatio = Number.isFinite(options.minLineRatio) ? options.minLineRatio : 1.0;
    if (options.coupleFontAndLine) {
      lineRatio = Math.max(minLineRatio, Math.min(options.maxLineRatio, lineRatio));
    }
    if (options.startFromMinimum) {
      fontSize = minFont;
      lineRatio = minLineRatio;
    }
    let stopReason = 'unknown';
    let lastCollision = null;
    applyGroup(nodes, fontSize, lineRatio);
    let safety = 0;
    while (safety < 120 && (!options.allowOverflow || options.enforceInitialCollisionBackoff)) {
      safety += 1;
      // measureGroup forces a synchronous reflow and a full glyph walk per node.
      // Its result here is consumed only through state.overflow, which cannot
      // block when overflow is allowed. Skip the measurement in that case so a
      // grow loop that permits overflow no longer pays for an unused reflow.
      const state = !options.allowOverflow ? measureGroup(nodes) : null;
      const initialProbe = beginBodyIterationProbe(nodes, 'initial-backoff');
      const collision = options.enforceInitialCollisionBackoff
        ? wouldCollideWithBlocks(nodes, options)
        : null;
      recordBodyIterationCollision(collision, initialProbe);
      const blockingOverflow = Boolean(state && state.overflow) && !options.allowOverflow;
      if (!blockingOverflow && !collision) break;
      let changed = false;
      if (!options.coupleFontAndLine && lineRatio > minLineRatio) {
        lineRatio = Math.max(minLineRatio, lineRatio - options.lineStep);
        changed = true;
      } else if (fontSize > minFont) {
        fontSize = Math.max(minFont, fontSize - options.step);
        changed = true;
      }
      applyGroup(nodes, fontSize, lineRatio);
      if (!changed) {
        stopReason = collision ? 'min-block-overlap' : 'min-overflow';
        break;
      }
    }
    safety = 0;
    // The current-state measurement is read only for overflow (inert when
    // overflow is allowed) and for the fill-stop test (inert when the group
    // never stops on fill, e.g. overflow-first body/title/generic text). Skip
    // it whenever neither consumer can act on it.
    const needFillState = options.stopWhenFilled !== false;
    // Purely collision-constrained growth (generic per-node text): the stop
    // condition is a monotone "first colliding tick", with no overflow or fill
    // early-out that would need per-tick measurement. Bracket-and-bisect that
    // boundary instead of probing every tick.
    //
    // Restricted to SINGLE-node groups with a wide remaining range. Multi-node
    // groups (titles/lists tuned with includeGroupPeers) share one scale that
    // usually collides just above baseFont, so a linear scan stops in a few
    // probes; galloping there would instead probe far-larger shared sizes
    // (title maxFont is 28px) and re-scan every peer at each, costing more. The
    // large single-node generic-text fleet on long papers is the real target.
    const remainingTicks = (options.maxFont - fontSize) / options.step;
    // enforceInitialCollisionBackoff marks groups (titles) whose fit is anchored
    // by a specific first-collision tick found incrementally; bisecting can skip
    // it and overshoot toward maxFont. Only the plain generic-text fleet — no
    // initial backoff, single node, wide range — is safe to gallop.
    const monotoneFontSearch = window.__gallopDisabled !== true &&
      options.allowOverflow && !needFillState &&
      options.avoidBlockOverlap && !options.coupleFontAndLine &&
      !options.enforceInitialCollisionBackoff &&
      nodes.length === 1 && remainingTicks >= 8;
    const __tLoop2 = window.__layoutPerfEnabled ? performance.now() : 0;
    if (monotoneFontSearch && fontSize < options.maxFont) {
      const grown = gallopingGrow(fontSize, options.maxFont, options.step, (value) => {
        applyGroup(nodes, value, lineRatio);
        beginBodyIterationProbe(nodes, 'font-grow');
        return Boolean(textCollisionDetails(nodes, options));
      });
      fontSize = grown.value;
      applyGroup(nodes, fontSize, lineRatio);
      stopReason = fontSize >= options.maxFont - 1e-6 ? 'max-font' : 'block-overlap';
    } else {
      while (safety < 80) {
        safety += 1;
        const state = (!options.allowOverflow || needFillState) ? measureGroup(nodes) : null;
        if (state && state.overflow && !options.allowOverflow) {
          stopReason = 'overflow';
          break;
        }
        if (needFillState && state && state.allReachedBand) {
          stopReason = 'filled';
          break;
        }
        const nextFont = fontSize + options.step;
        if (nextFont > options.maxFont) {
          stopReason = 'max-font';
          break;
        }
        applyGroup(nodes, nextFont, lineRatio);
        // nextState only guards the overflow break; unused when overflow is allowed.
        const nextState = !options.allowOverflow ? measureGroup(nodes) : null;
        const fontProbe = beginBodyIterationProbe(nodes, 'font-grow');
        const collision = options.avoidBlockOverlap ? textCollisionDetails(nodes, options) : null;
        recordBodyIterationCollision(collision, fontProbe);
        if (collision) {
          applyGroup(nodes, fontSize, lineRatio);
          stopReason = 'block-overlap';
          lastCollision = collision;
          break;
        }
        if (nextState && nextState.overflow && !options.allowOverflow) {
          applyGroup(nodes, fontSize, lineRatio);
          stopReason = 'font-overflow';
          break;
        }
        fontSize = nextFont;
      }
    }
    if (window.__layoutPerfEnabled) __perfMark('loop2Font', __tLoop2);
    safety = 0;
    const __tLoop3 = window.__layoutPerfEnabled ? performance.now() : 0;
    while (!options.coupleFontAndLine && safety < 80 && !(options.skipLineExpansionAfterFontCollision && stopReason === 'block-overlap')) {
      safety += 1;
      const state = (!options.allowOverflow || needFillState) ? measureGroup(nodes) : null;
      if ((state && state.overflow && !options.allowOverflow) || (needFillState && state && state.allReachedBand)) {
        stopReason = state && state.overflow && !options.allowOverflow ? 'overflow' : 'filled';
        break;
      }
      const nextRatio = lineRatio + options.lineStep;
      if (nextRatio > options.maxLineRatio) {
        stopReason = 'max-line';
        break;
      }
      applyGroup(nodes, fontSize, nextRatio);
      const nextState = !options.allowOverflow ? measureGroup(nodes) : null;
      const lineProbe = beginBodyIterationProbe(nodes, 'line-grow');
      const collision = options.avoidBlockOverlap ? textCollisionDetails(nodes, options) : null;
      recordBodyIterationCollision(collision, lineProbe);
      if (collision) {
        applyGroup(nodes, fontSize, lineRatio);
        stopReason = 'block-overlap';
        lastCollision = collision;
        break;
      }
      if (nextState && nextState.overflow && !options.allowOverflow) {
        applyGroup(nodes, fontSize, lineRatio);
        stopReason = 'line-overflow';
        break;
      }
      lineRatio = nextRatio;
    }
    if (window.__layoutPerfEnabled) __perfMark('loop3Line', __tLoop3);
    if (options.showLimiter) {
      markLimiter(nodes, fontSize, lineRatio, options, stopReason);
    }
    return { fontSize, lineRatio, stopReason, collision: lastCollision };
  }

  // 正文二次迭代必须始终保持统一字号：所有正文同步增大字号，
  // 发生碰撞时只压缩碰撞源的行距；如果行距降到下限仍无法消除碰撞，
  // 则撤销本轮所有正文的字号增长并结束二次迭代，绝不单独缩小某个正文块。
  function continueUnderfilledNodes(nodes, options) {
    const targetFill = Number.isFinite(options.minTextFillRatio)
      ? options.minTextFillRatio
      : 0.85;
    const bodyNodes = (nodes || []).filter((node) => node && node.style);
    const fillRatio = (node) => {
      const metrics = measureTextBand(node);
      if (!metrics.hasText || node.clientHeight <= 0) return 0;
      return Math.min(1, Math.max(0, metrics.lastBottom) / node.clientHeight);
    };
    if (!bodyNodes.length || !bodyNodes.some((node) => fillRatio(node) < targetFill)) return;

    const minLineRatio = Number.isFinite(options.collisionMinLineRatio)
      ? options.collisionMinLineRatio
      : 1.02;

    const snapshotStyles = () => bodyNodes.map((node) => ({
      node,
      fontSize: parseFloat(node.style.fontSize || node.dataset.baseFont || '8') || 8,
      lineRatio: parseFloat(node.style.lineHeight || node.dataset.lineRatio || '1.1') || 1.1,
    }));

    const restoreStyles = (snapshots) => {
      for (const snapshot of snapshots) {
        applyGroup([snapshot.node], snapshot.fontSize, snapshot.lineRatio);
      }
    };

    const markUniformFontStop = (source, collision, restoredFont) => {
      if (!document.body.classList.contains('layout-debug') || !source || !source.classList) return;
      source.classList.add('fit-limiter');
      source.dataset.fitLabel = 'STOP 统一字号';
      source.dataset.fitDebug = [
        '正文统一字号二次迭代停止',
        'reason=line-backoff-exhausted',
        `font=${restoredFont.toFixed(2)}`,
        `line=${parseFloat(source.style.lineHeight || '0').toFixed(3)}`,
        `fill=${(fillRatio(source) * 100).toFixed(1)}%`,
        collision ? `blocker=${collision.blockerName}` : '',
        `self=${blockDebugName(source)}`,
      ].filter(Boolean).join(' ');
      setDiagnosticTitle(source, source.dataset.fitDebug);
    };

    const recoverCollisionByLineRatio = (source, initialCollision) => {
      let collision = initialCollision;
      const fontSize = parseFloat(source.style.fontSize || source.dataset.baseFont || '8') || 8;
      let lineRatio = parseFloat(source.style.lineHeight || source.dataset.lineRatio || '1.1') || 1.1;
      const sourceMinLineRatio = minLineRatio;

      // 正文碰撞只允许降低碰撞源自己的行距，最低保持 1.02 倍字号；字号
      // 仍属于全文共享状态，禁止任何正文块单独缩小字号。
      for (let safety = 0; collision && safety < 80 && lineRatio > sourceMinLineRatio + 0.001; safety += 1) {
        lineRatio = Math.max(sourceMinLineRatio, lineRatio - options.lineStep);
        applyGroup([source], fontSize, lineRatio);
        const recoveryProbe = beginBodyIterationProbe([source], 'collision-line-backoff');
        collision = textCollisionDetails([source], options);
        recordBodyIterationCollision(collision, recoveryProbe);
      }
      return { collision, fontSize, lineRatio };
    };

    for (let safety = 0; safety < 320; safety += 1) {
      if (!bodyNodes.some((node) => fillRatio(node) < targetFill)) return;

      const snapshots = snapshotStyles();
      const currentFont = Math.min(...snapshots.map((snapshot) => snapshot.fontSize));
      const nextFont = currentFont + options.step;
      if (!Number.isFinite(nextFont) || nextFont > options.maxFont + 0.0001) return;

      // 每个正文块保留自己的行距，但所有正文统一应用同一个候选字号。
      for (const snapshot of snapshots) {
        applyGroup([snapshot.node], nextFont, snapshot.lineRatio);
      }

      const sharedProbe = beginBodyIterationProbe(bodyNodes, 'shared-font-grow');
      let collision = options.avoidBlockOverlap ? textCollisionDetails(bodyNodes, options) : null;
      recordBodyIterationCollision(collision, sharedProbe);

      while (collision) {
        const initialCollision = collision;
        const source = collision.source;
        const recovery = recoverCollisionByLineRatio(source, collision);

        if (initialCollision.blocker && document.body.classList.contains('layout-debug')) {
          drawCollisionDebug(initialCollision);
        }

        if (recovery.collision) {
          // 行距已经降到可读下限仍发生碰撞：撤销整组本轮增长并终止，保持正文统一字号。
          restoreStyles(snapshots);
          markUniformFontStop(source, recovery.collision, currentFont);
          return;
        }

        const retryProbe = beginBodyIterationProbe(bodyNodes, 'shared-recheck');
        collision = options.avoidBlockOverlap
          ? textCollisionDetails(bodyNodes, options)
          : null;
        recordBodyIterationCollision(collision, retryProbe);
      }
    }
  }

  function clampTranslatedOverflow() {
    if (!document.body.classList.contains('layout-translated')) return;
    for (const node of scopedNodes('.layout-flow-stream[data-flow-kind="ref_text"]')) {
      if (!node || !node.style) continue;
      let fontSize = parseFloat(node.style.fontSize || node.dataset.baseFont || "8") || 8;
      let lineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || "1.1") || 1.1;
      const isRef = node.dataset.flowKind === "ref_text";
      const minFont = isRef ? 4.8 : 5.0;
      const minLineRatio = isRef ? 0.98 : 1.0;
      applyGroup([node], fontSize, lineRatio);
      for (let i = 0; i < 120 && node.scrollHeight > node.clientHeight + 1 && (fontSize > minFont || lineRatio > minLineRatio); i += 1) {
        if (lineRatio > minLineRatio) {
          lineRatio = Math.max(minLineRatio, lineRatio - 0.025);
        } else {
          fontSize = Math.max(minFont, fontSize - 0.25);
        }
        applyGroup([node], fontSize, lineRatio);
      }
      node.dataset.baseFont = fontSize.toFixed(2);
      node.dataset.lineRatio = lineRatio.toFixed(3);
    }
  }

  // Code blocks are isolated containers with a fixed source bbox.  A translated
  // or rewrapped code sample can therefore overflow its own <pre> even though
  // it cannot collide with the prose below.  Fit the container internally:
  // tighten line-height first, then reduce font-size.  Only an exceptionally
  // dense block falls back to scrolling, so no content is silently discarded.
  function clampTranslatedCodeOverflow() {
    if (!document.body.classList.contains('layout-translated')) return false;
    let changed = false;
    for (const node of scopedNodes('.layout-block.type-code')) {
      if (!node || !node.style) continue;
      const code = node.querySelector(':scope > .layout-code') || node.querySelector('.layout-code');
      if (!code) continue;
      const minFont = 7.0;
      const minLineRatio = 1.10;
      const requestedFont = parseFloat(node.style.fontSize || node.dataset.baseFont || '10') || 10;
      const requestedLineRatio = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '1.18') || 1.18;
      let fontSize = Math.max(minFont, requestedFont);
      let lineRatio = Math.max(minLineRatio, requestedLineRatio);
      if (fontSize !== requestedFont || lineRatio !== requestedLineRatio) changed = true;
      code.style.overflow = 'hidden';
      applyGroup([node], fontSize, lineRatio);
      for (let i = 0; i < 160 && code.scrollHeight > code.clientHeight + 1 && (lineRatio > minLineRatio + 0.001 || fontSize > minFont + 0.001); i += 1) {
        if (lineRatio > minLineRatio + 0.001) {
          lineRatio = Math.max(minLineRatio, lineRatio - 0.025);
        } else {
          fontSize = Math.max(minFont, fontSize - 0.25);
        }
        applyGroup([node], fontSize, lineRatio);
        changed = true;
      }
      if (code.scrollHeight > code.clientHeight + 1) {
        // Keep the complete sample reachable when even the readable minimum
        // cannot fit inside the original PDF bbox.
        code.style.overflow = 'auto';
        node.dataset.codeFit = 'scroll';
      } else {
        node.dataset.codeFit = 'fit';
      }
      node.dataset.baseFont = fontSize.toFixed(2);
      node.dataset.lineRatio = lineRatio.toFixed(3);
    }
    return changed;
  }

  // All groups are tuned independently, so perform one final glyph-level
  // audit after every style mutation. Unlike the iteration probes, this checks
  // every visible glyph against every layout box, including cross-group cases
  // such as body text beside references. Only a detected source is backed off.
  function enforceFinalTextCollisionSafety() {
    const nodes = scopedNodes(
      '.layout-flow-stream[data-flow-kind="text"], .layout-flow-stream[data-flow-kind="ref_text"]'
    ).filter((node) => node && node.style);
    const exhausted = new Set();
    const options = {
      avoidBlockOverlap: true,
      avoidPageOverflow: true,
      includeGroupPeers: true,
      checkAllTextForCollisions: true,
      ignoreTopOverflow: false,
      ignoreBodyTopOverflow: true,
      bodyColumnIndependentFit: true,
      bodyTextCollisionGeometry: true,
      sharedEdgeTolerance: 4.0,
      sharedHorizontalEdgeTolerance: 3.0,
    };
    for (let safety = 0; safety < 3000; safety += 1) {
      const candidates = nodes.filter((node) => !exhausted.has(node));
      if (!candidates.length) return;
      const finalProbe = beginBodyIterationProbe(candidates, 'final-safety-audit');
      const collision = textCollisionDetails(candidates, options);
      recordBodyIterationCollision(collision, finalProbe);
      if (!collision) return;
      const source = collision.source;
      let fontSize = parseFloat(source.style.fontSize || source.dataset.baseFont || '8') || 8;
      let lineRatio = parseFloat(source.style.lineHeight || source.dataset.lineRatio || '1.1') || 1.1;
      const minFont = source.dataset.flowKind === 'ref_text' ? 4.8 : 4.8;
      // Short single-column transitions inherit the body baseline but are not
      // global anchors.  They may locally back off in this final safety pass
      // if translation genuinely cannot fit their source band.
      const isInheritedBodyText = source.dataset.styleKind === 'body_text'
        && source.dataset.bodyInherited === '1';
      const isBodyText = source.dataset.styleKind === 'body_text'
        && (!isInheritedBodyText || !ALLOW_INHERITED_BODY_FONT_BACKOFF);
      const minLineRatio = isBodyText ? 1.02 : 0.98;
      if (ALLOW_INHERITED_BODY_FONT_BACKOFF && isInheritedBodyText && fontSize > minFont + 0.001) {
        // The shared body font is restored first. If this one narrow
        // transition cannot fit, only this source may give back font size.
        fontSize = Math.max(minFont, fontSize - 0.25);
      } else if (lineRatio > minLineRatio + 0.001) {
        lineRatio = Math.max(minLineRatio, lineRatio - 0.025);
      } else if (isBodyText) {
        // 最终安全检查同样禁止单独缩小正文。行距到底仍冲突时放弃处理该正文块，
        // 避免最后一道检查重新破坏二次迭代已经保证的统一字号。
        exhausted.add(source);
        continue;
      } else if (fontSize > minFont + 0.001) {
        fontSize = Math.max(minFont, fontSize - 0.25);
      } else {
        exhausted.add(source);
        continue;
      }
      applyGroup([source], fontSize, lineRatio);
      if (document.body.classList.contains('layout-debug') && source.classList) {
        source.classList.add('fit-limiter');
        source.dataset.fitLabel = 'FINAL collision backoff';
        source.dataset.fitDebug = [
          'final glyph collision backoff',
          `font=${fontSize.toFixed(2)}`,
          `line=${lineRatio.toFixed(3)}`,
          `blocker=${collision.blockerName}`,
          `self=${blockDebugName(source)}`,
        ].join(' ');
        setDiagnosticTitle(source, source.dataset.fitDebug);
      }
    }
  }

  // Read-only layout auditor: reports the first real glyph collision and any
  // page overflow using the exact final-safety geometry, WITHOUT mutating any
  // style. A benchmark uses it to prove a faster solver produced a layout that
  // is still collision-free and in-bounds, independent of the solver itself.
  window.__mineruAuditLayoutOnly = () => {
    const nodes = scopedNodes(
      '.layout-flow-stream[data-flow-kind="text"], .layout-flow-stream[data-flow-kind="ref_text"]'
    ).filter((node) => node && node.style);
    const options = {
      avoidBlockOverlap: true,
      avoidPageOverflow: true,
      includeGroupPeers: true,
      checkAllTextForCollisions: true,
      ignoreTopOverflow: false,
      ignoreBodyTopOverflow: true,
      bodyColumnIndependentFit: true,
      bodyTextCollisionGeometry: true,
      sharedEdgeTolerance: 4.0,
      sharedHorizontalEdgeTolerance: 3.0,
    };
    const collision = textCollisionDetails(nodes, options);
    const bodyFonts = new Set();
    let minBodyFont = Infinity;
    let minLineRatio = Infinity;
    for (const node of nodes) {
      const font = parseFloat(node.style.fontSize || node.dataset.baseFont || '0') || 0;
      const line = parseFloat(node.style.lineHeight || node.dataset.lineRatio || '0') || 0;
      if (node.dataset.styleKind === 'body_text' && node.dataset.bodyInherited !== '1') {
        bodyFonts.add(font.toFixed(2));
        if (font > 0) minBodyFont = Math.min(minBodyFont, font);
      }
      if (line > 0) minLineRatio = Math.min(minLineRatio, line);
    }
    return {
      valid: !collision,
      firstCollision: collision ? {
        source: collision.sourceName,
        blocker: collision.blockerName,
      } : null,
      bodyFontUniform: bodyFonts.size <= 1,
      distinctBodyFonts: [...bodyFonts],
      minimumBodyFont: Number.isFinite(minBodyFont) ? minBodyFont : null,
      minimumLineRatio: Number.isFinite(minLineRatio) ? minLineRatio : null,
      textNodes: nodes.length,
    };
  };

  // Benchmark facility: when window.__layoutPerfEnabled is set, run() records
  // per-phase wall time into window.__layoutPhaseTimes so a harness can see
  // exactly which fit phase dominates. Zero cost and inert when disabled.
  function __perfMark(label, t0) {
    if (!window.__layoutPerfEnabled) return;
    const store = (window.__layoutPhaseTimes = window.__layoutPhaseTimes || {});
    store[label] = (store[label] || 0) + (performance.now() - t0);
  }
  function __perfPhase(label, fn) {
    if (!window.__layoutPerfEnabled) return fn();
    const t0 = performance.now();
    const result = fn();
    __perfMark(label, t0);
    return result;
  }

  function run(pageWraps = null, persist = false) {
    activeFitPages = pageWraps ? new Set(pageWraps) : null;
    if (window.__layoutPerfEnabled) window.__layoutPhaseTimes = {};
    const __runStart = performance.now();
    try {
      const debugMode = document.body.classList.contains('layout-debug');
      const strictSourceFit = document.body.classList.contains('layout-source-strict-fit');
      // Both parsed-source and translated reading views should start compact,
      // then fill until actual glyphs collide. The explicit strict-source view
      // remains an opt-in no-overflow diagnostic mode.
      const collisionFirstTextFit = !strictSourceFit;
      clearAllCollisionDebugLayers();
      fitLayoutPages();
      resetBodyIterationInspection(scopedNodes(
        '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])'
      ));
      demoteFalseSingleLineText();
      const titleFitOptions = {
        step: 0.25,
        minFont: 6.0,
        maxFont: 28.0,
        lineStep: 0.025,
        minLineRatio: 0.98,
        maxLineRatio: 1.35,
        // MinerU title boxes commonly describe the source ink band and are
        // only 10-12px tall.  Let translated headings extend beyond that box;
        // their actual glyphs are still checked against every other block's
        // bbox and against the page boundary below.
        allowOverflow: true,
        stopWhenFilled: false,
        avoidBlockOverlap: true,
        avoidPageOverflow: true,
        checkAllTextForCollisions: true,
        enforceInitialCollisionBackoff: true,
        showLimiter: debugMode
      };
      // A title with main-title geometry is treated as an article title, even
      // when it appears after page one (for example in a PDF containing
      // several papers).  Section/subsection titles first share one scale so
      // the document keeps a coherent heading hierarchy.
      const __tTitles = performance.now();
      tuneEach('.layout-block.type-title.main-title', titleFitOptions);
      tuneGroup('.layout-block.type-title:not(.main-title)', {
        ...titleFitOptions,
        includeGroupPeers: true
      });
      // A shared fit can be constrained by one unusually small title box.
      // Compensate only the visibly under-filled outliers, preserving the
      // shared result for all ordinary headings.
      expandUnderfilledTitles('.layout-block.type-title:not(.main-title)', {
        ...titleFitOptions,
        maxFont: 42.0,
        titleFillAreaThreshold: 0.42,
        titleFillDimensionThreshold: 0.72
      });
      // Do this after every title has completed its own recovery pass.  It
      // restores visual regularity among headings whose final sizes differ by
      // no more than one font-size unit, without collapsing truly distinct
      // title levels into a single scale.
      clusterTitleFontSizes('.layout-block.type-title', 1.0);
      keepShortTitlesOnOneLine('.layout-block.type-title:not(.main-title)', {
        maxCharacters: 12,
        maxBorrowPx: 18,
        maxWidthRatio: 1.35,
      });
      __perfMark('titles', __tTitles);
      const __tBody = performance.now();
      tuneGroup('.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])', {
        step: 0.5,
        minFont: collisionFirstTextFit ? 4.8 : undefined,
        minLineRatio: collisionFirstTextFit ? 1.12 : undefined,
        maxFont: 13,
        lineStep: 0.04,
        maxLineRatio: 1.45,
        // 以原始可读字号作为统一基线。后续二次迭代只允许整组正文同步增大字号，
        // 避免从最小字号起步时被邻近表格或题注不必要地压缩正文。
        allowOverflow: !strictSourceFit,
        avoidBlockOverlap: true,
        avoidPageOverflow: collisionFirstTextFit,
        includeGroupPeers: collisionFirstTextFit,
        // 两栏正文由各自的框宽决定换行；相邻栏的文字不再成为全篇字号上限。
        // 同列上下块、重叠源框及页面边界仍照常保护。
        bodyColumnIndependentFit: true,
        // 只有正文按逐行实际文字检测；其余文本类型保持按布局边框判定。
        bodyTextCollisionGeometry: true,
        // 首轮统一字号本身也可能碰撞，因此先对整组字号做全局安全回退，
        // 再进入“统一字号、局部调行距”的正文二次迭代。
        enforceInitialCollisionBackoff: collisionFirstTextFit,
        // 标题与正文的边界经常完全相邻，首行字形顶部允许少量光学悬出；
        // 正文向下增长，因此仍严格保护底边和左右边界。
        ignoreTopOverflow: collisionFirstTextFit,
        sharedEdgeTolerance: 4.0,
        sharedHorizontalEdgeTolerance: 3.0,
        // 对未填满正文执行二次迭代，但所有正文始终共享同一个字号。
        continueUnderfilledNodes: collisionFirstTextFit,
        minTextFillRatio: 0.85,
        collisionMinLineRatio: 1.02,
        // 首轮共享增长只改字号；二次迭代发生碰撞时，仅允许碰撞源局部降低行距，
        // 禁止任何正文块单独降低字号。
        coupleFontAndLine: true,
        // 首轮字体碰撞后不再对整组放大行距，正文二次迭代会单独处理碰撞源行距。
        skipLineExpansionAfterFontCollision: true,
        showLimiter: debugMode
      });
      syncInheritedBodyFontToBodyGroup();
      __perfMark('bodyGroup', __tBody);
      const __tGeneric = performance.now();
      tuneGroup('.layout-flow-stream[data-from-list="1"][data-flow-kind="text"]', {
        step: 0.35,
        minFont: collisionFirstTextFit ? 4.8 : undefined,
        minLineRatio: collisionFirstTextFit ? 0.98 : undefined,
        maxFont: 13,
        lineStep: 0.035,
        maxLineRatio: 1.85,
        allowOverflow: !strictSourceFit,
        avoidBlockOverlap: true,
        avoidPageOverflow: collisionFirstTextFit,
        includeGroupPeers: collisionFirstTextFit,
        ignoreTopOverflow: collisionFirstTextFit,
        startFromMinimum: collisionFirstTextFit,
        stopWhenFilled: !collisionFirstTextFit,
        showLimiter: debugMode
      });
      tuneEach('.layout-flow-stream.debug-text[data-flow-kind="text"][data-original-lines="multi"]', {
        step: 0.35,
        minFont: collisionFirstTextFit ? 4.8 : undefined,
        minLineRatio: collisionFirstTextFit ? 0.98 : undefined,
        maxFont: 13,
        lineStep: 0.035,
        maxLineRatio: 1.85,
        allowOverflow: !strictSourceFit,
        avoidBlockOverlap: true,
        avoidPageOverflow: collisionFirstTextFit,
        ignoreTopOverflow: collisionFirstTextFit,
        startFromMinimum: collisionFirstTextFit,
        stopWhenFilled: !collisionFirstTextFit,
        showLimiter: debugMode
      });
      tuneEach('.layout-block.type-text[data-original-lines="multi"]', {
        step: 0.35,
        minFont: collisionFirstTextFit ? 4.8 : undefined,
        minLineRatio: collisionFirstTextFit ? 0.98 : undefined,
        maxFont: 13,
        lineStep: 0.035,
        maxLineRatio: 1.85,
        allowOverflow: !strictSourceFit,
        avoidBlockOverlap: true,
        avoidPageOverflow: collisionFirstTextFit,
        ignoreTopOverflow: collisionFirstTextFit,
        startFromMinimum: collisionFirstTextFit,
        stopWhenFilled: !collisionFirstTextFit,
        showLimiter: debugMode
      });
      __perfMark('genericText', __tGeneric);
      const __tCaptions = performance.now();
      const tuneCaptionGroup = (selector) => tuneGroup(selector, {
        step: 0.25,
        minFont: 5.2,
        maxFont: 10.5,
        lineStep: 0.025,
        minLineRatio: 1.0,
        maxLineRatio: 1.55,
        allowOverflow: false,
        avoidBlockOverlap: true,
        markAll: true,
        labelPrefix: 'CAP',
        showLimiter: debugMode
      });
      tuneCaptionGroup('.layout-block.type-table_caption');
      tuneCaptionGroup('.layout-block.type-table_footnote');
      tuneCaptionGroup('.layout-block.type-chart_caption');
      tuneCaptionGroup('.layout-block.type-image_caption');
      tuneCaptionGroup('.layout-block.type-image_footnote');
      tuneGroup('.layout-flow-stream[data-flow-kind="ref_text"]', {
        step: 0.25,
        minFont: 4.8,
        maxFont: 12,
        lineStep: 0.025,
        minLineRatio: 0.98,
        maxLineRatio: 1.65,
        allowOverflow: false,
        avoidBlockOverlap: true,
        showLimiter: debugMode
      });
      __perfMark('captionsRefs', __tCaptions);
      const __tClamp = performance.now();
      clampTranslatedOverflow();
      clampTranslatedCodeOverflow();
      __perfMark('clampOverflow', __tClamp);
      const __tFinal = performance.now();
      enforceFinalTextCollisionSafety();
      __perfMark('finalSafetyAudit', __tFinal);
      publishBodyIterationInspection(scopedNodes(
        '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]:not([data-body-inherited="1"])'
      ));
      if (persist) saveFitCache();
      // 桌面阅读器可在自动碰撞迭代完成后设置按文献保存的正文覆盖字号。
      // 重跑诊断/排版时仍应尊重该选择；单位行距会随字体同步缩放。
      const userBodyFontPt = parseFloat(document.body.dataset.userBodyFontPt || '');
      if (Number.isFinite(userBodyFontPt)) {
        for (const node of document.querySelectorAll(
          '.layout-flow-stream[data-style-kind="body_text"][data-flow-kind="text"]'
        )) {
          node.style.fontSize = `${userBodyFontPt}pt`;
          node.dataset.userBodyFontPt = userBodyFontPt.toFixed(2);
        }
      }
    } catch (error) {
      if (!document.body.classList.contains('layout-debug')) return;
      for (const node of document.querySelectorAll('.layout-flow-stream')) {
        node.classList.add('fit-limiter');
        node.dataset.fitLabel = 'ERROR';
        node.dataset.fitDebug = String(error && error.message ? error.message : error);
        setDiagnosticTitle(node, node.dataset.fitDebug);
      }
    } finally {
      __perfMark('runTotal', __runStart);
      activeFitPages = null;
    }
  }

  window.__mineruRunLayoutFill = () => run(null, true);
  window.addEventListener('resize', () => requestAnimationFrame(fitLayoutPages));

  function revealFinalLayout() {
    document.body.classList.remove('layout-fit-pending');
    document.body.dataset.layoutFitState = 'ready';
    window.dispatchEvent(new CustomEvent('mineru-layout-fit-ready'));
  }

  function runAtomicInitialFit() {
    fitLayoutPages();
    fitLayoutEquations();
    try {
      run(null, true);
    } finally {
      revealFinalLayout();
    }
  }

  // Hidden QWebEngine export views do not reliably receive animation frames.
  // Give PDF export a synchronous path that restores the completed fit cache
  // (or performs the one required fit on a cold artifact) before printing.
  window.__mineruPrepareLayoutExport = () => {
    if (document.body.dataset.layoutFitState === 'ready') return true;
    fitLayoutPages();
    if (restoreFitCache()) {
      const codeFitChanged = clampTranslatedCodeOverflow();
      if (codeFitChanged) saveFitCache();
      fitLayoutPages();
      fitLayoutEquations();
      revealFinalLayout();
      return true;
    }
    runAtomicInitialFit();
    return document.body.dataset.layoutFitState === 'ready';
  };

  function initializeLayout() {
    initLayoutImageLightbox();
    initLayoutFormulaInteractions();
    fitLayoutPages();
    if (restoreFitCache()) {
      // Cached font/line styles are already final. Reveal them immediately:
      // waiting for (and explicitly repeating) whole-document MathJax on a
      // 500+ page manual made a warm reopen look like another cold fit.
      const codeFitChanged = clampTranslatedCodeOverflow();
      if (codeFitChanged) saveFitCache();
      fitLayoutEquations();
      revealFinalLayout();
      const cachedFontsReady = document.fonts && document.fonts.ready
        ? document.fonts.ready
        : Promise.resolve();
      const mathStartupReady = window.MathJax && window.MathJax.startup && window.MathJax.startup.promise
        ? window.MathJax.startup.promise.catch(() => {})
        : Promise.resolve();
      Promise.all([cachedFontsReady, mathStartupReady]).then(() => requestAnimationFrame(() => {
        fitLayoutPages();
        fitLayoutEquations();
      }));
      return;
    }
    document.body.classList.remove('layout-fit-cache-hit');
    document.body.classList.add('layout-fit-pending');
    document.body.dataset.layoutFitState = 'calculating';
    const fontsReady = document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve();
    Promise.resolve(fontsReady).then(() => {
      if (window.MathJax && window.MathJax.typesetPromise) {
        return window.MathJax.typesetPromise().catch(() => {});
      }
      return null;
    }).then(() => {
      document.body.dataset.layoutProgress = `正在完成全文版面计算（共 ${document.querySelectorAll('.layout-page-wrap').length} 页）…`;
      // Give Chromium two paint opportunities so the progress message is
      // visible before the synchronous whole-document collision pass starts.
      requestAnimationFrame(() => requestAnimationFrame(() => window.setTimeout(runAtomicInitialFit, 0)));
    });
  }

  if (document.body && document.body.classList.contains('layout-fit-cache-hit')) {
    // This script is emitted after the complete page DOM. A deferred MathJax
    // script can postpone DOMContentLoaded for a large manual, but a warm fit
    // does not need to keep the already-cached pages hidden while it starts.
    initializeLayout();
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeLayout, { once: true });
  } else {
    initializeLayout();
  }
})();
</script>
</body></html>"""
    )
    try:
        out_path.write_text(html_text, encoding="utf-8")
        cleanup_stale_layout_artifacts(markdown_path, [out_path, audit_path])
        return out_path
    except Exception as exc:
        if log:
            log(f"生成排版预览遇到问题：{exc}")
        return None


class SourcePreviewProvider:
    def __init__(self, workspace: Path):
        self.workspace = workspace

    def source_url_or_html(
        self,
        source_path: Path,
        parsed_markdown: Path | None,
        mode: PreviewMode,
        prefer_layout: bool = False,
        style: ExportStyleSettings | None = None,
    ):
        if mode == PreviewMode.PARSED and parsed_markdown and parsed_markdown.exists():
            if prefer_layout:
                layout_html = render_layout_preview_html(parsed_markdown, style=style)
                if layout_html and layout_html.exists():
                    return ("url", layout_html.resolve().as_uri())
            html_path = render_preview_html_internal(parsed_markdown, self.workspace)
            return ("url", html_path.resolve().as_uri()) if html_path else ("markdown", parsed_markdown)
        if not source_path or not source_path.exists():
            if parsed_markdown and parsed_markdown.exists():
                if prefer_layout:
                    layout_html = render_layout_preview_html(parsed_markdown, style=style)
                    if layout_html and layout_html.exists():
                        return ("url", layout_html.resolve().as_uri())
                html_path = render_preview_html_internal(parsed_markdown, self.workspace)
                return ("url", html_path.resolve().as_uri()) if html_path else ("markdown", parsed_markdown)
            return ("html", simple_file_html(Path("missing")))
        suffix = source_path.suffix.lower()
        if suffix == ".pdf":
            pdf_html = render_original_pdf_preview_html(source_path)
            if pdf_html and pdf_html.exists():
                return ("url", pdf_html.resolve().as_uri())
            return ("url", source_path.resolve().as_uri())
        if suffix in {".html", ".htm"}:
            return ("url", source_path.resolve().as_uri())
        if suffix in IMAGE_SUFFIXES:
            return ("html", simple_file_html(source_path))
        if suffix in PANDOC_OFFICE_SUFFIXES:
            html_path = self.convert_original_with_pandoc(source_path)
            if html_path:
                return ("url", html_path.resolve().as_uri())
        return ("html", simple_file_html(source_path))

    def convert_original_with_pandoc(self, path: Path) -> Path | None:
        pandoc = find_pandoc_for_workspace(self.workspace)
        if not pandoc:
            return None
        out = path.with_name(f"original.{re.sub(r'[^A-Za-z0-9_.-]+', '_', path.stem)}.html")
        epub_preview_marker = "litmtrans-epub-original-preview-v2-embedded-resources"
        is_epub = path.suffix.lower() == ".epub"
        if polished_preview_cache_is_fresh(out, original_document_preview_dependencies(path)) and (
            not is_epub or html_contains_marker(out, epub_preview_marker)
        ):
            return out
        try:
            command = [str(pandoc), str(path), "-s", "--mathjax"]
            if is_epub:
                # A standalone HTML beside the EPUB cannot resolve resources
                # stored inside the ZIP. Embed images, SVG and CSS as data URIs.
                command.append("--embed-resources")
            command.extend(["-o", str(out)])
            subprocess.run(
                command,
                cwd=str(path.parent),
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
                **hidden_subprocess_kwargs(),
            )
            if out.exists():
                if is_epub:
                    content = out.read_text(encoding="utf-8", errors="replace")
                    marker = f'<meta name="litmtrans-preview" content="{epub_preview_marker}">'
                    content = content.replace("</head>", f"{marker}\n</head>", 1)
                    out.write_text(content, encoding="utf-8")
                polish_html(out)
                return out
        except Exception:
            return None
        return None


def load_image_records(folder: Path) -> list[dict]:
    path = folder / "image_map.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


class _EmbeddedPreviewTools:
    PreviewMode = PreviewMode
    SourcePreviewProvider = SourcePreviewProvider
    find_pandoc = staticmethod(find_pandoc_for_workspace)
    polished_preview_cache_is_fresh = staticmethod(polished_preview_cache_is_fresh)
    preview_html_dependencies = staticmethod(preview_html_dependencies)
    render_preview_html = staticmethod(render_preview_html_internal)
    render_export_html = staticmethod(render_export_html_internal)
    polish_html = staticmethod(polish_html)
    normalize_markdown_for_export = staticmethod(normalize_markdown_for_export)
    layout_image_width_percentages = staticmethod(layout_image_width_percentages)
    load_image_records = staticmethod(load_image_records)


preview_tools = _EmbeddedPreviewTools()


__all__ = [name for name in globals() if not name.startswith("__")]
