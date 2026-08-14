"""Optional, geometry-only promotion for stable single-column reading flows.

The module deliberately has no dependency on the renderer.  A caller can turn
it off and return to the legacy promotion path without changing that path's
rules.  It uses only MinerU block geometry and line counts, never text content
or document language.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MIN_WIDTH_RATIO = 0.72
MIN_HEIGHT_RATIO = 0.025
MIN_SOURCE_LINES = 3
LEFT_TOLERANCE_RATIO = 0.045
RIGHT_TOLERANCE_RATIO = 0.055
MIN_SUPPORTING_PAGES = 2
MIN_SUPPORTING_BLOCKS = 3
# Short derivation transitions often have an ink-tight right edge.  Their left
# edge is still meaningful, so use a deliberately modest minimum width rather
# than requiring them to span the full reading lane.
MIN_SHORT_WIDTH_TO_LANE_RATIO = 0.20


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) < 4:
        return None
    try:
        left, top, right, bottom = (float(part) for part in value[:4])
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _page_size(page: dict[str, Any]) -> tuple[float, float]:
    raw_size = page.get("page_size") or [612.0, 792.0]
    try:
        width, height = float(raw_size[0]), float(raw_size[1])
    except (IndexError, TypeError, ValueError):
        return 612.0, 792.0
    return (width, height) if width > 0 and height > 0 else (612.0, 792.0)


def _source_line_count(block: dict[str, Any]) -> int:
    lines = block.get("lines")
    return len(lines) if isinstance(lines, list) else 0


@dataclass(frozen=True)
class SingleColumnProfile:
    """A repeated full-width text lane proven by multiple source pages."""

    left_ratio: float
    right_ratio: float
    supporting_pages: frozenset[int]
    supporting_blocks: int

    def matches(self, bbox: list[float], page_width: float, page_height: float, source_lines: int) -> bool:
        parsed = _bbox(bbox)
        if not parsed or page_width <= 0 or page_height <= 0:
            return False
        left, top, right, bottom = parsed
        width_ratio = (right - left) / page_width
        height_ratio = (bottom - top) / page_height
        if source_lines < MIN_SOURCE_LINES or height_ratio < MIN_HEIGHT_RATIO:
            return False
        return (
            width_ratio >= MIN_WIDTH_RATIO
            and abs(left / page_width - self.left_ratio) <= LEFT_TOLERANCE_RATIO
            and abs(right / page_width - self.right_ratio) <= RIGHT_TOLERANCE_RATIO
        )


def infer_single_column_profile(page_info: list[dict[str, Any]]) -> SingleColumnProfile | None:
    """Infer a stable wide text lane from raw MinerU geometry.

    A single wide block on an article front page is intentionally insufficient.
    The same lane must occur in at least two pages and three substantial text
    blocks before this optional path can promote anything.
    """

    candidates: list[tuple[int, float, float]] = []
    for page_index, page in enumerate(page_info):
        if not isinstance(page, dict):
            continue
        page_width, page_height = _page_size(page)
        for block in page.get("preproc_blocks") or []:
            if not isinstance(block, dict) or str(block.get("type") or "").lower() != "text":
                continue
            bbox = _bbox(block.get("bbox"))
            if not bbox or _source_line_count(block) < MIN_SOURCE_LINES:
                continue
            left, top, right, bottom = bbox
            if (right - left) / page_width < MIN_WIDTH_RATIO or (bottom - top) / page_height < MIN_HEIGHT_RATIO:
                continue
            candidates.append((page_index, left / page_width, right / page_width))

    best: list[tuple[int, float, float]] = []
    for _page_index, left, right in candidates:
        cluster = [
            candidate
            for candidate in candidates
            if abs(candidate[1] - left) <= LEFT_TOLERANCE_RATIO
            and abs(candidate[2] - right) <= RIGHT_TOLERANCE_RATIO
        ]
        if len(cluster) > len(best):
            best = cluster
    supporting_pages = frozenset(candidate[0] for candidate in best)
    if len(best) < MIN_SUPPORTING_BLOCKS or len(supporting_pages) < MIN_SUPPORTING_PAGES:
        return None
    return SingleColumnProfile(
        left_ratio=sum(candidate[1] for candidate in best) / len(best),
        right_ratio=sum(candidate[2] for candidate in best) / len(best),
        supporting_pages=supporting_pages,
        supporting_blocks=len(best),
    )


def promote_stable_single_column_items(
    flow_items: list[dict[str, Any]],
    page_width: float,
    page_height: float,
    profile: SingleColumnProfile | None,
) -> list[dict[str, Any]]:
    """Promote only substantial blocks that match an inferred full-width lane.

    Narrow author/address boxes and one- or two-line mathematical transition
    text deliberately remain outside this promotion.  They can be locally
    rendered later, but cannot become global body-font constraints through this
    feature.
    """

    if profile is None:
        return flow_items
    promoted: list[dict[str, Any]] = []
    for item in flow_items:
        if (
            item.get("kind") != "text"
            or item.get("from_list")
            or item.get("symbol_glossary")
            or str(item.get("debug_role") or "") == "toc"
        ):
            promoted.append(item)
            continue
        bbox = item.get("bbox")
        source_lines = int(item.get("original_line_count") or 0)
        if not isinstance(bbox, list) or not profile.matches(bbox, page_width, page_height, source_lines):
            promoted.append(item)
            continue
        copy = dict(item)
        copy["debug_role"] = "body_candidate"
        # Keep the legacy coarse ``side=full`` presentation label, but give
        # merge/style code a real column key rather than the sentinel ``full``.
        copy["column_key"] = "single-column"
        promoted.append(copy)
    return promoted


def _page_has_parallel_reading_lanes(flow_items: list[dict[str, Any]], page_width: float, page_height: float) -> bool:
    """Return true when this *page* contains two substantial side-by-side lanes.

    This is deliberately a conservative veto.  A false positive merely leaves
    a short sentence on the legacy path; a false negative could apply a
    single-column style inside a multi-column page, so two substantial text
    regions with overlapping vertical bands are enough to block inheritance.
    """

    lanes: list[tuple[float, float, float, float]] = []
    for item in flow_items:
        if item.get("kind") != "text" or item.get("from_list"):
            continue
        bbox = _bbox(item.get("bbox"))
        if not bbox or int(item.get("original_line_count") or 0) < MIN_SOURCE_LINES:
            continue
        left, top, right, bottom = bbox
        width_ratio = (right - left) / page_width
        height_ratio = (bottom - top) / page_height
        if not (0.18 <= width_ratio <= 0.72) or height_ratio < MIN_HEIGHT_RATIO:
            continue
        lanes.append((left, top, right, bottom))
    for index, first in enumerate(lanes):
        for second in lanes[index + 1 :]:
            vertical_overlap = min(first[3], second[3]) - max(first[1], second[1])
            min_height = min(first[3] - first[1], second[3] - second[1])
            horizontal_gap = max(first[0], second[0]) - min(first[2], second[2])
            if (
                min_height > 0
                and vertical_overlap >= min_height * 0.20
                and horizontal_gap >= page_width * 0.035
            ):
                return True
    return False


def _page_has_single_column_anchor(
    flow_items: list[dict[str, Any]],
    page_width: float,
    page_height: float,
    profile: SingleColumnProfile,
) -> bool:
    """Require this page, not merely this document, to show the wide lane."""

    return any(
        item.get("kind") == "text"
        and profile.matches(
            item.get("bbox") or [],
            page_width,
            page_height,
            int(item.get("original_line_count") or 0),
        )
        for item in flow_items
    )


def inherit_stable_single_column_short_items(
    flow_items: list[dict[str, Any]],
    page_width: float,
    page_height: float,
    profile: SingleColumnProfile | None,
) -> list[dict[str, Any]]:
    """Give later short, lane-aligned prose the body style without anchoring it.

    MinerU commonly shrinks a one-line derivation transition to its ink width.
    On a document whose single reading lane has already been proven, such a
    block can safely *inherit* the body style when it starts at that lane's
    left edge.  It deliberately receives ``body_inherited`` rather than
    ``body_candidate``: callers must exclude it from the global body-style
    solve, so a narrow fragment can never pull the whole document's font down.

    Page zero is intentionally outside this recovery pass.  That is where
    author, affiliation, and address panels occur; the normal legacy path
    continues to render them locally.
    """

    if profile is None or page_width <= 0 or page_height <= 0:
        return flow_items
    # A document-level profile only proves that it contains a single-column
    # region.  Mixed papers are common, so recover short text only on pages
    # that independently show the full lane and no side-by-side reading lanes.
    if (
        not _page_has_single_column_anchor(flow_items, page_width, page_height, profile)
        or _page_has_parallel_reading_lanes(flow_items, page_width, page_height)
    ):
        return flow_items
    lane_width = max(1.0, (profile.right_ratio - profile.left_ratio) * page_width)
    inherited: list[dict[str, Any]] = []
    for item in flow_items:
        if (
            item.get("kind") != "text"
            or item.get("from_list")
            or item.get("symbol_glossary")
            or str(item.get("debug_role") or "") != "text"
            or int(item.get("page_index") or 0) <= 0
        ):
            inherited.append(item)
            continue
        bbox = _bbox(item.get("bbox"))
        source_lines = int(item.get("original_line_count") or 0)
        if not bbox or source_lines not in {1, 2}:
            inherited.append(item)
            continue
        left, _top, right, _bottom = bbox
        width = right - left
        left_matches_lane = abs(left / page_width - profile.left_ratio) <= LEFT_TOLERANCE_RATIO
        stays_inside_lane = right / page_width <= profile.right_ratio + RIGHT_TOLERANCE_RATIO
        if (
            not left_matches_lane
            or not stays_inside_lane
            or width < lane_width * MIN_SHORT_WIDTH_TO_LANE_RATIO
        ):
            inherited.append(item)
            continue
        copy = dict(item)
        copy["debug_role"] = "body_inherited"
        copy["column_key"] = "single-column"
        inherited.append(copy)
    return inherited
