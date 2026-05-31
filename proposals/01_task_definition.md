# 01 · 任务定义 (Task Definition)

> TEND 公理层文档。本文定义任务的形式化签名、输出空间约束、正确性锚 (gold-as-class)、归一化契约、递归相等 ≡_rec，以及 instance 层根原则 P1–P4。下游文档 (02/03/04/05/06) 的一切规范必须与本文自洽；任何与本文冲突的下游段落视为下游 bug，而非本文规范更新。本文只回答「一个合法任务实例在形式上必须是什么」，不负责构造流水线细节。

---

## Part I

## TL;DR

TEND (Text-to-NoSQL benchmark for Mongo-flavored pipelines) 研究自然语言查询意图如何被可执行的 MongoDB 聚合管道精确表达。任务签名是 f(NLQ, S, db_id) → q^MQL：给定单条自然语言查询、完整 Schema 与 BIRD mini-dev 数据库标识，模型须输出 mongosh 可执行的 MQL 字符串。

TEND 以 BIRD mini-dev (11 库) 为数据源与场景源，由十一智能体流水线 (WP / SRA / SC / DM / QPS / MS / MUT / PV / NLP / RTV / NNC / RA) 产出 record。Phase B 采用 RAR 逆向工程构造 NL–MQL 对：先枚举异构驱动 `intent`，再以参照 R 锁死 gold MQL，最后从 intent 生成 NLQ。Agent 框架在构造期直接保证 P1 执行良构、P3 判别力、P4 世界非平凡性；P2 语义唯一性由 intent 控制 + RTV 结果级往返闭包 + NNC 歧义攻击共同承担。

正确性不以 MQL 字面相等为锚，而以 gold-as-class 等价类判定。每条 record 携带 canonical_form_set 四元组 (must_contain / must_not_contain / must_contain_at_root / must_not_contain_at_root) 与 canonical representative (MQL 字段)。预测 q_p 属于 gold-class 当且仅当 EX 双条件合取成立：AST_check 静态通过，且 NormExec(q_p, D) ≡_rec NormExec(q_g, D)。NormExec = Norm ∘ Exec ∘ Parse，所有执行层比较一律基于归一化结果，不直接比较原生 BSON。

输出空间须满足 read-only、deterministic、mongosh-executable 三条核心性质，并禁六件 operator ($sample, $rand, $$NOW, $out, $merge, $function)。Norm 四层契约 (标量 / 复合 / null-vs-missing / _id + shape-preserving) 将 Exec 结果投射到 R*；≡_rec 在 R* 上递归定义标量、字典、列表、顶层四层相等。评测主指标 EX 是唯一语义锚；EM / QSM / QFC / EFM / EVM / QIM 均为诊断 proxy，详见 [05 §2](./05_evaluation_methodology.md#05-2)。

Canonical 锚 (pending DAR Phase A 执行验证) financial/1001 贯穿全文：L4 难度、$lookup + $addFields + $cond/$type 结构约束、preserve shape_policy、稀疏 loan + 多态 trans 异构，以及 world_signature 冻结 witness 快照。record 字段契约、覆盖轴见 [02 §2](./02_dataset_design.md#02-2)；BIRD mini-dev 锚定数据世界见 [03 §3](./03_dataworld_construction.md#03-3)；Agent 框架见 [04 §2](./04_agent_framework.md#04-2)。

<a id="01-0"></a>
## §01-0 摘要

TEND 刻画如下映射的学习与评测问题：

$$
f:\ (\texttt{NLQ},\ S,\ \texttt{db\_id})\ \longrightarrow\ q^{\text{MQL}}
$$

与关系式 Text-to-SQL 基准的根本区别在于：NoSQL 数据世界原生包含嵌套文档、数组字段、稀疏列与混合类型，其意图空间本质上不是 SQL AST 的子集。因此 TEND 不以 MQL 字面相等作为正确性锚，而以 **gold-as-class 等价类** + **EX 双条件** + **Norm 四层** + **≡_rec** + **P1–P4 根原则** 作为公理层。

### 规模与基准

具体 record 数、NLQ 档位由 [02 §2](./02_dataset_design.md#02-2) 与 [02 §3](./02_dataset_design.md#02-3) 锁定。基准为 test-only：BIRD mini-dev 的 11 个 db_id 全部作为 test 提供数据与场景，无 train 切分；OOD 由「构造期不向 solver 暴露」保证，而非 train/test 域切分。每条 record 提供 canonical 与 colloquial 二联 NLQ。

### 本文五项核心承诺

1. **§01-1 任务签名**：IO 三元组与 Parse / Exec / Norm / NormExec 算子链。
2. **§01-2 输出空间约束**：read-only / deterministic / mongosh-executable 与六件禁用 operator。
3. **§01-3 正确性锚**：gold-as-class、AST_check、EX 双条件、三层构造期保证 (L1–L3)。
4. **§01-4 Norm 契约**：四层归一化规则。
5. **§01-5 + §01-6**：≡_rec 递归相等与 P1–P4；Agent 框架直接承担构造期 P1 / P3 / P4 验证（无 V_triple 映射）。

### 本文不负责的内容

| 主题 | 跳转 |
|------|------|
| 发布物目录、record 字段 schema、world_signature | [02 §2](./02_dataset_design.md#02-2) |
| BIRD mini-dev 锚定 schema / 数据迁移 / SRA 设计 | [03 §3](./03_dataworld_construction.md#03-3) |
| Phase B Agent 框架、canonical_form_set 派生、mutations | [04 §4](./04_agent_framework.md#04-4) |
| 七指标公式、4-panel 报表 | [05 §2](./05_evaluation_methodology.md#05-2) |
| SMART schema-less agentic 双智能体 / 四阶段解法 | [06 §1](./06_solution_design.md#06-1) |

<a id="01-1"></a>
## §01-1 任务签名

<a id="01-1-1"></a>
### §01-1-1 输入空间

任务输入为三元组 $(\texttt{NLQ},\ S,\ \texttt{db\_id})$：

- **NLQ**：单条自然语言查询。须满足单一闭包性（无多轮指示、无上下文代词链）、只读语义（不含写操作意图）、封闭引用（实体 / 属性 / 关系全部落在 S 内）。每条 record 提供 canonical 与 colloquial 二联 NLQ，评测默认以 canonical 为主、colloquial 为鲁棒性子集（字段定义见 [02 §2](./02_dataset_design.md#02-2)）。
- **S**：`db_id` 对应 MongoDB 数据库的完整 Schema——集合树、字段类型、嵌套路径、SRA 设计 rationale。由 [03 §3](./03_dataworld_construction.md#03-3) 的 SRA 产出。
- **db_id**：BIRD mini-dev 数据库标识符，索引 S 与冻结快照 D(db_id)。

形式化：设 $\mathcal{N}$ 为合法 NLQ 集合，$\mathcal{S}$ 为合法 Schema 集合，$\mathcal{I}$ 为合法 db_id 集合，则

$$
\mathcal{X} = \big\{\ (n, S, i)\ \in\ \mathcal{N} \times \mathcal{S} \times \mathcal{I}\ \big|\ S = \text{schema}(i)\ \big\}.
$$

<a id="01-1-2"></a>
### §01-1-2 输出空间

任务输出 $q^{\text{MQL}}$ 是一段 mongosh 可执行的 MongoDB 聚合管道字符串，通常形如 db.collection.aggregate([ stage_1, … ])；亦允许 db.collection.find(...) 当且仅当查询可退化为单 filter + 单 projection。输出空间记作 $\mathcal{Q}$，须通过 §01-2 的性质过滤与禁用 operator 扫描。

<a id="01-1-3"></a>
### §01-1-3 数据快照 D(db_id)

D(db_id) 是 db_id 在评测时刻的冻结 BSON 快照：该库下所有 collection 的全量镜像。同一 db_id 下所有 record 共享同一 D；不同 db_id 各自独立。数据由 [03 §4](./03_dataworld_construction.md#03-4) 的 DM (Data Migrator) 从 BIRD mini-dev 关系实例迁移而来，并经 RA (Realism Auditor) 审计。

world_signature 为 D(db_id) 经 canonical_text 序列化后的 SHA-256 摘要，保证评测可重现（定义见 [02 §2](./02_dataset_design.md#02-2)）。若 mutations 或 augment 更新 D，须重算 world_signature 并写入 record 日志。

<a id="01-1-4"></a>
### §01-1-4 Parse / Exec / Norm / NormExec

| 算子 | 签名 | 含义 |
|------|------|------|
| Parse | $\mathcal{Q} \to \mathcal{A} \cup \{\bot\}$ | mongosh 字符串 → AST；失败返回 ⊥ |
| Exec | $\mathcal{A} \times \mathcal{D} \to \mathcal{R} \cup \{\bot_{\text{exec}}\}$ | 在 D 上执行，返回原生结果；异常返回 ⊥_exec |
| Norm | $\mathcal{R} \to \mathcal{R}^\ast$ | 四层归一化，见 §01-4 |
| NormExec | 复合 | NormExec(q, D) ≜ Norm(Exec(Parse(q), D)) |

若 Parse(q) = ⊥ 或 Exec = ⊥_exec，则 NormExec(q, D) = ⊥。下游所有执行层相等判定一律基于 NormExec，从不直接比较原生 Exec 结果。

<a id="01-2"></a>
## §01-2 输出空间约束

<a id="01-2-1"></a>
### §01-2-1 三条核心性质

| 性质 | 含义 | 违反后果 |
|------|------|---------|
| P_ro (read-only) | Exec 不改变 D 的持久状态 | 污染 witness，跨 record 状态泄漏 |
| P_det (deterministic) | 固定 D 下 NormExec 确定 | EX 成为概率事件，不可复现 |
| P_mxe (mongosh-executable) | 合法 mongosh，不依赖 serverSideJS | 跨部署不可执行 |

P_mxe 是前提；P_ro 与 P_det 并行且合取为准入关卡。

<a id="01-2-2"></a>
### §01-2-2 六件禁用 operator

| operator | 主要破坏 | 禁用理由 |
|----------|---------|---------|
| $sample | P_det | 随机抽样不可重现 |
| $rand | P_det | 纯随机数 |
| $$NOW | P_det | 墙钟时间 |
| $out | P_ro | 写入 collection |
| $merge | P_ro | 写回 merge |
| $function | P_mxe | 需 serverSideJS |

$lookup、$graphLookup 等同库引用 operator 不禁。预测 q_p 经 disabled_operator_scanner 静态扫描：命中任一禁用 operator 则 AST_check = fail，q_p ∉ gold-class，无论 NormExec 是否凑巧匹配。

相关但不禁：$lookup（D 含同库全 collection）；$graphLookup（深度由 schema 约束）；$where 为 gray zone，NNC 倾向重写为纯 aggregation。

<a id="01-2-3"></a>
### §01-2-3 指标层级

| 指标 | 层级 | 说明 |
|------|------|------|
| EM / QSM / QFC | 字面 proxy | 字符串 / AST / 字段集 |
| EFM / EVM | 执行 proxy | 部分结果匹配 |
| QIM | 结构 proxy | AST 主干对齐 |
| **EX** | **唯一语义锚** | AST_check pass ∧ NormExec ≡_rec |

Leaderboard 以 EX 为准（[05 §4](./05_evaluation_methodology.md#05-4)）。七指标须同时披露，但仅 EX 具有语义权威性。

<a id="01-3"></a>
## §01-3 正确性锚 (gold-as-class)

<a id="01-3-1"></a>
### §01-3-1 canonical_form_set 与 EX 双条件

**核心陈述**：gold 不是单条 MQL 字面，而是等价类 gold-class(r)，由 record 的 canonical_form_set 四元组定义：

- must_contain：AST 任意深度须出现的 operator 集合
- must_not_contain：任意深度禁止的 operator 集合
- must_contain_at_root：pipeline 顶层 stage 须包含的 operator 集合
- must_not_contain_at_root：顶层禁止的 operator 集合

派生算法由 [04 §4](./04_agent_framework.md#04-4) 的 MS 机械派生、NNC 确认；Glossary 别名见 [GLOSSARY](./_meta/GLOSSARY.md#canonical_form_set)。

> **RAR cfs 最小化原则**：canonical_form_set 只承载 **idiom-不变量 + output-space 守卫**——`must_not_contain` 恒含六件禁用 operator；`must_contain` / `must_contain_at_root` 仅收**所有正确 idiom 共有的不可避免算子**（如该 schema 下唯一的关联手段 `$lookup`、唯一的窗口/分面结构算子）与 **shape_policy 守卫**（如 `preserve` 下根禁 `$unwind`/`$group`）；**不**锁 idiom 特定的可替换算子（`$addFields`↔`$project`、`$cond`↔`$switch`↔`$ifNull`），否则误杀等价解。「是否正面处理了异构」这一**结构判别力交由 witness（L2/P3）** 承担——近似错解在 rich witness 上 `≡_rec` 必败，无须 cfs 重复 police。派生细节见 [04 §04-3-2](./04_agent_framework.md#04-3-2)。

**gold-class 成员判定**（EX 双条件）：

$$
q_p \in \text{gold-class}(r)\ \iff\
\begin{cases}
\text{AST\_check}(q_p,\ r.\texttt{canonical\_form\_set}) = \text{pass}, \\
\text{NormExec}(q_p,\ D) \equiv_{\text{rec}} \text{NormExec}(q_g,\ D).
\end{cases}
$$

其中 q_g 为 record.MQL（canonical representative）。其余等价类成员通过结构侧 AST_check 与执行侧 NormExec ≡_rec 的合取被匿名接受。

**为何双条件缺一不可**（RAR 职责划分）：

- 仅 NormExec：可能接受 $sample 等禁用 operator 凑巧匹配、或破坏 shape_policy（多 flatten 一层却计数凑巧相等）的结果——由 AST_check 的 output-space 守卫（禁用算子 + shape）拦截；
- 仅 AST_check：结构守卫通过但语义错（窗口边界、排序键、未正面处理异构变体）——由 rich witness 上的 NormExec ≡_rec 拦截（L2 判别力）。

合取保证「合法地做（output 守卫）」与「做对了（witness 判别）」互为防线。**注**：cfs 不再承担「锁定 native idiom 结构」的重活（那是旧富指纹的来源，会误杀等价 idiom）；其结构判别力已最小化并下放给 witness（见上「cfs 最小化原则」）。

<a id="01-3-2"></a>
### §01-3-2 三层构造期保证 (L1–L3)

TEND 正确性是三层堆叠，而非单层 gold-class 通过测试：

**L1 · 执行层语义锚**

> 对任意 q_p ∈ gold-class(r)：NormExec(q_p, D) ≡_rec NormExec(q_g, D)。

同类必同果；是 EX 的执行侧定义本身。

**L2 · witness 判别力**

> 若存在 plausible wrong 解 q_w ∉ gold-class(r) 但 NormExec(q_w, D) ≡_rec NormExec(q_g, D)，record 在构造期驳回。

plausible wrong 由 [04 §4](./04_agent_framework.md#04-4) 的 MUT mutations 库生成；witness 须足够 rich 使近似错解必然失败。每条 record 5–8 条 mutation，全部须 EX fail。**RAR 下 witness 是结构正确性的唯一判别器**：cfs 已最小化、不再 police native idiom（§01-3-1），故「未正面处理异构变体 / 窗口边界错 / 漏分支」全靠 rich witness 令其 `≡_rec` 必败——witness 判别力不足即 record 构造期驳回。

**L3 · NLQ 一致性**

> canonical NLQ 须在 RTV 往返下 `NormExec ≡_rec gold`（结果级，强制；非 cfs 指纹闭包）；colloquial 走软检查。canonical NLQ 在独立 LLM 歧义攻击下收敛到唯一查询意图（结果级）；colloquial 变体不得引入歧义意图。

L3 由 RTV 闭包（canonical 强制 / colloquial 软）与 NNC 独立 LLM 歧义攻击承担（[04 §4](./04_agent_framework.md#04-4)）。

三层共同构成 gold-class 作为真值的合法性凭证：缺 L1 则类内自相矛盾；缺 L2 则 solver 可近似混过；缺 L3 则同一 NLQ 对应多个不等价解。

<a id="01-4"></a>
## §01-4 归一化契约 Norm

Norm 是从原生 Exec 结果到 $\mathcal{R}^\ast$ 的确定性投射。四层契约完整定义 Norm；全部下游归一化按此执行。

<a id="01-4-1"></a>
### §01-4-1 标量层

| 原生类型 | 规范化 |
|----------|--------|
| Int32 / Int64 / Long / Decimal128 / Double | 统一数值；纯整数保留 int 外观，否则 float；≥12 位有效数字 |
| ObjectId | 24 位 hex 小写 |
| UUID | 8-4-4-4-12 hex 小写 |
| Date / Timestamp | UTC ISO-8601 毫秒精度 |
| Binary | base64 |
| Regex | 不应出现在结果中 |
| String / Bool / null | 原样；null 与缺失严格区分 (§01-4-3) |

浮点 ≡_rec 使用双容差：|a−b| ≤ max(10⁻⁹, 10⁻⁹·max(|a|,|b|))。

<a id="01-4-2"></a>
### §01-4-2 复合层

- dict：移除键顺序；键为 case-sensitive Unicode，严格相等。
- list：默认保留元素顺序。若 gold 含 $sort 或 top-N 语义算子 ($limit 后置、$rank / $denseRank / $firstN / $lastN 等)，顺序有语义；否则 ≡_rec 在 list 层兜底使用 §01-5-3 规范化全序后 element-wise 比较。
- 嵌套结构递归应用；{} ≠ null，[] ≠ null，[] ≠ [null]。

<a id="01-4-3"></a>
### §01-4-3 null vs missing

{ field: null } 与 {} (字段缺失) 在 MongoDB 语义下不等价：$type:"null"、$exists、$ifNull 行为均不同。Norm 规定：显式 null 保留键+null；缺失键不引入、不补 null；≡_rec 在 dict 层判定二者不等。

<a id="01-4-4"></a>
### §01-4-4 _id 与 shape-preserving

- **_id**：默认剥除顶层副作用 _id（gold 未在 $project 列出 _id:1 且未在 $group 将 _id 作为语义键时）；显式保留时参与比较。
- **shape-preserving**：gold 未 flatten 的嵌套数组，Norm 严格保留形状；shape_policy ∈ {preserve, reshape, reduce} 见 [02 §2](./02_dataset_design.md#02-2)。preserve 下 flatten 多余一层判 ≢_rec，即使计数凑巧相等。

<a id="01-5"></a>
## §01-5 ≡_rec 递归相等

≡_rec 是 $\mathcal{R}^\ast$ 上的递归等价，分标量 / 字典 / 列表 / 顶层四层。

<a id="01-5-1"></a>
### §01-5-1 标量层

类型 tag 相同；float/int 用 §01-4-1 双容差；string/date/objectid/uuid/base64 精确相等；bool 精确；null ≡ null，null ≢ 任何其他类型。

<a id="01-5-2"></a>
### §01-5-2 字典层

keys(D_a) = keys(D_b)（严格集合相等）；对每个 k，D_a[k] ≡_rec D_b[k]。键顺序不参与。

<a id="01-5-3"></a>
### §01-5-3 列表层

|L_a| = |L_b|。顺序敏感模式（gold 含显式排序）：逐索引 ≡_rec。顺序无关模式：先按规范化全序排序再比较。全序：type-tag 序 null < bool < int < float < string < list < dict；同 tag 内按类型规则（数值序、Unicode 字典序、递归 lexicographic 等）。

<a id="01-5-4"></a>
### §01-5-4 顶层

aggregate 返回值恒为 list-of-dict；顶层 ≡_rec 按列表规则。[] ≠ [{}]；⊥ 与任何非 ⊥ 不等。

<a id="01-6"></a>
## §01-6 Instance 根原则 P1–P4

<a id="01-6-1"></a>
### §01-6-1 四项公理

每个合法 record 须同时满足 P1–P4，否则构造期驳回。

**P1 · Execution Well-formedness & Reference Agreement**

> NormExec(q_g, D) ≠ ⊥ 且 NormExec(q_g, D) ≡_rec R(D)。

gold representative 在 witness 上须解析、执行、归一化成功，**且其结果须与该 record 的独立参照实现 R 在 D 上一致**。R 是 archetype 目录为每个问题原型提供的 naive、可审计参照（独立 Python，跨执行范式），是 RAR 对 gold 正确性的**独立 oracle**——取代纯逆向「gold 即答案」的自证，抓得到 gold 的系统性 bug。R 与双路合成（mql_primary ≡ mql_alt）三角定位 gold（[04 §04-3](./04_agent_framework.md#04-3)）。允许 [] 仅当 NLQ 显式问「不存在」。

**P2 · Semantic Uniqueness**

> canonical NLQ 意图唯一；RTV 往返下 canonical 须 `NormExec ≡_rec gold`（结果级）；colloquial 不引入第二意图。

P2 由 QPS 枚举的 `intent` + RTV 结果级闭包（canonical 强制 / colloquial 软）+ NNC 独立 LLM 歧义攻击共同担保。

**P3 · Discriminativeness**

> mutations 库中 plausible wrong 解 q_w 须满足 NormExec(q_w, D) ≢_rec NormExec(q_g, D)；NNC graduated dual-bridge gate 对非 feasible 类 record 须拒绝 SQL/Template 桥捷径（RAR 纯结果判据：两桥均不得 NormExec ≡_rec gold，见 [04 §04-5-2](./04_agent_framework.md#04-5-2)）。

**P4 · World Non-triviality**

> NormExec(q_g, D) 非平凡：非空 list（除非 NLQ 问不存在）；group-by 组数满足 NLQ 隐含下界；window/rank 列值域 ≥2；$ifNull 字段须同时覆盖 null 与非 null 样本。

<a id="01-6-2"></a>
### §01-6-2 Agent 框架与 P1–P4 的对应

构造期验证由 Agent 流水线直接承担：

| 原则 | Agent 承担方 | 机制概要 |
|------|-------------|---------|
| **P1** | MS + PV | gold ≡_rec 参照 R ∧ 双路收敛 ∧ NormExec 非 ⊥ |
| **P2** | QPS + RTV + NNC | RTV 结果级闭包（canonical 强制 / colloquial 软）；独立 LLM 歧义攻击 |
| **P3** | MUT + PV + NNC | mutation 库全 fail；graduated dual-bridge gate（纯结果） |
| **P4** | RA + DM | witness 非平凡审计；必要时 targeted augment |

L1–L3 与 P 的耦合关系保持不变：L1 ↔ P1 类内；L2 ↔ P3∧P4 类外区分；L3 ↔ P2 源头收敛。评测期 solver 违反 P_ro/P_det/P_mxe 或 AST_check 时 EX 直接 fail。

<a id="01-7"></a>
## §01-7 Canonical 锚 (pending DAR Phase A): financial/1001

本示例为全文共享的 canonical 锚。下游文档 (02–06) 引用须字节级一致。record 取自 BIRD 真实库 `financial`(已在 test-only 集),异构信号实测;布局/MQL/`world_signature` 待 DAR Phase A 构造 + 执行验证。

> **⚠ PENDING DAR Phase A**：下列 record 取自 BIRD `financial`,异构信号(稀疏 `loan` 682/4500、多态 `trans`)实测;account 反范式化布局、gold MQL 与 `world_signature`(确定性占位)尚未经 DAR Phase A 在真实 MongoDB 上构造 + 执行验证。

> **RAR 注**：financial/1001 是异构驱动记录的范例——难度源自稀疏 `loan` 的 present/missing 变体（DAR 机制②），canonical NLQ「无 loan 则记 0」正是该 query-bearing 异构逼出的真实业务意图（archetype = 稀疏字段的存在性条件投影，见 [04 §04-2-4](./04_agent_framework.md#04-2-4)）。其 `canonical_form_set` 已按 [§01-3-1](#01-3-1) 收敛为 RAR thin contract：`must_contain` 仅保留不可避免的 `$lookup`，`must_not_contain` 恒含 6 件禁用 operator，`must_contain_at_root` 可空，`preserve` shape 以根禁 `$unwind`/`$group` 守住 output space；`$addFields`/`$cond`/`$type` 等可替换 idiom 不进入 cfs，由 witness + `NormExec ≡_rec` 判别。

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

**解读要点**：

- difficulty L4：$lookup 关联子管道（多态 trans 贷记求和）+ 稀疏 loan 的 $cond 变体调和，dual-bridge defeat 须拒绝纯 SQL 翻译捷径（[04 §3](./04_agent_framework.md#04-3)）。
- canonical_form_set：RAR thin guard；`must_contain` 仅 `$lookup`，`must_not_contain` 恒含 6 件禁用 operator，`must_contain_at_root` 可空，`preserve` shape 以根禁 `$unwind`/`$group` 守住文档数与嵌套结构。
- shape_policy preserve：只 $addFields 就地新增 loan_to_credit_ratio，禁 $unwind+$group 重建（§06-5）。
- mutations_ref 中典型错解（用 INNER-JOIN 式 drop 丢无 loan 账户、漏 $cond/$type、多态 trans 未按 type 过滤、credit_sum 为 0 未兜底）须在 witness 上 NormExec ≢_rec gold（P3）。

<a id="01-8"></a>
## §01-8 符号表

| 符号 | 含义 |
|------|------|
| NLQ, S, db_id | 输入三元组 |
| q^MQL, q_p, q_g | 预测 / gold representative MQL |
| D(db_id), world_signature | witness 快照及其 canonical 哈希 |
| Parse, Exec, Norm, NormExec | 算子链 |
| AST_check, canonical_form_set, gold-class | 结构侧 gold-as-class |
| ≡_rec | 执行侧递归相等 |
| P_ro, P_det, P_mxe | 输出空间三性质 |
| P1–P4 | instance 根原则 |
| L1–L3 | 构造期三层保证 |
| EX | 唯一语义锚指标 |

---

## Part II

> 实现附录。下列伪代码供评测 harness 与单元测试直接对照 Part I 公理；非 normative  prose 的补充说明。

<a id="01-ii-1"></a>
### §01-II-1 AST_check

# uses: json, re, typing
```

OPERATOR_TOKEN_RE = re.compile(r'\$(\w+)')

def collect_operators(node, *, at_root=False):
    """DFS BSON/JSON AST; yield (operator, at_root_flag)."""
    ops = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k.startswith("$"):
                ops.append((k, at_root))
            ops.extend(collect_operators(v, at_root=False))
    elif isinstance(node, list):
        if at_root:
            for stage in node:
                if isinstance(stage, dict):
                    for sk in stage:
                        if sk.startswith("$"):
                            ops.append((sk, True))
                ops.extend(collect_operators(stage, at_root=False))
        else:
            for item in node:
                ops.extend(collect_operators(item, at_root=False))
    return ops

def AST_check(q_mql: str, canonical_form_set: dict) -> bool:
    ast = Parse(q_mql)                     # ⊥ → fail
    if ast is BOT:
        return False
    if disabled_operator_scanner(q_mql):   # §01-II-5
        return False
    pipeline = extract_pipeline_stages(ast)
    all_ops = {op for op, _ in collect_operators(ast, at_root=False)}
    root_ops = {op for op, is_root in collect_operators(pipeline, at_root=True) if is_root}
    cfs = canonical_form_set
    if not set(cfs["must_contain"]).issubset(all_ops):
        return False
    if not set(cfs["must_not_contain"]).isdisjoint(all_ops):
        return False
    if not set(cfs["must_contain_at_root"]).issubset(root_ops):
        return False
    if not set(cfs["must_not_contain_at_root"]).isdisjoint(root_ops):
        return False
    return True
```

<a id="01-ii-2"></a>
### §01-II-2 NormExec

# uses: typing, bson
```

BOT = object()
BOT_EXEC = object()

def NormExec(q_mql: str, snapshot: dict) -> object:
    ast = Parse(q_mql)
    if ast is BOT:
        return BOT
    raw = Exec(ast, snapshot)
    if raw is BOT_EXEC:
        return BOT
    return Norm(raw, gold_mql=q_mql)       # shape_policy from record metadata
```

<a id="01-ii-3"></a>
### §01-II-3 Norm (四层)

# uses: decimal, datetime, base64, math
```

def Norm_scalar(v):
    # Layer 1: scalar
    if isinstance(v, (int, float, Decimal128)):
        return normalize_numeric(v)        # §01-4-1
    if isinstance(v, ObjectId):
        return v.hex().lower()
    if isinstance(v, UUID):
        return str(v).lower()
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat(timespec="milliseconds") + "Z"
    if isinstance(v, Binary):
        return base64.b64encode(v).decode("ascii")
    if isinstance(v, Regex):
        raise ValueError("Regex in result")
    return v                               # str, bool, None unchanged

def Norm_composite(v, *, order_sensitive: bool):
    # Layer 2: composite
    if isinstance(v, dict):
        return {k: Norm(val, order_sensitive=order_sensitive) for k, val in sorted(v.items())}
    if isinstance(v, list):
        elems = [Norm(x, order_sensitive=order_sensitive) for x in v]
        if not order_sensitive:
            elems = sorted(elems, key=canonical_sort_key)   # §01-5-3
        return elems
    return Norm_scalar(v)

def Norm_null_missing(v):
    # Layer 3: null vs missing — enforced at dict assembly (no phantom keys)
    return v

def Norm_id_shape(v, *, gold_mql: str, shape_policy: str):
    # Layer 4: _id strip + shape preserve
    if isinstance(v, list):
        return [Norm_id_shape(doc, gold_mql=gold_mql, shape_policy=shape_policy) for doc in v]
    if isinstance(v, dict):
        out = dict(v)
        if should_strip_id(gold_mql) and "_id" in out:
            del out["_id"]
        return out
    return v

def Norm(raw, *, gold_mql: str, shape_policy: str = "reshape") -> object:
    order_sensitive = pipeline_has_order_semantics(gold_mql)
    v = Norm_composite(raw, order_sensitive=order_sensitive)
    v = Norm_id_shape(v, gold_mql=gold_mql, shape_policy=shape_policy)
    return v
```

<a id="01-ii-4"></a>
### §01-II-4 canonical_text

# uses: json, hashlib
```

def canonical_text(obj) -> str:
    """Deterministic serialization for world_signature and ≡_rec sort keys."""
    def _encode(x):
        if x is None:
            return {"__tag__": "null"}
        if isinstance(x, bool):
            return {"__tag__": "bool", "v": x}
        if isinstance(x, int):
            return {"__tag__": "int", "v": x}
        if isinstance(x, float):
            return {"__tag__": "float", "v": normalize_numeric(x)}
        if isinstance(x, str):
            return {"__tag__": "str", "v": x}
        if isinstance(x, list):
            return {"__tag__": "list", "v": [_encode(i) for i in x]}
        if isinstance(x, dict):
            return {"__tag__": "dict", "v": {k: _encode(x[k]) for k in sorted(x)}}
        raise TypeError(type(x))
    return json.dumps(_encode(obj), separators=(",", ":"), ensure_ascii=False)

def world_signature(snapshot: dict) -> str:
    digest = hashlib.sha256(canonical_text(snapshot).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
```

<a id="01-ii-5"></a>
### §01-II-5 disabled_operator_scanner

# uses: re
```

DISABLED_OPERATORS = {"$sample", "$rand", "$out", "$merge", "$function"}
DISABLED_SYSTEM_VARS = {"$$NOW"}

def disabled_operator_scanner(q_mql: str) -> bool:
    """Return True if any forbidden token present (scan → fail AST_check)."""
    ast = Parse(q_mql)
    if ast is BOT:
        return True
    for op, _ in collect_operators(ast, at_root=False):
        if op in DISABLED_OPERATORS:
            return True
    text = q_mql
    for var in DISABLED_SYSTEM_VARS:
        if var in text:
            return True
    return False
```

<a id="01-ii-6"></a>
### §01-II-6 ≡_rec (实现)

# uses: math
```

def equiv_rec(a, b, *, order_sensitive: bool) -> bool:
    if a is BOT or b is BOT:
        return False
    ta, tb = type_tag(a), type_tag(b)
    if ta != tb:
        return False
    if ta in ("int", "float"):
        return numeric_equiv(a, b)         # §01-4-1 tolerance
    if ta in ("str", "bool", "null"):
        return a == b
    if ta == "dict":
        if set(a.keys()) != set(b.keys()):
            return False
        return all(equiv_rec(a[k], b[k], order_sensitive=order_sensitive) for k in a)
    if ta == "list":
        if len(a) != len(b):
            return False
        la, lb = (a, b) if order_sensitive else (sorted(a, key=canonical_sort_key), sorted(b, key=canonical_sort_key))
        return all(equiv_rec(la[i], lb[i], order_sensitive=True) for i in range(len(la)))
    return False

def EX_verdict(q_p: str, record: dict, snapshot: dict) -> bool:
    if not AST_check(q_p, record["canonical_form_set"]):
        return False
    order_sensitive = pipeline_has_order_semantics(record["MQL"])
    rp = NormExec(q_p, snapshot)
    rg = NormExec(record["MQL"], snapshot)
    return equiv_rec(rp, rg, order_sensitive=order_sensitive)
```

<a id="01-ii-7"></a>
### §01-II-7 单元测试伪代码

# uses: pytest, fixtures.financial
```

def test_AST_check_financial_1001_pass():
    rec = load_fixture("financial/1001.json")
    assert AST_check(rec["MQL"], rec["canonical_form_set"]) is True

def test_AST_check_missing_addfields_fail():
    rec = load_fixture("financial/1001.json")
    q_bad = strip_root_stage(rec["MQL"], "$addFields")
    assert AST_check(q_bad, rec["canonical_form_set"]) is False

def test_disabled_operator_sample_fail():
    q = 'db.c.aggregate([{"$sample": {"size": 1}}])'
    assert disabled_operator_scanner(q) is True

def test_disabled_operator_now_fail():
    q = 'db.c.aggregate([{"$match": {"t": "$$NOW"}}])'
    assert disabled_operator_scanner(q) is True

def test_NormExec_gold_non_bot(financial_snapshot):
    rec = load_fixture("financial/1001.json")
    assert NormExec(rec["MQL"], financial_snapshot) is not BOT

def test_equiv_rec_null_vs_missing():
    assert equiv_rec({"a": None}, {}, order_sensitive=False) is False

def test_equiv_rec_float_tolerance():
    assert equiv_rec(1.0, 1.0 + 1e-12, order_sensitive=False) is True

def test_world_signature_stable(financial_snapshot):
    rec = load_fixture("financial/1001.json")
    assert world_signature(financial_snapshot) == rec["world_signature"]

def test_EX_verdict_gold_member(financial_snapshot):
    rec = load_fixture("financial/1001.json")
    assert EX_verdict(rec["MQL"], rec, financial_snapshot) is True

def test_EX_verdict_mutation_fail(financial_snapshot):
    rec = load_fixture("financial/1001.json")
    for mut in load_mutations(rec["mutations_ref"]):
        assert EX_verdict(mut["MQL"], rec, financial_snapshot) is False
```

---

> **本文定义 TEND 的全部公理。下游文档对本文符号、性质、锚与原则的任何引用须完全对齐，不得重新定义或暗中弱化。**
