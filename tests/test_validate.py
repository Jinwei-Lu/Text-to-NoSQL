"""Tests for the release/record validator (Session A: tend.publish.validate)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from tend.execution import mql_signature, world_signature
from tend.execution import mql_skeleton_signature, mql_skeleton_summary
from tend.execution.ast_check import DISABLED_OPERATORS, DISABLED_SYSTEM_VARS
from tend.publish import validate_composition, validate_record, validate_record_jsonschema, validate_release
from tend.publish.validate import H11_SKELETON_FAMILY_MAX

_SIX = sorted(DISABLED_OPERATORS | DISABLED_SYSTEM_VARS)


def _valid_record(**over):
    rec = {
        "record_id": 1, "db_id": "financial",
        "nl_queries": {"canonical": "for each account attach the ratio", "colloquial": "label each"},
        "MQL": ('db.account.aggregate([{"$lookup":{"from":"trans","localField":"_id",'
                '"foreignField":"account_id","as":"t"}},{"$addFields":{"x":1}}])'),
        "canonical_form_set": {
            "must_contain": ["$lookup"], "must_not_contain": _SIX,
            "must_contain_at_root": ["$lookup"], "must_not_contain_at_root": ["$group", "$unwind"]},
        "difficulty": "L4", "sql_infeasibility_class": "structural_schema_flex",
        "shape_policy": "preserve", "world_signature": "sha256:" + "0" * 64,
        "schema_flex": "polymorphic",
    }
    rec.update(over)
    return rec


def test_valid_record_passes():
    assert validate_record(_valid_record()) == []


def test_validate_release_rejects_duplicate_mql(tmp_path: Path):
    out = tmp_path / "release"
    db = "financial"
    data = {"account": [{"_id": 1}], "trans": [{"_id": 10, "account_id": 1}]}
    sig = world_signature(data)
    records = [
        _valid_record(record_id=1, world_signature=sig),
        _valid_record(record_id=2, world_signature=sig),
    ]

    (out / "mongodb_data").mkdir(parents=True)
    (out / "mongodb_schema").mkdir()
    (out / "agent_design_rationale").mkdir()
    (out / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "mongodb_data" / f"{db}.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "mongodb_schema" / f"{db}.json").write_text(
        json.dumps({"account": {"_id": "INT"}, "trans": {"account_id": "INT"}}),
        encoding="utf-8",
    )
    (out / "agent_design_rationale" / f"{db}.yaml").write_text(
        "db_id: financial\n", encoding="utf-8"
    )

    report = validate_release(out, require_all_dbs=False)

    assert not report.ok
    assert any("duplicate MQL" in issue and "r2" in issue for issue in report.record_violations)


def test_validate_release_rejects_repeated_mql_skeleton_family(tmp_path: Path):
    out = tmp_path / "release"
    db = "financial"
    data = {"account": [{"_id": 1}], "trans": [{"_id": 10, "account_id": 1}]}
    sig = world_signature(data)
    records = [
        _valid_record(
            record_id=i,
            world_signature=sig,
            MQL=(
                'db.account.aggregate([{"$lookup":{"from":"trans","localField":"_id",'
                '"foreignField":"account_id","as":"t"}},{"$addFields":{'
                f'"x_{i}":1'
                '}}])'
            ),
        )
        for i in range(1, H11_SKELETON_FAMILY_MAX + 2)
    ]

    (out / "mongodb_data").mkdir(parents=True)
    (out / "mongodb_schema").mkdir()
    (out / "agent_design_rationale").mkdir()
    (out / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "mongodb_data" / f"{db}.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "mongodb_schema" / f"{db}.json").write_text(
        json.dumps({"account": {"_id": "INT"}, "trans": {"account_id": "INT"}}),
        encoding="utf-8",
    )
    (out / "agent_design_rationale" / f"{db}.yaml").write_text(
        "db_id: financial\n", encoding="utf-8"
    )

    report = validate_release(out, require_all_dbs=False)

    assert not report.ok
    assert any("MQL skeleton family too large" in issue for issue in report.record_violations)


def test_validate_release_rejects_duplicate_canonical_nl(tmp_path: Path):
    out = tmp_path / "release"
    db = "financial"
    data = {"account": [{"_id": 1}], "trans": [{"_id": 10, "account_id": 1}]}
    sig = world_signature(data)
    records = [
        _valid_record(
            record_id=1,
            world_signature=sig,
            nl_queries={
                "canonical": "Group accounts by frequency.",
                "colloquial": "Group them.",
            },
        ),
        _valid_record(
            record_id=2,
            world_signature=sig,
            nl_queries={
                "canonical": " group   ACCOUNTS by frequency. ",
                "colloquial": "Group those accounts another way.",
            },
            MQL=(
                'db.account.aggregate([{"$lookup":{"from":"trans","localField":"_id",'
                '"foreignField":"account_id","as":"t"}},{"$addFields":{"x_2":1}}])'
            ),
        ),
    ]

    (out / "mongodb_data").mkdir(parents=True)
    (out / "mongodb_schema").mkdir()
    (out / "agent_design_rationale").mkdir()
    (out / "test.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps(records), encoding="utf-8")
    (out / "mongodb_data" / f"{db}.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "mongodb_schema" / f"{db}.json").write_text(
        json.dumps({"account": {"_id": "INT"}, "trans": {"account_id": "INT"}}),
        encoding="utf-8",
    )
    (out / "agent_design_rationale" / f"{db}.yaml").write_text(
        "db_id: financial\n", encoding="utf-8"
    )

    report = validate_release(out, require_all_dbs=False)

    assert not report.ok
    assert any("duplicate canonical NL" in issue and "r2" in issue for issue in report.record_violations)


def test_c6_missing_disabled_ops():
    rec = _valid_record(canonical_form_set={
        "must_contain": ["$lookup"], "must_not_contain": ["$unwind"],   # not the 6
        "must_contain_at_root": ["$lookup"], "must_not_contain_at_root": []})
    iss = validate_record(rec)
    assert any("C6" in i for i in iss)


def test_c7_bad_difficulty():
    assert any("C7" in i for i in validate_record(_valid_record(difficulty="L9")))


def test_c9_structural_requires_l4_and_flex():
    rec = _valid_record(difficulty="L2", schema_flex="none")
    iss = validate_record(rec)
    assert any("C9" in i and "L4" in i for i in iss)
    assert any("C9" in i and "schema_flex" in i for i in iss)


def test_c2_nl_queries_shape():
    rec = _valid_record(nl_queries={"canonical": "x"})   # missing colloquial
    assert any("C2" in i for i in validate_record(rec))


def test_disabled_system_var_in_string_value_fails():
    rec = _valid_record(
        MQL='db.account.aggregate([{ "$addFields": { "generated_at": "$$NOW" } }])',
        canonical_form_set={
            "must_contain": ["$addFields"],
            "must_not_contain": _SIX,
            "must_contain_at_root": ["$addFields"],
            "must_not_contain_at_root": [],
        },
    )
    iss = validate_record(rec)
    assert any("C5" in i and "$$NOW" in i for i in iss)


def test_norm_exec_fallback_only_checks_runs_without_error():
    class EmptyExecutor:
        def __init__(self):
            self.calls = []

        def norm_exec(self, db_id, mql):
            self.calls.append((db_id, mql))
            return []

    executor = EmptyExecutor()
    iss = validate_record(_valid_record(), executor=executor, snapshot={"account": []})
    assert executor.calls
    assert not any("not a gold-class member" in i for i in iss)


def test_composition_constraints():
    # 10 records: 4 L4 (all ssf+flex), 5 L2, 1 L0 -> L4=40%, L0=10% (H8 fail)
    recs = []
    for i in range(4):
        recs.append(_valid_record(record_id=i, db_id=f"d{i%11}"))
    for i in range(5):
        recs.append(_valid_record(record_id=10 + i, db_id=f"d{i%11}", difficulty="L2",
                                   sql_infeasibility_class="feasible", schema_flex=None))
    recs.append(_valid_record(record_id=99, db_id="d0", difficulty="L0",
                              sql_infeasibility_class="feasible", schema_flex=None))
    rep = validate_composition(recs, require_all_dbs=False)
    assert rep.l4_ratio == 0.4 and rep.l0_ratio == 0.1
    assert not rep.ok and any("H8" in v for v in rep.violations)   # L0 10% > 5%


def test_jsonschema_against_record_schema():
    schema = Path("proposals/schemas/record.schema.json")
    if not schema.exists():
        return
    # the proposals' valid example must validate
    import json
    valid = Path("proposals/schemas/record.schema.valid.json")
    if valid.exists():
        rec = json.loads(valid.read_text(encoding="utf-8"))
        assert validate_record_jsonschema(rec, schema) == []
        assert not any("C6" in issue for issue in validate_record(rec))


def test_record_schema_enforces_exact_world_signature_length():
    schema = Path("proposals/schemas/record.schema.json")
    too_short = _valid_record(world_signature="sha256:" + "a" * 63)
    too_long = _valid_record(world_signature="sha256:" + "a" * 65)
    assert validate_record_jsonschema(too_short, schema)
    assert validate_record_jsonschema(too_long, schema)


def test_record_schema_accepts_diversity_metadata():
    schema = Path("proposals/schemas/record.schema.json")
    rec = _valid_record(
        mql_signature=mql_signature(_valid_record()["MQL"]),
        mql_skeleton_signature=mql_skeleton_signature(_valid_record()["MQL"]),
        mql_skeleton_summary=mql_skeleton_summary(_valid_record()["MQL"]),
        mechanism="sparse_embed",
        archetype="existence_count",
        diversity_key="sparse_embed:existence_count:financial.account.loan",
        schema_feature="account.loan",
    )
    assert validate_record_jsonschema(rec, schema) == []


def test_library_schema_rejects_unsafe_field_names():
    import jsonschema

    schema = json.loads(
        Path("proposals/schemas/library.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    cases = [
        {"students": {"$bad": "TEXT"}},
        {"students": {"profile.name": "TEXT"}},
        {"students": {"profile..name": "TEXT"}},
        {"students": {"   ": "TEXT"}},
        {"students": {"profile": {"type": "OBJECT", "fields": {"__variants": "TEXT"}}}},
    ]
    for case in cases:
        assert list(validator.iter_errors(case)), case


def test_validate_release_reports_malformed_mql_as_record_violation(tmp_path: Path):
    out = tmp_path / "release"
    db = "financial"
    data = {"account": [{"_id": 1}], "trans": [{"_id": 10, "account_id": 1}]}
    rec = _valid_record(
        MQL='db.account.aggregate([{ "$lookup": }])',
        canonical_form_set={
            "must_contain": ["$lookup"],
            "must_not_contain": _SIX,
            "must_contain_at_root": ["$lookup"],
            "must_not_contain_at_root": ["$group", "$unwind"],
        },
        world_signature=world_signature(data),
    )

    (out / "mongodb_data").mkdir(parents=True)
    (out / "mongodb_schema").mkdir()
    (out / "agent_design_rationale").mkdir()
    (out / "test.json").write_text(json.dumps([rec]), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps([rec]), encoding="utf-8")
    (out / "mongodb_data" / f"{db}.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "mongodb_schema" / f"{db}.json").write_text(
        json.dumps({"account": {"_id": "INT"}}), encoding="utf-8"
    )
    (out / "agent_design_rationale" / f"{db}.yaml").write_text(
        "db_id: financial\n", encoding="utf-8"
    )

    report = validate_release(out, require_all_dbs=False)
    assert not report.ok
    assert any("C5" in issue and "parse error" in issue for issue in report.record_violations)


def test_validate_release_checks_artifacts_and_signature(tmp_path: Path):
    out = tmp_path / "release"
    db = "financial"
    data = {
        "account": [{"_id": 1, "account_id": 1, "loan": {"amount": 100}}],
        "trans": [{"_id": 10, "account_id": 1, "type": "PRIJEM", "amount": 50}],
    }
    mql = (
        'db.account.aggregate([{ "$lookup": { "from": "trans", "localField": "_id", '
        '"foreignField": "account_id", "as": "t" } }])'
    )
    rec = _valid_record(
        MQL=mql,
        canonical_form_set={
            "must_contain": ["$lookup"],
            "must_not_contain": _SIX,
            "must_contain_at_root": ["$lookup"],
            "must_not_contain_at_root": ["$group", "$unwind"],
        },
        world_signature=world_signature(data),
    )
    schema = {
        "account": {
            "_id": "INT",
            "account_id": "INT",
            "loan": "OBJECT",
            "__variants": [
                {
                    "discriminator": {"loan": "present"},
                    "fields": {"loan": "OBJECT"},
                    "coverage": 1.0,
                    "source_signal": "test",
                },
                {
                    "discriminator": {"loan": "missing"},
                    "fields": {"loan": "OBJECT"},
                    "coverage": 0.0,
                    "source_signal": "test",
                },
            ],
        },
        "trans": {"_id": "INT", "account_id": "INT", "type": "TEXT", "amount": "REAL"},
    }
    adr = {
        "db_id": db,
        "source_spider_tables": ["account", "trans"],
        "patterns_applied": ["embed", "polymorphic"],
        "rationale_summary": "test rationale",
        "decisions": [{"id": "D01", "type": "embed", "rationale": "test"}],
        "anti_pattern_checks": {"pass": True, "issues": []},
    }
    catalog = {
        "source_version": "1.0",
        "generated_at": "2026-06-01T00:00:00+00:00",
        "databases": [{
            "db_id": db,
            "domain_id": "finance",
            "sqlite_path": "minidev/MINIDEV/dev_databases/financial/financial.sqlite",
            "table_count": 8,
            "query_count": 32,
            "selected": True,
            "flex_eligible": True,
        }],
    }

    (out / "mongodb_data").mkdir(parents=True)
    (out / "mongodb_schema").mkdir()
    (out / "agent_design_rationale").mkdir()
    (out / "test.json").write_text(json.dumps([rec]), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps([rec]), encoding="utf-8")
    (out / "mongodb_data" / f"{db}.json").write_text(json.dumps(data), encoding="utf-8")
    (out / "mongodb_schema" / f"{db}.json").write_text(json.dumps(schema), encoding="utf-8")
    (out / "agent_design_rationale" / f"{db}.yaml").write_text(
        yaml.safe_dump(adr), encoding="utf-8"
    )
    (out / "bird_db_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")

    report = validate_release(out, schemas_dir="proposals/schemas", require_all_dbs=False)
    assert report.ok, report.summary() + "\n" + "\n".join(report.file_violations)

    bad = dict(rec, world_signature="sha256:" + "1" * 64)
    (out / "test.json").write_text(json.dumps([bad]), encoding="utf-8")
    (out / "TEND.json").write_text(json.dumps([bad]), encoding="utf-8")
    report = validate_release(out, schemas_dir="proposals/schemas", require_all_dbs=False)
    assert not report.ok
    assert any("world_signature" in issue for issue in report.record_violations)
