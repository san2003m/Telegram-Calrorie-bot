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
    find_product_by_external_id,
    get_daily_summary,
    get_last_portion,
    get_menu_search_cache,
    get_or_create_catalog_product,
    get_product_version,
    get_recipe_parse_cache,
    reserve_ai_usage,
    reserve_recipe_ai_usage,
    save_menu_search_cache,
    save_recipe_parse_cache,
    search_catalog_products,
    search_recipe_products,
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
            "basis_count_amount": Decimal("1"),
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
    assert stored.basis_count_amount == Decimal("1.0000")
    assert stored.sodium_mg == Decimal("315.0000")
    assert stored.salt_equivalent_g == Decimal("0.8000")
    assert stored.sodium_derived is True
    assert stored.estimated_values is True


async def test_external_catalog_food_is_cached_and_searchable(sessions) -> None:
    public_food = candidate(barcode="").model_copy(
        update={
            "barcode": None,
            "external_source": "mfds_food_db",
            "external_id": "R000001",
            "name": "달걀, 삶은것",
            "source": "mfds_food_db",
        }
    )

    async with sessions() as session:
        first = await get_or_create_catalog_product(session, public_food)
        second = await get_or_create_catalog_product(session, public_food)
        await session.commit()
        found = await find_product_by_external_id(session, "mfds_food_db", "R000001")
        search_results = await search_catalog_products(
            session,
            source="mfds_food_db",
            terms=["달걀"],
        )

    assert second.id == first.id
    assert found is not None and found.id == first.id
    assert [result.id for result in search_results] == [first.id]


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


async def test_recipe_parse_cache_is_private_and_reused(sessions) -> None:
    payload = {
        "recipe_name": "달걀밥",
        "servings": "1",
        "ingredients": [
            {
                "raw_text": "달걀 2개",
                "name": "달걀",
                "amount": "2",
                "unit": "piece",
                "preparation": "unknown",
                "note": None,
            }
        ],
    }
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        await ensure_user(session, 2, "Asia/Seoul")
        first = await save_recipe_parse_cache(
            session,
            user_id=1,
            input_hash="a" * 64,
            parser_version="recipe-v1",
            result_json=payload,
            used_ai=True,
        )
        second = await save_recipe_parse_cache(
            session,
            user_id=1,
            input_hash="a" * 64,
            parser_version="recipe-v1",
            result_json=payload,
            used_ai=True,
        )
        await session.commit()
        other_user = await get_recipe_parse_cache(
            session,
            user_id=2,
            input_hash="a" * 64,
            parser_version="recipe-v1",
        )

    assert first.id == second.id
    assert other_user is None


async def test_recipe_ai_quota_counts_reserved_attempts(sessions) -> None:
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        first, first_error = await reserve_recipe_ai_usage(
            session,
            user_id=1,
            input_hash="a" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=1,
            monthly_limit=10,
        )
        await session.commit()
        second, second_error = await reserve_recipe_ai_usage(
            session,
            user_id=1,
            input_hash="b" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=1,
            monthly_limit=10,
        )

    assert first is not None
    assert first_error is None
    assert second is None
    assert "오늘" in (second_error or "")


async def test_menu_search_cache_is_reused_and_versioned(sessions) -> None:
    payload = {
        "found": False,
        "official_source": False,
        "brand": None,
        "menu_name": None,
        "basis_text": None,
        "basis_amount": None,
        "basis_unit": None,
        "kcal": None,
        "carbs_g": None,
        "protein_g": None,
        "fat_g": None,
        "source_url": None,
        "source_title": None,
        "evidence_text": [],
        "confidence": "0",
    }
    async with sessions() as session:
        first = await save_menu_search_cache(
            session,
            input_hash="m" * 64,
            search_version="menu-web-v1",
            query="브랜드 메뉴",
            result_json=payload,
            searched_urls=[],
        )
        second = await save_menu_search_cache(
            session,
            input_hash="m" * 64,
            search_version="menu-web-v1",
            query="브랜드 메뉴",
            result_json=payload,
            searched_urls=[],
        )
        await session.commit()
        cached = await get_menu_search_cache(
            session,
            input_hash="m" * 64,
            search_version="menu-web-v1",
            max_age_days=7,
        )
        wrong_version = await get_menu_search_cache(
            session,
            input_hash="m" * 64,
            search_version="menu-web-v2",
            max_age_days=7,
        )

    assert first.id == second.id
    assert cached is not None and cached.query == "브랜드 메뉴"
    assert wrong_version is None


async def test_ai_usage_quota_is_isolated_by_feature(sessions) -> None:
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        menu_usage, menu_error = await reserve_ai_usage(
            session,
            user_id=1,
            feature="menu_lookup",
            input_hash="m" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=1,
            monthly_limit=10,
            feature_label="AI 외식 메뉴 검색",
        )
        await session.commit()
        recipe_usage, recipe_error = await reserve_recipe_ai_usage(
            session,
            user_id=1,
            input_hash="r" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=1,
            monthly_limit=10,
        )

    assert menu_usage is not None and menu_error is None
    assert recipe_usage is not None and recipe_error is None


async def test_menu_ai_global_quota_limits_multiple_users(sessions) -> None:
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        await ensure_user(session, 2, "Asia/Seoul")
        first, first_error = await reserve_ai_usage(
            session,
            user_id=1,
            feature="menu_lookup",
            input_hash="a" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=5,
            monthly_limit=50,
            feature_label="AI 외식 메뉴 검색",
            global_daily_limit=1,
            global_monthly_limit=10,
        )
        await session.commit()
        second, second_error = await reserve_ai_usage(
            session,
            user_id=2,
            feature="menu_lookup",
            input_hash="b" * 64,
            timezone_name="Asia/Seoul",
            daily_limit=5,
            monthly_limit=50,
            feature_label="AI 외식 메뉴 검색",
            global_daily_limit=1,
            global_monthly_limit=10,
        )

    assert first is not None and first_error is None
    assert second is None
    assert "서비스 전체" in (second_error or "")


async def test_recipe_search_can_use_private_or_public_food_but_not_recipe(sessions) -> None:
    private_food = candidate().model_copy(update={"name": "비밀 달걀", "source": "manual"})
    recipe = candidate(barcode="").model_copy(
        update={"barcode": None, "name": "달걀 레시피", "source": "recipe"}
    )
    async with sessions() as session:
        await ensure_user(session, 1, "Asia/Seoul")
        await ensure_user(session, 2, "Asia/Seoul")
        private_version = await create_product_version(session, private_food, owner_id=1)
        await create_product_version(session, recipe, owner_id=1)
        await session.commit()
        owner_results = await search_recipe_products(session, owner_id=1, terms=["달걀"])
        other_results = await search_recipe_products(session, owner_id=2, terms=["달걀"])

    assert [item.id for item in owner_results] == [private_version.id]
    assert other_results == []
