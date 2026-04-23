from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
import json
import threading
import time

from tend_core import (
    Record,
    append_jsonl,
    load_json,
    schema_signature,
    si_hash,
    write_json,
)
from tend_construct.phase_a.domain_templates import (
    build_domain_catalog,
    default_assets_root,
    load_domain_templates,
)
from tend_construct.phase_a.phenomena_planter import plant_phenomena
from tend_construct.phase_a.schema_composer import compose_schema, schema_audit_payload
from tend_construct.phase_a.validators import validate_phase_a_bundle, validate_constructed_records
from tend_construct.phase_a.witness_generator import generate_witness_world
from tend_construct.phase_b.intents import (
    DEFAULT_INTENT_TEMPLATE_LATTICE,
    DEFAULT_PERSONA_BANK,
    IntentSeeder,
    SIRegistry,
    build_structured_intent,
)
from tend_construct.phase_c.materializer import materialize_record
from tend_construct.phase_c.lift import generate_grammar_variants, run_p1_p4_checks
from tend_construct.phase_d.external_runner import build_external_model_runner
from tend_construct.phase_d.validation import DiversityState, certify_record


@dataclass(frozen=True)
class BuildConfig:
    assets_root: Path
    output_root: Path
    dbs_per_template: int = 2
    records_per_db: int = 4
    base_seed: int = 7
    validation_backend: str = "stub"
    mongo_uri: str = "mongodb://localhost:27017"
    failure_bank_root: Path | None = None
    external_runner_kind: str = "noop"
    external_command: str | None = None
    external_base_url: str | None = None
    external_model: str | None = None
    external_api_key: str | None = None
    external_api_key_env: str = "OPENAI_API_KEY"
    max_workers: int = 1
    checkpoint_enabled: bool = True

    @property
    def target_db_count(self) -> int:
        from tend_construct.phase_a.domain_templates import load_domain_templates
        templates = load_domain_templates(self.assets_root)
        return len(templates) * self.dbs_per_template


class TendConstructPipeline:
    def __init__(self, config: BuildConfig):
        self.config = config
        self.output_root = config.output_root
        self.registry_dir = self.output_root / "_registry"
        self.si_registry = SIRegistry(self.registry_dir / "si_registry.jsonl")
        self.intent_seeder = IntentSeeder()
        self.diversity_state = DiversityState()
        self.record_counter = 1000
        self._counter_lock = threading.Lock()
        self.model_runner = build_external_model_runner(
            runner_kind=config.external_runner_kind,
            command=config.external_command,
            base_url=config.external_base_url,
            model=config.external_model,
            api_key=config.external_api_key,
            api_key_env=config.external_api_key_env,
        )

    def build_dataset(self) -> dict[str, Any]:
        templates = load_domain_templates(self.config.assets_root)
        self._prepare_output_dirs()
        self._write_global_assets(templates)

        checkpoint = self._load_checkpoint()
        completed_dbs = set(checkpoint.get("completed_dbs", []))

        all_db_tasks: list[tuple[dict[str, Any], str]] = []
        for template in templates:
            template_dict = template.to_dict()
            for db_index in range(self.config.dbs_per_template):
                db_id = f"{template.domain_id}_{db_index + 1:03d}"
                all_db_tasks.append((template_dict, db_id))

        train_records: list[dict[str, Any]] = []
        test_records: list[dict[str, Any]] = []
        manifest_rows: list[dict[str, Any]] = []

        # Load already completed records from checkpoint
        for db_id in completed_dbs:
            saved = self._load_db_checkpoint(db_id)
            if saved:
                manifest_rows.append(saved["manifest"])
                train_records.extend(saved["train_records"])
                test_records.extend(saved["test_records"])

        pending_tasks = [
            (tpl, db_id) for tpl, db_id in all_db_tasks
            if db_id not in completed_dbs
        ]

        if self.config.max_workers > 1 and len(pending_tasks) > 1:
            self._build_parallel(pending_tasks, train_records, test_records, manifest_rows, completed_dbs)
        else:
            for template_dict, db_id in pending_tasks:
                db_summary = self._build_db(template_dict, db_id)
                manifest_rows.append(db_summary["manifest"])
                train_records.extend(db_summary["train_records"])
                test_records.extend(db_summary["test_records"])
                if self.config.checkpoint_enabled:
                    self._save_db_checkpoint(db_id, db_summary)
                    completed_dbs.add(db_id)
                    self._save_checkpoint({"completed_dbs": sorted(completed_dbs)})

        all_records = sorted(train_records + test_records, key=lambda item: item["record_id"])
        write_json(self.output_root / "train.json", sorted(train_records, key=lambda item: item["record_id"]))
        write_json(self.output_root / "test.json", sorted(test_records, key=lambda item: item["record_id"]))
        write_json(self.output_root / "TEND.json", all_records)

        dataset_manifest = {
            "db_count": len(manifest_rows),
            "record_count": len(all_records),
            "train_count": len(train_records),
            "test_count": len(test_records),
            "dbs": manifest_rows,
        }
        write_json(self.output_root / "dataset_manifest.json", dataset_manifest)
        self._write_disclosure_manifest(dataset_manifest, all_records)

        return {
            "db_count": len(manifest_rows),
            "record_count": len(all_records),
            "train_count": len(train_records),
            "test_count": len(test_records),
        }

    def _build_parallel(
        self,
        tasks: list[tuple[dict[str, Any], str]],
        train_records: list[dict[str, Any]],
        test_records: list[dict[str, Any]],
        manifest_rows: list[dict[str, Any]],
        completed_dbs: set[str],
    ) -> None:
        with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            futures = {
                executor.submit(self._build_db, tpl, db_id): db_id
                for tpl, db_id in tasks
            }
            for future in as_completed(futures):
                db_id = futures[future]
                db_summary = future.result()
                manifest_rows.append(db_summary["manifest"])
                train_records.extend(db_summary["train_records"])
                test_records.extend(db_summary["test_records"])
                if self.config.checkpoint_enabled:
                    self._save_db_checkpoint(db_id, db_summary)
                    completed_dbs.add(db_id)
                    self._save_checkpoint({"completed_dbs": sorted(completed_dbs)})

    def validate_dataset(self) -> dict[str, Any]:
        errors: list[str] = []
        schema_dir = self.output_root / "mongodb_schema"
        data_dir = self.output_root / "mongodb_data"
        registry_dir = self.output_root / "phenomena_registry"
        for schema_path in sorted(schema_dir.glob("*.json")):
            db_id = schema_path.stem
            schema_payload = load_json(schema_path)
            data_payload = load_json(data_dir / f"{db_id}.json")
            registry_payload = load_json(registry_dir / f"{db_id}.json")
            errors.extend(
                f"{db_id}: {error}"
                for error in validate_phase_a_bundle(schema_payload, data_payload, registry_payload)
            )

        train = load_json(self.output_root / "train.json")
        test = load_json(self.output_root / "test.json")
        train_domains = {item["db_id"].rsplit("_", 1)[0] for item in train}
        test_domains = {item["db_id"].rsplit("_", 1)[0] for item in test}
        overlap = sorted(train_domains & test_domains)
        if overlap:
            errors.append(f"domain split overlap: {overlap}")

        all_records = train + test
        errors.extend(validate_constructed_records(all_records))

        return {"ok": not errors, "errors": errors}

    def _build_db(self, template: dict[str, Any], db_id: str) -> dict[str, Any]:
        schema_spec = compose_schema(
            template, db_id,
            topology_seed=self.config.base_seed + hash(db_id) % 10000,
        )
        schema_payload = schema_spec.to_publish_dict()
        schema_audit = schema_audit_payload(schema_spec)
        witness_world, noise_manifest = generate_witness_world(
            template,
            schema_spec.collections,
            db_id=db_id,
            noise_seed=self.config.base_seed + len(db_id),
        )
        planted_data, registry_payload, planter_trace = plant_phenomena(
            db_id=db_id,
            template=template,
            witness_data=witness_world.data,
            phenomena_seed=self.config.base_seed + len(db_id) * 3,
            noise_seed=self.config.base_seed + len(db_id),
        )
        registry_payload["_meta"] = {"schema_signature": schema_signature(schema_payload)}

        self._write_db_assets(
            db_id=db_id,
            schema_payload=schema_payload,
            data_payload=planted_data,
            registry_payload=registry_payload,
            schema_audit=schema_audit,
            noise_manifest=noise_manifest,
            planter_trace=planter_trace,
        )
        phase_a_errors = validate_phase_a_bundle(schema_payload, planted_data, registry_payload)
        if phase_a_errors:
            raise ValueError(f"Phase A validation failed for {db_id}: {phase_a_errors}")

        train_records: list[dict[str, Any]] = []
        test_records: list[dict[str, Any]] = []
        created = 0
        attempts = 0
        while created < self.config.records_per_db and attempts < self.config.records_per_db * 4:
            attempts += 1
            with self._counter_lock:
                self.record_counter += 1
                record_id = self.record_counter
            seed_tuple = self.intent_seeder.choose_seed(
                registry_payload,
                db_id=db_id,
                seed=self.config.base_seed,
                offset=attempts - 1,
                schema_payload=schema_payload,
            )
            si = build_structured_intent(db_id, record_id, seed_tuple, schema_payload)
            digest = si_hash(si.to_dict())
            if self.si_registry.exists(db_id, digest):
                continue
            si = replace(si, meta={**si.meta, "si_hash": digest})
            self.si_registry.register(db_id=db_id, record_id=record_id, si_payload=si.to_dict())
            si, _mql, qir, record, checker, mutations = materialize_record(
                record_id=record_id,
                db_id=db_id,
                structured_intent=si,
                model_runner=self.model_runner,
                schema_payload=schema_payload,
                witness_payload=planted_data,
            )
            variants = generate_grammar_variants(
                _mql, si, variant_seed=self.config.base_seed + record_id,
            )
            p_checks = run_p1_p4_checks(
                _mql, si, qir, record.canonical_form_set, mutations, variants,
            )
            record = replace(record, world_signature=registry_payload["world_signature"])
            certificate = certify_record(
                bundle_root=self.output_root,
                domain_id=template["domain_id"],
                record=record,
                structured_intent=si,
                schema=schema_payload,
                witness=planted_data,
                mutations=mutations,
                diversity_state=self.diversity_state,
                backend_name=self.config.validation_backend,
                mongo_uri=self.config.mongo_uri,
                failure_bank_root=self.config.failure_bank_root,
                model_runner=self.model_runner,
            )
            record = replace(record, empirical_difficulty=certificate.empirical_difficulty)
            self._write_record_audit(
                db_id, record, si, qir.to_dict(), checker, mutations,
                certificate.to_dict(), variants, p_checks,
            )
            payload = record.to_dict()
            if certificate.split == "test":
                test_records.append(payload)
            else:
                train_records.append(payload)
            created += 1

        return {
            "train_records": train_records,
            "test_records": test_records,
            "manifest": {
                "db_id": db_id,
                "domain_id": template["domain_id"],
                "record_count": created,
                "schema_signature": schema_audit["schema_signature"],
                "world_signature": registry_payload["world_signature"],
            },
        }

    def _prepare_output_dirs(self) -> None:
        for path in [
            self.output_root / "mongodb_schema",
            self.output_root / "mongodb_data",
            self.output_root / "phenomena_registry",
            self.output_root / "audit",
            self.registry_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def _write_global_assets(self, templates: list[Any]) -> None:
        write_json(self.output_root / "persona_bank.json", DEFAULT_PERSONA_BANK)
        write_json(self.output_root / "intent_template_lattice.json", DEFAULT_INTENT_TEMPLATE_LATTICE)
        write_json(self.output_root / "domain_catalog.json", build_domain_catalog(templates))

    def _write_db_assets(
        self,
        db_id: str,
        schema_payload: dict[str, Any],
        data_payload: dict[str, Any],
        registry_payload: dict[str, Any],
        schema_audit: dict[str, Any],
        noise_manifest: dict[str, Any],
        planter_trace: dict[str, Any],
    ) -> None:
        write_json(self.output_root / "mongodb_schema" / f"{db_id}.json", schema_payload)
        write_json(self.output_root / "mongodb_data" / f"{db_id}.json", data_payload)
        write_json(self.output_root / "phenomena_registry" / f"{db_id}.json", registry_payload)

        audit_dir = self.output_root / "audit" / db_id
        write_json(audit_dir / "schema_complexity_profile.json", schema_audit["schema_complexity_profile"])
        (audit_dir / "schema_signature.txt").write_text(schema_audit["schema_signature"], encoding="utf-8")
        write_json(audit_dir / "noise_injection_manifest.json", noise_manifest)
        write_json(audit_dir / "phenomena_planter_trace.json", planter_trace)
        (audit_dir / "world_signature.txt").write_text(registry_payload["world_signature"], encoding="utf-8")

    def _write_record_audit(
        self,
        db_id: str,
        record: Record,
        structured_intent: Any,
        qir_payload: dict[str, Any],
        checker: dict[str, Any],
        mutations: list[dict[str, Any]],
        certificate_payload: dict[str, Any],
        variants: list[str] | None = None,
        p_checks: dict[str, Any] | None = None,
    ) -> None:
        record_dir = self.output_root / "audit" / db_id / str(record.record_id)
        derived_dir = record_dir / "derived"
        write_json(record_dir / "structured_intent.yaml", structured_intent.to_dict())
        write_json(record_dir / "qir.yaml", qir_payload)
        write_json(derived_dir / "checker.json", checker)
        write_json(derived_dir / "mutations.json", mutations)
        write_json(derived_dir / "canonical_form_set.json", record.canonical_form_set.to_dict())
        write_json(record_dir / "certificate.json", certificate_payload)
        if variants:
            write_json(derived_dir / "grammar_variants.json", variants)
        if p_checks:
            write_json(derived_dir / "p_checks.json", p_checks)
        append_jsonl(
            self.registry_dir / "record_manifest.jsonl",
            {
                "record_id": record.record_id,
                "db_id": db_id,
                "split": certificate_payload["split"],
                "empirical_difficulty": certificate_payload["empirical_difficulty"],
            },
        )

    def _write_disclosure_manifest(
        self,
        dataset_manifest: dict[str, Any],
        all_records: list[dict[str, Any]],
    ) -> None:
        from collections import Counter
        patterns = Counter(r.get("operator_family", "?") for r in all_records)
        difficulties = Counter(r.get("empirical_difficulty", "?") for r in all_records)
        nativeness = Counter(r.get("nosql_nativeness_level", "?") for r in all_records)
        domains = sorted(set(r.get("db_id", "").rsplit("_", 1)[0] for r in all_records))

        disclosure = {
            "version": "0.1.0",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "record_count": dataset_manifest["record_count"],
            "db_count": dataset_manifest["db_count"],
            "train_count": dataset_manifest["train_count"],
            "test_count": dataset_manifest["test_count"],
            "pattern_distribution": dict(patterns.most_common()),
            "difficulty_distribution": dict(difficulties.most_common()),
            "nativeness_distribution": dict(nativeness.most_common()),
            "domain_list": domains,
            "domain_count": len(domains),
            "split_method": "cross_domain_holdout",
            "noise_taxonomy_version": "6x6_v1",
            "phenomena_taxonomy_version": "15_class_v1",
            "pattern_family_count": len(set(patterns.keys())),
            "validation_backend": self.config.validation_backend,
        }
        write_json(self.output_root / "disclosure_manifest.json", disclosure)

    # --- Checkpoint support ---

    def _checkpoint_path(self) -> Path:
        return self.registry_dir / "checkpoint.json"

    def _load_checkpoint(self) -> dict[str, Any]:
        path = self._checkpoint_path()
        if not self.config.checkpoint_enabled or not path.exists():
            return {}
        return load_json(path)

    def _save_checkpoint(self, data: dict[str, Any]) -> None:
        write_json(self._checkpoint_path(), data)

    def _db_checkpoint_path(self, db_id: str) -> Path:
        return self.registry_dir / f"db_checkpoint_{db_id}.json"

    def _save_db_checkpoint(self, db_id: str, summary: dict[str, Any]) -> None:
        write_json(self._db_checkpoint_path(db_id), summary)

    def _load_db_checkpoint(self, db_id: str) -> dict[str, Any] | None:
        path = self._db_checkpoint_path(db_id)
        if path.exists():
            return load_json(path)
        return None


def build_dataset(
    output_root: Path,
    assets_root: Path | None = None,
    dbs_per_template: int = 2,
    records_per_db: int = 4,
    base_seed: int = 7,
    validation_backend: str = "stub",
    mongo_uri: str = "mongodb://localhost:27017",
    failure_bank_root: Path | None = None,
    external_runner_kind: str = "noop",
    external_command: str | None = None,
    external_base_url: str | None = None,
    external_model: str | None = None,
    external_api_key: str | None = None,
    external_api_key_env: str = "OPENAI_API_KEY",
    max_workers: int = 1,
    checkpoint_enabled: bool = True,
) -> dict[str, Any]:
    config = BuildConfig(
        assets_root=assets_root or default_assets_root(),
        output_root=output_root,
        dbs_per_template=dbs_per_template,
        records_per_db=records_per_db,
        base_seed=base_seed,
        validation_backend=validation_backend,
        mongo_uri=mongo_uri,
        failure_bank_root=failure_bank_root,
        external_runner_kind=external_runner_kind,
        external_command=external_command,
        external_base_url=external_base_url,
        external_model=external_model,
        external_api_key=external_api_key,
        external_api_key_env=external_api_key_env,
        max_workers=max_workers,
        checkpoint_enabled=checkpoint_enabled,
    )
    return TendConstructPipeline(config).build_dataset()


def validate_dataset(output_root: Path, assets_root: Path | None = None) -> dict[str, Any]:
    config = BuildConfig(
        assets_root=assets_root or default_assets_root(),
        output_root=output_root,
    )
    return TendConstructPipeline(config).validate_dataset()
