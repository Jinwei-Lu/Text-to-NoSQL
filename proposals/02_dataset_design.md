# 02 数据资产层（dataset assets）

<a id="02-0"></a>
## §0 摘要

本文档定义 TEND benchmark 的**数据资产层**（dataset assets）：

- 哪些文件构成发布物，组织在哪些目录下；
- 单条记录（record）的字段集与字段语义；
- 库级 schema 与库级 data 的格式与一致性约束；
- train / test 切分规则；
- 规模与统计上的硬性数字。

本文档是上述事项的 Single Source of Truth（SSoT）。
任务的 IO 形式化（NLQ + schema → MQL）、结果归一化语义（`≡_rec`）由 [01 任务定义](./01_task_definition.md) 给出，本文档仅作引用，不重复。

读者路线图：

| 角色 | 推荐阅读 |
|---|---|
| 模型作者 | [§1](#02-1)–[§3](#02-3)：知道训练用什么、推理时输入什么 |
| 评测者 | [§4](#02-4)–[§5](#02-5)：知道怎么切分、规模数字是多少 |
| 数据构造者 | [§2.3](#02-2) 的强约束 + [§3.3](#02-3) 的 schema/data 一致性 |

---

<a id="02-1"></a>
## §1 资产清单与目录布局

### 1.1 资产清单

| 资产类 | 路径 | 粒度 | 主要用途 |
|---|---|---|---|
| 主集 record | `TEND/train.json`, `TEND/test.json` | record-level | 训练 / 测试 / headline 评测 |
| 完整集合（可选） | `TEND/TEND.json` | record-level | 全量参考；等于 train ∪ test |
| 库级 schema | `TEND/mongodb_schema/<db_id>.json` | db_id | 模型输入与 schema grounding |
| 库级 data | `TEND/mongodb_data/<db_id>.json` | db_id | mongosh 执行评测 |
| Audit 证书 | `audit/<db_id>/<record_id>/certificate.json` | record-level | 构造侧逆向工程证书（RE 证书）；仅审计可见 |
| Audit 拒绝 | `audit/rejected/<db_id>/<record_id>.json` | record-level | 未通过 RE 证书的候选；仅审计 |

### 1.2 目录树

```
TEND/
├── train.json                 # 14,245 条 record
├── test.json                  # 2,775 条 record
├── TEND.json                  # 17,020 条 = train ∪ test（可选）
├── mongodb_schema/            # 154 个 <db_id>.json
│   ├── orchestra.json
│   ├── school_bus.json
│   └── ...
└── mongodb_data/              # 154 个 <db_id>.json
    ├── orchestra.json
    ├── school_bus.json
    └── ...

audit/
├── <db_id>/
│   └── <record_id>/
│       └── certificate.json   # 构造侧 RE 证书（逆向工程证书）
└── rejected/
    └── <db_id>/
        └── <record_id>.json   # 未通过 RE 证书的候选
```

### 1.3 主集与 audit 的边界

- `TEND/train.json` 与 `TEND/test.json` 是**唯一**两类参与 headline 评测的 record 桶。
- `audit/` 子树**不进入** train、不进入 test、不进入 headline 评测。
- 主集**不再**划分为额外子桶（无 held-out pool、无 staging 多层目录）。
- audit 仅用于构造侧的留痕与可重复审查，是构造流水线 [03 数据集构造](./03_dataset_construction.md) 的副产物。

---

<a id="02-2"></a>
## §2 record schema 字段定义

每条 record 是 `train.json` / `test.json` / `TEND.json` 中的一个 JSON 对象。

### 2.1 核心字段（论文锚定，必填）

| 字段 | 类型 | 规则 |
|---|---|---|
| `record_id` | int | 全局唯一记录标识。整型；在 `TEND.json` 内部不重复 |
| `db_id` | string | 库标识符；与 `TEND/mongodb_schema/<db_id>.json` 和 `TEND/mongodb_data/<db_id>.json` 的文件名严格一致 |
| `nl_queries` | list[string] | 长度严格为 5 的 NLQ 变体列表；第 1 条为 canonical 表达 |
| `ref_sql` | string | 来源 Spider 的 SQL，用于追溯原始查询语义；**不直接进入评测** |
| `MQL` | string | gold MongoDB 查询；mongosh 可直接执行的 `find(...)` 或 `aggregate([...])` |

### 2.2 可选 audit 字段（不进入 headline 评测）

下列字段不参与评测排行，仅作为构造侧审计信息出现在记录上。具体生成与校验语义由 [03 数据集构造](./03_dataset_construction.md) 给出，本文档只**注册字段名与字段含义**：

| 字段 | 类型 | 规则 |
|---|---|---|
| `schema_complexity_profile` | dict | 库级 schema 复杂度向量；分量名与计算规则由 03 定义后复制到记录 |
| `world_signature` | string | `mongodb_data/<db_id>.json` 内容的稳定哈希；具体哈希算法由 03 给出 |
| `riv_certificate_ref` | string | 对应 RE 证书（逆向工程证书）的相对路径，例如 `audit/orchestra/99001/certificate.json`；字段名前缀 `riv_` 是历史命名，对应文档中统一称为"RE 证书 / 逆向工程证书"，构造与四问语义见 [03 §6](./03_dataset_construction.md#03-6) |
| `construction_origin` | enum | 取值 `{spider_synthetic, spider_remapped}`，标记该 record 的构造来源类别 |

### 2.3 强约束

- **省略而非 null**：任何字段不适用时，**应当省略 key**，不应写成 `null` 或 `""`。这保持 JSON 表面整洁，并防止下游误把 null 当作"已知不可用"。
- **`nl_queries` 长度严格为 5**：少 1 条或多 1 条都视为非法 record，不允许进入主集。
- **canonical 在 `nl_queries[0]`**：第 1 条为最清晰、最贴近业务用户口径的表达，供 prompt 模板与人工巡检默认使用；其余 4 条为同义改写。
- **`db_id` 一致性**：`record.db_id` 必须能在 `TEND/mongodb_schema/` 和 `TEND/mongodb_data/` 同时找到同名 `.json` 文件。
- **`MQL` 可执行性**：`record.MQL` 文本必须能被 mongosh 在加载了 `mongodb_data/<db_id>.json` 的实例上直接执行；输入输出契约见 [01 §1 任务的形式化](./01_task_definition.md#01-1)，结果归一化语义见 [01 §4 结果归一化契约](./01_task_definition.md#01-4) 与 [01 §5 递归相等](./01_task_definition.md#01-5)。
- **audit 字段全部可选**：未通过构造侧 audit 流程的候选已在 [03 数据集构造](./03_dataset_construction.md) 阶段被排除，根本不会出现在 `train.json` / `test.json`，因此在主集中看不到"audit 字段标记为 fail"这种情况。

---

<a id="02-3"></a>
## §3 库级资产规范

### 3.1 `mongodb_schema/<db_id>.json`

- 描述该库下每个 collection 的字段结构与类型；
- 顶层 key 是 collection 名；
- 字段值是字段类型字符串，或者嵌套子文档 / 嵌套数组；
- 字段类型至少包含：`INT, REAL, TEXT, BOOL, OBJECT, ARRAY`；
  - `OBJECT` 用 JSON dict 直接表示；
  - `ARRAY` 用 JSON list 包裹一个子结构表示，例如 `[{...}]`；
  - 支持任意层级嵌套；
- 同一 `db_id` 的所有 record 共享同一份 schema 文件；
- schema 文件**不允许**夹带任何"为评测服务"的隐藏 truth 字段（例如 gold pipeline、gold collection 列表等）——这些只能出现在 record 或 audit 中。

参考形态（节选自 `TEND/mongodb_schema/orchestra.json`）：

```json
{
  "conductor": {
    "Conductor_ID": "INT",
    "Name": "TEXT",
    "Age": "INT",
    "Nationality": "TEXT",
    "Year_of_Work": "INT",
    "orchestra": [
      {
        "Orchestra_ID": "INT",
        "Orchestra": "TEXT",
        "Conductor_ID": "INT",
        "Record_Company": "TEXT",
        "Year_of_Founded": "REAL",
        "Major_Record_Format": "TEXT",
        "performance": [
          {
            "Performance_ID": "INT",
            "Type": "TEXT",
            "Official_ratings_(millions)": "REAL",
            "show": [
              { "Show_ID": "INT", "If_first_show": "BOOL", "Attendance": "REAL" }
            ]
          }
        ]
      }
    ]
  }
}
```

### 3.2 `mongodb_data/<db_id>.json`

- 顶层 key 是 collection 名（与 schema 一致）；
- 每个 collection 是一个 JSON 数组，元素为该 collection 中的一个文档；
- 文档字段集合与 schema 中声明的字段一致；
- 公开发布前已脱敏（去除真实姓名 / 邮箱 / 实地址等敏感字段）；
- 数据规模足以让大多数 NLQ 在执行后返回非空、非平凡的结果。

### 3.3 schema 与 data 一致性约束

| 维度 | 规则 |
|---|---|
| collection 名 | data 中出现的 collection 集合 ⊆ schema 中声明的 collection 集合 |
| 字段名 | 文档中出现的字段名 ⊆ schema 中声明的字段名 |
| 字段类型 | 文档中字段的运行时类型必须能被 schema 声明的类型解释（整数 → `INT`、浮点 → `REAL`、字符串 → `TEXT`、布尔 → `BOOL`、dict → `OBJECT`、list → `ARRAY`） |
| 嵌套结构 | 文档中嵌套数组 / 子文档的形状必须能被 schema 中对应嵌套结构覆盖 |

补充：

- 数据中允许某些声明字段缺失（稀疏文档）；
- 不允许出现 schema 中未声明的字段；
- 嵌套数组允许为空 `[]`，但其元素若存在，则元素结构必须落入 schema 声明的子结构。

---

<a id="02-4"></a>
## §4 切分规则

### 4.1 切分单位

- **切分单位是 `db_id`**，不是 record；
- 同一 `db_id` 下的所有 record 必须落在同一侧（要么全在 train，要么全在 test）；
- 这是为了实现 cross-domain 评测：测试时模型见到的库（schema + data）在训练时从未出现。

### 4.2 切分比例

整体目标 record 比 ≈ 8 : 2；实际数字（论文锚定）：

| 桶 | record 数 |
|---|---|
| `TEND/train.json` | 14,245 |
| `TEND/test.json` | 2,775 |
| 合计（=`TEND/TEND.json`） | 17,020 |

### 4.3 domain 同侧聚合

- 库 id 的命名前缀作为 domain 信号，例如 `school_bus` / `school_finance` / `school_player` 共享 `school` 前缀；
- 共享同一 domain 前缀的库**尽量分配到同一侧**，以避免名义上的领域泄漏（test 见到的 schema 名字模式与 train 重叠）；
- 当某 domain 下库数极少、无法满足比例时，可允许跨侧个例，但需保证整体 8 : 2 比例不被打偏；
- 具体的 domain 提取算法与分配策略不在本文档范围内，由切分实现脚本承担；本文档只规定**结果属性**。

### 4.4 不引入的桶

| 概念 | 状态 |
|---|---|
| Held-out pool / horizon 桶 | **不引入**；`test.json` 直接承担 cross-domain 分布外评测职责 |
| sidecar / staging 多桶 | **不引入**；audit 留痕只在 `audit/` 子树内 |
| dev / val 切分 | 主集**不**单独切出 dev；如训练侧需要 dev，可从 train 内部再切，但这不属于发布物 |

---

<a id="02-5"></a>
## §5 规模与统计约定

| 指标 | 数值 |
|---|---|
| 数据库数（`db_id`） | 154 |
| 领域数（domain） | 105 |
| collection 总数 | 347 |
| record 总数（`(NLQ, NoSQL)` 对） | 17,020 |
| train record 数 | 14,245 |
| test record 数 | 2,775 |

衍生约束（任何发布版本都必须满足）：

- `len(TEND.json) == len(train.json) + len(test.json) == 17,020`；
- `len({record.db_id for record in TEND.json}) == 154`；
- `{record.db_id for record in train.json} ∩ {record.db_id for record in test.json} == ∅`；
- `len(os.listdir("TEND/mongodb_schema")) == 154` 且 `len(os.listdir("TEND/mongodb_data")) == 154`；
- 上述两个目录下的 `db_id` 文件名集合**完全相等**。

每条 record 满足 `len(record.nl_queries) == 5`，故 NLQ 总条数 = `17,020 × 5 = 85,100`。

---

<a id="02-6"></a>
## §6 canonical 示例

为了让 5 篇文档共用同一个具体例子，全套 proposals 选用 `orchestra` 库与下面这条 NLQ 作为 canonical 示例：

- canonical `db_id`：`orchestra`
- canonical NLQ：`"List the top 3 conductors with the most performances."`
- canonical `record_id`：`99001`

### 6.1 canonical record 的 JSON 形态

下面展示一条完整 record 的 JSON 形态。`nl_queries[1..4]`、`ref_sql`、`MQL` 的字面内容由 [03 数据集构造](./03_dataset_construction.md) 在构造时填入，本文档**只展示字段框架**：

```json
{
  "record_id": 99001,
  "db_id": "orchestra",
  "nl_queries": [
    "List the top 3 conductors with the most performances.",
    "<paraphrase 2>",
    "<paraphrase 3>",
    "<paraphrase 4>",
    "<paraphrase 5>"
  ],
  "ref_sql": "<spider SQL string>",
  "MQL": "<mongosh-executable find(...) or aggregate([...]) string>",
  "schema_complexity_profile": { "<dim>": "<value>" },
  "world_signature": "<stable hash of mongodb_data/orchestra.json>",
  "riv_certificate_ref": "audit/orchestra/99001/certificate.json",
  "construction_origin": "spider_remapped"
}
```

注意事项：

- 第 1 条 NLQ 是 canonical 表达，其余 4 条是 paraphrase；4 条 paraphrase 的具体文本不在 02 展开；
- `ref_sql` 与 `MQL` 的具体字面值不在 02 展开；
- `schema_complexity_profile` / `world_signature` / `riv_certificate_ref` / `construction_origin` 是 audit 字段，不参与 headline 评测；如果某 record 没有这些字段，按 [§2.3](#02-2) 应当省略 key 而不是写 `null`。

### 6.2 canonical 库的 schema 形态

`orchestra` 的 schema 见 `TEND/mongodb_schema/orchestra.json`，结构如下：

- 顶层 collection：`conductor`；
- 一级嵌套数组：`conductor.orchestra`（每个 conductor 关联多个 orchestra）；
- 二级嵌套数组：`conductor.orchestra.performance`；
- 三级嵌套数组：`conductor.orchestra.performance.show`；
- 字段类型覆盖 `INT, REAL, TEXT, BOOL, ARRAY, OBJECT`。

这一形态符合 [§3](#02-3) 给出的 schema 规范，可作为后续文档的演示库。

### 6.3 canonical 库的 data 形态

`orchestra` 的 data 见 `TEND/mongodb_data/orchestra.json`，顶层结构：

```json
{
  "conductor": [
    { "Conductor_ID": 1, "Name": "...", "...": "...", "orchestra": [ /* ... */ ] },
    { "Conductor_ID": 2, "...": "..." }
  ]
}
```

- 顶层 key 与 schema 一致（`conductor`）；
- 嵌套字段名与类型与 schema 一致；
- 数据集中包含足够多的 conductor / orchestra / performance 实例，足以支持 canonical NLQ "top 3 conductors with the most performances" 在执行后返回非平凡结果。

---

<a id="02-7"></a>
## §7 与其它 SSoT 文档的边界

本文档**只**定义数据资产、record schema、目录组织、切分规则、规模数字。下列内容**不在本文档范围内**，需查阅对应文档：

| 关注点 | 由谁定义 |
|---|---|
| 任务的 IO 形式化（输入 NLQ + schema，输出 MQL 字符串） | [01 §1 任务的形式化](./01_task_definition.md#01-1) |
| 结果归一化（`≡_rec` 的语义、字段顺序无关、bag/set 等） | [01 §4 结果归一化契约](./01_task_definition.md#01-4) + [01 §5 递归相等](./01_task_definition.md#01-5) |
| RE 证书（逆向工程证书）的内部字段、构造流水线、audit 准入算法 | [03 数据集构造](./03_dataset_construction.md) |
| `schema_complexity_profile` 与 `world_signature` 的具体计算 | [03 数据集构造](./03_dataset_construction.md) |
| 评测指标公式 EM / QSM / QFC / EX / EFM / EVM | [04 评测方法](./04_evaluation_methodology.md) |
| 模型方法、SLM / RAG / debug agent 架构 | [05 方法设计](./05_solution_design.md) |

任何与上述边界冲突的描述，以对应 SSoT 文档为准；本文档不重复，也不覆盖。
