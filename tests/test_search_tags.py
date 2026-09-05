from types import SimpleNamespace

from app.search_tags import (
    build_product_search_terms,
    concept_keys_for_query,
    matching_term,
    normalize_search_term,
)


def test_normalization_folds_spacing_width_case_and_kana() -> None:
    assert normalize_search_term(" 닭 가슴살 ") == "닭가슴살"
    assert normalize_search_term("ＣＯＬＡ") == "cola"
    assert normalize_search_term("コーヒー") == normalize_search_term("こーひー")


def test_japanese_egg_name_creates_korean_and_japanese_aliases() -> None:
    terms = build_product_search_terms(
        name="ゆで卵",
        brand=None,
        product_source="ai_label",
    )
    values = {term.term for term in terms}
    concepts = {term.concept_key for term in terms}

    assert {"달걀", "계란", "卵", "ゆで"} <= values
    assert {"egg", "boiled"} <= concepts


def test_product_concept_hierarchy_expands_cola_to_beverage() -> None:
    terms = build_product_search_terms(
        name="제로 콜라",
        brand=None,
        product_source="ai_label",
    )

    assert concept_keys_for_query("제로 콜라") == {"cola"}
    assert {"cola", "carbonated_drink", "beverage"} <= {term.concept_key for term in terms}


def test_english_aliases_use_word_boundaries() -> None:
    terms = build_product_search_terms(
        name="Beef steak",
        brand=None,
        product_source="open_food_facts",
    )

    assert "tea" not in {term.concept_key for term in terms}


def test_ai_terms_reject_subjective_and_url_values() -> None:
    terms = build_product_search_terms(
        name="제품",
        brand=None,
        search_terms_ko=["다이어트", "상큼한 레몬", "https://bad.example"],
        product_source="ai_label",
    )
    values = {term.term for term in terms}

    assert "상큼한 레몬" in values
    assert "다이어트" not in values
    assert "https://bad.example" not in values


def test_matching_term_explains_cross_language_match() -> None:
    specs = build_product_search_terms(
        name="サラダチキン",
        brand=None,
        product_source="ai_label",
    )
    terms = [
        SimpleNamespace(
            term=spec.term,
            normalized_term=spec.normalized_term,
            concept_key=spec.concept_key,
        )
        for spec in specs
    ]

    assert matching_term("닭가슴살", terms) == "닭가슴살"
