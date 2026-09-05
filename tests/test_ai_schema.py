from app.ai_recognition import NUTRITION_JSON_SCHEMA
from app.schemas import NutritionRecognition


def test_ai_json_schema_requires_every_top_level_field() -> None:
    properties = NUTRITION_JSON_SCHEMA["properties"]
    required = NUTRITION_JSON_SCHEMA["required"]

    assert set(properties) == set(required)
    for nested_name in ("nutrition_basis", "nutrients"):
        nested = properties[nested_name]
        assert set(nested["properties"]) == set(nested["required"])


def test_recognition_parses_expected_json() -> None:
    payload = """{
      "label_found": true,
      "product_name_found": true,
      "label_market": "KR",
      "label_language": "ko",
      "product_name": "닭가슴살",
      "brand": null,
      "nutrition_basis": {
        "amount": 100,
        "unit": "g",
        "raw_text": "총 내용량 100g당",
        "metric_amount": 100,
        "metric_unit": "g",
        "count_amount": null,
        "count_unit": null
      },
      "nutrients": {
        "energy_kcal": 120,
        "carbs_g": 1,
        "protein_g": 25,
        "fat_g": 2,
        "sugars_g": 0,
        "fiber_g": null,
        "saturated_fat_g": 0.5,
        "trans_fat_g": 0,
        "cholesterol_mg": 55,
        "sodium_mg": 320,
        "salt_equivalent_g": null
      },
      "package_amount": {"amount": 100, "unit": "g"},
      "servings_per_package": 1,
      "piece_count": 1,
      "search_concepts": ["chicken_breast", "high_protein"],
      "search_terms_ko": ["닭가슴살", "고단백"],
      "search_terms_ja": ["鶏むね肉", "高たんぱく"],
      "evidence_text": ["100 g당 120 kcal"],
      "estimated_values": false,
      "confidence": 0.96
    }"""

    result = NutritionRecognition.model_validate_json(payload)

    assert result.label_found is True
    assert result.product_name_found is True
    assert result.product_name == "닭가슴살"
    assert result.nutrients.protein_g == 25
    assert result.piece_count == 1
    assert result.label_market == "KR"
    assert result.nutrition_basis.raw_text == "총 내용량 100g당"
    assert result.search_concepts == ["chicken_breast", "high_protein"]
    assert result.search_terms_ja == ["鶏むね肉", "高たんぱく"]
