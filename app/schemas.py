from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Amount(StrictModel):
    amount: Decimal = Field(gt=0)
    unit: Literal["g", "ml", "serving", "package"]


class Nutrients(StrictModel):
    energy_kcal: Decimal = Field(ge=0, le=100_000)
    carbs_g: Decimal = Field(ge=0, le=10_000)
    protein_g: Decimal = Field(ge=0, le=10_000)
    fat_g: Decimal = Field(ge=0, le=10_000)


class NutritionRecognition(StrictModel):
    label_found: bool
    product_name: str = Field(min_length=1, max_length=240)
    brand: str | None = Field(default=None, max_length=160)
    nutrition_basis: Amount
    nutrients: Nutrients
    package_amount: Amount | None = None
    servings_per_package: Decimal | None = Field(default=None, gt=0, le=10_000)
    piece_count: Decimal | None = Field(default=None, gt=0, le=10_000)
    evidence_text: list[str] = Field(default_factory=list, max_length=12)
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
