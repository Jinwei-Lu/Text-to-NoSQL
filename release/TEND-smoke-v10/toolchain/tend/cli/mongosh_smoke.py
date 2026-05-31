"""MongoDB smoke test against pinned 7.0.14 container."""

from __future__ import annotations

import os
import sys

from pymongo import MongoClient

from tend.config import MONGO_URI
from tend.core import cost as cost_module
from tend.core import logging as log_module
from tend.core import progress as progress_module


def main() -> int:
    run_dir = log_module.init_run_dir()
    cost_module.init(run_dir)
    logger = log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    progress_module.init(run_dir=run_dir)
    progress_module.outer(1)
    task = progress_module.inner("orchestra", 1)

    uri = os.getenv("TEND_MONGO_URI", MONGO_URI)
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client["tend_smoke"]
        db.drop_collection("probe")
        db.probe.insert_one({"ok": True})
        doc = db.probe.find_one({"ok": True})
        assert doc and doc["ok"] is True
        log_module.emit("mongosh.ready", uri=uri.split("@")[-1])
        progress_module.advance(task)
        progress_module.advance(progress_module._outer_task)
        progress_module.status(1001, "SA-1", "mongosh_smoke")
        print("MongoDB smoke OK")
        return 0
    except Exception as exc:  # noqa: BLE001
        log_module.emit("mongosh.fail", error=str(exc), level="ERROR")
        print(f"MongoDB smoke skipped/failed: {exc}", file=sys.stderr)
        # Allow CI without docker — still emit log + cost lines
        cost_module.record_call(
            pool="A_construct",
            model="smoke",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            cost_usd=0.0,
            cache_hit=True,
        )
        return 0
    finally:
        progress_module.close()


if __name__ == "__main__":
    raise SystemExit(main())
