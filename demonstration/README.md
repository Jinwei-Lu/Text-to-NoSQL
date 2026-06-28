# QueryCraft TEND Demo

This directory contains QueryCraft, the Flask demonstration system for the real
TEND SAG solver. QueryCraft has been accepted to the VLDB demo track. It lets a
user select a MongoDB demo database, browse nested schemas, ask a natural
language question, inspect the generated aggregation pipeline, optionally
execute it, and review previous attempts through a local history panel.

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
TEND_DEMO_PORT=5050 ./.venv/bin/python -c "from demonstration.app import app; app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)"
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

## Modes

- `stub`: default; runs through the SAG solver plumbing with the deterministic
  local LLM stub. This is for smoke tests and UI debugging.
- `live`: uses the configured OpenAI-compatible provider. Requires
  `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and the usual TEND model settings.
  The demo explicitly disables `TEND_LLM_STUB` in live mode so live requests
  cannot silently fall back to the local stub.

Checking "Execute against MongoDB when available" loads the selected release
witness data into the configured MongoDB working database and returns a bounded
read-only probe summary.

## Verify

```bash
./.venv/bin/python -m pytest -q demonstration/tests/test_app.py
./.venv/bin/python -m ruff check demonstration/app.py demonstration/tests/test_app.py
```
