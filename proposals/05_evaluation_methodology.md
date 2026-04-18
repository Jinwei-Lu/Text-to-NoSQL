# TEND 评测方法学（Evaluation Methodology）

<a id="05-0"></a>
## §0 摘要

TEND 评测层（evaluation layer）做且仅做三件事：

1. 把 [01 §3](./01_task_definition.md#01-3) 给出的物理执行锚 $\operatorname{NormExec}(q_p, D) \equiv_{rec} \operatorname{NormExec}(q_g, D)$ 实例化为 7 个标量指标 EM, QSM, QFC, EX, EFM, EVM, QIM（[§1](#05-1)）。
2. 在 [02](./02_dataset_design.md) 给出的 test set（2,775 条 record）上，按 record 逐条计算 7 指标，再求 sample-mean，并为每条 record 显式落盘失败类型（[§2](#05-2)）。
3. 用一份 manifest 摘要锁，把"输入数据 + 运行时栈 + 评测器代码 + canonical_form_extractor + RP_diff 参考面板 + V7' SQL-bridge panel"全部 hash 进同一份记录，使任何一条 hash 不一致都直接中止评测（[§3](#05-3)）。

EX 是 headline 指标（直接实例化 [01 §3](./01_task_definition.md#01-3) 的物理执行锚 $\operatorname{NormExec}(q_p, D) \equiv_{rec} \operatorname{NormExec}(q_g, D)$）；其余 6 个指标（EM, QSM, QFC, EFM, EVM, QIM）都是代理或诊断指标（diagnostic metrics），用于在 EX = 0 时定位错误层级，或在 EX = 1 时揭示写法层面的 idiomatic 合规性。其中 QIM 是对 $q_p$ AST 的语法层代理（见 [§1.8](#05-1-8)），归属 [01 §2.3](./01_task_definition.md#01-2-3) 的代理指标范畴，不构成独立于 $\operatorname{NormExec}$ 的第二物理锚。本文档不定义任何超出 7 指标之外的派生量；不允许把 EX 拆成多个变体；不允许把 7 指标按 family、难度档或模板分桶后再当作 headline 上报。

TEND benchmark 的锁定数字为 154 db / 105 domain / 347 collection / 17,020 (NLQ, NoSQL) record pairs，按 14,245 train / 2,775 test 的 cross-domain 8:2 切分（详见 [02](./02_dataset_design.md)）。本文档评测协议与该切分严格一致。

<a id="05-1"></a>
## §1 7 指标的形式定义

<a id="05-1-1"></a>
### §1.1 符号约定与依赖算子

固定记号表如下，本文档全程沿用：

| 符号 | 含义 |
|---|---|
| $x$ | 一条 test record |
| $q_g(x)$ | 该 record 的 gold MQL |
| $q_p(x)$ | 模型对该 record 给出的预测 MQL |
| $D(x)$ | 该 record 对应 db_id 的只读数据快照 |
| $\operatorname{Parse}$ | 把 MQL 串解析为结构化 AST 的算子 |
| $\operatorname{Exec}$ | 在数据快照上执行已解析查询的算子 |
| $\operatorname{Norm}$ | [01 §4](./01_task_definition.md#01-4) 定义的结果归一化算子 |
| $\operatorname{NormExec}$ | $\operatorname{Norm} \circ \operatorname{Exec} \circ \operatorname{Parse}$ 的复合 |
| $\equiv_{rec}$ | [01 §5](./01_task_definition.md#01-5) 定义的递归相等关系 |
| $\bot$ | 解析失败、执行失败、超时或运行期错误的统一占位符 |

为表达 7 指标，引入六个纯派生算子（pure derived operators）：

- `norm_str(q)`：串面归一化。两端空白剥离后再把连续空白折叠为单个空格。仅作字符串层归一化，不做语义归一化。
- `stages(Parse(q))`：取 aggregation pipeline 的 stage 算子序列；`find` 调用视作长度为 1 的 `[find]`。
- `fields(Parse(q))`：取查询体中触及的字段路径集合，以完整嵌套路径为单位。
- `keys(r)` 与 `drop_keys(r)`：`keys` 递归收集结果对象 $r$ 中所有出现过的字段名集合；`drop_keys` 把规范树按结构剥离字段名后保留值的形态。
- `canonical_form_set(q_g)`：$q_g$ 的结构约束四元组 $C = (\text{must\_contain}, \text{must\_not\_contain}, \text{must\_contain\_at\_root}, \text{must\_not\_contain\_at\_root})$，每个分量均为 MQL operator token 集合；由 [04 §5.7](./04_dataset_construction.md#04-5) 机械派生，落盘路径 `audit/<db_id>/<record_id>/derived/canonical_form_set.json`。
- `AST_check(AST, C)`：对 AST 与 canonical_form_set $C$ 的结构性断言算子。令 $T$ 为 AST 中全部 operator token 的集合、$T_{\mathrm{root}}$ 为 AST 根层（top-level pipeline stage）operator token 的集合，则

$$\operatorname{AST\_check}(\mathrm{AST}, C) = \begin{cases} \text{pass} & \text{若}\ C.\text{must\_contain} \subseteq T \ \wedge\ T \cap C.\text{must\_not\_contain} = \emptyset \ \wedge \\ & \quad C.\text{must\_contain\_at\_root} \subseteq T_{\mathrm{root}} \ \wedge\ T_{\mathrm{root}} \cap C.\text{must\_not\_contain\_at\_root} = \emptyset \\ \text{fail} & \text{否则} \end{cases}$$

fail 分支须附不匹配原因（missing tokens / forbidden tokens / missing root tokens / forbidden root tokens 至少一条）作为诊断字段落盘，便于 [§6](#05-6) D-16 的 record 级抽样清单回溯。

`drop_keys` 形态澄清：把每个字典 $\{k_1: v_1, k_2: v_2, \ldots\}$ 按 $k_i$ 字典序排序后取值序列 $[v_{\sigma(1)}, v_{\sigma(2)}, \ldots]$，每个 $v_{\sigma(i)}$ 递归剥离；列表保持原顺序，元素递归剥离；标量原样返回。该形态保证"键名错但值对"与"键名值都对"在 EVM 下可被区分（参见 [§1.7](#05-1-7)）。

`canonical_form_set` 空约束行为：若四个集合全空（must_contain = must_not_contain = must_contain_at_root = must_not_contain_at_root = $\emptyset$），则 `AST_check` 对任意 AST 必然返回 pass；此时 QIM 退化为 $\mathbb{1}[\operatorname{Parse}(q_p) \neq \bot]$（详见 [§1.8](#05-1-8)）。

<a id="05-1-2"></a>
### §1.2 EM (Exact Match)

$$\operatorname{EM}(x) = \mathbb{1}\!\left[\operatorname{norm\_str}(q_p(x)) = \operatorname{norm\_str}(q_g(x))\right]$$

性质：

- 即便预测查询语义完全等价但写法不同（如等价改写顺序、重命名变量名），EM 也判 0。
- `norm_str` 不做语义层归一化；它仅做空白折叠。
- EM 不依赖 `Parse` 或 `Exec`，因此不会因解析或执行失败而走到 $\bot$ 分支。
- 若预测产物本身是空串或非字符串，EM 直接判 0。

<a id="05-1-3"></a>
### §1.3 QSM (Query Stage Match)

$$\operatorname{QSM}(x) = \mathbb{1}\!\left[\operatorname{Parse}(q_p) \neq \bot \,\wedge\, \operatorname{Parse}(q_g) \neq \bot \,\wedge\, \operatorname{stages}(\operatorname{Parse}(q_p)) = \operatorname{stages}(\operatorname{Parse}(q_g))\right]$$

性质：

- `stages` 只比较 stage 算子序列（如 `[$unwind, $unwind, $group, $sort, $limit, $project]`）。
- 序列比较是有序的：调换 `$sort` 与 `$limit` 顺序会判 0（语义虽相近但骨架不同）。
- $\operatorname{Parse}(q_p) = \bot$ 或 $\operatorname{Parse}(q_g) = \bot$（gold 解析失败不应发生，[04 §8](./04_dataset_construction.md#04-8) 已保证）任一情形导致 QSM = 0。

<a id="05-1-4"></a>
### §1.4 QFC (Query Field Coverage)

$$\operatorname{QFC}(x) = \mathbb{1}\!\left[\operatorname{Parse}(q_p) \neq \bot \,\wedge\, \operatorname{Parse}(q_g) \neq \bot \,\wedge\, \operatorname{fields}(\operatorname{Parse}(q_p)) = \operatorname{fields}(\operatorname{Parse}(q_g))\right]$$

性质：

- `fields` 是集合（set）不是多重集合（multiset）；同一字段被多次引用不重复计数。
- 字段路径以完整嵌套路径为单位，例如 `orchestra.performance.Type` 与 `Type` 视为不同元素。
- 解析失败时 QFC = 0。

<a id="05-1-5"></a>
### §1.5 EX (Execution Match)

EX 直接实例化 [01 §3](./01_task_definition.md#01-3) 的物理执行锚。**这是 TEND 的 headline 指标**：

$$\operatorname{EX}(x) = \mathbb{1}\!\left[\operatorname{NormExec}(q_p(x), D(x)) \equiv_{rec} \operatorname{NormExec}(q_g(x), D(x))\right]$$

约定：若 $\operatorname{NormExec}(q_p, D) = \bot$ 则 EX = 0；该 record 仍计入分母（参见 [§2.3](#05-2-3)）。

EX 是其它诊断指标的物理真值参照：EX = 1 时 EFM 与 EVM 必同时为 1；EX = 0 时 EM / QSM / QFC / EFM / EVM 各自取值能告诉错误层级；EX 与 QIM 的组合还能区分 "执行正确且 idiomatic" 与 "执行正确但写法不 idiomatic（SQL-bridge 退化）"。诊断模板表（7 比特指纹；"-" 表示由错误模式直接决定的从属取值）：

| EM | QSM | QFC | EFM | EVM | QIM | EX | 典型解读 |
|---|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 1 | 1 | 1 | 完全正确 |
| 0 | 1 | 1 | 1 | 1 | 1 | 1 | 语义等价、写法不同但 canonical_form 合规 |
| 0 | 1 | 1 | 1 | 1 | 0 | 1 | **SQL-bridge 退化**：执行正确但 AST 不满足 canonical_form_set（典型：gold 要求 shape-preserving 的 `$addFields + $map`、预测写成 `$unwind + $group`；执行结果一致但结构退化） |
| 0 | 1 | 1 | 1 | 0 | 0 | 0 | 骨架与字段全对，错在算子或常量（值面错）；写法也不 idiomatic |
| 0 | 1 | 1 | 0 | 0 | 0 | 0 | 骨架对，`$project` 或 `$group` 重命名错 |
| 0 | 0 | 1 | 1 | 0 | 0 | 0 | 骨架错（漏 / 多 / 顺序），字段集合巧合相同 |
| 0 | 0 | 0 | 0 | 0 | 0 | 0 | 全错 |
| 0 | - | - | - | - | 0 | 0 | `parse_error`：$q_p$ 解析失败（QIM 由于 $\operatorname{Parse}(q_p) = \bot$ 取 0） |
| 0 | - | - | - | - | - | 0 | `exec_error_*` 或 `timeout`：$q_p$ 执行失败；QIM 按 `AST_check` 结果独立取值 |

说明：QIM 只依赖 $\operatorname{Parse}(q_p)$，不依赖 $\operatorname{Exec}$；`parse_error` 下 QIM = 0；`exec_error` 下 $\operatorname{Parse}$ 仍可能成功，故 QIM 取决于 `AST_check`。诊断模板表读法：前 7 列给出 7 比特指纹，最右列给出该指纹对应的典型解读；SQL-bridge 退化行是 TEND 区别于既有 benchmark 的关键诊断信号（详见 [§7.5](#05-7-5)）。

<a id="05-1-6"></a>
### §1.6 EFM (Execution Field Match)

$$\operatorname{EFM}(x) = \mathbb{1}\!\left[\operatorname{NormExec}(q_p, D) \neq \bot \,\wedge\, \operatorname{NormExec}(q_g, D) \neq \bot \,\wedge\, \operatorname{keys}(\operatorname{NormExec}(q_p, D)) = \operatorname{keys}(\operatorname{NormExec}(q_g, D))\right]$$

性质：

- `keys` 递归收集所有层级出现过的字段名（包含嵌套对象内的字段）。
- EFM = 1 不代表 EX = 1：字段名一致但值不同时 EFM = 1、EX = 0。
- $\operatorname{NormExec}$ 走 $\bot$ 分支时 EFM = 0。

<a id="05-1-7"></a>
### §1.7 EVM (Execution Value Match)

$$\operatorname{EVM}(x) = \mathbb{1}\!\left[\operatorname{NormExec}(q_p, D) \neq \bot \,\wedge\, \operatorname{NormExec}(q_g, D) \neq \bot \,\wedge\, \operatorname{drop\_keys}(\operatorname{NormExec}(q_p, D)) \equiv_{rec} \operatorname{drop\_keys}(\operatorname{NormExec}(q_g, D))\right]$$

性质：

- `drop_keys` 后字典按键字典序取值序列；递归处理嵌套结构。
- 当 EVM = 1 且 EFM = 1 时，必有 EX = 1；这是 EX 的可分解性。
- EVM = 1 但 EFM = 0 表示值对了但字段名错了（如 gold 输出键 `Name`、预测输出键 `name`，但值序列相同）。

<a id="05-1-8"></a>
### §1.8 QIM (Query Idiomatic Match)

QIM 对预测查询 $q_p$ 的 AST 施加结构约束检查，判断其是否落在 gold canonical_form_set 指定的 idiomatic 形态内：

$$\operatorname{QIM}(x) = \mathbb{1}\!\left[\operatorname{Parse}(q_p) \neq \bot \,\wedge\, \operatorname{AST\_check}(\operatorname{Parse}(q_p),\ \operatorname{canonical\_form\_set}(q_g)) = \text{pass}\right]$$

其中 `AST_check` 与 `canonical_form_set` 在 [§1.1](#05-1-1) 定义。

性质：

- QIM 是对 $q_p$ 的 AST 语法层代理，归属 [01 §2.3](./01_task_definition.md#01-2-3) 代理指标范畴；与 EM / QSM / QFC 同属"不触达执行"一类。
- QIM 不构成独立于 $\operatorname{NormExec}$ 物理锚的第二正确性判定；它仅给出 "q_p 的写法是否落在 gold canonical 形态簇内" 的布尔答案。
- QIM 的结构约束 `canonical_form_set(q_g)` 来自 [04 §5.7](./04_dataset_construction.md#04-5) 的机械派生产物；评测器在启动时从 `audit/<db_id>/<record_id>/derived/canonical_form_set.json` 加载 $C$，加载失败按 `gold_invalid` 处理（参见 [§2.3](#05-2-3)）。
- QIM 与 EX 的四种组合：
  - $(EX=1,\ QIM=1)$：**idiomatic 且正确**——预测查询既执行正确又落在 gold canonical 形态内。
  - $(EX=1,\ QIM=0)$：**SQL-bridge 退化**——执行结果与 gold 一致但 AST 违反结构约束；是 TEND 对 NL2SQL-bridge 类方法的关键判别信号。
  - $(EX=0,\ QIM=1)$：结构合规但执行失败——少见，通常意味着值面错或运行时错。
  - $(EX=0,\ QIM=0)$：结构与执行均错。
- QIM 不进入 headline；headline 仍是 EX（参见 [§1.10](#05-1-10)）。
- 若 `canonical_form_set(q_g)` 四个集合全空，`AST_check` 必返回 pass，此时 QIM 退化为 $\mathbb{1}[\operatorname{Parse}(q_p) \neq \bot]$——该情形意味着 gold 未施加任何结构约束，通常出现在非 NoSQL-原生 pattern（L0）上。
- QIM 与 EM / QSM / QFC 的区别：EM 是串面比较、QSM 比较 stage 序列（要求逐位相同）、QFC 比较字段路径集合（集合等价），而 QIM 比较 AST operator token 在 canonical_form_set 四个方向上的约束——它允许多种等价写法，只要 AST operator token 集合满足约束即可。

<a id="05-1-9"></a>
### §1.9 sample-mean 总分聚合

设 test set $T = \{x_1, \ldots, x_N\}$，对任一指标 $M \in \{\operatorname{EM}, \operatorname{QSM}, \operatorname{QFC}, \operatorname{EX}, \operatorname{EFM}, \operatorname{EVM}, \operatorname{QIM}\}$：

$$\overline{M}(T) = \frac{1}{N} \sum_{i=1}^{N} M(x_i)$$

报告以百分比形式呈现，保留两位小数。$N = 2{,}775$；分母 $N$ 必须显式披露（见 [§6](#05-6) D-1 与 D-2）。

<a id="05-1-10"></a>
### §1.10 EX 是 headline 指标

EX 在 7 指标中具有特殊地位：

- 它直接实例化任务正确性锚 $\operatorname{NormExec}(q_p, D) \equiv_{rec} \operatorname{NormExec}(q_g, D)$（[01 §3](./01_task_definition.md#01-3)）。
- 其余 6 个指标是该锚的代理或诊断切面：EM / QSM / QFC 是语法层串面 / 结构代理；EFM / EVM 是结果层代理；QIM 是语法层结构代理（对 $q_p$ AST 与 gold canonical_form_set 的静态比较）。
- 论文与对外比较的主声明（headline claim）以 EX 为准。
- 任何把 EX 与其它 6 个指标做加权平均、几何平均、F1 合成等复合分数（composite score）的做法均不属于本规范，不得作为 headline；QIM 同样不得作为 headline。

<a id="05-2"></a>
## §2 评测协议

<a id="05-2-1"></a>
### §2.1 数据范围

- 评测在且仅在 [02](./02_dataset_design.md) 定义的 `TEND/test.json`（2,775 条 record）上进行。
- 不允许在 train split（14,245 条）上跑指标作为 headline 数。
- 不允许子采样、不允许按 db_id 抽样、不允许"剔除某些样本后再算"。

<a id="05-2-2"></a>
### §2.2 单 record 计算流程

对每条 record `(record_id, db_id, nl_queries, q_g)` 与对应预测 $q_p$，按下列顺序执行：

1. 取数据快照 $D(x) = D(\text{db\_id})$；$D$ 必须从评测器自带的只读 `mongosh` 实例中加载（见 [§3.5](#05-3-5)）。加载 $C = \operatorname{canonical\_form\_set}(q_g)$，读取自 `audit/<db_id>/<record_id>/derived/canonical_form_set.json`。
2. 计算 EM：调用 `norm_str` 后比较，不需要 Parse。
3. 计算 QSM 与 QFC：先 $\operatorname{Parse}(q_p)$。若 $\bot$ 则 QSM = 0、QFC = 0 并落盘 `parse_error`；否则继续 `stages` 与 `fields` 比较。$\operatorname{Parse}(q_g)$ 不应失败（[04 §8](./04_dataset_construction.md#04-8) 已保证），若失败则该 record 在评测层标记为 `gold_invalid` 并写入诊断日志，但仍按 [§2.3](#05-2-3) 计入分母。
4. 计算 EX、EFM、EVM：调用 $\operatorname{NormExec}(q_p, D)$；按 $\bot$ 类型细分失败标签（`parse_error` / `exec_error_validation` / `exec_error_runtime` / `exec_error_other` / `timeout`）；否则按 [§1.5](#05-1-5)–[§1.7](#05-1-7) 判定。
5. 计算 QIM：若 $\operatorname{Parse}(q_p) = \bot$ 则 QIM = 0 且已落盘 `parse_error`；否则令 $\operatorname{QIM} = 1\ \text{if}\ \operatorname{AST\_check}(\operatorname{Parse}(q_p), C) = \text{pass}\ \text{else}\ 0$。QIM 计算独立于步骤 4 的执行路径（QIM 只依赖 Parse，不依赖 Exec）；`exec_error_*` 与 `timeout` 不影响 QIM 取值。`AST_check` = fail 时落盘不匹配原因字段 `ast_check_violation` $\in$ {`missing_must_contain`, `forbidden_must_not_contain`, `missing_root`, `forbidden_root`, `mixed`}。
6. 逐 record 落盘诊断 JSON，包含 `record_id` / `db_id` / 7 个指标取值（EM, QSM, QFC, EX, EFM, EVM, QIM） / `pred_status` / `pred_rows` / `gold_rows` / `elapsed_ms` / 若 QIM = 0 且 Parse ≠ $\bot$ 则附 `ast_check_violation`。

<a id="05-2-3"></a>
### §2.3 失败类型与计入规则

任一种失败均映射到统一的"该 record 在该指标上记 0"规则；失败的 record 始终保留在分母 N 中，不静默丢弃。失败类型表：

| 类型 | 触发条件 | 影响指标 |
|---|---|---|
| `parse_error` | $\operatorname{Parse}(q_p) = \bot$ | QSM, QFC, EX, EFM, EVM, QIM 均 0；EM 仍按串面比较 |
| `exec_error_validation` | `mongosh` 拒绝执行（schema validation 或禁用算子） | EX, EFM, EVM 均 0；QSM, QFC, EM, QIM 不受影响（QIM 只依赖 $\operatorname{Parse}(q_p)$，不依赖 $\operatorname{Exec}$） |
| `exec_error_runtime` | `mongosh` 接受查询但运行时抛错 | EX, EFM, EVM 均 0；QSM, QFC, EM, QIM 不受影响 |
| `exec_error_other` | 其它非分类执行错误 | EX, EFM, EVM 均 0；QSM, QFC, EM, QIM 不受影响 |
| `timeout` | 单 record 执行超过 30 秒上限 | EX, EFM, EVM 均 0；QSM, QFC, EM, QIM 不受影响 |
| `gold_invalid` | $q_g$ 自身解析或执行失败、或 `canonical_form_set.json` 缺失 / 损坏（不应发生，[04 §8](./04_dataset_construction.md#04-8) V1'-V7' 与 [04 §5.7](./04_dataset_construction.md#04-5) 已过滤） | 7 指标均 0；该 record 列入诊断报告的 `gold_invalid` 列表显式披露 |

所有失败类型必须出现在 [§6](#05-6) 强制披露清单中，按计数披露而非只披露总分。

<a id="05-2-4"></a>
### §2.4 比较关系

- 标量、字典、列表三种构件的递归相等关系 $\equiv_{rec}$ 由 [01 §5](./01_task_definition.md#01-5) 定义，本文档不重定义。
- 列表是否按位置比较或排序后比较的判定（[01 §5](./01_task_definition.md#01-5)）由 gold 来源标明的"是否无序集合"决定。
- 标量在 BSON 类型层的归一化由 [01 §4](./01_task_definition.md#01-4) 定义。

<a id="05-2-5"></a>
### §2.5 不允许的偏离

1. 不允许重新定义指标（不得在 EX 中加入"近似匹配"分支；不得改写 QIM 的 AST_check 语义）。
2. 不允许改判定（不得把 EX ≈ gold 当作 EX；EX 是布尔；QIM 同样是布尔）。
3. 不允许跳样本（任何 $q_p$ 都必须送入评测器，包括明显格式错的）。
4. 不允许两次执行后取最优（评测必须是确定性单次执行）。
5. 不允许在评测期间写库（详见 [§3.5](#05-3-5) 只读不变式）。

<a id="05-3"></a>
## §3 复现性契约（输入锁 + 运行时锁）

manifest 由两部分构成：输入锁（[§3.1](#05-3-1)）与运行时锁（[§3.2](#05-3-2)）。所有摘要均使用 SHA-256，以小写 hex 形式记录。

<a id="05-3-1"></a>
### §3.1 输入锁

| 条目 | 路径模式 | 说明 |
|---|---|---|
| 预测文件 | `predictions/<run_id>.json` | 模型对 test set 的输出 |
| test set | `TEND/test.json` | 2,775 条 test record |
| schema 集合 | `TEND/mongodb_schema/<db_id>.json` | 逐文件 hash |
| 数据快照集合 | `TEND/mongodb_data/<db_id>.json` | 逐文件 hash |
| canonical_form_set 集合 | `audit/<db_id>/<record_id>/derived/canonical_form_set.json` | 逐文件 hash；QIM 依赖的结构约束源 |

<a id="05-3-2"></a>
### §3.2 运行时锁

| 条目 | 取证方式 | 说明 |
|---|---|---|
| `mongosh` 镜像 | docker image digest（`sha256:...`） | 与构造侧 [04 §9](./04_dataset_construction.md#04-9) RP_diff 用的镜像一致 |
| 评测器代码 | `metric.py` SHA-256 | 7 指标实现、失败处理、超时控制 |
| 解析器代码 | `extract_stages.py` / `extract_fields.py` / `mongosh_exec.py` 各 SHA-256 | `stages` / `fields` / `Parse` / `Exec` 代理实现 |
| **canonical_form_extractor** | `extract_canonical_form.py` SHA-256 | `AST_check` 算子实现；消费 `Parse(q_p)` 与 canonical_form_set，产出 pass / fail + violation 字段 |
| collation / locale | `{ locale: "en", strength: 3 }` | 控制 `$sort` 与字符串比较 |
| timezone | UTC | 影响 `$dateToString` 等 |
| 单 record 超时 | 默认 30 秒 | 与构造侧一致 |
| **RP_diff 参考面板 manifest digest** | `audit/reference_panel/diff_panel_manifest.json` 的文件 SHA-256 | 锁定经验难度校准面板；评测器在启动时验证 [04 §9](./04_dataset_construction.md#04-9) 与 [06 §7.3](./06_solution_design.md#06-7) 的 disjointness 约束（求解侧 LLM ID 不得与 RP_diff models 相交） |
| **V7' SQL-bridge panel manifest digest** | `audit/reference_panel/sql_bridge_manifest.json` 的文件 SHA-256 | 锁定构造期 NL2SQL ∘ sqltomongo defeat panel；评测器在启动时验证 [04 §9](./04_dataset_construction.md#04-9) 与 [06 §7.3](./06_solution_design.md#06-7) 的三方 disjointness 约束（SMART 求解侧 LLM ID ∩ V3' / V5' LLM ID ∩ RP_diff models ∩ V7' SQL-bridge panel models 两两空） |

<a id="05-3-3"></a>
### §3.3 manifest 形态

完整 manifest JSON 形态：

```json
{
  "benchmark": "TEND",
  "run_id": "<对外标识符>",
  "input_lock": {
    "predictions_sha256": "<hex>",
    "test_json_sha256": "<hex>",
    "schema_sha256": { "orchestra": "<hex>", "...": "..." },
    "data_sha256": { "orchestra": "<hex>", "...": "..." },
    "canonical_form_set_sha256": { "orchestra/99001": "<hex>", "...": "..." }
  },
  "runtime_lock": {
    "mongosh_image_digest": "sha256:<hex>",
    "metric_py_sha256": "<hex>",
    "extract_stages_py_sha256": "<hex>",
    "extract_fields_py_sha256": "<hex>",
    "mongosh_exec_py_sha256": "<hex>",
    "canonical_form_extractor_sha256": "<hex>",
    "collation": { "locale": "en", "strength": 3 },
    "timezone": "UTC",
    "per_record_timeout_seconds": 30,
    "diff_panel_manifest_sha256": "<hex>",
    "sql_bridge_manifest_sha256": "<hex>"
  }
}
```

<a id="05-3-4"></a>
### §3.4 摘要不一致的中止规则

评测器启动时执行三件检查：

1. 计算输入锁所列每个文件的 SHA-256（含 `canonical_form_set.json` 逐文件摘要）。
2. 计算运行时锁所列代码文件、镜像 digest、`diff_panel_manifest` digest 与 `sql_bridge_manifest` digest；并据 `sql_bridge_manifest.json` 验证求解侧 LLM ID 与 RP_diff models、V3' / V5' LLMs、V7' SQL-bridge panel models 的三方 disjointness。
3. 把上述结果与 manifest 中预声明的值逐字段比对。

任一字段不一致或三方 disjointness 校验失败 ⟹ 评测器中止运行；不允许"只警告 + 继续"。该硬中止保证任意一条复现报告都可由第三方在拿到 manifest 后逐位验证。

<a id="05-3-5"></a>
### §3.5 评测的只读不变式

- $D$ 在评测期间是只读快照（`mongosh` 实例以只读模式挂载快照卷）。
- 评测器不允许使用 [01 §2.2](./01_task_definition.md#01-2) 列出的禁用算子；若 $q_p$ 含禁用算子，按 `exec_error_validation` 处理。
- 评测期间不允许 import 数据、不允许重建索引、不允许做任何 schema migration。
- `null` 与 `missing` 在评测层严格区分（[01 §4.3](./01_task_definition.md#01-4)）。

<a id="05-4"></a>
## §4 结果归一化（引用 [01](./01_task_definition.md)）

<a id="05-4-1"></a>
### §4.1 引用关系

本文档不重新定义结果归一化。所有规则由 [01 §4](./01_task_definition.md#01-4) 唯一定义。本文档中所有出现的 $\operatorname{NormExec}$ 都隐式调用该归一化算子。

<a id="05-4-2"></a>
### §4.2 null 与 missing 强调

特此强调（不重定义）：`{"a": null}` 与 `{}` 在 [01 §4.3](./01_task_definition.md#01-4) 归一化下仍然不同；EFM 在两者上判 0，EX 同样判 0；`drop_keys` 在两者上也产生不同序列。这一判定不可在评测层放宽。

<a id="05-4-3"></a>
### §4.3 顺序敏感性

- 默认情形列表按位置比较（position-wise）。
- 仅当 gold 来源标明为无序集合时，先按规范全序排序再按位置比较。
- 评测层不自行判断 NLQ 是否暗示排序意图；该判定来自 [02](./02_dataset_design.md) 中 record 的 gold 标注。

<a id="05-5"></a>
## §5 报告结构

<a id="05-5-1"></a>
### §5.1 主表（test set 全集）

主表覆盖 test set 全集（N = 2,775，与 [02 §4](./02_dataset_design.md#02-4) cross-domain 8:2 切分一致）。每个数字是该指标的 sample-mean。表头：分母 N / EM / QSM / QFC / **EX**（headline，加粗） / EFM / EVM / QIM。报告必须以分母 N 作为第一行；EX 必须加粗以与其它 6 个诊断指标区分；不允许把 EX 单独放到与其它指标不同的列轴上让读者无法平视。

主表示意：

| 系统 | N | EM | QSM | QFC | **EX** | EFM | EVM | QIM |
|---|---|---|---|---|---|---|---|---|
| `<system_id>` | 2775 | xx.xx | xx.xx | xx.xx | **xx.xx** | xx.xx | xx.xx | xx.xx |

<a id="05-5-2"></a>
### §5.2 cross-domain 切片表

按 db_id（或更粗的 domain，由 [02](./02_dataset_design.md) 给出）分组，每组报告 7 指标。要求：

- $\sum_g N_g = N$（所有切片样本数之和必须等于全集）。
- 切片不允许只展示 top-K db_id；要么列全，要么把剩余打包为 `others` 单独一行并显式写出 N。
- 切片以 db_id 为最细粒度，不允许进一步按 record 内部 audit 字段做主切片（那属于 [§5.3](#05-5-3)）。

<a id="05-5-3"></a>
### §5.3 可选辅助切片

允许下列辅助切片，仅作诊断用，不进入 headline 主张：

1. 按 MQL pipeline 长度切片（aggregation pipeline 长度，`find` 视为 1）。
2. 按 `schema_complexity_profile` 切片（如嵌套深度、collection 数量等）。
3. **按 `empirical_difficulty` 切片**：四档 `{easy, medium, hard, expert}`（来自 [04 §9](./04_dataset_construction.md#04-9) RP_diff 实测分桶；本切片揭示模型在不同经验难度档上的表现）。
4. **按 SI pattern 切片**：23 个 pattern（来自 [04 §3.2](./04_dataset_construction.md#04-3)）；本切片揭示模型在不同意图模式上的能力分布。
5. 按 `coverage_neighbors` 子集切片：选取嵌入空间稀疏区域的 record 子集（来自 [04 §10.5](./04_dataset_construction.md#04-10)）。
6. **按 `nosql_nativeness_level` 切片**：五档 `{L0, L1, L2, L3, L4}`（来自 [04 §3.1](./04_dataset_construction.md#04-3) SI.nosql_nativeness.level）；本切片揭示模型在不同 NoSQL 原生度档上的 EX 与 QIM 表现；NL2SQL-bridge 类方法倾向在 L0-L1 接近 SOTA，而在 L2-L4 上 EX 与 QIM 同时显著下降。
7. **按 `canonical_form_compliance` 切片**：将 test set 按 (EX, QIM) 联合二元分类为四桶—$(1,1)$ idiomatic 且正确、$(1,0)$ SQL-bridge 退化、$(0,1)$ 结构合规但执行错、$(0,0)$ 全错；本切片揭示模型在"idiomaticness gap"上的具体分布；退化桶 $(1,0)$ 的占比即 D-16 的核心数字。

要求：

- 任何辅助切片必须在 [§6](#05-6) 强制披露清单中显式声明使用了 audit 字段。
- 辅助切片的 7 指标值不可写在主表里冒充全集 headline。
- 辅助切片不引入新指标。

<a id="05-5-4"></a>
### §5.4 不允许的报告形式

- 把同一 test set 拆为"主集 / 扩展集"双列上报。
- 引入 5 档难度（如 L1-L5）或 17 档算子特征作为主表的列轴。
- 报告 family-level 或模板-level 的指标（family 与模板属于构造层概念，不进入评测层 headline）。
- 以 sidecar 报告形式上报第二组指标。
- 不得单独报告 QIM 作为 headline 指标；QIM 必须出现在主表的诊断列中，而不得作为对外的主 claim。

<a id="05-6"></a>
## §6 强制披露清单

每次对外发布 TEND 评测结果，至少披露下列条目；缺一不可：

| 编号 | 条目 | 形式 |
|---|---|---|
| D-1 | test set 总样本数 N | 整数 |
| D-2 | 每个指标的分母 | 7 个整数；正常情况应等于 N |
| D-3 | 每个指标的解析失败计数（`parse_error`） | 7 个整数（EM 恒为 0；QSM / QFC / EX / EFM / EVM / QIM 相等于 `parse_error` 总数） |
| D-4 | 执行失败计数（按 `exec_error_validation` / `exec_error_runtime` / `exec_error_other` 分类） | 三个整数 |
| D-5 | 超时计数（`timeout`） | 整数 |
| D-6 | `gold_invalid` 列表（不应非空；若非空必须列出 record_id） | record_id 数组 |
| D-7 | 7 指标的总分 $\overline{M}(T)$ | 7 个百分数 |
| D-8 | cross-domain 切片表 | [§5.2](#05-5-2) 形态 |
| D-9 | 复现性 manifest 摘要 | [§3.3](#05-3-3) 的 manifest JSON 内容（含 `diff_panel_manifest_sha256`、`canonical_form_extractor_sha256`、`sql_bridge_manifest_sha256`） |
| D-10 | 是否使用了 audit 字段做辅助切片 | 布尔；若 true，列出使用的 audit 字段名 |
| **D-12** | **`empirical_difficulty` 在 test 上的分布 + RP_diff manifest digest + 三方 disjointness 验证结果** | 4 个百分数（`easy` / `medium` / `hard` / `expert`，按 [04 §9.4](./04_dataset_construction.md#04-9) 的分桶）+ `audit/reference_panel/diff_panel_manifest.json` 的 SHA-256；同时披露 SMART 求解侧的 LLM ID 集合，以及与 RP_diff models id、V3' / V5' LLM id、**V7' SQL-bridge panel model id** 的三方 disjointness 验证结果 |
| **D-13** | **5% 人审 anchor pass rate** | 浮点数 $\in [0, 1]$；来自 `audit/human_anchor/spot_audit.json`；伴随披露样本规模、审计员数量与意见分歧率 |
| **D-15** | **复杂度向量 $\vec{C}$ 在 test 上的分布** | 6 分量直方图（`C_schema` / `C_data` / `C_intent` / `C_query` / `C_nosql` / `C_cross`）；每分量 3 档（low / mid / high）占比，聚合自 `audit/<db_id>/<record_id>/complexity_vector.json`；复杂度向量语义由 [03 §3](./03_database_synthesis.md#03-3) 定义 |
| **D-16** | **QIM 分布 + `EX=1 ∧ QIM=0` 占比（SQL-bridge 退化率）** | 百分数（$\overline{\mathrm{QIM}}$） + 退化占比 $\lvert \{x: \mathrm{EX}(x)=1 \wedge \mathrm{QIM}(x)=0\} \rvert / N$ + record_id 抽样清单（最多 20 条，附 `ast_check_violation` 字段）；该占比是 TEND 对 NL2SQL-bridge 方法的关键判别信号 |
| **D-17** | **`nosql_nativeness` L0-L4 分布 + 各级 EX / QIM** | 5 档 record 计数（`L0` / `L1` / `L2` / `L3` / `L4`）+ 各档在主表 7 指标上的 sample-mean；来自 [04 §3.1](./04_dataset_construction.md#04-3) 的 SI.nosql_nativeness.level |
| **D-18** | **`sql_bridge_manifest` digest + 求解侧 LLM 与该 panel 的 disjointness 声明** | `audit/reference_panel/sql_bridge_manifest.json` 的 SHA-256（在 `runtime_lock` 中）+ 求解侧 LLM ID 集合（便于外部验证 [06 §7.3](./06_solution_design.md#06-7) 三方 disjointness：SMART LLM ∩ V3' / V5' LLM ∩ RP_diff ∩ SQL-bridge panel 两两空） |
| **D-19** | **噪声 6 层分布（`T_noise_mix` 轴）** | 6 个百分数（`Literal` / `Structural` / `Semantic` / `Historical` / `Pollution` / `Type-Polymorphism`）；各层在 train 与 test 上的覆盖比例，来自 `audit/coverage/coverage_report.json` 的 `taxonomy_axes.T_noise_mix` 子块；噪声层语义由 [03 §5](./03_database_synthesis.md#03-5) 与 [03 §A](./03_database_synthesis.md#03-A) 定义 |
| **D-20** | **`T_topology_features` 分布** | 7 个百分数（`flat` / `nested_N_deep` / `polymorphic_collection` / `dynamic_key_document` / `sparse_embedded` / `mixed_embed_ref` / `intentional_denormalization`）；每特性在 train 与 test 上的 record 级覆盖比例；来自 `audit/coverage/coverage_report.json` 的 `taxonomy_axes.T_topology_features` 子块；F_topology 语义由 [03 §4.1](./03_database_synthesis.md#03-4) 定义 |

D-2 至 D-6 是常被忽略但极易导致误读的条目（高总分有可能伴随大量 `parse_error` 静默失败）。D-12 至 D-20 是 TEND benchmark 区别于既有 benchmark 的关键质量证据：D-12 锁定经验难度面板的可复现性、求解侧 LLM ID 集合及三方 disjointness 验证；D-13 提供独立的人工抽样验证；D-15 揭示 6 维复杂度向量在测试集上的分布；D-16 揭示 QIM 在整体与 (EX=1, QIM=0) 退化桶上的占比，是判别 SQL-bridge 类方法的核心信号；D-17 揭示模型在 NoSQL 原生度五档上的表现梯度；D-18 锁定 V7' SQL-bridge panel manifest 并声明求解侧 LLM 与该 panel 的 disjointness；D-19 揭示 6 层噪声在数据集上的覆盖分布；D-20 揭示 F_topology 7 特性在数据集上的覆盖分布。

<a id="05-7"></a>
## §7 canonical 示例

用 [01 §7](./01_task_definition.md#01-7) 约定的 canonical 示例演示四种典型预测下 7 指标的取值过程。该 db 的 4 层 schema 为 `conductor → orchestra[] → performance[] → show[]`。

- db_id：`orchestra`
- record_id：`99001`
- 定位：本 canonical 实例 SI.nosql_nativeness.level = L4（详见 [04 §3.6](./04_dataset_construction.md#04-3)）——即最高 NoSQL 原生度档，关系型等价改写会结构退化。

<a id="05-7-1"></a>
### §7.1 NLQ 与 gold MQL

- NLQ："For each conductor, attach a `total_performances` field counting all performances across their orchestras, while preserving the original conductor document structure."
- gold MQL（来自 [02](./02_dataset_design.md) record `record_id = 99001`；单一 `$addFields` 根层 stage，内部用 `$map + $size` 做 shape-preserving aggregation）：

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

gold 在 $D$ 上的执行结果（已归一化）形如（保留原 conductor 文档结构，根层新增 `total_performances` 字段）：

```json
[
  {
    "Conductor_ID": 1, "Name": "Antal Doráti", "Age": 62, "Nationality": "Hungarian",
    "orchestra": [
      { "Orchestra_ID": 11, "Orchestra_Name": "London Symphony Orchestra", "Year_of_Founded": 1904,
        "performance": [ {"Performance_ID": 101, "...": "..."}, {"Performance_ID": 102, "...": "..."} ] }
    ],
    "total_performances": 2
  },
  {
    "Conductor_ID": 2, "Name": "Igor Stravinsky", "...": "...",
    "total_performances": 5
  }
]
```

对应的 canonical_form_set（来自 [04 §5.7](./04_dataset_construction.md#04-5) 机械派生，落盘 `audit/orchestra/99001/derived/canonical_form_set.json`）：

```json
{
  "must_contain": ["$addFields", "$map"],
  "must_not_contain": [],
  "must_contain_at_root": ["$addFields"],
  "must_not_contain_at_root": ["$unwind", "$group"]
}
```

即：AST 中必须出现 `$addFields` 与 `$map`（可在任意嵌套层）；根层 pipeline stage 必须含 `$addFields` 且不得含 `$unwind` 与 `$group`。

<a id="05-7-2"></a>
### §7.2 示例 A：完全正确

预测 $q_p^{(A)}$ 与 gold 完全相同。逐指标推算：

| 指标 | 取值 | 推算 |
|---|---|---|
| EM | 1 | `norm_str(q_p) = norm_str(q_g)` |
| QSM | 1 | `stages = [$addFields]`，两侧相同 |
| QFC | 1 | `fields = {orchestra, orchestra.performance, total_performances}`，两侧相同 |
| EX | 1 | $\operatorname{NormExec}(q_p, D) \equiv_{rec} \operatorname{NormExec}(q_g, D)$ |
| EFM | 1 | `keys` 包含原 conductor 字段 + `total_performances`，两侧相同 |
| EVM | 1 | `drop_keys` 后值序列两侧相同 |
| QIM | 1 | AST 含 `$addFields` 与 `$map`；根层含 `$addFields`，不含 `$unwind` 与 `$group`；`AST_check` = pass |

7 比特指纹（顺序 EM, QSM, QFC, EX, EFM, EVM, QIM）：`(1, 1, 1, 1, 1, 1, 1)`。诊断意义：完全正确。

<a id="05-7-3"></a>
### §7.3 示例 B：`$sum` 误写为 `$avg`

预测保留 shape-preserving 单 `$addFields` 结构，但把外层 `$sum` 改写为 `$avg`；其它完全相同。执行后 `total_performances` 对每个 conductor 都退化为 "每个 orchestra 的 performance 数" 的平均值，与 gold 的"总和"不同。

逐指标推算：

| 指标 | 取值 | 推算 |
|---|---|---|
| EM | 0 | 串面 `$sum` 与 `$avg` 不同 |
| QSM | 1 | stage 仍为 `[$addFields]` |
| QFC | 1 | 字段集合不变 |
| EX | 0 | `total_performances` 的值错（平均值 ≠ 总和） |
| EFM | 1 | 输出字段仍含 `total_performances` |
| EVM | 0 | 值序列与 gold 不同 |
| QIM | 1 | AST 仍含 `$addFields` 与 `$map`；根层仅 `$addFields`，无 `$unwind` / `$group`；`AST_check` = pass |

7 比特指纹：`(0, 1, 1, 0, 1, 0, 1)`。诊断意义：骨架与 canonical form 合规，错在外层聚合算子（值面错）。

<a id="05-7-4"></a>
### §7.4 示例 C：漏 `$ifNull` 兜底

预测把 `{ $size: { $ifNull: ["$$orch.performance", []] } }` 写成 `{ $size: "$$orch.performance" }`，漏掉 null 兜底；其它与 gold 相同。对 sparse orchestra（`performance` 字段缺失）调用 `$size` 于 null 会在 `mongosh` 层抛运行时错，进入 `exec_error_runtime`。

逐指标推算：

| 指标 | 取值 | 推算 |
|---|---|---|
| EM | 0 | 串面缺 `$ifNull` |
| QSM | 1 | 仍单 `$addFields` stage |
| QFC | 1 | 字段集合不变（`$ifNull` 本身不引入新字段路径） |
| EX | 0 | sparse orchestra 触发 `$size` 于 null 抛错，记 `exec_error_runtime` |
| EFM | 0 | $\operatorname{NormExec}(q_p) = \bot$ |
| EVM | 0 | 同上 |
| QIM | 1 | AST 仍含 `$addFields` 与 `$map`；根层仅 `$addFields`，无 `$unwind` / `$group`；`AST_check` = pass（AST_check 不依赖 Exec） |

7 比特指纹：`(0, 1, 1, 0, 0, 0, 1)`。诊断意义：骨架与 canonical form 合规但运行时错（AST 合规但漏掉 null 兜底的噪声耦合，触发 [03 §5](./03_database_synthesis.md#03-5) Structural 层的 sparse 噪声）。

<a id="05-7-5"></a>
### §7.5 示例 D：SQL-bridge 退化

预测 $q_p^{(D)}$ 由 NL2SQL ∘ sqltomongo 翻译链产出——SQL 侧用 JOIN + GROUP BY 实现"每个 conductor 的 performance 总数"，翻译到 MongoDB 时变成 `$unwind + $group + $project` 的扁平化重组：

```javascript
db.conductor.aggregate([
  { $unwind: { path: "$orchestra", preserveNullAndEmptyArrays: true } },
  { $unwind: { path: "$orchestra.performance", preserveNullAndEmptyArrays: true } },
  { $group: {
      _id: "$_id",
      Conductor_ID: { $first: "$Conductor_ID" },
      Name: { $first: "$Name" },
      Age: { $first: "$Age" },
      Nationality: { $first: "$Nationality" },
      orchestra_reconstructed: { $push: "$orchestra" },
      total_performances: {
        $sum: { $cond: [ { $ifNull: ["$orchestra.performance", false] }, 1, 0 ] }
      }
  } },
  { $project: {
      _id: 0,
      Conductor_ID: 1, Name: 1, Age: 1, Nationality: 1,
      orchestra: "$orchestra_reconstructed",
      total_performances: 1
  } }
]);
```

该写法的执行结果在 `total_performances` 标量与 conductor 计数上与 gold 一致；`orchestra` 数组的"平坦化后重新 `$push`"恰好在当前数据分布下与原嵌套结构满足 $\equiv_{rec}$（[01 §5](./01_task_definition.md#01-5)）—— EX 通过。然而 AST 根层出现了 `$unwind` 与 `$group`，这正是 canonical_form_set.`must_not_contain_at_root` 禁止的两个 token。

逐指标推算：

| 指标 | 取值 | 推算 |
|---|---|---|
| EM | 0 | 串面完全不同 |
| QSM | 0 | stage 序列从 `[$addFields]` 变为 `[$unwind, $unwind, $group, $project]` |
| QFC | 0 | `$group` 引入中间字段 `orchestra_reconstructed`，fields 集合与 gold 不同 |
| EX | 1 | 执行结果 $\equiv_{rec}$ gold（平坦化重组在该数据分布下还原了原嵌套结构） |
| EFM | 1 | 最终 `$project` 后输出字段与 gold 一致 |
| EVM | 1 | 值序列 $\equiv_{rec}$ 一致 |
| QIM | 0 | AST 根层含 `$unwind` 与 `$group`，违反 `must_not_contain_at_root = ["$unwind", "$group"]`；`ast_check_violation = forbidden_root` |

7 比特指纹：`(0, 0, 0, 1, 1, 1, 0)`。诊断意义：**SQL-bridge 退化**——执行正确但写法不符合 shape-preserving idiomatic 约束；这正是 TEND benchmark 引入 QIM 的核心防御目标。

<a id="05-7-6"></a>
### §7.6 示例小结

- EX 是单一的"成功 / 失败"标尺；本节 4 个示例中示例 A 与示例 D 均取 EX = 1，但两者的写法形态截然不同。
- 7 指标的组合相当于"错误与写法层级的 7 比特指纹"：示例 B 与 C 同样 EX = 0，但通过 QSM / QFC / EFM / EVM 的不同取值能区分"算子错"与"漏噪声耦合"。
- 示例 D 是 EX 单独不足以判定"写法是否 idiomatic"的反证：仅用 EX 无法把 A 与 D 区分开来；QIM 作为独立的语法层代理恰好填补了这一诊断能力缺口——$(EX=1, QIM=0)$ 桶的占比（D-16）即对 NL2SQL-bridge 类方法的关键判别信号。
- 任一种失败都不要在评测层"修复"；评测层只如实反映预测产物，"修复"属于求解层职责（[06](./06_solution_design.md)）。

<a id="05-8"></a>
## §8 与方法文档的接口

[06 方法设计](./06_solution_design.md) 在其评测相关章节只能消费本文档定义的：

1. 7 指标公式（[§1](#05-1)）。
2. 评测协议（[§2](#05-2)）。
3. 复现性契约（[§3](#05-3)）。
4. 报告结构（[§5](#05-5)）。

明确禁止 [06](./06_solution_design.md) 做的事：

- 不得引入第 8、第 9 个指标作为主指标。
- 不得修改 [§2.3](#05-2-3) 失败处理规则（如把 `parse_error` 在求解侧静默重试若干次后再上报，会破坏单次执行不变式；将 QIM 的 `AST_check` 在求解侧改写也属于此禁区）。
- 不得改写 [§3](#05-3) manifest 字段（含 `collation` / `timezone` / `diff_panel_manifest_sha256` / `canonical_form_extractor_sha256` / `sql_bridge_manifest_sha256`）。
- 不得用 [§5.3](#05-5-3) 辅助切片代替 [§5.1](#05-5-1) 主表 headline（如只报告 easy 档分数，或只报告 (EX=1, QIM=1) 桶内的分数）。
- 不得把 QIM 作为 headline 上报；QIM 只能作为主表诊断列与 D-16 披露数字呈现。

[06](./06_solution_design.md) 可以引用 7 指标的诊断意义（[§1.5](#05-1-5) 诊断模板表 + [§1.8](#05-1-8) QIM 性质），用以解释方法的失败模式与改进路径，但必须注明引用 [§1.5](#05-1-5) 或 [§1.8](#05-1-8)。

评测层不引用 [06](./06_solution_design.md)：评测层只承诺"给定 $(q_p, q_g, D, \operatorname{canonical\_form\_set}(q_g))$，返回 7 指标 + 失败标签"。求解侧具体如何产出 $q_p$ 与评测层无关。

<a id="05-9"></a>
## §9 全文符号表

完整列出本文档用到的所有符号：

| 符号 | 含义 | 出处 |
|---|---|---|
| $T = \{x_1, \ldots, x_N\}$ | test set 全集 | [§1.9](#05-1-9) |
| $N$ | test set 样本数（$N = 2{,}775$） | [§1.9](#05-1-9) |
| $q_p$ | 模型预测的 MQL | [§1.1](#05-1-1) |
| $q_g$ | record 自带的 gold MQL | [§1.1](#05-1-1) |
| $D$ | record 对应的只读数据快照 | [§1.1](#05-1-1) |
| $\operatorname{Parse}$ | MQL 串到 AST 的解析算子 | [§1.1](#05-1-1) |
| $\operatorname{Exec}$ | AST 在数据快照上的执行算子 | [§1.1](#05-1-1) |
| $\operatorname{Norm}$ | 结果归一化算子（[01 §4](./01_task_definition.md#01-4)） | [§1.1](#05-1-1) |
| $\operatorname{NormExec}$ | $\operatorname{Norm} \circ \operatorname{Exec} \circ \operatorname{Parse}$ | [§1.1](#05-1-1) |
| $\equiv_{rec}$ | 递归相等关系（[01 §5](./01_task_definition.md#01-5)） | [§1.1](#05-1-1) |
| $\bot$ | 解析 / 执行 / 超时 / 运行期错误的统一占位符 | [§1.1](#05-1-1) |
| `norm_str` | 串面归一化（空白折叠） | [§1.1](#05-1-1) |
| `stages` | aggregation pipeline 的 stage 算子序列 | [§1.1](#05-1-1) |
| `fields` | 查询体中触及的字段路径集合 | [§1.1](#05-1-1) |
| `keys` | 结果对象中所有出现过的字段名集合 | [§1.1](#05-1-1) |
| `drop_keys` | 按结构剥离字段名后的值形态 | [§1.1](#05-1-1) |
| `canonical_form_set(q_g)` | gold 查询的结构约束四元组 | [§1.1](#05-1-1) |
| `AST_check(AST, C)` | AST 与 canonical_form_set $C$ 的结构断言算子 | [§1.1](#05-1-1) |
| EM | Exact Match | [§1.2](#05-1-2) |
| QSM | Query Stage Match | [§1.3](#05-1-3) |
| QFC | Query Field Coverage | [§1.4](#05-1-4) |
| EX | Execution Match（headline） | [§1.5](#05-1-5) |
| EFM | Execution Field Match | [§1.6](#05-1-6) |
| EVM | Execution Value Match | [§1.7](#05-1-7) |
| QIM | Query Idiomatic Match（headline 辅助诊断） | [§1.8](#05-1-8) |
| $\overline{M}$ | 指标 $M$ 在 $T$ 上的 sample-mean | [§1.9](#05-1-9) |
| `parse_error` | $\operatorname{Parse}(q_p) = \bot$ | [§2.3](#05-2-3) |
| `exec_error_validation` | `mongosh` 拒绝执行 | [§2.3](#05-2-3) |
| `exec_error_runtime` | `mongosh` 接受查询但运行时抛错 | [§2.3](#05-2-3) |
| `exec_error_other` | 其它非分类执行错误 | [§2.3](#05-2-3) |
| `timeout` | 单 record 执行超过 30 秒上限 | [§2.3](#05-2-3) |
| `gold_invalid` | $q_g$ 或 canonical_form_set 自身无效 | [§2.3](#05-2-3) |
| `ast_check_violation` | `AST_check` = fail 时的不匹配原因字段 | [§2.2](#05-2-2) |
| `input_lock` | 复现性 manifest 中的输入锁块 | [§3.1](#05-3-1) |
| `runtime_lock` | 复现性 manifest 中的运行时锁块 | [§3.2](#05-3-2) |
| `canonical_form_extractor_sha256` | runtime lock 中 `AST_check` 算子实现的指纹 | [§3.2](#05-3-2) |
| `sql_bridge_manifest_sha256` | V7' SQL-bridge panel manifest 指纹 | [§3.2](#05-3-2) |
| D-1 | test set 总样本数 N | [§6](#05-6) |
| D-2 | 每个指标的分母 | [§6](#05-6) |
| D-3 | 每个指标的解析失败计数 | [§6](#05-6) |
| D-4 | 执行失败计数（三类） | [§6](#05-6) |
| D-5 | 超时计数 | [§6](#05-6) |
| D-6 | `gold_invalid` 列表 | [§6](#05-6) |
| D-7 | 7 指标的总分 | [§6](#05-6) |
| D-8 | cross-domain 切片表 | [§6](#05-6) |
| D-9 | 复现性 manifest 摘要 | [§6](#05-6) |
| D-10 | audit 字段使用声明 | [§6](#05-6) |
| D-12 | `empirical_difficulty` 分布 + RP_diff manifest digest + 三方 disjointness 验证 | [§6](#05-6) |
| D-13 | 5% 人审 anchor pass rate | [§6](#05-6) |
| D-15 | 复杂度向量 $\vec{C}$ 在 test 上的分布（6 分量直方图） | [§6](#05-6) |
| D-16 | QIM 分布 + SQL-bridge 退化占比 | [§6](#05-6) |
| D-17 | `nosql_nativeness` L0-L4 分布 + 各级 EX / QIM | [§6](#05-6) |
| D-18 | `sql_bridge_manifest` digest + 求解侧 LLM disjointness 声明 | [§6](#05-6) |
| D-19 | 噪声 6 层分布（`T_noise_mix` 轴） | [§6](#05-6) |
| D-20 | `T_topology_features` 分布 | [§6](#05-6) |

---

下游指针：record 字段、训练 / 测试切分与 audit 资产定义在 [02 数据集设计](./02_dataset_design.md)；Agentic 数据库合成方法（Agent 架构、三控制线、Taxonomy Board、F_topology 7 特性、6 层 Noise Taxonomy、复杂度 6 分量）在 [03 数据库合成](./03_database_synthesis.md)；gold 来源与 V1'-V7' 验证证书、canonical_form_set 机械派生（[04 §5.7](./04_dataset_construction.md#04-5)）、RP_diff 经验难度与 V7' SQL-bridge panel 在 [04 §9](./04_dataset_construction.md#04-9)；任务签名、归一化契约、$\equiv_{rec}$ 与代理指标范围在 [01 任务定义](./01_task_definition.md)；面向 7 指标进行优化的 SMART 方法架构、6 SSoT 边界与 V1'-V7' 责任分配在 [06 方法设计](./06_solution_design.md)。
