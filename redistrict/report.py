"""Human-readable Markdown audit report."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def _json_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def render_markdown(
    result: Dict[str, Any],
    *,
    map_filename: Optional[str] = None,
    verify_command: Optional[str] = None,
) -> str:
    out: List[str] = []
    version = result.get("boundary_version")
    status = result.get("status")
    binding = result.get("version_binding", {})
    out.append(f"# 区划换版地址候选与可证计数报告（{version}）")
    out.append("")
    out.append(f"- 状态：**{status}**")
    out.append(f"- 参与精确分配地址：{result.get('eligible_address_count')}")
    out.append(f"- 待核地址（不进入任何已证总量）：{result.get('pending_review_count')}")
    out.append(f"- 输入摘要：`{binding.get('input_sha256')}`")
    out.append(f"- 几何摘要：`{binding.get('geometry_sha256')}`")
    out.append(f"- 控制摘要：`{binding.get('controls_sha256')}`")
    pub = result.get("published_release")
    if pub and pub.get("release_id"):
        out.append(f"- 原发布版：`{pub.get('release_id')}`（只读，SHA-256 `{pub.get('sha256')}`）；本报告是新的重述产物。")
    else:
        out.append("- 原发布版：只读，未改写；本报告是新的重述产物。")
    if map_filename:
        out.append(f"- 小地图：[`{map_filename}`](./{map_filename})")
    out.append("")

    if map_filename:
        out.append(f"![候选归属小地图]({map_filename})")
        out.append("")

    out.append("## 1. 地址候选归属")
    out.append("")
    out.append("| 地址 | 状态 | 候选新区 | 来源/裁定 | 证据 |")
    out.append("|---|---|---|---|---|")
    for r in result.get("address_candidates", []):
        evidence = r.get("spatial_evidence") or {}
        detail = evidence.get("decision") or r.get("reason") or ""
        if "intersection_areas" in evidence:
            areas = evidence["intersection_areas"]
            detail += "；相交面积 " + _json_line(areas)
        out.append(
            "| `{id}` | {status} | {cand} | `{src}` | {detail} |".format(
                id=r.get("address_id"),
                status=r.get("status"),
                cand=", ".join(f"`{x}`" for x in r.get("candidate_districts", [])) or "—",
                src=r.get("candidate_source", ""),
                detail=str(detail).replace("|", "\\|"),
            )
        )
    out.append("")

    pending = result.get("pending_review", [])
    if pending:
        out.append("### 待核清单")
        out.append("")
        for p in pending:
            out.append(f"- `{p['address_id']}`：{p.get('reason')}")
        out.append("")

    out.append("## 2. 交叉控制（同版绑定）")
    out.append("")
    if result.get("control_population_basis"):
        out.append(f"> {result['control_population_basis']}")
        out.append("")
    out.append("| 控制 | 成员新区集合 | 可信总量 |")
    out.append("|---|---|---:|")
    for c in result.get("controls", []):
        out.append(f"| `{c['control_id']}` | {', '.join('`'+d+'`' for d in c['districts'])} | {c['total']} |")
    out.append("")

    if status == "infeasible":
        conflict = result.get("conflict", {})
        out.append("## 3. 整体无解：可复核冲突")
        out.append("")
        out.append(f"- 求解器结论：`{conflict.get('reason')}`")
        out.append(f"- 最小冲突控制核：{', '.join('`'+x+'`' for x in conflict.get('minimal_control_core_ids', []))}")
        out.append("- 以下每个真子集均存在完整分配见证；这说明不是单个控制或单个区界的局部问题。")
        out.append("")
        for check in conflict.get("proper_subset_checks", []):
            out.append(f"### 去掉 `{check['removed_control']}` 后可行")
            out.append("")
            out.append(f"剩余控制：{_json_line(check['remaining_control_ids'])}")
            w = check.get("witness")
            if w:
                out.append("")
                out.append("```json")
                out.append(_json_line(w["assignment"]))
                out.append("```")
        enum = conflict.get("raw_assignment_enumeration") or {}
        if enum.get("complete"):
            out.append("### 全量原始候选组合复核")
            out.append("")
            out.append(f"候选笛卡尔积共 **{enum['cartesian_candidate_combinations']}** 份，以下逐份检查：")
            out.append("")
            out.append("| # | 分配 | 各区计数 | 控制实测 | 全满足 |")
            out.append("|---:|---|---|---|---|")
            for i, case in enumerate(enum.get("cases", []), 1):
                checks = "; ".join(
                    f"{x['control_id']}={x['observed']}/{x['expected']}"
                    + ("✓" if x["satisfied"] else "✗")
                    for x in case["control_checks"]
                )
                out.append(
                    f"| {i} | `{_json_line(case['assignment'])}` | `{_json_line(case['district_counts'])}` | {checks} | "
                    f"{'是' if case['satisfies_all_controls'] else '否'} |"
                )
            out.append("")
        else:
            out.append("### 枚举")
            out.append("")
            out.append(f"候选组合数 {enum.get('cartesian_candidate_combinations')} 超过完整枚举上限；{enum.get('reason')}。可运行 verify 重新精确求解。")
            out.append("")
        command = verify_command or conflict.get("verification", {}).get("command", "")
        out.append(command)
        out.append("")
        out.append("**未输出任何区计数上下界或见证，避免制造假计数。**")
        out.append("")
        return "\n".join(out)

    out.append("## 3. 各新区可证计数上下界及全局见证")
    out.append("")
    out.append("> 上下界来自对全部合法分配的精确优化。每个界都附完整分配；不得把不同区的独立极值自行拼接。")
    out.append("")
    out.append("| 新区 | 下界 | 上界 | 确定计数 | 下界见证 | 上界见证 |")
    out.append("|---|---:|---:|---|---|---|")
    for b in result.get("count_bounds", []):
        out.append(
            f"| `{b['district_id']}` | **{b['lower']}** | **{b['upper']}** | "
            f"{'是' if b['fixed'] else '否'} | `{b['lower_witness_id']}` | `{b['upper_witness_id']}` |"
        )
    out.append("")

    for b in result.get("count_bounds", []):
        out.append(f"### `{b['district_id']}` 下界 {b['lower']} 见证")
        out.append("")
        out.append("```json")
        out.append(_json_line(b["lower_witness"]["assignment"]))
        out.append("```")
        out.append(f"控制复核：`{_json_line(b['lower_witness']['control_checks'])}`")
        out.append("")
        out.append(f"### `{b['district_id']}` 上界 {b['upper']} 见证")
        out.append("")
        out.append("```json")
        out.append(_json_line(b["upper_witness"]["assignment"]))
        out.append("```")
        out.append(f"控制复核：`{_json_line(b['upper_witness']['control_checks'])}`")
        out.append("")

    joint = result.get("joint_extrema_check") or {}
    if joint:
        out.append("### 极值不得自行拼接的联合检查")
        out.append("")
        out.append(joint.get("rule", ""))
        out.append("")
        for key, label in (
            ("all_district_lower_values", "所有区下界直接拼接"),
            ("all_district_upper_values", "所有区上界直接拼接"),
        ):
            item = joint.get(key) or {}
            out.append(
                f"- {label} `{_json_line(item.get('values'))}`："
                f"**{'存在完整联合见证' if item.get('jointly_feasible') else '不是合法分配（拒收该拼接）'}**"
            )
        out.append("")

    out.append("## 4. 地址级确定性")
    out.append("")
    out.append("只有在所有合法分配中候选新区唯一时才写作“确定”。")
    out.append("")
    out.append("| 地址 | 几何候选 | 控制约束后所有可能去向 | 结论 | 确定区 |")
    out.append("|---|---|---|---|---|")
    for d in result.get("determinations", []):
        out.append(
            f"| `{d['address_id']}` | {', '.join('`'+x+'`' for x in d['candidate_districts'])} "
            f"| {', '.join('`'+x+'`' for x in d['legally_possible_districts'])} "
            f"| {d['determination']} | {('`'+d['assigned_district']+'`') if d.get('assigned_district') else '—'} |"
        )
    out.append("")

    diffs = result.get("restatement_differences", [])
    if diffs:
        out.append("## 5. 与原发布版/可比发布数的重述差异")
        out.append("")
        out.append("差异字段同时给出两个方向，避免符号歧义。")
        out.append("")
        out.append("| 对象 | 原发布数 | 可证下界 | 可证上界 | 重述-发布(下边/上边) | 发布-重述(下界/上界) | 原值全局可行 |")
        out.append("|---|---:|---:|---:|---:|---:|---|")
        for d in diffs:
            if d.get("error"):
                out.append(f"| `{d.get('comparison_id')}` | 错误：{d['error']} | | | | | |")
                continue
            out.append(
                f"| `{d['comparison_id']}` | {d['published_count']} | {d['proven_lower']} | {d['proven_upper']} "
                f"| {d['restated_minus_published_lower_edge']:+d} / {d['restated_minus_published_upper_edge']:+d} "
                f"| {d['published_minus_lower']:+d} / {d['published_minus_upper']:+d} "
                f"| {'是（有见证）' if d['exact_published_value_globally_feasible'] else '否'} |"
            )
        out.append("")

    naive = result.get("naive_average_diagnostic")
    if naive:
        out.append("## 6. “平均摊”诊断（非证明计数）")
        out.append("")
        out.append(naive["warning"])
        out.append("")
        out.append("| 新区 | 等额均分等效值 | 按正面积加权等效值 | 可证区间 |")
        out.append("|---|---:|---:|---|")
        bounds = {b["district_id"]: b for b in result.get("count_bounds", [])}
        for did in result.get("district_ids", []):
            b = bounds.get(did, {})
            interval = f"[{b.get('lower')}, {b.get('upper')}]" if b else "—"
            out.append(
                f"| `{did}` | {naive['equal_split_district_equivalents'][did]:.3f} "
                f"| {naive['area_weighted_district_equivalents'][did]:.3f} | {interval} |"
            )
        out.append("")

    out.append("## 7. 独立复核")
    out.append("")
    out.append("```bash")
    out.append(verify_command or "python3 -I /workspace/redistrict/cli.py verify --input <input.json> --result <result.json>")
    out.append("```")
    out.append("")
    return "\n".join(out)
