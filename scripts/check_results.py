"""Recount the numbers of RESULTS.md from the per-question answers in ``results/``.

Usage (from the repository root)::

    python scripts/check_results.py
    python scripts/check_results.py --dataset-dir release/tend-native-mongodb-v1

The first form needs only ``results/``. With ``--dataset-dir`` (the restored release, see the
README), the benchmark statistics and the checks that read the reference pipelines of
``data/TEND.json`` run as well. Scores are the stored ones, and nothing is executed or
re-evaluated. Exit status 1 if any number differs from RESULTS.md.

Not recounted here, because their per-question answers are not in ``results/``: runs 2 and 3
of the stability check, the pre-registered subset of the dynamic-key detector comparison,
which needs a superseded GPT-5.6-Luna run, and the numbers of the submitted version. Also not
recounted: the ReAct step and SAG call counts, which come from the run records, and the facts
about stored documents and card sizes, which need the release MongoDB, and the operator counts
of MongoDB's natural-language-to-mongosh benchmark, which come from that dataset.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results"
DBS = [
    "california_schools",
    "card_games",
    "codebase_community",
    "debit_card_specializing",
    "european_football_2",
    "financial",
    "formula_1",
    "student_club",
    "superhero",
    "thrombosis_prediction",
    "toxicology",
]
WRONG_ROWS = {"value_mismatch", "row_subset", "row_superset", "order_only", "row_count_exceeded"}
MECHANICAL = {"no_submission", "exec_error", "empty"}
STRUCT = {"order_only", "row_subset", "row_superset"}
CATEGORIES = [
    "dynamic-key map",
    "nested event stream",
    "polymorphic shape",
    "missing versus present",
    "attribute bag",
]
# Operator sets of the release statistics (Table I of the paper).
ARRAY_OPS = {
    "$unwind",
    "$size",
    "$filter",
    "$map",
    "$isArray",
    "$addToSet",
    "$push",
    "$slice",
    "$arrayElemAt",
}
DYNAMIC_KEY_OPS = {"$objectToArray", "$arrayToObject", "$getField", "$setField"}

failures: list[str] = []


def load(name: str) -> dict[tuple[str, int], dict]:
    with gzip.open(RESULTS / f"{name}.jsonl.gz", "rt") as fh:
        return {(r["db_id"], r["record_id"]): r for r in map(json.loads, fh)}


def ok(row: dict | None) -> bool:
    return bool(row) and row["exc"] == 1


def correct(system: dict) -> int:
    return sum(r["exc"] for r in system.values())


def per_db(system: dict) -> list[int]:
    return [sum(r["exc"] for (db, _), r in system.items() if db == name) for name in DBS]


def wins(a: dict, b: dict) -> tuple[int, int]:
    keys = a.keys() & b.keys()
    return sum(ok(a[k]) and not ok(b[k]) for k in keys), sum(
        ok(b[k]) and not ok(a[k]) for k in keys
    )


def mcnemar(b: int, c: int) -> float:
    n, k = b + c, min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2**n)


def p2(p: float) -> str:
    return f"{p:.2g}"


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}"


def share(system: dict, outcomes: set[str], keys=None) -> int:
    keys = system.keys() if keys is None else keys
    return sum(system[k]["outcome"] in outcomes for k in keys)


def acc(system: dict, keys) -> float:
    keys = list(keys)
    return 100 * sum(ok(system[k]) for k in keys) / len(keys)


def check(label: str, got, want) -> None:
    status = "ok" if got == want else "MISMATCH"
    if got != want:
        failures.append(label)
    print(f"  {status:8s} {label}: {got}" + ("" if got == want else f" (RESULTS.md: {want})"))


def comparison(label: str, a: dict, b: dict, want: tuple) -> None:
    won, lost = wins(a, b)
    check(label, (won, lost, p2(mcnemar(won, lost))), want)


def profile(system: dict) -> tuple[str, ...]:
    """EXC, EXF1, and the outcome breakdown (Fail, Exec, Empty, Struct., Value) in percent."""
    n = len(system)
    exf1 = sum(r["exf1"] or 0 for r in system.values())
    buckets = ({"no_submission"}, {"exec_error"}, {"empty"}, STRUCT, {"value_mismatch"})
    return (pct(correct(system), n), f"{100 * exf1 / n:.1f}") + tuple(
        pct(share(system, b), n) for b in buckets
    )


def slice_row(system: dict, labels: dict) -> list[str]:
    """EXC per structure category, then per resistance to SQL transfer, in percent."""
    row = []
    for column, values in (
        ("structure_category", CATEGORIES),
        ("sql_transfer_resistance", ("medium", "strong", "weak")),
    ):
        for value in values:
            row.append(f"{acc(system, [k for k, r in labels.items() if r[column] == value]):.1f}")
    return row


def operators(node) -> set[str]:
    if isinstance(node, dict):
        return {k for k in node if k.startswith("$")}.union(*(operators(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(operators(v) for v in node)) if node else set()
    return set()


def has_dotted_path(node) -> bool:
    if isinstance(node, dict):
        return any(
            ("." in k and not k.startswith("$")) or has_dotted_path(v) for k, v in node.items()
        )
    if isinstance(node, list):
        return any(has_dotted_path(v) for v in node)
    return (
        isinstance(node, str) and node.startswith("$") and not node.startswith("$$") and "." in node
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset-dir", type=Path, help="restored release directory")
    args = parser.parse_args()

    ds = {
        name: load(
            "SAG/DeepSeek/SAG" if name == "SAG" else f"Baselines/DeepSeek/{name.replace(' ', '_')}"
        )
        for name in (
            "SAG",
            "ReAct",
            "Data-rich Direct",
            "Sampled-doc Direct",
            "DIN-SQL-inspired",
            "SQL Pivot",
            "Schema Direct",
            "NLQ-only Direct",
        )
    }
    lu = {
        name: load(
            "SAG/Luna/SAG"
            if name == "SAG"
            else f"Baselines/Luna/{name.replace(' + ', '+').replace(' ', '_')}"
        )
        for name in (
            "SAG",
            "Direct + conventions",
            "Data-rich Direct",
            "ReAct",
            "SQL Pivot + real DDL",
        )
    }
    ab = {
        name: load(f"Ablation_study/DeepSeek/{name.replace(' ', '_')}")
        for name in (
            "Full SAG",
            "One decode",
            "One candidate",
            "No value grounding",
            "No grounding",
            "Top-level fields only",
            "No dynamic-key collapse",
        )
    }
    labels = {}
    with open(RESULTS / "task_labels.csv") as fh:
        for row in csv.DictReader(fh):
            labels[(row["db_id"], int(row["record_id"]))] = row

    print("Main results, DeepSeek-V4-Flash")
    sag = ds["SAG"]
    check("SAG correct", correct(sag), 487)
    for name, total, want in (
        ("ReAct", 373, (205, 91, "2.9e-11")),
        ("Data-rich Direct", 352, (214, 79, "1.5e-15")),
        ("Sampled-doc Direct", 346, (214, 73, "3.1e-17")),
        ("DIN-SQL-inspired", 339, (215, 67, "2.9e-19")),
        ("SQL Pivot", 310, (246, 69, "1.9e-24")),
        ("Schema Direct", 3, (484, 0, "4e-146")),
        ("NLQ-only Direct", 2, (485, 0, "2e-146")),
    ):
        check(f"{name} correct", correct(ds[name]), total)
        comparison(f"SAG vs {name} (W, L, p)", sag, ds[name], want)
    comparison(
        "DIN-SQL-inspired vs Direct (W, L, p)",
        ds["DIN-SQL-inspired"],
        ds["Data-rich Direct"],
        (88, 101, "0.38"),
    )
    check("per database, SAG", per_db(sag), [34, 59, 34, 32, 54, 56, 18, 47, 48, 48, 57])
    check(
        "per database, Direct",
        per_db(ds["Data-rich Direct"]),
        [24, 45, 23, 22, 30, 56, 19, 45, 17, 34, 37],
    )
    check(
        "per database, DIN-SQL-inspired",
        per_db(ds["DIN-SQL-inspired"]),
        [28, 36, 27, 18, 34, 54, 22, 40, 18, 25, 37],
    )
    for name, want in (
        ("NLQ-only Direct", ("0.2", "0.2", "8.2", "1.1", "89.4", "0.0", "1.2")),
        ("Schema Direct", ("0.2", "0.4", "6.3", "15.3", "69.3", "0.1", "8.8")),
        ("Sampled-doc Direct", ("28.6", "29.4", "2.1", "4.7", "7.2", "1.7", "55.7")),
        ("Data-rich Direct", ("29.1", "29.7", "2.7", "2.1", "5.8", "1.1", "59.2")),
        ("SQL Pivot", ("25.6", "26.2", "1.5", "4.9", "8.8", "1.5", "57.8")),
        ("ReAct", ("30.8", "34.6", "9.0", "0.2", "0.3", "2.7", "56.9")),
        ("DIN-SQL-inspired", ("28.0", "30.8", "5.1", "3.9", "6.0", "1.8", "55.2")),
        ("SAG", ("40.2", "43.8", "1.7", "0.0", "1.0", "2.2", "54.9")),
    ):
        check(f"{name}: EXC, EXF1, Fail, Exec, Empty, Struct., Value (%)", profile(ds[name]), want)

    print("Main results, GPT-5.6-Luna")
    check("SAG correct", correct(lu["SAG"]), 514)
    for name, total, want in (
        ("Direct + conventions", 445, (152, 83, "8e-06")),
        ("Data-rich Direct", 421, (176, 83, "7.6e-09")),
        ("ReAct", 390, (207, 83, "2.2e-13")),
        ("SQL Pivot + real DDL", 269, (295, 50, "2.1e-43")),
    ):
        check(f"{name} correct", correct(lu[name]), total)
        comparison(f"SAG vs {name} (W, L, p)", lu["SAG"], lu[name], want)
    for name, want in (
        ("SAG", [53, 58, 34, 42, 55, 52, 29, 44, 43, 44, 60]),
        ("Direct + conventions", [37, 53, 33, 29, 45, 63, 27, 52, 18, 39, 49]),
        ("Data-rich Direct", [31, 51, 27, 24, 41, 59, 28, 49, 18, 37, 56]),
        ("ReAct", [34, 37, 24, 23, 49, 43, 29, 45, 22, 42, 42]),
        ("SQL Pivot + real DDL", [18, 37, 14, 12, 31, 42, 23, 39, 11, 8, 34]),
    ):
        check(f"per database, {name}", per_db(lu[name]), want)
    for name, want in (
        ("Data-rich Direct", ("34.8", "37.4", "4.0", "1.7", "1.5", "1.2", "56.9")),
        ("Direct + conventions", ("36.8", "40.7", "2.4", "1.3", "2.3", "1.8", "55.4")),
        ("SQL Pivot + real DDL", ("22.2", "24.3", "2.6", "3.9", "4.7", "0.9", "65.7")),
        ("ReAct", ("32.2", "35.0", "9.0", "0.7", "3.6", "1.3", "53.2")),
        ("SAG", ("42.5", "46.3", "0.2", "0.0", "0.2", "1.7", "55.4")),
    ):
        check(f"{name}: EXC, EXF1, Fail, Exec, Empty, Struct., Value (%)", profile(lu[name]), want)

    print("Ablation study, DeepSeek-V4-Flash")
    ref = ab["Full SAG"]
    check("Full SAG correct", correct(ref), 479)
    for name, total, want, dbs in (
        ("One decode", 457, (93, 71, "0.1"), [40, 53, 33, 29, 47, 53, 20, 44, 43, 42, 53]),
        ("One candidate", 456, (83, 60, "0.065"), [40, 57, 26, 31, 49, 48, 21, 44, 46, 41, 53]),
        (
            "No value grounding",
            446,
            (102, 69, "0.014"),
            [44, 53, 35, 27, 53, 50, 20, 42, 45, 45, 32],
        ),
        ("No grounding", 372, (178, 71, "8.9e-12"), [37, 39, 27, 18, 48, 38, 16, 50, 18, 30, 51]),
        (
            "Top-level fields only",
            142,
            (365, 28, "5.8e-76"),
            [25, 13, 7, 11, 24, 6, 8, 10, 14, 21, 3],
        ),
        (
            "No dynamic-key collapse",
            430,
            (118, 69, "0.00042"),
            [28, 48, 30, 40, 49, 50, 15, 58, 45, 43, 24],
        ),
    ):
        check(f"{name} correct", correct(ab[name]), total)
        comparison(f"full SAG vs {name} (W, L, p)", ref, ab[name], want)
        check(f"per database, {name}", per_db(ab[name]), dbs)
    check(
        "per database, Full SAG",
        per_db(ref),
        [44, 55, 33, 29, 51, 53, 23, 46, 45, 45, 55],
    )

    print("Ablation study, narrower dynamic-key detector, GPT-5.6-Luna")
    on = load("Ablation_study/Luna/Full_SAG")
    off = load("Ablation_study/Luna/Narrower_dynamic-key_detector")
    check("full / narrower detector correct", (correct(on), correct(off)), (512, 462))
    comparison("all 11 databases (W, L, p)", on, off, (89, 39, "1.2e-05"))
    for campaign, want_counts, want_wl in (
        ("2026-08", (398, 360), (60, 22, "3.2e-05")),
        ("2026-09", (114, 102), (29, 17, "0.1")),
    ):
        a = {k: r for k, r in on.items() if r["campaign"] == campaign}
        b = {k: off[k] for k in a}
        check(f"campaign {campaign} correct", (correct(a), correct(b)), want_counts)
        comparison(f"campaign {campaign} (W, L, p)", a, b, want_wl)
    august_on = {k: r for k, r in on.items() if r["campaign"] == "2026-08"}
    check(
        "August full-detector side equals the main SAG answers",
        all(r["exc"] == lu["SAG"][k]["exc"] for k, r in august_on.items()),
        True,
    )

    print("Stability, DeepSeek-V4-Flash (run 1 and the ablation reference)")
    subset = [k for k in sag if k[0] in ("superhero", "card_games", "financial")]
    for name, want in (("superhero", 48), ("card_games", 59), ("financial", 56)):
        check(f"{name}, run 1", sum(ok(sag[k]) for k in subset if k[0] == name), want)
    check(
        "330-question totals of run 1 and the ablation reference",
        (sum(ok(sag[k]) for k in subset), sum(ok(ref[k]) for k in subset)),
        (163, 153),
    )
    check(
        "flips between the two full runs, %",
        pct(sum(ok(sag[k]) != ok(ref[k]) for k in sag), len(sag)),
        "9.8",
    )

    print("By structural label")
    check(
        "tasks per category and per resistance level",
        [
            len([k for k, r in labels.items() if r[c] == v])
            for c, vs in (
                ("structure_category", CATEGORIES),
                ("sql_transfer_resistance", ("medium", "strong", "weak")),
            )
            for v in vs
        ],
        [1013, 130, 36, 18, 13, 111, 1087, 12],
    )
    for model, systems, table in (
        (
            "DeepSeek-V4-Flash",
            ds,
            (
                ("Sampled-doc Direct", "29.8 19.2 25.0 55.6 0.0 21.6 29.2 41.7"),
                ("Data-rich Direct", "29.5 28.5 25.0 38.9 0.0 23.4 29.6 33.3"),
                ("SQL Pivot", "26.0 22.3 33.3 33.3 0.0 21.6 25.7 58.3"),
                ("ReAct", "28.5 45.4 41.7 44.4 15.4 42.3 29.6 33.3"),
                ("DIN-SQL-inspired", "28.6 23.8 25.0 44.4 7.7 22.5 28.5 33.3"),
                ("SAG", "40.8 43.1 11.1 38.9 53.8 33.3 41.1 25.0"),
            ),
        ),
        (
            "GPT-5.6-Luna",
            lu,
            (
                ("Data-rich Direct", "35.0 33.8 30.6 61.1 0.0 36.0 34.6 41.7"),
                ("Direct + conventions", "38.1 29.2 30.6 55.6 0.0 28.8 37.6 33.3"),
                ("SQL Pivot + real DDL", "22.4 17.7 27.8 50.0 0.0 22.5 22.0 41.7"),
                ("ReAct", "31.1 43.8 27.8 44.4 0.0 37.8 31.7 25.0"),
                ("SAG", "44.2 33.8 16.7 50.0 53.8 35.1 43.4 25.0"),
            ),
        ),
    ):
        for name, want in table:
            check(
                f"{model} {name}: EXC by label (%)", slice_row(systems[name], labels), want.split()
            )

    print("Further analyses in the paper")
    n = len(sag)
    data_systems = [
        "Sampled-doc Direct",
        "Data-rich Direct",
        "SQL Pivot",
        "ReAct",
        "DIN-SQL-inspired",
        "SAG",
    ]
    shares = [100 * share(ds[s], WRONG_ROWS) / n for s in data_systems]
    check(
        "wrong-row share of the systems that see stored data, %",
        (f"{min(shares):.1f}", f"{max(shares):.1f}"),
        ("57.0", "60.2"),
    )
    check(
        "mechanical failures of SAG, Direct, ReAct, %",
        [pct(share(ds[s], MECHANICAL), n) for s in ("SAG", "Data-rich Direct", "ReAct")],
        ["2.6", "10.7", "9.5"],
    )
    wrong = [k for k in sag if sag[k]["outcome"] in WRONG_ROWS]
    check(
        "SAG wrong-row answers, with EXF1 = 0, partly overlapping, wrong order only",
        (
            len(wrong),
            sum((sag[k]["exf1"] or 0) == 0 for k in wrong),
            sum(0 < (sag[k]["exf1"] or 0) < 1 for k in wrong),
            sum(sag[k]["exf1"] == 1 and sag[k]["outcome"] == "order_only" for k in wrong),
        ),
        (691, 594, 90, 7),
    )
    dwrong = [
        k for k in ds["Data-rich Direct"] if ds["Data-rich Direct"][k]["outcome"] in WRONG_ROWS
    ]
    check(
        "Direct wrong-row answers with EXF1 = 0, %",
        f"{100 * sum((ds['Data-rich Direct'][k]['exf1'] or 0) == 0 for k in dwrong) / len(dwrong):.0f}",
        "88",
    )
    union = {k for k in sag if any(ok(s[k]) for s in ds.values())}
    only_sag = {
        k
        for k in sag
        if ok(sag[k]) and not any(ok(s[k]) for name, s in ds.items() if name != "SAG")
    }
    none = {k for k in sag if not any(ok(s[k]) for s in list(ds.values()) + list(lu.values()))}
    check(
        "union of the eight systems, answered only by SAG, answered by none of 13 runs",
        (len(union), len(only_sag), len(none)),
        (657, 85, 466),
    )
    check(
        "SQL-transfer resistance strong / medium / weak",
        tuple(
            sum(r["sql_transfer_resistance"] == lv for r in labels.values())
            for lv in ("strong", "medium", "weak")
        ),
        (1087, 111, 12),
    )
    medium = [k for k, r in labels.items() if r["sql_transfer_resistance"] == "medium"]
    check(
        "medium resistance: ReAct, SAG, %",
        (f"{acc(ds['ReAct'], medium):.1f}", f"{acc(sag, medium):.1f}"),
        ("42.3", "33.3"),
    )
    missing = [k for k, r in labels.items() if r["structure_category"] == "missing versus present"]
    check(
        "missing versus present: baselines above SAG",
        sorted(s for s in ds if s != "SAG" and acc(ds[s], missing) > acc(sag, missing)),
        ["DIN-SQL-inspired", "ReAct", "Sampled-doc Direct"],
    )
    weak = [k for k, r in labels.items() if r["sql_transfer_resistance"] == "weak"]
    check(
        "weak resistance: SQL Pivot, SAG (correct)",
        (sum(ok(ds["SQL Pivot"][k]) for k in weak), sum(ok(sag[k]) for k in weak)),
        (7, 3),
    )
    lu_shares = [100 * share(lu[s], WRONG_ROWS) / n for s in lu]
    check(
        "GPT-5.6-Luna wrong-row share of every system, %",
        (f"{min(lu_shares):.1f}", f"{max(lu_shares):.1f}"),
        ("54.5", "66.6"),
    )
    baselines = [s for s in lu if s != "SAG"]
    check(
        "GPT-5.6-Luna weak resistance: SAG, best baseline, %",
        (f"{acc(lu['SAG'], weak):.1f}", f"{max(acc(lu[s], weak) for s in baselines):.1f}"),
        ("25.0", "41.7"),
    )
    for category, want in (
        ("dynamic-key map", ("44.2", "38.1")),
        ("attribute bag", ("53.8", "0.0")),
        ("polymorphic shape", ("16.7", "30.6")),
        ("nested event stream", ("33.8", "43.8")),
    ):
        keys = [k for k, r in labels.items() if r["structure_category"] == category]
        check(
            f"GPT-5.6-Luna {category}: SAG, best baseline, %",
            (f"{acc(lu['SAG'], keys):.1f}", f"{max(acc(lu[s], keys) for s in baselines):.1f}"),
            want,
        )
    check(
        "top-level card and reference, empty results %",
        (pct(share(ab["Top-level fields only"], {"empty"}), n), pct(share(ref, {"empty"}), n)),
        ("24.8", "0.2"),
    )
    check(
        "one decode: mechanical failures, wrong rows %",
        (pct(share(ab["One decode"], MECHANICAL), n), pct(share(ab["One decode"], WRONG_ROWS), n)),
        ("6.9", "55.3"),
    )
    check(
        "one candidate: mechanical failures, wrong rows %",
        (
            pct(share(ab["One candidate"], MECHANICAL), n),
            pct(share(ab["One candidate"], WRONG_ROWS), n),
        ),
        ("1.5", "60.8"),
    )
    check(
        "Direct + conventions vs Direct, GPT-5.6-Luna (W, L)",
        wins(lu["Direct + conventions"], lu["Data-rich Direct"]),
        (80, 56),
    )
    check(
        "Direct with sampled documents vs SQL Pivot (W, L)",
        wins(ds["Sampled-doc Direct"], ds["SQL Pivot"]),
        (108, 72),
    )
    check(
        "empty results of SQL Pivot and sampled-document Direct, %",
        (
            pct(share(ds["SQL Pivot"], {"empty"}), n),
            pct(share(ds["Sampled-doc Direct"], {"empty"}), n),
        ),
        ("8.8", "7.2"),
    )
    check(
        "Direct vs SQL Pivot + DDL, GPT-5.6-Luna (W, L)",
        wins(lu["Data-rich Direct"], lu["SQL Pivot + real DDL"]),
        (188, 36),
    )
    check(
        "GPT-5.6-Luna ReAct failures that are parse errors",
        (
            share(lu["ReAct"], {"no_submission"}),
            sum(r["failure_code"] == "parse_error" for r in lu["ReAct"].values()),
        ),
        (109, 109),
    )

    if args.dataset_dir:
        sys.path.insert(0, str(REPO / "src"))
        from tend.execution import mql_skeleton_signature, parse_pipeline

        release = json.loads((args.dataset_dir / "data" / "TEND.json").read_text())
        gold = {(r["db_id"], int(r["record_id"])): r["MQL"] for r in release}
        stages = {k: parse_pipeline(m)[1] for k, m in gold.items()}
        print("Benchmark statistics (Table I)")
        ops = {k: operators(p) for k, p in stages.items()}
        n_tasks = len(gold)
        check(
            "dynamic-key operators, array operators, nested dotted paths (tasks, %)",
            [
                (c, pct(c, n_tasks))
                for c in (
                    sum(bool(o & DYNAMIC_KEY_OPS) for o in ops.values()),
                    sum(bool(o & ARRAY_OPS) for o in ops.values()),
                    sum(has_dotted_path(p) for p in stages.values()),
                )
            ],
            [(1094, "90.4"), (1172, "96.9"), (1174, "97.0")],
        )
        check(
            "tasks with $objectToArray, with $unwind",
            (
                sum("$objectToArray" in o for o in ops.values()),
                sum("$unwind" in o for o in ops.values()),
            ),
            (1093, 873),
        )
        lengths = [len(p) for p in stages.values()]
        check("median and maximum stages", (statistics.median(lengths), max(lengths)), (7, 14))
        families = Counter(mql_skeleton_signature(m) for m in gold.values())
        entropy = -sum(c / n_tasks * math.log2(c / n_tasks) for c in families.values())
        check(
            "skeleton families, largest family, normalized entropy",
            (len(families), max(families.values()), f"{entropy / math.log2(len(families)):.2f}"),
            (1115, 6, "0.99"),
        )
        print("Analyses that read the reference pipelines")
        base = ["Sampled-doc Direct", "Data-rich Direct", "SQL Pivot", "ReAct", "DIN-SQL-inspired"]
        twice = [k for k, p in stages.items() if sum("$unwind" in s for s in p) >= 2]
        check(
            "tasks that unwind at least twice: n, SAG, best baseline",
            (len(twice), f"{acc(sag, twice):.1f}", f"{max(acc(ds[b], twice) for b in base):.1f}"),
            (283, "42.0", "25.8"),
        )
        long_ = [k for k, p in stages.items() if len(p) >= 7]
        short = [k for k, p in stages.items() if len(p) < 7]
        check(
            "SAG lead over ReAct on >= 7 and < 7 stages",
            (
                len(long_),
                f"{acc(sag, long_) - acc(ds['ReAct'], long_):.1f}",
                len(short),
                f"{acc(sag, short) - acc(ds['ReAct'], short):.1f}",
            ),
            (831, "12.9", 379, "1.8"),
        )
        poly = [k for k, r in labels.items() if r["structure_category"] == "polymorphic shape"]
        check(
            'polymorphic tasks using the literal "constructor" or "circuit"',
            sum('"constructor"' in gold[k] or '"circuit"' in gold[k] for k in poly),
            34,
        )
        events = [k for k, r in labels.items() if r["structure_category"] == "nested event stream"]
        pit = [k for k in events if "pit_stop" in gold[k]]
        rest = [k for k in events if k not in pit]
        check(
            "pit-stop tasks: n, ReAct, SAG; other event tasks: n, SAG, ReAct",
            (
                len(pit),
                sum(ok(ds["ReAct"][k]) for k in pit),
                sum(ok(sag[k]) for k in pit),
                len(rest),
                sum(ok(sag[k]) for k in rest),
                sum(ok(ds["ReAct"][k]) for k in rest),
            ),
            (6, 6, 0, 124, 56, 53),
        )
        subset = [
            k
            for k, r in off.items()
            if r["campaign"] == "2026-08"
            and "$objectToArray" in gold[k]
            and "$objectToArray" not in (r["predicted_mql"] or "")
        ]
        won, lost = wins({k: on[k] for k in subset}, {k: off[k] for k in subset})
        failed = [k for k in subset if ok(on[k]) and off[k]["prediction_status"] == "failed"]
        check(
            "dynamic-key subset: n, W, L, W against a failed narrower-detector answer",
            (len(subset), won, lost, len(failed)),
            (203, 28, 3, 8),
        )
        check(
            "failed narrower-detector answers: all model calls failed, execution error",
            (
                sum(off[k]["failure_code"] == "LLM_ERROR" for k in failed),
                sum(off[k]["failure_code"] == "PRED_EXEC_ERROR" for k in failed),
            ),
            (7, 1),
        )
        check(
            "dynamic-key subset without them (W, L, p)",
            (won - len(failed), lost, p2(mcnemar(won - len(failed), lost))),
            (20, 3, "0.00049"),
        )

    print(
        f"\n{'All numbers match RESULTS.md.' if not failures else f'{len(failures)} mismatch(es).'}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
