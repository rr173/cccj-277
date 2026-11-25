"""Input validation, candidate construction, exact bounds, and restatement output."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import (
    GeometryError,
    intersection_area,
    parse_geometry,
    point_interior,
    is_on_boundary,
    validate_polygonal_geometry,
)
from .solver import (
    Control,
    Problem,
    cartesian_size,
    enumerate_raw_assignments,
    minimal_control_core,
    optimize_mask,
    possible_districts_for_address,
    solve_feasible,
    verify_assignment,
)

SCHEMA_VERSION = "redistrict-result/1.0"
TOOL_VERSION = "1.0.0"


class InputValidationError(ValueError):
    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(raw)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _as_nonneg_int(x: Any, path: str, errors: List[str]) -> Optional[int]:
    if not _is_int(x) or x < 0:
        errors.append(f"{path}: must be a nonnegative integer")
        return None
    return int(x)


def select_boundary_set(
    data: Dict[str, Any], requested_version: Optional[str]
) -> Tuple[str, List[Dict[str, Any]]]:
    if "boundary_sets" in data:
        sets = data["boundary_sets"]
        if not isinstance(sets, list) or not sets:
            raise InputValidationError(["boundary_sets must be a nonempty array"])
        if requested_version is None:
            requested_version = data.get("selected_boundary_version")
        if requested_version is None:
            if len(sets) == 1:
                selected = sets[0]
            else:
                versions = [s.get("boundary_version") for s in sets if isinstance(s, dict)]
                raise InputValidationError([
                    "multiple boundary versions present; pass --boundary-version one of "
                    + repr(versions)
                ])
        else:
            matches = [s for s in sets if isinstance(s, dict) and s.get("boundary_version") == requested_version]
            if not matches:
                versions = [s.get("boundary_version") for s in sets if isinstance(s, dict)]
                raise InputValidationError([
                    f"boundary_version {requested_version!r} not found; available {versions!r}"
                ])
            selected = matches[0]
    else:
        if not isinstance(data.get("districts"), list):
            raise InputValidationError(["input must contain districts[] or boundary_sets[]"])
        version = data.get("boundary_version", "UNVERSIONED")
        if requested_version is not None and requested_version != version:
            raise InputValidationError([
                f"requested boundary version {requested_version!r}, but input declares {version!r}"
            ])
        selected = data
    version = selected.get("boundary_version", requested_version or "UNVERSIONED")
    districts = selected.get("districts")
    if not isinstance(districts, list) or not districts:
        raise InputValidationError([f"boundary {version!r}: districts must be a nonempty array"])
    return str(version), districts


def _priority_key(i: int, district: Dict[str, Any], policy: str) -> Tuple[int, float]:
    raw = district.get("boundary_priority", district.get("priority"))
    if raw is None:
        if policy == "explicit":
            return (2, float("inf"))  # absent: not an explicit contender
        return (0, float(i))
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise InputValidationError([f"district {district.get('id')}: priority must be numeric"])
    return (0, float(raw))


def build_district_index(
    district_rows: List[Dict[str, Any]], tol: float
) -> Tuple[List[str], Dict[str, Dict[str, Any]], List[str]]:
    errors: List[str] = []
    ids: List[str] = []
    districts: Dict[str, Dict[str, Any]] = {}
    for i, d in enumerate(district_rows):
        if not isinstance(d, dict):
            errors.append(f"districts[{i}] must be an object")
            continue
        did = d.get("id")
        if not isinstance(did, str) or not did:
            errors.append(f"districts[{i}].id must be a nonempty string")
            continue
        if did in districts:
            errors.append(f"duplicate district id {did!r}")
            continue
        geom_raw = d.get("geometry")
        try:
            geom = parse_geometry(geom_raw, f"districts[{did}].geometry")
            if geom["type"] not in {"Polygon", "MultiPolygon"}:
                raise GeometryError("district geometry must be Polygon or MultiPolygon")
            validate_polygonal_geometry(geom, f"districts[{did}].geometry", tol=tol)
        except GeometryError as exc:
            errors.append(str(exc))
            continue
        record = dict(d)
        record["_index"] = i
        record["_geom"] = geom
        ids.append(did)
        districts[did] = record

    if errors:
        raise InputValidationError(errors)

    # District interiors must not overlap by positive area.
    overlaps: List[str] = []
    area_eps = float(district_rows[0].get("_area_epsilon", 1e-9)) if district_rows else 1e-9
    for i, a in enumerate(ids):
        ga = districts[a]["_geom"]
        for b in ids[i + 1:]:
            gb = districts[b]["_geom"]
            try:
                area = intersection_area(ga, gb, tol=tol)
            except GeometryError as exc:
                overlaps.append(f"{a} vs {b}: unable to evaluate overlap ({exc})")
                continue
            if area > area_eps:
                overlaps.append(
                    f"districts {a!r} and {b!r} have positive-area overlap "
                    f"{area:.12g} > epsilon {area_eps:g}"
                )
    if overlaps:
        raise InputValidationError(overlaps)
    return ids, districts, []


def parse_controls(
    data: Dict[str, Any], district_ids: Sequence[str], boundary_version: str
) -> Tuple[List[Control], List[Dict[str, Any]], List[str]]:
    idx = {d: i for i, d in enumerate(district_ids)}
    raw_controls = data.get("controls", [])
    if raw_controls is None:
        raw_controls = []
    if not isinstance(raw_controls, list):
        raise InputValidationError(["controls must be an array"])
    controls: List[Control] = []
    public: List[Dict[str, Any]] = []
    ignored: List[str] = []
    errors: List[str] = []
    seen = set()
    for i, c in enumerate(raw_controls):
        if not isinstance(c, dict):
            errors.append(f"controls[{i}] must be an object")
            continue
        cid = c.get("id", c.get("control_id", f"C{i + 1}"))
        if not isinstance(cid, str) or not cid:
            errors.append(f"controls[{i}].id must be a nonempty string")
            continue
        if cid in seen:
            errors.append(f"duplicate control id {cid!r}")
            continue
        cversion = c.get("boundary_version", boundary_version)
        if cversion != boundary_version:
            ignored.append(f"{cid}:{cversion}")
            continue
        seen.add(cid)
        members = c.get("districts", c.get("member_district_ids"))
        if not isinstance(members, list) or not members:
            errors.append(f"control {cid}: districts must be a nonempty array")
            continue
        mask = 0
        member_ids: List[str] = []
        for did in members:
            if did not in idx:
                errors.append(f"control {cid}: unknown district {did!r}")
            else:
                mask |= 1 << idx[did]
                member_ids.append(did)
        total = _as_nonneg_int(c.get("total", c.get("count")), f"control {cid}.total", errors)
        if total is None or errors:
            continue
        controls.append(Control(cid=cid, mask=mask, total=total, district_ids=tuple(member_ids)))
        public.append({
            "control_id": cid,
            "boundary_version": boundary_version,
            "districts": member_ids,
            "total": total,
        })
    if errors:
        raise InputValidationError(errors)
    return controls, public, ignored


def _explicit_candidates(
    addr: Dict[str, Any], district_ids: Sequence[str], errors: List[str]
) -> Optional[List[str]]:
    raw = addr.get("candidate_districts", addr.get("candidate_district_ids"))
    if raw is None:
        return None
    aid = addr.get("id", "<unknown>")
    if not isinstance(raw, list) or len(raw) == 0:
        errors.append(f"address {aid}: candidate_districts must be a nonempty array when supplied")
        return []
    out: List[str] = []
    seen = set()
    for d in raw:
        if d not in district_ids:
            errors.append(f"address {aid}: candidate district {d!r} is not in boundary set")
        elif d not in seen:
            seen.add(d)
            out.append(d)
    return out


def construct_candidates(
    data: Dict[str, Any],
    district_ids: List[str],
    districts: Dict[str, Dict[str, Any]],
    tol: float,
    area_eps: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str], List[int]]:
    raw_addresses = data.get("addresses", [])
    if not isinstance(raw_addresses, list):
        raise InputValidationError(["addresses must be an array"])
    errors: List[str] = []
    seen_ids = set()
    rows: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    eligible_ids: List[str] = []
    domains: List[int] = []
    policy = data.get("boundary_priority_policy", "input_order")
    if policy not in {"input_order", "explicit"}:
        errors.append("boundary_priority_policy must be 'input_order' or 'explicit'")
        policy = "input_order"

    for ai, addr in enumerate(raw_addresses):
        path = f"addresses[{ai}]"
        if not isinstance(addr, dict):
            errors.append(f"{path} must be an object")
            continue
        aid = addr.get("id")
        if not isinstance(aid, str) or not aid:
            errors.append(f"{path}.id must be a nonempty stable string")
            continue
        if aid in seen_ids:
            errors.append(f"duplicate address id {aid!r}")
            continue
        seen_ids.add(aid)

        geometry_raw = addr.get("geometry")
        is_missing = addr.get("location_status") == "missing" or geometry_raw is None
        explicit = _explicit_candidates(addr, district_ids, errors)
        if errors and errors[-1].startswith(f"address {aid}"):
            # Continue collecting, but this address will be skipped later if fatal.
            pass

        base: Dict[str, Any] = {
            "address_id": aid,
            "input_index": ai,
            "location_kind": "missing" if is_missing else geometry_raw.get("type", "Unknown") if isinstance(geometry_raw, dict) else "Invalid",
        }

        if is_missing:
            base["location_kind"] = "missing"
            if explicit:
                cand = list(explicit)
                base.update({
                    "status": "eligible",
                    "candidate_source": "explicit_missing_location",
                    "candidate_districts": cand,
                    "reason": None,
                })
                rows.append(base)
                eligible_ids.append(aid)
                mask = 0
                for d in cand:
                    mask |= 1 << district_ids.index(d)
                domains.append(mask)
            else:
                rec = {
                    **base,
                    "status": "pending_review",
                    "candidate_districts": [],
                    "reason": "missing_location_without_explicit_candidate_set",
                    "rule": "No old code or areal average may supply candidates.",
                }
                rows.append(rec)
                pending.append(rec)
            continue

        if explicit is not None:
            errors.append(
                f"address {aid}: candidate_districts is only allowed for missing geometry; "
                "located addresses must derive candidates from geometry"
            )
            continue

        try:
            geom = parse_geometry(geometry_raw, f"{path}.geometry")
        except GeometryError as exc:
            errors.append(str(exc))
            continue

        if geom["type"] == "Point":
            p = geom["point"]
            interiors: List[str] = []
            touching: List[str] = []
            evidence: Dict[str, Any] = {"boundary_touch_districts": [], "interior_districts": []}
            for di, did in enumerate(district_ids):
                dg = districts[did]["_geom"]
                if is_on_boundary(dg, p, tol=tol):
                    touching.append(did)
                elif point_interior(dg, p, tol=tol):
                    interiors.append(did)
            evidence["interior_districts"] = interiors
            evidence["boundary_touch_districts"] = touching
            if len(interiors) > 1:
                errors.append(
                    f"address {aid}: point is interior to multiple districts {interiors}; "
                    "boundary set must be repaired"
                )
                continue
            if interiors:
                cand = interiors
                source = "point_interior_containment"
                decision = "strict interior"
            elif touching:
                ranked = [
                    (_priority_key(districts[d]["_index"], districts[d], policy), d)
                    for d in touching
                ]
                min_key = min(k for k, _ in ranked)
                winners = [d for k, d in ranked if k == min_key]
                if len(winners) == 1 and min_key[0] == 0:
                    cand = winners
                    source = "shared_boundary_priority"
                    decision = f"priority winner {winners[0]}"
                else:
                    cand = list(touching)
                    source = "shared_boundary_all_touch"
                    if policy == "explicit":
                        decision = "no unique explicit priority; all touching districts retained"
                    else:
                        decision = "priority tie; all touching districts retained"
            else:
                cand = []
                source = "point_no_containment_or_touch"
                decision = "point outside all target districts"
            base.update({
                "status": "eligible" if cand else "pending_review",
                "candidate_source": source,
                "candidate_districts": cand,
                "reason": None if cand else "point_outside_or_not_touching_any_district",
                "spatial_evidence": {**evidence, "decision": decision},
            })
            rows.append(base)
            if cand:
                eligible_ids.append(aid)
                domains.append(sum(1 << district_ids.index(d) for d in cand))
            else:
                pending.append(base)
            continue

        # Polygon / MultiPolygon uncertainty range.
        try:
            validate_polygonal_geometry(geom, f"{path}.geometry", tol=tol)
        except GeometryError as exc:
            errors.append(str(exc))
            continue
        areas: Dict[str, float] = {}
        cand = []
        for did in district_ids:
            area = intersection_area(geom, districts[did]["_geom"], tol=tol)
            if area > area_eps:
                areas[did] = area
                cand.append(did)
        base.update({
            "status": "eligible" if cand else "pending_review",
            "candidate_source": "positive_area_intersection",
            "candidate_districts": cand,
            "spatial_evidence": {
                "intersection_areas": {k: round(v, 12) for k, v in areas.items()},
                "positive_area_epsilon": area_eps,
                "rule": "boundary-only/zero-area contact is not a candidate",
            },
        })
        rows.append(base)
        if cand:
            eligible_ids.append(aid)
            domains.append(sum(1 << district_ids.index(d) for d in cand))
        else:
            base["reason"] = "no_positive_area_intersection"
            pending.append(base)

    if errors:
        raise InputValidationError(errors)

    # Domains align with eligible_ids because appended together.
    return rows, pending, eligible_ids, domains


def make_witness_dict(
    witness_id: str,
    kind: str,
    problem: Problem,
    assignment: Dict[str, str],
    *,
    district_id: Optional[str] = None,
    group_districts: Optional[List[str]] = None,
    value: Optional[int] = None,
) -> Dict[str, Any]:
    verification = verify_assignment(problem, assignment)
    if not verification["verified"]:
        raise RuntimeError(f"refusing to emit invalid witness {witness_id}: {verification['errors']}")
    return {
        "witness_id": witness_id,
        "kind": kind,
        "district_id": district_id,
        "group_districts": group_districts,
        "value": value,
        "assignment": dict(sorted(assignment.items())),
        "district_counts": verification["district_counts"],
        "control_checks": verification["control_checks"],
        "verified": True,
    }


def naive_apportionment(
    candidate_rows: Sequence[Dict[str, Any]], district_ids: Sequence[str]
) -> Dict[str, Any]:
    equal = {d: 0.0 for d in district_ids}
    area_weighted = {d: 0.0 for d in district_ids}
    details = []
    for row in candidate_rows:
        if row["status"] != "eligible":
            continue
        cand = row["candidate_districts"]
        for d in cand:
            equal[d] += 1.0 / len(cand)
        areas = (row.get("spatial_evidence") or {}).get("intersection_areas") or {}
        positive = {d: float(areas[d]) for d in cand if d in areas and float(areas[d]) > 0}
        if positive and row["candidate_source"] == "positive_area_intersection":
            total = sum(positive.values())
            weights = {d: a / total for d, a in positive.items()}
            for d, w in weights.items():
                area_weighted[d] += w
            details.append({"address_id": row["address_id"], "weights": weights})
        else:
            # No measured overlap area for explicit/point ambiguity; mark equal
            # split only, never pretend this is an authoritative count.
            weights = {d: 1.0 / len(cand) for d in cand}
            for d, w in weights.items():
                area_weighted[d] += w
            details.append({"address_id": row["address_id"], "weights": weights, "basis": "equal_fallback"})
    return {
        "status": "diagnostic_not_legal_assignment",
        "warning": "Fractional averaging is not used for proven bounds and must not be published as an address count.",
        "equal_split_district_equivalents": equal,
        "area_weighted_district_equivalents": area_weighted,
        "address_weights": details,
    }


def published_differences(
    data: Dict[str, Any],
    district_ids: Sequence[str],
    problem: Problem,
    bounds: Dict[str, Dict[str, Any]],
    first_assignment: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Compare immutable published figures/groups with globally feasible ranges."""
    comparisons = data.get("restatement_comparisons", [])
    out: List[Dict[str, Any]] = []
    idx = {d: i for i, d in enumerate(district_ids)}
    for i, comp in enumerate(comparisons):
        cid = comp.get("id", f"comparison-{i + 1}")
        did = comp.get("district_id")
        groups = comp.get("districts")
        published = comp.get("published_count")
        label = comp.get("label", cid)
        if not _is_int(published):
            out.append({"comparison_id": cid, "error": "published_count must be an integer"})
            continue
        if did:
            if did not in bounds:
                out.append({"comparison_id": cid, "error": f"unknown district {did}"})
                continue
            lower = bounds[did]["lower"]
            upper = bounds[did]["upper"]
            members = [did]
            mask = 1 << idx[did]
        elif groups:
            if any(d not in idx for d in groups):
                out.append({"comparison_id": cid, "error": "unknown district in districts"})
                continue
            members = list(groups)
            mask = sum(1 << idx[d] for d in members)
            lo_res = optimize_mask(problem, mask, minimize=True)
            hi_res = optimize_mask(problem, mask, minimize=False)
            if not (lo_res.feasible and hi_res.feasible):
                out.append({"comparison_id": cid, "error": "comparison group optimization failed"})
                continue
            lower, upper = lo_res.value, hi_res.value
        else:
            out.append({"comparison_id": cid, "error": "provide district_id or districts[]"})
            continue

        exact_control = Control(
            cid=f"__exact_published_{cid}", mask=mask, total=int(published),
            district_ids=tuple(members),
        )
        exact_problem = Problem(
            problem.district_ids,
            problem.address_ids,
            problem.domains,
            list(problem.controls) + [exact_control],
        )
        exact = solve_feasible(exact_problem)
        row = {
            "comparison_id": cid,
            "label": label,
            "published_release_id": comp.get("published_release_id"),
            "members": members,
            "published_count": published,
            "proven_lower": lower,
            "proven_upper": upper,
            "restated_minus_published_lower_edge": lower - published,
            "restated_minus_published_upper_edge": upper - published,
            "published_minus_lower": published - lower,
            "published_minus_upper": published - upper,
            "published_count_within_outer_bounds": lower <= published <= upper,
            "exact_published_value_globally_feasible": exact.feasible,
        }
        if exact.feasible and exact.assignment is not None:
            row["exact_value_witness"] = make_witness_dict(
                f"witness:published-exact:{cid}",
                "published_exact_value",
                exact_problem,
                exact.assignment,
                group_districts=members,
                value=published,
            )
        out.append(row)
    return out


def run_analysis(
    data: Dict[str, Any],
    *,
    requested_version: Optional[str] = None,
    input_bytes: Optional[bytes] = None,
    published_bytes: Optional[bytes] = None,
    published_data: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    options = options or {}
    tol = float(data.get("geometry_tolerance", options.get("geometry_tolerance", 1e-9)))
    area_eps = float(data.get("positive_area_epsilon", options.get("positive_area_epsilon", 1e-9)))
    enum_limit = int(options.get("conflict_enumeration_limit", data.get("conflict_enumeration_limit", 1000)))

    boundary_version, district_rows = select_boundary_set(data, requested_version)
    # Attach area epsilon before overlap check.
    for d in district_rows:
        if isinstance(d, dict):
            d["_area_epsilon"] = area_eps
    district_ids, districts, _ = build_district_index(district_rows, tol)
    controls, public_controls, ignored_controls = parse_controls(data, district_ids, boundary_version)

    candidate_rows, pending, eligible_ids, domains = construct_candidates(
        data, district_ids, districts, tol, area_eps
    )

    problem = Problem(district_ids, eligible_ids, domains, controls)
    geometry_binding = {
        "boundary_version": boundary_version,
        "district_ids": district_ids,
        "geometries": [
            {"id": d, "geometry": districts[d].get("geometry")} for d in district_ids
        ],
    }
    control_binding = {"boundary_version": boundary_version, "controls": public_controls}
    binding = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "boundary_version": boundary_version,
        "input_sha256": sha256_bytes(input_bytes or b"") if input_bytes is not None else None,
        "input_canonical_sha256": canonical_digest(data),
        "geometry_sha256": canonical_digest(geometry_binding),
        "controls_sha256": canonical_digest(control_binding),
        "published_sha256": sha256_bytes(published_bytes) if published_bytes is not None else None,
        "original_publication_modified": False,
    }

    publication_info = None
    if published_data is not None:
        publication_info = {
            "release_id": published_data.get("release_id"),
            "release_status": published_data.get("release_status"),
            "boundary_version": published_data.get("boundary_version"),
            "published_at": published_data.get("published_at"),
            "sha256": binding["published_sha256"],
            "write_policy": "read_only_input; restatement is written to a separate output",
        }

    base_result: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "status": "unknown",
        "version_binding": binding,
        "published_release": publication_info,
        "boundary_version": boundary_version,
        "district_ids": district_ids,
        "address_count_total": len([a for a in data.get("addresses", []) if isinstance(a, dict) and a.get("id")]),
        "eligible_address_count": len(eligible_ids),
        "pending_review_count": len(pending),
        "pending_review": pending,
        "address_candidates": candidate_rows,
        "controls": public_controls,
        "control_population_basis": (
            "Each control.total is enforced on eligible participating addresses only. "
            "pending_review addresses are excluded from proven counts and witnesses."
        ),
        "ignored_controls_other_versions": ignored_controls,
        "naive_average_diagnostic": naive_apportionment(candidate_rows, district_ids),
    }

    first = solve_feasible(problem)
    if not first.feasible:
        core_ids = minimal_control_core(problem)
        core_controls = [c for c in controls if c.cid in core_ids]
        core_problem = Problem(district_ids, eligible_ids, domains, core_controls)
        proper_subset_checks = []
        for cid in core_ids:
            subset = [c for c in core_controls if c.cid != cid]
            res = solve_feasible(Problem(district_ids, eligible_ids, domains, subset))
            item = {
                "removed_control": cid,
                "remaining_control_ids": [c.cid for c in subset],
                "feasible": res.feasible,
                "nodes": res.nodes,
            }
            if res.feasible and res.assignment is not None:
                item["witness"] = make_witness_dict(
                    f"witness:core-minus-{cid}",
                    "conflict_proper_subset",
                    Problem(district_ids, eligible_ids, domains, subset),
                    res.assignment,
                )
            proper_subset_checks.append(item)

        csize = cartesian_size(problem)
        enumeration = None
        if csize <= enum_limit:
            enumeration = {
                "complete": True,
                "cartesian_candidate_combinations": csize,
                "cases": enumerate_raw_assignments(problem),
            }
        else:
            enumeration = {
                "complete": False,
                "cartesian_candidate_combinations": csize,
                "reason": f"exceeds conflict_enumeration_limit={enum_limit}; rerun verifier/solver or raise limit",
            }

        base_result.update({
            "status": "infeasible",
            "count_bounds": [],
            "witnesses": [],
            "determinations": [],
            "restatement_differences": [],
            "naive_average_diagnostic": {
                "status": "suppressed_because_global_controls_are_infeasible",
                "warning": "No fractional apportionment is presented as a count when the global problem has no legal assignment.",
            },
            "conflict": {
                "reviewable": True,
                "reason": first.reason,
                "solver_nodes_before_conflict": first.nodes,
                "minimal_control_core_ids": core_ids,
                "minimal_control_core": [c.to_dict() for c in core_controls],
                "proper_subset_checks": proper_subset_checks,
                "raw_assignment_enumeration": enumeration,
                "verification": {
                    "command": "python3 -I /workspace/redistrict/cli.py verify --input <input.json> --result <result.json>",
                    "method": "Exact finite-domain search; raw Cartesian enumeration is included only when complete.",
                },
                "no_proven_counts_emitted": True,
            },
        })
        return base_result

    assert first.assignment is not None
    witnesses: List[Dict[str, Any]] = []
    bounds: Dict[str, Dict[str, Any]] = {}
    bound_rows: List[Dict[str, Any]] = []

    for di, did in enumerate(district_ids):
        mask = 1 << di
        lo = optimize_mask(problem, mask, minimize=True)
        hi = optimize_mask(problem, mask, minimize=False)
        if not (lo.feasible and hi.feasible and lo.assignment and hi.assignment):
            # This should be impossible after first feasibility; fail loudly
            # rather than emitting fake bounds.
            raise RuntimeError(f"optimizer failed for feasible district {did}")
        lower_w = make_witness_dict(
            f"witness:{did}:lower", "district_lower", problem, lo.assignment,
            district_id=did, value=lo.value,
        )
        upper_w = make_witness_dict(
            f"witness:{did}:upper", "district_upper", problem, hi.assignment,
            district_id=did, value=hi.value,
        )
        witnesses.extend([lower_w, upper_w])
        row = {
            "district_id": did,
            "lower": lo.value,
            "upper": hi.value,
            "fixed": lo.value == hi.value,
            "lower_witness_id": lower_w["witness_id"],
            "upper_witness_id": upper_w["witness_id"],
            "lower_witness": lower_w,
            "upper_witness": upper_w,
            "solver_nodes": {"lower": lo.nodes, "upper": hi.nodes},
        }
        bounds[did] = row
        bound_rows.append(row)

    naive = base_result.get("naive_average_diagnostic") or {}
    naive_checks: Dict[str, Any] = {}
    for did in district_ids:
        area_value = float(naive["area_weighted_district_equivalents"][did])
        equal_value = float(naive["equal_split_district_equivalents"][did])
        lower = bounds[did]["lower"]
        upper = bounds[did]["upper"]
        naive_checks[did] = {
            "area_weighted_value": area_value,
            "area_weighted_minus_lower": area_value - lower,
            "area_weighted_minus_upper": area_value - upper,
            "area_weighted_within_integer_interval_span": lower <= area_value <= upper,
            "equal_split_value": equal_value,
            "equal_split_minus_lower": equal_value - lower,
            "equal_split_minus_upper": equal_value - upper,
            "warning": "A fractional value inside [lower, upper] is still not an assignment or proof.",
        }
    naive["district_bound_comparison"] = naive_checks
    base_result["naive_average_diagnostic"] = naive

    joint_checks: Dict[str, Any] = {}
    for edge_name, key in (("all_district_lower_values", "lower"), ("all_district_upper_values", "upper")):
        values = {did: bounds[did][key] for did in district_ids}
        joint_controls = [
            Control(
                cid=f"__joint_{key}_{did}",
                mask=1 << di,
                total=int(values[did]),
                district_ids=(did,),
            )
            for di, did in enumerate(district_ids)
        ]
        joint_problem = Problem(
            problem.district_ids,
            problem.address_ids,
            problem.domains,
            list(problem.controls) + joint_controls,
        )
        joint = solve_feasible(joint_problem)
        item: Dict[str, Any] = {
            "values": values,
            "jointly_feasible": joint.feasible,
        }
        if joint.feasible and joint.assignment is not None:
            item["witness"] = make_witness_dict(
                f"witness:joint-{key}", "joint_extremum_combination",
                joint_problem, joint.assignment
            )
            witnesses.append(item["witness"])
        joint_checks[edge_name] = item
    joint_checks["rule"] = (
        "District extrema are proven separately. A stitched vector is accepted only if this "
        "joint solver check returns a complete witness; otherwise it is not a legal assignment."
    )

    determinations: List[Dict[str, Any]] = []
    for ai, aid in enumerate(eligible_ids):
        possible_di, possibility_witnesses = possible_districts_for_address(
            problem, ai, first.assignment
        )
        possible_ids = [district_ids[di] for di in possible_di]
        deterministic = len(possible_ids) == 1
        row = {
            "address_id": aid,
            "candidate_districts": next(
                r["candidate_districts"] for r in candidate_rows if r["address_id"] == aid
            ),
            "legally_possible_districts": possible_ids,
            "determination": "deterministic" if deterministic else "ambiguous",
            "assigned_district": possible_ids[0] if deterministic else None,
        }
        if not deterministic:
            # One globally legal alternative is enough to demonstrate ambiguity;
            # all tested possibilities are listed above.
            alt_di = next(di for di in possible_di if possibility_witnesses[di] != first.assignment)
            alt = possibility_witnesses[alt_di]
            row["ambiguity_witness_district"] = district_ids[alt_di]
            row["ambiguity_witness"] = make_witness_dict(
                f"witness:ambiguity:{aid}",
                "address_alternative",
                problem,
                alt,
                district_id=district_ids[alt_di],
            )
            witnesses.append(row["ambiguity_witness"])
        determinations.append(row)

    comparison_source = data
    if not data.get("restatement_comparisons") and published_data:
        auto_comparisons: List[Dict[str, Any]] = []
        for fig in published_data.get("comparison_figures", []):
            if not isinstance(fig, dict):
                continue
            comp = dict(fig)
            if "comparable_new_district" in comp:
                comp["district_id"] = comp["comparable_new_district"]
            if "comparable_new_districts" in comp:
                comp["districts"] = comp["comparable_new_districts"]
            comp.setdefault("published_release_id", published_data.get("release_id"))
            auto_comparisons.append(comp)
        comparison_source = {**data, "restatement_comparisons": auto_comparisons}

    differences = published_differences(comparison_source, district_ids, problem, bounds, first.assignment)

    base_result.update({
        "status": "feasible",
        "count_bounds": bound_rows,
        "joint_extrema_check": joint_checks,
        "witnesses": witnesses,
        "determinations": determinations,
        "restatement_differences": differences,
        "global_warning": (
            "Each lower/upper witness is a complete global assignment. "
            "Do not combine district-specific lower or upper entries into one vector unless a witness exists for it."
        ),
    })
    return base_result
