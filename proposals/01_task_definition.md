# 任务定义 (Text-to-NoSQL)

> 文档定位: 形式化界定 Text-to-NoSQL 任务、I/O 规范与研究问题
> 目标读者: 团队成员 / 复现者 / 评审
> 前置阅读: 无 (本文档为系列入口)
> 最近更新: 2026-04-17



<a id="01-0"></a>
## 0. 摘要

> 为何这样设计: 摘要需要在一屏之内锁定读者对任务边界与基准资产的认知, 避免后续章节反复重置上下文, 因此先给任务定义、再给三子集定位、最后给表示层与核心指标。

Text-to-NoSQL 把自然语言查询 (NLQ) 翻译为可在 MongoDB 上直接执行的查询程序 (`find` 或 aggregation pipeline)。相较 Text-to-SQL, 任务的额外复杂度来自文档模型自身的 schema-less 特性与 pipeline 的顺序敏感语义: 同一 collection 内文档可以有异质字段集、同一字段可以承载多态类型、pipeline 阶段顺序本身就是语义的一部分。这三点共同要求 gold MQL 不仅语法合法, 还必须在执行层返回确定结果。

本系列文档围绕 MonGen 基准展开。MonGen 由三个互补子集构成: **MonGen-Synth** (16,000 families 正向合成, 覆盖 17 项 MongoDB 原生特性), **MonGen-Real** (目标 ~4,000 samples, 3,000-5,000 区间, 从开源代码与公开论坛挖矿得到, 提供真实意图分布), **MonGen-Hybrid** (2,000 samples, 真实意图骨架 × 合成异质库, 度量组合泛化)。三子集的设计定位、规模参数与原则锚点见 [02 §0 摘要](./02_dataset_design.md#02-0)。三子集合计约 60,000 条 (NLQ, MQL) pairs — Synth 每个 Sample Family 含 1 个 canonical 加 K 个 Intent Variant (negation / omission / coreference / jargon / composition 五类), Real / Hybrid 不强制 family 结构, 每条样本独立计数。

任务表面是单步翻译 NLQ → MQL, 但 MonGen 内部允许经由 **cMRL + fAST 双层中介**完成:

- **cMRL** (Compact-MRL) 是 30 原语紧凑 DSL, 作为 Sampler 的受控采样空间, 可在形式语义下做可证明正确的 Lowering;
- **fAST** (Full-AST) 是 MongoDB AST 的完整镜像, 是执行真源, 覆盖 `$setWindowFields / $graphLookup / $bucketAuto / $merge / $facet` 等 cMRL 尚未收纳的长尾算子;
- **Lowering** (cMRL → fAST) 是确定性编译; **Lifting** (fAST → cMRL) 是尽力逆向, 允许失败, 失败样本以 fAST-only 形式进入 MonGen-Real / MonGen-Hybrid。

这一结构让紧凑子集可证明正确的同时, 不对 MongoDB 完整表达力形成盲区。模型既可以直接生成 MQL (端到端), 也可以先生成 cMRL 再经 Lowering 落到 MQL (分阶段, 便于监督), 两条路径共享同一套评估指标与 gold。

正确性保证分三层承担: cMRL 子集由形式语义 + property-based test 机械证明 (对应 RQ5 可证明性); fAST 长尾由 **3-way Verifier** (三家异源 LLM 协议) 做概率裁决, 3/3 记 pass、2/3 记 probable-pass、1/3 人工仲裁、0/3 直接 fail、两两互不一致归入 **Ambiguous / Abstain 桶**; 执行反馈 (mongosh 实际跑通) 作为所有样本的最终守门员, 联合 **Active-Learning Human Loop** 把主训练集的估计错误率压到 <2%。难度分级基于 **IRT** (Item Response Theory): 8-12 个 pilot 模型对每条样本打分, 以 $1 - \text{pass\_rate}$ 作为 **IRT Difficulty**, **Discrimination** ≥ 0.3 的样本才入库, 整体按 5 等难度桶各 20% 均匀分布, train / test 以 8:2 在 Sample Family 粒度切分, 保证同一 canonical 与其 variants 不跨切分边界。

整个基准体系以**执行准确率 EX** 为核心指标, 结构类指标 (QSM / QFC / EFM / EVM) 作为补充视角协助诊断。主线贯穿示例使用 `ecommerce_017` 逻辑库 (电商域, Legacy-drifting **Modeling Style**) 与 NLQ "Top 3 customers by total paid item spending in 2026.", 该样本激活 F9 (Decimal128) / F10 (Date) / F15 (`$exists`) / F17 (`$unwind preserveNullAndEmptyArrays`) 四项特性, IRT difficulty ≈ 0.42 (medium 桶), Discrimination ≈ 0.58; 将在 §3 输出规范与 §5 任务难点中分别从 "fAST 输出" 与 "意图变体" 两个视角被引用, 保证读者跳入 02 / 03 / 05 时看到的是同一条样本在不同视角下的切片。

**贡献与阅读顺序**: 本任务定义 (§1 形式化 / §2 输入 / §3 输出) 是后续 4 份文档的共同前提; §4 与 §5 给出 Text-to-NoSQL 相比 SQL 的本质差异与可被测量的难点; §6 收敛 5 个研究问题 (RQ1 schema linking 小模型可行性 / RQ2 多视角检索 / RQ3 执行反馈闭环 / RQ4 外部有效性 / RQ5 可证明正确性); §7 明确 scope 边界, **fAST-only 长尾显式纳入 scope** 不依赖 cMRL 可证明性。首次阅读建议按 §0→§1→§2→§3→§4→§5→§6→§7→§X→§Y 顺序, 再跳至 02-05 四份文档。



<a id="01-1"></a>
## 1. 任务形式化定义

> 为何这样设计: 先把任务收敛为一个显式函数签名, 再逐项约束输入域、输出域与正确性标准, 可以让后续所有模块 (数据构建、评估、方法论) 都挂钩到同一套定义上; 同时把 cMRL 与 fAST 两个中间产物显式提拔为形式化对象, 以便评估既能作用于字符串 MQL, 也能作用于结构化 fAST, 必要时回落到 cMRL 层。

任务的目标是把 NLQ 翻译为可执行 MQL, 同时提供可评估的中间表示。为此, MonGen 使用 cMRL + fAST 双层中介 (具体规范、Lowering / Lifting / 形式语义 / 差异测试见 [03 §3 cMRL + fAST 双层表示](./03_dataset_construction.md#03-3)): cMRL 承担紧凑采样空间与可证明子集, fAST 承担执行真源与完整表达力。

任务的核心映射为:

$$f: (\text{NLQ}, \mathcal{S}) \to \text{MQL}$$

其中 $\text{NLQ}$ 为自然语言查询 (英文, 中文支持延后), $\mathcal{S}$ 为目标 MongoDB 数据库的 schema (含若干 collection、嵌套字段路径、类型分布), $\text{MQL}$ 为 MongoDB Query Language 形式的可执行查询程序 (`find` 或 `aggregate`)。

为明确 MQL 的来源与可验证性, 我们把 $f$ 拆为若干中间产物的组合 — **fAST 是执行真源, MQL 仅是其 unparse 后的字符串投影**:

$$g: \text{NLQ} \to \text{fAST}, \qquad h: \text{fAST} \to \text{MQL}, \qquad j: \text{fAST} \to \text{cMRL}$$

其中:

- $g$ 为学习目标的主体, 既可端到端 (模型直接产 fAST), 也可分阶段 (模型先产 cMRL 再由 Lowering 落到 fAST);
- $h$ 为 **deterministic unparser** — 把 fAST 线性化为 MQL 字符串, 严格一一对应, 只做空格 / 引号 / 运算符缩写层的规范化, 不做任何语义改动, 因此 fAST 与字符串 MQL 在评估上可互相替代;
- $j$ 为 **Lifting** — 尽力把 fAST 逆向回 cMRL, **允许失败**; Lift 失败的节点不阻塞 MQL 产出, 只是该样本在库中标记为 fAST-only, 不进入 cMRL 训练通道。

另外定义 cMRL 的 Lowering 作为确定性编译:

$$L: \text{cMRL} \to \text{fAST} \quad \text{(deterministic, total on cMRL)}$$

因此 $f = h \circ g$, 并且对于 cMRL 可达的样本有 $f = h \circ L \circ g_{\text{cmrl}}$, 其中 $g_{\text{cmrl}}: \text{NLQ} \to \text{cMRL}$。$L$ 的全定性 (total on cMRL) 与 $j$ 的部分定性 (partial on fAST) 的非对称是本双层设计的关键 — 采样与证明在 cMRL 上是封闭的, 但 fAST 不强求能被 Lifting 完全吸收, 保留了对 MongoDB 长尾算子的覆盖空间。

Schema $\mathcal{S}$ 展开为四元组 $(\mathcal{C}, \mathcal{F}, \mathcal{T}, \mathcal{N})$: $\mathcal{C}$ 是 collection 集合, $\mathcal{F}$ 是字段路径集 (支持点分嵌套与数组下标通配), $\mathcal{T}$ 是每条路径的类型分布 (Decimal128 / ObjectId / Date / Array / 子文档 / null vs missing 区分), $\mathcal{N}$ 是采样文档的 sparsity 与多形态指示。四项中 $\mathcal{T}$ 与 $\mathcal{N}$ 是 NoSQL 特有的 — 同一字段在不同文档可为异型, 在 Text-to-SQL 中不存在对应项。

最关键的约束是**可执行性**: $f$ 的输出必须能在目标 MongoDB 实例上无异常执行并返回结构化结果, 而非仅在语法层"看上去像 MQL"。这一约束直接催生了执行类指标 (EX / EFM / EVM), 并把单纯的字符串级监督学习推向了执行反馈闭环 (对应 RQ3)。

工程实现上, $g$ 进一步拆解为两段: **schema linking** ($g_{\text{link}}: (\text{NLQ}, \mathcal{S}) \to \mathcal{F}_{\text{relevant}}$) 负责识别相关 collection / 字段 / alias / target 字段子集, **query generation** ($g_{\text{gen}}: (\text{NLQ}, \mathcal{F}_{\text{relevant}}) \to \text{cMRL} \text{ 或 } \text{fAST}$) 负责生成结构化查询。两段可以选择不同模型规模 (RQ1 的小模型 vs LLM 选择就发生在第一段), 但最终都必须经 Lowering 或直接 unparse 落到相同 MQL 目标上, 以保证 EX 与下游指标可比。



<a id="01-2"></a>
## 2. 输入规范

> 为何这样设计: NLQ 承担意图信号、schema 锚定命名空间, 两者缺一不可; schema 的呈现形式还决定了 LLM 上下文中字段路径是否可被精确引用, 因此呈现策略要显式规定, 而不仅是"把 JSON 塞进 prompt"。

每条样本包含两类输入:

1. **NLQ**: 一句自然语言 (英文优先), 表达检索意图; 同一意图允许多种改写 (paraphrase) 与 Intent Variant (negation / omission / coreference / jargon / composition) 以覆盖语言多样性。
2. **Schema (Markdown)**: 数据库的层级化描述, 沿 "collection / 字段名 / 类型 / 嵌套层级" 四维展开, 可选附带采样文档片段与字段稀疏度注释。

之所以采用 Markdown 而非裸 JSON, 原因有三: (a) 扁平 JSON 在长上下文中难以直观呈现深层嵌套与数组结构, 模型容易"漏看"; (b) 自然语言无法精准引用形如 `orders.items.supplier.address.city` 的深层路径, 必须由 schema 显式列出; (c) Markdown 与 LLM prompt 模板拼接自然, 层级标题恰好对应 collection / subdocument / field 的嵌套结构。Markdown 由 [src/utils/schema_to_markdown.py](../src/utils/schema_to_markdown.py) 从 MongoDB 元信息生成, 保证同一库在不同样本间的字段顺序、类型标注稳定, 避免模型学到顺序特征而非结构特征。

Schema 的底层数据来源为 MonGen 离线产物 `schema.json` — 由 Schema Exporter 以字段路径为单位, 合并同 collection 内异质文档的字段出现集合与类型分布, 输出 **Union Schema** (详见 02 §2-2-2)。`schema.json` 支持两种导入路径: (a) **离线模式** — 直接加载 JSON 并转 Markdown 进 prompt, 用于数据集构建与离线评估; (b) **在线模式** — 在评估时通过 MongoDB `db.runCommand({listCollections:1})` 在线探测并与离线 Union Schema 做一致性比对, 防止评估环境 schema 漂移造成"输入就错了"的伪评估。两种模式输出同一份 Markdown, 读取口径一致。

Schema 采用**前置注入**而非"按需检索", 是为了控制幻觉: MongoDB 的字段命名远比关系数据库自由 (大小写混用、缩写、单复数交替), 一旦让模型自行猜测, 错误率剧增。前置完整 schema 把字段命名空间转化为强约束, 显著稳定下游 schema linking。对超长 schema (token 预算 > 8k), 再叠加基于 NLQ 的语义检索做二次压缩 (见 RQ2 多视角加权检索) — 检索视角覆盖 db_fields / alias_fields / target_fields / collection / draft MQL 等六通道, 确保在压缩下仍保留关键路径。



<a id="01-3"></a>
## 3. 输出规范

> 为何这样设计: 输出需要同时满足"引擎可执行"与"评估可比较"两类约束, 因此既要给执行型终态 (MQL), 也要给结构型中间态 (fAST + cMRL), 让执行类与结构类指标各自拿到作用对象, 并让难度可量化、可追溯。

输出严格限定于两类形式:

```text
db.<collection>.find(<filter>, <projection>);
db.<collection>.aggregate([<stage_1>, <stage_2>, ...]);
```

MQL 字符串由 fAST 通过 deterministic unparser 产出 — **fAST 是执行真源, MQL 是其线性投影**, 因此所有结构类评估都直接作用于 fAST 以避免字符串级 false diff (空格 / 引号风格 / 运算符短写在 AST 上完全透明), 仅执行类评估必须拿到 unparse 后的 MQL。Lowering (cMRL → fAST) 与 unparse (fAST → MQL) 的规范实现、节点类型表、property-based test 策略详见 [03 §3-3 Lowering (cMRL → fAST)](./03_dataset_construction.md#03-3-3)。

下面以 `ecommerce_017` 主线样本 (NLQ = "Top 3 customers by total paid item spending in 2026.", 建模哲学 = Legacy-drifting) 展示 cMRL → fAST → MQL → EXEC 的四层产出。

**cMRL 意图 (紧凑采样空间, 30 原语内可穷举)**:

```yaml
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
projection: {include: [user_id, total_spent]}
ordering: [{field: total_spent, direction: desc}]
limits: {limit: 3}
features: [F9, F10, F15, F17]
```

**fAST 节点树 (MongoDB AST 完整镜像, 每阶段一节点, 简化显示)**:

```json
{
  "type": "AggregatePipeline",
  "collection": "orders",
  "stages": [
    {"type": "MatchStage", "predicate": {"$and": [
      {"status": {"$eq": "paid"}},
      {"paid_at": {"$exists": true}},
      {"paid_at": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}
    ]}},
    {"type": "UnwindStage", "path": "$items", "preserveNullAndEmptyArrays": false},
    {"type": "GroupStage", "id": "$user_id", "accumulators": [
      {"alias": "total_spent", "op": "$sum", "expr": "$items.price"}
    ]},
    {"type": "ProjectStage", "fields": [
      {"name": "_id", "include": 0},
      {"name": "user_id", "expr": "$_id"},
      {"name": "total_spent", "include": 1}
    ]},
    {"type": "SortStage", "keys": [{"field": "total_spent", "direction": -1}]},
    {"type": "LimitStage", "n": 3}
  ]
}
```

**Gold MQL (由 fAST unparse 得到, 直接可执行)**:

```javascript
db.orders.aggregate([
  { $match: { status: "paid", paid_at: { $exists: true, $gte: ISODate("2026-01-01") } } },
  { $unwind: "$items" },
  { $group: { _id: "$user_id", total_spent: { $sum: "$items.price" } } },
  { $project: { _id: 0, user_id: "$_id", total_spent: 1 } },
  { $sort: { total_spent: -1 } },
  { $limit: 3 }
]);
```

**执行结果 (前 3 行)**:

```text
[
  { user_id: "u_10472", total_spent: "18423.55" },
  { user_id: "u_20188", total_spent: "15209.30" },
  { user_id: "u_00314", total_spent: "14870.00" }
]
```

该样本 `total_spent` 为 Decimal128, 在 JSON 序列化中落为字符串; 激活特性 F9 (Decimal128) / F10 (Date) / F15 (`$exists`) / F17 (`$unwind preserveNullAndEmptyArrays`), IRT 难度 0.42 (medium 桶), Discrimination 0.58。执行结果的字段命名由 `$project` 决定, `_id` 被显式剔除; 若模型输出遗漏 `_id: 0`, 会额外返回 ObjectId 字段, 导致 EVM (结果值集相等) 失败, 但 EFM (结果字段集相等, 允许超集) 通过 — 这类"部分正确"正是多指标并列的价值所在, 也解释了为何 EX 不能作为唯一信号。

输出必须同时满足**结构合法** (符合 MQL 语法) 与 **可执行** (在目标库上跑通且返回非异常结果) 两个约束。两者并非等价: 语法合法的查询可能因字段名拼错、`$unwind` 路径错位、类型不匹配而返回空集或抛 `BSONTypeError`; 反过来, 能跑通的查询若结构偏离 gold (例如 `$match` 与 `$group` 顺序颠倒导致语义漂移) 也不算成功。因此评估需要 EX (执行结果相等) 作为主指标, 再辅以 EFM / EVM / QSM / QFC 覆盖"跑通但错"与"结构像但执行错"两类边界。

三层表示 (cMRL / fAST / MQL) 的责任边界清晰: cMRL 是**采样与训练的统一接口** — MRL Sampler 在 cMRL 空间做特性组合与去重, Intent Mutator 在 cMRL 上做 AST rewrite, 模型评估可选择直接输出 cMRL (更紧凑、更易学) 再由 Lowering 出 MQL; fAST 是**评估的统一接口** — 所有结构类指标、3-way Verifier、Active-Learning 人工审核都以 fAST 为比较对象, 避免字符串级别的 false diff; MQL 是**执行的统一接口** — 任何跑通与否都以 mongosh 实际执行为准, 这是不可替代的最终语义。

为何不让模型直接输出 fAST 跳过 cMRL? 因为 fAST 空间庞大 (原则上无穷, 受 MongoDB 全集约束), 直接作为监督目标会让小模型训练信号稀疏, 并且采样时难以保证特性触达率。cMRL 虽然表达力小, 但 30 原语的空间可以结构化采样 (cross-feature 覆盖、IRT 难度分桶), 让训练集在"难度直方图"上均匀可控。对无法用 cMRL 表达的长尾算子 (`$setWindowFields` / `$graphLookup` / `$bucketAuto` 等), 再退化到 fAST-only 通道, 保证整体不失完整性。



<a id="01-4"></a>
## 4. 与 Text-to-SQL 的本质差异

> 为何这样设计: 任务表面同源, 但数据模型、执行模型与正确性机制各自存在本质差异, 必须先把差异讲清, 才能解释后续为何需要 cMRL + fAST 双层表示, 为什么正确性需要"可证明子集 + 概率验证补足"两段式承担, 而不能照搬 Text-to-SQL 的静态类型检查范式。

| 维度 | Text-to-SQL | Text-to-NoSQL |
| --- | --- | --- |
| 数据模型 | 平铺二维表, 行列固定, 静态 schema | 嵌套文档, 数组 / 对象任意深度, heterogeneous schema 可漂移 |
| 字段命名 | 字段绑定列, 静态可枚举 | `$project` 可动态生成 alias 字段, 脱离 schema |
| 语义基础 | 关系代数 (set-oriented, 子句可重排) | pipeline 语义 (阶段顺序敏感, 每阶段输入文档流即语义) |
| 顺序敏感 | `SELECT` / `WHERE` / `GROUP BY` 由优化器重排 | `$sort → $limit ≠ $limit → $sort`, 阶段顺序即语义 |
| **正确性保证** | 强类型 + 静态解析, 多数错误编译期暴露 | **可证明子集 + 概率验证补足**: cMRL 子集形式语义下可证, fAST-only 长尾不可静态证明, 靠执行验证 + 3-way Verifier 概率保障 |

这里把"正确性机制"做一次展开。传统 Text-to-SQL 依赖关系数据库的强类型系统 — 字段不存在、类型不匹配、子查询列数错位都能在 parse / plan 阶段抛错, 静态即可拦截大部分错误。Text-to-NoSQL 没有这种"编译期安全网": 字段可以在不同文档上动态出现或消失, `$project` 可以凭空造字段, `$unwind` 的路径可以指向嵌套任意深的数组, BSON 类型在运行时做弱强制转换 — 绝大多数"错误"在引擎侧都不报错, 只是返回错结果。因此本基准用两段式承担正确性:

- **可证明子集 (cMRL)**: 对 cMRL 30 原语及其组合, Lowering 编译器的形式语义 $\llbracket \cdot \rrbracket_{\text{sem}}$ 在 Python 原型中独立实现; 对任意 cMRL 程序 $c$ 与任意文档集 $D$, property-based test 验证 $\text{execute}(\text{Lowering}(c), D)$ 与 $\llbracket c \rrbracket_{\text{sem}}(D)$ 返回相同多重集合。100k+ 随机输入全过即视为子集正确性机械证明, 这是 cMRL 子集相对 SQL 的独有优势 — 把正确性从"单元测试覆盖率"升级到"代数恒等式"。该机械证明也等价于一次"编译器形式语义 + 双实现差异测试": Lowering 的参考实现与形式语义实现在随机输入下必须输出一致, 差异即编译器 bug。
- **概率验证 (fAST 长尾)**: cMRL 表达不了的算子 (`$setWindowFields` / `$graphLookup` / `$bucketAuto` / `$merge` / `$facet` 等) 直接以 fAST 入库, 正确性由 3-way Verifier 裁决 + 执行结果比对承担。**3-way Verifier 规则**: 三家异源 LLM (必须来自 ≥3 个不同预训练语料基座) 独立判断 (NLQ, MQL) 是否一致, **3/3 pass** 进主集, **2/3 probable-pass** 进主集但打标并排队人工复核, **1/3 人工仲裁**, **0/3 fail** 丢弃, **三家两两互不一致** 归入 **Ambiguous / Abstain 桶**。Ambiguous 桶不进主训练集但保留用于"识别歧义"副任务训练。
- **最终守门员 (Active-Learning)**: 两段式之外, Execution Grounder 在真实 MongoDB 实例上执行所有样本, 执行结果再与 gold 比对; Active-Learning Reviewer Console 从 probable-pass 与 1/3 仲裁桶中抽样由人工复核, 定期回灌规则修复, 把整体主集错误率压到 **<2%**。

两段 + 最终守门员合力下, 本基准既保留"绝对正确"的下界 (cMRL 子集), 又不对 MongoDB 完整表达力封顶。单一静态分析不足以证明任意 MQL 正确, 但对 cMRL 子集的机械编译可把部分正确性升级为可证明的代数恒等式, 这是本基准不"照搬 Text-to-SQL 范式"的核心原因。

举例: 在 `ecommerce_017` 主线上, 若把 `$match` 的 `paid_at ≥ 2026-01-01` 挪到 `$unwind` 之后再过滤, 查询仍语法合法, 但语义上已让"2026 年内订单"滑到 pipeline 末端过滤, 结果被 `$unwind` 放大再裁剪, EX 崩。Text-to-SQL 下 `WHERE` 与 `SELECT` 可由优化器重排; Text-to-NoSQL 下 pipeline 阶段顺序本身就是语义的一部分。这类错误在静态类型系统下无法被捕获, 只能由执行或形式语义逐步模拟来拒收。



<a id="01-5"></a>
## 5. 任务难点

> 为何这样设计: 难点拆为"结构层"与"意图层"两类, 能让后续合成器与 Intent Mutator 在各自责任边界内承担生成压力, 避免"语法难"和"意图难"混在同一个子模块里处理; 意图层的每一类难点都映射为 cMRL 上的显式 rewrite 规则, 保证 variant 产出在机械可证的范围内。

意图层的六类难点 (negation / omission / coreference / jargon / composition 五类显式 Intent Variant 加上 ambiguity 跨类) 在 MonGen 的合成阶段由 Intent Mutator 显式注入到每个 Sample Family — canonical NLQ 会派生 K 个 Intent Variant, 每条变异规则定义为 cMRL 上的 AST rewrite; 具体变异算子、rewrite 规则表、语义保持单元测试详见 [03 §3-7 Intent Mutator](./03_dataset_construction.md#03-3-7)。

**结构层难点**: 下列结构在 NLQ 中通常不会显式出现, 模型必须从 schema 与示例中推断:

- **动态字段存在性判断**: MongoDB 文档允许字段稀疏出现, 模型需用 `$exists` / `$type` 在运行时识别字段是否存在, 而非默认字段总在。`ecommerce_017` 的 Legacy-drifting 风格就是典型 — 老订单缺 `paid_at`, 必须显式筛出, 否则 `$gte` 比较会把 missing 当作小于任何 Date 处理, 结果错漏。
- **多态类型分支**: 同一字段在不同文档可能是数值 / 字符串 / 数组, 需用 `$type` 判断或 `$convert` 归一化。例如 `price` 在早期文档为 Double, 后期为 Decimal128, 直接 `$sum` 会触发类型混合告警或精度丢失。
- **动态键展开**: 当 key 本身承载数据维度 (日期作 key: `{"2026-01": 120, "2026-02": 98}`) 时, 需用 `$objectToArray` 把 map-style 文档转为 array 才能聚合; 之后还需 `$unwind` 得到 (k, v) 对, 再按 `k` 分组或过滤。
- **`$project` alias 字段**: 形如 `sum_population: {$sum: "$Population"}`, alias 既不在 schema 中, 也不在 NLQ 中, 完全是流水线内部生造; 后续 `$sort` / `$match` 若引用这个 alias, 必须在同一 pipeline 内连续, 且 alias 不能与原字段同名以免触发覆盖歧义。
- **`$unwind` 路径选择**: 同一 collection 可能有多层嵌套数组 (如 `orders.items.modifiers`), 选错路径会导致结果膨胀或空集; `preserveNullAndEmptyArrays: true` 保留无数组或空数组文档, `false` 直接过滤, 语义差异显著。
- **`$expr` 子查询表达式**: 在管道阶段内引用同文档的两个字段 (如 `total > budget`), 要求精确区分外层查询语法 (`$and` / `$gte`) 与内层聚合语法 (`$gt` / `$add`), 两者不能混用。

**意图层难点 (NLQ 侧 6 类, 对应 Intent Mutator 的变异桶)**:

- **歧义 (ambiguity)**: 同一 NLQ 对应多个合法 gold。典型例子是 "best customers" — 可按"下单金额"排序, 也可按"订单数量"排序, 模型必须给出 abstain 信号或按领域词典选择默认度量。MonGen 对这类样本打 `ambiguity=true` 标记, 允许 2 个以上 gold MQL, 采用 EX-any (任一 gold 通过即算过) 评估。
- **省略 (ellipsis / omission)**: 以 `ecommerce_017` 主线为例, canonical NLQ "Top 3 customers by total paid item spending in 2026." 的 omission 变体可写为 "**列出 2026 年消费最高的三个客户**" — 省略了 "paid" / "items" / "spending" 等关键限定, 模型必须同时从三条线索推断出与 canonical 等价的 cMRL: (a) 从 schema 中 `orders.status` 的枚举值推断 "消费" 默认指 status=paid 的订单; (b) 从 `orders.items[].price` 的存在推断度量要沿 items 展开求和; (c) 从 "2026 年" 推断 `paid_at` 的 Date 过滤。该 variant 的 cMRL 与 canonical 完全相同, gold MQL 也相同, 但 NLQ 变短、槽位减少, 是衡量模型槽位填补能力的核心信号。omission variant 在 EX 上的 gap 通常比 canonical 高 10-15 个百分点, 这正是 Intent Variant 被放入 Sample Family 的价值。
- **指代消歧 (coreference)**: "recent orders" / "that customer" / "the same region" 等指代依赖前文或部署默认值 (1 周 / 30 天 / 1 年), 模型对这类模糊词需有显式策略: 先查业务词典, 次以 schema 采样统计推断典型窗口, 最后才诉诸 LLM 常识。
- **黑话 (jargon)**: "VIP clients" / "whales" / "GMV" / "churn" 等术语需要显式词典映射, 不能依赖 LLM 常识 — 同一个词在不同租户域下可指不同度量 (电商 "GMV" vs 游戏 "GMV" 的计算口径不同)。MonGen 通过 `domain_glossary.yaml` 把这类映射固化为数据的一部分, 模型训练时必须显式读取。
- **否定 (negation)**: "哪些客户**没有**取消过订单" 需生成 `$nin` / `$not` / `$ne`, 且要精确区分"从未取消" (allMatch, 通常用 "not in cancelled_users") 与"存在未取消" (anyMatch, 对每条订单做行级过滤) — 两种语义在 MQL 里是截然不同的管道结构, allMatch 要求客户级聚合后筛选, anyMatch 只需订单级过滤; 前者基数下界是"至少下过单的客户", 后者基数上界是"所有非取消订单条数"。
- **复合 (composition)**: 将多个 canonical 意图组合为一条 NLQ, 例如 "2026 年消费最高的 3 个客户, 再列出他们的最常下单时段" — 需要两段 pipeline 或 `$facet` / 变量传递。复合样本难度显著提高, IRT 难度桶多落在 L4 - L5; composition 的 rewrite 规则需要对两个 cMRL 做合并后重新 Lowering, 这是 Intent Mutator 中最复杂的分支。

这 6 类意图难点在 MonGen 内部不是抽象标签, 而是**可机械化生成的 cMRL AST rewrite 规则**。以 omission 为例, rewrite rule 是"以概率 $p$ 删除 cMRL 里一个非必要 filter 原语并在 NLQ 层删除对应的自然语言限定", 每条 rewrite rule 都附带单元测试证明"变异后的 variant NLQ 在 gold MQL 下仍 EX 通过"。Intent Variant 的显式机械化让 Sample Family 结构 (1 canonical + K variants) 在训练期提供强一致信号 — 同一 family 内的样本共享 fAST, 模型若在 canonical 上通过却在 variant 上失败, 就能精确定位到意图层的薄弱点。

**为什么 NLQ 侧难点必须显式建模?** 结构层错误通常"跑不通", 容易被 EX 捕获; 而意图层错误经常"跑通但结果错误"。例如 negation 写成 `$ne` 而非 `$nin` 会漏掉空数组语义差, 结果数量级一致但内容错位; 这类"执行成功但语义错误"只能依赖 EFM (结果字段集) 与 EVM (结果值集) 双通道交叉验证, 并在数据集层通过 Intent Variant 的覆盖强制模型面对这些边界, 不能在评估环节打补丁解决。

两类难点也各自对应不同的数据来源: 结构层难点在 Synth 子集中由 17 特性 Checklist 显式覆盖 (F1 - F17 每特性触达率 ≥5%, 二元组 ≥60%, 三元组 ≥30%); 意图层难点在 Hybrid 子集中由"真实意图骨架 × 合成异质库"组合生成, 由 Real 子集提供意图原型。三子集分工承担不同难点维度, 使训练集既不偏科, 也有交叉验证的统计基础。



<a id="01-6"></a>
## 6. 研究问题

> 为何这样设计: 研究问题既要覆盖小模型可行性 (RQ1)、检索策略 (RQ2)、反馈闭环 (RQ3) 三个工程核心, 也要回答 MonGen 作为基准的两个外部命题 — 跨子集外部有效性 (RQ4) 与 cMRL 子集可证明正确性 (RQ5)。

RQ4 的原则基础见 [02 §1-5 externally-anchored](./02_dataset_design.md#02-1-5) — externally-anchored 明确要求基准至少有一个子集提供真实 workload 的意图锚点, 用以打破纯合成管线的自循环, 这是 RQ4 外部有效性评估的数据学前提。RQ5 的基础则在 cMRL + fAST 双层表示本身 — 对 cMRL 封闭子集做形式语义 + property-based test, 才能"机械证明" gold 正确。

围绕 MonGen 基准及其配套方法论的核心权衡, 提出五个研究问题:

- **RQ1 (schema linking 小模型可行性)**: 能否用小规模专用模型 (~1B 参数级) 替代 LLM 完成 schema 预测?
  - 实验方向: 全参数微调 4 个独立小模型, 分别预测 collection / db_fields / alias_fields / target_fields; 对比 LLM zero-shot 与 few-shot 基线在字段级命中率 (precision / recall / F1) 上的差距; 评估小模型在推理延迟与显存占用上的优势 (batch=4)。
  - 预期: 小模型专精在 precision 上追平 LLM, recall 上低 5-8 个百分点, 但推理延迟与显存降低 10× 以上, 满足边缘部署要求。

- **RQ2 (多视角加权检索)**: 多视角加权检索是否优于单视角 NLQ 检索?
  - 实验方向: 在共享嵌入库上, 对 NLQ / db_fields / alias_fields / target_fields / collection / draft MQL 六视角分别赋权 (1.0 / 0.7 / 0.5 / 0.5 / 0.7 / 0.3), Top-K = 20 召回; 与"仅 NLQ 余弦"基线在下游 EX / QSM 上对比。
  - 预期: 多视角加权在 alias_fields / target_fields 稀疏的 NoSQL 场景提供 5-10 个百分点 EX 增益, 但在 schema 极简的小库上可能劣化 (视角内噪声)。

- **RQ3 (执行反馈闭环)**: 执行反馈闭环是否能进一步提升 EX / EFM / EVM?
  - 实验方向: 以 refinement 后输出为基线, 叠加 mongosh 执行 + 结果差异分析的 Debug Agent 闭环 (小 / 中型 LLM, temperature = 0.0), 消融"无 / 有执行反馈"两组指标, 量化 EX 相对增益。
  - 预期: Debug Agent 把顺序敏感错误 (例如 `$sort / $limit` 倒置) 从 15-20% 压到 <5%, 整体 EX 提升 8-12 个百分点, 代价是推理延迟翻倍。

- **RQ4 (外部有效性 — Synth↔Real gap)**: 在 MonGen-Synth 上训练的模型能否零样本迁移到 MonGen-Real? Real ↔ Synth 的 EX gap 能否压到 15 个百分点以内?
  - 实验方向: 分别在 Synth / Real / Hybrid 上训练, 在另外两个子集上测试, 形成 3×3 迁移矩阵; 消融"是否在 Real 上加做 LoRA fine-tune"对 gap 的影响; 报告三子集各自 IRT 难度分布, 以剔除"难度漂移"而非"分布漂移"造成的 gap 伪影。
  - 预期: Synth → Real 零样本 EX gap 初始约 20-25 个百分点, 加入 Hybrid 共训练后压到 10-15 个百分点以内; gap 主要来自 Intent Variant 分布差异而非结构层差异, 证明 Intent Mutator 覆盖的意图维度比结构维度更易泛化, 而 Hybrid 子集是弥合 Synth↔Real 语言分布差异的关键支点。

- **RQ5 (可证明正确性 — cMRL Lowering 机械证明)**: cMRL 子集的 Lowering 编译器能否通过形式语义 + property-based test 证明正确, 为整个基准 gold 提供"绝对正确"的下界?
  - 实验方向: 对 cMRL 30 原语各自生成 ≥10k 次随机输入, 比对 $\text{execute}(\text{Lowering}(c), D)$ 与 $\llbracket c \rrbracket_{\text{sem}}(D)$; 100k+ 测试全过即视为子集正确性机械证明, 任一失败则编译器打回重构; 测试覆盖需包含空文档集合、单文档、千文档、极端类型 (Decimal128 边界值 / ISODate 闰年 / null-vs-missing) 四类输入分布。
  - 预期: 初期 Lowering 实现预计有 3-5 处反例 (主要在类型强制转换与 null-vs-missing 处理), 修复后全绿; 反例进入回归测试集保证不倒退; 为 Synth 子集的 100% gold 正确性提供机械凭证, 也为 Real / Hybrid 子集的 fAST-only 长尾提供"无法机械证明"的显式边界。

**研究问题预期贡献**: RQ1 验证 schema linking 可脱离 LLM, 用 1B 量级小模型专精; RQ2 验证多视角检索在 schema 稀疏域下优于单视角; RQ3 验证执行反馈闭环是 Text-to-NoSQL 不可或缺的最终守门员; RQ4 把 MonGen 从"合成基准"升级为"外部有效基准", 让方法论对真实 workload 具备可泛化性; RQ5 把 cMRL 子集的编译正确性从"经验上可信"推到"形式上可证", 为整个三子集的 gold MQL 提供机械保证。

五个 RQ 之间有纵向依赖: RQ5 提供 MonGen-Synth gold 的正确性底座; RQ4 在三子集上测量训练信号的外部有效性; RQ1 - RQ3 是方法论链条的三个组成 (schema 预测 → 检索增强 → 执行反馈), 不可单独评估。因此实验计划先完成 RQ5 的 property-based test 全绿, 再跑 RQ4 的 3×3 迁移矩阵, 最后在胜出的训练组合上对 RQ1 - RQ3 做消融。



<a id="01-7"></a>
## 7. 范围与限制

> 为何这样设计: 范围约束明确排除不打算覆盖的系统与语义域, 限制条款显式标出已知能力上限; 特别地, fAST-only 长尾样本显式纳入 scope 并说明其正确性承担方式, 让读者一眼判定某一需求是否在基准职责内。

- **仅只读查询**: insert / update / delete / replaceOne 等写操作不在范围内。写路径涉及一致性、并发与幂等性, 与查询任务的评估维度正交, 合并会稀释 EX 的信噪比。
- **目标引擎: MongoDB 7.0+, 含 7.0 之后所有 Release Note 算子**。cMRL 覆盖常用 30 原语, fAST 镜像 MongoDB 全集 (包含 `$setWindowFields` / `$densify` / `$fill` / `$facet` / `$merge` / `$graphLookup` / `$bucketAuto` 等)。MongoDB 新版本发布 Release Note 后, cMRL 原语表每季度扩充一次, 不入 cMRL 的新算子仍可通过 fAST 直接生成。跨主版本的语法变更 (例如 7.0 引入的 time-series collection 原生聚合) 需要回归 property-based test 套件。
- **fAST-only 长尾样本显式纳入 scope**。对涉及 `$setWindowFields` / `$graphLookup` / `$bucketAuto` / `$merge` / `$facet` 等 cMRL 暂未覆盖的算子, 采用两条采集通道: (a) **MonGen-Real 的真实 MQL 挖矿** — 从开源代码 / 公开论坛挖出的真实查询若用到 cMRL 外算子, 直接保留 fAST 入库; (b) **MonGen-Hybrid 的组合重投** — 把 Real 里提取出的意图骨架重新映射到合成异质库上, 若骨架展开后需要 cMRL 外算子则同样以 fAST 入库。两类样本在 fAST 层直接入库, **不依赖 cMRL 的可证明性, 正确性仅靠执行验证 + 3-way Verifier + Active-Learning 承担**, provenance 字段显式标注 `fast_only=true`。fAST-only 样本**不参与 Synth 的 IRT 难度直方图硬约束** (难度只按 Synth 分桶限制), 但参与 RQ4 外部有效性评估的主要通道。
- **不涉及 schema 设计与索引优化**: 给定 schema 视为固定约束, 不重新建模; 索引缺失导致的性能问题不纳入 EX 判定 — 正确的 MQL 即便慢也算 EX 通过。
- **不支持事务与 change stream**: multi-document transaction 改变一致性语义, change stream 属流式订阅而非查询, 都与"只读查询"的任务域正交。
- **仅覆盖 MongoDB**: 不扩展至 Couchbase / DynamoDB / Cassandra 等; 这些系统在文档模型上的方言差异足以单独立项, 但本基准的表示层设计 (cMRL + fAST 双层) 理论上可平移到其他文档型引擎。
- **NLQ 语言范围**: 当前仅覆盖英文 NLQ; 中文及其他语言支持延后, 详情见 §Y 未尽事项。



<a id="01-X"></a>
## X. 主要构件清单

> 为何这样设计: 清单按"数据资产 → 双层表示规范与编译器 → 采样与生成器 → 验证器与评估器 → 辅助工具"顺序排列, 对应读者从"拿数据→读规范→复现生成→复现评估"的自然路径; 每条构件给出一句话职责与其在 03 文档中的 anchor, 便于跨文档定位。

**A. 数据资产** (离线落盘, 由构建流水线产出):

| 构件 | 路径 | 一句话职责 |
| --- | --- | --- |
| MonGen-Synth 训练 / 测试集 | [MonGen/synth_train.json](../MonGen/synth_train.json), [MonGen/synth_test.json](../MonGen/synth_test.json) | 16,000 families × K variants, 17 特性覆盖由硬约束保证 |
| MonGen-Real 样本 | [MonGen/real.json](../MonGen/real.json) | 目标 ~4,000 (3,000-5,000 区间) 挖矿样本, 含 fAST-only 长尾 |
| MonGen-Hybrid 样本 | [MonGen/hybrid.json](../MonGen/hybrid.json) | 2,000 真实意图骨架 × 合成异质库组合样本 |
| MongoDB 库 (Synth) | [MonGen/mongodb_data/](../MonGen/mongodb_data/) | 220 异质库, 10 业务域 × 6 Modeling Style 组合 |
| MongoDB schema (Synth) | [MonGen/mongodb_schema/](../MonGen/mongodb_schema/) | 与 data 目录一一对应的 Union Schema JSON |

**B. 双层表示规范与编译器** (对应 03 §3 cMRL + fAST 双层表示):

| 构件 | 路径 | 一句话职责 | 03 anchor |
| --- | --- | --- | --- |
| cMRL 规范 | [dataset_construct/cmrl_spec.yaml](../dataset_construct/cmrl_spec.yaml) | 30 原语的 schema、类型、语义注释 | 03-3-1 |
| fAST 规范 | [dataset_construct/fast_spec.py](../dataset_construct/fast_spec.py) | MongoDB AST 节点类型表与 unparse 规则 | 03-3-2 |
| Lowering (cMRL → fAST) | [dataset_construct/lowering.py](../dataset_construct/lowering.py) | 确定性编译 cMRL 到 fAST | 03-3-3 |
| Lifting (fAST → cMRL) | [dataset_construct/lifting.py](../dataset_construct/lifting.py) | 尽力把 fAST 逆向到 cMRL, 允许失败 | 03-3-4 |
| 形式语义 + 差异测试 | [dataset_construct/cmrl_semantics.py](../dataset_construct/cmrl_semantics.py) | cMRL 代数语义 Python 原型, 支撑 property-based test 与双实现差异测试 | 03-3-5 |
| Skeleton Compiler | [dataset_construct/skeleton_compiler.py](../dataset_construct/skeleton_compiler.py) | 从 cMRL 提取结构骨架供 Hybrid 子集重投 | 03-3-8 |

**C. 采样与生成器** (对应 03 §2 正向构建 + §3-6 Sampler + §3-7 Mutator):

| 构件 | 路径 | 一句话职责 | 03 anchor |
| --- | --- | --- | --- |
| MRL Sampler | [dataset_construct/mrl_sampler.py](../dataset_construct/mrl_sampler.py) | 在 cMRL 空间做覆盖率硬约束下的加权采样 | 03-3-6 |
| Intent Mutator | [dataset_construct/intent_mutator.py](../dataset_construct/intent_mutator.py) | 对 canonical NLQ 产生 K 个 Intent Variant (5 类变异) | 03-3-7 |
| Event Planner | [dataset_construct/event_planner.py](../dataset_construct/event_planner.py) | 由产品文档挖矿出事件模板, 约束 Accreter 沉积轨迹 | 03-2-2 |
| Document Accreter | [dataset_construct/doc_accreter.py](../dataset_construct/doc_accreter.py) | 事件驱动沉积生成异质文档 (schema drift 源头) | 03-2-3 |
| Modeling Style Skew | [dataset_construct/modeling_skew.py](../dataset_construct/modeling_skew.py) | 按 6 种 Modeling Style 做异质采样倾斜 | 03-2-4 |
| Schema Exporter | [dataset_construct/schema_exporter.py](../dataset_construct/schema_exporter.py) | 从 MongoDB 导出 Union Schema 并合并异质结构 | 03-2-6 |
| fAST Parser | [dataset_construct/fast_parser.py](../dataset_construct/fast_parser.py) | 解析真实挖矿到的 MQL 字符串为 fAST | 03-4-2 |

**D. 验证器与评估器** (对应 03 §7 6 道防线 + 03 §8 IRT):

| 构件 | 路径 | 一句话职责 | 03 anchor |
| --- | --- | --- | --- |
| MRL Validator | [dataset_construct/mrl_validator.py](../dataset_construct/mrl_validator.py) | cMRL 语法 / 类型 / 引用合法性静态检查 | 03-7-1 |
| 双编译器差异测试 | [dataset_construct/diff_test.py](../dataset_construct/diff_test.py) | Lowering 参考实现 vs 形式语义实现的差异测试 | 03-7-2 |
| Execution Grounder | [dataset_construct/exec_grounder.py](../dataset_construct/exec_grounder.py) | 每条样本在真实 MongoDB 实例上执行并落盘结果 | 03-7-3 |
| 3-way Reverse Verifier | [dataset_construct/reverse_verifier_3way.py](../dataset_construct/reverse_verifier_3way.py) | 三家异源 LLM 独立判断 (NLQ, MQL) 一致性 + 多数决 | 03-7-5 |
| Active-Learning Reviewer Console | [dataset_construct/al_console.py](../dataset_construct/al_console.py) | 人工复核入口, 驱动主集错误率 <2% | 03-7-6 |
| IRT Scorer | [dataset_construct/irt_scorer.py](../dataset_construct/irt_scorer.py) | 用 8-12 个 pilot 模型打分, 输出 difficulty + discrimination | 03-8-1 |

**E. 辅助工具**:

| 构件 | 路径 | 一句话职责 |
| --- | --- | --- |
| Schema → Markdown 转换器 | [src/utils/schema_to_markdown.py](../src/utils/schema_to_markdown.py) | 把 Union Schema 转为 LLM prompt 友好的 Markdown |
| 指标实现入口 | [src/utils/metric.py](../src/utils/metric.py) | EX / EFM / EVM / QSM / QFC 的统一实现入口 |



<a id="01-Y"></a>
## Y. 未尽事项与已知风险

> 为何这样设计: 把已知 TODO 与识别出的风险合并列出, 每条显式标记"状态 + 对策", 让团队在每次评审时逐条过清; 同一项如果既是 TODO 又构成潜在风险, 合并为一条并说明两侧影响。

- **中文 NLQ 支持尚未覆盖**。当前 Intent Mutator 仅对英文 NLQ 做 AST rewrite, paraphrase 样本也是英文单语; 中文支持需要重新训练一套中文同义改写器、扩充 `domain_glossary.yaml` 的中文映射、并评估中文指代 / 省略在文化上的差异。状态: 延后到下一轮 release, 不阻塞当前主集评估。对策: 在样本 schema 中预留 `lang` 字段, 未来中文样本以 `lang=zh` 标记, 不混入英文主集评估, 保证评估指标的语言可比性。

- **cMRL 形式语义尚未完全覆盖 fAST 全集**。Lowering 的形式语义只对 cMRL 30 原语机械证明正确, 对 fAST 的 `$setWindowFields` / `$graphLookup` / `$densify` / `$fill` / `$bucketAuto` / `$merge` / `$facet` 等算子不做形式语义覆盖 — 即这些算子上的 gold 正确性由"执行 + 3-way Verifier"承担, 不声称"机械证明"。状态: 长期 TODO, 优先级低于 cMRL 原语扩展。对策: fAST-only 样本在 provenance 中显式标注 `fast_only=true` 与 `provable=false`, 评估报告中 Synth (可证) 与 Real / Hybrid (概率) 的 EX 分别汇总, 避免把两类正确性混在同一数字里稀释含义。

- **IRT pilot 模型选型可能引入 bias**。当前 8-12 个 pilot 模型若集中在同一模型家族 (同一预训练语料 / 同一 tokenizer), IRT 难度评估会系统性偏斜, 即 pilot 共同薄弱的样本被高估难度。状态: 已识别风险, 未定量。对策: 强制 pilot 覆盖 ≥3 个不同预训练语料基座, 公开 pilot 清单与其预训练来源; 每季度重采 pilot 组合, 监测 IRT 难度重校准前后的偏移, 重校准偏移 > 0.1 的样本触发 Active-Learning 复核。

- **Real 子集规模受限于合规清洗产能**。GitHub / Stack Overflow 挖矿样本必须过许可证筛查、PII 脱敏、敏感字段抹除三道人工关, 人工产能是瓶颈。状态: 3,000-5,000 目标是"现实产能区间", 目标值设定为 4,000; 若不达标触发降级预案至 2,500。对策: 预案启用 Hybrid 补位 (Hybrid 样本由真实意图骨架 + 合成异质库生成, 不依赖合规清洗), 评估报告显式说明 Real 规模与目标的 gap, 并在 RQ4 迁移矩阵中给出 Real 规模敏感性分析。

- **长尾算子的 Lifting 尚为近似**。Lifting 对 `$setWindowFields` / `$graphLookup` 等算子的反投影是手写规则 + 最佳努力, 不保证 $\text{Lift}(\text{Lower}(c)) = c$ 的恒等关系 (只对 cMRL 闭包内成立)。状态: 已识别局限, 预计长期保持近似。对策: 对 Lift 失败的样本保留为 fAST-only, 不混入 cMRL 训练通道, 避免"近似 Lift 污染训练信号"; Lift 部分成功的样本在 provenance 标注 `lift_approx=true`, 在 cMRL 级评估时作为 soft 信号。

- **"执行成功但语义错误"的判定规则仍为近似**。目前以 EFM + EVM 双通过作为语义正确的近似判据, 但 column-wise / row-wise 细粒度对齐指标尚未设计, 对 RQ3 执行反馈增益评估有少量噪声影响。状态: 长期 TODO。对策: 定期随机抽样人工复核 "EX 通过 / EFM 未通过" 与 "EX 未通过 / EFM 通过" 两类边界样本, 量化 EFM + EVM 的假阳 / 假阴率, 并把量化结果回灌到评估报告的置信区间中。
