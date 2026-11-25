"""Exact finite-domain solver for redistricting assignment controls.

Each address is a variable whose value is one candidate district.  Binary
variables would also work; this implementation uses one integer domain bitmask
per address and linear equality controls.

The solver is deliberately dependency-free and auditable.  It is exponential in
the worst case, which is unavoidable for arbitrary overlapping equality
controls.  It never turns independently optimized district margins into a
claim: every returned witness is a complete assignment checked against every
control.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Control:
    cid: str
    mask: int
    total: int
    district_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "control_id": self.cid,
            "districts": list(self.district_ids),
            "total": self.total,
        }


@dataclass
class Problem:
    district_ids: List[str]
    address_ids: List[str]
    domains: List[int]
    controls: List[Control]

    def __post_init__(self) -> None:
        if len(self.address_ids) != len(self.domains):
            raise ValueError("address_ids/domains length mismatch")
        for i, dom in enumerate(self.domains):
            if dom <= 0:
                raise ValueError(f"address {self.address_ids[i]} has empty domain")
        m = len(self.district_ids)
        full = (1 << m) - 1
        for c in self.controls:
            if c.mask & ~full:
                raise ValueError(f"control {c.cid} references unknown district bits")
            if c.total < 0:
                raise ValueError(f"control {c.cid} has negative total")


@dataclass
class SolveResult:
    feasible: bool
    assignment: Optional[Dict[str, str]] = None
    domains: Optional[List[int]] = None
    nodes: int = 0
    reason: Optional[str] = None


@dataclass
class OptimizeResult:
    feasible: bool
    value: Optional[int] = None
    assignment: Optional[Dict[str, str]] = None
    nodes: int = 0
    proven_optimal: bool = False
    reason: Optional[str] = None


def bit_indices(mask: int) -> List[int]:
    out: List[int] = []
    i = 0
    while mask:
        if mask & 1:
            out.append(i)
        mask >>= 1
        i += 1
    return out


def propagate(domains: List[int], controls: Sequence[Control]) -> Tuple[Optional[List[int]], bool]:
    """Fixpoint interval propagation for all linear group equalities."""
    doms = list(domains)
    changed = True
    while changed:
        changed = False
        if any(d == 0 for d in doms):
            return doms, False

        for c in controls:
            mask = c.mask
            lo = 0  # addresses forced into the control group
            hi = 0  # addresses that could be in the control group
            for dom in doms:
                inside = dom & mask
                if inside == dom:  # every possible district is in group
                    lo += 1
                if inside:
                    hi += 1

            if c.total < lo or c.total > hi:
                return doms, False

            if c.total == lo:
                # No flexible address may choose inside the group.
                for i, dom in enumerate(doms):
                    inside = dom & mask
                    outside = dom & ~mask
                    if inside and outside and inside != dom:
                        new = dom & ~mask
                        if new == 0:
                            return doms, False
                        if new != dom:
                            doms[i] = new
                            changed = True

            if c.total == hi:
                # Every address that can be in the group must choose inside.
                for i, dom in enumerate(doms):
                    inside = dom & mask
                    outside = dom & ~mask
                    if inside and outside:
                        new = dom & mask
                        if new == 0:
                            return doms, False
                        if new != dom:
                            doms[i] = new
                            changed = True

            if any(d == 0 for d in doms):
                return doms, False

    return doms, True


def _select_address(doms: Sequence[int]) -> Optional[int]:
    best = -1
    best_size = 10**9
    for i, dom in enumerate(doms):
        size = dom.bit_count()
        if size > 1 and size < best_size:
            best = i
            best_size = size
    return None if best < 0 else best


def _assignment_from_domains(problem: Problem, doms: Sequence[int]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for i, dom in enumerate(doms):
        bits = bit_indices(dom)
        if len(bits) != 1:
            raise RuntimeError("internal error: witness domain is not singleton")
        out[problem.address_ids[i]] = problem.district_ids[bits[0]]
    return out


def check_controls(
    problem: Problem, assignment: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], List[str], List[int]]:
    idx = {d: i for i, d in enumerate(problem.district_ids)}
    counts = [0] * len(problem.district_ids)
    errors: List[str] = []
    for aid, did in assignment.items():
        if did not in idx:
            errors.append(f"{aid}: unknown district {did}")
            continue
        counts[idx[did]] += 1

    rows: List[Dict[str, Any]] = []
    for c in problem.controls:
        observed = sum(counts[i] for i in bit_indices(c.mask))
        ok = observed == c.total
        rows.append({
            "control_id": c.cid,
            "expected": c.total,
            "observed": observed,
            "satisfied": ok,
        })
        if not ok:
            errors.append(f"control {c.cid}: expected {c.total}, observed {observed}")
    return rows, errors, counts


def verify_assignment(problem: Problem, assignment: Dict[str, str]) -> Dict[str, Any]:
    errors: List[str] = []
    if set(assignment) != set(problem.address_ids):
        missing = sorted(set(problem.address_ids) - set(assignment))
        extra = sorted(set(assignment) - set(problem.address_ids))
        if missing:
            errors.append(f"missing assignments: {missing}")
        if extra:
            errors.append(f"extra assignments: {extra}")

    idx = {d: i for i, d in enumerate(problem.district_ids)}
    for ai, aid in enumerate(problem.address_ids):
        did = assignment.get(aid)
        if did is None:
            continue
        di = idx.get(did)
        if di is None:
            errors.append(f"{aid}: unknown district {did!r}")
        elif not (problem.domains[ai] & (1 << di)):
            errors.append(f"{aid}: {did} is outside its candidate set")

    rows, control_errors, counts = check_controls(problem, assignment)
    errors.extend(control_errors)
    return {
        "verified": not errors,
        "errors": errors,
        "district_counts": {
            d: counts[i] for i, d in enumerate(problem.district_ids)
        },
        "control_checks": rows,
    }


def solve_feasible(
    problem: Problem,
    assumptions: Optional[Dict[int, int]] = None,
    extra_controls: Optional[Sequence[Control]] = None,
) -> SolveResult:
    controls = list(problem.controls) + list(extra_controls or [])
    doms = list(problem.domains)
    if assumptions:
        for ai, di in assumptions.items():
            bit = 1 << di
            if not (doms[ai] & bit):
                return SolveResult(False, reason=f"assumption {ai} not in domain")
            doms[ai] = bit

    doms, ok = propagate(doms, controls)
    if not ok:
        return SolveResult(False, domains=doms, reason="initial propagation conflict")

    nodes = 0

    def dfs(state: List[int]) -> Optional[List[int]]:
        nonlocal nodes
        nodes += 1
        propagated, good = propagate(state, controls)
        if not good or propagated is None:
            return None
        state = propagated
        ai = _select_address(state)
        if ai is None:
            # All domains singleton.  Recheck explicitly for an auditable leaf.
            assignment = _assignment_from_domains(problem, state)
            verification = verify_assignment(
                Problem(
                    problem.district_ids,
                    problem.address_ids,
                    problem.domains,
                    controls,
                ),
                assignment,
            )
            # Candidate verification against original domains is fine; only
            # controls changed via extra controls need the temporary Problem.
            temp_problem = Problem(
                problem.district_ids, problem.address_ids, state, controls
            )
            _, errors, _ = check_controls(temp_problem, assignment)
            if errors:
                return None
            return state

        for di in bit_indices(state[ai]):
            nxt = list(state)
            nxt[ai] = 1 << di
            found = dfs(nxt)
            if found is not None:
                return found
        return None

    leaf = dfs(doms)
    if leaf is None:
        return SolveResult(False, nodes=nodes, reason="exhausted all candidate branches")
    return SolveResult(
        True,
        assignment=_assignment_from_domains(problem, leaf),
        domains=leaf,
        nodes=nodes,
    )


def _objective_bounds(doms: Sequence[int], mask: int) -> Tuple[int, int]:
    lo = 0
    hi = 0
    for dom in doms:
        inside = dom & mask
        if inside == dom:
            lo += 1
        if inside:
            hi += 1
    return lo, hi


def optimize_mask(
    problem: Problem,
    mask: int,
    minimize: bool,
    assumptions: Optional[Dict[int, int]] = None,
    extra_controls: Optional[Sequence[Control]] = None,
) -> OptimizeResult:
    """Exhaustively optimize sum of counts for districts in ``mask``."""
    controls = list(problem.controls) + list(extra_controls or [])
    base = list(problem.domains)
    if assumptions:
        for ai, di in assumptions.items():
            bit = 1 << di
            if not (base[ai] & bit):
                return OptimizeResult(False, reason="assumption outside domain")
            base[ai] = bit

    base, ok = propagate(base, controls)
    if not ok or base is None:
        return OptimizeResult(False, reason="initial propagation conflict")

    nodes = 0
    best_value: Optional[int] = None
    best_state: Optional[List[int]] = None

    def better(value: int) -> bool:
        if best_value is None:
            return True
        return value < best_value if minimize else value > best_value

    def dfs(state: List[int]) -> None:
        nonlocal nodes, best_value, best_state
        nodes += 1
        propagated, good = propagate(state, controls)
        if not good or propagated is None:
            return
        state = propagated

        lo, hi = _objective_bounds(state, mask)
        if best_value is not None:
            if minimize and lo >= best_value:
                return
            if not minimize and hi <= best_value:
                return

        ai = _select_address(state)
        if ai is None:
            assignment = _assignment_from_domains(problem, state)
            temp = Problem(problem.district_ids, problem.address_ids, state, controls)
            _, errors, counts = check_controls(temp, assignment)
            if errors:
                return
            value = sum(counts[i] for i in bit_indices(mask))
            if better(value):
                best_value = value
                best_state = list(state)
            return

        choices = bit_indices(state[ai])
        target_bits = bit_indices(mask)
        if minimize:
            # Try choices outside the objective first.
            choices.sort(key=lambda di: 0 if not (mask & (1 << di)) else 1)
        else:
            choices.sort(key=lambda di: 0 if (mask & (1 << di)) else 1)

        for di in choices:
            nxt = list(state)
            nxt[ai] = 1 << di
            dfs(nxt)

    dfs(base)
    if best_state is None or best_value is None:
        return OptimizeResult(False, nodes=nodes, reason="no feasible assignment")
    return OptimizeResult(
        True,
        value=best_value,
        assignment=_assignment_from_domains(problem, best_state),
        nodes=nodes,
        proven_optimal=True,
    )


def district_mask(problem: Problem, district_id: str) -> int:
    return 1 << problem.district_ids.index(district_id)


def group_mask(problem: Problem, district_ids: Iterable[str]) -> int:
    idx = {d: i for i, d in enumerate(problem.district_ids)}
    mask = 0
    for d in district_ids:
        mask |= 1 << idx[d]
    return mask


def possible_districts_for_address(
    problem: Problem, ai: int, known_witness: Dict[str, str]
) -> Tuple[List[int], Dict[int, Dict[str, str]]]:
    """Test every candidate and return globally possible district indices."""
    current_id = known_witness[problem.address_ids[ai]]
    current_di = problem.district_ids.index(current_id)
    possible = [current_di]
    witnesses: Dict[int, Dict[str, str]] = {current_di: known_witness}
    for di in bit_indices(problem.domains[ai]):
        if di == current_di:
            continue
        res = solve_feasible(problem, assumptions={ai: di})
        if res.feasible and res.assignment is not None:
            possible.append(di)
            witnesses[di] = res.assignment
    return sorted(possible), witnesses


def minimal_control_core(problem: Problem) -> List[str]:
    """Greedy deletion core; every retained control is necessary to this core."""
    if solve_feasible(problem).feasible:
        return []
    core = list(range(len(problem.controls)))
    for ci in list(range(len(problem.controls))):
        if ci not in core:
            continue
        trial = [j for j in core if j != ci]
        trial_problem = Problem(
            problem.district_ids,
            problem.address_ids,
            problem.domains,
            [problem.controls[j] for j in trial],
        )
        if not solve_feasible(trial_problem).feasible:
            core = trial
    return [problem.controls[i].cid for i in core]


def enumerate_raw_assignments(
    problem: Problem, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Enumerate Cartesian candidate choices and attach control checks.

    This is intended for small conflict certificates.  It deliberately does not
    use propagation, so a human can inspect every raw combination.
    """
    choices = [bit_indices(d) for d in problem.domains]
    rows: List[Dict[str, Any]] = []
    for combo in product(*choices):
        assignment = {
            problem.address_ids[i]: problem.district_ids[combo[i]]
            for i in range(len(problem.address_ids))
        }
        verification = verify_assignment(problem, assignment)
        rows.append({
            "assignment": assignment,
            "satisfies_all_controls": verification["verified"],
            "control_checks": verification["control_checks"],
            "district_counts": verification["district_counts"],
        })
        if limit is not None and len(rows) >= limit:
            break
    return rows


def cartesian_size(problem: Problem) -> int:
    total = 1
    for d in problem.domains:
        total *= d.bit_count()
    return total
