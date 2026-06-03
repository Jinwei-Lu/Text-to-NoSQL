from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_audit import audit_database_structure, validate_structure_gate
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest
from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__
MAX_MONTH_BUCKETS_PER_ACCOUNT = 12
TRANSACTION_SAMPLE_PER_MONTH = 2


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent Czech banking accounts with monthly transaction buckets, "
            "account event streams, and typed account/client/loan/card facets."
        ),
        collections=[
            collection(
                "bank_account_activity",
                purpose="Account documents with dynamic monthly cash-flow buckets.",
                source_tables=["account", "trans", "loan", "district"],
                transforms=[
                    transform(
                        "transaction_totals_by_month",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="account",
                        child_table="trans",
                        join=join("account.account_id", "trans.account_id"),
                        target_field="transaction_totals_by_month",
                        key=expr("substr(trans.date, 1, 7)", "trans.date"),
                        values={
                            "credits": expr(
                                "sum(trans.amount where trans.type == 'PRIJEM')",
                                "trans.amount",
                                "trans.type",
                            ),
                            "withdrawals": expr(
                                "sum(trans.amount where trans.type == 'VYDAJ')",
                                "trans.amount",
                                "trans.type",
                            ),
                            "ending_balance": expr(
                                "max_by(trans.balance, trans.date)",
                                "trans.balance",
                                "trans.date",
                            ),
                        },
                    ),
                    transform(
                        "account_transaction_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="account",
                        event_source_table="trans",
                        join=join("account.account_id", "trans.account_id"),
                        target_field="transaction_events",
                        event_type_field="trans.operation",
                        event_time_field="trans.date",
                        event_payload={
                            "amount": "trans.amount",
                            "balance": "trans.balance",
                            "symbol": "trans.k_symbol",
                            "bank": "trans.bank",
                        },
                    ),
                    transform(
                        "account_risk_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="account_tags",
                        tags={
                            "loan_default_watch": {
                                "condition": "loan.status in ('B', 'D')",
                                "provenance": ["loan.status"],
                            },
                            "monthly_issuance": {
                                "condition": "account.frequency == 'POPLATEK MESICNE'",
                                "provenance": ["account.frequency"],
                            },
                            "regional_salary_context": {
                                "condition": "district.A11 is not null",
                                "provenance": ["district.A11"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "financial_parties",
                purpose="Typed account, client, loan, and card facets for native unions.",
                source_tables=["account", "client", "loan", "card", "disp"],
                transforms=[
                    transform(
                        "party_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="party_type",
                        variants={
                            "account": {
                                "source_table": "account",
                                "fields": {
                                    "party_id": expr(
                                        "concat('account:', account.account_id)",
                                        "account.account_id",
                                    ),
                                    "opened_on": field_source("account.date"),
                                    "frequency": field_source("account.frequency"),
                                },
                            },
                            "client": {
                                "source_table": "client",
                                "fields": {
                                    "party_id": expr(
                                        "concat('client:', client.client_id)",
                                        "client.client_id",
                                    ),
                                    "gender": field_source("client.gender"),
                                    "birth_date": field_source("client.birth_date"),
                                },
                            },
                            "loan": {
                                "source_table": "loan",
                                "fields": {
                                    "party_id": expr(
                                        "concat('loan:', loan.loan_id)",
                                        "loan.loan_id",
                                    ),
                                    "amount": field_source("loan.amount"),
                                    "status": field_source("loan.status"),
                                },
                            },
                            "card": {
                                "source_table": "card",
                                "fields": {
                                    "party_id": expr(
                                        "concat('card:', card.card_id)",
                                        "card.card_id",
                                    ),
                                    "card_type": field_source("card.type"),
                                    "issued": field_source("card.issued"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build account-ledger and party-network documents from real BIRD financial rows."""
    if db_id != "financial":
        raise ValueError(f"financial materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    accounts = _rows(conn, "account", ["account_id"])
    clients = _rows(conn, "client", ["client_id"])
    dispositions = _rows(conn, "disp", ["account_id", "type", "disp_id"])
    cards = _rows(conn, "card", ["disp_id", "issued", "card_id"])
    loans = _rows(conn, "loan", ["account_id", "date", "loan_id"])
    districts = _rows(conn, "district", ["district_id"])
    orders = _rows(conn, "order", ["account_id", "order_id"])
    monthly_summaries = _transaction_monthly_summaries(conn)
    transaction_samples = _transaction_samples(conn)
    balance_summaries = _balance_summaries(conn)
    loan_flow_summaries = _loan_flow_summaries(conn)

    clients_by_id = _by_id(clients, "client_id")
    districts_by_id = _by_id(districts, "district_id")
    disps_by_account = _group(dispositions, "account_id")
    cards_by_disp = _group(cards, "disp_id")
    loans_by_account = _group(loans, "account_id")
    monthly_by_account = _group(monthly_summaries, "account_id")
    samples_by_account = _group(transaction_samples, "account_id")
    balance_by_account = _by_id(balance_summaries, "account_id")
    loan_flows_by_account = _group(loan_flow_summaries, "account_id")
    orders_by_account = _group(orders, "account_id")

    account_docs = [
        _account_ledger_doc(
            account,
            district=districts_by_id.get(account.get("district_id"), {}),
            dispositions=disps_by_account.get(account.get("account_id"), []),
            clients_by_id=clients_by_id,
            cards_by_disp=cards_by_disp,
            loans=loans_by_account.get(account.get("account_id"), []),
            monthly_summaries=monthly_by_account.get(account.get("account_id"), []),
            transaction_samples=samples_by_account.get(account.get("account_id"), []),
            balance_summary=balance_by_account.get(account.get("account_id"), {}),
            loan_flow_summaries=loan_flows_by_account.get(account.get("account_id"), []),
            orders=orders_by_account.get(account.get("account_id"), []),
            db_id=db_id,
        )
        for account in accounts
    ]
    party_docs = _party_relationship_docs(
        accounts=accounts,
        disps_by_account=disps_by_account,
        clients_by_id=clients_by_id,
        cards_by_disp=cards_by_disp,
        districts_by_id=districts_by_id,
        loans_by_account=loans_by_account,
    )
    district_docs = _district_market_docs(
        districts=districts,
        accounts=accounts,
        clients=clients,
        loans=loans,
    )
    counterparty_docs = _counterparty_flow_docs(
        symbol_summaries=_counterparty_symbol_summaries(conn, orders),
        monthly_summaries=_counterparty_monthly_summaries(conn),
        samples=_counterparty_samples(conn, orders),
    )
    data = {
        "account_ledgers": account_docs,
        "party_relationship_graphs": party_docs,
        "district_market_contexts": district_docs,
        "counterparty_flow_profiles": counterparty_docs,
    }

    audit = audit_database_structure(db_id, data)
    features = _native_features()
    manifest = NativeFeatureManifest(db_id=db_id, features=features)
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "account_ledgers": {
                "document_count": len(account_docs),
                "root_entity": "bank account ledger",
                "source_tables": [
                    "account",
                    "trans",
                    "loan",
                    "disp",
                    "client",
                    "card",
                    "district",
                    "order",
                ],
            },
            "party_relationship_graphs": {
                "document_count": len(party_docs),
                "root_entity": "client-disposition-account party graph",
                "source_tables": ["account", "disp", "client", "card", "loan", "district"],
            },
            "district_market_contexts": {
                "document_count": len(district_docs),
                "root_entity": "district banking market context",
                "source_tables": ["district", "account", "client", "loan"],
            },
            "counterparty_flow_profiles": {
                "document_count": len(counterparty_docs),
                "root_entity": "external bank/counterparty flow profile",
                "source_tables": ["trans", "order"],
            },
        },
        "structure_audit": audit.to_dict(),
        "structure_gate": validate_structure_gate(audit).to_dict(),
    }
    provenance = {
        "db_id": db_id,
        "conversion_code_ref": f"{MODULE_REF}.materialize_native_dataworld",
        "entries": {
            feature.id: {
                "source_tables": _source_tables_from_refs(feature.provenance_refs),
                "provenance_refs": list(feature.provenance_refs),
                "field": feature.field,
            }
            for feature in features
        },
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "financial_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(features),
            max_depth=audit.max_depth,
            gate_ok=validate_structure_gate(audit).ok,
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
        validation=None,
    )


def _rows(conn: Any, table: str, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _query_rows(conn: Any, sql: str) -> list[dict[str, Any]]:
    cursor = conn.execute(sql)
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _transaction_monthly_summaries(conn: Any) -> list[dict[str, Any]]:
    return _query_rows(
        conn,
        f"""
        WITH monthly AS (
          SELECT
            account_id,
            substr(date, 1, 7) AS month,
            type,
            operation,
            k_symbol,
            count(*) AS transaction_count,
            sum(amount) AS amount_total,
            min(balance) AS min_balance,
            max(balance) AS max_balance
          FROM trans
          GROUP BY account_id, substr(date, 1, 7), type, operation, k_symbol
        ),
        ranked AS (
          SELECT
            monthly.*,
            dense_rank() OVER (PARTITION BY account_id ORDER BY month) AS month_rank
          FROM monthly
        )
        SELECT
          account_id, month, type, operation, k_symbol,
          transaction_count, amount_total, min_balance, max_balance
        FROM ranked
        WHERE month_rank <= {MAX_MONTH_BUCKETS_PER_ACCOUNT}
        ORDER BY account_id, month, type, operation, k_symbol
        """,
    )


def _loan_flow_summaries(conn: Any) -> list[dict[str, Any]]:
    return _query_rows(
        conn,
        """
        SELECT
          account_id,
          substr(date, 1, 7) AS month,
          type,
          operation,
          k_symbol,
          count(*) AS transaction_count,
          sum(amount) AS amount_total,
          min(balance) AS min_balance,
          max(balance) AS max_balance
        FROM trans
        WHERE k_symbol = 'UVER'
        GROUP BY account_id, substr(date, 1, 7), type, operation, k_symbol
        ORDER BY account_id, month, type, operation, k_symbol
        """,
    )


def _transaction_samples(conn: Any) -> list[dict[str, Any]]:
    return _query_rows(
        conn,
        f"""
        SELECT
          trans_id, account_id, date, type, operation, amount, balance, k_symbol, bank, account
        FROM (
          SELECT
            trans.*,
            dense_rank() OVER (
              PARTITION BY account_id
              ORDER BY substr(date, 1, 7)
            ) AS month_rank,
            row_number() OVER (
              PARTITION BY account_id, substr(date, 1, 7)
              ORDER BY date, trans_id
            ) AS rn
          FROM trans
        )
        WHERE month_rank <= {MAX_MONTH_BUCKETS_PER_ACCOUNT}
          AND rn <= {TRANSACTION_SAMPLE_PER_MONTH}
        ORDER BY account_id, date, trans_id
        """,
    )


def _balance_summaries(conn: Any) -> list[dict[str, Any]]:
    return _query_rows(
        conn,
        """
        WITH ranked AS (
          SELECT
            account_id,
            balance,
            row_number() OVER (PARTITION BY account_id ORDER BY date, trans_id) AS first_rn,
            row_number() OVER (PARTITION BY account_id ORDER BY date DESC, trans_id DESC) AS latest_rn
          FROM trans
        ),
        aggregate_balance AS (
          SELECT
            account_id,
            count(*) AS transaction_count,
            min(balance) AS minimum_balance,
            max(balance) AS maximum_balance
          FROM trans
          GROUP BY account_id
        )
        SELECT
          aggregate_balance.account_id,
          aggregate_balance.transaction_count,
          aggregate_balance.minimum_balance,
          aggregate_balance.maximum_balance,
          max(CASE WHEN ranked.first_rn = 1 THEN ranked.balance END) AS first_balance,
          max(CASE WHEN ranked.latest_rn = 1 THEN ranked.balance END) AS latest_balance
        FROM aggregate_balance
        JOIN ranked USING (account_id)
        GROUP BY
          aggregate_balance.account_id,
          aggregate_balance.transaction_count,
          aggregate_balance.minimum_balance,
          aggregate_balance.maximum_balance
        ORDER BY aggregate_balance.account_id
        """,
    )


def _counterparty_symbol_summaries(conn: Any, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          CASE
            WHEN bank IS NULL AND account IS NULL THEN 'missing_counterparty'
            WHEN bank IS NULL OR bank = '' THEN 'unknown_bank'
            ELSE bank
          END AS bank_key,
          CASE
            WHEN k_symbol IS NULL OR k_symbol = '' THEN 'no_symbol'
            ELSE k_symbol
          END AS symbol_key,
          'trans' AS source_table,
          count(*) AS flow_count,
          sum(amount) AS amount_total
        FROM trans
        GROUP BY bank_key, symbol_key
        ORDER BY bank_key, symbol_key
        """,
    )
    order_summaries: dict[tuple[str, str], dict[str, Any]] = {}
    for row in orders:
        key = (_safe_key(row.get("bank_to"), "unknown_bank"), _safe_key(row.get("k_symbol"), "no_symbol"))
        item = order_summaries.setdefault(
            key,
            {
                "bank_key": key[0],
                "symbol_key": key[1],
                "source_table": "order",
                "flow_count": 0,
                "amount_total": 0.0,
            },
        )
        item["flow_count"] += 1
        item["amount_total"] += row.get("amount") or 0
    return rows + [order_summaries[key] for key in sorted(order_summaries)]


def _counterparty_monthly_summaries(conn: Any) -> list[dict[str, Any]]:
    return _query_rows(
        conn,
        """
        SELECT
          CASE
            WHEN bank IS NULL AND account IS NULL THEN 'missing_counterparty'
            WHEN bank IS NULL OR bank = '' THEN 'unknown_bank'
            ELSE bank
          END AS bank_key,
          substr(date, 1, 7) AS month,
          operation,
          k_symbol,
          count(*) AS transaction_count,
          sum(amount) AS amount_total,
          min(balance) AS min_balance,
          max(balance) AS max_balance
        FROM trans
        GROUP BY bank_key, substr(date, 1, 7), operation, k_symbol
        ORDER BY bank_key, month, operation, k_symbol
        """,
    )


def _counterparty_samples(conn: Any, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _query_rows(
        conn,
        """
        SELECT
          'trans' AS source_table,
          CASE
            WHEN bank IS NULL AND account IS NULL THEN 'missing_counterparty'
            WHEN bank IS NULL OR bank = '' THEN 'unknown_bank'
            ELSE bank
          END AS bank_key,
          trans_id,
          account_id,
          date,
          type,
          operation,
          amount,
          balance,
          k_symbol,
          bank,
          account
        FROM (
          SELECT
            trans.*,
            row_number() OVER (
              PARTITION BY
                CASE
                  WHEN bank IS NULL AND account IS NULL THEN 'missing_counterparty'
                  WHEN bank IS NULL OR bank = '' THEN 'unknown_bank'
                  ELSE bank
                END,
                CASE
                  WHEN k_symbol IS NULL OR k_symbol = '' THEN 'no_symbol'
                  ELSE k_symbol
                END
              ORDER BY date, trans_id
            ) AS rn
          FROM trans
        )
        WHERE rn <= 10
        ORDER BY bank_key, date, trans_id
        """,
    )
    for row in orders:
        rows.append(
            {
                "source_table": "order",
                "bank_key": _safe_key(row.get("bank_to"), "unknown_bank"),
                "trans_id": None,
                "account_id": row.get("account_id"),
                "date": None,
                "type": "ORDER_RULE",
                "operation": "STANDING_ORDER",
                "amount": row.get("amount"),
                "balance": None,
                "k_symbol": row.get("k_symbol"),
                "bank": row.get("bank_to"),
                "account": row.get("account_to"),
            }
        )
    return rows


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row.get(key): row for row in rows}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _account_ledger_doc(
    account: dict[str, Any],
    *,
    district: dict[str, Any],
    dispositions: list[dict[str, Any]],
    clients_by_id: dict[Any, dict[str, Any]],
    cards_by_disp: dict[Any, list[dict[str, Any]]],
    loans: list[dict[str, Any]],
    monthly_summaries: list[dict[str, Any]],
    transaction_samples: list[dict[str, Any]],
    balance_summary: dict[str, Any],
    loan_flow_summaries: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    db_id: str,
) -> dict[str, Any]:
    account_id = account.get("account_id")
    loan = loans[0] if loans else None
    party_graph = _account_party_graph(dispositions, clients_by_id, cards_by_disp)
    monthly_activity = _activity_by_month(monthly_summaries, transaction_samples)
    return {
        "_id": f"account:{account_id}",
        "identity": {
            "source_db": db_id,
            "account_id": account_id,
            "account_presence_state": _presence_state(account_id),
            "opened_on": {
                "value": account.get("date"),
                "presence_state": _presence_state(account.get("date")),
            },
            "service_plan": {
                "frequency": account.get("frequency"),
                "frequency_key": _safe_key(account.get("frequency"), "unknown_frequency"),
                "presence_state": _presence_state(account.get("frequency")),
            },
        },
        "ledger": {
            "account_id": account_id,
            "district_id": account.get("district_id"),
            "transaction_count": int(balance_summary.get("transaction_count") or 0),
            "standing_order_count": len(orders),
            "balance_snapshot": _balance_snapshot(balance_summary),
            "standing_orders_by_symbol": _orders_by_symbol(orders),
        },
        "timeline": {
            "opened_on": account.get("date"),
            "events_by_month": _timeline_events_by_month(
                account,
                monthly_summaries=monthly_summaries,
                transaction_samples=transaction_samples,
                loans=loans,
                dispositions=dispositions,
                cards_by_disp=cards_by_disp,
                orders=orders,
            ),
        },
        "cashflow": {
            "component_presence": {
                "transactions": _presence_state(monthly_summaries),
                "standing_orders": _presence_state(orders),
                "loan": _presence_state(loan),
                "cards": _presence_state(
                    [card for disp in dispositions for card in cards_by_disp.get(disp.get("disp_id"), [])]
                ),
            },
            "activity_by_month": monthly_activity,
            "monthly_flows": _monthly_flow_docs(monthly_activity),
        },
        "loan": _loan_profile(loan, loan_flow_summaries, transaction_samples),
        "party_graph": party_graph,
        "district_context": _district_snapshot(district),
        "risk_tags": _risk_tags(loan, balance_summary, district, party_graph),
        "_provenance": {
            "source_tables": ["account", "trans", "loan", "disp", "client", "card", "district", "order"],
            "source_keys": {"account_id": account_id, "district_id": account.get("district_id")},
        },
    }


def _balance_snapshot(summary: dict[str, Any]) -> dict[str, Any]:
    if not summary:
        return {"presence_state": "missing", "first": None, "latest": None, "minimum": None, "maximum": None}
    return {
        "presence_state": "present",
        "first": summary.get("first_balance"),
        "latest": summary.get("latest_balance"),
        "minimum": summary.get("minimum_balance"),
        "maximum": summary.get("maximum_balance"),
    }


def _activity_by_month(
    summaries: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    samples_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in summaries:
        grouped[row.get("month")].append(row)
    for row in samples:
        samples_by_month[_month_key(row.get("date"))].append(row)
    out: dict[str, dict[str, Any]] = {}
    for month, rows in sorted(grouped.items()):
        credits = [row for row in rows if row.get("type") == "PRIJEM"]
        withdrawals = [row for row in rows if row.get("type") == "VYDAJ"]
        out[month] = {
            "presence_state": _presence_state(rows),
            "entry_count": sum(int(row.get("transaction_count") or 0) for row in rows),
            "credit_amount": sum(row.get("amount_total") or 0 for row in credits),
            "withdrawal_amount": sum(row.get("amount_total") or 0 for row in withdrawals),
            "ending_balance": max((row.get("max_balance") or 0 for row in rows), default=None),
            "operations_by_symbol": _operations_by_symbol_summary(rows, samples_by_month.get(month, [])),
            "entries": [_transaction_entry(row) for row in samples_by_month.get(month, [])[:12]],
            "entry_sample_limit": 12,
        }
    return out


def _operations_by_symbol_summary(
    rows: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        op = _safe_key(row.get("operation"), "no_operation")
        symbol = _safe_key(row.get("k_symbol"), "no_symbol")
        grouped[f"{op}::{symbol}"].append(row)
    samples_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in samples:
        op = _safe_key(row.get("operation"), "no_operation")
        symbol = _safe_key(row.get("k_symbol"), "no_symbol")
        samples_by_key[f"{op}::{symbol}"].append(row)
    return {
        key: {
            "presence_state": _presence_state(items),
            "transaction_count": sum(int(item.get("transaction_count") or 0) for item in items),
            "amount_total": sum(item.get("amount_total") or 0 for item in items),
            "sample_transactions": [_transaction_entry(item) for item in samples_by_key.get(key, [])[:8]],
        }
        for key, items in sorted(grouped.items())
    }


def _transaction_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "transaction_id": row.get("trans_id"),
        "date": row.get("date"),
        "flow": {
            "type": row.get("type"),
            "operation": {
                "value": row.get("operation"),
                "presence_state": _presence_state(row.get("operation")),
            },
            "k_symbol": {
                "value": row.get("k_symbol"),
                "presence_state": _presence_state(row.get("k_symbol")),
            },
        },
        "amount": row.get("amount"),
        "balance_after": row.get("balance"),
        "counterparty": {
            "bank": {"value": row.get("bank"), "presence_state": _presence_state(row.get("bank"))},
            "account": {
                "value": row.get("account"),
                "presence_state": _presence_state(row.get("account")),
            },
        },
    }


def _monthly_flow_docs(activity_by_month: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "month": month,
            "presence_state": payload.get("presence_state", "present"),
            "credit_amount": payload.get("credit_amount", 0),
            "withdrawal_amount": payload.get("withdrawal_amount", 0),
            "operations_by_symbol": payload.get("operations_by_symbol", {}),
        }
        for month, payload in sorted(activity_by_month.items())
    ]


def _timeline_events_by_month(
    account: dict[str, Any],
    *,
    monthly_summaries: list[dict[str, Any]],
    transaction_samples: list[dict[str, Any]],
    loans: list[dict[str, Any]],
    dispositions: list[dict[str, Any]],
    cards_by_disp: dict[Any, list[dict[str, Any]]],
    orders: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events[_month_key(account.get("date"))].append(
        {
            "event_type": "account_opened",
            "date": account.get("date"),
            "source": {"table": "account", "account_id": account.get("account_id")},
        }
    )
    summaries_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    samples_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in monthly_summaries:
        summaries_by_month[row.get("month")].append(row)
    for row in transaction_samples:
        samples_by_month[_month_key(row.get("date"))].append(row)
    for month, rows in summaries_by_month.items():
        events[month].append(
            {
                "event_type": "monthly_cashflow_closed",
                "date": month,
                "source": {
                    "table": "trans",
                    "transaction_count": sum(int(row.get("transaction_count") or 0) for row in rows),
                    "sample_limit": 5,
                },
                "amount_total": sum(row.get("amount_total") or 0 for row in rows),
                "sample_transactions": [_transaction_entry(row) for row in samples_by_month.get(month, [])[:5]],
            }
        )
    for row in loans:
        events[_month_key(row.get("date"))].append(
            {
                "event_type": "loan_originated",
                "date": row.get("date"),
                "source": {"table": "loan", "loan_id": row.get("loan_id")},
                "loan_status": row.get("status"),
                "amount": row.get("amount"),
            }
        )
    for row in orders:
        events["standing-order-rules"].append(
            {
                "event_type": "standing_order_rule",
                "source": {"table": "order", "order_id": row.get("order_id")},
                "symbol": row.get("k_symbol"),
                "amount": row.get("amount"),
            }
        )
    for disp in dispositions:
        for card in cards_by_disp.get(disp.get("disp_id"), []):
            events[_month_key(card.get("issued"))].append(
                {
                    "event_type": "card_issued",
                    "date": card.get("issued"),
                    "source": {"table": "card", "card_id": card.get("card_id")},
                    "disposition_type": disp.get("type"),
                    "card_type": card.get("type"),
                }
            )
    return {month: sorted(items, key=lambda item: (str(item.get("date")), str(item.get("event_type")))) for month, items in sorted(events.items())}


def _orders_by_symbol(orders: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in orders:
        grouped[_safe_key(row.get("k_symbol"), "no_symbol")].append(row)
    return {
        symbol: [
            {
                "order_id": row.get("order_id"),
                "bank_to": row.get("bank_to"),
                "account_to": row.get("account_to"),
                "amount": row.get("amount"),
                "presence_state": _presence_state(row.get("amount")),
            }
            for row in items
        ]
        for symbol, items in sorted(grouped.items())
    }


def _loan_profile(
    loan: dict[str, Any] | None,
    loan_flow_summaries: list[dict[str, Any]],
    transaction_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    if loan is None:
        return {
            "contract": {
                "loan_id": None,
                "presence_state": "missing",
                "status": None,
                "status_bucket": "no_loan",
            },
            "repayment_schedule": {"presence_state": "missing", "by_due_month": {}},
            "observed_loan_flows": {"presence_state": "missing", "transactions_by_month": {}},
        }
    schedule = _repayment_schedule(loan)
    loan_samples = [row for row in transaction_samples if row.get("k_symbol") == "UVER"]
    return {
        "contract": {
            "loan_id": loan.get("loan_id"),
            "presence_state": "present",
            "opened_on": loan.get("date"),
            "amount": loan.get("amount"),
            "duration_months": loan.get("duration"),
            "scheduled_payment": loan.get("payments"),
            "status": loan.get("status"),
            "status_bucket": _loan_status_bucket(loan.get("status")),
        },
        "repayment_schedule": {
            "presence_state": _presence_state(schedule),
            "by_due_month": schedule,
        },
        "observed_loan_flows": {
            "presence_state": _presence_state(loan_flow_summaries),
            "transactions_by_month": _activity_by_month(loan_flow_summaries, loan_samples),
        },
    }


def _repayment_schedule(loan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    start = _month_key(loan.get("date"))
    duration = int(loan.get("duration") or 0)
    payment = float(loan.get("payments") or 0)
    amount = float(loan.get("amount") or 0)
    out: dict[str, dict[str, Any]] = {}
    for index in range(duration):
        due_month = _add_months(start, index)
        scheduled_paid = round(payment * (index + 1), 2)
        out[due_month] = {
            "installment_index": index + 1,
            "presence_state": "present",
            "scheduled_payment": payment,
            "scheduled_principal_remaining": max(round(amount - scheduled_paid, 2), 0),
            "contract_status_bucket": _loan_status_bucket(loan.get("status")),
        }
    return out


def _account_party_graph(
    dispositions: list[dict[str, Any]],
    clients_by_id: dict[Any, dict[str, Any]],
    cards_by_disp: dict[Any, list[dict[str, Any]]],
) -> dict[str, Any]:
    members_by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
    disposition_docs: list[dict[str, Any]] = []
    for disp in dispositions:
        client = clients_by_id.get(disp.get("client_id"), {})
        cards = cards_by_disp.get(disp.get("disp_id"), [])
        role = _safe_key(disp.get("type"), "unknown_role")
        member = {
            "disp_id": disp.get("disp_id"),
            "role": disp.get("type"),
            "client": _client_snapshot(client),
            "cards": [_card_snapshot(card) for card in cards],
            "card_presence_state": _presence_state(cards),
        }
        members_by_role[role].append(member)
        disposition_docs.append(member)
    return {
        "presence_state": _presence_state(disposition_docs),
        "dispositions": disposition_docs,
        "members_by_role": {role: items for role, items in sorted(members_by_role.items())},
    }


def _client_snapshot(client: dict[str, Any]) -> dict[str, Any]:
    return {
        "client_id": client.get("client_id"),
        "gender": {"value": client.get("gender"), "presence_state": _presence_state(client.get("gender"))},
        "birth_date": {
            "value": client.get("birth_date"),
            "presence_state": _presence_state(client.get("birth_date")),
        },
        "district_id": client.get("district_id"),
    }


def _card_snapshot(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": card.get("card_id"),
        "card_type": card.get("type"),
        "issued": {"value": card.get("issued"), "presence_state": _presence_state(card.get("issued"))},
    }


def _district_snapshot(district: dict[str, Any]) -> dict[str, Any]:
    return {
        "district_id": district.get("district_id"),
        "name": district.get("A2"),
        "region": district.get("A3"),
        "population": district.get("A4"),
        "avg_salary": district.get("A11"),
        "unemployment": {
            "prior": {"value": district.get("A12"), "presence_state": _presence_state(district.get("A12"))},
            "current": {"value": district.get("A13"), "presence_state": _presence_state(district.get("A13"))},
        },
        "entrepreneur_rate": district.get("A14"),
        "crime": {
            "prior": {"value": district.get("A15"), "presence_state": _presence_state(district.get("A15"))},
            "current": {"value": district.get("A16"), "presence_state": _presence_state(district.get("A16"))},
        },
    }


def _risk_tags(
    loan: dict[str, Any] | None,
    balance_summary: dict[str, Any],
    district: dict[str, Any],
    party_graph: dict[str, Any],
) -> list[str]:
    tags: list[str] = []
    if loan is not None:
        tags.append("active_or_historical_loan")
        if loan.get("status") in {"B", "D"}:
            tags.append("loan_default_watch")
    if balance_summary and (balance_summary.get("minimum_balance") or 0) < 0:
        tags.append("negative_balance_observed")
    if district.get("A11") is not None and int(district.get("A11") or 0) >= 10000:
        tags.append("high_salary_district")
    if len(party_graph.get("dispositions", [])) > 1:
        tags.append("shared_account")
    return tags or ["standard_retail_account"]


def _party_relationship_docs(
    *,
    accounts: list[dict[str, Any]],
    disps_by_account: dict[Any, list[dict[str, Any]]],
    clients_by_id: dict[Any, dict[str, Any]],
    cards_by_disp: dict[Any, list[dict[str, Any]]],
    districts_by_id: dict[Any, dict[str, Any]],
    loans_by_account: dict[Any, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for account in accounts:
        account_id = account.get("account_id")
        graph = _account_party_graph(
            disps_by_account.get(account_id, []),
            clients_by_id,
            cards_by_disp,
        )
        loan = (loans_by_account.get(account_id) or [None])[0]
        docs.append(
            {
                "_id": f"party_graph:{account_id}",
                "account": {
                    "account_id": account_id,
                    "opened_on": account.get("date"),
                    "frequency": account.get("frequency"),
                    "district": _district_snapshot(districts_by_id.get(account.get("district_id"), {})),
                },
                "relationships": graph,
                "loan_link": {
                    "presence_state": _presence_state(loan),
                    "loan_id": None if loan is None else loan.get("loan_id"),
                    "status_bucket": "no_loan" if loan is None else _loan_status_bucket(loan.get("status")),
                },
                "party_states": {
                    "clients": _presence_state(graph.get("dispositions", [])),
                    "cards": _presence_state(
                        [card for item in graph.get("dispositions", []) for card in item.get("cards", [])]
                    ),
                },
            }
        )
    return docs


def _district_market_docs(
    *,
    districts: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    clients: list[dict[str, Any]],
    loans: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    accounts_by_district = _group(accounts, "district_id")
    clients_by_district = _group(clients, "district_id")
    loans_by_account = _group(loans, "account_id")
    docs: list[dict[str, Any]] = []
    for district in districts:
        district_id = district.get("district_id")
        district_accounts = accounts_by_district.get(district_id, [])
        district_clients = clients_by_district.get(district_id, [])
        docs.append(
            {
                "_id": f"district:{district_id}",
                "district": _district_snapshot(district),
                "market_presence": {
                    "accounts": _presence_state(district_accounts),
                    "clients": _presence_state(district_clients),
                    "loans": _presence_state(
                        [loan for account in district_accounts for loan in loans_by_account.get(account.get("account_id"), [])]
                    ),
                },
                "accounts_by_frequency": _accounts_by_frequency(district_accounts, loans_by_account),
                "clients_by_gender": _clients_by_gender(district_clients),
                "salary_band": _salary_band(district.get("A11")),
            }
        )
    return docs


def _accounts_by_frequency(
    accounts: list[dict[str, Any]],
    loans_by_account: dict[Any, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for account in accounts:
        grouped[_safe_key(account.get("frequency"), "unknown_frequency")].append(account)
    return {
        frequency: [
            {
                "account_id": account.get("account_id"),
                "opened_on": account.get("date"),
                "loan_presence_state": _presence_state(loans_by_account.get(account.get("account_id"), [])),
            }
            for account in items
        ]
        for frequency, items in sorted(grouped.items())
    }


def _clients_by_gender(clients: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for client in clients:
        grouped[_safe_key(client.get("gender"), "unknown_gender")].append(client)
    return {
        gender: [_client_snapshot(client) for client in items]
        for gender, items in sorted(grouped.items())
    }


def _counterparty_flow_docs(
    *,
    symbol_summaries: list[dict[str, Any]],
    monthly_summaries: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    flow_rows = _group(symbol_summaries, "bank_key")
    monthly_rows = _group(monthly_summaries, "bank_key")
    sample_rows = _group(samples, "bank_key")
    docs: list[dict[str, Any]] = []
    for bank in sorted(set(flow_rows) | set(monthly_rows) | set(sample_rows)):
        docs.append(
            {
                "_id": f"counterparty:{bank}",
                "counterparty": {
                    "bank_key": bank,
                    "bank_presence_state": "missing" if bank == "missing_counterparty" else "present",
                },
                "flow_presence": _presence_state(flow_rows.get(bank, [])),
                "flows_by_symbol": _flows_by_symbol_summary(
                    flow_rows.get(bank, []),
                    sample_rows.get(bank, []),
                ),
                "monthly_flow_index": _counterparty_monthly_flow_index(
                    monthly_rows.get(bank, []),
                    sample_rows.get(bank, []),
                ),
            }
        )
    return docs


def _flows_by_symbol_summary(
    rows: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_safe_key(row.get("symbol_key"), "no_symbol")].append(row)
    samples_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in samples:
        samples_by_symbol[_safe_key(row.get("k_symbol"), "no_symbol")].append(row)
    return {
        symbol: {
            "presence_state": _presence_state(items),
            "source_tables": sorted({str(item.get("source_table")) for item in items}),
            "flow_count": sum(int(item.get("flow_count") or 0) for item in items),
            "amount_total": sum(item.get("amount_total") or 0 for item in items),
            "sample_edges": [_flow_edge(item) for item in samples_by_symbol.get(symbol, [])[:20]],
        }
        for symbol, items in sorted(grouped.items())
    }


def _counterparty_monthly_flow_index(
    rows: list[dict[str, Any]],
    samples: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_safe_key(row.get("month"), "standing-order-rules")].append(row)
    samples_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in samples:
        samples_by_month[_month_key(row.get("date")) if row.get("date") else "standing-order-rules"].append(row)
    return {
        month: {
            "presence_state": _presence_state(items),
            "operations_by_symbol": _operations_by_symbol_summary(items, samples_by_month.get(month, [])),
        }
        for month, items in sorted(grouped.items())
    }


def _flow_edge(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_table": row.get("source_table"),
        "account_id": row.get("account_id"),
        "transaction_id": row.get("trans_id"),
        "date": row.get("date"),
        "operation": row.get("operation"),
        "symbol": row.get("k_symbol"),
        "amount": row.get("amount"),
        "counterparty_account": {
            "value": row.get("account"),
            "presence_state": _presence_state(row.get("account")),
        },
    }


def _loan_status_bucket(status: Any) -> str:
    return {
        "A": "completed_good",
        "B": "completed_bad",
        "C": "running_good",
        "D": "running_bad",
    }.get(str(status), "unknown_status")


def _salary_band(value: Any) -> str:
    if value is None:
        return "unknown_salary"
    salary = int(value)
    if salary >= 11000:
        return "high_salary"
    if salary >= 9000:
        return "middle_salary"
    return "lower_salary"


def _month_key(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 7:
        return text[:7]
    return "unknown-month"


def _add_months(month: str, offset: int) -> str:
    try:
        year = int(month[:4])
        month_number = int(month[5:7])
    except ValueError:
        return f"unknown-month-plus-{offset}"
    total = year * 12 + month_number - 1 + offset
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if value == "" or value == [] or value == {}:
        return "empty"
    return "present"


def _safe_key(value: Any, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    text = str(value)
    for old, new in [(" ", "_"), ("/", "_"), (".", "_"), ("$", "S")]:
        text = text.replace(old, new)
    return text


def _source_tables_from_refs(refs: list[str]) -> list[str]:
    return sorted({ref.split(".", 1)[0] for ref in refs if "." in ref})


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="account_ledgers.monthly_activity_matrix",
            type="dynamic_key_object",
            collection="account_ledgers",
            field="cashflow.activity_by_month",
            query_patterns=["monthly_account_cashflow_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum", "$size"],
            provenance_refs=["trans.date", "trans.amount", "trans.type", "trans.operation", "trans.k_symbol"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "monthly_account_cashflow_matrix",
                        "intent": "roll up account cash-flow entries from month-keyed ledger buckets",
                        "pipeline": [
                            {
                                "$project": {
                                    "account_id": "$identity.account_id",
                                    "months": {"$objectToArray": "$cashflow.activity_by_month"},
                                }
                            },
                            {"$unwind": "$months"},
                            {"$unwind": "$months.v.entries"},
                            {
                                "$group": {
                                    "_id": {
                                        "month": "$months.k",
                                        "flow_type": "$months.v.entries.flow.type",
                                    },
                                    "amount_total": {"$sum": "$months.v.entries.amount"},
                                    "transaction_count": {"$sum": 1},
                                    "accounts": {"$addToSet": "$account_id"},
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "month": "$_id.month",
                                    "flow_type": "$_id.flow_type",
                                    "amount_total": 1,
                                    "transaction_count": 1,
                                    "account_count": {"$size": "$accounts"},
                                }
                            },
                            {"$sort": {"month": 1, "flow_type": 1}},
                            {"$limit": 50},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="account_ledgers.loan_repayment_schedule",
            type="dynamic_key_object",
            collection="account_ledgers",
            field="loan.repayment_schedule.by_due_month",
            query_patterns=["loan_status_repayment_schedule"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["loan.date", "loan.amount", "loan.duration", "loan.payments", "loan.status"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "loan_status_repayment_schedule",
                        "intent": "compare scheduled loan exposure by loan status across due-month keys",
                        "pipeline": [
                            {"$match": {"loan.contract.presence_state": "present"}},
                            {
                                "$project": {
                                    "status_bucket": "$loan.contract.status_bucket",
                                    "schedule": {
                                        "$objectToArray": "$loan.repayment_schedule.by_due_month"
                                    },
                                }
                            },
                            {"$unwind": "$schedule"},
                            {
                                "$group": {
                                    "_id": {
                                        "status_bucket": "$status_bucket",
                                        "due_month": "$schedule.k",
                                    },
                                    "scheduled_payment_total": {
                                        "$sum": "$schedule.v.scheduled_payment"
                                    },
                                    "principal_remaining_total": {
                                        "$sum": "$schedule.v.scheduled_principal_remaining"
                                    },
                                    "loan_accounts": {"$sum": 1},
                                }
                            },
                            {"$sort": {"_id.status_bucket": 1, "_id.due_month": 1}},
                            {"$limit": 60},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="party_relationship_graphs.disposition_party_network",
            type="dynamic_key_object",
            collection="party_relationship_graphs",
            field="relationships.members_by_role",
            query_patterns=["disposition_role_card_network"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$ifNull"],
            provenance_refs=["disp.type", "disp.client_id", "client.gender", "card.type"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "disposition_role_card_network",
                        "intent": "traverse role-keyed disposition members and card facets in account party graphs",
                        "pipeline": [
                            {
                                "$project": {
                                    "account_id": "$account.account_id",
                                    "roles": {"$objectToArray": "$relationships.members_by_role"},
                                }
                            },
                            {"$unwind": "$roles"},
                            {"$unwind": "$roles.v"},
                            {
                                "$project": {
                                    "role": "$roles.k",
                                    "gender": "$roles.v.client.gender.value",
                                    "card_count": {"$size": {"$ifNull": ["$roles.v.cards", []]}},
                                }
                            },
                            {
                                "$group": {
                                    "_id": {"role": "$role", "gender": "$gender"},
                                    "member_count": {"$sum": 1},
                                    "card_count": {"$sum": "$card_count"},
                                }
                            },
                            {"$sort": {"member_count": -1, "_id.role": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$ifNull", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="district_market_contexts.account_market_segments",
            type="dynamic_key_object",
            collection="district_market_contexts",
            field="accounts_by_frequency",
            query_patterns=["district_salary_frequency_segments"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["district.A11", "district.A3", "account.frequency", "account.account_id"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "district_salary_frequency_segments",
                        "intent": "fold account frequency buckets through district salary and region context",
                        "pipeline": [
                            {
                                "$project": {
                                    "region": "$district.region",
                                    "salary_band": "$salary_band",
                                    "frequency_accounts": {"$objectToArray": "$accounts_by_frequency"},
                                }
                            },
                            {"$unwind": "$frequency_accounts"},
                            {"$unwind": "$frequency_accounts.v"},
                            {
                                "$group": {
                                    "_id": {
                                        "region": "$region",
                                        "salary_band": "$salary_band",
                                        "frequency": "$frequency_accounts.k",
                                    },
                                    "account_count": {"$sum": 1},
                                    "loan_accounts": {
                                        "$sum": {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        "$frequency_accounts.v.loan_presence_state",
                                                        "present",
                                                    ]
                                                },
                                                1,
                                                0,
                                            ]
                                        }
                                    },
                                }
                            },
                            {"$sort": {"account_count": -1, "_id.region": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum", "$cond"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="counterparty_flow_profiles.bank_symbol_flow_matrix",
            type="dynamic_key_object",
            collection="counterparty_flow_profiles",
            field="flows_by_symbol",
            query_patterns=["counterparty_operation_symbol_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["trans.bank", "trans.account", "trans.k_symbol", "trans.amount", "order.bank_to"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "counterparty_operation_symbol_matrix",
                        "intent": "compare external-bank counterparty flow buckets by semantic payment symbol",
                        "pipeline": [
                            {
                                "$project": {
                                    "bank_key": "$counterparty.bank_key",
                                    "symbols": {"$objectToArray": "$flows_by_symbol"},
                                }
                            },
                            {"$unwind": "$symbols"},
                            {
                                "$group": {
                                    "_id": {"bank": "$bank_key", "symbol": "$symbols.k"},
                                    "amount_total": {"$sum": "$symbols.v.amount_total"},
                                    "flow_count": {"$sum": "$symbols.v.flow_count"},
                                }
                            },
                            {"$sort": {"amount_total": -1, "_id.bank": 1, "_id.symbol": 1}},
                            {"$limit": 40},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                    }
                ]
            },
        ),
    ]
