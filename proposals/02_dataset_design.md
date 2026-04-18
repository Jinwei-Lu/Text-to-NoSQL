# 数据集设计

> 文档定位: 定义 MonGen 的数据资产、主集与侧车资产、唯一字段名与枚举、子集组成、三层算子层级、SDT/Horizon 元数据、切分规则。
> 目标读者: 数据集构造者 / 评测复现者 / 模型消费方
> 前置阅读: [01 任务定义](./01_task_definition.md), [03 数据集构造方法](./03_dataset_construction.md), [04 评估方法](./04_evaluation_methodology.md), [05 解决方案设计](./05_solution_design.md)
> 规范性声明: 本文是数据层字段名、枚举值、资产桶名、切分单位与 Horizon 语义的唯一权威来源。`03` 负责构造机制，`04` 负责评测读取，`05` 负责方法消费；三者不得并行定义另一套公开字段或桶名。

<a id="02-0"></a>
## 0. 摘要

本文只定义数据层契约，不重复 [01](./01_task_definition.md) 的任务语义、[03](./03_dataset_construction.md) 的构造流程、[04](./04_evaluation_methodology.md) 的指标公式、以及 [05](./05_solution_design.md) 的模型消费细节。本文的职责是把“哪些资产存在、哪些样本能进主集、每条记录必须长什么样、Horizon 如何独立存在、各子集如何切分”一次性说清。

MonGen 的 headline benchmark 只承认同时满足以下条件的样本: `query_read_only = true`、`query_deterministic = true`、`lifting_status = full`、`triple_consensus_status = pass`、`operator_layer ∈ {core, extension}`。因此，主集 `synth_*`、`real_*`、`hybrid_*` 只包含三路共识通过、可 Lift、可复现实例结果的样本。Long-Tail、`longtail_AB_only` 以及各种分歧桶只以侧车审计资产存在，不进入主集 headline 统计；仅具内部审核价值的状态只保留在 staging。

三子集的公开主资产分工固定如下: `synth_*` 以 Family 为基本记录单位，每条记录包含同一 canonical 查询下的多条 NLQ，且 `K >= 3`；`real_*` 与 `hybrid_*` 以 sample 为基本记录单位，默认 `K = 1`。因此，Family-level 的鲁棒性指标默认只对 Synth 主集成立；若未来为 Real 或 Hybrid 显式物化变体，再按新增资产单独声明。

难度元数据层使用 `sd_norm`、`sdt_level`、`is_horizon` 三个字段表达。`L1-L5` 是 `sd_norm ∈ [0,1]` 上的固定分段；Horizon 是独立持出的 held-out pool，不属于主 train/test，也不进入主 in-distribution 报告分母。`is_horizon = true` 的样本不得进入监督训练、检索记忆或主集分母。

本文同时固定库级资产语义: `db_id` 对应唯一的 schema universe、唯一的库级 `modeling_style`、唯一的 `sci_score/sci_bucket`，以及唯一的可执行 world materialization 结果。样本记录只通过 `db_id` 绑定这些库级资产，不允许在样本级另起一套 schema 命名或世界状态。

贯穿本文的唯一示例是 `ecommerce_017`: canonical 查询不含 join，激活特性集为 `{F10,F15,F17}`，canonical pipeline 为 `$match -> $unwind -> $group -> $project -> $sort -> $limit` 六阶段，公开结果键为 `user_id` 与 `total_spent`，总 NLQ 数 `K = 5`。

<a id="02-1"></a>
## 1. 基准资产与子集组成

> 为何这样组织: 先定义“有哪些资产”，再定义“每个子集承担什么角色”，最后明确“主集 / Horizon / 侧车”三层边界。这样 03 在构造时知道往哪里写，04 在评测时知道从哪里读，05 在训练和检索时知道哪些资产必须排除。

<a id="02-1-1"></a>
### 1.1 资产清单

| 资产类 | 逻辑文件形态 | 粒度 | 进入条件 | 主要用途 |
|---|---|---|---|---|
| 主集记录 | `synth_train.jsonl`, `synth_test.jsonl`, `real_train.jsonl`, `real_test.jsonl`, `hybrid_train.jsonl`, `hybrid_test.jsonl` | Synth 为 `family`；Real/Hybrid 为 `sample` | `triple_consensus_status = pass` 且 `operator_layer ∈ {core, extension}` 且 `lifting_status = full` 且 `query_read_only = true` 且 `query_deterministic = true` 且 `is_horizon = false` | headline benchmark、训练 / 测试、主报告分母 |
| Horizon 池 | `synth_horizon.jsonl`, `real_horizon.jsonl`, `hybrid_horizon.jsonl` | 同上 | 与主集相同，但 `is_horizon = true` | 独立 held-out 分析，不进主分母 |
| 侧车审计资产 | `audit/<bucket>/<subset>.jsonl` | 保持原始粒度 | `triple_consensus_status != pass` 或仅用于现实性 / 审计保留 | realism analysis、错误归因、构造审计 |
| schema 资产 | `schemas/<db_id>.json`, `schemas/<db_id>.md` | `db_id` | 每个 `db_id` 恰有一份公开规范 schema | 模型输入、schema grounding、库级统计 |
| world 资产 | `worlds/<db_id>/manifest.json` 及其只读快照 | `db_id` | 每个 `db_id` 恰有一份可执行世界快照 | 执行评测、三路结果复现 |

主集与 Horizon 池共用同一行级 schema；差别只在 `asset_bucket`、`split`、`is_horizon` 的取值与使用约束。侧车审计资产也复用同一行级 schema，但允许出现主集不接受的 `triple_consensus_status`。

<a id="02-1-2"></a>
### 1.2 三子集组成与用途

MonGen 的三子集不是“同一资产换三种来源”，而是三种互补的数据角色:

| 子集 | 主记录粒度 | NLQ 组织 | 主用途 | 主集排除项 |
|---|---|---|---|---|
| `synth` | `family` | `K >= 3`，含 canonical | 受控覆盖、Family 级鲁棒性、结构分布设计 | Horizon、Long-Tail、`longtail_AB_only`、所有公开分歧桶 |
| `real` | `sample` | `K = 1` | 真实分布锚点，但仍服从严格主集契约 | Horizon、Long-Tail、`longtail_AB_only`、所有公开分歧桶与未解决样本 |
| `hybrid` | `sample` | `K = 1` | 真实意图骨架 × 合成 schema 的组合泛化 | Horizon、Long-Tail、`longtail_AB_only`、所有公开分歧桶 |

固定规则如下:

- `synth` 主集是 Family-based；Family 是数据资产概念，不在 Real/Hybrid 主集中默认出现。
- `real` 与 `hybrid` 主集默认按单样本交付；若未来显式物化 NLQ 变体，必须新增资产声明，不能反向修改本文定义。
- Long-Tail 只存在于侧车审计资产，不在任何 headline 主集中出现。
- `real` 可以保留更丰富的侧车桶，以支持现实性分析；但这些桶不进入主集，也不进入 headline 指标分母。

<a id="02-1-3"></a>
### 1.3 主集、Horizon 与侧车的边界

三类资产的边界是强约束，不是展示偏好:

1. 主集 `synth_* / real_* / hybrid_*` 只收 `triple_consensus_status = pass`。
2. Horizon 也是 `pass` 样本，但它是单独 held-out pool，不进入主 train/test，也不进入主 in-distribution 统计。
3. `longtail_AB_only`、`engine_quirk`、`a_bug`、`b_bug`、`c_bug`、`spec_ambiguity` 只写入 `audit/`，不进入 headline main splits；仅内部审核状态不写入公开 JSONL。
4. `synth` 与 `hybrid` 主集不含 Long-Tail；`real` 主集同样不含 Long-Tail 与未解决样本。

<a id="02-2"></a>
## 2. Schema Universe、SCI 与 Modeling Style

> 为何这样组织: 记录级样本必须落在稳定的库级资产上。先把 `db_id` 对应的 schema universe 讲清楚，再定义 `SCI` 与 `modeling_style` 这两个库级标签，后续记录字段才能保持单义。

<a id="02-2-1"></a>
### 2.1 Schema Universe 的库级原则

`db_id` 是数据层的唯一库标识。对任意一条记录:

- `db_id` 唯一绑定一份公开 schema 资产: `schemas/<db_id>.json` 与 `schemas/<db_id>.md`。
- `db_id` 唯一绑定一份只读 world 资产: `worlds/<db_id>/...`。
- 同一 `db_id` 下的所有记录共享相同的字段路径宇宙、类型宇宙、集合命名与库级标签。
- 样本记录不得嵌入一份与 `db_id` 不一致的局部 schema；所有 schema grounding 都必须回到 `db_id` 指向的库级资产。

库级 schema 资产负责回答“这个库里有哪些集合、字段、类型、稀疏路径与多态路径”；样本记录只负责回答“本条查询在这个库上引用了哪些部分、产生了什么结果、落在哪个 split/bucket”。

<a id="02-2-2"></a>
### 2.2 SCI 是库级属性，不是样本级再估一次

`SCI` 是数据库级复杂度标签，落在每个 `db_id` 上，并被复制到该库下全部记录。公开字段含义固定如下:

- `sci_score`: `float ∈ [0,1]`
- `sci_bucket`: `enum ∈ {low, mid, high}`

分桶边界固定为:

- `low`: `0 <= sci_score < 1/3`
- `mid`: `1/3 <= sci_score < 2/3`
- `high`: `2/3 <= sci_score <= 1`

`SCI` 的计算方法由 [03](./03_dataset_construction.md) 实现；本文只定义它在数据层的存储语义与枚举边界。

<a id="02-2-3"></a>
### 2.3 `modeling_style` 的公开枚举与语义

`modeling_style` 是库级公开枚举，字段值必须严格使用以下 lower_snake_case 字符串:

| `modeling_style` | 数据资产语义 |
|---|---|
| `normalized` | 主要通过引用连接实体，嵌套较浅 |
| `embedded` | 主要通过嵌套文档或数组承载语义 |
| `bucket` | 主要通过时间或区间桶组织数据 |
| `polyglot` | 同一库内混合多种建模习惯 |
| `legacy_drifting` | 同一路径在不同文档形态间出现存在性或类型漂移 |
| `tenant_sharded` | 以 tenant 维度做分片、动态键或映射组织 |

说明:

- 公开字段值只接受上表中的精确枚举，不允许使用大小写变体、连字符变体或同义别名。
- `modeling_style` 是库级标签，样本记录只是复制该标签，不重新估计。

<a id="02-2-4"></a>
### 2.4 库级 manifest 最小字段

每个 `db_id` 的库级 manifest 至少包含以下字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `db_id` | string | 库标识，与 split 记录中的 `db_id` 对齐 |
| `collection_names` | list[string] | 该库公开集合名列表 |
| `modeling_style` | enum | 取值见 [§2.3](#02-2-3) |
| `sci_score` | float | 取值见 [§2.2](#02-2-2) |
| `sci_bucket` | enum | 取值见 [§2.2](#02-2-2) |
| `schema_hash` | string | schema 资产摘要，用于绑定公开 schema |
| `world_hash` | string | world 资产摘要，用于绑定可执行只读快照 |

样本级记录不重复发明这些库级字段；它们只引用并复制必要的最小子集。

<a id="02-3"></a>
## 3. World Materialization 与数据侧契约

> 为何这样组织: 04 的执行评测与 05 的模型消费都依赖“世界快照是什么”“主集能接受什么样本”“归一化结果如何表达”。这些都属于数据侧契约，必须在本文固定。

<a id="02-3-1"></a>
### 3.1 World Materialization 的公开原则

World Materialization 在数据层只承诺以下原则:

1. **世界独立于查询意图**。world 资产先存在，查询后执行；样本不能反过来塑造 world。
2. **世界是只读执行面**。主集与 Horizon 都只允许 read-only 查询；写操作、不确定性操作、依赖外部状态的操作不进入 headline benchmark。
3. **文档只含业务字段**。world 中不得出现任何“辅助真值字段”“答案提示字段”“样本归属字段”。
4. **schema grounding 发生在公开 schema 上**。查询中引用的 collection / field / type 解释必须回到 `db_id` 对应的 schema 资产。
5. **公开归一化结果严格区分 `null` 与 `missing`**。`null` 必须显式保留；`missing` 必须表现为字段缺席，不能被补成 `null`。

这意味着 world 资产是评测与审计的共同执行底面，但不是标签本体；标签仍由记录级 `mql_canonical`、`cmrl_canonical`、`result_*_norm` 与证书字段表达。

<a id="02-3-2"></a>
### 3.2 主集 / Horizon / 侧车的成员契约

为避免文档间再出现第二套准入规则，成员契约在此固定。

**主集成员条件**:

```text
asset_bucket = "main"
split ∈ {"train", "test"}
triple_consensus_status = "pass"
operator_layer ∈ {"core", "extension"}
lifting_status = "full"
query_read_only = true
query_deterministic = true
is_horizon = false
```

**Horizon 成员条件**:

```text
asset_bucket = "horizon"
split 字段缺席
triple_consensus_status = "pass"
operator_layer ∈ {"core", "extension"}
lifting_status = "full"
query_read_only = true
query_deterministic = true
is_horizon = true
```

**侧车审计成员条件**:

```text
asset_bucket = "audit"
split 字段缺席
triple_consensus_status ∈ {
  "longtail_AB_only",
  "engine_quirk",
  "a_bug",
  "b_bug",
  "c_bug",
  "spec_ambiguity"
}
```

额外公开契约:

- `gold_result_norm` 是公开结果视图；主集与 Horizon 中它必须与 `result_a_norm`、`result_b_norm`、`result_c_norm` 逐位一致。
- 当 `triple_consensus_status = longtail_AB_only` 时，`result_c_norm` 必须缺席，而不是设为 `null`。
- 当某字段对某记录不适用时，公开记录应省略该字段，不用 `null` 伪装“不适用”。
- `instance_certificate_*` 字段用于表达实例级正确性审计摘要，必须与公开结果归一化规则一致。

<a id="02-4"></a>
## 4. 记录单位与唯一字段 schema

> 为何这样组织: 这一节既要统一 Synth / Real / Hybrid 的记录粒度，也要给出唯一的公开字段名。03-05 只能引用这里的字段，不得再定义平行的 `canonical.*`、`sdt.*` 或 `provenance.*` 公开接口。

<a id="02-4-1"></a>
### 4.1 记录单位与 Family 定义

MonGen 的公开记录单位按子集固定:

| 子集 | `record_grain` | `nlq_count` 规则 | `family_id` 规则 | 备注 |
|---|---|---|---|---|
| `synth` | `family` | `nlq_count >= 3`，且含 canonical | 必填 | 共享同一 `cmrl_canonical` / `mql_canonical` / 结果与元数据 |
| `real` | `sample` | `nlq_count = 1` | 缺席 | 主资产默认单样本，不做 Family 聚合 |
| `hybrid` | `sample` | `nlq_count = 1` | 缺席 | 主资产默认单样本，不做 Family 聚合 |

**Family 的唯一公开定义**:

- 一个 Family 只在 Synth 主资产和 Synth Horizon 中默认存在。
- 一个 Family 由同一 `cmrl_canonical`、同一 `mql_canonical`、同一 `gold_result_norm`、同一 `db_id` 以及多条语义等价 NLQ 组成。
- Family 内全部 NLQ 共享 `activated_features`、`modeling_style`、`sci_score/sci_bucket`、`sd_norm/sdt_level`、`is_horizon`、`triple_consensus_status` 与 `instance_certificate_*`。
- Real 与 Hybrid 默认不把单样本强行包装成 Family；因此 Family-level robustness 指标默认是 Synth-only。

#### 4.1.1 `ecommerce_017` 规范示例

本文唯一 canonical 示例固定如下:

- `db_id = ecommerce_017`
- `subset = synth`
- `record_grain = family`
- `modeling_style = legacy_drifting`
- `nlq_count = 5`
- `nlq_canonical = "Top 3 customers by total paid item spending in 2026."`
- `activated_features = ["F10", "F15", "F17"]`
- canonical pipeline 为 `$match -> $unwind -> $group -> $project -> $sort -> $limit`
- canonical 查询**不含 join**
- `output_keys = ["user_id", "total_spent"]`
- `sci_bucket = mid`
- `sdt_level = L3`
- `is_horizon = false`

示例中的 canonical MQL 骨架应写为:

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

<a id="02-4-2"></a>
### 4.2 公开记录字段: 唯一权威表

除特别说明外，下表字段对所有公开记录都有效。若某字段不适用，应省略该字段，不用 `null` 代替“不适用”。

#### 4.2.1 身份、路由与来源字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `record_id` | string | 全局唯一记录标识 |
| `family_id` | string | 仅 `record_grain = family` 时必填；其余记录缺席 |
| `subset` | enum | `{synth, real, hybrid}` |
| `record_grain` | enum | `{family, sample}` |
| `asset_bucket` | enum | `{main, horizon, audit}` |
| `split` | enum | 仅 `asset_bucket = main` 时出现，取值 `{train, test}` |
| `db_id` | string | 指向唯一 schema/world 资产 |
| `split_unit_kind` | enum | `{db_modeling_style, source_group, hybrid_schema_source}` |
| `split_unit_id` | string | 与 `split_unit_kind` 对应的稳定切分单元标识 |
| `source_kind` | enum | `{synthetic, github_repo, stack_overflow_thread, mongodb_forum_thread, hybrid_remap}` |
| `source_group_id` | string | Real 为 repo 或 thread 锚；Hybrid 为源 skeleton 分组；Synth 可与 `split_unit_id` 相同 |
| `source_uri` | string | 非 synthetic 来源时可出现 |
| `license_tag` | string | 来源许可标签或 `synthetic` |
| `desensitized` | bool | 是否做过来源脱敏 |
| `source_record_id` | string | 仅 Hybrid 或审计追踪场景出现，指向上游来源记录 |

#### 4.2.2 NLQ 与查询字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `nlq_canonical` | string | canonical NLQ 文本 |
| `nlq_canonical_style` | enum | `{formal, colloquial, jargon, noisy, negated, ambiguous, multilingual}` |
| `nlq_canonical_lang` | string | 语言代码，如 `en`、`zh` |
| `nlq_count` | int | Synth 主资产 `>= 3`；Real/Hybrid 主资产 `= 1` |
| `nlq_variants` | list[object] | 当 `nlq_count > 1` 时必填；否则缺席 |
| `cmrl_canonical` | object | `lifting_status = full` 时必填 |
| `fast_canonical` | object | canonical fAST |
| `mql_canonical` | string | canonical MQL 字符串 |
| `output_keys` | list[string] | 公开归一化输出的键顺序 |
| `activated_features` | list[string] | 已排序、去重的特性 id 列表 |
| `operator_layer` | enum | `{core, extension, longtail}`；按“最高层算子”标注 |
| `modeling_style` | enum | 取值见 [§2.3](#02-2-3) |

`nlq_variants[]` 的子字段固定为:

| 子字段 | 类型 | 规则 |
|---|---|---|
| `nlq_text` | string | 变体文本 |
| `nlq_style` | enum | 与 `nlq_canonical_style` 同一枚举 |
| `nlq_lang` | string | 语言代码 |

#### 4.2.3 正确性、归一化结果与实例证书字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `query_read_only` | bool | 主集与 Horizon 必须为 `true` |
| `query_deterministic` | bool | 主集与 Horizon 必须为 `true` |
| `lifting_status` | enum | `{full, partial, none}`；主集与 Horizon 必须为 `full` |
| `gold_result_norm` | list[object] | 公开权威归一化结果 |
| `result_a_norm` | list[object] | Compiler A 归一化结果 |
| `result_b_norm` | list[object] | Compiler B 归一化结果 |
| `result_c_norm` | list[object] | Compiler C 归一化结果；当 C 不可用时缺席 |
| `triple_consensus_status` | enum | `{pass, longtail_AB_only, engine_quirk, a_bug, b_bug, c_bug, spec_ambiguity}` |
| `instance_certificate_status` | enum | `{pass, fail, skipped}` |
| `instance_certificate_checks` | list[enum] | 取值子集 `{schema_grounding_checked, lift_roundtrip_checked, normalized_output_checked, reverse_instance_checked}` |
| `instance_certificate_ref` | string | 实例证书摘要或稳定引用 |

归一化输出强约束:

- `null` 必须显式保留为 `null`。
- `missing` 必须表现为字段缺席。
- `gold_result_norm`、`result_a_norm`、`result_b_norm`、`result_c_norm` 中同名字段都遵守同一规则。

#### 4.2.4 难度、切片与泛化字段

| 字段 | 类型 | 规则 |
|---|---|---|
| `sci_score` | float | `0 <= sci_score <= 1` |
| `sci_bucket` | enum | `{low, mid, high}` |
| `sd_norm` | float | `0 <= sd_norm <= 1` |
| `sdt_level` | enum | `{L1, L2, L3, L4, L5}` |
| `is_horizon` | bool | Horizon 成员标记 |
| `combo_signature` | string | 规范形式为 `modeling_style|F...+F...`，用于组合泛化统计 |

**唯一权威说明**:

- 公开数据行只以以上**顶层字段名**为准。
- 任何 `canonical.*`、`sdt.*`、`provenance.*` 形式都只能视为派生视图，不是公开权威字段名。
- 若 03-05 需要展示嵌套视图，必须明确其来源字段仍然是本文定义的顶层字段。

<a id="02-5"></a>
## 5. 三层算子分布与主集 / 审计桶关系

> 为何这样组织: 三层算子是 01 的表示层语义，但“哪些层能进主集、主集中各层的目标比例是什么、Long-Tail 去哪里”是数据设计问题，必须在这里固定。

<a id="02-5-1"></a>
### 5.1 `operator_layer` 的标注规则

`operator_layer` 按查询中出现的**最高层算子**标注:

- `core`: 只出现 Core 原语。
- `extension`: 出现至少一个 Extension 原语，但不含 Long-Tail 节点。
- `longtail`: 出现任意 Long-Tail 节点。

主集与 Horizon 只允许 `core` 与 `extension`；`longtail` 只允许进入侧车审计资产。

<a id="02-5-2"></a>
### 5.2 主集的层分布目标

主集 headline benchmark 的层分布目标按子集分别约束:

| 资产 | `core` 目标 | `extension` 目标 | `longtail` 目标 | 说明 |
|---|---|---|---|---|
| `synth_*` 主集 | 65%-75% | 25%-35% | 0% | 受控覆盖，Long-Tail 只留侧车 |
| `real_*` 主集 | 60%-80% | 20%-40% | 0% | 主集从真实语料中过滤出可 Lift、可三路共识的部分 |
| `hybrid_*` 主集 | 60%-75% | 25%-40% | 0% | 组合泛化主集仍要求可 Lift、可三路共识 |

说明:

- 以上比例只针对主集，不包括 Horizon，也不包括任何 audit sidecar。
- `real` 的自然 Long-Tail 分布可以保留在 audit sidecar 中，但不得回灌进 headline main splits。
- `synth` 与 `hybrid` 主集没有 Long-Tail 配额；若构造期产生 Long-Tail，只能进入审计资产。

<a id="02-5-3"></a>
### 5.3 `triple_consensus_status` 到资产桶的映射

| `triple_consensus_status` | 去向 | 是否计入 headline |
|---|---|---|
| `pass` 且 `is_horizon = false` | `main` | 是 |
| `pass` 且 `is_horizon = true` | `horizon` | 否 |
| `longtail_AB_only` | `audit/longtail_AB_only/` | 否 |
| `engine_quirk` | `audit/engine_quirk/` | 否 |
| `a_bug` | `audit/a_bug/` | 否 |
| `b_bug` | `audit/b_bug/` | 否 |
| `c_bug` | `audit/c_bug/` | 否 |
| `spec_ambiguity` | `audit/spec_ambiguity/` | 否 |

这张映射表是主集与侧车之间的最终准绳。任何文档若把 `longtail_AB_only` 或分歧桶写入 headline main split，都与本文冲突。仅内部审核状态不属于本文定义的公开枚举。

<a id="02-6"></a>
## 6. SDT、L1-L5 与 Horizon 元数据

> 为何这样组织: 01 负责定义 SDT 的语义，03 负责计算；本文负责固定这些结果在数据资产中的存储含义与使用边界。

<a id="02-6-1"></a>
### 6.1 数据层只存三件事: `sd_norm`、`sdt_level`、`is_horizon`

对每条公开记录，难度侧只要求三项稳定元数据:

- `sd_norm`: 归一化后的标量难度，范围固定在 `[0,1]`
- `sdt_level`: 由 `sd_norm` 落入固定区间得到的离散档位
- `is_horizon`: 是否属于独立 Horizon 持出池

其中 `sdt_level` 与 `is_horizon` 是并列元数据，不是从属关系。一个样本既有 `sdt_level`，也可能同时是 Horizon 成员。

<a id="02-6-2"></a>
### 6.2 `L1-L5` 的固定区间

`sdt_level` 的分段在数据层固定为:

| `sdt_level` | `sd_norm` 区间 |
|---|---|
| `L1` | `[0.00, 0.20)` |
| `L2` | `[0.20, 0.40)` |
| `L3` | `[0.40, 0.60)` |
| `L4` | `[0.60, 0.80)` |
| `L5` | `[0.80, 1.00]` |

这五档是固定区间，不随子集、模型或报告场景改变。

<a id="02-6-3"></a>
### 6.3 Horizon 是独立 held-out pool

Horizon 的数据层语义固定如下:

- Horizon 不是主 train/test 的一个附加标签，而是**单独资产桶**。
- `is_horizon = true` 的样本必须写入 `asset_bucket = horizon`。
- Horizon 样本不进入监督训练。
- Horizon 样本不进入检索记忆。
- Horizon 样本不进入主 in-distribution 报告分母。
- Horizon 样本仍然必须满足 `triple_consensus_status = pass`、`lifting_status = full`、`query_read_only = true`、`query_deterministic = true`。

因此，Horizon 的差别只在“使用边界”，不在“正确性标准”。

<a id="02-7"></a>
## 7. 切分策略与组合泛化约束

> 为何这样组织: 三子集来源不同，切分单位不能混为一谈。本文分别固定 Synth、Real、Hybrid 的 split unit，再定义统一的组合泛化统计口径。

<a id="02-7-1"></a>
### 7.1 共同规则

所有主集切分都先执行以下共同步骤:

1. 先从候选 `pass` 样本中抽出全部 `is_horizon = true` 样本，写入 Horizon 池。
2. 剩余 `is_horizon = false` 的 `pass` 样本再进入主 `train/test` 切分。
3. 同一个 `split_unit_id` 下的记录不得跨 `train/test`。
4. `combo_signature` 统一定义为 `modeling_style|<sorted activated_features>`。

组合泛化统计口径固定为:

```text
test_combo_novelty
= 测试集中 distinct combo_signature 且训练集中未出现的个数
  / 测试集中 distinct combo_signature 的总个数
```

`combo_signature` 只用于泛化分析与切分约束，不替代 `split_unit_id`。

<a id="02-7-2"></a>
### 7.2 Synth: 按 schema-aware 单元切分

Synth 主集使用 `split_unit_kind = db_modeling_style`。

规范要求:

- `split_unit_id` 的规范形态为 `<db_id>::<modeling_style>`。
- 同一 `family_id` 内的全部 NLQ 必须同侧切分。
- 同一 `db_modeling_style` 单元下的全部 Family 必须同侧切分。
- Synth 主集以 schema-aware 隔离为第一优先级；在此基础上，测试集应尽量满足 `test_combo_novelty >= 30%`。

Synth 是 Family-based 资产，因此切分单位永远高于单条 NLQ 变体，不允许按变体打散。

<a id="02-7-3"></a>
### 7.3 Real: 按 source repo / thread 切分

Real 主集使用 `split_unit_kind = source_group`，并且**只能**按来源仓库或讨论线程切分，不能回退为 `(db_id × modeling_style)`。

规范要求:

- GitHub 类样本的 `source_group_id` 采用 repo 级锚点，例如 `github.com/org/repo`。
- 论坛 / 问答类样本的 `source_group_id` 采用 thread 级锚点，例如 `stackoverflow:123456`、`mongodb_forum:topic_abc`。
- 同一 `source_group_id` 下的全部样本必须同侧切分。
- `source_group` 隔离优先级高于组合泛化配额；若两者冲突，以来源隔离为准，并在 manifest 中记录实际 `test_combo_novelty`。

Real 主集只保留 `pass` 样本。`longtail_AB_only` 与各类公开分歧状态可在 `audit/` 中保留，用于现实性分析，但不得并入主集；仅内部审核状态不写入公开资产。

<a id="02-7-4"></a>
### 7.4 Hybrid: 按 schema × source 的复合单元切分

Hybrid 主集使用 `split_unit_kind = hybrid_schema_source`。

规范要求:

- `split_unit_id` 的规范形态为 `<target_db_id>::<modeling_style>::<source_group_id>`。
- 同一复合单元下的全部样本必须同侧切分。
- 该设计同时保留 schema-aware 隔离与来源隔离，避免相同 target schema 或相同 real source skeleton 在 train/test 两侧交叉泄漏。
- Hybrid 主集同样要求 `operator_layer ∈ {core, extension}`，不接纳 Long-Tail。

Hybrid 的 `source_group_id` 继承其 real skeleton 来源，因此来源隔离仍然是可追踪的，而不是在 remap 后丢失。

<a id="02-7-5"></a>
### 7.5 组合泛化约束的使用方式

`combo_signature` 是统一的组合泛化统计键:

- 对 Synth 与 Hybrid，`test_combo_novelty >= 30%` 是主切分目标。
- 对 Real，先满足 `source_group` 隔离，再报告实际 `test_combo_novelty`。
- `combo_signature` 只看 `modeling_style` 与 `activated_features`，不把 `db_id`、`source_group_id` 或 `subset` 混入签名本身。

这条约束保证测试集不仅是“没见过的库”或“没见过的来源”，也是“没见过的结构组合”。

<a id="02-Y"></a>
## Y. 外部引用关系

本文与其他 proposal 的责任边界固定如下:

| 文档 | 责任 |
|---|---|
| [01 任务定义](./01_task_definition.md) | 定义任务语义、cMRL / fAST 分层、SDT 语义与结果归一化含义 |
| [03 数据集构造方法](./03_dataset_construction.md) | 负责把 schema、world、三路结果与 `instance_certificate_*` 构造出来，并按本文字段名写盘 |
| [04 评估方法](./04_evaluation_methodology.md) | 只消费本文定义的 `main / horizon / audit` 资产边界与公开字段，不再另定义字段名 |
| [05 解决方案设计](./05_solution_design.md) | 只消费本文定义的主集记录；`horizon` 与 `audit` 不进入监督训练与检索记忆 |

最后再强调一次:

- 字段名与枚举以本文为准。
- 主集只收 `pass`。
- Horizon 单列，不进主 train/test 与主分母。
- Long-Tail 与所有未解决状态只存在于侧车审计资产。
