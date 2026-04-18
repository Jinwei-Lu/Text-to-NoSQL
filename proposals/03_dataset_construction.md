# 03 数据集构造方法

<a id="03-0"></a>

## §0 摘要

本文档定义 TEND 的**完整构造流水线**，从 Spider SQLite 出发，经由 NoSQL-native schema 重塑、世界物化、idiomatic MQL 重写、5 条 NLQ 重写，最终落到**逆向工程证书（RE 证书 / instance 正确性证书）**的四问准入，再写入 [02 数据集设计](./02_dataset_design.md) 所定义的资产路径。

本文档要解决的是 TEND 当前实现中的四个真实瓶颈：

1. MongoDB schema 是关系表的浅嵌套，不是 NoSQL native 文档建模；
2. gold MQL 经常带 SQL 转换器的翻译痕迹（如 `localField` 与 `foreignField` 同时写成 `employees.EMPLOYEE_ID` 这种双前缀错误，或者 `Docs1.X` 这种翻译器命名）；
3. NLQ 是从 SQL 回译来的，与文档建模脱节，看不出嵌套结构；
4. 没有"这个 MQL 在这个 world 上是该 NLQ 的唯一正确解"的证书，导致 Q3 失败时（near-miss MQL 跑出同样结果）我们无法判别。

本文档以 `orchestra` 库 + `"List the top 3 conductors with the most performances."` 作为 canonical 例子，端到端贯穿全文（见 [§9](#03-9)）。

**本文档的 SSoT 边界**：本文档**只**定义"如何把一条 record 造出来"，**不**定义 record 字段名（属于 [02](./02_dataset_design.md)）、不定义任务正确性 ≡_rec 的归一化规则（属于 [01 任务定义](./01_task_definition.md)）、不定义评测指标、不定义方法架构。

---

<a id="03-1"></a>

## §1 总流水线概览

每条候选 record 都按下列六段流水线生成。前五段是构造，第六段是准入：

```
Spider SQLite (db, ref_sql)
        │
        ▼
[§2] NoSQL-native Schema Rewriting          → TEND/mongodb_schema/<db_id>.json
        │
        ▼
[§3] World Materialization (deterministic)  → TEND/mongodb_data/<db_id>.json
        │
        ▼
[§4] Idiomatic MQL Rewriting                 → record.MQL
        │
        ▼
[§5] NLQ Rewriting (5 条覆盖约束)            → record.nl_queries[5]
        │
        ▼
[§6] 逆向工程证书 (Q1 ~ Q4)                  → audit/<db_id>/<record_id>/certificate.json
        │
        ▼
[§7] 路由：通过 → train.json / test.json；未通过 → audit/rejected/
```

每段的输入、输出、契约、失败处理在后续章节展开。完整伪码见 [§8](#03-8)。

| 阶段 | 输入 | 输出 | 失败动作 |
|---|---|---|---|
| §2 schema | Spider SQLite | NoSQL-native schema | 该 db 重塑失败 → 整库降级或人工介入 |
| §3 world | schema + Spider 数据 | `TEND/mongodb_data/<db_id>.json` + signature | 判别压力不足 → 调整注入参数后重试 |
| §4 MQL | ref_sql + schema + world | idiomatic MQL | 在 world 上结果与 ref_sql 不一致 → 重写 MQL |
| §5 NLQ | ref_sql + schema + MQL | 5 条 nl_queries | translator consensus 不通过 → 改写或丢弃 |
| §6 证书 | record candidate + world | certificate.json (Q1~Q4 状态) | 任一 Q 不通过 → 路由到 rejected |
| §7 路由 | candidate + 证书 | train.json / test.json / rejected/ | — |

---

<a id="03-2"></a>

## §2 NoSQL-native Schema Rewriting

### §2.1 目标

把 Spider 关系 schema 重写成**符合 MongoDB 文档建模惯例**的 schema，写到 `TEND/mongodb_schema/<db_id>.json`（路径由 [02](./02_dataset_design.md) 定义）。重写规则不是机械转表，而是按"查询负载 + 选择性"做建模决策。

### §2.2 主实体（root entity）选择

按外键拓扑构建有向图，节点是表，边是外键引用。选择 root 的优先级：

1. 入度最大的表通常是被多对多引用的字典表（如 `country`），不适合作 root；
2. 出度（含被引用方向）反映"以谁为主语展开"，出度最大的表优先；
3. 在 1:N 链中，N 端通常嵌入 1 端，因此 1 端是 root；
4. 同分上多个候选时，按业务语义选（如 `orchestra` 库中 conductor 拥有 orchestra，orchestra 又拥有 performance，root = `conductor`）。

`orchestra` 库的 root 选 `conductor`，得到嵌入链 `conductor → orchestra[] → performance[] → show[]`，与现有 `TEND/mongodb_schema/orchestra.json` 一致。

### §2.3 关系到嵌入的映射规则

| 关系 | 默认策略 | 例外 |
|---|---|---|
| 1:1 | 嵌入子文档 | 子表字段过多（>30）或频繁独立查询 → 保留为引用 |
| 1:N（N 平均小、查询模式以 parent 为主）| N 嵌入 parent 数组 | N 大且独立查询频繁 → 引用 + lookup |
| 1:N（N 大、独立查询多）| 引用 + lookup | — |
| M:N | 保留两个集合 + 中间引用 | 中间表带额外属性时，中间表保留 |

**重点**：嵌入不是无脑全部下沉。决策依据是 Spider 中该子表的"被独立 SELECT"频率与 join 频率，统计自该 db 下的全部 ref_sql 集合。

### §2.4 NoSQL-native 特性的可控注入

以下五类特性是 MongoDB 实际部署中常见、但 SQL 转换器不会自然产生的现象。注入时严格控制比率与位置，且**不破坏 ref_sql 的语义可达性**。

| 特性 | 操作化定义 | 默认注入比率 |
|---|---|---|
| **polymorphism** | 同一 collection 中存在结构不同的子集（按 discriminator 字段区分） | 10% – 30% 的 collection 受影响 |
| **sparsity** | 字段在部分文档中物理缺失（不是 null） | 字段缺失率落在 [10%, 60%] |
| **type drift** | 同一字段在不同文档中类型不同（如年份字段同时出现 int 与 string） | 整库 1 – 3 个字段受影响 |
| **embedding depth** | 嵌套层数（root 算第 1 层） | 由 §2.2 拓扑自然决定，多数库为 2 – 4 层 |
| **dynamic key** | 用文档键名编码 tenant 标识或枚举值 | 仅在少量库（<10%）适用 |

`orchestra` 库的注入示例（贯穿后续章节）：

| 特性 | 在 orchestra 库的具体注入 |
|---|---|
| polymorphism | `performance` 子文档增加 `Type ∈ {"live", "recorded"}`，两类各自可有不同补充字段 |
| sparsity | `Year_of_Work` 在 conductor 文档中以 ~30% 概率缺失 |
| type drift | `Year_of_Founded` 在 30% 文档中是 string（如 "1985"），其余是 int |
| embedding depth | 4 层（conductor → orchestra → performance → show） |
| dynamic key | 不注入（库规模小，注入会污染 join 路径） |

这些注入参数是**设计参数**，不是硬编码常量；为每个库单独标定。

### §2.5 schema_complexity_profile 可计算分量

构造侧负责把下列分量算出，写入 [02](./02_dataset_design.md) 在 record 上定义的 `schema_complexity_profile` 字段：

| 分量 | 定义 |
|---|---|
| `normalized_ratio` | 重塑后保留为独立 collection 的表数 / Spider 原表数 |
| `max_embed_depth` | 该库 schema 树的最大嵌套层数 |
| `polymorphism_rate` | 受 polymorphism 影响的 collection 占比 |
| `sparsity_rate` | 受 sparsity 影响的字段平均缺失率 |
| `type_drift_count` | 受 type drift 影响的字段数 |
| `dynamic_key_count` | 使用 dynamic key 的字段数 |
| `cross_collection_ref_count` | 跨 collection 的引用边数（即未嵌入而保留为 lookup 的关系数） |

字段名定义在 [02](./02_dataset_design.md)；本文档只承担**值的来源**。

---

<a id="03-3"></a>

## §3 World Materialization

### §3.1 目标

把 Spider 原 SQLite 数据按重塑后的 schema 物化为一份 MongoDB 数据快照，写到 `TEND/mongodb_data/<db_id>.json`，并产出 `world_signature` 写入 record。世界必须对查询有判别力，不能是平凡世界。

### §3.2 物化规则

1. **复用优先**：所有原始字段直接复用 SQLite 数据，按 [§2.3](#03-2) 的嵌入路径重组；
2. **新增字段确定性填充**：因 polymorphism / type drift 引入的新字段，按确定性规则（基于 row id 与 seed 的哈希）赋值；
3. **缺失注入**：受 sparsity 影响的字段，按 row id 与 seed 的哈希决定是否物理删除该字段；
4. **顺序稳定**：collection 内文档按 `_id` 排序，键序按 schema 顺序固定。

### §3.3 deterministic seed

整个物化过程接收 `(db_id, seed)` 二元组，**同一 seed 重跑产生字节级一致的快照**。这是 `world_signature` 可哈希、可复现的前提。

### §3.4 判别压力约束

世界必须满足下列硬约束，否则不构成有判别力的物化（具体阈值依库规模微调）：

| 约束 | 含义 | 默认阈值 |
|---|---|---|
| **边界数据点** | 对每个出现在 ref_sql 中的数值/时间字段，至少存在若干文档落在 gold predicate 边界的 ε 邻域内 | ≥ 1 个 |
| **稀疏分布健康** | 受 sparsity 影响字段的实际缺失率 | 落在 [10%, 90%] |
| **group cardinality** | 对 gold MQL 涉及的 group key，分组数 | ≥ k_group（默认 3） |
| **过滤后行数非退化** | gold MQL 在 world 上跑出的中间结果行数 | ≥ k_rows（默认 1，且不为"几乎全表"） |
| **输出非退化** | gold result | 非空，且行数 < 总行数 95% |

不满足时，回到 [§3.2](#03-2) 调整数据补充策略（如插入额外的边界点、重平衡分布），重新物化。

### §3.5 world_signature 计算

```
canonical_world := { collection_name : sorted_by_id([ canonical_json(doc) for doc in collection ]) }
world_signature := "sha256:" + hex( SHA256( canonical_json(canonical_world) ) )
```

其中 `canonical_json` 指 RFC 8785 风格的稳定序列化（键排序、固定数值精度、无可选空白）。该值写入 record 的 `world_signature` 字段（字段名定义见 [02](./02_dataset_design.md)）。

---

<a id="03-4"></a>

## §4 Idiomatic MQL Rewriting

### §4.1 不复用机械转换器输出

SQL→MongoDB 机械转换器的翻译痕迹（如 `Docs1.X` 命名、`localField` 与 `foreignField` 同时写成 `employees.EMPLOYEE_ID` 的双前缀错误）必须在本阶段清除。本阶段**不直接采用** SQL 转换器输出，而是把它当作"**起点提示**"，由人 + LLM 协作重写。

### §4.2 重写流程

1. 取该 db 下的 `ref_sql`；
2. 在新 schema 上重写 idiomatic MQL，**最大化使用 MongoDB-native 算子**；
3. 修复转换器残留 bug：双前缀字段引用、错误的 `from` 集合、`Docs1`/`Docs2` 之类的非语义命名；
4. 在 [§3](#03-3) 物化好的 world 上用 `mongosh` 执行新 MQL，得到结果 `R_mql`；
5. 在原 SQLite 上执行 `ref_sql`，得到结果 `R_sql`；
6. 按 [01 任务定义](./01_task_definition.md) 的归一化契约，要求 `R_mql ≡_rec R_sql`，否则回到第 2 步重写。

### §4.3 idiomatic 偏好清单

| 偏好 | 说明 |
|---|---|
| 优先 aggregation pipeline | 而非多次 find；除非确实只是简单 projection |
| 善用 `$unwind` / `$lookup with pipeline` / `$project` / `$group` | 而非把所有字段先抽到 root 再 group |
| 单 collection 查询尽量不用 `$lookup` | 嵌入字段直接走 `$unwind` |
| 输出键命名按 NLQ 自然名 | 如 `Name`、`performance_count`，而非 `Docs1.X` |
| `$lookup` 的 `localField` / `foreignField` 不带跨 collection 前缀 | `localField` 用本集合的字段路径，`foreignField` 用目标集合的字段路径 |
| 谓词放在能下推的地方 | `$match` 尽量前置；`$lookup` 内部用 pipeline 形式过滤 |

### §4.4 canonical 示例（orchestra）

ref_sql：

```sql
SELECT T1.Name, COUNT(*) AS performance_count
FROM conductor T1
JOIN orchestra T2 ON T1.Conductor_ID = T2.Conductor_ID
JOIN performance T3 ON T2.Orchestra_ID = T3.Orchestra_ID
GROUP BY T1.Conductor_ID, T1.Name
ORDER BY performance_count DESC
LIMIT 3;
```

idiomatic MQL（重写结果）：

```javascript
db.conductor.aggregate([
  { $unwind: "$orchestra" },
  { $unwind: "$orchestra.performance" },
  {
    $group: {
      _id: "$Conductor_ID",
      Name: { $first: "$Name" },
      performance_count: { $sum: 1 }
    }
  },
  { $sort: { performance_count: -1 } },
  { $limit: 3 },
  {
    $project: {
      _id: 0,
      Name: 1,
      performance_count: 1
    }
  }
]);
```

注意键名 `Name`、`performance_count` 来自 NLQ 自然语义，不是 `Docs1.Name`。两次 `$unwind` 沿嵌入路径展开，无需 `$lookup`，因为 schema 已经把 orchestra/performance 嵌入到 conductor。

---

<a id="03-5"></a>

## §5 NLQ Rewriting Reflecting MongoDB Structure

### §5.1 5 条 nl_queries 的覆盖约束

`nl_queries[0..4]` 不再是同义改写堆叠，而是覆盖五种"用户书写习惯"，且至少一条要让 MongoDB 嵌套结构能从问句中辨认出来。

| 槽位 | 风格 | 示例（基于 canonical NLQ） |
|---|---|---|
| `nl_queries[0]` | canonical：业务用户口吻、清晰、最短形式 | "List the top 3 conductors with the most performances." |
| `nl_queries[1]` | 暴露 MongoDB 嵌套结构（提及 conductor 的 orchestras 与其 performances） | "Across all conductors, count performances under each conductor's orchestras and return the top 3 conductors by total performances." |
| `nl_queries[2]` | NoSQL 习惯术语（document / embedded 等可提及；不直接抄算子名） | "For each conductor document, total the performances embedded under their orchestras and return the three conductors with the highest totals." |
| `nl_queries[3]` | 业务问句风格（"Which..." / "What..."） | "Which three conductors have led the most performances overall?" |
| `nl_queries[4]` | 自由表达 / multilingual（如中文版） | "列出指挥过演出最多的前三位指挥家。" |

约束：

- 五条都必须在 [§4](#03-4) 重写的 idiomatic MQL 上语义等价（在同一 world 上跑出相同结果）；
- 槽位 `[1]` 与 `[2]` 至少有一条能从问句中读出嵌套结构（提及 orchestra 与 performance 的归属关系），目的是给后续训练提供"NLQ 到嵌套路径"的可学习信号；
- 槽位 `[4]` 不强制中文，可以是任何与训练数据自然分布相符的语言；当库语义本身是中文/多语种时，多语种版本是自然候选。

### §5.2 translator consensus 校验

写完 5 条 NLQ 后，对**每一条**执行：

1. 派 ≥ 3 个独立 LLM（不同模型族，避免同质化），各自把该 NLQ 翻译成 SQL；
2. 把 3+ 条翻译结果与原 `ref_sql` 在原 SQLite 上执行；
3. 按 [01 任务定义](./01_task_definition.md) 的 ≡_rec 比较结果集；
4. 全部 ≡_rec → 该 NLQ 通过；任一不 ≡_rec → 该 NLQ 改写或丢弃；
5. 5 条全部通过后该 record 进入 [§6](#03-6) 证书阶段；不足 5 条则补写直到满足。

translator consensus 的目的是"NLQ 不带歧义"。后续的 candidate MQL consensus（[§6](#03-6) Q2）目的是"NLQ + world 锁定唯一答案"。两者方向不同：前者前向写 SQL，后者前向写 MQL。

---

<a id="03-6"></a>

## §6 逆向工程证书（核心创新）

<a id="03-6-overview"></a>

### §6.1 总览

本节是本文档的核心创新。每一条候选 record 在写入主集前必须通过四问机制，证书写到 `audit/<db_id>/<record_id>/certificate.json`，路径写入 record 的 `riv_certificate_ref` 字段（字段名定义见 [02](./02_dataset_design.md)）。

四问设计如下：

| 问 | 名称 | 检查目标 | 失败动作 |
|---|---|---|---|
| Q1 | 执行正确 | gold MQL 在新 world 上的结果 ≡_rec ref_sql 在原 SQLite 上的结果 | 重写 MQL 或重物化 world |
| Q2 | 语义唯一 | 由 ≥ 3 个独立 LLM 写出的 candidate MQL 集合 Q̂(NLQ)，每条在 new world 上的结果 == gold result | 改写 NLQ 或 reject |
| Q3 | 判别力 | 枚举 near-miss MQL 变异族，每条在 new world 上的结果 ≠ gold result | 改写 world 或 reject |
| Q4 | 世界非平凡 | world 对 gold MQL 提供非退化输入与输出 | 重物化 world |

注意：**RE 证书不是 MQL 验证器，而是 instance 正确性证书**——它判定的是"在这个 world 上，这条 (NLQ, MQL) 是唯一的、可执行的、可判别的、有意义的"，而不是"这条 MQL 在所有可能 world 上都正确"。

<a id="03-6-q1"></a>

### §6.2 Q1 执行正确（Execution Correctness）

```
R_mql := mongosh.run(gold_MQL, new_world)
R_sql := sqlite.run(ref_sql, original_sqlite)
assert canonicalize_recordset(R_mql) ≡_rec canonicalize_recordset(R_sql)
```

`canonicalize_recordset` 与 ≡_rec 的具体规则在 [01 任务定义](./01_task_definition.md) 中。Q1 失败的常见原因：

- MQL 语义偏移（漏一个 `$unwind`、用错聚合算子）→ 回到 [§4](#03-4) 重写；
- world 中相关字段类型漂移导致比较失败（如 type drift 把 `Year_of_Founded` 变成 string，而 ref_sql 用数值比较）→ 回到 [§3](#03-3) 调整注入；
- 归一化规则边界情况（数值精度、null 排序）→ 与 [01](./01_task_definition.md) 对齐后再判定。

<a id="03-6-q2"></a>

### §6.3 Q2 语义唯一（Semantic Uniqueness）

派 ≥ 3 个独立 LLM 各自针对 `nl_queries[0]` 写出 candidate MQL，构成集合 Q̂(NLQ)：

```
Q̂(NLQ) := { mql_i  for i in 1..N, N ≥ 3 }
forall mql_i ∈ Q̂(NLQ):
    R_i := mongosh.run(mql_i, new_world)
    assert canonicalize_recordset(R_i) ≡_rec canonicalize_recordset(R_gold)
```

证书记录：

| 字段 | 含义 |
|---|---|
| `candidate_count` | N，candidate MQL 总数 |
| `consensus_rate` | 与 gold result 一致的 candidate 比例 |
| `divergent_examples` | 不一致 candidate 的简短摘要（不入主集，仅供审计） |

判定：

- `consensus_rate == 1.0` → Q2 通过；
- `consensus_rate < 1.0` → 说明 NLQ 有歧义，或 gold MQL 选择了一种非默认解读 → 回到 [§5](#03-5) 改写 NLQ（让该解读变成唯一自然解读），或者整条 record reject。

> 例如 canonical NLQ "top 3 conductors with the most performances" 如果在某个 world 上有第 3 与第 4 名 tie，那么 candidate 之间会出现 `$limit 3` 截断的不同分支 → 这是 NLQ 与 world 的联合歧义，应改写 world（让 tie 消失）或改写 NLQ（让 tie-breaking 显式）。

<a id="03-6-q3"></a>

### §6.4 Q3 判别力（Discriminativity）

枚举 near-miss MQL 变异族，每条都应在 new world 上跑出**与 gold 不同**的结果。变异族划分如下：

| 变异族 | 变异示例 | 在 canonical 上的具体应用 |
|---|---|---|
| **aggregation_operator** | `$sum` ↔ `$avg` / `$count` / `$max` / `$min` | 把 `performance_count: { $sum: 1 }` 改成 `{ $avg: 1 }` |
| **sort_direction** | desc ↔ asc | `$sort: { performance_count: -1 }` 改成 `{ performance_count: 1 }` |
| **limit_truncation** | `$limit 3` → `$limit 5` / 删除 `$limit` | top 3 改成 top 5 / 全量 |
| **predicate_offset** | 边界值 ±1，`>=` ↔ `>`，`<=` ↔ `<` | 此条 NLQ 无显式谓词；通用情况下偏移 |
| **stage_drop** | 删除 `$unwind` / `$project` / `$sort` / `$match` 中关键阶段 | 删除第二个 `$unwind: "$orchestra.performance"` |
| **schema_link_swap** | 换字段为 schema 中同类型相近字段 | 把 group key 从 `Conductor_ID` 换成 `Orchestra_ID` |
| **join_path_swap** | 换 `foreignField` / `localField` / `from` 集合 | 此条 NLQ 无 `$lookup`；带 lookup 的 record 必检 |
| **null_missing_confusion** | 把 `$exists: true` 删除 / 改为 `$ne: null` | 用于 sparsity 字段相关的 record |

枚举规则：

- 每个 record 至少跑 8 个变异族中**适用**的全部条目，单族内可有多条变异；
- 总条数有上限（默认 ≤ 24），避免组合爆炸；
- 每条变异在 new world 上跑出结果 `R_mut_j`，要求 `canonicalize_recordset(R_mut_j) ≢_rec canonicalize_recordset(R_gold)`；
- 全部不同 → Q3 通过；任一相同 → world 区分度不够 → 回到 [§3](#03-3) 改写 world（如增加边界点、调整分布），或者整条 record reject。

证书记录：

| 字段 | 含义 |
|---|---|
| `near_miss_count` | 实际枚举的变异条数 |
| `all_different` | 是否全部 ≢_rec gold |
| `near_miss_summaries[]` | 每条变异的 `{family, mutation, result_diff_rows}` |

<a id="03-6-q4"></a>

### §6.5 Q4 世界非平凡（World Non-Triviality）

世界必须给 gold MQL 提供有意义的输入与输出，避免出现"空集 / 几乎全表 / 单组 / 退化边界"等平凡情况：

| 检查 | 判据 |
|---|---|
| `boundary_points_present` | 对 ref_sql 中所有数值/时间字段，边界 ε 邻域内有至少 1 个文档（与 [§3.4](#03-3) 一致） |
| `group_count` | gold MQL 的 group 阶段输出 ≥ 3 组（或该库特定阈值） |
| `filtered_rows` | gold MQL 中 `$match` 之后剩余行数 ≥ 1，且 < 总行数 95% |
| `output_non_trivial` | gold result 非空，且不等于"对 collection 的恒等 projection" |

任一不满足 → 回到 [§3](#03-3) 重物化 world；多次重试仍不满足 → reject。

<a id="03-6-cert"></a>

### §6.6 证书内容示例

写入 `audit/<db_id>/<record_id>/certificate.json`，示例（canonical orchestra record）：

```json
{
  "record_id": 99001,
  "db_id": "orchestra",
  "q1_execution": {
    "status": "pass",
    "compared_at": "2026-01-15T08:42:11Z"
  },
  "q2_uniqueness": {
    "status": "pass",
    "candidate_count": 5,
    "consensus_rate": 1.0,
    "divergent_examples": []
  },
  "q3_discrimination": {
    "status": "pass",
    "near_miss_count": 14,
    "all_different": true,
    "near_miss_summaries": [
      { "family": "aggregation_operator", "mutation": "$sum -> $avg",      "result_diff_rows": 3 },
      { "family": "sort_direction",       "mutation": "desc -> asc",        "result_diff_rows": 3 },
      { "family": "limit_truncation",     "mutation": "$limit 3 -> $limit 5","result_diff_rows": 2 },
      { "family": "stage_drop",           "mutation": "drop $unwind perf",  "result_diff_rows": 3 },
      { "family": "schema_link_swap",     "mutation": "group by Orchestra_ID", "result_diff_rows": 7 }
    ]
  },
  "q4_world_non_trivial": {
    "status": "pass",
    "group_count": 12,
    "filtered_rows": 47,
    "boundary_points_present": true,
    "output_non_trivial": true
  },
  "world_signature": "sha256:9c1f4a...",
  "schema_complexity_profile": {
    "normalized_ratio": 0.25,
    "max_embed_depth": 4,
    "polymorphism_rate": 0.25,
    "sparsity_rate": 0.30,
    "type_drift_count": 1,
    "dynamic_key_count": 0,
    "cross_collection_ref_count": 0
  }
}
```

证书路径写入 record 的 `riv_certificate_ref` 字段（字段名定义见 [02](./02_dataset_design.md)；本文档遵循已命名约定，但内文一律称为"RE 证书 / 逆向工程证书 / instance 正确性证书"）。

---

<a id="03-7"></a>

## §7 路由与产物落盘

### §7.1 通过证书的 record

四问全部 `status == "pass"` 的候选 record，按 [02](./02_dataset_design.md) 的切分规则进入主集：

- `TEND/train.json` 或 `TEND/test.json`（按 cross-domain 8:2，db 级别切分）；
- 同一 db 的 schema 与 world 各自落到 `TEND/mongodb_schema/<db_id>.json` 与 `TEND/mongodb_data/<db_id>.json`，全 db 共享，不重复落盘；
- 证书永久保留在 `audit/<db_id>/<record_id>/certificate.json`，主集 record 通过 `riv_certificate_ref` 引用。

### §7.2 未通过证书的 record

写到 `audit/rejected/<db_id>/<record_id>.json`，**不进入主集**。该文件包含：

- 原始候选 record（含未通过的 MQL / NLQ）；
- 失败的 Q 编号与失败摘要；
- 重试历史（如 [§6.4](#03-6-q3) 改写 world 失败 N 次后放弃）。

`audit/rejected/` 仅用于离线分析与构造侧 debug，不进入训练 / 评测流程。

### §7.3 资产桶单一原则

整个 TEND 只有两个状态：**主集（train + test）** 与 **rejected**。不存在 sidecar、staging 多层目录、长尾分桶等额外资产桶。这是有意设计：

- 主集每条都有完整 RE 证书背书；
- rejected 不参与训练 / 评测，避免污染；
- 没有"半通过"中间态，避免准入语义模糊。

---

<a id="03-8"></a>

## §8 构造侧总流程伪码

下列伪码刻画从一条 Spider 输入到一条 TEND record 的完整路径。注意 `record_id` 在跨 db 全局唯一，由构造侧分配。

```python
def build_record(spider_db, ref_sql, candidate_record_id, seed):
    db_id = spider_db.name

    schema = rewrite_schema_nosql_native(spider_db)
    write_to(f"TEND/mongodb_schema/{db_id}.json", schema)

    world = materialize_world(spider_db, schema, seed=seed)
    if not satisfy_discriminative_pressure(world, ref_sql, schema):
        world = repair_world(world, ref_sql, schema, seed=seed)
    write_to(f"TEND/mongodb_data/{db_id}.json", world)
    world_sig = compute_world_signature(world)

    mql = rewrite_mql_idiomatic(ref_sql, schema, world)

    nlqs = rewrite_5_nlqs(ref_sql, schema, world, mql)
    if not translator_consensus_pass(nlqs, ref_sql):
        return route_to_audit_rejected(candidate_record_id, reason="nlq_consensus_fail")

    cert = run_re_certificate(
        gold_mql=mql,
        canonical_nlq=nlqs[0],
        ref_sql=ref_sql,
        new_world=world,
        original_sqlite=spider_db,
        schema=schema,
    )

    if cert.all_pass():
        record = build_record_dict(
            record_id=candidate_record_id,
            db_id=db_id,
            nl_queries=nlqs,
            ref_sql=ref_sql,
            MQL=mql,
            schema_complexity_profile=cert.schema_profile,
            world_signature=world_sig,
            riv_certificate_ref=f"audit/{db_id}/{candidate_record_id}/certificate.json",
            construction_origin="spider_remapped",
        )
        write_certificate(cert, path=f"audit/{db_id}/{candidate_record_id}/certificate.json")
        return route_to_train_or_test(record)
    else:
        write_certificate(cert, path=f"audit/rejected/{db_id}/{candidate_record_id}.json")
        return route_to_audit_rejected(candidate_record_id, reason=cert.first_failed_q())


def run_re_certificate(gold_mql, canonical_nlq, ref_sql, new_world, original_sqlite, schema):
    cert = Certificate()
    cert.q1_execution        = check_q1_execution(gold_mql, ref_sql, new_world, original_sqlite)
    cert.q2_uniqueness       = check_q2_uniqueness(canonical_nlq, gold_mql, new_world, n_models=3)
    cert.q3_discrimination   = check_q3_discrimination(gold_mql, new_world, schema)
    cert.q4_world_non_trivial = check_q4_world_non_trivial(gold_mql, new_world)
    cert.schema_profile      = compute_schema_complexity_profile(schema)
    return cert
```

字段名 `record_id`、`db_id`、`nl_queries`、`ref_sql`、`MQL`、`schema_complexity_profile`、`world_signature`、`riv_certificate_ref`、`construction_origin` 均定义在 [02](./02_dataset_design.md)；本伪码仅承担"如何把这些字段值算出来"。

---

<a id="03-9"></a>

## §9 端到端 canonical 示例

把 [§2](#03-2) ~ [§7](#03-7) 串到一起，演示 `record_id = 99001` 的完整生成。

### §9.1 输入：Spider 源

- db：`orchestra`
- ref_sql：

```sql
SELECT T1.Name, COUNT(*) AS performance_count
FROM conductor T1
JOIN orchestra T2 ON T1.Conductor_ID = T2.Conductor_ID
JOIN performance T3 ON T2.Orchestra_ID = T3.Orchestra_ID
GROUP BY T1.Conductor_ID, T1.Name
ORDER BY performance_count DESC
LIMIT 3;
```

### §9.2 §2 阶段：schema 重塑

- root：`conductor`（按 [§2.2](#03-2)）；
- 嵌入链：`conductor → orchestra[] → performance[] → show[]`，与 `TEND/mongodb_schema/orchestra.json` 一致；
- NoSQL-native 注入：见 [§2.4](#03-2) 表格（performance 加 polymorphism、Year_of_Work 注 sparsity、Year_of_Founded 注 type drift）。

### §9.3 §3 阶段：world 物化

- 输入：原 SQLite + 重塑 schema + `seed`；
- 输出：`TEND/mongodb_data/orchestra.json`，每个 conductor 文档含若干 orchestra，每个 orchestra 含若干 performance；
- 判别压力：增加边界点使得"top 3"的 #3 与 #4 之间至少差 1 场演出（消除 tie）；
- `world_signature = "sha256:9c1f4a..."`。

### §9.4 §4 阶段：idiomatic MQL

见 [§4.4](#03-4)。要点：两次 `$unwind` 沿嵌入路径展开，无 `$lookup`，输出键名为 `Name` / `performance_count`。

### §9.5 §5 阶段：5 条 NLQ

见 [§5.1](#03-5) 的表格。每条经 translator consensus 校验通过。

### §9.6 §6 阶段：RE 证书四问

- Q1：mongosh 跑 idiomatic MQL 在 new world 上得到 `[("M. Cordoba", 9), ("L. Hartmann", 8), ("E. Tanaka", 7)]`；SQLite 跑 ref_sql 在原数据上得到等价结果（按 ≡_rec）→ pass；
- Q2：5 个 candidate MQL 全部跑出相同 top-3 → consensus_rate = 1.0 → pass；
- Q3：14 条 near-miss 全部不同（详见 [§6.6](#03-6-cert) 证书示例的 `near_miss_summaries`）→ pass；
- Q4：12 个 conductor 分组，filtered_rows = 47，边界点存在，输出非平凡 → pass。

### §9.7 §7 阶段：路由

- 四问全 pass → 写入 `TEND/train.json` 或 `TEND/test.json`（按 db 级别切分规则）；
- 同时落盘 `audit/orchestra/99001/certificate.json`；
- record 中 `riv_certificate_ref = "audit/orchestra/99001/certificate.json"`。

最终产物 record（仅展示构造侧填充的字段，完整字段定义见 [02](./02_dataset_design.md)）：

```json
{
  "record_id": 99001,
  "db_id": "orchestra",
  "nl_queries": [
    "List the top 3 conductors with the most performances.",
    "Across all conductors, count performances under each conductor's orchestras and return the top 3 conductors by total performances.",
    "For each conductor document, total the performances embedded under their orchestras and return the three conductors with the highest totals.",
    "Which three conductors have led the most performances overall?",
    "列出指挥过演出最多的前三位指挥家。"
  ],
  "ref_sql": "SELECT T1.Name, COUNT(*) AS performance_count FROM conductor T1 JOIN orchestra T2 ON T1.Conductor_ID = T2.Conductor_ID JOIN performance T3 ON T2.Orchestra_ID = T3.Orchestra_ID GROUP BY T1.Conductor_ID, T1.Name ORDER BY performance_count DESC LIMIT 3",
  "MQL": "db.conductor.aggregate([\n  { $unwind: \"$orchestra\" },\n  { $unwind: \"$orchestra.performance\" },\n  { $group: { _id: \"$Conductor_ID\", Name: { $first: \"$Name\" }, performance_count: { $sum: 1 } } },\n  { $sort: { performance_count: -1 } },\n  { $limit: 3 },\n  { $project: { _id: 0, Name: 1, performance_count: 1 } }\n]);\n",
  "schema_complexity_profile": {
    "normalized_ratio": 0.25,
    "max_embed_depth": 4,
    "polymorphism_rate": 0.25,
    "sparsity_rate": 0.30,
    "type_drift_count": 1,
    "dynamic_key_count": 0,
    "cross_collection_ref_count": 0
  },
  "world_signature": "sha256:9c1f4a...",
  "riv_certificate_ref": "audit/orchestra/99001/certificate.json",
  "construction_origin": "spider_remapped"
}
```

至此，一条 record 从 Spider SQLite 出发，经 schema 重塑、world 物化、MQL 重写、NLQ 重写、RE 证书四问，最终落到 TEND 主集。这条链路用 NoSQL-native 文档建模、可执行判别压力世界、idiomatic MQL、结构化 NLQ 与四问准入，逐一修复了 §0 列出的四个瓶颈。

---

## 文档间引用清单

| 引用方向 | 引用内容 |
|---|---|
| 03 → [01](./01_task_definition.md) | 任务正确性锚 ≡_rec、归一化契约、结果集比较规则 |
| 03 → [02](./02_dataset_design.md) | record 字段名（`record_id` / `db_id` / `nl_queries` / `ref_sql` / `MQL` / `schema_complexity_profile` / `world_signature` / `riv_certificate_ref` / `construction_origin`）、资产路径（`TEND/train.json`、`TEND/test.json`、`TEND/mongodb_schema/<db_id>.json`、`TEND/mongodb_data/<db_id>.json`、`audit/...`）、cross-domain 8:2 切分规则 |

本文档不引用 04 / 05。
