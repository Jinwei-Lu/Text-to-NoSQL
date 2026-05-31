"""Assert mandatory 13-item disclosure for a TEND evaluation release."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tend.evaluate.disclosure import check_disclosure_artifacts, disclosure_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assert TEND disclosure checklist")
    parser.add_argument("--release", default="dev0", help="Release tag suffix")
    parser.add_argument(
        "--eval-dir",
        default="out/eval",
        help="Evaluation output directory with artifacts",
    )
    parser.add_argument(
        "--require-panel",
        action="store_true",
        help="Require non-stub panel pr (Gate-F)",
    )
    args = parser.parse_args(argv)

    eval_dir = Path(args.eval_dir)
    leaderboard_path = eval_dir / "leaderboard.json"
    leaderboard = {}
    if leaderboard_path.exists():
        leaderboard = json.loads(leaderboard_path.read_text(encoding="utf-8"))

    meta_path = eval_dir / "_meta.json"
    panel_stub = False
    if meta_path.exists():
        panel_stub = bool(json.loads(meta_path.read_text(encoding="utf-8")).get("panel_stub", False))

    checks = check_disclosure_artifacts(
        eval_dir,
        leaderboard=leaderboard,
        panel_stub=panel_stub,
    )
    report = disclosure_report(checks)
    report_path = eval_dir / "disclosure_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    require_panel = args.require_panel or not panel_stub
    complete = report["complete"] if require_panel else all(
        check.present for check in checks if check.key != "panel_pr_quadruple"
    )

    if complete:
        print(f"OK: disclosure complete for release {args.release}")
        return 0

    missing = report["missing"]
    print(f"FAIL: disclosure incomplete ({len(missing)} missing): {', '.join(missing)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
