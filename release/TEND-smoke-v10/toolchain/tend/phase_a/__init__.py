"""Phase A (DataWorld): WP → SRA → SC → DM."""

from tend.phase_a.catalog import FIXTURE_DB_IDS, select_spider_dbs
from tend.phase_a.detectors import scan_phenomena
from tend.phase_a.dm import migrate
from tend.phase_a.sc import review_schema
from tend.phase_a.sra import design_schema
from tend.phase_a.wp import profile_workload

__all__ = [
    "FIXTURE_DB_IDS",
    "design_schema",
    "migrate",
    "profile_workload",
    "review_schema",
    "scan_phenomena",
    "select_spider_dbs",
]
