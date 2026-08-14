"""Probe parsed MinerU layouts for single-, multi-, and mixed-column geometry.

This is a diagnostic tool, not a document parser.  It deliberately uses only
MinerU block types, bounding boxes, line counts, and page-to-page repetition;
it never inspects the language or wording of a document.

Example:
    ./.venv/Scripts/python.exe tools/layout_family_probe.py ^
        "C:/path/to/parsed_documents" ^
        --output output/layout_family_probe
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WIDE_TEXT_RATIO = 0.72
MIN_WIDE_HEIGHT_RATIO = 0.025
MIN_NARROW_TEXT_RATIO = 0.16
MAX_NARROW_TEXT_RATIO = 0.70
LEFT_EDGE_CLUSTER_RATIO = 0.07
MIN_PARALLEL_LEFT_DELTA_RATIO = 0.12


@dataclass(frozen=True)
class PageEvidence:
    page_index: int
    text_blocks: int
    wide_blocks: int
    parallel_columns: int
    profile: str


def valid_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        left, top, right, bottom = (float(item) for item in value[:4])
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return [left, top, right, bottom]


def page_dimensions(page: dict[str, Any]) -> tuple[float, float]:
    raw = page.get("page_size")
    if isinstance(raw, list) and len(raw) >= 2:
        try:
            width, height = float(raw[0]), float(raw[1])
            if width > 0 and height > 0:
                return width, height
        except (TypeError, ValueError):
            pass
    return 612.0, 792.0


def top_level_text_boxes(page: dict[str, Any]) -> list[list[float]]:
    """Return only ordinary top-level text blocks.

    ``title``, captions, references, lists, and media are intentionally not
    used as evidence for a document's primary reading columns.  This depends
    on the layout-model block type rather than any text content.
    """

    boxes: list[list[float]] = []
    for block in page.get("preproc_blocks") or []:
        if not isinstance(block, dict) or str(block.get("type") or "").lower() != "text":
            continue
        bbox = valid_bbox(block.get("bbox"))
        if bbox:
            boxes.append(bbox)
    return boxes


def vertical_overlap_ratio(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap / max(1.0, min(left[3] - left[1], right[3] - right[1]))


def clustered_left_edges(boxes: list[list[float]], page_width: float) -> int:
    """Count distinct left-edge anchors with a scale-independent tolerance."""

    tolerance = page_width * LEFT_EDGE_CLUSTER_RATIO
    anchors: list[float] = []
    for left in sorted(box[0] for box in boxes):
        if not anchors or abs(left - anchors[-1]) > tolerance:
            anchors.append(left)
        else:
            anchors[-1] = (anchors[-1] + left) / 2.0
    return len(anchors)


def simultaneous_column_count(boxes: list[list[float]], page_width: float) -> int:
    """Find the largest set of text boxes that visibly occupy parallel columns.

    A page does not count as two-column merely because it contains two
    successive paragraphs.  Evidence requires boxes that overlap vertically,
    have separated left edges, and do not overlap horizontally.
    """

    maximum = 1 if boxes else 0
    min_left_delta = page_width * MIN_PARALLEL_LEFT_DELTA_RATIO
    for seed in boxes:
        parallel = [seed]
        for candidate in boxes:
            if candidate is seed:
                continue
            horizontal_overlap = max(0.0, min(seed[2], candidate[2]) - max(seed[0], candidate[0]))
            if horizontal_overlap > page_width * 0.03:
                continue
            if abs(seed[0] - candidate[0]) < min_left_delta:
                continue
            if vertical_overlap_ratio(seed, candidate) < 0.18:
                continue
            parallel.append(candidate)
        maximum = max(maximum, clustered_left_edges(parallel, page_width))
    return maximum


def classify_page(page: dict[str, Any], page_index: int) -> PageEvidence:
    page_width, page_height = page_dimensions(page)
    boxes = top_level_text_boxes(page)
    usable = [
        box
        for box in boxes
        if (box[2] - box[0]) / page_width >= MIN_NARROW_TEXT_RATIO
        and (box[3] - box[1]) / page_height >= 0.008
    ]
    wide = [
        box
        for box in usable
        if (box[2] - box[0]) / page_width >= WIDE_TEXT_RATIO
        and (box[3] - box[1]) / page_height >= MIN_WIDE_HEIGHT_RATIO
    ]
    narrow = [
        box
        for box in usable
        if MIN_NARROW_TEXT_RATIO <= (box[2] - box[0]) / page_width <= MAX_NARROW_TEXT_RATIO
    ]
    parallel_columns = simultaneous_column_count(narrow, page_width)
    if wide and parallel_columns >= 2:
        profile = "mixed-page"
    elif parallel_columns >= 3:
        profile = "three-or-more-columns"
    elif parallel_columns == 2:
        profile = "two-columns"
    elif wide:
        profile = "single-column"
    else:
        profile = "insufficient-evidence"
    return PageEvidence(page_index, len(usable), len(wide), parallel_columns, profile)


def classify_layout_pages(pages: list[dict[str, Any]]) -> tuple[str, int, list[PageEvidence]]:
    evidence = [classify_page(page, index) for index, page in enumerate(pages) if isinstance(page, dict)]
    profiles = Counter(item.profile for item in evidence)
    single_pages = profiles["single-column"]
    mixed_pages = profiles["mixed-page"]
    two_pages = profiles["two-columns"]
    three_pages = profiles["three-or-more-columns"]
    # A mixed first page is common in otherwise single-column papers: title
    # and author blocks can share a page with a small side panel, while every
    # subsequent reading page is a stable single flow.  It is page-level
    # evidence, but not enough by itself to call the *document* mixed.  A
    # document is mixed only when it has proven parallel-column pages as well
    # as proven single-column pages.
    proven_multi_pages = two_pages + three_pages

    if proven_multi_pages and single_pages:
        family = "mixed-layout"
        confidence = min(97, 62 + 8 * proven_multi_pages + 5 * single_pages)
    elif three_pages >= 2:
        family = "three-or-more-column-layout"
        confidence = min(97, 55 + 14 * three_pages)
    elif two_pages >= 2:
        family = "two-column-layout"
        confidence = min(97, 55 + 14 * two_pages)
    elif single_pages >= 2:
        family = "single-column-layout"
        confidence = min(97, 55 + 12 * single_pages)
    elif three_pages:
        family, confidence = "probable-three-or-more-column-layout", 52
    elif two_pages:
        family, confidence = "probable-two-column-layout", 52
    elif single_pages:
        family, confidence = "probable-single-column-layout", 50
    else:
        family, confidence = "insufficient-layout-evidence", 0
    return family, confidence, evidence


def locate_layout_file(document_dir: Path) -> Path | None:
    for candidate in (document_dir / "mineru_result" / "layout.json", document_dir / "layout.json"):
        if candidate.is_file():
            return candidate
    return None


def inspect_document(document_dir: Path) -> dict[str, Any] | None:
    layout_path = locate_layout_file(document_dir)
    if not layout_path:
        return None
    try:
        payload = json.loads(layout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "document": document_dir.name,
            "path": str(document_dir),
            "layout_path": str(layout_path),
            "error": str(error),
        }
    pages = payload.get("pdf_info") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return {
            "document": document_dir.name,
            "path": str(document_dir),
            "layout_path": str(layout_path),
            "error": "layout.json has no pdf_info page list",
        }
    family, confidence, evidence = classify_layout_pages(pages)
    return {
        "document": document_dir.name,
        "path": str(document_dir),
        "layout_path": str(layout_path),
        "pages": len(pages),
        "family": family,
        "confidence": confidence,
        "page_evidence": [item.__dict__ for item in evidence],
    }


def markdown_report(documents: list[dict[str, Any]], input_root: Path) -> str:
    lines = [
        "# 文献版式探针报告",
        "",
        f"扫描目录：`{input_root}`",
        "",
        "本报告只使用 MinerU 的块类型、页尺寸和坐标框；不读取任何正文语言或关键词。",
        "",
        "| 文献目录 | 页数 | 推测版式 | 置信度 | 单栏页 | 双栏页 | 三栏+页 | 混合页 |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for document in documents:
        if document.get("error"):
            lines.append(f"| {document['document']} | - | 读取失败 | - | - | - | - | - |")
            continue
        counts = Counter(item["profile"] for item in document["page_evidence"])
        lines.append(
            "| {document} | {pages} | {family} | {confidence}% | {single} | {two} | {three} | {mixed} |".format(
                document=document["document"],
                pages=document["pages"],
                family=document["family"],
                confidence=document["confidence"],
                single=counts["single-column"],
                two=counts["two-columns"],
                three=counts["three-or-more-columns"],
                mixed=counts["mixed-page"],
            )
        )
    for document in documents:
        lines.extend(["", f"## {document['document']}"])
        if document.get("error"):
            lines.extend(["", f"读取失败：`{document['error']}`"])
            continue
        lines.extend([
            "",
            f"- 判定：`{document['family']}`（置信度 {document['confidence']}%）",
            f"- 布局文件：`{document['layout_path']}`",
            "- 逐页证据：",
            "",
            "| 页 | 页内普通文本框 | 宽正文候选框 | 同时并列栏数 | 页面判定 |",
            "|---:|---:|---:|---:|---|",
        ])
        for item in document["page_evidence"]:
            lines.append(
                "| {page} | {blocks} | {wide} | {columns} | {profile} |".format(
                    page=item["page_index"] + 1,
                    blocks=item["text_blocks"],
                    wide=item["wide_blocks"],
                    columns=item["parallel_columns"],
                    profile=item["profile"],
                )
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_root", type=Path, help="包含已解析文献子目录的目录")
    parser.add_argument("--output", type=Path, default=Path("output/layout_family_probe"), help="报告输出目录")
    args = parser.parse_args()
    input_root = args.input_root.resolve()
    if not input_root.is_dir():
        parser.error(f"input_root is not a directory: {input_root}")
    documents = [result for child in sorted(input_root.iterdir()) if child.is_dir() if (result := inspect_document(child))]
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "layout_family_probe.md"
    json_path = args.output / "layout_family_probe.json"
    report_path.write_text(markdown_report(documents, input_root), encoding="utf-8")
    json_path.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Scanned {len(documents)} parsed document(s).")
    print(f"Markdown report: {report_path.resolve()}")
    print(f"JSON evidence: {json_path.resolve()}")
    for document in documents:
        if document.get("error"):
            print(f"- {document['document']}: ERROR {document['error']}")
        else:
            print(f"- {document['document']}: {document['family']} ({document['confidence']}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
