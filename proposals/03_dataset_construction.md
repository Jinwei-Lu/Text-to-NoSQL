# 数据集构造规范

> 文档定位: 本文只定义 MonGen 基准的构造机制, 即候选样本如何被生成、验证、分桶、写盘. 任务定义与数学符号以 [01 任务定义](./01_task_definition.md) 为准, 记录字段与 split 契约以 [02 数据集设计](./02_dataset_design.md) 为准, 评测与报告口径以 [04 评估方法](./04_evaluation_methodology.md) 为准.
> 目标读者: 数据构造者 / 审核者 / 复现者

<a id="03-0"></a>
## 0. 摘要

本文把 MonGen 的构造过程收敛为一条严格的准入合同: **只有 deterministic、read-only、schema-grounded、liftable、A/B/C 三路共识、且通过 Reverse Instance Verification (RIV) 的样本, 才能进入主基准**. 主基准唯一允许的入库状态是 `triple_consensus_status = pass`. `longtail_AB_only` 与全部公开分歧桶都只是 sidecar 审核制品, 不进入主集 split; 仅具内部审核价值的状态只保留在 staging。

三条构造轨道在这一收紧口径下分工明确:

- **Synth**: 从显式 schema 与 benchmark-owned world 正向生成 `cmrl_canonical`, 再确定性 Lowering 为 `fast_canonical` 与 `mql_canonical`. Synth 主集只使用 deterministic、read-only、可 full-lift 的 Core / Extension 子集, 不生成 Long-Tail.
- **Real**: 从公开来源挖掘真实 MQL, 经 parser 得到 `fast_canonical`, 经 full lifting 得到 `cmrl_canonical`, 再在 benchmark-owned、schema-grounded 的快照上做三路验证. 只有 full-lift 且 `pass` 的样本进入主集; Long-Tail、内部审核状态与 unresolved 样本都只保留为审核旁路.
- **Hybrid**: 仅以 **已通过主集准入的 Real 骨架** 为输入, 将其 remap 到 **已通过主集准入的 Synth schema / world 配置** 上重建样本. 因而 Hybrid 主集天然不含 Long-Tail, 也不含 unresolved skeleton.

构造流水线包含六道防线: `schema grounding -> world validity -> query admissibility & liftability -> triple compiler consensus -> RIV -> NLQ family assembly`. 任何一步失败都不会写入主集.

本文不另起一套公开 JSONL schema. **最终公开记录一律复用 02 的 top-level 字段**: `record_id`, `family_id`, `subset`, `record_grain`, `asset_bucket`, `split`, `db_id`, `nlq_canonical`, `nlq_variants`, `cmrl_canonical`, `fast_canonical`, `mql_canonical`, `gold_result_norm`, `result_a_norm`, `result_b_norm`, `result_c_norm`, `triple_consensus_status`, `instance_certificate_status`, `instance_certificate_checks`, `instance_certificate_ref`, `sci_score`, `sci_bucket`, `sd_norm`, `sdt_level`, `is_horizon`, `modeling_style`, `activated_features` 以及相关来源字段。

贯穿本文的 canonical 样例为 `ecommerce_017`: canonical NLQ 为 `Top 3 customers by total paid item spending in 2026.`, canonical query **无 join**, 管道严格为 6 个 stage: `[$match, $unwind, $group, $project, $sort, $limit]`, 结果键为 `user_id` 与 `total_spent`, 激活特性集为 `{F10, F15, F17}`, NLQ family 总数取 `K = 5`.

<a id="03-1"></a>
## 1. 主基准准入合同

主基准不是“所有能执行的样本”的合集, 而是满足下列合同的严格子集. 记候选 family 为 `F`, 则其进入主基准的充要条件是:

$$
\text{admit\_main}(F)
\iff
\text{deterministic}(F)
\wedge
\text{read\_only}(F)
\wedge
\text{schema\_grounded}(F)
\wedge
\text{liftable}(F)
\wedge
(\text{triple\_consensus\_status}(F)=\texttt{pass})
\wedge
\text{riv\_status}(F)=\texttt{pass}
\wedge
\text{nlq\_family\_valid}(F)
$$

其中各项含义如下:

- `deterministic`: 查询不依赖随机性、系统时钟、外部搜索索引状态或宿主函数副作用.
- `read_only`: 查询不写库、不导出结果、不合并结果、不依赖运行中系统状态.
- `schema_grounded`: 每个字段路径、集合引用、类型约束、输出键都能在显式 schema package 中解析.
- `liftable`: `mql_canonical -> fast_canonical -> cmrl_canonical` 的往返链条是 full、无歧义、无 Long-Tail 悬空节点.
- `triple_consensus_status = pass`: Compiler A / B / C 全部已定义且 `r_A \equiv_{rec} r_B \equiv_{rec} r_C`.
- `riv_status = pass`: 当前实例可以将 gold query 与一组 near-miss counterqueries 机械分开.
- `nlq_family_valid`: `nlq_canonical` 与 `nlq_variants` 满足 02 的 family 契约, 且 family 中保留的每条 NLQ 都经 translator 共识通过.

这一定义直接导出三条硬边界:

1. **主集不接收 `longtail_AB_only`**.
2. **主集不接收 `a_only`**.
3. **主集不接收分歧桶**: `engine_quirk`, `A_bug`, `B_bug`, `C_bug`, `spec_ambiguity` 全部只保留在 sidecar.

<a id="03-2"></a>
## 2. 三轨构造架构

### 2.1 三轨职责表

| 轨道 | 输入起点 | 主集范围 | 旁路范围 |
|---|---|---|---|
| `synth` | Schema Generator + World Materializer + cMRL Sampler | deterministic、read-only、liftable 的 Core / Extension 样本 | Long-Tail 审核样本, 编译分歧样本, RIV 失败样本, NLQ 失败样本 |
| `real` | 公开来源中的 MQL + 代码 / 论坛上下文 | 可解析、可 full-lift、可重建 schema / world、且三路 `pass` 的样本 | `longtail_AB_only`, unresolved schema, partial / failed lift, 编译分歧, internal `a_only` |
| `hybrid` | 已通过主集准入的 Real `cmrl_canonical` 骨架 + 已通过主集准入的 Synth schema / world 配置 | remap 后仍 deterministic、read-only、full-lift、三路 `pass` 的样本 | remap 冲突、类型不闭合、RIV 失败、NLQ 失败 |

### 2.2 三轨统一产物

三轨都收敛到同一公开 family 记录:

- `cmrl_canonical`
- `fast_canonical`
- `mql_canonical`
- `r_A`, `r_B`, `r_C`
- `triple_consensus_status`
- `nlq_canonical`, `nlq_variants`
- `structural_difficulty`, `sdt_level`, `is_horizon`
- `sci_score`, `sci_bucket`, `modeling_style`, `activated_features`
- `provenance`

因此三轨的差异只体现在**候选样本从哪里来, 如何被 grounding, 如何被重建 world**, 而不体现在公开写盘字段上.

### 2.3 三轨统一状态机

所有轨道都服从同一状态机:

```text
candidate
  -> schema_grounded
  -> world_frozen
  -> canonicalized
  -> triple_checked
  -> riv_checked
  -> nlq_assembled
  -> main_or_sidecar_route
```

任一阶段失败, 都不会越过该阶段直接写入主集.

<a id="03-3"></a>
## 3. 主集算子范围与可提升约束

01 定义的是 MonGen 可表达的表示空间; **03 进一步定义进入主集构造的可接纳子空间**. 主集的 admissible operator set 只保留 deterministic、read-only、可 full-lift 的算子.

### 3.1 主集允许的 Core / Extension 子集

主集允许使用的算子类别如下:

- Core 中的常规 filter / projection / grouping / unwind / sort / limit / skip / count / simple lookup / pipeline lookup
- deterministic 的 `graphLookup`
- deterministic 的 `facet`
- deterministic 的 `setWindowFields`
- deterministic 的 `bucket`, `bucketAuto`
- deterministic 的 `densify`, `fill`
- deterministic 的 `unionWith`
- deterministic 的 `redact`
- deterministic 的 `replaceRoot` / `replaceWith`
- deterministic 的 `push` / `addToSet`
- deterministic 的 `map` / `reduce` / `objectToArray` / `expr_complex`

是否允许某一具体节点进入主集, 由三条条件同时决定:

1. 可 full-lift 到 `cmrl_canonical`
2. Compiler C 对该节点有定义
3. 节点不引入随机性、外部依赖或写副作用

### 3.2 主集明确排除的节点

以下节点**不进入主集构造**, 只可作为 sidecar 的 Long-Tail 示例或审核材料:

- `$sample`
- `$search`
- `$out`
- `$merge`
- `$rand`
- `$$NOW`
- 宿主语言函数调用
- 依赖运行态系统表或系统统计的查询

这条规则同时作用于三轨:

- Synth 不生成这些节点
- Real 发现这些节点后只能进入 sidecar
- Hybrid 不从含这些节点的骨架派生主集样本

### 3.3 `null` 与 `missing` 的公开归一化

主集与公开 sidecar 都执行同一套结果归一化规则, 且**严格区分 `null` 与 `missing`**:

- 显式 `null` 保留为 `null`
- 缺失字段在归一化结果 dict 中保持**缺失**, 不回填 `null`
- 结果哈希、`equiv_rec` 比较、RIV witness 抽取都以这一规则为准

例如:

```json
{"paid_at": null}
```

与

```json
{}
```

在公开归一化层面不是同一个结果.

<a id="03-4"></a>
## 4. Synth 轨操作规程

Synth 轨从零开始生成 schema、world 与 query, 但主集准入合同会把可写入范围收缩到 deterministic、read-only、triple-pass 的 family.

### 4.1 Schema Generator

Schema Generator 的职责不是“写一个方便查询通过的库”, 而是生成一个**显式、可导出、可校验**的 schema package. 每个 Synth `db_id` 至少产出以下 staging 资产:

- `schema.json`: 机器可读 schema
- `schema.md`: 提供给 NLQ 构造与 translator 的 schema markdown
- `json_schema/`: 各 collection 的 `$jsonSchema` 导出
- `field_inventory.json`: 全字段路径、类型、稀疏约束、引用约束
- `role_inventory.json`: 可被 Hybrid remap 复用的角色槽位

Schema Generator 的产出必须满足:

1. 所有 collection 与字段名显式列出
2. 所有引用路径闭合
3. 所有 union / sparsity / nested 结构可导出为 validator
4. 不在文档中植入任何“只为评测服务”的隐藏 truth 字段

`ecommerce_017` 的 schema package 中, canonical query 只使用 `orders` 集合, 但 schema 仍然可以包含 `customers` 等其他 collection. 关键约束不是“库里只能有一张表”, 而是 **canonical query 本身不做 join**.

### 4.2 World Materializer

World Materializer 以 schema package 为唯一结构上游, 产出一个冻结的数据快照 `D` 与对应的 event trace. 每个 Synth world 至少产出:

- `collections/<coll>.ndjson`: 物化后的文档快照
- `event_log.ndjson`: 事件轨迹
- `world_stats.json`: collection 级计数、字段缺失率、值域摘要
- `lineage_index.json`: 为 RIV 和调试准备的文档来源索引

World Materializer 的操作约束:

1. 事件模板只写 schema 中存在的字段
2. 显式 `null` 与字段缺失分别生成
3. 同一个 world seed 重跑必须得到同一快照
4. 查询正确性不依赖 `event_log`; `event_log` 只用于审计与 witness 追溯

### 4.3 cMRL Sampler

cMRL Sampler 在显式 schema 与冻结 world 上采样 `cmrl_canonical`. Synth 主集中的 `cmrl_canonical` 必须同时满足:

- 字段路径全部可解析
- 类型约束全部可满足
- 算子属于主集允许子集
- 结果非空
- 结果不退化为“几乎全表”
- `mql_canonical` 经 parser 后可以 full-lift 回原 `cmrl_canonical`

操作上, Sampler 以“先结构, 后字段, 再字面量”的顺序工作:

1. 先选 stage skeleton
2. 再为每个节点绑定 schema-grounded 字段
3. 最后根据 world 统计绑定字面量边界与 limit
4. 再做一次 full-lift 回环校验

### 4.4 `ecommerce_017` 的 Synth canonical

`ecommerce_017` 的 canonical family 在 Synth 轨中的 `cmrl_canonical` 与 `mql_canonical` 固定为下列 6-stage 管道:

```javascript
db.orders.aggregate([
  { $match: { status: "paid", paid_at: { $exists: true, $gte: ISODate("2026-01-01") } } },
  { $unwind: "$items" },
  { $group: { _id: "$user_id", total_spent: { $sum: "$items.price" } } },
  { $project: { _id: 0, user_id: "$_id", total_spent: 1 } },
  { $sort: { total_spent: -1 } },
  { $limit: 3 }
])
```

该 family 的主集属性是:

- 无 join
- 输出键固定为 `user_id`, `total_spent`
- `activated_features = ["F10", "F15", "F17"]`
- `nlq_canonical = "Top 3 customers by total paid item spending in 2026."`
- NLQ family 总数 `K = 5`

<a id="03-5"></a>
## 5. Real 轨操作规程

Real 轨从公开来源获得真实 MQL, 但主集并不直接信任“原始真实查询”. 它必须被重新 grounding 到 benchmark-owned 的 schema 与 world 上, 然后接受与 Synth 同样严格的三路验证与 RIV.

### 5.1 来源接入与合规过滤

Real 候选样本进入构造流水线前, 先经过来源接入与过滤:

1. 抽取原始 MQL 与最小上下文
2. 脱敏专有名词、账号、标识符与私密字面量
3. 过滤 license 不可用来源
4. 过滤写操作、随机操作、外部搜索依赖与系统态依赖
5. 以 canonical MQL hash 去重

通过这一步的样本才进入 parser / lifting.

### 5.2 Parser -> fAST -> canonicalization

Real 轨的 parser 输出 `fast_canonical`. 这一步要求:

- 输入可以来自 mongosh、驱动 API 或代码字符串
- BSON 字面量必须被恢复为 typed AST 节点
- 字段路径、集合名、字面量类型被显式抽出
- query 先 canonicalize, 再进入 lifting

若 parser 失败, 样本停留在 staging, 不进入 sidecar JSONL.

### 5.3 Schema dossier 重建

Real 样本不能只靠一条查询字符串进入主集, 还必须被 grounding 到显式 schema dossier. Schema dossier 的来源按优先级合并:

1. 代码中的 ODM / schema 定义
2. 同文件或同帖子中的 sample docs
3. 查询本身暴露的字段与字面量类型
4. projection / group / sort / lookup 暴露的输出键与外键关系
5. 论坛上下文中的字段解释文字

只有当上述信息能收敛成**显式 schema package**时, Real 样本才继续向前推进. 否则标记为 unresolved, 只留在 staging.

### 5.4 Full lifting

Real 主集只接受 `lifting_status = full`.

full lifting 的定义是:

- `fast_canonical` 的所有关键节点都能映射到 `cmrl_canonical`
- 没有 Long-Tail 悬空节点
- 没有“只能保留部分语义”的 partial 占位
- `cmrl_canonical` 再 Lowering 回 `fast_canonical` 时语义不丢失

路由规则:

- `full` -> 继续
- `partial` -> staging only
- `failed` 且 A/B 可比较 -> 只可能去 `longtail_AB_only` sidecar
- `failed` 且连 A/B 审核价值都不足 -> staging only

### 5.5 Real world 重建

Real 主集使用 benchmark-owned world, 而不是依赖外部真实库. world 重建流程如下:

1. 从 schema dossier 抽取字段域、枚举、键关系与字面量模板
2. 生成一个 deterministic event program
3. 由 World Materializer 在显式 schema 下物化快照
4. 用原始查询与 near-miss 集合共同校验该快照具有区分度

这一步的目标不是复制原来源数据库, 而是构造一个**结构上忠实、语义上可验证、对 gold query 有判别力**的 benchmark-owned 实例.

### 5.6 Real 主集与旁路的分界

Real 轨进入主集的最小路径是:

`source MQL -> parser -> full lift -> schema dossier -> world freeze -> triple pass -> RIV pass -> NLQ family pass`

以下情况都不得进入主集:

- 仅 A 路可运行的 `a_only`
- A/B 一致但 C 未定义的 `longtail_AB_only`
- 无法恢复显式 schema 的 unresolved
- partial lift
- 分歧桶

<a id="03-6"></a>
## 6. Hybrid 轨 remap 操作规程

Hybrid 轨的输入不是任意 Real 候选, 而是**已经满足主集合同的 Real family**. 这使得 Hybrid 主集自动继承两条性质: 输入骨架已经 full-lift, 输入骨架已经 `pass`.

### 6.1 输入约束

Hybrid remap 的输入必须同时满足:

- 源 Real family 的 `triple_consensus_status = pass`
- 源 Real 记录的 `lifting_status = full`
- 源 Real 记录的 `instance_certificate_status = pass`
- 目标 Synth schema / world 配置已经通过 Synth 主集准入

因此 Hybrid 主集**不从 Long-Tail Real skeleton 派生**.

### 6.2 角色抽象与字段绑定

remap 分两步:

1. **角色抽象**: 从 Real `cmrl_canonical` 抽取角色槽位, 例如 `EntityID`, `MonetaryValue`, `Timestamp`, `Category`, `Status`
2. **字段绑定**: 在目标 Synth schema 的 `role_inventory.json` 中寻找唯一或可裁决绑定

字段绑定必须通过:

- 路径存在
- 类型兼容
- cardinality 兼容
- 输出键与 alias 可恢复

如果某角色需要二义裁决且无法机械收敛, remap 失败, 样本停留在 staging.

### 6.3 Hybrid world 重建

Hybrid 不直接复用源 Real 的 world. 它复用的是**目标 Synth schema 与其 world 构造配置**, 再在该 schema 下物化一个新的冻结快照. 这样做有两个目的:

1. 保持目标 schema 的结构压力
2. 防止源 Real world 中的字面量偶然性影响 remap 结果

### 6.4 Hybrid 主集边界

Hybrid 主集中的样本必须再次满足完整合同:

- remap 后仍 deterministic
- remap 后仍 read-only
- remap 后仍 full-lift
- remap 后三路仍 `pass`
- remap 后 RIV 仍 `pass`

因此 Hybrid 的 admission 是一次**完整重验证**, 不是拷贝源状态.

<a id="03-7"></a>
## 7. Triple Compiler 操作协议

Triple Compiler 是主集 admission 的核心门槛. 但在 03 中, 它是一个**操作协议**, 不是再定义 01 的数学语义.

### 7.1 输入合同

进入 Triple Compiler 的候选必须已经拥有:

- `cmrl_canonical`
- 冻结 world `D`
- 显式 schema package
- 由 Compiler A 生成的 `fast_canonical`
- 由 Compiler A unparse 的 `mql_canonical`

在执行前, 先做两件事:

1. 对 `mql_canonical` 重新 parser 一次, 确认可回到 `fast_canonical`
2. 对 `fast_canonical` 重新 lift 一次, 确认可回到 `cmrl_canonical`

只有回环闭合的样本才进入三路执行.

### 7.2 三路执行

- **Compiler A**: `cmrl_canonical -> fast_canonical -> mql_canonical -> MongoDB`
- **Compiler B**: `cmrl_canonical -> alternate lowering / planner -> MQL -> MongoDB`
- **Compiler C**: `cmrl_canonical -> denotational interpretation -> in-memory result`

三路都读取同一个冻结 world, 并使用同一套 BSON 归一化.

### 7.3 归一化与递归相等

主集与公开 sidecar 共享同一套归一化:

| 类型 | 公开归一化 |
|---|---|
| `ObjectId` | 24 位 hex string |
| `Date` | ISO-8601 UTC string |
| `Decimal128` | 保留精度的 string |
| `Long` | 安全范围内为 int, 否则为 string |
| `Binary` | base64 string |
| `Regex` | 规范化结构或字符串 |
| `null` | `null` |
| `missing` | 键缺失, 不回填 |

若查询无显式排序, 则在比较前按 canonical row hash 做稳定排序; 若查询声明了排序, 则按原顺序比对.

### 7.4 状态路由

Triple Compiler 的路由在构造层面固定如下:

| 条件 | 路由 | 是否主集可用 |
|---|---|---|
| `r_A = r_B = r_C` | `triple_consensus_status = pass` | 是 |
| `r_A = r_B`, `r_C = undefined` | `triple_consensus_status = longtail_AB_only` | 否 |
| 三路分歧, 可归因到 `engine_quirk` / `A_bug` / `B_bug` / `C_bug` / `spec_ambiguity` | 对应 sidecar divergence bucket | 否 |
| 只有 A 路可维持临时审核价值 | internal `a_only` staging route | 否 |

这里特别强调两点:

1. **`longtail_AB_only` 只保留旁路价值, 不进入主集**
2. **`a_only` 不属于 02 的公开 `triple_consensus_status` 枚举, 因而只保存在 staging 审核目录, 不写公开 JSONL**

### 7.5 `ecommerce_017` 的三路通过条件

`ecommerce_017` 的 canonical family 之所以可入主集, 必须同时满足:

- A 路得到按 `total_spent` 降序的前三个 `user_id`
- B 路得到相同三行与相同顺序
- C 路在内存解释下得到相同三行与相同顺序
- 结果行中若某键缺失, 仍保持缺失; 不能被归一化流程补成 `null`

只有这样, 它才会被标记为 `triple_consensus_status = pass`.

<a id="03-8"></a>
## 8. Reverse Instance Verification (RIV)

RIV 是 03 中新增的硬门. Triple Compiler 只能证明“gold query 的三路语义一致”; **RIV 还要证明“当前实例本身能把 gold query 与近似错误查询区分开”**. 一个样本若缺少 instance discriminativity, 即使三路共识成立, 也不进入主集.

### 8.1 RIV 的输入与输出

RIV 的输入是:

- `cmrl_canonical`
- 冻结 world `D`
- `mql_canonical`, `fast_canonical`
- 三路一致的 `r_A = r_B = r_C`

RIV 的输出是:

- `riv_status ∈ {pass, fail}`
- 一组 near-miss counterqueries
- 每个 counterquery 对应的 witness 集合
- 一份证书文件 `certificate.json`

### 8.2 witness 抽取

RIV 对每个 counterquery `q'` 抽取两层 witness:

1. **输出层 witness**
   - `positive_witness`: 在 gold 结果中存在, 在 `q'` 结果中不存在的行
   - `negative_witness`: 在 `q'` 结果中存在, 在 gold 结果中不存在的行
   - `rank_witness`: 行集合相同但排序或截断不同的首个分歧位置
2. **来源层 witness**
   - 由 `lineage_index.json` 或 stage trace 追到的最小输入文档集合
   - 用于说明差异由哪些源文档触发

输出层 witness 用公开归一化行哈希保存; 来源层 witness 用脱敏后的 `_id` 或行哈希保存.

### 8.3 near-miss counterquery 变异族

RIV 不靠人工拍脑袋写反例, 而是按固定 mutation family 机械生成 near-miss:

| 变异族 | 变异方式 |
|---|---|
| `predicate_boundary` | 调整数值 / 日期边界, 如 `gte` 改成更宽或更窄边界 |
| `predicate_operator` | `eq/ne/gt/gte/lt/lte/exists/type` 间做近邻替换 |
| `missing_null_confusion` | 删除 `exists`、把显式 `null` 条件改成缺失容忍, 或反向修改 |
| `aggregation_operator` | `sum/avg/min/max/count` 做近邻替换 |
| `group_key` | 替换 group key 或删除关键 group key |
| `stage_drop_or_bypass` | 删除 `unwind`、删除 `project`、删除关键 filter stage |
| `sort_limit` | 反转排序方向、改 `limit`、删除 `limit` |
| `join_binding` | 仅对含 join 的样本, 改 foreign key、改 join path、删 join stage |

并非每个 family 对所有样本都适用. RIV 只要求**适用的 mutation family**全部被覆盖.

### 8.4 RIV 接受条件

主集样本必须同时满足下列 RIV 条件:

1. 对每个适用的 mutation family, 至少生成 1 条可执行、deterministic、read-only、schema-grounded、liftable 的 counterquery
2. 全部适用 family 合计至少得到 6 条有效 counterquery
3. 对每条有效 counterquery, `result(gold, D)` 与 `result(q', D)` 在归一化后必须不相等
4. 每条有效 counterquery 都必须抽取到至少 1 个输出层 witness
5. 整个 family 至少抽取到 1 组来源层 witness

只要有一条有效 counterquery 与 gold 在当前实例上不可区分, `riv_status = fail`, 该样本不得进入主集.

### 8.5 证书写法

RIV 证书的**完整内容**保留在 staging, 公开记录只写入 02 已定义的顶层证书字段:

- staging 中保存完整证书:
  - `staging/<subset>/<family_id>/05_riv/certificate.json`
- 公开记录写入以下 top-level 字段:
  - `instance_certificate_status`
  - `instance_certificate_checks`
  - `instance_certificate_ref`

如需保存 counterquery 数量或 witness 摘要, 只能进入 staging 证书正文或来源侧 trace, 不得额外发明新的公开 top-level 字段。

### 8.6 `ecommerce_017` 的 RIV

`ecommerce_017` 的 canonical family 至少覆盖下列 near-miss:

1. 删除 `paid_at: {$exists: true}`
2. 将 `paid_at >= 2026-01-01` 改为更宽边界
3. 将 `$sum: "$items.price"` 改为 `$avg: "$items.price"`
4. 删除 `$unwind`
5. 将 `$sort: {total_spent: -1}` 改为升序
6. 将 `$limit: 3` 改为 `5`

其 witness 以输出键 `user_id` 与 `total_spent` 的归一化行哈希保存. 只要上述任一 near-miss 在当前实例上与 gold 不可分, 该 family 就不会被写入主集.

<a id="03-9"></a>
## 9. 六道防线

03 的操作栈用六道防线串起全流程. 这六道防线在 admission 上是“与”关系, 不是加权打分.

| 防线 | 负责问题 | 失败路由 |
|---|---|---|
| ① Schema Grounding | 字段、类型、引用、输出键是否显式可解析 | staging reject |
| ② World Validity | world 是否满足 schema validator, 且 `null` / `missing` 被分别物化 | 重新物化或 staging reject |
| ③ Query Admissibility & Liftability | 查询是否 deterministic、read-only、可 full-lift | 主集拒绝; 仅部分样本可去 sidecar |
| ④ Triple Compiler Consensus | A/B/C 是否都定义且三路一致 | `pass` 之外全部旁路 |
| ⑤ RIV | 当前实例是否能区分 gold 与 near-miss | 主集拒绝 |
| ⑥ NLQ Family Assembly | 是否形成满足 02 契约的 NLQ family | 主集拒绝或只留 staging |

这六道防线的职责分工是:

- ①② 保障 **实例本身合法**
- ③ 保障 **查询属于主集可接纳空间**
- ④ 保障 **gold 正确性**
- ⑤ 保障 **实例判别力**
- ⑥ 保障 **语言接口质量**

<a id="03-10"></a>
## 10. NLQ family 组装

主集中的公开 family 记录必须形成 02 所定义的 `nlq_canonical + nlq_variants` 结构. 03 只定义如何构造, 不重写 02 的字段契约.

### 10.1 构造原则

NLQ 构造只看:

- `cmrl_canonical` 的 pseudo-code
- `schema.md`
- 必要的 domain glossary

不直接把 `mql_canonical` 原样喂给 NLG, 以避免语言层抄写 MongoDB 语法.

### 10.2 translator 共识

每条候选 NLQ 都必须经过 translator 共识. 只有严格通过的候选, 才会进入公开 family:

- `nlq_canonical`: 从通过候选中选择一条最清晰、槽位最完整的主问句
- `nlq_variants`: 其余通过候选进入变体列表

未通过候选的去向:

- `one_translator_disagree` -> 留在 staging `06_nlq/rejected/`
- 明显多解或歧义候选 -> 留在 staging `06_nlq/ambiguous/`

它们都不进入公开 `nlq_variants`.

### 10.3 `ecommerce_017` 的 K=5

`ecommerce_017` 的公开 family 采用 `K = 5` 的写法:

- `nlq_canonical`: `"Top 3 customers by total paid item spending in 2026."`
- `nlq_variants`: 4 条通过 translator 共识的变体

无论变体语言风格如何, 它们共享同一 `cmrl_canonical`, `mql_canonical`, `r_A`, `r_B`, `r_C`, `triple_consensus_status`, `activated_features`.

<a id="03-11"></a>
## 11. 主集、Horizon 与 sidecar 的路由关系

主集 admission 完成后, 样本还需要按 02 的 split 规则进入 train / test 或 Horizon 保留集. 这里要明确区分三种目的完全不同的出口.

### 11.1 路由表

| 出口 | 条件 | 说明 |
|---|---|---|
| 主集 `*_train.jsonl` / `*_test.jsonl` | `triple_consensus_status = pass` 且 `instance_certificate_status = pass` 且 `is_horizon = false` | 正常主集 split |
| `synth_horizon.jsonl` / `real_horizon.jsonl` / `hybrid_horizon.jsonl` | `triple_consensus_status = pass` 且 `instance_certificate_status = pass` 且 `is_horizon = true` | 这是 pass-only holdout, 不是失败 sidecar |
| `sidecar/longtail_AB_only.jsonl` | `triple_consensus_status = longtail_AB_only` | sidecar 审核制品 |
| `sidecar/divergence/*.jsonl` | `engine_quirk`, `A_bug`, `B_bug`, `C_bug`, `spec_ambiguity` | sidecar 审核制品 |
| `staging/real/a_only/` | internal `a_only` | 只保留审核证据, 不写公开 JSONL |
| `staging/rejected/` | unresolved schema, partial lift, remap 失败, RIV 失败, NLQ 失败 | 只保留构造证据 |

### 11.2 sidecar 与主集的硬隔离

构造器在文件层面执行以下硬隔离:

1. splitter 只读取 `triple_consensus_status = pass` 且 `instance_certificate_status = pass` 的记录
2. sidecar 文件永远不参与 split assignment
3. subset-specific Horizon 文件只接收 `pass` 且 `instance_certificate_status = pass` 的记录
4. `a_only` 不产生公开 family 记录

因此不会出现“sidecar 样本误入主集 split”的路径.

<a id="03-12"></a>
## 12. 写盘结构与 staging 资产

### 12.1 顶层目录

```text
MonGen/
├── meta.json
├── synth_train.jsonl
├── synth_test.jsonl
├── real_train.jsonl
├── real_test.jsonl
├── hybrid_train.jsonl
├── hybrid_test.jsonl
├── synth_horizon.jsonl
├── real_horizon.jsonl
├── hybrid_horizon.jsonl
├── sidecar/
│   ├── longtail_AB_only.jsonl
│   └── divergence/
│       ├── engine_quirk.jsonl
│       ├── A_bug.jsonl
│       ├── B_bug.jsonl
│       ├── C_bug.jsonl
│       └── spec_ambiguity.jsonl
├── schemas/
│   ├── ecommerce_017.json
│   └── ecommerce_017.md
└── staging/
    ├── synth/
    ├── real/
    ├── hybrid/
    └── rejected/
```

这里有三个关键点:

1. subset-specific Horizon 文件是 **pass 样本的保留视图**
2. `sidecar/` 是 **非主集审核视图**
3. `staging/` 是 **构造证据仓**

### 12.2 staging 目录粒度

每个候选 family 都有自己的 staging 目录:

```text
staging/<subset>/<family_id>/
├── 01_schema/
├── 02_world/
├── 03_query/
├── 04_compilers/
├── 05_riv/
├── 06_nlq/
└── final_record.json
```

其中:

- `01_schema/` 保存 schema package
- `02_world/` 保存冻结快照、event trace 与 lineage index
- `03_query/` 保存 `cmrl_canonical`, `fast_canonical`, `mql_canonical`
- `04_compilers/` 保存 A/B/C 的原始输出与归一化结果
- `05_riv/` 保存 near-miss 集合与证书
- `06_nlq/` 保存候选 NLQ、translator 结果与保留清单
- `final_record.json` 是待写盘的 02 公开记录

### 12.3 公开记录字段

03 不定义新的 top-level 字段. `final_record.json` 必须与 02 对齐. 以 `ecommerce_017` 为例:

```json
{
  "record_id": "rec_synth_fam_ecommerce_017_top3_paid_2026",
  "family_id": "fam_ecommerce_017_top3_paid_2026",
  "subset": "synth",
  "record_grain": "family",
  "asset_bucket": "main",
  "split": "train",
  "db_id": "ecommerce_017",
  "split_unit_kind": "db_modeling_style",
  "split_unit_id": "ecommerce_017|legacy_drifting",
  "source_kind": "synthetic",
  "source_group_id": "ecommerce_017|legacy_drifting",
  "license_tag": "synthetic",
  "desensitized": false,
  "nlq_canonical": "Top 3 customers by total paid item spending in 2026.",
  "nlq_canonical_style": "formal",
  "nlq_canonical_lang": "en",
  "nlq_count": 5,
  "nlq_variants": [
    {"nlq_text": "Which three customers spent the most on paid items in 2026?", "nlq_style": "colloquial", "nlq_lang": "en"},
    {"nlq_text": "Rank the top 3 customers by paid item GMV in 2026.", "nlq_style": "jargon", "nlq_lang": "en"},
    {"nlq_text": "Give the three customers with the highest paid-item total in 2026.", "nlq_style": "formal", "nlq_lang": "en"},
    {"nlq_text": "2026 年按已支付商品消费总额排名前三的顾客是谁?", "nlq_style": "multilingual", "nlq_lang": "zh"}
  ],
  "cmrl_canonical": {
    "intent": "aggregate",
    "scope": {
      "collection": "orders",
      "filters": [
        {"field": "status", "op": "eq", "value": "paid"},
        {"field": "paid_at", "op": "exists", "value": true},
        {"field": "paid_at", "op": "gte", "value": "2026-01-01", "type": "Date"}
      ],
      "unwinds": [{"path": "items"}]
    },
    "grouping": {
      "by": ["user_id"],
      "aggs": [{"alias": "total_spent", "op": "sum", "field": "items.price"}]
    },
    "projection": {"include": ["user_id", "total_spent"]},
    "ordering": [{"field": "total_spent", "direction": "desc"}],
    "limits": {"limit": 3}
  },
  "fast_canonical": {"op": "aggregate", "collection": "orders", "stages": ["$match", "$unwind", "$group", "$project", "$sort", "$limit"]},
  "mql_canonical": "db.orders.aggregate([...])",
  "output_keys": ["user_id", "total_spent"],
  "activated_features": ["F10", "F15", "F17"],
  "operator_layer": "core",
  "modeling_style": "legacy_drifting",
  "query_read_only": true,
  "query_deterministic": true,
  "lifting_status": "full",
  "gold_result_norm": [
    {"user_id": "6512a0bb21c7f1e8d9a4b123", "total_spent": "2845.80"},
    {"user_id": "6512b1cc32d8f2f9e0a5c234", "total_spent": "2301.15"},
    {"user_id": "6512c2dd43e9f3fae1b6d345", "total_spent": "1987.60"}
  ],
  "result_a_norm": [
    {"user_id": "6512a0bb21c7f1e8d9a4b123", "total_spent": "2845.80"},
    {"user_id": "6512b1cc32d8f2f9e0a5c234", "total_spent": "2301.15"},
    {"user_id": "6512c2dd43e9f3fae1b6d345", "total_spent": "1987.60"}
  ],
  "result_b_norm": [
    {"user_id": "6512a0bb21c7f1e8d9a4b123", "total_spent": "2845.80"},
    {"user_id": "6512b1cc32d8f2f9e0a5c234", "total_spent": "2301.15"},
    {"user_id": "6512c2dd43e9f3fae1b6d345", "total_spent": "1987.60"}
  ],
  "result_c_norm": [
    {"user_id": "6512a0bb21c7f1e8d9a4b123", "total_spent": "2845.80"},
    {"user_id": "6512b1cc32d8f2f9e0a5c234", "total_spent": "2301.15"},
    {"user_id": "6512c2dd43e9f3fae1b6d345", "total_spent": "1987.60"}
  ],
  "triple_consensus_status": "pass",
  "instance_certificate_status": "pass",
  "instance_certificate_checks": [
    "schema_grounding_checked",
    "lift_roundtrip_checked",
    "normalized_output_checked",
    "reverse_instance_checked"
  ],
  "instance_certificate_ref": "staging/synth/fam_ecommerce_017_top3_paid_2026/05_riv/certificate.json",
  "sci_score": 0.44,
  "sci_bucket": "mid",
  "sd_norm": 0.43,
  "sdt_level": "L3",
  "is_horizon": false,
  "combo_signature": "legacy_drifting|F10+F15+F17"
}
```

### 12.4 原子写盘协议

写盘按以下顺序执行:

1. 在 staging 中生成 `final_record.json`
2. 用 02 的字段契约校验 `final_record.json`
3. 校验 `triple_consensus_status` 与路由出口一致
4. 若是 `pass` family, 再根据 `is_horizon` 与 split assignment 选择写入目标文件
5. 以原子 append 的方式写入 JSONL
6. 写入后更新 `meta.json` 中的计数、哈希与索引

sidecar 的公开 JSONL 也复用同一写盘协议, 只是目标文件不同.

<a id="03-13"></a>
## 13. 构造总流程伪码

```python
def build_family(track, candidate):
    schema_pkg = build_or_recover_schema(track, candidate)
    if not schema_pkg.ok:
        return route_staging("schema_unresolved", candidate)

    world = materialize_world(track, candidate, schema_pkg)
    if not world.ok:
        return route_staging("world_invalid", candidate)

    canonical = derive_canonical_query(track, candidate, schema_pkg, world)
    if not canonical.full_lift or not canonical.admissible:
        return route_nonmain(track, canonical)

    triple = run_triple_compiler(canonical, world)
    if triple.status != "pass":
      
        return route_nonmain(track, triple)

    riv = run_riv(canonical, world, triple)
    if riv.status != "pass":
        return route_staging("riv_fail", canonical)

    nlq_pack = build_nlq_family(canonical, schema_pkg)
    if not nlq_pack.ok:
        return route_staging("nlq_fail", canonical)

    record = assemble_record_according_to_02(
        track=track,
        schema_pkg=schema_pkg,
        world=world,
        canonical=canonical,
        triple=triple,
        riv=riv,
        nlq_pack=nlq_pack,
    )

    return write_record(record)
```

这段伪码体现的关键点是: **主集写盘永远发生在 triple pass 与 RIV pass 之后**, 而不是之前.
