# 01 · 任务定义（Task Semantics）

> 本文件是 TEND benchmark 任务语义层的根定义（SSoT）。它只回答"这个任务是什么、它的输出空间允许什么、它的正确性如何被锚定、结果如何被归一化、什么样的样本才有资格作为 gold"，不涉及 record schema、构造流程、评测指标公式或方法架构。下游文档（[02 数据集设计](./02_dataset_design.md) / [03 数据集构造](./03_dataset_construction.md) / [04 评测方法](./04_evaluation_methodology.md) / [05 方法设计](./05_solution_design.md)）必须把本文档作为唯一的语义参照点。

---

## §0 摘要 <a id="01-0"></a>

TEND 是一个 Text-to-NoSQL benchmark：给定一条自然语言查询（NLQ）和它所属数据库的 MongoDB schema 上下文，要求模型生成一条**可执行的 MongoDB 查询**（`find` 或 aggregation pipeline）。整体规模为 154 个数据库 / 105 个领域 / 347 个 collection / 17,020 对 `(NLQ, NoSQL)`，按 cross-domain 比例 8:2 切分（train 14,245 / test 2,775）。

本文档定义五件事：

1. 任务的形式化函数签名（[§1](#01-1)）。
2. 输出空间允许什么、不允许什么（[§2](#01-2)）。
3. 正确性的物理执行锚——这是任务正确与否的**唯一**判定（[§3](#01-3)）。
4. 结果归一化契约——把 BSON 结果转换为可比较的规范形式（[§4](#01-4)）。
5. 递归相等关系 $\equiv_{rec}$ 的形式化定义（[§5](#01-5)）。

之后追加两个支撑性章节：Instance 正确性的根原则（[§6](#01-6)）与 canonical 示例的语义解读（[§7](#01-7)）；最后给出全文符号表（[§8](#01-8)）。

---

## §1 任务的形式化 <a id="01-1"></a>

任务是一个把"自然语言 + 模式上下文 + 库标识符"映射到"MongoDB 查询"的函数：

$$
f:\ (\mathrm{NLQ},\ S,\ \mathit{db\_id})\ \longrightarrow\ q^{\mathrm{MQL}}
$$

### 1.1 输入空间

| 符号 | 含义 | 性质 |
|---|---|---|
| $\mathrm{NLQ}$ | 一条自然语言查询，例如 *"List the top 3 conductors with the most performances."* | 字符串 |
| $S$ | 该数据库对应的 MongoDB schema 上下文（collection 列表、字段名、字段 BSON 类型、嵌套子文档结构） | 结构化对象 |
| $\mathit{db\_id}$ | 数据库标识符（如 `orchestra`） | 字符串 |

### 1.2 输出空间

| 符号 | 含义 | 性质 |
|---|---|---|
| $q^{\mathrm{MQL}}$ | 一条 mongosh 可执行的 MongoDB 查询；形式上可以是 `db.<col>.find(...)` 或 `db.<col>.aggregate([...])` | 字符串 |

### 1.3 数据快照 $D$

数据快照 $D$ 是由 $\mathit{db\_id}$ **唯一绑定**的只读 MongoDB 实例状态：

$$
D \equiv D(\mathit{db\_id})
$$

- $D$ 在评测期间不被任何样本写入或修改。
- $D$ 的内容由 $\mathit{db\_id}$ 完全决定；不存在与 $\mathit{db\_id}$ 同名但内容不同的两个快照。
- $S$ 是 $D$ 在结构层面的描述；$D$ 是 $S$ 的具体实例化（具体文档集合）。

### 1.4 解析与执行的复合算子

为后续公式表达，引入两个原子算子：

| 算子 | 签名 | 含义 |
|---|---|---|
| $\mathrm{Parse}$ | $q^{\mathrm{MQL}} \to \mathrm{AST}$ | 由标准 mongosh 把查询字符串解析为抽象语法树 |
| $\mathrm{Exec}$ | $(\mathrm{AST},\ D) \to r$ | 在快照 $D$ 上执行 AST，返回原始 BSON 结果（文档列表） |

并定义结果归一化算子 $\mathrm{Norm}$（详见 [§4](#01-4)）。三者复合后得到任务正确性使用的执行算子：

$$
\mathrm{NormExec}(q,\ D)\ \triangleq\ \mathrm{Norm}\!\bigl(\mathrm{Exec}\bigl(\mathrm{Parse}(q),\ D\bigr)\bigr)
$$

---

## §2 输出空间约束 <a id="01-2"></a>

属于任务输出空间 $q^{\mathrm{MQL}}$ 的查询必须同时满足三条性质。

### 2.1 三条核心性质

1. **read-only**：查询不写入数据库、不导出数据、不依赖任何运行时副作用。`NormExec` 在执行前后必须保证 $D$ 完全不变。
2. **deterministic**：在固定快照 $D$ 上对同一查询重复调用 $\mathrm{NormExec}$，必须产生**完全相同**的归一化结果。
3. **mongosh-executable**：查询必须能被标准 mongosh 解析（$\mathrm{Parse}$ 不抛出语法错误）并在 $D$ 上执行（$\mathrm{Exec}$ 不抛出运行时错误）。

### 2.2 不允许使用的算子

下列算子破坏上述性质，**不在任务输出空间内**：

| 算子 | 破坏的性质 | 说明 |
|---|---|---|
| `$sample` | deterministic | 随机抽样，结果不可重复 |
| `$rand` | deterministic | 随机数，结果不可重复 |
| `$$NOW` | deterministic | 依赖系统当前时间，与执行时刻耦合 |
| `$out` | read-only | 把聚合结果写入新 collection |
| `$merge` | read-only | 把聚合结果合并到目标 collection |
| `$function` | deterministic + read-only | 允许任意 JavaScript，副作用与不确定性都无法约束 |

> **范围澄清**：本节只是声明这些算子**不在任务输出空间里**——也就是说，使用这些算子的查询不构成本任务的合法 $q^{\mathrm{MQL}}$。本节不规定模型生成时遇到这些算子如何处理，也不规定它们在数据组织上属于哪一类资产。

---

## §3 正确性锚点 <a id="01-3"></a>

模型对一条样本的预测 $q_p$ 在该样本的快照 $D$ 上"正确"当且仅当：

$$
\mathrm{NormExec}(q_p,\ D)\ \equiv_{rec}\ \mathrm{NormExec}(q_g,\ D)
$$

其中 $q_g$ 是该样本的 gold MQL，$\equiv_{rec}$ 是 [§5](#01-5) 定义的递归相等关系。

这是 TEND 任务的**唯一物理执行锚**。任务层不引入第二个独立判定：

- 不存在"另有一个符号语义判定"也能判正确。
- 不存在"先满足某个语义条件再满足执行条件"的两段式锚。
- 任何派生指标（包括 [04 评测方法](./04_evaluation_methodology.md) 中的查询级或执行级指标）要么直接实例化本式（如 EX 把上式作为成功条件），要么是它的代理近似（如 EM/QSM/QFC 在 $q$ 串层面的近似比较）；它们都不取代本式作为根判定。

---

## §4 结果归一化契约（BSON 类型规范化规则） <a id="01-4"></a>

$\mathrm{Norm}$ 把 $\mathrm{Exec}$ 返回的原始 BSON 结果（一棵以"文档列表"为根的嵌套结构）转换为标量、字典、列表三种构件组成的规范树。本节是任务层关于结果可比性的最权威规则；[02](./02_dataset_design.md) / [03](./03_dataset_construction.md) / [04](./04_evaluation_methodology.md) 涉及结果比较时必须引用本节。

### 4.1 标量类型规范化

| BSON 类型 | 规范化形式 | 示例 |
|---|---|---|
| `ObjectId` | 24 位小写 hex 字符串 | `507f1f77bcf86cd799439011` |
| `Date` | ISO-8601 UTC 字符串（带 `Z`） | `2024-03-15T08:30:00Z` |
| `Decimal128` | 保留全精度的字符串（不转 float） | `"3.141592653589793238"` |
| `Long` (`Int64`) | 若值在 JS 安全整数范围内（$\|n\| \le 2^{53}-1$）→ Python `int`；否则 → 数字字符串 | `1234567890`, `"9223372036854775807"` |
| `Binary` | base64 字符串 | `"SGVsbG8="` |
| `Regex` | `/pattern/flags` 字符串 | `/^abc$/i` |
| `NaN` | 显式 token 字符串 `"NaN"` | `"NaN"` |
| `Infinity` / `-Infinity` | 显式 token 字符串 `"Infinity"` / `"-Infinity"` | `"Infinity"` |
| 其它原生标量（`int`, `double`, `bool`, `string`） | 保持原值 | `42`, `3.14`, `true`, `"foo"` |

### 4.2 复合结构规范化

- **文档（document）** 规范化为字典：键集合保持，值递归规范化。键的字典序在比较时由 [§5](#01-5) 处理。
- **数组（array）** 规范化为列表：元素位置保持，元素递归规范化；是否按位置比较或排序后比较由 [§5](#01-5) 决定。

### 4.3 `null` 与 missing 的严格区分

这是归一化中最容易出错、必须显式声明的一条规则：

| 情形 | 规范化结果 |
|---|---|
| 字段存在且值为 `null` | 字段存在，值 = `null` |
| 字段不存在（missing） | 字段不存在；**不补 `null`、不补默认值、不补空字符串** |

也就是说，`{"a": null}` 与 `{}` 在归一化后**仍然不同**。这一条对 `$project`、`$lookup`、`$unwind` 等会引入"可能缺失字段"的算子尤其重要。

### 4.4 `_id` 的处理

`_id` 是 MongoDB 默认主键。归一化本身不擅自删除或注入 `_id`：

- 若 $q_g$ 的输出文档包含 `_id`，则 $q_p$ 的输出也必须包含同结构的 `_id`，并按 [§5](#01-5) 比较。
- 若 $q_g$ 的输出文档显式 `$project: {_id: 0}` 把 `_id` 抑制掉，则归一化结果中也不存在 `_id` 字段（适用 [§4.3](#01-4) 的 missing 语义）。

---

## §5 递归相等关系 $\equiv_{rec}$ 的形式化定义 <a id="01-5"></a>

$\equiv_{rec}$ 定义在 [§4](#01-4) 给出的规范树上，按结构归纳：

### 5.1 标量

$$
x \equiv_{rec} y\ \iff\ \mathrm{type}(x) = \mathrm{type}(y)\ \wedge\ \mathrm{value}(x) = \mathrm{value}(y)
$$

其中 type 与 value 都按 [§4.1](#01-4) 规范化后的形式比较。

### 5.2 字典

设 $a$、$b$ 都是字典，$\mathrm{keys}(a)$、$\mathrm{keys}(b)$ 是它们的键集合。

$$
a \equiv_{rec} b\ \iff\ \mathrm{keys}(a) = \mathrm{keys}(b)\ \wedge\ \forall k \in \mathrm{keys}(a):\ a[k] \equiv_{rec} b[k]
$$

> 由 [§4.3](#01-4)，`a` 中存在键 `k` 而 `b` 中不存在键 `k`，必然导致 $\mathrm{keys}(a) \neq \mathrm{keys}(b)$，即使 `a[k] = null` 也判不等。

### 5.3 列表

设 $u = [u_1, \dots, u_m]$、$v = [v_1, \dots, v_n]$。

**默认（顺序敏感）**：

$$
u \equiv_{rec} v\ \iff\ m = n\ \wedge\ \forall i \in [1,m]:\ u_i \equiv_{rec} v_i
$$

**当 gold 来源标明为无序集合**（例如顶层结果对应 `find` 且 NLQ 不含排序意图、或聚合管道在最外层不含 `$sort`）：先对 $u$ 与 $v$ 各自按某个固定的规范全序排序，再按上式逐位比较。规范全序由"对每个元素递归生成的规范字符串字典序"给出，确保排序结果稳定且与具体语言无关。

### 5.4 顶层语义

$\mathrm{NormExec}(q, D)$ 的根永远是一个"文档列表"。该列表是否按位置比较，遵循 [§5.3](#01-5) 中关于顺序敏感性的判定。

---

## §6 Instance 正确性的根原则 <a id="01-6"></a>

[§3](#01-3) 给出了一个对称形式的正确性锚：$q_p$ 与 $q_g$ 在 $D$ 上归一化执行结果递归相等。这条锚的判别力依赖于一个隐含前提：**gold $q_g$ 本身在该样本的 $(\mathrm{NLQ}, S, D)$ 上是唯一可信的正确解**。如果 gold 不唯一、不可信，或它在 $D$ 上输出退化（例如永远空），则 [§3](#01-3) 就失去了识别能力。

本节把这个隐含前提显式化为四条根原则。任务边界层只**陈述**它们；具体如何在数据集构造侧把它们落实为可执行的检查与过滤，由 [03 数据集构造](./03_dataset_construction.md) 负责。

### 6.1 四条根原则

每个有资格作为 gold 进入 TEND 的样本 $(\mathrm{NLQ}, S, \mathit{db\_id}, q_g)$ 必须满足：

| 编号 | 原则 | 形式陈述 |
|---|---|---|
| P1 | **执行正确** | $\mathrm{NormExec}(q_g, D)$ 不抛错、可被 [§4](#01-4) 完整归一化、且代表 NLQ 所述意图。 |
| P2 | **语义唯一** | $\mathrm{NLQ}$ 在 $(S, D)$ 上的语义意图唯一；不存在另一个"在自然语言上同样合理但语义本质不同"的意图与 $q_g$ 的输出竞争。 |
| P3 | **判别力** | 平凡 baseline（如返回空集合、返回未筛选全集、返回固定常量）在 $D$ 上不与 $\mathrm{NormExec}(q_g, D)$ 递归相等；任何"明显错"的查询能被 [§3](#01-3) 的锚区分出来。 |
| P4 | **世界非平凡** | 数据快照 $D$ 中存在足够实例使得查询语义被真实触发：例如 Top-$k$ 类查询要求至少 $k$ 个候选；分组聚合要求分组键有足够多样性；过滤类查询要求满足条件与不满足条件的文档**都**存在。 |

### 6.2 与正确性锚的耦合

- P1 是 [§3](#01-3) 的锚能成立的执行前提：若 $q_g$ 在 $D$ 上无法归一化执行，锚式右端无定义。
- P2 是锚的语义合法性：若 NLQ 本身歧义，则 $q_p$ 即使表达了另一个合法意图也会被误判。
- P3 是锚的"信号—噪声比"：保证一条样本通过/未通过提供真实信息，而不是被平凡解轻易满足。
- P4 是锚的"在 $D$ 上有效"：保证锚不被空集恒等之类的退化情况短路。

### 6.3 与下游评测的关系

[04 评测方法](./04_evaluation_methodology.md) 中的执行级正确性指标 EX 直接实例化 [§3](#01-3) 的锚——即 EX 把 $q_p$ 是否满足该等式作为单样本布尔判定。本节四条原则确保了 EX 在 TEND 上是有意义的判别量，而不是被 gold 退化拖垮的伪指标。

> 本节四条原则在概念上是任务边界与数据集构造之间的契约：任务层声明"什么样的样本才有资格存在"，构造层负责把这种"资格"变成具体的可执行检查。任务层不规定具体的检查算法、不规定中间过程产物的命名，也不规定样本最终被如何标注。

---

## §7 canonical 示例的语义解读 <a id="01-7"></a>

为让全部下游文档（[02](./02_dataset_design.md) / [03](./03_dataset_construction.md) / [04](./04_evaluation_methodology.md) / [05](./05_solution_design.md)）共享同一个心智锚点，约定一个 canonical 示例。所有具体的字段字面量、record 字段、构造步骤与 mongosh 命令均由下游文档负责给出，本节只解释**语义**。

### 7.1 canonical 三元组

| 项 | 值 |
|---|---|
| `db_id` | `orchestra` |
| canonical NLQ | *"List the top 3 conductors with the most performances."* |
| canonical `record_id` | `99001`（int 类型，与 [02 §2.1](./02_dataset_design.md#02-2) 字段定义一致） |

### 7.2 schema 的语义形态

`orchestra` 数据库以 `conductor` 为顶层 collection，向下嵌套四层：

```
conductor
└── orchestra[]            （一个 conductor 指挥多支 orchestra）
    └── performance[]      （一支 orchestra 有多场 performance）
        └── show[]         （一场 performance 包含多个 show）
```

也就是说，"performance" 这个被 NLQ 直接计数的对象**不在 conductor 文档的顶层**，而是嵌在 `conductor.orchestra[].performance[]` 路径下。

### 7.3 NLQ 的操作语义

把 canonical NLQ 翻译成操作语义大致是这样：对每位 `conductor`，统计她/他名下**所有** `orchestra` 中的**所有** `performance` 的总条数；再以这个总条数为键按降序排序；最后取前 3 名。

输出的形态是一个**长度为 3** 的列表，每个元素至少标识一位 conductor 并带上其 performance 总数。具体保留哪些字段、用什么键名展示，是 [02](./02_dataset_design.md) / [03](./03_dataset_construction.md) 处理的细节，本节不固定。

### 7.4 为什么需要嵌套展开

要在 mongosh 中数到 performance：

- 必须穿透 `conductor.orchestra[]` 与 `conductor.orchestra[].performance[]` 两层数组；
- 不能停在 conductor 的顶层字段，也不能只数 orchestra 的数量（那会回答另一个问题）；
- 因此可执行解通常要么走 `$unwind` 展开两层、要么走 `$reduce` / `$sum` 在数组上做内部累加。

无论选择哪条路径，"必须跨两层数组进行计数"是这条 NLQ 的硬约束。

### 7.5 判别力来自哪里

按 [§6](#01-6) 的 P3，判别力意味着平凡或近邻的错误解必须能被 [§3](#01-3) 的锚区分开。该样本的判别面至少包括：

- **聚合粒度**：必须聚合到 conductor 级，而不是 orchestra 级或 performance 级。
- **被计数对象**：必须计 performance，而不是 orchestra 或 show。
- **排序方向**：必须按 count 降序，而不是按字母序、年份或任意序。
- **截断长度**：必须取前 3，不是前 1、前 5 或全集。

只要 $q_p$ 在以上四个面之一与 $q_g$ 不一致，[§4](#01-4) 归一化后的结果就会与 gold 不相等，从而被 [§3](#01-3) 的锚判错。

### 7.6 在 $D$ 上的非平凡性

按 [§6](#01-6) 的 P4，`orchestra` 数据库的快照 $D$ 必须满足：

- conductor 数 $\ge 3$（否则取前 3 退化）；
- 至少有两位 conductor 的 performance 总数不同（否则降序无信号）；
- 至少存在跨两层嵌套的真实 performance 实例（否则两层 unwind 都返回空，结果退化为空集）。

---

## §8 全文符号表 <a id="01-8"></a>

| 符号 | 含义 | 首次出现 |
|---|---|---|
| $\mathrm{NLQ}$ | 自然语言查询字符串 | [§1.1](#01-1) |
| $S$ | MongoDB schema 上下文（collection / 字段 / BSON 类型 / 嵌套） | [§1.1](#01-1) |
| $\mathit{db\_id}$ | 数据库标识符 | [§1.1](#01-1) |
| $D$ | 由 $\mathit{db\_id}$ 唯一绑定的只读数据快照，$D \equiv D(\mathit{db\_id})$ | [§1.3](#01-1) |
| $q^{\mathrm{MQL}}$ | 任务输出的 mongosh 可执行查询（`find` 或 aggregation pipeline） | [§1.2](#01-1) |
| $q_p$ | 模型对某样本的预测查询 | [§3](#01-3) |
| $q_g$ | 某样本的 gold 查询 | [§3](#01-3) |
| $f$ | 任务函数 $f:(\mathrm{NLQ}, S, \mathit{db\_id}) \to q^{\mathrm{MQL}}$ | [§1](#01-1) |
| $\mathrm{Parse}$ | mongosh 解析算子，$q^{\mathrm{MQL}} \to \mathrm{AST}$ | [§1.4](#01-1) |
| $\mathrm{Exec}$ | mongosh 执行算子，$(\mathrm{AST}, D) \to r$，返回原始 BSON 结果 | [§1.4](#01-1) |
| $\mathrm{Norm}$ | BSON 结果归一化算子（[§4](#01-4) 给出全部规则） | [§1.4](#01-1) |
| $\mathrm{NormExec}$ | 复合算子 $\mathrm{Norm}\!\circ\!\mathrm{Exec}\!\circ\!\mathrm{Parse}$，对 $(q, D)$ 返回规范化执行结果 | [§1.4](#01-1) |
| $\equiv_{rec}$ | 规范树上的递归相等关系（[§5](#01-5) 给出定义） | [§3](#01-3) |
| P1–P4 | Instance 正确性的四条根原则（执行正确 / 语义唯一 / 判别力 / 世界非平凡） | [§6.1](#01-6) |

---

> 下游文档定位：record 字段与 audit 信息见 [02 数据集设计](./02_dataset_design.md)；从 schema/数据生成到 NLQ pipeline 的具体构造流水线见 [03 数据集构造](./03_dataset_construction.md)；6 个评测指标 EM/QSM/QFC/EX/EFM/EVM 的公式与协议见 [04 评测方法](./04_evaluation_methodology.md)；面向本任务的方法架构见 [05 方法设计](./05_solution_design.md)。
