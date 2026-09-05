from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TagDefinition:
    key: str
    kind: str
    aliases: Mapping[str, tuple[str, ...]]
    parents: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchTermSpec:
    term: str
    normalized_term: str
    locale: str
    kind: str
    source: str
    concept_key: str | None
    confidence: Decimal


TAG_DEFINITIONS = (
    TagDefinition(
        "beverage",
        "category",
        {"ko": ("음료", "음료수"), "ja": ("飲料", "ドリンク"), "en": ("beverage", "drink")},
    ),
    TagDefinition(
        "carbonated_drink",
        "category",
        {
            "ko": ("탄산", "탄산음료"),
            "ja": ("炭酸", "炭酸飲料"),
            "en": ("carbonated drink", "soda"),
        },
        ("beverage",),
    ),
    TagDefinition(
        "cola", "type", {"ko": ("콜라",), "ja": ("コーラ",), "en": ("cola",)}, ("carbonated_drink",)
    ),
    TagDefinition(
        "coffee",
        "type",
        {"ko": ("커피",), "ja": ("コーヒー", "珈琲"), "en": ("coffee",)},
        ("beverage",),
    ),
    TagDefinition(
        "tea",
        "type",
        {
            "ko": ("차음료", "티음료", "녹차", "홍차"),
            "ja": ("お茶", "茶飲料", "ティー"),
            "en": ("tea",),
        },
        ("beverage",),
    ),
    TagDefinition(
        "milk",
        "type",
        {"ko": ("우유",), "ja": ("牛乳", "ミルク"), "en": ("milk",)},
        ("beverage", "dairy"),
    ),
    TagDefinition("dairy", "category", {"ko": ("유제품",), "ja": ("乳製品",), "en": ("dairy",)}),
    TagDefinition(
        "yogurt",
        "type",
        {"ko": ("요거트", "요구르트"), "ja": ("ヨーグルト",), "en": ("yogurt", "yoghurt")},
        ("dairy",),
    ),
    TagDefinition(
        "cheese", "type", {"ko": ("치즈",), "ja": ("チーズ",), "en": ("cheese",)}, ("dairy",)
    ),
    TagDefinition(
        "egg",
        "ingredient",
        {"ko": ("달걀", "계란"), "ja": ("卵", "たまご", "玉子"), "en": ("egg",)},
    ),
    TagDefinition(
        "boiled", "preparation", {"ko": ("삶은", "삶기"), "ja": ("ゆで", "茹で"), "en": ("boiled",)}
    ),
    TagDefinition(
        "chicken",
        "ingredient",
        {"ko": ("닭", "닭고기", "치킨"), "ja": ("鶏", "鶏肉", "チキン"), "en": ("chicken",)},
    ),
    TagDefinition(
        "chicken_breast",
        "ingredient",
        {
            "ko": ("닭가슴살", "닭 가슴살"),
            "ja": ("鶏むね肉", "鶏胸肉", "サラダチキン"),
            "en": ("chicken breast",),
        },
        ("chicken",),
    ),
    TagDefinition(
        "beef",
        "ingredient",
        {"ko": ("소고기", "쇠고기"), "ja": ("牛肉", "ビーフ"), "en": ("beef",)},
    ),
    TagDefinition(
        "pork",
        "ingredient",
        {"ko": ("돼지고기", "돈육"), "ja": ("豚肉", "ポーク"), "en": ("pork",)},
    ),
    TagDefinition(
        "fish", "ingredient", {"ko": ("생선", "어류"), "ja": ("魚", "フィッシュ"), "en": ("fish",)}
    ),
    TagDefinition(
        "seafood",
        "ingredient",
        {"ko": ("해산물", "수산물"), "ja": ("魚介", "シーフード"), "en": ("seafood",)},
    ),
    TagDefinition("tofu", "ingredient", {"ko": ("두부",), "ja": ("豆腐",), "en": ("tofu",)}),
    TagDefinition(
        "rice",
        "ingredient",
        {"ko": ("밥", "쌀", "쌀밥"), "ja": ("ご飯", "米", "ライス"), "en": ("rice",)},
    ),
    TagDefinition(
        "noodle",
        "category",
        {
            "ko": ("면", "면류", "국수"),
            "ja": ("麺", "麺類", "ヌードル"),
            "en": ("noodle", "noodles"),
        },
    ),
    TagDefinition(
        "ramen",
        "type",
        {"ko": ("라면", "라멘"), "ja": ("ラーメン", "らーめん"), "en": ("ramen",)},
        ("noodle",),
    ),
    TagDefinition(
        "bread",
        "category",
        {"ko": ("빵", "베이커리"), "ja": ("パン", "ベーカリー"), "en": ("bread", "bakery")},
    ),
    TagDefinition(
        "snack", "category", {"ko": ("과자", "스낵"), "ja": ("菓子", "スナック"), "en": ("snack",)}
    ),
    TagDefinition(
        "dessert", "category", {"ko": ("디저트", "후식"), "ja": ("デザート",), "en": ("dessert",)}
    ),
    TagDefinition(
        "chocolate",
        "flavor",
        {"ko": ("초콜릿", "초콜렛"), "ja": ("チョコレート", "チョコ"), "en": ("chocolate",)},
    ),
    TagDefinition(
        "fruit", "ingredient", {"ko": ("과일",), "ja": ("果物", "フルーツ"), "en": ("fruit",)}
    ),
    TagDefinition(
        "vegetable", "ingredient", {"ko": ("채소", "야채"), "ja": ("野菜",), "en": ("vegetable",)}
    ),
    TagDefinition(
        "soup",
        "category",
        {"ko": ("국물요리", "수프", "스프"), "ja": ("スープ", "汁物"), "en": ("soup",)},
    ),
    TagDefinition(
        "sauce",
        "category",
        {"ko": ("소스", "양념"), "ja": ("ソース", "たれ", "タレ"), "en": ("sauce",)},
    ),
    TagDefinition(
        "ready_meal",
        "category",
        {
            "ko": ("즉석식품", "즉석조리식품", "간편식"),
            "ja": ("即席食品", "惣菜", "レトルト"),
            "en": ("ready meal", "instant food"),
        },
    ),
    TagDefinition(
        "frozen_food",
        "category",
        {"ko": ("냉동", "냉동식품"), "ja": ("冷凍", "冷凍食品"), "en": ("frozen food",)},
    ),
    TagDefinition(
        "canned_food",
        "category",
        {"ko": ("통조림", "캔식품"), "ja": ("缶詰",), "en": ("canned food",)},
    ),
    TagDefinition(
        "supplement",
        "category",
        {
            "ko": ("건강기능식품", "영양제", "보충제"),
            "ja": ("サプリ", "サプリメント"),
            "en": ("supplement",),
        },
    ),
    TagDefinition(
        "protein_product",
        "category",
        {
            "ko": ("프로틴", "단백질제품"),
            "ja": ("プロテイン", "たんぱく質食品", "タンパク質食品"),
            "en": ("protein product", "protein"),
        },
    ),
    TagDefinition(
        "sugar_free",
        "attribute",
        {
            "ko": ("무설탕", "무가당", "제로슈거", "제로당"),
            "ja": ("無糖", "砂糖不使用", "ゼロシュガー"),
            "en": ("sugar free", "no sugar"),
        },
    ),
    TagDefinition(
        "high_protein",
        "attribute",
        {
            "ko": ("고단백", "단백질강화"),
            "ja": ("高たんぱく", "高タンパク"),
            "en": ("high protein",),
        },
    ),
    TagDefinition(
        "spicy",
        "attribute",
        {"ko": ("매운맛", "매콤", "불닭"), "ja": ("辛口", "激辛", "スパイシー"), "en": ("spicy",)},
    ),
    TagDefinition(
        "caffeine_free",
        "attribute",
        {
            "ko": ("무카페인", "디카페인"),
            "ja": ("カフェインレス", "デカフェ"),
            "en": ("caffeine free", "decaf"),
        },
    ),
    TagDefinition("curry", "type", {"ko": ("카레", "커리"), "ja": ("カレー",), "en": ("curry",)}),
)

SEARCH_CONCEPT_KEYS = tuple(definition.key for definition in TAG_DEFINITIONS)
_DEFINITION_BY_KEY = {definition.key: definition for definition in TAG_DEFINITIONS}
_SAFE_SINGLE_CHARACTER_ALIASES = {"닭", "밥", "쌀", "빵", "면", "卵", "鶏", "魚", "米", "麺"}
_BLOCKED_AI_TERMS = {
    "건강식",
    "다이어트",
    "체중감량",
    "살빠지는음식",
    "healthy",
    "diet",
    "weightloss",
    "健康食",
    "ダイエット",
    "減量",
}


def normalize_search_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    kana_folded = "".join(
        chr(ord(character) - 0x60) if "ァ" <= character <= "ヶ" else character
        for character in normalized
    )
    return "".join(character for character in kana_folded if character.isalnum())


def detect_locale(value: str) -> str:
    has_korean = bool(re.search(r"[가-힣]", value))
    has_japanese = bool(re.search(r"[ぁ-ゖァ-ヶ一-龯]", value))
    if has_korean and has_japanese:
        return "mixed"
    if has_korean:
        return "ko"
    if has_japanese:
        return "ja"
    return "und"


def _is_matchable_alias(alias: str) -> bool:
    normalized = normalize_search_term(alias)
    return len(normalized) >= 2 or alias in _SAFE_SINGLE_CHARACTER_ALIASES


def _alias_occurs(alias: str, value: str, normalized_value: str) -> bool:
    if alias.isascii() and any(character.isalpha() for character in alias):
        words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", alias).casefold())
        if not words:
            return False
        pattern = r"(?<![a-z0-9])" + r"[^a-z0-9]*".join(map(re.escape, words))
        pattern += r"(?![a-z0-9])"
        return bool(re.search(pattern, unicodedata.normalize("NFKC", value).casefold()))
    return normalize_search_term(alias) in normalized_value


def _concepts_in_text(value: str) -> set[str]:
    normalized_value = normalize_search_term(value)
    if not normalized_value:
        return set()
    result = set()
    for definition in TAG_DEFINITIONS:
        for aliases in definition.aliases.values():
            if any(
                _alias_occurs(alias, value, normalized_value)
                for alias in aliases
                if _is_matchable_alias(alias)
            ):
                result.add(definition.key)
                break
    return result


def _expand_concepts(concepts: Iterable[str]) -> set[str]:
    expanded = {concept for concept in concepts if concept in _DEFINITION_BY_KEY}
    pending = list(expanded)
    while pending:
        concept = pending.pop()
        for parent in _DEFINITION_BY_KEY[concept].parents:
            if parent not in expanded:
                expanded.add(parent)
                pending.append(parent)
    return expanded


def concept_keys_for_query(query: str) -> set[str]:
    return _concepts_in_text(query)


def concept_labels(concepts: Iterable[str], locale: str) -> list[str]:
    labels = []
    for concept in concepts:
        definition = _DEFINITION_BY_KEY.get(concept)
        if definition is None:
            continue
        aliases = definition.aliases.get(locale, ())
        if aliases:
            labels.append(aliases[0])
    return list(dict.fromkeys(labels))


def _clean_term(value: str, *, reject_subjective: bool) -> str | None:
    value = " ".join(unicodedata.normalize("NFKC", value).split()).strip("#·,|/\\")
    normalized = normalize_search_term(value)
    if not value or not normalized or len(value) > 64 or len(normalized) > 64:
        return None
    if "http://" in value.casefold() or "https://" in value.casefold():
        return None
    if reject_subjective and any(
        normalize_search_term(term) in normalized for term in _BLOCKED_AI_TERMS
    ):
        return None
    return value


def _raw_category_terms(raw_data: Mapping | None) -> list[str]:
    if not isinstance(raw_data, Mapping):
        return []
    result = []
    for key in ("category", "source_food_name", "product_name_ko", "product_name_ja"):
        value = raw_data.get(key)
        if isinstance(value, str) and value.strip():
            result.append(value)
    categories = raw_data.get("categories")
    if isinstance(categories, str):
        result.extend(part.strip() for part in categories.split(",") if part.strip())
    category_tags = raw_data.get("categories_tags")
    if isinstance(category_tags, list):
        result.extend(str(value).removeprefix("en:") for value in category_tags[:20])
    return result


def build_product_search_terms(
    *,
    name: str,
    brand: str | None,
    raw_data: Mapping | None = None,
    search_concepts: Iterable[str] = (),
    search_terms_ko: Iterable[str] = (),
    search_terms_ja: Iterable[str] = (),
    product_source: str = "user",
) -> list[SearchTermSpec]:
    direct_source = (
        "ai"
        if product_source == "ai_label"
        else "catalog"
        if product_source in {"open_food_facts", "mfds_food_db", "brand_menu"}
        else "dictionary"
    )
    raw_terms = _raw_category_terms(raw_data)
    supplied_terms = [(value, "ko") for value in search_terms_ko if isinstance(value, str)]
    supplied_terms.extend((value, "ja") for value in search_terms_ja if isinstance(value, str))

    concepts = {
        concept
        for concept in search_concepts
        if isinstance(concept, str) and concept in _DEFINITION_BY_KEY
    }
    for value in [name, brand or "", *raw_terms, *(value for value, _ in supplied_terms)]:
        concepts.update(_concepts_in_text(value))
    concepts = _expand_concepts(concepts)

    terms_by_normalized: dict[str, SearchTermSpec] = {}

    def add_term(
        value: str,
        *,
        locale: str,
        kind: str,
        source: str,
        concept_key: str | None,
        confidence: Decimal,
        reject_subjective: bool = False,
    ) -> None:
        cleaned = _clean_term(value, reject_subjective=reject_subjective)
        if cleaned is None:
            return
        normalized = normalize_search_term(cleaned)
        terms_by_normalized.setdefault(
            normalized,
            SearchTermSpec(
                term=cleaned,
                normalized_term=normalized,
                locale=locale,
                kind=kind,
                source=source,
                concept_key=concept_key,
                confidence=confidence,
            ),
        )

    for value, locale in supplied_terms:
        add_term(
            value,
            locale=locale,
            kind="alias",
            source=direct_source,
            concept_key=None,
            confidence=Decimal("0.75") if direct_source == "ai" else Decimal("0.95"),
            reject_subjective=direct_source == "ai",
        )
    for value in raw_terms:
        add_term(
            value,
            locale=detect_locale(value),
            kind="category",
            source="catalog",
            concept_key=None,
            confidence=Decimal("0.95"),
        )
    for concept in sorted(concepts):
        definition = _DEFINITION_BY_KEY[concept]
        for locale, aliases in definition.aliases.items():
            for alias in aliases:
                add_term(
                    alias,
                    locale=locale,
                    kind=definition.kind,
                    source=direct_source,
                    concept_key=concept,
                    confidence=Decimal("0.85") if direct_source == "ai" else Decimal("1"),
                )
    return list(terms_by_normalized.values())


def matching_term(query: str, terms: Iterable[object]) -> str | None:
    normalized_query = normalize_search_term(query)
    if not normalized_query:
        return None
    query_concepts = concept_keys_for_query(query)
    matches = []
    for term in terms:
        normalized = str(getattr(term, "normalized_term", ""))
        concept_key = getattr(term, "concept_key", None)
        if normalized_query == normalized:
            score = 3
        elif normalized_query in normalized or normalized in normalized_query:
            score = 2
        elif concept_key and concept_key in query_concepts:
            score = 1
        else:
            continue
        matches.append((score, len(normalized), str(getattr(term, "term", ""))))
    return max(matches, default=(0, 0, ""))[2] or None


def search_term_score(query: str, terms: Iterable[object]) -> int:
    normalized_query = normalize_search_term(query)
    if not normalized_query:
        return 0
    query_concepts = concept_keys_for_query(query)
    score = 0
    for term in terms:
        normalized = str(getattr(term, "normalized_term", ""))
        concept_key = getattr(term, "concept_key", None)
        if normalized_query == normalized:
            score = max(score, 1_900)
        elif normalized_query in normalized or normalized in normalized_query:
            score = max(score, 1_500)
        elif concept_key and concept_key in query_concepts:
            score = max(score, 1_200)
    return score
