from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

from .phase_d.external_runner import DEFAULT_LLM_MODEL, SUPPORTED_LLM_MODELS
from .pipeline import build_dataset, validate_dataset


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="tend-construct")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser_cmd = subparsers.add_parser("build-dataset")
    build_parser_cmd.add_argument("--out", required=True, type=Path)
    build_parser_cmd.add_argument("--assets-root", type=Path, default=None)
    build_parser_cmd.add_argument("--dbs-per-template", type=int, default=2)
    build_parser_cmd.add_argument("--records-per-db", type=int, default=4)
    build_parser_cmd.add_argument("--base-seed", type=int, default=7)
    build_parser_cmd.add_argument(
        "--validation-backend",
        choices=["stub", "local-mongo"],
        default="stub",
    )
    build_parser_cmd.add_argument("--mongo-uri", default="mongodb://localhost:27017")
    build_parser_cmd.add_argument("--failure-bank-root", type=Path, default=None)
    build_parser_cmd.add_argument(
        "--external-runner-kind",
        choices=["noop", "command", "openai-compatible"],
        default="noop",
    )
    build_parser_cmd.add_argument("--external-command", default=None)
    build_parser_cmd.add_argument("--external-base-url", default=None)
    build_parser_cmd.add_argument("--external-model", choices=SUPPORTED_LLM_MODELS, default=DEFAULT_LLM_MODEL)
    build_parser_cmd.add_argument("--external-api-key", default=None)
    build_parser_cmd.add_argument("--external-api-key-env", default="OPENAI_API_KEY")
    build_parser_cmd.add_argument("--max-workers", type=int, default=1,
                                  help="Parallel workers for db construction")
    build_parser_cmd.add_argument("--no-checkpoint", action="store_true",
                                  help="Disable checkpoint/resume support")

    # Preset scale profiles
    build_parser_cmd.add_argument(
        "--scale",
        choices=["tiny", "small", "medium", "full"],
        default=None,
        help="Preset scale: tiny(1db/2rec), small(2db/4rec), medium(4db/8rec), full(8db/50rec)",
    )

    validate_parser_cmd = subparsers.add_parser("validate")
    validate_parser_cmd.add_argument("--bundle", required=True, type=Path)
    validate_parser_cmd.add_argument("--assets-root", type=Path, default=None)

    return parser


SCALE_PRESETS = {
    "tiny": {"dbs_per_template": 1, "records_per_db": 2},
    "small": {"dbs_per_template": 2, "records_per_db": 4},
    "medium": {"dbs_per_template": 4, "records_per_db": 8},
    "full": {"dbs_per_template": 8, "records_per_db": 50},
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "build-dataset":
        dbs = args.dbs_per_template
        recs = args.records_per_db
        if args.scale and args.scale in SCALE_PRESETS:
            preset = SCALE_PRESETS[args.scale]
            dbs = preset["dbs_per_template"]
            recs = preset["records_per_db"]

        summary = build_dataset(
            output_root=args.out,
            assets_root=args.assets_root,
            dbs_per_template=dbs,
            records_per_db=recs,
            base_seed=args.base_seed,
            validation_backend=args.validation_backend,
            mongo_uri=args.mongo_uri,
            failure_bank_root=args.failure_bank_root,
            external_runner_kind=args.external_runner_kind,
            external_command=args.external_command,
            external_base_url=args.external_base_url,
            external_model=args.external_model,
            external_api_key=args.external_api_key,
            external_api_key_env=args.external_api_key_env,
            max_workers=args.max_workers,
            checkpoint_enabled=not args.no_checkpoint,
        )
        print(f"built dataset into {args.out}: {summary}")
        return

    if args.command == "validate":
        result = validate_dataset(output_root=args.bundle, assets_root=args.assets_root)
        print(result)
        return

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
