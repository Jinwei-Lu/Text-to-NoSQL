# SMART 系统设计

> 文档定位: 在 `01-04` 已固定任务、数据、构造与评测契约的前提下，定义 SMART 的方法架构、阶段职责、训练与推理接口、检索视图、A/B/C 优化闭环，以及部署与监控边界。
> 目标读者: 模型团队 / 系统实现者 / 复现者
> 前置阅读: [01 任务定义](./01_task_definition.md), [02 数据集设计](./02_dataset_design.md), [03 数据集构造方法](./03_dataset_construction.md), [04 评估方法](./04_evaluation_methodology.md)

<a id="05-0"></a>
## 0. 摘要

本文只定义 SMART 的系统设计，不重写 `01-04` 已固定的任务、数据、构造与评测契约。SMART 直接消费 `01` 的任务 I/O、`cMRL + fAST` 表示、Lowering/Lifting 边界与 Compiler A/B/C 语义角色，消费 `02` 的三子集资产形态、`F1-F17` 特性体系、canonical 字段集与切分规则，消费 `03` 的 schema markdown、可执行工件与离线构造产物，消费 `04` 的 EX / EX-Sym / Family / 子集级报告协议。`05` 只回答“方法如何利用这些契约完成 Text-to-NoSQL 推理”，不改变 gold、split、bucket 或 metric 的定义。

SMART 主线由四个阶段组成: `Stage 1` 做 schema-grounded 理解与字段对齐; `Stage 2` 产出与 benchmark 主范围对齐的结构化查询草案; `Stage 3` 在严格 split-safe 的训练记忆中做多视图检索与结构修复; `Stage 4` 使用 pred-side 的 A/B/C 三类信号做执行约束与局部优化，输出最终 MQL。这里的 A/B/C 信号是方法侧的候选诊断与优化证据: A 是当前候选的物理执行摘要，B 是可恢复结构计划上的代数一致性摘要，C 是符号侧的结构形状指纹。它们用于候选排序、局部重写与停止条件，不改写 benchmark gold 逻辑；最终 benchmark 通过与否仍由 `04` 定义的 EX / EX-Sym 计算。

SMART 的主线目标严格对齐 benchmark 主范围。任何 Long-Tail 处理若被部署侧需要，只能作为 side-bucket / auxiliary lane 单独维护，不与主 benchmark 主线混写，不作为 headline 结果的一部分。Horizon 是评估侧的 held-out pool；`is_horizon`、审计桶、gold 执行结果与其他 benchmark-only 元数据都不进入模型输入、检索查询或推理 prompt。难度控制只能来自 NLQ、schema 与当前结构草案可导出的代理特征，而不是 ground-truth bucket 标签。

在贯穿示例 `ecommerce_017` 中，SMART 统一使用 canonical 主线: 无 join、六阶段管道 `[$match, $unwind, $group, $project, $sort, $limit]`，激活特性 `{F10, F15, F17}`。如果引用 Synth 风格 Family，则该 Family 含 `K=5` 条 NLQ 变体，但共享同一 canonical 查询骨架。任何扩展示例若超出这条主线，都在文中显式标注为“派生变体”，不替代 canonical benchmark 示例。

<a id="05-1"></a>
## 1. 上游契约与方法边界

> 为何这样组织: SMART 必须建立在 `01-04` 的单源权威之上。先把“SMART 读取什么、不重写什么、禁止注入什么”说清楚，后续架构与接口才能保持干净。

### 1.1 SMART 消费的上游契约

| 来源 | SMART 直接消费的内容 | 在 SMART 中的作用 |
|---|---|---|
| `01` | 任务输入输出、`db_id` 路由语义、`cMRL + fAST`、Lowering/Lifting、Compiler A/B/C 角色 | 限定主线 IR、可执行边界与 A/B/C 可用条件 |
| `02` | 三子集记录单位、canonical 字段集、`F1-F17`、切分纪律、Family/sample 语义 | 限定训练样本组织、检索记忆构建与 split-safe 规则 |
| `03` | schema markdown、world snapshot、compiler 工件、归一化执行产物 | 提供离线资产与在线执行适配器 |
| `04` | EX / EX-Sym、Family 级与子集级报告协议 | 提供训练后离线评估接口 |

### 1.2 SMART 不重写的内容

SMART 不重新定义下列对象:

- 任务输入输出形式，仍以 `01` 为准。
- `cMRL`、`fAST`、Lowering、Lifting、Compiler A/B/C 的语义边界，仍以 `01` 与 `03` 为准。
- 三子集的记录单位、切分规则、canonical 字段名、`F1-F17` 编号，仍以 `02` 为准。
- EX、EX-Sym、Family 级与子集级指标，仍以 `04` 为准。
- Horizon、审计桶、分歧桶的评估含义，仍以 `04` 为准。

### 1.3 方法侧的硬边界

SMART 在方法侧额外遵守以下边界:

- `db_id` 只作为 schema / 执行环境的路由键与缓存键，不作为语义捷径特征。
- 查询侧不可使用 `mql_canonical`、`cmrl_canonical`、`gold_result_norm / result_a_norm / result_b_norm / result_c_norm`、`is_horizon`、`triple_consensus_status`、审计桶标签或任何 benchmark-only 元数据。
- Real 的训练、验证与检索记忆必须遵守 source-anchored 切分，不得退化成 `(db_id × modeling_style)` 逻辑。
- 只有 Synth 风格 multi-NLQ Family 保证 family-aware 结构；Real 与 Hybrid 的主资产是 sample-level，不默认附带 Family 级鲁棒性语义。
- Long-Tail 若被部署侧额外处理，只能作为 auxiliary lane 单独管理；benchmark 主线不把它写成主目标或主记忆。

<a id="05-2"></a>
## 2. SMART 总体架构

> 为何这样组织: 先给四阶段全景，再把每一阶段的职责与输入输出对齐到上游契约。这样后面的训练接口、检索视图与优化闭环都能直接落位。

### 2.1 四阶段主线

| 阶段 | 核心责任 | 主要输入 | 主要输出 | 约束来源 |
|---|---|---|---|---|
| `Stage 1` Schema Grounding | 把 NLQ 锚定到 schema 命名空间，形成字段与集合候选 | `NLQ`, `schema_markdown`, `db_id` | `grounding_state` | `01` 输入契约, `02` schema 资产 |
| `Stage 2` Structured Drafting | 生成与 benchmark 主范围对齐的结构化查询草案 | `grounding_state` + 原始输入 | `draft_plan` | `01` 表示层, `02` canonical 字段 |
| `Stage 3` Retrieval-guided Repair | 在 split-safe 训练记忆中召回相似资产并修复草案 | `draft_plan` + derived views | `refined_plan` | `02` 切分规则, `03` 训练资产 |
| `Stage 4` A/B/C Optimization | 以候选执行摘要做局部重写与停止判定 | `refined_plan`, runtime DB handle | `final_mql`, `optimization_trace` | `01`/`03` compiler 边界, `04` 评估协议 |

### 2.2 阶段职责展开

#### Stage 1: Schema Grounding

Stage 1 的职责不是直接生成 MQL，而是把 NLQ 的语义槽位约束到目标 schema 命名空间内。它输出的对象至少包含:

- `candidate_collections`
- `candidate_fields`
- `target_fields`
- `feature_proxy(F1-F17)`
- `difficulty_proxy`

其中 `feature_proxy(F1-F17)` 是从当前 `NLQ + schema + partial draft` 推断出的预测侧代理，只用于检索与服务调度；它不是 benchmark gold 的 `activated_features`，也不替代 `02` 中的标注定义。

#### Stage 2: Structured Drafting

Stage 2 输出的是方法侧结构化草案，而不是新的 benchmark 语义层。该草案必须满足两点:

- 能够稳定映射到 `01` 已定义的 `cMRL / fAST` 主线表示。
- 能够被确定性 lower / unparse 到可执行 MQL。

SMART 主线优先面向 benchmark 主范围；因此 Stage 2 的训练目标与推理目标都以可回落到 `cMRL Core ∪ Extension` 的结构计划为中心。若部署侧检测到输入明显超出主范围，可把请求分流到 auxiliary long-tail side-bucket，但该路径与 benchmark 主线分仓、分表、分开报告。

#### Stage 3: Retrieval-guided Repair

Stage 3 的职责是把“当前草案”放到训练侧记忆中找相似结构，然后做最小必要修复，而不是从头重写。它只使用训练侧资产构建记忆:

- Synth 记忆是 family-aware 的。
- Real 记忆是 sample-aware 且 source-anchored 的。
- Hybrid 记忆是 sample-aware 的派生结构记录。

Stage 3 不能看到测试资产、Horizon pool、审计桶标签，也不能把 benchmark-only 元数据拼进 prompt。

#### Stage 4: A/B/C Optimization

Stage 4 只处理当前候选，不读取 gold。它把当前候选送入三类 pred-side 信号管道:

- A: 当前候选在目标执行环境上的物理执行摘要。
- B: 当前结构计划可恢复时的代数一致性摘要。
- C: 当前结构计划可恢复时的符号结构形状指纹。

A/B/C 在方法侧的用途是:

- 识别语法错误、字段绑定错误、聚合器错误、排序/限制遗漏等局部问题。
- 决定是否继续修复、如何修复、何时停止。
- 输出部署侧可观测的 trace。

A/B/C 不改变 benchmark 的 gold 判定；benchmark 通过与否仍由 `04` 的 EX / EX-Sym 计算。

### 2.3 贯穿示例 `ecommerce_017`

在 `ecommerce_017` 的 canonical Synth Family 中，共有 `K=5` 条 NLQ 变体，共享同一条主线查询骨架:

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

这条 canonical 主线满足:

- 无 join
- 六阶段管道包含 `$project`
- 激活特性 `{F10, F15, F17}`

SMART 对这条样例的阶段职责分工如下:

- `Stage 1` 把 “Top 3 customers by total paid item spending in 2026.” 对齐到 `orders`, `paid_at`, `items.price`, `user_id`。
- `Stage 2` 生成六阶段结构草案。
- `Stage 3` 从训练记忆中召回同类“时间过滤 + 聚合 + Top-K”资产，修复遗漏的字段投影、排序或聚合器。
- `Stage 4` 用 A/B/C 识别诸如“把 `$sum` 误写成 `$avg`”或“漏掉 `$project`”这类局部错误，并做最小 patch。

<a id="05-3"></a>
## 3. 训练与推理接口

> 为何这样组织: SMART 需要明确哪些对象是训练监督、哪些对象是运行时输入、哪些对象只是方法侧中间态。接口干净，系统才不会偷偷侵入上游契约。

### 3.1 运行时请求与中间对象

SMART 的运行时主请求仍与 `01` 对齐:

```text
SmartRequest = {
  nlq,
  schema_markdown,
  db_id,
  runtime_handle
}
```

方法侧中间对象定义为:

```text
GroundingState = {
  candidate_collections,
  candidate_fields,
  target_fields,
  feature_proxy[F1..F17],
  difficulty_proxy
}

DraftPlan = {
  structured_plan,
  lowerable_fast,
  auxiliary_route_flag
}

RetrievalBundle = {
  retrieved_records,
  rerank_trace,
  refined_plan
}

OptimizationTrace = {
  a_summary,
  b_summary,
  c_fingerprint,
  applied_patches,
  stop_reason
}

SmartResponse = {
  final_mql,
  final_plan,
  optimization_trace,
  needs_review
}
```

这些对象都是方法侧接口，不是新的 benchmark 数据契约。它们的字段命名可以随工程实现细化，但语义边界必须服从 `01-04`。

### 3.2 Stage 1/2 的训练监督

Stage 1 的监督目标直接由训练资产中的 canonical 结构投影得到，不需要新增标签体系。可直接从训练侧 `cmrl_canonical` / `fast_canonical` / schema 导出:

- 根 collection 对应 `candidate_collections`
- 被引用字段路径对应 `candidate_fields`
- 最终输出字段对应 `target_fields`
- `F1-F17` 只在记忆侧保留 benchmark-native 标注；查询侧只学习 `feature_proxy`

Stage 2 的监督目标来自训练侧的 canonical 结构资产:

- 主线监督来自 `cmrl_canonical` 与 `fast_canonical`
- 输出草案必须保持可 lower / parse / unparse 的结构稳定性
- 主线不以 Long-Tail 作为 headline 训练目标

如果部署侧确实要做 Long-Tail 兜底，应把 Long-Tail 样本单列为 auxiliary side-bucket，和 benchmark 主线分开训练、分开监控、分开报告。

### 3.3 Stage 3 记忆构建接口

记忆构建只读取训练侧资产，且严格服从 `02` 的切分纪律:

- Synth / Hybrid: 遵守 `02` 的数据切分，不跨训练侧与评估侧混用。
- Real: 遵守 source-anchored 切分；同一 source repo / thread 只会出现在一侧。
- Horizon: 不进入训练记忆。
- 审计桶与分歧桶: 不进入主线记忆。
- gold 执行结果: 不以值表形式进入 prompt；若需要使用，只能转成结构摘要或指纹。

### 3.4 Family 与 sample 的组织规则

SMART 在样本组织上显式区分两类资产:

- **Synth 风格 multi-NLQ Family**: 可做 family-aware 训练与检索，同一 canonical 结构对应多条 NLQ 变体。
- **Real / Hybrid 主资产**: 默认按 sample-level 组织，不假设 `K>=3` 的 family 结构，也不把 family-level robustness 当成全 benchmark 的默认前提。

这意味着:

- Stage 3 的“family 扩展召回”只在 family 结构真实存在时启用。
- Stage 4 的“同意图多表达一致性”主要作为 Synth 家族资产上的附加收益，而不是对 Real / Hybrid 的默认假设。

### 3.5 推理期禁止注入的对象

以下对象在推理期不进入模型、检索向量或 prompt:

- `is_horizon`
- `triple_consensus_status`
- 审计桶标签
- benchmark-only 的人工复核字段
- 评估侧 held-out pool 标记
- gold 的 `gold_result_norm / result_a_norm / result_b_norm / result_c_norm`
- gold `activated_features`
- 任何来自测试侧或 held-out 侧的记忆记录

SMART 如果需要难度控制，只能从当前 `NLQ + schema + draft_plan` 推出 `difficulty_proxy`，而不能读取 ground-truth bucket。

<a id="05-4"></a>
## 4. 检索视图与记忆组织

> 为何这样组织: Stage 3 是 SMART 的“经验层”，但它必须是 split-safe、contract-safe 的。先说 query 侧视图，再说 memory 侧记录，再说合并与 rerank 规则。

### 4.1 Query 侧视图

SMART 的 query 侧检索视图只来自当前请求与当前草案，可分为六个基础视图和一个可选 C 侧指纹视图:

| 视图 | Query 侧来源 | 作用 |
|---|---|---|
| `V_nlq` | NLQ 语义向量 | 找相近意图 |
| `V_schema` | schema 结构摘要 | 对齐 collection / field / type 形态 |
| `V_collection` | Stage 1 预测的 collection 集 | 限定召回域 |
| `V_field` | Stage 1 预测的字段集与 target 字段 | 强化 schema grounding |
| `V_plan` | Stage 2 当前结构草案的骨架 | 找相近聚合形状 |
| `V_feature_proxy` | 查询侧 `F1-F17` 代理向量 | 找相近特性组合 |
| `V_c_fingerprint` | 当前候选可恢复时的 C 侧结构指纹 | 只比较结果形状，不比较结果值 |

其中 `V_feature_proxy` 只使用 `F1-F17`，不引入额外编号体系。

### 4.2 Memory 侧记录

SMART 的 memory 侧记录按子集分开构建，再在检索层合并:

#### Synth 记忆

Synth 记忆是 family-aware 的。每条记录可包含:

- canonical 结构计划
- family 内多个 NLQ 变体
- schema 摘要
- memory 侧 `activated_features(F1-F17)`
- 可选 C 侧结构指纹

注意: 这里的 family-aware 只说明记忆组织方式，不等于把 Family 级鲁棒性推广为全 benchmark 默认语义。

#### Real 记忆

Real 记忆是 sample-aware 的，主锚点是 source-anchored 训练资产:

- 一条样本对应一条结构计划和一条主 NLQ
- 同一 source repo / thread 只进入训练侧或评估侧之一
- source 标识只用于 split 控制与可追溯性，不暴露为模型语义输入

#### Hybrid 记忆

Hybrid 记忆同样是 sample-aware 的:

- 记录的是“真实意图骨架映射到合成 schema 后”的单样本结构
- 它的价值在于 bridging，而不是 family 扩展

### 4.3 检索得分与遮罩

SMART 的检索得分是多视图相似度的加权和:

$$
\text{score}(q, e)=\sum_v m_v(q)\, w_v\, \text{sim}\!\left(V_v^q, V_v^e\right)
$$

其中:

- $m_v(q)$ 是可用性遮罩。某视图当前不可用时直接屏蔽，而不是用缺省值硬补。
- $w_v$ 是方法侧可学习或可调的视图权重。
- `V_c_fingerprint` 只有在当前候选可恢复到结构计划时才启用。

这一设计让 SMART 在 query 结构尚不稳定时，优先依赖 `V_nlq / V_schema / V_field`；在 query 结构已稳定时，再让 `V_plan / V_c_fingerprint` 增加权重。

### 4.4 C 侧指纹的边界

如果启用 `V_c_fingerprint`，它只能是结构形状指纹，例如:

- 输出字段签名
- 行数分桶
- 值域类型轮廓
- 排序 / 聚合后的形状标签

它不是答案值缓存，不包含具体输出行，也不把 held-out 结果集写入 memory。C 侧指纹的作用只是帮助检索“结构上像不像”，而不是泄漏“答案是什么”。

### 4.5 难度代理与检索调度

SMART 可以使用 derived difficulty heuristics，但只能来自当前请求可观察对象。典型代理包括:

- schema 规模与层级深度
- Stage 1 的字段歧义程度
- Stage 2 草案的聚合深度
- 时间条件、分组条件、Top-K 条件是否同时出现
- 当前计划是否需要多次局部 patch

这些代理可用于:

- 调整检索 breadth
- 调整 rerank 时结构视图与语义视图的相对权重
- 决定是否启用更保守的 patch 策略

这些代理不替代 `SDT`，也不读取 `is_horizon`。

<a id="05-5"></a>
## 5. A/B/C 优化闭环

> 为何这样组织: Stage 4 是 SMART 最容易越界的地方。必须明确 A/B/C 在方法侧到底是什么、如何用、以及为什么它们不会改写 benchmark gold 逻辑。

### 5.1 A/B/C 三类 pred-side 信号

| 信号 | 可用条件 | 方法侧用途 | 明确禁止 |
|---|---|---|---|
| A | 始终可用 | 看当前候选的执行状态、输出字段、行数分桶、运行错误 | 不把 A 当成 gold 定义 |
| B | 当前候选可恢复到结构计划时 | 做代数一致性与替代表达交叉检查 | 不把 B 的一致性写成 benchmark 通过条件 |
| C | 当前候选可恢复到结构计划时 | 产生结构形状指纹，用于 rerank / patch / stop | 不把 C 的具体结果值喂给模型 |

更具体地说:

- **A 信号**回答“当前候选在目标环境里跑出来像什么”。
- **B 信号**回答“同一结构计划换一条独立表达路径时，结构是否仍自洽”。
- **C 信号**回答“当前候选的符号结构形状是否像目标意图应有的样子”。

### 5.2 闭环流程

Stage 4 的主循环可以写成:

1. 接收 `refined_plan`
2. 生成候选 MQL 并做 A 侧执行
3. 若当前候选可恢复，则计算 B 侧一致性摘要与 C 侧形状指纹
4. 归因错误类型
5. 只 patch 最小必要结构片段
6. 满足停止条件后输出 `final_mql`

SMART 推荐的停止条件是方法侧条件，而不是 benchmark 条件:

- 当前候选语法合法
- A 侧执行不再报错
- 输出字段与行数分桶稳定
- 若 B/C 可用，则结构一致性不再继续改善
- 达到受控重试上限或触发 `needs_review`

### 5.3 常见 patch 类型

SMART 的 patch 应尽量保持局部、可回放、可解释。常见 patch 类型包括:

- **字段绑定 patch**: 把 `$group` / `$project` / `$match` 的字段路径改到正确 schema 路径
- **谓词边界 patch**: 补齐时间窗口、存在性检查、枚举过滤
- **聚合器 patch**: 在 `$sum` / `$avg` / `$count` / `$max` 等之间改正
- **阶段缺失 patch**: 补 `$project`、`$sort`、`$limit`
- **阶段顺序 patch**: 调整 `$project` 与 `$sort` 等可交换或需前置的节点
- **unsupported route patch**: 当当前候选明显超出主线时，把请求标记为 auxiliary side-bucket 或 `needs_review`

### 5.4 `ecommerce_017` 的局部修复示例

对 `ecommerce_017` 的 canonical 主线，最常见的局部错误不是 join，而是:

- 把 `$sum: "$items.price"` 误写成 `$avg: "$items.price"`
- 漏掉 `$project`
- 漏掉 `$limit`
- 遗漏 `paid_at` 的存在性约束

在这个样例里，A/B/C 的作用分工很清楚:

- A 侧会先告诉系统当前结果是否为空、字段是否不对、Top-K 是否失效。
- B 侧在结构可恢复时帮助确认“当前聚合骨架换一条独立表达后是否仍自洽”。
- C 侧不暴露答案值，只给出诸如“应返回 id 字段 + 数值字段、并呈现 Top-K 形状”这样的结构信号。

如果当前候选把 `$sum` 写成 `$avg`，SMART 不需要重生成整条查询，只需对 `$group` 的 accumulator 做局部 patch；如果当前候选漏了 `$project`，SMART 只需在 `$group` 与 `$sort` 之间插入对应节点。这样可以把修复范围稳定压在最小结构片段上。

### 5.5 Benchmark gold 逻辑保持不变

这里必须明确:

- SMART 运行时的 A/B/C 自洽，不等于 benchmark 的 gold 通过。
- benchmark 通过与否仍由 `04` 的 EX / EX-Sym 计算。
- SMART 只能使用 A/B/C 去提高候选质量，不能把“我在运行时看起来自洽”写成新的评测规则。

换句话说，A/B/C 是方法侧优化工具，不是 benchmark 侧裁决器。

### 5.6 Long-Tail 的辅助分流

如果部署侧额外维护 Long-Tail 辅助分流，SMART 只能这样使用它:

- 先检测“当前请求是否超出主线可恢复范围”
- 命中时标记为 auxiliary side-bucket
- 走单独的 handler、单独的日志、单独的运营指标
- 不把该路径的结果混入 benchmark 主线 headline

这条边界的目的，是防止方法文档把 auxiliary robustness 写成 benchmark 主任务的一部分。

<a id="05-6"></a>
## 6. 部署、监控与工程假设

> 为何这样组织: SMART 不是单模型脚本，而是多阶段系统。部署边界、缓存边界、监控边界必须先于性能讨论被写清。

### 6.1 服务分层

SMART 可以按下列逻辑分层部署:

| 组件 | 责任 |
|---|---|
| Grounder | 承载 Stage 1 的 schema grounding |
| Drafter | 承载 Stage 2 的结构草案生成 |
| Retriever | 承载 Stage 3 的向量检索与 rerank |
| Refiner | 承载 Stage 3 的局部结构修复 |
| Optimizer | 承载 Stage 4 的 patch 决策与 stop policy |
| Compiler Adapters | 适配 A/B/C 所需的执行与摘要生成 |
| Trace Store | 存储 patch、检索命中、A/B/C 摘要与 `needs_review` 记录 |

这些组件可以合并部署，也可以分开部署；但无论如何，split-safe memory、compiler adapter 与 trace store 都必须独立可审计。

### 6.2 工程假设

SMART 依赖以下工程假设:

- `schema_markdown`、BSON 归一化与 compiler adapter 与 `03/04` 保持一致。
- `db_id` 对应的 schema 与执行环境通过受控注册表查找，而不是由模型自由拼接。
- Structured plan 与 fAST 的序列化是确定性的，便于 patch 回放与 cache 命中。
- Real 训练资产的 source 信息只用于 split 控制与可追溯性，不作为模型语义输入。
- C 侧若参与检索或优化，只能输出结构指纹，不输出具体结果值。
- 测试侧与 held-out 侧资产不会进入 training memory、warm cache 或 few-shot prompt。

### 6.3 缓存边界

SMART 的缓存重点不是 gold 值缓存，而是方法侧稳定对象缓存:

- schema 指纹与 schema encoder 缓存
- training memory 的多视图向量缓存
- structured plan 的 parse / lower / unparse 缓存
- A/B/C 摘要缓存
- patch trace 缓存

需要避免缓存污染的对象包括:

- 测试侧请求的结果行
- held-out / Horizon 资产
- 审计桶标签
- gold 侧执行结果明文

### 6.4 监控指标

SMART 的监控应围绕“是否仍在 contract-safe 地工作”展开，而不是只看单一准确率。建议至少监控:

- Stage 1 的字段对齐稳定性
- Stage 2 的结构合法率
- Stage 3 的检索来源分布（Synth / Real / Hybrid）
- Stage 3 的 family-aware 命中率与 sample-aware 命中率
- Stage 4 的 patch 次数与 patch 类型分布
- A/B/C 不一致率
- C 侧可用率
- auxiliary side-bucket 命中率
- `needs_review` 占比
- 离线 EX / EX-Sym 漂移，按子集与特性代理切片观察

这些指标的目标不是重写评估，而是帮助系统在部署期尽早发现“split 泄漏、结构退化、memory 偏斜、A/B/C 适配异常”等工程问题。

### 6.5 故障与回退

SMART 的回退策略也必须保持 contract-safe:

- 若 Retriever 不可用，可退化为“无检索修复”，但不得改用测试记忆顶替。
- 若 B 或 C 暂不可用，可只用当前可用摘要做局部修复，但必须在 trace 中记录降级。
- 若请求被判为 unsupported auxiliary side-bucket，可进入 `needs_review`，而不是伪装成主线成功。
- 若执行环境与 schema 注册表不一致，应拒绝请求，而不是让模型盲猜。

<a id="05-7"></a>
## 7. 与 baseline 的比较假设

> 为何这样组织: 这里给的是方法侧期望，而不是 benchmark 结论。所有比较都应最终落回 `04` 的同一套指标协议。

下列判断都是方法侧假设，不是数据结论，也不替代 `04` 的评测结果:

- **相对 Direct Prompting**: SMART 预期会在 schema linking 歧义高、字段空间大的样本上更稳，因为 Stage 1 先显式做 grounding，Stage 2/3/4 再在结构计划上修复，而不是让单次生成同时承担全部任务。
- **相对单次 Fine-tuned 生成**: SMART 预期会在多阶段聚合、时间条件与 Top-K 同时出现的样本上更稳，因为它允许局部 patch，而不必重写整条查询。
- **相对只做语义 RAG 的方案**: SMART 预期会在结构合法性上更稳，因为检索只是修复证据，不直接替代结构计划；真正的候选始终停留在可 patch 的结构对象上。
- **相对 SQL 中转级联方案**: SMART 预期会更适合 MongoDB 原生的文档结构、数组展开、聚合阶段顺序与 NoSQL 式 schema grounding，而不需要先压成 SQL 心智模型。
- **相对不带 A/B/C 的执行反馈方案**: SMART 预期会更容易把错误定位到最小结构片段，因为 A/B/C 给的是三类互补摘要，而不是单一“执行对/错”信号。

这些假设在不同资产上的观测粒度应分开理解:

- 在 Synth 风格 multi-NLQ Family 上，更适合观察 family-aware 的鲁棒性收益。
- 在 Real / Hybrid 上，主比较对象仍应是 sample-level 表现，而不是默认追求 family-level 提升。
- 在所有比较里，最终都应回到 `04` 规定的 EX / EX-Sym / Family / 子集级报告，而不是引入新的 headline 规则。

---

SMART 的定位可以压缩为一句话: 它不是新的 benchmark 契约层，而是一个严格服从 `01-04` 契约、以 structured draft + split-safe retrieval + pred-side A/B/C optimization 为核心的 Text-to-NoSQL 系统设计。它的主线目标是让模型在不读取评估侧特权信号的前提下，更稳定地把 `(NLQ, schema markdown, db_id)` 转成可执行 MQL；任何超出主线的 Long-Tail 兜底，都必须作为 auxiliary side-bucket 单独处置。
