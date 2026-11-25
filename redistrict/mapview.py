"""Dependency-free SVG "small map" renderer for inputs and candidate evidence."""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .pipeline import select_boundary_set

DISTRICT_COLORS = ["#dbeafe", "#dcfce7", "#fef3c7", "#fae8ff", "#ffe4e6", "#cffafe"]
DISTRICT_STROKES = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#e11d48", "#0891b2"]


def _all_coords(geom: Dict[str, Any]) -> List[Tuple[float, float]]:
    gtype = geom.get("type")
    coords: List[Tuple[float, float]] = []

    def walk(x: Any) -> None:
        if (isinstance(x, list) and len(x) >= 2
                and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float))):
            coords.append((float(x[0]), float(x[1])))
        elif isinstance(x, list):
            for y in x:
                walk(y)

    if gtype == "Point":
        walk(geom.get("coordinates"))
    else:
        walk(geom.get("coordinates"))
    return coords


def render_svg(
    data: Dict[str, Any],
    boundary_version: Optional[str] = None,
    *,
    width: int = 860,
    height: int = 620,
    title: Optional[str] = None,
) -> str:
    version, districts = select_boundary_set(data, boundary_version)
    addresses = data.get("addresses", [])
    all_geoms = [d.get("geometry") for d in districts if isinstance(d, dict)]
    all_geoms += [a.get("geometry") for a in addresses if isinstance(a, dict) and a.get("geometry")]
    pts: List[Tuple[float, float]] = []
    for g in all_geoms:
        if isinstance(g, dict):
            pts.extend(_all_coords(g))
    if not pts:
        pts = [(0.0, 0.0), (1.0, 1.0)]

    minx = min(x for x, _ in pts)
    maxx = max(x for x, _ in pts)
    miny = min(y for _, y in pts)
    maxy = max(y for _, y in pts)
    if maxx == minx:
        minx -= 1
        maxx += 1
    if maxy == miny:
        miny -= 1
        maxy += 1
    pad_x = (maxx - minx) * 0.08 + 0.5
    pad_y = (maxy - miny) * 0.08 + 0.5
    minx -= pad_x
    maxx += pad_x
    miny -= pad_y
    maxy += pad_y

    left, top, right, bottom = 54, 58, width - 28, height - 42
    scale = min((right - left) / (maxx - minx), (bottom - top) / (maxy - miny))

    def X(x: float) -> float:
        return left + (x - minx) * scale

    def Y(y: float) -> float:
        # Flip y so larger northing is visually upward.
        return bottom - (y - miny) * scale

    def points_string(ring: Sequence[Sequence[float]]) -> str:
        return " ".join(f"{X(float(p[0])):.1f},{Y(float(p[1])):.1f}" for p in ring)

    parts: List[str] = []
    esc = html.escape
    map_title = title or f"Candidate attribution map · boundary {version}"
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(map_title)}">'
    )
    parts.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    parts.append(
        f'<text x="20" y="30" font-family="sans-serif" font-size="18" font-weight="700" '
        f'fill="#0f172a">{esc(map_title)}</text>'
    )

    # District polygons.
    for i, d in enumerate(districts):
        did = esc(str(d.get("id", i)))
        fill = DISTRICT_COLORS[i % len(DISTRICT_COLORS)]
        stroke = DISTRICT_STROKES[i % len(DISTRICT_STROKES)]
        geom = d.get("geometry") or {}
        gtype = geom.get("type")
        polys: List[List[Any]] = []
        if gtype == "Polygon":
            polys = [geom.get("coordinates", [])]
        elif gtype == "MultiPolygon":
            polys = geom.get("coordinates", [])
        for poly in polys:
            if not poly:
                continue
            exterior = poly[0]
            parts.append(
                f'<polygon points="{points_string(exterior)}" fill="{fill}" '
                f'fill-opacity="0.72" stroke="{stroke}" stroke-width="2.2"/>'
            )
            for hole in poly[1:]:
                parts.append(
                    f'<polygon points="{points_string(hole)}" fill="#ffffff" '
                    f'fill-opacity="0.85" stroke="{stroke}" stroke-width="1.2" stroke-dasharray="4 3"/>'
                )
        # Label at first exterior vertex's rough centroid average.
        try:
            ring = geom.get("coordinates", [[]])[0] if gtype == "Polygon" else geom["coordinates"][0][0]
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]
            cx = sum(float(p[0]) for p in ring) / len(ring)
            cy = sum(float(p[1]) for p in ring) / len(ring)
            parts.append(
                f'<text x="{X(cx):.1f}" y="{Y(cy):.1f}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="17" font-weight="800" fill="{stroke}">{did}</text>'
            )
        except Exception:
            pass

    # First pass: uncertainty ranges so points and labels stay on top.
    for a in addresses:
        if not isinstance(a, dict):
            continue
        aid = esc(str(a.get("map_label") or str(a.get("id", "?")).split("_")[0]))
        geom = a.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        gtype = geom.get("type")
        polys = [geom["coordinates"]] if gtype == "Polygon" else geom.get("coordinates", [])
        for poly in polys:
            exterior = poly[0]
            parts.append(
                f'<polygon points="{points_string(exterior)}" fill="#f97316" '
                f'fill-opacity="0.22" stroke="#ea580c" stroke-width="2" stroke-dasharray="7 4"/>'
            )
        try:
            ring = polys[0][0]
            if len(ring) > 1 and ring[0] == ring[-1]:
                ring = ring[:-1]
            cx = sum(float(p[0]) for p in ring) / len(ring)
            cy = sum(float(p[1]) for p in ring) / len(ring)
            parts.append(
                f'<text x="{X(cx):.1f}" y="{Y(cy):.1f}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="11" font-weight="700" fill="#9a3412" '
                f'stroke="#ffffff" stroke-width="2.4" paint-order="stroke">{aid}</text>'
            )
        except Exception:
            pass

    # Second pass: points and explicitly placed pending markers.
    for a in addresses:
        if not isinstance(a, dict):
            continue
        aid = esc(str(a.get("map_label") or str(a.get("id", "?")).split("_")[0]))
        geom = a.get("geometry")
        if not isinstance(geom, dict):
            pos = a.get("label_xy")
            if isinstance(pos, list) and len(pos) == 2:
                x, y = X(float(pos[0])), Y(float(pos[1]))
                parts.append(f'<rect x="{x-5:.1f}" y="{y-5:.1f}" width="10" height="10" fill="#94a3b8" transform="rotate(45 {x:.1f} {y:.1f})"/>')
                parts.append(
                    f'<text x="{x+9:.1f}" y="{y+4:.1f}" font-family="sans-serif" font-size="12" '
                    f'font-weight="700" fill="#475569" stroke="#ffffff" stroke-width="2.4" paint-order="stroke">{aid}*</text>'
                )
            continue
        if geom.get("type") != "Point":
            continue
        p = geom.get("coordinates")
        x, y = X(float(p[0])), Y(float(p[1]))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.4" fill="#7c3aed" stroke="#ffffff" stroke-width="2"/>')
        parts.append(
            f'<text x="{x+8:.1f}" y="{y-7:.1f}" font-family="sans-serif" font-size="12" '
            f'font-weight="700" fill="#5b21b6" stroke="#ffffff" stroke-width="2.4" '
            f'paint-order="stroke">{aid}</text>'
        )

    # Legend with fixed columns.
    ly = height - 28
    legend = [
        ("#7c3aed", "point"),
        ("#f97316", "uncertain range"),
        ("#93c5fd", "new district"),
        ("#94a3b8", "pending marker*"),
    ]
    cursor = 34
    for color, label in legend:
        parts.append(f'<rect x="{cursor}" y="{ly-11}" width="13" height="13" fill="{color}" fill-opacity="0.8" stroke="#334155" stroke-width="0.6"/>')
        parts.append(f'<text x="{cursor+19}" y="{ly}" font-family="sans-serif" font-size="13" fill="#334155">{esc(label)}</text>')
        cursor += 34 + len(label) * 8
    parts.append("</svg>")
    return "\n".join(parts)
