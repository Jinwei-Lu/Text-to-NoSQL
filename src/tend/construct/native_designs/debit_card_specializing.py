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
            "Represent fuel-card customers with month-keyed consumption, point-of-sale "
            "events, and product/station dimensions."
        ),
        collections=[
            collection(
                "fuel_customer_activity",
                purpose="Customer documents keyed by monthly consumption periods.",
                source_tables=["customers", "yearmonth", "transactions_1k"],
                transforms=[
                    transform(
                        "consumption_by_yearmonth",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="customers",
                        child_table="yearmonth",
                        join=join("customers.CustomerID", "yearmonth.CustomerID"),
                        target_field="consumption_by_yearmonth",
                        key=expr("yearmonth.Date", "yearmonth.Date"),
                        values={
                            "consumption": expr(
                                "sum(yearmonth.Consumption)",
                                "yearmonth.Consumption",
                            ),
                            "period_count": expr(
                                "count(yearmonth.Date)",
                                "yearmonth.Date",
                            ),
                        },
                    ),
                    transform(
                        "transaction_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="customers",
                        event_source_table="transactions_1k",
                        join=join("customers.CustomerID", "transactions_1k.CustomerID"),
                        target_field="transaction_events",
                        event_type_field="transactions_1k.ProductID",
                        event_time_field="transactions_1k.Date",
                        event_payload={
                            "time": "transactions_1k.Time",
                            "card_id": "transactions_1k.CardID",
                            "gas_station_id": "transactions_1k.GasStationID",
                            "amount": "transactions_1k.Amount",
                            "price": "transactions_1k.Price",
                        },
                    ),
                    transform(
                        "customer_segment_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="customer_tags",
                        tags={
                            "eur_payer": {
                                "condition": "customers.Currency == 'EUR'",
                                "provenance": ["customers.Currency"],
                            },
                            "fleet_segment": {
                                "condition": "customers.Segment is not null",
                                "provenance": ["customers.Segment"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "fuel_network_entities",
                purpose="Typed gas station and product entities for POS lookups.",
                source_tables=["gasstations", "products"],
                transforms=[
                    transform(
                        "network_entity_union",
                        "polymorphic_union",
                        module_ref=MODULE_REF,
                        discriminator="entity_type",
                        variants={
                            "gas_station": {
                                "source_table": "gasstations",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('gas:', gasstations.GasStationID)",
                                        "gasstations.GasStationID",
                                    ),
                                    "country": field_source("gasstations.Country"),
                                    "segment": field_source("gasstations.Segment"),
                                    "chain_id": field_source("gasstations.ChainID"),
                                },
                            },
                            "product": {
                                "source_table": "products",
                                "fields": {
                                    "entity_id": expr(
                                        "concat('product:', products.ProductID)",
                                        "products.ProductID",
                                    ),
                                    "description": field_source("products.Description"),
                                },
                            },
                        },
                    )
                ],
            ),
        ],
    )
