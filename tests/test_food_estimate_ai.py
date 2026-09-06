from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.food_estimate_ai import (
    FOOD_ESTIMATE_VERSION,
    FoodEstimateError,
    FoodEstimatePlan,
    FoodEstimatePlanner,
    _food_estimate_schema,
    food_estimate_query_hash,
    is_usable_food_estimate,
    normalize_food_estimate_query,
)


def _plan_payload() -> str:
    return (
        '{"supported":true,"reason":null,"dish_name":"케밥 랩","ingredients":['
        '{"raw_text":"또띠아 60g","name":"또띠아","amount":60,"unit":"g",'
        '"preparation":"cooked","note":null},'
        '{"raw_text":"닭고기 100g","name":"닭고기","amount":100,"unit":"g",'
        '"preparation":"cooked","note":null}],'
        '"assumptions":["보통 크기","닭고기 기준"],"uncertainty_percent":30,'
        '"confidence":0.7,"search_terms_ko":["케밥 랩"],'
        '"search_terms_ja":["ケバブ ラップ"]}'
    )


def test_food_estimate_schema_is_strict_and_bounded() -> None:
    schema = _food_estimate_schema(12)
    ingredients = schema["properties"]["ingredients"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    assert ingredients["maxItems"] == 12
    assert ingredients["items"]["additionalProperties"] is False
    assert ingredients["items"]["properties"]["unit"]["enum"] == ["g", "ml"]


def test_food_estimate_query_is_normalized_bounded_and_versioned() -> None:
    assert FOOD_ESTIMATE_VERSION == "food-estimate-v2"
    assert normalize_food_estimate_query("  ｹﾊﾞﾌﾞ   ﾗｯﾌﾟ ", max_chars=80) == "ケバブ ラップ"
    assert food_estimate_query_hash("케밥  랩") == food_estimate_query_hash("케밥 랩")

    with pytest.raises(FoodEstimateError):
        normalize_food_estimate_query("https://example.com/kebab", max_chars=80)
    with pytest.raises(FoodEstimateError):
        normalize_food_estimate_query("x" * 81, max_chars=80)


def test_supported_plan_requires_metric_ingredients() -> None:
    with pytest.raises(ValidationError):
        FoodEstimatePlan.model_validate(
            {
                "supported": True,
                "reason": None,
                "dish_name": "음식",
                "ingredients": [
                    {
                        "raw_text": "달걀 1개",
                        "name": "달걀",
                        "amount": 1,
                        "unit": "piece",
                        "preparation": "cooked",
                        "note": None,
                    }
                ],
                "assumptions": [],
                "uncertainty_percent": 30,
                "confidence": 0.8,
                "search_terms_ko": [],
                "search_terms_ja": [],
            }
        )


async def test_food_estimate_call_has_no_tools_and_hard_output_limit() -> None:
    planner = FoodEstimatePlanner(
        "sk-test",
        "gpt-5.6-luna",
        max_output_tokens=600,
        max_ingredients=12,
    )
    planner.client.responses.create = AsyncMock(
        return_value=SimpleNamespace(
            output_text=_plan_payload(),
            usage=SimpleNamespace(input_tokens=160, output_tokens=220, total_tokens=380),
        )
    )

    result = await planner.estimate(query="ケバブ ラップ", user_id=1234)
    kwargs = planner.client.responses.create.await_args.kwargs
    await planner.client.close()

    assert kwargs["store"] is False
    assert kwargs["reasoning"] == {"effort": "none"}
    assert kwargs["max_output_tokens"] == 600
    assert kwargs["tools"] == []
    assert kwargs["tool_choice"] == "none"
    assert "Use g for solid foods and ml for pourable liquids" in kwargs["instructions"]
    assert kwargs["text"]["format"]["strict"] is True
    assert kwargs["text"]["format"]["schema"]["properties"]["ingredients"]["maxItems"] == 12
    assert result.total_tokens == 380
    assert is_usable_food_estimate(result.plan) is True
    assert result.plan.uncertainty_percent == Decimal("30")
