# QueryCraft TEND Demo

This directory contains QueryCraft, the Flask demonstration system for the real
TEND SAG solver. QueryCraft has been accepted to the VLDB demo track. The page
is one column and three steps — pick a database, ask in plain English, read the
aggregation pipeline and its result — with a collapsible section that shows what
a schema-less MongoDB collection actually looks like (inferred document shape,
dynamic-key maps, one real document).

The directory is code-only: copied demo data directories such as
`mongodb_data/`, `mongodb_schema/`, and `schemas/` are intentionally not stored
here. The demo-paper source in `../paper_demo/` is local-only and should not be
uploaded to GitHub.

## Data Source

By default the demo reads the formal release package from:

```text
release/tend-native-mongodb-v1/
```

Set `TEND_DEMO_DATASET_DIR` to point at another release-compatible dataset
root. The app uses `resolve_release_dataset_layout()` and expects:

```text
data/TEND.json
schema/mongodb_schema/<db_id>.json
mongodb_data/<db_id>.json
```

## Setup

Install the optional demo dependency:

```bash
uv pip install --python ./.venv/bin/python -e '.[demo]'
```

or, in a pip-enabled environment:

```bash
python -m pip install -e '.[demo]'
```

## Run

```bash
TEND_DEMO_PORT=5050 TEND_USE_EXISTING_MONGO_DBS=1 ./.venv/bin/python -m demonstration.app
```

Open:

```text
http://127.0.0.1:5050
```

Useful environment variables:

- `TEND_DEMO_DATASET_DIR`: alternate release-compatible dataset root.
- `TEND_DEMO_SOLVER_MODE`: default UI mode, either `stub` or `live`.
- `TEND_DEMO_SOLVE_TIMEOUT_S`: server-side request timeout; default `90`.
- `TEND_DEMO_DEBUG=1`: enables Flask debug mode only on loopback hosts.
- `TEND_DEMO_MAX_RETRIES`: forwarded to `TEND_MAX_RETRIES` for live mode.
- `TEND_USE_EXISTING_MONGO_DBS=1`: strongly recommended for the demo — see below.

## Presenting

Run with `TEND_USE_EXISTING_MONGO_DBS=1` against a MongoDB that already holds
the eleven release databases (each named after its `db_id`). Then:

- Schema browsing samples documents straight from MongoDB, so selecting
  `european_football_2` costs ~0.4 s instead of parsing a 2.2 GB witness file.
- Live-mode solving and execution never read the witness files at all.
- Stub mode still induces its grounding index offline from the witness file, so
  prefer a small database (`student_club`, `superhero`, `toxicology`) when
  demonstrating stub mode; `european_football_2` carries a 2.2 GB witness.

The pipeline's stage chips are coloured by operator family (filter, reshape,
aggregate, join, order) so the shape of a query reads from across the room, and
the MQL tab is editable — fix a stage by hand and press Run without re-invoking
the model. The UI is light by default for a lit room; the header toggle switches
to dark and the choice is remembered. All assets are local — no CDN font, icon,
or highlighter — so the demo behaves identically offline.

## Modes

- `stub`: default; runs through the SAG solver plumbing with the deterministic
  local LLM stub. This is for smoke tests and UI debugging.
- `live`: uses the configured OpenAI-compatible provider. Requires
  `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and the usual TEND model settings.
  The demo explicitly disables `TEND_LLM_STUB` in live mode so live requests
  cannot silently fall back to the local stub.

## HTTP surface

| Route | Purpose |
| --- | --- |
| `GET /api/health` | dataset labels, counts, policy defaults and bounds |
| `GET /api/databases` | per-database collection/question counts and witness size |
| `GET /api/examples/<db_id>` | benchmark questions (canonical + colloquial) |
| `GET /api/schema/<db_id>` | bounded document samples, inferred shapes, dynamic key maps |
| `POST /api/solve` | run the solver; optionally execute the prediction |
| `POST /api/execute` | execute an already-generated (or hand-edited) pipeline |

`POST /api/execute` takes `{database, mql, mode, limit}` and runs one bounded
read-only aggregation: banned operators are rejected before execution, the
pipeline is capped at `MAX_EXECUTION_ROWS` rows, and the parsed stages are
returned so the UI can re-render the pipeline the presenter just edited.
`GET /get_databases`, `GET /get_schema/<db_id>`, and `POST /query` remain as
backward-compatible aliases.

Only the question text and its record id ever cross into the solver — gold MQL
in the release records is never read by the demo.

## Verify

```bash
./.venv/bin/python -m pytest -q demonstration/tests/test_app.py
```

```bash
./.venv/bin/python -m ruff check demonstration/app.py demonstration/tests/test_app.py
```
