# Text-to-NoSQL

把自然语言查询翻译成可执行的 MongoDB 查询。包含 **TEND** benchmark 和 **SMART** 多阶段推理框架。

## 课题简介

- **任务**：Text-to-NoSQL —— 给定 NLQ + MongoDB schema，生成可执行的 MongoDB 查询（find / aggregation pipeline）。
- **数据集 TEND**：基于 Spider 重构而来，154 个数据库 / 105 个领域 / 347 个 collection / 17,020 对 (NLQ, NoSQL)；按 cross-domain 8:2 切分（train 14,245 / test 2,775）。
- **方法 SMART**：4 阶段流水线
  1. **SLM-based Schema Prediction** —— 微调 Llama-3.2-1B 预测 collection / db_fields / alias_fields / target_fields
  2. **SLM-based Query Generation** —— 微调 Llama-3.2-1B 生成初稿 MQL
  3. **Memory-driven Refinement** —— Agent 用多视角向量检索（NLQ / fields / collections / draft 加权 cosine）召回 Top-K 示例，重写初稿
  4. **Execution-grounded Optimization** —— Debug Agent 把初稿喂给本地 mongosh 执行，对比结果迭代修正
- **指标**：EM, QSM, QFC（query-based）+ EX, EFM, EVM（execution-based）；EX 是核心指标。SMART (deepseek-v3) 在 TEND 上 EX = 65.08%。

## 目录结构

```
Text2NoSQL/
├── SMART/                       # SMART 主框架
│   ├── build_vec_lib.py             # 训练集向量库（多视角嵌入）
│   ├── build_test_vec_lib.py        # 测试集向量库
│   ├── prepare_SLM_data.py          # 生成 4 类 SLM 训练样本
│   ├── rag_by_nlq_pref.py           # 加权 cosine 检索 Top-K
│   ├── LLM_Optimizer.py             # 阶段 3：refinement agent
│   ├── LLM_debugger.py              # 阶段 4：debug agent
│   ├── debug.sh                     # 运行入口示例
│   └── utils/                       # mongosh_exec / schema_to_markdown 等
├── SMART_all/                   # SMART 早期变体（消融对照：_no_pref / _ori 等）
├── baselines/                   # 论文 Table 1 的所有 baseline
│   ├── zero-shot/                   # Instructing LLM
│   ├── ICL/                         # Few-shot LLM
│   ├── RAG/                         # Memory-augmented LLM
│   ├── self_debug/                  # 自调试
│   └── SQL_to_NoSQL/                # Cascaded by LLM / by Grammar
├── transformer/                 # Transformer baseline 模型代码
├── src/                         # 共享工具
│   └── utils/                       # metric.py / mongosh_exec.py / extract_*.py
├── scripts/                     # 数据驱动脚本
│   ├── prepare_SLM_data.py
│   ├── get_SLM_prediction.py        # 整合 4 路 SLM 输出 → test_SLM_prediction.json
│   ├── cal_ref.py / count_mql.py / get_Example.py
├── tools/                       # 通用工具
│   ├── sqlite_to_mongodb.py         # SQLite→MongoDB（Algorithm 1）
│   ├── js_mongo_parser.py           # MQL 递归下降解析器
│   ├── schema_to_markdown.py / export_db_to_md.py
│   └── mql_difficulty_analyzer.py
├── dataset_construct/           # TEND 构造流水线（Section 2）
│   ├── sqlite_to_mongodb.py / sqlite_to_mongodb.ipynb
│   ├── construct_dataset.ipynb / check_dataset.ipynb
│   ├── generate_nlq*.py / generate_feedback.py
│   ├── nlq_pipeline.py / query_pipeline*.py
│   └── example_prompt.py / clean_dataset.py
├── notebooks/                   # 数据准备实验本子
│   ├── dataset_split.ipynb          # cross-domain 8:2 切分
│   ├── transform_dataset.ipynb / transform_dataset_for_baselines.ipynb
│   ├── reconstruct_dataset.ipynb
│   ├── js_to_flat.ipynb / cnt_tend.ipynb
├── TEND/                        # TEND 数据集（已合并 TEND_exp）
│   ├── train.json / test.json       # 原始切分
│   ├── TEND.json                    # 完整数据集
│   ├── mongodb_schema/ / mongodb_data/   # 154 个数据库 schema + 数据
│   ├── test_SLM_prediction.json     # SLM 4 路预测合并
│   ├── test_SLM_prediction_rag.json
│   ├── test_SLM_prediction_rag_no_pref.json
│   ├── train_SLM_prediction.json
│   ├── test_debug_rag20_*.json      # baseline 用：refinement 后输入
│   └── test_debug_rag_exec20_*.json # baseline 用：execution-debug 后输入
├── mongodb_schema/              # 根级 schema（SMART/prepare_SLM_data.py 依赖）
├── mongodb_data/                # 根级数据（baseline mongosh_exec 依赖）
├── release/                     # 发布版本
├── SLM_data_cross_domain/       # SLM 训练样本（4 类 preference + 1 类 text2nosql）
│   ├── query_collection/ / db_fields/ / alias_fields/ / target_fields/ / text2nosql/
├── SLM_prediction_cross_domain/ # SLM 预测输出
│   ├── predictions_{query_collection,db_fields,alias_fields,target_fields,text2nosql}.json
├── Llama-3.2-1B/                # ⚠️ 仅含 config/tokenizer，需自行下载权重
├── seq2seq_data/                # Seq2Seq baseline 训练数据（src/tgt）
├── transformer_data/            # Transformer baseline 训练数据（src/tgt）
├── vector_store/                # 预计算 ada-002 嵌入（872MB）
│   ├── train.pkl / test.pkl / *_ori.pkl
├── cache/                       # embeddings.pkl 缓存
├── results/                     # 实验结果（论文 Table 1 等）
│   ├── results_{icl,rag,self_debug,zero_shot}_deepseekv3.json
│   ├── retrieve_{5,10,15}/          # 不同 Top-K 的对比
│   ├── ori/ / ori2/ / no_pref/      # 消融对照
│   └── SQL_to_NoSQL/
├── error_case/                  # 错误案例（深入分析用）
└── error_analysis/              # 4 类 schema 预测错误的细分
```

## 运行准备

### 1. 依赖

```bash
pip install pymongo openai tqdm pandas numpy scipy demjson tiktoken
```

模型微调需要 `llama-factory`（论文使用的框架）。

### 2. MongoDB 实例

代码默认连接 `mongodb://localhost:27017/`。需要先启动本地 MongoDB：

```bash
mongod --dbpath <your-data-path>
```

把 `mongodb_data/` 下的 154 个 JSON 数据库 import 进去（参考 `dataset_construct/insert_data_to_db.py`）。

### 3. Llama-3.2-1B 权重

`Llama-3.2-1B/` 只包含 config 和 tokenizer，**没有模型权重**。需要从 HuggingFace 下载：

```bash
huggingface-cli download meta-llama/Llama-3.2-1B --local-dir Llama-3.2-1B
```

### 4. OpenAI API Key

`SMART/rag_by_nlq_pref.py` 第 19 行硬编码了 API key（**安全风险，请替换或迁出**）。建议改成环境变量：

```python
api_key = os.environ["OPENAI_API_KEY"]
```

## 运行实验

所有脚本都从**项目根目录**运行（路径都是 `./TEND/...` 形式）。

### 复现 SMART 结果（论文 Table 1，EX = 65.08%）

```bash
# 阶段 1+2：SLM 预测（需要先用 llama-factory 微调好 5 个 SLM 模型）
python scripts/get_SLM_prediction.py     # 合并 4 路 schema 预测 → TEND/test_SLM_prediction.json

# 阶段 3：memory-driven refinement
cd SMART
bash debug.sh                            # 内含 LLM_Optimizer.py 调用

# 阶段 4：execution-grounded debugging
python LLM_debugger.py
```

### 跑 baseline

```bash
cd baselines/ICL && python icl.py                # Few-shot LLM
cd baselines/RAG && python rag.py                # Memory-augmented LLM
cd baselines/self_debug && python self_debug.py
cd baselines/zero-shot && python zero-shot.py    # Instructing LLM
cd baselines/SQL_to_NoSQL && python SQL_to_NoSQL_zero_shot.py
```

### 评估

```bash
python src/utils/metric.py    # 计算 EM/QSM/QFC/EX/EFM/EVM
```

## 没复制的内容

以下未复制（按需手动从原项目目录拿）：

- `spider/` (1.7GB) —— Spider 源数据，仅在**重建 TEND** 时需要（`dataset_construct/sqlite_to_mongodb.py` 用）。已有 TEND/ 就不必复制
- `data/` (1.7GB) —— 早期中间产物（旧版 vec_lib 和 sLM 数据），已被 `vector_store/` + `SLM_data_cross_domain/` 取代
- `SLM_data/` (672MB) / `SLM_prediction/` (29MB) —— 旧版 SLM 数据/预测，被 `_cross_domain` 版本取代
- `tend_src/` (22MB) —— TEND 构造的中间 JSON（all/right/wrong/correct 多版本）
- `paper/` / `paper_demo/` / `paper_WWW/` / `latex/` —— 论文与模板
- `imgs/` / `review/` —— 论文配图与审稿
- `NoSQL_Demo/` —— 单独的 Demo Web 应用
- `gnn_model/` / `project/` / `build_data/` / `debug/` / `method/` —— 早期实验代码（已被 SMART/ 取代）

## 已知小问题

1. SMART/rag_by_nlq_pref.py 第 19 行硬编码 OpenAI API key —— **请尽快替换**
2. Llama-3.2-1B 权重缺失，需自行下载
3. `dataset_construct/sqlite_to_mongodb.py` 引用 `./spider/spider/database/...`，重建 TEND 时需要把 spider 复制回根目录
