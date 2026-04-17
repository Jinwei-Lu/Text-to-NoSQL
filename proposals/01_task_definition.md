# 任务定义 (Text-to-NoSQL)

> 文档定位: 形式化界定 Text-to-NoSQL 任务、I/O 规范与研究问题
> 目标读者: 团队成员 / 复现者 / 评审
> 前置阅读: 无 (本文档为系列入口)
> 最近更新: 2026-04-17

<a id="01-0"></a>
## 0. 摘要

Text-to-NoSQL 把自然语言查询 (NLQ) 翻译为可在 MongoDB 上直接执行的查询程序 (`find` 或 aggregation pipeline)。动机有二: 文档型 NoSQL 已成为现代 Web 后端主力存储, 而其嵌套文档与多阶段聚合远超普通用户的语法负担。本任务与 Text-to-SQL 同源但不可照搬: schema 半结构化、字段可在管道内动态生成、执行语义对阶段顺序敏感。系列文档围绕 TEND 数据集与 SMART 流水线展开, 以执行准确率 EX 为核心指标 ([Paper §1 Introduction](../Paper/main.tex))。

<a id="01-1"></a>
## 1. 任务形式化定义

任务可形式化为下述映射:

\[ f: (\text{NLQ}, \mathcal{S}) \to \text{MQL} \]

其中 \(\text{NLQ}\) 为自然语言查询, \(\mathcal{S}\) 为目标 MongoDB 数据库的 schema (含若干 collection、嵌套字段路径、类型标注), \(\text{MQL}\) 为 MongoDB Query Language 形式的可执行查询程序。

输入空间是"自然语言 + 结构化 schema"的混合域: 前者承载意图, 后者锚定字段命名空间。输出空间限定为只读查询 (`find` 或 `aggregate`); 写操作在范围外 (见 §7)。

最关键的是**可执行性约束**: \(f\) 的输出必须能在目标 MongoDB 实例上无异常执行并返回结构化结果, 而不仅仅是"语法上长得像 MQL"。这一约束直接催生了执行类指标 (EX/EFM/EVM)。具体数据形式见 [02 §3 数据记录 schema](./02_dataset_design.md#02-3)。

<a id="01-2"></a>
## 2. 输入规范

每条样本包含两类输入:

1. **NLQ**: 一句自然语言, 表达检索意图; 同一意图允许多种改写以覆盖语言多样性。
2. **Schema (Markdown)**: 数据库的层级化描述, 沿"collection / 字段名 / 类型 / 嵌套层级"四维展开。

之所以采用 Markdown 而非裸 JSON, 原因有三: (a) 扁平 JSON 在长上下文中难以直观呈现深层嵌套与数组结构, 模型容易"漏看"; (b) 自然语言无法精准引用形如 `Docs1.Members.address.city` 的深层路径, 必须由 schema 显式给出; (c) Markdown 与 LLM prompt 模板拼接更自然。

Schema 采用**前置注入**而非"按需检索", 是为了控制幻觉: MongoDB 的字段命名远比关系数据库自由 (大小写混用、缩写、单复数交替), 一旦让模型自行猜测, 错误率剧增。前置完整 schema 把字段命名空间转化为强约束, 显著稳定下游 schema linking。

<a id="01-3"></a>
## 3. 输出规范

输出严格限定于两类形式:

```text
db.<collection>.find(<filter>, <projection>);
db.<collection>.aggregate([<stage_1>, <stage_2>, ...]);
```

下面是一条简化的 NLQ→MQL→执行结果三元组样例 (字段已截短):

```text
NLQ : Show the school name and driver name for all school buses.
SCH : driver{Driver_ID, Name, school_bus[School_ID, ...]}
      school{School_ID, School, school_bus[School_ID, ...]}
MQL : db.school.aggregate([
        {$lookup: {from: "driver", localField: "School_ID",
                   foreignField: "school_bus.School_ID", as: "Docs1"}},
        {$unwind: "$Docs1"},
        {$project: {"School": 1, "Name": "$Docs1.Name", "_id": 0}}
      ]);
EXEC: [{School: "...", Name: "..."}, ...]
```

输出必须同时满足**结构合法**(符合 MQL 语法) 与 **可执行**(在目标库上跑通且返回非异常结果) 两个约束。两者并非等价: 一条语法合法的查询可能因字段名拼错、`$unwind` 路径错位而返回空集或抛 `BSONTypeError`。输出的评估方式见 [04 §1 6 指标体系总览](./04_evaluation_methodology.md#04-1)。

<a id="01-4"></a>
## 4. 与 Text-to-SQL 的本质差异

| 维度 | Text-to-SQL | Text-to-NoSQL |
| --- | --- | --- |
| 数据模型 | 平铺二维表, 行列固定 | 嵌套文档, 数组/对象任意深度 |
| 字段命名 | 字段绑定列, 静态可枚举 | `$project` 可动态生成 alias 字段, schema 之外 |
| 顺序敏感 | SELECT 子句可重排, 优化器决定执行计划 | pipeline 顺序敏感: `$sort→$limit ≠ $limit→$sort` |
| 静态可验证 | 强类型 + 静态解析, 多数错误编译期暴露 | 缺乏静态类型校验, 必须依赖执行验证 |

第三与第四点共同决定了"执行反馈"是 Text-to-NoSQL 不可省略的一环, 也构成 SMART 中 Debug Agent 的存在根因。

<a id="01-5"></a>
## 5. 任务难点

下列结构在 NLQ 中通常**不会显式出现**, 模型必须从 schema 与示例中推断:

- **`$lookup` 子集合命名约定**: TEND 将 lookup 产生的临时子文档统一标记为 `Docs1`、`Docs2`, 便于跨样本对齐。模型若未学到这一约定, 就无法稳定生成可对比的 alias。
- **`$project` alias 字段**: 形如 `sum_population: {$sum: "$Population"}`, alias 名既不在 schema 中, 也不在 NLQ 中, 完全是流水线内部生造。
- **`$unwind` 路径选择**: 同一 collection 可能有多层嵌套数组, 选错路径会导致结果膨胀或空集。
- **`$expr` 子查询表达式**: 用于在管道阶段中嵌入条件判断, 涉及对 BSON 表达式语法的精确把握。

这些点合在一起意味着: 单纯的 seq2seq 监督学习难以泛化, 必须引入 schema 预测与执行反馈两个外部信号。

<a id="01-6"></a>
## 6. 研究问题 RQ1-RQ3

围绕 SMART 框架的核心权衡, 提出三个研究问题:

- **RQ1**: 能否用 SLM (Llama-3.2-1B) 替代 LLM 完成 schema 预测?
  - 实验方向: 全参数微调 4 个独立 SLM, 分别预测 collection / db_fields / alias_fields / target_fields, 对比 LLM zero-shot 与 few-shot 基线在字段级命中率上的差距, 验证"小模型 + 任务专精"在 schema linking 上的可行性 (batch=4, 训练规模 14,245)。
- **RQ2**: 多视角加权检索是否优于单视角 NLQ 检索?
  - 实验方向: 在共享 `text-embedding-ada-002` 嵌入库上, 对 NLQ / db_fields / alias_fields / target_fields / collection / draft MQL 六视角分别赋权 (1.0 / 0.7 / 0.5 / 0.5 / 0.7 / 0.3), Top-K=20 召回; 与"仅 NLQ 余弦"基线在下游 EX/QSM 上对比。
- **RQ3**: 执行反馈闭环是否能进一步提升 EX/EFM/EVM?
  - 实验方向: 以 refinement 后输出为基线, 加入 mongosh 执行 + 结果差异分析的 Debug Agent 闭环 (deepseek-v3 或 gpt-4o-mini, temperature=0.0), 消融"无/有执行反馈"两组指标, 量化 EX 相对增益。

对应的解决方案设计见 [05 §1 设计动机与总览](./05_solution_design.md#05-1)。

<a id="01-7"></a>
## 7. 范围与限制

- 仅支持**只读查询**: insert / update / delete / replaceOne 等写操作不在范围内。
- 目标引擎: **MongoDB 7.0+**, 依赖部分较新的聚合算子。
- 不涉及 **schema 设计**与**索引优化**: 给定 schema 视为固定约束, 不重新建模。
- 不支持**事务** (multi-document transaction) 与 **change stream**。
- 仅覆盖 **MongoDB**, 不扩展至其他 NoSQL 系统 (Couchbase / DynamoDB / Cassandra)。

<a id="01-X"></a>
## X. 主要构件清单

| 主题 | 文件 |
| --- | --- |
| TEND 测试样例 | [TEND/test.json](../TEND/test.json) |
| TEND 训练样例 | [TEND/train.json](../TEND/train.json) |
| MongoDB schema 目录 | [TEND/mongodb_schema/](../TEND/mongodb_schema/) |
| Schema → Markdown 转换器 | [SMART/utils/schema_to_markdown.py](../SMART/utils/schema_to_markdown.py) |
| 指标实现入口 | [src/utils/metric.py](../src/utils/metric.py) |

<a id="01-Y"></a>
## Y. 未尽事项与已知风险

- TODO(@team): RQ 边界讨论 — 是否需要新增 RQ4 处理多语言 NLQ (中/英以外的输入)。
- TODO(@team): 明确"执行成功但语义错误"的工程化判定规则, 划清与 EX 的边界。
- 风险: 数据来源仅 Spider, 领域偏差未量化, 跨业务场景 (金融、IoT、日志分析) 的泛化能力存疑。
- 风险: 仅覆盖 MongoDB, 其他 NoSQL 系统 (Couchbase / DynamoDB / Cassandra) 未在 scope 内, 当前结论不可平移。
- 风险: schema 前置注入在大规模 schema 下会撞 LLM 上下文上限, 需要后续设计 schema 压缩或检索式补给。
- 风险: 仓库中存在硬编码 API key (应替换为环境变量, 此处以 `<REDACTED>` 表示), 在公开复现前必须清理。
