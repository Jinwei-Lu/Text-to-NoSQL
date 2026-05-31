# TEND Smoke-v9 发布说明

本目录说明 **2026-05-31 smoke-v9** 一轮构建的发布物清单：包含可公开发布的 **数据集快照** 与 **构建工具链源码**，并标注各文件用途。

---

## 发布概览

| 项目 | 值 |
|------|-----|
| Release tag | `smoke-v9` |
| 构建命令 | `python -m tend.cli.run_smoke --release-tag smoke-v9 --workers 128 --out out/TEND/smoke-v9` |
| 环境 | `TEND_FORCE_DOCUMENT_FLEX=1`（H0 全库 document 级 schema flex，默认开启） |
| LLM | 真实 API（`llm_stub=false`） |
| 目标规模 | 10 库 × 5 条 = 50 条 |
| 实际发布 | **24** 条 record（train 19 / test 5） |
| 入库 Mongo 库 | **7** 个（见下方 Tier-1 列表） |
| Schema-less 异构比 | **~49.95%**（7/7 collection 多 shape） |
| Publish | 已执行（未 skip） |

**Tier-1 已发布库（含 mongodb_schema / mongodb_data）：**  
`concert_singer`, `cre_doc_tracking_db`, `department_store`, `flight_2`, `orchestra`, `student_assessment`, `world_1`

**构建尝试过但未产出 record 的库：**  
`baseball_1`, `hospital_1`, `assets_maintenance`（Phase B MS 失败，未进入 release bundle）

---

## 推荐发布包结构

建议打两个压缩包，便于数据消费者与工程复现分离：

```
TEND-smoke-v9-dataset/          # 仅数据 + audit + eval
TEND-smoke-v9-toolchain/        # 源码 + proposals 契约 + 最小 Spider 引用
```

或将二者合并为 monorepo 子目录：`release/TEND-smoke-v9/{dataset,toolchain}/`。

---

## 一、数据集发布文件（`out/TEND/smoke-v9/`）

复制以下路径到发布包的 `dataset/` 根目录（保持相对路径）。

### 1.1 核心 Record 与切分

| 路径 | 功能 / 内容 |
|------|-------------|
| `TEND.json` | 全量 24 条 benchmark record：NLQ（canonical/colloquial）、MQL、难度、`schema_flex`、六轴元数据、`*_ref` 审计引用 |
| `train.json` | 训练切分（19 条） |
| `test.json` | 测试切分（5 条）；含 H5/H7/H8/H9 约束下的 cross-domain split |
| `_meta.json` | 本次 smoke 运行元数据：workers=128、失败列表、publish/eval 状态、`release_tag` |
| `spider_db_catalog.json` | Spider 库扫描结果：166 库 qualifying/flex_eligible/selected 标记；`selection_policy.min_flex_db_ratio=0.30` |

### 1.2 Phase A — MongoDB 数据世界（Tier-1）

| 路径 | 功能 / 内容 |
|------|-------------|
| `mongodb_schema/<db_id>.json` | SRA 输出的 collection 布局；H0 库含 `__variants`（polymorphic document flex 契约） |
| `mongodb_data/<db_id>.json` | DM 迁移后的文档快照；H0 库同一 collection 内约 50% 文档 shape 异构（`field_a` / `field_b` 交替） |
| `agent_design_rationale/<db_id>.yaml` | Stage A/B 设计 rationale；含 `heterogenization`（H0/H1–H4 触发证据） |

当前 release 含 **7** 个 db_id（见上文 Tier-1 列表）。

### 1.3 审计轨迹（Tier-2，按 record）

| 路径 | 功能 / 内容 |
|------|-------------|
| `audit/<db_id>/<record_id>/qps.yaml` | QPS 查询规划与 coverage cell |
| `audit/<db_id>/<record_id>/ms.yaml` | MS 双路径合成 trace（mql_primary / mql_alt / converged） |
| `audit/<db_id>/<record_id>/mutations.json` | PV 变异集 |
| `audit/<db_id>/<record_id>/pv.yaml` | 属性验证结果 |
| `audit/<db_id>/<record_id>/nlp.yaml` | NLP  paraphrase |
| `audit/<db_id>/<record_id>/rtv.yaml` | Round-trip 验证 |
| `audit/<db_id>/<record_id>/nnc.yaml` | NNC 难度 / sql_infeasibility 诊断 |
| `audit/<db_id>/<record_id>/ra.yaml` | RA 五条 realism 检查 + augment 计划 |
| `audit/<db_id>/<record_id>/phase_b_valid_summary.json` | Phase B valid 汇总（status / gate_pass） |

### 1.4 审计轨迹（Tier-2，按 db Phase A）

| 路径 | 功能 / 内容 |
|------|-------------|
| `audit/<db_id>/wp_output.yaml` | WP 工作负载画像 |
| `audit/<db_id>/migration_log.json` | DM 迁移日志 + `world_signature` |
| `audit/<db_id>/phenomena_audit.json` | 现象扫描（stub/detector） |

### 1.5 全局审计

| 路径 | 功能 / 内容 |
|------|-------------|
| `audit/_global/coverage_report.json` | 六轴 coverage 接受情况 + `flex_yield_by_db` |
| `audit/_global/flex_supply_report.json` | Flex 供给与 split 阈值相关报告 |
| `audit/_global/domain_map_warnings.json` | 领域映射 fallback 警告 |

### 1.6 评估产物（可选，建议一并发布）

路径：`out/TEND/eval/smoke/`（与 `release_tag=smoke-v9` 对应的一次 evaluate）

| 路径 | 功能 / 内容 |
|------|-------------|
| `leaderboard.json` | Echo-gold 求解器 leaderboard 摘要 |
| `per_record_metrics.csv` | 逐 record 指标 |
| `nnc_histogram.json` | NNC 难度直方图 |
| `ra_pass_rate.json` | RA 通过率统计 |
| `slices/six_axes.json` | 六轴 slice 聚合 |
| `disclosure_report.json` | 披露清单检查（smoke-v9 缺 `panel_pr_quadruple`） |
| `panel_pr.json` / `panel_pr_smoke-v*.json` | Panel PR 历史 stub（**非** full 20-model panel） |
| `_meta.json` | eval 元数据（`panel_stub: true`） |

### 1.7 验证脚本（发布说明引用，源码见第二节）

```powershell
# Schema-less 异构比例（需本地 Mongo 或内置 snapshot 模式）
python -m tend.cli.verify_mongo_schemaless --release-root <dataset_root>

# Flex 扫描（全 Spider qualifying 库）
python -m tend.cli.scan_spider_flex --qualifying-only --compact
```

smoke-v9 实测：`overall_heterogeneous_ratio ≈ 0.4995`，7 个 collection 均为 2 种 document shape（`orchestra` 为嵌套数组导致的 3 shape，非 H0）。

---

## 二、构建工具链源码（建议完整发布）

以下为 **复现 / 扩展构建** 所需的最小源码集。安装：`pip install -e .`（见根目录 `pyproject.toml`）。

### 2.1 包入口与配置

| 路径 | 功能 / 内容 |
|------|-------------|
| `pyproject.toml` | `tend` 包定义、依赖、CLI entry points |
| `infra/pip/requirements.txt` | 补充依赖锁定（若有） |
| `tend/config.py` | LLM/Mongo/Spider 路径、`force_document_flex()` 等运行时配置 |
| `tend/core/` | NormExec、AST_check、LLM 客户端、签名、MQL 解析执行 |
| `tend/schemas/` | JSON Schema 校验器 |
| `data/spider_domain_map.yaml` | Spider db_id → domain_id 映射（split / coverage 用） |

### 2.2 Phase A — DataWorld 构建

| 路径 | 功能 / 内容 |
|------|-------------|
| `tend/phase_a/wp.py` | WP：Spider 工作负载画像 |
| `tend/phase_a/sra.py` | SRA：Stage A layout + Stage B H0/H1–H4 触发 |
| `tend/phase_a/sc.py` | SC：schema 审查 |
| `tend/phase_a/dm.py` | DM：SQLite → mongodb_data + flex 物化 |
| `tend/phase_a/catalog.py` | Spider 库 catalog 扫描与 flex_eligible |
| `tend/phase_a/flex_scan.py` | 全库 flex 预审计 |
| `tend/cli/build_phase_a.py` | 单库 Phase A CLI |

### 2.3 Phase B — Record 合成与验证

| 路径 | 功能 / 内容 |
|------|-------------|
| `tend/phase_b/qps.py` | QPS 查询规划 |
| `tend/phase_b/ms.py` | MS 双路径 MQL 合成 |
| `tend/phase_b/pv.py` / `nnc.py` / `nlp.py` / `rtv.py` / `ra.py` | 验证链各 agent |
| `tend/phase_b/templates/` | 确定性 MQL 模板与 compile |
| `tend/cli/build_phase_b_synth.py` | 单 record 合成 |
| `tend/cli/build_phase_b_valid.py` | 单 record 验证 |

### 2.4 编排、发布与评估

| 路径 | 功能 / 内容 |
|------|-------------|
| `tend/cli/run_smoke.py` | **Smoke 入口**（10 库 × N 条，128 workers，publish+eval） |
| `tend/cli/run_build.py` | Full-scale Phase A+B 入口（~200 库 / target records） |
| `tend/cli/run_full.py` | Full 发布编排（含 catalog 扩展） |
| `tend/orchestrate/publish.py` | C1–C9 发布门 + audit materialize |
| `tend/orchestrate/split.py` | Cross-domain train/test + H5/H7/H8/H9 |
| `tend/orchestrate/record_metadata.py` | 六轴元数据推导 |
| `tend/orchestrate/mongo_schemaless.py` | Document shape 异构统计 |
| `tend/cli/evaluate.py` | Post-publish 评估 |
| `tend/cli/scan_spider_flex.py` | Flex 扫描 CLI |
| `tend/cli/verify_mongo_schemaless.py` | Schema-less 验证 CLI |
| `tend/cli/publish.py` | 独立 publish CLI |

### 2.5 契约与 Prompt（构建行为规格）

| 路径 | 功能 / 内容 |
|------|-------------|
| `proposals/schemas/` | `record.schema.json`、`agent_design_rationale.schema.json` 等 |
| `proposals/agent_prompts/` | WP/SRA/QPS/MS/NLP/… LLM prompt 模板 |
| `proposals/_meta/GLOSSARY.md` | 术语表（含 **H0** build-policy flex 说明） |
| `proposals/02_dataset_design.md` | 数据集设计 |
| `proposals/03_spider_anchored_dataworld.md` | Spider-anchored DataWorld 规格 |
| `proposals/04_agent_framework.md` | Agent 框架与验证门 |

### 2.6 测试（建议发布，便于验收）

| 路径 | 功能 / 内容 |
|------|-------------|
| `tests/unit/phase_a/` | Phase A + H0 flex 单测 |
| `tests/unit/phase_b/` | Phase B synth/valid 单测 |
| `fixtures-snapshot/smoke-publish/` | 小型 publish 回归快照（可选） |

### 2.7 Spider 源数据（体积大，单独说明）

| 路径 | 功能 / 内容 | 发布建议 |
|------|-------------|----------|
| `proposals/spider_data/` | Spider SQLite + 查询 JSON（~166 库） | **单独数据包**或 Git LFS；复现 Phase A 必需 |
| `proposals/fixtures/` | 开发用 fixture YAML（orchestra 等） | 可选；stub 模式需要 |

---

## 三、不要发布的内容

| 路径 / 类型 | 原因 |
|-------------|------|
| `infra/env/.env` | API Key、Mongo URI 等密钥 |
| `tend/config.py` 内嵌的默认 API Key | 改用环境变量 |
| `out/.llm_cache/` | LLM 响应缓存，体积大且可能含 prompt 快照 |
| `out/runs/` | 临时运行日志 |
| `out/TEND/audit/`（repo 根下与 release 重复的中间态） | 与 release bundle 内 audit 重复时可省略 |
| `out/audit/`（Phase B 失败 record 的局部 audit） | 非 publish 产物，易混淆 |
| `.cursor/`、`*.plan.md` | 内部计划，非发布物 |
| 完整 `baselines/`、`SMART/`、`TEND/`（旧版 17k 数据集） | 与本 smoke-v9 构建线无关，除非另做 legacy 包 |

---

## 四、复现命令

### 4.1 环境

```powershell
pip install -e ".[dev]"
# 配置 OPENAI_API_KEY、可选 TEND_MONGO_URI
$env:TEND_FORCE_DOCUMENT_FLEX = "1"
```

### 4.2 重跑 smoke-v9 等价构建

```powershell
python -m tend.cli.run_smoke `
  --release-tag smoke-v9 `
  --workers 128 `
  --out out/TEND/smoke-v9
```

### 4.3 单库 Phase A（例如 world_1）

```powershell
python -m tend.cli.build_phase_a --db world_1 --out out/TEND/smoke-v9
```

### 4.4 发布后验证

```powershell
python -m tend.cli.verify_mongo_schemaless --release-root out/TEND/smoke-v9
python -m pytest tests/unit/phase_a/ tests/unit/phase_b/synth/ -q
```

---

## 五、已知限制（发布时应向使用者说明）

1. **规模**：smoke 为等比缩小版（24/50 条），非论文级 17k full build。
2. **Yield**：26 条 Phase B 失败；3 库零产出（MS divergence，见 `ms_ra_universal_yield.plan.md`）。
3. **Panel**：`panel_stub=true`，disclosure 缺 `panel_pr_quadruple`；Gate-F 完整 panel 需另跑 `build_panel_pr --full`。
4. **H0 flex**：qualifying 库默认 polymorphic 双 shape；`orchestra` 排除 H0，保持锚点 embed。
5. **Eval 目录**：当前 evaluate 输出在 `out/TEND/eval/smoke/`，与 `out/TEND/smoke-v9/` 分离；发布时请一并打包或写清相对路径。

---

## 六、快速文件计数（smoke-v9 release 树）

| 类别 | 约计 |
|------|------|
| 顶层 JSON/YAML | 8（TEND/train/test/_meta/catalog + 7×3 Tier-1） |
| Tier-1 文件 | 7 schema + 7 data + 7 rationale |
| audit/ record 级 | ~24 × 8 文件 |
| audit/ db 级 + _global | 若干 |
| **合计** | ~335 文件（含 audit 全树） |

---

## 七、引用与版本

- 构建流水线代号：**TEND Pilot-B**（`tend/` 包）
- 本 release：**smoke-v9**（2026-05-31）
- 关联计划：`/.cursor/plans/ms_ra_universal_yield.plan.md`（yield 改进，未包含在本 release 代码中则注明 commit/branch）

发布前请确认：**git commit hash**、**Python 版本**、**LLM 模型 id**（见 `tend/core/llm_pools.yaml`）写入 release notes 或 `_meta.json` 旁附 `MANIFEST.json`。
