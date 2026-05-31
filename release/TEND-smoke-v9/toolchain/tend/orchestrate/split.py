"""Cross-domain train/test split with H5/H7/H8/H9 hard constraints."""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

from tend.errors import SplitError
from tend.orchestrate.coverage import CoverageController
from tend.orchestrate.seed import split_seed


def estimate_schema_flex_ceiling(records: list[dict[str, Any]], catalog: dict[str, Any]) -> float:
    return CoverageController().estimate_schema_flex_ceiling(records, catalog)


def _thresholds(
    records: list[dict[str, Any]], catalog: dict[str, Any]
) -> tuple[bool, float, float, float]:
    selected = [e for e in catalog.get("databases", []) if e.get("selected")]
    flex_eligible_db_ratio = sum(1 for e in selected if e.get("flex_eligible")) / max(len(selected), 1)
    supply_relax = flex_eligible_db_ratio < 0.30
    supply_ceiling = estimate_schema_flex_ceiling(records, catalog)
    if supply_relax:
        h7_min = supply_ceiling if supply_ceiling > 0 else 0.0
        h9_min = supply_ceiling * 0.8 if supply_ceiling > 0 else 0.0
    else:
        h7_min = 0.25
        h9_min = 0.20
    return supply_relax, supply_ceiling, h7_min, h9_min


def _validate_test(
    test: list[dict[str, Any]],
    *,
    h7_min: float,
    h9_min: float,
    supply_relax: bool,
) -> None:
    if not test:
        raise SplitError("empty test set")
    n_test = len(test)
    l4_ratio = sum(1 for r in test if r["difficulty"] == "L4") / n_test
    if l4_ratio < 0.30:
        raise SplitError(f"test L4 ratio {l4_ratio:.3f} < 0.30 (H5)")

    flex_ratio = sum(1 for r in test if r.get("schema_flex", "none") != "none") / n_test
    if flex_ratio < h7_min:
        suffix = " [supply-relax active]" if supply_relax else ""
        raise SplitError(
            f"test schema_flex ratio {flex_ratio:.3f} < h7_min {h7_min:.3f} (H7){suffix}"
        )

    l0_ratio = sum(1 for r in test if r["difficulty"] == "L0") / n_test
    if l0_ratio > 0.05:
        raise SplitError(f"test L0 ratio {l0_ratio:.3f} > 0.05 (H8)")

    ssf_ratio = sum(
        1 for r in test if r.get("sql_infeasibility_class") == "structural_schema_flex"
    ) / n_test
    if ssf_ratio < h9_min:
        suffix = " [supply-relax active]" if supply_relax else ""
        raise SplitError(
            f"test structural_schema_flex ratio {ssf_ratio:.3f} < h9_min {h9_min:.3f} (H9){suffix}"
        )


def _materialize_split(
    records: list[dict[str, Any]],
    db_to_domain: dict[str, str],
    test_domains: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    train: list[dict[str, Any]] = []
    test: list[dict[str, Any]] = []
    for record in records:
        domain = db_to_domain[record["db_id"]]
        if domain in test_domains:
            test.append(record)
        else:
            train.append(record)
    return train, test


def _greedy_test_domains(
    domain_record_count: Counter[str],
    *,
    target_test: int,
    rng: random.Random,
) -> set[str]:
    domains = list(domain_record_count.keys())
    rng.shuffle(domains)
    test_domains: set[str] = set()
    test_count = 0
    for domain in sorted(domains, key=lambda d: -domain_record_count[d]):
        if test_count < target_test:
            test_domains.add(domain)
            test_count += domain_record_count[domain]
    return test_domains


def _search_test_domains(
    records: list[dict[str, Any]],
    domain_record_count: Counter[str],
    db_to_domain: dict[str, str],
    *,
    target_test: int,
    h7_min: float,
    h9_min: float,
    supply_relax: bool,
    rng: random.Random,
) -> set[str]:
    domains = list(domain_record_count.keys())
    rng.shuffle(domains)

    candidates: list[set[str]] = []
    greedy = _greedy_test_domains(domain_record_count, target_test=target_test, rng=rng)
    candidates.append(greedy)

    for size in range(1, len(domains) + 1):
        for combo in combinations(domains, size):
            count = sum(domain_record_count[d] for d in combo)
            if count >= max(1, target_test):
                candidates.append(set(combo))

    seen: set[tuple[str, ...]] = set()
    for test_domains in candidates:
        key = tuple(sorted(test_domains))
        if key in seen:
            continue
        seen.add(key)
        _, test = _materialize_split(records, db_to_domain, test_domains)
        try:
            _validate_test(test, h7_min=h7_min, h9_min=h9_min, supply_relax=supply_relax)
        except SplitError:
            continue
        return test_domains

    raise SplitError("unable to satisfy H5/H7/H8/H9 with available domains")


def cross_domain_split(
    catalog: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    test_ratio: float = 0.20,
    seed: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not records:
        raise SplitError("empty record pool")

    rng = random.Random(seed if seed is not None else split_seed())
    supply_relax, supply_ceiling, h7_min, h9_min = _thresholds(records, catalog)

    domain_to_dbs: dict[str, set[str]] = defaultdict(set)
    db_to_domain: dict[str, str] = {}
    for entry in catalog.get("databases", []):
        if not entry.get("selected"):
            continue
        domain_to_dbs[entry["domain_id"]].add(entry["db_id"])
        db_to_domain[entry["db_id"]] = entry["domain_id"]

    for record in records:
        db_id = record["db_id"]
        if db_id not in db_to_domain:
            raise KeyError(f"record db_id {db_id!r} not in catalog")
        if "domain_id" not in record:
            record["domain_id"] = db_to_domain[db_id]

    domain_record_count: Counter[str] = Counter()
    for record in records:
        domain_record_count[db_to_domain[record["db_id"]]] += 1

    target_test = max(1, int(len(records) * test_ratio))
    test_domains = _search_test_domains(
        records,
        domain_record_count,
        db_to_domain,
        target_test=target_test,
        h7_min=h7_min,
        h9_min=h9_min,
        supply_relax=supply_relax,
        rng=rng,
    )
    train, test = _materialize_split(records, db_to_domain, test_domains)
    train_domains = set(domain_record_count) - test_domains

    if not train_domains.isdisjoint(test_domains):
        raise SplitError("train/test domain overlap")
    if not test:
        raise SplitError("empty test set after domain assignment")

    _validate_test(test, h7_min=h7_min, h9_min=h9_min, supply_relax=supply_relax)

    meta = {
        "train_domains": sorted(train_domains),
        "test_domains": sorted(test_domains),
        "supply_relax_active": supply_relax,
        "flex_eligible_db_ratio": sum(
            1 for e in catalog.get("databases", []) if e.get("selected") and e.get("flex_eligible")
        )
        / max(sum(1 for e in catalog.get("databases", []) if e.get("selected")), 1),
        "supply_ceiling": supply_ceiling,
        "h7_min": h7_min,
        "h9_min": h9_min,
        "test_ratio_target": test_ratio,
        "train_count": len(train),
        "test_count": len(test),
    }
    return train, test, meta
