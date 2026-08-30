from decimal import Decimal
from types import SimpleNamespace

from app.portion import ParsedPortion
from app.telegram import (
    _can_correct_basis_unit,
    _candidate_with_basis_unit,
    _format_uptime,
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
