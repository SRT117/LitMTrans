from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import litmtrans as mineru


TRANSLATABLE_TYPES = {
    "title",
    "text",
    "table_caption",
    "table_footnote",
    "chart_caption",
    "image_caption",
    "image_footnote",
}

LAYOUT_GROUP_MAX_CHARS = 135000
LAYOUT_GROUP_MAX_BLOCKS = 160
LAYOUT_GROUP_CONCURRENCY = 3
DEEPSEEK_FAST_LAYOUT_TARGET_CHARS = 2_000
DEEPSEEK_FAST_LAYOUT_MIN_BODY_CHARS = 500
DEEPSEEK_FAST_LAYOUT_SOFT_OVERFLOW_CHARS = 500
DEEPSEEK_FAST_LAYOUT_CONCURRENCY = 100
DEEPSEEK_FAST_LAYOUT_WARMUP_REQUESTS = 2
DEEPSEEK_FAST_LAYOUT_MIN_CACHE_HIT_RATE = 0.5
DEEPSEEK_FAST_LAYOUT_RETRY_MIN_CACHE_HIT_RATE = 0.6
DEEPSEEK_FAST_CACHE_PROTECTION_ERROR_PREFIX = "DEEPSEEK_FAST_CACHE_PROTECTION:"
LAYOUT_TRANSLATION_PROTOCOL = "layout-json-groups-v6-image-footnote-fit"

# Inline ``array`` environments found inside a text/caption block are an OCR
# artifact, not a trustworthy display equation.  They are often truncated
# (for example an unmatched ``\\left``) and MathJax renders such content as a
# large yellow error panel.  Real display equations are separate MinerU blocks
# and never pass through ``set_block_text`` below.
INLINE_TEX_RE = re.compile(r"\\\((?P<body>[\s\S]*?)\\\)")
MATH_EXPRESSION_RE = re.compile(
    r"\\\[[\s\S]*?\\\]"
    r"|\\\([\s\S]*?\\\)"
    r"|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$"
    r"|(?<!\\)\$(?![\s$])[^\n$]*(?<!\\)\$(?!\d)"
)


def inline_tex_to_safe_text(body: str) -> str:
    """Make a broken inline TeX fragment readable without invoking MathJax."""
    text = html.unescape(str(body or ""))
    text = re.sub(r"\\(?:begin|end)\s*\{[^}]*\}", " ", text)
    text = re.sub(r"\\(?:left|right|mathsf|mathrm|mathbf|boldsymbol|textstyle|displaystyle|bf|tt)\b", "", text)
    replacements = {r"\mu": "μ", r"\star": "*", r"\cdot": "·", r"\prime": "′"}
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = text.replace("{", "").replace("}", "")
    text = re.sub(r"\\", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;；")
    return f"（{text}）" if text else ""


def neutralize_broken_inline_tex(text: str) -> str:
    """Prevent malformed OCR TeX in translated text from becoming merror UI."""
    def replace(match: re.Match) -> str:
        body = match.group("body")
        # Arrays are particularly harmful because MathJax reserves a large
        # error box.  An unmatched sizing command is likewise not safe to
        # typeset.  Keep normal formulas untouched.
        unsafe = (
            bool(re.search(r"\\(?:begin|end)\s*\{", body))
            or body.count(r"\left") != body.count(r"\right")
        )
        return inline_tex_to_safe_text(body) if unsafe else match.group(0)

    return INLINE_TEX_RE.sub(replace, str(text or ""))


def normalize_math_comparison_entities(text: str) -> str:
    """Decode comparison entities once before the HTML renderer escapes them.

    Layout text is HTML-escaped at render time.  Leaving ``&lt;`` in the
    translation therefore produces ``&amp;lt;`` in the page; MathJax then sees
    a literal ampersand inside TeX and renders an ``merror`` (the yellow box).
    """
    return re.sub(
        r"&(?:amp;)?(?P<sign>lt|gt);",
        lambda match: "<" if match.group("sign").lower() == "lt" else ">",
        str(text or ""),
        flags=re.I,
    )


def math_expression_body(token: str) -> str:
    value = str(token or "")
    if (value.startswith(r"\(") and value.endswith(r"\)")) or (
        value.startswith(r"\[") and value.endswith(r"\]")
    ):
        return value[2:-2]
    if value.startswith("$$") and value.endswith("$$"):
        return value[2:-2]
    if value.startswith("$") and value.endswith("$"):
        return value[1:-1]
    return value


def collapse_redundant_formula_braces(value: str) -> str:
    output = str(value or "")
    previous = None
    while output != previous:
        previous = output
        # Collapse only nested groups. Braces delimiting a fraction,
        # subscript, superscript, or command argument remain significant.
        output = re.sub(r"\{\s*\{([^{}]*)\}\s*\}", r"{\1}", output)
    return output


def normalize_math_body_for_retry(value: str) -> str:
    output = unicodedata.normalize("NFKC", str(value or ""))
    output = html.unescape(output)
    output = re.sub(r"\s+", " ", output).strip()
    # MinerU/OCR occasionally absorbs an ordered-list marker such as
    # ``; (ii)`` into the inline formula. Moving that marker outside the
    # formula is a safe layout repair, not a mathematical change.
    output = re.sub(
        r"(?:\\[,;:!]\s*|[,;:]\s+|\s+)\((?:i{1,3}|iv|v|[a-c])\)\s*$",
        "",
        output,
        flags=re.I,
    )
    output = collapse_redundant_formula_braces(output)
    # Treat upright-text wrappers as presentation-only for retry decisions:
    # ``\\mathrm{H}``, ``\\mathrm { H }`` and ``H`` are equivalent here.
    previous = None
    while output != previous:
        previous = output
        output = re.sub(r"\\mathrm\s*\{([^{}]*)\}", r"\1", output)
        output = re.sub(r"\\mathrm\s+([A-Za-z])", r"\1", output)
        output = re.sub(r"\\mathrm(?=[A-Za-z])", "", output)
        output = collapse_redundant_formula_braces(output)
    return re.sub(r"[\s,.;:，。；：、]+", "", output)


def formula_retry_detail(
    expected: str,
    actual: str | None,
    expected_index: int,
    actual_index: int,
) -> str:
    expected_body = normalize_math_body_for_retry(math_expression_body(expected))
    actual_body = normalize_math_body_for_retry(math_expression_body(actual)) if actual else ""
    label = f"公式#{expected_index + 1}"
    if not actual:
        return f"{label}缺少可渲染的数学定界符或主体（源: {str(expected)[:180]}）"
    return (
        f"{label}数学主体疑似变化（标准化源: {expected_body[:180]}；"
        f"标准化当前值: {actual_body[:180]}；当前公式序号: {actual_index + 1}）"
    )


def inline_formula_integrity_issue(record: "LayoutTextBlock", translated_text: str) -> str:
    """Keep exact-fidelity warnings separate from paid retry decisions."""
    source = [re.sub(r"\s+", " ", match.group(0)).strip() for match in MATH_EXPRESSION_RE.finditer(record.text)]
    if not source:
        return ""
    translated = [
        re.sub(r"\s+", " ", match.group(0)).strip()
        for match in MATH_EXPRESSION_RE.finditer(str(translated_text or ""))
    ]
    if source == translated:
        return ""
    return f"原块含 {len(source)} 个数学表达式，译文保留 {len(translated)} 个；精确 TeX 写法存在差异。"


def is_ocr_nonmath_inline_token(token: str) -> bool:
    """Recognize OCR artifacts that were incorrectly wrapped as inline TeX.

    MinerU occasionally represents equation references, author-year citation
    tails, and a unit plus citation as mathematics.  A model correctly turns
    those into prose (for example ``Eq. ~3!`` -> ``式 (3)``), so they must not
    contribute to the automatic formula-loss count.
    """
    body = html.unescape(math_expression_body(token))
    if re.fullmatch(r"\s*\\operatorname\s*\{\s*E\s*q\s*\.?\s*\}\s*", body, flags=re.I):
        return True
    if re.fullmatch(
        r"\s*[-+]?\d+(?:\s*\.\s*\d+)?\s*\^\s*\{\s*\\circ\s*\}\s*\\mathrm\s*\{\s*[CFK]\s*\}\s*",
        body,
        flags=re.I,
    ):
        return True
    if re.fullmatch(
        r"\s*(?:\\mathrm\s*\{\s*)?a\s*l\s*\.\s*\^\s*\{\s*(?:\d\s*)+(?:[-,]\s*(?:\d\s*)+)*\}\s*\}?\s*",
        body,
        flags=re.I,
    ):
        return True
    return bool(re.fullmatch(
        r"\s*\\mathrm\s*\{\s*(?:[A-Za-z]\s*){1,8}\.\s*\^\s*\{\s*(?:\d\s*)+(?:[-,]\s*(?:\d\s*)+)*\}\s*\}\s*",
        body,
        flags=re.I,
    ))


def inline_formula_retry_issue(record: "LayoutTextBlock", translated_text: str) -> str:
    """Retry only when a source formula is definitely missing from the output.

    Formula-body comparisons are useful review diagnostics, but are too
    sensitive to use as an automatic paid retry gate: capable models commonly
    normalize OCR TeX, choose an equivalent notation, or repair presentation
    wrappers.  A smaller number of recognizable formula tokens is the
    objective failure that needs recovery; same-count differences remain in
    ``inline_formula_integrity_issue`` for human review.
    """
    source = [match for match in MATH_EXPRESSION_RE.finditer(record.text) if not is_ocr_nonmath_inline_token(match.group(0))]
    if not source:
        return ""
    translated = [
        match
        for match in MATH_EXPRESSION_RE.finditer(str(translated_text or ""))
        if not is_ocr_nonmath_inline_token(match.group(0))
    ]
    if len(translated) < len(source):
        missing_index = len(translated)
        return formula_retry_detail(
            source[missing_index].group(0),
            None,
            missing_index,
            missing_index,
        )
    return ""


@dataclass
class LayoutTextBlock:
    block_id: str
    page: int
    block_type: str
    text: str
    block: dict


@dataclass
class LayoutFormulaItem:
    formula_id: str
    page: int
    formula_type: str
    text: str
    bbox: list[float]
    spans: list[dict]


def plain_block_text(block: dict) -> str:
    body_html = mineru.layout_lines_to_html(block.get("lines"))
    if mineru.is_symbol_glossary_block(block):
        # A nomenclature row is one semantic entry, not a wrapped prose line.
        # Blank lines are the translation protocol's existing representation
        # for hard paragraph boundaries.
        body_html = re.sub(r"<br\s*/?>", "\n\n", body_html, flags=re.IGNORECASE)
    # Superscript/subscript markers in affiliation lines, citations and
    # formula-adjacent prose are semantic content, not decoration.  Stripping
    # them turns ``<sup>1</sup>State … <sup>2</sup>Chongqing …`` into one long
    # unstructured sentence.  MTranServer can then enter a deterministic
    # decoder loop (for example repeated ``北京市``).  Keep only the inline
    # markup that the translation pipeline explicitly protects; flatten every
    # other tag as before so block payloads remain plain, safe text.
    preserved = re.sub(
        r"</?(?:sup|sub)\b[^>]*>",
        lambda match: match.group(0),
        body_html,
        flags=re.I,
    )
    return re.sub(r"<(?!/?(?:sup|sub)\b)[^>]+>", "", preserved).strip()


def restore_symbol_glossary_row_breaks(block: dict, translated_text: str) -> str:
    """Restore glossary entry boundaries if a translator collapses them.

    This only runs for blocks already accepted by the geometric glossary
    detector.  It uses the leading formula from each source row as a stable,
    semantic delimiter; ordinary formula-heavy prose never enters this path.
    """
    if not block.get("_layout_symbol_glossary") or "\n" in translated_text:
        return translated_text
    source_lines = block.get("_layout_original_lines") or block.get("lines") or []
    markers: list[str] = []
    for line in source_lines:
        if not isinstance(line, dict):
            continue
        spans = [span for span in (line.get("spans") or []) if isinstance(span, dict) and str(span.get("content") or "").strip()]
        if not spans:
            continue
        first = spans[0]
        first_type = str(first.get("type") or "").lower()
        formula = str(first.get("content") or "").strip()
        if ("equation" in first_type or "formula" in first_type) and formula:
            markers.append(r"\(" + formula + r"\)")
    if len(markers) < 8:
        return translated_text
    positions: list[int] = []
    cursor = 0
    for marker in markers:
        position = translated_text.find(marker, cursor)
        if position < 0:
            continue
        positions.append(position)
        cursor = position + len(marker)
    if len(positions) * 5 < len(markers) * 4:
        return translated_text
    for position in reversed(positions[1:]):
        translated_text = translated_text[:position] + "\n\n" + translated_text[position:]
    return translated_text


def iter_formula_spans(block: dict) -> list[dict]:
    spans: list[dict] = []
    for line in block.get("lines") or []:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans") or []:
            if not isinstance(span, dict):
                continue
            span_type = str(span.get("type") or "").lower()
            content = str(span.get("content") or "").strip()
            if content and ("equation" in span_type or "formula" in span_type):
                spans.append(span)
    return spans


def layout_formula_text_from_block(block: dict) -> str:
    parts: list[str] = []
    for span in iter_formula_spans(block):
        parts.append(str(span.get("content") or "").strip())
    return "\n".join(parts).strip()


def set_block_text(block: dict, translated_text: str) -> None:
    # Keep formulas/media blocks untouched; translated text is deliberately plain
    # so it can be safely reflowed inside the original bbox.
    translated_text = mineru.normalize_translated_inline_html(translated_text)
    translated_text = normalize_math_comparison_entities(translated_text)
    translated_text = neutralize_broken_inline_tex(translated_text)
    translated_text = restore_symbol_glossary_row_breaks(block, translated_text)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", translated_text.strip()) if part.strip()]
    if not paragraphs:
        paragraphs = [translated_text.strip()]
    block["lines"] = [{"spans": [{"type": "text", "content": paragraph}]} for paragraph in paragraphs]


def iter_translatable_blocks(page_info: list[dict]) -> list[LayoutTextBlock]:
    records: list[LayoutTextBlock] = []
    first_title_marked = False
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            continue
        local_index = 0

        def visit(block: dict, prefix: str = "b") -> None:
            nonlocal local_index, first_title_marked
            if not isinstance(block, dict):
                return
            block_type = str(block.get("type") or "").lower()
            bbox = block.get("bbox")
            if block_type in TRANSLATABLE_TYPES and isinstance(bbox, list) and len(bbox) >= 4:
                if mineru.is_symbol_glossary_block(block):
                    block["_layout_symbol_glossary"] = True
                text = plain_block_text(block)
                if text:
                    original_lines = [copy.deepcopy(line) for line in (block.get("lines") or []) if isinstance(line, dict)]
                    block["_layout_original_line_count"] = mineru.layout_visual_line_count(original_lines)
                    block["_layout_original_lines"] = original_lines
                    if block_type == "title" and not first_title_marked:
                        block["_layout_main_title"] = True
                        first_title_marked = True
                    block["_layout_original_plain_text"] = text
                    local_index += 1
                    records.append(
                        LayoutTextBlock(
                            block_id=f"p{page_index + 1:03d}_{prefix}{local_index:04d}",
                            page=page_index + 1,
                            block_type=block_type,
                            text=text,
                            block=block,
                        )
                    )
            for child in block.get("blocks") or []:
                if isinstance(child, dict):
                    visit(child, prefix="c")

        for block in page.get("preproc_blocks") or []:
            visit(block)
    return records


def iter_formula_context(page_info: list[dict]) -> list[LayoutFormulaItem]:
    formulas: list[LayoutFormulaItem] = []
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            continue
        local_index = 0

        def visit(block: dict, prefix: str = "f") -> None:
            nonlocal local_index
            if not isinstance(block, dict):
                return
            block_type = str(block.get("type") or "").lower()
            bbox = block.get("bbox")
            if isinstance(bbox, list) and len(bbox) >= 4:
                formula_spans = iter_formula_spans(block)
                if formula_spans:
                    for span in formula_spans:
                        formula_text = str(span.get("content") or "").strip()
                        if not formula_text:
                            continue
                        local_index += 1
                        formulas.append(
                            LayoutFormulaItem(
                                formula_id=f"p{page_index + 1:03d}_{prefix}{local_index:04d}",
                                page=page_index + 1,
                                formula_type=block_type or str(span.get("type") or "formula").lower(),
                                text=formula_text,
                                bbox=[float(part) for part in bbox[:4]],
                                spans=[span],
                            )
                        )
                elif block_type in {"interline_equation", "equation", "inline_equation"}:
                    formula_text = plain_block_text(block)
                    if formula_text:
                        writable_spans = [
                            span
                            for line in block.get("lines") or []
                            if isinstance(line, dict)
                            for span in line.get("spans") or []
                            if isinstance(span, dict) and "content" in span
                        ]
                        if not writable_spans:
                            return
                    local_index += 1
                    formulas.append(
                        LayoutFormulaItem(
                            formula_id=f"p{page_index + 1:03d}_{prefix}{local_index:04d}",
                            page=page_index + 1,
                            formula_type=block_type or "formula",
                            text=formula_text,
                            bbox=[float(part) for part in bbox[:4]],
                            spans=[writable_spans[0]],
                        )
                    )
            for child in block.get("blocks") or []:
                if isinstance(child, dict):
                    visit(child, prefix="c")

        for block in page.get("preproc_blocks") or []:
            visit(block)
    return formulas


def repair_invalid_json_escapes(text: str) -> str:
    """Escape bare backslashes such as LaTeX ``\\alpha`` inside JSON strings."""
    valid_simple = {'"', "\\", "/", "b", "f", "n", "r", "t"}
    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(text):
            output.append("\\\\")
            index += 1
            continue
        next_char = text[index + 1]
        if next_char in valid_simple:
            output.extend((char, next_char))
            index += 2
            continue
        if next_char == "u" and index + 5 < len(text) and re.fullmatch(r"[0-9a-fA-F]{4}", text[index + 2:index + 6]):
            output.extend((char, text[index + 1:index + 6]))
            index += 6
            continue
        output.append("\\\\")
        index += 1
    return "".join(output)


def extract_json_object(text: str) -> dict:
    cleaned = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.S | re.I)
    if fenced:
        cleaned = fenced.group(1).strip()
    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])
    last_error = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as error:
            last_error = error
        repaired = repair_invalid_json_escapes(candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError as error:
                last_error = error
    if last_error:
        raise last_error
    raise json.JSONDecodeError("No JSON object found", cleaned, 0)


def block_payload(records: list[LayoutTextBlock]) -> list[dict]:
    return [
        {
            "id": record.block_id,
            "page": record.page,
            "type": record.block_type,
            "text": record.text,
        }
        for record in records
    ]


def formula_payload(formulas: list[LayoutFormulaItem], limit: int = 1200) -> list[dict]:
    """Return only formula semantics for the model, never layout coordinates.

    ``bbox`` remains on ``LayoutFormulaItem`` for local formula replacement and
    rendering.  It has no bearing on translation quality and would only add
    visual-layout noise to the model prompt.
    """
    return [
        {
            "id": item.formula_id,
            "page": item.page,
            "type": item.formula_type,
            "tex": item.text,
        }
        for item in formulas[:limit]
    ]


def normalize_formula_tex(text: str) -> str:
    return str(text or "").strip()


def target_expects_cjk(target_language: str) -> bool:
    lowered = str(target_language or "").lower()
    return any(token in lowered for token in ("中文", "汉语", "chinese", "zh", "简体", "繁体"))


def cjk_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", str(text or "")))


def latin_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z]", str(text or "")))


def normalized_compare_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def has_unsafe_control_characters(value: str) -> bool:
    return any(
        (ord(character) < 32 and ord(character) not in {9, 10, 13})
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in str(value or "")
    )


EQUATION_REFERENCE_RE = re.compile(
    r"\b(?:Eq|Eqs|Equation|Equations)\.?\s+"
    r"(?:(?![.;。；]\s).){0,120}"
    r"[（(]\s*[A-Za-z]?\d+[A-Za-z]?\s*[)）]",
    re.I,
)
EQUATION_NUMBER_RE = re.compile(r"[（(]\s*([A-Za-z]?\d+[A-Za-z]?)\s*[)）]")
GARBLED_PAREN_REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[~～〜]\s*)([A-Za-z]?\d+[A-Za-z]?|[a-z])\s*[!！](?![A-Za-z0-9])")


def source_equation_numbers(text: str) -> list[str]:
    numbers: list[str] = []
    for match in EQUATION_REFERENCE_RE.finditer(str(text or "")):
        for number_match in EQUATION_NUMBER_RE.finditer(match.group(0)):
            number = number_match.group(1).strip()
            if number and number not in numbers:
                numbers.append(number)
    return numbers


def repair_equation_reference_translation(source_text: str, translated_text: str) -> str:
    numbers = source_equation_numbers(source_text)
    result = GARBLED_PAREN_REFERENCE_RE.sub(lambda match: f"({match.group(1)})", str(translated_text or ""))
    if not numbers:
        return result
    for number in numbers:
        escaped = re.escape(number)
        garbled = re.compile(rf"(?:[~～〜]\s*)?{escaped}\s*[!！]")
        if garbled.search(result):
            result = garbled.sub(f"式 ({number})", result)
        result = re.sub(rf"(?<![A-Za-z0-9])式\s*{escaped}(?![A-Za-z0-9])", f"式 ({number})", result)
        result = re.sub(rf"(?<![A-Za-z0-9])方程\s*{escaped}(?![A-Za-z0-9])", f"方程 ({number})", result)
    result = re.sub(r"(?:方程|公式)\s*[。.]\s*(式\s*\()", r"\1", result)
    result = re.sub(r"\)\s*(和|与|及)\s*式", r") \1式", result)
    return result


def repair_record_translation(record: LayoutTextBlock, translated_text: str) -> str:
    normalized = mineru.normalize_translated_inline_html(translated_text)
    return repair_equation_reference_translation(record.text, normalized)


def repair_record_translations(records: list[LayoutTextBlock], translations: dict[str, str]) -> dict[str, str]:
    by_id = {record.block_id: record for record in records}
    return {
        block_id: repair_record_translation(by_id[block_id], text) if block_id in by_id else text
        for block_id, text in translations.items()
    }


def affiliation_like_text(text: str) -> bool:
    normalized = normalized_compare_text(text)
    if not normalized:
        return False
    affiliation_tokens = (
        "department",
        "university",
        "institute of technology",
        "faculty",
        "laboratory",
        "college",
        "〒",
        "japan",
        "china",
    )
    return any(token in normalized for token in affiliation_tokens) and not re.search(r"\b(fig|figure|table|we|this|the|water|shock)\b", normalized)


def author_byline_like_text(text: str) -> bool:
    """Return true for dense author bylines, which should not be forced into Chinese.

    A byline made mostly of personal names and superscript affiliations is valid
    even when it contains no CJK characters.  Treating it as an untranslated
    body paragraph causes pointless retries on every run.
    """
    source = str(text or "")
    if len(re.findall(r"<sup>.*?</sup>", source, flags=re.I | re.S)) < 3:
        return False
    names = re.findall(r"\b[A-Z][A-Za-z'’.-]+(?:\s+[A-Z][A-Za-z'’.-]+)+\b", source)
    return len(names) >= 3


def bibliography_like_text(text: str) -> bool:
    """Allow citation metadata, DOI and URLs to remain in their source form."""
    source = str(text or "").strip()
    if not source:
        return False
    if re.search(r"https?://\S+", source, flags=re.I):
        return True
    has_doi = bool(re.search(r"\b10\.\d{4,9}/\S+", source, flags=re.I))
    has_year = bool(re.search(r"[\[(](?:19|20)\d{2}[\])]", source))
    has_publication = bool(
        re.search(
            r"\b(?:J\.|Journal|Phys\.|Proc\.|Proceedings|Conf\.|Rev\.|Vol\.|Appl\.)\b",
            source,
            flags=re.I,
        )
    )
    return has_doi and (has_year or has_publication)


CHECK_TRANSLATED_TYPES = {
    "title",
    "text",
    "table_caption",
    "table_footnote",
    "chart_caption",
    "image_caption",
    "image_footnote",
}


def should_check_translation(record: LayoutTextBlock) -> bool:
    if record.block_type not in CHECK_TRANSLATED_TYPES:
        return False
    if record.block_type == "text" and affiliation_like_text(record.text):
        return False
    if record.block_type == "text" and author_byline_like_text(record.text):
        return False
    if record.block_type == "text" and bibliography_like_text(record.text):
        return False
    return True


def visible_text_length(text: str) -> int:
    """Count visible source/translation characters, ignoring markup tags."""
    plain = re.sub(r"<[^>]+>", "", html.unescape(str(text or "")))
    return len(re.sub(r"\s+", "", plain))


def looks_overexpanded(record: LayoutTextBlock, translated_text: str) -> bool:
    """Detect unsafe model completion beyond the boundaries of a layout block.

    Layout blocks may be split at a column edge.  A model must translate the
    visible fragment only, never complete it with the next block.  The generous
    limit avoids penalising ordinary Chinese academic translations while
    catching multi-sentence continuations such as a 2-line source becoming an
    entire paragraph.
    """
    if record.block_type not in CHECK_TRANSLATED_TYPES:
        return False
    source_size = visible_text_length(record.text)
    translated_size = visible_text_length(translated_text)
    if source_size < 24 or not translated_size:
        return False
    return translated_size > max(80, source_size * 2)


def looks_untranslated(record: LayoutTextBlock, translated_text: str, target_language: str) -> bool:
    if not target_expects_cjk(target_language):
        return False
    if not should_check_translation(record):
        return False
    source = str(record.text or "").strip()
    translated = str(translated_text or "").strip()
    if not source:
        return False
    source_latin = latin_count(source)
    # Titles are often short ("ABSTRACT", "6 EXPERIMENTS"), so the body
    # paragraph threshold below would silently accept an unchanged English
    # heading.  For a CJK target, an unchanged title containing enough natural
    # Latin text must be retried.  Very short labels such as "A" and formula
    # only headings are deliberately left alone.
    if record.block_type == "title":
        return (
            source_latin >= 4
            and normalized_compare_text(source) == normalized_compare_text(translated)
            and cjk_count(translated) < 2
        )
    if source_latin < 60:
        return False
    translated_cjk = cjk_count(translated)
    translated_latin = latin_count(translated)
    if normalized_compare_text(source) == normalized_compare_text(translated):
        return True
    return translated_cjk < 8 and translated_latin >= max(60, source_latin * 0.45)


def untranslated_or_missing_records(
    records: list[LayoutTextBlock],
    translations: dict[str, str],
    target_language: str,
) -> list[LayoutTextBlock]:
    bad: list[LayoutTextBlock] = []
    for record in records:
        if not should_check_translation(record):
            continue
        if record.block_id not in translations:
            bad.append(record)
            continue
        if looks_untranslated(record, translations.get(record.block_id, ""), target_language):
            bad.append(record)
    return bad


def unsafe_overexpanded_records(
    records: list[LayoutTextBlock],
    translations: dict[str, str],
) -> list[LayoutTextBlock]:
    return [
        record
        for record in records
        if record.block_id in translations and looks_overexpanded(record, translations.get(record.block_id, ""))
    ]


def suspicious_duplicate_translation_records(
    records: list[LayoutTextBlock],
    translations: dict[str, str],
) -> list[LayoutTextBlock]:
    """Find a likely ID shift where a long output was copied to the next block.

    When a model joins a split sentence into the preceding ID, it commonly
    emits the next block's translation twice.  Retrying the earlier duplicate
    restores the one-to-one source/output mapping without guessing semantics.
    """
    suspicious: list[LayoutTextBlock] = []
    for previous, current in zip(records, records[1:]):
        previous_text = normalized_compare_text(translations.get(previous.block_id, ""))
        current_text = normalized_compare_text(translations.get(current.block_id, ""))
        if len(previous_text) < 80 or previous_text != current_text:
            continue
        if normalized_compare_text(previous.text) != normalized_compare_text(current.text):
            suspicious.append(previous)
    return suspicious


RETRY_REASON_DESCRIPTIONS = {
    "missing": "上一轮 JSON 缺少该块 ID。",
    "untranslated": "译文高度疑似仍为源文语言。",
    "overexpanded": "译文明显超出原版面块边界。",
    "duplicate": "译文疑似复制了相邻块内容。",
    "formula-structure": "公式定界符或数学主体可能被破坏。",
    "unsafe-characters": "上一轮结果含非法 JSON 控制字符或未正确转义的 TeX 反斜杠。",
}


def retry_details_for_record(
    record: LayoutTextBlock,
    translations: dict[str, str],
    reasons: tuple[str, ...] | list[str] = (),
) -> str:
    translation = translations.get(record.block_id, "")
    details: list[str] = []
    for reason in reasons or ():
        if reason == "formula-structure":
            details.append(inline_formula_retry_issue(record, translation) or RETRY_REASON_DESCRIPTIONS[reason])
        else:
            details.append(RETRY_REASON_DESCRIPTIONS.get(reason, str(reason)))
    return "; ".join(details)


def is_format_only_retry_reasons(reasons: tuple[str, ...] | list[str]) -> bool:
    return bool(reasons) and all(reason in {"formula-structure", "unsafe-characters"} for reason in reasons)


def classify_retry_records(
    records: list[LayoutTextBlock],
    translations: dict[str, str],
    target_language: str,
) -> list[tuple[LayoutTextBlock, tuple[str, ...]]]:
    """Classify retry causes without collapsing every QA signal into “missing”."""
    reasons_by_id: dict[str, list[str]] = {}

    def add(record: LayoutTextBlock, reason: str) -> None:
        reasons = reasons_by_id.setdefault(record.block_id, [])
        if reason not in reasons:
            reasons.append(reason)

    for record in records:
        if should_check_translation(record) and record.block_id not in translations:
            add(record, "missing")
        if record.block_id in translations and has_unsafe_control_characters(translations.get(record.block_id, "")):
            add(record, "unsafe-characters")
        if looks_untranslated(record, translations.get(record.block_id, ""), target_language):
            add(record, "untranslated")
        if record.block_id in translations and looks_overexpanded(
            record,
            translations.get(record.block_id, ""),
        ):
            add(record, "overexpanded")
    for record in unsafe_overexpanded_records(records, translations):
        add(record, "overexpanded")
    for record in suspicious_duplicate_translation_records(records, translations):
        add(record, "duplicate")
    for record in records:
        if record.block_id not in translations:
            continue
        if inline_formula_retry_issue(record, translations.get(record.block_id, "")):
            add(record, "formula-structure")
    return [
        (record, tuple(reasons_by_id[record.block_id]))
        for record in records
        if record.block_id in reasons_by_id
    ]


def records_needing_retry(
    records: list[LayoutTextBlock],
    translations: dict[str, str],
    target_language: str,
) -> list[LayoutTextBlock]:
    return [
        record
        for record, _reasons in classify_retry_records(records, translations, target_language)
    ]


def retry_reason_summary(
    classified: list[tuple[LayoutTextBlock, tuple[str, ...]]],
) -> str:
    counts = {
        "missing": 0,
        "untranslated": 0,
        "overexpanded": 0,
        "duplicate": 0,
        "formula-structure": 0,
        "unsafe-characters": 0,
    }
    for _record, reasons in classified:
        for reason in reasons:
            if reason in counts:
                counts[reason] += 1
    return (
        f"真正缺失 {counts['missing']}、疑似未译 {counts['untranslated']}、"
        f"越界扩写 {counts['overexpanded']}、错位重复 {counts['duplicate']}、"
        f"公式内容/定界符异常 {counts['formula-structure']}、"
        f"非法控制字符 {counts['unsafe-characters']}"
    )


def apply_formula_replacements(formulas: list[LayoutFormulaItem] | None, replacements: dict[str, str], log=None) -> int:
    # Text translation must never rewrite standalone formulas. Inline formulas
    # are preserved inside their complete text blocks and validated on retry.
    return 0


def build_translation_prompt(
    records: list[LayoutTextBlock],
    target_language: str,
    guide: str = "",
    reference_context: str = "",
    formula_context: list[LayoutFormulaItem] | None = None,
    full_markdown_context: str = "",
) -> str:
    guide_section = f"\nGlobal translation guide:\n{guide}\n" if guide.strip() else ""
    reference_section = ""
    if reference_context.strip():
        reference_section = (
            "\nUser-provided reference corpus for terminology/style only:\n"
            f"{reference_context}\n"
            "Use the reference corpus as soft evidence only. The source blocks below have absolute priority.\n"
        )
    markdown_context_section = ""
    if full_markdown_context:
        markdown_context_section = (
            "\nFull paper Markdown context follows. It is context only: do not translate, repeat, "
            "summarize, or return it. Use it to resolve continuations across pages or columns, but "
            "translate ONLY the target blocks supplied after this fixed context and never merge their output.\n"
            "===== BEGIN FULL PAPER MARKDOWN =====\n"
            f"{full_markdown_context}\n"
            "===== END FULL PAPER MARKDOWN =====\n"
        )
    return (
        f"Translate the following academic-paper layout text blocks into {target_language}.\n"
        f"{mineru.target_language_instruction(target_language)}\n"
        "This is a layout-preserving academic-paper translation task. Each block has an id, page, type, and text.\n"
        "Only the listed text-like blocks and captions are translation targets. Image bodies, table bodies, and standalone equation/media blocks must not be translated, described, or reconstructed.\n"
        "Return ONLY valid JSON with this exact shape:\n"
        '{"translations":[{"id":"...","text":"..."}]}\n'
        "Rules:\n"
        "1. Preserve every id exactly and return one translation for every input block.\n"
        "2. Translate in context across all blocks; do not treat blocks as isolated sentences. Titles and section headings are translation targets too: translate their natural-language words, while retaining section numbers, formulas, variables, and standard abbreviations as appropriate. If a sentence is interrupted by layout/column splitting, translate only the visible fragment in each id. Never complete a fragment with text from the next block, merge blocks, duplicate a neighbouring block, or move content between ids.\n"
        "3. Use formal, accurate, fluent academic style suitable for scientific papers. Prefer standard technical terminology over literal word-by-word translation.\n"
        "4. Hard output rule: this reader renders inline mathematics with MathJax. Formulas are part of the paper's meaning, not formatting. If inline formulas, variables, citations, reference numbers, units, chemical symbols, material names, figure/table numbers, or numerical values appear inside a text/caption block, preserve their scientific meaning and translate only the surrounding natural language. Copy every inline formula verbatim with its original TeX and \\(...\\) delimiters. A naked TeX body such as R _ { 0 } cannot be rendered; never rewrite, normalize, omit, or move a formula.\n"
        "5. Preserve equation-number references exactly. For example, translate 'Eqs. (3) and (4)' as '式 (3) 和式 (4)' or an equivalent target-language phrase; never turn the parentheses into punctuation such as ~3!, ~4!, !, or prose words.\n"
        "6. If a citation/reference marker in body text appears to be a superscript in the source, mark it explicitly with <sup>...</sup>, for example <sup>[1]</sup> or <sup>[1-4]</sup>. Preserve the original citation number or symbol, do not translate it into prose, and do not invent citation numbers.\n"
        "7. For author names, journal headers, page numbers, URLs, affiliations, and bibliographic tokens, keep them mostly unchanged unless natural translation is clearly needed.\n"
        "8. Keep captions concise because they must fit in original layout boxes, but do not delete scientific meaning.\n"
        "9. Preserve natural paragraph breaks inside a block with blank lines when the source clearly contains multiple paragraphs; do not collapse unrelated paragraphs into one.\n"
        "10. If OCR/layout parsing introduced obvious spacing or line-break defects in text blocks, silently repair them in the translated text. Do not invent missing data.\n"
        "11. Do not add notes, explanations, Markdown fences, or extra fields.\n"
        f"{reference_section}\n"
        f"{guide_section}\n"
        f"{markdown_context_section}\n"
        "Input blocks JSON:\n"
        f"{json.dumps({'blocks': block_payload(records)}, ensure_ascii=False)}"
    )


def build_global_guide(
    records: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    formula_context: list[LayoutFormulaItem] | None = None,
    audit_dir: Path | None = None,
    log=None,
) -> str:
    sample_records: list[LayoutTextBlock] = []
    sample_chars = 0
    for record in records:
        estimated = len(record.text) + len(record.block_id) + 80
        if sample_records and sample_chars + estimated > 24000:
            break
        sample_records.append(record)
        sample_chars += estimated
    sample_blocks = block_payload(sample_records)
    prompt = (
        f"Read these layout text blocks from one academic paper and prepare a concise translation guide for {target_language}.\n"
        "Identify research field, paper topic, key terminology, recurring abbreviations, citation/style conventions, formula/symbol handling, and how to handle captions. "
        "Do not translate all blocks. Produce guidance that will keep later block-group translations consistent across the full paper.\n"
        f"{json.dumps({'blocks': sample_blocks}, ensure_ascii=False)}"
    )
    system_prompt = "You are a careful academic translation planner."
    custom_instruction_section = mineru.translation_custom_instruction_section(config)
    if custom_instruction_section:
        system_prompt += "\n\n" + custom_instruction_section
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]
    if audit_dir:
        mineru.save_translation_request_audit(audit_dir, "排版-术语指南", config, messages, timeout=240)
    return mineru.ai_chat_completion(
        config,
        messages,
        timeout=240,
        rate_limit_callback=log,
    )


def split_records(
    records: list[LayoutTextBlock],
    max_chars: int = LAYOUT_GROUP_MAX_CHARS,
    max_blocks: int = LAYOUT_GROUP_MAX_BLOCKS,
) -> list[list[LayoutTextBlock]]:
    groups: list[list[LayoutTextBlock]] = []
    current: list[LayoutTextBlock] = []
    current_size = 0
    for record in records:
        size = len(record.text) + len(record.block_id) + 80
        if current and (
            (max_chars > 0 and current_size + size > max_chars)
            or (max_blocks > 0 and len(current) >= max_blocks)
        ):
            groups.append(current)
            current = []
            current_size = 0
        current.append(record)
        current_size += size
    if current:
        groups.append(current)
    return groups


def strip_markdown_images(markdown: str) -> str:
    """Keep the full textual Markdown context while never sending image data/links."""
    text = str(markdown or "")
    text = re.sub(r"!\[[^\]]*\]\([^\n)]*\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _fast_group_kind(record: LayoutTextBlock) -> str:
    if record.block_type == "text":
        return "body"
    if record.block_type in {"table_caption", "chart_caption", "image_caption", "image_footnote"}:
        return "caption"
    if record.block_type == "table_footnote":
        return "table-note"
    return record.block_type


def _record_finishes_sentence(record: LayoutTextBlock) -> bool:
    text = re.sub(r"(?:</?(?:sup|sub)\b[^>]*>|\s)+$", "", record.text or "")
    return bool(re.search(r"(?:[.!?。！？]|[)）\]]\s*[.!?。！？])$", text))


def normalized_markdown_search_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", str(text or ""))
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def markdown_record_positions(
    records: list[LayoutTextBlock],
    full_markdown_context: str,
) -> dict[str, tuple[int, int]]:
    """Map blocks to the Markdown reading stream without matching duplicates backwards."""
    source = normalized_markdown_search_text(full_markdown_context)
    positions: dict[str, tuple[int, int]] = {}
    cursor = 0
    for record in records:
        needle = normalized_markdown_search_text(record.text)
        if len(needle) < 12:
            continue
        start = source.find(needle, cursor)
        if start < 0:
            continue
        end = start + len(needle)
        positions[record.block_id] = (start, end)
        cursor = end
    return positions


def deepseek_fast_layout_groups(
    records: list[LayoutTextBlock],
    full_markdown_context: str = "",
) -> list[list[LayoutTextBlock]]:
    """Group substantial body text by reading order and batch short auxiliary blocks.

    Titles are intentionally one leading request. Body blocks retain MinerU's
    stable order, while the Markdown stream verifies whether an unfinished
    sentence truly continues into the next block across a column/page break.
    Titles do not split an otherwise short body batch, and captions/footnotes
    are accumulated into supplementary batches instead of creating tiny calls.
    """
    titles = [record for record in records if record.block_type == "title"]
    groups: list[list[LayoutTextBlock]] = [titles] if titles else []
    markdown_positions = markdown_record_positions(records, full_markdown_context)
    body_groups: list[list[LayoutTextBlock]] = []
    current: list[LayoutTextBlock] = []
    current_chars = 0
    title_boundary_after_current = False
    supplementary: list[LayoutTextBlock] = []

    def can_use_soft_overflow(existing_chars: int, next_chars: int) -> bool:
        """Keep batches near the target without treating it as a hard ceiling."""
        return (
            existing_chars >= DEEPSEEK_FAST_LAYOUT_TARGET_CHARS * 0.5
            and existing_chars + next_chars
            <= DEEPSEEK_FAST_LAYOUT_TARGET_CHARS + DEEPSEEK_FAST_LAYOUT_SOFT_OVERFLOW_CHARS
        )

    def flush() -> None:
        nonlocal current, current_chars, title_boundary_after_current
        if current:
            body_groups.append(current)
        current = []
        current_chars = 0
        title_boundary_after_current = False

    for record in records:
        if record.block_type == "title":
            # Headings are translated in the leading title request. They do
            # not turn two otherwise short body runs into separate API calls,
            # but they must prevent a false “unfinished sentence” continuation
            # from leaking across a section boundary.
            if current:
                title_boundary_after_current = True
            continue
        kind = _fast_group_kind(record)
        if kind != "body":
            supplementary.append(record)
            continue
        size = len(record.text)
        previous_continues = False
        if current and not title_boundary_after_current and not _record_finishes_sentence(current[-1]):
            previous_position = markdown_positions.get(current[-1].block_id)
            position = markdown_positions.get(record.block_id)
            previous_continues = bool(
                not full_markdown_context
                or (previous_position and position and 0 <= position[0] - previous_position[1] <= 160)
            )
        if current and (
            current_chars + size > DEEPSEEK_FAST_LAYOUT_TARGET_CHARS
            and not previous_continues
            and not can_use_soft_overflow(current_chars, size)
        ):
            flush()
        current.append(record)
        current_chars += size
        title_boundary_after_current = False
    flush()

    # A final OCR/layout fragment can be a few words long even though it is a
    # complete block. It is not worth an independent request: preserve its
    # block ID but attach it to the adjacent body batch. This is deliberately a
    # soft minimum, so a document containing only one short body block still
    # produces one valid request.
    if len(body_groups) > 1 and sum(len(record.text) for record in body_groups[0]) < DEEPSEEK_FAST_LAYOUT_MIN_BODY_CHARS:
        body_groups[1] = body_groups[0] + body_groups[1]
        del body_groups[0]
    merged_body_groups: list[list[LayoutTextBlock]] = []
    for group in body_groups:
        if (
            merged_body_groups
            and sum(len(record.text) for record in group) < DEEPSEEK_FAST_LAYOUT_MIN_BODY_CHARS
        ):
            merged_body_groups[-1].extend(group)
        else:
            merged_body_groups.append(group)
    groups.extend(merged_body_groups)

    # Captions and table notes are independent layout elements. The full
    # Markdown remains available as context, so batching them avoids wasting
    # an API call on a 100-300 character standalone caption while preserving
    # each item's ID and original type in the payload. These are one or more
    # target-sized batches, never a document-wide forced single request.
    auxiliary: list[LayoutTextBlock] = []
    auxiliary_chars = 0
    for record in supplementary:
        size = len(record.text)
        if (
            auxiliary
            and auxiliary_chars + size > DEEPSEEK_FAST_LAYOUT_TARGET_CHARS
            and not can_use_soft_overflow(auxiliary_chars, size)
        ):
            groups.append(auxiliary)
            auxiliary = []
            auxiliary_chars = 0
        auxiliary.append(record)
        auxiliary_chars += size
    if auxiliary:
        groups.append(auxiliary)
    return groups


def is_official_deepseek_config(config: mineru.AITranslateConfig) -> bool:
    """Limit this cost-sensitive mode to DeepSeek's own API endpoint."""
    if str(getattr(config, "provider_id", "") or "").strip().lower() != "deepseek":
        return False
    try:
        host = (urlparse(str(getattr(config, "base_url", "") or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == "api.deepseek.com"


def wait_for_deepseek_cache_settle(should_stop=None, log=None) -> None:
    """Leave a visible one-second gap between the cache-sensitive stages."""
    (log or print)("正在等待 DeepSeek 服务端缓存就绪…")
    for _ in range(10):
        if should_stop and should_stop():
            raise RuntimeError("用户已停止翻译。")
        time.sleep(0.1)


def layout_translation_cache_identity(
    records: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    reference_context: str,
    translation_mode: str,
    full_markdown_context: str = "",
) -> str:
    payload = {
        "protocol": LAYOUT_TRANSLATION_PROTOCOL,
        "provider": str(getattr(config, "provider_id", "") or ""),
        "base_url": str(getattr(config, "base_url", "") or ""),
        "model": str(getattr(config, "model", "") or ""),
        "target_language": target_language,
        "translation_mode": translation_mode,
        "deepseek_fast_layout": bool(
            getattr(config, "deepseek_fast_layout_translation", False)
        ),
        "reference_hash": hashlib.sha256(reference_context.encode("utf-8")).hexdigest(),
        "full_markdown_hash": hashlib.sha256(full_markdown_context.encode("utf-8")).hexdigest()
        if full_markdown_context else "",
        "records": [[record.block_id, record.page, record.block_type, record.text] for record in records],
    }
    custom_instruction_hash = mineru.translation_custom_instruction_hash(config)
    if custom_instruction_hash:
        payload["custom_instruction_hash"] = custom_instruction_hash
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_layout_translation_checkpoint(
    cache_path: Path,
    records: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    cache_identity: str,
    translations: dict[str, str],
    formula_context: list[LayoutFormulaItem] | None,
    formula_replacements: dict[str, str],
    guide: str,
    translation_mode: str,
    *,
    complete: bool,
    completed_groups: int,
    group_count: int,
    concurrency: int,
) -> None:
    payload = {
        "schema_version": 2,
        "protocol": LAYOUT_TRANSLATION_PROTOCOL,
        "identity": cache_identity,
        "complete": complete,
        "target_language": target_language,
        "translation_mode": translation_mode,
        "model": str(getattr(config, "model", "") or ""),
        "translations": [
            {"id": record.block_id, "text": translations[record.block_id]}
            for record in records
            if record.block_id in translations
        ],
        "formula_context_count": len(formula_context or []),
        "formula_replacements": [
            {"id": formula_id, "tex": tex}
            for formula_id, tex in sorted(formula_replacements.items())
        ],
        "guide": guide,
        "completed_groups": completed_groups,
        "group_count": group_count,
        "concurrency": concurrency,
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(cache_path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def request_layout_group(
    group: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    guide: str,
    reference_context: str,
    formula_context: list[LayoutFormulaItem] | None,
    index: int,
    count: int,
    primary_system_message: dict,
    audit_dir: Path,
    *,
    log=None,
    preview_callback=None,
    reasoning_callback=None,
    should_stop=None,
    full_markdown_context: str = "",
    usage_callback=None,
) -> tuple[dict[str, str], dict[str, str], list[dict]]:
    if should_stop and should_stop():
        raise RuntimeError("用户已停止翻译。")
    prompt = build_translation_prompt(
        group, target_language, guide, reference_context, formula_context,
        full_markdown_context=full_markdown_context,
    )
    streamed_parts: list[str] = []

    def on_stream_delta(delta: str) -> None:
        if not delta:
            return
        streamed_parts.append(delta)
        if preview_callback:
            preview_callback(
                "正在生成排版译文...\n\n"
                f"- 当前返回块组: {index + 1}/{count}\n"
                f"- 本组块数: {len(group)}\n\n"
                "模型正在返回块级 JSON，完成后会由协调器按块 ID 合并。\n\n"
                + "".join(streamed_parts)[-3000:]
            )

    primary_messages = [
        primary_system_message,
        {"role": "user", "content": prompt},
    ]
    mineru.save_translation_request_audit(
        audit_dir,
        f"排版-块组翻译-第{index + 1}组",
        config,
        primary_messages,
        timeout=300,
    )
    response = mineru.ai_chat_completion(
        config,
        primary_messages,
        timeout=None,
        stream_callback=on_stream_delta,
        reasoning_callback=reasoning_callback,
        rate_limit_callback=log,
        usage_callback=usage_callback,
    )
    if should_stop and should_stop():
        raise RuntimeError("用户已停止翻译。")
    data = extract_json_object(response)
    expected = {record.block_id: record for record in group}
    translations: dict[str, str] = {}
    for item in data.get("translations", []):
        if not isinstance(item, dict):
            continue
        block_id = str(item.get("id") or "")
        record = expected.get(block_id)
        if not record:
            continue
        translations[block_id] = repair_record_translation(record, str(item.get("text") or ""))
    formula_replacements: dict[str, str] = {}
    (log or print)(f"已完成第 {index + 1}/{count} 组排版翻译（已处理 {len(translations)}/{len(group)} 个内容块）。")
    return translations, formula_replacements, primary_messages


def _translate_records_serial_legacy(
    records: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    cache_path: Path,
    max_chars: int,
    reference_context: str = "",
    formula_context: list[LayoutFormulaItem] | None = None,
    log=None,
    preview_callback=None,
    reasoning_callback=None,
) -> dict[str, str]:
    translations: dict[str, str] = {}
    formula_replacements: dict[str, str] = {}
    used_partial_cache = False
    primary_system_content = (
        "You are a professional academic paper translator and scientific copy editor. "
        "Translate with field-aware terminology, preserve scientific facts exactly, and keep layout block IDs stable. "
        "You must output strict JSON only, without Markdown fences."
    )
    custom_instruction_section = mineru.translation_custom_instruction_section(config)
    if custom_instruction_section:
        primary_system_content += "\n\n" + custom_instruction_section
    primary_system_message = {
        "role": "system",
        "content": primary_system_content,
    }
    # Retained for the retry path so its cached prefix exactly matches a
    # streamed first-pass request whenever there was one.
    primary_messages: list[dict] | None = None
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
        if not formula_context or "formula_replacements" in data:
            cached_translations = {
                str(item["id"]): str(item["text"])
                for item in data.get("translations", [])
                if isinstance(item, dict) and item.get("id")
            }
            cached_translations = repair_record_translations(records, cached_translations)
            formula_replacements = {
                str(item.get("id") or ""): str(item.get("tex") or "")
                for item in data.get("formula_replacements", [])
                if isinstance(item, dict) and item.get("id")
            }
            bad_cache_records = records_needing_retry(records, cached_translations, target_language)
            if bad_cache_records:
                sample = [record.block_id for record in bad_cache_records[:8]]
                (log or print)(f"检测到历史缓存中存在 {len(bad_cache_records)} 处待补全内容，已保留已有译文并继续补全未完成部分。")
                translations = cached_translations
                used_partial_cache = True
            else:
                apply_formula_replacements(formula_context, formula_replacements, log=log)
                return cached_translations

    guide = ""
    if not used_partial_cache:
        total_chars = sum(len(record.text) for record in records)
        groups = [records] if max_chars <= 0 or total_chars <= max_chars else split_records(records, max_chars)
        if len(groups) > 1:
            message = f"文档内容较长，已提取全局术语表，并将分为 {len(groups)} 组进行连贯翻译。"
            (log or print)(message)
            guide = build_global_guide(
                records,
                config,
                target_language,
                formula_context,
                audit_dir=cache_path.parent,
                log=log,
            )

        for index, group in enumerate(groups, 1):
            message = f"正在翻译第 {index}/{len(groups)} 组内容（共 {len(group)} 个文本块）…"
            (log or print)(message)
            if preview_callback:
                preview_callback(
                    "正在生成排版译文...\n\n"
                    f"- 当前块组: {index}/{len(groups)}\n"
                    f"- 本组块数: {len(group)}\n"
                    f"- 已完成块数: {len(translations)}/{len(records)}\n"
            )
            prompt = build_translation_prompt(group, target_language, guide, reference_context, formula_context)
            streamed_parts: list[str] = []

            def on_stream_delta(delta: str) -> None:
                if not delta:
                    return
                streamed_parts.append(delta)
                if preview_callback:
                    preview_callback(
                        "正在生成排版译文...\n\n"
                        f"- 当前块组: {index}/{len(groups)}\n"
                        f"- 已完成块数: {len(translations)}/{len(records)}\n\n"
                        "模型正在返回块级 JSON，完成后会自动回填到原版面。\n\n"
                        + "".join(streamed_parts)[-3000:]
                    )

            primary_messages = [
                primary_system_message,
                {"role": "user", "content": prompt},
            ]
            mineru.save_translation_request_audit(cache_path.parent, f"排版-块组翻译-第{index}组", config, primary_messages, timeout=300)
            response = mineru.ai_chat_completion(
                config,
                primary_messages,
                # Reasoning models can spend several minutes before emitting
                # the first JSON token.  Do not turn that into a failed
                # translation merely because a fixed client deadline elapsed:
                # the user can stop the task explicitly from the UI instead.
                timeout=None,
                stream_callback=on_stream_delta,
                reasoning_callback=reasoning_callback,
            )
            data = extract_json_object(response)
            for item in data.get("translations", []):
                if isinstance(item, dict) and item.get("id"):
                    record = next((record for record in group if record.block_id == str(item["id"])), None)
                    text = str(item.get("text") or "")
                    translations[str(item["id"])] = repair_record_translation(record, text) if record else text
            for item in data.get("formula_replacements", []):
                if isinstance(item, dict) and item.get("id"):
                    formula_replacements[str(item.get("id") or "")] = normalize_formula_tex(item.get("tex") or "")
            if preview_callback:
                preview_callback(
                    "正在生成排版译文...\n\n"
                    f"- 当前块组: {index}/{len(groups)} 已返回\n"
                    f"- 已完成块数: {len(translations)}/{len(records)}\n"
                )

    # A single retry is enough for a real omission.  Do not repeatedly punish
    # legitimate names/identifiers merely because a heuristic is uncertain.
    for retry_index in range(1, 2):
        bad_records = records_needing_retry(records, translations, target_language)
        if not bad_records:
            break
        sample = [record.block_id for record in bad_records[:8]]
        (log or print)(
            f"正在校对并自动补全未完成的文本块（第 {retry_index} 轮）…"
        )
        retry_prompt = (
            "补翻以下块。仅返回这些块的目标语言译文，不要输出全文，不要复制英文原文充当译文；标题和章节标题也必须翻译自然语言部分，同时保留编号、公式和变量。"
            "每个 id 必须只翻译其自身可见文本；尤其是列尾截断片段，不得补全下一块内容、合并相邻块或重复相邻块译文。"
            "若块内含 \\(...\\) 行内公式，阅读器会交给 MathJax 渲染；必须逐个原样保留其 TeX 和定界符，不能输出裸 TeX。"
            "Return ONLY valid JSON with this exact shape: "
            '{"translations":[{"id":"...","text":"..."}],"formula_replacements":[]}\n'
            f"{json.dumps({'blocks': block_payload(bad_records)}, ensure_ascii=False)}"
        )
        retry_streamed_parts: list[str] = []

        def on_retry_stream_delta(delta: str) -> None:
            if not delta:
                return
            retry_streamed_parts.append(delta)
            if preview_callback:
                preview_callback(
                    "正在补翻缺失版面块...\n\n"
                    f"- 重试轮次: {retry_index}/1\n"
                    f"- 待补块数: {len(bad_records)}\n\n"
                    + "".join(retry_streamed_parts)[-3000:]
                )

        if primary_messages is None:
            # A partial on-disk cache can enter retries without a first-pass
            # request in this process. Keep that recovery path functional;
            # it has no same-run prefix to reuse.
            primary_messages = [
                primary_system_message,
                {"role": "user", "content": build_translation_prompt(records, target_language, guide, reference_context, formula_context)},
            ]

        # 保持首轮的 system + 完整块组 user 消息逐字不变，使服务端可以
        # 命中首轮已写入的前缀缓存；只在末尾追加极小的补翻任务。
        # OpenAI Chat Completions 允许连续 user 消息，这里无需把上轮大 JSON
        # 回答再作为输入发送一次。
        retry_messages = [
            *primary_messages,
            {"role": "user", "content": retry_prompt},
        ]
        mineru.save_translation_request_audit(cache_path.parent, f"排版-块级补翻-第{retry_index}轮", config, retry_messages, timeout=240)
        retry_response = mineru.ai_chat_completion(
            config,
            retry_messages,
            # Keep the recovery request subject to the same user-controlled
            # waiting policy as the initial layout translation.
            timeout=None,
            stream_callback=on_retry_stream_delta,
            reasoning_callback=reasoning_callback,
            rate_limit_callback=log,
        )
        retry_data = extract_json_object(retry_response)
        retry_by_id = {record.block_id: record for record in bad_records}
        for item in retry_data.get("translations", []):
            if isinstance(item, dict) and item.get("id"):
                block_id = str(item["id"])
                text = str(item.get("text") or "")
                record = retry_by_id.get(block_id)
                translations[block_id] = repair_record_translation(record, text) if record else text

    bad_records = untranslated_or_missing_records(records, translations, target_language)
    if bad_records:
        sample = [record.block_id for record in bad_records[:12]]
        (log or print)(
            f"补全完成：保留已成功翻译的内容，未返回结果的部分保留原文。"
        )

    unsafe_records = unsafe_overexpanded_records(records, translations)
    unsafe_records.extend(
        record
        for record in suspicious_duplicate_translation_records(records, translations)
        if record.block_id not in {item.block_id for item in unsafe_records}
    )
    if unsafe_records:
        sample = [record.block_id for record in unsafe_records[:12]]
        (log or print)(
            f"检测到个别内容存在异常重复或格式错位，为确保版面准确性，已自动恢复为原文。"
        )
        for record in unsafe_records:
            translations[record.block_id] = record.text

    formula_failures = [
        (record, inline_formula_integrity_issue(record, translations.get(record.block_id, "")))
        for record in records
        if inline_formula_integrity_issue(record, translations.get(record.block_id, ""))
    ]
    if formula_failures:
        sample = "; ".join(f"{record.block_id}: {issue}" for record, issue in formula_failures[:4])
        (log or print)(f"检测到部分公式格式可能存在微小差异，已保留模型译文供核对。")

    for record in records:
        if record.block_id not in translations:
            translations[record.block_id] = record.text
    translations = repair_record_translations(records, translations)

    apply_formula_replacements(formula_context, formula_replacements, log=log)

    payload = {
        "target_language": target_language,
        "model": config.model,
        "translations": [{"id": record.block_id, "text": translations[record.block_id]} for record in records],
        "formula_context_count": len(formula_context or []),
        "formula_replacements": [
            {"id": formula_id, "tex": tex}
            for formula_id, tex in formula_replacements.items()
        ],
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return translations


def translate_records(
    records: list[LayoutTextBlock],
    config: mineru.AITranslateConfig,
    target_language: str,
    cache_path: Path,
    max_chars: int = LAYOUT_GROUP_MAX_CHARS,
    max_blocks: int = LAYOUT_GROUP_MAX_BLOCKS,
    concurrency: int = 0,
    translation_mode: str = "full_context",
    reference_context: str = "",
    formula_context: list[LayoutFormulaItem] | None = None,
    log=None,
    preview_callback=None,
    reasoning_callback=None,
    should_stop=None,
    full_markdown_context: str = "",
) -> dict[str, str]:
    requested_mode = (
        "chunked"
        if str(translation_mode or "").strip().lower() in {"chunked", "chunks"}
        else "full_context"
    )
    fast_mode = bool(getattr(config, "deepseek_fast_layout_translation", False))
    full_markdown_context = strip_markdown_images(full_markdown_context)
    if fast_mode and not is_official_deepseek_config(config):
        (log or print)("高速并发模式需要使用 DeepSeek 官方接口，当前已自动切换为标准排版翻译模式。")
        fast_mode = False
    translation_mode = "deepseek_fast" if fast_mode else requested_mode
    if fast_mode:
        max_chars = 0
        concurrency = DEEPSEEK_FAST_LAYOUT_CONCURRENCY
    elif translation_mode == "full_context":
        # 全文连续模式必须保持一个有顺序的完整上下文请求，不能并发拆开。
        max_chars = 0
        max_blocks = 0
        concurrency = 1
    else:
        concurrency = mineru.normalize_translation_request_concurrency(
            config.provider_id,
            concurrency,
        )
    cache_identity = layout_translation_cache_identity(
        records,
        config,
        target_language,
        reference_context,
        translation_mode,
        full_markdown_context,
    )
    translations: dict[str, str] = {}
    formula_replacements: dict[str, str] = {}
    guide = ""
    primary_system_content = (
        "You are a professional academic paper translator and scientific copy editor. "
        "Translate with field-aware terminology, preserve scientific facts exactly, and keep layout block IDs stable. "
        "You must output strict JSON only, without Markdown fences."
    )
    custom_instruction_section = mineru.translation_custom_instruction_section(config)
    if custom_instruction_section:
        primary_system_content += "\n\n" + custom_instruction_section
    primary_system_message = {
        "role": "system",
        "content": primary_system_content,
    }

    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
        compatible = not data.get("identity") or data.get("identity") == cache_identity
        if compatible and (not formula_context or "formula_replacements" in data):
            translations = repair_record_translations(
                records,
                {
                    str(item["id"]): str(item["text"])
                    for item in data.get("translations", [])
                    if isinstance(item, dict) and item.get("id")
                },
            )
            formula_replacements = {
                str(item.get("id") or ""): str(item.get("tex") or "")
                for item in data.get("formula_replacements", [])
                if isinstance(item, dict) and item.get("id")
            }
            guide = str(data.get("guide") or "")
            cached_classified = classify_retry_records(records, translations, target_language)
            cached_bad = [record for record, _reasons in cached_classified]
            if not cached_bad:
                apply_formula_replacements(formula_context, formula_replacements, log=log)
                return translations
            sample = [record.block_id for record in cached_bad[:8]]
            (log or print)(
                f"检测到历史缓存中存在待完善内容，已保留已有译文并继续处理剩余部分。"
            )
        elif not compatible:
            (log or print)("检测到翻译配置或原文已更新，正在重新生成排版译文。")

    bad_ids = {
        record.block_id
        for record in records_needing_retry(records, translations, target_language)
    }
    remaining = [
        record
        for record in records
        if record.block_id not in translations or record.block_id in bad_ids
    ]
    all_groups = (
        deepseek_fast_layout_groups(records, full_markdown_context)
        if fast_mode else split_records(records, max_chars, max_blocks)
    )
    source_group_index = {
        record.block_id: index
        for index, group in enumerate(all_groups)
        for record in group
    }
    groups = (
        deepseek_fast_layout_groups(remaining, full_markdown_context)
        if fast_mode else split_records(remaining, max_chars, max_blocks)
    )
    if len(all_groups) > 1 and not guide and not fast_mode:
        (log or print)(
            f"已准备全局术语指南，将分为 {len(all_groups)} 组进行并行翻译（最大并发数: {concurrency}）。"
        )
        guide = build_global_guide(
            records,
            config,
            target_language,
            formula_context,
            audit_dir=cache_path.parent,
            log=log,
        )

    primary_messages_by_group: dict[int, list[dict]] = {}
    completed_results: dict[int, tuple[dict[str, str], dict[str, str], list[dict]]] = {}
    base_translations = dict(translations)
    base_formula_replacements = dict(formula_replacements)
    fast_cache_telemetry: dict[int, dict[str, object]] = {}
    # ``elapsed_seconds`` in the per-request rows are intentionally summed in
    # the report (useful for API latency/cost diagnosis), but that total is not
    # the elapsed wall-clock time once the final wave is concurrent.  Retain
    # the initial-round boundaries and submitted-wave size as separate facts.
    fast_initial_round_started_at = time.monotonic() if fast_mode else 0.0
    fast_initial_round_finished_at = 0.0
    fast_parallel_wave_request_count = 0
    fast_parallel_worker_limit = 0

    def capture_usage(destination: dict[str, object]):
        def callback(value: dict) -> None:
            # A streamed response can publish usage more than once. Keep the
            # latest complete snapshot instead of adding duplicate totals.
            destination.clear()
            destination.update(value if isinstance(value, dict) else {})
        return callback

    def record_fast_cache_telemetry(
        index: int,
        usage: dict[str, object],
        phase: str,
        elapsed_seconds: float,
    ) -> None:
        if not fast_mode:
            return
        group = groups[index]
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        denominator = hit + miss
        fast_cache_telemetry[index] = {
            "phase": phase,
            "hit": hit,
            "miss": miss,
            "prompt": prompt,
            "completion": completion,
            "usage_received": bool(usage),
            "elapsed_seconds": elapsed_seconds,
        }
        rate = f"{hit / denominator:.1%}" if denominator else "API 未返回可计算的缓存 token"
        block_ids = ", ".join(record.block_id for record in group)
        (log or print)(
            f"DeepSeek 高速缓存请求 {index + 1}/{len(groups)}（{phase}）："
            f"块 {len(group)} 个、原文 {sum(len(record.text) for record in group)} 字符、"
            f"命中 {hit} tokens、未命中 {miss} tokens、命中率 {rate}、"
            f"输入 {prompt}、输出 {completion} tokens、耗时 {elapsed_seconds:.2f} 秒；块: {block_ids}"
        )
        # DeepSeek returns request-level rather than block-level cache usage.
        # Record every block's exact request association without claiming that
        # a particular block's individual tokens were demonstrably cached.
        for record in group:
            (log or print)(
                f"  缓存归属块 {record.block_id}（第 {record.page} 页/{record.block_type}）："
                f"所在请求命中 {hit}、未命中 {miss} tokens（API 仅提供请求级统计）。"
            )

    def emit_fast_cache_report() -> None:
        if not fast_mode:
            return
        entries = [fast_cache_telemetry[index] for index in sorted(fast_cache_telemetry)]
        hit = sum(int(item["hit"]) for item in entries)
        miss = sum(int(item["miss"]) for item in entries)
        prompt = sum(int(item["prompt"]) for item in entries)
        completion = sum(int(item["completion"]) for item in entries)
        elapsed_seconds = sum(float(item["elapsed_seconds"]) for item in entries)
        denominator = hit + miss
        covered_blocks = sum(len(groups[index]) for index in fast_cache_telemetry)
        rate = f"{hit / denominator:.1%}" if denominator else "不可计算（服务未返回缓存 usage）"
        initial_wall_seconds = (
            fast_initial_round_finished_at - fast_initial_round_started_at
            if fast_initial_round_finished_at and fast_initial_round_started_at
            else 0.0
        )
        warmup_count = min(DEEPSEEK_FAST_LAYOUT_WARMUP_REQUESTS, len(groups))
        (log or print)(
            "DeepSeek 高速并发翻译缓存统计报告（仅初轮全文 Markdown 请求；不含无上下文修复轮）：\n"
            f"- 调度：前 {warmup_count} 个请求串行预热（两次之间、以及探针后各等待 1 秒）；"
            f"后续已提交 {fast_parallel_wave_request_count} 个请求，工作线程上限 {fast_parallel_worker_limit}。\n"
            f"- 成功返回 usage 的请求: {sum(bool(item['usage_received']) for item in entries)}/{len(groups)}\n"
            f"- 已完成请求: {len(entries)}/{len(groups)}；关联块: {covered_blocks}/{len(remaining)}\n"
            f"- 缓存命中: {hit} tokens；未命中: {miss} tokens；请求级命中率: {rate}\n"
            f"- API 报告输入: {prompt} tokens；输出: {completion} tokens；"
            f"初轮墙钟耗时: {initial_wall_seconds:.2f} 秒；请求耗时累计: {elapsed_seconds:.2f} 秒\n"
            "- 说明：DeepSeek API 不返回每个块的独立缓存 token。本报告已逐块记录其所属请求及该请求的命中数据，不能将请求级命中误解为每一块逐 token 的确定命中。"
        )
        for index in sorted(fast_cache_telemetry):
            item = fast_cache_telemetry[index]
            hit = int(item["hit"])
            miss = int(item["miss"])
            denominator = hit + miss
            rate = f"{hit / denominator:.1%}" if denominator else "不可计算"
            ids = ", ".join(record.block_id for record in groups[index])
            (log or print)(
                f"- 请求 {index + 1}（{item['phase']}）：{len(groups[index])} 块，"
                f"输入 {item['prompt']}、输出 {item['completion']}、命中 {hit}、未命中 {miss}、"
                f"命中率 {rate}、耗时 {float(item['elapsed_seconds']):.2f} 秒；块: {ids}"
            )

    def merge_completed_results() -> None:
        """Merge in source order so checkpoints never depend on finish order."""
        nonlocal translations, formula_replacements
        translations = dict(base_translations)
        formula_replacements = dict(base_formula_replacements)
        for result_index in sorted(completed_results):
            group_translations, group_formulas, primary_messages = completed_results[result_index]
            translations.update(group_translations)
            formula_replacements.update(group_formulas)
            primary_messages_by_group[result_index] = primary_messages

    if groups:
        if translation_mode == "full_context":
            (log or print)(
                f"开始排版全文连续翻译：共 {len(remaining)} 个版面文本块，"
                "整篇作为 1 个 JSON 请求，不限制字符数和块数。"
            )
        elif fast_mode:
            (log or print)(
                f"开始 DeepSeek 高速并发排版翻译：{len(groups)} 组，每组目标约 "
                f"{DEEPSEEK_FAST_LAYOUT_TARGET_CHARS} 字符；标题优先单独翻译，"
                "跨页/跨栏未完句保持在同一组。先串行预热并验证缓存命中，再最多 "
                f"{DEEPSEEK_FAST_LAYOUT_CONCURRENCY} 路并发。"
            )
        else:
            (log or print)(
                f"开始排版分块翻译：待处理 {len(groups)} 组，"
                f"字符上限 {max_chars}，块数上限 {max_blocks}，"
                f"最大并发 {concurrency}。"
            )
        pending_indexes = list(range(len(groups)))
        if fast_mode:
            cache_hit_confirmed = False
            cache_probe_rate: float | None = None
            warmup_count = min(DEEPSEEK_FAST_LAYOUT_WARMUP_REQUESTS, len(groups))
            for index in range(warmup_count):
                if should_stop and should_stop():
                    raise RuntimeError("用户已停止翻译。")
                usage: dict = {}

                try:
                    request_started = time.monotonic()
                    completed_results[index] = request_layout_group(
                        groups[index], config, target_language, guide, reference_context,
                        formula_context, index, len(groups), primary_system_message,
                        cache_path.parent, log=log, preview_callback=preview_callback,
                        reasoning_callback=reasoning_callback, should_stop=should_stop,
                        full_markdown_context=full_markdown_context,
                        usage_callback=capture_usage(usage),
                    )
                    elapsed_seconds = time.monotonic() - request_started
                    merge_completed_results()
                except Exception as exc:
                    (log or print)(f"高速模式预热组 {index + 1}/{len(groups)} 请求失败：{exc}")
                    if index == 0 and warmup_count > 1:
                        wait_for_deepseek_cache_settle(should_stop, log)
                    continue
                hit_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
                miss_tokens = int(usage.get("prompt_cache_miss_tokens") or 0)
                phase = "标题预热" if index == 0 else "正文缓存探针"
                record_fast_cache_telemetry(index, usage, phase, elapsed_seconds)
                (log or print)(
                    f"DeepSeek 缓存探针 {index + 1}/{warmup_count}：命中 {hit_tokens} tokens，"
                    f"未命中 {miss_tokens} tokens。"
                )
                # Always send the second request after the title warm-up.  A
                # hit on an old cache is useful telemetry, but the body probe
                # is the current-run gate for releasing 100-way concurrency.
                if index == warmup_count - 1 and warmup_count > 1:
                    cache_tokens = hit_tokens + miss_tokens
                    if cache_tokens:
                        cache_probe_rate = hit_tokens / cache_tokens
                        cache_hit_confirmed = (
                            cache_probe_rate >= DEEPSEEK_FAST_LAYOUT_MIN_CACHE_HIT_RATE
                        )
                if index == 0 and warmup_count > 1:
                    wait_for_deepseek_cache_settle(should_stop, log)
            if warmup_count > 1:
                # The body probe has ended. Give the cache one further second
                # before releasing the concurrent request wave.
                wait_for_deepseek_cache_settle(should_stop, log)
            # A cold cache can need one more request to settle. If the body
            # probe misses the 50% gate, verify a third request can reach the
            # stricter steady-state threshold before releasing concurrency.
            if (
                not cache_hit_confirmed
                and warmup_count > 1
                and len(groups) > warmup_count
            ):
                index = warmup_count
                if should_stop and should_stop():
                    raise RuntimeError("用户已停止翻译。")
                usage: dict = {}
                (log or print)("正在进一步验证服务端缓存命中状态…")
                try:
                    request_started = time.monotonic()
                    completed_results[index] = request_layout_group(
                        groups[index], config, target_language, guide, reference_context,
                        formula_context, index, len(groups), primary_system_message,
                        cache_path.parent, log=log, preview_callback=preview_callback,
                        reasoning_callback=reasoning_callback, should_stop=should_stop,
                        full_markdown_context=full_markdown_context,
                        usage_callback=capture_usage(usage),
                    )
                    elapsed_seconds = time.monotonic() - request_started
                    merge_completed_results()
                except Exception as exc:
                    (log or print)(f"高速模式第三次缓存探针请求失败：{exc}")
                else:
                    hit_tokens = int(usage.get("prompt_cache_hit_tokens") or 0)
                    miss_tokens = int(usage.get("prompt_cache_miss_tokens") or 0)
                    record_fast_cache_telemetry(index, usage, "正文缓存复检", elapsed_seconds)
                    cache_tokens = hit_tokens + miss_tokens
                    cache_probe_rate = hit_tokens / cache_tokens if cache_tokens else None
                    cache_hit_confirmed = (
                        cache_probe_rate is not None
                        and cache_probe_rate >= DEEPSEEK_FAST_LAYOUT_RETRY_MIN_CACHE_HIT_RATE
                    )
            pending_indexes = [index for index in pending_indexes if index not in completed_results]
            if cache_hit_confirmed:
                (log or print)(f"已确认 DeepSeek 完整 Markdown 前缀缓存命中，启动最多 {concurrency} 路并发。")
            else:
                if pending_indexes and warmup_count > 1:
                    merge_completed_results()
                    write_layout_translation_checkpoint(
                        cache_path,
                        records,
                        config,
                        target_language,
                        cache_identity,
                        translations,
                        formula_context,
                        formula_replacements,
                        guide,
                        translation_mode,
                        complete=False,
                        completed_groups=len(completed_results),
                        group_count=len(groups),
                        concurrency=1,
                    )
                    rate_text = (
                        f"{cache_probe_rate:.1%}"
                        if cache_probe_rate is not None
                        else "不可计算（服务未返回缓存 token 统计）"
                    )
                    message = (
                        f"DeepSeek 正文缓存第三次探针命中率为 {rate_text}，未达到 "
                        f"{DEEPSEEK_FAST_LAYOUT_RETRY_MIN_CACHE_HIT_RATE:.0%} 的高速并发安全阈值。"
                        "已停止本次高速翻译，未发送剩余高速请求，以避免重复全文输入造成较高 token 消耗。"
                        "可尝试重试，或切换普通速度翻译模式"
                    )
                    (log or print)(message)
                    raise RuntimeError(DEEPSEEK_FAST_CACHE_PROTECTION_ERROR_PREFIX + message)
                concurrency = 1
                (log or print)("高速缓存探针未达到并发条件，但没有剩余块组，无需启动后续请求。")

            fast_parallel_wave_request_count = len(pending_indexes)
            fast_parallel_worker_limit = min(concurrency, len(pending_indexes))

        if pending_indexes:
            executor = ThreadPoolExecutor(
                max_workers=min(concurrency, len(pending_indexes)),
                thread_name_prefix="layout-translate",
            )
            futures = {}

            def request_fast_group_with_telemetry(
                index: int,
                group: list[LayoutTextBlock],
                usage: dict[str, object],
            ) -> tuple[tuple[dict[str, str], dict[str, str], list[dict]], float]:
                request_started = time.monotonic()
                result = request_layout_group(
                    group,
                    config,
                    target_language,
                    guide,
                    reference_context,
                    formula_context,
                    index,
                    len(groups),
                    primary_system_message,
                    cache_path.parent,
                    log=log,
                    preview_callback=preview_callback,
                    reasoning_callback=reasoning_callback,
                    should_stop=should_stop,
                    full_markdown_context=full_markdown_context,
                    usage_callback=capture_usage(usage),
                )
                return result, time.monotonic() - request_started

            for index in pending_indexes:
                group = groups[index]
                usage: dict[str, object] = {}
                future = (
                    executor.submit(request_fast_group_with_telemetry, index, group, usage)
                    if fast_mode else executor.submit(
                        request_layout_group,
                        group,
                        config,
                        target_language,
                        guide,
                        reference_context,
                        formula_context,
                        index,
                        len(groups),
                        primary_system_message,
                        cache_path.parent,
                        log=log,
                        preview_callback=preview_callback,
                        reasoning_callback=reasoning_callback,
                        should_stop=should_stop,
                        full_markdown_context="",
                    )
                )
                futures[future] = (index, usage)

            for future in as_completed(futures):
                index, usage = futures[future]
                try:
                    result = future.result()
                    if fast_mode:
                        completed_results[index], elapsed_seconds = result
                    else:
                        completed_results[index] = result
                except Exception as exc:
                    (log or print)(
                        f"第 {index + 1}/{len(groups)} 组翻译出现异常，"
                        f"其他组继续执行并保存已有进度：{exc}"
                    )
                    continue

                if fast_mode:
                    phase = "并发正文/补充批次" if concurrency > 1 else "串行正文/补充批次"
                    record_fast_cache_telemetry(index, usage, phase, elapsed_seconds)

                # Network results may finish out of order. Rebuild shared state
                # strictly in source-group order before every atomic checkpoint.
                merge_completed_results()
                write_layout_translation_checkpoint(
                    cache_path,
                    records,
                    config,
                    target_language,
                    cache_identity,
                    translations,
                    formula_context,
                    formula_replacements,
                    guide,
                    translation_mode,
                    complete=False,
                    completed_groups=len(completed_results),
                    group_count=len(groups),
                    concurrency=concurrency,
                )
                if preview_callback:
                    preview_callback(
                        "正在生成排版译文...\n\n"
                        f"- 已完成块组: {len(completed_results)}/{len(groups)}\n"
                        f"- 已缓存块数: {len(translations)}/{len(records)}\n"
                        f"- 最大并发: {concurrency}\n"
                    )
            executor.shutdown(wait=True)

    if fast_mode:
        fast_initial_round_finished_at = time.monotonic()

    # One targeted pass is enough after the initial translation. More rounds
    # repeatedly spend tokens on inherently uncertain OCR/format diagnostics.
    format_retry_attempted: set[str] = set()
    max_retry_rounds = 1
    for retry_round in range(1, max_retry_rounds + 1):
        bad_classified = classify_retry_records(records, translations, target_language)
        if not fast_mode:
            bad_classified = [
                item for item in bad_classified
                if not is_format_only_retry_reasons(item[1]) or item[0].block_id not in format_retry_attempted
            ]
        bad_records = [record for record, _reasons in bad_classified]
        if not bad_records:
            break
        retry_reasons_by_id = {
            record.block_id: reasons
            for record, reasons in bad_classified
        }
        # One document-level recovery request avoids turning a few heuristic
        # findings into several sequential model calls. Every item carries its
        # source and current translation, so no wider block context is needed.
        retry_jobs: list[tuple[list[LayoutTextBlock], bool]] = [(bad_records, False)]
        if not fast_mode:
            for record in bad_records:
                reasons = retry_reasons_by_id.get(record.block_id, ())
                if is_format_only_retry_reasons(reasons):
                    format_retry_attempted.add(record.block_id)
        sample = [record.block_id for record in bad_records[:8]]
        detail_sample = [
            f"{record.block_id}: {retry_details_for_record(record, translations, retry_reasons_by_id.get(record.block_id, ()))[:220]}"
            for record in bad_records[:3]
        ]
        (log or print)(
            f"正在对 {len(bad_records)} 处待校对文本块执行统一校对与补全（第 {retry_round}/{max_retry_rounds} 轮）…"
        )
        retry_translation_snapshot = dict(translations)

        def request_retry_group(
            retry_index: int,
            retry_job: tuple[list[LayoutTextBlock], bool],
        ) -> dict[str, str]:
            if should_stop and should_stop():
                raise RuntimeError("用户已停止翻译。")
            retry_group, retain_context = retry_job
            source_indexes = {
                source_group_index.get(record.block_id)
                for record in retry_group
                if source_group_index.get(record.block_id) is not None
            }
            source_index = next(iter(source_indexes)) if len(source_indexes) == 1 else None
            if retry_round == 1 and retain_context:
                primary_messages = (
                    primary_messages_by_group[source_index]
                    if source_index is not None and source_index in primary_messages_by_group
                    else [
                        primary_system_message,
                        {
                            "role": "user",
                            "content": build_translation_prompt(
                                all_groups[source_index] if source_index is not None else retry_group,
                                target_language,
                                guide,
                                reference_context,
                                formula_context,
                            ),
                        },
                    ]
                )
            else:
                # Providers without prefix caching should not pay the complete
                # source-context cost repeatedly.  The initial translation and
                # first recovery already had it; later passes are surgical.
                primary_messages = [primary_system_message]
            retry_payload = []
            for record in retry_group:
                reasons = retry_reasons_by_id.get(record.block_id, ())
                symbol_only = is_format_only_retry_reasons(reasons)
                retry_payload.append(
                    {
                        "id": record.block_id,
                        "page": record.page,
                        "type": record.block_type,
                        "text": record.text,
                        "current_translation": retry_translation_snapshot.get(record.block_id, ""),
                        "retry_reasons": list(reasons),
                        "retry_details": retry_details_for_record(
                            record,
                            retry_translation_snapshot,
                            reasons,
                        ),
                        "repair_mode": "symbol-format-only" if symbol_only else "retranslate",
                    }
                )
            retry_format_only = bool(retry_payload) and all(
                item["repair_mode"] == "symbol-format-only" for item in retry_payload
            )
            retry_config = copy.copy(config)
            if retry_format_only:
                # A surgical structural check should not inherit a large
                # reasoning budget from the document translation request.
                retry_config.thinking_mode = "disabled"
                retry_config.reasoning_effort = "minimal"
            retry_prompt = (
                (
                    "这是一次极窄的公式/JSON格式修复。不要解释推理，不要重译或润色自然语言。"
                    "retry_reasons 是启发式质检信号，不是事实结论，可能误报或漏报；retry_details 只是可能误报的诊断。你必须自行核对源文、current_translation 与数学结构，不得机械服从这些提示。若标准化后的数学内容等价（空格、冗余花括号、\\mathrm 包装或 OCR 枚举标记差异），或无法确认真实错误，原样返回 current_translation。"
                    "只有确认确有问题时，才修复指出的公式定界符/数学主体，并保持周围正文、引用和标识符不变。只返回形如 {\"translations\":[{\"id\":\"...\",\"text\":\"...\"}],\"formula_replacements\":[]} 的 JSON。\n"
                )
                if retry_format_only
                else (
                    "请只核对下列块。retry_reasons 是启发式质检信号，不是事实结论，可能误报或漏报；retry_details 也只是程序的猜测。你必须自行判断源文与 current_translation 是否存在真实问题，并决定最准确的输出；仅在确认问题时才修改，无法确认时原样返回 current_translation。"
                    "不得补全、合并或重复相邻块；每个 id 只返回自身可见文本。只返回形如 {\"translations\":[{\"id\":\"...\",\"text\":\"...\"}],\"formula_replacements\":[]} 的 JSON，不要解释。\n"
                )
            ) + f"{json.dumps({'blocks': retry_payload}, ensure_ascii=False)}"
            retry_messages = [*primary_messages, {"role": "user", "content": retry_prompt}]
            mineru.save_translation_request_audit(
                cache_path.parent,
                f"排版-块级补翻-第{retry_round}轮-第{retry_index + 1}组",
                retry_config,
                retry_messages,
                timeout=240,
            )
            retry_streamed_parts: list[str] = []

            def on_retry_stream_delta(delta: str) -> None:
                if not delta:
                    return
                retry_streamed_parts.append(delta)
                if preview_callback:
                    preview_callback(
                        f"正在补翻缺失版面块（第 {retry_round}/{max_retry_rounds} 轮，{retry_index + 1}/{len(retry_jobs)} 组）...\n\n"
                        + "".join(retry_streamed_parts)[-3000:]
                    )

            response = mineru.ai_chat_completion(
                retry_config,
                retry_messages,
                timeout=None,
                stream_callback=on_retry_stream_delta,
                reasoning_callback=reasoning_callback,
                rate_limit_callback=log,
            )
            data = extract_json_object(response)
            expected = {record.block_id: record for record in retry_group}
            return {
                block_id: repair_record_translation(expected[block_id], str(item.get("text") or ""))
                for item in data.get("translations", [])
                if isinstance(item, dict)
                and (block_id := str(item.get("id") or "")) in expected
            }

        retry_results: dict[int, dict[str, str]] = {}
        retry_base_translations = dict(translations)
        with ThreadPoolExecutor(
            max_workers=min(concurrency, len(retry_jobs)),
            thread_name_prefix="layout-retry",
        ) as executor:
            futures = {
                executor.submit(request_retry_group, index, job): index
                for index, job in enumerate(retry_jobs)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    retry_results[index] = future.result()
                except Exception as exc:
                    (log or print)(
                        f"补全校对第 {index + 1} 组未成功，已保留其他成功部分缓存：{exc}"
                    )
                    continue
                translations = dict(retry_base_translations)
                for result_index in sorted(retry_results):
                    translations.update(retry_results[result_index])
                write_layout_translation_checkpoint(
                    cache_path,
                    records,
                    config,
                    target_language,
                    cache_identity,
                    translations,
                    formula_context,
                    formula_replacements,
                    guide,
                    translation_mode,
                    complete=False,
                    completed_groups=len(completed_results),
                    group_count=len(groups),
                    concurrency=concurrency,
                )

    remaining_classified = classify_retry_records(records, translations, target_language)
    if remaining_classified:
        sample = [record.block_id for record, _reasons in remaining_classified[:12]]
        (log or print)(
            f"校对完成：个别未确认项已保留当前结果，避免额外消耗 Token。"
        )
    unsafe_records = unsafe_overexpanded_records(records, translations)
    unsafe_records.extend(
        record
        for record in suspicious_duplicate_translation_records(records, translations)
        if record.block_id not in {item.block_id for item in unsafe_records}
    )
    if unsafe_records:
        sample = [record.block_id for record in unsafe_records[:12]]
        (log or print)(
            f"检测到个别内容存在异常重复，已自动恢复为原文以确保准确性。"
        )
    for record in unsafe_records:
        translations[record.block_id] = record.text
    non_structural_formula_differences = [
        record
        for record in records
        if record.block_id in translations
        and inline_formula_integrity_issue(record, translations.get(record.block_id, ""))
        and not inline_formula_retry_issue(record, translations.get(record.block_id, ""))
    ]
    if non_structural_formula_differences:
        sample = [record.block_id for record in non_structural_formula_differences[:12]]
        (log or print)(
            f"部分公式存在轻微格式差异，但数学结构完整，已保留译文供人工核对。"
        )
    for record in records:
        if record.block_id not in translations:
            translations[record.block_id] = record.text
    translations = repair_record_translations(records, translations)
    apply_formula_replacements(formula_context, formula_replacements, log=log)
    emit_fast_cache_report()
    write_layout_translation_checkpoint(
        cache_path,
        records,
        config,
        target_language,
        cache_identity,
        translations,
        formula_context,
        formula_replacements,
        guide,
        translation_mode,
        complete=True,
        completed_groups=len(groups),
        group_count=len(groups),
        concurrency=concurrency,
    )
    return translations


def saved_ai_config(provider_override: str = "", model_override: str = "") -> mineru.AITranslateConfig:
    settings = mineru.app_config.load_settings()
    provider_id = provider_override.strip() or getattr(settings, "ai_provider", "") or "deepseek"
    provider = getattr(settings, "providers", {}).get(provider_id)
    api_key = mineru.app_config.load_secret(provider_id, "api_key")
    if not api_key:
        return mineru.build_ai_translate_config(provider_id, log=print)
    default_url = mineru.provider_default_base_url(provider_id)
    base_url = provider.base_url if provider and provider.base_url else default_url
    model = model_override.strip() or (provider.model if provider and provider.model else "")
    if not model:
        model = mineru.build_ai_translate_config(provider_id, log=print).model
    return mineru.AITranslateConfig(
        provider_id=provider_id,
        api_key=api_key,
        base_url=mineru.normalize_ai_base_url(base_url, provider_id),
        model=model,
        request_body_mode=getattr(provider, "request_body_mode", "codex"),
        thinking_mode="enabled" if provider_id == "deepseek" and getattr(settings, "translation_deepseek_thinking_enabled", True) else "disabled",
        reasoning_effort=getattr(settings, "translation_deepseek_reasoning_effort", "default"),
        deepseek_fast_layout_translation=(
            provider_id == "deepseek"
            and bool(getattr(settings, "translation_deepseek_fast_layout_enabled", True))
        ),
        custom_translation_instruction=str(getattr(settings, "translation_custom_instruction", "") or ""),
    )


def apply_translations(records: list[LayoutTextBlock], translations: dict[str, str]) -> None:
    for record in records:
        translated = translations.get(record.block_id)
        if translated:
            set_block_text(record.block, translated)


def extract_layout_assets(layout_html: str) -> tuple[str, str]:
    styles = "\n".join(re.findall(r"<style>(.*?)</style>", layout_html, flags=re.S | re.I))
    scripts = "\n".join(re.findall(r"<script>(.*?)</script>", layout_html, flags=re.S | re.I))
    return styles, scripts


def source_layout_stamp(layout_html: str) -> str:
    """Stable identity of the source layout structure used by a translation."""
    match = re.search(r"<!--\s*layout-preview version=([^>]+?)\s*-->", layout_html or "", flags=re.I)
    source_marker = match.group(0) if match else (layout_html or "")[:4096]
    return hashlib.sha1(source_marker.encode("utf-8", errors="replace")).hexdigest()[:16]


def _float_style_value(style: str, name: str) -> float | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*(-?\d+(?:\.\d+)?)px", style or "", flags=re.I)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def source_flow_role_index(source_html: str) -> dict[int, list[dict]]:
    role_index: dict[int, list[dict]] = {}
    page_pattern = re.compile(
        r'<section\b[^>]*class="[^"]*\blayout-page-wrap\b[^"]*"[^>]*data-sync-page-index="(\d+)"[^>]*>(.*?)(?=<section\b[^>]*class="[^"]*\blayout-page-wrap\b|</main>)',
        flags=re.S | re.I,
    )
    stream_pattern = re.compile(
        r'<div\b[^>]*class="([^"]*\blayout-flow-stream\b[^"]*)"[^>]*style="([^"]*)"[^>]*data-flow-kind="([^"]+)"',
        flags=re.S | re.I,
    )
    for page_match in page_pattern.finditer(source_html or ""):
        try:
            page_index = int(page_match.group(1))
        except ValueError:
            continue
        entries: list[dict] = []
        for stream_match in stream_pattern.finditer(page_match.group(2) or ""):
            classes = stream_match.group(1) or ""
            role_match = re.search(r"\bdebug-([A-Za-z0-9_]+)\b", classes)
            if not role_match:
                continue
            style = stream_match.group(2) or ""
            left = _float_style_value(style, "left")
            top = _float_style_value(style, "top")
            width = _float_style_value(style, "width")
            height = _float_style_value(style, "height")
            if None in {left, top, width, height}:
                continue
            entries.append(
                {
                    "role": role_match.group(1),
                    "kind": stream_match.group(3),
                    "bbox": [left, top, left + width, top + height],
                }
            )
        role_index[page_index] = entries
    return role_index


def _bbox_overlap_score(a: list[float], b: list[float]) -> float:
    left = max(float(a[0]), float(b[0]))
    top = max(float(a[1]), float(b[1]))
    right = min(float(a[2]), float(b[2]))
    bottom = min(float(a[3]), float(b[3]))
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    a_area = max(1.0, (float(a[2]) - float(a[0])) * (float(a[3]) - float(a[1])))
    b_area = max(1.0, (float(b[2]) - float(b[0])) * (float(b[3]) - float(b[1])))
    return overlap / min(a_area, b_area)


def source_role_for_stream(role_index: dict[int, list[dict]], page_index: int, bbox: list[float], flow_kind: str) -> str:
    candidates = [entry for entry in role_index.get(page_index, []) if entry.get("kind") == flow_kind]
    if not candidates:
        return ""
    best_role = ""
    best_score = 0.0
    for entry in candidates:
        source_bbox = entry.get("bbox") or [0, 0, 0, 0]
        score = _bbox_overlap_score(source_bbox, bbox)
        if score > best_score:
            best_score = score
            best_role = str(entry.get("role") or "")
    return best_role if best_score >= 0.82 else ""


def _json_ready_layout_bundle(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_ready_layout_bundle(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready_layout_bundle(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready_layout_bundle(item) for item in value]
    return value


def ensure_layout_fit_revision(bundle: dict, reset: bool) -> str:
    """Keep runtime upgrades reusable while isolating every real translation."""
    current = str(bundle.get("_layout_fit_revision") or "").strip()
    if reset or not current:
        current = uuid.uuid4().hex
        bundle["_layout_fit_revision"] = current
    return current


def render_translated_layout(
    markdown_path: Path,
    bundle: dict,
    source_layout_path: Path,
    out_path: Path,
    debug_overlay: bool = False,
    reset_fit_cache: bool = True,
    bundle_out_path: Path | None = None,
) -> Path:
    source_html = source_layout_path.read_text(encoding="utf-8", errors="replace")
    source_stamp = source_layout_stamp(source_html)
    # _json_ready_layout_bundle already constructs a detached recursive copy.
    # Deep-copying the same page/model tree first doubled peak memory for large
    # papers without adding isolation.
    serializable_bundle = _json_ready_layout_bundle(bundle)
    # A translation command is always a new fit generation, even if a new
    # model happens to return byte-for-byte identical wording. The one caller
    # that only upgrades preview runtime explicitly opts out below.
    ensure_layout_fit_revision(serializable_bundle, reset=reset_fit_cache)
    layout_cache_key = hashlib.sha1(
        (
            source_stamp
            + ":"
            + json.dumps(serializable_bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ).encode("utf-8")
    ).hexdigest()[:16]
    canonical_output_path = mineru.layout_translation_preview_html_path(markdown_path)
    layout_cache_scope = hashlib.sha1(
        str(canonical_output_path.resolve()).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    bundle_out_path = bundle_out_path or mineru.layout_translation_bundle_path(markdown_path)
    layout_css, layout_scripts = extract_layout_assets(source_html)
    body_class = (
        "layout-debug layout-translated layout-fit-pending"
        if debug_overlay
        else "layout-production layout-translated layout-fit-pending"
    )
    source_roles = source_flow_role_index(source_html)
    page_info = bundle["page_info"]
    model_pages = bundle["model_pages"]
    asset_dir = bundle["asset_dir"]
    # Translation bundles replace every source line with one translated line,
    # so their line counts cannot prove a stable reading lane.  Reuse the
    # immutable source layout geometry when it is available; fall back to the
    # translated bundle only for legacy/incomplete jobs.
    profile_page_info = page_info
    source_layout_path = Path(str(bundle.get("layout_path") or ""))
    try:
        source_layout_payload = json.loads(source_layout_path.read_text(encoding="utf-8"))
        source_pages = source_layout_payload.get("pdf_info") if isinstance(source_layout_payload, dict) else None
        if isinstance(source_pages, list) and len(source_pages) == len(page_info):
            profile_page_info = source_pages
    except (OSError, json.JSONDecodeError):
        pass
    single_column_profile = (
        mineru.infer_single_column_profile(profile_page_info)
        if mineru.single_column_body_promotion_enabled()
        else None
    )
    page_contexts: list[dict] = []
    ocr_boxes_by_page: dict[int, list[list[float]]] = {}
    body_streams: list[dict] = []
    ref_streams: list[dict] = []

    # Keep translated layout reconstruction in lockstep with the source
    # preview: short prose on equation/figure-heavy pages may rely on nearby
    # page body context, rather than only on this page's long paragraphs.
    provisional_pages: list[dict] = []
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            provisional_pages.append({"profiles": {}})
            continue
        page["page_idx"] = page_index
        raw_page_size = page.get("page_size") or [612, 792]
        page_width = max(1.0, float(raw_page_size[0]))
        page_height = max(1.0, float(raw_page_size[1]))
        model_page = model_pages[page_index] if page_index < len(model_pages) else []
        ocr_boxes = mineru.collect_model_ocr_boxes(model_page, page_width, page_height)
        flow_items, _absolute_blocks = mineru.streamable_layout_items(page, page_width, page_height, ocr_boxes)
        provisional_pages.append({
            "profiles": mineru.body_column_profiles(flow_items, page_width, page_height),
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
        page_width = max(1.0, float(raw_page_size[0]))
        page_height = max(1.0, float(raw_page_size[1]))
        model_page = model_pages[page_index] if page_index < len(model_pages) else []
        ocr_boxes = mineru.collect_model_ocr_boxes(model_page, page_width, page_height)
        ocr_boxes_by_page[page_index] = ocr_boxes
        flow_items, absolute_blocks = mineru.streamable_layout_items(page, page_width, page_height, ocr_boxes)
        flow_items = mineru.promote_text_items_to_body(
            flow_items,
            page_width,
            page_height,
            promotion_contexts.get(page_index),
        )
        flow_items = mineru.promote_stable_single_column_items(
            flow_items,
            page_width,
            page_height,
            single_column_profile,
        )
        flow_items = mineru.inherit_stable_single_column_short_items(
            flow_items,
            page_width,
            page_height,
            single_column_profile,
        )
        flow_items = mineru.merge_vertical_body_items(flow_items, absolute_blocks)
        flow_items = mineru.mark_equation_dense_body_items(flow_items, absolute_blocks, page_height)
        flow_items = mineru.merge_reference_items(flow_items)
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
            roles = [str(item.get("debug_role") or "") for item in stream.get("items") or []]
            if roles and all(role == roles[0] for role in roles):
                stream["debug_role"] = roles[0]
            flow_kind = "ref_text" if all(item.get("kind") == "ref_text" for item in stream.get("items") or []) else "text"
            inherited_role = source_role_for_stream(source_roles, page_index, stream.get("bbox") or [0, 0, 0, 0], flow_kind)
            if inherited_role and str(stream.get("debug_role") or "") != "body_inherited":
                stream["debug_role"] = inherited_role
            if all(item.get("kind") == "ref_text" for item in stream.get("items") or []):
                ref_streams.append(stream)
            elif str(stream.get("debug_role") or "") in {"body_candidate", "merged_body"}:
                body_streams.append(stream)
        page_contexts.append(
            {
                "page_index": page_index,
                "page_width": page_width,
                "page_height": page_height,
                "model_page": model_page,
                "ocr_boxes": ocr_boxes,
                "streams": streams,
                "absolute_blocks": absolute_blocks,
                "column_rights": mineru.column_right_edges_from_streams(streams, page_width),
            }
        )

    inferred_body_style = mineru.solve_uniform_stream_style(
        body_streams,
        ocr_boxes_by_page,
        refs_only=False,
    )

    # 中文译文需要比源论文的英文几何行距更宽松。
    # 所有正文块以同一行距作为初始值；后续发生碰撞时，
    # 现有浏览器排版逻辑仍只压缩发生碰撞的单个正文块。
    translated_body_line_ratio = min(
        1.28,
        max(1.22, inferred_body_style[1] + 0.06),
    )

    uniform_styles = {
        "body_text": (
            inferred_body_style[0],
            translated_body_line_ratio,
        ),
        "ref_text": mineru.solve_uniform_stream_style(
            ref_streams,
            ocr_boxes_by_page,
            refs_only=True,
        ),
    }
    rendered_pages: list[str] = []
    main_title_seen = False
    for context in page_contexts:
        page_index = context["page_index"]
        page_width = context["page_width"]
        page_height = context["page_height"]
        scale = mineru.layout_preview_scale(page_width)
        blocks: list[str] = []
        column_rights = context.get("column_rights") or {}
        occupied_boxes: list[list[float]] = []
        barrier_boxes = mineru.layout_barrier_boxes(context["absolute_blocks"])
        for stream in context["streams"]:
            mineru.expand_narrow_text_stream_to_column(stream, page_width, column_rights, barrier_boxes)
        mineru.retreat_intruding_column_boundaries(context["streams"])
        for stream in context["streams"]:
            stream_bbox = stream.get("bbox")
            if isinstance(stream_bbox, list) and len(stream_bbox) >= 4:
                occupied_boxes.append([float(part) for part in stream_bbox[:4]])
            rendered_stream = mineru.render_flow_stream(stream, page_width, page_height, context["ocr_boxes"], uniform_styles)
            if rendered_stream:
                blocks.append(rendered_stream)
        for block in context["absolute_blocks"]:
            if not isinstance(block, dict):
                continue
            occupied_boxes.extend(mineru.collect_layout_occupied_boxes(block))
            block_type = str(block.get("type") or "").lower()
            if block_type in {"title", "text"}:
                force_main_title = block_type == "title" and not main_title_seen
                rendered = mineru.render_layout_text_block(block, page_width, page_height, column_rights, force_main_title=force_main_title)
                if force_main_title and rendered:
                    main_title_seen = True
                if rendered:
                    blocks.append(rendered)
            elif block_type in {"table", "chart", "image"}:
                blocks.extend(mineru.render_layout_block_children(block, asset_dir, page_width, page_height, column_rights))
            elif block_type == "interline_equation":
                # Equations must use the same inferred physical-column gutter
                # as the source preview.  Without this, translated pages fall
                # back to MinerU's ink-tight formula bbox and put the number
                # beside a short formula instead of at the single/multi-column
                # reading edge.
                rendered = mineru.render_layout_equation_block(
                    block,
                    asset_dir,
                    page_width,
                    page_height,
                    column_rights,
                )
                if rendered:
                    blocks.append(rendered)
            else:
                rendered = mineru.render_layout_generic_block(block, asset_dir, page_width, page_height, column_rights)
                if rendered:
                    blocks.append(rendered)
        # MinerU's aside_text blocks are often vertically oriented page metadata
        # (for example a journal name or timestamp). They are not translation
        # targets, and drawing them as normal HTML text makes the translated
        # layout look broken. Keep them in the source preview, but omit them
        # from the translated page.
        blocks.extend(
            mineru.render_layout_model_items(
                context["model_page"],
                page_width,
                page_height,
                column_rights,
                occupied_boxes,
                excluded_types={"aside_text"},
            )
        )
        rendered_pages.append(
            f"""<section class="layout-page-wrap" data-sync-page-index="{page_index}" data-page-width="{page_width:.2f}" data-page-height="{page_height:.2f}"><div class="layout-page-label">Page {page_index + 1}</div>"""
            f"""<div class="layout-page-shell" data-page-width="{page_width:.2f}" data-page-height="{page_height:.2f}" """
            f"""style="width:{page_width * scale:.2f}px;height:{page_height * scale:.2f}px;">"""
            f"""<div class="layout-page" style="width:{page_width:.2f}px;height:{page_height:.2f}px;transform:scale({scale:.6f});transform-origin:top left;">"""
            f"""{"".join(blocks)}</div></div></section>"""
        )

    out_path.write_text(
        f"""<!doctype html><html><head><meta charset="utf-8">
<!-- layout-translation-runtime-v29-gallop-tail -->
<!-- layout-translation-source-stamp={source_stamp} -->
<!-- layout-translation-preview source="{html.escape(str(markdown_path))}" -->
{mineru.mathjax_script_html()}
{mineru.qt_webchannel_script_html()}
<style>
{layout_css}
body.layout-translated .layout-flow-stream[data-flow-kind="text"],
body.layout-translated .layout-flow-stream[data-flow-kind="ref_text"] {{
  word-break: break-word;
}}
.layout-build-badge::after {{ content: " translated"; }}
</style></head><body class="{body_class}" data-layout-cache-version="{mineru.LAYOUT_FIT_CACHE_VERSION}"
data-layout-cache-key="{layout_cache_key}"
data-layout-cache-scope="{layout_cache_scope}"
data-layout-progress="正在准备全文排版（共 {len(rendered_pages)} 页）…">
<script data-layout-fit-disk-cache></script>
{mineru.layout_fit_cache_bootstrap_html(layout_cache_key, layout_cache_scope)}<main class="layout-doc">
<div class="layout-build-badge">layout translation</div>
<p class="layout-note">此视图把 AI 译文按 MinerU 块 ID 回填到原始 bbox 中；图片、表格、公式和页码保持原位。</p>
{''.join(rendered_pages)}
</main><script>
{layout_scripts}
</script></body></html>""",
        encoding="utf-8",
    )
    # The caller may render into temporary files and publish the HTML / bundle
    # together only after both are complete.  Writing this after the HTML also
    # prevents a failed render from replacing the prior reusable bundle.
    with bundle_out_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(serializable_bundle, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate MinerU layout blocks and render a layout-preserving translated preview.")
    parser.add_argument("markdown", type=Path, help="Path to full.md or full.cleaned.md")
    parser.add_argument("--target", default="简体中文")
    parser.add_argument("--provider", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--max-chars", type=int, default=LAYOUT_GROUP_MAX_CHARS)
    parser.add_argument("--max-blocks", type=int, default=LAYOUT_GROUP_MAX_BLOCKS)
    parser.add_argument("--concurrency", type=int, default=LAYOUT_GROUP_CONCURRENCY)
    parser.add_argument("--mode", choices=("full_context", "chunked"), default="full_context")
    parser.add_argument("--force", action="store_true", help="Ignore cached layout_translation_blocks.json")
    args = parser.parse_args()

    markdown_path = args.markdown
    bundle = mineru.load_layout_preview_bundle(markdown_path)
    if not bundle:
        raise SystemExit(f"Cannot find MinerU layout bundle for {markdown_path}")

    source_layout_path = mineru.render_layout_preview_html(markdown_path, log=print)
    if not source_layout_path:
        raise SystemExit("Cannot render source layout preview.")

    translated_bundle = copy.deepcopy(bundle)
    records = iter_translatable_blocks(translated_bundle["page_info"])
    if not records:
        raise SystemExit("No translatable layout blocks found.")
    print(f"提取到 {len(records)} 个可翻译版面文本块。", flush=True)
    print("独立公式保持本地原样；行内公式仅随所属完整文本块发送。", flush=True)

    cache_path = markdown_path.parent / f"layout_translation_blocks.{mineru.translation_language_suffix(args.target)}.json"
    if args.force and cache_path.exists():
        cache_path.unlink()
    config = saved_ai_config(args.provider, args.model)
    translations = translate_records(
        records,
        config,
        args.target,
        cache_path,
        args.max_chars,
        args.max_blocks,
        args.concurrency,
        translation_mode=args.mode,
        formula_context=None,
        full_markdown_context=(
            markdown_path.read_text(encoding="utf-8", errors="replace")
            if getattr(config, "deepseek_fast_layout_translation", False)
            else ""
        ),
    )
    apply_translations(records, translations)

    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", markdown_path.stem)
    out_path = markdown_path.with_name(f"preview_layout_translated_current.{safe_stem}.html")
    render_translated_layout(
        markdown_path,
        translated_bundle,
        source_layout_path,
        out_path,
        reset_fit_cache=True,
    )
    print(f"TRANSLATED_PREVIEW: {out_path}", flush=True)
    print(f"TRANSLATION_CACHE: {cache_path}", flush=True)


if __name__ == "__main__":
    main()
