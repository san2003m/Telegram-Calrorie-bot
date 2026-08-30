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


@dataclass(frozen=True)
class NormalizedSalt:
    sodium_mg: Decimal | None
    salt_equivalent_g: Decimal | None
    sodium_derived: bool
    salt_equivalent_derived: bool


def normalize_salt(sodium_mg: Decimal | None, salt_equivalent_g: Decimal | None) -> NormalizedSalt:
    if sodium_mg is None and salt_equivalent_g is not None:
        sodium_mg = (salt_equivalent_g * Decimal("1000") / Decimal("2.54")).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        return NormalizedSalt(sodium_mg, salt_equivalent_g, True, False)
    if salt_equivalent_g is None and sodium_mg is not None:
        salt_equivalent_g = (sodium_mg * Decimal("2.54") / Decimal("1000")).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        return NormalizedSalt(sodium_mg, salt_equivalent_g, False, True)
    return NormalizedSalt(sodium_mg, salt_equivalent_g, False, False)


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
    if result.label_market == "UNKNOWN":
        warnings.append("한국·일본 표시 형식을 확정하지 못했습니다.")
    elif result.label_market == "KR" and result.nutrients.sodium_mg is None:
        warnings.append("한국 영양표의 나트륨 값을 읽지 못했습니다.")
    elif result.label_market == "JP" and result.nutrients.salt_equivalent_g is None:
        warnings.append("일본 영양표의 식염상당량을 읽지 못했습니다.")

    if result.estimated_values:
        warnings.append("포장지에서 영양값이 추정치 또는 참고값으로 표시되어 있습니다.")

    if (
        result.nutrients.sugars_g is not None
        and result.nutrients.sugars_g > result.nutrients.carbs_g
    ):
        warnings.append("당류가 총 탄수화물보다 크게 인식되었습니다.")
    if result.nutrients.fiber_g is not None and result.nutrients.fiber_g > result.nutrients.carbs_g:
        warnings.append("식이섬유가 총 탄수화물보다 크게 인식되었습니다.")

    sodium = result.nutrients.sodium_mg
    salt = result.nutrients.salt_equivalent_g
    if sodium is not None and salt is not None:
        expected_salt = sodium * Decimal("2.54") / Decimal("1000")
        tolerance = max(Decimal("0.1"), salt * Decimal("0.2"))
        if abs(expected_salt - salt) > tolerance:
            warnings.append("나트륨과 식염상당량의 환산값이 서로 맞지 않습니다.")
    return warnings


def parse_positive_decimal(raw: str, *, maximum: Decimal = Decimal("10000")) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except Exception as exc:
        raise ValueError("숫자 형식이 올바르지 않습니다.") from exc
    if not value.is_finite() or value <= 0 or value > maximum:
        raise ValueError(f"0보다 크고 {maximum} 이하인 숫자를 입력하세요.")
    return value
