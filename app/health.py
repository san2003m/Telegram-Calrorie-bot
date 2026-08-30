from __future__ import annotations

from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def create_health_app(sessions: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="Calorie Bot Health", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    return app
