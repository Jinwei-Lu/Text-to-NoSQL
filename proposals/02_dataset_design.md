# MonGen 数据集设计

> 文档定位: 阐述 MonGen 基准的设计目标、规模、记录形式与切分策略
> 目标读者: 团队成员 / 复现者 / 评审
> 前置阅读: [01 任务定义](./01_task_definition.md)
> 最近更新: 2026-04-17

<a id="02-0"></a>
## 0. 摘要

MonGen (Mongo-native Generative Benchmark) 是面向 Text-to-NoSQL 的三轨基准, 由正向合成、真实挖矿、组合重组三个子集互补构成. **MonGen-Synth** 目标 16,000 Sample Families, 覆盖 17 项 MongoDB 原生特性与 6 种 Modeling Style; **MonGen-Real** 目标 3,000-5,000 samples (目标值 4,000), 从开源代码与公开论坛挖矿真实 MQL; **MonGen-Hybrid** 目标 2,000 samples, 把 Real 的真实意图骨架重新落在 Synth 的异质合成库上. 三子集合计约 60,000 对 (NLQ, MQL), train / test 切分比 8:2. 任务形式化定义 (函数签名 `f: (NLQ, S) → MQL` 及其输入输出约束) 见 [01 §1 任务形式化](./01_task_definition.md#01-1).

MonGen 的设计围绕五条核心原则 (§1 展开): (i) **schema-less native** — 同 collection ≥ 3 种 distinct 文档结构, 字段稀疏率 ≥ 30%; (ii) **MongoDB-aligned** — 17 特性触达率每项 ≥ 5%, cMRL 覆盖 30 常用原语, fAST 作为 MongoDB AST 完整镜像兜底长尾算子; (iii) **reverse-cross-checked** — 3-way Verifier 对每条 (NLQ, MQL) 做三家异源 LLM 协议裁决; (iv) **multi-faceted difficulty (IRT)** — 用 pilot 模型相对 pass 率定义 IRT Difficulty, Discrimination ≥ 0.3 入库; (v) **externally-anchored** — 样本至少一侧 (NLQ 或 MQL) 来自外部真实来源, 或经 Hybrid 与真实意图耦合, 防止纯合成造成的 Synth-only 偏差. 前三条保证"数据本身合法", 后两条保证"数据能被客观评估与外部校准".

表示层采用 cMRL + fAST 双层: cMRL (Compact-MRL, 30 原语紧凑 DSL) 是 Sampler 的采样空间, 服务于约束求解与覆盖率硬约束; fAST (Full-AST, MongoDB AST 完整镜像) 是执行真源, 覆盖 `$setWindowFields` / `$densify` / `$fill` / `$facet` 等 cMRL 外算子. Lowering (cMRL → fAST) 是确定性编译, 让紧凑采样空间的约束求解与 MongoDB 完整表达力不冲突; Lifting (fAST → cMRL) 是 Real 轨的尽力逆向, 失败时样本以 fAST-only 形式入库. 切分采用 cross-domain × cross-feature × cross-modeling-style 三维 stratified 切分, 测试集至少 30% 的 "特性 × 建模哲学" 组合在训练期未出现, 度量组合泛化而非记忆.

贯穿全文档的运行示例 (ecommerce_017 主线): 电商域 `ecommerce_017` 逻辑库 / `orders` 集合 / NLQ "Top 3 customers by total paid item spending in 2026." / cMRL 包含 filters (`status=paid`, `paid_at exists`, `paid_at ≥ 2026-01-01`) + unwind (`items`) + group (by `user_id`, sum `items.price` as `total_spent`) + sort desc + limit 3. 该 Sample Family 激活 **F9 Decimal128** / **F10 Date** / **F15 $exists** / **F17 $unwind preserveNull** 四项特性, 采用 **Legacy-drifting** Modeling Style, IRT Difficulty = 0.42 (L3 medium), Discrimination = 0.58. 本例在 §3-1 / §3-2 / §3-3 / §4-1 / §4-2 均会出现, 作为设计语义的具体化参照.

<a id="02-1"></a>
## 1. 设计目标与原则

本节展开五条原则. 每条原则先回答"解决什么问题", 再给出对应的工程化落地指标. 五条原则并非独立: schema-less native 刻画数据分布底色, MongoDB-aligned 界定表达空间边界, reverse-cross-checked 保证单样本正确性, multi-faceted difficulty (IRT) 让难度可区分, externally-anchored 让基准有效性向真实场景外推. 失去任一条, 基准都只是"看上去很完整": 缺 schema-less native 则退化为关系表嵌套; 缺 MongoDB-aligned 则覆盖面不足以支持统计推断; 缺 reverse-cross-checked 则样本正确性依赖单 verifier 的点估计; 缺 multi-faceted difficulty (IRT) 则难度分层依赖未校准的经验权重; 缺 externally-anchored 则整条管线是自循环, 评估结果无法外推.

<a id="02-1-1"></a>
### 1.1 schema-less native

> 为何这样设计: 现有 Text-to-NoSQL 数据多由关系表简单嵌套化而来, 同 collection 内文档结构高度同质, 模型只需学"表 → collection"的字段映射即可作弊式通过评估, 无法触达 NoSQL 真正的难点 — 字段稀疏、类型多态、键集合演化.

落地指标:

- 同一 collection 内必须出现 **≥ 3 种 distinct 文档结构** (字段集合差异 + 类型差异), 由 Schema Exporter 通过 MinHash 聚类验证; 若某 collection 仅出现 ≤ 2 种结构, 该库不入基准.
- 全库平均字段稀疏率 ≥ 30% (任一文档对全集字段集的平均缺失率). 若全集字段数为 45, 平均每文档缺失 ≥ 13.5 个字段.
- 异质来源仅允许 Document Accreter 的事件流沉积: 事件模板 (create / update / status_change / refund / cancel / migrate_schema 等) 按概率触发, 累积沉积出异质形态. 禁止由扁平表加嵌套人工拼出, 防止"先有目标结构再反向造数据"的逆向拟合.
- 字段稀疏率非均匀: 配合事件模板, "长尾字段" (例如 `shipping.location` 仅在含物流的订单出现) 稀疏率可达 60% - 70%, "主干字段" (例如 `_id` / `status`) 稀疏率接近 0%. 非均匀分布更贴近真实 workload, 逼模型学会针对字段类型选择不同的防御性策略 (`$exists` / `$type` / `$ifNull`).

该原则只对 MonGen-Synth 做硬约束 (220 库均需满足); MonGen-Real 受真实代码库限制, 多数库也天然满足, 少量不满足的库仍允许入基准但在 `provenance.desensitized` 中标注字段完整度. 对由 Document Accreter 直接生成的库, 不满足则该库不入 Synth.

<a id="02-1-2"></a>
### 1.2 MongoDB-aligned

> 为何这样设计: 评估基准若只覆盖 `find` + `$match` + `$project`, 就无法度量模型对 MongoDB 真正生产特性的掌握; 反过来, 若特性铺得过散又无重点, 单项触达率不足以做统计推断. MonGen 用 17 项 checklist 锁定边界, 对每项设最低触达率 5%, 并通过 cMRL + fAST 双层表达让"紧凑采样"与"完整表达力"两个看似对立的目标共存.

落地指标 (三层):

- **cMRL 紧凑 DSL** 覆盖常用 30 原语 (filter 12 / projection 5 / grouping 8 / join 3 / array 5 / sort-page 4; 同类合并后合计 30 个语义原语), 服务于 Sampler 的紧凑采样与约束求解, 保证采样空间足够小以支持覆盖率硬约束. 展开:
  - **filter 12**: eq / ne / gt / gte / lt / lte / in / nin / exists / type / regex / size
  - **projection 5**: include / exclude / alias / computed_expr / array_slice
  - **grouping 8**: sum / avg / min / max / count / first / last / push
  - **join 3**: lookup_simple / lookup_with_pipeline / graph_lookup
  - **array 5**: unwind / filter_array / map_array / reduce_array / slice_array
  - **sort-page 4**: sort_single / sort_multi / skip / limit
- **fAST 作为 MongoDB AST 完整镜像**: 除了承担 cMRL 的 Lowering 目标, fAST 兜底 `$setWindowFields` / `$densify` / `$fill` / `$facet` / `$merge` 等 cMRL 原语表外的长尾算子, 这些算子以 fAST-only 样本形式进入 MonGen-Real / Hybrid, 杜绝结构性盲区. fAST 本身是一棵以 pipeline stage 为节点、`BSONExpr` 为叶的树, 可直接 unparse 为 `db.coll.aggregate([...])` 字符串 — 因此 fAST 不仅是一个中间表示, 也是执行与 Lowering 的共同真源: 任何模型输出最终都会被 unparse 为 fAST 后再做结构 / 执行比对.
- **新算子流入机制**: 每季度扫描 MongoDB Release Note (官方 changelog + What's New 页), 新算子先以 fAST-only 样本进入基准; 观察使用稳定后再由 Lifting 支持并编入 cMRL. 此节奏让 cMRL 始终保持"语义必要最小化"不臃肿, 同时基准不因 MongoDB 版本推进而出现覆盖缺口.

17 特性 checklist (F1-F17) 按类别概览:

| 类别 | 特性 ID 区间 | 关键覆盖物 |
|---|---|---|
| Schema 异质 | F1-F4 | 稀疏字段、多态类型、可选嵌套、数组元素多态 |
| 动态键 | F5-F7 | 日期 / 租户作 key、`$objectToArray` 转映射、key 集合演化 |
| 原生 BSON | F8-F11 | `ObjectId`、`Decimal128`、`Date`、`GeoJSON Point / Polygon` |
| 文档形态 | F12-F14 | 嵌入 vs 引用并存、多 schema 共存 (schema version drift)、大数组 / bucket pattern |
| 查询算子 | F15-F17 | `$exists` / `$type` 存在性、`$lookup` with pipeline + `$graphLookup`、`$unwind` + preserveNullAndEmptyArrays |

每项预期触达率见 [§2-6](#02-2-6). cMRL 主干算子与 fAST-only 长尾算子的联合频次表见 [§2-5](#02-2-5).

<a id="02-1-3"></a>
### 1.3 reverse-cross-checked

> 为何改名: "reverse-verified" 暗含"证明正确"的强承诺, 实际做到的是多个独立复算之间的"交叉一致性", 而非形式化正确性证明. 改为 "reverse-cross-checked" 更准确反映 **3-way Verifier** 的工作原理 — 三家异源 LLM 独立从 NLQ 重构 MQL, 以多数决 + 不一致时 abstain 取代单 verifier 的点估计, 保留对"三家同错"风险的显式承认. NLQ → MQL 的正确性因此由 3-way Verifier 在执行验证之外再做一道**概率交叉验证**.

落地指标:

- **3-way Verifier 结构**: 三家异源 LLM, 至少 3 个不同供应商 + 2 个不同预训练语料基座 (避免"同源同错"盲区). 三家独立从 NLQ 重构 MQL, 在同一 MongoDB 实例上执行, 比对结果集 (行级结构一致, 排序无关时稳定排序后比对; 允许少量 BSON 类型抖动, 例如 `int` vs `long`).
- **裁决规则** (严格):
  - **3/3 pass** → 直接入库
  - **2/3 probable-pass** → 入库但标记, 进入 Active-Learning 人工复核队列
  - **1/3** → 人工仲裁 (裁决员决定入库或丢弃)
  - **0/3 fail** → 丢弃
  - **三者两两互不一致 ambiguous** → 归入 **Ambiguous / Abstain Bucket**, 由 Intent Mutator 重写 NLQ 后再送验
- **Active-Learning Human Loop**: 按 probable-pass 与 Ambiguous / Abstain Bucket 两桶的 entropy 排序, 优先复核边界样本. 目标 Active-Learning 错误率 < 2%, 该指标是基准发布门槛.
- **异源组合规范**: pilot 实测表明, 若三家 verifier 均来自同一 base family (同 tokenizer + 同预训练语料), "同错"率可达 8%; 若强制不同 tokenizer + 不同预训练语料, "同错"率降至 1.5% 以下. 这是"至少 3 个不同供应商 + 2 个不同预训练语料基座"规范的量化依据.
- **结果集比对容忍**: 行级结构一致的定义是"排序无关时对两个结果集做 multiset 比较; 排序敏感时按 canonical 字段字典序 tuple 化后比较". BSON 类型抖动允许: `int ↔ long ↔ double` 在数值相等时视为一致, `Date ↔ ISO-8601 string` 在毫秒级相等时视为一致, 其余类型抖动 (例如 `string` ↔ `ObjectId`) 视为不一致.

该机制的详细流程 (阈值、裁决器实现、Ambiguous 桶的二次送验规则) 见 [03 §7-5 3-way Reverse Verifier](./03_dataset_construction.md#03-7-5).

<a id="02-1-4"></a>
### 1.4 multi-faceted difficulty (IRT)

> 为何改用 IRT: 经验加权公式 (pipeline 深度 / 特性数 / 歧义度等) 的权重由人工经验设定, 其与人类认知难度、与新模型实际过样本概率的相关性均未验证. 本设计采用 **IRT (Item Response Theory, 项目反应理论)**: 用一组 pilot 模型的相对 pass 率直接定义 **IRT Difficulty**, 以模型能力与 pass 结果的相关度定义 **Discrimination**; 两者均是可观测外部量, 不依赖人工权重, 且可随新模型加入 pilot 集合而自校准.

落地流程:

- **pilot 模型集合**: 8-12 个, 覆盖小 LM / 中 LLM / 大 LLM + 至少 3 个不同家族 (不同参数规模 + 不同供应商). 每条样本在 pilot 集合上跑一次零样本推理, 记录 pass / fail 向量.
- **难度公式**: `IRT Difficulty(s) = 1 - 平均 pass 率`, 值域 `[0, 1]`.
- **区分度公式**: `Discrimination(s) = corr(模型能力, pass)`, 用模型在 pilot 全体样本上的平均 pass 率作为"模型能力"估计, 与本样本 pass 结果的相关度作为 Discrimination 值.
- **入库门槛**: **Discrimination ≥ 0.3** 才纳入最终基准. Discrimination 过低意味着"所有模型都过"或"所有模型都不过", 对模型能力排序无区分价值, 剔除.

目标难度分布 (按 IRT Difficulty):

| 分桶 | IRT Difficulty 区间 | 占比 |
|---|---|---|
| L1 | `[0.0, 0.2)` | 20% |
| L2 | `[0.2, 0.4)` | 20% |
| L3 | `[0.4, 0.6)` | 20% |
| L4 | `[0.6, 0.8)` | 20% |
| L5 | `[0.8, 1.0]` | 20% |

经验加权的结构难度分 (pipeline 深度 / 特性数 / 歧义度等) 仍作为附加元数据落盘 (`irt.legacy_structural`), 但**不再是主难度指标**. 保留结构分的目的: (a) pilot 跑完前作为临时分层支撑采样权重调整; (b) 留给后续做"结构难度 vs IRT Difficulty"的相关性分析, 反过来回推经验公式的有效性.

**为何 Discrimination ≥ 0.3 是合理门槛**: 当 pilot 集合 |M| = 10 时, 若 Discrimination = 0.3 意味着"能力最强的 3 个模型"与"能力最弱的 3 个模型"在该样本 pass 率上至少约 30% 绝对差值, 足以让样本在能力排序中贡献信息; 若 Discrimination < 0.3, 样本基本上"对强弱模型一视同仁", 加入基准只会增加噪声. 该门槛来自 IRT 教科书经验值, 具体校准值在 pilot 实验后会重新检视.

ecommerce_017 主线的 IRT 分数: IRT Difficulty = 0.42 (L3 bucket, medium), Discrimination = 0.58. 在 pilot 的 10 个模型中, 能力最强的 3 个全部 pass, 能力中间的 4 个中 2 个 pass, 能力最弱的 3 个全部 fail — 这是 Discrimination ≈ 0.6 量级的典型分布.

评分流程 (pilot 选型、运行预算、评分固化窗口、重校准规则) 见 [03 §8-1 IRT 难度评分](./03_dataset_construction.md#03-8-1).

<a id="02-1-5"></a>
### 1.5 externally-anchored

> 为何新增: 纯合成管线 (Sampler 生成 cMRL → Lowering 出 MQL → 3-way Verifier 自检) 是**全链路自循环**, 没有外部真值来源. Sampler 生成什么意图就有什么意图, 不保证覆盖真实用户的查询分布. 若模型在 MonGen-Synth 上 EX = 90%, 无法判定这是"真的解决了 Text-to-NoSQL 任务"还是"模型学会了 MonGen-Synth 的意图风格与 Sampler 偏置". 外部锚点是打破自循环的唯一手段.

落地 (核心命题): **每条 Sample Family 必须满足"至少一侧 (NLQ 或 MQL) 来自外部真实来源, 或经 Hybrid 轨与真实意图耦合"**. 由此产生两个外部子集:

- **MonGen-Real (目标 4,000 samples, 允许 3,000-5,000 区间)**: 从公开来源挖矿真实 MQL, 反向生成 NLQ. 来源包括:
  - **GitHub 公开仓库** (MIT / Apache, star ≥ 100) 的 `db.coll.aggregate(...)` / `db.coll.find(...)` 调用
  - **Stack Overflow / MongoDB Community Forums 问答对** (NLQ 取自问题标题 / 正文, gold MQL 取自最高票答案)
  - **开源业务系统** (Odoo / Saleor / Medusa 等) 的 webhook 与 reporting 代码
- **MonGen-Hybrid (2,000 samples)**: 把 MonGen-Real 的真实意图骨架重新 Lowering 到 MonGen-Synth 的异质库上, 测组合泛化 (真实意图 × 合成异质库). 此子集的意义是分离"意图熟悉度"与"库熟悉度"两个因素: 若模型在 MonGen-Synth 上 EX 高但在 MonGen-Hybrid 上骤降, 说明模型依赖"意图 × 库"的联合记忆而非真正的组合推理.
- **报告要求**: EX 指标三子集分列 (EX(Synth) / EX(Real) / EX(Hybrid)), 不做加权合并, 避免 MonGen-Synth 规模主导而掩盖 MonGen-Real 的真实困难. 在论文 / 报告中不允许用一个综合 EX 数字作为 headline, 必须以三元组展示.
- **外部有效性与内部有效性的权衡**: MonGen-Real 样本数受限于公开代码供给, 且真实代码中的 MQL 质量参差 (例如忘记 `$match` 防御性过滤), 因此 MonGen-Real 的 3-way Verifier 通过率会低于 MonGen-Synth. 本设计接受这一事实: 外部有效性的获取必然以牺牲部分内部自洽性为代价.
- **三轨缺一不可**: 若只有 MonGen-Real, 覆盖面有限且无法保证 17 特性硬约束; 若只有 MonGen-Synth, 全链路自循环; 若只有 MonGen-Hybrid, 无法独立于前两者. 三子集互补: MonGen-Synth 提供受控覆盖率保证, MonGen-Real 提供真实分布锚点, MonGen-Hybrid 检验"意图 × 库"的组合泛化.

<a id="02-2"></a>
## 2. 数据规模与统计

> 为何这样设计: 规模指标 (库数、样本数、特性触达率) 决定 benchmark 的统计置信度与表征能力. 本章表格**按子集分列**, 不做一次性汇总表, 原因是三子集的来源、保证、分布边界各不相同; 混表后会让读者难以识别哪一项指标属于哪一子集, 也无法支撑"跨子集对比"这类分析.

<a id="02-2-1"></a>
### 2.1 三子集规模总表

| 子集 | Sample Family 数 | (NLQ, MQL) pairs | 平均 Family size | 逻辑库数 | 来源 | 保证 |
|---|---|---|---|---|---|---|
| MonGen-Synth | 16,000 | ~48,000 | 1 canonical + K 个 Intent Variant (K=3-5, 均值 3) | 220 | Sampler + Lowering + Intent Mutator | 17 特性 & 建模哲学覆盖硬约束; cMRL ↔ fAST 双实现自洽 |
| MonGen-Real | 每族 1 canonical + K variants, 目标 4,000 samples (3,000-5,000) | ~12,000 | 1 canonical + Paraphraser 生成的 ~2 个 NLQ 同义表达 (无结构变异) | 未限 (来自真实代码库) | GitHub + Stack Overflow + MongoDB Forums mining | 外部有效性; 真实意图分布 |
| MonGen-Hybrid | 2,000 | ~6,000 | 1 canonical (复用 Real 意图) + Intent Mutator 在合成库上重派生的 ~2 个 variant | 复用 Synth 220 库 | Real 意图骨架 × Synth 异质库 | 组合泛化 |
| 合计 | ~22,000 | ~60,000 | — | — | — | — |

说明: 总体 (NLQ, MQL) 约 60,000. train / test 切分比统一为 8:2. 三子集的 train / test 并非各自独立切分再汇总, 跨子集一致性规则见 [§5-3](#02-5-3).

NLQ-MQL pair 的计数口径: 每个 canonical 对应 5+ 条等义 NLQ 但只算 1 对 (NLQ 集合, MQL) pair; 每个 Intent Variant 对应 3+ 条等义 NLQ 也只算 1 对. 若按"NLQ 单句数 × MQL 数"口径, 总数约 3 倍 (约 18 万 NLQ 单句). 基准在报告外部规模时用 "pair" 口径; 在训练数据摊平时用"单句"口径.

<a id="02-2-2"></a>
### 2.2 库与集合层 (Synth)

| 维度 | 预期数值 | 说明 |
|---|---|---|
| 逻辑库数 | **220 (仅 MonGen-Synth)** | 由 Document Accreter 按业务剧本独立沉积 |
| 业务域数 | **10** | 电商 / IoT / 日志 / CMS / 社交 / 金融 / 医疗 / 游戏 / SaaS / 教育 |
| 每库 collection 数 | 2 - 10 (均值 ≈ 4.3, 中位 5) | 与业务剧本复杂度相关 |
| 每库平均文档数 | 1,100 | 涵盖热点与长尾 |
| 文档数范围 | 50 - 50,000 | 反映真实生产分布的长尾 |
| 同 collection 内 distinct 文档结构数 | 4 - 7 (中位 5) | schema-less 异质度的核心刻画 |
| 平均字段数 (库内) | 45 | 含嵌套字段递归展开 |
| 字段稀疏率 | 35% | 任一文档对全集字段的平均缺失率 |

设计论证: 文档数与字段数的范围刻意拉开两个数量级, 是为了让评估能区分"样本受限 / 字段稀疏"与"样本充足 / 字段稠密"两种 workload 下的模型表现; 同 collection 内 distinct 结构数的中位 5 是 [§1-1 schema-less native](#02-1-1) 硬约束的实例化. MonGen-Real 来自真实代码库, 库数不受约束; MonGen-Hybrid 直接复用 MonGen-Synth 的 220 个逻辑库, 不额外生成.

**总文档数估算**: 220 × 1,100 ≈ 242,000 文档, 按平均每文档 1KB 估算, 磁盘占用约 240MB JSON (未压缩); gzip 压缩后约 60MB. 该规模适合在单机 MongoDB 实例上热加载, 不需要分布式.

**数据生成成本**: 每库的 Document Accreter 以事件流驱动, 单库合成耗时 30s - 5min (取决于事件数与复杂度), 220 库全量合成约 2-4 小时. 该成本允许在每次基准升级时全量重建, 无需增量更新.

<a id="02-2-3"></a>
### 2.3 业务域分布 (Synth)

| 业务域 | 预期占比 | 备注 |
|---|---|---|
| 电商 | 14% | 订单 / 库存 / 评价 / 推广位 |
| IoT | 13% | 时序 + 设备影子 + 告警 |
| 日志 | 13% | 应用日志 / 审计日志 / 异常 |
| 社交 | 12% | 用户 / 关系 / 动态 / 私信 |
| 金融 | 12% | 账户 / 交易 / 风控 / 对账 |
| SaaS | 8% | 多租户 + 订阅 + 配额 |
| CMS | 7% | 文档 / 分类 / 评论 / 修订 |
| 医疗 | 7% | 病例 / 检验 / 处方 |
| 游戏 | 7% | 角色 / 物品 / 战局 / 商城 |
| 教育 | 7% | 课程 / 学员 / 作业 / 评测 |
| 合计 | 100% | |

设计论证: 5 个高占比业务域 (电商 / IoT / 日志 / 社交 / 金融) 共占 64%, 集中刻画"数据量大 + 形态多样"的主流 MongoDB workload; 其余 5 个业务域均匀铺开 7% - 8%, 保证长尾领域至少有数百 Sample Family 支撑分项指标统计. ecommerce_017 主线即属电商域, 占本域约 1/31 份额.

业务域比例的选定综合三个信号: (a) MongoDB 官方 case study 公开的客户业务分布, (b) Stack Overflow `mongodb` 标签下问答的业务关键词频次, (c) GitHub 公开代码中提到 MongoDB 的仓库按业务分类的分布. 三路信号加权平均后得到上表; 比例偏差将通过 [§Y](#02-Y) 的 TODO 项在正式发布前对齐 MonGen-Real 实测分布.

<a id="02-2-4"></a>
### 2.4 建模哲学分布 (Synth)

> 为何这样设计: 仅按业务域划分不足以防止"模型记住一个架构就跨库泛化". 同一业务域 (如电商) 在真实生产中会出现 Normalized / Embedded / Bucket 等多种 Modeling Style, 甚至同库内多种混用. 若基准在电商域固定使用 Embedded, 模型只需记住"电商 = Embedded"即可通过, 没有学到跨 Modeling Style 推理的能力.

MonGen-Synth 将 6 种 Modeling Style 分布如下:

| Modeling Style | 特点 | 典型触发特性 | Synth 占比 |
|---|---|---|---|
| Normalized | 小文档 + 大量 `$lookup` | F16 偏高 | 17% |
| Embedded | 大文档 + 嵌入数组 | F4 / F14 偏高 | 17% |
| Bucket | 时序分桶 + 预聚合 | F14 / F10 偏高 | 17% |
| Polyglot | 上述三种混用 | F2 / F12 偏高 | 17% |
| Legacy-drifting | 多年演化遗留, 多 schema 共存 | F1 / F13 偏高 | 17% |
| Tenant-sharded | 租户 ID 作动态键 | F5 / F7 偏高 | 15% |

合计 100% (最后一行取 15% 让总和成立). 业务域 × Modeling Style 是 10 × 6 = 60 个单元格, 每格至少 150 families 保证单格统计显著. ecommerce_017 主线采用 Legacy-drifting 建模哲学: `schema_version` 字段同时存在 1 与 3 两个取值, 并有 partial 形态的瘦身文档并存, 详见 [§4-1](#02-4-1).

60 个单元格不是所有都等同重要: 某些 (业务域, Modeling Style) 组合在真实世界几乎不存在 (如"医疗 + Tenant-sharded"), 在基准中保留最小样本量 (150) 即可; 另一些组合 (如 "IoT + Bucket", "电商 + Legacy-drifting") 是高频真实场景, 可上调到 400 - 500 families 以提供更密的统计支持.

具体配额表与分配机制见 §2-4 对应的 Modeling Style Skew 实现 (链接在 [§4-2](#02-4-2) 段尾给出).

<a id="02-2-5"></a>
### 2.5 算子频次 (含 fAST-only 长尾)

设计论证: cMRL 外的长尾算子 (`$setWindowFields` / `$densify` / `$fill` / `$facet` / 复杂 `$expr` 嵌套) 必须显式承担一定比例, 否则 fAST 的"MongoDB 完整镜像"承诺无法在基准里体现. 下表把 cMRL 主干与 fAST-only 长尾并列, 并标注每项所属层与 fAST-only 长尾占比栏.

| 算子 / 类别 | 层 | Synth 预期占比 | fAST-only 长尾占比 (Real/Hybrid 内部估计) |
|---|---|---|---|
| `aggregate` | cMRL | 78% | — |
| `find` | cMRL | 22% | — |
| `$match` | cMRL | 62% | — |
| `$project` | cMRL | 85% | — |
| `$sort` | cMRL | 28% | — |
| `$limit` | cMRL | 20% | — |
| `$group` | cMRL | 32% | — |
| `$unwind` | cMRL | 38% | — |
| `$lookup` | cMRL | 18% | — |
| `$graphLookup` | cMRL | 5% | 提升至 ~7% (Real 复杂图查询偏多) |
| `$objectToArray` | cMRL | 10% | — |
| `$exists` | cMRL | 12% | — |
| `$type` | cMRL | 8% | — |
| `$setWindowFields` | **fAST-only 长尾** | <1% | 3% (Real 金融 / 时序场景常用) |
| `$densify` / `$fill` | **fAST-only 长尾** | <1% | 2% (Real 时序补齐) |
| `$facet` | **fAST-only 长尾** | <1% | 4% (Real 报表多视图) |
| `$bucketAuto` | **fAST-only 长尾** | <1% | 1.5% |
| 复杂 `$expr` 嵌套 (`$let` / `$switch` / `$reduce` 深组合) | **fAST-only 长尾** | 2% | 5% |
| `$rank` / `$median` / `$percentile` (MongoDB 7.0+) | **fAST-only 长尾, 分阶段进入 cMRL** | 1% | 2% |
| `$merge` / `$out` | **不入基准 (只读约束)** | 0% | 0% |

设计论证:

- cMRL 外长尾算子合计在 MonGen-Synth 内仅占 <5% (以 fAST-only 样本形式), 不压低 cMRL 主干算子的覆盖目标; 但在 MonGen-Real / MonGen-Hybrid 中, fAST-only 长尾算子合计占 5% - 10%, 真正让 fAST 撑起"MongoDB 完整表达力"的承诺.
- `$objectToArray` / `$exists` / `$type` 三个算子合计 ≥ 30% 触达, 是对 Text-to-SQL 基准缺失的"schema-less 算子空白"的直接补足.
- `$graphLookup` 显式列出 5%, 保证递归图查询不被边缘化.
- `$merge` / `$out` 列入"不入基准"是因为它们带写副作用, 与 Text-to-NoSQL 任务定义的只读约束相违; 模型若在推理中输出这两个算子, 评估侧判定为越权 fail.

算子频次的总和不等于 100%: 这些百分比是"出现至少一次"的频次, 而非"唯一算子"的占比. 一个 pipeline 往往包含多个算子 (例如 ecommerce_017 的 MQL 同时用到 `$match` / `$unwind` / `$group` / `$project` / `$sort` / `$limit`), 每个都计一次, 故行列合计可远超 100%. 这是"算子覆盖率"而非"算子占比"的标准表达.

**为何把 cMRL 与 fAST 的覆盖目标分开**: 若把它们合并成一张表, 会产生 "cMRL 也能表达 `$setWindowFields`?" 的歧义 — 实际 cMRL 只覆盖 30 常用原语, 而 fAST 覆盖 MongoDB 全部 100+ stage. 分层表格明确了每个算子的归属, 避免在研究论文中出现 "MonGen 宣称 30 原语但实际上 `$facet` 从未真正进入 cMRL" 的误读.

<a id="02-2-6"></a>
### 2.6 17 特性触达率 (F1-F17)

| ID | 特性 | 预期占比 | 设计动机 |
|---|---|---|---|
| F1 | 稀疏字段 sparse | 45% | schema-less 最常见表现, 高频是基础 |
| F2 | 多态类型 polymorphic | 22% | 字段类型跨 schema 迁移 |
| F3 | 可选嵌套层 optional nesting | 35% | 子文档存在与否 |
| F4 | 数组元素多态 array-of-union | 14% | 同数组不同元素 schema |
| F5 | 日期 / 租户作 key | 12% | 时间分桶 / 多租户分区 |
| F6 | `$objectToArray` 可转映射 | 10% | 动态键查询的核心算子 |
| F7 | key 集合随文档演化 | 8% | tag map / 配置 map |
| F8 | `ObjectId` | 60% | MongoDB 默认主键, 普遍存在 |
| F9 | `Decimal128` | 11% | 金融场景必备 |
| F10 | `Date` | 55% | 时间字段在大多数业务出现 |
| F11 | `GeoJSON Point / Polygon` | 7% | 地理查询 (`$geoNear` / `$geoWithin`) |
| F12 | 嵌入 vs 引用并存 | 30% | 同库混用模式 |
| F13 | 多 schema 共存 (schema version drift) | 25% | 长期演化的真实表现 |
| F14 | 大数组 / bucket pattern | 9% | 时序分桶 / 评论分桶 |
| F15 | `$exists` / `$type` 存在性 | 18% | 异质字段过滤的核心 |
| F16 | `$lookup` with pipeline + `$graphLookup` | 16% | 复杂跨集合 + 递归图查询 |
| F17 | `$unwind` + preserveNullAndEmptyArrays | 13% | 处理空数组的关键开关 |

说明: MonGen-Synth 硬约束**每项触达率 ≥ 5%** (即每项至少 800 families 实例化); MonGen-Real 随缘 (不约束, 允许真实分布稀疏某些特性, 反映外部分布本色).

设计论证: F1 / F8 / F10 三项远高于其他特性, 因为它们是 MongoDB 几乎所有库都会出现的"底色", 不刻意压低反而更贴近真实分布; F11 / F7 / F14 等小众特性保持 7% - 9%, 既稀缺到能形成评估难点, 又不至于样本不足而影响统计显著性.

**Cross-feature 二元组覆盖 ≥ 60%, 三元组覆盖 ≥ 30%**: 17 特性两两组合有 C(17, 2) = 136 个二元组, 其中至少 82 个要在 MonGen-Synth 中同时出现至少 10 次; C(17, 3) = 680 个三元组, 至少 204 个需覆盖. 该约束防止"每个特性都单独出现但从不组合"这种退化分布, 因为模型对"多特性同时激活"的查询的泛化能力才是 Text-to-NoSQL 的关键.

**MonGen-Real 的覆盖差异**: MonGen-Real 不做硬约束, 但发布时会与上表做 χ² 对齐检验, 偏差方向会作为"真实世界与预期分布的 gap 报告"公开, 让后续研究能针对性修正.

<a id="02-2-7"></a>
### 2.7 MonGen-Real 规模与来源

| 来源 | 预期样本数 | 说明 | fAST-only 长尾供给 |
|---|---|---|---|
| GitHub 公开仓库 (MIT / Apache, star ≥ 100) 的 aggregate / find 调用 | ~2,000 | 自动挖矿 + fAST 解析 + 脱敏 | 主供给 `$setWindowFields` / `$facet` / 复杂 `$expr` |
| Stack Overflow `mongodb` 标签问答对 (CC BY-SA, 正确归属) | ~1,200 | 标题 / 正文作为 NLQ, 最高票答案的 MQL 作为 gold | `$bucketAuto` / `$graphLookup` 长尾 |
| MongoDB Community Forums | ~500 | 同 Stack Overflow 策略 | `$densify` / `$fill` 时序补齐 |
| Odoo / Saleor / Medusa / 其他开源业务系统的 reporting 代码 | ~300 | 手工挑选 + 脱敏 | 复杂业务级 `$facet` 多视图 |
| 合计 | **~4,000 (目标值 3,000-5,000 区间)** | — | MonGen-Real 是 fAST-only 长尾算子的主要供给源 |

脱敏流程: 常量字符串替换 (公司名 / 用户邮箱 / API key) + collection 与字段名**语义保留但哈希混淆** (防止对特定产品的 de-anonymize).

**挖矿的通过率估计**: 仓库筛选 → 调用点抽取步骤预期保留约 50% 原始候选 (剩余 50% 是非查询代码、测试代码、或过于简单的调用); MQL 解析步骤预期保留 80% (剩余 20% 语法异常或用动态字符串拼 MQL); 3-way Verifier 步骤因真实代码中可能含隐式业务逻辑 (如"用户自然缺失 paid 字段的订单默认算未支付"), 预期仅 65% - 70% 通过. 三层级联乘, 最终通过率约 26% - 28%; 原始候选需挖约 15,000 条才能产出 ~4,000 入库样本.

**脱敏的语义保留原则**: 字段名 `user_email` 哈希为 `f_7a3e2`, 但全数据集中的所有 `user_email` 哈希为同一值, 保持跨样本一致; collection 名类似处理. 这样做是为了: (a) 保护具体产品的可识别性, (b) 模型仍能从上下文 (如同一 collection 的其他字段) 推断出字段语义. 脱敏前后的映射表以 salted-hash 形式保存, 由 legal 合规团队持有, 非研究人员无权访问.

**许可证与归属维护**: GitHub MIT / Apache 代码的使用需保留原始版权声明, 数据集 README 中维护 attribution 表 (格式: `{repo_url, license, commit_sha, file_path}`); Stack Overflow CC BY-SA 要求署名作者与答案 URL, 按答案级颗粒度维护; MongoDB Forums 遵循其 Terms of Service, 引用范围限定在公开可见的问答.

<a id="02-3"></a>
## 3. 数据记录 schema

> 为何这样设计: MonGen 的记录单位是 **Sample Family** (一族关联样本) 而非单条 (NLQ, MQL) 对. 原因: Intent Variant (negation / omission / coreference / jargon / composition / expression_rephrase) 必须与 canonical 共享 db_id 与执行环境才能做可比较的意图稳健性评估, 若拆成独立记录, 就失去了"同族内谁对谁错"的诊断价值. 字段集是"训练监督 + 执行验证 + 难度分层 + IRT 校准 + 来源溯源"五项需求的最小完备集.

<a id="02-3-1"></a>
### 3.1 Sample Family 结构

Sample Family 由 1 个 canonical + K 个 Intent Variant 构成 (K = 3 - 5). 顶层字段:

| 字段 | 类型 | 说明 |
|---|---|---|
| `family_id` | int | 全局唯一, 同族 canonical 与 Intent Variant 共享 |
| `subset` | enum | `"synth"` / `"real"` / `"hybrid"` |
| `db_id` | str | 逻辑库 id, 对应 `mongodb_data/{db_id}.json` |
| `modeling_style` | enum | 6 种 Modeling Style 之一 (Synth 必填; Real / Hybrid 可选) |
| `canonical` | object | 主样本 (见 [§3-2](#02-3-2)) |
| `intent_variants` | list[object] | Intent Variant 列表 (见 [§3-3](#02-3-3)) |
| `irt` | object | IRT 评分 (见 [§3-4](#02-3-4)) |
| `provenance` | object | 来源元数据 (见 [§3-5](#02-3-5)) |

**为何用 Sample Family 而非单 pair**: 若每对 (NLQ, MQL) 都是独立记录, 做 Intent Variant 时需要重复落 db_id / modeling_style / 数据库 snapshot 等冗余信息, 存储膨胀且关联分析困难. Sample Family 把同意图的 canonical + Intent Variant 聚在一起, 读写时以 family 为事务单元, 评估时也以 family 为诊断单元 (例如"该 family 的 5 个 Intent Variant 中有几个 pass"), 比单 pair 的聚合分析更直接.

**为何要求 K ≥ 3 变体**: Intent Variant 少于 3 个时, 不足以形成"意图变形稳健性"的统计诊断. 若某 family 的 4 个 Intent Variant 中 model 在 3 个 pass 但在 negation variant 上 fail, 就能定位"该 model 对否定语义弱"; 若只有 1 个 Intent Variant, 无从比较.

**Sample Family 的哲学**: 同一意图的多个表达绑在一起, 评估时可产出"family-level EX"(family 内所有 pair 都 pass 才算 family pass) 与"pair-level EX"(每 pair 独立统计) 两种指标. Family-level 更严格, 近似"模型对某意图的所有变形都能正确处理才真正掌握"; pair-level 宽松, 保留单 pair 的信息量. 两种指标都报告, 供不同研究问题使用.

Sample Family 的 JSON 骨架 (省略部分细节):

```json
{
  "family_id": 17031,
  "subset": "synth",
  "db_id": "ecommerce_017",
  "modeling_style": "Legacy-drifting",
  "canonical": {
    "cmrl": { "...": "见 §3-2" },
    "fast": { "...": "见 §3-2" },
    "mql": "db.orders.aggregate([ ... ])",
    "nlq": "Top 3 customers by total paid item spending in 2026.",
    "schema_ref": "ecommerce_017#orders",
    "exec_result_head": [ { "user_id": "u_10472", "total_spent": "18423.55" } ],
    "exec_result_hash": "sha256:7a2e...d31c",
    "skeleton": { "F9": true, "F10": true, "F15": true, "F17": true }
  },
  "intent_variants": [
    { "variant_id": "17031-var01", "mutation_type": "negation", "nlq": "...", "delta_from_canonical": "append $ne refund_request", "exec_result_hash": "sha256:1f9b...4ae0" },
    { "variant_id": "17031-var02", "mutation_type": "omission", "nlq": "...", "delta_from_canonical": "drop year filter", "exec_result_hash": "sha256:c4a5...ed12" },
    { "variant_id": "17031-var03", "mutation_type": "jargon", "nlq": "...", "delta_from_canonical": "paraphrase to 'converted users'", "exec_result_hash": "sha256:9e11...a0b4" },
    { "variant_id": "17031-var04", "mutation_type": "coreference", "nlq": "...", "delta_from_canonical": "'those customers' anaphora", "exec_result_hash": "sha256:1b0c...7d59" },
    { "variant_id": "17031-var05", "mutation_type": "composition", "nlq": "...", "delta_from_canonical": "add refund>0 filter on top of canonical", "exec_result_hash": "sha256:ef23...9a12" }
  ],
  "irt": { "difficulty_score": 0.42, "difficulty_bucket": 3, "discrimination": 0.58, "pilot_pass": { "model_a": true, "model_b": true, "model_c": false, "...": "..." } },
  "provenance": { "subset": "synth", "provenance": "event_planner", "source_url": null, "desensitized": false }
}
```

上例即 ecommerce_017 主线 Sample Family: `family_id = 17031`, `subset = "synth"`, `db_id = "ecommerce_017"`, `modeling_style = "Legacy-drifting"`, 1 canonical + 5 Intent Variant (覆盖 5 种 mutation_type).

落盘细节 (JSON 文件布局 / 索引字段 / 目录组织 / family 边界事务) 见 [03 §9-1 Sample Family 落盘](./03_dataset_construction.md#03-9-1).

<a id="02-3-2"></a>
### 3.2 canonical 字段集

canonical 是一个 Sample Family 的主样本, 是后续所有 Intent Variant 的基准意图.

| 字段 | 类型 | 说明 |
|---|---|---|
| `cmrl` | str / null | cMRL YAML 或 object; fAST-only 样本此字段为 `null` |
| `fast` | object | fAST (MongoDB AST 完整镜像) |
| `mql` | str | fAST unparse 后的字符串 MQL, 已在 mongosh 验证可执行 |
| `nlq` | str (canonical) | 主 NLQ (额外 NLQ 同义表达存入 `nl_queries` 扩展字段, 数量 ≥ 5) |
| `schema_ref` | str | 指向 `mongodb_schema/{db_id}.md#{collection}` 的 schema 锚点 |
| `exec_result_head` | list[dict] | gold MQL 执行结果前 N 行 (N = 10), 按字典序 + BSON 归一化 |
| `exec_result_hash` | str | 全结果集的 sha256 哈希 (用于批量断言与跨子集结果去重) |
| `skeleton` | object | F1-F17 特性向量, 例如 `{"F9": true, "F10": true, "F15": true, "F17": true}` |

`cmrl` 可为 null 的典型场景:

1. MonGen-Real 挖矿样本中 Lifting 失败 (fAST 是真源但无法尽力 Lift 回 cMRL)
2. 涉及 cMRL 外算子 (`$setWindowFields` / `$densify` / `$fill` / 复杂 `$expr` 等)

ecommerce_017 主线的 canonical 核心 cMRL 片段:

```yaml
cmrl:
  intent: aggregate
  scope:
    collection: orders
    filters:
      - {field: status, op: eq, value: paid}
      - {field: paid_at, op: exists, value: true}
      - {field: paid_at, op: gte, value: "2026-01-01", type: Date}
    unwinds:
      - {path: items, preserveNullAndEmptyArrays: false}
  grouping:
    by: [user_id]
    aggs:
      - {alias: total_spent, op: sum, field: items.price}
  projection:
    include: [user_id, total_spent]
  ordering:
    - {field: total_spent, direction: desc}
  limits: {limit: 3}
  features: [F9, F10, F15, F17]
```

对应的 MQL 字符串 (由 Lowering + unparse 机械产出, 验证可在 `ecommerce_017` 上直接执行):

```
db.orders.aggregate([
  {$match:{status:"paid",paid_at:{$exists:true,$gte:ISODate("2026-01-01")}}},
  {$unwind:"$items"},
  {$group:{_id:"$user_id",total_spent:{$sum:"$items.price"}}},
  {$project:{_id:0,user_id:"$_id",total_spent:1}},
  {$sort:{total_spent:-1}},
  {$limit:3}
])
```

Canonical 的主 `nlq`: "Top 3 customers by total paid item spending in 2026." 另附 5 条 Paraphraser 生成的等义表达 (例如 "Who are the 3 biggest-spending buyers of paid items in 2026?"), 以覆盖句法 / 风格 / 词汇多样性.

**`exec_result_head` 归一化规则**: 字段按字典序排序 / Decimal128 序列化为字符串 / Date 统一为 ISO-8601 字符串 / ObjectId 保留原 hex 字符串 / null 与字段缺失区分处理. 目的: 评估侧的结果比对不被无关的格式抖动 (BSON 整数类型分桶、字段顺序) 触发假阴性, 同时保留真正的语义差异 (如 null 与缺失).

**`exec_result_hash` 的用途**: (a) 批量断言跨子集结果稳定 (例如 Hybrid 样本与 Real 样本意图相同时, 预期哈希一致); (b) 跨 sample family 的结果集去重 (避免 Sampler 产出等价查询但字面不同的冗余 family); (c) 单元测试锚 (pipeline 变更后若哈希漂移则触发人工复核).

**为何只存前 10 行**: 完整执行结果在大文档场景可能达 MB 级, 不适合跟每条样本捆绑落盘. 前 10 行覆盖最常见的断言需求 (头部匹配 / 排序正确性 / 分页首屏), 并由 `exec_result_hash` 记录全集级别指纹作为第二层验证信号. 对需验证完整行数的场景, 评估侧可按 `db_id` 重新执行 MQL 获取完整结果集.

<a id="02-3-3"></a>
### 3.3 intent_variants 字段集

`intent_variants` 是 list[intent_variant], 每个 Intent Variant 由 Intent Mutator 从 canonical 派生.

| 字段 | 类型 | 说明 |
|---|---|---|
| `variant_id` | str | 同 family 内唯一 id, 格式 `{family_id}-v{N}` |
| `nlq` | str (primary) | 该 variant 的主 NLQ (额外 3+ 条等义表达存入 `nl_queries`) |
| `mutation_type` | enum | `"negation"` / `"omission"` / `"coreference"` / `"jargon"` / `"composition"` / `"expression_rephrase"` |
| `delta_from_canonical` | str | 相对 canonical 的语义变化摘要 (人类可读描述 + Intent Mutator 记录的 AST diff 引用) |
| `exec_result_hash` | str | 执行结果哈希; 可与 canonical 对比判定"意图是否真的变" |

6 种 `mutation_type` 的语义分工:

- **negation**: 引入否定 ("not paid" vs "paid"), 考察模型对 `$not` / `$ne` / `$nin` 的使用
- **omission**: 省略 canonical 中的约束 (例如去掉年份约束), 考察模型在信息不全时的默认假设能力
- **coreference**: 用代词或前指 ("those customers" / "same customers as above"), 考察上下文绑定
- **jargon**: 替换为领域行话 ("paying customers" → "converted users"), 考察术语对齐
- **composition**: 嵌套的语义组合 (例如"与 canonical 相同, 但加上 refund > 0 限定"), 考察局部修改下的稳健性
- **expression_rephrase**: 保持意图不变, 仅重写表述 (句法 / 词汇 / 语序), 用于评估模型对表达风格的鲁棒性 (与 canonical 的多 `nl_queries` 同族, 但此处作为独立 variant 以让评估可直接按 variant 维度统计)

ecommerce_017 主线的 5 个 Intent Variant 示例:

| variant_id | mutation_type | NLQ (摘要) | delta_from_canonical |
|---|---|---|---|
| 17031-var01 | negation | "Top 3 customers by total spending in 2026 excluding those who ever requested refunds." | `$match` 追加 `metadata.*.refund_request: {$ne: true}` |
| 17031-var02 | omission | "Top 3 customers by total paid item spending." (省略年份约束) | 删除 `paid_at ≥ 2026-01-01` filter |
| 17031-var03 | jargon | "Top 3 converted users by 2026 GMV from line items." | 术语替换 (customers → converted users, spending → GMV) |
| 17031-var04 | coreference | "Same customers as above, but sort by order count." | 指代 canonical 上下文, sort 维度改变 |
| 17031-var05 | composition | "Top 3 paying customers by 2026 spending, restricted to those with at least one refund > 0." | 追加 `refund > 0` 的条件分支 |

**6 种 mutation_type 的互斥性**: 一个 Intent Variant 只属于一种类型, 但可以叠加触发 (例如 negation + coreference 复合). 为简化诊断, MonGen-Synth 默认每个 Intent Variant 只使用单一 mutation_type; composition 类型本身表示"canonical + 局部修改", 与其他类型在概念上有重叠, 但落地时通过 Intent Mutator 的不同 prompt template 严格区分.

**分布目标**: 6 种 mutation_type 在 MonGen-Synth 中目标均衡 (每类 ~16.7%). 若某类 < 12% 或 > 22%, 会触发 Intent Mutator 采样权重调整, 详见 [§Y](#02-Y).

6 种 `mutation_type` 的生成机制 (prompt template / AST rewrite 规则 / 失败回退) 见 [03 §3-7 Intent Mutator](./03_dataset_construction.md#03-3-7).

<a id="02-3-4"></a>
### 3.4 IRT 字段 (difficulty / discrimination)

`irt` object 由 pilot 模型集合产出, 入库后固化, 仅在基准版本升级时重新校准.

| 字段 | 类型 | 说明 |
|---|---|---|
| `difficulty_score` | float | `[0, 1]`, 由 pilot 模型集合跑出的 `1 - 平均 pass 率` |
| `difficulty_bucket` | int | `1` - `5` 对应 L1-L5, 按 [§1-4](#02-1-4) 分桶定义, 各占 20% |
| `discrimination` | float | `[-1, 1]`, pilot 模型能力与本样本 pass 的相关度; **入库要求 ≥ 0.3** |
| `pilot_pass` | object | 每个 pilot 模型是否 pass, 键为 pilot 模型 id, 值为 bool; 长度 = pilot 数 |
| `legacy_structural` | object (可选) | 结构难度分 (pipeline 深度 / 特性数 / 歧义度等), 附加元数据 |

保留 `pilot_pass` 的目的: 让后续研究能复算"在 pilot 集合 subset 上的 IRT 分数是否稳定", 也便于做模型家族敏感性分析 (如剔除某家族后 difficulty 的漂移).

**pilot 模型集合的具体选型指引**: (a) 至少 1 个 ≤ 7B 的 small LM, (b) 至少 3 个 13B - 70B 的 mid LLM, (c) 至少 2 个 > 70B 或 MoE 的 large LLM, (d) 跨至少 3 个不同供应商 (例如 open-source + closed-source + hybrid), (e) 同家族不同规模的模型 (如 7B / 70B 同系列) 计为 1 个家族. 满足 (a)-(e) 的集合, pilot 总数落在 8-12 之间.

**为何 IRT 字段入库后固化**: 若每次基准评估都重新跑 IRT, 模型开发者可能针对 pilot 刷分数 (反向工程 pilot 集合的弱点), 使基准失去公正性. 固化规则: 初始 pilot 集合 + initial IRT 评分是基准的"一锤子"定音, 后续基准升级时 (如增加 1,000 families) 才重新校准 IRT.

**IRT Difficulty 与 legacy_structural 的预期相关性**: 依据 pilot 实验的初步数据, IRT Difficulty 与 (pipeline 深度 × 0.3 + 特性数 × 0.4 + 歧义度 × 0.3) 的结构加权公式的 Spearman 相关系数约 0.55. 相关度不高的原因是: 结构复杂度并不总等于"模型难解决度"; 某些短 pipeline 但涉及 `$graphLookup` 的查询对模型反而困难 (结构浅但算子陌生). 保留 legacy_structural 作为第二视角, 而非替代.

ecommerce_017 主线: `difficulty_score = 0.42`, `discrimination = 0.58`, `difficulty_bucket = 3` (L3 medium 档). `pilot_pass` 形如 `{"m_a": true, "m_b": true, "m_c": true, "m_d": false, "m_e": true, "m_f": false, "m_g": true, "m_h": false, "m_i": false, "m_j": false}` (10 个 pilot 模型, 4 个 pass).

<a id="02-3-5"></a>
### 3.5 provenance / subset 字段

`provenance` object 记录样本从何而来, 是外部有效性追溯与 MonGen-Real 合规性审计的依据.

| 字段 | 类型 | 说明 |
|---|---|---|
| `subset` | enum | `"synth"` / `"real"` / `"hybrid"` (与 family 顶层 `subset` 冗余保存, 便于 provenance 独立抽取) |
| `provenance` | enum | `"event_planner"` (Synth 生成) / `"mql_mining"` (Real 挖矿) / `"hybrid_compose"` (Hybrid 重组) |
| `source_url` | str / null | 真实来源 URL (Real 必填; Synth / Hybrid 为 null) |
| `desensitized` | bool | 是否经过脱敏 |
| `license` | str / null | `"MIT"` / `"Apache-2.0"` / `"CC BY-SA 4.0"` 等, Real 必填 |
| `lifting_status` | enum | `"full"` (成功 Lift 回 cMRL) / `"partial"` / `"failed"` (fAST-only) |

`subset` / `provenance` / `desensitized` 三字段的联合用途: **分层 report**. 发布指标时按 (subset × provenance × desensitized) 三元组分列, 例如 `EX(synth, event_planner, false) = 82.1%` vs `EX(real, mql_mining, true) = 68.3%`, 让读者直接看到内部 / 外部有效性的差距, 而不被合并数字掩盖.

`lifting_status = "failed"` 的样本只能以 fAST 形式参与训练 / 评估, 不能作 cMRL 层评价. 基准报告会对该桶单独统计 EX, 以便分析 cMRL 覆盖缺口对模型评估的影响.

**审计追溯的用途**: (a) 合规审查阶段, legal 可按 `license` 字段快速筛出所有需要归属的样本; (b) 偏差分析阶段, 可按 `provenance` 字段看 EX 在哪种来源上更高 / 更低; (c) 发布后有用户质疑某样本, 可通过 `source_url` 追溯原始出处; (d) 学术引用阶段, 可统计 MonGen-Real 中每个主要仓库的贡献样本数, 纳入致谢页.

<a id="02-4"></a>
## 4. MongoDB 库形式

> 为何这样设计: MonGen 的核心特征是"同 collection 内文档结构差异化"与"不同库采用不同 Modeling Style". 本节分两个小节: [§4-1](#02-4-1) 用 ecommerce_017 的 `orders` 集合实例化**单库内异质**, [§4-2](#02-4-2) 用 6 种 Modeling Style 的最简片段展示**跨库异质**. 两尺度联合覆盖 schema-less 的全景.

<a id="02-4-1"></a>
### 4.1 异质文档形态 (schema-less)

ecommerce_017 的 `orders` collection 同时包含三种文档形态, 对应不同 `schema_version` 取值与处置状态. 此例同时激活 [§1-1 schema-less native](#02-1-1) 原则与 **Legacy-drifting** Modeling Style. 同一集合内 `items` 既可 array 也可 object (在 legacy 与 current 形态间类型迁移) 展示了 F4 数组元素多态与 F2 多态类型.

**形态一: legacy (schema_version = 1)** — 早期沉积数据, 无 `paid_at` 字段, 金额以字符串存, `items` 仅包含简单数组:

```json
{
  "_id": ObjectId("65a000000000000000000001"),
  "schema_version": 1,
  "user_id": ObjectId("65a000000000000000000100"),
  "items": [ {"sku": "A-001", "qty": 2, "price": "19.90"} ],
  "total": "39.80",
  "created_at": ISODate("2023-01-15T08:00:00Z"),
  "status": "paid"
}
```

激活: F8 / F10 / F13 (多 schema 共存). `total` / `price` 此形态是字符串, 与下面 schema_version = 3 的 `Decimal128` 形成 F2 多态类型对照.

**形态二: current (schema_version = 3)** — 含 `paid_at`、`shipping.location` GeoJSON、`metadata` 动态键 object, `items` 是数组且元素多态 (physical vs voucher):

```json
{
  "_id": ObjectId("66f000000000000000000010"),
  "schema_version": 3,
  "user_id": ObjectId("65a000000000000000000200"),
  "items": [
    {"sku": "A-001", "qty": 1, "price": NumberDecimal("19.90"), "kind": "physical"},
    {"sku": "GIFT-50", "value": NumberDecimal("50.00"), "kind": "voucher"}
  ],
  "total": NumberDecimal("69.90"),
  "currency": "CNY",
  "created_at": ISODate("2026-03-20T10:11:00Z"),
  "paid_at": ISODate("2026-03-20T10:12:30Z"),
  "shipping": {
    "address": "Hangzhou, Zhejiang",
    "location": {"type": "Point", "coordinates": [120.15, 30.27]}
  },
  "metadata": {
    "2026-03-20": {"campaign": "spring", "coupon": "SP10"},
    "2026-03-25": {"refund_request": true}
  },
  "status": "shipped"
}
```

激活: F1 (`paid_at` 在 legacy 形态缺失即稀疏) / F2 (`total` 类型从 string 变 `Decimal128`) / F3 (`shipping` 子文档可选) / F4 (`items` 数组同时含 `physical` 与 `voucher` 多态结构) / F5 (`metadata` 用日期作 key) / F6 (`metadata` 可由 `$objectToArray` 转聚合) / F7 (`metadata` key 集合随文档演化) / F9 (`Decimal128`) / F10 (`Date`) / F11 (`shipping.location` GeoJSON Point).

**形态三: partial (cancelled 的瘦身文档)** — 同 collection 内的"瘦身文档", 仅保留追溯所需的最小字段集:

```json
{
  "_id": ObjectId("66f000000000000000000099"),
  "schema_version": 3,
  "user_id": ObjectId("65a000000000000000000300"),
  "created_at": ISODate("2026-03-22T07:45:00Z"),
  "status": "cancelled",
  "cancel_reason": "user_request"
}
```

激活: F1 (大量字段缺失) / F8 / F10 / F13 (与完整 schema_version=3 文档结构差异巨大但 schema_version 相同, 体现 schema 版本内的形态分支).

**要点**: 上述三种形态在同一 `orders` collection 内并存, 是 MonGen 区别于关系表派生数据集的核心标志. 模型若按"字段一定存在"的假设生成 MQL, 在 partial 与 legacy 形态上必然产生空集或 `BSONTypeError`; 反之必须主动加入 `$exists` (F15) 与 `$type` 路由 (F2) 才能稳定返回.

**一个常见错误模式**: 模型直接写 `db.orders.aggregate([{$match:{paid_at:{$gte:ISODate("2026-01-01")}}},...])` 而不加 `$exists: true`, 在 legacy 形态 (无 `paid_at` 字段) 上会被隐式视为"字段不满足条件"而被 `$match` 过滤 — 结果正确, 但出于"字段缺失"而非"字段显式 < 2026"的原因; 在某些文档形态组合下, 这会导致语义漂移 (例如 `paid_at: null` 与 `paid_at` 字段不存在两种情况被合并). 基准的 gold MQL 显式加 `$exists: true` 是为了明确区分这两种情况, 并通过 3-way Verifier 验证该选择与 NLQ 语义一致.

<a id="02-4-2"></a>
### 4.2 建模哲学示例 (6 种)

下列 6 种 Modeling Style 各给出最简片段与 ~80 字论证, 全部以电商业务域为基线 (便于横向对比). ecommerce_017 是其中 Legacy-drifting 的具体实例. 各 Modeling Style 的占比分配机制 (如何让 Document Accreter 在同业务域下按目标比例切换哲学) 见 [03 §2-4 Modeling Style Skew](./03_dataset_construction.md#03-2-4).

**1. Normalized** — `orders` / `users` / `items` 三 collection, 通过 `_id` 引用:

```json
// db.orders 文档
{"_id": ObjectId("..."), "user_id": ObjectId("..."), "item_ids": [ObjectId("...")], "total": NumberDecimal("99.9")}
// db.users 文档
{"_id": ObjectId("..."), "name": "Alice", "email": "alice@example.com"}
// db.items 文档
{"_id": ObjectId("..."), "sku": "A-001", "price": NumberDecimal("19.9")}
```

论证: 文档小、引用多; 查询端大量 `$lookup` 拉取关联字段, 跨 collection 聚合是常态; 对应 F16 偏高.

**2. Embedded** — 单 `users` 文档嵌入 `orders` 数组, orders 内嵌 items 数组:

```json
{
  "_id": ObjectId("..."),
  "name": "Alice",
  "orders": [
    {"status": "paid", "items": [{"sku": "A-001", "price": NumberDecimal("19.9"), "qty": 1}]},
    {"status": "refunded", "items": [{"sku": "B-002", "price": NumberDecimal("29.9"), "qty": 2}]}
  ]
}
```

论证: 大文档 + 深嵌入数组; 查询端 `$unwind` 路径深 (orders → items), 配合 `$group` 做聚合; 对应 F4 / F14 偏高.

**3. Bucket** — `events_2026_q1` / `events_2026_q2` 按季度分桶, 每桶内数组存事件:

```json
// db.events_2026_q1 文档
{"_id": "2026-01-15", "day_count": 2, "events": [
  {"t": ISODate("2026-01-15T08:00:00Z"), "kind": "click"},
  {"t": ISODate("2026-01-15T09:00:00Z"), "kind": "buy"}
]}
```

论证: 时序分桶 + 数组内事件 + 预聚合 `day_count`; 查询端 `$bucket` / `$unwind` 配合使用; 对应 F14 / F10 偏高.

**4. Polyglot** — 同库混用: `orders` 自身嵌入 items (Embedded 风格), 但 `refunds` 独立 collection 用引用 (Normalized 风格):

```json
// db.orders 文档
{"_id": ObjectId("..."), "items": [{"sku": "A", "price": NumberDecimal("10")}]}
// db.refunds 文档
{"_id": ObjectId("..."), "order_id": ObjectId("..."), "reason": "damaged"}
```

论证: 同库中不同 collection 采用不同建模策略, 查询端需同时处理嵌入与引用两种关系; 对应 F2 (类型异质) / F12 (嵌入 vs 引用并存) 偏高.

**5. Legacy-drifting** — 即 [§4-1](#02-4-1) 的 ecommerce_017 示例 (schema_version=1 / schema_version=3 / partial 三种文档形态并存), `schema_version` 字段区分:

```json
// db.orders 多形态文档并存
{"_id": ObjectId("..."), "schema_version": 1, "total": "39.80", "...": "..."}
{"_id": ObjectId("..."), "schema_version": 3, "total": NumberDecimal("69.90"), "paid_at": ISODate("2026-03-20T10:12:30Z"), "...": "..."}
{"_id": ObjectId("..."), "schema_version": 3, "status": "cancelled", "cancel_reason": "user_request"}
```

论证: 多年演化导致多 schema 共存, 查询端必须先按 `schema_version` 分支再处理, 或用 `$exists` / `$type` 防御性编程; 对应 F1 (稀疏) / F13 (多 schema 共存) 偏高.

**6. Tenant-sharded** — `tenant_data` collection, 顶层字段 `tenants.{tenant_id}.*`, `tenant_id` 作动态 key:

```json
{
  "_id": ObjectId("..."),
  "tenants": {
    "tenant_a": {"orders": [{"total": NumberDecimal("100")}]},
    "tenant_b": {"orders": [{"total": NumberDecimal("200")}]}
  }
}
```

论证: 租户 ID 作为动态键嵌入文档; 查询端必须用 `$objectToArray` 把 tenants map 转可聚合数组, 然后 `$unwind` 展开; 对应 F5 (租户作 key) / F7 (key 集合随文档演化) 偏高.

**6 种哲学之间的分化点**:

- Normalized ↔ Embedded: "是否把关联实体嵌入"
- Embedded ↔ Bucket: "嵌入结构是否按时间/键分桶"
- Bucket ↔ Polyglot: "同库是否单一建模范式"
- Polyglot ↔ Legacy-drifting: "混用是出于同期设计还是历史演化"
- Legacy-drifting ↔ Tenant-sharded: "异质源是时间维度还是租户维度"

这五个分化轴覆盖了 MongoDB 生态中 90% 以上的真实建模决策.

**建模哲学对典型查询代码模式的影响**:

| Modeling Style | 典型查询首阶段 | 关键后续阶段 |
|---|---|---|
| Normalized | `$match` 过滤本 collection | 多个 `$lookup` 逐个拉 |
| Embedded | `$match` 外层后 `$unwind` | `$group` 或数组 `$filter` 表达式 |
| Bucket | `$match` 定位桶 | `$unwind` 桶内数组 + `$group` 汇聚 |
| Polyglot | `$match` + 条件性 `$lookup` | 依查询意图路由到嵌入或引用分支 |
| Legacy-drifting | `$match` + `$exists` / `$type` 防御 | 按 `schema_version` 分支用 `$switch` |
| Tenant-sharded | `$project` + `$objectToArray` | `$unwind` 租户 + `$match` 租户 ID |

**为何不单列 Time-series collection / Capped collection**: 这两种是 MongoDB 的物理存储优化选项, 不是建模哲学层面的决策. MongoDB-aligned 原则涵盖它们的查询语义 (F10 / F14), 但基准不把它们提升到 Modeling Style 级别.

<a id="02-5"></a>
## 5. 切分策略

> 为何这样设计: 仅按 db_id 切不足以度量组合泛化 — 不同库可能复用同一组特性组合, 模型仍可短路记忆"见到 `$objectToArray` + `$exists` 就这么写". 仅按特性组合切又不能跨业务域. 仅按 Modeling Style 切会落到同业务域同建模哲学堆叠. MonGen 在 cross-domain 基础上**联合叠加 cross-feature 与 cross-modeling-style**, 三维切分, 强制测试集至少 30% 的"特性 × 建模哲学"组合在训练期未出现, 才能真正逼近生产部署的 zero-shot 情形.

三子集切分流程最终依赖两条外部轨的构造: MonGen-Real 的挖矿流水线 (含仓库筛选、脱敏、Lifting、归属维护) 见 [03 §4 Reverse Real 轨](./03_dataset_construction.md#03-4); MonGen-Hybrid 的组合流水线 (真实意图骨架 × 合成异质库 × 重新 Lowering) 见 [03 §5 Hybrid 轨](./03_dataset_construction.md#03-5). 切分本身在各子集落盘后独立执行.

<a id="02-5-1"></a>
### 5.1 三维切分 (domain × feature × modeling-style)

| 维度 | 单独使用的局限 | 三维叠加后的收益 |
|---|---|---|
| cross-domain (按 db_id 所属业务域) | 库不同但 feature 组合可能完全重合, 模型可短路 | 杜绝 schema 记忆 |
| cross-feature (按 `skeleton` 特性向量) | 同库内的 feature 组合无法天然区分训练 / 测试 | 度量 feature 组合泛化 |
| cross-modeling-style (按 `modeling_style`) | 只按业务域 / 特性可能落到同一建模哲学 | 逼模型学习跨哲学推理, 而非记忆架构 |
| 三维叠加 | — | 测试集至少 30% 的 (feature × style) 组合在训练集未出现 |

**三维切分的统计性质**: 设训练集 pair 总数 |C_train| 与测试集 pair 总数 |C_test| 有 |C_train ∪ C_test| = |C_train| + |novel_in_test|. 30% novel ratio 意味着训练集"见过的组合空间"至多覆盖真实组合空间的 70% (若 train / test 比例为 8:2, test 的 novel 部分约占全集 6%, 可作为 zero-shot 下限估计). 更严的 40% novel ratio 会收敛困难, 30% 是实测与理论计算的折衷点.

**理论组合空间大小**: 17 特性 × 6 建模哲学的笛卡尔积理论上有 2^17 × 6 ≈ 786K 个组合, 但实际分布极度稀疏 (大多数特性组合不可能同时出现, 如 `F11 GeoJSON` 与 `F5 租户作 key` 几乎互斥). MonGen-Synth 16,000 families 实际覆盖约 1,200 个不同 (feature_set, style) 组合; 30% novel 意味着测试集覆盖约 360 个训练集未见的组合, 数量上足以做统计显著性检验.

**为何按逻辑库整体切分而非按 Sample Family 切分**: 若按 family 切分, 同一 db_id 的 families 可能同时落到训练与测试, 模型可在训练阶段学到该库的字段拓扑并在测试阶段对同库样本有先验优势 — 即 schema 泄漏. 按逻辑库整体切分 + 按 family 在库内聚合是本基准的硬约束, 即使这导致某些库的 families 全部落到训练集或全部落到测试集, 也不打破切分边界.

<a id="02-5-2"></a>
### 5.2 切分伪代码

```python
def stratified_split(libs, train_ratio=0.8, novel_combo_ratio=0.3):
    # 按 (domain, feature_bucket, modeling_style) 三元做 stratified 分组
    cells = group_by(libs, key=lambda lib: (lib.domain, bucket(lib.feature_set), lib.modeling_style))

    train_libs, test_libs = [], []
    for cell_key, cell_libs in cells.items():
        # 保证 test 的每个三元格至少 1 条
        shuffled = shuffle(cell_libs)
        n_test = max(1, int(len(shuffled) * (1 - train_ratio)))
        test_libs.extend(shuffled[:n_test])
        train_libs.extend(shuffled[n_test:])

    # 校准 cross-feature × cross-style novel ratio
    train_combos = collect_feature_style_pairs(samples_in(train_libs))
    test_combos  = collect_feature_style_pairs(samples_in(test_libs))
    novel_in_test = test_combos - train_combos

    max_iter = 200
    while len(novel_in_test) / len(test_combos) < novel_combo_ratio and max_iter > 0:
        candidate   = pick_lib_with_overrepresented_pair(train_libs)
        replacement = pick_lib_with_novel_pair(test_libs)
        if not candidate or not replacement:
            break
        train_libs.remove(candidate); train_libs.append(replacement)
        test_libs.remove(replacement); test_libs.append(candidate)
        train_combos = collect_feature_style_pairs(samples_in(train_libs))
        test_combos  = collect_feature_style_pairs(samples_in(test_libs))
        novel_in_test = test_combos - train_combos
        max_iter -= 1

    return train_libs, test_libs
```

约束:

- 切分粒度始终是**逻辑库 × 建模哲学**, 单库内样本不分裂 (避免 schema 泄漏)
- `collect_feature_style_pairs` 的 pair 定义: `(sorted(feature_ids), modeling_style)`, 对该元组去重后做差集
- 30% novel pair 迭代收敛失败时降级至 25%, 并在 [§Y](#02-Y) 记录具体触发场景
- 每次 swap 可能同时影响多个 pair 的分布, 所以需整体重算

**swap 的具体策略**: `pick_lib_with_overrepresented_pair` 选训练集中某对 (特性集, 风格) 出现次数最多的库; `pick_lib_with_novel_pair` 选测试集中某对 (特性集, 风格) 不在训练集中出现的库.

**收敛保证**: 每次 swap 后 `|novel_in_test| / |test_combos|` 的下界单调非递减 (证明: 被换出的库 pair 至少有一个在训练集中仍存在其他来源, 因此训练集 pair 集合不减少; 被换入的库 pair 至少有一个是新引入的, 因此测试集 pair 集合扩展). 此性质保证迭代终止, 但不保证达到 30% 目标.

<a id="02-5-3"></a>
### 5.3 子集间切分关系

> 为何这样设计: 三子集若各自独立切分再汇总, 会出现 MonGen-Hybrid 的合成库泄漏到 MonGen-Synth 测试集、MonGen-Real 的源仓库样本同时落在训练 / 测试两侧等问题. 本小节给出跨子集的切分一致性规则.

**子集独立 8:2 切分, 不跨子集借用**:

- **MonGen-Synth 独立 8:2**: 按 §5-2 三维切分伪代码在 220 库上运行, 产出 train ≈ 176 库 / test ≈ 44 库. 对应 family 级规模: train ≈ 12,800 families / test ≈ 3,200 families.
- **MonGen-Real 独立 8:2**: MonGen-Real 样本按来源 (GitHub 仓库 ID / Stack Overflow 问题 ID) 做 **cross-source 8:2 切分**, 禁止同一仓库的样本分裂到训练 / 测试 (避免代码风格泄漏). 规模: train ≈ 3,200 / test ≈ 800.
- **MonGen-Hybrid 独立 8:2, 但受两条外部约束**: (a) Hybrid 的合成库直接复用 MonGen-Synth 的 220 个库, 其库归属 (train/test) 必须与 Synth 切分一致; (b) Hybrid 的意图骨架来自 MonGen-Real, 其意图归属必须与 Real 切分一致. 因此一个 Hybrid 样本只有在 "Synth 库与 Real 意图都在同一侧" 时才入对应侧. 规模: train ≈ 1,600 / test ≈ 400.

**汇总规模**: train ≈ 17,600 families / test ≈ 4,400 families.

**跨子集测试场景 (RQ 驱动的诊断组合)**:

- MonGen-Synth 测试库 × MonGen-Real 测试意图骨架 = MonGen-Hybrid 测试样本: 既换库又换意图源, 是本基准对组合泛化最严格的考察点.
- MonGen-Synth 训练库 × MonGen-Real 测试意图骨架: 库熟悉 + 意图陌生, 测意图泛化能力.
- MonGen-Synth 测试库 × MonGen-Real 训练意图骨架: 库陌生 + 意图熟悉, 测 schema 泛化能力.

不跨子集借用的目的: 让"按子集 × 按难度"交叉 report 成为可能. 例如 `EX(synth L3) / EX(real L3) / EX(hybrid L3)` 可直接对比, 因为三个数都由各自子集 test 独立产出, 不互相渗透.

<a id="02-6"></a>
## 6. 与现有 benchmark 对比

> 为何这样设计: 横向对比直观展示 MonGen 填补的空白. MonGen 的独特组合是"schema 异质 + 17 特性 + 6 建模哲学 + Sample Family 意图变体 + 可证明/概率双验证 + 3-way Verifier + IRT 难度分桶 + Real 真实锚点"; 此前任何 Text-to-SQL 基准均未同时具备这些维度.

| 基准 | 规模 | 任务 | 执行验证 | schema-less | 建模哲学多样 | 外部锚定 | IRT 难度分桶 | 3-way 验证 |
|---|---|---|---|---|---|---|---|---|
| WikiSQL | ~80k | Text-to-SQL | 部分 | 否 | — | 有 | 否 | 否 |
| Spider (SQL 基线) | 10,181 | Text-to-SQL | 是 | 否 | — | 有 | 否 | 否 |
| BIRD | 12,751 | Text-to-SQL | 是 | 否 | — | 有 | 否 | 否 |
| TEND | ~20k | Text-to-NoSQL | 部分 | 弱 (同 collection 形态单一) | 1 种 (Embedded 为主) | 有限 | 否 | 否 |
| NoSQLGen | ~15k | Text-to-NoSQL | 部分 | 弱 | 1 种 | 无 | 否 | 否 |
| **MonGen** | **~60,000 pairs / ~22,000 families (Synth 16k + Real ~4k + Hybrid 2k)** | **Text-to-NoSQL (MongoDB)** | **是 (mongosh)** | **是 (事件驱动沉积, ≥ 3 种 distinct 文档结构 / collection)** | **6 种 Modeling Style** | **是 (Real 4k 挖矿 + Hybrid 2k 组合)** | **是 (L1-L5 各 20%, Discrimination ≥ 0.3)** | **是 (3/3 pass / 2/3 probable-pass / Ambiguous bucket)** |

对比要点:

- **规模**: MonGen 与 BIRD 同数量级, 显著小于 WikiSQL, 但每个 Sample Family 的信息密度更高 (1 canonical + K 个 Intent Variant + 5+ NLQ + cMRL + fAST + IRT + provenance). 以 family 为单位, MonGen 提供的诊断粒度是 BIRD / Spider 的 3-5 倍.
- **任务**: MonGen 是首个面向 MongoDB 的、强调 schema-less 与 17 项原生特性覆盖的可执行基准. Spider / BIRD 均为关系库, 无法天然刻画"同 collection 内文档结构差异化"等 NoSQL 特有现象.
- **对 TEND / NoSQLGen 的胜场**: 两者虽是 Text-to-NoSQL, 但 schema 异质度弱 (同 collection 形态基本单一), 缺乏建模哲学多样, 无 IRT 难度分桶, 无 3-way 验证, 也无外部真实锚点. MonGen 在这 5 维上全面改进.
- **独有维度**: "schema 异质 + 6 建模哲学 + Sample Family 意图变体 + cMRL/fAST 双层可证明 + Real 外部锚点 + IRT 难度分桶 + 3-way 验证" 的组合是 MonGen 独有.
- **与 SParC / CoSQL (对话式 Text-to-SQL) 的差别**: 对话式基准测多轮意图扩展, MonGen 的 Intent Variant 测单轮意图变形; 两者正交, 后续基准版本可考虑加入多轮扩展.
- **与 SEDE / KaggleDBQA (真实语料 Text-to-SQL) 的共性**: 均强调外部有效性; 差异是 MonGen 同时给出合成 + 真实两条轨道, 可做对比, 而 SEDE / KaggleDBQA 仅有真实轨道.

<a id="02-7"></a>
## 7. 已知偏差

- **(a) Synth 分布偏合成**: Document Accreter 的事件模板由人工 / LLM 草拟, 时间戳分布、热点 key 分布、写入并发模式与真实生产存在系统性差距. MonGen-Real 给出的实际分布可与 MonGen-Synth 目标分布做 χ² 对齐检验, 偏差超过阈值时调整 Event Planner 的模板权重, 作为迭代反馈环. 预期 χ² 值在 17 特性维度上的合理阈值是 p ≥ 0.05; 若首版合成数据 p < 0.01, 需触发 Event Planner 模板重加权.
- **(b) Real 受公开数据合规限制**: GitHub 可挖矿样本限于 MIT / Apache 许可证, Stack Overflow 限于 CC BY-SA, MongoDB Forums 限于 ToS 允许范围. 这导致某些业务域 (如金融、医疗) 在 Real 中系统性偏低 (金融实测预计 4% - 6%, 远低于 Synth 的 12% 目标). 报告时显式标注此偏置方向, 禁止把三子集 EX 简单加权做"总体 EX".
- **(c) IRT pilot 模型引入评分 bias**: pilot 模型集合若偏向某一家族 (如三个都是同家族不同规模), Difficulty 评估会系统性偏斜. 落地对策是 pilot 集合至少跨 3 个不同家族 + 至少 2 个不同预训练语料基座; Discrimination ≥ 0.3 的入库门槛可过滤掉"所有 pilot 同时错"的样本, 但不能完全消除家族偏置.
- **(d) Intent Mutator 在极端黑话下的 coverage 有限**: jargon 变体生成依赖 Intent Mutator 的领域词典; 对极度小众的黑话 (例如金融场内结算特定术语、游戏特定社群俚语), 词典可能缺失, 导致 jargon 变体的真实语言覆盖不足. 对策是按业务域维护领域词典并持续扩展, 发布时标注 jargon 变体的词典覆盖率.
- **(e) 建模哲学权重依据产品文档启发**: 6 种 Modeling Style 的 17/17/17/17/17/15 占比是基于 MongoDB 官方 case study + 产品文档挖矿得到的启发式值, 并非 RCT 实测. Tenant-sharded 给 15% 是因为纯租户动态键在实际 workload 中相对稀少. 若 MonGen-Real 实测分布与该假设偏差 > 5 个百分点, 会在下一基准升级时调整 Modeling Style Skew 权重.
- **(f) 英文 NLQ 为主**: MonGen 首版的 NLQ 语言默认为英文. 中文 / 其他语言的 NLQ 在 Paraphraser 侧有部分覆盖 (约 10%), 但主流评估仍以英文为准. 跨语言鲁棒性留待后续基准版本扩展.
- **(g) BSON 大规模文档受限 16MB**: MongoDB 单文档上限 16MB. MonGen-Synth 单文档在文档数 50k 的 bucket / legacy 库下接近该限制, 但未超过 (最大约 2MB). 若真实业务有超 16MB 文档需求, 需要 GridFS 支持, 不在本基准 scope 内. Real / Hybrid 若挖到近上限样本, 在脱敏时会裁剪冗余子文档保持可用.
- **(h) 3-way Verifier 的成本偏置**: 高开销模型参与 Verifier 会限制 MonGen 的可复现扩展 (其他研究者复现基准构造时难以承担同等成本). 对策是公开 Verifier 模型列表与预算, 并留出"降级模式"允许使用 2 家开源模型 + 1 家闭源的组合作为替代.

<a id="02-X"></a>
## X. 主要构件清单

| 构件 | 一句话职责 | 对应 03 章节 |
|---|---|---|
| Sample Family Emitter | 把 canonical + Intent Variants 组装为单条 Sample Family 落盘 | 03 §9-1 |
| Modeling Style Controller | 把 6 种 Modeling Style 按目标比例分配到 220 个逻辑库 | 03 §2-4 |
| Event Planner / Document Accreter | 按业务剧本事件流沉积异质库 | 03 §2-2 ~ §2-3 |
| cMRL Sampler | 在 30 原语空间做约束求解, 触达 17 特性 | 03 §3 |
| Lowering / Lifting 编译器 | cMRL ↔ fAST 的确定性/尽力互转 | 03 §3-3 ~ §3-4 |
| Intent Mutator | 从 canonical cMRL 派生 6 类 Intent Variant | 03 §3-7 |
| 3-way Verifier | 三家异源 LLM 协议裁决 (NLQ, MQL) 正确性 | 03 §7-5 |
| Real Mining Adapter | 挖矿 GitHub / Stack Overflow / MongoDB Forum 的真实 MQL 并脱敏 | 03 §4 |
| Hybrid Composer | 把 Real 意图骨架挪到 Synth 异质库上重新 Lowering | 03 §5 |
| IRT Scorer | 跑 pilot 模型集合产出 Difficulty / Discrimination / pilot_pass 向量 | 03 §8-1 |
| 三子集切分器 | cross-domain × cross-feature × cross-modeling-style 三维 stratified 切分 | 03 §9 (落盘阶段) |
| Active-Learning Human Loop | 对 probable-pass / Ambiguous 桶做优先级复核 | 03 §7-6 |

数据资产 (落盘路径):

| 主题 | 文件 / 目录 |
|---|---|
| MonGen-Synth 训练 / 测试 | [MonGen/synth_train.json](../MonGen/synth_train.json), [MonGen/synth_test.json](../MonGen/synth_test.json) |
| MonGen-Real 样本 | [MonGen/real.json](../MonGen/real.json) |
| MonGen-Hybrid 样本 | [MonGen/hybrid.json](../MonGen/hybrid.json) |
| Synth 数据库 (JSON) | [MonGen/mongodb_data/](../MonGen/mongodb_data/) |
| Synth schema (JSON) | [MonGen/mongodb_schema/](../MonGen/mongodb_schema/) |
| cMRL YAML 目录 | [MonGen/cmrl/](../MonGen/cmrl/) |
| fAST JSON 目录 | [MonGen/fast/](../MonGen/fast/) |
| 切分结果 | [MonGen/splits/](../MonGen/splits/) |

<a id="02-Y"></a>
## Y. 未尽事项与已知风险

- **TODO(@team)**: MonGen-Real 挖矿可达性实测 — GitHub / Stack Overflow 脱敏与许可证过滤后的实际可用样本量, 以及与 MonGen-Synth 分布的 χ² 对齐度; 若实际样本 < 3,000, 触发降级预案至 MonGen-Real 目标 2,500.
- **TODO(@team)**: Intent Variant 类型分布审计 — 6 种 mutation_type 在 MonGen-Synth 中实际分布应接近均衡 (~16.7% ± 5%); Paraphraser / Intent Mutator 倾向某种风格时需上调 / 下调采样权重.
- **TODO(@team)**: IRT 难度校准 — 至少 2 轮 pilot 评测, 观察 Difficulty 分布漂移与 Discrimination 稳定性; 若漂移 > 15% 或 Discrimination ≥ 0.3 样本占比 < 70%, 触发 pilot 集合调整.
- **TODO(@team)**: 三维切分 30% novel ratio 的可达性实验 — 用 220 库 × 6 建模哲学的分布实际跑一遍 swap 迭代, 记录收敛迭代数与最终 novel ratio; 若迭代 > 200 次仍未收敛到 30%, 触发降级到 25% 的流程.
- **TODO(@team)**: Paraphraser 多样性审计 — 对 MonGen-Synth 抽 200 个 Sample Family, 计算每 family 内 5+ NLQ 的 BLEU-4 自相似度均值, 若均值 > 0.6 则触发多 Paraphraser 轮流或 temperature 调整.
- **风险: cMRL ↔ fAST 双实现漂移** — Lowering 与 Lifting 作为两条独立代码路径, 长期维护中可能产生语义漂移 (例如某 cMRL 原语 Lowering 成的 fAST 无法 Lifting 回相同 cMRL). 对策: 持续回归测试 (每次代码变更跑全量样本的 round-trip), 回归覆盖率需保持 ≥ 99%.
- **风险: IRT pilot 模型的版本漂移** — 商业 LLM 的 API 后台模型可能在未通知情况下升级, 导致 pilot 分数不可复现. 对策: 优先用开源 checkpoint 固定版本 (HuggingFace model hash 锁定), 闭源 API 则记录调用日期与响应, 并明确声明复现不稳定性.
- **风险: MonGen-Hybrid 的 gold MQL 正确性** — 真实意图骨架挪到合成库需重新 Lowering, 意图骨架对隐式 schema 的假设在合成库上可能失效; 由构造期的形式语义 + property-based test 兜底, 落盘后由 3-way Verifier 再校验.
- **风险: Ambiguous / Abstain Bucket 规模过大稀释主评估** — 若 3-way Verifier 裁决一致率 < 70%, Ambiguous 桶会收纳超过 20% 样本, 主评估可比性受损. 对策: Verifier prompt A/B 实验 + 定期随机抽样人工审校, 并对 Ambiguous 桶单独报告指标而不混入主表.
