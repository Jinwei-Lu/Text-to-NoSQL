# TEND §02 · Dataset Design

> 本文件是 TEND **发布物 (released artifacts)** 的单一真源 (Single Source of Truth)。
> 它定义：哪些文件存在、每条 record 的字段契约、库级资产的 JSON 格式、train/test 切分规则、六轴覆盖配额。
> 它**不定义**：任务签名 ([01](./01_task_definition.md))、Spider 锚定 DataWorld 合成 ([03](./03_spider_anchored_dataworld.md))、Agent 查询构造 ([04](./04_agent_framework.md))、评测协议 ([05](./05_evaluation_methodology.md))、解法侧 ([06](./06_solution_design.md))。

---

## Part I

## TL;DR

TEND 的发布物由 **主集 (Tier-1)** 与 **Audit 子树 (Tier-2)** 两层构成。主集包含 `train.json`、`test.json`、`TEND.json` 三条 record 数组，以及 per-db 库级资产（`mongodb_schema/`、`mongodb_data/`、`agent_design_rationale/`）和全局 `spider_db_catalog.json`。任何合规 solver 仅读取 Tier-1 即可完成 [05](./05_evaluation_methodology.md) 规定的全部评测；`audit/` 仅供研究者复现与诊断，缺失不构成数据集不完整。

每条 record 携带 **5 项 gold 必填字段**：`record_id`、`db_id`、`nl_queries`（canonical + colloquial 二联 NLQ）、`MQL`（代表实例）、`canonical_form_set`（四元组等价类指纹）。此外每条已发布 record 必须携带扁平元数据 `difficulty`（L0–L4）、`sql_infeasibility_class`、`shape_policy`、`world_signature`。Gold 判定沿用 [01](./01_task_definition.md) 的 EX 双条件：`AST_check(q_p, canonical_form_set) = pass` 且 `NormExec(q_p, D) ≡_rec NormExec(MQL, D)`。可选 `_ref` 字段指向 audit 工件；缺失时必须**省略键**，禁止 `null` 或空字符串。

数据源为 **Spider 1.0**（约 200 个 SQLite DB），作为 **数据 + 场景源** 锚定 Phase A；Phase B 由 `QPS → MS → MUT → PV → NLP → RTV → NNC → RA` 八 Agent 流水线逆向构造 NL–MQL record，**不以 Spider NL/SQL 为查询 oracle**。切分采用 **cross-domain holdout**：train 与 test 的 Spider `domain_id` 集合不相交；同一 `domain_id` 下的全部 `db_id` 及其 record 整体进入 train 或 test，禁止跨集拆分单库。

覆盖目标采用 **六轴 + min/max 双配额**：`domain`（Spider 域）、`join_depth`（$lookup 深度）、`aggregation_depth`（管线阶段深度桶）、`schema_pattern`（SRA 应用的主 design pattern）、`schema_flex`（SRA Stage B 异构化类型）、`difficulty_tier`（L0–L4）。Coverage Controller 对每个 (轴, cell) 维护 `{min_quota, max_quota}`，欠填 cell 强拉、饱和 cell 拒绝。发布硬约束单独监控：**test L4 ≥ 30%**、**test schema_flex ≠ none ≥ 25%**（Phase A flex 供给不足时 supply-relax）、**test L0 ≤ 5%**、**test structural_schema_flex ≥ 20%**（同步 supply-relax）。

Canonical anchor 为 Spider 真实 DB `orchestra` 的 `record_id = 1001`，跨 6 卷字节级一致（见 [CANONICAL_ANCHOR.md](./_meta/CANONICAL_ANCHOR.md) 与本卷 Part II）。

---

<a id="02-1"></a>
### 02-1 主集资产清单

| 文件 / 目录 | 份数 | 角色 |
|---|---:|---|
| `train.json` | 1 | 训练集 record 数组 |
| `test.json` | 1 | 固定评测集 record 数组 |
| `TEND.json` | 1 | train + test 并集（`record_id` 排序后逐字节等于 concat） |
| `mongodb_schema/<db_id>.json` | 每选中 DB 1 份 | MongoDB 结构声明 |
| `mongodb_data/<db_id>.json` | 每选中 DB 1 份 | 冻结 witness 数据 |
| `agent_design_rationale/<db_id>.yaml` | 每选中 DB 1 份 | SRA 设计决策与 evidence chain |
| `spider_db_catalog.json` | 1 | Spider DB 清单、域映射、flex 供给标记、入选/拒绝原因 |

**Tier-1 与 Audit 硬边界**

| 规则 | 内容 |
|---|---|
| **B1 评测闭包** | 仅 Tier-1 + 库级资产构成完整评测闭包 |
| **B2 Audit 可选** | `audit/` 任意子路径缺失不构成不完整 |
| **B3 省略语义** | 可选字段缺失 = 删除键；禁止 `null` / `""` / `{}` |
| **B4 单向引用** | record 可引用 audit；删除 audit 后主集仍自洽 |

---

<a id="02-2"></a>
### 02-2 Record 字段契约

#### 02-2-1 五项 gold 必填

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `record_id` | int | 全局唯一正整数 | 跨 train/test 不重用 |
| `db_id` | string | 与三目录 per-db 文件基名一致 | 指向该 record 所用数据库 |
| `nl_queries` | object | 必含 `canonical` + `colloquial` 两个非空 string | 二联 NLQ：L1 canonical 与口语化 underspecified 端点 |
| `MQL` | string | 在 `mongodb_data/<db_id>.json` 上可执行；`AST_check(MQL, canonical_form_set) = pass` | 等价类代表实例 |
| `canonical_form_set` | object | 四元组；`must_contain_at_root` 至少 1 项 | gold-as-class AST 成员资格谓词 |

**`canonical_form_set` 四元组**

| 子字段 | 语义 |
|---|---|
| `must_contain` | 管线任意深度至少出现一次的 operator token |
| `must_not_contain` | 管线任意深度不得出现的 operator token |
| `must_contain_at_root` | 顶层 aggregation 阶段必须出现的 stage operator |
| `must_not_contain_at_root` | 顶层 aggregation 阶段不得出现的 stage operator |

#### 02-2-2 发布必填扁平字段

| 字段 | 类型 | 取值域 |
|---|---|---|
| `difficulty` | string | `L0` / `L1` / `L2` / `L3` / `L4`（NNC 赋值；L4 = NoSQL-native / translation-lossy） |
| `sql_infeasibility_class` | string | `feasible` / `semantic` / `performative` / `structural_pipeline` / `structural_schema_flex`（NNC 赋值；与 difficulty / schema_flex 相容，见 [04 §04-3](./04_agent_framework.md#04-3)） |
| `shape_policy` | string | `preserve` / `reshape` / `reduce` |
| `world_signature` | string | `sha256:<64 hex>`，钉住 `mongodb_data/<db_id>.json` |
| `schema_flex` | string | `none` / `polymorphic` / `attribute_bag` / `schema_versioning` / `dynamic_key`；H1–H4 触发时必填，否则省略或 `none` |

**`sql_infeasibility_class` 与 difficulty 相容性（摘要）**

| 类别 | 典型 difficulty | 说明 |
|---|---|---|
| `feasible` | L0–L1 | SQL 完全可直译 |
| `semantic` | L2–L3 | null/missing 语义 lossy |
| `performative` | L3–L4 | SQL 需 CTE/window 拼装，性能/结构 lossy |
| `structural_pipeline` | L4 | 管线结构 SQL 不可同步表达（如 `$facet + $setWindowFields`） |
| `structural_schema_flex` | L4 | schema 形状 SQL 不可表达；`schema_flex != none` 时 NNC 必须标注此类 |

#### 02-2-3 可选 `_ref` 字段

存在时必为指向 audit 或 fixtures 的路径字符串；不存在时必须省略键。

| 字段名 | 典型指向 |
|---|---|
| `agent_design_rationale_ref` | SRA 输出 YAML |
| `mutations_ref` | 同义/等价改写枚举 JSON |
| `_diagnostic_bridge_ref` | NNC SQL/Template bridge 诊断 verdict（非发布门） |
| `property_verification_ref` | PV 性质验证与 probe 结果 |
| `round_trip_ref` | RTV NL→MQL 往返闭包轨迹 |
| `nnc_verdict_ref` | NNC L 级与 graduated gate 裁决 |
| `ra_audit_ref` | RA realism 审计 |
| `migration_log_ref` | DM 行级迁移日志 |

#### 02-2-4 强约束 C1–C9

| ID | 约束 | 违约动作 |
|---|---|---|
| **C1** | 可选字段缺失以省略 key 表达 | 发布前校验拒绝 |
| **C2** | `nl_queries` 必含且仅含 `canonical` + `colloquial` | 发布前校验拒绝 |
| **C3** | `canonical` 为 L1 canonical（最显式、schema-naive） | NLP + RTV 生成时保证 |
| **C4** | `db_id` 必须在 `mongodb_schema/` + `mongodb_data/` + `agent_design_rationale/` 三目录同时存在 | 3-way 文件名校验 |
| **C5** | `MQL` 可执行且 AST 一致 | 发布前执行 + AST 双通 |
| **C6** | `canonical_form_set.must_contain_at_root` 非空 | 发布前校验拒绝 |
| **C7** | `difficulty`、`sql_infeasibility_class` 与 `canonical_form_set` / MQL 算子相容 | NNC 校验 |
| **C8** | 存在的 `_ref` 路径必须可解引 | 校验器对存在字段解引 |
| **C9** | `schema_flex != none` 时，`mongodb_schema/<db_id>.json` 对应 collection 须含匹配 `__variants`；`schema_flex = none` 时 record 不得声明非 none 值；`sql_infeasibility_class = structural_schema_flex` 时 `schema_flex != none` 且 `difficulty = L4` | 3-way schema/record 一致性 |

机器可读 schema：`schemas/record.schema.json`。

---

<a id="02-3"></a>
### 02-3 Cross-Domain Holdout 切分

#### 02-3-1 切分单位

| 规则 | 内容 |
|---|---|
| **SP1 domain 原子** | 切分以 Spider `domain_id` 为原子单位 |
| **SP2 domain disjoint** | `set(train.domain_id) ∩ set(test.domain_id) = ∅` |
| **SP3 db 同侧** | 同一 `domain_id` 下全部 `db_id` 及其 record 整体进入 train 或 test |
| **SP4 db_id disjoint** | `set(train.db_id) ∩ set(test.db_id) = ∅`（SP3 推论） |
| **SP5 无 audit 泄漏** | 评测时 solver 仅可访问 test 侧 `db_id` 对应库级资产 |

**禁止** domain 跨 train/test，以保证域外泛化评测的可解释性。

#### 02-3-2 比例目标

| 集合 | 近似 db 数 | 近似 record 数 | 说明 |
|---|---:|---:|---|
| Train | ~160 | ~13,500 | 按 record 数目标 ≈ 80% |
| Test | ~40 | ~3,400 | 按 record 数目标 ≈ 20% |
| **合计** | **~200** | **~17,000** | 实际规模随 Spider 入选 DB 微调 |

不设 dev / val / hidden 额外桶；研究者可从 train 按 `difficulty` 自行抽样构建 dev 集。

---

<a id="02-4"></a>
### 02-4 六轴覆盖与 test 硬约束

#### 02-4-1 六轴定义

| 轴 ID | 观测字段 | 取值域（示例） | 来源 |
|---|---|---|---|
| `domain` | `domain_id`（record 或 catalog） | Spider ~138 domain | `spider_db_catalog.json` |
| `join_depth` | `join_depth` | 0, 1, 2, 3+ | MS 代表 MQL 统计 |
| `aggregation_depth` | `aggregation_depth` | `shallow` / `medium` / `deep` | 根管线 stage 数分桶 |
| `schema_pattern` | `schema_pattern` | embed, extended_reference, polymorphic, bucket, computed, mixed, … | SRA `patterns_applied[0]` |
| `schema_flex` | `schema_flex` | `none`, `polymorphic`, `attribute_bag`, `schema_versioning`, `dynamic_key` | SRA Stage B H1–H4 |
| `difficulty_tier` | `difficulty` | L0–L4 | NNC |

**分桶规则（aggregation_depth）**

| 桶 | 根 stage 数 |
|---|---|
| `shallow` | 1–4 |
| `medium` | 5–9 |
| `deep` | ≥ 10 |

#### 02-4-2 min/max 双配额协议

Coverage Controller 对每个 (轴, cell) 维护 `count[cell]` 与 `{min_quota[cell], max_quota[cell]}`：

| 条件 | 动作 | 语义 |
|---|---|---|
| `count[cell] < min_quota[cell]` | **Strong-pull accept** | 无条件接收候选，直至到达 min |
| `min_quota[cell] ≤ count[cell] < max_quota[cell]` 且 `ΔF ≥ ε` | **Marginal accept** | 仅当带来边际覆盖增益 ΔF 超过 ε 才接收 |
| `count[cell] ≥ max_quota[cell]` | **Reject** | 该 cell 饱和，拒绝新候选 |

QPS 按 `max(0, min_quota[cell] - count[cell])` 加权采样欠填 cell。若 cell 在当前 `db_id` 上不可行（例如需要 `__variants` 但 Phase A 未触发 H1–H4），标记 `supply_constrained` 并由 Coverage Controller 将 effective min 放宽为 `min(target_min, supply_ceiling[cell])`。

<a id="02-4-3"></a>
#### 02-4-3 衍生硬约束

| ID | 约束 | 监控点 |
|---|---|---|
| **H1** | `|train| + |test| = |TEND|` | 全集基数 |
| **H2** | train/test `domain_id` 集合不相交 | cross-domain holdout |
| **H3** | train/test `db_id` 集合不相交 | db 级隔离 |
| **H4** | 三库级 per-db 目录文件基名集合相等 | schema/data/rationale 3-way |
| **H5** | `count(test, difficulty = L4) / |test| ≥ 0.30` | **L4 硬约束** |
| **H6** | 每个六轴 cell 在 test 侧 `count ≤ max_quota` | 覆盖上界 |
| **H7** | `count(test, schema_flex != "none") / |test| ≥ h7_min`；默认 `h7_min = 0.25`；当 Phase A 选中库 `flex_eligible` 比例 < 30% 时 **supply-relax**：`h7_min = max(0.15, supply_ceiling)` | **schema_flex 硬约束** |
| **H8** | `count(test, difficulty = L0) / |test| ≤ 0.05` | **L0 上界硬约束** |
| **H9** | `count(test, sql_infeasibility_class = "structural_schema_flex") / |test| ≥ h9_min`；默认 `h9_min = 0.20`；当 Phase A 选中库 `flex_eligible` 比例 < 30% 时 **supply-relax**：`h9_min = max(0.10, supply_ceiling × 0.8)` | **structural_schema_flex 硬约束** |

**supply-relax 定义**

- `flex_eligible_db_ratio = |{db ∈ selected : flex_eligible}| / |selected|`，来自 `spider_db_catalog.json`（见 §02-II-2）。
- `supply_ceiling` = 在当前 Phase A 库存下，test 侧 `schema_flex != none`（或 `structural_schema_flex`）的**可达比例上界**，由 Coverage Controller 在切分前估算。
- 当 `flex_eligible_db_ratio < 0.30` 时，H7/H9 阈值自动降为上表 supply-relax 公式；`audit/_global/coverage_report.json` 必须记录 `supply_relax_active: true` 与实际采用的 `h7_min` / `h9_min`。

违反 H1–H9 的发布候选将被拒绝。

---

<a id="02-5"></a>
### 02-5 边界声明

| 主题 | 归属 |
|---|---|
| 任务签名、NormExec、≡_rec、6 禁用算子 | [01](./01_task_definition.md) |
| gold-as-class 语义 | [01 §3](./01_task_definition.md) |
| WP/SRA/SC/DM Agent 契约 | [03](./03_spider_anchored_dataworld.md) |
| QPS/MS/MUT/PV/NLP/RTV/NNC/RA、`canonical_form_set` 派生 | [04](./04_agent_framework.md) |
| 7 评测指标、EX 判定 | [05](./05_evaluation_methodology.md) |
| SMART 求解侧边界 | [06](./06_solution_design.md) |

**发布前校验清单（本卷负责）**

1. C1–C9 record 级强约束全通
2. schema / data / rationale 三方文件名一致
3. H1–H9 切分与覆盖硬约束全通（含 L4 ≥ 30%、L0 ≤ 5%、schema_flex / structural_schema_flex 阈值及 supply-relax 状态）
4. canonical anchor `orchestra/1001` 与 [CANONICAL_ANCHOR.md](./_meta/CANONICAL_ANCHOR.md) 逐字节一致
5. 全部 record 通过 `schemas/record.schema.json` 校验（含 C9 schema_flex / `__variants` / `sql_infeasibility_class` 一致性）

---

## Part II

### 02-II-1 目录树规范

```text
TEND/
├── train.json                              # train records
├── test.json                               # test records
├── TEND.json                               # union (sorted by record_id)
│
├── mongodb_schema/                         # per-db MongoDB schema
│   ├── orchestra.json
│   └── ...
│
├── mongodb_data/                           # per-db frozen witness data
│   ├── orchestra.json
│   └── ...
│
├── agent_design_rationale/                 # per-db SRA output (YAML)
│   ├── orchestra.yaml
│   └── ...
│
├── spider_db_catalog.json                  # global Spider inventory
│
└── audit/                                  # Tier-2, optional
    ├── _global/
    │   ├── coverage_report.json
    │   └── rejected/
    └── <db_id>/<record_id>/
        ├── qps_trace.json
        ├── synthesis_trace.json
        ├── property_verification.json
        ├── round_trip_verification.json
        ├── nnc_verdict.json
        ├── diagnostic_bridge.json
        ├── mutations.json
        └── migration_log.json
```

**命名不变式**

- 所有 per-db 文件名 = Spider `db_id`（snake_case，与 SQLite 基名一致）
- `TEND.json` 排序键 = `record_id` 升序
- BSON 扩展类型以 MongoDB Extended JSON 编码（`{"$oid": "..."}` / `{"$date": "..."}`）

---

<a id="02-ii-2"></a>
### 02-II-2 Spider DB 选择脚本规范

**脚本路径（建议）**：`proposals/scripts/select_spider_dbs.py`

**输入**

| 参数 | 类型 | 说明 |
|---|---|---|
| `--spider-root` | path | Spider 1.0 根目录（含 `database/` 与 `train_spider.json`） |
| `--min-tables` | int | 默认 2 |
| `--min-queries` | int | 默认 10 |
| `--min-flex-db-ratio` | float | 默认 0.30；选中库集合中 `flex_eligible: true` 的最低比例目标（供 SC pre-audit 与 H7/H9 supply-relax 判定） |
| `--output` | path | 默认 `spider_db_catalog.json` |

**处理步骤**

1. 扫描 `database/<db_id>/<db_id>.sqlite`，跳过无法打开的库
2. 读取 `tables.json` / `columns.json` 统计 `table_count`；读取 `train_spider.json` + `dev.json` 统计每库 `query_count`
3. 从 Spider 官方 domain 映射加载 `domain_id`（若无则回退 `db_id` 前缀聚类）
4. 对每个候选库运行 **flex eligibility 预判**（与 [03](./03_spider_anchored_dataworld.md) SC pre-audit 同规则）：至少一条 H1–H4 触发证据 → `flex_eligible: true`，否则 `false`
5. 过滤：`table_count ≥ min_tables` AND `query_count ≥ min_queries` AND 每表至少 1 行数据
6. 写入 `spider_db_catalog.json`，每条记录含 `selected: true/false`、`flex_eligible: bool`、`selection_reason` / `reject_reason`
7. 输出 JSON 必须通过 `schemas/library.schema.json` 中 `spider_db_catalog` 定义
8. 若 `|{selected : flex_eligible}| / |selected| < --min-flex-db-ratio`，脚本 **warn**（不 fail-fast）；Coverage Controller 在发布切分时对 H7/H9 启用 supply-relax

**catalog 条目示例字段**

```json
{
  "db_id": "student_assessment",
  "domain_id": "education",
  "table_count": 5,
  "query_count": 42,
  "flex_eligible": true,
  "selected": true,
  "selection_reason": "meets min_tables/min_queries; H1 polymorphic trigger"
}
```

**边界条件（至少 3 条）**

1. SQLite 文件损坏 → 标记 `selected: false`，`reject_reason: "sqlite_open_failed"`
2. 查询数为 0 但表结构有效 → 拒绝，保留 catalog 行供审计
3. 重复 `db_id` → 脚本 fail-fast，不静默覆盖

---

### 02-II-3 Cross-Domain Split 算法

# uses: json, random, collections

```
function cross_domain_split(catalog, records, *, test_ratio=0.20, seed=42):
    """
    Partition records into train/test with domain-disjoint holdout.
    catalog: spider_db_catalog.json parsed
    records: list of record dicts (pre-split pool)
    """
    rng = random.Random(seed)

    # 0. Phase A flex supply (for H7/H9 supply-relax)
    selected = [e for e in catalog["databases"] if e["selected"]]
    flex_eligible_db_ratio = (
        sum(1 for e in selected if e.get("flex_eligible", False)) / max(len(selected), 1)
    )
    supply_relax = flex_eligible_db_ratio < 0.30
    supply_ceiling = estimate_schema_flex_ceiling(records, catalog)  # pre-split upper bound

    if supply_relax:
        h7_min = max(0.15, supply_ceiling)
        h9_min = max(0.10, supply_ceiling * 0.8)
    else:
        h7_min = 0.25
        h9_min = 0.20

    # 1. Group db_ids by domain
    domain_to_dbs = defaultdict(set)
    db_to_domain = {}
    for entry in catalog["databases"]:
        if not entry["selected"]:
            continue
        domain_to_dbs[entry["domain_id"]].add(entry["db_id"])
        db_to_domain[entry["db_id"]] = entry["domain_id"]

    # 2. Count records per domain
    domain_record_count = Counter()
    for r in records:
        d = db_to_domain[r["db_id"]]
        domain_record_count[d] += 1

    # 3. Greedy bin-packing: assign whole domains to test until ~test_ratio
    domains = list(domain_record_count.keys())
    rng.shuffle(domains)
    target_test = int(len(records) * test_ratio)
    test_domains = set()
    test_count = 0
    for d in sorted(domains, key=lambda x: -domain_record_count[x]):
        if test_count < target_test:
            test_domains.add(d)
            test_count += domain_record_count[d]

    train_domains = set(domains) - test_domains
    assert train_domains.isdisjoint(test_domains)

    # 4. Materialize splits (whole db_id follows domain)
    train, test = [], []
    for r in records:
        if db_to_domain[r["db_id"]] in test_domains:
            test.append(r)
        else:
            train.append(r)

    n_test = max(len(test), 1)

    # 5. Hard constraint H5: L4 ratio in test
    l4_ratio = sum(1 for r in test if r["difficulty"] == "L4") / n_test
    if l4_ratio < 0.30:
        raise SplitError(f"test L4 ratio {l4_ratio:.3f} < 0.30 (H5)")

    # 6. Hard constraint H7: schema_flex ratio in test (supply-relax aware)
    flex_ratio = sum(1 for r in test if r.get("schema_flex", "none") != "none") / n_test
    if flex_ratio < h7_min:
        raise SplitError(
            f"test schema_flex ratio {flex_ratio:.3f} < h7_min {h7_min:.3f} (H7)"
            + (" [supply-relax active]" if supply_relax else "")
        )

    # 7. Hard constraint H8: L0 upper bound in test
    l0_ratio = sum(1 for r in test if r["difficulty"] == "L0") / n_test
    if l0_ratio > 0.05:
        raise SplitError(f"test L0 ratio {l0_ratio:.3f} > 0.05 (H8)")

    # 8. Hard constraint H9: structural_schema_flex ratio in test (supply-relax aware)
    ssf_ratio = sum(
        1 for r in test if r.get("sql_infeasibility_class") == "structural_schema_flex"
    ) / n_test
    if ssf_ratio < h9_min:
        raise SplitError(
            f"test structural_schema_flex ratio {ssf_ratio:.3f} < h9_min {h9_min:.3f} (H9)"
            + (" [supply-relax active]" if supply_relax else "")
        )

    return train, test, {
        "train_domains": train_domains,
        "test_domains": test_domains,
        "supply_relax_active": supply_relax,
        "flex_eligible_db_ratio": flex_eligible_db_ratio,
        "h7_min": h7_min,
        "h9_min": h9_min,
    }
```

**错误处理**

| 异常 | 触发条件 | 动作 |
|---|---|---|
| `SplitError` | test 违反 H5 / H7 / H8 / H9 | 拒绝发布；回传 Coverage Controller 重采样或调整 test domain 集合 |
| `KeyError` | record.db_id 不在 catalog | fail-fast |
| 空 test 集 | 所有 domain 被分到 train | fail-fast |

---

### 02-II-4 Canonical Anchor Record

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

### 02-II-5 JSON Schema 索引

| 文件 | 校验对象 |
|---|---|
| `schemas/record.schema.json` | 单条 record |
| `schemas/record.schema.valid.json` | valid 示例（orchestra/1001） |
| `schemas/record.schema.invalid.json` | invalid 示例（缺 `MQL`） |
| `schemas/library.schema.json` | 库级资产（schema / data / rationale / catalog） |

**校验命令**

```bash
jsonschema --schema proposals/schemas/record.schema.json \
  --instance proposals/schemas/record.schema.valid.json

jsonschema --schema proposals/schemas/record.schema.json \
  --instance proposals/schemas/record.schema.invalid.json
# 期望：非零退出码
```
