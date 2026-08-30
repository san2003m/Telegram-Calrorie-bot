from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.schemas import NutritionRecognition

Q = Decimal("0.1")


def quantize(value: Decimal) -> Decimal:
    return value.quantize(Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class MacroTotals:
    kcal: Decimal
    carbs_g: Decimal
    protein_g: Decimal
    fat_g: Decimal

    def scaled(self, multiplier: Decimal) -> MacroTotals:
        if multiplier <= 0 or multiplier > 1000:
            raise ValueError("섭취 배수는 0보다 크고 1000 이하여야 합니다.")
        return MacroTotals(
            kcal=quantize(self.kcal * multiplier),
            carbs_g=quantize(self.carbs_g * multiplier),
            protein_g=quantize(self.protein_g * multiplier),
            fat_g=quantize(self.fat_g * multiplier),
        )


def recognition_warnings(result: NutritionRecognition) -> list[str]:
    warnings: list[str] = []
    if not result.label_found:
        warnings.append("영양정보 표를 확실히 찾지 못했습니다.")

    macro_kcal = (
        result.nutrients.carbs_g * Decimal("4")
        + result.nutrients.protein_g * Decimal("4")
        + result.nutrients.fat_g * Decimal("9")
    )
    tolerance = max(Decimal("40"), result.nutrients.energy_kcal * Decimal("0.35"))
    if abs(macro_kcal - result.nutrients.energy_kcal) > tolerance:
        warnings.append("표시 열량과 탄단지 환산 열량의 차이가 큽니다.")
    if result.confidence < Decimal("0.75"):
        warnings.append("AI 인식 신뢰도가 낮으니 포장지 숫자를 확인하세요.")
    return warnings


def parse_positive_decimal(raw: str, *, maximum: Decimal = Decimal("10000")) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except Exception as exc:
        raise ValueError("숫자 형식이 올바르지 않습니다.") from exc
    if not value.is_finite() or value <= 0 or value > maximum:
        raise ValueError(f"0보다 크고 {maximum} 이하인 숫자를 입력하세요.")
    return value
