# TEND Proposals Changelog

## SMART v3 · schema-less agentic Solver (2026-05) — Active

**Focus**: Volume 06 求解侧 SMART 重铸为 **schema-less agentic** 范式,以 MongoDB「每条 document 结构可不同」为脊柱。两类**推理期、零训练**智能体:**SLM Agent(感知层)** 独占 ① Shape Comprehension——以 map-reduce 探针群**高并发**扫整库异质结构,产出形状模型 `Ŝ`(`field_locus` 字段×变体定位图);**LLM Agent(认知层)** 独占 ②③④——沿"② Intent Formalization(范式中立逻辑规约)→ ③ Heterogeneity Reconciliation & NoSQL Planning(命门,含 `variant_handling`)→ ④ Query Realization & Self-Debug(生成+executor 内联+自纠正)"深推理。阶段边界按**输出表示类型**切;**②→③ = SQL/NoSQL 分水岭**;生成与调试同属 ④(只差一个 executor 工具)。**Agentic RAG = 横切共享检索工具(非阶段、非 agent)**:单一 `structural_example_retriever`,两个匹配方法——`regex_example_retriever`(算子指纹/shape_flex 签名)+ `embedding_example_retriever`(去域化意图向量),**只匹配相似 examples**;**cross-domain holdout 下按抽象结构跨域迁移 NoSQL 惯用法,不按 surface**。回路按"哪个表示错了"分流(④→③/②/①)。求解侧硬边界(§06-4 audit 屏蔽 / 6 件禁用 operator / 构造–panel disjointness / §06-5 shape-preserving)**架构无关、逐字保留**;`solver_allow_list.json` 阶段键改为 `shape_comprehension/intent_formalization/heterogeneity_planning/query_realization`、`tools.example_retrieval` 两方法、`S_solver` = SLM + LLM backbone + 检索 embedding。**修正既有不一致**:`test.json:shape_policy` 改为 forbidden(§06-5 要求 solver 从 NLQ 自推断,不作 gold 提示)。`/SMART/` 实际代码未改,仅在 §06-II-4 更新映射叙述。canonical anchor `orchestra/1001` 字节不变。

## Reverse-Engineered NL-MQL (2026-05) — Active

**Focus**: Spider as data+scenario source only; Phase B reverse-engineered NL–MQL construction via QPS→MS→MUT→PV→NLP→RTV→NNC→RA eight-agent pipeline; graduated SQL-shortcut gate; test L4 ≥ 30% with schema_flex supply-relax.

Historical proposal versions are archived under `proposals/archive/` and are not referenced by active volumes.
