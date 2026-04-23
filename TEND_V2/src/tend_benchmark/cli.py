from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from .evaluator import evaluate_bundle, export_solver_view


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="tend-benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export-solver-view")
    export_parser.add_argument("--bundle", required=True, type=Path)
    export_parser.add_argument("--out", required=True, type=Path)
    export_parser.add_argument("--split", default="test")

    eval_parser = subparsers.add_parser("evaluate")
    eval_parser.add_argument("--bundle", required=True, type=Path)
    eval_parser.add_argument("--predictions", required=True, type=Path)
    eval_parser.add_argument("--out", required=True, type=Path)
    eval_parser.add_argument("--split", default="test")
    eval_parser.add_argument("--backend", choices=["replay", "local-mongo"], default="replay")
    eval_parser.add_argument("--mongo-uri", default="mongodb://localhost:27017")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "export-solver-view":
        export_solver_view(bundle_root=args.bundle, out_path=args.out, split=args.split)
        print(f"solver view exported to {args.out}")
        return

    if args.command == "evaluate":
        rows = evaluate_bundle(
            bundle_root=args.bundle,
            predictions_path=args.predictions,
            out_dir=args.out,
            split=args.split,
            backend_name=args.backend,
            mongo_uri=args.mongo_uri,
        )
        print(f"evaluated {len(rows)} records into {args.out}")
        return

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
