# TEND Canonical Anchor: financial/1001

> **⚠ PENDING DAR Phase A execution-verification**:financial/1001 取自 BIRD mini-dev 真实库 `financial`(已在 test-only 数据集内),但其 MongoDB 反范式化布局(account 聚合 + 稀疏 `loan`/`card` embed + 多态 `trans`)、gold MQL 与 `world_signature` **尚未由 DAR Phase A 在真实 MongoDB 上构造并执行验证**——record 的异构信号(loan 覆盖 682/4500、trans.type 多态、card.type)为**实测**,布局与 MQL 为**提议态**,待 DAR Phase A 落地后冻结真值并替换 `world_signature`(当前为确定性占位)。构造侧 `orchestra` worked examples 仅为 smoke fixtures,不是 production release 记录,待 DAR Phase A financial 构造实现后同步替换。

> Cross-volume byte-identical reference record. Every volume that embeds this example MUST use the JSON block below verbatim (including whitespace inside `MQL`).

## Anchor Selection

| Property | Value |
|----------|-------|
| BIRD mini-dev (test-only) | `financial` |
| record_id | `1001` |
| Rationale | 真实 BIRD 业务库;account 聚合反范式化(稀疏 `loan`/`card` embed + 多态 `trans` 判别);**`preserve` L4** 查询同时演练 §06-5 就地惯用法、③ `variant_handling`(present/missing + polymorphic dispatch)、④ 逐 stage 跨变体塌缩定位(4500→682)——这三项正是 orchestra/1001(`reshape`、无变体)无法演练的部分 |

## Canonical Record

<!-- canonical-anchor: financial/1001 -->
```json
{
  "record_id": 1001,
  "db_id": "financial",
  "nl_queries": {
    "canonical": "为每个 account 附加一个字段 loan_to_credit_ratio:若该 account 有 loan,取 loan.amount 除以该 account 所有贷记交易(trans.type = 'PRIJEM')的 amount 之和(该和为 0 时按 1 计);若该 account 无 loan,则该字段为 0。保留每个 account 文档(含无 loan 的),只在原文档上新增该字段,不改变文档数与嵌套结构;不要求排序。",
    "colloquial": "给每个账户标注它的贷款相对贷记流水的占比;没有贷款的账户记 0,一个账户都别漏。"
  },
  "MQL": "db.account.aggregate([
  { $lookup: {
      from: \"trans\",
      let: { aid: \"$_id\" },
      pipeline: [
        { $match: { $expr: { $and: [ { $eq: [\"$account_id\", \"$$aid\"] }, { $eq: [\"$type\", \"PRIJEM\"] } ] } } },
        { $group: { _id: null, credit_sum: { $sum: \"$amount\" } } }
      ],
      as: \"_credit\"
  } },
  { $addFields: {
      loan_to_credit_ratio: {
        $cond: [
          { $ne: [ { $type: \"$loan\" }, \"missing\" ] },
          { $divide: [ \"$loan.amount\", { $max: [ { $ifNull: [ { $arrayElemAt: [\"$_credit.credit_sum\", 0] }, 0 ] }, 1 ] } ] },
          0
        ]
      }
  } },
  { $project: { _credit: 0 } }
])",
  "canonical_form_set": {
    "must_contain": ["$lookup"],
    "must_not_contain": ["$sample", "$rand", "$$NOW", "$out", "$merge", "$function"],
    "must_contain_at_root": [],
    "must_not_contain_at_root": ["$unwind", "$group"]
  },
  "difficulty": "L4",
  "sql_infeasibility_class": "structural_schema_flex",
  "shape_policy": "preserve",
  "world_signature": "sha256:58d575b0eb62b1499642ec46e9efe5d5576082ce45d871df0326821f44751346",
  "agent_design_rationale_ref": "fixtures/financial/sra.yaml",
  "mutations_ref": "fixtures/financial/mutations.json"
}
```

## Usage Rules

1. Copy the fenced JSON block above into vols 01–06 without modification.
2. Tag every embedded copy with `<!-- canonical-anchor: financial/1001 -->` immediately before the fence.
3. Gate 3 verifies all six copies hash identically.

## BIRD Ground Truth (financial)

Relational schema (BIRD mini-dev `financial`,真实):

- `account(account_id, district_id, frequency, date)` — 4500 行
- `loan(loan_id, account_id, date, amount, duration, payments, status)` — 682 行,**仅覆盖 ~15% 账户(稀疏)**
- `card(card_id, disp_id, type∈{classic,gold,junior}, issued)` — 892 行,**覆盖 ~17% disp(稀疏)**
- `disp(disp_id, client_id, account_id, type)`、`client`、`district`、`order`
- `trans(trans_id, account_id, date, type∈{PRIJEM,VYDAJ,VYBER}, operation?, amount, balance, k_symbol?, bank?, account?)` — 1.06M 行,**多态 + 稀疏字段**

实测 query-bearing 异构信号(BIRD financial 32 题中 `loan` 被 **10 题**引用、`status` 5 题):

- 稀疏可选 embed:loan 682/4500、card 892/5369 → present/missing 变体(机制②稀疏);
- 多态判别:`trans.type` PRIJEM(贷)/VYDAJ(借)/VYBER,`operation` 在利息贷记上 NULL(机制①多态);
- null-vs-missing:`k_symbol`/`bank`/`account` 大量 NULL → 反范式化后变缺字段(机制③)。

MongoDB design (SRA output,**pending DAR Phase A**): account-rooted aggregate embeds optional `loan` + `dispositions[].card`;`trans` referenced(1.06M,多态)— see `fixtures/financial/sra.yaml`(pending)。relational gold 普遍用 `INNER JOIN loan`(静默丢弃无 loan 账户),反范式化为文档后该 present/missing 变体必须由求解器显式处理——这正是"不可被 SQL 平移"的难度来源。
