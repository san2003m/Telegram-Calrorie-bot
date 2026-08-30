from __future__ import annotations

from decimal import Decimal, InvalidOperation

import httpx

from app.portion import PortionError, parse_portion
from app.schemas import ProductCandidate


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


class OpenFoodFactsCatalog:
    base_url = "https://world.openfoodfacts.org/api/v3/product"

    def __init__(self, user_agent: str, *, timeout_seconds: float = 8.0) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds

    async def lookup(self, barcode: str) -> ProductCandidate | None:
        fields = ",".join(
            (
                "code",
                "product_name",
                "product_name_ko",
                "brands",
                "nutriments",
                "nutrition_data_per",
                "quantity",
            )
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    f"{self.base_url}/{barcode}.json", params={"fields": fields}
                )
            if response.status_code != 200:
                return None
            return self.from_payload(barcode, response.json())
        except (httpx.HTTPError, ValueError):
            return None

    @staticmethod
    def from_payload(barcode: str, payload: dict) -> ProductCandidate | None:
        if payload.get("status") not in (1, "success"):
            return None
        product = payload.get("product") or {}
        nutrients = product.get("nutriments") or {}
        kcal = _decimal(nutrients.get("energy-kcal_100g"))
        if kcal is None:
            return None
        name = product.get("product_name_ko") or product.get("product_name")
        if not isinstance(name, str) or not name.strip():
            return None
        package_amount = None
        package_unit = None
        quantity = product.get("quantity")
        if isinstance(quantity, str):
            try:
                parsed_quantity = parse_portion(quantity)
                if parsed_quantity.unit in {"g", "ml"}:
                    package_amount = parsed_quantity.amount
                    package_unit = parsed_quantity.unit
            except PortionError:
                pass
        nutrition_data_per = str(product.get("nutrition_data_per") or "").lower()
        basis_unit = "ml" if nutrition_data_per.replace(" ", "") == "100ml" else "g"
        return ProductCandidate(
            barcode=barcode,
            name=" ".join(name.split()),
            brand=(product.get("brands") or None),
            basis_amount=Decimal("100"),
            basis_unit=basis_unit,
            package_amount=package_amount,
            package_unit=package_unit,
            kcal=kcal,
            carbs_g=_decimal(nutrients.get("carbohydrates_100g")) or Decimal("0"),
            protein_g=_decimal(nutrients.get("proteins_100g")) or Decimal("0"),
            fat_g=_decimal(nutrients.get("fat_100g")) or Decimal("0"),
            source="open_food_facts",
            verified=False,
            raw_data={
                "nutrition_data_per": product.get("nutrition_data_per"),
                "quantity": quantity,
                "nutriments": nutrients,
            },
        )
