from decimal import Decimal

import pytest

from app.nutrition import MacroTotals, parse_positive_decimal, recognition_warnings
from app.schemas import Amount, Nutrients, NutritionRecognition


def test_macro_totals_are_scaled_and_rounded() -> None:
    totals = MacroTotals(
        kcal=Decimal("123.45"),
        carbs_g=Decimal("10.25"),
        protein_g=Decimal("20"),
        fat_g=Decimal("3.33"),
    ).scaled(Decimal("1.5"))

    assert totals.kcal == Decimal("185.2")
    assert totals.carbs_g == Decimal("15.4")
    assert totals.protein_g == Decimal("30.0")
    assert totals.fat_g == Decimal("5.0")


@pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity", "hello"])
def test_positive_decimal_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_positive_decimal(raw)


def test_recognition_warns_about_inconsistent_calories() -> None:
    result = NutritionRecognition(
        label_found=True,
        product_name="테스트 식품",
        nutrition_basis=Amount(amount=Decimal("100"), unit="g"),
        nutrients=Nutrients(
            energy_kcal=Decimal("500"),
            carbs_g=Decimal("1"),
            protein_g=Decimal("1"),
            fat_g=Decimal("1"),
        ),
        confidence=Decimal("0.9"),
    )

    assert any("차이가 큽니다" in warning for warning in recognition_warnings(result))
