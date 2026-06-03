# TEND

TEND 是一个面向 MongoDB 的 Text-to-NoSQL 基准构造与评测工作区。它从 BIRD mini-dev 关系型数据库构建 MongoDB 风格的文档世界，通过多智能体构造流水线生成自然语言到 MQL 的基准记录，按项目契约验证候选发布版本，并提供面向已发布记录的 SMART 参考求解器。

这个仓库同时是研究产物和可执行流水线：

- `src/tend/` 是当前活跃的构造、验证、执行、日志和求解器代码。
- `proposals/*.md` 是当前保留的方案设计文档。`proposals/` 下的 prompt、schema、fixtures 是运行时/测试资产，不属于 Proposal 正文叙事。
- `src/tend/baselines/` 是当前维护的受限 LLM baseline runtime，可通过 `tend baseline` 运行。
- 顶层 `baselines/` 目录已经废弃并从项目中移除；新的 baseline 实现和实验入口统一放在 `src/tend/baselines/`。

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
| `tend.construction` | 最终 MongoDB-native 数据集构建包，包含 Phase A/B、每库专属 design、recipe、executor、audit、verification 和 artifact writers。 |
| `tend.agents` | 定义 agent 生命周期、LLM agent 基类，以及仍被运行时使用的 native 设计/NL 辅助 agent。 |
| `tend.workflow` | 提供动态 workflow engine，包括结构化的 `agent`、`parallel`、`pipeline`；live LLM 并发由 `tend.llm` 客户端统一限流。 |
| `tend.execution` | 解析 MQL，扫描禁用 operator，派生 canonical form set，加载/执行 MongoDB witness，归一化结果并计算 world signature。 |
| `tend.publish` | 校验发布记录、schema 夹具、必需文件和测试集组成约束。 |
| `tend.solver` | 实现 SMART 参考求解器，包括 solver 可见边界、分阶段契约、逐 stage 执行检查和类型化失败。 |
| `tend.baselines` | 实现受限 LLM baseline 套件，包括 direct、schema-direct、SQL-pivot、plan-then-MQL、ReAct-lite 和 static self-debug。 |
| `tend.observability` | 写入文件优先的 JSONL 日志、异常、Markdown LLM transcript、诊断 JSON，以及可选的 rich 进度 UI。 |

更细的模块级说明见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## Final benchmark release

The final 11-database, 1,210-record TEND benchmark is packaged under
`release/tend-native-mongodb-v1/`. GitHub keeps the release JSON, schemas,
provenance, paper statistics, and surgical audit evidence there. The large
MongoDB witness data is distributed through Google Drive rather than Git/LFS.
The `runs/` tree is retained only as local generation evidence and intermediate
state.

- Artifact index: [docs/TEND_FINAL_ARTIFACTS.md](docs/TEND_FINAL_ARTIFACTS.md)
- Release package: [release/tend-native-mongodb-v1/](release/tend-native-mongodb-v1/)
- MongoDB data recovery notes: [release/tend-native-mongodb-v1/MONGODB_DATA.md](release/tend-native-mongodb-v1/MONGODB_DATA.md)
- Drive folder: https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

## 构造流水线

构造 workflow 分为两个阶段，当前唯一可执行路线是 MongoDB-native construction，代码集中在 `src/tend/construction/`。仓库不再提供 legacy 关系型 schema 到文档聚合的构建 API、CLI 入口或兼容 shim。

### Phase A：Native DataWorld 构造

Phase A 按 BIRD 数据库运行。`src/tend/construction/designs/` 下的每个数据库模块根据该库自己的表语义、字段含义、外键、workload 和取值分布，直接物化 MongoDB-native DataWorld。每个设计模块可以复用 `common.py` helper，但最终必须显式决定哪些真实源字段形成多形态集合、动态 key object、派生 tag array、嵌套事件流、attribute bag、版本化字段或 missing-vs-present 结构。

Phase A 会写出 release 可见资产：

- `mongodb_schema/<db>.json`
- `mongodb_data/<db>.json`
- `agent_design_rationale/<db>.yaml`
- `bird_db_catalog.json`

同时写出 native provenance 和审计资产：

- `migration_recipe/<db>.yaml`
- `native_feature_manifest/<db>.yaml`
- `provenance/<db>.json`

`provenance/<db>.json` 中的 `conversion_code_ref` 指向 `tend.construction.designs.<db>`，用于追溯具体转换代码、源字段和派生规则。若某个数据库没有注册 native design，构建会 fail closed，不会回退到 legacy 迁移。

### Phase B：Manifest-driven NL-MQL 记录构造

Phase B 从 Phase A 产出的 `native_feature_manifest` 规划 coverage slots，并由 `src/tend/construction/phase_b.py` 中的确定性 compiler 生成 gold MQL 和 native metadata。它覆盖动态 key 比较、多形态 subtype dispatch、派生 tag 集合逻辑、嵌套事件流过滤、missing-vs-present 表达等 MongoDB-native 模式，并通过 `src/tend/construction/verify.py` 做结构验证和 anti-SQL-transfer 分级。

`--phase B` 只在同一进程中已经有 Phase A artifacts 时可用；没有同进程 artifacts 会 fail closed。当前实现不做磁盘 resume。

## SMART 求解器

SMART-EG 的方案设计见 `proposals/06_solution_design.md`。目标求解侧接口是 `NLQ + read-only MongoDB db_handle`，schema 只能通过数据库探索工具归纳出来；solver 不应把 difficulty、shape policy、gold MQL、canonical form、audit 或 train artifacts 当作输入。

SMART-EG 是 provider-native tool-call ReAct agent，而不是结构化 LLM call：

1. Shape comprehension 通过 MongoDB 探索工具发现 collection、path、shape、dynamic key、array nesting、missing/null 分布和关系线索。
2. Intent formalization 将 NLQ 转换为带 evidence refs 的意图假设。
3. NoSQL planning 生成带 variant handling 和 sentinel coverage 的 MongoDB 物理计划。
4. Query realization 渲染 MQL，检查禁用 operator，并通过 prefix execution 与 `submit_final_mql` 完成显式终止。

solver 边界会在构建 prompt 之前移除禁止泄露的 gold/audit 字段，例如 `MQL`、`canonical_form_set`、`shape_policy` 和 `*_ref`。solver 终止失败会写成类型化的 `solver_failure` JSONL 记录，而不是写入伪造查询。

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
proposals/*.md               方案设计文档
proposals/agent_prompts/     runtime 仍加载的 LLM prompt 资产
proposals/schemas/           runtime/validation 使用的 JSON Schemas 与夹具
proposals/fixtures/          contract smoke fixtures，不是生产发布数据
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
TEND_LLM_MAX_CONCURRENCY=16  # bounds concurrent LLM calls; set 0 to run unbounded
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

对 `financial` 数据库执行 native 离线 smoke construction：

```bash
python -m tend construct --phase all --dbs financial --records 1 --stub --quiet
```

验证 smoke run 的 native artifacts：

```bash
python -m tend construct --phase all --dbs financial --records 2 --stub --quiet --run-id native-smoke
python -m tend validate --dataset-dir runs/native-smoke/dataset --smoke
```

只运行 live native Phase A：

```bash
python -m tend construct --phase A --dbs financial
```

尝试在所有已配置的 BIRD mini-dev 数据库上做更大的 native construction：

```bash
python -m tend construct --phase all --dbs all --records 20
```

构建完整 native 目标数据集：11 个数据库，每库 100 条 NL-MQL 记录：

```bash
python -m tend construct --phase all --dbs all --records-per-db 100 --stub --quiet --run-id native-full-11db
python -m tend validate --dataset-dir runs/native-full-11db/dataset
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--phase A|B|all` | 选择构造阶段。Phase B 需要同一 run 中的 Phase A artifacts。 |
| `--dbs financial` | 逗号分隔的数据库 id，或 `all`。 |
| `--records 1` | Phase B 要尝试构造的记录数。 |
| `--records-per-db 100` | 为每个选中数据库尝试构造固定数量的记录，适合全 11 库 native benchmark 构建。 |
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

活跃发布验证器检查 `proposals/schemas/` 和 `src/tend/publish/validate.py` 中编码的运行时契约；这些资产不属于 Proposal 正文设计文档。

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

baseline runtime 位于 `src/tend/baselines/`，通过 `tend baseline` 运行。它继承主 runtime 的日志、异常、LLM transcript、diagnostics sidecar 和终端进度系统，是当前项目唯一维护的 baseline 实验入口。

顶层 `baselines/` 目录已经废弃；旧的 zero-shot、ICL、RAG、self-debugging 和 SQL-to-NoSQL reproduction 脚本不再作为仓库入口维护。新的论文/leaderboard 对照实验应使用 `tend baseline`，因为它会明确执行 solver-visible boundary、输出 disclosure，并把每次 LLM 调用记录为 Markdown transcript 与 diagnostics JSON。新增或修改 baseline 时，请在 `src/tend/baselines/strategies.py` 定义策略，并通过 `src/tend/baselines/workflow.py` 和 `tests/test_baselines.py` 维护运行与契约覆盖。

## 开发说明

- 文档、prompt、schema 和示例中优先使用仓库相对路径。
- 烟测夹具必须明确标注为 smoke，不要描述成生产发布数据。
- 快速检查 workflow 连通性时使用 `--stub --quiet`。
- 小型夹具使用 `tend validate --smoke`，release candidate 使用完整的 `tend publish`。
- 排障时先看 `anomalies.jsonl`，再打开其中引用的 LLM transcript Markdown 和 diagnostics JSON。
- 本仓库已配置 CodeGraph，可用于结构化代码导航。
