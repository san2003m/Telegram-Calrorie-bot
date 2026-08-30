from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.portion import (
    ParsedPortion,
    PortionError,
    display_portion,
    parse_portion,
    portion_multiplier,
)


def product(**overrides):
    values = {
        "basis_amount": Decimal("30"),
        "basis_unit": "g",
        "package_amount": Decimal("90"),
        "package_unit": "g",
        "servings_per_package": Decimal("3"),
        "piece_count": Decimal("6"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45g", ParsedPortion(Decimal("45"), "g")),
        ("250 ml", ParsedPortion(Decimal("250"), "ml")),
        ("2개", ParsedPortion(Decimal("2"), "piece")),
        ("0.5봉", ParsedPortion(Decimal("0.5"), "package")),
        ("70%", ParsedPortion(Decimal("70"), "percent")),
        ("절반", ParsedPortion(Decimal("50"), "percent")),
        ("1.5L", ParsedPortion(Decimal("1500.0"), "ml")),
        ("１本", ParsedPortion(Decimal("1"), "piece")),
        ("2個", ParsedPortion(Decimal("2"), "piece")),
        ("0.5袋", ParsedPortion(Decimal("0.5"), "package")),
        ("1食分", ParsedPortion(Decimal("1"), "serving")),
        ("半分", ParsedPortion(Decimal("50"), "percent")),
    ],
)
def test_parse_portion(raw: str, expected: ParsedPortion) -> None:
    assert parse_portion(raw) == expected


def test_portion_multiplier_uses_product_measures() -> None:
    target = product()

    assert portion_multiplier(target, parse_portion("45g")) == Decimal("1.5")
    assert portion_multiplier(target, parse_portion("전부")) == Decimal("3")
    assert portion_multiplier(target, parse_portion("절반")) == Decimal("1.5")
    assert portion_multiplier(target, parse_portion("1회분")) == Decimal("1")
    assert portion_multiplier(target, parse_portion("2개")) == Decimal("1")


def test_portion_multiplier_rejects_unknown_conversion() -> None:
    target = product(package_amount=None, servings_per_package=None, piece_count=None)

    with pytest.raises(PortionError, match="총 내용량"):
        portion_multiplier(target, parse_portion("절반"))
    with pytest.raises(PortionError, match="기준 단위"):
        portion_multiplier(target, parse_portion("250ml"))


def test_piece_based_japanese_label_uses_explicit_package_count() -> None:
    target = product(
        basis_amount=Decimal("1"),
        basis_unit="piece",
        package_amount=Decimal("90"),
        package_unit="g",
        piece_count=Decimal("6"),
    )

    assert portion_multiplier(target, parse_portion("2個")) == Decimal("2")
    assert portion_multiplier(target, parse_portion("全部")) == Decimal("6")


def test_display_portion_uses_human_units() -> None:
    assert display_portion(parse_portion("0.5봉")) == "0.5포장"
    assert display_portion(parse_portion("70%")) == "70%"
