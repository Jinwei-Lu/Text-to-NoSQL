# 评测与报告契约

> 文档定位: MonGen Text-to-NoSQL 的唯一评测与报告契约。  
> 依赖边界: [01 任务定义](./01_task_definition.md) 负责任务语义、符号与比较关系; [02 数据集设计](./02_dataset_design.md) 负责记录字段、split 与 `is_horizon` 元数据; [03 数据集构造方法](./03_dataset_construction.md) 负责 gold 构造、分桶来源与归一化 provenance; [05 解决方案设计](./05_solution_design.md) 只能消费本文定义, 不得重定义口径。

## 0. 摘要

本文只定义三件事: **评什么、在哪些样本上评、怎样报告**。

- headline benchmark 只作用于 `pass` 主集与 `pass` Horizon 持出池。
- Ambiguous、Long-Tail、分歧桶都属于 sidecar, 只能单独报告, 不得污染 headline。
- `Real` 主集天然排除 Long-Tail 与未解消样本; 这不是附加脚注, 而是主契约本身。
- Family 级指标与 NLQ-Robustness 只定义在显式带多 NLQ 变体的 family 子集上; 当前 primary contract 只要求 Synth-style family。
- `L1-L5` 由归一化 `SD ∈ [0,1]` 的固定区间给出, 不是分位数。
- `null` 与 `missing` 严格区分; 缺失字段必须保持缺席, 不得转写为 `null`。

贯穿示例统一使用 `ecommerce_017`: 入口集合 `orders`, **无 join**, 激活特性 `{F10,F15,F17}`, canonical pipeline 为 `[$match,$unwind,$group,$project,$sort,$limit]`, 输出键为 `user_id` 与 `total_spent`, 查询返回前 3 名，对应 family 含 5 条 NLQ。

<a id="04-1"></a>
## 1. 指标与分母契约

记 `S ∈ {Synth, Real, Hybrid}` 为子集, `R_S` 为 [02](./02_dataset_design.md) 给出的原始记录集合。本文**不重新判桶**; Ambiguous、Long-Tail 与各分歧桶成员资格都直接读取 [03](./03_dataset_construction.md) 的 provenance。

统一定义五个池:

- `P_S = {x ∈ R_S | status(x)=pass 且 Ambiguous(x)=false}`
- `M_S = {x ∈ P_S | is_horizon(x)=false}`: 子集 `S` 的主集
- `H_S = {x ∈ P_S | is_horizon(x)=true}`: 子集 `S` 的 Horizon 持出池
- `A_S = {x ∈ R_S | Ambiguous(x)=true}`: Ambiguous 侧桶
- `L_S = {x ∈ R_S | LongTail(x)=true}`: Long-Tail 侧车桶
- `D_{S,b} = {x ∈ R_S | DivergenceBucket(x)=b}`: 分歧侧车桶, 其中 `b ∈ {engine_quirk, A_bug, B_bug, C_bug, spec_ambiguity}`

headline 只允许使用 `M_S` 与 `H_S`。`A_S`、`L_S`、`D_{S,b}` 只能进入辅助表。任何元数据冲突样本都必须退出 headline, 单独披露计数。

聚合记号 `Avg_m(G)` 表示指标 `m` 在池 `G` 上的算术平均; 若 `|G|=0`, 则该单元报告为 `NA`。`NA` 必须原样报告, 不得回填。

<a id="04-1-1"></a>
### 1.1 样本级指标

记预测查询为 `q_p(x)`, gold 查询为 `q_g(x)`。`fast_canonical(x)`、物理 gold 结果 `r_g^A(x)`、符号 gold 结果 `r_g^C(x)` 均来自 [02](./02_dataset_design.md) 与 [03](./03_dataset_construction.md)。

基础算子:

- `Parse(q)`: 从 MQL 到 fAST; 失败返回 `⊥`
- `Lift(f)`: 从 fAST 到 Core/Extension cMRL 的部分函数; 失败返回 `⊥`
- `ExecA(q,D)`: 在评测环境的 MongoDB 物理执行后, 依 [03](./03_dataset_construction.md) 同源归一化得到的结果

比较关系 `≡cmp` 由 gold provenance 决定:

- gold 标明结果有序时, 按顺位递归比较;
- gold 明确标明结果无序时, 先做确定性 canonical sort, 再递归比较。

指标定义:

$$
\operatorname{EM}(x)=\mathbb{1}[\operatorname{norm\_str}(q_p(x))=\operatorname{norm\_str}(q_g(x))]
$$

$$
\operatorname{QSM}(x)=\mathbb{1}[\operatorname{Parse}(q_p(x))\neq\bot \land \operatorname{stages}(\operatorname{Parse}(q_p(x)))=\operatorname{stages}(fast\_canonical(x))]
$$

$$
\operatorname{QFC}(x)=\mathbb{1}[\operatorname{Parse}(q_p(x))\neq\bot \land \operatorname{fields}(\operatorname{Parse}(q_p(x)))=\operatorname{fields}(fast\_canonical(x))]
$$

$$
\operatorname{EX}(x)=\mathbb{1}[\operatorname{ExecA}(q_p(x),D(x)) \equiv_{cmp} r_g^A(x)]
$$

$$
\operatorname{EX\text{-}Sym}(x)=\mathbb{1}\Big[
\operatorname{Parse}(q_p(x))\neq\bot \land
\operatorname{Lift}(\operatorname{Parse}(q_p(x)))\neq\bot \land
\llbracket \operatorname{Lift}(\operatorname{Parse}(q_p(x))) \rrbracket_C(D(x)) \equiv_{cmp} r_g^C(x)
\Big]
$$

$$
\operatorname{EFM}(x)=\mathbb{1}[\operatorname{keys}(\operatorname{ExecA}(q_p(x),D(x)))=\operatorname{keys}(r_g^A(x))]
$$

$$
\operatorname{EVM}(x)=\mathbb{1}[\operatorname{drop\_keys}(\operatorname{ExecA}(q_p(x),D(x))) \equiv_{cmp} \operatorname{drop\_keys}(r_g^A(x))]
$$

执行失败、解析失败、Lift 失败、运行期错误或执行上界触发时, 样本**保留在原分母中**, 相应指标记 `0`。当 `Parse` 或 `Lift` 失败时, 还必须记录 `pred_unliftable=1`。

报告角色固定如下:

- headline 指标: `EX`, `EX-Sym`
- 必报派生量: `Dual-Pass = EX ∧ EX-Sym`, `Anchor-Split = EX \oplus EX-Sym`
- 诊断指标: `EM`, `QSM`, `QFC`, `EFM`, `EVM`

<a id="04-1-2"></a>
### 1.2 Family 级指标与 NLQ-Robustness

Family 级指标只适用于 [02](./02_dataset_design.md) 中**明确带多 NLQ 变体**的 family 子集。当前 primary contract 只要求 Synth-style family; Real 与 Hybrid 主表默认填 `NA`, 除非 [02](./02_dataset_design.md) 明确提供可比 family 结构。

记 `V` 为所有带 family 变体的样本族集合, `F.members` 为 family `F` 的全部成员。当前主契约只定义两个 family headline 池:

- `FM`: `subset(F)=Synth` 且 `F.members ⊆ M_Synth` 的 family 集合
- `FH`: `subset(F)=Synth` 且 `F.members ⊆ H_Synth` 的 family 集合

只要某个 family 含 Ambiguous、Long-Tail、分歧成员或跨池成员, 该 family 就不能进入 family headline 分母。

$$
\operatorname{EX\text{-}Family}(F)=\mathbb{1}[\forall x \in F.members,\ \operatorname{EX}(x)=1]
$$

$$
\operatorname{EX\text{-}Sym\text{-}Family}(F)=\mathbb{1}[\forall x \in F.members,\ \operatorname{EX\text{-}Sym}(x)=1]
$$

$$
\operatorname{NLQ\text{-}Robustness}(F)=\frac{1}{|F.members|}\sum_{x \in F.members}\operatorname{EX}(x)
$$

style 切片定义为:

$$
\operatorname{NLQ\text{-}Robustness}_{style=s}
=
\operatorname{Avg}_{EX}\big(\{x \mid x \in F.members,\ style(x)=s,\ F \in FM \text{ 或 } F \in FH\}\big)
$$

Ambiguous 变体不进入 `NLQ-Robustness` 主分母; 如需分析 Ambiguous 语言现象, 只能进入侧桶辅助表。

<a id="04-1-3"></a>
### 1.3 主报告、L1-L5 与 Horizon

主报告至少包含一张样本级主表和一张 family 级分表。

**样本级主表的列与行是固定模板**

列顺序固定为:

1. `主集 Synth`
2. `主集 Real`
3. `主集 Hybrid`
4. `Horizon Synth`
5. `Horizon Real`
6. `Horizon Hybrid`

行顺序固定为:

1. 分母 `N`
2. `EX`
3. `EX-Sym`
4. `Dual-Pass`
5. `Anchor-Split`
6. `EM`
7. `QSM`
8. `QFC`
9. `EFM`
10. `EVM`

`Horizon` 列中的 `EX` 就是该子集的 `Horizon-EX`。主集列与 Horizon 列必须分别计算, 不得先合并再切片。

**family 级分表的列与行也是固定模板**

列顺序固定为:

1. `主集 Synth`
2. `Horizon Synth`

行顺序固定为:

1. family 分母 `N_family`
2. `EX-Family`
3. `EX-Sym-Family`
4. `NLQ-Robustness`

**固定难度分档**

- `L1`: `SD ∈ [0.0, 0.2)`
- `L2`: `SD ∈ [0.2, 0.4)`
- `L3`: `SD ∈ [0.4, 0.6)`
- `L4`: `SD ∈ [0.6, 0.8)`
- `L5`: `SD ∈ [0.8, 1.0]`

若上游记录自带的 `sdt_level` 与上述区间不一致, 公开报告以**本区间映射**为准。`Horizon` 仍以 [02](./02_dataset_design.md) 的 `is_horizon` 元数据为准, 但不并入主集均值。

如需给出按难度切片或 `SCI × SD` 热图, 单元格分母必须继承所属 headline 池。Real 的热图可以给出, 但只能标为辅助图。

<a id="04-2"></a>
## 2. EX / EX-Sym 双锚协议

双锚协议只在 `pass` 主集与 `pass` Horizon 池中作为 headline 生效。此时 gold 侧同时存在物理锚 `r_g^A` 与数学锚 `r_g^C`。

| `EX` | `EX-Sym` | 含义 | headline 处理 |
| --- | --- | --- | --- |
| 1 | 1 | 双锚同时通过 | 计入 `EX`、`EX-Sym` 与 `Dual-Pass` |
| 1 | 0 | 物理锚通过, 数学锚未通过 | 计入 `EX`; 计入 `Anchor-Split`; 不得把 `EX` 回填进 `EX-Sym` |
| 0 | 1 | 数学锚通过, 物理锚未通过 | 计入 `EX-Sym`; 计入 `Anchor-Split` |
| 0 | 0 | 双锚同时失败 | 两者都记失败 |

fallback 规则只有三条:

1. headline 池中**禁止 gold 侧 fallback**。若某条 headline 样本缺 `r_g^C`, 说明上游分桶错误; 该样本必须退出 headline 并单独披露。
2. headline 池中**禁止用 `EX` 回填 `EX-Sym`**。预测不可解析、不可 Lift 或被 Compiler C 判为 undefined 时, 样本必须留在原分母中, 并记 `EX-Sym = 0`。
3. sidecar 可以用 bucket-local 协议, 但绝不能回流到 headline。

围绕 `ecommerce_017` 的三个最小示例:

- 预测与 gold 一致, pipeline 为 `[$match,$unwind,$group,$project,$sort,$limit]`, 返回前 3 名 `user_id,total_spent`: `EX=1`, `EX-Sym=1`。
- 预测把 `$sum(items.price)` 误写为 `$avg(items.price)`: `EX=0`, `EX-Sym=0`。
- 预测在等价位置引入不可 Lift 的 Long-Tail `$function`, 物理结果仍刚好与 gold 相同: `EX=1`, `EX-Sym=0`, 该样本仍计入 `Anchor-Split`, 不允许 fallback。

`Anchor-Split` 与 `pred_unliftable` 是必披露量, 不是脚注。

<a id="04-3"></a>
## 3. 执行环境与可复现契约

任一摘要不一致时, 评测应中止而不是继续生成不可比数字。

**输入锁定**

- 预测文件摘要
- 数据集 manifest 摘要
- split manifest 摘要
- 每个 `db_id` 的 schema 摘要
- 每个 `db_id` 的只读快照摘要
- [03](./03_dataset_construction.md) 归一化实现摘要与 gold 结果摘要

**运行时锁定**

- MongoDB 执行环境镜像摘要
- 评测器代码摘要
- Parser 摘要
- Lifter 摘要
- Compiler C 二进制摘要
- 排序、collation、locale、timezone、数值格式化策略
- 固定的执行上界策略与失败重试策略

**执行规则**

1. 评测只能在只读快照上运行, 不允许写回数据库。
2. `EX` 与 `EX-Sym` 的比较必须复用 [03](./03_dataset_construction.md) 的归一化 provenance; 摘要不一致时直接中止。
3. `null` 与 `missing` 必须严格区分: `null` 保留为显式空值, `missing` 保留为字段缺席。
4. 任何解析失败、Lift 失败、执行失败、运行期错误都必须逐样本记录; 不允许静默跳过。
5. 结果顺序是否有序只能由 gold provenance 决定; 预测侧不得通过自报“无序”改变比较模式。

**输出工件**

- 逐样本结果文件: 含样本标识、所属池、全部样本级指标、`pred_unliftable` 标记、`anchor_state`
- 逐池汇总表: 主表、family 分表、侧桶辅助表
- 排除台账: 记录每个 headline 单元的原始计数、排除计数与剩余分母
- 环境 manifest: 记录全部输入锁定与运行时锁定摘要

默认建议直接复用 [03](./03_dataset_construction.md) 已存的 gold 结果。若确需重算, 也必须与 gold 摘要逐条对齐并披露。

<a id="04-4"></a>
## 4. 侧车桶辅助报告

侧车桶报告是可选的, 但一旦给出就必须使用 bucket-local 分母, 并显式标注“非 headline”。

| 侧桶 | 成员来源 | 推荐指标 | 分母 | headline 可否使用 |
| --- | --- | --- | --- | --- |
| Ambiguous | 由 [03](./03_dataset_construction.md) 的意图歧义裁决给出 | `Coverage_any`, `Abstain`, bucket-local `EX` | `|A_S|` | 否 |
| Long-Tail | 由 [03](./03_dataset_construction.md) 的 Long-Tail / degraded 标记给出 | `LT-EX`, 解析成功率, 操作符分布 | `|L_S|` | 否 |
| Divergence | `engine_quirk`, `A_bug`, `B_bug`, `C_bug`, `spec_ambiguity` | 桶内计数、占比、bucket-local `EX`/`EX-Sym` | `|D_{S,b}|` | 否 |

最低约束:

- Ambiguous 侧桶若提供成功率, 必须使用 [03](./03_dataset_construction.md) 保留的 admissible intent 集合作为判定域;
- Long-Tail 侧桶若没有符号 gold, 则 `EX-Sym` 必须填 `NA`, 不得改写成 `EX`;
- 分歧侧桶可以给 bucket-local `EX` 或 `EX-Sym`, 但必须按桶分别报告, 不能先合并成单个 side score。

<a id="04-5"></a>
## 5. 强制披露清单

每次公开报告至少要披露:

- 每个子集的原始样本数、主集分母、Horizon 分母、Ambiguous 计数、Long-Tail 计数、各分歧桶计数。
- 每个 headline 单元的 `EX/EX-Sym` 四象限计数: `(1,1)`, `(1,0)`, `(0,1)`, `(0,0)`。
- 每个 headline 单元的 `pred_unliftable` 比例。
- 每个 headline 单元中使用“无序比较”的样本计数。
- `L1-L5` 固定区间定义与各档分母。
- family 分表的适用范围、family 分母、family 大小分布。
- `null`/`missing` 严格区分的声明。
- 预测文件摘要、数据集摘要、环境 manifest 摘要、归一化实现摘要。
- 是否重算 gold; 若重算, 需说明对齐方式与一致性检查结果。
- 所有 `NA` 单元的零分母原因。

缺失上述任一条目的结果都视为**不完整报告**。

<a id="04-7"></a>
## 7. 与方法文档的接口

[05 解决方案设计](./05_solution_design.md) 以及其他方法文档只能消费本文三类产物:

1. `§1.3` 的样本级主表;
2. `§1.2` 的 Synth-style family 分表;
3. `§5` 的强制披露台账。

方法文档可以额外给出消融、检索与优化分析、侧车桶分析、错误归因, 但不得重定义:

- 主集与 Horizon 的分母资格
- `EX` 与 `EX-Sym` 的 fallback 规则
- Family 指标的适用范围
- `L1-L5` 的区间边界
- `null` 与 `missing` 的归一化语义

换言之, **本文定义 benchmark contract, 方法文档只消费 contract**。
