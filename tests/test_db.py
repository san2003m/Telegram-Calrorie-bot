from decimal import Decimal

from sqlalchemy import inspect, text

from app.db import Database


async def test_create_schema_adds_columns_to_existing_database() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE product_versions ("
                "id INTEGER PRIMARY KEY, package_amount NUMERIC(12, 4), "
                "package_unit VARCHAR(24), raw_data JSON)"
            )
        )
        await connection.execute(text("CREATE TABLE intake_logs (id INTEGER PRIMARY KEY)"))
        await connection.execute(
            text(
                'INSERT INTO product_versions (id, raw_data) VALUES (1, \'{"quantity": "500 ml"}\')'
            )
        )

    await database.create_schema()

    async with database.engine.connect() as connection:
        product_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]
                for column in inspect(sync_connection).get_columns("product_versions")
            }
        )
        log_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("intake_logs")
            }
        )
        package_measure = (
            await connection.execute(
                text("SELECT package_amount, package_unit FROM product_versions WHERE id = 1")
            )
        ).one()
    await database.dispose()

    assert {
        "piece_count",
        "label_market",
        "label_language",
        "basis_text",
        "basis_metric_amount",
        "basis_metric_unit",
        "basis_count_amount",
        "basis_count_unit",
        "sodium_mg",
        "salt_equivalent_g",
        "sodium_derived",
        "salt_equivalent_derived",
        "estimated_values",
    } <= product_columns
    assert {"input_amount", "input_unit"} <= log_columns
    assert Decimal(str(package_measure.package_amount)) == Decimal("500")
    assert package_measure.package_unit == "ml"
