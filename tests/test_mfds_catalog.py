from decimal import Decimal

import httpx
import pytest

from app.mfds_catalog import (
    MfdsCatalogError,
    MfdsFoodCatalog,
    candidate_from_item,
    food_match_score,
    ingredient_match_score,
    search_terms,
)


def mfds_item(**updates) -> dict:
    item = {
        "FOOD_CD": "R000001",
        "FOOD_NM_KR": "달걀, 삶은것",
        "FOOD_CAT1_NM": "난류",
        "SERVING_SIZE": "100 g",
        "AMT_NUM1": "143",
        "AMT_NUM3": "12.6",
        "AMT_NUM4": "9.5",
        "AMT_NUM6": "0.7",
        "AMT_NUM13": "140",
        "NUTRI_AMOUNT_SERVING": "1개(50 g)",
        "SUB_REF_NAME": "국가표준식품성분표",
        "UPDATE_DATE": "2026-08-28",
    }
    item.update(updates)
    return item


def test_candidate_maps_mfds_nutrients_and_piece_reference() -> None:
    candidate = candidate_from_item(mfds_item())

    assert candidate is not None
    assert candidate.external_source == "mfds_food_db"
    assert candidate.external_id == "R000001"
    assert candidate.basis_amount == Decimal("100")
    assert candidate.basis_unit == "g"
    assert candidate.kcal == Decimal("143")
    assert candidate.protein_g == Decimal("12.6")
    assert candidate.basis_count_amount == Decimal("2")
    assert candidate.basis_count_unit == "개"
    assert candidate.raw_data["quick_portions"] == [
        {
            "amount": "1",
            "unit": "piece",
            "label": "1개(참고 50g)",
            "piece_weight": "50",
        },
        {"amount": "2", "unit": "piece", "label": "2개(참고 100g)"},
    ]
    assert candidate.salt_equivalent_derived is True


def test_candidate_requires_complete_macro_values() -> None:
    assert candidate_from_item(mfds_item(AMT_NUM4="-")) is None


def test_search_terms_and_ranking_handle_common_korean_aliases() -> None:
    assert search_terms("삶은 계란") == ["삶은 계란", "삶은 달걀", "달걀"]
    assert search_terms("계란후라이") == [
        "계란후라이",
        "달걀후라이",
        "계란프라이",
        "달걀프라이",
        "달걀",
    ]
    assert food_match_score("삶은 계란", "달걀, 삶은것") >= 600
    assert food_match_score("삶은 계란", "달걀말이") < 600


async def test_search_parses_wrapped_json_and_deduplicates_results() -> None:
    requested_terms = []

    def handler(request: httpx.Request) -> httpx.Response:
        term = request.url.params["FOOD_NM_KR"]
        requested_terms.append(term)
        items = [mfds_item()] if term == "삶은 달걀" else []
        payload = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
                "body": {"items": {"item": items}},
            }
        }
        return httpx.Response(200, json=payload)

    catalog = MfdsFoodCatalog(
        "encoded%2Bkey",
        transport=httpx.MockTransport(handler),
    )
    results = await catalog.search("삶은 계란", limit=5)

    assert requested_terms == ["삶은 계란", "삶은 달걀"]
    assert len(results) == 1
    assert results[0].name == "달걀, 삶은것"


def test_exact_food_outranks_a_dish_that_merely_contains_it() -> None:
    names = ["삶은 달걀이 통째로 들어있는 쫄면", "달걀 · 삶은것", "달걀 · 삶은것 · 노른자"]

    ranked = sorted(names, key=lambda name: food_match_score("삶은 달걀", name), reverse=True)

    assert ranked[0] == "달걀 · 삶은것"


def test_ingredient_matching_rejects_processed_dishes_and_flavored_snacks() -> None:
    assert ingredient_match_score("양고기", "홍박쿠과자(양고기향)") == 0
    assert ingredient_match_score("양상추", "양상추 해초칩") == 0
    assert ingredient_match_score("토마토", "치즈오븐토마토파스타") == 0
    assert ingredient_match_score("양고기 또는 소고기", "양고기, 구운것") == 0


def test_ingredient_matching_accepts_basic_and_compound_ingredient_names() -> None:
    assert ingredient_match_score("토마토", "토마토, 생것") >= 900
    assert ingredient_match_score("구운 소고기", "소고기, 구운것") >= 900
    assert ingredient_match_score("밀가루 또띠아", "밀가루또띠아(8인치)") >= 900
    assert ingredient_match_score("요거트 소스", "델리아 요거트 소스") >= 900


async def test_search_does_not_stop_on_many_weak_processed_food_matches() -> None:
    requested_terms = []

    def handler(request: httpx.Request) -> httpx.Response:
        term = request.url.params["FOOD_NM_KR"]
        requested_terms.append(term)
        if term == "계란후라이":
            items = [
                mfds_item(
                    FOOD_CD=f"P{index}",
                    FOOD_NM_KR=f"계란후라이맛 과자 {index}",
                )
                for index in range(20)
            ]
        elif term == "달걀후라이":
            items = [mfds_item(FOOD_CD="D-FRIED-EGG", FOOD_NM_KR="달걀후라이")]
        else:
            items = []
        return httpx.Response(
            200,
            json={
                "header": {"resultCode": "00"},
                "body": {"items": {"item": items}},
            },
        )

    catalog = MfdsFoodCatalog("key", transport=httpx.MockTransport(handler))
    results = await catalog.search("계란후라이", limit=5)

    assert requested_terms == ["계란후라이", "달걀후라이"]
    assert results[0].name == "달걀후라이"


async def test_search_reports_api_rejection_without_exposing_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "header": {"resultCode": "30", "resultMsg": "unregistered key"},
                "body": {},
            },
        )

    catalog = MfdsFoodCatalog("top-secret", transport=httpx.MockTransport(handler))

    with pytest.raises(MfdsCatalogError, match="API 키") as error:
        await catalog.search("삶은 달걀")
    assert "top-secret" not in str(error.value)
