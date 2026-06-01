# TEND

TEND 是一个面向 MongoDB 的 Text-to-NoSQL 基准构造与评测工作区。它从 BIRD mini-dev 关系型数据库构建 MongoDB 风格的文档世界，通过多智能体构造流水线生成自然语言到 MQL 的基准记录，按项目契约验证候选发布版本，并提供面向已发布记录的 SMART 参考求解器。

这个仓库同时是研究产物和可执行流水线：

- `src/tend/` 是当前活跃的构造、验证、执行、日志和求解器代码。
- `proposals/` 存放方法论文档、提示词契约、JSON Schema、烟测夹具以及运行时代码实现的发布标准。
- `src/tend/baselines/` 是当前活跃的受限 LLM baseline runtime，可通过 `tend baseline` 运行。

仓库中 `proposals/fixtures/` 和 `tests/fixtures/` 下的夹具都是烟测夹具。它们适合做契约和管线连通性检查，但不是生产基准发布版本。生产发布版本应由构造流水线生成，并通过 `tend publish` 的发布验证。

## TEND 生成什么

TEND 关注那些很难通过机械 SQL 翻译得到的 MongoDB 查询。构造流水线以真实 BIRD mini-dev schema、外键、列描述、SQLite 数据以及自然语言/SQL workload 为锚点，生成以下资产：

- 每个数据库的 MongoDB schema，位于 `mongodb_schema/`；
- witness MongoDB 数据，位于 `mongodb_data/`；
- agent 设计理由，位于 `agent_design_rationale/`；
- 基准记录文件 `test.json` 和 `TEND.json`；
- 源数据库目录 `bird_db_catalog.json`；
- 结构化运行日志、异常流和 LLM transcript，位于 `runs/`。

每条记录预计包含 canonical/colloquial 两种自然语言问题、锁定的 gold MQL、difficulty、SQL infeasibility class、shape policy、schema-flex 元数据、world signature，以及一个轻量的 `canonical_form_set` guard。

## 架构

活跃运行时代码是名为 `tend` 的 Python 包。

| 区域 | 作用 |
| --- | --- |
| `tend.config` | 读取 `.env`，解析路径，配置 OpenAI-compatible LLM、MongoDB URI、stub 模式、并发和 run id。 |
| `tend.source` | 加载 BIRD mini-dev schema、workload、列描述、SQLite 探针、census 数据和源目录。 |
| `tend.mechanisms` | 检测 query-bearing 异构机制，并映射到 archetype 和 reference oracle。 |
| `tend.construct` | 将关系型 BIRD 表确定性迁移为文档聚合式 MongoDB witness 数据。 |
| `tend.agents` | 定义 agent 生命周期、LLM agent 基类、Phase A agent、Phase B agent 和确定性验证 agent。 |
| `tend.workflow` | 提供动态 workflow engine，包括受并发限制的 `agent`、`parallel`、`pipeline`、Phase A 和 Phase B flow。 |
| `tend.execution` | 解析 MQL，扫描禁用 operator，派生 canonical form set，加载/执行 MongoDB witness，归一化结果并计算 world signature。 |
| `tend.publish` | 校验发布记录、schema 夹具、必需文件和测试集组成约束。 |
| `tend.solver` | 实现 SMART 参考求解器，包括 solver 可见边界、分阶段契约、逐 stage 执行检查和类型化失败。 |
| `tend.baselines` | 实现受限 LLM baseline 套件，包括 direct、schema-direct、SQL-pivot、plan-then-MQL、ReAct-lite 和 static self-debug。 |
| `tend.observability` | 写入文件优先的 JSONL 日志、异常、Markdown LLM transcript、诊断 JSON，以及可选的 rich 进度 UI。 |

更细的模块级说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 构造流水线

构造 workflow 分为两个阶段。

### Phase A：DataWorld 构造

Phase A 按 BIRD 数据库运行：

1. `WP` 分析真实 workload，并总结访问模式。
2. `SRA` 根据 workload 和 schema 记录文档设计理由。
3. `DM` 根据真实外键基数确定性推导文档聚合结构，物化 witness 文档，计算 `world_signature`，并在 MongoDB 可用时加载数据。
4. `SC` 审查物化后的 schema/data 以及 query-bearing 证据。若审查拒绝，会触发有界的 SRA 修订循环。

DM 是确定性的，并且是物化 schema/data 的权威来源。LLM 输出可以提供理由和审查上下文，但不会覆盖实际 DM witness。

### Phase B：NL-MQL 记录构造

Phase B 对每个 coverage slot 运行一条 pipeline：

1. `QPS` 为检测到的 mechanism/archetype cell 枚举一个具体 intent。
2. `MS` 合成候选 gold MQL，并通过确定性执行检查和 reference oracle 做 gold-lock。
3. `MUT` 生成看起来合理但结果错误的 mutation。
4. `PV` 验证足够多的 mutation 具有区分能力。
5. `NLP` 写出 canonical 和 colloquial 自然语言问题。
6. `RTV` 独立地把 canonical NLQ 翻译回 MQL，并检查结果等价。
7. `NNC` 分配 difficulty 和 SQL infeasibility class。
8. `RA` 基于 witness 检查记录是否非平凡。

workflow 对已知反馈环使用有界重试，包括 SC->SRA、MS gold-lock retry、PV->MUT、RTV->NLP，以及 RA/NNC 后续路径。在 stub 模式下，LLM 调用使用固定响应，依赖执行的检查会走离线友好路径，因此可以在没有 API 调用的情况下跑通整个控制流。

## SMART 求解器

`tend solve` 命令会在 release 风格的数据集上运行 SMART schema-less 参考求解器。它会刻意隔离 solver 可见信息和构造阶段 gold 信息：

1. Shape comprehension 按 collection 探测公开 schema，并归约为 shape model。
2. Intent formalization 将 NLQ 转换为逻辑规格。
3. NoSQL planning 生成带 variant-handling 说明的 MongoDB 物理计划。
4. Query realization 渲染 MQL，检查禁用 operator，并在本地 MongoDB executor 可用时逐 stage 执行 prefix。

solver 边界会在构建 prompt 之前移除禁止泄露的 gold/audit 字段，例如 `MQL`、`canonical_form_set` 和 `*_ref`。solver 终止失败会写成类型化的 `solver_failure` JSONL 记录，而不是写入伪造查询。

## 活跃 Baseline Runtime

`tend baseline` 运行一组有意受限的 LLM baseline，用于和 SMART solver 做实验对照。它们共享和 solver 相同的数据读取、gold-field redaction、日志、异常和终端进度系统，但不会使用 SMART 的 shape-specific 多阶段规划、逐 stage execution feedback 或 gold/audit 信息。

当前内置 6 个 baseline：

| baseline id | 说明 | 主要限制 |
| --- | --- | --- |
| `direct` | NLQ + compact schema summary 一步生成 MQL。 | one-shot、无 witness、无 repair。 |
| `schema_direct` | NLQ + full public schema 一步生成 MQL。 | one-shot、无 execution feedback。 |
| `sql_pivot` | 先生成 SQL sketch，再把 SQL sketch 翻译为 MQL。 | 受 SQL bottleneck 限制，不做 schema-flex planner。 |
| `plan_then_mql` | 先生成简短 query plan，再一次性转成 MQL。 | 单计划、无 self-debug、无 per-stage 检查。 |
| `react_lite` | 模拟一个 ReAct thought/observation turn，再生成最终 MQL。 | 只有一轮 ReAct，observation 只来自 public schema/sample digest。 |
| `static_self_debug` | 先 draft MQL，再用静态 parser/operator feedback 修一次。 | 只用静态反馈，不使用 MongoDB execution feedback。 |

baseline prompt 只读取 release 可见信息：`nl_queries.canonical`、`db_id`、公开 MongoDB schema，以及可选的 public witness digest。它们不会把 `MQL`、`canonical_form_set`、`shape_policy`、`*_ref` 或其他 gold/audit 字段放进 prompt。运行结果会披露 `uses_gold_mql=false`、`uses_execution_feedback=false` 和 disjointness 检查信息。baseline 失败会写成 `baseline_failure`，不会伪造 MQL。

## 仓库结构

```text
src/tend/                    活跃 Python 包
tests/                       runtime、validation、solver 和 contract 测试
docs/                        活跃 runtime 的架构说明
proposals/                   方法论文档、agent prompts、schemas、烟测夹具
proposals/agent_prompts/     LLM-backed agents 使用的提示词契约
proposals/schemas/           JSON Schemas、valid/invalid 夹具、solver allow-list
proposals/fixtures/          proposal 烟测夹具，不是生产发布数据
runs/                        本地运行输出和日志
release/TEND-dataset/        默认生产发布目标目录
minidev/MINIDEV/             期望的 BIRD mini-dev 源数据根目录，如果本机存在
```

## 运行要求

- Python 3.11 或更高版本。
- 执行 `construct` 时需要 BIRD mini-dev 数据位于 `minidev/MINIDEV`，也可以通过 `TEND_BIRD_ROOT` 指定自定义路径。
- live `construct` 和 `solve` 需要 OpenAI-compatible chat-completions provider。
- live Phase B 执行门控和 SMART 逐 stage 执行需要 MongoDB。即使 MongoDB 不可达，Phase A 仍然可以物化数据；stub 模式也可以离线跑通流水线。
- 推荐使用 `uv`，因为仓库包含 `uv.lock`；标准 `pip` 也可以工作。

## 安装

使用 `uv`：

```bash
uv sync
uv pip install -e ".[test]"
```

使用 `venv` 和 `pip`：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[test]"
```

创建本地运行配置：

```bash
cp .env.example .env
```

然后按需编辑 `.env`：

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
TEND_MONGO_URI=mongodb://localhost:27017
TEND_BIRD_ROOT=minidev/MINIDEV
TEND_MODEL=deepseek-v4-flash
TEND_LLM_MAX_CONCURRENCY=16
TEND_QUIET=0
```

离线管线检查可以在命令行使用 `--stub`，也可以设置 `TEND_LLM_STUB=1`。

## 命令行接口

安装后可以使用 `tend ...`，也可以使用 `python -m tend ...`。

```bash
python -m tend --help
python -m tend construct --help
python -m tend validate --help
python -m tend publish --help
python -m tend solve --help
python -m tend baseline --help
```

### 构造

对 `financial` 数据库执行确定性离线 smoke construction：

```bash
python -m tend construct --phase all --dbs financial --records 1 --stub --quiet
```

只运行 live Phase A：

```bash
python -m tend construct --phase A --dbs financial
```

尝试在所有已配置的 BIRD mini-dev 数据库上做更大的 live construction：

```bash
python -m tend construct --phase all --dbs all --records 20
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--phase A|B|all` | 选择构造阶段。Phase B 需要同一 run 中的 Phase A artifacts。 |
| `--dbs financial` | 逗号分隔的数据库 id，或 `all`。 |
| `--records 1` | Phase B 要尝试构造的记录数。 |
| `--stub` | 使用确定性的固定 LLM 响应。 |
| `--quiet` | 关闭 live rich 进度 UI，保留结构化 console/log 输出。 |
| `--run-id my-run` | 固定 run id 和输出目录。 |

默认情况下，构造输出会写到 `runs/<run_id>/dataset/`。可用 `TEND_DATASET_OUT` 覆盖该路径。

### 验证

验证候选数据集目录：

```bash
python -m tend validate --dataset-dir runs/<run_id>/dataset
```

烟测验证会放宽全 11 个数据库覆盖的组成要求，适合小型夹具：

```bash
python -m tend validate --dataset-dir tests/fixtures/smoke_release --smoke
```

### 发布

`publish` 会在完整验证模式下验证输入数据集，只有验证通过才复制到发布目录：

```bash
python -m tend publish \
  --dataset-dir runs/<run_id>/dataset \
  --out release/TEND-dataset
```

完整验证会检查记录契约、JSON Schema、必需的 per-database 文件、`test.json`/`TEND.json` 一致性、world signature，以及测试集组成阈值，例如全 11 个 BIRD 数据库、L4 占比、L0 上限、schema-flex 占比和 structural-schema-flex 占比。

### 求解

在 release 风格的数据集上运行 SMART solver：

```bash
python -m tend solve \
  --dataset-dir tests/fixtures/smoke_release \
  --db-id financial \
  --record-id 1001 \
  --stub \
  --quiet
```

输出会写到 run 目录：

- `solver_predictions.jsonl` 存放成功预测；
- `solver_failures.jsonl` 存放类型化终止失败；
- 同时写入标准的 `events.jsonl`、`anomalies.jsonl` 和 LLM transcripts。

### Baseline

在 release 风格的数据集上运行全部受限 baseline：

```bash
python -m tend baseline \
  --dataset-dir tests/fixtures/smoke_release \
  --db-id financial \
  --record-id 1001 \
  --baselines all \
  --stub \
  --quiet
```

只运行部分 baseline：

```bash
python -m tend baseline \
  --dataset-dir tests/fixtures/smoke_release \
  --baselines direct,schema_direct,react_lite \
  --stub \
  --quiet
```

输出会写到 run 目录：

- `baseline_predictions.jsonl` 存放成功 baseline 预测；
- `baseline_failures.jsonl` 存放类型化 baseline 失败；
- `events.jsonl` 记录每个 baseline step、LLM call、最终 run 状态和 progress 统计；
- `anomalies.jsonl` 是排障入口；
- `llm/<baseline_agent>/<call_id>.md` 是人类/agent 可读 prompt/response；
- `llm/<baseline_agent>/<call_id>.diagnostics.json` 保存完整结构化诊断。

`tend baseline` 使用和 `tend solve` 相同的 run-scoped observability。每个 LLM call 的事件和 diagnostics 都带 `baseline_id`、`baseline_step`、`db_id`、`record_id`、`transcript_ref` 和 `diagnostics_ref`，便于 Claude Code 直接定位 prompt 构造错误、schema validation 错误、parse 错误或 provider 异常。

## 输出和日志

除非显式传入 `--run-id`，每次运行都会生成类似 `run-20260601-013355-a1b2` 的 run id。

```text
runs/<run_id>/
  events.jsonl
  anomalies.jsonl
  llm/<agent>/<call_id>.md
  llm/<agent>/<call_id>.diagnostics.json
  solver_predictions.jsonl
  solver_failures.jsonl
  baseline_predictions.jsonl
  baseline_failures.jsonl
  dataset/
    mongodb_schema/<db_id>.json
    mongodb_data/<db_id>.json
    agent_design_rationale/<db_id>.yaml
    bird_db_catalog.json
    test.json
    TEND.json
```

排障优先从 `runs/<run_id>/anomalies.jsonl` 开始。LLM 相关异常会包含 `transcript_ref` 和 diagnostics 引用，指向精确的 prompt、response attempts、解析输出、验证失败和 usage 元数据。完整事件流在 `events.jsonl`。

## 发布契约

活跃发布验证器检查 `proposals/schemas/` 和 `src/tend/publish/validate.py` 中编码的契约。

重要不变量：

- Gold MQL 必须是 `db.<collection>.aggregate([...])` pipeline。
- 以下禁用 token 在任意深度都会被拒绝：`$sample`、`$rand`、`$$NOW`、`$out`、`$merge`、`$function`。
- `canonical_form_set` 有意保持轻量：它携带禁用 token、不可避免的结构性 operator 和 shape guard，不锁定 `$addFields`、`$cond`、`$type` 这类可替换 idiom。
- `shape_policy` 必须是 `preserve`、`reshape` 或 `reduce`。
- `structural_schema_flex` 记录必须是 L4，并且必须带有非 `none` 的 `schema_flex` 模式。
- `world_signature` 必须匹配该记录 `db_id` 对应 witness 数据的 canonicalized signature。
- 生产发布模式要求完整 release composition，不能使用仅供烟测的小夹具冒充发布数据。

## 运行测试

安装依赖后运行：

```bash
python -m pytest
```

常用定向检查：

```bash
python -m pytest tests/test_validate.py
python -m pytest tests/test_solver_workflow.py
python -m pytest tests/test_baselines.py
python -m pytest tests/test_cli.py
python -m pytest tests/test_pipeline.py
```

部分测试和 live 路径依赖本地 BIRD mini-dev 数据或 MongoDB。Stub-mode 测试不需要 live LLM 调用。

## 基线实验

活跃 baseline runtime 位于 `src/tend/baselines/`，通过 `tend baseline` 运行。它继承主 runtime 的日志、异常、LLM transcript、diagnostics sidecar 和终端进度系统，是当前推荐的 baseline 实验入口。

新的论文/leaderboard 对照实验应优先使用 `tend baseline`，因为它会明确执行 solver-visible boundary、输出 disclosure，并把每次 LLM 调用记录为 Markdown transcript 与 diagnostics JSON。

## 开发说明

- 文档、prompt、schema 和示例中优先使用仓库相对路径。
- 烟测夹具必须明确标注为 smoke，不要描述成生产发布数据。
- 快速检查 workflow 连通性时使用 `--stub --quiet`。
- 小型夹具使用 `tend validate --smoke`，release candidate 使用完整的 `tend publish`。
- 排障时先看 `anomalies.jsonl`，再打开其中引用的 LLM transcript Markdown 和 diagnostics JSON。
- 本仓库已配置 CodeGraph，可用于结构化代码导航。
