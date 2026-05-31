"""Run TEND evaluation on test.json with solver narrow-face enforcement."""

from __future__ import annotations

import argparse
import csv
import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from tend.config import REPO_ROOT, SCHEMAS_ROOT
from tend.core import AST_check, NormExec, disabled_operator_scanner
from tend.core.io import load_json
from tend.core.models import EvaluationRow, Record
from tend.errors import BOT, BOT_EXEC

from tend.evaluate.disjointness import write_disjointness_manifest
from tend.evaluate.fingerprint import compute_fingerprint
from tend.evaluate.leaderboard import build_leaderboard_payload, validate_leaderboard_payload, write_leaderboard
from tend.evaluate.panel import stub_panel_pr
from tend.evaluate.slice_aggregate import aggregate_slices

ALLOW_LIST_PATH = SCHEMAS_ROOT / "solver_allow_list.json"


def load_allow_list() -> dict[str, Any]:
    return json.loads(ALLOW_LIST_PATH.read_text(encoding="utf-8"))


def _normalize_posix(path: str) -> str:
    return path.replace("\\", "/")


def path_matches_glob(path: str, pattern: str) -> bool:
    path = _normalize_posix(path)
    pattern = _normalize_posix(pattern)
    if ":" in pattern:
        file_part, field_part = pattern.split(":", 1)
        if not fnmatch.fnmatch(path, file_part):
            return False
        return field_part in path or field_part == "*"
    return fnmatch.fnmatch(path, pattern)


def assert_solver_path_allowed(path: Path | str, *, allow_list: dict[str, Any] | None = None) -> None:
    allow_list = allow_list or load_allow_list()
    rel = _normalize_posix(str(path))
    if rel.startswith("/") or ":" in rel[:3]:
        rel = Path(path).as_posix()

    for pattern in allow_list.get("tier1_forbidden_glob", []):
        if path_matches_glob(rel, pattern):
            raise PermissionError(f"Solver narrow-face forbids path: {rel} (matched {pattern})")

    for pattern in allow_list.get("audit_blocklist", []):
        if path_matches_glob(rel, pattern):
            raise PermissionError(f"Solver blocked audit path: {rel}")


def narrow_record_for_solver(record: dict[str, Any], *, allow_list: dict[str, Any] | None = None) -> dict[str, Any]:
    allow_list = allow_list or load_allow_list()
    forbidden = set(allow_list.get("test_record_forbidden_fields", []))
    view = {key: value for key, value in record.items() if key not in forbidden and not key.endswith("_ref")}
    if "nl_queries" in view and isinstance(view["nl_queries"], dict):
        view["nl_queries"] = {
            key: view["nl_queries"][key]
            for key in ("canonical", "colloquial")
            if key in view["nl_queries"]
        }
    return view


def echo_gold_solver(record: dict[str, Any], **_kwargs: Any) -> str:
    assert_solver_path_allowed("test.json:record_id")
    narrow = narrow_record_for_solver(record)
    if "MQL" in narrow:
        raise PermissionError("Solver narrow-face forbids reading test.json:MQL")
    return record["MQL"]


SOLVERS: dict[str, Callable[..., str]] = {
    "echo_gold": echo_gold_solver,
}


def _resolve_bundle_root(test_path: Path) -> Path:
    test_path = Path(test_path)
    if test_path.name == "test.json":
        return test_path.parent
    return test_path


def _load_records(test_path: Path) -> list[Record]:
    payload = load_json(test_path)
    if isinstance(payload, list):
        return [Record.from_dict(item) for item in payload]
    return [Record.from_dict(payload)]


def _load_witness(bundle_root: Path, db_id: str) -> dict[str, Any]:
    return load_json(bundle_root / "mongodb_data" / f"{db_id}.json")


def evaluate_records(
    bundle_root: Path,
    records: list[Record],
    solver_fn: Callable[..., str],
    *,
    out_dir: Path,
) -> tuple[list[dict[str, Any]], list[EvaluationRow]]:
    bundle_root = Path(bundle_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fingerprint_rows: list[dict[str, Any]] = []
    eval_rows: list[EvaluationRow] = []

    for record in records:
        raw = record.raw or record.to_dict()
        prediction = solver_fn(record=raw, bundle_root=bundle_root)
        forbidden_hit = disabled_operator_scanner(prediction)
        ast_result = AST_check(prediction, record.canonical_form_set)

        witness = _load_witness(bundle_root, record.db_id)
        r_p: Any = []
        r_g: Any = []
        parse_error = False
        timeout_hit = False
        oom_hit = False
        exec_error: str | None = None

        if ast_result == "fail:parse_error":
            parse_error = True
        else:
            try:
                r_g = NormExec(record.mql, witness)
                r_p = NormExec(prediction, witness)
            except Exception as exc:  # noqa: BLE001
                exec_error = str(exc)

        if isinstance(r_p, BOT):
            parse_error = True
        if isinstance(r_p, BOT_EXEC):
            message = str(r_p)
            exec_error = message
            if "timeout" in message.lower():
                timeout_hit = True
            if "oom" in message.lower() or "memory" in message.lower():
                oom_hit = True

        fp = compute_fingerprint(
            prediction,
            record.mql,
            r_p if not isinstance(r_p, (BOT, BOT_EXEC)) else r_p,
            r_g if not isinstance(r_g, (BOT, BOT_EXEC)) else r_g,
            record.canonical_form_set,
            ast_result=ast_result,
            forbidden_op_hit=forbidden_hit,
            timeout_hit=timeout_hit,
            oom_hit=oom_hit,
        )
        em, qsm, qfc, ex, efm, evm, qim = fp

        eval_rows.append(
            EvaluationRow(
                record_id=record.record_id,
                db_id=record.db_id,
                prediction=prediction,
                em=em,
                qsm=qsm,
                qfc=qfc,
                ex=ex,
                efm=efm,
                evm=evm,
                qim=qim,
                ast_result=ast_result,
                forbidden_op_hit=forbidden_hit,
                exec_error=exec_error,
            )
        )
        fingerprint_rows.append(
            {
                "record_id": record.record_id,
                "db_id": record.db_id,
                "fp": fp,
                "parse_error": parse_error,
                "timeout_hit": timeout_hit,
                "oom_hit": oom_hit,
                "forbidden_op_hit": forbidden_hit,
                "ast_result": ast_result,
            }
        )

    _write_eval_outputs(out_dir, eval_rows, fingerprint_rows, [r.raw or r.to_dict() for r in records])
    _write_disclosure_artifacts(
        out_dir,
        [r.raw or r.to_dict() for r in records],
        bundle_root=bundle_root,
    )
    return fingerprint_rows, eval_rows


def _write_disclosure_artifacts(
    out_dir: Path,
    records: list[dict[str, Any]],
    *,
    bundle_root: Path,
) -> None:
    difficulty_counts: dict[str, int] = {}
    sql_class_counts: dict[str, int] = {}
    ra_pass = 0
    ra_total = 0
    signatures: list[str] = []

    for record in records:
        tier = record.get("difficulty", "unknown")
        difficulty_counts[tier] = difficulty_counts.get(tier, 0) + 1
        sql_class = record.get("sql_infeasibility_class", "unknown")
        sql_class_counts[sql_class] = sql_class_counts.get(sql_class, 0) + 1
        ra_total += 1
        if record.get("ra_audit", {}).get("pass") is True:
            ra_pass += 1

    for db_id in sorted({r["db_id"] for r in records}):
        data_path = bundle_root / "mongodb_data" / f"{db_id}.json"
        if data_path.exists():
            from tend.core.signatures import world_signature

            signatures.append(world_signature(load_json(data_path)))

    (out_dir / "nnc_histogram.json").write_text(
        json.dumps(
            {
                "difficulty": difficulty_counts,
                "sql_infeasibility_class": sql_class_counts,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out_dir / "ra_pass_rate.json").write_text(
        json.dumps(
            {
                "pass_rate": (ra_pass / ra_total) if ra_total else 0.0,
                "passed": ra_pass,
                "total": ra_total,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    digest_source = "|".join(signatures) if signatures else "no-signatures"
    digest = hashlib.sha256(digest_source.encode()).hexdigest()
    (out_dir / "world_signature_digest.txt").write_text(f"sha256:{digest}\n", encoding="utf-8")


def _write_eval_outputs(
    out_dir: Path,
    eval_rows: list[EvaluationRow],
    fingerprint_rows: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> None:
    slices_dir = out_dir / "slices"
    slices_dir.mkdir(parents=True, exist_ok=True)
    slice_payload = aggregate_slices(fingerprint_rows, records)
    (slices_dir / "six_axes.json").write_text(json.dumps(slice_payload, indent=2), encoding="utf-8")

    with (out_dir / "fingerprints.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["record_id", "em", "qsm", "qfc", "ex", "efm", "evm", "qim"])
        for row in fingerprint_rows:
            em, qsm, qfc, ex, efm, evm, qim = row["fp"]
            writer.writerow([row["record_id"], em, qsm, qfc, ex, efm, evm, qim])

    with (out_dir / "per_record_metrics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "record_id",
                "db_id",
                "em",
                "qsm",
                "qfc",
                "ex",
                "efm",
                "evm",
                "qim",
                "ast_result",
                "forbidden_op_hit",
                "exec_error",
            ],
        )
        writer.writeheader()
        for item in eval_rows:
            writer.writerow(
                {
                    "record_id": item.record_id,
                    "db_id": item.db_id,
                    "em": item.em,
                    "qsm": item.qsm,
                    "qfc": item.qfc,
                    "ex": item.ex,
                    "efm": item.efm,
                    "evm": item.evm,
                    "qim": item.qim,
                    "ast_result": item.ast_result,
                    "forbidden_op_hit": int(item.forbidden_op_hit),
                    "exec_error": item.exec_error or "",
                }
            )


def run_evaluation(
    test_path: Path,
    *,
    solver: str,
    out_dir: Path,
    release_tag: str = "tend-release-dev0",
    submission_id: str = "tend-eval-dev",
    solver_id: str = "echo-gold",
) -> dict[str, Any]:
    test_path = Path(test_path)
    bundle_root = _resolve_bundle_root(test_path)
    records = _load_records(test_path)
    solver_fn = SOLVERS[solver]

    manifest_dir = REPO_ROOT / "audit" / "reference_panel"
    manifest_paths = write_disjointness_manifest(manifest_dir, release=release_tag.replace("tend-release-", ""))

    fingerprint_rows, _eval_rows = evaluate_records(bundle_root, records, solver_fn, out_dir=out_dir)

    panel_pr = stub_panel_pr([r.raw or r.to_dict() for r in records])
    (out_dir / "panel_pr.json").write_text(json.dumps(panel_pr, indent=2), encoding="utf-8")

    meta = {"panel_stub": True, "solver": solver, "record_count": len(records)}
    (out_dir / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    payload = build_leaderboard_payload(
        submission_id=submission_id,
        solver_id=solver_id,
        release_tag=release_tag,
        fingerprints=fingerprint_rows,
        records=[r.raw or r.to_dict() for r in records],
        panel_pr_meta=panel_pr,
        solver_llm_backbones=[
            {"model_id": "echo-gold", "vendor": "tend", "version_pin": "dev0"},
        ],
        eval_dir=out_dir,
        construction_gate_digest=json.loads(manifest_paths["construction_gate"].read_text(encoding="utf-8"))[
            "manifest_digest"
        ],
        evaluation_gate_digest=json.loads(manifest_paths["evaluation_gate"].read_text(encoding="utf-8"))[
            "manifest_digest"
        ],
        panel_stub=True,
    )
    validate_leaderboard_payload(payload)
    write_leaderboard(payload, out_dir / "leaderboard.json")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a solver on TEND test.json")
    parser.add_argument("--test", required=True, help="Path to test.json or bundle root")
    parser.add_argument("--solver", default="echo_gold", choices=sorted(SOLVERS))
    parser.add_argument("--out", default="out/eval", help="Evaluation output directory")
    parser.add_argument("--release-tag", default="tend-release-dev0")
    parser.add_argument("--submission-id", default="tend-eval-dev")
    parser.add_argument("--solver-id", default="echo-gold")
    args = parser.parse_args(argv)

    try:
        load_allow_list()
        payload = run_evaluation(
            Path(args.test),
            solver=args.solver,
            out_dir=Path(args.out),
            release_tag=args.release_tag,
            submission_id=args.submission_id,
            solver_id=args.solver_id,
        )
        print(
            f"OK: evaluated {payload['scores']['record_count']} records; "
            f"EX={payload['scores']['ex_unweighted']:.3f}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
