from decimal import Decimal
from types import SimpleNamespace

from app.portion import ParsedPortion
from app.schemas import Nutrients, NutritionBasis, NutritionRecognition
from app.telegram import (
    _can_correct_basis_unit,
    _candidate_from_recognition,
    _candidate_with_basis_unit,
    _format_uptime,
    _recognition_result_text,
)


def test_format_uptime() -> None:
    assert _format_uptime(0.9) == "0초"
    assert _format_uptime(65) == "1분 5초"
    assert _format_uptime(90_061) == "1일 1시간 1분 1초"


def test_format_uptime_does_not_show_negative_time() -> None:
    assert _format_uptime(-10) == "0초"


def test_liquid_unit_correction_creates_private_candidate_data() -> None:
    product = SimpleNamespace(barcode="4902102084178", name="콜라", brand="테스트")
    version = SimpleNamespace(
        product=product,
        basis_amount=Decimal("100"),
        basis_unit="g",
        package_amount=Decimal("500"),
        package_unit="ml",
        servings_per_package=None,
        piece_count=None,
        kcal=Decimal("0"),
        carbs_g=Decimal("0"),
        protein_g=Decimal("0"),
        fat_g=Decimal("0"),
        raw_data={"quantity": "500 ml"},
    )
    portion = ParsedPortion(Decimal("500"), "ml")

    assert _can_correct_basis_unit(version, portion) is True
    candidate = _candidate_with_basis_unit(version, "ml")
    assert candidate.basis_unit == "ml"
    assert candidate.source == "user_correction"
    assert candidate.raw_data["user_basis_unit_correction"] == {
        "from": "g",
        "to": "ml",
    }


def test_japanese_recognition_preserves_basis_and_converts_salt() -> None:
    result = NutritionRecognition(
        label_found=True,
        label_market="JP",
        label_language="ja",
        product_name="テスト飲料",
        nutrition_basis=NutritionBasis(
            amount=Decimal("200"),
            unit="ml",
            raw_text="1本（200ml）当たり",
            metric_amount=Decimal("200"),
            metric_unit="ml",
            count_amount=Decimal("1"),
            count_unit="本",
        ),
        nutrients=Nutrients(
            energy_kcal=Decimal("140"),
            carbs_g=Decimal("10"),
            protein_g=Decimal("7"),
            fat_g=Decimal("8"),
            salt_equivalent_g=Decimal("0.8"),
        ),
        package_amount={"amount": Decimal("200"), "unit": "ml"},
        piece_count=Decimal("1"),
        confidence=Decimal("0.95"),
    )

    candidate = _candidate_from_recognition("4901234567894", result)
    text = _recognition_result_text(result)

    assert candidate.label_market == "JP"
    assert candidate.basis_text == "1本（200ml）当たり"
    assert candidate.basis_count_amount == Decimal("1")
    assert candidate.basis_count_unit == "本"
    assert candidate.sodium_mg == Decimal("315.0")
    assert candidate.sodium_derived is True
    assert "🇯🇵 일본" in text
    assert "식염상당량 0.8 g" in text
    assert "나트륨 315 mg (식염상당량에서 환산)" in text


def test_small_salt_value_keeps_required_precision() -> None:
    result = NutritionRecognition(
        label_found=True,
        label_market="JP",
        label_language="ja",
        product_name="無塩テスト",
        nutrition_basis=NutritionBasis(amount=Decimal("100"), unit="g"),
        nutrients=Nutrients(
            energy_kcal=Decimal("10"),
            carbs_g=Decimal("1"),
            protein_g=Decimal("0"),
            fat_g=Decimal("0"),
            salt_equivalent_g=Decimal("0.02"),
        ),
        confidence=Decimal("0.95"),
    )

    assert "식염상당량 0.02 g" in _recognition_result_text(result)
