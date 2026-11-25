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
    intersection_area,
    parse_geometry,
    point_interior,
    is_on_boundary,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
