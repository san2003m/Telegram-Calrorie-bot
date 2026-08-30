from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.repository import (
    add_intake,
    create_product_version,
    ensure_user,
    find_product_by_barcode,
    get_daily_summary,
    get_last_portion,
    get_product_version,
    undo_last_intake,
)
from app.schemas import ProductCandidate


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def candidate(barcode: str = "8801234567890") -> ProductCandidate:
    return ProductCandidate(
        barcode=barcode,
        name="테스트 식품",
        basis_amount=Decimal("100"),
        basis_unit="g",
        kcal=Decimal("200"),
        carbs_g=Decimal("20"),
        protein_g=Decimal("10"),
        fat_g=Decimal("8"),
        source="test",
    )


async def test_product_cache_and_intake_snapshot(sessions) -> None:
    async with sessions() as session:
        user = await ensure_user(session, 1234, "Asia/Seoul")
        version = await create_product_version(session, candidate(), owner_id=1234)
        log = await add_intake(
            session,
            user_id=user.telegram_id,
            version=version,
            multiplier=Decimal("1.5"),
            input_amount=Decimal("150"),
            input_unit="g",
            consumed_at=datetime(2026, 8, 29, 3, 0, tzinfo=UTC),
        )
        await session.commit()

        cached = await find_product_by_barcode(session, "8801234567890", 1234)
        summary = await get_daily_summary(
            session,
            user,
            now=datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        )
        last_portion = await get_last_portion(session, user.telegram_id, version.id)

    assert cached is not None
    assert cached.id == version.id
    assert log.kcal == Decimal("300.0")
    assert summary.item_count == 1
    assert summary.totals.protein_g == Decimal("15.0000")
    assert last_portion is not None
    assert last_portion.input_amount == Decimal("150.0000")
    assert last_portion.input_unit == "g"


async def test_private_product_does_not_leak_to_other_user(sessions) -> None:
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        await ensure_user(session, 2, "Asia/Seoul")
        private_version = await create_product_version(session, candidate(), owner_id=1)
        await session.commit()

        assert await find_product_by_barcode(session, "8801234567890", 2) is None
        assert await get_product_version(session, private_version.id, 2) is None
        assert await get_product_version(session, private_version.id, 1) is not None


async def test_country_label_metadata_is_persisted(sessions) -> None:
    japanese = candidate().model_copy(
        update={
            "label_market": "JP",
            "label_language": "ja",
            "basis_text": "1本（200ml）当たり",
            "basis_metric_amount": Decimal("200"),
            "basis_metric_unit": "ml",
            "basis_count_unit": "本",
            "sodium_mg": Decimal("315.0"),
            "salt_equivalent_g": Decimal("0.8"),
            "sodium_derived": True,
            "estimated_values": True,
        }
    )

    async with sessions() as session:
        await ensure_user(session, 1234, "Asia/Seoul")
        version = await create_product_version(session, japanese, owner_id=1234)
        await session.commit()
        stored = await get_product_version(session, version.id, 1234)

    assert stored is not None
    assert stored.label_market == "JP"
    assert stored.basis_text == "1本（200ml）当たり"
    assert stored.sodium_mg == Decimal("315.0000")
    assert stored.salt_equivalent_g == Decimal("0.8000")
    assert stored.sodium_derived is True
    assert stored.estimated_values is True


async def test_undo_marks_last_log_void(sessions) -> None:
    async with sessions() as session:
        user = await ensure_user(session, 1234, "Asia/Seoul")
        version = await create_product_version(session, candidate(), owner_id=1234)
        await add_intake(
            session,
            user_id=user.telegram_id,
            version=version,
            multiplier=Decimal("1"),
        )
        await session.commit()

        undone = await undo_last_intake(session, user.telegram_id)
        await session.commit()

        assert undone is not None
        assert undone.voided_at is not None
