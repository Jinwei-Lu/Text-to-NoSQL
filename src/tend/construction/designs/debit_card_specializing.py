from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..audit import audit_database_structure
from ..executor import NativeExecutionResult
from ..recipe import NativeFeature, NativeFeatureManifest
from ..recipe import NativeMigrationRecipe
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


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build fuel-card MongoDB documents from the debit-card specialization schema."""
    if db_id != "debit_card_specializing":
        raise ValueError(f"debit_card_specializing materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    customers = _rows(conn, "customers", ["CustomerID"])
    stations = _rows(conn, "gasstations", ["GasStationID"])
    products = _rows(conn, "products", ["ProductID"])
    transactions = _rows(conn, "transactions_1k", ["Date", "Time", "TransactionID"])
    yearmonths = _rows(conn, "yearmonth", ["CustomerID", "Date"])

    stations_by_id = _by_id(stations, "GasStationID")
    products_by_id = _by_id(products, "ProductID")
    transactions_by_customer = _group(transactions, "CustomerID")
    transactions_by_station = _group(transactions, "GasStationID")
    transactions_by_product = _group(transactions, "ProductID")
    periods_by_customer = _group(yearmonths, "CustomerID")

    profile_docs = [
        _customer_profile_doc(
            customer,
            periods=periods_by_customer.get(customer.get("CustomerID"), []),
            transactions=transactions_by_customer.get(customer.get("CustomerID"), []),
            stations=stations_by_id,
            products=products_by_id,
            db_id=db_id,
        )
        for customer in customers
    ]
    station_docs = [
        _station_catalog_doc(
            station,
            transactions=transactions_by_station.get(station.get("GasStationID"), []),
            products=products_by_id,
            db_id=db_id,
        )
        for station in stations
    ]
    product_docs = [
        _product_timeline_doc(
            product,
            transactions=transactions_by_product.get(product.get("ProductID"), []),
            stations=stations_by_id,
            db_id=db_id,
        )
        for product in products
    ]

    data = {
        "fuel_customer_spend_profiles": profile_docs,
        "fuel_product_payment_timeline": product_docs,
        "fuel_station_market_catalog": station_docs,
    }
    audit = audit_database_structure(db_id, data)
    manifest = _manifest()
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "fuel_customer_spend_profiles": {
                "document_count": len(profile_docs),
                "root_entity": "fuel card customer",
                "source_tables": ["customers", "yearmonth", "transactions_1k", "gasstations", "products"],
            },
            "fuel_station_market_catalog": {
                "document_count": len(station_docs),
                "root_entity": "gas station merchant site",
                "source_tables": ["gasstations", "transactions_1k", "products"],
            },
            "fuel_product_payment_timeline": {
                "document_count": len(product_docs),
                "root_entity": "fuel product and payment timeline",
                "source_tables": ["products", "transactions_1k", "gasstations"],
            },
        },
        "structure_audit": audit.to_dict(),
    }
    provenance = _provenance(manifest)
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "debit_card_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(manifest.features),
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
    )


def _rows(conn: Any, table: str, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row.get(key): row for row in rows}


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _customer_profile_doc(
    customer: dict[str, Any],
    *,
    periods: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    stations: dict[Any, dict[str, Any]],
    products: dict[Any, dict[str, Any]],
    db_id: str,
) -> dict[str, Any]:
    customer_id = customer.get("CustomerID")
    event_docs = [
        _transaction_event_doc(
            transaction,
            station=stations.get(transaction.get("GasStationID")),
            product=products.get(transaction.get("ProductID")),
        )
        for transaction in transactions
    ]
    return {
        "_id": f"customer:{customer_id}",
        "identity": {
            "source_db": db_id,
            "customer_id": customer_id,
            "segment": {
                "value": customer.get("Segment"),
                "state": _presence_state(customer.get("Segment")),
            },
            "currency": {
                "value": customer.get("Currency"),
                "state": _presence_state(customer.get("Currency")),
            },
            "customer_tags": _customer_tags(customer, transactions),
        },
        "spend": {
            "customer_id": customer_id,
            "total_consumption": round(sum(float(row.get("Consumption") or 0.0) for row in periods), 4),
            "consumption_by_month": _consumption_by_month(periods, event_docs),
            "consumption_by_year": _consumption_by_year(periods),
        },
        "transactions": {
            "schema_state": _presence_state(event_docs),
            "events": event_docs,
            "basket_by_date": _basket_by_date(event_docs),
            "station_buckets_by_station_id": _station_buckets(event_docs),
        },
        "market_context": {
            "merchant_countries_by_country": _merchant_countries(event_docs),
            "station_segments_by_segment": _station_segments(event_docs),
        },
        "schema_state": {
            "monthly_consumption": _presence_state(periods),
            "transaction_events": _presence_state(event_docs),
            "customer_segment": _presence_state(customer.get("Segment")),
            "external_loyalty_tier": "missing",
        },
        "_provenance": {
            "source_tables": ["customers", "yearmonth", "transactions_1k", "gasstations", "products"],
            "source_keys": {"CustomerID": customer_id},
        },
    }


def _transaction_event_doc(
    transaction: dict[str, Any],
    *,
    station: dict[str, Any] | None,
    product: dict[str, Any] | None,
) -> dict[str, Any]:
    amount = transaction.get("Amount")
    price = transaction.get("Price")
    product_category = _product_category((product or {}).get("Description"))
    return {
        "transaction_id": transaction.get("TransactionID"),
        "occurred_at": {
            "date": transaction.get("Date"),
            "time": transaction.get("Time"),
            "month": _month_key(transaction.get("Date")),
            "date_state": _presence_state(transaction.get("Date")),
            "time_state": _presence_state(transaction.get("Time")),
        },
        "card": {
            "card_id": transaction.get("CardID"),
            "state": _presence_state(transaction.get("CardID")),
        },
        "merchant_context": {
            "station_id": transaction.get("GasStationID"),
            "station": _station_snapshot(station),
            "country_bucket": _safe_key((station or {}).get("Country"), "unknown_country"),
            "segment_bucket": _safe_key((station or {}).get("Segment"), "unknown_segment"),
        },
        "basket": {
            "product_id": transaction.get("ProductID"),
            "product": _product_snapshot(product),
            "category": product_category,
            "amount": amount,
            "amount_state": _presence_state(amount),
        },
        "payment": {
            "amount": amount,
            "unit_price": price,
            "gross_value": _gross_value(amount, price),
            "amount_state": _presence_state(amount),
            "price_state": _presence_state(price),
        },
        "product_mix_by_category": {
            product_category: {
                "schema_state": _presence_state(product),
                "items": [
                    {
                        "product_id": transaction.get("ProductID"),
                        "description": (product or {}).get("Description"),
                        "payment": {
                            "amount": amount,
                            "unit_price": price,
                            "gross_value": _gross_value(amount, price),
                        },
                    }
                ],
            }
        },
    }


def _consumption_by_month(
    periods: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    event_totals: dict[str, dict[str, float | int]] = defaultdict(lambda: {"amount": 0.0, "gross": 0.0, "count": 0})
    for event in events:
        month = event["occurred_at"]["month"]
        event_totals[month]["amount"] = float(event_totals[month]["amount"]) + float(event["payment"].get("amount") or 0.0)
        event_totals[month]["gross"] = float(event_totals[month]["gross"]) + float(event["payment"].get("gross_value") or 0.0)
        event_totals[month]["count"] = int(event_totals[month]["count"]) + 1

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in periods:
        grouped[_yearmonth_key(row.get("Date"))].append(row)
    return {
        month: {
            "schema_state": _presence_state(rows),
            "year": month[:4],
            "month_number": month[5:7],
            "consumption_total": round(sum(float(row.get("Consumption") or 0.0) for row in rows), 4),
            "transaction_amount_total": round(float(event_totals.get(month, {}).get("amount") or 0.0), 4),
            "transaction_gross_total": round(float(event_totals.get(month, {}).get("gross") or 0.0), 4),
            "transaction_count": int(event_totals.get(month, {}).get("count") or 0),
            "periods": [
                {
                    "source_period": row.get("Date"),
                    "consumption": row.get("Consumption"),
                    "consumption_state": _presence_state(row.get("Consumption")),
                    "trend_context": {
                        "period_key": _yearmonth_key(row.get("Date")),
                        "has_pos_sample": "present" if int(event_totals.get(month, {}).get("count") or 0) else "empty",
                    },
                }
                for row in rows
            ],
        }
        for month, rows in sorted(grouped.items())
    }


def _consumption_by_year(periods: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in periods:
        grouped[_yearmonth_key(row.get("Date"))[:4]].append(row)
    return {
        year: {
            "schema_state": _presence_state(rows),
            "period_count": len(rows),
            "consumption_total": round(sum(float(row.get("Consumption") or 0.0) for row in rows), 4),
            "months": [
                {
                    "month": _yearmonth_key(row.get("Date")),
                    "consumption": row.get("Consumption"),
                    "state": _presence_state(row.get("Consumption")),
                }
                for row in rows
            ],
        }
        for year, rows in sorted(grouped.items())
    }


def _basket_by_date(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event["occurred_at"].get("date"), "unknown_date")].append(event)
    return {
        date: {
            "schema_state": _presence_state(items),
            "payment_total": round(sum(float(item["payment"].get("gross_value") or 0.0) for item in items), 4),
            "amount_total": round(sum(float(item["payment"].get("amount") or 0.0) for item in items), 4),
            "payments": [
                {
                    "transaction_id": item["transaction_id"],
                    "time": item["occurred_at"].get("time"),
                    "station": item["merchant_context"]["station"],
                    "product": item["basket"]["product"],
                    "payment": item["payment"],
                }
                for item in items
            ],
        }
        for date, items in sorted(grouped.items())
    }


def _station_buckets(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event["merchant_context"].get("station_id"), "unknown_station")].append(event)
    return {
        station_id: {
            "schema_state": _presence_state(items),
            "visit_count": len(items),
            "gross_total": round(sum(float(item["payment"].get("gross_value") or 0.0) for item in items), 4),
            "events": [
                {
                    "transaction_id": item["transaction_id"],
                    "date": item["occurred_at"]["date"],
                    "product_category": item["basket"]["category"],
                    "payment": item["payment"],
                }
                for item in items
            ],
        }
        for station_id, items in sorted(grouped.items())
    }


def _merchant_countries(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["merchant_context"]["country_bucket"]].append(event)
    return {
        country: {
            "schema_state": _presence_state(items),
            "station_ids": sorted({item["merchant_context"]["station_id"] for item in items}),
            "gross_total": round(sum(float(item["payment"].get("gross_value") or 0.0) for item in items), 4),
        }
        for country, items in sorted(grouped.items())
    }


def _station_segments(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["merchant_context"]["segment_bucket"]].append(event)
    return {
        segment: {
            "schema_state": _presence_state(items),
            "visit_count": len(items),
            "products_by_category": _products_by_category(items),
        }
        for segment, items in sorted(grouped.items())
    }


def _station_catalog_doc(
    station: dict[str, Any],
    *,
    transactions: list[dict[str, Any]],
    products: dict[Any, dict[str, Any]],
    db_id: str,
) -> dict[str, Any]:
    event_docs = [
        _station_transaction_doc(transaction, product=products.get(transaction.get("ProductID")))
        for transaction in transactions
    ]
    return {
        "_id": f"station:{station.get('GasStationID')}",
        "identity": {
            "source_db": db_id,
            "station_id": station.get("GasStationID"),
            "chain_id": station.get("ChainID"),
            "chain_state": _presence_state(station.get("ChainID")),
        },
        "merchant_dimension": {
            "country": station.get("Country"),
            "country_state": _presence_state(station.get("Country")),
            "segment": station.get("Segment"),
            "segment_state": _presence_state(station.get("Segment")),
            "regional_key": _safe_key(station.get("Country"), "unknown_country"),
            "chain_segment_key": f"{_safe_key(station.get('ChainID'), 'chain_unknown')}::{_safe_key(station.get('Segment'), 'segment_unknown')}",
        },
        "transactions_by_date": _station_transactions_by_date(event_docs),
        "product_mix_by_category": _products_by_category(event_docs),
        "customers_by_segment": {
            "unknown_customer_segment": [
                {
                    "customer_id": item["customer_id"],
                    "transaction_id": item["transaction_id"],
                    "date": item["date"],
                    "payment": item["payment"],
                }
                for item in event_docs
            ]
        },
        "schema_state": {
            "transactions": _presence_state(event_docs),
            "chain_id": _presence_state(station.get("ChainID")),
            "external_merchant_rating": "missing",
        },
        "_provenance": {
            "source_tables": ["gasstations", "transactions_1k", "products"],
            "source_keys": {"GasStationID": station.get("GasStationID")},
        },
    }


def _station_transaction_doc(
    transaction: dict[str, Any],
    *,
    product: dict[str, Any] | None,
) -> dict[str, Any]:
    category = _product_category((product or {}).get("Description"))
    return {
        "transaction_id": transaction.get("TransactionID"),
        "date": transaction.get("Date"),
        "time": transaction.get("Time"),
        "customer_id": transaction.get("CustomerID"),
        "card_id": transaction.get("CardID"),
        "product": _product_snapshot(product),
        "category": category,
        "payment": {
            "amount": transaction.get("Amount"),
            "unit_price": transaction.get("Price"),
            "gross_value": _gross_value(transaction.get("Amount"), transaction.get("Price")),
            "state": _presence_state(transaction.get("Price")),
        },
    }


def _station_transactions_by_date(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event.get("date"), "unknown_date")].append(event)
    return {
        date: {
            "schema_state": _presence_state(items),
            "transaction_count": len(items),
            "payments": [
                {
                    "transaction_id": item["transaction_id"],
                    "customer_id": item["customer_id"],
                    "card_id": item["card_id"],
                    "product": item["product"],
                    "payment": item["payment"],
                }
                for item in items
            ],
        }
        for date, items in sorted(grouped.items())
    }


def _product_timeline_doc(
    product: dict[str, Any],
    *,
    transactions: list[dict[str, Any]],
    stations: dict[Any, dict[str, Any]],
    db_id: str,
) -> dict[str, Any]:
    event_docs = [
        _product_transaction_doc(transaction, station=stations.get(transaction.get("GasStationID")))
        for transaction in transactions
    ]
    return {
        "_id": f"product:{product.get('ProductID')}",
        "identity": {
            "source_db": db_id,
            "product_id": product.get("ProductID"),
            "description": product.get("Description"),
            "description_state": _presence_state(product.get("Description")),
            "category": _product_category(product.get("Description")),
        },
        "payment_timeline": {
            "events": event_docs,
            "by_date": _product_events_by_date(event_docs),
            "by_station_id": _product_events_by_station(event_docs),
        },
        "market_reach": {
            "countries_by_country": _product_country_reach(event_docs),
            "station_segments_by_segment": _product_segment_reach(event_docs),
        },
        "schema_state": {
            "transactions": _presence_state(event_docs),
            "description": _presence_state(product.get("Description")),
            "external_product_taxonomy": "missing",
        },
        "_provenance": {
            "source_tables": ["products", "transactions_1k", "gasstations"],
            "source_keys": {"ProductID": product.get("ProductID")},
        },
    }


def _product_transaction_doc(
    transaction: dict[str, Any],
    *,
    station: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "transaction_id": transaction.get("TransactionID"),
        "customer_id": transaction.get("CustomerID"),
        "card_id": transaction.get("CardID"),
        "date": transaction.get("Date"),
        "time": transaction.get("Time"),
        "station": _station_snapshot(station),
        "payment": {
            "amount": transaction.get("Amount"),
            "unit_price": transaction.get("Price"),
            "gross_value": _gross_value(transaction.get("Amount"), transaction.get("Price")),
            "amount_state": _presence_state(transaction.get("Amount")),
            "price_state": _presence_state(transaction.get("Price")),
        },
    }


def _product_events_by_date(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event.get("date"), "unknown_date")].append(event)
    return {
        date: {
            "schema_state": _presence_state(items),
            "station_buckets_by_station_id": _product_events_by_station(items),
            "payments": [
                {
                    "transaction_id": item["transaction_id"],
                    "customer_id": item["customer_id"],
                    "station": item["station"],
                    "payment": item["payment"],
                }
                for item in items
            ],
        }
        for date, items in sorted(grouped.items())
    }


def _product_events_by_station(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event["station"].get("station_id"), "unknown_station")].append(event)
    return {
        station_id: [
            {
                "transaction_id": item["transaction_id"],
                "date": item["date"],
                "customer_id": item["customer_id"],
                "payment": item["payment"],
                "station": item["station"],
            }
            for item in items
        ]
        for station_id, items in sorted(grouped.items())
    }


def _product_country_reach(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event["station"].get("country"), "unknown_country")].append(event)
    return {
        country: {
            "schema_state": _presence_state(items),
            "station_count": len({item["station"].get("station_id") for item in items}),
            "gross_total": round(sum(float(item["payment"].get("gross_value") or 0.0) for item in items), 4),
        }
        for country, items in sorted(grouped.items())
    }


def _product_segment_reach(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event["station"].get("segment"), "unknown_segment")].append(event)
    return {
        segment: {
            "schema_state": _presence_state(items),
            "transaction_count": len(items),
            "station_buckets_by_station_id": _product_events_by_station(items),
        }
        for segment, items in sorted(grouped.items())
    }


def _products_by_category(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[_safe_key(event.get("category") or event.get("basket", {}).get("category"), "unknown_category")].append(event)
    return {
        category: {
            "schema_state": _presence_state(items),
            "transaction_count": len(items),
            "gross_total": round(
                sum(float((item.get("payment") or {}).get("gross_value") or 0.0) for item in items),
                4,
            ),
            "items": [
                {
                    "transaction_id": item.get("transaction_id"),
                    "product": item.get("product") or item.get("basket", {}).get("product"),
                    "payment": item.get("payment"),
                }
                for item in items
            ],
        }
        for category, items in sorted(grouped.items())
    }


def _station_snapshot(station: dict[str, Any] | None) -> dict[str, Any]:
    if station is None:
        return {"state": "missing"}
    return {
        "station_id": station.get("GasStationID"),
        "chain_id": station.get("ChainID"),
        "country": station.get("Country"),
        "segment": station.get("Segment"),
        "state": "present",
    }


def _product_snapshot(product: dict[str, Any] | None) -> dict[str, Any]:
    if product is None:
        return {"state": "missing"}
    return {
        "product_id": product.get("ProductID"),
        "description": product.get("Description"),
        "category": _product_category(product.get("Description")),
        "state": "present",
    }


def _customer_tags(customer: dict[str, Any], transactions: list[dict[str, Any]]) -> list[str]:
    tags: list[str] = []
    if customer.get("Currency"):
        tags.append(f"currency:{customer['Currency']}")
    if customer.get("Segment"):
        tags.append(f"segment:{customer['Segment']}")
    if transactions:
        tags.append("has_pos_sample")
    return tags


def _product_category(description: Any) -> str:
    text = str(description or "").lower()
    if "nafta" in text or "diesel" in text:
        return "diesel"
    if "natural" in text or "special" in text or "benz" in text:
        return "petrol"
    if "ruc" in text or "zad" in text or "manual" in text:
        return "manual_entry"
    if "myt" in text or "wash" in text:
        return "service"
    if not text:
        return "unknown_product"
    return _safe_key(text.split()[0], "other_product")


def _gross_value(amount: Any, price: Any) -> float | None:
    if amount is None or price is None:
        return None
    try:
        return round(float(amount) * float(price), 4)
    except (TypeError, ValueError):
        return None


def _yearmonth_key(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 6 and text[:6].isdigit():
        return f"{text[:4]}-{text[4:6]}"
    return _month_key(value)


def _month_key(value: Any) -> str:
    text = str(value or "")
    if len(text) >= 7 and text[:4].isdigit():
        return text[:7]
    return "unknown-month"


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
    text = re.sub(r"\s+", "_", text.strip())
    for old, new in [("/", "_"), (".", "_"), ("$", "S"), ("::", "_")]:
        text = text.replace(old, new)
    return text or fallback


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="debit_card_specializing",
        features=[
            NativeFeature(
                id="fuel_customer_spend_profiles.consumption_by_month",
                type="dynamic_key_object",
                collection="fuel_customer_spend_profiles",
                field="spend.consumption_by_month",
                query_patterns=["fuel_month_consumption_profile"],
                required_constructs=["$objectToArray", "$unwind", "$group", "$sum", "$ifNull"],
                provenance_refs=["yearmonth.Date", "yearmonth.Consumption", "customers.CustomerID"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "fuel_month_consumption_profile",
                            "intent": "roll up customer consumption from month-keyed fuel spend buckets",
                            "pipeline": [
                                {
                                    "$project": {
                                        "customer_id": "$identity.customer_id",
                                        "segment": "$identity.segment.value",
                                        "months": {"$objectToArray": {"$ifNull": ["$spend.consumption_by_month", {}]}},
                                    }
                                },
                                {"$unwind": "$months"},
                                {"$unwind": "$months.v.periods"},
                                {
                                    "$group": {
                                        "_id": {
                                            "month": "$months.k",
                                            "segment": {"$ifNull": ["$segment", "unknown_segment"]},
                                        },
                                        "consumption_total": {"$sum": "$months.v.periods.consumption"},
                                        "customer_count": {"$sum": 1},
                                    }
                                },
                                {"$sort": {"_id.month": 1, "consumption_total": -1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$ifNull", "$unwind", "$group", "$sum"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="fuel_customer_spend_profiles.product_mix_by_category",
                type="nested_event_stream",
                collection="fuel_customer_spend_profiles",
                field="transactions.events.product_mix_by_category",
                query_patterns=["fuel_basket_category_dispatch"],
                required_constructs=["$filter", "$objectToArray", "$unwind", "$switch", "$ifNull"],
                provenance_refs=["transactions_1k.ProductID", "products.Description", "transactions_1k.Price"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "fuel_basket_category_dispatch",
                            "intent": "filter transaction events by dynamic product-category buckets inside each basket",
                            "pipeline": [
                                {"$unwind": "$transactions.events"},
                                {
                                    "$project": {
                                        "customer_id": "$identity.customer_id",
                                        "event": "$transactions.events",
                                        "category_buckets": {
                                            "$objectToArray": {
                                                "$ifNull": [
                                                    "$transactions.events.product_mix_by_category",
                                                    {},
                                                ]
                                            }
                                        },
                                    }
                                },
                                {
                                    "$addFields": {
                                        "fuel_class": {
                                            "$switch": {
                                                "branches": [
                                                    {"case": {"$eq": ["$event.basket.category", "diesel"]}, "then": "liquid_fuel"},
                                                    {"case": {"$eq": ["$event.basket.category", "petrol"]}, "then": "liquid_fuel"},
                                                    {"case": {"$eq": ["$event.basket.category", "service"]}, "then": "non_fuel_service"},
                                                ],
                                                "default": "other_basket",
                                            }
                                        }
                                    }
                                },
                                {
                                    "$addFields": {
                                        "native_matching_dynamic_keys": {
                                            "$filter": {
                                                "input": "$category_buckets",
                                                "as": "bucket",
                                                "cond": {"$gt": [{"$size": {"$ifNull": ["$$bucket.v.items", []]}}, 0]},
                                            }
                                        }
                                    }
                                },
                                {"$match": {"$expr": {"$gt": [{"$size": "$native_matching_dynamic_keys"}, 0]}}},
                                {
                                    "$group": {
                                        "_id": {"category": "$event.basket.category", "fuel_class": "$fuel_class"},
                                        "gross_total": {"$sum": "$event.payment.gross_value"},
                                        "transaction_count": {"$sum": 1},
                                    }
                                },
                                {"$sort": {"gross_total": -1, "_id.category": 1}},
                                {"$limit": 25},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$ifNull", "$filter", "$switch", "$size", "$group"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="fuel_station_market_catalog.station_transactions_by_date",
                type="dynamic_key_object",
                collection="fuel_station_market_catalog",
                field="transactions_by_date",
                query_patterns=["station_market_day_payment_rollup"],
                required_constructs=["$objectToArray", "$unwind", "$group", "$sum", "$ifNull"],
                provenance_refs=["gasstations.GasStationID", "gasstations.Country", "transactions_1k.Date", "transactions_1k.Price"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "station_market_day_payment_rollup",
                            "intent": "aggregate station payments through date-keyed merchant timelines",
                            "pipeline": [
                                {
                                    "$project": {
                                        "station_id": "$identity.station_id",
                                        "country": "$merchant_dimension.country",
                                        "days": {"$objectToArray": {"$ifNull": ["$transactions_by_date", {}]}},
                                    }
                                },
                                {"$unwind": "$days"},
                                {"$unwind": "$days.v.payments"},
                                {
                                    "$group": {
                                        "_id": {
                                            "country": {"$ifNull": ["$country", "unknown_country"]},
                                            "date": "$days.k",
                                        },
                                        "gross_total": {"$sum": "$days.v.payments.payment.gross_value"},
                                        "station_count": {"$addToSet": "$station_id"},
                                    }
                                },
                                {
                                    "$project": {
                                        "_id": 0,
                                        "country": "$_id.country",
                                        "date": "$_id.date",
                                        "gross_total": 1,
                                        "station_count": {"$size": "$station_count"},
                                    }
                                },
                                {"$sort": {"gross_total": -1, "country": 1, "date": 1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$ifNull", "$unwind", "$group", "$sum", "$size"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="fuel_product_payment_timeline.product_day_station_buckets",
                type="dynamic_key_object",
                collection="fuel_product_payment_timeline",
                field="payment_timeline.by_date",
                query_patterns=["product_day_station_bucket_matrix"],
                required_constructs=["$objectToArray", "$unwind", "$group", "$sum", "$filter", "$ifNull"],
                provenance_refs=["products.ProductID", "transactions_1k.Date", "transactions_1k.GasStationID", "transactions_1k.Amount"],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "product_day_station_bucket_matrix",
                            "intent": "traverse product date buckets and nested station-id buckets for payment timelines",
                            "pipeline": [
                                {
                                    "$project": {
                                        "product_id": "$identity.product_id",
                                        "category": "$identity.category",
                                        "days": {"$objectToArray": {"$ifNull": ["$payment_timeline.by_date", {}]}},
                                    }
                                },
                                {"$unwind": "$days"},
                                {
                                    "$project": {
                                        "product_id": 1,
                                        "category": 1,
                                        "date": "$days.k",
                                        "stations": {
                                            "$objectToArray": {
                                                "$ifNull": [
                                                    "$days.v.station_buckets_by_station_id",
                                                    {},
                                                ]
                                            }
                                        },
                                    }
                                },
                                {
                                    "$addFields": {
                                        "active_stations": {
                                            "$filter": {
                                                "input": "$stations",
                                                "as": "station",
                                                "cond": {"$gt": [{"$size": {"$ifNull": ["$$station.v", []]}}, 0]},
                                            }
                                        }
                                    }
                                },
                                {"$unwind": "$active_stations"},
                                {"$unwind": "$active_stations.v"},
                                {
                                    "$group": {
                                        "_id": {
                                            "category": "$category",
                                            "date": "$date",
                                            "station_id": "$active_stations.k",
                                        },
                                        "amount_total": {"$sum": "$active_stations.v.payment.amount"},
                                        "gross_total": {"$sum": "$active_stations.v.payment.gross_value"},
                                        "transaction_count": {"$sum": 1},
                                    }
                                },
                                {"$sort": {"gross_total": -1, "_id.category": 1, "_id.date": 1}},
                                {"$limit": 50},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$ifNull", "$filter", "$size", "$unwind", "$group", "$sum"],
                        }
                    ]
                },
            ),
        ],
    )


def _provenance(manifest: NativeFeatureManifest) -> dict[str, Any]:
    return {
        feature.id: {
            "module": MODULE_REF,
            "field": feature.field,
            "source_tables": sorted(
                {ref.split(".", 1)[0] for ref in feature.provenance_refs if "." in ref}
            ),
            "source_columns": list(feature.provenance_refs),
        }
        for feature in manifest.features
    }
