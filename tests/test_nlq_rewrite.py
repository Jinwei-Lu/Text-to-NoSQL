from __future__ import annotations

import asyncio
import json

from tend.config import LLMSettings, Paths, Settings
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.publish.nlq_rewrite import (
    anti_template_violations_for_text,
    build_anti_template_report,
    run_llm_nlq_rewrite,
)
from tend.stubs import stub_fn


def _settings(tmp_path) -> Settings:
    return Settings(
        llm=LLMSettings(
            base_url="http://stub.invalid",
            api_key="stub",
            model="stub-model",
            max_retries=1,
            max_concurrency=2500,
        ),
        paths=Paths(
            repo_root=tmp_path,
            bird_root=tmp_path,
            proposals=tmp_path / "proposals",
            agent_prompts=tmp_path / "proposals" / "agent_prompts",
            schemas=tmp_path / "schemas",
            runs=tmp_path / "runs",
            dataset_out=tmp_path / "dataset",
        ),
        mongo_uri="mongodb://stub.invalid",
        stub=True,
        run_id="nlq-rewrite",
    )


def _write_release(tmp_path):
    root = tmp_path / "release"
    data_dir = root / "data"
    pairs_dir = root / "audits" / "nl_mql"
    data_dir.mkdir(parents=True)
    pairs_dir.mkdir(parents=True)
    record = {
        "record_id": 1,
        "db_id": "financial",
        "native_query_pattern": "financial.stub",
        "nl_queries": {
            "canonical": (
                "On `account` for `financial.stub`, return the top 1 documents; "
                "output fields account_id."
            ),
            "colloquial": "Show the top 1 documents from `account` with fields account_id.",
        },
        "MQL": 'db.account.aggregate([{"$project":{"_id":0,"account_id":1}},{"$limit":1}])',
    }
    (data_dir / "TEND.json").write_text(
        json.dumps([record], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (data_dir / "test.json").write_text(
        json.dumps([record], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (pairs_dir / "post_surgery_nl_mql_pairs.jsonl").write_text("", encoding="utf-8")
    return root


def test_anti_template_violations_flag_old_release_phrases() -> None:
    text = (
        "On `account` for `financial.stub`, return the top 1 documents; "
        "predicate fields status=present; output fields account_id."
    )

    violations = anti_template_violations_for_text(text)

    assert "starts with On `" in violations
    assert "contains banned phrase 'predicate fields'" in violations
    assert "contains banned phrase 'output fields'" in violations
    assert "contains semicolon-separated slot list" in violations


def test_run_llm_nlq_rewrite_applies_stubbed_rewrites(tmp_path) -> None:
    release = _write_release(tmp_path)
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    client = LLMClient(settings, log)
    client.set_stub(stub_fn)

    summary = asyncio.run(
        run_llm_nlq_rewrite(
            release,
            llm=client,
            logger=log,
            out_dir=tmp_path / "rewrite",
            workers=2500,
            apply=True,
        )
    )
    log.close()

    assert summary.calls_ok == 1
    assert summary.calls_failed == 0
    assert summary.invalid_rewrites == 0
    assert summary.applied_updates == 2
    assert summary.anti_template_violations == 0
    records = json.loads((release / "data" / "TEND.json").read_text(encoding="utf-8"))
    nlq = records[0]["nl_queries"]
    assert not nlq["canonical"].startswith("On `")
    assert not nlq["colloquial"].startswith("Show the top")
    assert "output fields" not in nlq["canonical"]
    assert "with fields" not in nlq["colloquial"]
    lean = json.loads((release / "data" / "TEND_lean.json").read_text(encoding="utf-8"))
    assert lean[0]["NLQ"] == nlq["canonical"]
    assert lean[0]["NLQ_colloquial"] == nlq["colloquial"]


def test_build_anti_template_report_counts_track_violations() -> None:
    records = [
        {
            "nl_queries": {
                "canonical": "On `account`, return documents; output fields account_id.",
                "colloquial": "Show the top 1 documents from `account` with fields account_id.",
            }
        }
    ]

    report = build_anti_template_report(records)

    assert report["violations"] >= 4
    assert report["tracks"]["canonical"]["starts_on_backtick"] == 1
    assert report["tracks"]["colloquial"]["starts_show_the_top"] == 1
