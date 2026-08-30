from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dashboard import DASHBOARD_HTML, build_dashboard_data

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_health_app(
    sessions: async_sessionmaker[AsyncSession],
    *,
    owner_telegram_id: int = 0,
    app_timezone: str = "Asia/Seoul",
) -> FastAPI:
    app = FastAPI(title="Calorie Bot Dashboard", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def add_security_headers(request, call_next) -> Response:
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

    return app
