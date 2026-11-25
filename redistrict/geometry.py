"""Pure-stdlib geometry validation and planar predicates.

The audit workloads for this tool are small administrative extracts.  Keeping
the geometry kernel in the standard library makes the exact assignment proof
runnable in an isolated interpreter without installing packages.

The important contract is:

* Polygon and MultiPolygon inputs are validated as simple rings.
* Positive-area overlaps between target districts are rejected.
* Shared edges and corner touches are allowed.
* Polygon/polygon intersection area is computed by horizontal slabs.  Between
  consecutive vertex/edge-intersection heights the cross-section width is
  linear, so evaluating it at the slab mid-point and multiplying by the slab
  height gives the exact integral (up to floating-point arithmetic).
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Ring = List[Point]
PolygonalGeometry = Dict[str, Any]


class GeometryError(ValueError):
    """Raised when input geometry is invalid or districts overlap in area."""


def _coordinate(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryError(f"{path}: coordinate must be a finite number")
    out = float(value)
    if not math.isfinite(out):
        raise GeometryError(f"{path}: coordinate must be finite")
    return out


def parse_point(raw: Any, path: str) -> Point:
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise GeometryError(f"{path}: Point coordinates must be [x, y]")
    return (_coordinate(raw[0], f"{path}[0]"), _coordinate(raw[1], f"{path}[1]"))


def _ring(raw: Any, path: str) -> Ring:
    if not isinstance(raw, (list, tuple)):
        raise GeometryError(f"{path}: ring must be an array of positions")
    if len(raw) < 4:
        raise GeometryError(f"{path}: ring must contain at least four positions")
    pts = [parse_point(p, f"{path}[{i}]") for i, p in enumerate(raw)]
    if pts[0] != pts[-1]:
        raise GeometryError(f"{path}: ring is not closed; first and last points differ")
    unique = pts[:-1]
    if len(unique) < 3:
        raise GeometryError(f"{path}: ring needs at least three distinct vertices")
    for i in range(len(unique)):
        if unique[i] == unique[(i + 1) % len(unique)]:
            raise GeometryError(f"{path}: consecutive duplicate vertex at index {i}")
    return unique


def parse_polygonal(raw: Dict[str, Any], path: str) -> PolygonalGeometry:
    if not isinstance(raw, dict):
        raise GeometryError(f"{path}: geometry must be an object")
    gtype = raw.get("type")
    coords = raw.get("coordinates")
    if gtype == "Polygon":
        if not isinstance(coords, list) or not coords:
            raise GeometryError(f"{path}: Polygon coordinates must contain rings")
        polygons = [[_ring(r, f"{path}.coordinates[{i}]") for i, r in enumerate(coords)]]
    elif gtype == "MultiPolygon":
        if not isinstance(coords, list) or not coords:
            raise GeometryError(f"{path}: MultiPolygon coordinates must contain polygons")
        polygons = []
        for pi, poly in enumerate(coords):
            if not isinstance(poly, list) or not poly:
                raise GeometryError(f"{path}.coordinates[{pi}]: empty polygon")
            polygons.append([
                _ring(r, f"{path}.coordinates[{pi}][{ri}]")
                for ri, r in enumerate(poly)
            ])
    else:
        raise GeometryError(f"{path}: unsupported polygonal geometry type {gtype!r}")
    return {"type": gtype, "polygons": polygons}


def parse_geometry(raw: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise GeometryError(f"{path}: geometry must be an object")
    gtype = raw.get("type")
    if gtype == "Point":
        return {"type": "Point", "point": parse_point(raw.get("coordinates"), f"{path}.coordinates")}
    if gtype in {"Polygon", "MultiPolygon"}:
        return parse_polygonal(raw, path)
    raise GeometryError(f"{path}: unsupported geometry type {gtype!r}")


def segment_dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def cross(a: Point, b: Point, c: Point) -> float:
    return ((b[0] - a[0]) * (c[1] - a[1])
            - (b[1] - a[1]) * (c[0] - a[0]))


def orientation_epsilon(a: Point, b: Point, c: Point, tol: float) -> float:
    ab = segment_dist(a, b)
    ac = segment_dist(a, c)
    # Floating-point scale plus a caller-supplied coordinate tolerance.
    return 1e-12 * max(ab * ac, 1.0) + tol * max(ab, ac, 1.0)


def on_segment(p: Point, a: Point, b: Point, tol: float = 1e-9) -> bool:
    eps = orientation_epsilon(a, b, p, tol)
    if abs(cross(a, b, p)) > eps:
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def proper_intersect(a: Point, b: Point, c: Point, d: Point, tol: float = 1e-9) -> bool:
    e1 = orientation_epsilon(a, b, c, tol)
    e2 = orientation_epsilon(a, b, d, tol)
    e3 = orientation_epsilon(c, d, a, tol)
    e4 = orientation_epsilon(c, d, b, tol)
    c1, c2 = cross(a, b, c), cross(a, b, d)
    c3, c4 = cross(c, d, a), cross(c, d, b)
    return ((c1 > e1 and c2 < -e2) or (c1 < -e1 and c2 > e2)) and (
        (c3 > e3 and c4 < -e4) or (c3 < -e3 and c4 > e4)
    )


def segments_intersect(a: Point, b: Point, c: Point, d: Point, tol: float = 1e-9) -> bool:
    if any((on_segment(c, a, b, tol), on_segment(d, a, b, tol),
            on_segment(a, c, d, tol), on_segment(b, c, d, tol))):
        return True
    return proper_intersect(a, b, c, d, tol)


def iter_edges(geom: PolygonalGeometry) -> Iterable[Tuple[Point, Point]]:
    for poly in geom["polygons"]:
        for ring in poly:
            n = len(ring)
            for i in range(n):
                yield ring[i], ring[(i + 1) % n]


def _validate_ring_shape(ring: Ring, path: str, tol: float) -> None:
    area2 = abs(sum(
        ring[i][0] * ring[(i + 1) % len(ring)][1]
        - ring[(i + 1) % len(ring)][0] * ring[i][1]
        for i in range(len(ring))
    ))
    scale = 1.0
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    scale = max(1.0, (max(xs) - min(xs)) * (max(ys) - min(ys)))
    if area2 <= 1e-14 * scale:
        raise GeometryError(f"{path}: ring has zero or numerically degenerate area")

    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if a == b:
            raise GeometryError(f"{path}: zero-length edge at {i}")
        for j in range(i + 1, n):
            c, d = ring[j], ring[(j + 1) % n]
            adjacent = (j == i + 1) or (i == 0 and j == n - 1)
            if adjacent:
                # Adjacent edges may meet only at their shared endpoint.
                if j == i + 1:
                    common = b
                    other1, other2 = a, d
                else:
                    common = a
                    other1, other2 = b, c
                if common not in (a, b) or common not in (c, d):
                    raise GeometryError(f"{path}: internal ring topology error")
                if on_segment(other1, c, d, tol) or on_segment(other2, a, b, tol):
                    raise GeometryError(
                        f"{path}: ring self-overlap or invalid adjacent edges ({i},{j})"
                    )
            elif segments_intersect(a, b, c, d, tol):
                raise GeometryError(f"{path}: self-intersecting ring at edges {i} and {j}")


def point_in_ring_system(rings: Sequence[Ring], p: Point, tol: float = 1e-9) -> bool:
    """Even-odd containment. Caller checks boundary first."""
    x, y = p
    inside = False
    for ring in rings:
        n = len(ring)
        for i in range(n):
            a = ring[i]
            b = ring[(i + 1) % n]
            ay, by = a[1], b[1]
            if (ay > y) != (by > y):
                x_cross = a[0] + (y - ay) * (b[0] - a[0]) / (by - ay)
                if x_cross > x + tol:
                    inside = not inside
                elif x_cross >= x - tol:
                    # A near-boundary crossing should have been classified as
                    # boundary by the caller.  Treat as boundary, not interior.
                    return False
    return inside


def is_on_boundary(geom: PolygonalGeometry, p: Point, tol: float = 1e-9) -> bool:
    for a, b in iter_edges(geom):
        if on_segment(p, a, b, tol):
            return True
    return False


def point_interior(geom: PolygonalGeometry, p: Point, tol: float = 1e-9) -> bool:
    if is_on_boundary(geom, p, tol):
        return False
    for poly in geom["polygons"]:
        if point_in_ring_system(poly, p, tol):
            return True
    return False


def _ring_contains_point_strict(ring: Ring, p: Point, tol: float) -> bool:
    for i in range(len(ring)):
        if on_segment(p, ring[i], ring[(i + 1) % len(ring)], tol):
            return False
    return point_in_ring_system([ring], p, tol)


def validate_polygonal_geometry(geom: PolygonalGeometry, path: str, tol: float = 1e-9) -> None:
    if geom.get("type") not in {"Polygon", "MultiPolygon"}:
        raise GeometryError(f"{path}: not polygonal geometry")
    for pi, poly in enumerate(geom["polygons"]):
        for ri, ring in enumerate(poly):
            _validate_ring_shape(ring, f"{path}.polygon[{pi}].ring[{ri}]", tol)
        exterior = poly[0]
        for hi, hole in enumerate(poly[1:], start=1):
            hp = hole[0]
            if not _ring_contains_point_strict(exterior, hp, tol):
                raise GeometryError(
                    f"{path}.polygon[{pi}].hole[{hi}] must lie strictly inside exterior"
                )
            for ei, (a, b) in enumerate(_edge_list(exterior)):
                for h_j in range(len(hole)):
                    c, d = hole[h_j], hole[(h_j + 1) % len(hole)]
                    if segments_intersect(a, b, c, d, tol):
                        raise GeometryError(
                            f"{path}.polygon[{pi}].hole[{hi}] intersects exterior"
                        )
        # Holes must be disjoint and non-nested in this conservative kernel.
        for h1 in range(1, len(poly)):
            for h2 in range(h1 + 1, len(poly)):
                r1, r2 = poly[h1], poly[h2]
                if _ring_contains_point_strict(r2, r1[0], tol) or _ring_contains_point_strict(r1, r2[0], tol):
                    raise GeometryError(f"{path}: nested holes are not supported as one Polygon")
                for a, b in _edge_list(r1):
                    for i in range(len(r2)):
                        c, d = r2[i], r2[(i + 1) % len(r2)]
                        if segments_intersect(a, b, c, d, tol):
                            raise GeometryError(f"{path}: holes intersect")
    # Parts of a MultiPolygon must not overlap in positive area.
    if geom["type"] == "MultiPolygon":
        for i in range(len(geom["polygons"])):
            gi = {"type": "Polygon", "polygons": [geom["polygons"][i]]}
            for j in range(i + 1, len(geom["polygons"])):
                gj = {"type": "Polygon", "polygons": [geom["polygons"][j]]}
                area = intersection_area(gi, gj, tol=tol)
                if area > max(1e-9, tol * tol):
                    raise GeometryError(f"{path}: MultiPolygon parts {i} and {j} overlap by area")


def _edge_list(ring: Ring) -> List[Tuple[Point, Point]]:
    return [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]


def _proper_intersection_point(
    a: Point, b: Point, c: Point, d: Point
) -> Optional[Point]:
    x1, y1 = a
    x2, y2 = b
    x3, y3 = c
    x4, y4 = d
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(den) < 1e-18:
        return None
    px = ((x1 * y2 - y1 * x2) * (x3 - x4)
          - (x1 - x2) * (x3 * y4 - y3 * x4)) / den
    py = ((x1 * y2 - y1 * x2) * (y3 - y4)
          - (y1 - y2) * (x3 * y4 - y3 * x4)) / den
    return px, py


def _cross_section_intervals(geom: PolygonalGeometry, y: float) -> List[Tuple[float, float]]:
    xs: List[float] = []
    for a, b in iter_edges(geom):
        y1, y2 = a[1], b[1]
        if (y1 > y) != (y2 > y):
            x = a[0] + (y - y1) * (b[0] - a[0]) / (y2 - y1)
            xs.append(x)
    xs.sort()
    if len(xs) % 2:
        # Generic slab heights never pass through a vertex, so this indicates
        # numerical trouble rather than a valid geometry.
        raise GeometryError(f"odd horizontal crossing count {len(xs)} at y={y!r}")
    intervals = [(xs[i], xs[i + 1]) for i in range(0, len(xs), 2) if xs[i + 1] > xs[i]]
    intervals.sort()
    return intervals


def _overlap_interval_length(
    a: Sequence[Tuple[float, float]], b: Sequence[Tuple[float, float]]
) -> float:
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        left = max(a[i][0], b[j][0])
        right = min(a[i][1], b[j][1])
        if right > left:
            total += right - left
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


def intersection_area(
    g1: PolygonalGeometry, g2: PolygonalGeometry, tol: float = 1e-9
) -> float:
    """Positive intersection area; shared lines/corners contribute zero."""
    ys: List[float] = []
    for geom in (g1, g2):
        for a, b in iter_edges(geom):
            ys.extend((a[1], b[1]))

    edges1 = list(iter_edges(g1))
    edges2 = list(iter_edges(g2))
    for a, b in edges1:
        for c, d in edges2:
            if not proper_intersect(a, b, c, d, tol):
                # Supporting-line intersection for endpoint/touch cases is not
                # needed as an extra event because endpoints are already in ys.
                continue
            p = _proper_intersection_point(a, b, c, d)
            if p is not None:
                ys.append(p[1])

    ys = sorted(set(ys))
    area = 0.0
    for lo, hi in zip(ys, ys[1:]):
        height = hi - lo
        if height <= 1e-14:
            continue
        mid = (lo + hi) / 2.0
        i1 = _cross_section_intervals(g1, mid)
        i2 = _cross_section_intervals(g2, mid)
        width = _overlap_interval_length(i1, i2)
        if width > 0:
            area += width * height
    return max(0.0, area)


def geometries_positive_overlap(
    g1: PolygonalGeometry, g2: PolygonalGeometry, area_epsilon: float
) -> float:
    area = intersection_area(g1, g2)
    return area if area > area_epsilon else 0.0
