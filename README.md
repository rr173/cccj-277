# 区划换版：候选归属、可证计数上下界与重述差异

本工具针对**指定边界版本**，把稳定历史地址 ID 映射到候选新区，并在所有同时满足可信交叉控制组总量的合法分配中，精确计算：

- 每个新区的人数/地址数最小、最大值；
- 达到每个下界和上界的**完整全局分配见证**；
- 每个地址在所有合法分配中的全部可能去向；仅当去向唯一时标记为“确定”；
- 原发布数/可比汇总数与可证区间的重述差异；
- “等额平均摊”和“按相交面积加权摊”的分数诊断，但**不**把这些分数当作证明计数；
- 整体无解时的最小冲突控制核、真子集可行见证和小规模全量枚举；
- 可复算的 JSON 结果、Markdown 审计报告和纯标准库生成的 SVG 小地图。

原发布版文件只被读取；所有新结果写入 `outputs/` 或用户指定的新路径，不回写原文件。

## 0. 一分钟运行

环境只需 Python 3.10+；示例和测试仅用标准库。按隔离方式运行读取 JSON 的解释器：

```bash
python3 -I /workspace/redistrict/cli.py run \
  --input /workspace/examples/cross_controls_demo.json \
  --published /workspace/examples/published_release_2020.json \
  --out /workspace/outputs/cross-result.json \
  --report /workspace/outputs/cross-report.md \
  --map /workspace/outputs/cross-map.svg

python3 -I /workspace/redistrict/cli.py verify \
  --input /workspace/examples/cross_controls_demo.json \
  --result /workspace/outputs/cross-result.json \
  --published /workspace/examples/published_release_2020.json
```

局部可行、整体矛盾的例子：

```bash
python3 -I /workspace/redistrict/cli.py run \
  --input /workspace/examples/local_global_conflict.json \
  --out /workspace/outputs/conflict-result.json \
  --report /workspace/outputs/conflict-report.md \
  --map /workspace/outputs/conflict-map.svg
# 退出码 3 表示已完成校验，但控制整体无解；结果中不写任何计数上下界。

python3 -I /workspace/redistrict/cli.py verify \
  --input /workspace/examples/local_global_conflict.json \
  --result /workspace/outputs/conflict-result.json
```

几何重叠/坏几何输入会以退出码 `2` 拒收，且不产生计数：

```bash
python3 -I /workspace/redistrict/cli.py run \
  --input /workspace/examples/invalid_overlap.json \
  --out /tmp/rejected.json
```

运行测试：

```bash
python3 -I /workspace/tests/test_redistrict.py
```

## 1. 输入契约

主输入是一个 JSON 对象。最简形式：

```json
{
  "boundary_version": "BV-2026.1",
  "geometry_tolerance": 1e-9,
  "positive_area_epsilon": 1e-9,
  "boundary_priority_policy": "input_order",
  "districts": [
    {
      "id": "A",
      "boundary_priority": 1,
      "geometry": {"type": "Polygon", "coordinates": [[...]]}
    }
  ],
  "addresses": [
    {"id": "addr-1", "geometry": {"type": "Point", "coordinates": [2, 8]}},
    {"id": "addr-2", "geometry": {"type": "Polygon", "coordinates": [[...]]}},
    {"id": "addr-3", "location_status": "missing",
     "candidate_districts": ["B", "C"]},
    {"id": "addr-4", "location_status": "missing"}
  ],
  "controls": [
    {"id": "K_AB", "boundary_version": "BV-2026.1",
     "districts": ["A", "B"], "total": 5}
  ],
  "restatement_comparisons": [
    {"id": "PUB_A", "district_id": "A", "published_count": 3}
  ]
}
```

也支持一个文件放多个版本：

```json
{
  "boundary_sets": [
    {"boundary_version": "BV-2020", "districts": []},
    {"boundary_version": "BV-2026.1", "districts": []}
  ]
}
```

然后用 `--boundary-version BV-2026.1` 指定。控制中的 `boundary_version` 必须与所选版本绑定；其他版本的控制会被明确列为 ignored，不会混入本次证明。

### 几何候选规则

1. **精确点**
   - 严格内部包含：唯一候选为内部区。
   - 位于共享边界：若相触区中存在唯一最高优先级，按优先级裁定。
   - 若优先级相同、使用 `boundary_priority_policy: "explicit"` 但没有唯一显式优先级，或无法唯一裁定，则候选为**所有相触区**。
   - 点在所有区外：待核。
2. **不确定 Polygon/MultiPolygon**
   - 仅当与新区有多边形**正面积相交**时才进入候选集。
   - 共边、角接触等零面积接触不成为候选。
3. **缺位置**
   - 只能使用输入明确列出的非空 `candidate_districts`。
   - 没有明确候选集时进入 `pending_review`；不使用旧区划代码、面积平均或邻接关系补候选。
4. 每个 `eligible` 地址在每份见证中恰好分配给一个候选区；待核地址不进入求解器、区计数、上下界或见证。

### 几何拒收

以下情况返回 `status: "input_rejected"` 和可复核错误：

- 坐标非有限数、环未闭合、顶点不足、连续重复点、零面积环；
- 环自交/自重叠、洞不在外部环内、洞相交或不受支持的嵌套洞；
- MultiPolygon 部件正面积重叠；
- 两个目标新区之间正面积重叠；
- 重复 ID、未知候选区/控制成员、负的或非整数控制总量等模式错误。

共享边和公共角点允许。面积由水平条带积分计算；共享线贡献面积为零。

## 2. 精确求解与“不能拼接极值”

设地址 `a` 的候选区集合为 `C_a`，变量 `x_a ∈ C_a`。控制 `k` 为：

\[
\sum_a \mathbf 1[x_a \in G_k] = T_k,\qquad T_k\in\mathbb Z_{\ge 0}.
\]

所有控制同时成立。求解器使用：

- 每个地址一个整数位掩码域；
- 线性组等式的下界/上界传播；
- 域分裂 DFS；
- 对目标区或目标组的精确分支限界；
- 对每个候选去向反复做可行性检验，得到“所有合法分配中的可能去向”。

最坏情况下该问题是指数级的；任意交叉集合上的整数分配本身包含 NP-hard 组合问题。此实现不声称大规模高性能，但不使用近似或启发式计数。示例和审计级数据可直接运行；大规模数据可在同一 `Problem` 接口后替换为 MILP/CP 后端，而输出契约不变。

### 全局见证，而非局部极值拼盘

对每个区 `d`：

- `count_bounds[d].lower`：全部合法分配中的最小计数；
- `count_bounds[d].upper`：全部合法分配中的最大计数；
- `lower_witness.assignment` / `upper_witness.assignment`：达到该值的完整分配；
- 每份见证都重新检查所有候选域和所有控制。

JSON 中还有 `joint_extrema_check`：

- 尝试把所有区的下界同时作为精确区计数；
- 尝试把所有区的上界同时作为精确区计数；
- 只有求解器返回完整联合见证时才称可行。

在交叉控制示例中，所有下界之和小于参与地址数，所有上界之和大于参与地址数，因此两份拼盘都被拒收。这正是“每区独立可行的极值不能拼成一份不存在分配”的机器检查。

## 3. 无解冲突证书

若整体无解，结果中：

- `status = "infeasible"`；
- `count_bounds = []`、`witnesses = []`，不输出假计数；
- `conflict.minimal_control_core` 给出贪心删除得到的必要控制核；
- `proper_subset_checks` 证明去掉核中任一控制后存在完整分配；
- 当候选笛卡尔积不超过 `--enumeration-limit` 时，`raw_assignment_enumeration.cases` 列出每一份原始组合及每个控制的实测值；
- `verify` 会重新运行精确求解器并比较状态、上下界和可能性集合。

`examples/local_global_conflict.json` 是一个三地址、三交叉控制的小例：

- `X_AB ∈ {A,B}`
- `Y_BC ∈ {B,C}`
- `Z_AC ∈ {A,C}`
- `|AB| = 2`
- `|BC| = 1`
- `|AC| = 2`

每个控制单独可行，任意两个控制也有完整见证，但三者加“共 3 个参与地址”矛盾。全量只有 \(2^3=8\) 份组合，报告逐份列出。

## 4. 重述差异和原发布版

`restatement_comparisons` 可写单个新区或任意新区县集合：

```json
{
  "id": "PUB_NORTH_AB",
  "districts": ["A", "B"],
  "published_release_id": "PUB-2020-OLD",
  "published_count": 5
}
```

输出：

- `proven_lower/proven_upper`：对该组合同样运行**全局**优化，而不是把各区上下界相加；
- `restated_minus_published_*` 和 `published_minus_*`：两个符号方向的差异；
- `published_count_within_outer_bounds`；
- `exact_published_value_globally_feasible`：若外层区间包含该数，仍额外检验这个精确值是否真的能由某份完整分配达到；可行时附见证。

`--published` 文件的 SHA-256 会写入 `version_binding.published_sha256`。工具不提供改写该文件的代码路径。

## 5. 输出结构

```text
redistrict/
  geometry.py    # 纯标准库几何验证、包含、相触、正面积相交
  solver.py      # 位掩码域、传播、DFS、精确优化、冲突枚举
  pipeline.py    # 输入契约、候选生成、版本/摘要绑定、重述差异
  report.py      # Markdown 审计报告
  mapview.py     # SVG 小地图
  cli.py         # run / verify

examples/
  cross_controls_demo.json       # 可行：小地图+交叉控制+待核+重述差异
  local_global_conflict.json     # 局部/两两可行，整体矛盾
  invalid_overlap.json           # 几何重叠拒收
  published_release_2020.json    # 只读原发布版示例

tests/test_redistrict.py         # 几何、示例、随机暴力对照测试
outputs/                         # 生成的 JSON/MD/SVG
```

主要 JSON 字段：

- `version_binding`：边界版本、输入/几何/控制/原发布版 SHA-256；
- `address_candidates`：每个地址的候选集、来源、面积/优先级证据；
- `pending_review`：待核地址和原因；
- `controls`：实际参与本次求解的同版控制；
- `count_bounds`：每区上下界及内嵌见证；
- `joint_extrema_check`：上下界拼盘的联合可行性检查；
- `determinations`：地址级 `deterministic` / `ambiguous` 和所有合法可能去向；
- `restatement_differences`：发布数差异及精确值可行性；
- `naive_average_diagnostic`：等额/面积加权分数诊断；
- `conflict`：仅无解时出现的可复核冲突证书。

## 6. 已验证的示例结论

交叉控制示例有 8 个参与地址、2 个待核地址。精确上下界为：

| 新区 | 下界 | 上界 |
|---|---:|---:|
| A | 2 | 4 |
| B | 1 | 3 |
| C | 1 | 3 |
| D | 0 | 2 |

`P01_A_interior` 和经优先级裁定的共享边界点 `P02_AC_boundary_priority` 在所有合法分配中都确定为 A；其他参与地址仍有两个合法去向。所有见证和上下界均由 `verify` 重算通过。

测试还包含 120 个随机小问题，把优化器结果与完整笛卡尔积暴力统计逐一对照。

## 7. 局限与使用注意

- 几何容差单位与输入 CRS 一致；经纬度数据应理解 `geometry_tolerance=1e-9` 是度，投影坐标则是米/英尺。跨 CRS 数据应先统一投影。
- 当前纯标准库几何内核面向审计级小数据；超大数据集建议增加 Shapely/GEOS 后端，但仍应保留同一套拒收规则和求解输出。
- 求解器是精确指数算法，不适合在无超时/无规模评估情况下直接处理超大规模任意控制网络。可按当前 `solver.Problem` 接口接入整数规划或 SAT 后端。
- 朴素均分/面积加权值可以帮助解释换版差异，但永远不是整数地址归属证明。
