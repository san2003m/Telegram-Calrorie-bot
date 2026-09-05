import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ai_recognition import NutritionRecognizer


def _recognition_payload() -> str:
    return json.dumps(
        {
            "label_found": True,
            "product_name_found": True,
            "label_market": "KR",
            "label_language": "ko",
            "product_name": "한 장 인식 제품",
            "brand": "테스트",
            "nutrition_basis": {
                "amount": 100,
                "unit": "g",
                "raw_text": "100g당",
                "metric_amount": 100,
                "metric_unit": "g",
                "count_amount": None,
                "count_unit": None,
            },
            "nutrients": {
                "energy_kcal": 120,
                "carbs_g": 10,
                "protein_g": 5,
                "fat_g": 6,
                "sugars_g": None,
                "fiber_g": None,
                "saturated_fat_g": None,
                "trans_fat_g": None,
                "cholesterol_mg": None,
                "sodium_mg": 100,
                "salt_equivalent_g": None,
            },
            "package_amount": {"amount": 100, "unit": "g"},
            "servings_per_package": 1,
            "piece_count": None,
            "search_concepts": ["snack"],
            "search_terms_ko": ["과자"],
            "search_terms_ja": ["お菓子"],
            "evidence_text": ["100g당 120 kcal"],
            "confidence": 0.95,
            "estimated_values": False,
        }
    )


@pytest.mark.asyncio
async def test_recognizer_accepts_one_photo_and_uses_original_detail(tmp_path) -> None:
    image_path = tmp_path / "product.jpg"
    image_path.write_bytes(b"jpeg-placeholder")
    create = AsyncMock(return_value=SimpleNamespace(output_text=_recognition_payload(), usage=None))
    recognizer = NutritionRecognizer("test-key", "test-model")
    recognizer.client = SimpleNamespace(responses=SimpleNamespace(create=create))

    result = await recognizer.recognize_images([image_path])

    assert result.product_name == "한 장 인식 제품"
    assert result.search_terms_ja == ["お菓子"]
    request = create.await_args.kwargs
    content = request["input"][0]["content"]
    image_inputs = [item for item in content if item["type"] == "input_image"]
    assert len(image_inputs) == 1
    assert image_inputs[0]["detail"] == "original"
    assert request["store"] is False


@pytest.mark.asyncio
async def test_recognizer_rejects_more_than_three_photos(tmp_path) -> None:
    recognizer = NutritionRecognizer("test-key", "test-model")
    paths = [tmp_path / f"{index}.jpg" for index in range(4)]

    with pytest.raises(ValueError, match="1~3장"):
        await recognizer.recognize_images(paths)
