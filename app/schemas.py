from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.search_tags import SEARCH_CONCEPT_KEYS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Amount(StrictModel):
    amount: Decimal = Field(gt=0)
    unit: Literal["g", "ml", "serving", "package", "piece"]


class NutritionBasis(Amount):
    raw_text: str = Field(default="", max_length=160)
    metric_amount: Decimal | None = Field(default=None, gt=0)
    metric_unit: Literal["g", "ml"] | None = None
    count_amount: Decimal | None = Field(default=None, gt=0)
    count_unit: str | None = Field(default=None, max_length=32)


class Nutrients(StrictModel):
    energy_kcal: Decimal = Field(ge=0, le=100_000)
    carbs_g: Decimal = Field(ge=0, le=10_000)
    protein_g: Decimal = Field(ge=0, le=10_000)
    fat_g: Decimal = Field(ge=0, le=10_000)
    sugars_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    fiber_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    saturated_fat_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    trans_fat_g: Decimal | None = Field(default=None, ge=0, le=10_000)
    cholesterol_mg: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    sodium_mg: Decimal | None = Field(default=None, ge=0, le=1_000_000)
    salt_equivalent_g: Decimal | None = Field(default=None, ge=0, le=10_000)


class NutritionRecognition(StrictModel):
    label_found: bool
    product_name_found: bool = True
    label_market: Literal["KR", "JP", "UNKNOWN"] = "UNKNOWN"
    label_language: Literal["ko", "ja", "mixed", "unknown"] = "unknown"
    product_name: str = Field(min_length=1, max_length=240)
    brand: str | None = Field(default=None, max_length=160)
    nutrition_basis: NutritionBasis
    nutrients: Nutrients
    package_amount: Amount | None = None
    servings_per_package: Decimal | None = Field(default=None, gt=0, le=10_000)
    piece_count: Decimal | None = Field(default=None, gt=0, le=10_000)
    search_concepts: list[str] = Field(default_factory=list, max_length=8)
    search_terms_ko: list[str] = Field(default_factory=list, max_length=4)
    search_terms_ja: list[str] = Field(default_factory=list, max_length=4)
    evidence_text: list[str] = Field(default_factory=list, max_length=12)
    estimated_values: bool = False
    confidence: Decimal = Field(ge=0, le=1)

    @field_validator("product_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("search_concepts")
    @classmethod
    def validate_search_concepts(cls, values: list[str]) -> list[str]:
        unknown = set(values) - set(SEARCH_CONCEPT_KEYS)
        if unknown:
            raise ValueError(f"지원하지 않는 검색 태그: {', '.join(sorted(unknown))}")
        return list(dict.fromkeys(values))

    @field_validator("search_terms_ko", "search_terms_ja")
    @classmethod
    def clean_search_terms(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(" ".join(value.split()) for value in values if value.strip()))
        if any(len(value) > 32 for value in cleaned):
            raise ValueError("검색어는 각각 32자 이하여야 합니다.")
        return cleaned


class ProductCandidate(BaseModel):
    barcode: str | None = None
    external_source: str | None = None
    external_id: str | None = None
    name: str
    brand: str | None = None
    basis_amount: Decimal
    basis_unit: str
    package_amount: Decimal | None = None
    package_unit: str | None = None
    servings_per_package: Decimal | None = None
    piece_count: Decimal | None = None
    kcal: Decimal
    carbs_g: Decimal
    protein_g: Decimal
    fat_g: Decimal
    source: str
    verified: bool = False
    raw_data: dict | None = None
    label_market: Literal["KR", "JP", "UNKNOWN"] = "UNKNOWN"
    label_language: Literal["ko", "ja", "mixed", "unknown"] = "unknown"
    basis_text: str | None = None
    basis_metric_amount: Decimal | None = None
    basis_metric_unit: Literal["g", "ml"] | None = None
    basis_count_amount: Decimal | None = None
    basis_count_unit: str | None = None
    search_concepts: list[str] = Field(default_factory=list, max_length=8)
    search_terms_ko: list[str] = Field(default_factory=list, max_length=4)
    search_terms_ja: list[str] = Field(default_factory=list, max_length=4)
    sodium_mg: Decimal | None = None
    salt_equivalent_g: Decimal | None = None
    sodium_derived: bool = False
    salt_equivalent_derived: bool = False
    estimated_values: bool = False


class RecipeIngredientInput(StrictModel):
    raw_text: str = Field(default="", max_length=160)
    name: str = Field(min_length=1, max_length=80)
    amount: Decimal | None = Field(default=None, gt=0, le=1_000_000)
    unit: Literal["g", "ml", "piece", "tbsp", "tsp", "cup", "unknown"]
    preparation: Literal["raw", "cooked", "unknown"] = "unknown"
    note: str | None = Field(default=None, max_length=160)

    @field_validator("name")
    @classmethod
    def clean_ingredient_name(cls, value: str) -> str:
        return " ".join(value.split()).strip(" ,·-|:")


class RecipeExtraction(StrictModel):
    recipe_name: str = Field(min_length=1, max_length=80)
    servings: Decimal = Field(default=Decimal("1"), gt=0, le=100)
    ingredients: list[RecipeIngredientInput] = Field(min_length=1, max_length=50)

    @field_validator("recipe_name")
    @classmethod
    def clean_recipe_name(cls, value: str) -> str:
        return " ".join(value.split()).strip(" ,·-|:")
