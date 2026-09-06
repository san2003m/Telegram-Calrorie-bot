from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dashboard import DASHBOARD_HTML, build_dashboard_data
from app.models import User
from app.portion import PortionError
from app.repository import (
    add_intake,
    create_product_version,
    ensure_user,
    get_intake_by_client_request_id,
    get_intake_log,
    get_product_version,
    search_saved_products,
    set_goals,
    set_intake_voided,
    update_intake,
)
from app.schemas import ProductCandidate
from app.web_api import (
    GoalUpdate,
    IntakeCreate,
    IntakeUpdate,
    ManualIntakeCreate,
    normalize_consumed_at,
    serialize_mutation_log,
    serialize_product,
    user_timezone,
    validated_portion,
)
from app.web_auth import (
    AccessIdentity,
    AccessTokenVerifier,
    AccessVerificationError,
    CloudflareAccessVerifier,
)

logger = logging.getLogger(__name__)

CSRF_COOKIE = "calorie_csrf"
CSRF_HEADER = "x-csrf-token"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin", "")
    host = request.headers.get("host", "")
    if not origin or not host:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == host.casefold()


def _web_error(message: str, *, status_code: int = 422) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def create_health_app(
    sessions: async_sessionmaker[AsyncSession],
    *,
    owner_telegram_id: int = 0,
    app_timezone: str = "Asia/Seoul",
    dashboard_writes_enabled: bool = False,
    cloudflare_access_team_domain: str = "",
    cloudflare_access_aud: str = "",
    cloudflare_access_allowed_email: str = "",
    access_verifier: AccessTokenVerifier | None = None,
    csrf_secure_cookie: bool = True,
) -> FastAPI:
    app = FastAPI(title="Calorie Bot Dashboard", docs_url=None, redoc_url=None)

    verifier = access_verifier
    if dashboard_writes_enabled and verifier is None:
        try:
            verifier = CloudflareAccessVerifier(
                team_domain=cloudflare_access_team_domain,
                audience=cloudflare_access_aud,
                allowed_email=cloudflare_access_allowed_email,
            )
        except ValueError as exc:
            logger.error("Dashboard writes remain locked: %s", exc)

    async def require_access(request: Request) -> AccessIdentity:
        if not dashboard_writes_enabled:
            raise _web_error("웹 입력 기능이 비활성화되어 있습니다.", status_code=503)
        if owner_telegram_id <= 0 or verifier is None:
            raise _web_error("웹 입력 보안 설정이 완료되지 않았습니다.", status_code=503)
        token = request.headers.get("cf-access-jwt-assertion", "")
        try:
            return await verifier.verify(token)
        except AccessVerificationError as exc:
            raise _web_error(str(exc), status_code=401) from exc

    async def require_mutation(request: Request) -> AccessIdentity:
        identity = await require_access(request)
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        header_token = request.headers.get(CSRF_HEADER, "")
        if (
            not _same_origin(request)
            or not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
        ):
            raise _web_error(
                "요청 보안 토큰이 만료되었습니다. 페이지를 새로고침해 주세요.",
                status_code=403,
            )
        return identity

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def dashboard_root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard", status_code=307)

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/api/dashboard")
    async def dashboard_data() -> dict[str, object]:
        if owner_telegram_id <= 0:
            raise HTTPException(status_code=503, detail="dashboard owner is not configured")
        async with sessions() as session:
            return await build_dashboard_data(
                session,
                owner_telegram_id,
                fallback_timezone=app_timezone,
            )

    @app.get("/api/web/session")
    async def web_session(request: Request) -> JSONResponse:
        await require_access(request)
        csrf_token = secrets.token_urlsafe(32)
        response = JSONResponse({"writable": True, "csrf_token": csrf_token})
        response.set_cookie(
            CSRF_COOKIE,
            csrf_token,
            max_age=3_600,
            secure=csrf_secure_cookie,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/api/web/products")
    async def web_product_search(
        request: Request,
        query: str = Query(min_length=1, max_length=60),
    ) -> dict[str, object]:
        await require_access(request)
        clean_query = " ".join(query.split())
        if not clean_query:
            raise _web_error("검색어를 입력해 주세요.")
        async with sessions() as session:
            versions = await search_saved_products(
                session,
                owner_id=owner_telegram_id,
                query=clean_query,
                limit=12,
            )
        return {
            "query": clean_query,
            "products": [serialize_product(item) for item in versions],
        }

    @app.post("/api/web/intakes")
    async def web_add_intake(request: Request, payload: IntakeCreate) -> dict[str, object]:
        await require_mutation(request)
        request_id = str(payload.request_id)
        async with sessions() as session:
            user = await ensure_user(session, owner_telegram_id, app_timezone)
            existing = await get_intake_by_client_request_id(session, owner_telegram_id, request_id)
            if existing is not None:
                return {"created": False, "log": serialize_mutation_log(existing)}
            version = await get_product_version(
                session, payload.product_version_id, owner_telegram_id
            )
            if version is None:
                raise _web_error("상품을 찾지 못했습니다.", status_code=404)
            try:
                portion, multiplier = validated_portion(version, payload.amount, payload.unit)
                consumed_at = normalize_consumed_at(
                    payload.consumed_at,
                    user_timezone(user, app_timezone),
                )
            except (PortionError, ValueError) as exc:
                raise _web_error(str(exc)) from exc
            log = await add_intake(
                session,
                user_id=owner_telegram_id,
                version=version,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
                consumed_at=consumed_at,
                client_request_id=request_id,
            )
            await session.commit()
            return {"created": True, "log": serialize_mutation_log(log)}

    @app.post("/api/web/manual-intakes")
    async def web_add_manual_intake(
        request: Request, payload: ManualIntakeCreate
    ) -> dict[str, object]:
        await require_mutation(request)
        request_id = str(payload.request_id)
        async with sessions() as session:
            user = await ensure_user(session, owner_telegram_id, app_timezone)
            existing = await get_intake_by_client_request_id(session, owner_telegram_id, request_id)
            if existing is not None:
                return {"created": False, "log": serialize_mutation_log(existing)}
            candidate = ProductCandidate(
                name=payload.name,
                brand=payload.brand,
                basis_amount=payload.basis_amount,
                basis_unit=payload.basis_unit,
                kcal=payload.kcal,
                carbs_g=payload.carbs_g,
                protein_g=payload.protein_g,
                fat_g=payload.fat_g,
                source="manual",
                verified=True,
                raw_data={"created_via": "dashboard"},
            )
            version = await create_product_version(
                session,
                candidate,
                owner_id=owner_telegram_id,
            )
            try:
                portion, multiplier = validated_portion(
                    version,
                    payload.intake_amount,
                    payload.intake_unit,
                )
                consumed_at = normalize_consumed_at(
                    payload.consumed_at,
                    user_timezone(user, app_timezone),
                )
            except (PortionError, ValueError) as exc:
                raise _web_error(str(exc)) from exc
            log = await add_intake(
                session,
                user_id=owner_telegram_id,
                version=version,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
                consumed_at=consumed_at,
                client_request_id=request_id,
            )
            await session.commit()
            return {"created": True, "log": serialize_mutation_log(log)}

    @app.patch("/api/web/intakes/{log_id}")
    async def web_update_intake(
        log_id: int,
        request: Request,
        payload: IntakeUpdate,
    ) -> dict[str, object]:
        await require_mutation(request)
        async with sessions() as session:
            user = await session.get(User, owner_telegram_id)
            log = await get_intake_log(session, owner_telegram_id, log_id)
            if log is None:
                raise _web_error("기록을 찾지 못했습니다.", status_code=404)
            if log.voided_at is not None:
                raise _web_error("취소된 기록은 복원한 뒤 수정해 주세요.", status_code=409)
            try:
                portion, multiplier = validated_portion(
                    log.product_version,
                    payload.amount,
                    payload.unit,
                )
                consumed_at = normalize_consumed_at(
                    payload.consumed_at,
                    user_timezone(user, app_timezone),
                )
            except (PortionError, ValueError) as exc:
                raise _web_error(str(exc)) from exc
            update_intake(
                session,
                log,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
                consumed_at=consumed_at,
            )
            await session.commit()
            return {"updated": True, "log": serialize_mutation_log(log)}

    @app.post("/api/web/intakes/{log_id}/void")
    async def web_void_intake(log_id: int, request: Request) -> dict[str, object]:
        await require_mutation(request)
        async with sessions() as session:
            log = await get_intake_log(session, owner_telegram_id, log_id)
            if log is None:
                raise _web_error("기록을 찾지 못했습니다.", status_code=404)
            set_intake_voided(log, voided=True)
            await session.commit()
            return {"voided": True, "log": serialize_mutation_log(log)}

    @app.post("/api/web/intakes/{log_id}/restore")
    async def web_restore_intake(log_id: int, request: Request) -> dict[str, object]:
        await require_mutation(request)
        async with sessions() as session:
            log = await get_intake_log(session, owner_telegram_id, log_id)
            if log is None:
                raise _web_error("기록을 찾지 못했습니다.", status_code=404)
            set_intake_voided(log, voided=False)
            await session.commit()
            return {"restored": True, "log": serialize_mutation_log(log)}

    @app.patch("/api/web/goals")
    async def web_update_goals(request: Request, payload: GoalUpdate) -> dict[str, bool]:
        await require_mutation(request)
        async with sessions() as session:
            user = await ensure_user(session, owner_telegram_id, app_timezone)
            await set_goals(
                session,
                user,
                kcal=payload.kcal,
                carbs=payload.carbs_g,
                protein=payload.protein_g,
                fat=payload.fat_g,
            )
            await session.commit()
        return {"updated": True}

    return app
