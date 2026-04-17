# TEND 数据集设计

> 文档定位: 阐述 TEND 基准的设计目标、规模、记录形式与切分策略
> 目标读者: 团队成员 / 复现者 / 评审
> 前置阅读: [01 任务定义](./01_task_definition.md)
> 最近更新: 2026-04-17

<a id="02-0"></a>
## 0. 摘要

TEND (Text-to-NoSQL Evaluation Natural-language Dataset) 共 **17,020 对** (NLQ, MongoDB query), 覆盖 **154 个数据库 / 105 个领域 / 347 个 collection**, 按 cross-domain 8:2 切为 train 14,245 / test 2,775。其设计围绕四大特性: (i) **cross-domain** 训练/测试库不重叠; (ii) **executable** 每条 gold MQL 在 mongosh 验证可执行; (iii) **multi-NLQ** 每条 MQL 配 5 条 LLM 改写问句; (iv) **canonical formatting** 统一 alias 与子文档命名。TEND 由 Spider 训练集自动转换而来 (详见 [Paper §2.1 Pipeline Overview](../Paper/main.tex)), 因此可直接复用 Spider 的领域多样性与难度分层, 同时避开 SQL 评估范式对嵌套结构的不适用。

<a id="02-1"></a>
## 1. 设计目标与原则

TEND 的设计原则直接服务于 Text-to-NoSQL 的可信评估; 任务的形式化定义见 [01 §1 任务形式化定义](./01_task_definition.md#01-1)。

**1.1 cross-domain (跨领域)** —— 训练 / 测试集按 *数据库 (db_id)* 而非按记录切分, 任何测试库都不在训练阶段出现。这避免了模型通过记忆 schema (collection 名、字段名、惯用 alias) 完成填空式预测, 模拟真实部署中模型遇到全新业务库的情形。in-domain split 会让模型把训练阶段见过的字段顺序直接背下来, 显著高估泛化能力。

**1.2 executable (可执行)** —— 每条 gold MQL 都在本地 MongoDB 上以 mongosh 实际执行通过, 与对应 SQL 在 SQLite 上的执行结果做 JSON 级比对; 仅当行级值一致 (允许排序非显式时的稳定排序) 才入库。这条原则使 EX (Execution Accuracy) 能成为 Text-to-NoSQL 的核心指标, 而非 EM 这种纯字符串匹配 —— 因为 NoSQL pipeline 存在多种等价写法, 字符串比对会严重低估真实正确率, 详见 [04 §4 EX 为何是核心](./04_evaluation_methodology.md#04-4)。

**1.3 multi-NLQ (多自然语言问句)** —— 每条 MQL 配 **5 条** 由不同 LLM (GPT-4o / GPT-4o-mini / Claude-3.5-sonnet) 改写的等义问句。目的有二: (a) 训练阶段迫使模型学习语言现象 (主动/被动、命令/疑问、长短句、列表化与口语化) 而非记忆某种固定问句模板; (b) 评估阶段为同一逻辑意图提供 5 个表面形式, 削弱单个问句歧义带来的方差。

**1.4 canonical formatting (规范化格式)** —— 聚合中间产物有统一命名约定: 聚合后的字段使用 `[操作]_[对象]` (如 `sum_population`、`avg_age`); `$lookup` 临时连接结果按出现顺序命名为 `Docs1` / `Docs2`; `$project` 输出字段保持字母序。规范化的目的是把 EM / QSM 等基于字符串比对的指标从"格式抖动"中解放出来, 降低假阴性, 也让样本之间可直接做差异分析。

<a id="02-2"></a>
## 2. 数据规模与统计

> 数据来源: [Paper §2.6 Statistics of TEND](../Paper/main.tex)

**库与集合层**

| 维度 | 数值 | 说明 |
|---|---|---|
| 数据库数 | 154 | Spider 训练 166 库中过滤掉双向外键库后的产物 |
| 领域数 | 105 | 一个领域 (如 college / sports) 可对应多库 |
| collection 数 | 347 | 平均每库约 2.25 个根集合 |
| (NLQ, NoSQL) 对数 | 17,020 | 每条 gold MQL 与其 5 条改写 NLQ 一一组合后的样本计数 |
| 平均字段数 (库内) | 38.7 | 含嵌入字段递归展开后的字段总数 |
| 字段数范围 | 7 – 331 | 反映规模长尾, 大库主要来自多对多关系反向嵌入 |
| 平均文档数 (库内) | 214.2 | 单根集合的实际记录数中位偏小, 长尾偏大 |
| 文档数范围 | 3 – 13,694 | 含极小测试库与较大业务库 |

**操作与 pipeline 阶段分布**

| 操作 / 阶段 | 占比 | 设计含义 |
|---|---|---|
| `aggregate` | 83.7% | 大多数查询需 pipeline; 反映 Spider 中 JOIN+GROUP 的转化结果 |
| `find` | 16.3% | 对应单表无聚合的简单 SQL |
| `$project` | 92.9% | alias / 字段重排几乎无所不在, 印证规范化命名的硬要求 |
| `$unwind` | 60.2% | 嵌入数组在 TEND 中是常态, 而非边缘情形 |
| `$group` | 54.3% | GROUP BY 在 Spider 中本就高频, NoSQL 仍延续 |
| `$match` | 51.3% | WHERE 子句的等价物 |
| `$sort` | 23.3% | ORDER BY 转化 |
| `$limit` | 16.8% | LIMIT 转化 |
| `$lookup` | 15.1% | 跨集合 JOIN; 受 Stage A 嵌入策略压低占比 |
| `$count` | 6.7% | COUNT(\*) 显式聚合 |

`$project` 92.9% 的高占比说明 NoSQL 输出几乎总要经过字段重排或重命名, 因而把 alias 命名规范化是评估前的硬要求; `$unwind` 60.2% 则提示 schema 设计大量使用嵌入数组, 模型需理解数组路径展开语义。`$lookup` 仅 15.1% 是 Stage A 嵌入策略的直接后果 —— 多数 1:N 关联已固化为子文档, 不再需要运行时 JOIN。

<a id="02-3"></a>
## 3. 数据记录 schema

每条 TEND 记录是一个 JSON 对象, 字段定义如下:

| 字段 | 类型 | 设计含义 |
|---|---|---|
| `record_id` | int | 全局唯一 ID, 用于追溯到 Spider 原条目, 便于错误归因与跨实验对照 |
| `db_id` | str | 数据库标识, 同时作为 `mongodb_schema/{db_id}.json` 与 `mongodb_data/{db_id}.json` 的检索键 |
| `nl_queries` | list[str] (len=5) | 5 条等义自然语言问句, 取并集语义减少改写风格偏差 |
| `ref_sql` | str | 来自 Spider 的原始 gold SQL, 仅作溯源 / 对照, 不参与训练标签 |
| `MQL` | str | 规范化后、可执行的 gold MongoDB 查询 (find 或 aggregate pipeline) |

字段选择的依据如下: `record_id` 与 `ref_sql` 共同回答"为什么这条 MQL 会长成这样" (溯源与并集对照); `nl_queries` 用列表而非单串保证多样性, 训练时通常对每条 NLQ 都展开成一条样本以扩充数量; `MQL` 字段以字符串保存而非 AST, 是为了让任意下游执行器 (mongosh / Python 解析器) 都能按需解析, 也便于 diff 工具逐字符比对。嵌套文档 (collection 内的子数组) 由 Stage A 数据库变换算法产出, 详见 [03 §2 Stage A 数据库变换](./03_dataset_construction.md#03-2)。

<a id="02-4"></a>
## 4. MongoDB 库形式

TEND 的 MongoDB 库与传统 SQL 库的根本差异在于: **以嵌入 (embedding) 替代 JOIN**。子表行不再独立存在, 而是按 1:N 关系作为数组挂在父文档下; 跨集合关联仅在父子关系出现回环 (双向外键) 或多对多反向访问时才退化为 `$lookup`。

以 `activity_1` 库为例 (3 根集合 + 双向反向嵌入):

```text
Activity                                # 根集合 1
  ├─ actid, activity_name
  ├─ Participates_in[]                  # 学生参与, 字段 (stuid, actid)
  └─ Faculty_Participates_in[]          # 教师参与, 字段 (FacID, actid)

Student                                 # 根集合 2
  ├─ StuID, LName, Fname, Age, Sex, Major, Advisor, city_code
  └─ Participates_in[]                  # 同名嵌入, 反向视角

Faculty                                 # 根集合 3
  ├─ FacID, Lname, Fname, Rank, Sex, Phone, Room, Building
  └─ Faculty_Participates_in[]          # 同名嵌入, 反向视角
```

这种"双向反向嵌入"是 Stage A 的有意选择: 同一关系表 (`Participates_in`) 既挂在 `Activity` 下也挂在 `Student` 下, 让从任一侧出发的查询都能在单集合内闭合, 避免不必要的 `$lookup`; 副作用是字段总数会膨胀, 也是 §2 中"平均 38.7 字段、最高 331 字段"的根源之一。

**Docs1 / Docs2 命名约定**: 当查询确实需要 `$lookup` 跨集合连接时, `as` 别名按出现顺序写为 `Docs1`、`Docs2`、…。这是 Stage A 输出与 Manual Review 阶段共同强制的规范, 让所有 lookup 临时变量在不同样本之间可以直接进行字符串级比对, 不会因别名拼写差异 (如 `joined_data` vs `result`) 触发假阴性。

<a id="02-5"></a>
## 5. 切分策略

TEND 采用 **cross-domain 8:2** 切分: 154 个 db_id 按约 8:2 划分到训练 / 测试集, 同一 db_id 不同时出现在两侧。落到样本层为 **train 14,245 / test 2,775** (合计 17,020)。

与 in-domain split (按记录随机切, 库可重叠) 相比, cross-domain 的优势:

- **更接近真实部署**: 生产环境引入新库的频率远高于在已有库上写新查询, 所以"模型在没见过的 schema 上表现如何"才是有意义的问题。
- **避免 schema 记忆作弊**: in-domain 下模型可学到"看到 db `college_3` 就一定要 `$lookup` Student 与 Department" 这种短路, 显著高估泛化。
- **与 Spider 评估传统对齐**: Spider 自身就采用 cross-domain split, TEND 沿用便于同基准类比与方法迁移。

代价是 EX 等核心指标会更低 (论文 SMART 在 cross-domain 下 EX = 65.08%), 但这种"压低"才是真实泛化能力的反映。

<a id="02-6"></a>
## 6. 与现有 benchmark 对比

| 基准 | 规模 (对) | 任务 | gold 可执行 | 多 NLQ | 嵌套支持 |
|---|---|---|---|---|---|
| WikiSQL | ~80k | Text-to-SQL | 部分 | 否 (1 条) | 否 (扁平表) |
| Spider | 10,181 | Text-to-SQL | 是 (SQLite) | 否 (1 条) | 否 |
| BIRD | 12,751 | Text-to-SQL | 是 (大库) | 否 (1 条) | 否 |
| **TEND** | **17,020** | **Text-to-NoSQL** | **是 (mongosh)** | **是 (5 条)** | **是 (嵌入数组)** |

TEND 在规模上略大于 Spider/BIRD, 在任务上是首个面向 MongoDB 的可执行基准, 且独有 multi-NLQ 与原生嵌套支持。该数据集的构造流水线见 [03 §1 流水线总览](./03_dataset_construction.md#03-1), 解释了如何从 Spider 出发自动得到具备上述四特性的语料。

<a id="02-7"></a>
## 7. 已知偏差

- **Spider 来源带来的领域偏差**: Spider 的 105 领域以教育、运动、地理、商业为主, 缺少日志 / IoT / 电商交易等典型 MongoDB 业务, 因而 TEND 的难度与算子分布并非真实 NoSQL workload 的镜像。
- **LLM 生成的 NLQ 风格偏差**: 5 条改写虽来自 3 个不同 LLM, 但提示模板有限, 倾向于"祈使句 + 列表化"; 真实用户问句中的口语化、半句式残缺、含错别字与代指等情形未被覆盖。
- **嵌套深度受限**: Stage A 用 BFS 遍历外键, 嵌套深度通常不超过 2 – 3 层 (因 Spider 库本身关系不深); 真实 MongoDB 文档可能嵌入 5 – 6 层, TEND 不能直接评估这种复杂度。
- **执行检查的"近似一致"**: gold MQL 对 SQL 的等价检查是 JSON 行集比对, 对排序非显式的查询采取稳定排序后比对, 仍可能漏掉值类型差异 (如 `int` 与 `long`)。

<a id="02-X"></a>
## X. 主要构件清单

| 主题 | 文件 / 目录 |
|---|---|
| TEND 训练集 | [TEND/train.json](../TEND/train.json) |
| TEND 测试集 | [TEND/test.json](../TEND/test.json) |
| TEND MongoDB 数据目录 | [TEND/mongodb_data/](../TEND/mongodb_data/) |
| TEND MongoDB schema 目录 | [TEND/mongodb_schema/](../TEND/mongodb_schema/) |
| 切分相关 (cross-domain) 训练样本 | [SMART/SLM_data_cross_domain/](../SMART/SLM_data_cross_domain/) |

<a id="02-Y"></a>
## Y. 未尽事项与已知风险

- TODO(@team): 评估 benchmark 扩展空间, 引入 MongoDB-native workload (GeoJSON 地理查询、Time Series 集合、Change Streams) 以补足 Spider 来源带来的领域偏差。
- TODO(@team): 量化 TEND 与真实 MongoDB 生产环境的对齐度 —— 例如统计真实 workload 中 `$unwind` / `$lookup` / `$group` 的实际占比并与 §2 表对比。
- TODO(@team): 在 5 条 LLM 改写之外补充 ≥1k 条人工改写 NLQ, 度量 LLM 改写对评估方差与 EX 上限的贡献。
- 风险: gold MQL 的"可执行"仅在 mongosh 8.x + Node 20 环境验证, 未在 mongosh 5/6/7 与 mongo legacy shell 上回归。
- 风险: cross-domain split 是按 db_id 随机划分的, 没有显式控制领域 (如把所有 sports 库都放在一边), 可能存在领域级泄漏。
- 风险: `Docs1` / `Docs2` 命名虽避免假阴性, 但模型可能学到"`$lookup` 总要起名为 Docs1"的过强先验, 与真实业务代码风格 (常用语义化别名) 脱节。
