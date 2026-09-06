from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.dashboard import build_dashboard_data
from app.db import Base
from app.health import create_health_app
from app.models import IntakeLog, Product, ProductVersion, User
from app.repository import add_intake, create_product_version, ensure_user
from app.schemas import ProductCandidate
from app.web_auth import AccessIdentity, AccessVerificationError


class FakeAccessVerifier:
    async def verify(self, token: str) -> AccessIdentity:
        if token != "valid-access-token":
            raise AccessVerificationError("Access 인증을 확인하지 못했습니다.")
        return AccessIdentity(email="owner@example.com", subject="owner")


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


async def test_web_writes_are_locked_without_access_configuration() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    transport = httpx.ASGITransport(app=create_health_app(sessions, owner_telegram_id=4321))

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/api/web/session")

    assert response.status_code == 503
    await engine.dispose()


async def test_authenticated_web_crud_flow_and_idempotency() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        await ensure_user(session, 4321, "Asia/Seoul")
        version = await create_product_version(
            session,
            ProductCandidate(
                name="웹 테스트 닭가슴살",
                brand="테스트",
                basis_amount=Decimal("100"),
                basis_unit="g",
                kcal=Decimal("200"),
                carbs_g=Decimal("10"),
                protein_g=Decimal("30"),
                fat_g=Decimal("5"),
                source="manual",
            ),
            owner_id=4321,
        )
        await ensure_user(session, 9999, "Asia/Seoul")
        other_version = await create_product_version(
            session,
            ProductCandidate(
                name="다른 사용자 비공개 음식",
                basis_amount=Decimal("1"),
                basis_unit="serving",
                kcal=Decimal("500"),
                carbs_g=Decimal("50"),
                protein_g=Decimal("20"),
                fat_g=Decimal("20"),
                source="manual",
            ),
            owner_id=9999,
        )
        other_log = await add_intake(
            session,
            user_id=9999,
            version=other_version,
            multiplier=Decimal("1"),
        )
        await session.commit()

    app = create_health_app(
        sessions,
        owner_telegram_id=4321,
        dashboard_writes_enabled=True,
        access_verifier=FakeAccessVerifier(),
    )
    transport = httpx.ASGITransport(app=app)
    access_headers = {"Cf-Access-Jwt-Assertion": "valid-access-token"}
    request_id = str(uuid4())
    consumed_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        unauthenticated = await client.get("/api/web/session")
        session_response = await client.get("/api/web/session", headers=access_headers)
        csrf = session_response.json()["csrf_token"]
        mutation_headers = {
            **access_headers,
            "Origin": "https://test",
            "X-CSRF-Token": csrf,
        }
        missing_csrf = await client.post(
            "/api/web/intakes",
            headers=access_headers,
            json={
                "product_version_id": version.id,
                "amount": 50,
                "unit": "g",
                "request_id": request_id,
            },
        )
        wrong_origin = await client.patch(
            "/api/web/goals",
            headers={**mutation_headers, "Origin": "https://evil.example"},
            json={"kcal": 2100, "carbs_g": 260, "protein_g": 140, "fat_g": 65},
        )
        products = await client.get(
            "/api/web/products",
            params={"query": "닭가슴살"},
            headers=access_headers,
        )
        created = await client.post(
            "/api/web/intakes",
            headers=mutation_headers,
            json={
                "product_version_id": version.id,
                "amount": 50,
                "unit": "g",
                "consumed_at": consumed_at,
                "request_id": request_id,
            },
        )
        duplicate = await client.post(
            "/api/web/intakes",
            headers=mutation_headers,
            json={
                "product_version_id": version.id,
                "amount": 50,
                "unit": "g",
                "consumed_at": consumed_at,
                "request_id": request_id,
            },
        )
        log_id = created.json()["log"]["id"]
        updated = await client.patch(
            f"/api/web/intakes/{log_id}",
            headers=mutation_headers,
            json={"amount": 75, "unit": "g", "consumed_at": consumed_at},
        )
        voided = await client.post(
            f"/api/web/intakes/{log_id}/void",
            headers=mutation_headers,
        )
        after_void = await client.get("/api/dashboard")
        restored = await client.post(
            f"/api/web/intakes/{log_id}/restore",
            headers=mutation_headers,
        )
        goals = await client.patch(
            "/api/web/goals",
            headers=mutation_headers,
            json={"kcal": 2100, "carbs_g": 260, "protein_g": 140, "fat_g": 65},
        )
        other_user_attempt = await client.post(
            f"/api/web/intakes/{other_log.id}/void",
            headers=mutation_headers,
        )

    assert unauthenticated.status_code == 401
    assert session_response.status_code == 200
    assert session_response.cookies.get("calorie_csrf") == csrf
    assert missing_csrf.status_code == 403
    assert wrong_origin.status_code == 403
    assert products.status_code == 200
    assert products.json()["products"][0]["version_id"] == version.id
    assert created.status_code == 200
    assert created.json()["created"] is True
    assert created.json()["log"]["kcal"] == 100.0
    assert duplicate.json()["created"] is False
    assert updated.json()["log"]["kcal"] == 150.0
    assert voided.json()["log"]["voided"] is True
    assert after_void.json()["recent"] == []
    assert after_void.json()["voided_recent"][0]["id"] == log_id
    assert restored.json()["log"]["voided"] is False
    assert goals.json() == {"updated": True}
    assert other_user_attempt.status_code == 404

    async with sessions() as session:
        assert await session.scalar(select(func.count(IntakeLog.id))) == 2
        stored = await session.get(IntakeLog, log_id)
        assert stored is not None
        assert stored.input_amount == Decimal("75.0000")
        assert stored.voided_at is None
        user = await session.get(User, 4321)
        assert user is not None and user.kcal_goal == Decimal("2100.00")
        protected_log = await session.get(IntakeLog, other_log.id)
        assert protected_log is not None and protected_log.voided_at is None
    await engine.dispose()


async def test_authenticated_manual_web_entry_is_saved_as_private_product() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = create_health_app(
        sessions,
        owner_telegram_id=4321,
        dashboard_writes_enabled=True,
        access_verifier=FakeAccessVerifier(),
    )
    transport = httpx.ASGITransport(app=app)
    access_headers = {"Cf-Access-Jwt-Assertion": "valid-access-token"}

    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        session_response = await client.get("/api/web/session", headers=access_headers)
        mutation_headers = {
            **access_headers,
            "Origin": "https://test",
            "X-CSRF-Token": session_response.json()["csrf_token"],
        }
        response = await client.post(
            "/api/web/manual-intakes",
            headers=mutation_headers,
            json={
                "name": "삶은 달걀",
                "brand": "직접 조리",
                "basis_amount": 1,
                "basis_unit": "piece",
                "kcal": 75,
                "carbs_g": 0.6,
                "protein_g": 6.3,
                "fat_g": 5.3,
                "intake_amount": 2,
                "intake_unit": "piece",
                "request_id": str(uuid4()),
            },
        )

    assert response.status_code == 200
    assert response.json()["log"]["kcal"] == 150.0
    async with sessions() as session:
        stored = await session.scalar(select(IntakeLog))
        assert stored is not None
        product = await session.scalar(
            select(Product)
            .join(ProductVersion)
            .where(ProductVersion.id == stored.product_version_id)
        )
        assert product is not None
        assert product.owner_telegram_id == 4321
        assert product.name == "삶은 달걀"
    await engine.dispose()
