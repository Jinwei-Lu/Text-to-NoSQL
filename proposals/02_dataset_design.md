# TEND Benchmark · 02 数据资产设计 (Dataset Design)

<a id="02-0"></a>
## §0 摘要

本文档定义 TEND benchmark 的数据资产层（dataset assets），作为数据资产的单一可信源（SSoT）。涵盖五项主题：发布物文件构成与目录布局、单条 record 的字段集与字段语义、库级 schema 与 data 的格式约束与一致性、train/test 切分规则、覆盖目标与配额机制。

边界声明：TEND 的设计文档由 6 份 SSoT 编排——[01](./01_task_definition.md) 任务定义、[02](./02_dataset_design.md) 数据资产设计（本文）、[03](./03_database_synthesis.md) 数据库合成、[04](./04_dataset_construction.md) 数据集构造、[05](./05_evaluation_methodology.md) 评测方法、[06](./06_solution_design.md) 方法设计。任务 IO 形式化、正确性锚、结果归一化契约与递归相等 ≡_rec、Instance 正确性根原则 P1-P4 由 [01](./01_task_definition.md) 给出；库级 Agentic 数据库合成（Agent 架构、三控制线、Taxonomy Board、6 层 Noise Taxonomy、Business Simulator、业务叙事、Schema Evolution Simulator、F_topology 特性集合）由 [03](./03_database_synthesis.md) 给出；Agentic 合成产物汇入、Structured Intent DSL（含 `nosql_nativeness` 与 `canonical_form_set` 顶层字段）、规整化与汇入、SI 自动派生、Gold MQL 生成、NLQ × 5 写作、23 个 intent pattern 族、V1'-V7' spec-grounded / SQL-bridge defeat 验证、RP_diff 经验难度校准、嵌入覆盖审计与路由由 [04](./04_dataset_construction.md) 给出；7 评测指标（EM / QSM / QFC / EX / EFM / EVM / QIM）公式与协议、7 比特指纹、强制披露清单由 [05](./05_evaluation_methodology.md) 给出；SMART 4 阶段方法架构与求解侧硬边界屏蔽清单由 [06](./06_solution_design.md) 给出。本文档只注册字段名、文件路径、切分契约与覆盖配额机制，不重复上述语义。

全局不变量：本 benchmark 所有 record 的库级资产（`mongodb_schema/<db_id>.json` 与 `mongodb_data/<db_id>.json`）均由 [03](./03_database_synthesis.md) Agentic 合成管线唯一产出；不存在其他来源。

读者路线图：

| 角色 | 推荐阅读顺序 |
|---|---|
| 模型作者（model authors） | §0 → §2.1 必填字段 → §3 → §6 |
| 评测者（evaluator） | §0 → §1 → §2 → §4 → §5 |
| 数据构造者（data builder） | §0 → §1 → §2 → §3 → §5 → §6 → §7 |

整体规模锁定：154 db、105 domain、347 collection、17,020 (NLQ, NoSQL) record pair；切分锁定 14,245 train / 2,775 test，cross-domain 8:2 比例；NLQ 文本总条数 $= 17{,}020 \times 5 = 85{,}100$。

<a id="02-1"></a>
## §1 资产清单与目录布局

### §1.1 资产清单

| 资产类 | 路径 | 粒度 | 主要用途 |
|---|---|---|---|
| 主集训练桶 | `TEND/train.json` | 整集 | 模型训练唯一允许暴露的 record 集合 |
| 主集测试桶 | `TEND/test.json` | 整集 | headline 评测唯一调用的 record 集合 |
| 完整集合（可选） | `TEND/TEND.json` | 整集 | train ∪ test 全集快照，便于一致性核查 |
| 库级 schema | `TEND/mongodb_schema/<db_id>.json` | 库级 | 描述该库下每个 collection 的字段结构与类型 |
| 库级 canonical data | `TEND/mongodb_data/<db_id>.json` | 库级 | 单世界发布的实际文档数据，MQL 在其上执行 |
| Taxonomy Board 快照 | `audit/taxonomy_board/board_snapshot_*.json` | 全局 | Taxonomy Board 快照（来自 03 合成管线） |
| Stratified Budget Matrix | `audit/taxonomy_board/budget_matrix.json` | 全局 | Stratified Budget Matrix 留痕 |
| 嵌入覆盖审计 | `audit/coverage/coverage_report.json` | 全局 | facility-location 覆盖度量、cell 实际填充率、9 语义轴直方图 |
| RP_diff 参考面板 manifest | `audit/reference_panel/diff_panel_manifest.json` | 全局 | V6' 经验难度校准用的 5 frozen 模型与运行配置 |
| SQL-bridge 参考面板 manifest | `audit/reference_panel/sql_bridge_manifest.json` | 全局 | V7' SQL-bridge defeat 面板，记录 NL2SQL panel 与 sqltomongo translator 的模型与运行配置，锁定与 V3'/V5'/V6' 的三方 disjoint 约束 |
| 5% 人审 anchor 报告 | `audit/human_anchor/spot_audit.json` | 全局 | 抽样人审与机审一致性留痕 |
| Structured Intent | `audit/<db_id>/<record_id>/structured_intent.yaml` | record 级 | 构造期 SI DSL 实例 |
| 自动派生 oracle | `audit/<db_id>/<record_id>/derived/oracle.py` | record 级 | 由 SI 自动派生的参考实现 |
| 自动派生 checker | `audit/<db_id>/<record_id>/derived/checker.py` | record 级 | 结果归一化后的相等性判定器 |
| 自动派生 mutations | `audit/<db_id>/<record_id>/derived/mutations.json` | record 级 | 故意构造的负样本扰动集合 |
| 自动派生 canonical_form_set | `audit/<db_id>/<record_id>/derived/canonical_form_set.json` | record 级 | 由 SI 派生的 canonical_form_set 四元组（`must_contain` / `must_not_contain` / `must_contain_at_root` / `must_not_contain_at_root`），为评测层 QIM 提供结构约束源 |
| 多世界备选 | `audit/<db_id>/<record_id>/world_variants/<world_id>.json` | record 级 | K=2 候选世界中未被选为 canonical 的备选数据，仅 audit 可见 |
| 多世界鲁棒性证书 | `audit/<db_id>/<record_id>/world_robustness.json` | record 级 | gold MQL 在 K 个候选世界上的通过情况 |
| V1'-V7' 证书 | `audit/<db_id>/<record_id>/certificate.json` | record 级 | spec-grounded 与 SQL-bridge defeat 验证的七项语义留痕 |
| V6' 经验难度结果 | `audit/<db_id>/<record_id>/empirical_difficulty.json` | record 级 | RP_diff 5 frozen 模型 per-model EX 结果 |
| V7' SQL-bridge defeat 结果 | `audit/<db_id>/<record_id>/sql_bridge_defeat.json` | record 级 | V7' 在该 record 上的 SQL-bridge 候选 MQL、EX 与 QIM 判定、分类标签（`accepted` / `sql_trivial` / `sql_bridge_defeat_partial`） |
| 噪声注入追溯 | `audit/<db_id>/<record_id>/noise_trace.json` | record 级 | 本条 record 注入的噪声追溯（层 / type_id / target field / coupling operator / si policy key） |
| 复杂度向量实测 | `audit/<db_id>/<record_id>/complexity_vector.json` | record 级 | 6 维复杂度向量 $\vec{C}$ 实测值 |
| 业务叙事 | `audit/<db_id>/<record_id>/business_narrative.json` | record 级 | Agentic 合成时 Domain Architect 写下的业务画像与事件流概要 |
| Audit 拒绝 | `audit/rejected/<db_id>/<record_id>.json` | record 级 | 未通过 V1'-V7' 的候选拒绝原因留痕 |

### §1.2 目录树

```
TEND/
├── train.json
├── test.json
├── TEND.json
├── mongodb_schema/
│   ├── orchestra.json
│   ├── ...
│   └── <db_id>.json
└── mongodb_data/
    ├── orchestra.json
    ├── ...
    └── <db_id>.json

audit/
├── taxonomy_board/
│   ├── board_snapshot_*.json
│   └── budget_matrix.json
├── coverage/
│   └── coverage_report.json
├── reference_panel/
│   ├── diff_panel_manifest.json
│   └── sql_bridge_manifest.json
├── human_anchor/
│   └── spot_audit.json
├── rejected/
│   └── <db_id>/
│       └── <record_id>.json
└── <db_id>/
    └── <record_id>/
        ├── structured_intent.yaml
        ├── derived/
        │   ├── oracle.py
        │   ├── checker.py
        │   ├── mutations.json
        │   └── canonical_form_set.json
        ├── world_variants/
        │   └── <world_id>.json
        ├── world_robustness.json
        ├── certificate.json
        ├── empirical_difficulty.json
        ├── sql_bridge_defeat.json
        ├── noise_trace.json
        ├── complexity_vector.json
        └── business_narrative.json
```

### §1.3 主集与 audit 的边界

- `TEND/train.json` 与 `TEND/test.json` 是唯一两类参与 headline 评测的 record 桶。
- `audit/` 子树不进入 train、不进入 test、不进入 headline 评测，仅作为 spec-grounded 验证、SQL-bridge defeat 留痕、经验难度校准、覆盖审计、噪声追溯、复杂度实测、业务叙事与人审 anchor 的留痕载体。
- 主集不划分为额外子桶（无 dev / sidecar / horizon 多桶）。
- 发布层 `TEND/mongodb_data/<db_id>.json` 永远是单世界：每个 `db_id` 在该目录下只有一份 data 文件，对应构造期从 K=2 个候选世界中按既定标准选定的 canonical world；K-1 个备选变体存放于 `audit/<db_id>/<record_id>/world_variants/`，仅 audit 可见。
- `audit/reference_panel/sql_bridge_manifest.json` 与每条 record 的 `audit/<db_id>/<record_id>/derived/canonical_form_set.json`、`audit/<db_id>/<record_id>/sql_bridge_defeat.json` 同属 audit 留痕，不进入主集评测。

<a id="02-2"></a>
## §2 record schema 字段定义

### §2.1 必填字段

| 字段 | 类型 | 语义 |
|---|---|---|
| `record_id` | int | 全局唯一记录标识，TEND.json 内部不重复 |
| `db_id` | string | 与 `TEND/mongodb_schema/<db_id>.json` 和 `TEND/mongodb_data/<db_id>.json` 的文件名严格一致 |
| `nl_queries` | list[string] | 长度严格为 5；`nl_queries[0]` 永远是 L1 canonical 表达——schema_naive、业务用户口吻、不暴露 schema 细节；其它槽位的层级映射记录在 audit 字段 `nlq_specificity_levels` |
| `MQL` | string | gold MongoDB 查询；`mongosh` 可直接执行的 `find(...)` 或 `aggregate([...])` |

### §2.2 audit 字段（全部可选）

| 字段 | 类型 | 语义 |
|---|---|---|
| `structured_intent_ref` | string | 指向 `audit/<db_id>/<record_id>/structured_intent.yaml`；该 yaml 内含构造期的 canonical Structured Intent，DSL 形态（含顶层 `nosql_nativeness` 与 `canonical_form_set` 字段）由 [04 §3](./04_dataset_construction.md#04-3) 定义 |
| `re_certificate_ref` | string | 指向 `audit/<db_id>/<record_id>/certificate.json`；V1'-V7' spec-grounded / SQL-bridge defeat 验证证书的留痕，七项语义见 [04 §8](./04_dataset_construction.md#04-8) |
| `world_robustness_certificate_ref` | string | 指向 `audit/<db_id>/<record_id>/world_robustness.json`；记录 gold 在 K 个候选世界上的通过情况 |
| `empirical_difficulty_ref` | string | 指向 `audit/<db_id>/<record_id>/empirical_difficulty.json`；V6' RP_diff 5 frozen 模型的 per-model EX 结果 |
| `noise_trace_ref` | string | 指向 `audit/<db_id>/<record_id>/noise_trace.json`；6 层噪声分布与 gold MQL coupling 算子留痕，语义由 [03 §5](./03_database_synthesis.md#03-5) 定义 |
| `complexity_vector_ref` | string | 指向 `audit/<db_id>/<record_id>/complexity_vector.json`；6 维复杂度向量 $\vec{C}$，语义由 [03 §3](./03_database_synthesis.md#03-3) 定义 |
| `business_narrative_ref` | string | 指向 `audit/<db_id>/<record_id>/business_narrative.json`；Agentic 合成时的业务画像与驱动事件流概要，语义由 [03 §6](./03_database_synthesis.md#03-6) 定义 |
| `canonical_form_set_ref` | string | 指向 `audit/<db_id>/<record_id>/derived/canonical_form_set.json`；由 SI 派生的 canonical_form_set 四元组（`must_contain` / `must_not_contain` / `must_contain_at_root` / `must_not_contain_at_root`），评测层 [05 §1.8](./05_evaluation_methodology.md#05-1-8) 的 QIM 指标消费该资产 |
| `sql_bridge_defeat_ref` | string | 指向 `audit/<db_id>/<record_id>/sql_bridge_defeat.json`；V7' SQL-bridge defeat 的 record 级留痕（SQL-bridge 候选 MQL、EX 与 QIM 判定、分类标签），语义由 [04 §8.6](./04_dataset_construction.md#04-8) 定义 |
| `target_difficulty` | enum | 取值 ∈ {easy, medium, hard, expert}；构造期声明的目标难度桶 |
| `empirical_difficulty` | enum | 取值 ∈ {easy, medium, hard, expert}；由 RP_diff `pass_rate` 实测分桶得到，分桶规则由 [04 §9](./04_dataset_construction.md#04-9) 给出 |
| `pass_rate` | float ∈ [0, 1] | RP_diff 5 frozen 模型在该 record 上 EX = 1 的比例 |
| `tds_cell` | string | TDS 网格 cell 标识，形如 `"<schema_topology> × <operator_family> × <difficulty> × <nlq_specificity> × <language>"`；`schema_topology` 允许多特性组合（例如 `nested_4_deep+sparse_embedded`），取自 [03 §4.1](./03_database_synthesis.md#03-4) 的 $\mathcal{F}_{topo}$ 特性集合；本字段是事后描述符——构造期由 [04](./04_dataset_construction.md) 的嵌入覆盖路由决定 record 是否准入，cell 标签只在 record 落地后填写以便评测端按 cell 聚合 |
| `operator_family` | string | gold MQL 的主算子族标签；覆盖 23 个 pattern（含 14 个基础算子族 + 9 个 NoSQL-native 算子族），族表由 [04 §3.2](./04_dataset_construction.md#04-3) 维护 |
| `nosql_nativeness_level` | enum | 取值 ∈ {L0, L1, L2, L3, L4}；构造期声明的意图 NoSQL 原生度，L0 为 SQL-equivalent、L1 为 structure-aware、L2 为 type-aware、L3 为 schema-dynamic、L4 为 NoSQL-exclusive；语义由 [04 §3.1](./04_dataset_construction.md#04-3) 与 [04 §3.2](./04_dataset_construction.md#04-3) 定义 |
| `idiomatic_score` | float ∈ [0, 1] | 保留为 audit 描述性度量，刻画 gold MQL 的 NoSQL-idiomatic 程度；公式由 [04](./04_dataset_construction.md) 给出 |
| `nlq_specificity_levels` | list[string] | 长度严格 5；每位取值 ∈ {L0, L1, L2, L3, L4}；约束见 §2.3 |
| `schema_complexity_profile` | dict | 库级 schema 复杂度向量；10 个分量（含 `polymorphic_collection_count`、`mixed_embed_ref_count`、`sparse_embedded_rate` 等拓扑特性计数）名与计算规则由 [04 §4.4](./04_dataset_construction.md#04-4) 定义 |
| `world_signature` | string | 已发布 canonical world——即 `mongodb_data/<db_id>.json`——的稳定哈希；算法由 [04](./04_dataset_construction.md) 给出 |
| `coverage_neighbors` | list[int] | 该 record 在嵌入空间中的最近邻 record_id 列表，用于多样性事后审计；编码与距离度量由 [04 §10](./04_dataset_construction.md#04-10) 定义 |

### §2.3 强约束

- **省略而非 null**：任何字段不适用时省略 key，不写 `null` 也不写空串。
- **`nl_queries` 长度严格为 5**：少 1 条或多 1 条都视为非法 record，不允许进入主集。
- **`nl_queries[0]` 永远是 L1 canonical 表达**：schema_naive、业务用户口吻、不暴露 collection 名或文档结构。
- **`nlq_specificity_levels` 是 {L0, L1, L2, L3, L4} 的排列**：必须满足 `nlq_specificity_levels[0] == "L1"`，且 5 个层级各出现恰好 1 次。
- **`db_id` 一致性**：`record.db_id` 必须能在 `TEND/mongodb_schema/` 与 `TEND/mongodb_data/` 同时找到同名 `.json` 文件。
- **MQL 可执行性**：`record.MQL` 文本必须能被 `mongosh` 在加载了 `mongodb_data/<db_id>.json` 的实例上直接执行；任务签名见 [01 §1](./01_task_definition.md#01-1)，正确性锚见 [01 §3](./01_task_definition.md#01-3)，结果归一化语义见 [01 §4](./01_task_definition.md#01-4) 与 [01 §5](./01_task_definition.md#01-5)。
- **`nosql_nativeness_level` 与 SI 字面一致**：record 的 `nosql_nativeness_level` 必须与该 record 在 `structured_intent.yaml` 顶层 `nosql_nativeness.level` 字段字面一致，构造期由 [04 §3.1](./04_dataset_construction.md#04-3) 规定的 SI 序列化器强制。
- **`canonical_form_set` 的 QIM 可消费性**：record 的 `canonical_form_set_ref` 所指 JSON 的结构必须能被 QIM 的 `AST_check` 算子机械消费，四元组键名固定为 `must_contain` / `must_not_contain` / `must_contain_at_root` / `must_not_contain_at_root`，取值为 MQL operator token 列表；定义见 [05 §1.8](./05_evaluation_methodology.md#05-1-8)。
- **audit 字段全部可选**：未通过 V1'-V7' 验证的候选已在 [04 §8](./04_dataset_construction.md#04-8) 阶段被排除（落入 `audit/rejected/`），不会出现在 `train.json` / `test.json`，因此主集中看不到 "audit 字段标记为 fail" 的情况。

<a id="02-3"></a>
## §3 库级资产规范

### §3.1 `TEND/mongodb_schema/<db_id>.json`

- 描述该库下每个 collection 的字段结构与类型。
- 顶层 key 是 collection 名。
- 字段值是字段类型字符串，或嵌套子文档/数组。
- 字段类型至少包含：`INT`、`REAL`、`TEXT`、`BOOL`、`OBJECT`、`ARRAY`；`OBJECT` 用 JSON dict 直接表示；`ARRAY` 用 JSON list 包裹一个子结构表示，例如 `[{...}]`；支持任意层级嵌套。
- 同一 `db_id` 的所有 record 共享同一份 schema 文件。
- schema 文件不允许夹带任何为评测服务的隐藏 truth 字段。
- schema 支持 $\mathcal{F}_{topo}$ 特性集合中的各种拓扑（`flat` / `nested_N_deep` / `polymorphic_collection` / `dynamic_key_document` / `sparse_embedded` / `mixed_embed_ref` / `intentional_denormalization`），具体表达形式由 [03 §4.1](./03_database_synthesis.md#03-4) 定义；每个 db 的 schema 特性集在构造期登记于该库 record 的 `schema_complexity_profile` 中。

`orchestra` schema 参考形态（节选）：

```json
{
  "conductor": {
    "Conductor_ID": "INT",
    "Name": "TEXT",
    "Age": "INT",
    "Nationality": "TEXT",
    "orchestra": [
      {
        "Orchestra_ID": "INT",
        "Orchestra_Name": "TEXT",
        "Year_of_Founded": "INT",
        "performance": [
          {
            "Performance_ID": "INT",
            "Date": "TEXT",
            "Type": "TEXT",
            "show": [
              {
                "Show_ID": "INT",
                "Result": "TEXT",
                "Attendance": "INT"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

### §3.2 `TEND/mongodb_data/<db_id>.json`

- 顶层 key 是 collection 名（与 schema 一致）。
- 每个 collection 是一个 JSON 数组，元素为该 collection 的文档。
- 文档字段集合与 schema 中声明的字段一致。
- 公开发布前已脱敏。
- 数据规模足以让 NLQ 在执行后返回非空、非平凡的结果（最小 doc 数下界由 [04 §4](./04_dataset_construction.md#04-4) 按 difficulty 分层给出）。

### §3.3 schema 与 data 一致性约束

| 维度 | 约束 |
|---|---|
| collection 名 | `mongodb_data/<db_id>.json` 顶层 key 集合 ⊆ `mongodb_schema/<db_id>.json` 顶层 key 集合 |
| 字段名 | data 文档中出现的任何字段必须在 schema 中已声明 |
| 字段类型 | data 中字段值类型必须与 schema 声明类型一致（INT / REAL / TEXT / BOOL / OBJECT / ARRAY） |
| 嵌套结构 | 任意层级的嵌套子文档与嵌套数组结构必须与 schema 同构 |

补充约束：

- **稀疏文档允许**：单个文档可省略 schema 声明的某些字段（不强制 schema 中所有字段都在每条 doc 上出现）；此种 sparse 分布计入 `schema_complexity_profile.sparse_embedded_rate`。
- **不允许 schema 未声明字段**：data 文档不得引入 schema 中未声明的字段。
- **嵌套数组允许 `[]`**：嵌套数组可为空，但若非空，每个元素的结构必须落入 schema 同构。

### §3.4 单世界发布与多世界 audit 的关系

发布层 `TEND/mongodb_data/<db_id>.json` 是单世界产物（每个 `db_id` 在该目录下仅一份）。该 canonical world 由 Agentic 合成管线（[03](./03_database_synthesis.md)）统一产出，经 [04](./04_dataset_construction.md) 的汇入与校验后发布；资产层接口契约（schema / data / 噪声种子 / 复杂度向量）由 [03](./03_database_synthesis.md) 与 [04](./04_dataset_construction.md) 共同约束。构造期从 K=2 个候选世界中按既定标准选定其中一份为 canonical 发布；其余 K-1 个备选变体存放于 `audit/<db_id>/<record_id>/world_variants/<world_id>.json`，仅 audit 可见，不进入 headline 评测。gold MQL 在 K 个候选世界上的通过情况由 `audit/<db_id>/<record_id>/world_robustness.json` 留痕。

<a id="02-4"></a>
## §4 切分规则

### §4.1 切分单位

切分单位是 `db_id`：同一 `db_id` 下的所有 record 必须落在同一侧（要么全部进 train、要么全部进 test）。该约束保证测试集对训练集是 cross-domain 的，模型在测试时不会见过该库的任何 schema 或 data。

### §4.2 切分比例

- 整体 record 比例为 8:2，对应锁定数字 14,245 train / 2,775 test。
- 由于切分单位是 `db_id` 而 record 数在不同 `db_id` 之间分布不均匀，实际切分由分配脚本在 `db_id` 粒度上做最小偏差搜索，使 record 数比例最接近 8:2。

### §4.3 domain 同侧聚合

`db_id` 命名前缀作为 domain 信号；同一 domain 下的多个 `db_id` 尽量同侧聚合，进一步降低 train 与 test 之间的近邻泄漏。具体聚合算法由切分实现脚本承担，本文档只规定结果属性：105 个 domain 在 train/test 之间不出现 domain 跨侧分裂的前提下，再按 §4.2 的比例约束做 db 级分配。

### §4.4 不引入的桶

- 不引入 held-out / horizon 桶。
- 不引入 sidecar / staging 多桶。
- 主集不单独切出 dev 子集；模型作者若需要内部验证集，应自行在 `train.json` 的 `db_id` 子集上构建。
- 不引入多世界发布桶（每个 `db_id` 在 `mongodb_data/` 下只有一份 canonical data）。

<a id="02-5"></a>
## §5 覆盖目标与配额机制

本文档不写入具体规模数字。规模由构造管线按 [04](./04_dataset_construction.md) 的 Agentic 合成产物汇入 + 嵌入覆盖路由产生，构造完成后由发布脚本写入 `audit/coverage/coverage_report.json`。

### §5.1 单一来源声明

TEND benchmark 全集 100% record 的库级资产（schema 与 data）均由 [03](./03_database_synthesis.md) 定义的 Agentic 合成管线唯一产出；不存在其他来源，不设来源配额。Agentic 合成内部的多样性由 [03 §4](./03_database_synthesis.md#03-4) 的 Diversity Scheduler 在 `T_domain / T_pattern / T_topology / T_operator_family / T_difficulty / T_nosql_feature_mix / T_noise_mix / T_nosql_nativeness / T_topology_features` 共 9 个覆盖轴上按 Stratified Budget Matrix 调度，留痕于 `audit/taxonomy_board/budget_matrix.json`。

### §5.2 嵌入覆盖目标

写入 `audit/coverage/coverage_report.json`：

- 每条 record 的嵌入由 schema embedding + intent embedding + query AST embedding 三段拼接（具体编码管线见 [04 §10](./04_dataset_construction.md#04-10)）。
- 数据集层覆盖度量：facility-location 覆盖（每条 record 到其在已落地集合中最近邻的距离之和）。
- 准入目标：每条新 record 必须使数据集 facility-location 覆盖度量提升 $\geq \varepsilon$，或落入 under-coverage 区域。
- `coverage_neighbors` 字段：每条 record 落地后写入其在嵌入空间中的 8 个最近邻 `record_id`。

### §5.3 衍生约束（任何发布版本都必须满足）

$$
\text{len}(\text{TEND.json}) = \text{len}(\text{train.json}) + \text{len}(\text{test.json})
$$

$$
\{r.\text{db\_id} \mid r \in \text{train.json}\} \cap \{r.\text{db\_id} \mid r \in \text{test.json}\} = \varnothing
$$

$$
\text{len}(\text{os.listdir}(\text{TEND/mongodb\_schema})) = \text{len}(\text{os.listdir}(\text{TEND/mongodb\_data}))
$$

- 上述两个目录下的 `db_id` 文件名集合完全相等。
- $\{r.\text{db\_id} \mid r \in \text{TEND.json}\} = \{\text{basename}(f) \mid f \in \text{os.listdir}(\text{TEND/mongodb\_schema})\}$。
- 每条 record 满足 $\text{len}(r.\text{nl\_queries}) = 5$，因此 NLQ 总条数 $= 17{,}020 \times 5 = 85{,}100$。
- 每条 record 的 `nlq_specificity_levels` 是 {L0, L1, L2, L3, L4} 的排列，且 `nlq_specificity_levels[0] == "L1"`。
- 每条 record 的 `nosql_nativeness_level` ∈ {L0, L1, L2, L3, L4}；test 集上 L2+（即 L2 ∪ L3 ∪ L4）占比 $\geq 40\%$、L4 占比 $\geq 15\%$，配额来自 [04 §3.1](./04_dataset_construction.md#04-3) 的构造期预算。

### §5.4 覆盖审计

构造完成后，发布脚本对照 §5.1 与 §5.2 的目标计算 cell 实际填充率与嵌入覆盖度量，写入 `audit/coverage/coverage_report.json`。覆盖审计为后续构造迭代提供反馈信号；具体迭代算法（under-coverage cell 的补样优先级、嵌入空间分桶策略）见 [04 §10](./04_dataset_construction.md#04-10)。

### §5.5 T_noise_mix 多样性轴

数据集层语义覆盖审计共 9 个轴；前 7 个列于下表，后 2 个在 §5.6 给出：

| 轴 | 含义 |
|---|---|
| `T_domain` | 领域分布（105 个 domain 的实际 record 计数） |
| `T_pattern` | intent pattern 族分布（23 个 pattern，含 14 个基础 + 9 个 NoSQL-native） |
| `T_topology` | schema topology 深度标签分布（`flat` / `nested_2_deep` / `nested_3_deep` / `nested_4_deep` / `nested_5_plus_deep`） |
| `T_operator_family` | MQL 主算子族分布 |
| `T_difficulty` | `empirical_difficulty` 四桶（easy / medium / hard / expert）分布 |
| `T_nosql_feature_mix` | 文档 NoSQL-native 特性（嵌套数组 / polymorphic / denormalization / partial index 适用性 / …）的混合分布 |
| `T_noise_mix` | 6 层噪声在 record 级的分布 |

第 7 轴 `T_noise_mix` 刻画 [03 §5](./03_database_synthesis.md#03-5) 定义的噪声 6 层（Literal / Structural / Semantic / Historical / Pollution / Type-Polymorphism）在数据集上的覆盖度。每条 record 的 `noise_trace_ref` 指向 `audit/<db_id>/<record_id>/noise_trace.json`，其中记录本条 record 的实际噪声注入层、`type_id`、target field、gold MQL 中的 coupling operator 与 SI 策略键。

其中第 6 层 Type-Polymorphism 专门针对 BSON/NoSQL 的类型多态现象，由 [03 §5.1](./03_database_synthesis.md#03-5) 定义；该层对应 6 个 `tp_*` type_id（`tp_union_payment` / `tp_numeric_string_mix` / `tp_array_or_scalar` / `tp_nested_vs_flat` / `tp_typed_vs_untyped` / `tp_decimal_vs_double`，见 [03 §A](./03_database_synthesis.md#03-A)），其 coupling operators 取自 `{$switch on $type, $convert, $type, $isNumber, $getField}`。构造期由 [03](./03_database_synthesis.md) 的 Taxonomy Board 按 Stratified Budget Matrix 调度各层覆盖下界，目标是每一层噪声在 train 与 test 上各出现不低于下界（具体下界由 Taxonomy Board 维护，落入 `audit/taxonomy_board/budget_matrix.json`）。

数据集落地后，审计脚本对全集、train 与 test 三个视图分别做 `T_noise_mix` 直方图统计，归集至 `audit/coverage/coverage_report.json` 的 `taxonomy_axes.T_noise_mix` 子块，供后续构造迭代的补样反馈。其余 6 轴同理归集至 `taxonomy_axes.T_domain` / `taxonomy_axes.T_pattern` / … / `taxonomy_axes.T_nosql_feature_mix`。

### §5.6 T_nosql_nativeness 与 T_topology_features 覆盖轴

数据集层覆盖审计的第 8、9 个轴专门刻画 NoSQL-Exclusive 维度：

| 轴 | 含义 |
|---|---|
| `T_nosql_nativeness` | `nosql_nativeness_level` 在 record 级的 5 档分布（L0 / L1 / L2 / L3 / L4） |
| `T_topology_features` | $\mathcal{F}_{topo}$ 特性集合 $\mathcal{F}_{topo} \subseteq \{\texttt{flat, nested\_N\_deep, polymorphic\_collection, dynamic\_key\_document, sparse\_embedded, mixed\_embed\_ref, intentional\_denormalization}\}$ 在 db 级的分布 |

第 8 轴 `T_nosql_nativeness`：每条 record 声明的 NoSQL 原生度档位 `nosql_nativeness_level` 构成该轴的随机变量，取值空间为 {L0, L1, L2, L3, L4}。构造期目标在 test 集上 L2+ 占比 $\geq 40\%$、L4 占比 $\geq 15\%$，目标来自 [04 §3.1](./04_dataset_construction.md#04-3) 的构造期预算。审计脚本对全集、train 与 test 三个视图分别做直方图归集至 `audit/coverage/coverage_report.json` 的 `taxonomy_axes.T_nosql_nativeness` 子块。

第 9 轴 `T_topology_features`：每个 db 声明的 $\mathcal{F}_{topo}$ 特性集合 $\mathcal{F}_{topo} \subseteq \{\texttt{flat, nested\_N\_deep, polymorphic\_collection, dynamic\_key\_document, sparse\_embedded, mixed\_embed\_ref, intentional\_denormalization}\}$（其中 `nested_N_deep` 按 $N \in \{2, 3, 4, 5+\}$ 展开为 4 个具体标签）。该集合是多标签的——单个 db 可同时承载多个特性。采样下限由难度决定：easy 至少携带 1 个特性、medium 至少 2 个、hard 至少 3 个、expert 至少 4 个。审计脚本在 db 级做特性命中频次直方图，归集至 `audit/coverage/coverage_report.json` 的 `taxonomy_axes.T_topology_features` 子块；语义由 [03 §4.1](./03_database_synthesis.md#03-4) 与 [04 §4.4](./04_dataset_construction.md#04-4) 定义。

Stratified Budget Matrix 对上述两轴的覆盖下界同样由 Taxonomy Board 维护，落入 `audit/taxonomy_board/budget_matrix.json` 的对应子块。

<a id="02-6"></a>
## §6 canonical 示例

本节给出一个完整、自洽的示例 record，所有字段值与共享契约字面一致，可作为字段格式与跨字段一致性的对照基准。

### §6.1 canonical record JSON

```json
{
  "record_id": 99001,
  "db_id": "orchestra",
  "nl_queries": [
    "For each conductor, attach a total_performances field counting all performances across their orchestras, while preserving the original conductor document structure.",
    "Add performance totals to conductors.",
    "For each conductor document in the conductor collection, add a field total_performances equal to the total count of entries in the embedded orchestra.performance arrays, without flattening the document.",
    "For each conductor document, augment with a top-level total_performances field aggregating the sizes of nested performance arrays; preserve the embedded orchestra-performance-show array structure.",
    "在每位指挥家的文档上附加 total_performances 字段，记录其旗下所有乐团的演出总数，并保持原文档的嵌套结构不变。"
  ],
  "MQL": "db.conductor.aggregate([{ $addFields: { total_performances: { $sum: { $map: { input: { $ifNull: [\"$orchestra\", []] }, as: \"orch\", in: { $size: { $ifNull: [\"$$orch.performance\", []] } } } } } } }])",
  "structured_intent_ref": "audit/orchestra/99001/structured_intent.yaml",
  "re_certificate_ref": "audit/orchestra/99001/certificate.json",
  "world_robustness_certificate_ref": "audit/orchestra/99001/world_robustness.json",
  "empirical_difficulty_ref": "audit/orchestra/99001/empirical_difficulty.json",
  "noise_trace_ref": "audit/orchestra/99001/noise_trace.json",
  "complexity_vector_ref": "audit/orchestra/99001/complexity_vector.json",
  "business_narrative_ref": "audit/orchestra/99001/business_narrative.json",
  "canonical_form_set_ref": "audit/orchestra/99001/derived/canonical_form_set.json",
  "sql_bridge_defeat_ref": "audit/orchestra/99001/sql_bridge_defeat.json",
  "target_difficulty": "medium",
  "empirical_difficulty": "medium",
  "pass_rate": 0.6,
  "tds_cell": "nested_4_deep+sparse_embedded × shape_preserving_augment × medium × schema_naive × english",
  "operator_family": "shape_preserving_augment",
  "nosql_nativeness_level": "L4",
  "idiomatic_score": 0.92,
  "nlq_specificity_levels": ["L1", "L0", "L2", "L3", "L4"],
  "schema_complexity_profile": { "<dim>": "<value>" },
  "world_signature": "sha256:9c1f4a...",
  "coverage_neighbors": [99002, 99008, 99023, 99041, 99077, 99102, 99155, 99204]
}
```

`nl_queries` 与 `nlq_specificity_levels` 的位级映射：

- `nl_queries[0]` 对应 `nlq_specificity_levels[0] = "L1"`——L1 canonical、schema_naive、业务用户口吻，只提"conductor"与"performances"等业务词汇，不暴露 collection 名或文档物理结构；
- `nl_queries[1]` 对应 `"L0"`——极简、关键词式（"Add performance totals to conductors."）；
- `nl_queries[2]` 对应 `"L2"`——出现 "conductor collection"、"embedded orchestra.performance arrays"、"flattening" 等 type-aware 与 schema 词汇；
- `nl_queries[3]` 对应 `"L3"`——出现 "document"、"top-level field"、"embedded orchestra-performance-show array structure" 等 NoSQL 物理结构与 schema-dynamic 词汇；
- `nl_queries[4]` 对应 `"L4"`——多语言（中文），同时明确要求"保持原文档的嵌套结构不变"，对应 NoSQL-exclusive shape-preserving 语义。

本 record 的库级资产由 [03](./03_database_synthesis.md) 的 Agentic 合成管线产出（Domain Architect 写出业务叙事、Schema Evolution Simulator 演化出 4 层嵌套拓扑并登记 `sparse_embedded` 特性、Noise Taxonomy 按 Stratified Budget Matrix 注入噪声）；`nosql_nativeness_level = "L4"` 表明该 record 落入 NoSQL-exclusive 档位；`operator_family = "shape_preserving_augment"` 对应 [04 §3.2](./04_dataset_construction.md#04-3) 的 23 pattern 中的 shape-preserving 子族。未适用的可选字段按 §2.3 "省略而非 null" 规则直接省略 key。

### §6.2 canonical 库的 schema 4 层结构

| Schema 层级 | 路径 | 数据结构 | 代表性字段 |
|---|---|---|---|
| L1（顶层 collection） | `conductor` | top-level collection（JSON 数组） | `Conductor_ID`, `Name`, `Age`, `Nationality` |
| L2（嵌套数组） | `conductor.orchestra[]` | 嵌套数组 | `Orchestra_ID`, `Orchestra_Name`, `Year_of_Founded` |
| L3（嵌套数组） | `conductor.orchestra[].performance[]` | 嵌套数组 | `Performance_ID`, `Date`, `Type` |
| L4（嵌套数组） | `conductor.orchestra[].performance[].show[]` | 嵌套数组 | `Show_ID`, `Result`, `Attendance` |

该 schema 拓扑对应 `tds_cell` 中 `schema_topology = "nested_4_deep+sparse_embedded"`（即同时携带 4 层嵌套与稀疏嵌入两个 $\mathcal{F}_{topo}$ 特性），与 `operator_family = "shape_preserving_augment"` 共同构成该 record 的拓扑–算子签名。

### §6.3 canonical 库的 data 形态

`TEND/mongodb_data/orchestra.json` 顶层结构（节选）：

```json
{
  "conductor": [
    {
      "Conductor_ID": 1,
      "Name": "Antal Doráti",
      "Age": 62,
      "Nationality": "Hungarian",
      "orchestra": [
        {
          "Orchestra_ID": 11,
          "Orchestra_Name": "London Symphony Orchestra",
          "Year_of_Founded": 1904,
          "performance": [
            { "Performance_ID": 101, "Date": "1969-03-21", "Type": "concert", "show": [ ] },
            { "Performance_ID": 102, "Date": "1969-09-04", "Type": "concert", "show": [ ] }
          ]
        }
      ]
    }
  ]
}
```

该文件包含足够多 conductor / orchestra / performance 实例，使 canonical NLQ 在执行后返回非平凡结果——输出文档数严格等于输入 conductor 数，每条 conductor 文档均被 augment 出 `total_performances` 字段，整数取值分布非平凡（至少存在两档不同计数）。该 data 中还包含若干缺失 `Name` 字段的 conductor 实例，用以触发 Structural 层的 `sparse_optional_name` 噪声分支（登记于 `noise_trace.json` 的 `type_id` 与 `target field`），并使 `schema_complexity_profile.sparse_embedded_rate` 取非零值。该 data 文件即构造期从 K=2 个候选世界中选定的 canonical world；K-1 个备选变体存放于 `audit/orchestra/99001/world_variants/`。

### §6.4 canonical gold MQL

`record.MQL` 字段对应的 single-stage `$addFields + $map + $ifNull` 管道（与共享契约字面一致）：

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

- `operator_family = "shape_preserving_augment"`：`$addFields` 在根层附加派生字段，`$map` 对嵌套数组逐元素计算演出数量，`$ifNull` 为缺失数组提供空数组回退；整条管道不展开、不分组、不投影剪裁。
- 输出结构：每条 `conductor` 文档的原嵌套结构（`conductor.orchestra[].performance[].show[]` 4 层）全部保留，仅在根层新增 `total_performances` 整型字段；**输出文档数严格等于输入 conductor 数**，不多不少、不改变文档树形状。
- 该 gold MQL 在 canonical world 上的执行结果通过 `audit/orchestra/99001/derived/checker.py`（该 checker 含 `preserves_document_tree` 结构断言，机械核验输入–输出文档结构同构、仅允许根层 `total_performances` 一项 diff），并在 K=2 候选世界上全部通过，留痕在 `audit/orchestra/99001/world_robustness.json`。
- V1'-V7' 完整证书在 `audit/orchestra/99001/certificate.json`，七项验证语义见 [04 §8](./04_dataset_construction.md#04-8)。
- V6' empirical_difficulty 见 [04 §9](./04_dataset_construction.md#04-9)；该 record 的 `pass_rate = 0.6` 落入 `empirical_difficulty = medium` 桶，与 `target_difficulty = medium` 一致。
- canonical_form_set 留痕在 `audit/orchestra/99001/derived/canonical_form_set.json`：`must_contain = ["$addFields", "$map"]`、`must_not_contain_at_root = ["$unwind", "$group"]`；由该四元组驱动的 QIM 语法层指纹在 [05 §1.8](./05_evaluation_methodology.md#05-1-8) 定义。
- V7' SQL-bridge defeat 结果在 `audit/orchestra/99001/sql_bridge_defeat.json`：SQL-bridge panel（`NL2SQL_panel ∘ sqltomongo_translator`）生成的候选 MQL 要么 `EX = 0`，要么 `EX = 1` 而 `QIM = 0`——典型退化形态是 `$unwind + $group + 自定义合并` 的 SQL-bridge 翻译结果：数值层面可能把 `total_performances` 算对（EX = 1），但管道结构把原文档 `conductor.orchestra[].performance[].show[]` 嵌套结构拆平再合并，根层出现了 `must_not_contain_at_root` 中的 `$unwind` / `$group` 算子，`AST_check` 判定 QIM = 0。因此该 record 被 V7' 接受为非 SQL-bridge 可解、进入 V6'。
- 噪声注入追溯在 `audit/orchestra/99001/noise_trace.json`（6 层分布与 coupling operator，其中第 6 层 Type-Polymorphism 若触发则伴随 `$type` / `$switch on $type` 等 coupling 算子）；6 维复杂度向量 $\vec{C}$ 实测值在 `audit/orchestra/99001/complexity_vector.json`；Agentic 合成时 Domain Architect 的业务画像与事件流概要在 `audit/orchestra/99001/business_narrative.json`。

<a id="02-7"></a>
## §7 与其它 SSoT 文档的边界

本文档只定义数据资产、record 字段名、目录组织、切分规则、覆盖目标与配额机制。下列内容不在本文档范围内，需查阅对应 SSoT 文档：

| 关注点 | 由谁定义 |
|---|---|
| 任务 IO / 正确性锚 / 归一化 / ≡_rec / P1-P4 / shape-preserving 子树保留 / V1'-V7' 映射 | [01](./01_task_definition.md)（§1-§7） |
| Agentic 合成方法（6-Agent / 三控制线 / Taxonomy Board / 6 层 Noise Taxonomy / Business Simulator / 业务叙事 / Schema Evolution Simulator / F_topology 特性集合 / 36 条 Noise Taxonomy） | [03](./03_database_synthesis.md)（§1-§12 + §A） |
| Agentic 合成产物汇入 / SI DSL（含 `nosql_nativeness` 与 `canonical_form_set`）/ 23 个 intent pattern / 6 层 noise / L4 canonical SI / 规整化与汇入 / SI 派生 / Gold MQL / NLQ × 5 / V1'-V7' spec-grounded / SQL-bridge defeat / RP_diff / 嵌入覆盖审计 / 路由 | [04](./04_dataset_construction.md)（§2-§12） |
| 7 评测指标（EM / QSM / QFC / EX / EFM / EVM / QIM）公式与协议、7 比特指纹、强制披露清单 | [05](./05_evaluation_methodology.md)（§1, §5-§7） |
| SMART 4 阶段方法架构、shape_preserving target_fields、三方 disjointness、求解侧硬边界屏蔽清单 | [06](./06_solution_design.md)（§1-§2, §5, §7, §10） |

任何与上述边界冲突的描述，以对应 SSoT 文档为准；本文档不重复，也不覆盖。
