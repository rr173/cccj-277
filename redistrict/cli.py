#!/usr/bin/env python3
"""Command line entry point.

Example:
  python3 -I /workspace/redistrict/cli.py run \
    --input /workspace/examples/cross_controls_demo.json \
    --published /workspace/examples/published_release_2020.json \
    --out /workspace/outputs/cross-result.json \
    --report /workspace/outputs/cross-report.md \
    --map /workspace/outputs/cross-map.svg
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from typing import Any, Dict, List, Optional, Tuple

# ``python -I /path/cli.py`` does not put the script directory on sys.path in
# this environment.  Add the repository root explicitly before package imports.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from redistrict.mapview import render_svg  # noqa: E402
from redistrict.pipeline import InputValidationError, run_analysis, sha256_bytes  # noqa: E402
from redistrict.report import render_markdown  # noqa: E402
from redistrict.solver import Control, Problem, verify_assignment  # noqa: E402


def _load_json_path(path: str) -> tuple[Any, bytes]:
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8")), raw


def _write_text(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _write_json(path: str, obj: Any) -> None:
    _write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n")


def cmd_run(args: argparse.Namespace) -> int:
    data, raw = _load_json_path(args.input)
    published_bytes = None
    published_data = None
    if args.published:
        published_data, published_bytes = _load_json_path(args.published)

    try:
        result = run_analysis(
            data,
            requested_version=args.boundary_version,
            input_bytes=raw,
            published_bytes=published_bytes,
            published_data=published_data,
            options={"conflict_enumeration_limit": args.enumeration_limit},
        )
    except InputValidationError as exc:
        error_obj = {
            "status": "input_rejected",
            "errors": exc.errors,
            "note": "No candidate counts or bounds were produced. Repair geometry/input and rerun.",
        }
        text = json.dumps(error_obj, ensure_ascii=False, indent=2)
        if args.error_out:
            _write_text(args.error_out, text + "\n")
        print(text, file=sys.stderr)
        return 2

    _write_json(args.out, result)

    map_path = args.map
    if map_path is None and args.report:
        map_path = os.path.splitext(args.report)[0] + "-map.svg"
    if map_path:
        svg = render_svg(data, result.get("boundary_version"))
        _write_text(map_path, svg)

    if args.report:
        map_name = os.path.basename(map_path) if map_path else None
        cmd_parts = [
            sys.executable, "-I", os.path.abspath(__file__), "verify",
            "--input", os.path.abspath(args.input),
            "--result", os.path.abspath(args.out),
        ]
        if args.published:
            cmd_parts += ["--published", os.path.abspath(args.published)]
        _write_text(
            args.report,
            render_markdown(
                result,
                map_filename=map_name,
                verify_command=shlex.join(cmd_parts),
            ),
        )

    summary = {
        "status": result["status"],
        "boundary_version": result["boundary_version"],
        "out": os.path.abspath(args.out),
        "report": os.path.abspath(args.report) if args.report else None,
        "map": os.path.abspath(map_path) if map_path else None,
        "eligible_address_count": result["eligible_address_count"],
        "pending_review_count": result["pending_review_count"],
    }
    if result["status"] == "feasible":
        summary["bounds"] = {
            b["district_id"]: [b["lower"], b["upper"]] for b in result["count_bounds"]
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if result["status"] == "infeasible":
        return 3
    return 0


def _problem_from_result(result: Dict[str, Any]) -> Problem:
    district_ids = list(result["district_ids"])
    idx = {d: i for i, d in enumerate(district_ids)}
    address_ids: List[str] = []
    domains: List[int] = []
    for row in result["address_candidates"]:
        if row["status"] != "eligible":
            continue
        address_ids.append(row["address_id"])
        mask = 0
        for d in row["candidate_districts"]:
            mask |= 1 << idx[d]
        domains.append(mask)
    controls = []
    for c in result.get("controls", []):
        mask = 0
        for d in c["districts"]:
            mask |= 1 << idx[d]
        controls.append(Control(c["control_id"], mask, c["total"], tuple(c["districts"])))
    return Problem(district_ids, address_ids, domains, controls)


def cmd_verify(args: argparse.Namespace) -> int:
    data, raw = _load_json_path(args.input)
    with open(args.result, "rb") as f:
        result_raw = f.read()
    result = json.loads(result_raw.decode("utf-8"))
    pub_raw = None
    pub_data = None
    if args.published:
        pub_data, pub_raw = _load_json_path(args.published)

    errors: List[str] = []
    binding = result.get("version_binding", {})
    if binding.get("input_sha256") != sha256_bytes(raw):
        errors.append("input_sha256 binding does not match input file")
    if pub_raw is not None and binding.get("published_sha256") != sha256_bytes(pub_raw):
        errors.append("published_sha256 binding does not match published file")

    problem = _problem_from_result(result)
    proof_witnesses = list(result.get("witnesses", []))
    for comp in result.get("restatement_differences", []):
        if isinstance(comp, dict) and comp.get("exact_value_witness"):
            proof_witnesses.append(comp["exact_value_witness"])

    # Every emitted global proof witness must be complete and legal under all
    # ordinary controls. Conflict proper-subset witnesses are intentionally
    # checked against their own subset in the certificate.
    for w in proof_witnesses:
        checked = verify_assignment(problem, w.get("assignment", {}))
        if not checked["verified"]:
            errors.append(f"witness {w.get('witness_id')} invalid: {checked['errors']}")
        if w.get("verified") is not True:
            errors.append(f"witness {w.get('witness_id')} is not marked verified")
        if any(not c.get("satisfied") for c in w.get("control_checks", [])):
            errors.append(f"witness {w.get('witness_id')} has a failed embedded control check")
        elif w.get("district_id") and w.get("value") is not None:
            observed = checked["district_counts"][w["district_id"]]
            if observed != w["value"]:
                errors.append(
                    f"witness {w['witness_id']} value {w['value']} != observed {observed}"
                )

    # Pending IDs must not appear in global proof witnesses.
    pending_ids = {p["address_id"] for p in result.get("pending_review", [])}
    for w in proof_witnesses:
        leaked = pending_ids.intersection(w.get("assignment", {}))
        if leaked:
            errors.append(f"witness {w.get('witness_id')} mixes pending addresses {sorted(leaked)}")

    # Rerun exact solver and compare proof-bearing fields.
    fresh = run_analysis(
        data,
        requested_version=result.get("boundary_version"),
        input_bytes=raw,
        published_bytes=pub_raw,
        published_data=pub_data,
    )
    if fresh["status"] != result.get("status"):
        errors.append(f"status changed on rerun: {fresh['status']} != {result.get('status')}")

    fresh_candidates = {
        r["address_id"]: (r["status"], tuple(r["candidate_districts"]), r.get("candidate_source"))
        for r in fresh.get("address_candidates", [])
    }
    old_candidates = {
        r["address_id"]: (r["status"], tuple(r["candidate_districts"]), r.get("candidate_source"))
        for r in result.get("address_candidates", [])
    }
    if fresh_candidates != old_candidates:
        errors.append("candidate attribution differs from exact input/geometry rerun")
    if [p["address_id"] for p in fresh.get("pending_review", [])] != [
        p["address_id"] for p in result.get("pending_review", [])
    ]:
        errors.append("pending-review set differs from rerun")

    if fresh["status"] == "feasible":
        fresh_bounds = {b["district_id"]: (b["lower"], b["upper"]) for b in fresh["count_bounds"]}
        old_bounds = {b["district_id"]: (b["lower"], b["upper"]) for b in result.get("count_bounds", [])}
        if fresh_bounds != old_bounds:
            errors.append(f"bounds differ from exact rerun: fresh={fresh_bounds} result={old_bounds}")
        fresh_det = {d["address_id"]: d["legally_possible_districts"] for d in fresh["determinations"]}
        old_det = {d["address_id"]: d["legally_possible_districts"] for d in result.get("determinations", [])}
        if fresh_det != old_det:
            errors.append("address possibility/determination sets differ from exact rerun")
    elif fresh["status"] == "infeasible":
        if fresh["conflict"]["minimal_control_core_ids"] != result.get("conflict", {}).get("minimal_control_core_ids"):
            errors.append("conflict core differs from exact rerun")

    verification = {"verified": not errors, "errors": errors, "result_sha256": sha256_bytes(result_raw)}
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    return 0 if not errors else 4


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Boundary-revision candidate and count-bound audit tool")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="generate candidates, bounds, witnesses, report and map")
    run.add_argument("--input", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--report")
    run.add_argument("--map")
    run.add_argument("--published")
    run.add_argument("--boundary-version")
    run.add_argument("--enumeration-limit", type=int, default=1000)
    run.add_argument("--error-out")
    run.set_defaults(func=cmd_run)

    ver = sub.add_parser("verify", help="independently check bindings, witnesses and exact results")
    ver.add_argument("--input", required=True)
    ver.add_argument("--result", required=True)
    ver.add_argument("--published")
    ver.set_defaults(func=cmd_verify)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
