from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _add_compatibility_columns(connection: Connection) -> None:
    inspector = inspect(connection)
    additions = {
        "products": {
            "external_source": "VARCHAR(32)",
            "external_id": "VARCHAR(80)",
        },
        "product_versions": {
            "piece_count": "NUMERIC(12, 4)",
            "label_market": "VARCHAR(16) DEFAULT 'UNKNOWN'",
            "label_language": "VARCHAR(16) DEFAULT 'unknown'",
            "basis_text": "TEXT",
            "basis_metric_amount": "NUMERIC(12, 4)",
            "basis_metric_unit": "VARCHAR(24)",
            "basis_count_amount": "NUMERIC(12, 4)",
            "basis_count_unit": "VARCHAR(32)",
            "sodium_mg": "NUMERIC(12, 4)",
            "salt_equivalent_g": "NUMERIC(12, 4)",
            "sodium_derived": "BOOLEAN DEFAULT FALSE",
            "salt_equivalent_derived": "BOOLEAN DEFAULT FALSE",
            "estimated_values": "BOOLEAN DEFAULT FALSE",
        },
        "intake_logs": {
            "input_amount": "NUMERIC(12, 4)",
            "input_unit": "VARCHAR(24)",
        },
    }
    for table_name, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, column_type in columns.items():
            if column_name not in existing:
                connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                )
    connection.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_product_external_source_id "
            "ON products (external_source, external_id)"
        )
    )


def _backfill_legacy_package_amounts(connection: Connection) -> None:
    from app.portion import PortionError, parse_portion

    rows = connection.execute(
        text(
            "SELECT id, raw_data FROM product_versions "
            "WHERE package_amount IS NULL AND raw_data IS NOT NULL"
        )
    ).mappings()
    for row in rows:
        raw_data = row["raw_data"]
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except json.JSONDecodeError:
                continue
        if not isinstance(raw_data, dict) or not isinstance(raw_data.get("quantity"), str):
            continue
        try:
            portion = parse_portion(raw_data["quantity"])
        except PortionError:
            continue
        if portion.unit not in {"g", "ml"}:
            continue
        connection.execute(
            text(
                "UPDATE product_versions "
                "SET package_amount = CAST(:amount AS NUMERIC(12, 4)), package_unit = :unit "
                "WHERE id = :version_id"
            ),
            {"amount": str(portion.amount), "unit": portion.unit, "version_id": row["id"]},
        )


def _backfill_product_search_terms(connection: Connection) -> None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    if not {"products", "product_versions", "product_search_terms"} <= table_names:
        return
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    version_columns = {column["name"] for column in inspector.get_columns("product_versions")}
    if not {"id", "name", "brand", "source"} <= product_columns:
        return
    if not {"product_id", "is_current", "raw_data"} <= version_columns:
        return

    from app.models import ProductSearchTerm
    from app.search_tags import build_product_search_terms

    rows = connection.execute(
        text(
            "SELECT p.id, p.name, p.brand, p.source, pv.raw_data "
            "FROM products p "
            "LEFT JOIN product_versions pv "
            "ON pv.product_id = p.id AND pv.is_current = TRUE "
            "WHERE NOT EXISTS ("
            "SELECT 1 FROM product_search_terms pst WHERE pst.product_id = p.id)"
        )
    ).mappings()
    seen_product_ids: set[int] = set()
    for row in rows:
        product_id = int(row["id"])
        if product_id in seen_product_ids:
            continue
        seen_product_ids.add(product_id)
        raw_data = row["raw_data"]
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except json.JSONDecodeError:
                raw_data = None
        search_concepts = raw_data.get("search_concepts", []) if isinstance(raw_data, dict) else []
        search_terms_ko = raw_data.get("search_terms_ko", []) if isinstance(raw_data, dict) else []
        search_terms_ja = raw_data.get("search_terms_ja", []) if isinstance(raw_data, dict) else []
        specs = build_product_search_terms(
            name=str(row["name"]),
            brand=str(row["brand"]) if row["brand"] else None,
            raw_data=raw_data if isinstance(raw_data, dict) else None,
            search_concepts=search_concepts if isinstance(search_concepts, list) else [],
            search_terms_ko=search_terms_ko if isinstance(search_terms_ko, list) else [],
            search_terms_ja=search_terms_ja if isinstance(search_terms_ja, list) else [],
            product_source=str(row["source"]),
        )
        values = [
            {
                "product_id": product_id,
                "term": spec.term,
                "normalized_term": spec.normalized_term,
                "locale": spec.locale,
                "kind": spec.kind,
                "source": spec.source,
                "concept_key": spec.concept_key,
                "confidence": spec.confidence,
            }
            for spec in specs
        ]
        if values:
            connection.execute(ProductSearchTerm.__table__.insert(), values)


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        from app import models  # noqa: F401

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(_add_compatibility_columns)
            await connection.run_sync(_backfill_legacy_package_amounts)
            await connection.run_sync(_backfill_product_search_terms)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
