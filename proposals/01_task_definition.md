# 任务定义：MonGen 基准根语义契约

> 文档定位: 本文档只定义 MonGen 的任务边界、语义锚点、样本准入原则、表示分层、难度契约与结果归一化契约。它是 `02_dataset_design.md`、`03_dataset_construction.md`、`04_evaluation_methodology.md`、`05_solution_design.md` 的上游语义基线。
> 约束原则: 本文档只陈述“是什么必须成立”，不展开数据规模、切分算法、实现伪代码、报告表格或求解方案。

## 0. 摘要

- MonGen 的主任务是**确定性、只读、面向 MongoDB 的查询生成**，输入是自然语言查询与 schema 上下文，输出是可执行的 MQL 查询程序。
- 主基准样本只来自**schema-grounded、可提升、确定性、只读**的查询，并要求 **A/B/C 三路完全共识**、状态为 `pass`，同时满足基准侧的 **Reverse Instance Verification (RIV)** 概念保证。
- `longtail_AB_only` 以及其他公开分歧或降级结果只进入**旁路桶 / 审计桶**，不进入 headline benchmark；仅具内部审核价值的状态只保留在 staging，不进入公开 benchmark 资产。
- 表示层采用 **cMRL Core / cMRL Extension / fAST Long-Tail** 三层结构。主基准只覆盖 Core 与 Extension 的确定性只读 liftable 子域；Long-Tail 只作为覆盖与审计资产存在。
- 难度由 **Structural Difficulty Tensor (SDT)** 六维向量与标量 `SD ∈ [0,1]` 定义，等级固定为 `L1-L5`。`Horizon` 是独立 held-out pool，不属于主 train/test，也不进入主 in-distribution 汇总。
- `02` 是数据字段名、状态字段名、样本资产形态与切分规则的单一来源；`03` 负责构造、校验、Triple Compiler 执行流程与 RIV 的操作化；`04` 负责指标与公开报告；`05` 负责求解系统设计。

<a id="01-1"></a>
## 1. Text-to-NoSQL 任务形式化

### 1.1 输入 / 输出

MonGen 定义的单条任务输入为三元组:

- `NLQ`: 自然语言查询。
- `S`: 与目标逻辑库绑定的 schema 上下文。
- `db_id`: 目标逻辑库标识符，用于绑定数据快照。

输出为一个 **MQL 查询程序** `q`。在主任务中，`q` 满足以下约束:

- `q` 是只读查询，不修改数据库状态；
- `q` 是确定性的，在同一快照上重复执行得到相同的归一化结果；
- `q` 可以解析为 fAST；
- `q` 处于主基准允许的 Core / Extension 语义范围内时，`q` 可以从 fAST 提升回 cMRL。

本基准不把写操作、不确定性行为、依赖外部检索服务的行为或不可提升行为纳入 headline task。具体样本字段名、记录格式与 split 规则不在本文定义，统一见 `02_dataset_design.md`。

### 1.2 函数签名

任务的外部函数签名定义为

$$
f : (\text{NLQ}, S, \text{db\_id}) \to q
$$

其中 `q` 属于 MonGen 主任务允许的确定性只读查询空间 `Q_main`。若写成分层形式，则有

$$
g : (\text{NLQ}, S) \to q^{\text{cMRL}}, \qquad
L : q^{\text{cMRL}} \to q^{\text{fAST}}, \qquad
h : q^{\text{fAST}} \to q^{\text{MQL}}
$$

这里:

- `g` 是待学习映射；
- `L` 是确定性 lowering；
- `h` 是确定性 unparser。

`L` 与 `h` 属于固定编译链路，不属于模型能力的一部分。

### 1.3 成功条件与语义正确性锚点

记模型输出为 `q_p`，gold 程序为 `q_g`，与 `db_id` 绑定的数据快照为 `D`。MonGen 在根契约层定义两类语义锚点:

1. **物理执行锚**: 预测查询与 gold 查询在同一快照上的归一化执行结果递归相等。

$$
\operatorname{NormExec}(q_p, D) \equiv_{rec} \operatorname{NormExec}(q_g, D)
$$

2. **符号语义锚**: 当 `q_p` 可从 `Parse(q_p)` 提升回主域 cMRL 时，其 denotational 结果与 gold cMRL 的 denotational 结果递归相等。

$$
\llbracket j(\operatorname{Parse}(q_p)) \rrbracket_C(D)
\equiv_{rec}
\llbracket q_g^{\text{cMRL}} \rrbracket_C(D)
$$

其中 `j` 是 lifting，`Parse` 是 MQL 到 fAST 的解析，`⟦·⟧_C` 是 Compiler C 的语义解释函数。

本文只定义这两个锚点作为语义正确性的根基；它们在公开评测中的指标命名、降级协议与聚合口径统一由 `04_evaluation_methodology.md` 定义。

### 1.4 数据快照与任务边界

`db_id` 唯一绑定一个评测数据快照 `D`。MonGen 任务始终在该静态快照上解释查询意图，不引入在线写入、副作用回放或外部状态变化。

根契约层要求:

- 查询正确性只依赖 `NLQ`、`S`、`db_id` 与由 `db_id` 指向的快照 `D`；
- 文档本体不携带“该文档服务于哪条查询”的隐藏真值标记；
- gold 正确性来自执行共识与实例级验证，而不是来自数据中的辅助标签。

`D` 的构造方式、世界物化过程与校验流程由 `03_dataset_construction.md` 负责。

### 1.5 `ecommerce_017` 规范示例

本文统一使用 `ecommerce_017` 作为 canonical running example。该示例满足以下固定约束:

- `db_id = ecommerce_017`
- canonical NLQ: `Top 3 customers by total paid item spending in 2026.`
- canonical 查询**不含 join**
- activated features: `{F10, F15, F17}`
- canonical pipeline:

```text
[$match, $unwind, $group, $project, $sort, $limit]
```

- 结果输出键: `user_id`, `total_spent`
- 所属 Synth family 的总 NLQ 数 `K = 5`，且包含 canonical NLQ

其 canonical cMRL 骨架写为:

```text
Match(status = paid ∧ paid_at exists ∧ paid_at >= 2026-01-01)
→ Unwind(items)
→ Group(by = user_id, sum(items.price) -> total_spent)
→ Project(user_id, total_spent)
→ Sort(total_spent desc)
→ Limit(3)
```

该示例在 SDT 中满足:

- `d_2 = log2(6) ≈ 2.58`，因为 6 个 stage 类型各出现 1 次；
- `d_3 = 0`，因为 canonical 查询不含 join chain。

<a id="01-2"></a>
## 2. 主基准边界与资产单元

### 2.1 主集准入原则

主基准 headline 样本满足以下共同条件:

- **schema-grounded**: gold 程序中出现的 collection、field、类型约束与输出键都由输入 schema 与编译链路解释；
- **read-only**: 不产生数据库副作用；
- **deterministic**: 在同一快照上重复执行得到同一归一化结果；
- **liftable**: gold 的 fAST 可以提升回主域 cMRL Core ∪ Extension；
- **triple-consensus**: A/B/C 三路结果完全一致；
- **status = `pass`**: 这里的 `pass` 是主基准准入语义。具体状态字段名与枚举以 `02_dataset_design.md` 为准；
- **RIV 通过**: 样本满足实例级 witness / discriminativity 保证。

不满足上述条件的样本不进入主 headline benchmark。

### 2.2 主集与旁路桶

MonGen 严格区分主集与旁路桶:

- `pass` 样本进入主集；
- `longtail_AB_only` 以及其他公开分歧或降级结果只进入旁路桶 / 审计桶；仅内部审核状态不进入公开 benchmark 资产；
- 旁路桶用于透明披露、误差分析、覆盖审计或未来扩展，不进入 headline benchmark，也不进入主 in-distribution 汇总。

### 2.3 三个子集的主资产形态

主集层面，三类资产形态固定如下:

- **Synth main set**: family-based 资产。每个 family 至少含 `K >= 3` 个总 NLQ，且包含 canonical NLQ。
- **Real main set**: primary sample-level 资产。默认 `K = 1`，不假定 family-level robustness。
- **Hybrid main set**: primary sample-level 资产。默认 `K = 1`，不假定 family-level robustness。

因此，family-level robustness 是**局部资产性质**，不是 benchmark-wide 的默认前提。

### 2.4 Long-Tail 的主集边界

MonGen 主集对子集边界作如下硬约束:

- **Synth main set 不含 Long-Tail**
- **Hybrid main set 不含 Long-Tail**
- **Real main set 只包含可提升且三路共识为 `pass` 的样本**
- **Real Long-Tail 只允许存在于旁路桶**

<a id="01-3"></a>
## 3. cMRL + fAST 三层表示与编译接口

三层表示的目的不是扩大 headline scope，而是把“主域”和“旁路覆盖域”明确切开。

<a id="01-3-1"></a>
### 3.1 cMRL Core

cMRL Core 是主基准最核心的可提升、可解释、可共识子域。它承载**高频、确定性、只读**的查询骨架，规模约为 `~30` 个原语。

Core 的语义角色是:

- 为主集提供紧凑、可组合、可提升的 canonical 表示；
- 为 Compiler A / B / C 提供共同的语义输入；
- 为主任务中的 lowering、lifting 与 symbolic evaluation 提供稳定接口。

Core 的具体原语清单由 `03_dataset_construction.md` 给出；本文只固定其边界条件，不重复枚举实现清单。

<a id="01-3-2"></a>
### 3.2 cMRL Extension

cMRL Extension 是主基准的扩展可提升子域。它承载**额外的确定性、只读、仍可由三路共识覆盖**的查询能力，规模约为 `~16` 个原语。

Extension 的语义角色是:

- 补足 Core 之外但仍属于主任务的结构能力；
- 保持可 lowering、可 lifting、可 symbolic 解释；
- 与 Core 一起构成主 benchmark 的 liftable deterministic scope。

根契约层明确规定:

- `$sample`
- `$search`
- `$out`
- `$merge`

**不属于 Core，也不属于 Extension 的主域范围**。若这些能力被记录，它们只处于 Long-Tail / side-bucket 语境，不进入 headline benchmark。

<a id="01-3-3"></a>
### 3.3 fAST Long-Tail

fAST Long-Tail 是 MongoDB AST 的开放承载层，用于承接以下情形:

- 超出 Core ∪ Extension 的节点；
- 不满足只读 / 确定性 / liftable 约束的节点；
- 无法由 Compiler C 赋予主域 denotational semantics 的节点。

Long-Tail 的角色是**覆盖与审计**，不是 headline 语义承诺。它允许基准记录现实工作负载中的额外写法，但这些样本:

- 不进入主集；
- 不进入主 in-distribution 汇总；
- 不与 `pass` 主样本混合报告。

<a id="01-3-4"></a>
### 3.4 Lowering / Lifting / Unparser 的职责

MonGen 固定三类编译职责:

- **Lowering** `L : \text{cMRL}_{\text{Core} \cup \text{Ext}} \to \text{fAST}`  
  `L` 是总函数、确定性函数、语义保持函数。

- **Lifting** `j : \text{fAST} \rightharpoonup \text{cMRL}_{\text{Core} \cup \text{Ext}}`  
  `j` 是部分函数。主集 gold 样本要求 `j` 成功；Long-Tail 允许 `j` 失败，但只进入旁路桶。

- **Unparser** `h : \text{fAST} \to \text{MQL}`  
  `h` 是确定性 surface projection。它负责 canonical MQL 输出，不负责语义改写、优化重写或样本放行。

由此，主域 gold 程序满足:

$$
q^{\text{MQL}} = h(L(q^{\text{cMRL}}))
$$

而主集 membership 的关键条件之一是:

$$
j(\operatorname{Parse}(q^{\text{MQL}})) \text{ defined}
$$

<a id="01-4"></a>
## 4. Triple Compiler Consensus 与 RIV

Triple Compiler Consensus 是 gold 正确性的主原则，RIV 是实例正确性的补充保证。两者共同决定主集是否接纳一个样本。

<a id="01-4-1"></a>
### 4.1 Triple Compiler Consensus 作为 gold 原则

MonGen 固定三条彼此独立的语义路径:

- **Compiler A**: `cMRL -> fAST -> MQL -> MongoDB execution`
- **Compiler B**: `cMRL -> relational bridge -> MQL' -> MongoDB execution`
- **Compiler C**: `cMRL -> denotational interpretation over D`

对同一 gold cMRL 与同一快照 `D`，三路产出结果分别记为 `r_A`、`r_B`、`r_C`。主域的 gold 共识条件定义为

$$
\operatorname{TCC\_pass}(x)
\iff
r_A(x) \equiv_{rec} r_B(x) \equiv_{rec} r_C(x)
$$

TCC 的意义是:

- A 保证直接 lowering 路径的工程可执行性；
- B 提供与 A 不同中间表示下的独立交叉检查；
- C 提供不依赖 MongoDB 引擎的符号语义锚。

### 4.2 RIV 与主集接纳规则

**Reverse Instance Verification (RIV)** 是 MonGen 在 TCC 之外附加的基准侧实例正确性保证。RIV 不重新定义查询语义，而是检查:

- gold 结果是否由快照中的具体实例 witness 支撑；
- 样本是否对语义邻近但实质不同的候选具有区分能力；
- 共识是否不是由“空结果巧合”“欠约束快照”或“不可判别实例配置”造成。

RIV 在概念上提供**instance discriminativity / witness-checking** 保证，具体操作流程、见证生成与检查规则统一由 `03_dataset_construction.md` 定义。

因此，主集 gold 接纳规则定义为

$$
\operatorname{GoldAccept}_{main}(x)
\iff
\operatorname{schema\_grounded}(x)
\land
\operatorname{read\_only}(x)
\land
\operatorname{deterministic}(x)
\land
\operatorname{liftable}(x)
\land
\operatorname{status}(x)=\texttt{pass}
\land
\operatorname{TCC\_pass}(x)
\land
\operatorname{RIV\_pass}(x)
$$

旁路桶与审计桶包括但不限于:

- TCC 分歧桶: `engine_quirk`, `A_bug`, `B_bug`, `C_bug`, `spec_ambiguity`
- 降级或旁路状态: `longtail_AB_only` 及其他非 `pass` 的公开 sidecar 状态；内部审核状态不进入公开枚举

这些样本可以被记录，但不属于主集。

<a id="01-5"></a>
## 5. Structural Difficulty Tensor

SDT 是 MonGen 的结构难度契约。它只刻画样本结构，不承担数据资产字段设计、构造流程或公开报告格式。

<a id="01-5-1"></a>
### 5.1 SDT 六维

SDT 由六个维度组成:

| 维度 | 记号 | 含义 |
|---|---|---|
| ast depth | `d_1` | cMRL AST 的结构深度 |
| operator entropy | `d_2` | stage 类型分布的 Shannon 熵 |
| max join chain depth | `d_3` | join 链最大深度 |
| schema linking ambiguity | `d_4` | NLQ 到 schema 的链接歧义度 |
| aggregation composition count | `d_5` | 聚合组合复杂度 |
| temporal reasoning hops | `d_6` | 时间推理步数 |

其中 `d_2` 定义为

$$
d_2(q) = -\sum_{op} p(op)\log_2 p(op)
$$

`d_1` 到 `d_6` 的可计算化规则由 `03_dataset_construction.md` 操作化；`02_dataset_design.md` 负责这些量如何附着到数据资产。

<a id="01-5-2"></a>
### 5.2 标量 `SD` 与固定等级 `L1-L5`

对任一样本 `x`，先把六维分量归一化到 `[0,1]`，得 `\tilde d_i(x) ∈ [0,1]`，再定义

$$
\operatorname{SD}(x) = \sum_{i=1}^{6} w_i \cdot \tilde d_i(x),
\qquad
w_i \ge 0,
\qquad
\sum_{i=1}^{6} w_i = 1
$$

因此 `SD(x) ∈ [0,1]`。根契约只固定 `SD ∈ [0,1]` 与等级边界，具体归一化常数与权重向量由 `03_dataset_construction.md` 负责实现并由 `02_dataset_design.md` 负责资产落盘。

等级边界固定为:

- `L1`: `[0.0, 0.2)`
- `L2`: `[0.2, 0.4)`
- `L3`: `[0.4, 0.6)`
- `L4`: `[0.6, 0.8)`
- `L5`: `[0.8, 1.0]`

### 5.3 `ecommerce_017` 的难度锚点

对 `ecommerce_017` canonical 查询:

- `d_2 = log2(6) ≈ 2.58`
- `d_3 = 0`

该示例因此是一个**无 join、六阶段均匀混合**的规范锚点，用于跨文档对齐 `d_2` 与 `d_3` 的解释。

<a id="01-5-4"></a>
### 5.4 Horizon

`Horizon` 是独立 held-out pool，不属于主 train/test，也不进入主 in-distribution aggregate。

根契约层只固定两件事:

- `Horizon` 依据 SDT 的高难度尾部定义；
- `Horizon` 与主集分开构建、分开汇总、分开报告。

`Horizon` 的具体成员规则、资产落盘方式与公开报告位置分别由 `02`、`03`、`04` 定义。

<a id="01-6"></a>
## 6. 多样性原则

MonGen 的多样性不是把所有资产混在一个汇总里，而是把不同来源、不同结构层和不同语言变体在语义上解耦后再并列组织。

### 6.1 三个正交维度

MonGen 至少区分三个相互正交的多样性维度:

- **schema diversity**: 不同逻辑库与不同 schema 结构；
- **program diversity**: 不同 cMRL / fAST 结构骨架；
- **language diversity**: 同一语义下的多种 NLQ 表达。

主集要求三者互相独立地贡献变化，而不是让任一维度通过隐藏标签泄漏到另外两维。

### 6.2 Family 与 Sample 的分工

多样性资产的单位按子集区分:

- **Synth** 以 family 为主。每个 family 至少有 `K >= 3` 个总 NLQ，canonical 包含在内。
- **Real** 与 **Hybrid** 以 sample 为主。主集默认 `K = 1`，不把 family-level robustness 作为全基准默认假设。

因此，family robustness 是 Synth 及少量扩展资产的分析维度，不是整个 benchmark 的强制结构。

### 6.3 语言变体原则

当 family 资产存在时，其 NLQ 变体允许覆盖多种表达风格，例如:

- `formal`
- `colloquial`
- `jargon`
- `noisy`
- `negated`
- `ambiguous`
- `multilingual`

这些风格是语言多样性的组织方式，不改变 gold 程序的语义身份。变体生成与回译共识的操作细节由 `03_dataset_construction.md` 定义，公开 robustness 指标由 `04_evaluation_methodology.md` 定义。

### 6.4 主集与旁路资产不混合

多样性原则不覆盖以下混合行为:

- 不把 `pass` 主样本与 Long-Tail 降级样本混合成一个主汇总；
- 不把 Synth family 资产与 Real / Hybrid 的 sample 资产当作同一假设结构；
- 不把 Horizon 与主 in-distribution 资产放进同一 headline 聚合。

<a id="01-7"></a>
## 7. 记号与结果归一化契约

### 7.1 核心记号

| 记号 | 含义 |
|---|---|
| `S` | 输入 schema 上下文 |
| `D` | `db_id` 绑定的数据快照 |
| `q_p` | 预测 MQL |
| `q_g` | gold MQL |
| `q^{cMRL}` | cMRL 程序 |
| `q^{fAST}` | fAST 程序 |
| `L` | lowering |
| `j` | lifting |
| `h` | unparser |
| `Parse` | MQL 到 fAST 的解析 |
| `r_A, r_B, r_C` | A / B / C 三路结果 |
| `\equiv_{rec}` | 结果递归相等关系 |
| `d_1,\dots,d_6` | SDT 六维分量 |
| `SD` | 归一化后的结构难度标量 |
| `Horizon` | 独立 held-out 难样本池 |

### 7.2 归一化与递归相等契约

公开结果比较之前，BSON 结果统一做规范化。根契约固定以下规则:

- `ObjectId` 规范化为 24 位 hex 字符串；
- `Date` 规范化为 ISO-8601 UTC 字符串；
- `Decimal128` 规范化为保留全精度的字符串；
- `Long` 在安全整数范围内可转为整数，否则保留字符串；
- `Binary` 规范化为 base64 字符串；
- `Regex` 规范化为 `/pattern/flags`；
- `NaN` / `Infinity` 使用显式 token；
- **`null` 与 `missing` 严格区分**: `null` 保持为 `null`，missing 字段在归一化结果字典中保持缺席，不被补成 `null`。

递归相等 `\equiv_{rec}` 的契约是:

- 标量按规范化后的值比较；
- 字典要求键集合一致，且每个键对应值递归相等；
- 列表默认按顺序逐位比较；若某任务在 `04` 中被定义为无序结果，则先进入其指定的规范排序后再比较。

### 7.3 文档职责边界

为避免定义漂移，MonGen 各提案文档的职责边界固定如下:

- `02_dataset_design.md`  
  单一来源: 数据资产字段名、状态字段名、family/sample 记录形态、切分规则、主集与旁路桶的落盘表达。

- `03_dataset_construction.md`  
  单一来源: 构造流水线、Core / Extension 具体原语、lowering / lifting / unparser 的操作化、Triple Compiler 运行流程、RIV witness-checking、旁路桶生成与审计流程。

- `04_evaluation_methodology.md`  
  单一来源: EX / EX-Sym 等指标、聚合口径、公开报告结构、主集与 Horizon 的报告边界。

- `05_solution_design.md`  
  单一来源: 求解系统、训练与推理设计、检索与优化策略。`05` 不重新定义任务边界、样本准入规则或结果归一化契约。

---

本文到此固定 MonGen 的根语义契约: 任务 I/O、主域边界、三层表示、Triple Compiler Consensus、RIV、SDT、Horizon、Family 与 Sample 的语义分工，以及结果规范化规则。后续文档若与本文冲突，以本文为准；但任何具体字段名、状态枚举、切分与操作流程，都必须回到 `02/03/04/05` 的职责文档中查证。
