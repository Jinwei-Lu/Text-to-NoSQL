# 01 Task Definition



## §0 摘要

TEND 是一个 Text-to-NoSQL benchmark。其待解任务的输入是三元组 (NLQ, MongoDB schema, db_id)，输出是一段在指定数据库快照上 mongosh 可执行的 MongoDB 查询字符串。整个 benchmark 共覆盖 154 db、105 domain、347 collection，包含 17,020 条 record，每条 record 携带 5 条不同 specificity 级别的 NLQ（`L0`、`L1`、`L2`、`L3`、`L4` 的一种排列，`nl_queries[0]` 恒为 L1 canonical）与一段 gold MongoDB query，按 cross-domain 8:2 切分得到 14,245 train / 2,775 test。

本文档作为 TEND 任务语义层的 single source of truth，定义如下五件事：

1. §1 任务的形式化：将 TEND 任务建模为一个确定函数 $f$，给出输入空间、输出空间、数据快照与算子链
2. §2 输出空间约束：read-only / deterministic / mongosh-executable 三条核心性质、禁用算子表、以及代理指标的范围澄清
3. §3 正确性锚：唯一物理执行锚 $\mathrm{NormExec}(q_p, D) \equiv_{rec} \mathrm{NormExec}(q_g, D)$
4. §4 归一化契约：BSON 类型、复合结构、null-vs-missing、shape-preserving 子树保留的规范化规则
5. §5 递归相等关系 $\equiv_{rec}$ 的形式化定义

为支撑上述五件事，本文档另设三个支撑性章节：§6 给出 instance 正确性的根原则 P1-P4 及其对锚的耦合方式与在数据集构造侧的操作化对应；§7 用 canonical 示例（`db_id = orchestra`、`record_id = 99001`）把以上抽象定义具象化；§8 列出全文符号表。每条 record 的字段组织（5 条 NLQ × 5 specificity、gold MQL、Structured Intent、`canonical_form_set`、`noise_policies`、audit 信息等）与数据集切分细节由 [02 §2](./02_dataset_design.md#02-2) 给出。



<a id="01-1"></a>
## §1 任务的形式化

TEND 任务是一个确定的函数：

$$f:\ (\mathrm{NLQ},\ S,\ \mathit{dbid})\ \longrightarrow\ q^{\mathrm{MQL}}$$

### §1.1 输入空间


| 输入分量            | 类型    | 含义                                                                               |
| --------------- | ----- | -------------------------------------------------------------------------------- |
| $\mathrm{NLQ}$  | 字符串   | 用户用自然语言（英文）描述的查询意图                                                               |
| $S$             | 结构化对象 | 目标 MongoDB 数据库的 schema 描述，包含 collection 列表、字段名、字段 BSON 类型、嵌套结构与跨 collection 引用关系 |
| $\mathit{dbid}$ | 字符串   | 目标数据库的全局唯一标识，用于唯一定位数据快照 $D$                                                      |


三个分量缺一不可：缺 $\mathrm{NLQ}$ 则没有意图；缺 $S$ 则模型必须从底层数据反向猜测结构；缺 $\mathit{dbid}$ 则锚定不到具体数据快照，使 §3 的执行锚失效。

### §1.2 输出空间


| 输出                 | 类型  | 含义                                   |
| ------------------ | --- | ------------------------------------ |
| $q^{\mathrm{MQL}}$ | 字符串 | mongosh 可解析、可在 $D$ 上直接执行的 MongoDB 查询 |


$q^{\mathrm{MQL}}$ 的合法形式覆盖 `db.<collection>.find(...)` 与 `db.<collection>.aggregate([...])` 两类顶层语法，且必须满足 §2 给出的三条核心性质。

### §1.3 数据快照 $D$

$$D \equiv D(\mathit{dbid})$$

$D$ 是 $\mathit{dbid}$ 唯一绑定的只读 MongoDB 数据库快照。任何对 $D$ 的访问都不得修改其状态。同一 $\mathit{dbid}$ 在 benchmark 生命周期内对应同一个 $D$，确保 §3 的执行锚是 well-defined 的。

### §1.4 原子算子与复合算子

定义三个原子算子：


| 算子               | 签名                                                 | 语义                                             |
| ---------------- | -------------------------------------------------- | ---------------------------------------------- |
| $\mathrm{Parse}$ | $\mathrm{string} \to \mathrm{AST}$                 | 将 $q^{\mathrm{MQL}}$ 字符串解析为 mongosh 可识别的查询 AST |
| $\mathrm{Exec}$  | $(\mathrm{AST},\ D) \to \mathrm{ResultSet}$        | 将查询 AST 在 $D$ 上求值，返回原生 BSON 结果集                |
| $\mathrm{Norm}$  | $\mathrm{ResultSet} \to \mathrm{NormalizedResult}$ | 按 §4 的归一化契约，把 BSON 结果集映射到统一可比较表示               |


复合算子定义为：

$$\mathrm{NormExec}\ :=\ \mathrm{Norm}\ \circ\ \mathrm{Exec}\ \circ\ \mathrm{Parse}$$

即 $\mathrm{NormExec}(q,\ D) = \mathrm{Norm}(\mathrm{Exec}(\mathrm{Parse}(q),\ D))$。$\mathrm{NormExec}$ 是 §3 唯一锚的左右两侧的求值器。



<a id="01-2"></a>
## §2 输出空间约束

### §2.1 三条核心性质

任意属于任务输出空间的 $q^{\mathrm{MQL}}$ 必须同时满足下述三条性质：


| 性质                 | 含义                                                                                  |
| ------------------ | ----------------------------------------------------------------------------------- |
| read-only          | $q^{\mathrm{MQL}}$ 不修改 $D$ 的任何状态；执行前后 $D$ 字节级一致                                     |
| deterministic      | 给定同一 $q^{\mathrm{MQL}}$ 与同一 $D$，重复执行 $\mathrm{Exec}$ 必返回结构与值完全一致的结果集                |
| mongosh-executable | $q^{\mathrm{MQL}}$ 可被官方 mongosh 直接 $\mathrm{Parse}$ 并 $\mathrm{Exec}$，无需任何外部预处理或宏展开 |


### §2.2 禁用算子表

下列 6 个算子至少破坏一条核心性质，因此不在 TEND 任务的输出空间内：


| 算子          | 破坏的性质                     | 失效原因                      |
| ----------- | ------------------------- | ------------------------- |
| `$sample`   | deterministic             | 随机采样导致同输入多结果              |
| `$rand`     | deterministic             | 运行期随机数注入                  |
| `$$NOW`     | deterministic             | 运行期系统时间注入                 |
| `$out`      | read-only                 | 写出到 collection            |
| `$merge`    | read-only                 | 写入或合并到 collection         |
| `$function` | deterministic + read-only | 任意 JS 子程序，副作用与不确定性均无法静态约束 |


**范围澄清**：§2.2 禁用算子表的语义是"这些算子不在 TEND 任务的输出空间"，即 gold $q_g$ 不会包含它们，正确性锚 $\mathrm{NormExec}$ 也不必为它们定义。本节不规定模型在生成阶段如何处理这些算子；模型生成是否触发它们以及如何被打分，由 §3 的执行锚捕获，而非任务定义本身。

<a id="01-2-3"></a>
### §2.3 代理指标的范围澄清

TEND 任务定义层仅承认一条物理执行锚——§3 给出的 $\mathrm{NormExec}(q_p, D) \equiv_{rec} \mathrm{NormExec}(q_g, D)$。评测层在实践中除了直接实例化该锚的 EX 指标外，还会基于 $q_p$ 的字符串、AST、中间执行产物等计算一组辅助指标；本节澄清这些辅助指标与任务语义锚的关系：它们都是锚式的代理（proxy），不构成独立于 §3 的第二正确性判定。

**代理指标按判定对象划分为两层**：


| 层次     | 判定对象                                                                 | 覆盖指标             |
| ------ | -------------------------------------------------------------------- | ---------------- |
| 语法层代理  | 对 $q_p$ 与 $q_g$ 在查询字符串 / 结构 / 字段 / AST 层面的直接比较                       | EM, QSM, QFC, QIM |
| 语义层代理  | 对 $\mathrm{Exec}(q_p, D)$ 中间产物（非 $\mathrm{NormExec}$ 终态输出）的弱等价判定      | EFM, EVM         |


这两类代理指标的判定协议由评测层 [05 §1](./05_evaluation_methodology.md#05-1) 实例化；任务定义本身不规定其具体形态，只声明三条边界：

- 任一代理指标的"通过"都不能覆盖 EX 的"失败"——代理 ≠ 独立锚
- 任一代理指标的"失败"都不必被视为任务层面的 incorrect——代理仅提供诊断分解
- EX 仍是 headline，也是唯一与本文档 §3 锚式语义完全对齐的指标

**QIM（Query Idiomatic Match）的归属**：QIM 是语法层代理集合中专门用于检测"$q_p$ 在 AST 层面是否满足 $q_g$ 所声明的结构约束"的指标。它对照 gold 侧携带的 `canonical_form_set` 字段判定 $q_p$ 是否符合惯用的 NoSQL 结构；`canonical_form_set` 本身是一个四元组

$$\texttt{canonical\_form\_set} = \{\ \texttt{must\_contain},\ \texttt{must\_not\_contain},\ \texttt{must\_contain\_at\_root},\ \texttt{must\_not\_contain\_at\_root}\ \}$$

其值为 MQL operator token 列表（如 `$addFields`、`$map`、`$unwind`、`$group` 等），在构造期由 [04 §3.1](./04_dataset_construction.md#04-3) 的 SI DSL 与 Gold MQL 共同派生并冻结。QIM 的形式化定义与比较协议由 [05 §1.8](./05_evaluation_methodology.md#05-1) 给出：

$$\mathrm{QIM}(x) = \mathbb{1}\!\left[\mathrm{Parse}(q_p) \neq \bot \wedge \mathrm{AST\_check}(\mathrm{Parse}(q_p), \mathrm{canonical\_form\_set}(q_g)) = \mathrm{pass}\right]$$

QIM 始终对 $q_p$ AST 做语法层比对，与 EM/QSM/QFC 同层；它既不读取 $\mathrm{Exec}(q_p, D)$ 的执行产物，也不复制 EX 的 $\equiv_{rec}$ 判定。因此 QIM 不构成独立于 §3 物理锚的第二判定：当 EX 通过而 QIM 失败时，样本在任务层面仍属正确，只在评测层被标注为"shape 退化"或"结构不符合惯用"以支持诊断披露（详见 [05 §1.8](./05_evaluation_methodology.md#05-1) 与 [05 §5](./05_evaluation_methodology.md#05-5)）。



<a id="01-3"></a>
## §3 正确性锚

TEND 任务的唯一物理执行锚为：

$$\mathrm{NormExec}(q_p,\ D)\ \equiv_{rec}\ \mathrm{NormExec}(q_g,\ D)$$

其中 $q_p$ 是被评测系统给出的 predicted 查询，$q_g$ 是 gold 查询，$D$ 由 $\mathit{dbid}$ 唯一绑定，$\equiv_{rec}$ 由 §5 严格定义。

该等式是 TEND 任务层面唯一的正确性判定：任务定义不引入任何独立于本式的第二判定。评测层汇报的 7 比特指纹 (EM, QSM, QFC, EX, EFM, EVM, QIM) 中，EX 为 headline 并直接实例化本式，其余 6 项（EM, QSM, QFC, EFM, EVM, QIM）均为本式的代理（proxy）。任何在本文档之外被引入但与本式语义不一致的判定，都不属于 TEND 任务正确性的定义。

本式是任务层面唯一判定；代理指标的范围澄清见 §2.3。



<a id="01-4"></a>
## §4 结果归一化契约

$\mathrm{Norm}$ 算子的目标是消除 BSON 物理表示差异，使 $\equiv_{rec}$ 的比较只依赖语义内容。

### §4.1 标量类型规范化


| BSON 类型                                      | 规范化目标表示                                         |
| -------------------------------------------- | ----------------------------------------------- |
| `ObjectId`                                   | 24 位小写 hex 字符串                                  |
| `Date`                                       | ISO-8601 UTC 字符串，固定带后缀 `Z`，毫秒精度                 |
| `Decimal128`                                 | 全精度十进制字符串，保留尾随零的语义形态                            |
| `Long` (Int64)                               | 若数值落在 JS 安全整数范围内，序列化为 number；否则序列化为字符串          |
| `Binary`                                     | base64 字符串                                      |
| `Regex`                                      | `/pattern/flags` 字面格式                           |
| `NaN` / `+Infinity` / `-Infinity`            | 显式 token：`"NaN"` / `"Infinity"` / `"-Infinity"` |
| `String` / `Bool` / `Int32` / `Double`（非特殊值） | 保持原值                                            |
| `null`                                       | 保持为 null（与 missing 严格区别，见 §4.3）                 |


### §4.2 复合结构规范化


| 结构       | 规则                                     |
| -------- | -------------------------------------- |
| document | 映射为 dict；键集合保持不变；每个值递归应用 §4.1 / §4.2   |
| array    | 映射为 list；元素位置保持原序；每个元素递归应用 §4.1 / §4.2 |


### §4.3 null 与 missing 的严格区分

约定 `{"a": null}` 与 `{}` 在 $\equiv_{rec}$ 下不相等：前者声明字段 `a` 存在且取值 null，后者声明字段 `a` 不存在。该区分在涉及 `$project`、`$lookup`、`$unwind` 时尤其关键：

- `$project: {a: 1}` 与 `$project: {a: {$ifNull: ["$a", null]}}` 可能在 missing/null 上产生差异
- `$lookup` 失败匹配产生的字段是空数组 `[]`，与 missing 也不相等
- `$unwind` 在 `preserveNullAndEmptyArrays` 不同设置下会区别对待 missing、null、`[]`

§4 的合约要求 $\mathrm{Norm}$ 严格保留这些差别，不做任何 missing/null 折叠。

### §4.4 `_id` 的处理


| 情形                                       | 规则                                             |
| ---------------------------------------- | ---------------------------------------------- |
| gold 查询保留 `_id`                          | predicted 必须保留 `_id`，且 `_id` 字段值按 §5 与 gold 比较 |
| gold 显式投影出 `_id`（如 `$project: {_id: 0}`） | 归一化结果不含 `_id` 键                                |
| gold 与 predicted 在 `_id` 处理上不一致          | $\equiv_{rec}$ 必然失败，由 §5 自动捕获                  |


$\mathrm{Norm}$ 不擅自删除或注入 `_id`；它的唯一职责是按上述规则呈现 $\mathrm{Exec}$ 的真实输出。

**shape-preserving 情形下的子树保留规则**：当 gold 查询属于 shape-preserving 家族（即 SI 声明的 `output.shape` $\in \{\texttt{shape\_preserved\_augmented},\ \texttt{nested\_with\_projected\_subtree},\ \texttt{polymorphic\_output}\}$）时，$\mathrm{Norm}$ 保留输入文档的全部原嵌套子树（包括各层 array、embedded document、叶子标量）不做任何折叠、重排或字段删除；只在根层追加 gold 显式声明的新字段（例如通过 `$addFields` 产生的聚合结果字段）。missing / null / `[]` 的严格区分契约（§4.3）在原子树与新追加字段上一并保持：原子树的稀疏字段不因 augmentation 被实例化为 null，新字段也不因输入为空自动降级为 missing。具体哪些字段属于"子树保留"、哪些属于"根层追加"，由 [04 §3.1](./04_dataset_construction.md#04-3) 的 `SI.output.shape` 与 `canonical_form_set` 共同声明；$\mathrm{Norm}$ 只按声明呈现，不推断、不降级、不合并。



<a id="01-5"></a>
## §5 递归相等关系 $\equiv_{rec}$ 的形式化定义

$\equiv_{rec}$ 是定义在 NormalizedResult（§4 输出空间）上的递归相等关系。

### §5.1 标量层

对任意标量值 $x, y$：

$$x \equiv_{rec} y\ \iff\ \mathrm{type}(x) = \mathrm{type}(y)\ \wedge\ \mathrm{value}(x) = \mathrm{value}(y)$$

其中 $\mathrm{type}$ 与 $\mathrm{value}$ 都来自 §4.1 的规范化结果。例如归一化后的 `Long` 与 `Int32`/`Double` 即使数值相等，因 type 不同而不等价；`Decimal128` 字符串 `"1.00"` 与 `"1.0"` 也不等价（保留尾随零的语义形态差异）。

### §5.2 字典层

对任意字典 $a, b$：

$$a \equiv_{rec} b\ \iff\ \mathrm{keys}(a) = \mathrm{keys}(b)\ \wedge\ \forall k \in \mathrm{keys}(a):\ a[k] \equiv_{rec} b[k]$$

字典层比较与键序无关，但与键集合敏感（缺键即不等）。

### §5.3 列表层

对任意列表 $u = (u_1, \dots, u_m)$ 与 $v = (v_1, \dots, v_n)$，**默认顺序敏感**：

$$u \equiv_{rec} v\ \iff\ m = n\ \wedge\ \forall i \in [1, m]:\ u_i \equiv_{rec} v_i$$

当且仅当 gold 来源标明结果为无序集合（如查询不含 `$sort` 且语义不依赖元素位置）时，比较器先按预先约定的规范全序对 $u, v$ 同时排序，再逐位比较。该排序在比较器侧承担，不进入 $\mathrm{NormExec}$ 本身。

### §5.4 顶层语义

$\mathrm{NormExec}(q, D)$ 的根永远是文档列表（即 list of dict）。两边是否按位置比较，完全遵循 §5.3 给出的顺序敏感判定。综合 §5.1–§5.3，$\equiv_{rec}$ 在顶层、字典层、列表层、标量层各自递归收敛于明确定义的相等条件。



<a id="01-6"></a>
## §6 Instance 正确性的根原则

§3 的锚 $\mathrm{NormExec}(q_p, D) \equiv_{rec} \mathrm{NormExec}(q_g, D)$ 依赖一个隐含前提：gold $q_g$ 在样本 $(\mathrm{NLQ}, S, D)$ 上是唯一可信的正确解。本节把该前提显式拆解为四条根原则。

### §6.1 四条根原则


| 原则  | 名称    | 内容                                                                |
| --- | ----- | ----------------------------------------------------------------- |
| P1  | 执行正确  | $\mathrm{NormExec}(q_g, D)$ 不抛错、可被 §4 完整归一化、且其结果代表 NLQ 所述意图       |
| P2  | 语义唯一  | NLQ 在 $(S, D)$ 上的语义意图唯一；不存在另一个"在自然语言层同样合理但语义本质不同"的意图 $q_g'$       |
| P3  | 判别力   | 平凡 baseline（空集、全集、常量返回）在 $D$ 上不与 $\mathrm{NormExec}(q_g, D)$ 递归相等 |
| P4  | 世界非平凡 | $D$ 中存在足够实例使查询语义被真实触发；不出现 vacuous truth                           |


### §6.2 与正确性锚的耦合


| 原则  | 对锚的角色                                                     |
| --- | --------------------------------------------------------- |
| P1  | 锚的执行前提：若 P1 失败，则锚右侧 $\mathrm{NormExec}(q_g, D)$ 本身未定义或无意义 |
| P2  | 锚的语义合法性：若 P2 失败，则锚衡量的"正确"对应多个互不相容的目标，等式失去判定意义             |
| P3  | 锚的信噪比：若 P3 失败，则平凡 baseline 也能通过锚，无法区分有效模型与无效模型            |
| P4  | 锚在 $D$ 上有效：若 P4 失败，则即使语义不同的查询也可能在 vacuous 数据上同时返回空集而触发锚等式 |


四条原则共同保证 §3 的等式既 well-defined（P1 + P2）也 informative（P3 + P4）。

### §6.3 P1-P4 在数据集构造侧的操作化对应

P1-P4 是任务语义层的根原则；在数据集构造侧，它们各自由具体的可执行检查负责落地。操作化对应如下：

- **P1（执行正确）** 由 V1' spec correctness 与 V2' perturbation robustness 共同操作化：V1' 要求 gold $q_g$ 在 canonical world 上通过自动派生的 checker；V2' 进一步要求 gold 在 $K = 2$ perturbed worlds 上仍通过 checker（K 个 candidate world 中择一发布为 canonical）
- **P2（语义唯一）** 由 V3' SI consistency 操作化：由 $\geq 3$ 个跨 vendor 的 LLM 把 NLQ 各自 parse 成 Structured Intent（SI），并要求所得 SI 全部在 $\equiv_{SI}$ 意义下结构等价；$\equiv_{SI}$ 同时覆盖 `nosql_nativeness` 的三元组等价与 `canonical_form_set` 的集合等价
- **P3（判别力）** 由 V4' mechanical mutation 全枚举 + property-based 邻域 testing 操作化，**并由 V7' SQL-bridge defeat test 做对抗增强**：当 SI 的 `nosql_nativeness.level` $\geq$ L2 时，V7' 调用一组三方 disjoint 的 SQL-bridge panel（由 NL2SQL 模型与确定性 `sqltomongo` 翻译器串联而成，panel 的模型集合与 V3' / V5' 的 LLM 集合、V6' RP_diff 模型集合两两不相交）生成候选 MQL 对抗准入；凡同时通过 EX 与 QIM 的 SQL-bridge 候选都被判定为"SQL 可轻易复现的平凡样本"而阻止其进入主集，从而保证 test set 在 L2+ 级别上能真正测出 NoSQL-native 的判别力
- **P4（世界非平凡）** 由 V5' 5% 人审 anchor sample audit 与 V6' empirical difficulty calibration（RP_diff 5 frozen 模型 pass_rate）共同覆盖：V5' 从专家视角排除 vacuous 语义与歧义；V6' 通过经验 pass_rate 把"过于容易 / 过于困难"两端的样本标注回难度层，避免锚在某个子群上失效

具体如何在数据集构造侧把 P1-P4 落实为可执行检查（V1'-V7'）与经验难度校准（V6'），由 [04 §8](./04_dataset_construction.md#04-8) 与 [04 §9](./04_dataset_construction.md#04-9) 负责；V7' SQL-bridge defeat test 的 panel manifest 与三方 disjointness 协议由 [04 §8](./04_dataset_construction.md#04-8) 与 [04 §9](./04_dataset_construction.md#04-9) 给出。



<a id="01-7"></a>
## §7 canonical 示例的语义解读

为把 §1–§6 的抽象定义具象化，本节固定一个 canonical 示例。该示例在所有相关文档中字面一致。

### §7.1 canonical 三元组


| 字段            | 值                                                                                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `db_id`       | `orchestra`                                                                                                                                                            |
| `record_id`   | `99001`                                                                                                                                                                |
| canonical NLQ | `"For each conductor, attach a total_performances field counting all performances across their orchestras, while preserving the original conductor document structure."` |


该 NLQ 占据 canonical 槽位（即 `nl_queries[0]`，对应 specificity 级别 L1），对应的 intent pattern 为 `shape_preserving_augment`，`nosql_nativeness.level` 为 L4（NoSQL-exclusive）。该 record 的库级资产由 [03 §1](./03_database_synthesis.md#03-1) 与 [03 §5](./03_database_synthesis.md#03-5) 的 Agentic 合成管线产出（Domain Architect 立题 → Schema Designer 长出 4 层嵌套 → Business Simulator 跑 $K = 2$ worlds → Noise Planner 按预算注入 6 层带噪），由 [04 §3.1](./04_dataset_construction.md#04-3) 与 [04 §6](./04_dataset_construction.md#04-6) 接管后完成 SI 派生、Gold MQL 生成、`canonical_form_set` 派生、5 条 NLQ × 5 specificity 的填充，以及 V1'-V7' spec-grounded validation 与 V6' RP_diff 经验难度校准；评测层仅消费 record 主体字段，求解侧不读取任何 audit 中间态（边界由 [06 §7](./06_solution_design.md#06-7) 定义）。

### §7.2 schema 4 层语义形态

数据库 `orchestra` 的逻辑结构按嵌套关系展开为四层：

```
conductor
└── orchestra[]                       (一个 conductor 旗下若干 orchestra)
    └── performance[]                 (一个 orchestra 在若干 performance 中演出)
        └── show[]                    (一场 performance 包含若干 show)
```

`[]` 表示该层是 array 字段。conductor 是 collection 根，`orchestra` / `orchestra.performance` / `orchestra.performance.show` 都是 embedded array；4 层嵌套的语义形态是本 canonical 示例 L4 nativeness 的直接根源（任何"把嵌套压平再 group"的实现都会破坏 shape-preserving 约束，见 §7.4）。

### §7.3 NLQ 的操作语义与 Gold MQL

canonical NLQ 的操作语义为：

1. 对 `conductor` collection 中的每一份 conductor 文档，计算其名下所有 orchestra 的所有 performance 的总条数
2. 将该总数以新字段 `total_performances` 的形式追加到该 conductor 文档的**根层**
3. 保留原 conductor 文档的全部嵌套结构（`orchestra` array 及其内层 `performance` / `show` array）不做任何压平、重排或字段删除

对应的 canonical gold MQL 是一个 in-place 的 `$addFields` + `$map` 形态：

```javascript
db.conductor.aggregate([
  { $addFields: {
      total_performances: {
        $sum: {
          $map: {
            input: { $ifNull: ["$orchestra", []] },
            as: "orch",
            in: { $size: { $ifNull: ["$$orch.performance", []] } }
          }
        }
      }
  } }
]);
```

该 gold MQL 的关键字段在 SI 与 audit 中的派生如下：

- `pattern: shape_preserving_augment`
- `nosql_nativeness.level: L4`
- `output.shape: shape_preserved_augmented`
- `operator_family: "shape_preserving_augment"`
- `tds_cell: "nested_4_deep+sparse_embedded × shape_preserving_augment × medium × schema_naive × english"`
- `canonical_form_set.must_contain: ["$addFields", "$map"]`
- `canonical_form_set.must_not_contain_at_root: ["$unwind", "$group"]`
- `noise_policies`: `applied_layers=[Structural]`、`type_ids=["Structural.sparse_optional_name"]`、`coupling_operators=["$ifNull"]`、`noise_seed=42`
- `target_difficulty=medium`、`empirical_difficulty=medium`、`pass_rate=0.6`、`idiomatic_score=0.92`

该 MQL 只有一个 stage（单 `$addFields`）；其内部 `$map` 在 `orchestra` 数组内部做 in-place 尺寸聚合，`$ifNull` 覆盖 sparse conductor（无 `orchestra` 字段）与 sparse orchestra（`orchestra` 元素无 `performance` 字段）两类缺失语义，`$sum` 把每个 orchestra 的 performance 数加总后写入根层 `total_performances`。整条管线不展开任何 array、不重组任何 document、不删除任何现存字段。

### §7.4 为什么不做 `$unwind` / `$group`

canonical gold 的 `output.shape` 为 `shape_preserved_augmented`，这对应 §4.4 的 shape-preserving 子树保留规则：输出文档集合必须与输入 `conductor` collection **文档数完全一致**、且**每份输出文档的原嵌套子树完整保留**，仅在根层追加新字段。

任何 `$unwind: "$orchestra"` + `$unwind: "$orchestra.performance"` + `$group` 的组合写法都会：

1. 把一份 conductor 文档展开为若干条 performance 行，输出文档数 $\neq$ 输入 conductor 数
2. `$group` 之后只能通过手工 `$first: "$..."` 逐字段恢复被展开的上层字段，无法完整保留 4 层嵌套子树（`orchestra` / `performance` / `show`）的原序与原结构
3. 即便尝试后接一次 `$lookup` 或 `$replaceRoot` 回写，也无法保证 `_id` 之外的原嵌套完全字节一致

因此 canonical gold 选择 `$addFields` + `$map` 的 in-place 形态：`$map` 在数组内部计算 performance 数的总和，既不展开 `orchestra` array，也不破坏 `performance` / `show` 的内层结构；`$addFields` 仅在根层追加 `total_performances`，对所有原字段不作触碰。这是 `canonical_form_set.must_contain = ["$addFields", "$map"]` 与 `must_not_contain_at_root = ["$unwind", "$group"]` 两条硬约束的直接语义根源。

### §7.5 判别力来源（按 P3）

下表列举若干错误 baseline 及其错位形态，说明 canonical gold 在 P3 下的判别力来源：


| 错误 baseline                                                                          | 错位形态                                                                                                        |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `$unwind "$orchestra" + $unwind "$orchestra.performance" + $group` 计算 count 再 `$lookup` merge 回 | 输出结构被压平，conductor 文档数不再等于原始 conductor 数；即使 `$lookup` 回写，`orchestra` / `performance` / `show` 嵌套子树已被破坏，shape 断言失败 |
| 顶层使用 `$project: {total_performances: ..., _id: 1, Name: 1}` 输出聚合结果                 | `$project` 默认去除所有未显式声明的字段，导致 `orchestra` 子树被投影删除，破坏子树保留                                                   |
| `$map` 的 `in` 直接写 `$$orch.performance`（即数组本身）而非 `{$size: {$ifNull: [...]}}` | 聚合对象错位（被加和的是 array 本身的 concat 结果而非 performance 条数），`$sum` 要么报错要么语义完全不符                                    |
| 漏 `$ifNull`（不覆盖 sparse 分支）                                                           | 遇到 `noise_policies` 注入的 sparse conductor（无 `orchestra` 字段）或 sparse orchestra（无 `performance` 字段）时 null / missing 分支未处理，`$map` 报错或 `$size` 对 null 抛错 |
| 顶层 `$group: { _id: "$_id", total_performances: ... }` 承担 `total_performances` 聚合 | `$group` 只保留 `_id` 与聚合字段，原 conductor 文档的其他字段（包括 `Name`、`orchestra` 子树）全部丢失                                 |


上述任何偏差都会使 $\mathrm{NormExec}(q_p, D)$ 与 $\mathrm{NormExec}(q_g, D)$ 在 §5.3 列表层或 §5.2 字典层发生不等，从而被 §3 的锚拒绝。判别力的源头有四：**聚合形态**（必须是 in-place `$addFields` + `$map`，不可展开）、**子树保留**（所有原字段必须字节一致保留）、**sparse 处理**（必须覆盖 null / missing 两类缺失）、**根层新字段**（`total_performances` 必须在根层而非嵌套位置）。QIM 在此基础上通过 `canonical_form_set` 的 `must_contain` / `must_not_contain_at_root` 两条约束对 $q_p$ AST 做独立的语法层诊断，进一步暴露即使 EX 偶然通过、但结构已偏离惯用的退化候选（参见 §2.3 对 QIM 与 EX 关系的澄清）。

### §7.6 在 $D$ 上的非平凡性（按 P4）

`orchestra` 数据快照 $D$ 必须满足：

1. conductor 数 $\geq 3$，避免 "每位 conductor" 的遍历语义退化为单实例
2. 至少存在一位 conductor，其 `orchestra[].performance[]` 非空，使 `total_performances` 能取到正整数值，从而在 canonical 结果集中与其他 conductor 产生判别性差异
3. 至少存在 `noise_policies.type_ids = ["Structural.sparse_optional_name"]` 所注入的稀疏实例（如缺失 `Name` 字段的 conductor、或 `orchestra` 为 null / 缺失的 conductor），使 `$ifNull` 分支在真实快照上被触发并暴露漏写 `$ifNull` 的退化候选
4. 至少存在一位 conductor，其下多个 orchestra 的 performance 数分布不均（而非全 0 或全同值），使 `total_performances` 成为有语义负载的聚合字段而非常量字段

满足以上四点，§3 的锚在 canonical 示例上方为 informative，P3 与 P4 协同保证错误 baseline 在真实快照上必然被锚拒绝。



<a id="01-8"></a>
## §8 全文符号表


| 符号                    | 含义                                                                                                   | 首次出现章节  |
| --------------------- | ---------------------------------------------------------------------------------------------------- | ------- |
| $\mathrm{NLQ}$        | 用户用自然语言（英文）描述的查询意图                                                                                   | §1.1    |
| $S$                   | 目标 MongoDB 数据库的 schema 描述                                                                            | §1.1    |
| $\mathit{dbid}$       | 目标数据库的全局唯一标识                                                                                         | §1.1    |
| $D$                   | $\mathit{dbid}$ 唯一绑定的只读数据快照                                                                          | §1.3    |
| $q^{\mathrm{MQL}}$    | mongosh 可执行的 MongoDB 查询字符串                                                                           | §1.2    |
| $q_p$                 | 被评测系统给出的 predicted 查询                                                                                | §3      |
| $q_g$                 | gold 查询                                                                                              | §3      |
| $f$                   | 任务函数 $(\mathrm{NLQ}, S, \mathit{dbid}) \to q^{\mathrm{MQL}}$                                         | §1      |
| $\mathrm{Parse}$      | 字符串到查询 AST 的解析算子                                                                                     | §1.4    |
| $\mathrm{Exec}$       | 查询 AST 在 $D$ 上求值算子                                                                                   | §1.4    |
| $\mathrm{Norm}$       | BSON 结果集到统一可比较表示的归一化算子                                                                               | §1.4    |
| $\mathrm{NormExec}$   | $\mathrm{Norm} \circ \mathrm{Exec} \circ \mathrm{Parse}$ 复合算子                                        | §1.4    |
| $\equiv_{rec}$        | 递归相等关系                                                                                               | §3 / §5 |
| P1                    | 根原则：执行正确                                                                                             | §6.1    |
| P2                    | 根原则：语义唯一                                                                                             | §6.1    |
| P3                    | 根原则：判别力                                                                                              | §6.1    |
| P4                    | 根原则：世界非平凡                                                                                            | §6.1    |
| `canonical_form_set`  | $q_g$ 的结构约束集合（`{must_contain, must_not_contain, must_contain_at_root, must_not_contain_at_root}`） | §2.3    |
| `nosql_nativeness`    | 意图的 NoSQL 原生度（L0-L4）                                                                                 | §6.3    |
| QIM                   | Query Idiomatic Match 代理指标                                                                           | §2.3    |
| V7'                   | SQL-bridge defeat 构造期验证                                                                              | §6.3    |


---

下游指针：

- record 字段与 audit 信息：[02 §2 record schema](./02_dataset_design.md#02-2)
- Agentic 数据库合成方法（Agent 架构、三控制线、Taxonomy Board、Noise Taxonomy 6 层）：[03 §1](./03_database_synthesis.md#03-1)、[03 §5](./03_database_synthesis.md#03-5)、[03 §A](./03_database_synthesis.md#03-A)
- 从 Agentic 合成产物汇入到 NLQ 的完整构造流水线、SI DSL（含 `nosql_nativeness` / `canonical_form_set`）、23 个 intent pattern、V1'-V7' 验证证书、RP_diff 经验难度校准：[04 §3](./04_dataset_construction.md#04-3)、[04 §6](./04_dataset_construction.md#04-6)、[04 §8](./04_dataset_construction.md#04-8)、[04 §9](./04_dataset_construction.md#04-9)
- 7 个评测指标 EM / QSM / QFC / EX / EFM / EVM / QIM 的公式与协议（EX 为 headline）：[05 §1](./05_evaluation_methodology.md#05-1)
- 面向本任务的 SMART 4 阶段方法架构与求解侧硬边界（含 audit 屏蔽清单、三方 disjointness）：[06 §1](./06_solution_design.md#06-1)、[06 §7](./06_solution_design.md#06-7)
