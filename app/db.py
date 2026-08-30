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

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
