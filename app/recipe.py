from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.nutrition import MacroTotals
from app.portion import ParsedPortion, PortionError, portion_multiplier
from app.schemas import RecipeExtraction, RecipeIngredientInput

PARSER_VERSION = "recipe-v1"


class RecipeError(ValueError):
    pass


_SERVINGS_RE = re.compile(
    r"^(?:총\s*)?(?P<amount>\d+(?:\.\d+)?)\s*(?:인분|servings?|회분)(?:\s*(?:분량)?)?$",
    re.IGNORECASE,
)
_INGREDIENT_RE = re.compile(
    r"^(?P<name>.+?)\s*(?:[:=|-]\s*)?"
    r"(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>킬로그램|밀리리터|큰술|작은술|그램|kg|ml|cc|tbsp|tsp|cup|컵|개|알|g|l)"
    r"(?:\s*(?P<note>.*))?$",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s*")

_UNIT_MAP = {
    "g": "g",
    "그램": "g",
    "kg": "kg",
    "킬로그램": "kg",
    "ml": "ml",
    "cc": "ml",
    "밀리리터": "ml",
    "l": "l",
    "개": "piece",
    "알": "piece",
    "큰술": "tbsp",
    "tbsp": "tbsp",
    "작은술": "tsp",
    "tsp": "tsp",
    "컵": "cup",
    "cup": "cup",
}


@dataclass(frozen=True)
class ResolvedRecipeIngredient:
    input_name: str
    matched_name: str
    amount: Decimal
    unit: str
    multiplier: Decimal
    version_id: int
    source: str
    totals: MacroTotals


@dataclass(frozen=True)
class RecipeDraft:
    draft_id: str
    user_id: int
    input_hash: str
    name: str
    servings: Decimal
    used_ai: bool
    ingredients: tuple[ResolvedRecipeIngredient, ...]
    total: MacroTotals

    @property
    def per_serving(self) -> MacroTotals:
        return self.total.scaled(Decimal("1") / self.servings)


def recipe_input_hash(name_hint: str | None, raw_text: str) -> str:
    normalized = unicodedata.normalize("NFKC", f"{name_hint or ''}\n{raw_text}")
    normalized = "\n".join(" ".join(line.lower().split()) for line in normalized.splitlines())
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()


def _clean_line(raw: str) -> str:
    return _BULLET_RE.sub("", unicodedata.normalize("NFKC", raw)).strip()


def _decimal(raw: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise RecipeError("재료의 양을 숫자로 확인해 주세요.") from exc
    if not value.is_finite() or value <= 0:
        raise RecipeError("재료의 양은 0보다 커야 합니다.")
    return value


def _preparation(name: str, note: str) -> str:
    text = f"{name} {note}"
    if any(word in text for word in ("생것", "생 ", "생닭", "생고기", "raw")):
        return "raw"
    if any(word in text for word in ("삶", "구운", "익힌", "볶은", "찐", "조리")):
        return "cooked"
    return "unknown"


def _parse_ingredient(line: str) -> RecipeIngredientInput | None:
    match = _INGREDIENT_RE.fullmatch(line)
    if match is None:
        return None
    name = match.group("name").strip(" ,·-|:")
    if not name:
        return None
    amount = _decimal(match.group("amount"))
    unit = _UNIT_MAP[match.group("unit").lower()]
    if unit == "kg":
        amount *= Decimal("1000")
        unit = "g"
    elif unit == "l":
        amount *= Decimal("1000")
        unit = "ml"
    note = (match.group("note") or "").strip()[:160] or None
    return RecipeIngredientInput(
        raw_text=line[:160],
        name=name,
        amount=amount,
        unit=unit,
        preparation=_preparation(name, note or ""),
        note=note,
    )


def parse_structured_recipe(
    raw_text: str,
    *,
    name_hint: str | None = None,
    max_ingredients: int = 20,
) -> RecipeExtraction | None:
    lines = [_clean_line(line) for line in raw_text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise RecipeError("레시피 내용을 입력해 주세요.")

    recipe_name = " ".join((name_hint or "").split()).strip()[:80]
    if not recipe_name and len(lines) >= 2:
        first = lines[0]
        if _parse_ingredient(first) is None and _SERVINGS_RE.fullmatch(first) is None:
            recipe_name = first[:80]
            lines = lines[1:]
    recipe_name = recipe_name or "내 레시피"

    servings = Decimal("1")
    ingredients: list[RecipeIngredientInput] = []
    for line in lines:
        serving_match = _SERVINGS_RE.fullmatch(line)
        if serving_match:
            servings = _decimal(serving_match.group("amount"))
            if servings > 100:
                raise RecipeError("레시피 인분 수는 100 이하로 입력해 주세요.")
            continue
        ingredient = _parse_ingredient(line)
        if ingredient is None:
            return None
        ingredients.append(ingredient)
        if len(ingredients) > max_ingredients:
            raise RecipeError(f"재료는 최대 {max_ingredients}개까지 입력할 수 있습니다.")

    if not ingredients:
        raise RecipeError("계산할 재료를 한 개 이상 입력해 주세요.")
    return RecipeExtraction(
        recipe_name=recipe_name,
        servings=servings,
        ingredients=ingredients,
    )


def ingredient_portion(ingredient: RecipeIngredientInput) -> ParsedPortion:
    if ingredient.amount is None or ingredient.unit == "unknown":
        raise RecipeError(f"{ingredient.name}: 양과 단위를 확인해 주세요.")
    amount = ingredient.amount
    unit = ingredient.unit
    if unit == "tbsp":
        amount *= Decimal("15")
        unit = "ml"
    elif unit == "tsp":
        amount *= Decimal("5")
        unit = "ml"
    elif unit == "cup":
        amount *= Decimal("200")
        unit = "ml"
    return ParsedPortion(amount=amount, unit=unit)


def ingredient_multiplier(
    version, ingredient: RecipeIngredientInput
) -> tuple[Decimal, ParsedPortion]:
    portion = ingredient_portion(ingredient)
    try:
        multiplier = portion_multiplier(version, portion)
    except PortionError as exc:
        raise RecipeError(f"{ingredient.name}: {exc}") from exc
    return multiplier, portion


def sum_recipe_totals(ingredients: list[ResolvedRecipeIngredient]) -> MacroTotals:
    return MacroTotals(
        kcal=sum((item.totals.kcal for item in ingredients), Decimal("0")),
        carbs_g=sum((item.totals.carbs_g for item in ingredients), Decimal("0")),
        protein_g=sum((item.totals.protein_g for item in ingredients), Decimal("0")),
        fat_g=sum((item.totals.fat_g for item in ingredients), Decimal("0")),
    )


def ingredient_totals(version, multiplier: Decimal) -> MacroTotals:
    return MacroTotals(
        kcal=version.kcal,
        carbs_g=version.carbs_g,
        protein_g=version.protein_g,
        fat_g=version.fat_g,
    ).scaled(multiplier)


def display_recipe_amount(amount: Decimal, unit: str) -> str:
    labels = {
        "g": "g",
        "ml": "ml",
        "piece": "개",
        "tbsp": "큰술",
        "tsp": "작은술",
        "cup": "컵",
        "unknown": "",
    }
    normalized = format(amount.normalize(), "f")
    return f"{normalized}{labels.get(unit, unit)}"
