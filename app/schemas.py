from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    label_market: Literal["KR", "JP", "UNKNOWN"] = "UNKNOWN"
    label_language: Literal["ko", "ja", "mixed", "unknown"] = "unknown"
    product_name: str = Field(min_length=1, max_length=240)
    brand: str | None = Field(default=None, max_length=160)
    nutrition_basis: NutritionBasis
    nutrients: Nutrients
    package_amount: Amount | None = None
    servings_per_package: Decimal | None = Field(default=None, gt=0, le=10_000)
    piece_count: Decimal | None = Field(default=None, gt=0, le=10_000)
    evidence_text: list[str] = Field(default_factory=list, max_length=12)
    estimated_values: bool = False
    confidence: Decimal = Field(ge=0, le=1)

    @field_validator("product_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())


class ProductCandidate(BaseModel):
    barcode: str | None = None
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
    sodium_mg: Decimal | None = None
    salt_equivalent_g: Decimal | None = None
    sodium_derived: bool = False
    salt_equivalent_derived: bool = False
    estimated_values: bool = False
