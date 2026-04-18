# 04 · 评测方法（Evaluation Methodology）

> 本文件是 TEND benchmark 评测层的根定义（SSoT）。它只回答"在 [02 数据集设计](./02_dataset_design.md) 给出的 test set 上、用 [01 任务定义](./01_task_definition.md) 给出的语义锚，怎样把模型预测翻译成 6 个标量指标、怎样组织报告、怎样保证可复现"。它不重新定义任务签名（[01](./01_task_definition.md) 的事）、不重新定义 record 字段（[02](./02_dataset_design.md) 的事）、不重新定义构造流程（[03 数据集构造](./03_dataset_construction.md) 的事）、也不规定方法架构（[05 方法设计](./05_solution_design.md) 的事）。下游方法文档必须把本文档作为指标与协议的唯一参照点。

---

## §0 摘要 <a id="04-0"></a>

TEND 评测层做且仅做三件事：

1. 把 [01 §3](./01_task_definition.md) 的物理执行锚 $\mathrm{NormExec}(q_p,D) \equiv_{rec} \mathrm{NormExec}(q_g,D)$ 实例化为 **6 个标量指标**：`EM`、`QSM`、`QFC`、`EX`、`EFM`、`EVM`（[§1](#04-1)）。这与 `src/utils/metric.py` 第 21 行 `metrics_list = ('EX', 'EM', 'QSM', 'QFC', 'EFM', 'EVM')` 完全对齐——不增不减。
2. 在 [02](./02_dataset_design.md) 给出的单一 test set 上按 record 逐条计算 6 指标，再求 sample-mean，并为每条 record 显式落盘失败类型（[§2](#04-2)）。
3. 用一份 **manifest 摘要锁**把"输入数据 + 运行时栈 + 评测器代码"全部 hash 进同一份记录，使得任何一条 hash 不一致都直接中止评测、不强行产出不可比数字（[§3](#04-3)）。

**EX 是 headline 指标**，对应执行结果的递归相等；其余 5 个是诊断指标，用于回答"如果 EX 错了，错在串面、结构面、字段面还是值面"。本文档不定义任何超出 6 指标之外的派生量；不允许把 EX 拆成多个变体、不允许把 6 指标按 family / 难度档 / 模板分桶后再当作 headline 上报。

---

## §1 6 指标的形式定义 <a id="04-1"></a>

### 1.1 符号约定与依赖算子 <a id="04-1-1"></a>

固定记号：

| 符号 | 含义 | 来源 |
|---|---|---|
| $x$ | 一条 test record（[02](./02_dataset_design.md) 给出 schema） | [02](./02_dataset_design.md) |
| $q_g(x)$ | record $x$ 的 gold MQL（来自 record 字段 `MQL`） | [02](./02_dataset_design.md) |
| $q_p(x)$ | 模型对 record $x$ 输出的预测 MQL | 模型预测文件 |
| $D(x)$ | record $x$ 所在 `db_id` 唯一绑定的只读数据快照 | [01 §1.3](./01_task_definition.md) |
| $\mathrm{Parse}$ | mongosh 解析算子 | [01 §1.4](./01_task_definition.md) |
| $\mathrm{Exec}$ | mongosh 执行算子 | [01 §1.4](./01_task_definition.md) |
| $\mathrm{Norm}$ | BSON 结果归一化算子 | [01 §4](./01_task_definition.md) |
| $\mathrm{NormExec}$ | $\mathrm{Norm}\circ\mathrm{Exec}\circ\mathrm{Parse}$ 的复合 | [01 §1.4](./01_task_definition.md) |
| $\equiv_{rec}$ | 规范树上的递归相等 | [01 §5](./01_task_definition.md) |
| $\bot$ | 解析失败 / 执行失败 / 超时 / 运行期错误的统一占位符 | 本文 [§2.3](#04-2-3) |

为表达 6 指标，本文额外引入四个**纯派生算子**——它们都只是已有原子算子的封装，不引入新语义：

| 算子 | 签名 | 含义 |
|---|---|---|
| $\mathrm{norm\_str}(q)$ | $q^{\mathrm{MQL}} \to \mathrm{string}$ | 串面归一化：把查询字符串两端空白剥离、把所有连续空白序列折叠为一个空格；对应 `src/utils/metric.py` 中 `_deal_query` 的行为 |
| $\mathrm{stages}(\mathrm{Parse}(q))$ | $\mathrm{AST} \to [\mathrm{StageOp}]$ | 取 aggregation pipeline 的 stage 算子序列（如 `[$match, $group, $sort, $limit, $project]`）；`find` 视作长度为 1 的退化序列 `[find]`；对应 `src/utils/extract_stages.py::get_query_stages` |
| $\mathrm{fields}(\mathrm{Parse}(q))$ | $\mathrm{AST} \to \mathrm{set}[\mathrm{FieldPath}]$ | 取查询体中触及的字段路径集合（如 `{"orchestra.performance", "Conductor_ID", "Name"}`）；对应 `src/utils/extract_fields.py::extract_fields` |
| $\mathrm{keys}(r),\ \mathrm{drop\_keys}(r)$ | $\mathrm{NormResult} \to \mathrm{set}[\mathrm{string}]\ /\ \mathrm{NormResult}$ | $\mathrm{keys}$ 递归收集结果集中所有出现过的字段名集合；$\mathrm{drop\_keys}$ 把同一规范树按结构剥离字段名后保留值的形态（数组保持顺序、字典退化为按键字典序排序后的值序列） |

> 关于 $\mathrm{drop\_keys}$ 的形态澄清：把每个字典 $\{k_1{:}v_1, k_2{:}v_2, \dots\}$ 按 $k_i$ 字典序排序后取值序列 $[v_{\sigma(1)}, v_{\sigma(2)}, \dots]$，每个 $v_{\sigma(i)}$ 递归剥离；列表保持原顺序，元素递归剥离；标量原样返回。这样，$\mathrm{drop\_keys}$ 后的 $\equiv_{rec}$ 不再受字段名差异影响，只看"位置上的值"是否一致。

### 1.2 EM（Exact Match） <a id="04-1-2"></a>

EM 是查询字符串层的最严苛指标：两条查询在串面归一化后必须完全相同。

$$
\operatorname{EM}(x)\ =\ \mathbb{1}\!\left[\,\operatorname{norm\_str}\!\bigl(q_p(x)\bigr)\ =\ \operatorname{norm\_str}\!\bigl(q_g(x)\bigr)\,\right]
$$

性质：

- 即便预测查询语义完全等价，但用了不同的算子组合（如 `$lookup` 替代 `$unwind`+`$group`），EM 也判 0。
- $\mathrm{norm\_str}$ 不做语义层归一化（不改写算子顺序、不把 `1` 与 `true` 等同、不解析 BSON 字面量类型），仅吸收无意义的空白差。
- EM 不依赖 $\mathrm{Parse}$、$\mathrm{Exec}$，因此不会因为解析失败或执行失败而走到 $\bot$ 分支；但若预测产物本身是空串或非字符串，EM 直接判 0。

EM 的诊断意义：EM=1 时几乎确定其它五个指标全 1（仅 $\mathrm{norm\_str}$ 容许的空白差是噪声）；EM=0 时不能反推任何东西。

### 1.3 QSM（Query Stage Match） <a id="04-1-3"></a>

QSM 比较查询的 stage 算子序列。它衡量"查询的算子流水线骨架"是否一致。

$$
\operatorname{QSM}(x)\ =\ \mathbb{1}\!\left[\ \operatorname{Parse}\!\bigl(q_p(x)\bigr)\neq\bot\ \wedge\ \operatorname{Parse}\!\bigl(q_g(x)\bigr)\neq\bot\ \wedge\ \operatorname{stages}\!\bigl(\operatorname{Parse}(q_p)\bigr)\ =\ \operatorname{stages}\!\bigl(\operatorname{Parse}(q_g)\bigr)\,\right]
$$

性质：

- $\mathrm{stages}$ 只比较 stage 算子序列（如 `[$match, $group, $sort, $limit, $project]`）。同一 stage 内部的具体表达式（字段名、阈值、分组键）由 [§1.4](#04-1-4) 的 QFC 与 [§1.5](#04-1-5) 的 EX 覆盖。
- 序列比较是有序的：调换 `$sort` 与 `$limit` 顺序会被判 0，因为它们在 mongosh 里语义不同。
- $\mathrm{Parse}(q_p) = \bot$（语法错误）或 $\mathrm{Parse}(q_g) = \bot$（不应发生，因为 [03](./03_dataset_construction.md) 保证 gold 可解析）任一情形导致 QSM=0。

### 1.4 QFC（Query Field Coverage） <a id="04-1-4"></a>

QFC 比较查询触及的字段路径集合。它衡量"模型有没有看对字段"。

$$
\operatorname{QFC}(x)\ =\ \mathbb{1}\!\left[\ \operatorname{Parse}\!\bigl(q_p(x)\bigr)\neq\bot\ \wedge\ \operatorname{Parse}\!\bigl(q_g(x)\bigr)\neq\bot\ \wedge\ \operatorname{fields}\!\bigl(\operatorname{Parse}(q_p)\bigr)\ =\ \operatorname{fields}\!\bigl(\operatorname{Parse}(q_g)\bigr)\,\right]
$$

性质：

- $\mathrm{fields}$ 是**集合**，不是多重集合，也不计较出现位置：同一字段在 `$match` 与 `$project` 中各用一次只算一份。
- 字段路径以**完整嵌套路径**为单位（如 `orchestra.performance.Type` 而非 `Type`），与 schema 中的字段路径一一对应。
- 解析失败时 QFC=0；与 QSM 的失败处理对称。

### 1.5 EX（Execution Match） <a id="04-1-5"></a>

EX 直接实例化 [01 §3](./01_task_definition.md) 的物理执行锚。它是 TEND 的 **headline 指标**：

$$
\operatorname{EX}(x)\ =\ \mathbb{1}\!\left[\ \operatorname{NormExec}\!\bigl(q_p(x),\ D(x)\bigr)\ \equiv_{rec}\ \operatorname{NormExec}\!\bigl(q_g(x),\ D(x)\bigr)\ \right]
$$

约定：若 $\mathrm{NormExec}(q_p, D) = \bot$（解析失败 / 执行失败 / 超时 / 运行期错误任一情形），则 EX=0；该 record 仍计入分母（[§2.3](#04-2-3)）。

EX 是其余诊断指标的真值参照点：在 EX=1 的样本上，EFM 与 EVM 必同时为 1；在 EX=0 的样本上，EM/QSM/QFC/EFM/EVM 各自的取值能告诉我们错误的层级——

| EM | QSM | QFC | EFM | EVM | EX | 典型解读 |
|---|---|---|---|---|---|---|
| 1 | 1 | 1 | 1 | 1 | 1 | 完全正确 |
| 0 | 1 | 1 | 1 | 1 | 1 | 串面写法不同但语义等价 |
| 0 | 1 | 1 | 1 | 0 | 0 | 算子选错（如 `$sum` → `$avg`），骨架对、字段对、值错 |
| 0 | 1 | 1 | 0 | 0 | 0 | 投影字段名错（值与字段都错） |
| 0 | 0 | 1 | 1 | 0/1 | 0 | 流水线骨架错（如漏 `$limit`） |
| 0 | 1 | 0 | 0 | 0 | 0 | 字段对错位（看错列） |

> 该表只是定性诊断模板，不是指标定义；实际报告以 [§5](#04-5) 的主表为准。

### 1.6 EFM（Execution Field Match） <a id="04-1-6"></a>

EFM 在执行结果层比较字段名集合。它独立于值是否正确，只问"输出文档的字段名集合对不对"。

$$
\operatorname{EFM}(x)\ =\ \mathbb{1}\!\left[\ \operatorname{NormExec}(q_p, D)\neq\bot\ \wedge\ \operatorname{NormExec}(q_g, D)\neq\bot\ \wedge\ \operatorname{keys}\!\bigl(\operatorname{NormExec}(q_p, D)\bigr)\ =\ \operatorname{keys}\!\bigl(\operatorname{NormExec}(q_g, D)\bigr)\,\right]
$$

性质：

- $\mathrm{keys}$ 递归收集所有层级出现过的字段名，因此嵌套子文档里的字段也参与比较。
- EFM=1 不代表 EX=1：例如 `$sum` 误写为 `$avg`，字段名 `count` 仍存在，但值不同 → EFM=1 而 EX=0。
- $\mathrm{NormExec}$ 走 $\bot$ 分支时 EFM=0；该 record 仍计入分母。

### 1.7 EVM（Execution Value Match） <a id="04-1-7"></a>

EVM 在剥离字段名后比较值结构。它独立于字段名差异，只问"位置上的值序列对不对"。

$$
\operatorname{EVM}(x)\ =\ \mathbb{1}\!\left[\ \operatorname{NormExec}(q_p, D)\neq\bot\ \wedge\ \operatorname{NormExec}(q_g, D)\neq\bot\ \wedge\ \operatorname{drop\_keys}\!\bigl(\operatorname{NormExec}(q_p, D)\bigr)\ \equiv_{rec}\ \operatorname{drop\_keys}\!\bigl(\operatorname{NormExec}(q_g, D)\bigr)\,\right]
$$

性质：

- 由 [§1.1](#04-1-1) $\mathrm{drop\_keys}$ 的定义，字典按键字典序排序后取值序列，因此 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 在 $\mathrm{drop\_keys}$ 后等同；但 `{"a":1,"b":2}` 与 `{"a":2,"b":1}` 不等同。
- EVM=1 且 EFM=1 ⟹ EX=1（结构与值都匹配 ⟹ 整体规范树匹配）；这是诊断维度的内部一致性约束。
- EVM=1 但 EFM=0 表示"值对了但字段名错了"——例如把投影字段 `count` 重命名为 `total`，行数与值序列一致但字段名不同。

### 1.8 sample-mean 的总分聚合 <a id="04-1-8"></a>

设 test set 为 $T = \{x_1, \dots, x_N\}$，对任一指标 $M \in \{\text{EM}, \text{QSM}, \text{QFC}, \text{EX}, \text{EFM}, \text{EVM}\}$，其 test set 总分为：

$$
\overline{M}(T)\ =\ \frac{1}{N}\sum_{i=1}^{N} M(x_i)
$$

> 报告时 $\overline{M}$ 通常以百分比形式（保留两位小数）呈现；分母 $N$ 必须显式披露（[§6](#04-6)）。

### 1.9 EX 是 headline 指标 <a id="04-1-9"></a>

`EX` 在 6 指标中具有特殊地位：

- EX 直接实例化任务正确性锚（[01 §3](./01_task_definition.md)），其余 5 个都是该锚的代理或诊断切面。
- 论文与对外比较的主声明（"模型在 TEND 上的性能"）以 EX 为准；其余 5 个仅作为辅助诊断。
- 任何把 EX 与其它 5 个指标做加权平均、几何平均、F1 合成等"复合分数"的做法**不属于本规范**。

---

## §2 评测协议 <a id="04-2"></a>

### 2.1 数据范围 <a id="04-2-1"></a>

- 评测在且仅在 [02](./02_dataset_design.md) 定义的 `TEND/test.json`（2,775 条 record）上进行。
- 不允许在 train split（`TEND/train.json`，14,245 条）上跑指标作为 headline 数；train split 仅供模型训练 / 检索库构建。
- 不允许子采样、不允许按 db_id 抽样、不允许"剔除某些样本后再算"——若有失败，按 [§2.3](#04-2-3) 的规则计入分母。

### 2.2 单 record 的计算流程 <a id="04-2-2"></a>

对每条 record $x = (\mathit{record\_id}, \mathit{db\_id}, \mathit{nl\_queries}, \mathit{ref\_sql}, q_g)$ 与对应的预测 $q_p(x)$，按以下顺序执行：

1. **取数据快照** $D(x) = D(\mathit{db\_id})$；$D$ 必须从评测器自带的只读 mongosh 实例中加载（[§3.5](#04-3-5)）。
2. **计算 EM**：调用 $\mathrm{norm\_str}$ 后比较，不需要 $\mathrm{Parse}$。
3. **计算 QSM / QFC**：先 $\mathrm{Parse}(q_p)$；若 $\bot$ 则 QSM=0、QFC=0 并落盘"parse_error"；否则继续 $\mathrm{stages}$ / $\mathrm{fields}$ 的比较。$\mathrm{Parse}(q_g)$ 不应失败（[03](./03_dataset_construction.md) 已保证），若失败则该 record 在评测层标记为"gold_invalid"并写入诊断日志，但仍按 [§2.3](#04-2-3) 计入分母（任一指标记 0）。
4. **计算 EX / EFM / EVM**：调用 $\mathrm{NormExec}(q_p, D)$；
   - 若解析失败 → "parse_error"，三指标均记 0；
   - 若执行失败 → 按错误类型细分（"exec_error_validation" / "exec_error_runtime" / "exec_error_other"），三指标均记 0；
   - 若超过单 record 超时（默认 30 秒，与 `src/utils/metric.py` 的 `MetricConfig.timeout` 对齐）→ "timeout"，三指标均记 0；
   - 否则对 $\mathrm{NormExec}(q_p, D)$ 与 $\mathrm{NormExec}(q_g, D)$ 应用 [§1.5](#04-1-5)–[§1.7](#04-1-7) 的判定。
5. **逐 record 落盘**：把 6 指标取值、失败类型、$\mathrm{NormExec}$ 的输出大小（行数、字段数）、执行时长写入逐 record 诊断 JSON，形如：
   ```json
   {
     "record_id": ...,
     "db_id": ...,
     "EM": 0, "QSM": 1, "QFC": 1, "EX": 0, "EFM": 1, "EVM": 0,
     "pred_status": "ok" | "parse_error" | "exec_error_runtime" | "timeout" | ...,
     "pred_rows": 5, "gold_rows": 5,
     "elapsed_ms": 137
   }
   ```

> 这份逐 record 诊断 JSON 是 [§6](#04-6) 强制披露清单的输入；它不能被合成报告替代。

### 2.3 失败类型与计入规则 <a id="04-2-3"></a>

任一种失败都映射到统一的"该 record 在该指标上记 0"规则；失败的 record **始终保留在分母 $N$ 中**，不静默丢弃。失败类型有且只有以下五类：

| 类型 | 触发条件 | 影响指标 |
|---|---|---|
| `parse_error` | $\mathrm{Parse}(q_p) = \bot$（mongosh 解析抛出语法错误） | QSM、QFC、EX、EFM、EVM 均 0；EM 仍按串面比较 |
| `exec_error_validation` | mongosh 拒绝执行（如算子用法非法、字段类型与算子要求不符） | EX、EFM、EVM 均 0；QSM、QFC、EM 不受影响 |
| `exec_error_runtime` | mongosh 接受查询但运行时抛错（如 `$divide` 除零、`$convert` 类型不可转） | EX、EFM、EVM 均 0；QSM、QFC、EM 不受影响 |
| `timeout` | 单 record 执行超过 30 秒上限 | EX、EFM、EVM 均 0；QSM、QFC、EM 不受影响 |
| `gold_invalid` | $q_g$ 自身解析或执行失败（不应发生，[03](./03_dataset_construction.md) 已过滤；若发生，整 record 标记并人工介入） | 6 指标均 0；该 record 列入诊断报告的"gold_invalid 列表"显式披露 |

> 所有失败类型都必须出现在 [§6](#04-6) 的强制披露清单里，按计数披露而不是只披露总分。

### 2.4 比较关系 <a id="04-2-4"></a>

- 标量、字典、列表三种构件的递归相等关系 $\equiv_{rec}$ 由 [01 §5](./01_task_definition.md) 定义，本文不重定义、不修改。
- 列表是否按位置比较或排序后比较的判定（[01 §5.3](./01_task_definition.md)）由 gold 来源标明的"是否无序集合"决定；评测器读 gold 时直接使用该标注，不在评测层重新推断。
- 标量在 BSON 类型层的归一化（如 `Decimal128` 全精度字符串、`Date` 转 ISO-8601 UTC）由 [01 §4](./01_task_definition.md) 定义，本文不重写。

### 2.5 不允许的偏离 <a id="04-2-5"></a>

为保证 6 指标在跨论文、跨方法之间的可比性，以下偏离明确禁止：

1. **不允许重新定义指标**：方法文档 [05](./05_solution_design.md) 不得在 6 指标外引入新的"主指标"，也不得把 EX 拆成多个分量上报。
2. **不允许改判定**：不得把"EX≈gold（容忍 1 行差）"或"EX≥0.9（按行匹配率折算）"当作 EX；EX 是布尔。
3. **不允许跳样本**：不得对失败样本"丢弃"或"用平均代替"；任一种失败按 [§2.3](#04-2-3) 计 0。
4. **不允许两次执行后取最优**：单 record 单次执行；评测层不允许多次采样后选最好。
5. **不允许在评测期间写库**：[§3.5](#04-3-5)。

---

## §3 复现性契约（输入锁 + 运行时锁） <a id="04-3"></a>

任何一份 TEND 评测报告必须附带一份 **manifest 摘要锁**。manifest 由两部分构成：输入锁（[§3.1](#04-3-1)）与运行时锁（[§3.2](#04-3-2)）。所有摘要均使用 SHA-256，以小写 hex 形式记录。manifest 的总体形态见 [§3.3](#04-3-3)；摘要不一致时的中止规则见 [§3.4](#04-3-4)；评测过程中 $D$ 的只读不变式见 [§3.5](#04-3-5)。

### 3.1 输入锁 <a id="04-3-1"></a>

输入锁锁住"评测器看到的所有数据"。每个条目都是文件级 SHA-256：

| 条目 | 路径模式 | 说明 |
|---|---|---|
| 预测文件 | `predictions/<run_id>.json` | 模型对 test set 的输出，schema 由 [02](./02_dataset_design.md) 给出（至少包含 `record_id` 与 `prediction`） |
| test set | `TEND/test.json` | 2,775 条 test record |
| schema 集合 | `TEND/mongodb_schema/<db_id>.json` | test set 中出现的每个 `db_id` 对应的 schema 文件，逐文件 hash |
| 数据快照集合 | `TEND/mongodb_data/<db_id>.json` | test set 中出现的每个 `db_id` 对应的数据文件，逐文件 hash |

> 预测文件不允许在评测期间被替换；评测器在加载时立即计算并锁定其 hash。

### 3.2 运行时锁 <a id="04-3-2"></a>

运行时锁锁住"评测器自身与执行环境"：

| 条目 | 取证方式 | 说明 |
|---|---|---|
| mongosh 镜像 | docker image digest（`sha256:...`） | 与 [03 §执行环境](./03_dataset_construction.md) 中构造侧使用的镜像保持版本一致；评测层与构造层共用同一镜像哈希以排除"gold 在构造时能跑、评测时跑不通"的环境偏差 |
| 评测器代码 | 对 `src/utils/metric.py` 取文件级 SHA-256 | 涵盖 6 指标实现、失败处理、超时控制 |
| 解析器代码 | 对 `src/utils/extract_stages.py`、`src/utils/extract_fields.py`、`src/utils/mongosh_exec.py` 各取文件级 SHA-256 | 涵盖 $\mathrm{stages}$、$\mathrm{fields}$ 与 $\mathrm{Parse}$/$\mathrm{Exec}$ 的代理实现 |
| collation / locale | 显式记录 `{ locale: "en", strength: 3 }` | 控制 `$sort` 与字符串比较行为；缺省值不允许"由系统决定" |
| timezone | 显式记录 `UTC` | 影响 `$dateToString` 等日期算子的输出 |
| 单 record 超时 | 显式记录数值（默认 30 秒） | 与 `src/utils/metric.py` 的 `MetricConfig.timeout` 一致 |

### 3.3 manifest 形态 <a id="04-3-3"></a>

manifest 是一个 JSON 文件，与评测报告一同发布，形如：

```json
{
  "benchmark": "TEND",
  "run_id": "<对外标识符>",
  "input_lock": {
    "predictions_sha256": "<hex>",
    "test_json_sha256": "<hex>",
    "schema_sha256": {
      "orchestra": "<hex>",
      "school_bus": "<hex>",
      "...": "..."
    },
    "data_sha256": {
      "orchestra": "<hex>",
      "school_bus": "<hex>",
      "...": "..."
    }
  },
  "runtime_lock": {
    "mongosh_image_digest": "sha256:<hex>",
    "metric_py_sha256": "<hex>",
    "extract_stages_py_sha256": "<hex>",
    "extract_fields_py_sha256": "<hex>",
    "mongosh_exec_py_sha256": "<hex>",
    "collation": { "locale": "en", "strength": 3 },
    "timezone": "UTC",
    "per_record_timeout_seconds": 30
  }
}
```

> 该 manifest 是评测报告的"指纹"。同样的指纹意味着指标可以被严格复现；指纹任一字段不同，意味着报告不可比，必须重新跑。

### 3.4 摘要不一致的中止规则 <a id="04-3-4"></a>

评测器在启动时执行三件检查：

1. 计算输入锁所列每个文件的 SHA-256。
2. 计算运行时锁所列代码文件与镜像 digest。
3. 把上述结果与 manifest 中预声明的值逐字段比对。

任一字段不一致 ⟹ 评测器**中止运行**，不允许"用现场结果覆盖 manifest"或"先跑出数再补 manifest"。这一规则是为了防止"环境漂移后报告依旧声称指标"的悄无声息退化。

### 3.5 评测的只读不变式 <a id="04-3-5"></a>

- $D$ 在评测期间是**只读快照**：评测器只接受读账户连接、不持有写权限。
- 评测器不允许使用 [01 §2.2](./01_task_definition.md) 列出的不在任务输出空间内的算子（`$out`、`$merge`、`$function` 等）；若 $q_p$ 含这些算子，按 `exec_error_validation` 处理。
- 评测期间不允许 import 数据、不允许重建索引、不允许做任何 schema migration——这些都是构造侧的事，应在 [03](./03_dataset_construction.md) 完成。
- `null` 与 missing 在评测层严格区分（[01 §4.3](./01_task_definition.md)），任何把 missing 默认补 `null` 的"便利化"行为均破坏复现性。

---

## §4 结果归一化（引用 [01](./01_task_definition.md) 的契约） <a id="04-4"></a>

### 4.1 引用关系 <a id="04-4-1"></a>

本文不重新定义结果归一化。所有结果归一化规则——BSON 标量类型规范化、复合结构规范化、`null` 与 missing 的区分、`_id` 处理——都由 [01 §4](./01_task_definition.md) 唯一定义。本文档中所有出现的 $\mathrm{NormExec}$ 都隐式调用该归一化算子。

### 4.2 null 与 missing 的强调 <a id="04-4-2"></a>

由于这是评测层最常见的"看似差不多但被判 0"的来源，特此强调（不重定义，仅复述以醒目）：

- `{"a": null}` 与 `{}` 在 [01 §4.3](./01_task_definition.md) 的归一化下**仍然不同**；
- 因此 EFM 在两者上判 0（键集合 `{a}` ≠ `∅`），EX 同样判 0；
- $\mathrm{drop\_keys}$ 在两者上也产生不同序列（`[null]` vs `[]`），故 EVM 亦判 0；
- 这一判定不可在评测层"放宽"为 missing 等价 null。

### 4.3 顺序敏感性 <a id="04-4-3"></a>

- 默认情形下，列表（含顶层结果列表）按位置比较——见 [01 §5.3](./01_task_definition.md)。
- 仅当 gold 来源标明为"无序集合"（典型情况：`find` 且 NLQ 不含排序意图，或聚合管道最外层不含 `$sort`）时，评测器先对 $u$ 与 $v$ 各自按 [01 §5.3](./01_task_definition.md) 的规范全序排序，再按位置比较。
- 评测层**不**自行判断"NLQ 是否暗示排序意图"；该判定来自 [02](./02_dataset_design.md) 中 record 的 gold 标注。

---

## §5 报告结构 <a id="04-5"></a>

TEND 的对外报告由三部分构成：主表（[§5.1](#04-5-1)）、cross-domain 切片表（[§5.2](#04-5-2)）、可选辅助切片（[§5.3](#04-5-3)）。任何超出这三部分的报告形态需在论文里显式声明并提供原始的逐 record 诊断 JSON 供复算。

### 5.1 主表（test set 全集） <a id="04-5-1"></a>

主表覆盖 test set 全集（$N = 2{,}775$，与 [02](./02_dataset_design.md) 的 cross-domain 8:2 切分一致）。每个数字是该指标的 sample-mean。

| 指标 | test set 全集 |
|---|---|
| 分母 $N$ | 2,775（或当前 test set 实际样本数） |
| EM | $\overline{\mathrm{EM}}(T)$ |
| QSM | $\overline{\mathrm{QSM}}(T)$ |
| QFC | $\overline{\mathrm{QFC}}(T)$ |
| **EX** | $\overline{\mathrm{EX}}(T)$ ← headline |
| EFM | $\overline{\mathrm{EFM}}(T)$ |
| EVM | $\overline{\mathrm{EVM}}(T)$ |

> 报告必须以"分母 $N$"作为第一行；缺失分母的报告视为不可比。

### 5.2 cross-domain 切片表 <a id="04-5-2"></a>

cross-domain 切片把 test set 按 `db_id`（或更粗的 domain，由 [02](./02_dataset_design.md) 给出）分组，每组报告 6 指标。该切片用于检查"模型是否在某些 domain 上系统性失败"。

| db_id / domain | $N_g$ | EM | QSM | QFC | EX | EFM | EVM |
|---|---|---|---|---|---|---|---|
| `orchestra` | $N_{\mathrm{orchestra}}$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ |
| `school_bus` | $N_{\mathrm{school\_bus}}$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ |
| $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ | $\dots$ |

要求：

- $\sum_g N_g = N$（每个 record 恰被分到一组）。
- 切片不允许"只展示 top-K db_id"；要么列全，要么把剩余打包为 `others` 单独一行并显式 $N$。
- 切片以 db_id 为最细粒度；不允许进一步按某个 record 内部的 audit 字段做主切片（那属于 [§5.3](#04-5-3) 的辅助切片）。

### 5.3 可选辅助切片 <a id="04-5-3"></a>

允许提供以下辅助切片，**仅作诊断用**，不进入 headline 主张：

1. **按 ref_sql 复杂度切片**：以 `ref_sql` 中 `JOIN` 数、`GROUP BY` 是否出现、是否含子查询等可机器抽取的特征分组。
2. **按 MQL pipeline 长度切片**：以 gold MQL 的 stage 数（aggregation pipeline 长度，find 视为 1）分桶。
3. **按 schema_complexity_profile 切片**：以 [02](./02_dataset_design.md) 的 audit 字段（如嵌套深度、collection 数量等）分桶。

要求：

- 任何辅助切片都必须在 [§6](#04-6) 强制披露清单中显式声明使用了 audit 字段，并附 audit 字段名称。
- 辅助切片的 6 指标值 **不**可写在主表里冒充全集 headline。
- 辅助切片不引入新指标。

### 5.4 不允许的报告形式 <a id="04-5-4"></a>

为防止把 6 指标"装饰化"为更多变体，以下报告形式禁止：

- 把同一 test set 拆为"主集 / 扩展集"双列上报。
- 引入 5 档难度（如 L1–L5）或 17 档算子特征作为主表的列轴。
- 报告 family-level / 模板-level 的指标（即把多个表面写法不同但语义相同的预测合并后再算指标）。
- 以"sidecar 报告"形式上报第二组指标（如某种"鲁棒性版 EX"）：本文档只承认 6 指标。

---

## §6 强制披露清单 <a id="04-6"></a>

每次对外发布 TEND 评测结果，至少披露以下条目；缺一不可。披露顺序建议与下表一致，便于横向对照：

| 编号 | 条目 | 形式 |
|---|---|---|
| D-1 | test set 总样本数 $N$ | 整数 |
| D-2 | 每个指标的分母 | 6 个整数；正常情况应等于 $N$ |
| D-3 | 每个指标的解析失败计数（`parse_error`） | 整数 |
| D-4 | 每个指标的执行失败计数（按 `exec_error_validation` / `exec_error_runtime` / `exec_error_other` 分类） | 三个整数 |
| D-5 | 每个指标的超时计数（`timeout`） | 整数 |
| D-6 | `gold_invalid` 列表（不应非空；若非空必须列出 `record_id`） | record_id 数组 |
| D-7 | 6 指标的总分 $\overline{M}(T)$ | 6 个百分数 |
| D-8 | cross-domain 切片表 | [§5.2](#04-5-2) 形态 |
| D-9 | 复现性 manifest 摘要 | [§3.3](#04-3-3) 的 manifest JSON 内容 |
| D-10 | 是否使用了 audit 字段做辅助切片 | 布尔；若 true，列出使用的 audit 字段名 |

> D-2 至 D-6 是常被忽略但极易导致误读的条目：例如 `EX = 65.08%` 若不附 `parse_error` 与 `timeout` 计数，读者无法判断"模型究竟错在生成质量还是错在工程稳定性"。

---

## §7 canonical 示例 <a id="04-7"></a>

本节用 [01 §7](./01_task_definition.md) 约定的 canonical 示例（`db_id = orchestra`，canonical NLQ = *"List the top 3 conductors with the most performances."*）演示三种典型预测下 6 指标的取值过程。

### 7.1 NLQ 与 gold MQL <a id="04-7-1"></a>

- `db_id`：`orchestra`
- NLQ：`"List the top 3 conductors with the most performances."`
- gold MQL（来自 [02](./02_dataset_design.md) record `record_id = 99001` 的 `MQL` 字段）：

```javascript
db.conductor.aggregate([
  { $unwind: "$orchestra" },
  { $unwind: "$orchestra.performance" },
  { $group: {
      _id: "$_id",
      Name: { $first: "$Name" },
      count: { $sum: 1 }
  }},
  { $sort: { count: -1 } },
  { $limit: 3 },
  { $project: { _id: 0, Name: 1, count: 1 } }
]);
```

记 gold 在 $D$ 上的执行结果（已归一化）为：

```
[
  { "Name": "Antal Doráti", "count": 7 },
  { "Name": "Igor Stravinsky", "count": 5 },
  { "Name": "Colin Davis", "count": 4 }
]
```

> 上述具体 `Name` 与 `count` 的字面值仅用于本节演示的语义直觉；它们由 [02](./02_dataset_design.md) 的实际 `TEND/mongodb_data/orchestra.json` 决定，本节不规范化具体数值。

### 7.2 示例 A：完全正确 <a id="04-7-2"></a>

预测 $q_p^{(A)}$ 与 gold 完全相同（甚至连空白也一致）：

```javascript
db.conductor.aggregate([
  { $unwind: "$orchestra" },
  { $unwind: "$orchestra.performance" },
  { $group: {
      _id: "$_id",
      Name: { $first: "$Name" },
      count: { $sum: 1 }
  }},
  { $sort: { count: -1 } },
  { $limit: 3 },
  { $project: { _id: 0, Name: 1, count: 1 } }
]);
```

逐指标推算：

| 指标 | 计算 | 取值 |
|---|---|---|
| EM | $\mathrm{norm\_str}(q_p^{(A)}) = \mathrm{norm\_str}(q_g)$ | 1 |
| QSM | $\mathrm{stages} = [\$unwind, \$unwind, \$group, \$sort, \$limit, \$project]$ 与 gold 相同 | 1 |
| QFC | $\mathrm{fields} = \{\text{orchestra}, \text{orchestra.performance}, \text{Name}, \_id, \text{count}\}$ 与 gold 相同 | 1 |
| EX | $\mathrm{NormExec}(q_p^{(A)}, D) \equiv_{rec} \mathrm{NormExec}(q_g, D)$ | 1 |
| EFM | $\mathrm{keys} = \{\text{Name}, \text{count}\}$ 双方相同 | 1 |
| EVM | $\mathrm{drop\_keys}$ 后值序列双方相同 | 1 |

整体：6 指标均为 1；该 record 落在 [§1.5](#04-1-5) 表的"完全正确"行。

### 7.3 示例 B：`$sum` 误写为 `$avg` <a id="04-7-3"></a>

预测 $q_p^{(B)}$ 把 `count: { $sum: 1 }` 改成 `count: { $avg: 1 }`，其余完全相同：

```javascript
db.conductor.aggregate([
  { $unwind: "$orchestra" },
  { $unwind: "$orchestra.performance" },
  { $group: {
      _id: "$_id",
      Name: { $first: "$Name" },
      count: { $avg: 1 }
  }},
  { $sort: { count: -1 } },
  { $limit: 3 },
  { $project: { _id: 0, Name: 1, count: 1 } }
]);
```

执行后 `count` 全部退化为常数 `1`（每个 conductor 的平均值都是 1）。归一化结果形如：

```
[
  { "Name": "<某个 conductor>", "count": 1 },
  { "Name": "<某个 conductor>", "count": 1 },
  { "Name": "<某个 conductor>", "count": 1 }
]
```

> 此时 `$sort: { count: -1 }` 在常数键上排序，按 mongosh 实际行为顺序由 `_id` 决定，输出的 conductor 顺序很可能与 gold 不同；即便顺序碰巧相同，`count` 的数值（1 vs 真实计数）也已不同。

逐指标推算：

| 指标 | 计算 | 取值 |
|---|---|---|
| EM | 串面比较：`$sum` ≠ `$avg` | 0 |
| QSM | $\mathrm{stages}$ 仍为 $[\$unwind, \$unwind, \$group, \$sort, \$limit, \$project]$ | 1 |
| QFC | $\mathrm{fields}$ 仍为 $\{\text{orchestra}, \text{orchestra.performance}, \text{Name}, \_id, \text{count}\}$ | 1 |
| EX | $\mathrm{NormExec}(q_p^{(B)}, D) \not\equiv_{rec} \mathrm{NormExec}(q_g, D)$（值不同，可能顺序也不同） | 0 |
| EFM | $\mathrm{keys}(\mathrm{NormExec}(q_p^{(B)}, D)) = \{\text{Name}, \text{count}\}$ 与 gold 相同 | 1 |
| EVM | $\mathrm{drop\_keys}$ 后 $q_p^{(B)}$ 的值序列形如 `[(<name>, 1), (<name>, 1), (<name>, 1)]`，gold 形如 `[(<name>, 7), (<name>, 5), (<name>, 4)]`，按位置（或排序后）比较均不等 | 0 |

整体：`(EM, QSM, QFC, EX, EFM, EVM) = (0, 1, 1, 0, 1, 0)`。诊断意义：骨架与字段全对，错在算子（值面错）。

### 7.4 示例 C：漏 `$limit 3` <a id="04-7-4"></a>

预测 $q_p^{(C)}$ 完全照抄 gold，但删去最后那个 `$limit: 3` 之外的什么都不动——为对照清晰，假设它把整个 `{ $limit: 3 }` 这一阶段移除：

```javascript
db.conductor.aggregate([
  { $unwind: "$orchestra" },
  { $unwind: "$orchestra.performance" },
  { $group: {
      _id: "$_id",
      Name: { $first: "$Name" },
      count: { $sum: 1 }
  }},
  { $sort: { count: -1 } },
  { $project: { _id: 0, Name: 1, count: 1 } }
]);
```

执行后归一化结果包含**全部** conductor（按 count 降序），形如：

```
[
  { "Name": "Antal Doráti", "count": 7 },
  { "Name": "Igor Stravinsky", "count": 5 },
  { "Name": "Colin Davis", "count": 4 },
  { "Name": "Charles Mackerras", "count": 3 },
  ...
]
```

逐指标推算：

| 指标 | 计算 | 取值 |
|---|---|---|
| EM | 串面比较：少了 `{ $limit: 3 }` 子串 | 0 |
| QSM | $\mathrm{stages}(q_p^{(C)}) = [\$unwind, \$unwind, \$group, \$sort, \$project]$ 比 gold 少一项 $\$limit$ | 0 |
| QFC | $\mathrm{fields}$ 与 gold 一致（`$limit` 不引入字段） | 1 |
| EX | $\mathrm{NormExec}(q_p^{(C)}, D)$ 长度 = $\#\text{conductors}$，gold 长度 = 3 | 0 |
| EFM | 字段名集合仍为 $\{\text{Name}, \text{count}\}$ | 1 |
| EVM | 顶层列表长度不同 ⟹ 按 [01 §5.3](./01_task_definition.md) 长度判等先于元素判等，直接判 0；唯一的退化情形是 $D$ 中 conductor 数恰为 3，此时长度相同且值序列相同 → EVM=1（但由 [01 §6.1](./01_task_definition.md) 的 P4，TEND 上 conductor 数严格大于 3，因此该退化不发生） | 0 |

整体：`(EM, QSM, QFC, EX, EFM, EVM) = (0, 0, 1, 0, 1, 0)`。诊断意义：错在骨架（漏 stage）；字段层与字段名层都没问题。

### 7.5 示例小结 <a id="04-7-5"></a>

三个示例共同说明：

- EX 是单一的"成功 / 失败"标尺：示例 B、C 都判 0，但 6 指标的指纹不同——B 是值面错（`(0,1,1,0,1,0)`），C 是骨架错（`(0,0,1,0,1,0)`）。
- 6 指标的组合相当于一个"错误层级 6 比特指纹"，它能唯一区分串面错 / 结构错 / 字段错 / 算子值错的多数典型情形——这正是把它们一起报告（而不是只报 EX）的诊断价值。
- 任一种失败都不要在评测层"修复"——示例 B 的 `$avg` 是合法 mongosh 算子，因此 EX=0 与 EFM=1 的指纹应原样落盘，不允许评测器"看到 `$avg` 觉得不合理就改写成 `$sum`"。

---

## §8 与方法文档的接口 <a id="04-8"></a>

[05 方法设计](./05_solution_design.md) 在其评测相关章节只能消费本文档定义的：

1. 6 指标公式（[§1](#04-1)）；
2. 评测协议（[§2](#04-2)）；
3. 复现性契约（[§3](#04-3)）；
4. 报告结构（[§5](#04-5)）。

明确禁止 [05](./05_solution_design.md) 做的事：

- 不得引入第 7、第 8 个指标作为主指标；
- 不得修改 [§2.3](#04-2-3) 的失败处理规则（如"timeout 重试一次后再算"）；
- 不得改写 [§3](#04-3) 的 manifest 字段（如省略 `collation` / `timezone`）；
- 不得用 [§5.3](#04-5-3) 的辅助切片代替 [§5.1](#04-5-1) 的主表 headline。

[05](./05_solution_design.md) 可以——也鼓励——在方法叙述中引用 6 指标的诊断意义（[§1.5](#04-1-5) 的诊断模板表），用以解释方法的失败模式与改进路径，但必须注明引用 [§1.5](#04-1-5)。

> 评测层不引用 [05](./05_solution_design.md)：一次评测对哪种方法运行的细节属于方法层；评测层只承诺"给定 $(q_p, q_g, D)$，返回 6 指标 + 失败标签"。

---

## §9 全文符号表 <a id="04-9"></a>

| 符号 | 含义 | 首次出现 |
|---|---|---|
| $T = \{x_1, \dots, x_N\}$ | test set 全集 | [§1.8](#04-1-8) |
| $N$ | test set 总样本数（与 [02](./02_dataset_design.md) 的 cross-domain 切分一致） | [§1.8](#04-1-8) |
| $q_p(x), q_g(x)$ | record $x$ 的预测 MQL 与 gold MQL | [§1.1](#04-1-1) |
| $D(x)$ | record $x$ 的只读数据快照 | [§1.1](#04-1-1) |
| $\mathrm{Parse}, \mathrm{Exec}, \mathrm{Norm}, \mathrm{NormExec}$ | 解析 / 执行 / 归一化 / 复合算子（[01 §1.4](./01_task_definition.md)） | [§1.1](#04-1-1) |
| $\equiv_{rec}$ | 规范树上的递归相等关系（[01 §5](./01_task_definition.md)） | [§1.1](#04-1-1) |
| $\bot$ | 解析失败 / 执行失败 / 超时 / 运行期错误的统一占位符 | [§1.1](#04-1-1) |
| $\mathrm{norm\_str}(q)$ | 串面归一化（去除两端空白、合并连续空白） | [§1.1](#04-1-1) |
| $\mathrm{stages}(\cdot)$ | aggregation pipeline stage 算子序列 | [§1.1](#04-1-1) |
| $\mathrm{fields}(\cdot)$ | 查询触及的字段路径集合 | [§1.1](#04-1-1) |
| $\mathrm{keys}(r)$ | 规范结果树中所有出现过的字段名集合 | [§1.1](#04-1-1) |
| $\mathrm{drop\_keys}(r)$ | 规范结果树剥离字段名后的值结构 | [§1.1](#04-1-1) |
| $\mathrm{EM}, \mathrm{QSM}, \mathrm{QFC}, \mathrm{EX}, \mathrm{EFM}, \mathrm{EVM}$ | 6 个标量指标 | [§1.2](#04-1-2)–[§1.7](#04-1-7) |
| $\overline{M}(T)$ | 指标 $M$ 在 $T$ 上的 sample-mean | [§1.8](#04-1-8) |
| `parse_error`, `exec_error_validation`, `exec_error_runtime`, `exec_error_other`, `timeout`, `gold_invalid` | 失败类型枚举 | [§2.3](#04-2-3) |
| `input_lock`, `runtime_lock` | 复现性 manifest 的两半 | [§3.1](#04-3-1)–[§3.2](#04-3-2) |
| D-1 … D-10 | 强制披露清单条目 | [§6](#04-6) |

---

> 下游文档定位：record 字段、训练 / 测试切分与 audit 字段定义在 [02 数据集设计](./02_dataset_design.md)；gold 来源、构造侧执行环境与 mongosh 镜像约定在 [03 数据集构造](./03_dataset_construction.md)；任务签名、归一化契约、$\equiv_{rec}$ 在 [01 任务定义](./01_task_definition.md)；面向 6 指标进行优化的方法架构在 [05 方法设计](./05_solution_design.md)。
