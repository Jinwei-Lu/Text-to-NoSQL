# TEND Construction Pipeline — Architecture

A dynamic-workflow orchestrator that spawns LLM sub-agents to construct the TEND
Text-to-NoSQL benchmark from BIRD mini-dev. Phase A builds the MongoDB DataWorld;
Phase B reverse-engineers NL–MQL records.

## Layout (`src/tend/`)

| Module | Role |
|---|---|
| `config.py` | `.env` loading, paths, OpenAI-compatible provider (DeepSeek default), toggles |
| `errors.py` | Typed exception taxonomy + `Anomaly` classification — the backbone of anomaly capture |
| `source/bird.py` | BIRD mini-dev loader: schema, workload, column enums, SQLite probes |
| `observability/logging.py` | File-first JSONL logging: `events.jsonl`, `anomalies.jsonl`, per-call `llm/<agent>/<call_id>.md` transcripts plus `.diagnostics.json` sidecars; anomaly subscriber callbacks |
| `observability/progress.py` | Live `rich` phase→group→task tree + counters + anomaly ticker |
| `llm/client.py` | Async OpenAI-compatible client: transport retries, JSON/schema repair loop, per-call Markdown transcripts + diagnostics, typed anomaly classification, stub mode |
| `agents/base.py` | `Agent` lifecycle wrapper + `LLMAgent` (prompt→schema→contract repair) + registry |
| `agents/phase_a.py` | WP, SRA, SC (LLM) |
| `agents/dm.py` | DM — **deterministic** migration agent |
| `agents/phase_b.py` | QPS, MS, MUT, PV, NLP, RTV, NNC, RA |
| `execution/ast_check.py` | MQL parse, 6 banned-operator scan, `canonical_form_set` eval + thin derivation |
| `execution/mongo.py` | NormExec (run MQL aggregate) + `equiv_rec` (≡_rec) |
| `execution/signature.py` | `world_signature` over canonicalized witness |
| `construct/migrate.py` | DM's deterministic document-aggregate migration (FK-derived embed/reference plan) |
| `workflow/engine.py` | `Workflow`: `agent` / `parallel` / `pipeline` primitives (concurrency + failure isolation) |
| `workflow/flows.py` | `run_phase_a` (per-db WP→SRA→SC*→DM) and `run_phase_b` (per-record 8-agent pipeline + feedback) |
| `cli.py` | Runtime assembly + `tend construct` |

## Run

```bash
# offline (no LLM, exercises the whole machine deterministically)
python -m tend construct --phase all --dbs financial --records 1 --stub --quiet

# live (DeepSeek per .env) — Phase A only
python -m tend construct --phase A --dbs financial

# all 11 dbs
python -m tend construct --phase all --dbs all --records 20

# full financial source workload + uncapped MongoDB export
python -m tend construct --phase all --dbs financial --records all --full-db
```

Everything for a run lands under `runs/<run_id>/`; by default `tend construct`
writes dataset assets under `runs/<run_id>/dataset/` (`mongodb_schema/`,
`mongodb_data/`, `agent_design_rationale/`, `test.json`, `TEND.json`,
`bird_db_catalog.json`). `--records all` resolves to the selected BIRD workload count
before Phase B starts and schedules source-full structural-schema-flex slots rather than
lower-tier composition padding. When sparse optional embeds are available, the source-full
planner prefers those cells because Phase A materializes them as MongoDB `__variants` that
live gold-lock can verify. `--full-db` disables the deterministic migration reference-table
cap, so large fact collections such as `financial.trans` are exported completely. The
explicit release-copy step writes `release/TEND-dataset/`.

## Logging & anomaly capture (for operators / Claude Code)

Triage starts at one file: `runs/<run_id>/anomalies.jsonl`. Every line is a structured
anomaly with `anomaly` (kind), `message`, the bound context (`db_id`/`record_id`/`agent`),
and — for LLM faults — a `transcript_ref` pointing at
`runs/<run_id>/llm/<agent>/<call_id>.md` plus a `diagnostics_ref` sidecar at
`runs/<run_id>/llm/<agent>/<call_id>.diagnostics.json`. The Markdown transcript is the
human/agent triage view; the diagnostics JSON preserves the full structured payload.
Prompt anomalies (malformed/oversize prompts) are captured too. `events.jsonl` is the full
event stream.

Anomaly kinds: `api_error`, `rate_limit`, `timeout`, `empty_response`, `truncated`,
`refusal`, `prompt_malformed`, `context_overflow`, `parse_error`, `schema_invalid`,
`contract_violation`, `exec_error`, `disabled_operator`, `gold_lock_failed`,
`gate_failed`, `migration_error`, `supply_exhausted`, `internal`.

## Progress

A live terminal tree (phase → db/records → agent tasks) with running/ok/fail/retry
counters, anomaly counts, and watched warning/retry/reject alert counts. Disabled with
`--quiet` (or off-TTY), which still writes `progress.jsonl` snapshots.

## The dynamic workflow

`Workflow` is the engine; the work-list is discovered at runtime (dbs from the source,
records from coverage slots). `run_phase_a` fans out one sub-agent chain per db in
parallel; `run_phase_b` pipelines records through the 8-agent chain with bounded feedback
loops (SC→SRA, MS gold-lock retry, RTV→NLP, etc.). Each `agent(...)` call dynamically
spawns one concurrency-limited sub-agent whose lifecycle is logged and shown in progress.

## Status / next

- **Done**: full pipeline runs stub end-to-end and live `financial` Phase A/B; deterministic
  migration can run capped for fast iteration or uncapped via `--full-db`; Phase B uses the
  census-driven coverage controller and emits validated NL-MQL records.
- **Next**: broaden live release-scale runs across all dbs and tune coverage/yield policy
  from the resulting logs.
