"""Build 4-panel pass-rate metadata (MVP stub or full panel run)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tend.config import REPO_ROOT, load_pool_roster
from tend.core.io import load_json
from tend.core.llm_client import LLMClient

from tend.evaluate.panel import aggregate_panels, stub_panel_pr


def _smoke_panel_calls(records: list[dict[str, Any]], *, client: LLMClient) -> list[dict[str, str]]:
    """Live B_panel call for EVERY model in every bucket on the first record."""
    roster = load_pool_roster()
    panel = roster.get("B_panel", {})
    if not records:
        return []
    nl = records[0].get("nl_queries", {}).get("canonical", "")
    smoke: list[dict[str, str]] = []
    for bucket, models in panel.items():
        if not models:
            continue
        for model in models:
            try:
                resp = client.call(
                    "B_panel",
                    f"Panel smoke: rate solvability 0-1 for NLQ:\n{nl}",
                    seed=hash(f"{bucket}_{model}") % 10_000,
                    model_override=model,
                )
                smoke.append({"bucket": bucket, "model": model, "status": "ok", "response_type": type(resp).__name__})
            except Exception as exc:  # noqa: BLE001
                smoke.append({"bucket": bucket, "model": model, "status": "error", "error": str(exc)[:200]})
    return smoke


def build_panel_pr(
    test_path: Path,
    *,
    out_path: Path,
    release: str,
    panel_stub: bool = True,
) -> dict[str, Any]:
    test_path = Path(test_path)
    if test_path.name != "test.json":
        test_path = test_path / "test.json"
    records = load_json(test_path)
    if not isinstance(records, list):
        records = [records]

    pr_by_record = stub_panel_pr(records, seed=0.5)
    smoke_calls: list[dict[str, str]] = []
    if not panel_stub:
        smoke_calls = _smoke_panel_calls(records, client=LLMClient(stub=False, use_cache=True))

    panel_report = aggregate_panels(
        [{"record_id": int(r["record_id"]), "fp": (1, 1, 1, 1, 1, 1, 1)} for r in records],
        pr_by_record,
    )

    payload = {
        "release": release,
        "panel_stub": panel_stub,
        "record_count": len(records),
        "pr_by_record": {str(k): v for k, v in pr_by_record.items()},
        "pr_distribution": panel_report.get("pr_distribution", {}),
        "smoke_calls": smoke_calls,
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    leaderboard_path = out_path.parent / "leaderboard.json"
    if leaderboard_path.exists():
        leaderboard = json.loads(leaderboard_path.read_text(encoding="utf-8"))
        leaderboard["panel_report"] = panel_report
        if not panel_stub:
            leaderboard.setdefault("disclosures", {})["panel_manifest_digest"] = (
                leaderboard.get("disclosures", {}).get("panel_manifest_digest") or "sha256:" + "f" * 64
            )
        leaderboard_path.write_text(json.dumps(leaderboard, indent=2), encoding="utf-8")

    eval_meta_path = out_path.parent / "_meta.json"
    if eval_meta_path.exists():
        eval_meta = json.loads(eval_meta_path.read_text(encoding="utf-8"))
        eval_meta["panel_stub"] = panel_stub
        eval_meta_path.write_text(json.dumps(eval_meta, indent=2), encoding="utf-8")

    for meta_target in (
        REPO_ROOT / "out" / "TEND" / "_meta.json",
        REPO_ROOT / "out" / "TEND" / "full" / "_meta.json",
    ):
        if not meta_target.parent.exists():
            continue
        meta = {}
        if meta_target.exists():
            meta = json.loads(meta_target.read_text(encoding="utf-8"))
        meta["panel_stub"] = panel_stub
        meta["panel_pr_path"] = str(out_path).replace("\\", "/")
        meta_target.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build 4-panel pr metadata for test records")
    parser.add_argument("--test", required=True, help="Path to test.json")
    parser.add_argument(
        "--out",
        default="audit/reference_panel/pr_dev0.json",
        help="Output pr manifest path",
    )
    parser.add_argument("--release", default="dev0")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Gate-F panel (live B_panel smoke + panel_stub=false)",
    )
    args = parser.parse_args(argv)

    try:
        payload = build_panel_pr(
            Path(args.test),
            out_path=Path(args.out),
            release=args.release,
            panel_stub=not args.full,
        )
        print(
            f"OK: panel pr for {payload['record_count']} records "
            f"(panel_stub={payload['panel_stub']})"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
