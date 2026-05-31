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
| `observability/logging.py` | File-first JSONL logging: `events.jsonl`, `anomalies.jsonl`, per-call `llm/<agent>/<id>.json` transcripts; anomaly subscriber callbacks |
| `observability/progress.py` | Live `rich` phase→group→task tree + counters + anomaly ticker |
| `llm/client.py` | Async OpenAI-compatible client: transport retries, JSON/schema repair loop, per-call transcripts, typed anomaly classification, stub mode |
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
```

Everything for a run lands under `runs/<run_id>/`; dataset assets under
`release/TEND-dataset/` (`mongodb_schema/`, `mongodb_data/`, `agent_design_rationale/`,
`test.json`, `TEND.json`, `bird_db_catalog.json`).

## Logging & anomaly capture (for operators / Claude Code)

Triage starts at one file: `runs/<run_id>/anomalies.jsonl`. Every line is a structured
anomaly with `anomaly` (kind), `message`, the bound context (`db_id`/`record_id`/`agent`),
and — for LLM faults — a `transcript_ref` pointing at `runs/<run_id>/llm/<agent>/<id>.json`
(the **exact prompt + every attempt**). Prompt anomalies (malformed/oversize prompts) are
captured too. `events.jsonl` is the full event stream.

Anomaly kinds: `api_error`, `rate_limit`, `timeout`, `empty_response`, `truncated`,
`refusal`, `prompt_malformed`, `context_overflow`, `parse_error`, `schema_invalid`,
`contract_violation`, `exec_error`, `disabled_operator`, `gold_lock_failed`,
`gate_failed`, `migration_error`, `supply_exhausted`, `internal`.

## Progress

A live terminal tree (phase → db/records → agent tasks) with running/ok/fail/retry
counters and a rolling anomaly ticker. Disabled with `--quiet` (or off-TTY), which falls
back to structured console log lines.

## The dynamic workflow

`Workflow` is the engine; the work-list is discovered at runtime (dbs from the source,
records from coverage slots). `run_phase_a` fans out one sub-agent chain per db in
parallel; `run_phase_b` pipelines records through the 8-agent chain with bounded feedback
loops (SC→SRA, MS gold-lock retry, RTV→NLP, etc.). Each `agent(...)` call dynamically
spawns one concurrency-limited sub-agent whose lifecycle is logged and shown in progress.

## Status / next

- **Done**: full pipeline runs stub end-to-end and live Phase A; deterministic migration
  makes the canonical `financial/1001` anchor MQL **execute on real MongoDB** (4500 docs
  preserved, 682 loan-present) — the previously "pending" DAR verification.
- **Next**: live Phase B gold-locking (MS reference-oracle anchoring), census-driven
  coverage controller, multi-level embed nesting, and the review fixes (anchor SSoT/status,
  `optional_embed` schema_flex, dual-axis difficulty).
