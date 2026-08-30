from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote

import httpx

from app.nutrition import normalize_salt
from app.schemas import ProductCandidate

MFDS_SOURCE = "mfds_food_db"


class MfdsCatalogError(RuntimeError):
    """A safe-to-display error raised by the MFDS public-data adapter."""


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).strip().split())


def _decimal(value: object) -> Decimal | None:
    text = _clean_text(value).replace(",", "")
    if not text or text in {"-", "—", "N/A", "NA"}:
        return None
    match = re.search(r"-?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if match is None:
        return None
    try:
        result = Decimal(match.group(0))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result >= 0 else None


def _metric_portion(
    value: object, *, default_unit: str | None = None
) -> tuple[Decimal, str] | None:
    text = _clean_text(value).lower()
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(ml|mℓ|cc|g|그램|밀리리터)", text)
    if match:
        unit = match.group(2)
        return Decimal(match.group(1)), "ml" if unit in {"ml", "mℓ", "cc", "밀리리터"} else "g"
    if default_unit in {"g", "ml"} and re.fullmatch(r"\d+(?:\.\d+)?", text):
        return Decimal(text), default_unit
    return None


def _basis(value: object) -> tuple[Decimal, str] | None:
    metric = _metric_portion(value, default_unit="g")
    if metric is None or metric[0] <= 0:
        return None
    return metric


_ALIASES = {
    "계란": "달걀",
    "후라이": "프라이",
}
_COOKING_WORDS = {
    "구운",
    "구이",
    "날것",
    "생것",
    "삶",
    "삶기",
    "삶은",
    "익힌",
    "조림",
    "찐",
    "찜",
    "튀긴",
    "튀김",
    "프라이",
}


def _alias_text(value: str) -> str:
    result = value
    for old, new in _ALIASES.items():
        result = result.replace(old, new)
    return result


def _tokens(value: str) -> list[str]:
    normalized = _alias_text(unicodedata.normalize("NFKC", value).lower())
    normalized = normalized.replace("삶은것", "삶").replace("삶은", "삶")
    normalized = normalized.replace("구운것", "구이").replace("구운", "구이")
    return [token for token in re.findall(r"[0-9a-z가-힣]+", normalized) if token]


def search_terms(query: str) -> list[str]:
    clean = _clean_text(query)
    if not clean:
        return []
    results: list[str] = []

    egg_aliased = clean.replace("계란", "달걀")
    variants = [clean, egg_aliased]
    for term in list(variants):
        if "후라이" in term:
            variants.append(term.replace("후라이", "프라이"))
        if "프라이" in term:
            variants.append(term.replace("프라이", "후라이"))
    for term in variants:
        if term and term not in results:
            results.append(term)

    aliased = _alias_text(clean)
    content_tokens = [
        token
        for token in re.findall(r"[0-9a-z가-힣]+", aliased.lower())
        if len(token) >= 2 and token not in _COOKING_WORDS
    ]
    for token in list(content_tokens):
        for cooking_word in sorted(_COOKING_WORDS, key=len, reverse=True):
            if token.endswith(cooking_word) and len(token) > len(cooking_word) + 1:
                content_tokens.append(token[: -len(cooking_word)])
                break
    for token in sorted(content_tokens, key=len, reverse=True):
        if token not in results:
            results.append(token)
    return results[:5]


def food_match_score(query: str, name: str) -> int:
    query_tokens = _tokens(query)
    name_tokens = _tokens(name)
    if not query_tokens or not name_tokens:
        return 0
    query_joined = " ".join(query_tokens)
    name_joined = " ".join(name_tokens)
    if query_joined == name_joined:
        return 1_000
    matched = sum(1 for token in query_tokens if any(token in item for item in name_tokens))
    if matched == 0:
        return 0
    score = matched * 100
    if matched == len(query_tokens):
        score += 500
    if set(query_tokens) == set(name_tokens):
        score += 400
    if query_joined in name_joined:
        score += 50
    score -= max(0, len(name_tokens) - len(query_tokens)) * 40
    return score


def _piece_reference(
    item: dict, basis_amount: Decimal, basis_unit: str
) -> tuple[Decimal | None, dict | None]:
    serving_text = _clean_text(item.get("NUTRI_AMOUNT_SERVING"))
    count_match = re.search(r"(\d+(?:\.\d+)?)\s*(개|알)", serving_text)
    if count_match is None:
        return None, None
    count = Decimal(count_match.group(1))
    metric = _metric_portion(serving_text)
    if metric is None:
        metric = _metric_portion(item.get("DISH_ONE_SERVING"), default_unit=basis_unit)
    if metric is None or metric[1] != basis_unit or metric[0] <= 0 or count <= 0:
        return None, None
    piece_weight = metric[0] / count
    basis_count = basis_amount / piece_weight
    return basis_count, {
        "amount": "1",
        "unit": "piece",
        "label": f"1개(참고 {piece_weight.normalize():f}{basis_unit})",
        "piece_weight": str(piece_weight),
    }


def _quick_portions(
    item: dict, basis_amount: Decimal, basis_unit: str
) -> tuple[list[dict], Decimal | None]:
    basis_count, piece = _piece_reference(item, basis_amount, basis_unit)
    if piece is not None:
        piece_weight = Decimal(piece["piece_weight"])
        return [
            piece,
            {
                "amount": "2",
                "unit": "piece",
                "label": f"2개(참고 {(piece_weight * 2).normalize():f}{basis_unit})",
            },
        ], basis_count

    for key in ("NUTRI_AMOUNT_SERVING", "DISH_ONE_SERVING"):
        metric = _metric_portion(item.get(key), default_unit=basis_unit)
        if metric is None or metric[0] <= 0:
            continue
        amount, unit = metric
        if unit != basis_unit or amount == basis_amount:
            continue
        return [
            {
                "amount": str(amount),
                "unit": unit,
                "label": f"참고 1회분 {amount.normalize():f}{unit}",
            }
        ], None
    return [], None


def candidate_from_item(item: dict) -> ProductCandidate | None:
    external_id = _clean_text(item.get("FOOD_CD"))
    source_food_name = _clean_text(item.get("FOOD_NM_KR"))
    name = " · ".join(part.strip() for part in source_food_name.split("_") if part.strip())
    basis = _basis(item.get("SERVING_SIZE"))
    kcal = _decimal(item.get("AMT_NUM1"))
    protein = _decimal(item.get("AMT_NUM3"))
    fat = _decimal(item.get("AMT_NUM4"))
    carbs = _decimal(item.get("AMT_NUM6"))
    if not external_id or not name or basis is None or None in (kcal, protein, fat, carbs):
        return None

    basis_amount, basis_unit = basis
    quick_portions, basis_count = _quick_portions(item, basis_amount, basis_unit)
    sodium = _decimal(item.get("AMT_NUM13"))
    salt_equivalent = _decimal(item.get("AMT_NUM156"))
    salt = normalize_salt(sodium, salt_equivalent)
    category = _clean_text(item.get("FOOD_CAT1_NM"))
    maker = _clean_text(item.get("MAKER_NM")) or None
    source_name = _clean_text(item.get("SUB_REF_NAME"))

    raw_data = {
        "food_code": external_id,
        "source_food_name": source_food_name,
        "database_group": _clean_text(item.get("DB_GRP_NM")),
        "category": category,
        "source_name": source_name,
        "serving_size": _clean_text(item.get("SERVING_SIZE")),
        "serving_reference": _clean_text(item.get("NUTRI_AMOUNT_SERVING")),
        "dish_one_serving": _clean_text(item.get("DISH_ONE_SERVING")),
        "food_weight": _clean_text(item.get("Z10500")),
        "data_updated_at": _clean_text(item.get("UPDATE_DATE")),
        "quick_portions": quick_portions,
    }
    return ProductCandidate(
        external_source=MFDS_SOURCE,
        external_id=external_id,
        name=name,
        brand=maker,
        basis_amount=basis_amount,
        basis_unit=basis_unit,
        basis_metric_amount=basis_amount,
        basis_metric_unit=basis_unit,
        basis_count_amount=basis_count,
        basis_count_unit="개" if basis_count is not None else None,
        kcal=kcal,
        carbs_g=carbs,
        protein_g=protein,
        fat_g=fat,
        sodium_mg=salt.sodium_mg,
        salt_equivalent_g=salt.salt_equivalent_g,
        sodium_derived=salt.sodium_derived,
        salt_equivalent_derived=salt.salt_equivalent_derived,
        label_language="ko",
        source=MFDS_SOURCE,
        verified=True,
        raw_data=raw_data,
    )


def _response_items(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise MfdsCatalogError("식약처 DB 응답 형식을 확인하지 못했습니다.")
    response = payload.get("response", payload)
    if not isinstance(response, dict):
        raise MfdsCatalogError("식약처 DB 응답 형식을 확인하지 못했습니다.")
    header = response.get("header") or {}
    if isinstance(header, dict):
        code = str(header.get("resultCode") or "00")
        if code not in {"00", "0000", "INFO-000"}:
            raise MfdsCatalogError(
                "식약처 DB 요청이 거절되었습니다. API 키와 사용 승인을 확인하세요."
            )
    body = response.get("body") or {}
    if not isinstance(body, dict):
        return []
    items = body.get("items") or {}
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


class MfdsFoodCatalog:
    base_url = "https://apis.data.go.kr/1471000/FoodNtrCpntDbInfo02/getFoodNtrCpntDbInq02"

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = unquote(api_key.strip())
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def search(self, query: str, *, limit: int = 5) -> list[ProductCandidate]:
        if not self.api_key:
            raise MfdsCatalogError("MFDS_API_KEY가 설정되지 않았습니다.")
        terms = search_terms(query)
        if not terms:
            return []

        candidates: dict[str, ProductCandidate] = {}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                for term in terms:
                    response = await client.get(
                        self.base_url,
                        params={
                            "serviceKey": self.api_key,
                            "pageNo": 1,
                            "numOfRows": max(20, limit * 4),
                            "type": "json",
                            "FOOD_NM_KR": term,
                        },
                    )
                    response.raise_for_status()
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise MfdsCatalogError(
                            "식약처 DB가 JSON이 아닌 오류 응답을 반환했습니다."
                        ) from exc
                    for item in _response_items(payload):
                        candidate = candidate_from_item(item)
                        if candidate and candidate.external_id:
                            candidates[candidate.external_id] = candidate
                    if any(
                        food_match_score(query, candidate.name) >= 900
                        for candidate in candidates.values()
                    ):
                        break
        except MfdsCatalogError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MfdsCatalogError("식약처 DB에 일시적으로 연결하지 못했습니다.") from exc

        ranked = sorted(
            candidates.values(),
            key=lambda candidate: (
                food_match_score(query, candidate.name),
                candidate.name,
            ),
            reverse=True,
        )
        return [candidate for candidate in ranked if food_match_score(query, candidate.name) > 0][
            :limit
        ]
