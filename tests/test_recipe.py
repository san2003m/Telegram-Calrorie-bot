from decimal import Decimal
from types import SimpleNamespace

from app.recipe import (
    display_recipe_amount,
    ingredient_multiplier,
    parse_structured_recipe,
    recipe_input_hash,
)
from app.schemas import RecipeIngredientInput


def _version(**overrides):
    values = {
        "basis_amount": Decimal("100"),
        "basis_unit": "g",
        "package_amount": None,
        "package_unit": None,
        "servings_per_package": None,
        "piece_count": None,
        "basis_count_amount": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_structured_recipe_is_parsed_without_ai() -> None:
    result = parse_structured_recipe(
        "밥 420g\n김치 160그램\n달걀 2개\n총 2인분",
        name_hint="김치볶음밥",
    )

    assert result is not None
    assert result.recipe_name == "김치볶음밥"
    assert result.servings == Decimal("2")
    assert [(item.name, item.amount, item.unit) for item in result.ingredients] == [
        ("밥", Decimal("420"), "g"),
        ("김치", Decimal("160"), "g"),
        ("달걀", Decimal("2"), "piece"),
    ]


def test_first_line_can_be_recipe_name_and_metric_units_are_normalized() -> None:
    result = parse_structured_recipe("스튜\n물 1l\n고기 0.5kg\n4인분")

    assert result is not None
    assert result.recipe_name == "스튜"
    assert result.servings == Decimal("4")
    assert result.ingredients[0].amount == Decimal("1000")
    assert result.ingredients[0].unit == "ml"
    assert result.ingredients[1].amount == Decimal("500")
    assert result.ingredients[1].unit == "g"


def test_free_form_recipe_falls_back_to_ai() -> None:
    assert (
        parse_structured_recipe(
            "밥 두 공기에 김치 한 컵, 달걀 두 개를 넣고 2인분으로 볶았어",
            name_hint="김치볶음밥",
        )
        is None
    )


def test_household_volume_is_only_converted_to_volume() -> None:
    ingredient = RecipeIngredientInput(name="물", amount=Decimal("2"), unit="tbsp")
    multiplier, portion = ingredient_multiplier(
        _version(basis_amount=Decimal("100"), basis_unit="ml"), ingredient
    )

    assert portion.amount == Decimal("30")
    assert portion.unit == "ml"
    assert multiplier == Decimal("0.3")


def test_piece_amount_uses_catalog_piece_reference() -> None:
    ingredient = RecipeIngredientInput(name="달걀", amount=Decimal("2"), unit="piece")
    multiplier, _ = ingredient_multiplier(_version(basis_count_amount=Decimal("2")), ingredient)

    assert multiplier == Decimal("1")
    assert display_recipe_amount(Decimal("2"), "piece") == "2개"


def test_recipe_hash_ignores_simple_whitespace_changes() -> None:
    first = recipe_input_hash("김치볶음밥", "밥  100g\n김치 50g")
    second = recipe_input_hash(" 김치볶음밥 ", " 밥 100g\n김치  50g ")

    assert first == second
