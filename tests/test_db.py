from decimal import Decimal

from sqlalchemy import inspect, select, text

from app.db import Database
from app.models import Product, ProductSearchTerm, ProductVersion, User


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
        version_columns = await connection.run_sync(
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
        product_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("products")
            }
        )
        table_names = await connection.run_sync(
            lambda sync_connection: set(inspect(sync_connection).get_table_names())
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
    } <= version_columns
    assert {"input_amount", "input_unit"} <= log_columns
    assert {"external_source", "external_id"} <= product_columns
    assert "product_search_terms" in table_names
    assert Decimal(str(package_measure.package_amount)) == Decimal("500")
    assert package_measure.package_unit == "ml"


async def test_create_schema_backfills_cross_language_search_terms() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    async with database.sessions() as session:
        user = User(telegram_id=1, timezone="Asia/Seoul")
        product = Product(
            barcode="4900000000001",
            name="ゆで卵",
            source="ai_label",
            owner_telegram_id=1,
        )
        version = ProductVersion(
            product=product,
            basis_amount=Decimal("1"),
            basis_unit="piece",
            kcal=Decimal("70"),
            carbs_g=Decimal("0.2"),
            protein_g=Decimal("6"),
            fat_g=Decimal("5"),
            source="ai_label",
        )
        session.add_all([user, product, version])
        await session.commit()

    await database.create_schema()

    async with database.sessions() as session:
        terms = set(
            (
                await session.scalars(
                    select(ProductSearchTerm.term).where(ProductSearchTerm.product_id == product.id)
                )
            ).all()
        )
    await database.dispose()

    assert {"달걀", "계란", "卵", "ゆで"} <= terms
