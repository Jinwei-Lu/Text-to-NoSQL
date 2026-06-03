from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tend.errors import MigrationError
from tend.source import BirdSource, ColumnSchema, DbSchema, ForeignKey
from tend.source.bird import BIRD_DBS


class _SqliteFixtureSource:
    def __init__(self, conn: sqlite3.Connection, schema: DbSchema) -> None:
        self._conn = conn
        self._schema = schema

    def schema(self, db_id: str) -> DbSchema:
        assert db_id == self._schema.db_id
        return self._schema

    def connection(self, db_id: str) -> sqlite3.Connection:
        assert db_id == self._schema.db_id
        return self._conn


def _bird_source() -> BirdSource:
    root = Path(__file__).resolve().parents[1] / "minidev" / "MINIDEV"
    return BirdSource(root)


def _fixture_schema(
    db_id: str,
    *,
    tables: list[str],
    columns: list[tuple[str, str, str]],
    primary_keys: dict[str, list[str]],
    foreign_keys: list[ForeignKey] | None = None,
) -> DbSchema:
    return DbSchema(
        db_id=db_id,
        domain="native-fixture",
        tables=tables,
        columns=[ColumnSchema(table, name, col_type) for table, name, col_type in columns],
        foreign_keys=foreign_keys or [],
        primary_keys=primary_keys,
        sqlite_path=Path(":memory:"),
    )


def test_native_design_registry_covers_each_bird_minidev_db():
    from tend.construct.native_designs.registry import NATIVE_DESIGN_MODULES

    assert set(NATIVE_DESIGN_MODULES) == set(BIRD_DBS)
    for db_id, module_ref in NATIVE_DESIGN_MODULES.items():
        assert module_ref == f"tend.construct.native_designs.{db_id}"


def test_native_designs_build_verified_database_specific_recipes():
    from tend.construct.native_designs.registry import build_native_recipe_for_db
    from tend.construct.native_recipe import NATIVE_TRANSFORMS, verify_native_recipe

    source = _bird_source()

    for db_id in BIRD_DBS:
        recipe = build_native_recipe_for_db(source, db_id)
        result = verify_native_recipe(recipe, source.schema(db_id))
        transforms = [
            transform
            for collection in recipe.collections.values()
            for transform in collection.transforms
        ]

        assert recipe.db_id == db_id
        assert recipe.recipe_version == 1
        assert result.ok, f"{db_id}: {result.errors}"
        assert any(transform.type == "dynamic_key_object" for transform in transforms)
        assert sum(transform.type in NATIVE_TRANSFORMS for transform in transforms) >= 2
        assert any(
            f"tend.construct.native_designs.{db_id}" in transform.raw.get("design_module", "")
            for transform in transforms
        )


def test_native_design_registry_fails_closed_for_unknown_db():
    from tend.construct.native_designs.registry import build_native_recipe_for_db

    with pytest.raises(MigrationError, match="no native design module"):
        build_native_recipe_for_db(_bird_source(), "not_a_bird_db")


def test_financial_bank_account_tags_cover_source_local_and_derived_rules():
    from tend.construct.native_designs.financial import build_native_recipe
    from tend.construct.native_executor import execute_native_recipe

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table account (
          account_id integer primary key,
          district_id integer,
          frequency text,
          date text
        );
        create table loan (
          loan_id integer primary key,
          account_id integer,
          date text,
          amount real,
          duration integer,
          payments real,
          status text
        );
        create table district (
          district_id integer primary key,
          A2 text, A3 text, A4 integer, A5 integer, A6 integer, A7 integer,
          A8 integer, A9 integer, A10 real, A11 real, A12 real, A13 real,
          A14 integer, A15 integer, A16 integer
        );
        create table client (
          client_id integer primary key,
          gender text,
          birth_date text
        );
        create table card (
          card_id integer primary key,
          account_id integer,
          type text,
          issued text
        );
        create table disp (
          disp_id integer primary key,
          client_id integer,
          account_id integer,
          type text
        );
        create table trans (
          trans_id integer primary key,
          account_id integer,
          date text,
          type text,
          operation text,
          amount real,
          balance real,
          k_symbol text,
          bank text,
          account integer
        );
        """
    )
    conn.executemany(
        "insert into account values (?, ?, ?, ?)",
        [
            (1, 10, "POPLATEK MESICNE", "1993-01-01"),
            (2, 11, "POPLATEK TYDNE", "1993-01-02"),
        ],
    )
    conn.executemany(
        "insert into loan values (?, ?, ?, ?, ?, ?, ?)",
        [(100, 2, "1994-01-01", 5000.0, 12, 420.0, "B")],
    )
    conn.executemany(
        "insert into district values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (10, "Region A", "R", 1, 1, 1, 1, 1, 1, 1.0, 12000.0, 1.0, 1.0, 1, 1, 1),
            (11, "Region B", "R", 1, 1, 1, 1, 1, 1, 1.0, 13000.0, 1.0, 1.0, 1, 1, 1),
        ],
    )
    conn.executemany(
        "insert into trans values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "1993-01-03", "PRIJEM", "VKLAD", 100.0, 100.0, None, None, None),
            (2, 2, "1993-01-04", "VYDAJ", "VYBER", 20.0, 80.0, None, None, None),
        ],
    )
    conn.commit()
    source = _SqliteFixtureSource(
        conn,
        _fixture_schema(
            "financial",
            tables=["account", "loan", "district", "client", "card", "disp", "trans"],
            columns=[
                ("account", "account_id", "integer"),
                ("account", "district_id", "integer"),
                ("account", "frequency", "text"),
                ("account", "date", "text"),
                ("loan", "loan_id", "integer"),
                ("loan", "account_id", "integer"),
                ("loan", "date", "text"),
                ("loan", "amount", "real"),
                ("loan", "duration", "integer"),
                ("loan", "payments", "real"),
                ("loan", "status", "text"),
                ("district", "district_id", "integer"),
                ("district", "A2", "text"),
                ("district", "A3", "text"),
                ("district", "A4", "integer"),
                ("district", "A5", "integer"),
                ("district", "A6", "integer"),
                ("district", "A7", "integer"),
                ("district", "A8", "integer"),
                ("district", "A9", "integer"),
                ("district", "A10", "real"),
                ("district", "A11", "real"),
                ("district", "A12", "real"),
                ("district", "A13", "real"),
                ("district", "A14", "integer"),
                ("district", "A15", "integer"),
                ("district", "A16", "integer"),
                ("client", "client_id", "integer"),
                ("client", "gender", "text"),
                ("client", "birth_date", "text"),
                ("card", "card_id", "integer"),
                ("card", "account_id", "integer"),
                ("card", "type", "text"),
                ("card", "issued", "text"),
                ("disp", "disp_id", "integer"),
                ("disp", "client_id", "integer"),
                ("disp", "account_id", "integer"),
                ("disp", "type", "text"),
                ("trans", "trans_id", "integer"),
                ("trans", "account_id", "integer"),
                ("trans", "date", "text"),
                ("trans", "type", "text"),
                ("trans", "operation", "text"),
                ("trans", "amount", "real"),
                ("trans", "balance", "real"),
                ("trans", "k_symbol", "text"),
                ("trans", "bank", "text"),
                ("trans", "account", "integer"),
            ],
            primary_keys={
                "account": ["account_id"],
                "loan": ["loan_id"],
                "district": ["district_id"],
                "client": ["client_id"],
                "card": ["card_id"],
                "disp": ["disp_id"],
                "trans": ["trans_id"],
            },
            foreign_keys=[
                ForeignKey("loan", "account_id", "account", "account_id"),
                ForeignKey("trans", "account_id", "account", "account_id"),
                ForeignKey("account", "district_id", "district", "district_id"),
            ],
        ),
    )

    result = execute_native_recipe(source, "financial", build_native_recipe(source, "financial"))

    tag_counts = Counter(
        tag
        for doc in result.data["bank_account_activity"]
        for tag in doc.get("account_tags", [])
    )
    declared_tags = {"loan_default_watch", "monthly_issuance", "regional_salary_context"}
    assert {tag for tag in declared_tags if tag_counts[tag] > 0} == declared_tags
    assert result.provenance["bank_account_activity.account_risk_tags"]["source_columns"] == [
        "account.account_id",
        "account.district_id",
        "account.frequency",
        "district.A11",
        "district.district_id",
        "loan.account_id",
        "loan.status",
    ]


def test_student_club_declared_event_and_member_tags_materialize():
    from tend.construct.native_designs.student_club import build_native_recipe
    from tend.construct.native_executor import execute_native_recipe

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table event (
          event_id integer primary key,
          event_name text,
          event_date text,
          type text,
          notes text,
          location text,
          status text
        );
        create table budget (
          budget_id integer primary key,
          category text,
          spent real,
          remaining real,
          amount real,
          event_status text,
          link_to_event integer
        );
        create table attendance (link_to_event integer, link_to_member integer);
        create table member (
          member_id integer primary key,
          first_name text,
          last_name text,
          email text,
          position text,
          t_shirt_size text,
          phone text,
          zip text,
          link_to_major integer
        );
        create table major (
          major_id integer primary key,
          major_name text,
          department text,
          college text
        );
        create table expense (
          expense_id integer primary key,
          expense_description text,
          expense_date text,
          cost real,
          approved text,
          link_to_member integer,
          link_to_budget integer
        );
        create table income (
          income_id integer primary key,
          date_received text,
          amount real,
          source text,
          notes text,
          link_to_member integer
        );
        """
    )
    conn.execute(
        "insert into event values (?, ?, ?, ?, ?, ?, ?)",
        (1, "Kickoff", "2024-01-01", "Meeting", None, "Hall", "Closed"),
    )
    conn.execute(
        "insert into budget values (?, ?, ?, ?, ?, ?, ?)",
        (10, "Food", 50.0, 25.0, 75.0, "Closed", 1),
    )
    conn.execute(
        "insert into member values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (20, "Ada", "Lovelace", "ada@example.test", "President", "Medium", None, None, 30),
    )
    conn.execute(
        "insert into major values (?, ?, ?, ?)",
        (30, "Computer Science", "CS", "Engineering"),
    )
    conn.execute(
        "insert into expense values (?, ?, ?, ?, ?, ?, ?)",
        (40, "Pizza", "2024-01-02", 50.0, "Yes", 20, 10),
    )
    conn.commit()
    source = _SqliteFixtureSource(
        conn,
        _fixture_schema(
            "student_club",
            tables=["event", "budget", "attendance", "member", "major", "expense", "income"],
            columns=[
                ("event", "event_id", "integer"),
                ("event", "event_name", "text"),
                ("event", "event_date", "text"),
                ("event", "type", "text"),
                ("event", "notes", "text"),
                ("event", "location", "text"),
                ("event", "status", "text"),
                ("budget", "budget_id", "integer"),
                ("budget", "category", "text"),
                ("budget", "spent", "real"),
                ("budget", "remaining", "real"),
                ("budget", "amount", "real"),
                ("budget", "event_status", "text"),
                ("budget", "link_to_event", "integer"),
                ("attendance", "link_to_event", "integer"),
                ("attendance", "link_to_member", "integer"),
                ("member", "member_id", "integer"),
                ("member", "first_name", "text"),
                ("member", "last_name", "text"),
                ("member", "email", "text"),
                ("member", "position", "text"),
                ("member", "t_shirt_size", "text"),
                ("member", "phone", "text"),
                ("member", "zip", "text"),
                ("member", "link_to_major", "integer"),
                ("major", "major_id", "integer"),
                ("major", "major_name", "text"),
                ("major", "department", "text"),
                ("major", "college", "text"),
                ("expense", "expense_id", "integer"),
                ("expense", "expense_description", "text"),
                ("expense", "expense_date", "text"),
                ("expense", "cost", "real"),
                ("expense", "approved", "text"),
                ("expense", "link_to_member", "integer"),
                ("expense", "link_to_budget", "integer"),
                ("income", "income_id", "integer"),
                ("income", "date_received", "text"),
                ("income", "amount", "real"),
                ("income", "source", "text"),
                ("income", "notes", "text"),
                ("income", "link_to_member", "integer"),
            ],
            primary_keys={
                "event": ["event_id"],
                "budget": ["budget_id"],
                "member": ["member_id"],
                "major": ["major_id"],
                "expense": ["expense_id"],
                "income": ["income_id"],
            },
        ),
    )

    result = execute_native_recipe(source, "student_club", build_native_recipe(source, "student_club"))

    event_tags = set(result.data["club_event_plans"][0]["event_tags"])
    member_tags = set(result.data["club_member_ledgers"][0]["member_tags"])
    assert {"meeting", "completed", "has_location"} <= event_tags
    assert {"medium_shirt", "club_officer", "major_known"} <= member_tags


def test_california_schools_design_materializes_frpm_snapshot_stream():
    from tend.construct.native_designs.california_schools import build_native_recipe
    from tend.construct.native_executor import execute_native_recipe

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        create table schools (
          CDSCode text primary key,
          NCESDist text,
          NCESSchool text,
          StatusType text,
          County text,
          District text,
          School text,
          Street text,
          StreetAbr text,
          City text,
          Zip text,
          State text,
          MailStreet text,
          MailStrAbr text,
          MailCity text,
          MailZip text,
          MailState text,
          Phone text,
          Ext text,
          Website text,
          OpenDate text,
          ClosedDate text,
          Charter integer,
          CharterNum text,
          FundingType text,
          DOC text,
          DOCType text,
          SOC text,
          SOCType text,
          EdOpsCode text,
          EdOpsName text,
          EILCode text,
          EILName text,
          GSoffered text,
          GSserved text,
          Virtual text,
          Magnet integer,
          Latitude real,
          Longitude real,
          AdmFName1 text,
          AdmLName1 text,
          AdmEmail1 text,
          AdmFName2 text,
          AdmLName2 text,
          AdmEmail2 text,
          AdmFName3 text,
          AdmLName3 text,
          AdmEmail3 text,
          LastUpdate text
        );
        create table frpm (
          CDSCode text,
          "Academic Year" text,
          "County Code" text,
          "District Code" text,
          "School Code" text,
          "County Name" text,
          "District Name" text,
          "School Name" text,
          "District Type" text,
          "School Type" text,
          "Educational Option Type" text,
          "NSLP Provision Status" text,
          "Charter School (Y/N)" text,
          "Charter School Number" text,
          "Charter Funding Type" text,
          IRC text,
          "Low Grade" text,
          "High Grade" text,
          "Enrollment (K-12)" integer,
          "Free Meal Count (K-12)" integer,
          "Percent (%) Eligible Free (K-12)" real,
          "FRPM Count (K-12)" integer,
          "Percent (%) Eligible FRPM (K-12)" real,
          "Enrollment (Ages 5-17)" integer,
          "Free Meal Count (Ages 5-17)" integer,
          "Percent (%) Eligible Free (Ages 5-17)" real,
          "FRPM Count (Ages 5-17)" integer,
          "Percent (%) Eligible FRPM (Ages 5-17)" real,
          "2013-14 CALPADS Fall 1 Certification Status" text
        );
        create table satscores (
          cds text,
          rtype text,
          sname text,
          dname text,
          cname text,
          enroll12 integer,
          NumTstTakr integer,
          AvgScrRead integer,
          AvgScrMath integer,
          AvgScrWrite integer,
          NumGE1500 integer
        );
        """
    )
    conn.execute(
        "insert into schools values "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "001",
            None,
            None,
            "Active",
            "County",
            "District",
            "School",
            None,
            None,
            "City",
            None,
            "CA",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "2020-01-01",
            None,
            1,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "Virtual",
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.execute(
        "insert into frpm values "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "001",
            "2014-2015",
            "01",
            "001",
            "0001",
            "County",
            "District",
            "School",
            "Unified",
            "Elementary",
            "Traditional",
            "Provision",
            "Y",
            None,
            None,
            "Y",
            "K",
            "5",
            100,
            30,
            30.0,
            40,
            40.0,
            90,
            20,
            22.0,
            35,
            38.0,
            "Certified",
        ),
    )
    conn.commit()
    columns: list[tuple[str, str, str]] = []
    for table in ("schools", "frpm", "satscores"):
        for _, name, col_type, *_ in conn.execute(f"pragma table_info({table})"):
            columns.append((table, str(name), str(col_type or "text")))
    source = _SqliteFixtureSource(
        conn,
        _fixture_schema(
            "california_schools",
            tables=["schools", "frpm", "satscores"],
            columns=columns,
            primary_keys={"schools": ["CDSCode"]},
        ),
    )

    result = execute_native_recipe(
        source,
        "california_schools",
        build_native_recipe(source, "california_schools"),
    )

    feature_types = {feature.type for feature in result.manifest.features}
    assert "nested_event_stream" in feature_types
    assert result.data["school_profiles"][0]["frpm_eligibility_events"] == [
        {
            "event_type": "2014-2015",
            "event_time": "2014-2015",
            "district_name": "District",
            "school_type": "Elementary",
            "enrollment_k12": 100,
            "free_meal_count_k12": 30,
            "frpm_pct_k12": 40.0,
        }
    ]
