# TEND §02 · Dataset Design

> 本文件是 TEND 基准**发布物 (released artifacts)** 的单一真源 (Single Source of Truth)。
> 它定义：哪些文件存在、每条 record 的字段契约、库级资产 (schema / data / phenomena_registry / persona_bank / intent_template_lattice / domain_catalog) 的 JSON 格式、train/test 切分规则、9 + 1 轴的 min/max 双配额协议。
> 它**不定义**：任务签名 ([01](./01_task_definition.md#01-0))、资产如何被合成 ([03](./03_dataworld_synthesis.md#03-0) / [04](./04_intent_to_query_construction.md#04-0))、评测协议 ([05](./05_evaluation_methodology.md#05-0))、解法侧 ([06](./06_solution_design.md#06-0))。

---

<a id="02-0"></a>
## 02-0 摘要

本文件规范 TEND 发布物的 **FORM (形式)**，覆盖五个主题：

1. **资产清单与目录布局** —— 发布物由哪些文件构成 ([§02-1](#02-1))
2. **Record 字段契约** —— 每条样本必须有哪些字段、类型、强约束 ([§02-2](#02-2))
3. **库级资产格式** —— 6 类库级 JSON 的 schema 规范 ([§02-3](#02-3))
4. **切分规则** —— train / test 如何划分 ([§02-4](#02-4))
5. **覆盖目标与配额** —— 9 + 1 轴的 min/max 双配额协议 ([§02-5](#02-5))

### 规模概览

| 维度 | 数量 |
|---|---:|
| Database (`db_id` 粒度) | 154 |
| Domain (`domain_id` 粒度) | 105 |
| Collection (累计) | 347 |
| Record (样本) | 17,020 |
| NLQ per record (严格) | 5 |
| NLQ 累计 | 85,100 |
| Train records | 14,245 |
| Test records | 2,775 |
| Train/Test 比例 | ≈ 83.7 / 16.3 |
| 库级全局文件 | 3 (persona_bank + intent_template_lattice + domain_catalog) |
| 库级 per-db 文件 | 462 = 154 × 3 (schema + data + phenomena_registry) |

### 单一来源不变式 (single-source invariant)

**所有 17,020 条 record 均由 [03](./03_dataworld_synthesis.md#03-0) + [04](./04_intent_to_query_construction.md#04-0) 定义的四阶段流水线 (Phase A DataWorld Synthesis → Phase B Intent Seeding → Phase C Query Materialization → Phase D Adversarial Validation) 机制化产出。** 主集中不存在人工撰写的 record；人类仅出现在 `audit/human_anchor/` 的零样本锚定评估子集 (solver-side only，不混入主集)。

### 边界声明 (本文件**不**涵盖)

| 主题 | 归属 |
|---|---|
| 任务签名、NormExec、≡_rec、6 禁用算子、P1–P4 | [01 §1](./01_task_definition.md#01-1) – [01 §6](./01_task_definition.md#01-6) |
| gold-as-class 等价类的形式定义 | [01 §3](./01_task_definition.md#01-3) |
| Domain Template Bank、Schema Composer、Witness Data Generator | [03 §2](./03_dataworld_synthesis.md#03-2) – [03 §4](./03_dataworld_synthesis.md#03-4) |
| F_topology 7 特性、schema_complexity_profile | [03 §3](./03_dataworld_synthesis.md#03-3) |
| 6 噪声层 + 36-item taxonomy | [03 §4](./03_dataworld_synthesis.md#03-4) |
| Phenomena taxonomy、detectors、world_signature | [03 §5](./03_dataworld_synthesis.md#03-5) |
| Persona Bank 内部内容 | [04 §2](./04_intent_to_query_construction.md#04-2) |
| SI DSL、SI→MQL Compiler、Symbolic Lift→QIR | [04 §4](./04_intent_to_query_construction.md#04-4) – [04 §7](./04_intent_to_query_construction.md#04-7) |
| canonical_form_set 四元组的派生算法 | [04 §9](./04_intent_to_query_construction.md#04-9) |
| V_correct / V_discrim / V_diverse / 4-panel 难度 | [04 §10](./04_intent_to_query_construction.md#04-10) – [04 §11](./04_intent_to_query_construction.md#04-11) |
| 7 评测指标、EX 判定、4-panel 报告 | [05 §1](./05_evaluation_methodology.md#05-1) – [05 §4](./05_evaluation_methodology.md#05-4) |
| SMART 4-stage 解法流水线、解法侧边界 | [06 §1](./06_solution_design.md#06-1) |

---

<a id="02-1"></a>
## 02-1 资产清单与目录布局

TEND 的发布物分为两层：

- **Tier-1 · 主集 (main set)** —— 评测闭包的最小充分集，任何合规 solver 读入即可运行
- **Tier-2 · Audit 子树** —— 供研究者复现 / 调试 / 诊断的可选工件，评测时**不**被读取

两层通过固定目录位置严格分隔，见 [§02-1-5](#02-1-5)。

<a id="02-1-1"></a>
### 02-1-1 主集 (train.json / test.json / TEND.json)

| 文件 | 内容 | Record 数 | 用途 |
|---|---|---:|---|
| `train.json` | 训练集 records 数组 | 14,245 | 模型训练、few-shot 例库 |
| `test.json` | 测试集 records 数组 | 2,775 | 固定评测集，提交对此集评分 |
| `TEND.json` | train + test 的等价并集 | 17,020 | 第三方研究、全量覆盖分析 |

三者满足: `TEND.json == concat(train.json, test.json)` 在 `record_id` 排序后逐字节相等。

Record 字段契约见 [§02-2](#02-2)。

<a id="02-1-2"></a>
### 02-1-2 库级资产 (library-level assets)

库级资产分 **per-db 组** (每个 `db_id` 一份，154 份) 与 **global 组** (全数据集共享一份) 两类：

| 类别 | 文件 | 份数 | 角色 |
|---|---|---:|---|
| per-db | `mongodb_schema/<db_id>.json` | 154 | 数据库结构声明 (collections × fields × types) |
| per-db | `mongodb_data/<db_id>.json` | 154 | 执行所需的 witness 数据实例 |
| per-db | `phenomena_registry/<db_id>.json` | 154 | 该库被植入的 Phenomena 清单与 witness 指针 |
| global | `persona_bank.json` | 1 | 全部 5+ personas 的属性表 |
| global | `intent_template_lattice.json` | 1 | (phenomenon_class × persona) → SI_pattern_family 映射 |
| global | `domain_catalog.json` | 1 | 105 domains 的模板结构注册表 |

三份 global 文件的语义定义见 [04 §2](./04_intent_to_query_construction.md#04-2)；phenomena_registry 的物理格式由 [§02-3-3](#02-3-3) 规定，其生成规则由 [03 §5](./03_dataworld_synthesis.md#03-5) 定义。

<a id="02-1-3"></a>
### 02-1-3 Audit 子树

Audit 按粒度分为两层：

**Per-record audit** (路径 `audit/<db_id>/<record_id>/`)：

| 工件 | 文件 | 说明 |
|---|---|---|
| Structured Intent 原件 | `structured_intent.yaml` | SI DSL 序列化，生成 MQL 的上游 ([04 §4](./04_intent_to_query_construction.md#04-4)) |
| Symbolic Lift 产物 | `qir.yaml` | MQL→QIR 反向提升的中间表示 ([04 §7](./04_intent_to_query_construction.md#04-7)) |
| Checker 衍生件 | `derived/oracle.json` | 语义等价判定器 |
| Checker 衍生件 | `derived/checker.json` | AST 检查器快照 |
| Checker 衍生件 | `derived/mutations.json` | 同义与等价改写枚举 |
| Checker 衍生件 | `derived/canonical_form_set.json` | 详细形式的 canonical_form_set (见 [§02-2-2](#02-2-2)) |
| World Robustness | `world_variants/` | 同语义但不同 witness 数据的 K 个副本 |
| World Robustness | `world_robustness.json` | K 个副本上的等价性证书 |
| Difficulty | `empirical_difficulty.json` | 4-panel 失败概率 + 难度标签 |
| Noise | `noise_trace.json` | 6 噪声层每层被命中的 36-item 列表 |
| Complexity | `complexity_vector.json` | 结构复杂度向量 + schema_complexity_profile |
| Lift | `lift_trace.json` | 符号提升每步变换记录 |
| Bridge Defeat | `sql_bridge_defeat.json` | SQL→MQL 直译为何失败的证据 |
| Bridge Defeat | `template_bridge_defeat.json` | 模板化查询为何失败的证据 |
| Seed | `phenomena_seed.json` | 该 record 种子化的 (phenomenon_id, persona_id) |
| Seed | `intent_template.json` | 命中的 lattice 单元与 SI_pattern_family |
| Seed | `witness_augmentation_trace.json` | Phase C 增量 witness 补齐记录 |
| Certificate | `certificate.json` | V_correct / V_discrim / V_diverse 三元通过证书 |
| Panel Verdict | `frontier_panel_verdict.json` | small / medium / large / frontier 各 panel 裁决 |

**Global audit** (路径 `audit/_global/`)：

| 工件 | 文件 | 说明 |
|---|---|---|
| 分类板 | `taxonomy_board.json` | 9 + 1 轴的全集分布与配额快照 |
| 参考面板 | `reference_panel/diff_panel_small.json` | small solver 集与裁决矩阵 |
| 参考面板 | `reference_panel/diff_panel_medium.json` | medium solver 集 |
| 参考面板 | `reference_panel/diff_panel_large.json` | large solver 集 |
| 参考面板 | `reference_panel/diff_panel_frontier.json` | frontier solver 集 |
| 参考面板 | `reference_panel/sql_bridge.json` | SQL 直译基线 |
| 参考面板 | `reference_panel/template_bridge.json` | 模板基线 |
| 覆盖 | `coverage/coverage_report.json` | 9 + 1 轴的最终覆盖审计 (见 [§02-5-6](#02-5-6)) |
| 人类锚 | `human_anchor/` | 零样本人类解的锚定子集 (solver-side only) |
| 语法 | `grammar/mql_grammar.ebnf` | MQL 子集的 EBNF 定义 |
| 拒绝池 | `rejected/` | 未通过 V_correct / V_discrim / V_diverse 的候选与拒绝原因 |

<a id="02-1-4"></a>
### 02-1-4 目录树

```
TEND/
├── train.json                              # 14,245 records
├── test.json                               #  2,775 records
├── TEND.json                               # 17,020 records (并集, 便利文件)
│
├── mongodb_schema/                         # 154 个 per-db schema
│   ├── orchestra.json
│   ├── academic_system.json
│   └── ... (共 154 份)
│
├── mongodb_data/                           # 154 个 per-db witness data
│   ├── orchestra.json
│   ├── academic_system.json
│   └── ... (共 154 份)
│
├── phenomena_registry/                     # 154 个 per-db phenomena 清单
│   ├── orchestra.json
│   ├── academic_system.json
│   └── ... (共 154 份)
│
├── persona_bank.json                       # 全局, 5+ personas
├── intent_template_lattice.json            # 全局, (phenomenon × persona) → pattern
├── domain_catalog.json                     # 全局, 105 domains
│
└── audit/                                  # Tier-2, 可选工件
    ├── _global/
    │   ├── taxonomy_board.json
    │   ├── reference_panel/
    │   │   ├── diff_panel_small.json
    │   │   ├── diff_panel_medium.json
    │   │   ├── diff_panel_large.json
    │   │   ├── diff_panel_frontier.json
    │   │   ├── sql_bridge.json
    │   │   └── template_bridge.json
    │   ├── coverage/coverage_report.json
    │   ├── human_anchor/
    │   ├── grammar/mql_grammar.ebnf
    │   └── rejected/
    │
    └── <db_id>/<record_id>/                # per-record audit, 可缺省
        ├── structured_intent.yaml
        ├── qir.yaml
        ├── derived/
        │   ├── oracle.json
        │   ├── checker.json
        │   ├── mutations.json
        │   └── canonical_form_set.json
        ├── world_variants/
        ├── world_robustness.json
        ├── empirical_difficulty.json
        ├── noise_trace.json
        ├── complexity_vector.json
        ├── lift_trace.json
        ├── sql_bridge_defeat.json
        ├── template_bridge_defeat.json
        ├── phenomena_seed.json
        ├── intent_template.json
        ├── witness_augmentation_trace.json
        ├── certificate.json
        └── frontier_panel_verdict.json
```

<a id="02-1-5"></a>
### 02-1-5 主集与 Audit 的硬边界

**规则 B1 (评测闭包性):** 仅读取主集 (`train.json` / `test.json`) 与库级资产 (`mongodb_schema/` + `mongodb_data/` + `phenomena_registry/` + `persona_bank.json` + `intent_template_lattice.json` + `domain_catalog.json`) 已经构成完整的评测闭包 —— 任何合规 solver + 评测器不访问 `audit/` 下任何文件即可完成 [05 §3](./05_evaluation_methodology.md#05-3) 中规定的全部评测流程。

**规则 B2 (Audit 可选性):** `audit/` 下任何子路径的缺失都**不构成** dataset 不完整；使用方可以按需只发布 Tier-1 主集 + 库级资产，压缩发布体积。

**规则 B3 (缺失表达方式):** Record 中的 audit 引用字段 (见 [§02-2-3](#02-2-3)) 若不可用，**通过省略该字段表达**，禁止写入 `null` 或空字符串。这与 [01 §5](./01_task_definition.md#01-5) 中的 **省略语义 = 删除键** 约束相一致。

**规则 B4 (单向引用):** 主集 record 可以**引用** audit 路径 (通过 `_ref` 字段)，但 audit 文件**不得**被主集语义所依赖。换言之，删除 `audit/` 之后，主集仍自洽。

---

<a id="02-2"></a>
## 02-2 Record 字段契约

<a id="02-2-1"></a>
### 02-2-1 必填字段 (5 项)

每条 record 必须、且仅必须包含以下 5 个顶层字段：

| 字段 | 类型 | 约束 | 语义 |
|---|---|---|---|
| `record_id` | int | 全局唯一，正整数 | Record 的全集 ID，跨 train/test 不重用 |
| `db_id` | string | 与 `mongodb_schema/<db_id>.json` / `mongodb_data/<db_id>.json` / `phenomena_registry/<db_id>.json` 文件名严格一致 | 指向该 record 所用的数据库 |
| `nl_queries` | list[string] | 长度严格等于 5，`nl_queries[0]` 为 L1 canonical | 5 条等语义 NLQ，覆盖 specificity 光谱 |
| `MQL` | string | 可被 MongoDB shell 在 `mongodb_data/<db_id>.json` 上执行；与 `canonical_form_set` AST_check = pass | 一条**代表性** gold MQL 实例 |
| `canonical_form_set` | object | 四元组，详见 [§02-2-2](#02-2-2) | **主 gold**，等价类成员的 AST 特征 |

**示例最小骨架** (仅示意，不含真实数据):

```json
{
  "record_id": 1001,
  "db_id": "orchestra",
  "nl_queries": ["...", "...", "...", "...", "..."],
  "MQL": "db.conductor.aggregate([...])",
  "canonical_form_set": {
    "must_contain": ["$setWindowFields", "$facet", "$ifNull"],
    "must_not_contain": [],
    "must_contain_at_root": ["$setWindowFields", "$facet"],
    "must_not_contain_at_root": []
  }
}
```

<a id="02-2-2"></a>
### 02-2-2 Gold 字段的解读 (canonical_form_set 为主，MQL 为代表实例)

TEND 将 gold 定义为**等价类 (equivalence class)**，而非单一串。主集 record 通过两件工件共同描述这个等价类：

1. **`canonical_form_set` (主 gold, 四元组)** —— AST 层面的成员资格谓词
2. **`MQL` (代表实例)** —— 等价类中一条可执行的具体实例，用于执行结果对比

**`canonical_form_set` 的四元组结构:**

| 子字段 | 类型 | 语义 |
|---|---|---|
| `must_contain` | list[string] | 管线中**至少出现一次** (任意嵌套深度均可) 的 operator token |
| `must_not_contain` | list[string] | 管线中**不得出现** (任意嵌套深度) 的 operator token |
| `must_contain_at_root` | list[string] | 顶层 aggregation 阶段**必须**出现的 stage operator (如 `$setWindowFields` / `$facet`) |
| `must_not_contain_at_root` | list[string] | 顶层 aggregation 阶段**不得**出现的 stage operator |

四元组的派生算法 (从 SI + QIR + mutations 合成) 见 [04 §9](./04_intent_to_query_construction.md#04-9)；此处仅规范其**物理形式**。

**评测接受判定 (eval acceptance):**

对任何 solver 预测 `q_p`，评测器判定 pass 当且仅当：

```
(a) AST_check(q_p, canonical_form_set) == pass
    AND
(b) NormExec(q_p, D) ≡_rec NormExec(MQL, D)
```

其中 `AST_check` 检查四元组约束，`NormExec` 与 `≡_rec` 见 [01 §3-1](./01_task_definition.md#01-3-1)。这保证：

- **语法等价类成员性** (条件 a) 由 `canonical_form_set` 捕获
- **语义执行一致性** (条件 b) 由代表实例 `MQL` 锚定

**为何两者都需要:** 仅 (a) 不足以排除算子虽对但执行偏差的情形；仅 (b) 不足以排除 6 禁用算子绕过 ([01 §4](./01_task_definition.md#01-4)) 与模板桥接攻击。合取 (a) ∧ (b) 等价于 gold 等价类成员资格。

<a id="02-2-3"></a>
### 02-2-3 Audit 可选字段清单

以下字段**可选**；存在时必为字符串，形如 `audit/<db_id>/<record_id>/<path>`；**不存在时必须省略键**，禁止写入 `null` / `""` / 空 object。

| 字段名 | 指向 audit 文件 |
|---|---|
| `structured_intent_ref` | `structured_intent.yaml` |
| `qir_ref` | `qir.yaml` |
| `canonical_form_set_detailed_ref` | `derived/canonical_form_set.json` |
| `oracle_ref` | `derived/oracle.json` |
| `mutations_ref` | `derived/mutations.json` |
| `empirical_difficulty_ref` | `empirical_difficulty.json` |
| `noise_trace_ref` | `noise_trace.json` |
| `complexity_vector_ref` | `complexity_vector.json` |
| `lift_trace_ref` | `lift_trace.json` |
| `phenomena_seed_ref` | `phenomena_seed.json` |
| `persona_ref` | `phenomena_seed.json` 中 `persona_id` 字段的解引 |
| `intent_template_ref` | `intent_template.json` |
| `witness_augmentation_trace_ref` | `witness_augmentation_trace.json` |
| `world_robustness_certificate_ref` | `world_robustness.json` |
| `frontier_panel_verdict_ref` | `frontier_panel_verdict.json` |
| `sql_bridge_defeat_ref` | `sql_bridge_defeat.json` |
| `template_bridge_defeat_ref` | `template_bridge_defeat.json` |

**扁平字段 (非 `_ref`):** 以下扁平字段可选携带，方便统计 / 过滤而无须解引：

| 字段名 | 类型 | 取值域 |
|---|---|---|
| `operator_family` | string | 23 SI patterns 之一 ([04 §3](./04_intent_to_query_construction.md#04-3)) |
| `nosql_nativeness_level` | string | `L0` / `L1` / `L2` / `L3` / `L4` (L0 = SQL 可直译, L4 = translation-lossy, 详见 [04 §4](./04_intent_to_query_construction.md#04-4) SI pattern 表的默认 nativeness) |
| `shape_policy` | string | `preserve` / `reshape` / `reduce` |
| `empirical_difficulty` | string | `easy` / `medium` / `hard` / `expert` ([04 §11-2](./04_intent_to_query_construction.md#04-11-2) 主桶由 pr_medium 决定) |
| `world_signature` | string | `sha256:<hex>` ([03 §5](./03_dataworld_synthesis.md#03-5)) |
| `tds_cell` | string | `<topology>×<pattern>×<difficulty>×<noise>×<nlq_style>` 6 元拼接 |

<a id="02-2-4"></a>
### 02-2-4 强约束 C1–C9

| ID | 约束 | 违约动作 |
|---|---|---|
| **C1** | **省略语义一致**：所有可选字段的缺失以**省略 key** 表达，禁止 `null` / `""` / `{}` | 发布前校验器拒绝 |
| **C2** | **NLQ 长度严格 5**：`len(nl_queries) == 5` 无例外 | 发布前校验器拒绝 |
| **C3** | **L1 锚点**：`nl_queries[0]` 为 L1 canonical 形式 (最显式、去除所有 underspecification)；其余 4 条在 specificity 光谱上递减 | Phase C 生成时保证 |
| **C4** | **Specificity 排列**：`nl_queries[0]` 固定为 L1 canonical;`nl_queries[1..4]` 是 {L0, L2, L3, L4} 的一个排列(5 层 specificity 定义见 [04 §7-1](./04_intent_to_query_construction.md#04-7-1)) | Phase C NLQ×5 生成时保证 |
| **C5** | **db_id 一致性**：`db_id` 必须同时为 `mongodb_schema/` + `mongodb_data/` + `phenomena_registry/` 三目录下存在的文件基名 | 发布前 3-way 文件名集合校验 |
| **C6** | **MQL 可执行且 AST 一致**：`MQL` 在 `mongodb_data/<db_id>.json` 上能用有限资源执行完毕且 `AST_check(MQL, canonical_form_set) == pass` | 发布前执行 + AST 双通 |
| **C7** | **canonical_form_set 非空**：`must_contain_at_root` 至少含 1 项 (即根阶段至少一个特征 stage) | 发布前校验器拒绝 |
| **C8** | **Nativeness 一致**：`operator_family` / `canonical_form_set` / `nosql_nativeness_level` 三者相容 (具体对应表见 [04 §9-3](./04_intent_to_query_construction.md#04-9)) | 发布前三元组校验 |
| **C9** | **Audit 可选性**：任何 `_ref` 字段缺失不构成 record 不合规；但存在时路径必解引成功 | 校验器对存在的 `_ref` 解引；缺失跳过 |

---

<a id="02-3"></a>
## 02-3 库级资产格式

<a id="02-3-1"></a>
### 02-3-1 `mongodb_schema/<db_id>.json`

顶层 keys = collection 名；每个 value 为字段声明表，字段类型取自固定类型集 `{INT, REAL, TEXT, BOOL, OBJECT, ARRAY}`。`OBJECT` 与 `ARRAY` 支持递归嵌套。

**F_topology 7 特性** (schema 层面的结构特征) 由 [03 §3-1](./03_dataworld_synthesis.md#03-3-1) 枚举，本文件仅规范其**物理编码方式** (通过嵌套 `OBJECT` / `ARRAY` / `polymorphic marker` / `dynamic_key marker`)。

**示例 (orchestra schema, 部分):**

```json
{
  "conductor": {
    "_id": "INT",
    "Conductor_ID": "INT",
    "Name": "TEXT",
    "Age": "INT",
    "Nationality": "TEXT",
    "Years_of_Work": "INT",
    "orchestra": {
      "type": "ARRAY",
      "items": {
        "type": "OBJECT",
        "fields": {
          "Orchestra_ID": "INT",
          "Orchestra": "TEXT",
          "Year_of_Founded": "INT",
          "Major_Record_Format": "TEXT",
          "performance": {
            "type": "ARRAY",
            "items": {
              "type": "OBJECT",
              "fields": {
                "Performance_ID": "INT",
                "Attendance": "INT",
                "Date": "TEXT"
              }
            }
          }
        }
      }
    }
  }
}
```

上例展示 F_topology 中的 `nested_3_deep` (3 层嵌套) + `sparse_embedded` (当部分 conductor 文档省略 `Name` 字段时触发)。

<a id="02-3-2"></a>
### 02-3-2 `mongodb_data/<db_id>.json`

顶层 keys **必须** 与 `mongodb_schema/<db_id>.json` 完全一致；每个 value 为该 collection 的文档数组。

**尺寸下界** 与 **6 噪声层** 的注入规则由 [03 §4](./03_dataworld_synthesis.md#03-4) 规范。本文件仅规范：

- 顶层 keys 集合 = schema keys 集合 (强等)
- 每个文档至少含 `_id` 字段
- 文档数组非空
- `ObjectId` / `ISODate` 等 BSON 扩展类型以 `{"$oid": "..."}` / `{"$date": "..."}` MongoDB Extended JSON 编码

**示例** (orchestra data, 截取):

```json
{
  "conductor": [
    {
      "_id": 1,
      "Conductor_ID": 1,
      "Name": "Antal Doráti",
      "Age": 80,
      "orchestra": [
        {
          "Orchestra_ID": 1,
          "Orchestra": "BBC Symphony",
          "Year_of_Founded": 1930,
          "performance": [
            { "Performance_ID": 1, "Attendance": 5000, "Date": "2019-01-10" },
            { "Performance_ID": 2, "Attendance": 6200, "Date": "2019-02-14" }
          ]
        }
      ]
    },
    { "_id": 2, "Conductor_ID": 2, "orchestra": [/* ... */] }
  ]
}
```

注意 `_id: 2` 的文档**省略**了 `Name` 键 —— 此为 `sparse_embedded` 特性的物理实现，对应 [01 §5](./01_task_definition.md#01-5) 的省略语义。

<a id="02-3-3"></a>
### 02-3-3 `phenomena_registry/<db_id>.json`

每个 `db_id` 对应一份 registry，列出该库被**植入**的 Phenomena、其 witness 证据指针与 intent hooks。

**格式:**

```json
{
  "db_id": "orchestra",
  "world_signature": "sha256:a47f3e...",
  "phenomena": [
    {
      "phenomenon_id": "temporal_trend@Attendance",
      "phenomenon_class": "temporal_trend",
      "witness_evidence": {
        "collection": "conductor",
        "path": "orchestra[].performance[].Attendance",
        "document_ids": ["conductor/1", "conductor/2", "conductor/3"]
      },
      "detector_signature": "sha256:1b29c7...",
      "intent_hooks": ["window_function_with_facet_filter", "change_point"]
    },
    {
      "phenomenon_id": "cross_conductor_comparison",
      "phenomenon_class": "cross_entity_comparison",
      "witness_evidence": {
        "collection": "conductor",
        "path": "orchestra[].performance[].Attendance",
        "document_ids": ["conductor/1", "conductor/2", "conductor/3", "conductor/4"]
      },
      "detector_signature": "sha256:8c42a1...",
      "intent_hooks": ["facet_comparison", "window_function_with_facet_filter"]
    },
    {
      "phenomenon_id": "null_cluster@Name",
      "phenomenon_class": "null_cluster",
      "witness_evidence": {
        "collection": "conductor",
        "path": "Name",
        "document_ids": ["conductor/2", "conductor/5"]
      },
      "detector_signature": "sha256:9d15f2...",
      "intent_hooks": ["ifnull_coalesce", "null_aware_projection"]
    },
    {
      "phenomenon_id": "pollution@Attendance",
      "phenomenon_class": "measurement_pollution",
      "witness_evidence": {
        "collection": "conductor",
        "path": "orchestra[].performance[].Attendance",
        "document_ids": ["conductor/3"]
      },
      "detector_signature": "sha256:4e62d8...",
      "intent_hooks": ["robust_aggregation"]
    },
    {
      "phenomenon_id": "cardinality_boundary@orchestra",
      "phenomenon_class": "cardinality_boundary",
      "witness_evidence": {
        "collection": "conductor",
        "path": "orchestra[]",
        "document_ids": ["conductor/1", "conductor/6"]
      },
      "detector_signature": "sha256:7f03c4...",
      "intent_hooks": ["existence_check", "boundary_grouping"]
    }
  ]
}
```

**字段语义:**

| 字段 | 语义 |
|---|---|
| `phenomenon_id` | 库内唯一的现象实例 ID (通常形如 `<class>@<path>` 或 `<class>`) |
| `phenomenon_class` | 属于 [03 §5-1](./03_dataworld_synthesis.md#03-5-1) 中 10+ 分类的哪一类 |
| `witness_evidence` | 证据三元组: 所在 collection / 文档内路径 / 至少 1 个触发文档 ID |
| `detector_signature` | Detector 脚本在当前 witness 数据上的指纹 (`sha256`) |
| `intent_hooks` | 该 phenomenon 可被哪些 SI_pattern_family 消费 (用于 Phase B 种子化) |

**强约束 (见 [§02-3-7](#02-3-7)):** `witness_evidence.document_ids` 中的每个 ID 必须在 `mongodb_data/<db_id>.json` 中存在；`phenomenon_class` 必须在 [03 §5-1](./03_dataworld_synthesis.md#03-5-1) 的正式分类表中。

<a id="02-3-4"></a>
### 02-3-4 `persona_bank.json`

全局单文件，列出所有 persona。**本文件仅规范物理格式**；persona 目录与编写规则见 [04 §2-1](./04_intent_to_query_construction.md#04-2-1)。

```json
{
  "personas": [
    {
      "persona_id": "analyst",
      "name": "Business Analyst",
      "framing_style": "analyst",
      "priorities": ["window_function_with_facet_filter", "facet_comparison", "rank_with_tie_breaking", "time_bucket_aggregation"],
      "description": "…"
    },
    {
      "persona_id": "ops",
      "name": "Operations Engineer",
      "framing_style": "ops",
      "priorities": ["existence_check", "cardinality_boundary", "recent_window_filter"],
      "description": "…"
    },
    {
      "persona_id": "auditor",
      "name": "Compliance Auditor",
      "framing_style": "auditor",
      "priorities": ["null_aware_projection", "anomaly_filter", "exhaustive_match"],
      "description": "…"
    },
    {
      "persona_id": "researcher",
      "name": "Data Researcher",
      "framing_style": "researcher",
      "priorities": ["robust_aggregation", "multi_level_unwind", "distribution_tail"],
      "description": "…"
    },
    {
      "persona_id": "end_user",
      "name": "End User",
      "framing_style": "end-user",
      "priorities": ["simple_lookup", "single_group_summary"],
      "description": "…"
    }
  ]
}
```

**字段:**

| 字段 | 类型 | 约束 |
|---|---|---|
| `persona_id` | string | 全局唯一，snake_case |
| `name` | string | 人类可读名 |
| `framing_style` | enum | `analyst` / `ops` / `auditor` / `researcher` / `end-user` |
| `priorities` | list[string] | SI_pattern_family 偏好列表 (有序) |
| `description` | string | 自然语言描述，影响 NLQ 生成语气 ([04 §8](./04_intent_to_query_construction.md#04-8)) |

**强约束:** 至少 5 个 persona；`priorities` 中每个 token 必须是 [04 §3](./04_intent_to_query_construction.md#04-3) 23 SI_pattern_family 之一。

<a id="02-3-5"></a>
### 02-3-5 `intent_template_lattice.json`

全局单文件，编码 `(phenomenon_class, persona_id) → SI_pattern_family` 的多对多映射 (即 lattice 单元)。

```json
{
  "lattice": [
    {
      "phenomenon_class": "temporal_trend",
      "persona_id": "analyst",
      "si_pattern_family": "window_function_with_facet_filter",
      "expansion_template_ref": "templates/wf_facet_filter.yaml",
      "priority": 0.85
    },
    {
      "phenomenon_class": "temporal_trend",
      "persona_id": "researcher",
      "si_pattern_family": "change_point_detection",
      "expansion_template_ref": "templates/change_point.yaml",
      "priority": 0.60
    },
    {
      "phenomenon_class": "null_cluster",
      "persona_id": "auditor",
      "si_pattern_family": "null_aware_projection",
      "expansion_template_ref": "templates/null_projection.yaml",
      "priority": 0.90
    }
  ]
}
```

**字段:**

| 字段 | 类型 | 约束 |
|---|---|---|
| `phenomenon_class` | string | 必须出现在 [03 §5-1](./03_dataworld_synthesis.md#03-5-1) 的正式 taxonomy |
| `persona_id` | string | 必须出现在 `persona_bank.json` |
| `si_pattern_family` | string | 必须是 23 SI_pattern_family 之一 |
| `expansion_template_ref` | string | 指向 SI DSL 展开模板 ([04 §4](./04_intent_to_query_construction.md#04-4)) |
| `priority` | float | [0,1]；Phase B 采样时的权重系数 |

**容量估计:** 10+ phenomenon_class × 5 personas × (平均 2–3 个相容 pattern) ≈ 200+ lattice 单元，覆盖全部 23 SI_pattern_family。

<a id="02-3-6"></a>
### 02-3-6 `domain_catalog.json`

全局单文件，列出 105 个 domain 的模板结构。

```json
{
  "domains": [
    {
      "domain_id": "performing_arts",
      "domain_name": "Performing Arts",
      "template_structure": {
        "entities": [
          { "name": "conductor", "attrs": ["name", "age", "nationality", "years_of_work"] },
          { "name": "orchestra", "attrs": ["name", "year_of_founded", "major_record_format"] },
          { "name": "performance", "attrs": ["performance_id", "attendance", "date"] }
        ],
        "cardinality": [
          { "parent": "conductor", "child": "orchestra", "rel": "1..*" },
          { "parent": "orchestra", "child": "performance", "rel": "1..*" }
        ]
      },
      "f_topology_hints": ["nested_3_deep", "sparse_embedded"]
    },
    {
      "domain_id": "academic_system",
      "domain_name": "Academic System",
      "template_structure": {
        "entities": [/* … */],
        "cardinality": [/* … */]
      },
      "f_topology_hints": ["mixed_embed_ref", "polymorphic_collection"]
    }
  ]
}
```

**字段:**

| 字段 | 类型 | 约束 |
|---|---|---|
| `domain_id` | string | 全局唯一 |
| `domain_name` | string | 人类可读名 |
| `template_structure.entities` | list | 每项含 `name` + `attrs` |
| `template_structure.cardinality` | list | 每项含 `parent` / `child` / `rel` (如 `1..1` / `1..*` / `*..*`) |
| `f_topology_hints` | list[string] | 该 domain 倾向诱导的 F_topology 子集 |

**强约束:** 共 105 条；`f_topology_hints` 每项属于 [03 §3-1](./03_dataworld_synthesis.md#03-3-1) 的 7 特性集合。

<a id="02-3-7"></a>
### 02-3-7 Schema / Data / Phenomena 三方一致性约束

以下约束在发布前由静态校验器强制：

| ID | 约束 | 范围 |
|---|---|---|
| **S1** | `mongodb_schema/<db_id>.json` 与 `mongodb_data/<db_id>.json` 的顶层 keys 集合完全相等 | 所有 154 db |
| **S2** | 每条 `phenomena_registry/<db_id>.json` 中 `phenomena[*].witness_evidence.document_ids` 里每个 ID 必在 `mongodb_data/<db_id>.json` 中可解引 | 所有 154 db |
| **S3** | `phenomena_registry/<db_id>.json` 中 `phenomena[*].witness_evidence.path` 必可在 `mongodb_schema/<db_id>.json` 中通过 `type=OBJECT/ARRAY` 递归解析到字段 | 所有 154 db |
| **S4** | `phenomenon_class` 取值 ⊂ [03 §5-1](./03_dataworld_synthesis.md#03-5-1) taxonomy | 所有 phenomena |
| **S5** | `intent_template_lattice.json` 中 `persona_id` 取值 ⊂ `persona_bank.json` 的 `persona_id` 集合 | 所有 lattice 单元 |
| **S6** | `intent_template_lattice.json` 中 `phenomenon_class` 取值 ⊂ [03 §5-1](./03_dataworld_synthesis.md#03-5-1) taxonomy | 所有 lattice 单元 |
| **S7** | `intent_template_lattice.json` 中 `si_pattern_family` 取值 ⊂ 23 SI_pattern_family | 所有 lattice 单元 |
| **S8** | 每条 record 的 `db_id` 必在 `mongodb_schema/` / `mongodb_data/` / `phenomena_registry/` 三个目录下同时存在同名文件 | 所有 17,020 record |

**S1 + S2 + S3** 是 **schema ↔ data ↔ phenomena 三方不变式**，保证：(1) Phenomena 所声称的结构在 schema 中可达；(2) Phenomena 所声称的证据在 data 中可见；(3) 不存在"空头现象" (registered but un-witnessed)。

<a id="02-3-8"></a>
### 02-3-8 单世界发布 vs K 世界 Audit

**发布 (Tier-1):** 每个 `db_id` 对应**单一** `mongodb_data/<db_id>.json`。该文件即"金标准 witness world"，`world_signature` 由其内容哈希决定。

**Audit (Tier-2):** `audit/<db_id>/<record_id>/world_variants/` 目录下额外存放 K 个 (默认 K=5) **等语义变体世界** —— 相同 schema 下，phenomena 同类但 witness 实例不同的数据副本。用于 [04 §10-3](./04_intent_to_query_construction.md#04-10-3) 的 `world_robustness` 测试。

**关键区别:**

| 属性 | Tier-1 (单世界) | Tier-2 (K 世界) |
|---|---|---|
| 评测执行依赖 | 是 | 否 |
| 包含 schema 多版本 | 否 | 否 (schema 固定) |
| 包含 phenomena 多版本 | 否 | 是 (类同、实例异) |
| 驱动的评测 | EX / FEX / AST / etc. | `world_robustness_certificate` |

---

<a id="02-4"></a>
## 02-4 切分规则

<a id="02-4-1"></a>
### 02-4-1 切分单位 (按 db_id)

**规则 SP1 (db 不可分):** 切分以 `db_id` 为原子单位 —— 同一 `db_id` 下的所有 record 整体进入 train 或整体进入 test，**不得跨集拆分**。

**规则 SP2 (db_id disjoint):** `set(train.db_id) ∩ set(test.db_id) == ∅`。

**规则 SP3 (no leakage via audit):** 同一 `db_id` 的 `mongodb_schema/` / `mongodb_data/` / `phenomena_registry/` 文件仅被其所在的集合使用；评测时 solver 仅可访问 `test.db_id` 对应的库级资产。

<a id="02-4-2"></a>
### 02-4-2 比例与规模

| 集合 | db_id 数 | Record 数 | NLQ 数 |
|---|---:|---:|---:|
| Train | 130 | 14,245 | 71,225 |
| Test | 24 | 2,775 | 13,875 |
| **合计** | **154** | **17,020** | **85,100** |

Train/Test ≈ 83.7 / 16.3 (按 record 数)。

平均每 db 约 110.5 record；分布由 Phase B 的 (phenomenon × persona × pattern) 种子覆盖决定，见 [§02-5](#02-5)。

<a id="02-4-3"></a>
### 02-4-3 Domain 同侧聚合

**规则 SP4 (domain coherence):** 同一 `domain_id` 下的所有 `db_id` **原则上**倾向分到同一侧 (train 或 test)；允许**有限跨侧**以满足配额，但跨侧比例必须 ≤ 15%。

**105 domain → 154 db 映射:**

- 多数 domain 下有 1–2 个 db (单 schema 变体)
- 少数高产 domain 下有 3+ db (多 schema 变体，例如 `academic_system` 下可能有多个不同拓扑的 academic db)

**Domain 切分目标:**

| 集合 | domain 数 (近似) |
|---:|---:|
| Train-only | ≈ 82 |
| Test-only | ≈ 18 |
| Cross-side (跨 train/test) | ≤ 5 |

Domain 同侧聚合降低 schema-level leakage 风险 —— solver 无法通过记忆训练 domain 的 schema 去获得 test 上的不公平优势。

<a id="02-4-4"></a>
### 02-4-4 不设多桶 (no extra buckets)

TEND 采用**扁平 train / test 两分**，不额外设置 dev / val / hidden 等桶。

**理由:**

1. **Audit 完备性替代验证桶**：`audit/_global/reference_panel/` 已含 4-panel (small/medium/large/frontier) 的裁决矩阵，研究者可从 train 中按 `empirical_difficulty` 字段自行抽样构建 dev 集。
2. **Test 即最终集**：test 一经发布即冻结，不再拆"hidden test"；研究透明度优先。
3. **Coverage 监控在 train/test 两侧独立进行** ([§02-5-5](#02-5-5))。

---

<a id="02-5"></a>
## 02-5 覆盖目标与配额

<a id="02-5-1"></a>
### 02-5-1 单一来源声明

**本节是 TEND 覆盖目标的唯一规范来源。** 03、04、05 文件中提及"覆盖"均指向本节。

Phenomena 生成 ([03 §5](./03_dataworld_synthesis.md#03-5))、SI 采样 ([04 §2](./04_intent_to_query_construction.md#04-2))、评测报告 ([05 §4](./05_evaluation_methodology.md#05-4)) 各自引用本节的轴定义与配额表，不得自行扩充或修改。

<a id="02-5-2"></a>
### 02-5-2 嵌入覆盖的 Facility-Location 目标

TEND 的种子采样遵循**设施位置 (facility-location) 目标**：

```
maximize   Σ_{axis ∈ 轴集} coverage_entropy(axis)
subject to  min_quota[cell] ≤ count[cell] ≤ max_quota[cell]  ∀ cell
            每一轴上的分布熵 ≥ H_min(axis)
            轴间条件独立性 (Cramer's V / mutual information) 受控
```

**解读:**

- **覆盖熵最大化** —— 避免在任一轴上坍缩到少数 cell
- **min_quota 下界** —— 保证稀有 cell (如 `L4 × frontier`) 有至少 `min_quota` 个代表
- **max_quota 上界** —— 避免高频 cell (如 `flat × simple_lookup × easy`) 压垮配额
- **轴间独立性** —— 避免"L4 仅出现在 nested_3_deep 上"这种强相关耦合

该目标由 Phase B 的 Intent Seeding 采样器在线实现 ([04 §2-3](./04_intent_to_query_construction.md#04-2-3))；本节仅规范目标函数**形式**。

<a id="02-5-3"></a>
### 02-5-3 覆盖 9 + 1 轴

| # | 轴 ID | 取值域 (示例) | 观测点 | 主要覆盖来源 |
|---:|---|---|---|---|
| 1 | `T_domain` | 105 domain_id | `domain_catalog.json` | Phase A · Domain Template Bank |
| 2 | `T_pattern` | 23 SI_pattern_family | `intent_template_lattice.json` | Phase B + Phase C |
| 3 | `T_topology` | 7 F_topology 组合 | `mongodb_schema/<db_id>.json` | Phase A · Schema Composer |
| 4 | `T_operator_family` | 23 (同 T_pattern，MQL 角度) | record.`operator_family` | Phase C |
| 5 | `T_difficulty` | `easy` / `medium` / `hard` / `expert` | record.`empirical_difficulty` | Phase D · 4-panel pr_medium 主桶 |
| 6 | `T_nosql_feature_mix` | 24-feature 布尔向量 (如 `$setWindowFields`、`$lookup`、`$unwind` 等) | record.`canonical_form_set` | Phase C |
| 7 | `T_noise_mix` | 6 noise layers × 36 items 布尔向量 | audit `noise_trace.json` | Phase A · Noise Injection |
| 8 | `T_nosql_nativeness` | `L0` / `L1` / `L2` / `L3` / `L4` | record.`nosql_nativeness_level` | Phase C · SI → MQL |
| 9 | `T_topology_features` | 7 F_topology 二进制向量 (单独/组合) | `mongodb_schema/<db_id>.json` | Phase A · Schema Composer |
| **+1** | `T_intent_space` | **(phenomenon_class × persona × SI_pattern) 网格** | (`phenomenon_class`, `persona_id`, `si_pattern_family`) | Phase B · Intent Seeding |

**T_intent_space 网格容量:**

```
|T_intent_space| = |phenomenon_class| × |persona| × |SI_pattern_family|
                 = 10+ × 5 × 23
                 ≈ 1150 cells
```

其中**可行 cell** (即 lattice 单元中有非零 priority 的) 约 200–250；覆盖目标为这 200–250 cell 上的 min_quota 全达成，而非 1150 的全覆盖。

**T_topology 与 T_topology_features 的区别:**

- `T_topology` 按 F_topology 7 特性的**组合 bucket** (约 7 + C(7,2) 常见组合 ≈ 30 cells)
- `T_topology_features` 按 F_topology 7 特性的**独立二进制向量** (最多 2^7 = 128 cells；实际触发约 40+)

<a id="02-5-4"></a>
### 02-5-4 min/max 双配额协议

每个 (轴, cell) 对关联一组配额 `{min_quota, max_quota}`：

| 条件 | 动作 | 语义 |
|---|---|---|
| `current[cell] < min_quota` | **Strong-pull accept** | 无条件接收新候选，直至到达 min |
| `min_quota ≤ current[cell] < max_quota` 且 `ΔF ≥ ε` | **Marginal accept** | 仅当带来边际覆盖增益 ΔF 超过 ε 才接收 |
| `current[cell] ≥ max_quota` | **Reject** | 该 cell 已饱和，拒绝新候选 |

其中 ΔF 为 facility-location 目标的边际增益 ([§02-5-2](#02-5-2))；ε 是流水线超参数 (Phase B)。

**反馈回路 (feedback loop):**

若对某 cell `current < min_quota` 连续 N 轮无法填满 (candidate 生成失败率过高)，Phase B 向 Phase A / Phase B 本身回传反馈信号：

| 症状 | 反馈信号 | 调整目标 |
|---|---|---|
| `T_difficulty = expert` 的某 cell 空 | `bump_amplify_rounds_for_hard_records` | Phase D 难度 calibration (amplify 到 expert 桶) |
| `T_nosql_nativeness = L4` 在某 domain 为空 | `expand_domain_l4_templates` | Phase A Domain Template Bank |
| `T_intent_space` 某 (phenomenon × persona) 空 | `adjust_persona_sampling_prior` | Phase B persona 采样权重 |
| `T_topology = polymorphic + sparse` 为空 | `rerun_schema_composer_with_seed` | Phase A Schema Composer 种子 |

反馈的具体实现机制见 [04 §10-3](./04_intent_to_query_construction.md#04-10-3) (V_diverse 根因反馈)。

<a id="02-5-5"></a>
### 02-5-5 衍生硬约束

以下约束由 [§02-5-3](#02-5-3) + [§02-5-4](#02-5-4) 在发布时刻快照出的结果，作为**发布物的硬不变式**：

| ID | 约束 | 监控点 |
|---|---|---|
| **H1** | `|train| + |test| == 17,020` | 全集基数 |
| **H2** | `set(train.db_id) ∩ set(test.db_id) == ∅` | db 级 disjoint |
| **H3** | `set(mongodb_schema/*.json) == set(mongodb_data/*.json) == set(phenomena_registry/*.json)` (按文件基名) | 3-way 文件集合等 |
| **H4** | `∀ record: len(record.nl_queries) == 5` | NLQ 严格 5 |
| **H5** | `count(test, nosql_nativeness_level ∈ {L2,L3,L4}) / |test| ≥ 0.40` | Test 集 L2+ 比例 (高于 SQL-bridgeable tier) |
| **H6** | `count(test, nosql_nativeness_level == L4) / |test| ≥ 0.15` | Test L4 比例 |
| **H7** | `∀ persona_id ∈ persona_bank: count(test, persona_id=p) ≥ min_quota[persona=p]` | Test 每 persona 下界 |
| **H8** | `∀ phenomenon_class ∈ [03 §5-1]: count(test, phenomenon_class=c) ≥ min_quota[phenomenon_class=c]` | Test 每 phenomenon 下界 |
| **H9** | `∀ axis ∈ 10 轴: H_observed(axis, test) ≥ H_min(axis)` | Test 每轴熵下界 |
| **H10** | 每个 `operator_family` 至少在 train 与 test 中分别出现 ≥ `min_quota` 次 | 23 patterns 跨集覆盖 |

违反任一 Hx 的发布候选将被 [04 §10](./04_intent_to_query_construction.md#04-10) 的 V_diverse 验证器拒绝。

<a id="02-5-6"></a>
### 02-5-6 覆盖审计 (coverage_report.json)

`audit/_global/coverage/coverage_report.json` 是发布时的覆盖快照，不被评测读取但供第三方复核。

**Schema:**

```json
{
  "dataset_snapshot": "TEND-release-<hash>",
  "axes": {
    "T_domain": {
      "cells_total": 105,
      "cells_covered": 105,
      "min_quota_met_ratio": 1.0,
      "histogram": { "performing_arts": 220, "academic_system": 340, /* … */ }
    },
    "T_pattern": {
      "cells_total": 23,
      "cells_covered": 23,
      "min_quota_met_ratio": 1.0,
      "histogram": { "window_function_with_facet_filter": 480, "facet_comparison": 512, /* … */ }
    },
    "T_topology": { /* … */ },
    "T_operator_family": { /* … */ },
    "T_difficulty": {
      "cells_total": 4,
      "histogram": { "easy": 4250, "medium": 6780, "hard": 4910, "expert": 1080 }
    },
    "T_nosql_feature_mix": { /* … */ },
    "T_noise_mix": { /* … */ },
    "T_nosql_nativeness": {
      "cells_total": 5,
      "histogram": { "L0": 1260, "L1": 7160, "L2": 3960, "L3": 2820, "L4": 1820 }
    },
    "T_topology_features": { /* … */ },
    "T_intent_space": {
      "cells_total": 1150,
      "cells_feasible": 228,
      "cells_covered": 226,
      "intent_space_cells_covered": 226,
      "histogram_top_10": [/* … */]
    }
  },
  "hard_constraints": {
    "H1_total_cardinality": { "train": 14245, "test": 2775, "sum": 17020, "pass": true },
    "H2_db_disjoint": { "pass": true },
    "H3_file_set_equal": { "pass": true, "n_files_each": 154 },
    "H4_nlq_length_5": { "violations": 0, "pass": true },
    "H5_test_nonL1_ratio": { "value": 0.513, "pass": true },
    "H6_test_L4_ratio": { "value": 0.187, "pass": true },
    "H7_persona_min": { "pass": true, "details": { /* per persona */ } },
    "H8_phenomenon_min": { "pass": true, "details": { /* per class */ } },
    "H9_entropy_min": { "pass": true, "details": { /* per axis */ } },
    "H10_operator_cross_split": { "pass": true }
  },
  "frontier_panel_defeat_distribution": {
    "all_four_panels_solved": 0.52,
    "large_solved_medium_failed": 0.18,
    "large_solved_small_failed": 0.15,
    "only_frontier_solved": 0.09,
    "none_solved": 0.06
  }
}
```

**新字段说明:**

| 字段 | 含义 |
|---|---|
| `intent_space_cells_covered` | T_intent_space 网格上有至少 1 条 record 的 cell 数 (对应 H7 + H8 联合) |
| `frontier_panel_defeat_distribution` | 4-panel 裁决矩阵的分布 —— 追踪数据集的"难度形状" ([04 §11](./04_intent_to_query_construction.md#04-11)) |

---

<a id="02-6"></a>
## 02-6 Canonical 示例 Record (完整 JSON)

以下为 `db_id = orchestra`, `record_id = 1001` 的完整 JSON (主集形式，含全部必填 + 主要 audit `_ref`)。该示例与 [01 §7](./01_task_definition.md#01-7)、[03 §7](./03_dataworld_synthesis.md#03-7)、[04 §12](./04_intent_to_query_construction.md#04-12)、[05 §5](./05_evaluation_methodology.md#05-5)、[06 §5](./06_solution_design.md#06-5) 的 `orchestra/1001` 逐字节一致。

```json
{
  "record_id": 1001,
  "db_id": "orchestra",

  "nl_queries": [
    "对每位 conductor，先在其指挥的 orchestra 的 performance 上按 Performance_ID 升序、对 Attendance 计算窗口大小为 (当前, 前 2 场) 的滑动平均；取该 conductor 的最后一次窗口平均值作为代表值 (Attendance 缺失视为 0)。然后计算所有 conductor 代表值的中位数。最终只输出代表值严格大于该中位数的 conductor，字段为 Name 与 last_window_avg；若 Name 缺失则显示为 (unknown)；不要求排序。",
    "对每个指挥，计算其 orchestra 的 performance 记录 Attendance 上 3 场滑动平均的最后一次值 (Attendance 缺失按 0)，再求所有指挥该值的中位数，最后返回严格高于中位数的指挥的 Name 和 last_window_avg (Name 缺失写 (unknown))。",
    "给我每个指挥的 3 场滑动出勤均值的最后一个值，和全体的中位数比较，只保留超过中位数的指挥；Attendance 空算 0，Name 空写 (unknown)。",
    "找出出勤滑动均值明显高于大家中位数的指挥名与该均值 (缺失算 0 / (unknown))。",
    "列出最近场次出勤趋势高于同行中位数的指挥。"
  ],

  "MQL": "db.conductor.aggregate([\n  { $unwind: { path: \"$orchestra\", preserveNullAndEmptyArrays: false } },\n  { $unwind: { path: \"$orchestra.performance\", preserveNullAndEmptyArrays: false } },\n  { $setWindowFields: {\n      partitionBy: \"$_id\",\n      sortBy: { \"orchestra.performance.Performance_ID\": 1 },\n      output: {\n        moving_avg_attendance: {\n          $avg: { $ifNull: [\"$orchestra.performance.Attendance\", 0] },\n          window: { documents: [-2, 0] }\n        }\n      }\n  } },\n  { $group: {\n      _id: \"$_id\",\n      Name: { $first: { $ifNull: [\"$Name\", \"(unknown)\"] } },\n      last_window_avg: { $last: \"$moving_avg_attendance\" }\n  } },\n  { $facet: {\n      per_conductor: [ { $project: { _id: 0, Name: 1, last_window_avg: 1 } } ],\n      global_median: [\n        { $sort: { last_window_avg: 1 } },\n        { $group: { _id: null, vals: { $push: \"$last_window_avg\" } } },\n        { $project: { _id: 0, median: { $arrayElemAt: [\"$vals\", { $floor: { $divide: [{ $size: \"$vals\" }, 2] } }] } } }\n      ]\n  } },\n  { $project: {\n      kept: { $filter: {\n        input: \"$per_conductor\",\n        as: \"c\",\n        cond: { $gt: [\"$$c.last_window_avg\", { $arrayElemAt: [\"$global_median.median\", 0] }] }\n      } }\n  } },\n  { $unwind: \"$kept\" },\n  { $project: { _id: 0, Name: \"$kept.Name\", last_window_avg: \"$kept.last_window_avg\" } }\n])",

  "canonical_form_set": {
    "must_contain": ["$setWindowFields", "$facet", "$ifNull"],
    "must_not_contain": [],
    "must_contain_at_root": ["$setWindowFields", "$facet"],
    "must_not_contain_at_root": []
  },

  "operator_family": "window_function_with_facet_filter",
  "nosql_nativeness_level": "L4",
  "shape_policy": "reshape",
  "empirical_difficulty": "hard",
  "world_signature": "sha256:a47f3e...",
  "tds_cell": "nested_3_deep+sparse_embedded × window_function_with_facet_filter × hard × schema_naive × english",

  "structured_intent_ref": "audit/orchestra/1001/structured_intent.yaml",
  "qir_ref": "audit/orchestra/1001/qir.yaml",
  "canonical_form_set_detailed_ref": "audit/orchestra/1001/derived/canonical_form_set.json",
  "oracle_ref": "audit/orchestra/1001/derived/oracle.json",
  "mutations_ref": "audit/orchestra/1001/derived/mutations.json",
  "empirical_difficulty_ref": "audit/orchestra/1001/empirical_difficulty.json",
  "noise_trace_ref": "audit/orchestra/1001/noise_trace.json",
  "complexity_vector_ref": "audit/orchestra/1001/complexity_vector.json",
  "lift_trace_ref": "audit/orchestra/1001/lift_trace.json",
  "phenomena_seed_ref": "audit/orchestra/1001/phenomena_seed.json",
  "intent_template_ref": "audit/orchestra/1001/intent_template.json",
  "witness_augmentation_trace_ref": "audit/orchestra/1001/witness_augmentation_trace.json",
  "world_robustness_certificate_ref": "audit/orchestra/1001/world_robustness.json",
  "frontier_panel_verdict_ref": "audit/orchestra/1001/frontier_panel_verdict.json",
  "sql_bridge_defeat_ref": "audit/orchestra/1001/sql_bridge_defeat.json",
  "template_bridge_defeat_ref": "audit/orchestra/1001/template_bridge_defeat.json"
}
```

**示例字段解读:**

| 字段 | 取值 | 说明 |
|---|---|---|
| `record_id` | `1001` | 全集唯一 ID |
| `db_id` | `"orchestra"` | 对应 `mongodb_schema/orchestra.json` + `mongodb_data/orchestra.json` + `phenomena_registry/orchestra.json` |
| `operator_family` | `"window_function_with_facet_filter"` | 23 SI_pattern_family 之一 |
| `nosql_nativeness_level` | `"L4"` | 最原生 (heavy use of `$setWindowFields` + `$facet` + `$ifNull`)，不可被 SQL 直译 |
| `shape_policy` | `"reshape"` | 输出 shape 与输入不同 (聚合 + filter + unwind) |
| `empirical_difficulty` | `"hard"` | 由 `(pr_small, pr_medium, pr_large, pr_frontier) = (0.0, 0.2, 0.6, 0.2)` 判定 ([04 §11](./04_intent_to_query_construction.md#04-11)) |
| `world_signature` | `"sha256:a47f3e..."` | `mongodb_data/orchestra.json` + `phenomena_registry/orchestra.json` 合成哈希 |
| `canonical_form_set.must_contain` | `["$setWindowFields","$facet","$ifNull"]` | 管线中必存在 (any depth) |
| `canonical_form_set.must_contain_at_root` | `["$setWindowFields","$facet"]` | 根阶段必存在 |
| `canonical_form_set.must_not_contain` | `[]` | 本题无被禁算子 |
| `canonical_form_set.must_not_contain_at_root` | `[]` | 本题无根层被禁算子 |

**Phenomena seed (解引 `phenomena_seed_ref`):**

```json
{
  "seed_phenomena": ["temporal_trend@Attendance", "cross_conductor_comparison"],
  "persona_id": "analyst",
  "si_pattern_family": "window_function_with_facet_filter",
  "intent_template_cell": {
    "phenomenon_class": "temporal_trend + cross_entity_comparison",
    "persona_id": "analyst",
    "priority_at_sample_time": 0.85
  }
}
```

**NLQ specificity 光谱** (`nl_queries[0..4]` 的 5 层 specificity,定义见 [04 §7-1](./04_intent_to_query_construction.md#04-7-1)):

| Index | L 级 | 特征 |
|---|---|---|
| 0 | L1 canonical | schema-naive canonical,显式展开 intent + 部分 params,无 NoSQL 术语 |
| 1 | L0 | underspecified colloquial,模糊口语,缺 schema 线索 |
| 2 | L2 | schema-aware,显式提及字段、集合、键 |
| 3 | L3 | NoSQL-jargon,使用 `$match`、`$facet` 等术语 |
| 4 | L4 | multilingual or strong colloquial,中英混杂 / 方言 / 隐喻 |

---

<a id="02-7"></a>
## 02-7 边界声明

| 跨引主题 | 本文件角色 | 正式来源 |
|---|---|---|
| 任务签名、NormExec、≡_rec、6 禁用算子、P1–P4、3 层正确性 | 仅引用 | [01 §1](./01_task_definition.md#01-1) – [01 §6](./01_task_definition.md#01-6) |
| gold-as-class 等价类的语义定义 | 仅引用 | [01 §3](./01_task_definition.md#01-3) |
| Domain Template Bank 目录与表 | 仅引用；通过 `domain_catalog.json` 映射 | [03 §2](./03_dataworld_synthesis.md#03-2) |
| Schema Composer + F_topology 7 特性 + schema_complexity_profile | 仅引用 | [03 §3](./03_dataworld_synthesis.md#03-3) |
| Witness Data Generator + 6 噪声层 + 36 item taxonomy | 仅引用；通过 `mongodb_data/` 承载 | [03 §4](./03_dataworld_synthesis.md#03-4) |
| Phenomena taxonomy + detectors + world_signature | 仅引用；通过 `phenomena_registry/` 承载 | [03 §5](./03_dataworld_synthesis.md#03-5) |
| Persona Bank 内容与语气规则 | 仅规范 `persona_bank.json` 格式 | [04 §2-1](./04_intent_to_query_construction.md#04-2-1) |
| Intent Template Lattice 语义内容 | 仅规范 `intent_template_lattice.json` 格式 | [04 §2-2](./04_intent_to_query_construction.md#04-2-2) |
| SI DSL 语法 + SI→MQL Compiler | 仅引用 | [04 §4](./04_intent_to_query_construction.md#04-4) – [04 §6](./04_intent_to_query_construction.md#04-6) |
| Symbolic Lift → QIR | 仅引用；audit 字段 `qir_ref` | [04 §7](./04_intent_to_query_construction.md#04-7) |
| NLQ × 5 生成策略 + specificity 光谱 | 仅规范长度=5 + L1 锚点；不规范文本策略 | [04 §8](./04_intent_to_query_construction.md#04-8) |
| Witness Augmentation 增量 | 仅引用；audit 字段 `witness_augmentation_trace_ref` | [04 §8](./04_intent_to_query_construction.md#04-8) |
| canonical_form_set 四元组派生 | 仅规范物理格式；不规范派生算法 | [04 §9](./04_intent_to_query_construction.md#04-9) |
| V_correct / V_discrim / V_diverse 三方验证 | 仅引用；audit 字段 `certificate_ref` | [04 §10](./04_intent_to_query_construction.md#04-10) |
| 4-panel 难度校准 (small/medium/large/frontier) | 仅引用；record 字段 `empirical_difficulty` | [04 §11](./04_intent_to_query_construction.md#04-11) |
| SQL Bridge / Template Bridge 路由与防御 | 仅引用；audit 字段 `sql_bridge_defeat_ref` / `template_bridge_defeat_ref` | [04 §11](./04_intent_to_query_construction.md#04-11) |
| 7 评测指标、EX canonical_form_set 成员判定、4-party disjointness | 仅引用 | [05 §1](./05_evaluation_methodology.md#05-1) – [05 §3](./05_evaluation_methodology.md#05-3) |
| 4-panel 报告 + 强制披露 (mandatory disclosure) | 仅引用 | [05 §4](./05_evaluation_methodology.md#05-4) |
| SMART 4-stage 解法流水线 + solver-side 边界 | 仅引用 | [06 §1](./06_solution_design.md#06-1) |

**单向依赖声明:**

- 本文件**依赖** [01](./01_task_definition.md#01-0) 的基本概念 (任务签名 / gold 等价类)
- 本文件**依赖** [03](./03_dataworld_synthesis.md#03-0) 与 [04](./04_intent_to_query_construction.md#04-0) 产出的资产语义
- [05](./05_evaluation_methodology.md#05-0) 与 [06](./06_solution_design.md#06-0) **依赖** 本文件定义的发布形式
- 本文件**不**反向依赖 [05](./05_evaluation_methodology.md#05-0) 或 [06](./06_solution_design.md#06-0)

**发布前校验清单 (本文件所负责):**

1. C1–C9 record 级强约束全通
2. S1–S8 schema/data/phenomena 一致性全通
3. H1–H10 切分与覆盖硬约束全通
4. 9 + 1 轴覆盖审计达标
5. canonical 示例 `orchestra/1001` 与 [01 §7](./01_task_definition.md#01-7) / [03 §7](./03_dataworld_synthesis.md#03-7) / [04 §12](./04_intent_to_query_construction.md#04-12) / [05 §5](./05_evaluation_methodology.md#05-5) / [06 §5](./06_solution_design.md#06-5) 逐字节一致

任一失败则 dataset 不予发布。
