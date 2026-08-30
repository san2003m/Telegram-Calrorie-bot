from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol


class PortionTarget(Protocol):
    basis_amount: Decimal
    basis_unit: str
    package_amount: Decimal | None
    package_unit: str | None
    servings_per_package: Decimal | None
    piece_count: Decimal | None


class PortionError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedPortion:
    amount: Decimal
    unit: str


_UNIT_ALIASES = {
    "g": "g",
    "그램": "g",
    "kg": "kg",
    "킬로": "kg",
    "킬로그램": "kg",
    "ml": "ml",
    "cc": "ml",
    "미리": "ml",
    "밀리리터": "ml",
    "l": "l",
    "리터": "l",
    "serving": "serving",
    "회": "serving",
    "회분": "serving",
    "인분": "serving",
    "食": "serving",
    "食分": "serving",
    "package": "package",
    "봉": "package",
    "봉지": "package",
    "팩": "package",
    "캔": "package",
    "병": "package",
    "포장": "package",
    "袋": "package",
    "パック": "package",
    "缶": "package",
    "瓶": "package",
    "ボトル": "package",
    "piece": "piece",
    "개": "piece",
    "個": "piece",
    "本": "piece",
    "枚": "piece",
    "%": "percent",
    "percent": "percent",
    "퍼센트": "percent",
}

_SPECIAL_PORTIONS = {
    "전부": ParsedPortion(Decimal("1"), "package"),
    "전체": ParsedPortion(Decimal("1"), "package"),
    "한봉": ParsedPortion(Decimal("1"), "package"),
    "한봉지": ParsedPortion(Decimal("1"), "package"),
    "한팩": ParsedPortion(Decimal("1"), "package"),
    "한캔": ParsedPortion(Decimal("1"), "package"),
    "한병": ParsedPortion(Decimal("1"), "package"),
    "절반": ParsedPortion(Decimal("50"), "percent"),
    "반": ParsedPortion(Decimal("50"), "percent"),
    "한개": ParsedPortion(Decimal("1"), "piece"),
    "全部": ParsedPortion(Decimal("1"), "package"),
    "半分": ParsedPortion(Decimal("50"), "percent"),
}

_AMOUNT_RE = re.compile(
    r"(?P<amount>(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?P<unit>킬로그램|밀리리터|퍼센트|serving|package|percent|piece|"
    r"ボトル|パック|食分|봉지|그램|킬로|리터|회분|인분|포장|미리|"
    r"ml|kg|cc|g|l|봉|팩|캔|병|회|개|袋|缶|瓶|食|個|本|枚|%)",
    re.IGNORECASE,
)


def _compact_number(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def parse_portion(raw: str) -> ParsedPortion:
    normalized = unicodedata.normalize("NFKC", raw).strip().lower().replace(",", "")
    compact = re.sub(r"\s+", "", normalized)
    if compact in _SPECIAL_PORTIONS:
        return _SPECIAL_PORTIONS[compact]

    match = _AMOUNT_RE.fullmatch(normalized)
    if match is None:
        raise PortionError("예: 45g, 250ml, 2개, 0.5봉, 70%, 절반")
    try:
        amount = Decimal(match.group("amount"))
    except InvalidOperation as exc:
        raise PortionError("섭취량 숫자를 확인해 주세요.") from exc
    if not amount.is_finite() or amount <= 0:
        raise PortionError("섭취량은 0보다 커야 합니다.")

    unit = _UNIT_ALIASES[match.group("unit").lower()]
    if unit == "kg":
        amount *= Decimal("1000")
        unit = "g"
    elif unit == "l":
        amount *= Decimal("1000")
        unit = "ml"
    return ParsedPortion(amount=amount, unit=unit)


def display_portion(portion: ParsedPortion) -> str:
    labels = {
        "g": "g",
        "ml": "ml",
        "serving": "회분",
        "package": "포장",
        "piece": "개",
        "percent": "%",
    }
    return f"{_compact_number(portion.amount)}{labels.get(portion.unit, portion.unit)}"


def package_multiplier(target: PortionTarget) -> Decimal | None:
    if (
        target.package_amount is not None
        and target.package_unit == target.basis_unit
        and target.basis_amount > 0
    ):
        return target.package_amount / target.basis_amount
    if target.servings_per_package is not None and target.basis_unit == "serving":
        return target.servings_per_package / target.basis_amount
    if target.piece_count is not None and target.basis_unit == "piece":
        return target.piece_count / target.basis_amount
    if target.basis_unit == "package":
        return Decimal("1") / target.basis_amount
    return None


def portion_multiplier(target: PortionTarget, portion: ParsedPortion) -> Decimal:
    multiplier: Decimal | None = None
    if portion.unit == target.basis_unit:
        multiplier = portion.amount / target.basis_amount
    elif portion.unit in {"package", "percent"}:
        package = package_multiplier(target)
        if package is None:
            raise PortionError("총 내용량이 없어 포장 또는 % 단위로 계산할 수 없습니다.")
        factor = portion.amount if portion.unit == "package" else portion.amount / Decimal("100")
        multiplier = package * factor
    elif portion.unit == "serving":
        package = package_multiplier(target)
        if package is None or target.servings_per_package is None:
            raise PortionError("1회 제공량 정보가 없어 회분 단위로 계산할 수 없습니다.")
        multiplier = package * portion.amount / target.servings_per_package
    elif portion.unit == "piece":
        package = package_multiplier(target)
        if package is None or target.piece_count is None:
            raise PortionError("총 낱개 수 정보가 없어 개수로 계산할 수 없습니다.")
        multiplier = package * portion.amount / target.piece_count
    elif portion.unit in {"g", "ml"}:
        raise PortionError(
            f"이 제품의 영양정보 기준 단위는 {target.basis_unit}라서 "
            f"{portion.unit}으로 바로 환산할 수 없습니다."
        )

    if multiplier is None or not multiplier.is_finite() or multiplier <= 0:
        raise PortionError("섭취량을 계산하지 못했습니다.")
    if multiplier > Decimal("1000"):
        raise PortionError("섭취량이 너무 큽니다. 입력값과 단위를 확인해 주세요.")
    return multiplier
