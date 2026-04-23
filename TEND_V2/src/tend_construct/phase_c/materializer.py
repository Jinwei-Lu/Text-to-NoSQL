from __future__ import annotations

from typing import Any

from tend_core import (
    CanonicalFormSet,
    QIR,
    Record,
    StructuredIntent,
    canonical_text,
    extract_operator_tokens,
    extract_root_stage_tokens,
)
from tend_construct.phase_d.external_runner import ExternalModelRunner, NoopExternalModelRunner


ALL_PATTERN_FAMILIES = (
    "simple_filter",
    "filter_then_aggregate",
    "group_then_aggregate",
    "top_k_by_aggregate",
    "time_window_aggregate",
    "facet_split",
    "anomaly_vs_baseline",
    "existential_quantifier",
    "window_function_with_facet_filter",
    "project_only",
    "null_vs_missing_disambig",
    "coalesce_with_default",
    "polymorphic_branch",
    "type_introspection",
    "dynamic_key_expansion",
    "array_positional_select",
    "array_reshape",
    "lookup_join",
    "graph_recursive_deep",
    "percentile_approximation",
    "universal_quantifier",
    "window_function",
    "filter_then_count",
)

SPECIFICITY_ORDER = ["L1", "L0", "L2", "L3", "L4"]

_REQUIRED_OPERATORS: dict[str, set[str]] = {
    "simple_filter": {"$match", "$project"},
    "filter_then_aggregate": {"$match", "$group"},
    "group_then_aggregate": {"$group", "$project"},
    "top_k_by_aggregate": {"$group", "$sort", "$limit"},
    "time_window_aggregate": {"$sort", "$group"},
    "facet_split": {"$facet"},
    "anomaly_vs_baseline": {"$setWindowFields", "$match"},
    "existential_quantifier": {"$match", "$expr", "$anyElementTrue"},
    "window_function_with_facet_filter": {"$setWindowFields", "$facet"},
    "project_only": {"$project"},
    "null_vs_missing_disambig": {"$project", "$group", "$type"},
    "coalesce_with_default": {"$project", "$ifNull"},
    "polymorphic_branch": {"$project", "$switch", "$type"},
    "type_introspection": {"$group", "$type"},
    "dynamic_key_expansion": {"$objectToArray", "$unwind", "$group"},
    "array_positional_select": {"$project", "$arrayElemAt"},
    "array_reshape": {"$unwind", "$group"},
    "lookup_join": {"$lookup", "$project"},
    "graph_recursive_deep": {"$graphLookup", "$project"},
    "percentile_approximation": {"$group", "$percentile"},
    "universal_quantifier": {"$match", "$expr", "$allElementsTrue"},
    "window_function": {"$setWindowFields", "$project"},
    "filter_then_count": {"$match", "$count"},
}

_PRIMARY_OPERATOR: dict[str, str] = {
    "simple_filter": "$match",
    "filter_then_aggregate": "$group",
    "group_then_aggregate": "$group",
    "top_k_by_aggregate": "$group",
    "time_window_aggregate": "$group",
    "facet_split": "$facet",
    "anomaly_vs_baseline": "$setWindowFields",
    "existential_quantifier": "$match",
    "window_function_with_facet_filter": "$setWindowFields",
    "project_only": "$project",
    "null_vs_missing_disambig": "$type",
    "coalesce_with_default": "$ifNull",
    "polymorphic_branch": "$switch",
    "type_introspection": "$type",
    "dynamic_key_expansion": "$objectToArray",
    "array_positional_select": "$arrayElemAt",
    "array_reshape": "$unwind",
    "lookup_join": "$lookup",
    "graph_recursive_deep": "$graphLookup",
    "percentile_approximation": "$percentile",
    "universal_quantifier": "$allElementsTrue",
    "window_function": "$setWindowFields",
    "filter_then_count": "$count",
}


def materialize_record(
    record_id: int,
    db_id: str,
    structured_intent: StructuredIntent,
    model_runner: ExternalModelRunner | None = None,
    schema_payload: dict[str, Any] | None = None,
    witness_payload: dict[str, Any] | None = None,
) -> tuple[StructuredIntent, str, QIR, Record, dict[str, Any], list[dict[str, Any]]]:
    mql = compile_mql(structured_intent)
    canonical_form = derive_canonical_form_set(mql, structured_intent.intent["pattern_family"])
    structured_intent = StructuredIntent(
        meta=structured_intent.meta,
        intent=structured_intent.intent,
        output=structured_intent.output,
        properties=structured_intent.properties,
        noise_policies=structured_intent.noise_policies,
        nosql_nativeness=structured_intent.nosql_nativeness,
        canonical_form_set=canonical_form,
    )
    qir = build_qir(structured_intent)
    nl_queries = tuple(
        generate_nl_queries(
            structured_intent,
            model_runner=model_runner,
            schema_payload=schema_payload,
            witness_payload=witness_payload,
        )
    )
    checker = build_checker(structured_intent, mql, qir)
    mutations = build_mutations(structured_intent, mql)
    record = Record(
        record_id=record_id,
        db_id=db_id,
        nl_queries=nl_queries,
        mql=mql,
        canonical_form_set=canonical_form,
        operator_family=structured_intent.intent["pattern_family"],
        nosql_nativeness_level=structured_intent.nosql_nativeness["level"],
        empirical_difficulty=None,
        shape_policy=structured_intent.output["shape_policy"],
        raw={
            "collection": structured_intent.intent["collection"],
            "metric_field": structured_intent.intent["metric_field"],
            "label_field": structured_intent.intent["label_field"],
            "category_field": structured_intent.intent.get("category_field"),
            "time_field": structured_intent.intent.get("time_field"),
            "array_field": structured_intent.intent.get("array_field"),
        },
    )
    return structured_intent, mql, qir, record, checker, mutations


# ---------------------------------------------------------------------------
# compile_mql – 23 pattern families
# ---------------------------------------------------------------------------

def compile_mql(si: StructuredIntent) -> str:
    intent = si.intent
    collection = intent["collection"]
    metric = intent["metric_field"]
    label = intent["label_field"]
    category = intent.get("category_field") or label
    array_f = intent.get("array_field")
    time_f = intent.get("time_field") or label
    pattern = intent["pattern_family"]
    metric_leaf = metric.split(".")[-1]
    threshold = si.properties.get("threshold", 70)

    if pattern == "simple_filter":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $match: {{ "{metric}": {{ $gt: {threshold} }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {metric_leaf}: "${metric}" }} }}'
            f"])"
        )

    if pattern == "filter_then_aggregate":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $match: {{ "{metric}": {{ $gt: {threshold} }} }} }},'
            f' {{ $group: {{ _id: "${category}", matched_count: {{ $sum: 1 }}, avg_value: {{ $avg: "${metric}" }} }} }},'
            f' {{ $sort: {{ matched_count: -1, avg_value: -1 }} }},'
            f' {{ $project: {{ _id: 0, {category}: "$_id", matched_count: 1, avg_value: 1 }} }}'
            f"])"
        )

    if pattern == "group_then_aggregate":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $group: {{ _id: "${category}", avg_value: {{ $avg: "${metric}" }}, total_docs: {{ $sum: 1 }} }} }},'
            f' {{ $sort: {{ avg_value: -1 }} }},'
            f' {{ $project: {{ _id: 0, {category}: "$_id", avg_value: 1, total_docs: 1 }} }}'
            f"])"
        )

    if pattern == "top_k_by_aggregate":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $group: {{ _id: "${label}", total_value: {{ $sum: "${metric}" }} }} }},'
            f' {{ $sort: {{ total_value: -1 }} }},'
            f' {{ $limit: {si.properties["top_k"]} }},'
            f' {{ $project: {{ _id: 0, {label}: "$_id", total_value: 1 }} }}'
            f"])"
        )

    if pattern == "time_window_aggregate":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $sort: {{ "{time_f}": 1 }} }},'
            f' {{ $group: {{ _id: "${time_f}", avg_value: {{ $avg: "${metric}" }} }} }},'
            f' {{ $project: {{ _id: 0, bucket: "$_id", avg_value: 1 }} }}'
            f"])"
        )

    if pattern == "facet_split":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $facet: {{'
            f' high: [{{ $match: {{ "{metric}": {{ $gt: {threshold} }} }} }}, {{ $group: {{ _id: "${category}", count: {{ $sum: 1 }} }} }}],'
            f' low: [{{ $match: {{ "{metric}": {{ $lte: {threshold} }} }} }}, {{ $group: {{ _id: "${category}", count: {{ $sum: 1 }} }} }}]'
            f' }} }},'
            f' {{ $project: {{ _id: 0, high: 1, low: 1 }} }}'
            f"])"
        )

    if pattern == "anomaly_vs_baseline":
        ws = si.properties["window_size"]
        bm = si.properties["baseline_multiplier"]
        return (
            f'db.{collection}.aggregate(['
            f' {{ $setWindowFields: {{ sortBy: {{ "{time_f}": 1 }}, output: {{ moving_avg: {{ $avg: "${metric}", window: {{ documents: [-{ws - 1}, 0] }} }} }} }} }},'
            f' {{ $match: {{ $expr: {{ $gt: ["${metric}", {{ $multiply: ["$moving_avg", {bm}] }}] }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {metric_leaf}: "${metric}", moving_avg: 1 }} }}'
            f"])"
        )

    if pattern == "existential_quantifier":
        if not array_f:
            raise ValueError("existential_quantifier requires an array field")
        return (
            f'db.{collection}.aggregate(['
            f' {{ $match: {{ $expr: {{ $anyElementTrue: {{ $map: {{ input: "${array_f}", as: "item", in: {{ $gt: ["$$item", {threshold}] }} }} }} }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {array_f}: 1 }} }}'
            f"])"
        )

    if pattern == "window_function_with_facet_filter":
        ws = si.properties["window_size"]
        return (
            f'db.{collection}.aggregate(['
            f' {{ $setWindowFields: {{ sortBy: {{ "{time_f}": 1 }}, output: {{ moving_avg: {{ $avg: "${metric}", window: {{ documents: [-{ws - 1}, 0] }} }} }} }} }},'
            f' {{ $facet: {{'
            f' ranked: [{{ $sort: {{ moving_avg: -1 }} }}, {{ $project: {{ _id: 0, {label}: 1, moving_avg: 1, category: "${category}" }} }}],'
            f' baseline: [{{ $group: {{ _id: null, global_avg: {{ $avg: "$moving_avg" }} }} }}]'
            f' }} }},'
            f' {{ $project: {{ kept: {{ $filter: {{ input: "$ranked", as: "row", cond: {{ $gt: ["$$row.moving_avg", {{ $arrayElemAt: ["$baseline.global_avg", 0] }}] }} }} }} }} }},'
            f' {{ $unwind: "$kept" }},'
            f' {{ $project: {{ _id: 0, {label}: "$kept.{label}", moving_avg: "$kept.moving_avg", category: "$kept.category" }} }}'
            f"])"
        )

    # ---- 14 new pattern families ----

    if pattern == "project_only":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, {label}: 1, {metric_leaf}: "${metric}" }} }}'
            f"])"
        )

    if pattern == "null_vs_missing_disambig":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, {label}: 1, null_status: '
            f'{{ $cond: {{ if: {{ $eq: [{{ $type: "${metric}" }}, "missing"] }}, then: "missing", '
            f'else: {{ $cond: {{ if: {{ $eq: ["${metric}", null] }}, then: "explicit_null", else: "has_value" }} }} }} }} }} }},'
            f' {{ $group: {{ _id: "$null_status", count: {{ $sum: 1 }} }} }},'
            f' {{ $project: {{ _id: 0, status: "$_id", count: 1 }} }}'
            f"])"
        )

    if pattern == "coalesce_with_default":
        default_val = si.properties.get("default_value", 0)
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, {label}: 1, value: {{ $ifNull: ["${metric}", {default_val}] }} }} }},'
            f' {{ $sort: {{ value: -1 }} }}'
            f"])"
        )

    if pattern == "polymorphic_branch":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, {label}: 1, branch: {{ $switch: {{ branches: ['
            f'{{ case: {{ $in: [{{ $type: "${metric}" }}, ["int", "long", "double", "decimal"]] }}, then: "numeric" }}, '
            f'{{ case: {{ $eq: [{{ $type: "${metric}" }}, "string"] }}, then: "string" }}, '
            f'{{ case: {{ $eq: [{{ $type: "${metric}" }}, "null"] }}, then: "null" }}], '
            f'default: "other" }} }} }} }},'
            f' {{ $group: {{ _id: "$branch", count: {{ $sum: 1 }} }} }},'
            f' {{ $project: {{ _id: 0, type_branch: "$_id", count: 1 }} }}'
            f"])"
        )

    if pattern == "type_introspection":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $group: {{ _id: {{ $type: "${metric}" }}, count: {{ $sum: 1 }} }} }},'
            f' {{ $project: {{ _id: 0, field_type: "$_id", count: 1 }} }}'
            f"])"
        )

    if pattern == "dynamic_key_expansion":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, kv: {{ $objectToArray: "$$ROOT" }} }} }},'
            f' {{ $unwind: "$kv" }},'
            f' {{ $group: {{ _id: "$kv.k", distinct_types: {{ $addToSet: {{ $type: "$kv.v" }} }}, count: {{ $sum: 1 }} }} }},'
            f' {{ $project: {{ _id: 0, field_name: "$_id", types: "$distinct_types", count: 1 }} }}'
            f"])"
        )

    if pattern == "array_positional_select":
        if not array_f:
            raise ValueError("array_positional_select requires an array field")
        return (
            f'db.{collection}.aggregate(['
            f' {{ $project: {{ _id: 0, {label}: 1, '
            f'first: {{ $arrayElemAt: ["${array_f}", 0] }}, '
            f'last: {{ $arrayElemAt: ["${array_f}", -1] }}, '
            f'size: {{ $size: "${array_f}" }} }} }}'
            f"])"
        )

    if pattern == "array_reshape":
        if not array_f:
            raise ValueError("array_reshape requires an array field")
        return (
            f'db.{collection}.aggregate(['
            f' {{ $unwind: "${array_f}" }},'
            f' {{ $group: {{ _id: "${label}", values: {{ $push: "${array_f}" }}, count: {{ $sum: 1 }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: "$_id", values: 1, count: 1 }} }}'
            f"])"
        )

    if pattern == "lookup_join":
        secondary = intent.get("secondary_collection") or collection
        join_field = intent.get("join_field") or label
        return (
            f'db.{collection}.aggregate(['
            f' {{ $lookup: {{ from: "{secondary}", localField: "{join_field}", foreignField: "{join_field}", as: "joined" }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {metric_leaf}: "${metric}", joined_count: {{ $size: "$joined" }} }} }},'
            f' {{ $sort: {{ joined_count: -1 }} }}'
            f"])"
        )

    if pattern == "graph_recursive_deep":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $graphLookup: {{ from: "{collection}", startWith: "${label}", connectFromField: "{label}", connectToField: "_id", as: "chain", maxDepth: 3 }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, chain_depth: {{ $size: "$chain" }} }} }},'
            f' {{ $sort: {{ chain_depth: -1 }} }}'
            f"])"
        )

    if pattern == "percentile_approximation":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $group: {{ _id: "${category}", '
            f'p50: {{ $percentile: {{ input: "${metric}", p: [0.5], method: "approximate" }} }}, '
            f'p95: {{ $percentile: {{ input: "${metric}", p: [0.95], method: "approximate" }} }} }} }},'
            f' {{ $project: {{ _id: 0, {category}: "$_id", p50: 1, p95: 1 }} }}'
            f"])"
        )

    if pattern == "universal_quantifier":
        if not array_f:
            raise ValueError("universal_quantifier requires an array field")
        return (
            f'db.{collection}.aggregate(['
            f' {{ $match: {{ $expr: {{ $allElementsTrue: {{ $map: {{ input: "${array_f}", as: "item", in: {{ $gt: ["$$item", {threshold}] }} }} }} }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {array_f}: 1 }} }}'
            f"])"
        )

    if pattern == "window_function":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $setWindowFields: {{ sortBy: {{ "{time_f}": 1 }}, output: {{ running_total: {{ $sum: "${metric}", window: {{ documents: ["unbounded", "current"] }} }} }} }} }},'
            f' {{ $project: {{ _id: 0, {label}: 1, {metric_leaf}: "${metric}", running_total: 1 }} }}'
            f"])"
        )

    if pattern == "filter_then_count":
        return (
            f'db.{collection}.aggregate(['
            f' {{ $match: {{ "{metric}": {{ $gt: {threshold} }} }} }},'
            f' {{ $count: "total" }}'
            f"])"
        )

    raise ValueError(f"Unsupported pattern family: {pattern}")


# ---------------------------------------------------------------------------
# derive_canonical_form_set
# ---------------------------------------------------------------------------

def derive_canonical_form_set(mql: str, pattern_family: str) -> CanonicalFormSet:
    root_tokens = tuple(sorted(set(extract_root_stage_tokens(mql))))
    all_tokens = tuple(sorted(set(extract_operator_tokens(mql))))
    must_contain = tuple(
        token for token in all_tokens if token in _REQUIRED_OPERATORS.get(pattern_family, set())
    )
    return CanonicalFormSet(
        must_contain=must_contain,
        must_not_contain=(),
        must_contain_at_root=root_tokens,
        must_not_contain_at_root=(),
        known_variants=(canonical_text(mql),),
    )


# ---------------------------------------------------------------------------
# build_qir
# ---------------------------------------------------------------------------

def build_qir(si: StructuredIntent) -> QIR:
    intent = si.intent
    pf = intent["pattern_family"]
    primary = _PRIMARY_OPERATOR.get(pf)
    if primary is None:
        raise ValueError(f"No primary operator mapping for pattern family: {pf}")
    return QIR(
        pattern_family=pf,
        primary_operator=primary,
        input_shape={"collections": [intent["collection"]]},
        output_shape=si.output,
        referenced_fields=tuple(
            field
            for field in (
                intent["label_field"],
                intent["metric_field"],
                intent.get("category_field"),
                intent.get("time_field"),
                intent.get("array_field"),
                intent.get("secondary_collection"),
            )
            if field
        ),
    )


# ---------------------------------------------------------------------------
# generate_nl_queries – 23 pattern families
# ---------------------------------------------------------------------------

def generate_nl_queries(
    si: StructuredIntent,
    model_runner: ExternalModelRunner | None = None,
    schema_payload: dict[str, Any] | None = None,
    witness_payload: dict[str, Any] | None = None,
) -> list[str]:
    model_runner = model_runner or NoopExternalModelRunner()
    intent = si.intent
    metric = intent["metric_field"]
    label = intent["label_field"]
    pattern = intent["pattern_family"]
    category = intent.get("category_field") or label
    time_f = intent.get("time_field") or label
    array_f = intent.get("array_field") or "values"

    base = _NL_TEMPLATES.get(pattern)
    if base is None:
        base = _fallback_nl(si)
    else:
        base = [
            t.format(
                metric=metric,
                label=label,
                category=category,
                time=time_f,
                array=array_f,
                threshold=si.properties.get("threshold", 70),
                top_k=si.properties.get("top_k", 3),
            )
            for t in base
        ]

    llm_queries = _generate_llm_nl_queries(
        si, model_runner=model_runner, schema_payload=schema_payload, witness_payload=witness_payload,
    )
    if llm_queries:
        return llm_queries
    mapping = dict(zip(SPECIFICITY_ORDER, base))
    return [mapping[level] for level in SPECIFICITY_ORDER]


_NL_TEMPLATES: dict[str, list[str]] = {
    "simple_filter": [
        "找出 {metric} 大于 {threshold} 的记录，并返回 {label} 和该数值。",
        "筛出数值超过阈值的项。",
        "筛选 {metric} 超过阈值的文档并显示 {label}。",
        "用聚合方式找出高值记录。",
        "Identify high-value rows by {metric}.",
    ],
    "filter_then_aggregate": [
        "先筛选 {metric} 高于阈值的记录，再按 {category} 汇总数量和均值。",
        "过滤高值后按类别统计。",
        "对超过阈值的数据做分组聚合。",
        "筛选再聚合，返回每组命中量。",
        "Filter then aggregate by {category}.",
    ],
    "group_then_aggregate": [
        "按 {category} 分组，计算 {metric} 的均值和样本数。",
        "给我每个类别的平均值和记录数。",
        "先分组再做聚合统计。",
        "比较不同组的平均表现。",
        "Group by {category} and aggregate {metric}.",
    ],
    "top_k_by_aggregate": [
        "按 {label} 汇总 {metric}，返回总量最高的前 {top_k} 项。",
        "统计每个 {label} 的总值并给我 Top {top_k}。",
        "聚合 {metric} 后按 {label} 排名。",
        "用分组加排序找出最强的几项。",
        "Return the top {top_k} groups by aggregated {metric}.",
    ],
    "time_window_aggregate": [
        "按 {time} 聚合 {metric}，返回每个时间桶的平均值。",
        "看一下按时间分桶后的均值趋势。",
        "给我 {metric} 的时间窗口平均。",
        "按时间聚合输出平均值。",
        "Compute the average {metric} per time bucket.",
    ],
    "facet_split": [
        "把 {metric} 按阈值分成高低两组，并分别按 {category} 汇总。",
        "做一个高低分面的统计视图。",
        "按阈值拆成两个分支再统计类别分布。",
        "用 facet 输出高值和低值的双视图。",
        "Split into high and low facets by {metric}.",
    ],
    "anomaly_vs_baseline": [
        "按 {time} 计算 {metric} 的滑动基线，只保留显著高于基线的异常记录。",
        "找出超过滚动平均基线的异常点。",
        "基于窗口平均检测异常记录。",
        "用滑动基线筛出高于预期的记录。",
        "Detect anomalies above the rolling baseline of {metric}.",
    ],
    "existential_quantifier": [
        "找出 {array} 中至少有一个值高于阈值的记录。",
        "筛出数组里存在高值元素的项。",
        "只保留数组中命中条件的文档。",
        "检查是否存在任一元素超过阈值。",
        "Keep rows where any array element exceeds the threshold.",
    ],
    "window_function_with_facet_filter": [
        "先按 {time} 计算 {metric} 的窗口平均，再用 facet 结合全局基线，只保留高于整体基线的记录。",
        "做窗口平均并与全局基线比较，输出高于基线的结果。",
        "窗口统计后再做双分面筛选。",
        "利用窗口函数和 facet 找出持续高于基线的项。",
        "Combine windowed averages with a facet baseline filter on {metric}.",
    ],
    "project_only": [
        "只提取 {label} 和 {metric} 两个字段。",
        "做一次纯投影。",
        "返回文档中的 {label} 和 {metric} 字段。",
        "提取选定的列。",
        "Project only {label} and {metric} from the collection.",
    ],
    "null_vs_missing_disambig": [
        "区分 {metric} 字段是显式 null、缺失还是有值，并统计每类数量。",
        "分辨空值和缺失字段。",
        "检查字段的 null 与 missing 分布。",
        "统计字段的三种存在状态。",
        "Classify {metric} as explicit null, missing, or has value.",
    ],
    "coalesce_with_default": [
        "对 {metric} 为空的记录用默认值替换，然后按值排序。",
        "空值替换后做排序。",
        "用 ifNull 填补缺失再展示。",
        "给缺失的字段一个默认值。",
        "Coalesce null {metric} with a default value and sort.",
    ],
    "polymorphic_branch": [
        "按 {metric} 的实际类型（数值/字符串/null/其他）分支统计。",
        "用 switch 做多态分支统计。",
        "检查字段的类型多态性。",
        "按运行时类型分组计数。",
        "Branch on the runtime type of {metric} and count each branch.",
    ],
    "type_introspection": [
        "统计 {metric} 字段的 BSON 类型分布。",
        "看看字段里有几种不同类型。",
        "分析字段的类型多样性。",
        "给出类型内省结果。",
        "Introspect the BSON type distribution of {metric}.",
    ],
    "dynamic_key_expansion": [
        "把整条文档展开成键值对，统计每个字段出现的次数和类型。",
        "动态展开键值做统计。",
        "用 objectToArray 分析文档结构。",
        "枚举文档的字段名和类型。",
        "Expand document keys and count occurrences of each field.",
    ],
    "array_positional_select": [
        "取出 {array} 的第一个和最后一个元素，并返回数组长度。",
        "提取数组首尾元素。",
        "用位置选择器取数组元素。",
        "获取数组边界值。",
        "Select the first and last elements from {array}.",
    ],
    "array_reshape": [
        "把 {array} 展开，按 {label} 重新分组收集成新数组。",
        "展开再聚合，做数组变形。",
        "用 unwind + group 重塑数组。",
        "把嵌套数组拍平到分组内。",
        "Reshape {array} by unwinding and re-grouping by {label}.",
    ],
    "lookup_join": [
        "用 lookup 关联另一个集合，统计每条记录的关联数。",
        "做一个跨集合的连接查询。",
        "用 $lookup 把两个集合关联起来。",
        "关联后统计匹配项数。",
        "Join with a secondary collection via $lookup and count matches.",
    ],
    "graph_recursive_deep": [
        "用 graphLookup 递归遍历关联链，返回每个节点的链深度。",
        "做递归的图查询。",
        "用 $graphLookup 找关联路径。",
        "递归展开层级关系。",
        "Recursively traverse the graph and return chain depth per node.",
    ],
    "percentile_approximation": [
        "按 {category} 分组，计算 {metric} 的 P50 和 P95 分位数。",
        "给出各组的中位数和高分位数。",
        "用近似分位数做统计。",
        "计算分组的百分位数。",
        "Compute approximate P50 and P95 of {metric} per {category}.",
    ],
    "universal_quantifier": [
        "找出 {array} 中所有元素都高于阈值的记录。",
        "筛出数组全部满足条件的项。",
        "只保留数组中每一个元素都达标的文档。",
        "检查是否所有元素都超过阈值。",
        "Keep rows where every array element exceeds the threshold.",
    ],
    "window_function": [
        "按 {time} 排序，计算 {metric} 的累计总和。",
        "做一个运行总计的窗口计算。",
        "用 setWindowFields 算累计值。",
        "按时间序列做累积统计。",
        "Compute a running total of {metric} sorted by {time}.",
    ],
    "filter_then_count": [
        "统计 {metric} 大于 {threshold} 的记录总数。",
        "计算满足条件的文档数量。",
        "筛选后只返回计数。",
        "给出通过条件的总行数。",
        "Count documents where {metric} exceeds {threshold}.",
    ],
}


def _fallback_nl(si: StructuredIntent) -> list[str]:
    metric = si.intent["metric_field"]
    label = si.intent["label_field"]
    return [
        f"对 {label} 和 {metric} 做聚合分析。",
        "做一次通用聚合查询。",
        f"分析 {metric} 的分布。",
        "用聚合管道处理数据。",
        f"Aggregate {metric} across documents.",
    ]


# ---------------------------------------------------------------------------
# build_checker / build_mutations
# ---------------------------------------------------------------------------

def build_checker(si: StructuredIntent, mql: str, qir: QIR) -> dict[str, Any]:
    return {
        "pattern_family": si.intent["pattern_family"],
        "primary_operator": qir.primary_operator,
        "must_contain": list(si.canonical_form_set.must_contain),
        "must_contain_at_root": list(si.canonical_form_set.must_contain_at_root),
        "query": canonical_text(mql),
    }


def build_mutations(si: StructuredIntent, mql: str) -> list[dict[str, Any]]:
    pattern = si.intent["pattern_family"]

    if pattern == "simple_filter":
        return [
            {"mutation_id": "wrong_operator", "query": mql.replace("$gt", "$lt", 1)},
            {"mutation_id": "tight_threshold", "query": mql.replace(str(si.properties["threshold"]), str(si.properties["threshold"] + 20), 1)},
        ]
    if pattern == "filter_then_aggregate":
        return [
            {"mutation_id": "wrong_match", "query": mql.replace("$gt", "$lt", 1)},
            {"mutation_id": "wrong_group_metric", "query": mql.replace("$avg", "$sum", 1)},
        ]
    if pattern == "group_then_aggregate":
        return [
            {"mutation_id": "wrong_aggregate", "query": mql.replace("$avg", "$sum", 1)},
            {"mutation_id": "reverse_sort", "query": mql.replace(": -1", ": 1", 1)},
        ]
    if pattern == "top_k_by_aggregate":
        return [
            {"mutation_id": "wrong_operator", "query": mql.replace("$sum", "$avg", 1)},
            {"mutation_id": "wrong_limit", "query": mql.replace(f'$limit: {si.properties["top_k"]}', f'$limit: {si.properties["top_k"] + 1}', 1)},
        ]
    if pattern == "time_window_aggregate":
        return [
            {"mutation_id": "wrong_operator", "query": mql.replace("$avg", "$sum", 1)},
            {"mutation_id": "reverse_sort", "query": mql.replace(": 1", ": -1", 1)},
        ]
    if pattern == "facet_split":
        return [
            {"mutation_id": "facet_threshold_flip", "query": mql.replace("$gt", "$lt", 1)},
            {"mutation_id": "drop_facet_branch", "query": mql.replace("low: [", "low_disabled: [", 1)},
        ]
    if pattern == "anomaly_vs_baseline":
        return [
            {"mutation_id": "baseline_too_low", "query": mql.replace(str(si.properties["baseline_multiplier"]), "0.8", 1)},
            {"mutation_id": "window_reverse", "query": mql.replace(f'[-{si.properties["window_size"] - 1}, 0]', "[0, 0]", 1)},
        ]
    if pattern == "existential_quantifier":
        return [
            {"mutation_id": "existential_to_universal_like", "query": mql.replace("$anyElementTrue", "$allElementsTrue", 1)},
            {"mutation_id": "lower_threshold", "query": mql.replace(str(si.properties["threshold"]), str(si.properties["threshold"] - 20), 1)},
        ]
    if pattern == "window_function_with_facet_filter":
        return [
            {"mutation_id": "drop_window", "query": mql.replace("$setWindowFields", "$group", 1)},
            {"mutation_id": "facet_threshold_flip", "query": mql.replace("$gt", "$lt", 1)},
        ]
    if pattern == "project_only":
        return [
            {"mutation_id": "add_extra_field", "query": mql.replace("_id: 0", "_id: 1", 1)},
        ]
    if pattern == "null_vs_missing_disambig":
        return [
            {"mutation_id": "confuse_null_missing", "query": mql.replace('"missing"', '"null"', 1)},
            {"mutation_id": "drop_cond", "query": mql.replace("$cond", "$literal", 1)},
        ]
    if pattern == "coalesce_with_default":
        return [
            {"mutation_id": "wrong_default", "query": mql.replace("$ifNull", "$literal", 1)},
            {"mutation_id": "reverse_sort", "query": mql.replace(": -1", ": 1", 1)},
        ]
    if pattern == "polymorphic_branch":
        return [
            {"mutation_id": "drop_switch_default", "query": mql.replace('"other"', '"unknown"', 1)},
            {"mutation_id": "wrong_type_check", "query": mql.replace('"numeric"', '"number"', 1)},
        ]
    if pattern == "type_introspection":
        return [
            {"mutation_id": "wrong_type_op", "query": mql.replace("$type", "$toString", 1)},
        ]
    if pattern == "dynamic_key_expansion":
        return [
            {"mutation_id": "wrong_expansion", "query": mql.replace("$objectToArray", "$arrayToObject", 1)},
        ]
    if pattern == "array_positional_select":
        return [
            {"mutation_id": "wrong_index", "query": mql.replace(", 0]", ", 1]", 1)},
            {"mutation_id": "drop_size", "query": mql.replace("$size", "$type", 1)},
        ]
    if pattern == "array_reshape":
        return [
            {"mutation_id": "skip_unwind", "query": mql.replace("$unwind", "$project", 1)},
            {"mutation_id": "wrong_accumulator", "query": mql.replace("$push", "$addToSet", 1)},
        ]
    if pattern == "lookup_join":
        return [
            {"mutation_id": "wrong_from", "query": mql.replace("$lookup", "$group", 1)},
            {"mutation_id": "reverse_sort", "query": mql.replace(": -1", ": 1", 1)},
        ]
    if pattern == "graph_recursive_deep":
        return [
            {"mutation_id": "shallow_depth", "query": mql.replace("maxDepth: 3", "maxDepth: 0", 1)},
            {"mutation_id": "wrong_connect", "query": mql.replace("$graphLookup", "$lookup", 1)},
        ]
    if pattern == "percentile_approximation":
        return [
            {"mutation_id": "wrong_percentile", "query": mql.replace("0.5", "0.1", 1)},
            {"mutation_id": "wrong_method", "query": mql.replace('"approximate"', '"exact"', 1)},
        ]
    if pattern == "universal_quantifier":
        return [
            {"mutation_id": "universal_to_existential", "query": mql.replace("$allElementsTrue", "$anyElementTrue", 1)},
            {"mutation_id": "lower_threshold", "query": mql.replace(str(si.properties["threshold"]), str(si.properties["threshold"] - 20), 1)},
        ]
    if pattern == "window_function":
        return [
            {"mutation_id": "wrong_accumulator", "query": mql.replace("$sum", "$avg", 1)},
            {"mutation_id": "wrong_window", "query": mql.replace('"unbounded"', "0", 1)},
        ]
    if pattern == "filter_then_count":
        return [
            {"mutation_id": "wrong_operator", "query": mql.replace("$gt", "$lt", 1)},
            {"mutation_id": "drop_count", "query": mql.replace("$count", "$group", 1)},
        ]
    return [
        {"mutation_id": "generic_flip", "query": mql.replace("$gt", "$lt", 1)},
    ]


# ---------------------------------------------------------------------------
# LLM NLQ generation helpers
# ---------------------------------------------------------------------------

def _generate_llm_nl_queries(
    si: StructuredIntent,
    model_runner: ExternalModelRunner,
    schema_payload: dict[str, Any] | None,
    witness_payload: dict[str, Any] | None,
) -> list[str]:
    response = model_runner.generate(
        "phase_c_nlq_x5",
        {
            "prompt": (
                "Generate five semantically equivalent NL queries for the given structured intent. "
                "Return JSON with a top-level 'candidates' array of exactly five strings in specificity order "
                "[L1, L0, L2, L3, L4].\n"
                f"SI: {si.to_dict()}\n"
                f"Schema: {schema_payload or {}}\n"
                f"Witness sample: {_sample_witness(witness_payload)}"
            )
        },
    )
    candidates = response.get("candidates", [])
    if len(candidates) == 5 and all(isinstance(item, str) and item.strip() for item in candidates):
        return [item.strip() for item in candidates]
    return []


def _sample_witness(witness_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not witness_payload:
        return {}
    sampled: dict[str, Any] = {}
    for collection, documents in witness_payload.items():
        sampled[collection] = documents[:2]
    return sampled
