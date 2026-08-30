from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import IntakeLog, ProductVersion, User
from app.portion import ParsedPortion, display_portion

HISTORY_DAYS = 30
RECENT_LOG_LIMIT = 12
DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


def _number(value: Decimal | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Seoul")


def _empty_totals() -> dict[str, float]:
    return {"kcal": 0.0, "carbs_g": 0.0, "protein_g": 0.0, "fat_g": 0.0}


def _goals(user: User | None) -> dict[str, float | None]:
    return {
        "kcal": _number(user.kcal_goal) if user else None,
        "carbs_g": _number(user.carb_goal) if user else None,
        "protein_g": _number(user.protein_goal) if user else None,
        "fat_g": _number(user.fat_goal) if user else None,
    }


def _add_log(totals: dict[str, float], log: IntakeLog) -> None:
    totals["kcal"] += float(log.kcal)
    totals["carbs_g"] += float(log.carbs_g)
    totals["protein_g"] += float(log.protein_g)
    totals["fat_g"] += float(log.fat_g)


def _round_totals(totals: dict[str, float]) -> dict[str, float]:
    return {key: round(value, 1) for key, value in totals.items()}


def _portion_text(log: IntakeLog) -> str:
    if log.input_amount is not None and log.input_unit:
        return display_portion(ParsedPortion(amount=log.input_amount, unit=log.input_unit))
    multiplier = Decimal(log.multiplier).normalize()
    return f"기준량 × {format(multiplier, 'f')}"


def _basis_text(version: ProductVersion) -> str:
    if version.basis_text:
        return version.basis_text
    amount = format(Decimal(version.basis_amount).normalize(), "f")
    return f"{amount}{version.basis_unit} 기준"


def _serialize_recent(logs: Sequence[IntakeLog], tz: ZoneInfo) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for log in logs:
        version = log.product_version
        product = version.product
        local_time = _utc_aware(log.consumed_at).astimezone(tz)
        result.append(
            {
                "id": log.id,
                "name": product.name,
                "brand": product.brand,
                "amount": _portion_text(log),
                "consumed_at": local_time.isoformat(),
                "kcal": _number(log.kcal),
                "carbs_g": _number(log.carbs_g),
                "protein_g": _number(log.protein_g),
                "fat_g": _number(log.fat_g),
                "nutrition": {
                    "source": version.source,
                    "market": version.label_market,
                    "language": version.label_language,
                    "basis": _basis_text(version),
                    "verified": version.verified,
                    "estimated": version.estimated_values,
                    "derived": version.sodium_derived or version.salt_equivalent_derived,
                },
            }
        )
    return result


async def build_dashboard_data(
    session: AsyncSession,
    owner_telegram_id: int,
    *,
    fallback_timezone: str = "Asia/Seoul",
    now: datetime | None = None,
) -> dict[str, object]:
    generated_at = _utc_aware(now or datetime.now(UTC))
    user = await session.get(User, owner_telegram_id)
    timezone_name = user.timezone if user else fallback_timezone
    tz = _timezone(timezone_name)
    local_now = generated_at.astimezone(tz)
    first_day = local_now.date() - timedelta(days=HISTORY_DAYS - 1)
    first_local = datetime.combine(first_day, datetime.min.time(), tzinfo=tz)
    first_utc = first_local.astimezone(UTC)

    history_logs: Sequence[IntakeLog] = []
    recent_logs: Sequence[IntakeLog] = []
    if user is not None:
        history_logs = (
            await session.scalars(
                select(IntakeLog).where(
                    IntakeLog.user_telegram_id == owner_telegram_id,
                    IntakeLog.voided_at.is_(None),
                    IntakeLog.consumed_at >= first_utc,
                )
            )
        ).all()
        recent_logs = (
            await session.scalars(
                select(IntakeLog)
                .options(joinedload(IntakeLog.product_version).joinedload(ProductVersion.product))
                .where(
                    IntakeLog.user_telegram_id == owner_telegram_id,
                    IntakeLog.voided_at.is_(None),
                )
                .order_by(IntakeLog.consumed_at.desc())
                .limit(RECENT_LOG_LIMIT)
            )
        ).all()

    daily: dict[date, dict[str, object]] = {}
    for offset in range(HISTORY_DAYS):
        day = first_day + timedelta(days=offset)
        daily[day] = {"date": day.isoformat(), "totals": _empty_totals(), "item_count": 0}

    for log in history_logs:
        day = _utc_aware(log.consumed_at).astimezone(tz).date()
        bucket = daily.get(day)
        if bucket is None:
            continue
        totals = bucket["totals"]
        assert isinstance(totals, dict)
        _add_log(totals, log)
        bucket["item_count"] = int(bucket["item_count"]) + 1

    for bucket in daily.values():
        totals = bucket["totals"]
        assert isinstance(totals, dict)
        bucket["totals"] = _round_totals(totals)

    today_bucket = daily[local_now.date()]
    return {
        "generated_at": generated_at.isoformat(),
        "timezone": timezone_name,
        "has_user": user is not None,
        "today": {
            "date": local_now.date().isoformat(),
            "totals": today_bucket["totals"],
            "goals": _goals(user),
            "item_count": today_bucket["item_count"],
        },
        "days": list(daily.values()),
        "recent": _serialize_recent(recent_logs, tz),
    }
