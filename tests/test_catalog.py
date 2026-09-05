from decimal import Decimal

from app.catalog import OpenFoodFactsCatalog


def test_open_food_facts_payload_mapping() -> None:
    payload = {
        "status": "success",
        "product": {
            "product_name_ko": "테스트 과자",
            "product_name_ja": "テスト菓子",
            "brands": "테스트 브랜드",
            "categories": "Snacks",
            "categories_tags": ["en:snacks"],
            "quantity": "80 g",
            "nutriments": {
                "energy-kcal_100g": 510,
                "carbohydrates_100g": 61.2,
                "proteins_100g": 7,
                "fat_100g": 26.5,
            },
        },
    }

    candidate = OpenFoodFactsCatalog.from_payload("8801234567890", payload)

    assert candidate is not None
    assert candidate.name == "테스트 과자"
    assert candidate.basis_amount == Decimal("100")
    assert candidate.kcal == Decimal("510")
    assert candidate.fat_g == Decimal("26.5")
    assert candidate.package_amount == Decimal("80")
    assert candidate.package_unit == "g"
    assert candidate.search_terms_ko == ["테스트 과자"]
    assert candidate.search_terms_ja == ["テスト菓子"]
    assert candidate.raw_data["categories_tags"] == ["en:snacks"]


def test_open_food_facts_requires_kcal() -> None:
    payload = {"status": "success", "product": {"product_name": "No nutrition"}}

    assert OpenFoodFactsCatalog.from_payload("8801234567890", payload) is None


def test_open_food_facts_respects_100ml_basis() -> None:
    payload = {
        "status": "success",
        "product": {
            "product_name": "테스트 음료",
            "quantity": "500 ml",
            "nutrition_data_per": "100ml",
            "nutriments": {
                "energy-kcal_100g": 42,
                "carbohydrates_100g": 10.5,
            },
        },
    }

    candidate = OpenFoodFactsCatalog.from_payload("1234567890123", payload)

    assert candidate is not None
    assert candidate.basis_unit == "ml"
    assert candidate.package_amount == Decimal("500")
    assert candidate.package_unit == "ml"


def test_open_food_facts_preserves_japanese_name_and_salt() -> None:
    payload = {
        "status": "success",
        "product": {
            "product_name_ja": "テストせんべい",
            "quantity": "100 g",
            "nutriments": {
                "energy-kcal_100g": 380,
                "carbohydrates_100g": 82,
                "proteins_100g": 7,
                "fat_100g": 2.5,
                "salt_100g": 1.2,
            },
        },
    }

    candidate = OpenFoodFactsCatalog.from_payload("4901234567894", payload)

    assert candidate is not None
    assert candidate.name == "テストせんべい"
    assert candidate.label_language == "ja"
    assert candidate.salt_equivalent_g == Decimal("1.2")
    assert candidate.sodium_mg == Decimal("472.4")
    assert candidate.sodium_derived is True
