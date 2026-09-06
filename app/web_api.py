from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator

from app.models import IntakeLog, ProductVersion, User
from app.portion import ParsedPortion, PortionError, display_portion, portion_multiplier
from app.schemas import StrictModel

PortionUnit = Literal["g", "ml", "serving", "package", "piece", "percent"]
BasisUnit = Literal["g", "ml", "serving", "package", "piece"]


class IntakeCreate(StrictModel):
    product_version_id: int = Field(gt=0)
    amount: Decimal = Field(gt=0, le=1_000_000)
    unit: PortionUnit
    consumed_at: datetime | None = None
    request_id: UUID


class IntakeUpdate(StrictModel):
    amount: Decimal = Field(gt=0, le=1_000_000)
    unit: PortionUnit
    consumed_at: datetime


class ManualIntakeCreate(StrictModel):
    name: str = Field(min_length=1, max_length=240)
    brand: str | None = Field(default=None, max_length=160)
    basis_amount: Decimal = Field(gt=0, le=1_000_000)
    basis_unit: BasisUnit
    kcal: Decimal = Field(ge=0, le=100_000)
    carbs_g: Decimal = Field(ge=0, le=10_000)
    protein_g: Decimal = Field(ge=0, le=10_000)
    fat_g: Decimal = Field(ge=0, le=10_000)
    intake_amount: Decimal = Field(gt=0, le=1_000_000)
    intake_unit: PortionUnit
    consumed_at: datetime | None = None
    request_id: UUID

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("brand")
    @classmethod
    def clean_brand(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())
        return cleaned or None


class GoalUpdate(StrictModel):
    kcal: Decimal = Field(gt=0, le=100_000)
    carbs_g: Decimal = Field(gt=0, le=10_000)
    protein_g: Decimal = Field(gt=0, le=10_000)
    fat_g: Decimal = Field(gt=0, le=10_000)


def normalize_consumed_at(
    value: datetime | None,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    if value is None:
        return current
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("Asia/Seoul")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    normalized = value.astimezone(UTC)
    if normalized > current + timedelta(minutes=5):
        raise ValueError("미래 시각으로는 기록할 수 없습니다.")
    if normalized < current - timedelta(days=3_650):
        raise ValueError("10년보다 오래된 시각으로는 기록할 수 없습니다.")
    return normalized


def validated_portion(
    version: ProductVersion, amount: Decimal, unit: str
) -> tuple[ParsedPortion, Decimal]:
    portion = ParsedPortion(amount=amount, unit=unit)
    return portion, portion_multiplier(version, portion)


def _decimal_number(value: Decimal | None) -> float | None:
    return None if value is None else round(float(value), 4)


def portion_options(version: ProductVersion) -> list[dict[str, object]]:
    candidates = [
        ParsedPortion(version.basis_amount, version.basis_unit),
        ParsedPortion(Decimal("1"), "package"),
        ParsedPortion(Decimal("50"), "percent"),
        ParsedPortion(Decimal("1"), "serving"),
        ParsedPortion(Decimal("1"), "piece"),
    ]
    labels = {
        "package": "포장",
        "percent": "%",
        "serving": "회분",
        "piece": "개",
        "g": "g",
        "ml": "ml",
    }
    options: list[dict[str, object]] = []
    seen: set[str] = set()
    for portion in candidates:
        if portion.unit in seen:
            continue
        try:
            portion_multiplier(version, portion)
        except PortionError:
            continue
        seen.add(portion.unit)
        options.append(
            {
                "unit": portion.unit,
                "label": labels.get(portion.unit, portion.unit),
                "default_amount": _decimal_number(portion.amount),
                "default_text": display_portion(portion),
            }
        )
    return options


def serialize_product(version: ProductVersion) -> dict[str, object]:
    return {
        "version_id": version.id,
        "name": version.product.name,
        "brand": version.product.brand,
        "barcode": version.product.barcode,
        "basis": {
            "amount": _decimal_number(version.basis_amount),
            "unit": version.basis_unit,
            "text": version.basis_text,
        },
        "nutrition": {
            "kcal": _decimal_number(version.kcal),
            "carbs_g": _decimal_number(version.carbs_g),
            "protein_g": _decimal_number(version.protein_g),
            "fat_g": _decimal_number(version.fat_g),
            "source": version.source,
            "estimated": version.estimated_values,
        },
        "portion_options": portion_options(version),
    }


def serialize_mutation_log(log: IntakeLog) -> dict[str, object]:
    return {
        "id": log.id,
        "name": log.product_version.product.name,
        "amount": display_portion(
            ParsedPortion(
                amount=log.input_amount or log.product_version.basis_amount,
                unit=log.input_unit or log.product_version.basis_unit,
            )
        ),
        "kcal": _decimal_number(log.kcal),
        "consumed_at": log.consumed_at.isoformat(),
        "voided": log.voided_at is not None,
    }


def user_timezone(user: User | None, fallback: str) -> str:
    return user.timezone if user is not None else fallback
