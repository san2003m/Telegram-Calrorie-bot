from app.ai_recognition import NUTRITION_JSON_SCHEMA
from app.schemas import NutritionRecognition


def test_ai_json_schema_requires_every_top_level_field() -> None:
    properties = NUTRITION_JSON_SCHEMA["properties"]
    required = NUTRITION_JSON_SCHEMA["required"]

    assert set(properties) == set(required)


def test_recognition_parses_expected_json() -> None:
    payload = """{
      "label_found": true,
      "product_name": "닭가슴살",
      "brand": null,
      "nutrition_basis": {"amount": 100, "unit": "g"},
      "nutrients": {
        "energy_kcal": 120,
        "carbs_g": 1,
        "protein_g": 25,
        "fat_g": 2
      },
      "package_amount": {"amount": 100, "unit": "g"},
      "servings_per_package": 1,
      "piece_count": 1,
      "evidence_text": ["100 g당 120 kcal"],
      "confidence": 0.96
    }"""

    result = NutritionRecognition.model_validate_json(payload)

    assert result.label_found is True
    assert result.product_name == "닭가슴살"
    assert result.nutrients.protein_g == 25
    assert result.piece_count == 1
