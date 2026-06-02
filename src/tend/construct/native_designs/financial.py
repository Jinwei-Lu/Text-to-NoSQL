from __future__ import annotations

from typing import Any

from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


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
