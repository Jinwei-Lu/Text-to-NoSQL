# TEND §02 · Dataset Design

> 本文件是 TEND **发布物 (released artifacts)** 的单一真源 (Single Source of Truth)。
> 它定义：哪些文件存在、每条 record 的字段契约、库级资产的 JSON 格式、**test-only** 集组成规则、覆盖配额（RAR 因端三轴）。
> 它**不定义**：任务签名 ([01](./01_task_definition.md))、BIRD 锚定的 DAR DataWorld 构造 ([03](./03_dataworld_construction.md))、Agent 查询构造 ([04](./04_agent_framework.md))、评测协议 ([05](./05_evaluation_methodology.md))、解法侧 ([06](./06_solution_design.md))。
>
> **构造源**：BIRD mini-dev(`minidev/MINIDEV/`,11 真实业务库），**test-only**(11 库全部进 test，无 train split、无 cross-domain holdout）。

---

## Part I

## TL;DR

TEND 的发布物由 **主集 (Tier-1)** 与 **Audit 子树 (Tier-2)** 两层构成。TEND 为 **test-only** 基准：11 个 BIRD 业务库全部进入 test，**不产 `train.json`、无 cross-domain holdout**。主集包含 `test.json` 与 `TEND.json`（`TEND.json ≡ test.json`，仅 `record_id` 排序后逐字节相等）两条等价 record 数组，以及 per-db 库级资产（`mongodb_schema/`、`mongodb_data/`、`agent_design_rationale/`）和全局 `bird_db_catalog.json`。任何合规 solver 仅读取 Tier-1 即可完成 [05](./05_evaluation_methodology.md) 规定的全部评测；`audit/` 仅供研究者复现与诊断，缺失不构成数据集不完整。

每条 record 携带 **5 项 gold 必填字段**：`record_id`、`db_id`、`nl_queries`（canonical + colloquial 二联 NLQ）、`MQL`（代表实例）、`canonical_form_set`（四元组等价类指纹）。此外每条已发布 record 必须携带扁平元数据 `difficulty`（L0–L4）、`sql_infeasibility_class`、`shape_policy`、`world_signature`。Gold 判定沿用 [01](./01_task_definition.md) 的 EX 双条件：`AST_check(q_p, canonical_form_set) = pass` 且 `NormExec(q_p, D) ≡_rec NormExec(MQL, D)`。可选 `_ref` 字段指向 audit 工件；缺失时必须**省略键**，禁止 `null` 或空字符串。

数据源为 **BIRD mini-dev**（`minidev/MINIDEV/`，11 个真实业务库），作为 **数据 + 查询工作负载源** 锚定 Phase A（详见 [03](./03_dataworld_construction.md)）；Phase B 由 `QPS → MS → MUT → PV → NLP → RTV → NNC → RA` 八 Agent 流水线逆向构造 NL–MQL record，**不以 BIRD NL/SQL 为查询 oracle**。发布采用 **test-only 组成**：11 库及其全部 record 整体进入 test，**无 train split、无 domain-disjoint holdout**；研究者可自行从 test 按 `difficulty` 抽样构建 dev 集（见 §02-3）。

覆盖目标采用 **因端三轴 + min/max 双配额**（RAR：配额管因、观测果）：主轴 `seed_mechanism`（DAR 机制 = `schema_flex`）、`question_archetype`（archetype 目录条目，[04 §04-2-4](./04_agent_framework.md#04-2-4)）、`domain`（BIRD 域）；`difficulty_tier` / `join_depth` / `aggregation_depth` / `schema_pattern` 降为 gold 落出的**派生观测**。Coverage Controller 对每个 (主轴, cell) 维护 `{min_quota, max_quota}`，欠填 cell 强拉、饱和 cell 拒绝。test 集组成硬约束单独监控：**test L4 ≥ 30%**、**test schema_flex ≠ none ≥ 25%**（query-bearing 供给不足时放宽）、**test L0 ≤ 5%**、**test structural_schema_flex ≥ 20%**（同步放宽）。

Canonical anchor 为 `financial` 的 `record_id = 1001`，跨 6 卷字节级一致（见 [CANONICAL_ANCHOR.md](./_meta/CANONICAL_ANCHOR.md) 与本卷 Part II）。它取自 BIRD 真实库 `financial`（已在 test-only 集）；**pending DAR Phase A 执行验证**——account 反范式化布局、gold MQL 与 `world_signature` 待 DAR Phase A 在 MongoDB 上构造 + 执行验证后冻结真值（异构信号 稀疏 `loan` / 多态 `trans` 实测，见 [03 §03-6-4](./03_dataworld_construction.md#03-6-4)）。

---

<a id="02-1"></a>
### 02-1 主集资产清单

| 文件 / 目录 | 份数 | 角色 |
|---|---:|---|
| `train.json` | 0 | **test-only 不产**（无 train split） |
| `test.json` | 1 | 固定评测集 record 数组（11 库全部 record） |
| `TEND.json` | 1 | **`TEND.json ≡ test.json`**：与 `test.json` 同集，仅 `record_id` 排序后逐字节相等 |
| `mongodb_schema/<db_id>.json` | 每库 1 份（共 11） | MongoDB 结构声明 |
| `mongodb_data/<db_id>.json` | 每库 1 份（共 11） | 冻结 witness 数据 |
| `agent_design_rationale/<db_id>.yaml` | 每库 1 份（共 11） | SRA 设计决策与 evidence chain |
| `bird_db_catalog.json` | 1 | BIRD 11 库清单、域映射、query-bearing 供给标记、装载/拒绝原因 |

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
| `record_id` | int | 全局唯一正整数 | test 全集内不重用 |
| `db_id` | string | 与三目录 per-db 文件基名一致 | 指向该 record 所用数据库 |
| `nl_queries` | object | 必含 `canonical` + `colloquial` 两个非空 string | 二联 NLQ：L1 canonical 与口语化 underspecified 端点 |
| `MQL` | string | 在 `mongodb_data/<db_id>.json` 上可执行；`AST_check(MQL, cfs) = pass` 且 `NormExec(MQL,D) ≡_rec R(D)`（P1 参照锚定，[04 §04-2-4](./04_agent_framework.md#04-2-4)） | 等价类代表实例 |
| `canonical_form_set` | object | 四元组（**RAR thin**：idiom-不变量 + output 守卫）；`must_not_contain` 恒含 6 禁用算子；`must_contain_at_root` **可空** | gold-as-class AST 成员资格谓词（结构判别力交 witness，[01 §01-3-1](./01_task_definition.md#01-3-1)） |

**`canonical_form_set` 四元组**

| 子字段 | 语义 |
|---|---|
| `must_contain` | 管线任意深度至少出现一次的 operator token |
| `must_not_contain` | 管线任意深度不得出现的 operator token |
| `must_contain_at_root` | 顶层 aggregation 阶段必须出现的 stage operator |
| `must_not_contain_at_root` | 顶层 aggregation 阶段不得出现的 stage operator |

> **RAR thin cfs**：cfs 已坍缩为 **idiom-不变量 + output-space 守卫**——`must_not_contain` 恒含 6 禁用算子；`must_contain`/`must_contain_at_root` 仅收所有正确 idiom 共有的不可避免结构算子（如 `$lookup`/`$setWindowFields`）+ shape 守卫，**不**锁可替换 idiom（`$addFields`↔`$project`、`$cond`↔`$switch`↔`$ifNull`）。「是否正面处理异构」由 witness（L2/P3）判别，非 cfs。派生见 [04 §04-3-2](./04_agent_framework.md#04-3-2)。

#### 02-2-2 发布必填扁平字段

| 字段 | 类型 | 取值域 |
|---|---|---|
| `difficulty` | string | `L0` / `L1` / `L2` / `L3` / `L4`（NNC 赋值；L4 = NoSQL-native / translation-lossy） |
| `sql_infeasibility_class` | string | `feasible` / `semantic` / `performative` / `structural_pipeline` / `structural_schema_flex`（NNC 赋值；与 difficulty / schema_flex 相容，见 [04 §04-3](./04_agent_framework.md#04-3)） |
| `shape_policy` | string | `preserve` / `reshape` / `reduce` |
| `world_signature` | string | `sha256:<64 hex>`，钉住 `mongodb_data/<db_id>.json` |
| `schema_flex` | string | `none` / `polymorphic` / `attribute_bag` / `schema_versioning` / `dynamic_key`；DAR 五机制（详见 [03 §03-6](./03_dataworld_construction.md#03-6)）命中时必填，否则省略或 `none` |

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
| `reference_oracle_ref` | archetype 参照实现 R（gold 锁死 oracle，[04 §04-2-4](./04_agent_framework.md#04-2-4)） |
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
| **C6** | cfs 非空性：`must_not_contain` ⊇ 6 禁用算子（**RAR：`must_contain_at_root` 可空**，非空性由 must_not_contain + shape 守卫承担，[04 §04-5-4](./04_agent_framework.md#04-5-4)） | 发布前校验拒绝 |
| **C7** | `difficulty`、`sql_infeasibility_class` 与 `canonical_form_set` / MQL 算子相容 | NNC 校验 |
| **C8** | 存在的 `_ref` 路径必须可解引 | 校验器对存在字段解引 |
| **C9** | `schema_flex != none` 时，`mongodb_schema/<db_id>.json` 对应 collection 须含匹配 `__variants`；`schema_flex = none` 时 record 不得声明非 none 值；`sql_infeasibility_class = structural_schema_flex` 时 `schema_flex != none` 且 `difficulty = L4` | 3-way schema/record 一致性 |

> **C9 补充（DAR 门禁，详见 [03 §03-6-3](./03_dataworld_construction.md#03-6-3)）**：
> - **Gate-QB（query-bearing）**：每个异构机制实例（一个 `__variant` / 一个稀疏字段 / 一个版本）必须至少被一条已发布 record 的 MQL 真正利用——抹平该机制后 echo-gold 结果改变；否则判定为装饰，从 schema 与 data 一并删除。
> - **Gate-SD（schema≡data，集合级）**：S 声明的每个字段/变体须在 `mongodb_data` 出现，data 每个字段/变体须在 S 契约下可声明；守**集合层**，**不要求逐 doc 形状**（`__variants` 为混合披露契约：声明判别列 + 字段集 + presence，不承诺判别值→精确字段集的确定函数，[03 §03-6-2](./03_dataworld_construction.md#03-6-2)）。
> - **判别器键用真实列名**（如 `bond_type`）；禁止 `field_a` / `variant_a` / `__type` 合成键与冗余 `payload` 镜像。**③ 类型漂移仅当真实混合类型列存在才恢复，永不合成。**

机器可读 schema：`schemas/record.schema.json`。

---

<a id="02-3"></a>
### 02-3 Test-only 组成

TEND 是 **test-only** 基准：BIRD mini-dev 的 11 个业务库**全部进入 test**，**不切出 train、不做 cross-domain holdout**。不存在 domain-disjoint 约束——构造源仅 11 库，按域划分 train/test 既无足够供给也非本基准目标（域外泛化评测不在 TEND 范围内）。

#### 02-3-1 组成单位

| 规则 | 内容 |
|---|---|
| **TO1 全部入 test** | 11 个 BIRD 库及其全部 record 整体构成 test；不产 `train.json` |
| **TO2 无 domain holdout** | 不按 `domain_id` 切分；不存在 `train.domain_id` / `test.domain_id` 不相交约束（N/A，test-only） |
| **TO3 TEND ≡ test** | `TEND.json` 与 `test.json` 同集，仅 `record_id` 升序排序后逐字节相等 |
| **TO4 db 全覆盖** | `set(test.db_id)` = 11 个 BIRD `db_id` 全集；每库三方库级资产齐备 |
| **TO5 无 audit 泄漏** | 评测时 solver 仅读取 Tier-1（test 侧库级资产）；`audit/` 不入评测闭包 |

#### 02-3-2 test 集规模

| 集合 | db 数 | record 数 | 说明 |
|---|---:|---:|---|
| Test（= TEND） | 11 | 待 DAR Phase A/B 物化后确定 | 11 库全部入 test；规模随 query-bearing 供给与覆盖配额而定 |

11 个 BIRD 库：`california_schools`、`card_games`、`codebase_community`、`debit_card_specializing`、`european_football_2`、`financial`、`formula_1`、`student_club`、`superhero`、`thrombosis_prediction`、`toxicology`。

不设 train / dev / val / hidden 额外桶；研究者可从 test 按 `difficulty` 自行抽样构建 dev 集（不改变发布物）。

---

<a id="02-4"></a>
### 02-4 覆盖轴与 test 硬约束

#### 02-4-1 覆盖轴（RAR：配额管因，观测果）

RAR 下配额 cell 底座从「算子/形状」换为**因端三元组**（[04 §04-2-4](./04_agent_framework.md#04-2-4)）；难度/join/agg 是 gold 落出的**派生观测**，监控不设目标。

**主配额轴（cause，min/max 双配额驱动 QPS 采样）**

| 轴 ID | 观测字段 | 取值域（示例） | 来源 |
|---|---|---|---|
| `seed_mechanism` | `schema_flex` | `none` / `polymorphic` / `attribute_bag` / `schema_versioning` / `dynamic_key`（DAR 机制 ①②④⑤ / none） | Phase A Gate-QB 异构 |
| `question_archetype` | `archetype` | 目录条目（per-subtype agg / present-missing 投影 / dynamic-key fold / 跨版本 agg / 基线 filter-group …） | archetype 目录（[04 §04-2-4](./04_agent_framework.md#04-2-4)） |
| `domain` | `domain_id` | BIRD 11 库 domain（finance, sports, education, …） | `bird_db_catalog.json` |

**派生观测轴（effect，监控不配额）**

| 轴 ID | 观测字段 | 来源 |
|---|---|---|
| `difficulty_tier` | `difficulty`（L0–L4） | 由 `seed_mechanism × archetype` 派生，NNC 确认 |
| `join_depth` / `aggregation_depth` | gold MQL 统计 | MS 落出 |
| `schema_pattern` | `patterns_applied[0]` | SRA（Phase A 描述统计） |

**分桶规则（aggregation_depth）**

| 桶 | 根 stage 数 |
|---|---|
| `shallow` | 1–4 |
| `medium` | 5–9 |
| `deep` | ≥ 10 |

#### 02-4-2 min/max 双配额协议

Coverage Controller 对每个 (主轴, cell) 维护 `count[cell]` 与 `{min_quota[cell], max_quota[cell]}`：

| 条件 | 动作 | 语义 |
|---|---|---|
| `count[cell] < min_quota[cell]` | **Strong-pull accept** | 无条件接收候选，直至到达 min |
| `min_quota[cell] ≤ count[cell] < max_quota[cell]` 且 `ΔF ≥ ε` | **Marginal accept** | 仅当带来边际覆盖增益 ΔF 超过 ε 才接收 |
| `count[cell] ≥ max_quota[cell]` | **Reject** | 该 cell 饱和，拒绝新候选 |

QPS 按 `max(0, min_quota[cell] - count[cell])` 加权采样欠填 cell。若 cell 在当前 `db_id` 上不可行（例如需要 `__variants` 但该库 DAR 五机制无 query-bearing 供给），标记 `supply_constrained` 并由 Coverage Controller 将 effective min 放宽为 `min(target_min, supply_ceiling[cell])`。

<a id="02-4-3"></a>
#### 02-4-3 衍生硬约束

所有约束均为 **test 集组成目标**（test-only，无 train split）。

| ID | 约束 | 监控点 |
|---|---|---|
| **H1** | `|test| = |TEND|`（`TEND.json ≡ test.json`） | 全集基数 |
| **H2** | ~~train/test `domain_id` 集合不相交~~ | **N/A（test-only，无 holdout）** |
| **H3** | ~~train/test `db_id` 集合不相交~~ | **N/A（test-only，11 库全入 test）** |
| **H4** | 三库级 per-db 目录文件基名集合相等（= 11 BIRD `db_id`） | schema/data/rationale 3-way |
| **H5** | `count(test, difficulty = L4) / |test| ≥ 0.30` | **L4 组成目标** |
| **H6** | 每个主轴 cell 在 test 侧 `count ≤ max_quota` | 覆盖上界 |
| **H7** | `count(test, schema_flex != "none") / |test| ≥ h7_min`；默认 `h7_min = 0.25`；当 query-bearing 异构供给比例 < 30% 时 **放宽**：`h7_min = max(0.15, supply_ceiling)` | **schema_flex 组成目标** |
| **H8** | `count(test, difficulty = L0) / |test| ≤ 0.05` | **L0 上界目标** |
| **H9** | `count(test, sql_infeasibility_class = "structural_schema_flex") / |test| ≥ h9_min`；默认 `h9_min = 0.20`；当 query-bearing 异构供给比例 < 30% 时 **放宽**：`h9_min = max(0.10, supply_ceiling × 0.8)` | **structural_schema_flex 组成目标** |

**供给放宽定义（query-bearing 供给不足时放宽）**

- `flex_eligible_db_ratio = |{db : query-bearing 异构供给充足}| / 11`，来自 `bird_db_catalog.json`（见 §02-II-2）；判定依据为 DAR 五机制经 Gate-QB 后的 query-bearing 命中（[03 §03-6](./03_dataworld_construction.md#03-6)）。
- `supply_ceiling` = 在 11 库 DAR 物化结果下，test 侧 `schema_flex != none`（或 `structural_schema_flex`）的**可达比例上界**，由 Coverage Controller 在组成校验前估算。
- 当 `flex_eligible_db_ratio < 0.30` 时，H7/H9 阈值自动降为上表放宽公式；`audit/_global/coverage_report.json` 必须记录 `supply_relax_active: true` 与实际采用的 `h7_min` / `h9_min`。

违反 H1、H4–H9 的发布候选将被拒绝（H2/H3 在 test-only 下不适用）。

---

<a id="02-5"></a>
### 02-5 边界声明

| 主题 | 归属 |
|---|---|
| 任务签名、NormExec、≡_rec、6 禁用算子 | [01](./01_task_definition.md) |
| gold-as-class 语义 | [01 §3](./01_task_definition.md) |
| WP/SRA/SC/DM Agent 契约 | [03](./03_dataworld_construction.md) |
| QPS/MS/MUT/PV/NLP/RTV/NNC/RA、`canonical_form_set` 派生 | [04](./04_agent_framework.md) |
| 7 评测指标、EX 判定 | [05](./05_evaluation_methodology.md) |
| SMART 求解侧边界 | [06](./06_solution_design.md) |

**发布前校验清单（本卷负责）**

1. C1–C9 record 级强约束全通（含 Gate-QB / Gate-SD、判别器真实列名）
2. schema / data / rationale 三方文件名一致（= 11 BIRD `db_id`）
3. H1、H4–H9 test 集组成硬约束全通（含 L4 ≥ 30%、L0 ≤ 5%、schema_flex / structural_schema_flex 阈值及供给放宽状态；H2/H3 N/A）
4. **Canonical 锚一致性**：canonical anchor `financial/1001` 与 [CANONICAL_ANCHOR.md](./_meta/CANONICAL_ANCHOR.md) 逐字节一致（pending DAR Phase A 执行验证）
5. 全部 record 通过 `schemas/record.schema.json` 校验（含 C9 schema_flex / `__variants` / `sql_infeasibility_class` 一致性）

---

## Part II

### 02-II-1 目录树规范

```text
TEND/                                       # test-only (no train.json)
├── test.json                               # test records (all 11 BIRD dbs)
├── TEND.json                               # ≡ test.json (sorted by record_id)
│
├── mongodb_schema/                         # per-db MongoDB schema (11)
│   ├── financial.json
│   └── ...
│
├── mongodb_data/                           # per-db frozen witness data (11)
│   ├── financial.json
│   └── ...
│
├── agent_design_rationale/                 # per-db SRA output (YAML, 11)
│   ├── financial.yaml
│   └── ...
│
├── bird_db_catalog.json                    # global BIRD 11-db inventory
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

- 所有 per-db 文件名 = BIRD `db_id`（snake_case，与 `minidev/MINIDEV/dev_databases/<db_id>/` 基名一致）
- `TEND.json` 排序键 = `record_id` 升序（`TEND.json ≡ test.json`）
- BSON 扩展类型以 MongoDB Extended JSON 编码（`{"$oid": "..."}` / `{"$date": "..."}`）

---

<a id="02-ii-2"></a>
### 02-II-2 BIRD 库装载规范

test-only 下**不做选择过滤**：BIRD mini-dev 的 11 个库**全部直接装载并入 test**。本脚本仅做 catalog 物化与 query-bearing 供给标记，无入选/拒绝门槛（`min-flex-db-ratio` 等 Spider 选择门槛删除）。

**脚本路径（建议）**：`proposals/scripts/load_bird_dbs.py`

**输入**

| 参数 | 类型 | 说明 |
|---|---|---|
| `--bird-root` | path | BIRD mini-dev 根目录 `minidev/MINIDEV/`（含 `dev_databases/`、`dev_tables.json`、`mini_dev_sqlite.json`） |
| `--output` | path | 默认 `bird_db_catalog.json` |

**处理步骤**

1. 扫描 `dev_databases/<db_id>/<db_id>.sqlite`（11 库），逐库装载；任一库无法打开 → fail-fast（11 库均为 test 必需，不静默跳过）
2. 读取 `dev_tables.json` 统计每库 `table_count`；读取 `mini_dev_sqlite.json` 的 `(question, evidence, SQL, difficulty)` 工作负载统计每库 `query_count`
3. 从 BIRD 库语义指定 `domain_id`（如 `financial` → finance、`formula_1` → sports、`california_schools` → education）
4. 对每个库标注 **query-bearing 异构供给预判**（与 [03 §03-6-3](./03_dataworld_construction.md#03-6-3) SC Gate-QB pre-audit 同规则）：DAR 五机制中存在被真实 BIRD SQL 引用、经 Gate-QB 后 bearing 的机制 → `flex_eligible: true`，否则 `false`（不影响装载，仅供 H7/H9 供给放宽判定）
5. 写入 `bird_db_catalog.json`，每条记录含 `selected: true`（11 库恒为 true）、`flex_eligible: bool`、`load_note`
6. 输出 JSON 须通过 `schemas/library.schema.json` 中 `bird_db_catalog` 定义
7. query-bearing 供给统计：`flex_eligible_db_ratio = |{db : flex_eligible}| / 11`；若 `< 0.30`，脚本 **warn**（不 fail-fast）；Coverage Controller 在组成校验时对 H7/H9 启用供给放宽

**catalog 条目示例字段**

```json
{
  "db_id": "financial",
  "domain_id": "finance",
  "table_count": 8,
  "query_count": 106,
  "flex_eligible": true,
  "selected": true,
  "load_note": "loaded into test; H1 polymorphic on account.frequency (query-bearing, 8 SQL refs)"
}
```

**边界条件（至少 3 条）**

1. SQLite 文件损坏 → **fail-fast**（test-only 要求 11 库齐备，缺一即不完整）
2. 某库 query-bearing 异构供给为零（`flex_eligible: false`）→ 仍装载入 test，仅计入 supply 放宽分母
3. 重复 `db_id` 或库数 ≠ 11 → 脚本 fail-fast，不静默覆盖

---

### 02-II-3 Test-only 组成与校验算法

test-only 下无切分：**全部 record 进 test**，不按 domain 分组、不物化 train/test 两侧。本算法仅 (1) 把候选池整体落为 test（= TEND），(2) 校验 H5/H7/H8/H9 组成比例（含供给放宽）。

# uses: json

```
function compose_test_set(catalog, records):
    """
    Compose the test-only release: ALL records form test (= TEND).
    No domain partition, no train side, no holdout.
    catalog: bird_db_catalog.json parsed (11 BIRD dbs, selected==true for all)
    records: list of record dicts (full pool)
    Returns (test, report); raises ComposeError on H5/H7/H8/H9 violation.
    """
    # 0. query-bearing flex supply (for H7/H9 supply-relax)
    flex_eligible_db_ratio = (
        sum(1 for e in catalog["databases"] if e.get("flex_eligible", False)) / 11
    )
    supply_relax = flex_eligible_db_ratio < 0.30
    supply_ceiling = estimate_schema_flex_ceiling(records, catalog)  # reachable upper bound

    if supply_relax:
        h7_min = max(0.15, supply_ceiling)
        h9_min = max(0.10, supply_ceiling * 0.8)
    else:
        h7_min = 0.25
        h9_min = 0.20

    # 1. All records -> test (= TEND). No domain grouping, no train side.
    test = list(records)
    if not test:
        raise ComposeError("empty test set")

    # 2. db coverage: test db_ids must equal the 11 BIRD db_ids
    catalog_dbs = {e["db_id"] for e in catalog["databases"]}
    test_dbs = {r["db_id"] for r in test}
    if test_dbs != catalog_dbs or len(catalog_dbs) != 11:
        raise ComposeError(f"test db coverage {len(test_dbs)} != 11 BIRD dbs (H4)")

    n_test = len(test)

    # 3. Hard constraint H5: L4 ratio in test
    l4_ratio = sum(1 for r in test if r["difficulty"] == "L4") / n_test
    if l4_ratio < 0.30:
        raise ComposeError(f"test L4 ratio {l4_ratio:.3f} < 0.30 (H5)")

    # 4. Hard constraint H7: schema_flex ratio in test (supply-relax aware)
    flex_ratio = sum(1 for r in test if r.get("schema_flex", "none") != "none") / n_test
    if flex_ratio < h7_min:
        raise ComposeError(
            f"test schema_flex ratio {flex_ratio:.3f} < h7_min {h7_min:.3f} (H7)"
            + (" [supply-relax active]" if supply_relax else "")
        )

    # 5. Hard constraint H8: L0 upper bound in test
    l0_ratio = sum(1 for r in test if r["difficulty"] == "L0") / n_test
    if l0_ratio > 0.05:
        raise ComposeError(f"test L0 ratio {l0_ratio:.3f} > 0.05 (H8)")

    # 6. Hard constraint H9: structural_schema_flex ratio in test (supply-relax aware)
    ssf_ratio = sum(
        1 for r in test if r.get("sql_infeasibility_class") == "structural_schema_flex"
    ) / n_test
    if ssf_ratio < h9_min:
        raise ComposeError(
            f"test structural_schema_flex ratio {ssf_ratio:.3f} < h9_min {h9_min:.3f} (H9)"
            + (" [supply-relax active]" if supply_relax else "")
        )

    return test, {
        "test_dbs": sorted(test_dbs),
        "supply_relax_active": supply_relax,
        "flex_eligible_db_ratio": flex_eligible_db_ratio,
        "h7_min": h7_min,
        "h9_min": h9_min,
    }
    # TEND.json := test sorted by record_id (byte-identical to test.json sorted)
```

**错误处理**

| 异常 | 触发条件 | 动作 |
|---|---|---|
| `ComposeError` | test 违反 H5 / H7 / H8 / H9，或 db 覆盖 ≠ 11 库 | 拒绝发布；回传 Coverage Controller 重采样（QPS 调配覆盖 cell 以补足组成比例） |
| `KeyError` | record.db_id 不在 catalog | fail-fast |
| 空 test 集 | record 池为空 | fail-fast |

---

### 02-II-4 Canonical Anchor Record

> **⚠ PENDING DAR Phase A**：下方 `financial/1001` 取自 BIRD 真实库 `financial`（test-only 集内），跨卷逐字节一致（Gate 3）；异构信号（稀疏 `loan` 682/4500、多态 `trans`）实测，但 account 反范式化布局、gold MQL 与 `world_signature`（确定性占位）**尚未经 DAR Phase A 在 MongoDB 上构造 + 执行验证**（见 [03 §03-6-4](./03_dataworld_construction.md#03-6-4)）。

<!-- canonical-anchor: financial/1001 -->
```json
{
  "record_id": 1001,
  "db_id": "financial",
  "nl_queries": {
    "canonical": "为每个 account 附加一个字段 loan_to_credit_ratio:若该 account 有 loan,取 loan.amount 除以该 account 所有贷记交易(trans.type = 'PRIJEM')的 amount 之和(该和为 0 时按 1 计);若该 account 无 loan,则该字段为 0。保留每个 account 文档(含无 loan 的),只在原文档上新增该字段,不改变文档数与嵌套结构;不要求排序。",
    "colloquial": "给每个账户标注它的贷款相对贷记流水的占比;没有贷款的账户记 0,一个账户都别漏。"
  },
  "MQL": "db.account.aggregate([
  { $lookup: {
      from: \"trans\",
      let: { aid: \"$_id\" },
      pipeline: [
        { $match: { $expr: { $and: [ { $eq: [\"$account_id\", \"$$aid\"] }, { $eq: [\"$type\", \"PRIJEM\"] } ] } } },
        { $group: { _id: null, credit_sum: { $sum: \"$amount\" } } }
      ],
      as: \"_credit\"
  } },
  { $addFields: {
      loan_to_credit_ratio: {
        $cond: [
          { $ne: [ { $type: \"$loan\" }, \"missing\" ] },
          { $divide: [ \"$loan.amount\", { $max: [ { $ifNull: [ { $arrayElemAt: [\"$_credit.credit_sum\", 0] }, 0 ] }, 1 ] } ] },
          0
        ]
      }
  } },
  { $project: { _credit: 0 } }
])",
  "canonical_form_set": {
    "must_contain": ["$lookup"],
    "must_not_contain": ["$sample", "$rand", "$$NOW", "$out", "$merge", "$function"],
    "must_contain_at_root": [],
    "must_not_contain_at_root": ["$unwind", "$group"]
  },
  "difficulty": "L4",
  "sql_infeasibility_class": "structural_schema_flex",
  "shape_policy": "preserve",
  "world_signature": "sha256:58d575b0eb62b1499642ec46e9efe5d5576082ce45d871df0326821f44751346",
  "agent_design_rationale_ref": "fixtures/financial/sra.yaml",
  "mutations_ref": "fixtures/financial/mutations.json"
}
```

---

### 02-II-5 JSON Schema 索引

| 文件 | 校验对象 |
|---|---|
| `schemas/record.schema.json` | 单条 record |
| `schemas/record.schema.valid.json` | valid 示例（financial/1001） |
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
