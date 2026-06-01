"""Live terminal progress — so a human catches stalls and failures *as they happen*.

A single :class:`ProgressReporter` renders a phase -> group -> task tree with status
icons, a global counters line, and a rolling anomaly ticker fed directly from the
logger's anomaly callbacks. The workflow engine calls the lifecycle hooks
(``start_task``/``finish_task``/...); rendering is decoupled and refreshes on a timer.

Degrades gracefully: when disabled (``--quiet``) or not attached to a TTY, the hooks
still accumulate state for the end-of-run :meth:`summary`, but no Live UI is drawn.
"""
from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from .logging import RunLogger

_ICON = {
    "pending": ("○", "grey50"),
    "running": ("◐", "cyan"),
    "ok": ("●", "green"),
    "fail": ("✗", "red"),
    "retry": ("↻", "yellow"),
}


@dataclass
class _Task:
    label: str
    group: str
    status: str = "running"
    detail: str = ""
    anomaly: str | None = None
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None

    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started


@dataclass
class _Group:
    label: str
    phase: str
    total: int | None = None
    order: int = 0


class ProgressReporter:
    """Thread-safe progress aggregator + optional rich Live renderer."""

    def __init__(self, run_id: str, logger: RunLogger, *, enabled: bool = True) -> None:
        self.run_id = run_id
        self._log = logger
        self._console = Console(stderr=True)
        self._enabled = enabled and self._console.is_terminal and not self._console.is_dumb_terminal
        self._lock = threading.RLock()
        self._phase = "—"
        self._groups: dict[str, _Group] = {}
        self._tasks: dict[str, _Task] = {}
        self._anoms: deque[dict[str, Any]] = deque(maxlen=8)
        self._counts = {"started": 0, "ok": 0, "fail": 0, "retry": 0}
        self._anom_by_kind: dict[str, int] = {}
        self._t0 = time.monotonic()
        self._live: Live | None = None
        # surface anomalies live, regardless of which agent/task raised them
        logger.subscribe_anomaly(self._on_anomaly)

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #
    def __enter__(self) -> "ProgressReporter":
        if self._enabled:
            self._live = Live(self, console=self._console, refresh_per_second=8,
                              transient=False)
            self._live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._live is not None:
            self._live.update(self)        # final frame
            self._live.stop()
            self._live = None
        if not self._enabled:
            self._console.print(self._render_summary_line())

    def phase(self, name: str) -> None:
        with self._lock:
            self._phase = name

    def add_group(self, group_id: str, label: str, *, phase: str | None = None,
                  total: int | None = None) -> None:
        with self._lock:
            order = self._groups[group_id].order if group_id in self._groups else len(self._groups)
            self._groups[group_id] = _Group(label=label, phase=phase or self._phase,
                                            total=total, order=order)

    # ----------------------------------------------------------------- #
    # task hooks (called by the workflow engine)
    # ----------------------------------------------------------------- #
    def start_task(self, task_id: str, label: str, *, group: str = "", detail: str = "") -> None:
        with self._lock:
            if group and group not in self._groups:
                self.add_group(group, group)
            self._tasks[task_id] = _Task(label=label, group=group, detail=detail)
            self._counts["started"] += 1
            self._ensure_group_uses_task_units(group)

    def update_task(self, task_id: str, *, detail: str | None = None,
                    status: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            if detail is not None:
                t.detail = detail
            if status is not None:
                t.status = status

    def retry_task(self, task_id: str, *, detail: str = "") -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.status = "retry"
                t.detail = detail or t.detail
                self._counts["retry"] += 1

    def finish_task(self, task_id: str, *, ok: bool = True, anomaly: str | None = None,
                    detail: str | None = None) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            was_terminal = t.status in ("ok", "fail")
            t.status = "ok" if ok else "fail"
            t.ended = time.monotonic()
            t.anomaly = anomaly
            if detail is not None:
                t.detail = detail
            if not was_terminal:
                self._counts["ok" if ok else "fail"] += 1

    def _on_anomaly(self, record: dict[str, Any]) -> None:
        with self._lock:
            kind = record.get("anomaly", "internal")
            self._anom_by_kind[kind] = self._anom_by_kind.get(kind, 0) + 1
            self._anoms.append(record)

    def _ensure_group_uses_task_units(self, group: str) -> None:
        if not group:
            return
        g = self._groups.get(group)
        if g is None or g.total is None:
            return
        task_count = sum(1 for task in self._tasks.values() if task.group == group)
        if task_count > g.total:
            g.total = None

    # ----------------------------------------------------------------- #
    # rendering
    # ----------------------------------------------------------------- #
    def __rich__(self) -> Group:
        with self._lock:
            return Group(self._render_tree(), self._render_footer())

    _MAX_GROUPS = 20  # global cap on rendered groups; active/failing shown first

    def _render_tree(self) -> Tree:
        elapsed = time.monotonic() - self._t0
        root = Tree(Text.assemble(
            ("TEND ", "bold magenta"), (f"{self.run_id}  ", "bold"),
            (f"phase={self._phase}  ", "cyan"), (f"{elapsed:6.1f}s", "grey62"),
        ))
        by_phase: dict[str, list[tuple[str, _Group]]] = {}
        for gid, g in sorted(self._groups.items(), key=lambda kv: kv[1].order):
            by_phase.setdefault(g.phase, []).append((gid, g))

        # Collect all (phase, gid, group) triples and classify them so that
        # groups with running or failed tasks rank above fully-done groups.
        all_groups: list[tuple[str, str, _Group]] = []
        for phase, groups in by_phase.items():
            for gid, g in groups:
                all_groups.append((phase, gid, g))

        def _group_priority(item: tuple[str, str, _Group]) -> int:
            phase, gid, _ = item
            tasks = [t for t in self._tasks.values() if t.group == gid]
            if any(t.status == "fail" for t in tasks):
                return 0   # highest priority: has failures
            if any(t.status in ("running", "retry") for t in tasks):
                return 1   # active
            return 2       # fully done / pending

        all_groups.sort(key=_group_priority)

        visible = all_groups[: self._MAX_GROUPS]
        hidden = all_groups[self._MAX_GROUPS :]

        # Render visible groups, preserving the phase-grouping structure.
        rendered_phases: dict[str, Any] = {}
        for phase, gid, g in visible:
            if phase not in rendered_phases:
                rendered_phases[phase] = root.add(Text(f"Phase {phase}", style="bold yellow"))
            pnode = rendered_phases[phase]
            tasks = [t for t in self._tasks.values() if t.group == gid]
            done = sum(1 for t in tasks if t.status in ("ok", "fail"))
            total = g.total if g.total is not None else len(tasks)
            total = max(total, done)
            gnode = pnode.add(Text.assemble(
                (f"{g.label} ", "bold"),
                (f"[{done}/{total}]", "grey62"),
            ))
            # show running + failed tasks first, then others; skip finished-ok
            non_ok = [t for t in tasks if t.status != "ok"]
            non_ok.sort(key=lambda t: 0 if t.status in ("fail", "running", "retry") else 1)
            for t in non_ok[:12]:
                icon, color = _ICON.get(t.status, ("?", "white"))
                line = Text.assemble((f"{icon} ", color), (t.label, ""))
                if t.detail:
                    line.append(f"  {t.detail}", style="grey62")
                if t.status in ("running", "retry"):
                    line.append(f"  {t.elapsed():.1f}s", style="grey42")
                if t.anomaly:
                    line.append(f"  !{t.anomaly}", style="red")
                gnode.add(line)

        if hidden:
            done_count = sum(
                1 for _, gid, _ in hidden
                if all(t.status in ("ok", "fail") for t in self._tasks.values() if t.group == gid)
            )
            root.add(Text(
                f"… +{len(hidden)} more groups ({done_count} done)",
                style="grey50",
            ))

        return root

    def _render_footer(self) -> Panel:
        c = self._counts
        running = max(0, c["started"] - c["ok"] - c["fail"])
        counters = Text.assemble(
            ("running ", "cyan"), (f"{running}  ", "bold cyan"),
            ("ok ", "green"), (f"{c['ok']}  ", "bold green"),
            ("fail ", "red"), (f"{c['fail']}  ", "bold red"),
            ("retry ", "yellow"), (f"{c['retry']}  ", "bold yellow"),
        )
        if self._anom_by_kind:
            counters.append("│ anomalies ", style="grey50")
            for kind, n in sorted(self._anom_by_kind.items(), key=lambda kv: -kv[1]):
                counters.append(f"{kind}={n} ", style="red")
        tbl = Table.grid(padding=(0, 1))
        tbl.add_row(counters)
        for a in list(self._anoms)[-5:]:
            loc = "/".join(str(a[k]) for k in ("db_id", "record_id", "agent") if a.get(k))
            tbl.add_row(Text.assemble(
                ("⚠ ", "red"), (f"{a.get('anomaly','?')} ", "bold red"),
                (f"{loc}  ", "grey62"), (str(a.get("message", ""))[:80], "grey78"),
            ))
        return Panel(tbl, title="status", border_style="grey37", padding=(0, 1))

    def _render_summary_line(self) -> Text:
        c = self._counts
        return Text.assemble(
            (f"[{self.run_id}] ", "bold"),
            (f"ok={c['ok']} fail={c['fail']} retry={c['retry']} ", ""),
            (f"anomalies={sum(self._anom_by_kind.values())}",
             "red" if self._anom_by_kind else "green"),
        )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "elapsed_s": round(time.monotonic() - self._t0, 1),
                "tasks": dict(self._counts),
                "anomalies_by_kind": dict(self._anom_by_kind),
                "anomaly_total": sum(self._anom_by_kind.values()),
            }


def make_reporter(run_id: str, logger: RunLogger, *, enabled: bool = True) -> ProgressReporter:
    return ProgressReporter(run_id, logger, enabled=enabled and sys.stderr.isatty())
