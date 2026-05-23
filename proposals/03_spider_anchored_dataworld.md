# 03 · Spider-Anchored DataWorld (v2-Agent)

> 本文件是 TEND v2-Agent **Spider 锚定数据世界** 的单一真源。
> 它定义：Spider 1.0 workload 如何驱动 MongoDB schema 设计；WP / SRA / SC / DM 四 Agent 的角色与契约；11 种官方 design pattern 与 3 种 anti-pattern；自然涌现的数据现象与 audit-only detector 边界。
> 它**不定义**：record 发布格式 ([02](./02_dataset_design.md))、QRA / NNC / RA 查询构造 ([04](./04_agent_framework.md))、评测协议 ([05](./05_evaluation_methodology.md))。

---

## Part I

## TL;DR

TEND v2-Agent 的 DataWorld 不再由 105 份手写 domain template 正向合成，而以 **Spider 1.0** 约 200 个 SQLite 数据库为唯一 schema / 数据 / workload 锚点。对每个入选 `db_id`，四 Agent 子流水线 **WP → SRA → SC → DM** 产出库级三元组：MongoDB schema、冻结 witness 数据、SRA 设计 rationale。查询与 NLQ 在 Phase B 由 QRA 消费此世界，03 只生产、不消费。

**WP (Workload Profiler)** 从 Spider 的 NL + SQL 对中提取访问模式：join 路径、共现实体、聚合深度、热字段与嵌套遍历需求。**SRA (Schema Re-architect)** 在 WP 信号与 MongoDB 官方 11 pattern 菜单下，将关系 schema 重设计为 workload-driven 文档布局。**SC (Schema Critic)** 对抗性审查 SRA 输出：anti-pattern 命中、workload 覆盖缺口、referential 完整性风险。**DM (Data Migrator)** 按 SRA 映射将 Spider 行级实例迁入 MongoDB 集合，写入行级 migration log 并计算 `world_signature`。

设计立场从 v2-original 的「世界先于问题」保留为「**Spider workload 先于 MongoDB layout**」：layout 必须解释该库全部 Spider 查询的可执行性，但 03 不为任何单条未来 MQL 定制 schema。v2-original 的 Phenomena Planter 15 类主动注入已删除；**数据现象自然涌现**于迁移后的 witness（稀疏字段、类型漂移、outlier、cardinality 边界等），audit 层 detector 仅扫描登记，不做 minimal perturbation plant。

MongoDB 设计采用 **11 官方 pattern + 3 anti-pattern** 菜单（见 §03-2）。Pattern 包括 embedded、extended reference、polymorphic、attribute、bucket、computed、subset、tree、outlier、schema versioning、mixed。Anti-pattern 包括 unnecessary collections、excessive lookups、over-indexing——SC 与 publish gate 必须拒绝命中项。

Canonical anchor `orchestra/1001` 展示典型路径：Spider 四表（conductor / orchestra / performance / show）→ SRA 三层 embed（performance[] ⊂ orchestra[] ⊂ conductor）→ DM 迁移 → L4 MQL 依赖 `$setWindowFields` + `$facet` + `$ifNull` 于嵌套 Attendance 路径。库级资产路径与 record 契约见 [02 §2](./02_dataset_design.md#02-2)；Agent prompt 与 JSON Schema 见本卷 Part II。

---

<a id="03-0"></a>
## §03-0 摘要与立场

**本章定位**：TEND 构造流水线的 Phase A（DataWorld），职责是在 QRA 出现之前产出可消费的二元组：

$$
\mathcal{W} \;=\; (\text{Schema},\ \text{WitnessData})
$$

外加 per-db **agent_design_rationale**（SRA 决策链）。v2-original 第三分量 PhenomenaRegistry 降为 **audit-only 自然现象扫描**，不再作为 Tier-1 发布物。

**立场声明 (Spider-anchored stance)**：

1. **Spider workload 先于 layout**。Schema 设计以该 `db_id` 全部 Spider NL/SQL 对的统计访问模式为输入，而非以单条待生成 MQL 为条件。
2. **DataWorld 是自足的**。即便从未生成任何 record，schema + witness + rationale 本身应能支撑该库 workload 的可执行性论证。
3. **查询是消费者，不是生产者**。03 不定义 canonical_form_set、不产出 MQL、不进行 NNC / dual-bridge 裁决。这些归属 [04](./04_agent_framework.md)。

**与 v2-original 的对照**

| v2-original | v2-Agent 处置 |
|---|---|
| Domain Template Bank (105 domain) | 删除 → Spider 200 DB |
| Schema Composer + F_topology 7 特性 | 替换 → WP workload + SRA 11 pattern |
| Witness Data Generator + 6×36 noise | 替换 → DM 关系迁移 + ≤8 可选 stress test |
| Phenomena Planter 主动注入 | 删除 → 自然涌现 + audit detector |
| phenomena_registry (Tier-1) | 删除 → audit 可选 |

---

<a id="03-1"></a>
## §03-1 架构与数据流

### §03-1-1 整体架构

Phase A 以 **Profile → Design → Critique → Migrate** 四拍推进：

1. **WP** 读取 Spider catalog 条目、SQLite schema、NL/SQL 对，输出 workload profile（`wp_output.yaml`）。
2. **SRA** 读取 WP 输出 + Spider DDL，应用 11 pattern 菜单，输出 MongoDB schema + `agent_design_rationale/<db_id>.yaml`。
3. **SC** 对抗性审查 SRA 输出；命中 anti-pattern 或 workload 覆盖缺口则 **reject → SRA 修订**（最多 2 轮）。
4. **DM** 按 SRA 映射迁移 Spider 行级数据 → `mongodb_data/<db_id>.json`；写入 `audit/<db_id>/migration_log.json`；计算 `world_signature`。
5. **Audit detectors**（非 Agent）扫描 witness，登记自然涌现现象至 `audit/<db_id>/phenomena_audit.json`（Tier-2，可选）。

下游 QRA / NNC / RA 在 Phase B 消费 Tier-1 库级资产；03 不回流。

### §03-1-2 单库主路径（以 orchestra 为例）

1. **Catalog 选中**：`spider_db_catalog.json` 中 `db_id = orchestra`，`selected = true`。
2. **WP 剖析 workload**：四表 join 链 conductor → orchestra → performance → show；62% 查询触及 performance/show 度量；38% 需 per-entity 序列聚合。
3. **SRA 设计 layout**：单集合 `conductor` 根文档；embed `orchestra[]`；embed `performance[]`（Attendance 来自 show 表 denormalize）；pattern 主标签 `embed` + `mixed`（show 字段折叠）。
4. **SC 审查**：确认无 unnecessary collection（仅 1 个 Tier-1 集合）；$lookup 深度预算 0（embed 消解）；workload 热路径覆盖。
5. **DM 迁移**：12 行 conductor → 12 文档；嵌套 orchestra / performance 数组；migration log 逐行可追溯。
6. **发布**：`mongodb_schema/orchestra.json`、`mongodb_data/orchestra.json`、`agent_design_rationale/orchestra.yaml` 写入 Tier-1。

### §03-1-3 确定性与可复现性

给定同一 Spider SQLite 快照、同一 Agent prompt 版本、同一 `--seed`，DM 输出 bit-by-bit 可复现；`world_signature = sha256(JCS(mongodb_data/<db_id>.json))` 完全相同。SRA 设计 rationale 须记录 seed 与 prompt 版本供 audit 重放。

---

<a id="03-2"></a>
## §03-2 MongoDB Design Pattern 菜单

SRA 只允许从下列 **11 官方 pattern** 组合选型；`patterns_applied[0]` 写入 record 的 `schema_pattern` 字段（五轴覆盖轴，见 [02 §4](./02_dataset_design.md#02-4)）。

| # | Pattern ID | 含义 | 典型触发（WP 信号） |
|---|---|---|---|
| 1 | `embed` | 1:N 子文档嵌入父文档数组/对象 | 共现率 ≥ 0.7；子实体无独立查询 |
| 2 | `extended_reference` | 冗余常用字段到引用侧，避免二次 fetch | 热字段跨集合重复读取 |
| 3 | `polymorphic` | 单集合多形状，以判别字段区分 | Spider 多表同构子类型 |
| 4 | `attribute` | 稀疏列折叠为键值或嵌套属性包 | 宽表大量 NULL 列 |
| 5 | `bucket` | 按时间/哈希分桶子文档，控制文档大小 | 高基数时间序列或日志 |
| 6 | `computed` | 预计算派生字段服务热聚合 | WP 标记重复 aggregate 表达式 |
| 7 | `subset` | 仅嵌入满足谓词的子集 | 查询始终带同一 filter |
| 8 | `tree` | 物化路径 / 父指针表达层级 | Spider 自引用 FK 或 closure 表 |
| 9 | `outlier` | 极端值单独子文档或标记字段 | 分布长尾；稳健统计查询 |
| 10 | `schema_versioning` | 文档带 schema 版本号，多代字段共存 | Spider 历史列并存 |
| 11 | `mixed` | 同一实体部分 embed、部分 reference | WP 共现率分裂（0.3–0.7） |

**3 Anti-Pattern（SC 必须拒绝）**

| Anti-Pattern ID | 定义 | 检测摘要 |
|---|---|---|
| `unnecessary_collections` | 存在从不被 workload 独立访问、且可被 embed/reference 合并的集合 | 集合零独立 query hit 且行数 < 父实体 10× |
| `excessive_lookups` | 代表 MQL 预估 $lookup 链深度 > WP 建议 join_depth p95 + 1 | SRA 布局迫使 >2 次 $lookup 才能覆盖 50% workload |
| `over_indexing` | 索引数 > 集合数 × 3 且无 workload 谓词支撑 | 索引字段未出现在 WP hot_fields top-20 |

Pattern 选取须写进 `agent_design_rationale` 的 `decisions[]`，每条含 `id`（D01…）、`type`、`parent/child`、`rationale`、引用的 WP `pattern_id` 或 query 证据。

---

<a id="03-3"></a>
## §03-3 Spider 锚定 Schema 设计（SRA）

SRA 输入：WP `wp_output.yaml`、Spider `tables.json` / `columns.json`、可选 SQLite 采样行。输出：`mongodb_schema/<db_id>.json` 与 `agent_design_rationale/<db_id>.yaml`（schema 见 `schemas/agent_design_rationale.schema.json`）。

**设计规则**

1. **Workload coverage**：WP 标记的 top-10 access pattern 必须能在 SRA layout 上表达为 ≤2 次 `$unwind` 或可避免的 `$lookup`。
2. **Referential sanity**：每个 Spider FK 在 MongoDB 中必须有 embed 路径或 `_id` 引用目标；DM 不得产生 dangling ref。
3. **Document size budget**：单文档 BSON < 16 MB；超限时 SRA 须切换 bucket 或 reference，并在 rationale 中记录。
4. **Primary pattern 标签**：`patterns_applied[0]` 取对 workload 覆盖贡献最大的 pattern；其余模式写入 `patterns_applied[1:]`。
5. **No phenomena planting**：SRA 不为「未来现象」人工注入 outlier / null cluster；稀疏与异常来自 Spider 源数据与 DM 保真迁移。

**Canonical orchestra 决策摘要**

- D01 embed：orchestra[] ⊂ conductor（WP AP02 co_access 0.91）
- D02 embed：performance[] ⊂ orchestra（WP AP01 nested_traversal 0.62）
- D03 extended_reference：Attendance 自 show 表 denormalize 至 performance（WP hot_field show.Attendance）
- patterns_applied: `[embed, mixed]`

---

<a id="03-4"></a>
## §03-4 数据迁移（DM）与自然现象

DM 输入：SRA schema + rationale、Spider SQLite。输出：`mongodb_data/<db_id>.json`、`audit/<db_id>/migration_log.json`、`world_signature`。

**迁移原则**

1. **Lossless at workload grain**：WP 热路径涉及的列必须可追溯到 witness 字段路径。
2. **PK 稳定**：Spider 主键映射为 MongoDB `_id` 或业务键字段，全库一致。
3. **Null as missing**：Spider NULL → 字段缺失（非 JSON null），保留 empty-vs-missing 自然现象。
4. **Append-only audit**：migration log 仅追加条目，不修改已发布 witness。

**自然涌现现象（audit-only detectors）**

v2-original 15 类 phenomenon 缩减为 **audit detector 扫描**，不写入 Tier-1。Detector 在 DM 完成后只读 witness，典型登记类包括：

| _detector_ | 自然来源 | 用途 |
|---|---|---|
| sparse_field | 源库 NULL / 缺失列 | RA / QRA null-coalesce 提示 |
| type_drift | Spider 类型不一致 | 鲁棒解析题 |
| outlier_value | 源库极端值或录入错误 | 稳健统计题 |
| cardinality_boundary | embed 数组长度 0/1 | 组大小敏感聚合 |
| empty_vs_missing | DM null-as-missing 策略 | $ifNull / 存在性题 |

Detector **禁止**修改 witness；若 RA 后续 augment，须 append-only 且重算 `world_signature`（边界见 [02 §2](./02_dataset_design.md#02-2)）。

---

<a id="03-5"></a>
## §03-5 与下游的接口契约

### §03-5-1 Tier-1 输出资产

| 资产 | 路径 | 消费者 |
|---|---|---|
| MongoDB schema | `mongodb_schema/<db_id>.json` | QRA、solver、评测 |
| Witness data | `mongodb_data/<db_id>.json` | NormExec、RA |
| Design rationale | `agent_design_rationale/<db_id>.yaml` | 研究者、NNC 诊断 |
| WP profile | `audit/<db_id>/wp_output.yaml` | 复现（Tier-2） |

### §03-5-2 03 不负责的内容

| 主题 | 归属 |
|---|---|
| record 字段、train/test 切分 | [02](./02_dataset_design.md) |
| MQL、canonical_form_set、mutations | [04](./04_agent_framework.md) |
| EX、七指标 | [05](./05_evaluation_methodology.md) |
| SMART solver | [06](./06_solution_design.md) |

---

## Part II

### §03-II-1 Agent 契约总览

| Agent | 输入 | 输出 | 边界（禁止） |
|---|---|---|---|
| **WP** | `db_id`, Spider SQLite, NL/SQL pairs, catalog metadata | `audit/<db_id>/wp_output.yaml` | 不得输出 MongoDB schema 或 MQL |
| **SRA** | WP output, Spider DDL, pattern menu | `mongodb_schema/<db_id>.json`, `agent_design_rationale/<db_id>.yaml` | 不得读 QRA/MQL；不得 plant 现象 |
| **SC** | SRA schema + rationale, WP output, anti-pattern rules | `pass/reject` verdict + `issues[]` | 不得重写 witness；不得产出最终 schema 文件 |
| **DM** | SRA schema + rationale, Spider SQLite | `mongodb_data/<db_id>.json`, `migration_log.json`, `world_signature` | 不得改 schema 字段集；不得 delete 源映射行 |

Prompt 文件：`agent_prompts/wp_workload_profiler.md`、`sra_schema_rearchitect.md`、`sc_schema_critic.md`、`dm_data_migrator.md`。

---

### §03-II-2 WP Agent 契约

**Input**

| 字段 | 来源 | 必需 |
|---|---|---|
| `db_id` | catalog | ✓ |
| `sqlite_path` | catalog | ✓ |
| `spider_queries` | train_spider.json + dev.json 过滤 | ✓ |
| `tables` / `columns` | Spider schema JSON | ✓ |

**Output**（`schemas/wp_output.schema.json`）

| 字段 | 说明 |
|---|---|
| `db_id`, `spider_version`, `generated_at` | 元数据 |
| `source.tables`, `source.query_count` | 源库摘要 |
| `access_patterns[]` | join 路径、频率、示例 query_id、NL hint |
| `hot_fields[]` | 字段级访问计数 |
| `co_location_signals[]` | embed 候选对的共现率 |
| `join_depth_distribution` | 驱动 $lookup 预算 |
| `design_constraints[]` | 传给 SRA 的硬约束句子 |

**Boundaries**

- 不猜测 MongoDB layout；不输出 pattern 选型。
- 频率统计对同一 SQL 模板去重后计数。
- 若 `query_count < 10`，输出 `insufficient_workload: true` 并建议 catalog reject。

---

### §03-II-3 SRA Agent 契约

**Input**

| 字段 | 来源 | 必需 |
|---|---|---|
| `wp_output.yaml` | WP | ✓ |
| `tables` / `columns` | Spider | ✓ |
| `pattern_menu` | 本卷 §03-2 | ✓ |

**Output**

| 文件 | Schema |
|---|---|
| `mongodb_schema/<db_id>.json` | `schemas/library.schema.json#mongodb_schema` |
| `agent_design_rationale/<db_id>.yaml` | `schemas/agent_design_rationale.schema.json` |

**Boundaries**

- 每个 `decisions[]` 必须引用 ≥1 条 WP 证据（`access_pattern` id 或 `hot_field` path）。
- 不得创建 anti-pattern 命中布局。
- SC reject 后仅修订 rationale 与 schema，不得调用 DM。

---

### §03-II-4 SC Agent 契约

**Input**

| 字段 | 来源 | 必需 |
|---|---|---|
| SRA schema + rationale | SRA | ✓ |
| `wp_output.yaml` | WP | ✓ |
| `anti_pattern_rules` | §03-II-5 | ✓ |

**Output**

| 字段 | 说明 |
|---|---|
| `verdict` | `pass` \| `reject` |
| `issues[]` | `{rule_id, severity, message, evidence}` |
| `coverage_gaps[]` | WP pattern 无 layout 表达 |
| `suggested_fixes[]` | 自然语言修订建议（非自动 patch） |

**Boundaries**

- 最多 2 轮 reject；仍 fail 则 catalog 标记 `schema_review_failed`。
- 不写入 Tier-1 文件；人类或 SRA 编排器应用 fixes。

---

<a id="03-ii-5"></a>
### §03-II-5 Anti-Pattern Detector 规则

| Rule ID | 条件 | Severity | 动作 |
|---|---|---|---|
| AP-UC-01 | 集合 `C` 满足：`independent_query_hits(C) = 0` AND `rows(C) < 10 × rows(parent(C))` AND `C` 非 polymorphic 判别集合 | error | reject |
| AP-UC-02 | Tier-1 集合数 > WP `access_patterns` 中 distinct root entity 数 + 1 | error | reject |
| AP-EL-01 | 预估覆盖 50% workload 所需 `$lookup` 次数 > `floor(join_depth_p95) + 1` | error | reject |
| AP-EL-02 | 同一查询路径需链式 `$lookup` ≥ 3 且无 bucket 豁免 | warning | reject if ≥2 warnings |
| AP-OI-01 | 索引字段 ∉ WP `hot_fields` top-20 且非 `_id`/FK | warning | reject if warnings ≥ 3 |
| AP-OI-02 | 索引数 > 3 × 集合数 | error | reject |
| AP-WC-01 | WP top-5 `access_patterns` 中任一无法在 SRA layout 表达（SC 仿真 unwind/lookup） | error | reject |
| AP-WC-02 | WP `design_constraints` 任一条无对应 `decisions[]` 引用 | error | reject |

**Workload coverage 仿真**：SC 对每个 access pattern 做静态路径解析——若需 >2 `$unwind` 或 >1 `$lookup` 且 SRA 未声明 `mixed`/`extended_reference` 豁免，则记 AP-WC-01。

---

### §03-II-6 DM Agent 契约

**Input**

| 字段 | 来源 | 必需 |
|---|---|---|
| `mongodb_schema/<db_id>.json` | SRA | ✓ |
| `agent_design_rationale/<db_id>.yaml` | SRA | ✓ |
| Spider SQLite | catalog | ✓ |

**Output**

| 文件 | Schema |
|---|---|
| `mongodb_data/<db_id>.json` | `schemas/library.schema.json#mongodb_data` |
| `audit/<db_id>/migration_log.json` | `schemas/migration_log.schema.json` |
| `world_signature` | `sha256:` + 64 hex |

**Boundaries**

- 不得增删 schema 声明字段；仅填充值。
- 每条 source row 至少一条 migration log entry。
- FK 违反写入 `integrity_checks.orphan_refs`；>0 则 DM fail。

---

### §03-II-7 Migration Log Schema 参考

完整 JSON Schema：`schemas/migration_log.schema.json`。

**顶层字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `db_id` | string | Spider db_id |
| `generated_at` | date-time | 迁移完成时间 |
| `source_sqlite` | string | 源 SQLite 路径 |
| `target_collections` | string[] | 产出集合列表 |
| `stats` | object | `source_rows`, `target_documents`, `tables_migrated` |
| `entries` | array | 行级映射（见下） |
| `integrity_checks` | object | `referential_pass`, `row_count_reconciled`, `orphan_refs` |

**Entry 字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `entry_id` | string | `M` + 序号，如 `M0001` |
| `source_table` | string | Spider 表名 |
| `source_pk` | string | 主键字符串化 |
| `target_collection` | string | MongoDB 集合 |
| `target_id` | string \| number | 目标 `_id` |
| `operation` | enum | `root_insert` / `embed_push` / `field_denorm` / `ref_link` |
| `target_path` | string | 点路径；根插入时为 null |
| `embedded_children` | string[] | 嵌套表名列表 |

**校验命令**

```bash
jsonschema --schema proposals/schemas/migration_log.schema.json \
  --instance proposals/schemas/migration_log.schema.valid.json

jsonschema --schema proposals/schemas/agent_design_rationale.schema.json \
  --instance proposals/schemas/agent_design_rationale.schema.valid.json

jsonschema --schema proposals/schemas/wp_output.schema.json \
  --instance proposals/audit/orchestra/wp_output.yaml
# WP YAML 实例需先 yq -o=json 转换
```

---

### §03-II-8 JSON Schema 索引

| 文件 | 校验对象 |
|---|---|
| `schemas/wp_output.schema.json` | WP Agent 输出 |
| `schemas/agent_design_rationale.schema.json` | SRA rationale YAML |
| `schemas/agent_design_rationale.schema.valid.json` | valid 示例 |
| `schemas/agent_design_rationale.schema.invalid.json` | invalid 示例 |
| `schemas/migration_log.schema.json` | DM migration log |
| `schemas/migration_log.schema.valid.json` | valid 示例 |
| `schemas/migration_log.schema.invalid.json` | invalid 示例 |

---

### §03-II-9 Canonical Anchor Record

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

Spider 四表 DDL 见 [phase0_spider_verify_report.md](./phase0_spider_verify_report.md)。