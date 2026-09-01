from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.recipe_ai import RecipeAIParser, _recipe_schema


def test_recipe_ai_schema_is_strict_and_bounded() -> None:
    schema = _recipe_schema(12)
    ingredient = schema["properties"]["ingredients"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])
    assert ingredient["maxItems"] == 12
    assert ingredient["items"]["additionalProperties"] is False
    assert set(ingredient["items"]["properties"]) == set(ingredient["items"]["required"])


async def test_recipe_ai_call_has_hard_output_and_tool_limits() -> None:
    parser = RecipeAIParser(
        "sk-test",
        "gpt-5.6-luna",
        max_output_tokens=800,
        max_ingredients=20,
    )
    parser.client.responses.create = AsyncMock(
        return_value=SimpleNamespace(
            output_text=(
                '{"recipe_name":"달걀밥","servings":1,"ingredients":['
                '{"raw_text":"달걀 두 개","name":"달걀","amount":2,'
                '"unit":"piece","preparation":"unknown","note":null}]}'
            ),
            usage=SimpleNamespace(input_tokens=200, output_tokens=80, total_tokens=280),
        )
    )

    result = await parser.parse(raw_text="달걀 두 개", name_hint="달걀밥", user_id=1234)
    kwargs = parser.client.responses.create.await_args.kwargs
    await parser.client.close()

    assert kwargs["store"] is False
    assert kwargs["reasoning"] == {"effort": "none"}
    assert kwargs["max_output_tokens"] == 800
    assert kwargs["tools"] == []
    assert kwargs["tool_choice"] == "none"
    assert kwargs["text"]["format"]["strict"] is True
    assert result.total_tokens == 280
