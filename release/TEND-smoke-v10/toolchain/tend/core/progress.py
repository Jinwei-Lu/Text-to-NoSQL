"""Rich terminal progress UI."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

from tend.config import RUN_DIR

_QUIET = os.getenv("TEND_QUIET", "0") == "1"
_progress: Progress | None = None
_outer_task = None
_inner_tasks: dict[str, Any] = {}
_run_dir: Path | None = None


def init(*, run_dir: Path | None = None) -> None:
    global _progress, _run_dir
    _run_dir = run_dir or RUN_DIR
    _run_dir.mkdir(parents=True, exist_ok=True)
    if _QUIET or not sys.stdout.isatty():
        return
    _progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
    )
    _progress.start()


def outer(total: int) -> Any:
    global _outer_task
    if _progress is None:
        return None
    _outer_task = _progress.add_task("Overall", total=total)
    return _outer_task


def inner(db_id: str, total: int) -> Any:
    if _progress is None:
        return None
    task = _progress.add_task(db_id, total=total)
    _inner_tasks[db_id] = task
    return task


def advance(task: Any, n: int = 1) -> None:
    if _progress is not None and task is not None:
        _progress.advance(task, n)


def status(record_id: int | str, agent: str, stage: str, attempt: int = 1) -> None:
    line = f"{record_id} · agent={agent} stage={stage} attempt={attempt}"
    if _progress is not None:
        _progress.console.print(line)
    elif not _QUIET:
        print(json.dumps({"status": line}), flush=True)
    if _run_dir and (time.time() % 5 < 0.2):
        snap = {"ts": time.time(), "tasks": list(_inner_tasks.keys())}
        (_run_dir / "progress.json").write_text(json.dumps(snap), encoding="utf-8")


def close() -> None:
    global _progress
    if _progress is not None:
        _progress.stop()
        _progress = None
