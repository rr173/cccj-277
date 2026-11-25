#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import sys
import unittest
from itertools import product

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from redistrict.geometry import (  # noqa: E402
    geometry_area,
    intersection_area,
    parse_geometry,
    point_interior,
    is_on_boundary,
    uncovered_geometry,
    validate_polygonal_geometry,
)
from redistrict.pipeline import InputValidationError, run_analysis  # noqa: E402
from redistrict.solver import Control, Problem, optimize_mask, solve_feasible, verify_assignment  # noqa: E402

EXAMPLES = os.path.join(ROOT, "examples")


def load_example(name: str):
    with open(os.path.join(EXAMPLES, name), "rb") as f:
        raw = f.read()
    return json.loads(raw.decode("utf-8")), raw


class GeometryTests(unittest.TestCase):
    def test_shared_edge_has_zero_area_and_interior_predicate(self):
        left = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]]],
        }, "left")
        right = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[5, 0], [10, 0], [10, 5], [5, 5], [5, 0]]],
        }, "right")
        self.assertAlmostEqual(intersection_area(left, right), 0.0, places=8)
        self.assertTrue(is_on_boundary(left, (5, 2)))
        self.assertFalse(point_interior(left, (5, 2)))
        self.assertTrue(point_interior(left, (2, 2)))
        self.assertFalse(point_interior(left, (6, 2)))

    def test_positive_overlap_area(self):
        a = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [6, 0], [6, 6], [0, 6], [0, 0]]],
        }, "a")
        b = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[4, 4], [10, 4], [10, 10], [4, 10], [4, 4]]],
        }, "b")
        self.assertAlmostEqual(intersection_area(a, b), 4.0, places=7)

    def test_uncertain_rectangle_crosses_boundary(self):
        a = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]],
        }, "a")
        u = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[4, 2], [6, 2], [6, 8], [4, 8], [4, 2]]],
        }, "u")
        self.assertAlmostEqual(intersection_area(a, u), 6.0, places=7)


class EndToEndExampleTests(unittest.TestCase):
    def test_cross_controls_demo(self):
        data, raw = load_example("cross_controls_demo.json")
        result = run_analysis(data, input_bytes=raw)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["eligible_address_count"], 8)
        self.assertEqual(result["pending_review_count"], 2)
        pending = {x["address_id"] for x in result["pending_review"]}
        self.assertEqual(pending, {"M09_missing_pending", "P10_outside_pending"})

        candidates = {x["address_id"]: x["candidate_districts"] for x in result["address_candidates"]}
        self.assertEqual(candidates["P01_A_interior"], ["A"])
        self.assertEqual(candidates["P02_AC_boundary_priority"], ["A"])
        self.assertEqual(candidates["P03_CD_boundary_tie"], ["C", "D"])
        self.assertEqual(candidates["U04_AB_range"], ["A", "B"])
        self.assertEqual(candidates["M08_explicit_BC"], ["B", "C"])

        expected_bounds = {"A": (2, 4), "B": (1, 3), "C": (1, 3), "D": (0, 2)}
        self.assertEqual(
            {b["district_id"]: (b["lower"], b["upper"]) for b in result["count_bounds"]},
            expected_bounds,
        )

        district_ids = result["district_ids"]
        idx = {d: i for i, d in enumerate(district_ids)}
        addresses, domains = [], []
        for row in result["address_candidates"]:
            if row["status"] == "eligible":
                addresses.append(row["address_id"])
                domains.append(sum(1 << idx[d] for d in row["candidate_districts"]))
        controls = [
            Control(c["control_id"], sum(1 << idx[d] for d in c["districts"]), c["total"])
            for c in result["controls"]
        ]
        problem = Problem(district_ids, addresses, domains, controls)

        # Every emitted witness is complete and legal.
        self.assertGreater(len(result["witnesses"]), 0)
        for w in result["witnesses"]:
            checked = verify_assignment(problem, w["assignment"])
            self.assertTrue(checked["verified"], checked["errors"])

        # Independent brute-force check of every district bound.
        feasible_vectors = []
        choices = [[i for i in range(len(district_ids)) if dom & (1 << i)] for dom in domains]
        for combo in product(*choices):
            assignment = {addresses[i]: district_ids[combo[i]] for i in range(len(addresses))}
            checked = verify_assignment(problem, assignment)
            if checked["verified"]:
                feasible_vectors.append(checked["district_counts"])
        self.assertTrue(feasible_vectors)
        for di, did in enumerate(district_ids):
            vals = [v[did] for v in feasible_vectors]
            self.assertEqual(min(vals), expected_bounds[did][0])
            self.assertEqual(max(vals), expected_bounds[did][1])

        # The all-lower and all-upper stitched vectors are indeed not assignments.
        self.assertFalse(result["joint_extrema_check"]["all_district_lower_values"]["jointly_feasible"])
        self.assertFalse(result["joint_extrema_check"]["all_district_upper_values"]["jointly_feasible"])

        # Published A=3 is inside and globally attainable.
        comp = next(x for x in result["restatement_differences"] if x["comparison_id"] == "PUB_A_2020")
        self.assertTrue(comp["exact_published_value_globally_feasible"])
        self.assertTrue(comp["published_count_within_outer_bounds"])

    def test_conflict_certificate(self):
        data, raw = load_example("local_global_conflict.json")
        result = run_analysis(data, input_bytes=raw)
        self.assertEqual(result["status"], "infeasible")
        conflict = result["conflict"]
        self.assertEqual(conflict["minimal_control_core_ids"], [
            "K_AB_eq_2", "K_BC_eq_1", "K_AC_eq_2"
        ])
        enum = conflict["raw_assignment_enumeration"]
        self.assertTrue(enum["complete"])
        self.assertEqual(enum["cartesian_candidate_combinations"], 8)
        self.assertFalse(any(case["satisfies_all_controls"] for case in enum["cases"]))
        self.assertEqual(len(conflict["proper_subset_checks"]), 3)
        self.assertTrue(all(x["feasible"] for x in conflict["proper_subset_checks"]))
        self.assertEqual(result["count_bounds"], [])
        self.assertEqual(result["witnesses"], [])

    def test_overlap_is_rejected(self):
        data, raw = load_example("invalid_overlap.json")
        with self.assertRaises(InputValidationError) as ctx:
            run_analysis(data, input_bytes=raw)
        self.assertTrue(any("positive-area overlap" in e for e in ctx.exception.errors))


class SolverRandomBruteForceTests(unittest.TestCase):
    def test_optimizer_matches_bruteforce(self):
        rng = random.Random(20261125)
        for case_no in range(120):
            m = rng.randint(2, 4)
            n = rng.randint(3, 7)
            district_ids = [f"D{i}" for i in range(m)]
            address_ids = [f"a{i}" for i in range(n)]
            domains = []
            for _ in range(n):
                # Nonempty random subset, biased toward pairs.
                k = rng.randint(1, min(m, 3))
                chosen = rng.sample(range(m), k)
                domains.append(sum(1 << i for i in chosen))

            controls = []
            for ci in range(rng.randint(0, 3)):
                members = rng.sample(range(m), rng.randint(1, m))
                mask = sum(1 << i for i in members)
                total = rng.randint(0, n)
                controls.append(Control(f"c{ci}", mask, total))
            problem = Problem(district_ids, address_ids, domains, controls)

            brute_values = []
            choices = []
            for dom in domains:
                choices.append([i for i in range(m) if dom & (1 << i)])
            for combo in product(*choices):
                counts = [0] * m
                for di in combo:
                    counts[di] += 1
                if all(sum(counts[i] for i in self._bits(c.mask)) == c.total for c in controls):
                    brute_values.append(counts)

            feasible = solve_feasible(problem).feasible
            self.assertEqual(feasible, bool(brute_values), f"case {case_no} feasibility mismatch")
            if not brute_values:
                continue

            for mask in [1 << di for di in range(m)] + [(1 << m) - 1]:
                vals = [sum(v[i] for i in self._bits(mask)) for v in brute_values]
                for minimize, expected in ((True, min(vals)), (False, max(vals))):
                    opt = optimize_mask(problem, mask, minimize=minimize)
                    self.assertTrue(opt.feasible)
                    self.assertEqual(opt.value, expected)
                    checked = verify_assignment(problem, opt.assignment)
                    self.assertTrue(checked["verified"], checked["errors"])

    @staticmethod
    def _bits(mask: int):
        i = 0
        while mask:
            if mask & 1:
                yield i
            mask >>= 1
            i += 1


class CoverageGeometryTests(unittest.TestCase):
    def test_uncovered_geometry_with_diagonal_edges(self):
        # Triangle jurisdiction {0 <= y <= x <= 10}; one square district.
        jur = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]],
        }, "jur")
        sq = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [5, 0], [5, 5], [0, 5], [0, 0]]],
        }, "sq")
        decomp = uncovered_geometry(jur, [sq])
        # jurisdiction area 50, covered 12.5 (half the square) -> 37.5.
        self.assertAlmostEqual(decomp["area"], 37.5, places=9)
        self.assertEqual(len(decomp["parts"]), 2)
        # The upper slab degenerates to a triangle (3 vertices).
        sizes = sorted(len(poly[0]) for poly in decomp["geometry"]["polygons"])
        self.assertEqual(sizes, [3, 4])
        self.assertAlmostEqual(
            geometry_area(decomp["geometry"]), decomp["area"], places=9
        )

    def test_uncovered_geometry_full_cover_is_empty(self):
        jur = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
        }, "jur")
        left = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[0, 0], [5, 0], [5, 10], [0, 10], [0, 0]]],
        }, "left")
        right = parse_geometry({
            "type": "Polygon",
            "coordinates": [[[5, 0], [10, 0], [10, 10], [5, 10], [5, 0]]],
        }, "right")
        decomp = uncovered_geometry(jur, [left, right])
        self.assertAlmostEqual(decomp["area"], 0.0, places=9)
        self.assertEqual(decomp["parts"], [])


class CoverageAuditTests(unittest.TestCase):
    def test_gap_demo_suppresses_counts_and_reclassifies_pending(self):
        data, raw = load_example("coverage_gap_demo.json")
        result = run_analysis(data, input_bytes=raw)
        self.assertEqual(result["status"], "coverage_incomplete")

        cov = result["coverage_audit"]
        self.assertEqual(cov["status"], "incomplete")
        self.assertFalse(cov["coverage_complete"])
        self.assertAlmostEqual(cov["jurisdiction_area"], 100.0, places=9)
        self.assertAlmostEqual(cov["covered_area"], 90.0, places=9)
        self.assertAlmostEqual(cov["uncovered_area"], 10.0, places=9)
        self.assertEqual(cov["uncovered_part_count"], 1)
        self.assertTrue(cov["area_cross_check"]["within_tolerance"])
        self.assertFalse(cov["gate"]["proven_counts_publishable"])
        self.assertFalse(cov["gate"]["restatement_publishable"])

        # No proven counts or restatement conclusions are published.
        self.assertEqual(result["count_bounds"], [])
        self.assertEqual(result["witnesses"], [])
        self.assertEqual(result["determinations"], [])
        self.assertEqual(result["joint_extrema_check"], {})
        self.assertEqual(result["restatement_differences"], [])
        self.assertEqual(
            result["naive_average_diagnostic"]["status"],
            "suppressed_because_coverage_incomplete",
        )
        suppressed = result["publication_suppressed"]
        self.assertTrue(suppressed["no_proven_counts_emitted"])
        self.assertFalse(suppressed["original_publication_modified"])

        # Pending addresses inside the gap are re-marked as coverage gaps,
        # not as missing/outside addresses.
        reasons = {p["address_id"]: p["reason"] for p in result["pending_review"]}
        self.assertEqual(reasons["P02_in_gap"], "inside_uncovered_jurisdiction_area")
        self.assertEqual(reasons["U03_range_gap"], "overlaps_uncovered_jurisdiction_area")
        self.assertEqual(
            reasons["P07_outside_jurisdiction"], "point_outside_or_not_touching_any_district"
        )
        self.assertEqual(
            reasons["M06_missing_pending"], "missing_location_without_explicit_candidate_set"
        )
        self.assertEqual(
            set(suppressed["pending_inside_uncovered_area"]),
            {"P02_in_gap", "U03_range_gap"},
        )
        # Original reasons are preserved for audit.
        row = next(p for p in result["pending_review"] if p["address_id"] == "P02_in_gap")
        self.assertEqual(row["pre_coverage_reason"], "point_outside_or_not_touching_any_district")

    def test_uncovered_geometry_is_valid_and_consistent(self):
        data, raw = load_example("coverage_gap_demo.json")
        result = run_analysis(data, input_bytes=raw)
        cov = result["coverage_audit"]
        geom = parse_geometry(cov["uncovered_geometry"], "uncovered")
        validate_polygonal_geometry(geom, "uncovered")
        self.assertAlmostEqual(geometry_area(geom), cov["uncovered_area"], places=9)
        # Uncovered set is disjoint from every district and inside jurisdiction.
        for d in data["districts"]:
            dg = parse_geometry(d["geometry"], "d")
            self.assertLessEqual(intersection_area(geom, dg), 1e-9)
        jur = parse_geometry(data["jurisdiction"]["geometry"], "jur")
        self.assertAlmostEqual(
            intersection_area(geom, jur), cov["uncovered_area"], places=9
        )

    def test_complete_coverage_keeps_existing_results(self):
        data, raw = load_example("coverage_complete_demo.json")
        result = run_analysis(data, input_bytes=raw)
        self.assertEqual(result["status"], "feasible")
        cov = result["coverage_audit"]
        self.assertEqual(cov["status"], "complete")
        self.assertTrue(cov["coverage_complete"])
        self.assertLessEqual(cov["uncovered_area"], 1e-9)
        self.assertEqual(cov["uncovered_part_count"], 0)
        self.assertIsNone(cov["uncovered_geometry"])
        self.assertTrue(cov["gate"]["proven_counts_publishable"])

        # Address attribution and count results are identical to the same
        # input without a jurisdiction block.
        stripped = {k: v for k, v in data.items() if k != "jurisdiction"}
        baseline = run_analysis(stripped)
        self.assertEqual(result["address_candidates"], baseline["address_candidates"])
        self.assertEqual(result["pending_review"], baseline["pending_review"])
        self.assertEqual(result["count_bounds"], baseline["count_bounds"])
        self.assertEqual(result["determinations"], baseline["determinations"])
        self.assertEqual(
            result["restatement_differences"], baseline["restatement_differences"]
        )
        bounds = {b["district_id"]: (b["lower"], b["upper"]) for b in result["count_bounds"]}
        self.assertEqual(bounds, {"A": (1, 1), "B": (0, 1), "C": (0, 1), "D": (1, 1)})

    def test_missing_jurisdiction_is_not_audited_and_counts_unchanged(self):
        data, raw = load_example("cross_controls_demo.json")
        result = run_analysis(data, input_bytes=raw)
        self.assertEqual(result["status"], "feasible")
        cov = result["coverage_audit"]
        self.assertEqual(cov["status"], "not_audited")
        self.assertIsNone(cov["coverage_complete"])
        self.assertTrue(cov["gate"]["proven_counts_publishable"])
        self.assertEqual(
            {b["district_id"]: (b["lower"], b["upper"]) for b in result["count_bounds"]},
            {"A": (2, 4), "B": (1, 3), "C": (1, 3), "D": (0, 2)},
        )

    def test_jurisdiction_version_mismatch_rejected(self):
        data, _ = load_example("coverage_complete_demo.json")
        bad = json.loads(json.dumps(data))
        bad["jurisdiction"]["boundary_version"] = "BV-OTHER"
        with self.assertRaises(InputValidationError) as ctx:
            run_analysis(bad)
        self.assertTrue(any("does not match" in e for e in ctx.exception.errors))

    def test_jurisdiction_invalid_geometry_rejected(self):
        data, _ = load_example("coverage_complete_demo.json")
        bad = json.loads(json.dumps(data))
        bad["jurisdiction"]["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10]]],  # not closed
        }
        with self.assertRaises(InputValidationError):
            run_analysis(bad)

    def test_boundary_sets_with_per_set_jurisdiction(self):
        complete, _ = load_example("coverage_complete_demo.json")
        gap, _ = load_example("coverage_gap_demo.json")
        jur_ok = dict(complete["jurisdiction"], boundary_version="V-OK")
        jur_gap = dict(gap["jurisdiction"], boundary_version="V-GAP")
        data = {
            "boundary_sets": [
                {
                    "boundary_version": "V-OK",
                    "districts": complete["districts"],
                    "jurisdiction": jur_ok,
                },
                {
                    "boundary_version": "V-GAP",
                    "districts": gap["districts"],
                    "jurisdiction": jur_gap,
                },
            ],
            "addresses": complete["addresses"],
            "controls": [
                {"id": "K_BC", "boundary_version": "V-OK",
                 "districts": ["B", "C"], "total": 1}
            ],
        }
        ok = run_analysis(data, requested_version="V-OK")
        self.assertEqual(ok["status"], "feasible")
        self.assertEqual(ok["coverage_audit"]["status"], "complete")
        self.assertEqual(ok["coverage_audit"]["jurisdiction_id"], jur_ok["id"])

        gapped = run_analysis(data, requested_version="V-GAP")
        self.assertEqual(gapped["status"], "coverage_incomplete")
        self.assertEqual(gapped["coverage_audit"]["jurisdiction_id"], jur_gap["id"])
        self.assertEqual(gapped["count_bounds"], [])

    def test_holed_jurisdiction_and_outside_area(self):
        # Jurisdiction is a 10x10 square with a 4x4 hole; districts are the
        # four quadrants covering the whole square (including the hole area).
        data, _ = load_example("coverage_complete_demo.json")
        mod = json.loads(json.dumps(data))
        mod["jurisdiction"]["geometry"] = {
            "type": "Polygon",
            "coordinates": [
                [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
                [[3, 3], [3, 7], [7, 7], [7, 3], [3, 3]],
            ],
        }
        result = run_analysis(mod)
        cov = result["coverage_audit"]
        self.assertEqual(cov["status"], "complete")
        self.assertAlmostEqual(cov["jurisdiction_area"], 84.0, places=9)
        self.assertAlmostEqual(cov["covered_area"], 84.0, places=9)
        self.assertAlmostEqual(
            cov["district_area_outside_jurisdiction"], 16.0, places=9
        )
        self.assertEqual(result["status"], "feasible")


if __name__ == "__main__":
    unittest.main(verbosity=2)
