# 06 · Solution Design — SMART-EG 多阶段 ReAct 求解方案

> 本文件是 TEND 求解侧的方案设计文档。它只定义 SMART-EG 为什么要这样设计、agent 如何分阶段工作、工具集合应如何划分、以及哪些机制承担稳定性与合规边界。
>
> 本文件不再承载工程规范、prompt 模板、JSON Schema、实现计划、类名、文件名或伪代码。工程实现可以有多种形态，但必须满足本文的方案约束。

---

## Part I

## TL;DR

SMART-EG 不是结构化 LLM call，也不是把四个 prompt 串起来的 fixed workflow。它是一个 provider-native tool-call ReAct Agent：模型在同一个 agent loop 中反复执行 `reason -> tool call -> observation -> revise`，并且只能通过显式 `submit_*` 工具提交阶段性产物或最终 MQL。

SMART-EG 的任务输入只有两项：

1. `NLQ`：自然语言查询。
2. `db_handle`：可交互、只读的 MongoDB database。

求解器不得依赖 `difficulty`、`shape_policy`、gold MQL、`canonical_form_set`、公开 metadata、audit artifact、train examples、预生成 schema 文件，或任何 release-only 字段。MongoDB 是 schema-less 的；同一 collection 内不同 document 可以有不同形状。Shape Comprehension 必须从可交互数据库中探索出来，而不是读取一个预先给好的完整 schema。

四个 SMART 阶段被保留为工作记忆里程碑，而不是不可动摇的 waterfall：

| Milestone | 产物 | 作用 |
|---|---|---|
| Shape Comprehension | `EnvironmentModel` | 从数据库工具观察中建立集合、路径、类型、变体、动态键、稀疏/缺失、关系线索。 |
| Intent Formalization | `IntentHypothesis` | 把 NLQ 拆成可验证的信息需求、谓词、聚合、输出形态与缺失语义。 |
| NoSQL Planning | `QueryPlan` | 把意图调和到实际 MongoDB 异质结构上，选择 pipeline 策略与变体处理。 |
| Query Realization | `FinalMQL` | 生成只读 deterministic MQL，经静态安全检查与执行证据检查后提交。 |

这四个阶段的价值不在于“阶段越多越好”，而在于让 agent 的关键承诺可提交、可拦截、可回退、可做 evidence gate。真正值得做 ablation 的不是阶段名字，而是机制：tool-call ReAct、submit gate、Evidence Debt、schema-less probes、counterexample sentinels、prefix execution、provider retry。

SMART-EG 的稳定性机制不是 perplexity，也不是 LLM debate，而是 **Evidence Debt**：每个会影响 MQL 的重要 claim 都必须绑定数据库工具证据；缺证 claim 形成 debt；`submit_*` gate 会阻止 agent 在关键 debt 未清时进入下一阶段或提交最终答案。这个机制便宜、可解释、易实现，并直接对应 schema-less MongoDB 的主要错误来源：漏变体、错路径、误把 missing 当 null、动态键没展开、数组层级弄错、关系方向猜错。

LLM provider 问题必须对 agent 透明。SMART-EG 的 LLM 调用使用流式输出，并以 **first-token timeout = 6s** 判断 provider 是否卡住；首 token 超时、stream stall、429、5xx、连接中断、route failover 都由运行时透明重试，不计为 agent turn，不进入 observation，不污染 Evidence Debt。

---

<a id="06-1"></a>
## §06-1 设计立场

### §06-1-1 为什么旧 smart_solver 不够

旧 `smart_solver` 类方法本质上是结构化 LLM call：给一段上下文，让模型一次性输出 JSON、plan 或 MQL。这类设计的问题是：

1. 它没有持续的环境探索能力。模型无法主动决定接下来该查哪个 collection、哪个路径、哪个动态键、哪个变体。
2. 它没有真实 ReAct observation。所谓“阶段输出”通常只是模型自述，而不是工具证据驱动的状态更新。
3. 它没有可验证的终止语义。模型自然语言说“final”不等价于运行时接受答案；必须有 `submit_final_mql` 这样的显式 terminal tool。
4. 它把 provider 异常暴露成 agent 失败。LLM 首 token 卡住不应被 agent 当成数据库事实或推理失败。
5. 它容易把 release metadata 当成捷径。真实 solver 面对的是 NLQ 和 MongoDB，不是带答案影子的 benchmark record。

SMART-EG 的核心改动是把 solver 从“结构化生成器”变成“受工具约束的数据库调查员”。所有重要判断都要回到数据库证据。

### §06-1-2 四阶段是否必要

四阶段不是论文里必须证明“每个阶段单独提升性能”的强假设。第一性原理上，阶段过多可能增加上下文负担、错误传播和 submit friction，去掉某些阶段反而可能提升性能。

因此本文不把 SMART-EG 定义为 rigid four-agent pipeline，而定义为一个 **single ReAct loop with milestone gates**：

- agent 可以从 later milestone 回到 earlier milestone。
- runtime 可以在简单问题上跳过非阻塞 debt，快速到 `submit_final_mql`。
- ablation 应关注机制而非阶段名，例如去掉 Evidence Debt、去掉 prefix execution、去掉 dynamic-key probes、去掉 submit gate，而不是机械比较“有无第 2 阶段”。
- 四个 milestone 的主要作用是压缩工作记忆、组织证据、提供 gate，而不是制造不可消融的模块边界。

这解决了“四阶段无法做干净 ablation”的问题：阶段只是观察与提交的工作面，真正的独立变量是机制。

---

<a id="06-2"></a>
## §06-2 ReAct Runtime

### §06-2-1 Agent loop

SMART-EG 使用 provider-native tool call 范式。每轮只能发生三类动作：

1. 模型继续思考并请求一个或多个工具。
2. 模型调用当前 milestone 允许的 `submit_*` 工具。
3. 模型调用 `abandon_with_failure`，以 typed failure 结束。

运行时维护状态，而不是让模型在自由文本里自封状态：

| State | 含义 |
|---|---|
| `mode` | 当前工作面：environment / intent / planning / realization。 |
| `messages` | provider-native tool-call history。 |
| `evidence_ledger` | 工具观察、摘要、claim 引用关系。 |
| `evidence_debt` | 缺证、冲突、低覆盖、未验证假设。 |
| `milestones` | 已接受的 `EnvironmentModel` / `IntentHypothesis` / `QueryPlan` / `FinalMQL`。 |
| `budgets` | tool call、Mongo execution、LLM retry、wall-clock、token 使用。 |

模型不能通过输出 JSON 字符串伪造工具调用。一个成功的中间产物必须由对应 `submit_*` tool 接受。

### §06-2-2 Terminal tools

SMART-EG 必须有显式 terminal semantics：

| Tool | 终止性 | 作用 |
|---|---:|---|
| `submit_environment_model` | 否 | 提交数据库环境模型；通过 gate 后进入 intent mode。 |
| `submit_intent_hypothesis` | 否 | 提交 NLQ 意图假设；通过 gate 后进入 planning mode。 |
| `submit_query_plan` | 否 | 提交 MongoDB 物理计划；通过 gate 后进入 realization mode。 |
| `submit_final_mql` | 是 | 唯一成功终止工具；返回最终 MQL。 |
| `abandon_with_failure` | 是 | 明确放弃，给出 typed failure，如 `insufficient_evidence`、`budget_exhausted`、`unsupported_query`。 |

没有 `submit_final_mql`，就没有成功答案。自然语言“最终答案如下”不算结束。

### §06-2-3 Revisit tools

因为 MongoDB schema-less，后期执行证据经常推翻早期假设。agent 需要显式回退工具：

| Tool | 作用 |
|---|---|
| `request_revisit_environment` | 当执行遇到未建模形状、路径或类型时回到 Shape Comprehension。 |
| `request_revisit_intent` | 当 pipeline 证据显示 NLQ 子句被误读或漏读时回到 Intent Formalization。 |
| `request_revisit_plan` | 当意图正确但 Mongo 策略错误时回到 NoSQL Planning。 |

这些工具不是失败，而是 ReAct agent 的正常控制流。

---

<a id="06-3"></a>
## §06-3 工具设计原则

SMART-EG 的工具必须足够支撑 schema-less MongoDB 探索，但功能要正交，避免一个工具又采样、又推理、又生成答案。

### §06-3-1 正交原则

工具分为五类：

| 类别 | 只做什么 | 不做什么 |
|---|---|---|
| Discovery | 列出 collection、路径、shape、类型、动态键、数组层级。 | 不解释 NLQ，不生成 MQL。 |
| Profiling | 给出 bounded statistics：频率、覆盖率、distinct、null/missing/empty 分布。 | 不返回大批原始行。 |
| Probe | 执行只读、预算内的聚合探针，验证关系或候选路径。 | 不写库，不读 gold，不自作语义判断。 |
| Check | 静态检查 MQL 安全性、禁用 operator、pipeline shape、stage 前缀。 | 不修复 query。 |
| Submit | 提交 milestone 或 final answer。 | 不探索数据库。 |

### §06-3-2 观察裁剪

所有工具 observation 都要 bounded：

- 默认返回统计摘要，而不是完整 documents。
- 原始样本必须小、可配置、带字段白名单或路径裁剪。
- 大数组只返回长度分布、元素 shape、少量 representative snippets。
- 动态键只返回 top keys、key pattern、value shape、覆盖率。
- 执行结果只返回计数、字段存在性、错误、少量 redacted rows。

这不是为了省 token，而是为了防止 agent 把数据库 dump 当作隐式训练集，也避免 observation 淹没推理上下文。

### §06-3-3 安全边界

工具层必须拒绝：

- 任意 shell / filesystem / network 工具。
- MongoDB 写操作。
- `$out`、`$merge`、`$function`、`$sample`、`$rand`、`$$NOW`。
- 访问 gold MQL、canonical form、difficulty、shape_policy、audit、train examples。
- 把 provider timeout 或 retry 信息暴露为数据库 observation。

---

<a id="06-4"></a>
## §06-4 四个 Milestone 的工具配置

<a id="06-4-1"></a>
### §06-4-1 Shape Comprehension

目标：从 `db_handle` 中建立足够可用的 `EnvironmentModel`。这里面对的是 schema-less MongoDB，因此必须能探索实际数据形状，而不是假设已有 schema。

推荐工具：

| Tool | 输入 | 输出 | 设计理由 |
|---|---|---|---|
| `list_collections` | none | collection 名、估计文档数 | 最小入口；不含字段解释。 |
| `sample_document_shapes` | collection, sample policy | shape signature、路径树、类型摘要 | 发现多形态 document。 |
| `summarize_path` | collection, path | presence/null/missing/type 分布 | 区分 missing、null、empty、present。 |
| `profile_array_shapes` | collection, path | array 长度分布、元素类型、嵌套 shape | 防止 `$unwind` 层级错误。 |
| `profile_dynamic_keys` | collection, path | key 样本、key pattern、value shape | 支撑 `$objectToArray` 类问题。 |
| `profile_values` | collection, path | top values、distinct 估计、numeric range | 找判别键、枚举值、过滤常量。 |
| `probe_relationship` | from collection/path, to collection/path | match rate、fanout 分布、null side | 验证跨 collection 关系方向。 |
| `run_readonly_probe` | bounded aggregation | count、shape、redacted sample | 验证工具集中未覆盖的特定环境假设。 |
| `submit_environment_model` | EnvironmentModel | gate result | 只有提交通过才进入下一 milestone。 |

`EnvironmentModel` 至少要记录：

- collections and candidate root entities
- relevant paths and aliases
- variant signatures
- discriminator candidates
- dynamic-key regions
- array nesting levels
- relationship hypotheses
- coverage gaps
- evidence references for every important claim

<a id="06-4-2"></a>
### §06-4-2 Intent Formalization

目标：把 NLQ 拆成可验证的意图假设，而不是急着写 MQL。

推荐工具：

| Tool | 输入 | 输出 | 设计理由 |
|---|---|---|---|
| `inspect_environment_model` | filters | clipped EnvironmentModel | 让 agent 在已提交环境模型内定位相关证据。 |
| `find_evidence` | claim or path | evidence refs | 防止意图假设凭空绑定字段。 |
| `check_clause_coverage` | NLQ clauses, IntentHypothesis | uncovered / weakly covered clauses | 廉价发现漏读 NLQ。 |
| `request_revisit_environment` | reason | mode shift | 意图中出现环境模型未覆盖概念时回退。 |
| `submit_intent_hypothesis` | IntentHypothesis | gate result | 进入 planning 前拦截未落地意图。 |

`IntentHypothesis` 至少要记录：

- NLQ 子句列表。
- 实体粒度，例如 per account / per district / global aggregate。
- 谓词、分组、排序、窗口、输出字段。
- 缺失语义：missing、null、empty array 是否有不同含义。
- preserve / reshape / reduce 这类输出形态，必须从 NLQ 推断，不可读取 `shape_policy`。
- 每个语义 claim 对应的 environment evidence。

<a id="06-4-3"></a>
### §06-4-3 NoSQL Planning

目标：把意图调和到实际 MongoDB 结构上，明确跨形状访问策略。

推荐工具：

| Tool | 输入 | 输出 | 设计理由 |
|---|---|---|---|
| `inspect_evidence_debt` | scope | blocking / non-blocking debt | planning 前先看哪些环境假设还没证据。 |
| `check_plan_static` | QueryPlan | read-only、operator、stage-order、path-risk 检查 | 在生成 MQL 前捕获明显坏计划。 |
| `probe_plan_cardinality` | plan fragment | count and survival by variant | 检查 `$match` / `$unwind` 是否过早塌缩。 |
| `mine_counterexample` | claim, sentinel type | counterexample found / not found | 针对 missing/null/dynamic key/variant 找反例。 |
| `render_pipeline_draft` | QueryPlan | draft pipeline | 只做 plan 到 pipeline 的机械展开，不提交。 |
| `request_revisit_intent` | reason | mode shift | 计划发现意图矛盾时回退。 |
| `submit_query_plan` | QueryPlan | gate result | 进入 realization 前确认 plan 有证据支撑。 |

`QueryPlan` 至少要记录：

- root collection。
- pipeline strategy。
- variant handling：多态、稀疏字段、dynamic keys、schema version、array-of-object 等。
- sentinel coverage：至少说明 missing、null、empty array、mixed type、dynamic key 中哪些与本题有关，哪些已排除。
- expected output shape。
- risks and remaining non-blocking debt。

<a id="06-4-4"></a>
### §06-4-4 Query Realization

目标：生成最终 MQL，并用工具证据发现可修复错误。

推荐工具：

| Tool | 输入 | 输出 | 设计理由 |
|---|---|---|---|
| `check_mql_static` | MQL | parse、安全、禁用 operator、read-only | 最便宜的出口闸。 |
| `execute_pipeline_prefix` | collection, stages[0:k] | count、shape、errors、variant survival | MQL stage 序列天然适合前缀执行。 |
| `execute_candidate_bounded` | full candidate | bounded output summary | 不以结果等于 gold 为目标，只检查自洽性。 |
| `compare_candidate_variants` | candidates | same-shape / divergent summary | 发现 idiom 替换导致的语义漂移。 |
| `request_revisit_plan` | reason | mode shift | MQL 生成暴露 plan 错误时回退。 |
| `submit_final_mql` | MQL, evidence refs | accepted / rejected | 唯一成功终止。 |

`submit_final_mql` gate 至少检查：

- MQL 可 parse。
- 只读、deterministic。
- 不含 6 件禁用 operator。
- root collection 明确。
- 和 `IntentHypothesis` 的主要子句有对应证据。
- blocking Evidence Debt 已清空，或以 typed reason 明确降级为 non-blocking。

---

<a id="06-5"></a>
## §06-5 Evidence Debt

Evidence Debt 是 SMART-EG 的 easy-but-effective 稳定机制。它替代 perplexity threshold 和 LLM debate。

### §06-5-1 Debt 的定义

当 agent 做出会影响 MQL 的 claim，但没有足够数据库工具证据时，运行时创建 Evidence Debt。

典型 debt：

| Debt | 例子 | 风险 |
|---|---|---|
| `unverified_path` | 假设 `loan.amount` 存在 | path 写错或只在少数变体出现。 |
| `variant_gap` | 只看了有 loan 的 account | preserve 题漏掉无 loan 文档。 |
| `missing_null_confusion` | 把 missing 当成 null | `$ifNull`、`$type`、`$exists` 语义错。 |
| `dynamic_key_unopened` | 直接按字段名访问动态 map | 需要 `$objectToArray`。 |
| `array_level_uncertain` | 不清楚数组嵌套层级 | `$unwind` 多一层或少一层。 |
| `relationship_unverified` | 猜测两个 collection 用某字段 join | `$lookup` 方向或 key 错。 |
| `cardinality_risk` | 早期 `$match` 或 `$unwind` 可能塌缩 | 输出少行或丢 preserve 文档。 |

### §06-5-2 Submit gate

每个 `submit_*` tool 都运行 evidence gate：

- `submit_environment_model` 拒绝没有证据引用的环境 claim。
- `submit_intent_hypothesis` 拒绝没有字段/路径落点的关键 NLQ 子句。
- `submit_query_plan` 拒绝没有 variant handling 的 schema-flex plan。
- `submit_final_mql` 拒绝 blocking debt 未清的最终答案。

gate 的输出是工具 observation，告诉 agent 哪些 debt 需要 probe，而不是让模型自己猜。

### §06-5-3 Sentinel probes

Evidence Debt 的关键不是“多想一遍”，而是触发便宜的 targeted probes：

| Sentinel | Probe 目标 |
|---|---|
| `missing` | 字段不存在的 document 是否存在，是否应保留。 |
| `null` | 字段存在但值为 null 的语义。 |
| `empty_array` | 空数组在 `$unwind`、`$size`、`$filter` 中的行为。 |
| `mixed_type` | 同一路径是否有 number/string/object/array 混合。 |
| `dynamic_key` | key 是否是数据值，需要 `$objectToArray`。 |
| `rare_variant` | 低频 shape 是否影响输出。 |

这套机制成本低，因为它通常只需要 count、presence 和少量 redacted samples；但它正好覆盖 Text-to-NoSQL 最常见的失败模式。

---

<a id="06-6"></a>
## §06-6 Provider Timeout 与透明重试

SMART-EG 的 LLM 调用必须流式执行。不能用完整 call 的结束时间判断 provider 是否健康，因为完整生成时间混合了模型思考、输出长度和 provider 故障。

### §06-6-1 First-token timeout

运行时策略：

| Event | 处理 |
|---|---|
| 6 秒内没有首 token | 判定 first-token timeout，取消请求并重试。 |
| 首 token 已到，但后续长时间无 token | 判定 stream stall，按 provider retry policy 处理。 |
| 429 / 5xx / connection reset / SSE broken | 透明重试或 route failover。 |
| retry exhausted | 运行时返回 provider failure，不伪装成 agent 推理失败。 |

first-token timeout 固定为 6 秒。首 token 到达后，可以使用更长的 inter-token timeout 和 total-call timeout。

### §06-6-2 对 agent 透明

provider 问题不进入 ReAct history：

- 不消耗 agent turn。
- 不创建 Evidence Debt。
- 不作为 tool observation。
- 不触发 revisit。
- 只写入 runtime log、progress、cost accounting。

agent 只看见成功完成的 LLM response 或最终 typed provider failure。

---

<a id="06-7"></a>
## §06-7 Ablation 设计

由于四个阶段只是 milestone，不应把“去掉某阶段”作为唯一消融方式。更合理的 ablation 是机制级：

| Ablation | 预期回答的问题 |
|---|---|
| structured LLM call baseline | ReAct tool loop 是否必要。 |
| no submit gates | 显式 `submit_*` 是否减少未验证 final。 |
| no Evidence Debt | claim-evidence 绑定是否提升稳定性。 |
| no sentinel probes | missing/null/dynamic-key/rare-variant 探针是否贡献主要收益。 |
| no prefix execution | MQL stage 前缀执行是否提升自纠错。 |
| no transparent provider retry | provider 故障处理是否影响 agent 成功率统计。 |
| collapsed milestones | 简单题上减少 milestone friction 是否更好。 |

这比“固定四阶段 vs 三阶段”更科学，因为它把性能差异归因到具体机制，而不是归因到命名边界。

---

<a id="06-8"></a>
## §06-8 合规边界

SMART-EG 求解器必须满足：

1. 输入只包含 `NLQ` 和只读 `db_handle`。
2. 不读取 difficulty、shape_policy、gold MQL、canonical_form_set、audit、train examples、release-only metadata。
3. 不使用任意 shell、filesystem 或网络探索工具。
4. 不执行 MongoDB 写操作。
5. 不生成 `$sample`、`$rand`、`$$NOW`、`$out`、`$merge`、`$function`。
6. 最终成功只能通过 `submit_final_mql`。
7. provider timeout、retry、failover 对 agent 透明。
8. 所有重要 MQL claim 必须有 evidence refs 或明确 debt 状态。

---

<a id="06-9"></a>
## §06-9 边界声明

| 主题 | 权威文档 |
|---|---|
| 任务正确性、EX、禁用 operator 的语义 | [01](./01_task_definition.md) |
| 发布物、record 字段、gold 字段屏蔽 | [02](./02_dataset_design.md) |
| MongoDB DataWorld 构造 | [03](./03_dataworld_construction.md) |
| 构造期 NL-MQL record 生成 | [04](./04_agent_framework.md) |
| 评测指标与报告 | [05](./05_evaluation_methodology.md) |

本文只声明 SMART-EG 求解侧方案：schema-less ReAct agent、milestone submit tools、正交 MongoDB 探索工具、Evidence Debt、sentinel probes、prefix execution、provider first-token timeout 与透明重试。
