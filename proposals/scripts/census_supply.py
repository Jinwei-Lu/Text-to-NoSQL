#!/usr/bin/env python3
"""Pass 1 · query-bearing heterogeneity supply census (deterministic, zero-LLM).

Scans the 11 BIRD mini-dev databases + the minidev SQL workload (500 questions)
and reports, per db and per mechanism, how much *query-bearing* heterogeneity the
source can yield. This bounds the achievable L4 / structural_schema_flex share and
replaces a-priori composition targets (L4>=30% etc.) with census-derived ones.

Mechanisms (post-review four-mechanism set; type-drift is audit-only, not supply):
  (1) polymorphic    low-card discriminator col (2..8 distinct) + value_description
                     enum, conditioned by >=1 workload SQL  -> structural_schema_flex / L4
  (2a) sparse_scalar nullable column, NULL rate in (0.05,0.95)             -> semantic
  (2b) sparse_embed  FK child covering < EMBED_COVER_MAX of parent rows
                     (relational INNER JOIN silently drops)  -> structural_schema_flex / L4
  (4) dynamic_key    EAV (attribute_name / attribute_value column pair)    -> L2..L4
  (5) versioning     time/season column + a rename-pair of columns         -> semantic

A mechanism instance counts toward supply only if query-bearing (referenced by the
real workload SQL). type_drift (real mixed-type columns) is scanned but reported
under audit_only, never counted as construction supply.

Usage:
    python3 proposals/scripts/census_supply.py \
        --bird-root minidev/MINIDEV \
        --out proposals/scripts/census_supply_report.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

# ---- tunables (deterministic thresholds) ----
POLY_MIN_DISTINCT, POLY_MAX_DISTINCT = 2, 8
SPARSE_NULL_LO, SPARSE_NULL_HI = 0.05, 0.95
EMBED_COVER_MAX = 0.90        # child covers < 90% of parent rows -> sparse optional embed
L4_TARGET_RATIO = 0.30        # the H5 ideal; report feasibility against ceiling

DOMAIN = {
    "financial": "finance", "debit_card_specializing": "finance",
    "california_schools": "education", "student_club": "education",
    "codebase_community": "community", "card_games": "games",
    "european_football_2": "sports", "formula_1": "sports",
    "superhero": "entertainment", "toxicology": "chemistry",
    "thrombosis_prediction": "medical",
}

# archetypes reachable per mechanism, tagged with the difficulty tier they fall out at
ARCHETYPES = {
    "polymorphic":   [("per_subtype_agg", "L4"), ("subtype_cond_projection", "L4"),
                      ("cross_subtype_compare", "L3"), ("subtype_specific_field", "L4")],
    "sparse_embed":  [("present_missing_projection", "L4"), ("has_vs_absent_compare", "L4")],
    "sparse_scalar": [("existence_count", "L2"), ("null_coalesce_agg", "L3")],
    "dynamic_key":   [("dynamic_key_fold", "L4"), ("cross_keyset_value", "L3")],
    "versioning":    [("cross_version_agg", "L3")],
}
STRUCTURAL = {"polymorphic", "sparse_embed", "dynamic_key"}  # -> structural_schema_flex


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_schema(bird_root: Path) -> dict:
    data = json.load(open(bird_root / "dev_tables.json"))
    out = {}
    for db in data:
        cols = db["column_names_original"]  # [[tbl_idx, col_name], ...] global index
        tnames = db["table_names_original"]
        out[db["db_id"]] = {
            "tables": tnames,
            "cols": cols,
            "col_types": db["column_types"],
            "foreign_keys": db["foreign_keys"],
            "primary_keys": db["primary_keys"],
        }
    return out


def load_workload(bird_root: Path) -> dict[str, list[dict]]:
    rows = json.load(open(bird_root / "mini_dev_sqlite.json"))
    by_db = defaultdict(list)
    for r in rows:
        by_db[r["db_id"]].append(r)
    return by_db


def load_value_descriptions(bird_root: Path, db_id: str, tables: list[str]) -> dict:
    """(table_lower, col_lower) -> value_description string (enum semantics)."""
    vd = {}
    desc_dir = bird_root / "dev_databases" / db_id / "database_description"
    for t in tables:
        p = desc_dir / f"{t}.csv"
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8-sig", errors="replace") as f:
                for row in csv.DictReader(f):
                    col = (row.get("original_column_name") or "").strip()
                    val = (row.get("value_description") or "").strip()
                    if col:
                        vd[(t.lower(), col.lower())] = val
        except Exception:
            pass
    return vd


def cond_count(sqls: list[str], col: str) -> int:
    """# of workload SQLs that use `col` in a conditional (WHERE/CASE/HAVING comparison)."""
    pat = re.compile(rf"\b{re.escape(col.lower())}\b\s*(=|in\b|like\b|<>|!=|<|>)")
    return sum(1 for s in sqls if pat.search(s.lower()))


def ref_count(sqls: list[str], token: str) -> int:
    pat = re.compile(rf"\b{re.escape(token.lower())}\b")
    return sum(1 for s in sqls if pat.search(s.lower()))


def census_db(db_id: str, schema: dict, sqls: list[str], vd: dict, conn) -> dict:
    cur = conn.cursor()
    tables = schema["tables"]
    cols = schema["cols"]
    col_types = schema["col_types"]
    raw_sql = [r["SQL"] for r in sqls]

    # global col idx -> (table_name, col_name, type)
    gidx = {}
    for i, (tbl_idx, cname) in enumerate(cols):
        if tbl_idx < 0:
            continue
        gidx[i] = (tables[tbl_idx], cname, col_types[i])

    def table_rowcount(t):
        try:
            return cur.execute(f"SELECT COUNT(*) FROM {qident(t)}").fetchone()[0]
        except sqlite3.Error:
            return None

    rowcounts = {t: table_rowcount(t) for t in tables}

    mech = {"polymorphic": [], "sparse_scalar": [], "sparse_embed": [],
            "dynamic_key": [], "versioning": []}
    audit_type_drift = []

    # ---- per-column scans: polymorphic, sparse_scalar, type_drift ----
    for i, (t, c, typ) in gidx.items():
        try:
            n = rowcounts.get(t) or 0
            if n == 0:
                continue
            ndist = cur.execute(
                f"SELECT COUNT(DISTINCT {qident(c)}) FROM {qident(t)}").fetchone()[0]
            nnull = cur.execute(
                f"SELECT SUM(CASE WHEN {qident(c)} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {qident(t)}").fetchone()[0] or 0
        except sqlite3.Error:
            continue
        null_rate = nnull / n if n else 0.0
        enum = vd.get((t.lower(), c.lower()), "")

        # (1) polymorphic
        if POLY_MIN_DISTINCT <= ndist <= POLY_MAX_DISTINCT:
            cc = cond_count(raw_sql, c)
            if enum and cc >= 1:
                mech["polymorphic"].append({
                    "table": t, "discriminator_col": c, "distinct": ndist,
                    "has_enum": True, "sql_cond_refs": cc, "query_bearing": True,
                })

        # (2a) sparse scalar
        if SPARSE_NULL_LO < null_rate < SPARSE_NULL_HI:
            rc = ref_count(raw_sql, c)
            mech["sparse_scalar"].append({
                "table": t, "col": c, "null_rate": round(null_rate, 3),
                "sql_refs": rc, "query_bearing": rc >= 1,
            })

        # (3) type drift -> AUDIT ONLY: real mixed storage class in one column
        try:
            kinds = cur.execute(
                f"SELECT COUNT(DISTINCT typeof({qident(c)})) FROM {qident(t)} "
                f"WHERE {qident(c)} IS NOT NULL").fetchone()[0]
            if kinds and kinds >= 2:
                audit_type_drift.append({"table": t, "col": c, "storage_classes": kinds})
        except sqlite3.Error:
            pass

    # ---- (2b) sparse embed via FK coverage ----
    for child_idx, parent_idx in schema["foreign_keys"]:
        if child_idx not in gidx or parent_idx not in gidx:
            continue
        ct, cc_, _ = gidx[child_idx]
        pt, pc, _ = gidx[parent_idx]
        n_parent = rowcounts.get(pt) or 0
        n_child = rowcounts.get(ct) or 0
        if n_parent == 0:
            continue
        # sparse OPTIONAL embed requires the child to be the dependent satellite that
        # would embed into the parent (loan into account). When the child is a big fact
        # table (Match) and the parent a dimension (Player), coverage<0.9 only means the
        # dimension isn't fully used -- NOT present/missing optionality. Guard on it.
        if n_child > n_parent:
            continue
        try:
            n_child_distinct = cur.execute(
                f"SELECT COUNT(DISTINCT {qident(cc_)}) FROM {qident(ct)} "
                f"WHERE {qident(cc_)} IS NOT NULL").fetchone()[0]
        except sqlite3.Error:
            continue
        coverage = n_child_distinct / n_parent if n_parent else 0.0
        if coverage < EMBED_COVER_MAX:
            qb = ref_count(raw_sql, ct) >= 1
            mech["sparse_embed"].append({
                "child_table": ct, "fk_col": cc_, "parent_table": pt,
                "coverage": round(coverage, 3), "sql_refs_child": ref_count(raw_sql, ct),
                "query_bearing": qb,
            })

    # ---- (4) dynamic_key (EAV) ----
    name_suf = ("attribute_name", "attr_name", "property_name", "key", "name")
    val_suf = ("attribute_value", "attr_value", "property_value", "value")
    by_table_cols = defaultdict(list)
    for i, (t, c, typ) in gidx.items():
        by_table_cols[t].append(c.lower())
    for t, clist in by_table_cols.items():
        has_name = any(any(c.endswith(s) for s in name_suf) for c in clist)
        has_val = any(any(c.endswith(s) for s in val_suf) for c in clist)
        if has_name and has_val:
            mech["dynamic_key"].append({
                "table": t, "query_bearing": ref_count(raw_sql, t) >= 1,
                "note": "EAV-shaped column pair (name+value)",
            })

    # ---- (5) versioning: time/season col + a rename-pair heuristic ----
    for t, clist in by_table_cols.items():
        has_time = any(("date" in c or "time" in c or "season" in c or "year" in c)
                       for c in clist)
        # rename pair: same stem with old/new/prev/curr or _1/_2 suffix
        stems = defaultdict(list)
        for c in clist:
            stem = re.sub(r"(_?(old|new|prev|curr|current|v\d+|\d+))$", "", c)
            stems[stem].append(c)
        rename_pair = any(len(v) >= 2 for k, v in stems.items() if k)
        if has_time and rename_pair:
            mech["versioning"].append({"table": t, "query_bearing": True})

    # ---- archetype reachability + L4 supply ----
    archetype_instances = []
    l4_supply = 0
    ssf_supply = 0
    for mname, insts in mech.items():
        if mname not in ARCHETYPES:
            continue
        qb_insts = [x for x in insts if x.get("query_bearing")]
        for inst in qb_insts:
            for arch, tier in ARCHETYPES[mname]:
                archetype_instances.append({"mechanism": mname, "archetype": arch,
                                            "tier": tier})
                if tier == "L4":
                    l4_supply += 1
                    if mname in STRUCTURAL:
                        ssf_supply += 1

    def qb(mname):
        return sum(1 for x in mech[mname] if x.get("query_bearing"))

    # conservative: distinct query-bearing STRUCTURAL mechanism instances (no archetype
    # multiplier) -> a lower-bound-ish floor on dedup'd L4 records.
    l4_qb_instances = sum(qb(m) for m in STRUCTURAL if m in mech)

    return {
        "domain": DOMAIN.get(db_id, "unknown"),
        "table_count": len(tables),
        "query_count": len(sqls),
        "mechanisms": mech,
        "query_bearing_counts": {m: qb(m) for m in mech},
        "archetype_instances": archetype_instances,
        "l4_qb_instances_conservative": l4_qb_instances,
        "l4_supply_instances": l4_supply,
        "structural_schema_flex_supply_instances": ssf_supply,
        "audit_only": {"type_drift": audit_type_drift},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-root", default="minidev/MINIDEV", type=Path)
    ap.add_argument("--out", default="proposals/scripts/census_supply_report.json", type=Path)
    args = ap.parse_args()

    schema = load_schema(args.bird_root)
    workload = load_workload(args.bird_root)

    dbs = {}
    for db_id in sorted(schema):
        sqlite_path = args.bird_root / "dev_databases" / db_id / f"{db_id}.sqlite"
        vd = load_value_descriptions(args.bird_root, db_id, schema[db_id]["tables"])
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            dbs[db_id] = census_db(db_id, schema[db_id], workload.get(db_id, []), vd, conn)
        finally:
            conn.close()

    total_l4 = sum(d["l4_supply_instances"] for d in dbs.values())
    total_l4_cons = sum(d["l4_qb_instances_conservative"] for d in dbs.values())
    total_ssf = sum(d["structural_schema_flex_supply_instances"] for d in dbs.values())
    flex_eligible = sum(1 for d in dbs.values() if d["l4_qb_instances_conservative"] > 0)
    # If we publish all L4 supply and pad to L4_TARGET_RATIO, max total records:
    max_total_at_target = int(total_l4 / L4_TARGET_RATIO) if total_l4 else 0

    report = {
        "source": "minidev 11 dbs + mini_dev_sqlite.json (500 q); workload = minidev only",
        "thresholds": {
            "poly_distinct": [POLY_MIN_DISTINCT, POLY_MAX_DISTINCT],
            "sparse_null": [SPARSE_NULL_LO, SPARSE_NULL_HI],
            "embed_cover_max": EMBED_COVER_MAX, "l4_target_ratio": L4_TARGET_RATIO,
        },
        "global": {
            "dbs": len(dbs),
            "flex_eligible_dbs": flex_eligible,
            "flex_eligible_db_ratio": round(flex_eligible / len(dbs), 3),
            "l4_supply_instances": total_l4,
            "l4_qb_instances_conservative": total_l4_cons,
            "structural_schema_flex_supply_instances": total_ssf,
            "max_total_records_at_L4_30pct": max_total_at_target,
            "max_total_records_at_L4_30pct_conservative": int(total_l4_cons / L4_TARGET_RATIO)
            if total_l4_cons else 0,
            "note": "l4_supply_instances = (query-bearing mech instance x L4 archetype) cells "
                    "= optimistic intent-slot ceiling. l4_qb_instances_conservative = distinct "
                    "query-bearing structural instances (no archetype multiplier) = floor. "
                    "True dedup'd L4 record yield lies between, after Gate-QB.",
        },
        "databases": dbs,
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    # ---- human summary ----
    print(f"\n=== TEND Pass 1 supply census (minidev, 11 dbs) ===")
    print(f"flex-eligible dbs: {flex_eligible}/11  "
          f"(ratio {flex_eligible/len(dbs):.2f})")
    print(f"L4 supply: ceiling={total_l4} instances (x-archetype) | "
          f"floor={total_l4_cons} distinct qb-structural instances")
    print(f"max total records at L4>=30%: ceiling {max_total_at_target} | "
          f"floor {int(total_l4_cons/L4_TARGET_RATIO) if total_l4_cons else 0}\n")
    hdr = (f"{'db':24} {'dom':12} {'poly':>4} {'sp.emb':>6} {'sp.scal':>7} {'dyn':>4} "
           f"{'ver':>4} {'L4floor':>7} {'L4ceil':>6}")
    print(hdr); print("-" * len(hdr))
    for db_id, d in dbs.items():
        c = d["query_bearing_counts"]
        print(f"{db_id:24} {d['domain']:12} {c['polymorphic']:>4} {c['sparse_embed']:>6} "
              f"{c['sparse_scalar']:>7} {c['dynamic_key']:>4} {c['versioning']:>4} "
              f"{d['l4_qb_instances_conservative']:>7} {d['l4_supply_instances']:>6}")
    print(f"\nfull report -> {args.out}")


if __name__ == "__main__":
    main()
