"""Shared fixtures for orchestra Phase B synth tests."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from tend.config import SPIDER_DATA_ROOT


def build_orchestra_witness() -> dict[str, Any]:
    db_path = SPIDER_DATA_ROOT / "database" / "orchestra" / "orchestra.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM conductor ORDER BY Conductor_ID")
    conductors = [dict(row) for row in cur.fetchall()]
    for conductor in conductors:
        cid = conductor["Conductor_ID"]
        conductor["_id"] = cid
        del conductor["Conductor_ID"]
        cur.execute(
            "SELECT * FROM orchestra WHERE Conductor_ID=? ORDER BY Orchestra_ID",
            (cid,),
        )
        orchestras: list[dict[str, Any]] = []
        for orchestra in cur.fetchall():
            od = dict(orchestra)
            oid = od["Orchestra_ID"]
            del od["Orchestra_ID"]
            del od["Conductor_ID"]
            cur.execute(
                "SELECT * FROM performance WHERE Orchestra_ID=? ORDER BY Performance_ID",
                (oid,),
            )
            performances: list[dict[str, Any]] = []
            for perf in cur.fetchall():
                pd = dict(perf)
                pid = pd["Performance_ID"]
                del pd["Performance_ID"]
                del pd["Orchestra_ID"]
                cur.execute("SELECT Attendance FROM show WHERE Performance_ID=?", (pid,))
                show_row = cur.fetchone()
                if show_row is not None:
                    pd["Attendance"] = show_row["Attendance"]
                performances.append(pd)
            od["performance"] = performances
            orchestras.append(od)
        conductor["orchestra"] = orchestras
    conn.close()
    return {"conductor": conductors}


@pytest.fixture(scope="module")
def orchestra_witness():
    return build_orchestra_witness()


@pytest.fixture(scope="module")
def orchestra_schema():
    return {"collections": ["conductor"], "root_collection": "conductor"}
