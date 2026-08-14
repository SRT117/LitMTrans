"""Validated, self-contained SVG renderers for document-chat diagrams."""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict, deque


MINDMAP_V2_MARKER = "<!-- litmtrans-mindmap-v2 -->"
FLOWCHART_V2_MARKER = "<!-- litmtrans-flowchart-v2 -->"
MAX_NODES = 64
MAX_EDGES = 128
MINDMAP_KINDS = {
    "root", "background", "problem", "gap", "hypothesis", "method",
    "data", "result", "mechanism", "comparison", "validation",
    "contribution", "limitation", "implication", "other",
}
FLOWCHART_TYPES = {
    "terminator", "process", "decision", "io", "input", "output",
    "subprocess", "database", "document",
}
FLOWCHART_ROLES = {
    "background", "gap", "question", "hypothesis", "design", "method",
    "evidence", "result", "inference", "mechanism", "validation",
    "conclusion", "limitation", "other",
}


def _importance(value) -> int:
    try:
        return max(1, min(3, int(value or 1)))
    except (TypeError, ValueError):
        return 1


def _evidence(value) -> list[dict]:
    return [dict(item) for item in value[:6] if isinstance(item, dict)] if isinstance(value, list) else []


def _payload_after_marker(text: str, marker: str) -> dict | None:
    match = re.match(r"^\s*" + re.escape(marker) + r"\s*", str(text or ""))
    if not match:
        return None
    try:
        payload = json.loads(str(text)[match.end():].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("version") == 2 else None


def parse_mindmap_v2(text: str) -> dict | None:
    payload = _payload_after_marker(text, MINDMAP_V2_MARKER)
    nodes = payload.get("nodes") if payload else None
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        return None
    by_id: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            return None
        node_id, label = str(node.get("id") or ""), str(node.get("label") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", node_id) or not label or node_id in by_id:
            return None
        parent = node.get("parentId")
        if parent is not None and not isinstance(parent, str):
            return None
        kind = str(node.get("kind") or "other").lower()
        by_id[node_id] = {
            **node,
            "id": node_id,
            "label": label[:300],
            "detail": str(node.get("detail") or "").strip()[:420],
            "kind": kind if kind in MINDMAP_KINDS else "other",
            "importance": _importance(node.get("importance")),
            "evidence": _evidence(node.get("evidence")),
            "children": [],
        }
    roots = [node for node in by_id.values() if node.get("parentId") is None]
    if len(roots) != 1:
        return None
    for node in by_id.values():
        parent_id = node.get("parentId")
        if parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                return None
            parent["children"].append(node)
    seen: set[str] = set()

    def visit(node: dict, depth: int) -> bool:
        if node["id"] in seen or depth > 5:
            return False
        seen.add(node["id"])
        return all(visit(child, depth + 1) for child in node["children"])

    root = roots[0]
    return {
        "title": str(payload.get("title") or root["label"]).strip()[:120],
        "mode": str(payload.get("mode") or "mindmap").strip()[:40],
        "root": root,
    } if visit(root, 0) and len(seen) == len(by_id) else None


def parse_flowchart_v2(text: str) -> dict | None:
    payload = _payload_after_marker(text, FLOWCHART_V2_MARKER)
    nodes, edges = (payload.get("nodes"), payload.get("edges")) if payload else (None, None)
    if not isinstance(nodes, list) or not isinstance(edges, list) or not 1 <= len(nodes) <= MAX_NODES or len(edges) > MAX_EDGES:
        return None
    by_id: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            return None
        node_id, label = str(node.get("id") or ""), str(node.get("label") or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", node_id) or not label or node_id in by_id:
            return None
        node_type = str(node.get("type") or "process").lower()
        if node_type not in FLOWCHART_TYPES:
            return None
        by_id[node_id] = {
            **node,
            "id": node_id,
            "label": label[:300],
            "detail": str(node.get("detail") or "").strip()[:420],
            "type": "io" if node_type in {"input", "output"} else node_type,
            "importance": _importance(node.get("importance")),
            "role": str(node.get("role") or "other").lower() if str(node.get("role") or "other").lower() in FLOWCHART_ROLES else "other",
            "evidence": _evidence(node.get("evidence")),
        }
    clean_edges = []
    for edge in edges:
        if not isinstance(edge, dict):
            return None
        source, target = str(edge.get("from") or ""), str(edge.get("to") or "")
        if source not in by_id or target not in by_id:
            return None
        clean_edges.append({
            "from": source,
            "to": target,
            "label": str(edge.get("label") or "").strip()[:32],
            "relation": str(edge.get("relation") or "").strip()[:32],
        })
    requested = str(payload.get("direction") or payload.get("layout") or "").upper()
    direction = requested if requested in {"TB", "LR"} else ("LR" if len(nodes) <= 18 else "TB")
    return {
        "title": str(payload.get("title") or "流程图").strip()[:120],
        "mode": str(payload.get("mode") or "flowchart").strip()[:40],
        "direction": direction,
        "nodes": list(by_id.values()),
        "edges": clean_edges,
    }


def _text_units(value: str) -> float:
    return sum(1.0 if ord(character) > 255 else 0.56 for character in str(value or ""))


def _wrap_text(value: str, max_units: float, max_lines: int = 4) -> list[str]:
    """Wrap mixed Chinese/Latin labels without cutting escaped HTML entities."""
    lines, current, units = [], [], 0.0
    for character in re.sub(r"\s+", " ", str(value or "")).strip():
        cost = 1.0 if ord(character) > 255 else 0.56
        if current and units + cost > max_units:
            lines.append("".join(current).strip())
            current, units = [], 0.0
            if len(lines) >= max_lines:
                break
        current.append(character)
        units += cost
    if current and len(lines) < max_lines:
        lines.append("".join(current).strip())
    consumed = "".join(lines).replace(" ", "")
    original = re.sub(r"\s+", "", str(value or ""))
    if lines and len(consumed) < len(original):
        lines[-1] = lines[-1].rstrip("…") + "…"
    return lines or [""]


def _node_metrics(label: str, detail: str = "", root: bool = False) -> tuple[int, int, dict[str, list[str]]]:
    content_units = max(_text_units(label), min(_text_units(detail), 24))
    width = max(166, min(282 if not root else 302, int(min(content_units, 24) * 11 + 36)))
    title_lines = _wrap_text(label, max(9, (width - 30) / 11.5), 3)
    detail_lines = _wrap_text(detail, max(12, (width - 34) / 9.6), 4) if detail else []
    height = 28 + len(title_lines) * 19 + (8 + len(detail_lines) * 16 if detail_lines else 0)
    return width, max(54, height), {"title": title_lines, "detail": detail_lines}


def _svg_text(lines: list[str], x: float, y: float, css_class: str = "") -> str:
    top = y - (len(lines) - 1) * 9
    return "".join(
        f'<tspan class="{css_class}" x="{x:.0f}" y="{top + index * 18:.0f}">{html.escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )


def _svg_node_text(parts: dict[str, list[str]], x: float, y: float, *, light_text: bool = False) -> str:
    title, detail = parts.get("title") or [""], parts.get("detail") or []
    total_height = len(title) * 19 + (8 + len(detail) * 16 if detail else 0)
    title_y = y - total_height / 2 + 10
    title_style = ' style="fill:#fff"' if light_text else ""
    detail_style = ' style="fill:#dce5e9"' if light_text else ""
    title_markup = "".join(
        f'<tspan class="node-title"{title_style} x="{x:.0f}" y="{title_y + index * 19:.0f}">{html.escape(line)}</tspan>'
        for index, line in enumerate(title)
    )
    detail_y = title_y + len(title) * 19 + 5
    detail_markup = "".join(
        f'<tspan class="node-detail"{detail_style} x="{x:.0f}" y="{detail_y + index * 16:.0f}">{html.escape(line)}</tspan>'
        for index, line in enumerate(detail)
    )
    return title_markup + detail_markup


def _node_data_attributes(node: dict) -> str:
    evidence = html.escape(json.dumps(_evidence(node.get("evidence")), ensure_ascii=False), quote=True)
    return (
        f'data-node-id="{html.escape(str(node.get("id") or ""), quote=True)}" '
        f'data-node-label="{html.escape(str(node.get("label") or ""), quote=True)}" '
        f'data-node-detail="{html.escape(str(node.get("detail") or ""), quote=True)}" '
        f'data-node-evidence="{evidence}" tabindex="0" role="button"'
    )


def render_mindmap_svg(map_data: dict) -> str:
    root = map_data["root"]
    positions: dict[str, dict] = {}

    def weight(node: dict) -> int:
        return 1 if not node["children"] else sum(weight(child) for child in node["children"])

    left, right, left_weight, right_weight = [], [], 0, 0
    for child in sorted(root["children"], key=weight, reverse=True):
        if left_weight <= right_weight:
            left.append(child); left_weight += weight(child)
        else:
            right.append(child); right_weight += weight(child)

    root_w, root_h, root_parts = _node_metrics(root["label"], root.get("detail", ""), True)
    positions[root["id"]] = {"x": 0.0, "y": 0.0, "w": root_w, "h": root_h, "parts": root_parts, "side": 0, "depth": 0}

    def place_side(branches: list[dict], side: int):
        cursor = 0.0

        def place(node: dict, parent_id: str, depth: int) -> float:
            nonlocal cursor
            node_w, node_h, parts = _node_metrics(node["label"], node.get("detail", ""))
            if node["children"]:
                child_ys = [place(child, node["id"], depth + 1) for child in node["children"]]
                y = sum(child_ys) / len(child_ys)
            else:
                y = cursor + node_h / 2
                cursor += node_h + 34
            positions[node["id"]] = {"x": 0.0, "y": y, "w": node_w, "h": node_h, "parts": parts, "side": side, "depth": depth, "parent_id": parent_id}
            return y

        for branch in branches:
            place(branch, root["id"], 1)
        if branches:
            centre = (min(positions[node["id"]]["y"] for node in _walk_forest(branches)) + max(positions[node["id"]]["y"] for node in _walk_forest(branches))) / 2
            for node in _walk_forest(branches):
                positions[node["id"]]["y"] -= centre

            def place_x(node: dict):
                item = positions[node["id"]]
                parent = positions[item["parent_id"]]
                item["x"] = parent["x"] + parent["w"] + 78 if side > 0 else parent["x"] - item["w"] - 78
                for child in node["children"]:
                    place_x(child)

            for branch in branches:
                place_x(branch)

    place_side(left, -1)
    place_side(right, 1)
    min_x = min(item["x"] for item in positions.values())
    max_x = max(item["x"] + item["w"] for item in positions.values())
    min_y = min(item["y"] - item["h"] / 2 for item in positions.values())
    max_y = max(item["y"] + item["h"] / 2 for item in positions.values())
    shift_x, shift_y = 42 - min_x, 42 - min_y
    for item in positions.values():
        item["x"] += shift_x; item["y"] += shift_y
    width, height = max(520, max_x - min_x + 84), max(240, max_y - min_y + 84)
    edges, nodes = [], []
    for node in _walk_tree(root):
        item = positions[node["id"]]
        x, y, node_w, node_h = item["x"], item["y"], item["w"], item["h"]
        for child in node["children"]:
            target = positions[child["id"]]
            if target["side"] < 0:
                x1, x2 = x, target["x"] + target["w"]
            else:
                x1, x2 = x + node_w, target["x"]
            middle = (x1 + x2) / 2
            edges.append(f'<path data-kind="{html.escape(child.get("kind", "other"))}" d="M {x1:.0f} {y:.0f} C {middle:.0f} {y:.0f}, {middle:.0f} {target["y"]:.0f}, {x2:.0f} {target["y"]:.0f}" />')
        depth = item["depth"]
        node_class = "root" if depth == 0 else ("branch" if depth == 1 else "leaf")
        surface_style = ' style="fill:#1e3347;stroke:#1e3347"' if depth == 0 else ""
        tooltip = html.escape(node.get("detail") or node["label"])
        nodes.append(
            f'<g class="mindmap-node {node_class}" data-kind="{html.escape(node.get("kind", "other"))}" {_node_data_attributes(node)}><title>{tooltip}</title>'
            f'<rect{surface_style} x="{x:.0f}" y="{y - node_h / 2:.0f}" width="{node_w}" height="{node_h}" rx="{12 if depth == 0 else 8}" />'
            f'<text x="{x + node_w / 2:.0f}" y="{y:.0f}" text-anchor="middle">{_svg_node_text(item["parts"], x + node_w / 2, y, light_text=depth == 0)}</text></g>'
        )
    return _svg_document(width, height, "".join(edges), "".join(nodes))


def _walk_forest(roots: list[dict]):
    for root in roots:
        yield from _walk_tree(root)


def _walk_tree(root: dict):
    yield root
    for child in root["children"]:
        yield from _walk_tree(child)


def render_flowchart_svg(chart: dict) -> str:
    nodes = {node["id"]: node for node in chart["nodes"]}
    incoming = defaultdict(int)
    outgoing = defaultdict(list)
    for edge in chart["edges"]:
        incoming[edge["to"]] += 1
        outgoing[edge["from"]].append(edge["to"])
    queue = deque(node_id for node_id in nodes if not incoming[node_id])
    levels = {node_id: 0 for node_id in queue}
    visited = set()
    while queue:
        node_id = queue.popleft()
        visited.add(node_id)
        for target in outgoing[node_id]:
            levels[target] = max(levels.get(target, 0), levels[node_id] + 1)
            incoming[target] -= 1
            if not incoming[target]:
                queue.append(target)
    # Cyclic or disconnected components still need a deterministic rank.
    for node_id in nodes:
        if node_id not in levels:
            predecessor_levels = [levels.get(edge["from"], -1) for edge in chart["edges"] if edge["to"] == node_id]
            levels[node_id] = max(predecessor_levels, default=-1) + 1
    columns: dict[int, list[str]] = defaultdict(list)
    for node_id in nodes:
        columns[levels[node_id]].append(node_id)
    metrics = {node_id: _node_metrics(node["label"], node.get("detail", "")) for node_id, node in nodes.items()}
    direction = chart.get("direction", "LR")
    positions = {}
    if direction == "TB":
        level_heights = {level: max(metrics[node_id][1] for node_id in node_ids) for level, node_ids in columns.items()}
        level_row_widths = {
            level: sum(metrics[node_id][0] for node_id in node_ids) + max(0, len(node_ids) - 1) * 54
            for level, node_ids in columns.items()
        }
        canvas_row_width = max(level_row_widths.values(), default=448)
        level_y, cursor_y = {}, 36.0
        for level in sorted(columns):
            level_y[level] = cursor_y
            cursor_y += level_heights[level] + 86
        for level, node_ids in columns.items():
            row_width = level_row_widths[level]
            cursor = 36.0 + (canvas_row_width - row_width) / 2
            for node_id in node_ids:
                node_w, node_h, _ = metrics[node_id]
                positions[node_id] = (cursor, level_y[level] + (level_heights[level] - node_h) / 2)
                cursor += node_w + 54
        width = max(520, canvas_row_width + 72)
        height = max(220, cursor_y - 50)
    else:
        level_widths = {level: max(metrics[node_id][0] for node_id in node_ids) for level, node_ids in columns.items()}
        longest_edge_label = max((_text_units(edge.get("label", "")) for edge in chart["edges"]), default=0)
        rank_gap = max(140, min(240, int(longest_edge_label * 8 + 42)))
        level_x, cursor_x = {}, 36.0
        for level in sorted(columns):
            level_x[level] = cursor_x
            cursor_x += level_widths[level] + rank_gap
        for level, node_ids in columns.items():
            cursor_y = 42.0
            for row, node_id in enumerate(node_ids):
                node_w, node_h, _ = metrics[node_id]
                positions[node_id] = (level_x[level] + (level_widths[level] - node_w) / 2, cursor_y)
                cursor_y += node_h + 58
        width = max(520, cursor_x - 68)
        height = max(220, max((y + metrics[node_id][1] for node_id, (_x, y) in positions.items()), default=180) + 42)
    edges, shapes = [], []
    for edge in chart["edges"]:
        x1, y1 = positions[edge["from"]]
        x2, y2 = positions[edge["to"]]
        w1, h1, _ = metrics[edge["from"]]; w2, h2, _ = metrics[edge["to"]]
        if direction == "TB":
            start_x, start_y = x1 + w1 / 2, y1 + h1
            end_x, end_y = x2 + w2 / 2, y2
            middle = (start_y + end_y) / 2
            path = f'M {start_x:.0f} {start_y:.0f} C {start_x:.0f} {middle:.0f}, {end_x:.0f} {middle:.0f}, {end_x:.0f} {end_y:.0f}'
        else:
            start_x, start_y = x1 + w1, y1 + h1 / 2
            end_x, end_y = x2, y2 + h2 / 2
            middle = (start_x + end_x) / 2
            path = f'M {start_x:.0f} {start_y:.0f} C {middle:.0f} {start_y:.0f}, {middle:.0f} {end_y:.0f}, {end_x:.0f} {end_y:.0f}'
        edges.append(f'<path d="{path}" marker-end="url(#arrow)" />')
        if edge["label"]:
            edges.append(f'<text class="edge-label" x="{(start_x + end_x) / 2:.0f}" y="{(start_y + end_y) / 2 - 7:.0f}">{html.escape(edge["label"][:24])}</text>')
    for node_id, node in nodes.items():
        x, y = positions[node_id]
        node_w, node_h, parts = metrics[node_id]
        shape = str(node.get("type") or "")
        dark_surface = shape == "terminator" or node.get("role") == "conclusion"
        surface_style = ' style="fill:#1e3347;stroke:#1e3347"' if dark_surface else ""
        if shape == "decision":
            body = f'<path{surface_style} d="M {x + node_w/2:.0f} {y} L {x + node_w:.0f} {y + node_h/2:.0f} L {x + node_w/2:.0f} {y + node_h:.0f} L {x} {y + node_h/2:.0f} Z" />'
        elif shape == "io":
            inset = 15
            body = f'<path{surface_style} d="M {x + inset} {y} L {x + node_w} {y} L {x + node_w - inset} {y + node_h} L {x} {y + node_h} Z" />'
        elif shape == "database":
            body = f'<path{surface_style} d="M{x} {y+10} Q{x+node_w/2:.0f} {y-8} {x+node_w:.0f} {y+10} V{y+node_h-10:.0f} Q{x+node_w/2:.0f} {y+node_h+8:.0f} {x} {y+node_h-10:.0f} Z" />'
        elif shape == "document":
            body = f'<path{surface_style} d="M{x} {y} H{x+node_w:.0f} V{y+node_h-10:.0f} Q{x+node_w*.75:.0f} {y+node_h-22:.0f} {x+node_w*.5:.0f} {y+node_h-10:.0f} Q{x+node_w*.25:.0f} {y+node_h+2:.0f} {x} {y+node_h-10:.0f} Z" />'
        else:
            body = f'<rect{surface_style} x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="{node_h/2 if shape == "terminator" else 8:.0f}" />'
            if shape == "subprocess":
                body += f'<path class="subprocess-lines" d="M{x+10} {y}V{y+node_h}M{x+node_w-10:.0f} {y}V{y+node_h}" />'
        tooltip = html.escape(node.get("detail") or node["label"])
        role = html.escape(str(node.get("role") or "other"))
        shapes.append(f'<g class="flow-node type-{shape} role-{role}" {_node_data_attributes(node)}><title>{tooltip}</title>{body}<text x="{x + node_w/2:.0f}" y="{y + node_h/2:.0f}" text-anchor="middle">{_svg_node_text(parts, x + node_w/2, y + node_h/2, light_text=dark_surface)}</text></g>')
    return _svg_document(width, height, "".join(edges), "".join(shapes))


def _svg_document(width: float, height: float, edges: str, nodes: str) -> str:
    return (
        f'<svg class="litmtrans-diagram" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" role="img">'
        '<defs>'
        '<marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#81929d" /></marker>'
        '<linearGradient id="flow-surface" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#f5f7f8"/></linearGradient>'
        '<linearGradient id="flow-cool" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbfdfe"/><stop offset="1" stop-color="#edf3f6"/></linearGradient>'
        '<linearGradient id="flow-sage" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fbfdfc"/><stop offset="1" stop-color="#edf4f0"/></linearGradient>'
        '<linearGradient id="flow-warm" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fffdf9"/><stop offset="1" stop-color="#f7f1e7"/></linearGradient>'
        '<linearGradient id="flow-plum" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fdfcfd"/><stop offset="1" stop-color="#f2eff4"/></linearGradient>'
        '<linearGradient id="flow-rose" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fffdfc"/><stop offset="1" stop-color="#f7f0ed"/></linearGradient>'
        '<linearGradient id="flow-dark" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#304956"/><stop offset="1" stop-color="#203540"/></linearGradient>'
        '<filter id="paper-shadow" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="1" stdDeviation="1" flood-color="#1e2e38" flood-opacity=".12"/><feDropShadow dx="0" dy="5" stdDeviation="5" flood-color="#1e2e38" flood-opacity=".055"/></filter>'
        '</defs>'
        '<style>'
        '.edges path{fill:none;stroke:#81929d;stroke-width:1.15}.nodes text{fill:#1b2931;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;dominant-baseline:middle}'
        '.edge-label{fill:#52616a;font-size:10.5px;font-weight:500;dominant-baseline:auto;paint-order:stroke;stroke:#f7f9fa;stroke-width:4px;stroke-linejoin:round}'
        # Keep labels legible when the SVG is scaled down in an in-chat preview.
        # The former muted detail colour became too faint against the pastel nodes.
        '.node-title{font-size:13.5px;font-weight:700;fill:#102a3a}.node-detail{font-size:10.8px;font-weight:500;fill:#304b5b}'
        '.mindmap-node rect,.flow-node>rect,.flow-node>path:not(.subprocess-lines){fill:url(#flow-surface);stroke:#8799a4;stroke-width:1.05;filter:url(#paper-shadow)}'
        '.mindmap-node.root rect,.flow-node.type-terminator>rect{fill:#1e3347;stroke:#1e3347}'
        '.mindmap-node.branch rect{fill:#e9f1f5;stroke:#5c8399}.mindmap-node[data-kind="result"] rect,.mindmap-node[data-kind="validation"] rect{fill:#eaf4ef;stroke:#659078}'
        '.mindmap-node[data-kind="gap"] rect,.mindmap-node[data-kind="limitation"] rect{fill:#fff5e6;stroke:#ad8247}'
        '.flow-node.role-background>rect,.flow-node.role-background>path:not(.subprocess-lines),.flow-node.role-other>rect,.flow-node.role-other>path:not(.subprocess-lines){fill:url(#flow-surface);stroke:#9eabb2}'
        '.flow-node.role-gap>rect,.flow-node.role-gap>path:not(.subprocess-lines){fill:url(#flow-warm);stroke:#9b8564}.flow-node.role-question>rect,.flow-node.role-question>path:not(.subprocess-lines),.flow-node.role-design>rect,.flow-node.role-design>path:not(.subprocess-lines),.flow-node.role-method>rect,.flow-node.role-method>path:not(.subprocess-lines),.flow-node.role-validation>rect,.flow-node.role-validation>path:not(.subprocess-lines){fill:url(#flow-cool);stroke:#718a99}'
        '.flow-node.role-evidence>rect,.flow-node.role-evidence>path:not(.subprocess-lines),.flow-node.role-result>rect,.flow-node.role-result>path:not(.subprocess-lines){fill:url(#flow-sage);stroke:#718c7d}.flow-node.role-hypothesis>rect,.flow-node.role-hypothesis>path:not(.subprocess-lines),.flow-node.role-inference>rect,.flow-node.role-inference>path:not(.subprocess-lines),.flow-node.role-mechanism>rect,.flow-node.role-mechanism>path:not(.subprocess-lines){fill:url(#flow-plum);stroke:#887c91}'
        '.flow-node.role-limitation>rect,.flow-node.role-limitation>path:not(.subprocess-lines){fill:url(#flow-rose);stroke:#987d74;stroke-dasharray:5 3}.flow-node.role-conclusion>rect,.flow-node.role-conclusion>path:not(.subprocess-lines){fill:url(#flow-dark);stroke:#1c303d;stroke-width:1.45}'
        '.flow-node.role-conclusion .node-title{fill:#fff}.flow-node.role-conclusion .node-detail{fill:#dce5e9}'
        '.flow-node.type-decision>path{stroke-width:1.2}.flow-node.type-database>path{stroke:#758e82}.flow-node.type-io>path,.flow-node.type-document>path{stroke:#849aa7}.subprocess-lines{fill:none;stroke:#8799a4;stroke-width:1}'
        '.mindmap-node,.flow-node{cursor:pointer;outline:none}.mindmap-node:focus>rect,.flow-node:focus>rect,.flow-node:focus>path:not(.subprocess-lines){stroke-width:2}'
        '</style>'
        f'<g class="edges">{edges}</g><g class="nodes">{nodes}</g></svg>'
    )


def render_diagram_html(text: str) -> str | None:
    mindmap = parse_mindmap_v2(text)
    if mindmap:
        title, svg = mindmap["title"], render_mindmap_svg(mindmap)
    else:
        flowchart = parse_flowchart_v2(text)
        if not flowchart:
            return None
        title, svg = flowchart["title"], render_flowchart_svg(flowchart)
    return (
        '<section class="litmtrans-diagram-wrap">'
        f'<h3>{html.escape(title)}</h3>'
        f'<div class="litmtrans-diagram-preview" role="button" tabindex="0" title="点击打开图形阅读">{svg}</div>'
        '</section>'
    )


def is_valid_diagram_response(text: str) -> bool:
    """Recognize valid diagram output before chat-line folding truncates its JSON."""
    return parse_mindmap_v2(text) is not None or parse_flowchart_v2(text) is not None
