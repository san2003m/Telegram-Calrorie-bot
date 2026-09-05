from decimal import Decimal
from types import SimpleNamespace

from app.menu_ai import MenuNutritionEvidence
from app.nutrition import MacroTotals
from app.portion import ParsedPortion
from app.recipe import RecipeDraft, ResolvedRecipeIngredient
from app.schemas import Nutrients, NutritionBasis, NutritionRecognition
from app.telegram import (
    MenuSearchDraft,
    _can_correct_basis_unit,
    _candidate_from_recognition,
    _candidate_with_basis_unit,
    _format_uptime,
    _menu_candidate,
    _product_text,
    _recipe_candidate,
    _recognition_follow_up_text,
    _recognition_is_complete,
    _recognition_result_text,
    _stored_quick_portions,
)


def test_format_uptime() -> None:
    assert _format_uptime(0.9) == "0초"
    assert _format_uptime(65) == "1분 5초"
    assert _format_uptime(90_061) == "1일 1시간 1분 1초"


def test_format_uptime_does_not_show_negative_time() -> None:
    assert _format_uptime(-10) == "0초"


def test_liquid_unit_correction_creates_private_candidate_data() -> None:
    product = SimpleNamespace(barcode="4902102084178", name="콜라", brand="테스트")
    version = SimpleNamespace(
        product=product,
        basis_amount=Decimal("100"),
        basis_unit="g",
        package_amount=Decimal("500"),
        package_unit="ml",
        servings_per_package=None,
        piece_count=None,
        kcal=Decimal("0"),
        carbs_g=Decimal("0"),
        protein_g=Decimal("0"),
        fat_g=Decimal("0"),
        raw_data={"quantity": "500 ml"},
    )
    portion = ParsedPortion(Decimal("500"), "ml")

    assert _can_correct_basis_unit(version, portion) is True
    candidate = _candidate_with_basis_unit(version, "ml")
    assert candidate.basis_unit == "ml"
    assert candidate.source == "user_correction"
    assert candidate.raw_data["user_basis_unit_correction"] == {
        "from": "g",
        "to": "ml",
    }


def test_japanese_recognition_preserves_basis_and_converts_salt() -> None:
    result = NutritionRecognition(
        label_found=True,
        label_market="JP",
        label_language="ja",
        product_name="テスト飲料",
        nutrition_basis=NutritionBasis(
            amount=Decimal("200"),
            unit="ml",
            raw_text="1本（200ml）当たり",
            metric_amount=Decimal("200"),
            metric_unit="ml",
            count_amount=Decimal("1"),
            count_unit="本",
        ),
        nutrients=Nutrients(
            energy_kcal=Decimal("140"),
            carbs_g=Decimal("10"),
            protein_g=Decimal("7"),
            fat_g=Decimal("8"),
            salt_equivalent_g=Decimal("0.8"),
        ),
        package_amount={"amount": Decimal("200"), "unit": "ml"},
        piece_count=Decimal("1"),
        confidence=Decimal("0.95"),
    )

    candidate = _candidate_from_recognition("4901234567894", result)
    text = _recognition_result_text(result)

    assert candidate.label_market == "JP"
    assert candidate.basis_text == "1本（200ml）当たり"
    assert candidate.basis_count_amount == Decimal("1")
    assert candidate.basis_count_unit == "本"
    assert candidate.sodium_mg == Decimal("315.0")
    assert candidate.sodium_derived is True
    assert "🇯🇵 일본" in text
    assert "식염상당량 0.8 g" in text
    assert "나트륨 315 mg (식염상당량에서 환산)" in text


def test_small_salt_value_keeps_required_precision() -> None:
    result = NutritionRecognition(
        label_found=True,
        label_market="JP",
        label_language="ja",
        product_name="無塩テスト",
        nutrition_basis=NutritionBasis(amount=Decimal("100"), unit="g"),
        nutrients=Nutrients(
            energy_kcal=Decimal("10"),
            carbs_g=Decimal("1"),
            protein_g=Decimal("0"),
            fat_g=Decimal("0"),
            salt_equivalent_g=Decimal("0.02"),
        ),
        confidence=Decimal("0.95"),
    )

    assert "식염상당량 0.02 g" in _recognition_result_text(result)


def test_single_photo_can_complete_product_recognition() -> None:
    result = NutritionRecognition(
        label_found=True,
        product_name_found=True,
        label_market="KR",
        label_language="ko",
        product_name="한 장 인식 제품",
        nutrition_basis=NutritionBasis(amount=Decimal("100"), unit="g"),
        nutrients=Nutrients(
            energy_kcal=Decimal("120"),
            carbs_g=Decimal("10"),
            protein_g=Decimal("5"),
            fat_g=Decimal("6"),
        ),
        confidence=Decimal("0.9"),
    )

    assert _recognition_is_complete(result) is True


def test_recognition_requests_only_the_missing_label_photo() -> None:
    result = NutritionRecognition(
        label_found=False,
        product_name_found=True,
        product_name="제품명만 보이는 상품",
        nutrition_basis=NutritionBasis(amount=Decimal("1"), unit="serving"),
        nutrients=Nutrients(
            energy_kcal=Decimal("0"),
            carbs_g=Decimal("0"),
            protein_g=Decimal("0"),
            fat_g=Decimal("0"),
        ),
        confidence=Decimal("0.6"),
    )

    follow_up = _recognition_follow_up_text(result)

    assert _recognition_is_complete(result) is False
    assert "제품명" in follow_up
    assert "영양정보 표" in follow_up
    assert "앞면 사진" not in follow_up


def test_recognition_requests_only_the_missing_front_photo() -> None:
    result = NutritionRecognition(
        label_found=True,
        product_name_found=False,
        product_name="확인 불가",
        nutrition_basis=NutritionBasis(amount=Decimal("100"), unit="g"),
        nutrients=Nutrients(
            energy_kcal=Decimal("120"),
            carbs_g=Decimal("10"),
            protein_g=Decimal("5"),
            fat_g=Decimal("6"),
        ),
        confidence=Decimal("0.6"),
    )

    follow_up = _recognition_follow_up_text(result)

    assert _recognition_is_complete(result) is False
    assert "영양정보 표는 확인" in follow_up
    assert "앞면 사진" in follow_up


def test_mfds_piece_shortcuts_use_metric_reference() -> None:
    version = SimpleNamespace(
        basis_amount=Decimal("100"),
        basis_unit="g",
        package_amount=None,
        package_unit=None,
        servings_per_package=None,
        piece_count=None,
        basis_count_amount=Decimal("2"),
        raw_data={
            "quick_portions": [
                {"amount": "1", "unit": "piece", "label": "1개(참고 50g)"},
                {"amount": "2", "unit": "piece", "label": "2개(참고 100g)"},
            ]
        },
    )

    shortcuts = _stored_quick_portions(version)

    assert shortcuts == [
        ("1개(참고 50g)", ParsedPortion(Decimal("1"), "piece")),
        ("2개(참고 100g)", ParsedPortion(Decimal("2"), "piece")),
    ]


def test_recipe_candidate_is_saved_per_serving_with_ingredient_snapshot() -> None:
    ingredient_totals = MacroTotals(
        kcal=Decimal("400"),
        carbs_g=Decimal("60"),
        protein_g=Decimal("20"),
        fat_g=Decimal("10"),
    )
    draft = RecipeDraft(
        draft_id="draft",
        user_id=1234,
        input_hash="a" * 64,
        name="달걀밥",
        servings=Decimal("2"),
        used_ai=False,
        ingredients=(
            ResolvedRecipeIngredient(
                input_name="달걀",
                matched_name="달걀, 삶은것",
                amount=Decimal("2"),
                unit="piece",
                multiplier=Decimal("1"),
                version_id=10,
                source="mfds_food_db",
                totals=ingredient_totals,
            ),
        ),
        total=ingredient_totals,
    )

    result = _recipe_candidate(draft)

    assert result.basis_unit == "serving"
    assert result.servings_per_package == Decimal("2")
    assert result.kcal == Decimal("200.0")
    assert result.external_id == f"1234:{'a' * 56}"
    assert result.raw_data["recipe"]["ingredients"][0]["product_version_id"] == 10


def test_official_menu_candidate_keeps_verified_source_and_url() -> None:
    evidence = MenuNutritionEvidence(
        found=True,
        official_source=True,
        brand="테스트카페",
        menu_name="카페 라떼 Tall",
        basis_text="Tall 1잔 (355 ml)",
        basis_amount=Decimal("1"),
        basis_unit="serving",
        kcal=Decimal("180"),
        carbs_g=Decimal("18"),
        protein_g=Decimal("9"),
        fat_g=Decimal("8"),
        source_url="https://brand.example/menu/latte",
        source_title="테스트카페 공식 영양정보",
        confidence=Decimal("0.95"),
    )
    draft = MenuSearchDraft(
        draft_id="menu-draft",
        user_id=1234,
        query="테스트카페 카페 라떼 Tall",
        input_hash="m" * 64,
        evidence=evidence,
        searched_urls=("https://brand.example/menu/latte",),
        from_cache=False,
    )

    result = _menu_candidate(draft)

    assert result.external_source == "brand_menu"
    assert result.external_id == "m" * 64
    assert result.servings_per_package == Decimal("1")
    assert result.verified is True
    assert result.estimated_values is False
    assert result.raw_data["official_menu"]["source_url"] == evidence.source_url


def test_official_menu_source_is_visible_in_product_text() -> None:
    product = SimpleNamespace(name="카페 라떼 Tall", brand="테스트카페")
    version = SimpleNamespace(
        product=product,
        basis_amount=Decimal("1"),
        basis_unit="serving",
        basis_text="Tall 1잔 (355 ml)",
        kcal=Decimal("180"),
        carbs_g=Decimal("18"),
        protein_g=Decimal("9"),
        fat_g=Decimal("8"),
        sodium_mg=None,
        salt_equivalent_g=None,
        estimated_values=False,
        source="brand_menu",
        package_amount=None,
        package_unit=None,
        servings_per_package=Decimal("1"),
        piece_count=None,
        label_market="UNKNOWN",
        raw_data={
            "official_menu": {
                "source_title": "테스트카페 공식 영양정보",
                "source_url": "https://brand.example/menu/latte",
            }
        },
    )

    text = _product_text(version)

    assert "출처: 테스트카페 공식 영양정보" in text
    assert "메뉴 단위: 1회분" in text
    assert "https://brand.example/menu/latte" in text
