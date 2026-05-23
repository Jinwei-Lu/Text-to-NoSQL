# TEND §05 · Evaluation Methodology (v2-Agent)

> 本卷是 TEND v2-Agent **评测层** 的单一真源 (SSoT)。给定 solver 在 test 窄可见面上产出 MQL `q_p`，评测管线将其映射为 7 比特指纹并聚合为可引用的 headline 与诊断分数。任务签名、NormExec、≡_rec、gold-as-class 定义见 [01](./01_task_definition.md)；record 字段与五轴覆盖见 [02](./02_dataset_design.md)；Agent 构造见 [04](./04_agent_framework.md)。

---

## Part I

## TL;DR

TEND v2-Agent 评测层把每条 test record 上的 solver 预测 `q_p` 压缩为固定顺序的 7 比特指纹 `(EM, QSM, QFC, EX, EFM, EVM, QIM)`，再聚合为 per-record / per-slice / per-panel 三级报表。**Headline = EX**（Execution Accuracy）；其余六项均为诊断 proxy，不得替代 EX 排名。

**EX 双条件**（本架构核心承诺）：EX = 1 当且仅当 (a) `AST_check(q_p, C) = pass`，其中 `C = record.canonical_form_set`；且 (b) `NormExec(q_p, D) ≡_rec NormExec(q_g, D)`，其中 `q_g = record.MQL`、`D = mongodb_data/<db_id>.json` 冻结 witness。仅语义等价或仅结构合法均不足以得分。

**QIM** 是 EX 的**结构半**：`QIM = 1 ⟺ Parse(q_p) ≠ ⊥ ∧ AST_check(q_p, C) = pass`。因此 **EX = 1 ⟹ QIM = 1** 为严格蕴含；`QIM = 0 ∧ EX = 1` 在本架构下不可能。QIM = 1 ∧ EX = 0 表示「结构合法但执行语义错误」，是训练改进的关键信号。

其余指标（保留 v2-original 公式）：**EM** = canonical_text 相等；**QSM** = 结构树相等（stage 序列 + 算子多重集，字段/字面量屏蔽）；**QFC** = 引用字段路径集合相等；**EFM** = 结果序列等长且逐文档顶层键集合相等；**EVM** = 在 EFM = 1 前提下逐字段多重集相等，否则置 0。

**Solver 窄可见面**（test.json 单条 record）：仅可读 `nl_queries.canonical`、`db_id`、`mongodb_schema/<db_id>.json`、`mongodb_data/<db_id>.json`。显式禁止读取 `MQL`、`canonical_form_set`、`nl_queries.colloquial`（默认主评测）、一切 `_ref` / `_eval_` / `_audit_` 字段及整个 `audit/` 树。train.json 中 `MQL` 与 `canonical_form_set` 可读作监督信号。

**五轴切片**（见 [02 §4](./02_dataset_design.md#02-4)）：`domain`（domain_id）、`join_depth`（0/1/2/3+）、`aggregation_depth`（shallow/medium/deep）、`schema_pattern`（SRA 主 pattern）、`difficulty_tier`（L0–L4）。每轴对 7 指标分别求均值，形成 slice × metric 矩阵；主 headline 仍为全 test 等权 EX。

**4-panel 报告（纯观测）**：构造期由 20 个冻结模型（small/medium/large/frontier 各 5）在每条 record 上产出 pr 四元组 `(pr_small, pr_medium, pr_large, pr_frontier)`，随 release 发布为 evaluator-only 元数据。**v2-Agent 删除 amplify 反馈闭环**——4-panel 仅用于难度形状观测、Solver-vs-Panel 对比视图与 `empirical_difficulty` 标签（由 pr_medium 分桶），不得反向修改 witness 或 record。

**强制披露**：任何公开引用 TEND 分数的提交须同时披露 7 指标三级聚合、五轴切片矩阵、4-panel pr 分布、构造/评测 disjointness 时间戳、环境 digest、Solver LLM 骨干清单、per-record 指纹 CSV 等 ≥12 项（见 [§05-4](#05-4)）。缺失任一项标记 `[DISCLOSURE_INCOMPLETE]`，不得汇入 official leaderboard。

Canonical anchor **orchestra/1001** 的评测实例见 [§05-5](#05-5)；字节级 JSON 见 Part II。

---

<a id="05-1"></a>
### 05-1 七评测指标

本节给出 7 个指标的严格公式。符号：`q_p` = solver 产出；`q_g` = `record.MQL`；`D` = witness；`C` = `canonical_form_set`；`Parse` / `NormExec` / `≡_rec` 定义见 [01 §4](./01_task_definition.md#01-4) 与 [01 §5](./01_task_definition.md#01-5)。

<a id="05-1-1"></a>
#### 05-1-1 EM (Exact Match)

$$
\mathrm{EM}(q_p, q_g) = \mathbb{1}\!\left[\ \mathrm{canonical\_text}(q_p) = \mathrm{canonical\_text}(q_g)\ \right]
$$

`canonical_text` 三步：mongosh tokenize（去注释/空白）→ JSON 对象键排序 canonicalize → 空白归一化。EM 极度苛刻，主要用于检测训练集记忆，非质量指标。

<a id="05-1-2"></a>
#### 05-1-2 QSM (Query Structure Match)

$$
\mathrm{QSM}(q_p, q_g) = \mathbb{1}\!\left[\ \mathrm{tree\_equal}_\text{struct}(\mathrm{Parse}(q_p),\ \mathrm{Parse}(q_g))\ \right]
$$

结构相等四步：S1 stage 序列一致；S2 每 stage 算子 token 多重集相等；S3 字段路径 → `<F>`；S4 字面量 → `<LIT>`。

<a id="05-1-3"></a>
#### 05-1-3 QFC (Query Field Coverage)

$$
\mathrm{QFC}(q_p, q_g) = \mathbb{1}\!\left[\ \mathrm{fields}(q_p) = \mathrm{fields}(q_g)\ \right]
$$

`fields` 收集 `$match` / `$project` / `$group` 等中的字段路径（复合路径不拆分；`$let` 临时变量不计）。

<a id="05-1-4"></a>
#### 05-1-4 EX (Execution Accuracy) — 双条件头牌

$$
\boxed{\ \mathrm{EX}(q_p, q_g, D, C)\ =\ \mathbb{1}\!\left[\ \mathrm{AST\_check}(q_p, C) = \text{pass}\ \wedge\ \mathrm{NormExec}(q_p, D) \equiv_\text{rec} \mathrm{NormExec}(q_g, D)\ \right]\ }
$$

**(a) 结构条件**：`AST_check` 对四元组 `C` 扫描 `must_contain` / `must_contain_at_root` / `must_not_contain` / `must_not_contain_at_root`（权威算法见 [01 §3](./01_task_definition.md#01-3) 与 Part II）。

**(b) 语义条件**：witness 上归一化执行后与 gold 逐记录等价。

双条件堵死 shortcut：仅 (b) 允许 witness 巧合匹配的错误算子；仅 (a) 允许结构合法但累加器/过滤错误的查询。`canonical_form_set` 在构造期由 QRA/NNC 派生并扩宽，承担等价重写宽容度；评测期不可调整。

<a id="05-1-5"></a>
#### 05-1-5 EFM (Execution Field Match)

记 `R_p = NormExec(q_p, D)`、`R_g = NormExec(q_g, D)`：

$$
\mathrm{EFM}(q_p, q_g, D) = \mathbb{1}\!\left[\ |R_p| = |R_g|\ \wedge\ \forall i.\ \mathrm{keys}(R_p[i]) = \mathrm{keys}(R_g[i])\ \right]
$$

<a id="05-1-6"></a>
#### 05-1-6 EVM (Execution Value Match)

$$
\mathrm{EVM}(q_p, q_g, D) =
\begin{cases}
\mathbb{1}[\ \forall k\in \mathrm{keys}_\text{common}.\ \mathrm{multiset}(R_p[\cdot][k]) = \mathrm{multiset}(R_g[\cdot][k])\ ] & \text{if } \mathrm{EFM}=1 \\
0 & \text{otherwise}
\end{cases}
$$

<a id="05-1-7"></a>
#### 05-1-7 QIM (Query Idiomatic Match) — 结构半

$$
\mathrm{QIM}(q_p, C) = \mathbb{1}\!\left[\ \mathrm{Parse}(q_p) \ne \bot\ \wedge\ \mathrm{AST\_check}(q_p, C) = \text{pass}\ \right]
$$

| QIM | EX | 解读 |
|---|---|---|
| 0 | 0 | 结构失败 |
| 1 | 0 | 执行失败（结构合法、语义错） |
| 0 | 1 | **不可能** |
| 1 | 1 | 完全成功 |

严格蕴含：`EM = 1 ⟹ EX = 1`；`EX = 1 ⟹ QIM = EFM = EVM = 1`。QSM / QFC 与 EX 信息正交。

<a id="05-1-8"></a>
#### 05-1-8 7 比特指纹

$$
\mathrm{fp}(r) = (\mathrm{EM},\ \mathrm{QSM},\ \mathrm{QFC},\ \mathrm{EX},\ \mathrm{EFM},\ \mathrm{EVM},\ \mathrm{QIM}) \in \{0,1\}^7
$$

固定顺序；受蕴含律约束，可达组合远少于 128。指纹 per-record 落盘，切片/面板层对七维分别求均值。

---

<a id="05-2"></a>
### 05-2 评测协议

<a id="05-2-1"></a>
#### 05-2-1 数据接入

| 资产 | 路径 | solver | 评测器 |
|---|---|---|---|
| 记录清单 | `test.json` | 窄面 | 全部 |
| witness | `mongodb_data/<db_id>.json` | ✓ | ✓ |
| schema | `mongodb_schema/<db_id>.json` | ✓ | ✓ |
| 域 catalog | `spider_db_catalog.json` | 可选元数据 | ✓ |
| SRA rationale | `agent_design_rationale/<db_id>.yaml` | 禁（audit） | ✓ |

<a id="05-2-2"></a>
#### 05-2-2 Solver 窄可见面

Solver 对单条 test record **仅**读入四字段：

| 字段 | 来源 |
|---|---|
| NLQ | `record.nl_queries.canonical` |
| `db_id` | `record.db_id` |
| schema | `mongodb_schema/<db_id>.json` |
| witness | `mongodb_data/<db_id>.json` |

**禁止读取**：`MQL`、`canonical_form_set`、`nl_queries.colloquial`（主评测）、任意 `_ref` / `_eval_` / `_audit_` 前缀字段、`audit/` 全树。违反窄面视为作弊提交，整次评测拒入 leaderboard。

<a id="05-2-3"></a>
#### 05-2-3 五轴切片维度

| 轴 ID | 观测字段 | 取值域 |
|---|---|---|
| `domain` | `domain_id` | Spider ~138 domain |
| `join_depth` | `join_depth` | 0, 1, 2, 3+ |
| `aggregation_depth` | `aggregation_depth` | shallow / medium / deep |
| `schema_pattern` | `schema_pattern` | embed, bucket, mixed, … |
| `difficulty_tier` | `difficulty` | L0–L4 |

每轴生成 `|values| × 7` 指标矩阵；须与 headline EX 一并披露。

<a id="05-2-4"></a>
#### 05-2-4 4-panel 难度报告（纯观测）

pr 四元组 `(pr_small, pr_medium, pr_large, pr_frontier)`：每个 pr_X 为 panel X 上 5 个冻结模型的平均 EX。20 模型 evaluator-only 元数据随 release 发布（通常 `record._meta.pr` 或 `audit/reference_panel/`）。

| 视图 | 用途 |
|---|---|
| pr_small | 入门难度下限探测 |
| pr_medium | **主桶**；`empirical_difficulty` 由 pr_medium 分桶 |
| pr_large | 闭源旗舰对照 |
| pr_frontier | 前沿饱和探测 |

**v2-Agent 硬约束**：4-panel **仅观测**。删除 v2-original 的 amplify ≤3 轮 witness/persona 反馈。**禁止**用 panel 信号反向修改已发布 record 或 witness。报告提供 Solver EX vs Panel EX 四视图对比；`EX_ceiling`（难度加权 EX）为补充视图，不得替代 headline EX。

`empirical_difficulty` 分桶（pr_medium 主桶，跨 release 稳定）：

| 桶 | 条件 |
|---|---|
| easy | pr_medium ≥ 0.8 |
| medium | 0.5 ≤ pr_medium < 0.8 |
| hard | 0.2 ≤ pr_medium < 0.5 |
| expert | pr_medium < 0.2 |

---

<a id="05-3"></a>
### 05-3 三方 disjointness（构造 + 评测）

v2-Agent 保留 panel 对偶，构造池映射为 QRA/NNC/RA：

| 符号 | 名称 | 规模 |
|---|---|---|
| **A** | 构造 Agent LLM 池（QRA + NNC + RA） | release 钉死 |
| **B** | 4-panel 冻结 20 模型 | 5 × 4 |
| **C** | dual-bridge 池（SQL + Template） | 4 |
| **F** | frontier 子集，`F ⊂ B` | 5 |

硬条件：`A ∩ B = A ∩ C = B ∩ C = ∅`；评测期 solver 骨干 `S` 须满足 `S ∩ A = S ∩ B = S ∩ C = ∅`，否则 `disjointness_violation`，拒入 leaderboard（评测仍跑完并标记）。

证据落盘：`audit/reference_panel/construction_gate.json`、`evaluation_gate.json`、`manifest_<release>.json`。

---

<a id="05-4"></a>
### 05-4 强制披露清单

公开引用 TEND 分数须披露以下 **13 项**。缺失任一项 → `[DISCLOSURE_INCOMPLETE]`。

| # | 披露项 | 格式 |
|---|---|---|
| 1 | 7 指标三级聚合（per-record / per-slice / per-panel） | 7 张 CSV |
| 2 | 五轴 slice × metric 矩阵 | 5 张 CSV 或 JSON |
| 3 | 4-panel pr 四元组 + empirical_difficulty 分布 | per-record 4 float + enum |
| 4 | NNC dual-bridge defeat 分布 | per-bridge 计数 |
| 5 | QRA 双轨收敛率 + NNC L-tier 分布 | 百分比 + 直方图 |
| 6 | RA realism 审计通过率 | 百分比 |
| 7 | 构造/评测 disjointness 时间戳 + manifest digest | 2 个 gate JSON |
| 8 | Panel + bridge manifest digests | ≥4 SHA-256 |
| 9 | `spider_db_catalog.json` digest | SHA-256 |
| 10 | release `world_signature` 汇总 digest | SHA-256 |
| 11 | mongosh + MongoDB server image digest | 见 [§05-II-3](#05-ii-3) |
| 12 | Solver LLM 骨干 ID 清单 | `[{model_id, vendor, version_pin}, …]` |
| 13 | per-record 7-bit 指纹 | `record_id → fp` CSV |

**若适用附加项**：`disjointness_violation` 旗标；`parse_error` / `timeout_hit` / `oom_hit` / `forbidden_op_hit` 计数；自定义权重须同时报 EX 等权值；solver 使用的外部 MCP/工具清单。

Leaderboard 提交 JSON 须通过 `schemas/leaderboard.schema.json`（见 Part II）。

---

<a id="05-5"></a>
### 05-5 Canonical 示例（orchestra/1001）

三种评测实例摘要（完整 JSON 见 Part II）：

| 场景 | 指纹 | 要点 |
|---|---|---|
| 结构简化失败 | `(0,0,1,0,0,0,0)` | 缺 `$setWindowFields`/`$facet` → AST_check fail → QIM=0 |
| 逐字成功 | `(1,1,1,1,1,1,1)` | EM=1 ⟹ EX=1 |
| 等价重写 | `(0,?,1,1,1,1,1)` | EM=0 但 AST_check pass + ≡_rec → EX=1, QIM=1 |

**不变量**：合法等价重写若 AST_check fail，须在构造期扩宽 `canonical_form_set`，不得在评测期放宽 EX。

---

<a id="05-6"></a>
### 05-6 边界声明

| 不在本卷 | 去向 |
|---|---|
| NormExec / ≡_rec / 六禁算子 | [01](./01_task_definition.md) |
| record 字段 / 五轴 / split | [02](./02_dataset_design.md) |
| QRA / NNC / RA / mutations | [04](./04_agent_framework.md) |
| SMART solver / solver 边界 | [06](./06_solution_design.md) |

**收束**：给定 `(q_p, q_g, D, C)`，本卷压缩为 7 比特指纹并聚合为 EX headline；更深语义在其它卷。

---

## Part II

> 实现附录：评测管线伪代码、指标实现、mongosh 环境契约、切片聚合、leaderboard schema 索引。

<a id="05-ii-1"></a>
### 05-II-1 评测管线主循环

# uses: json, pathlib, typing

```
for record in load_json("test.json"):
    db_id = record["db_id"]
    S = load_json(f"mongodb_schema/{db_id}.json")
    D = load_json(f"mongodb_data/{db_id}.json")
    reset_witness(db_id, D)                    # 每条 record 前重载 witness

    q_p = solver(
        nl=record["nl_queries"]["canonical"],
        db_id=db_id,
        schema=S,
        witness=D,
    )

    q_g = record["MQL"]                          # evaluator-only
    C   = record["canonical_form_set"]         # evaluator-only

    if forbidden_operator_scanner(q_p):
        emit_partial_zero_fp(record, flag="forbidden_op_hit")
        continue

    ast_result = AST_check(q_p, C)
    R_p = NormExec(q_p, D)
    R_g = NormExec(q_g, D)

    fp = compute_fingerprint(q_p, q_g, R_p, R_g, C, ast_result)
    emit(
        record_id=record["record_id"],
        fingerprint=fp,
        slice_keys=extract_five_axis(record),
        diagnostics={
            "ast_result": ast_result,
            "exec_hash_p": sha256(canonical_json(R_p)),
            "exec_hash_g": sha256(canonical_json(R_g)),
            "timeout_hit": timed_out(R_p),
            "oom_hit": oom(R_p),
        },
    )

aggregate_slices(all_fps, axes=FIVE_AXES)
aggregate_panels(all_fps, panel_meta=load_panel_pr())
write_leaderboard_payload(...)
```

**异常分支**

| 情况 | 处理 |
|---|---|
| `Parse(q_p) = ⊥` | 七比特全 0，`parse_error` |
| 六禁算子 | EX = 0，其余照常 |
| 超时 (>30s) / OOM (>8GB) | `≡_rec` 判 0，EFM/EVM = 0 |
| witness 加载失败 | `env_error`，挂起该 record |

---

<a id="05-ii-2"></a>
### 05-II-2 七指标实现

# uses: typing, re, json

```
METRICS = ("EM", "QSM", "QFC", "EX", "EFM", "EVM", "QIM")

def compute_fingerprint(q_p, q_g, R_p, R_g, C, ast_result) -> tuple[int, ...]:
    if Parse(q_p) is BOT:
        return (0, 0, 0, 0, 0, 0, 0)

    em  = int(canonical_text(q_p) == canonical_text(q_g))
    qsm = int(tree_equal_struct(Parse(q_p), Parse(q_g)))
    qfc = int(fields(q_p) == fields(q_g))
    qim = int(ast_result == "pass")
    ex  = int(qim == 1 and rec_equiv(R_p, R_g))
    efm = int(execution_field_match(R_p, R_g))
    evm = int(execution_value_match(R_p, R_g)) if efm else 0
    return (em, qsm, qfc, ex, efm, evm, qim)

def AST_check(q_p: str, C: dict) -> str:
    ast = Parse(q_p)
    if ast is None:
        return "fail:parse_error"
    tokens_all  = all_operator_tokens(ast)
    tokens_root = root_stage_tokens(ast)
    for tok in C["must_contain"]:
        if tok not in tokens_all:
            return f"fail:missing:{tok}"
    for tok in C["must_contain_at_root"]:
        if tok not in tokens_root:
            return f"fail:missing_at_root:{tok}"
    for tok in C["must_not_contain"]:
        if tok in tokens_all:
            return f"fail:forbidden:{tok}"
    for tok in C["must_not_contain_at_root"]:
        if tok in tokens_root:
            return f"fail:forbidden_at_root:{tok}"
    return "pass"

def canonical_text(q: str) -> str:
    tokens = mongosh_tokenize(strip_comments(q))
    tokens = [json_canonicalize_literals(t) for t in tokens]
    return " ".join(tokens).strip()

def tree_equal_struct(a, b) -> bool:
    return (
        stage_sequence(a) == stage_sequence(b)
        and stage_operator_multiset(a) == stage_operator_multiset(b)
        and mask_fields_and_literals(a) == mask_fields_and_literals(b)
    )

def fields(q: str) -> frozenset:
    return frozenset(extract_field_paths(Parse(q)))

def execution_field_match(R_p, R_g) -> bool:
    if len(R_p) != len(R_g):
        return False
    return all(set(d.keys()) == set(g.keys()) for d, g in zip(R_p, R_g))

def execution_value_match(R_p, R_g) -> bool:
    keys = set(R_p[0].keys()) if R_p else set()
    for k in keys:
        if multiset([d[k] for d in R_p]) != multiset([d[k] for d in R_g]):
            return False
    return True
```

---

<a id="05-ii-3"></a>
### 05-II-3 mongosh 执行环境契约

评测必须在下列 **钉死环境** 下运行；否则不得挂官方 leaderboard。

| 项 | 规定 |
|---|---|
| MongoDB server | `mongodb/mongodb-community-server:7.0.14-ubuntu2204@sha256:0000000000000000000000000000000000000000000000000000000000000000` |
| mongosh | 与 server 7.0.14 配套版本；digest 落盘 `audit/env/mongosh.lock` |
| 超时 | 单查询 30 s hard cap |
| 内存 | 单查询 8 GB hard cap（OOM → 不等价） |
| 网络 | 完全禁用（`--network none`） |
| 随机 | 禁用；test 集不含 `$sample` |
| 时区 | UTC；date literal `ISODate('…Z')` |
| 浮点 | IEEE 754 double；≡_rec 在 1 ulp 容差内 |
| Collation | `simple` |

**Digest 维护**：上表 `sha256:000…000` 为 **placeholder**（64 hex）。每次 TEND release 须用 `docker inspect mongodb/mongodb-community-server:7.0.14-ubuntu2204 --format='{{index .RepoDigests 0}}'` 解析真实 digest 并更新 `audit/env/mongodb_server.lock` 与本卷引用；tag 漂移不得 silent upgrade。

每条 record 执行前 **reset witness**（从 `mongodb_data/<db_id>.json` 重新导入）。

---

<a id="05-ii-4"></a>
### 05-II-4 五轴切片聚合

# uses: collections, statistics

```
FIVE_AXES = {
    "domain":            lambda r: r.get("domain_id", catalog_domain(r["db_id"])),
    "join_depth":        lambda r: bucket_join_depth(r.get("join_depth", 0)),
    "aggregation_depth": lambda r: r["aggregation_depth"],
    "schema_pattern":    lambda r: r["schema_pattern"],
    "difficulty_tier":   lambda r: r["difficulty"],
}

def bucket_join_depth(n: int) -> str:
    if n >= 3:
        return "3+"
    return str(n)

def aggregate_slices(fingerprints: list[dict], records: list[dict]) -> dict:
    """
    fingerprints: [{record_id, fp: (em,...,qim)}, ...]
    returns: {axis: {slice_value: {metric: mean_float}}}}
    """
    out = {}
    rec_by_id = {r["record_id"]: r for r in records}
    for axis, key_fn in FIVE_AXES.items():
        buckets = defaultdict(list)
        for row in fingerprints:
            r = rec_by_id[row["record_id"]]
            buckets[key_fn(r)].append(row["fp"])
        out[axis] = {}
        for slice_val, fps in buckets.items():
            out[axis][slice_val] = {
                m: mean(bit[i] for bit in fps)
                for i, m in enumerate(METRICS)
            }
    return out

def aggregate_panels(fingerprints, panel_pr_meta) -> dict:
    """Optional: bucket by empirical_difficulty derived from pr_medium."""
    panels = ("small", "medium", "large", "frontier")
    return {
        p: mean_metric_by_panel_bucket(fingerprints, panel_pr_meta, p)
        for p in panels
    }
```

---

<a id="05-ii-5"></a>
### 05-II-5 Canonical Anchor Record

<!-- canonical-anchor: orchestra/1001 -->
```json
{
  "record_id": 1001,
  "db_id": "orchestra",
  "nl_queries": {
    "canonical": "对每位 conductor，先在其指挥的 orchestra 的 performance 上按 Performance_ID 升序、对 Attendance 计算窗口大小为 (当前, 前 2 场) 的滑动平均；取该 conductor 的最后一次窗口平均值作为代表值 (Attendance 缺失视为 0)。然后计算所有 conductor 代表值的中位数。最终只输出代表值严格大于该中位数的 conductor，字段为 Name 与 last_window_avg；若 Name 缺失则显示为 (unknown)；不要求排序。",
    "colloquial": "列出最近场次出勤趋势高于同行中位数的指挥。"
  },
  "MQL": "db.conductor.aggregate([
  { $unwind: { path: \"$orchestra\", preserveNullAndEmptyArrays: false } },
  { $unwind: { path: \"$orchestra.performance\", preserveNullAndEmptyArrays: false } },
  { $setWindowFields: {
      partitionBy: \"$_id\",
      sortBy: { \"orchestra.performance.Performance_ID\": 1 },
      output: {
        moving_avg_attendance: {
          $avg: { $ifNull: [\"$orchestra.performance.Attendance\", 0] },
          window: { documents: [-2, 0] }
        }
      }
  } },
  { $group: {
      _id: \"$_id\",
      Name: { $first: { $ifNull: [\"$Name\", \"(unknown)\"] } },
      last_window_avg: { $last: \"$moving_avg_attendance\" }
  } },
  { $facet: {
      per_conductor: [ { $project: { _id: 0, Name: 1, last_window_avg: 1 } } ],
      global_median: [
        { $sort: { last_window_avg: 1 } },
        { $group: { _id: null, vals: { $push: \"$last_window_avg\" } } },
        { $project: { _id: 0, median: { $arrayElemAt: [\"$vals\", { $floor: { $divide: [{ $size: \"$vals\" }, 2] } }] } } }
      ]
  } },
  { $project: {
      kept: { $filter: {
        input: \"$per_conductor\",
        as: \"c\",
        cond: { $gt: [\"$$c.last_window_avg\", { $arrayElemAt: [\"$global_median.median\", 0] }] }
      } }
  } },
  { $unwind: \"$kept\" },
  { $project: { _id: 0, Name: \"$kept.Name\", last_window_avg: \"$kept.last_window_avg\" } }
])",
  "canonical_form_set": {
    "must_contain": ["$setWindowFields", "$facet", "$ifNull"],
    "must_not_contain": [],
    "must_contain_at_root": ["$setWindowFields", "$facet"],
    "must_not_contain_at_root": []
  },
  "difficulty": "L4",
  "shape_policy": "reshape",
  "world_signature": "sha256:a47f3e8b1c2d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e90",
  "agent_design_rationale_ref": "fixtures/orchestra/sra.yaml",
  "mutations_ref": "fixtures/orchestra/mutations.json"
}
```

---

<a id="05-ii-6"></a>
### 05-II-6 Leaderboard JSON Schema 索引

| 文件 | 用途 |
|---|---|
| `schemas/leaderboard.schema.json` | 官方 leaderboard 提交 envelope |
| `schemas/leaderboard.schema.valid.json` | 合规示例 |
| `schemas/leaderboard.schema.invalid.json` | 违规示例（缺 EX headline） |

**校验命令**

```bash
jsonschema --schema proposals/schemas/leaderboard.schema.json \
  --instance proposals/schemas/leaderboard.schema.valid.json

jsonschema --schema proposals/schemas/leaderboard.schema.json \
  --instance proposals/schemas/leaderboard.schema.invalid.json
# 期望：非零退出码
```
