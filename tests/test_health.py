from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard import build_dashboard_data
from app.db import Base
from app.health import create_health_app
from app.repository import add_intake, create_product_version, ensure_user
from app.schemas import ProductCandidate


async def test_health_endpoint_checks_database() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=create_health_app(sessions))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    await engine.dispose()


async def test_dashboard_page_and_api_are_private_owner_views() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as session:
        user = await ensure_user(session, 4321, "Asia/Seoul")
        user.kcal_goal = Decimal("2000")
        user.carb_goal = Decimal("250")
        user.protein_goal = Decimal("120")
        user.fat_goal = Decimal("60")
        version = await create_product_version(
            session,
            ProductCandidate(
                barcode="4900000000001",
                name="현미 주먹밥",
                brand="테스트 브랜드",
                basis_amount=Decimal("1"),
                basis_unit="piece",
                basis_text="1개(120g)당",
                label_market="KR",
                label_language="ko",
                kcal=Decimal("280"),
                carbs_g=Decimal("52"),
                protein_g=Decimal("7"),
                fat_g=Decimal("5"),
                source="ai_label",
            ),
            owner_id=4321,
        )
        await add_intake(
            session,
            user_id=4321,
            version=version,
            multiplier=Decimal("1"),
            input_amount=Decimal("1"),
            input_unit="piece",
            consumed_at=datetime(2026, 8, 30, 1, 30, tzinfo=UTC),
        )
        voided = await add_intake(
            session,
            user_id=4321,
            version=version,
            multiplier=Decimal("1"),
            consumed_at=datetime(2026, 8, 30, 2, 0, tzinfo=UTC),
        )
        voided.voided_at = datetime(2026, 8, 30, 2, 5, tzinfo=UTC)
        await session.commit()

        data = await build_dashboard_data(
            session,
            4321,
            now=datetime(2026, 8, 30, 3, 0, tzinfo=UTC),
        )

    assert data["timezone"] == "Asia/Seoul"
    assert data["has_user"] is True
    assert data["today"] == {
        "date": "2026-08-30",
        "totals": {"kcal": 280.0, "carbs_g": 52.0, "protein_g": 7.0, "fat_g": 5.0},
        "goals": {"kcal": 2000.0, "carbs_g": 250.0, "protein_g": 120.0, "fat_g": 60.0},
        "item_count": 1,
    }
    assert len(data["days"]) == 30
    assert len(data["recent"]) == 1
    assert data["recent"][0]["amount"] == "1개"
    assert data["recent"][0]["nutrition"]["source"] == "ai_label"
    assert "telegram" not in str(data).lower()

    app = create_health_app(sessions, owner_telegram_id=4321)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/", follow_redirects=False)
        page = await client.get("/dashboard")
        api = await client.get("/api/dashboard")

    assert root.status_code == 307
    assert root.headers["location"] == "/dashboard"
    assert page.status_code == 200
    assert "오늘, 잘 먹고 있나요?" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"
    assert api.status_code == 200
    assert api.json()["recent"][0]["name"] == "현미 주먹밥"
    await engine.dispose()


async def test_dashboard_api_requires_configured_owner() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=create_health_app(sessions))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/dashboard")

    assert response.status_code == 503
    await engine.dispose()
