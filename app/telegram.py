from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from time import monotonic
from uuid import uuid4

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai_recognition import NutritionRecognizer
from app.barcode import decode_barcodes, normalize_barcode
from app.catalog import OpenFoodFactsCatalog
from app.config import Settings
from app.image_tools import preprocess_image, remove_private_image, write_private_image
from app.menu_ai import (
    MENU_SEARCH_VERSION,
    MenuLookupError,
    MenuNutritionEvidence,
    MenuNutritionSearcher,
    is_usable_menu_evidence,
    menu_query_hash,
    normalize_menu_query,
)
from app.mfds_catalog import (
    MFDS_SOURCE,
    MfdsCatalogError,
    MfdsFoodCatalog,
    food_match_score,
    search_terms,
)
from app.models import IntakeLog, RecognitionJob
from app.nutrition import normalize_salt, parse_positive_decimal, recognition_warnings
from app.portion import (
    ParsedPortion,
    PortionError,
    display_portion,
    package_multiplier,
    parse_portion,
    portion_multiplier,
)
from app.recipe import (
    PARSER_VERSION,
    RecipeDraft,
    RecipeError,
    ResolvedRecipeIngredient,
    display_recipe_amount,
    ingredient_multiplier,
    ingredient_totals,
    parse_structured_recipe,
    recipe_input_hash,
    sum_recipe_totals,
)
from app.recipe_ai import RecipeAIParser
from app.repository import (
    add_intake,
    create_product_version,
    ensure_user,
    find_product_by_barcode,
    find_product_by_external_id,
    finish_ai_usage,
    get_active_job,
    get_daily_summary,
    get_last_portion,
    get_menu_search_cache,
    get_or_create_catalog_product,
    get_product_version,
    get_recipe_parse_cache,
    recent_logs,
    reserve_ai_usage,
    reserve_recipe_ai_usage,
    save_menu_search_cache,
    save_recipe_parse_cache,
    search_catalog_products,
    search_recipe_products,
    search_saved_products,
    set_goals,
    start_job,
    undo_last_intake,
)
from app.schemas import NutritionRecognition, ProductCandidate, RecipeExtraction
from app.search_tags import (
    build_product_search_terms,
    matching_term,
    normalize_search_term,
    search_term_score,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotContext:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    catalog: OpenFoodFactsCatalog
    food_catalog: MfdsFoodCatalog | None
    recognizer: NutritionRecognizer | None
    recipe_parser: RecipeAIParser | None = None
    menu_searcher: MenuNutritionSearcher | None = None
    started_at: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class PreparedRecipeExtraction:
    extraction: RecipeExtraction
    input_hash: str
    used_ai: bool
    from_cache: bool


@dataclass(frozen=True)
class MenuSearchDraft:
    draft_id: str
    user_id: int
    query: str
    input_hash: str
    evidence: MenuNutritionEvidence
    searched_urls: tuple[str, ...]
    from_cache: bool


def _allowed(user_id: int, settings: Settings) -> bool:
    if settings.public_signup:
        return True
    return settings.owner_telegram_id != 0 and user_id == settings.owner_telegram_id


async def _guard(message: Message, settings: Settings) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    if _allowed(user_id, settings):
        return True
    if settings.owner_telegram_id == 0:
        await message.answer(
            f"아직 소유자 설정이 필요합니다.\n내 Telegram ID: {user_id}\n"
            "서버의 .env에 OWNER_TELEGRAM_ID를 넣고 재시작하세요."
        )
    else:
        await message.answer("현재는 소유자만 사용할 수 있는 봇입니다.")
    return False


def _fmt(value: Decimal) -> str:
    return _fmt_precision(value, Decimal("0.1"))


def _fmt_precision(value: Decimal, quantum: Decimal) -> str:
    normalized = value.quantize(quantum)
    return f"{normalized:f}".rstrip("0").rstrip(".")


def _format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}일")
    if hours or days:
        parts.append(f"{hours}시간")
    if minutes or hours or days:
        parts.append(f"{minutes}분")
    parts.append(f"{seconds}초")
    return " ".join(parts)


def _product_text(version) -> str:
    product = version.product
    brand = f" · {product.brand}" if product.brand else ""
    basis = f"{_fmt(version.basis_amount)} {version.basis_unit}"
    if getattr(version, "basis_text", None):
        basis = f"{version.basis_text} → {basis}"
    lines = [
        f"{product.name}{brand}\n영양 기준: {basis}",
        f"{_fmt(version.kcal)} kcal · 탄 {_fmt(version.carbs_g)} g · "
        f"단 {_fmt(version.protein_g)} g · 지 {_fmt(version.fat_g)} g",
    ]
    market = _market_text(getattr(version, "label_market", "UNKNOWN"))
    if market:
        lines.insert(1, f"표시 형식: {market}")
    salt_text = _salt_text(version)
    if salt_text:
        lines.append(salt_text)
    if getattr(version, "estimated_values", False):
        if version.source == "recipe":
            lines.append("참고: 재료 DB 매칭과 조리 전 입력량을 합산한 추정 영양정보")
        else:
            lines.append("참고: 포장지에서 추정치 또는 참고값으로 표시된 영양정보")
    if version.source == MFDS_SOURCE:
        lines.append("출처: 식품의약품안전처 식품영양성분DB")
    elif version.source == "brand_menu":
        raw_data = version.raw_data if isinstance(version.raw_data, dict) else {}
        source_data = raw_data.get("official_menu", {})
        source_title = source_data.get("source_title") if isinstance(source_data, dict) else None
        source_url = source_data.get("source_url") if isinstance(source_data, dict) else None
        lines.append(f"출처: {source_title or '브랜드 공식 영양정보'}")
        if isinstance(source_url, str) and source_url.startswith(("https://", "http://")):
            lines.append(source_url)
    elif version.source == "recipe":
        lines.append("출처: 사용자 레시피 · 재료 영양값은 저장된 식품 DB 기준")
    package_details = []
    if version.package_amount is not None and version.package_unit:
        package_details.append(f"총 {_fmt(version.package_amount)} {version.package_unit}")
    if version.servings_per_package is not None:
        package_details.append(f"{_fmt(version.servings_per_package)}회분")
    if version.piece_count is not None:
        package_details.append(f"{_fmt(version.piece_count)}개")
    if package_details:
        detail_label = "메뉴 단위" if version.source == "brand_menu" else "포장 정보"
        lines.append(f"{detail_label}: " + " · ".join(package_details))
    return "\n".join(lines)


def _market_text(market: str) -> str:
    return {"KR": "🇰🇷 한국", "JP": "🇯🇵 일본"}.get(market, "")


def _salt_text(values) -> str:
    sodium = getattr(values, "sodium_mg", None)
    salt = getattr(values, "salt_equivalent_g", None)
    if sodium is None and salt is None:
        return ""
    parts = []
    if salt is not None:
        suffix = " (나트륨에서 환산)" if getattr(values, "salt_equivalent_derived", False) else ""
        parts.append(f"식염상당량 {_fmt_precision(salt, Decimal('0.001'))} g{suffix}")
    if sodium is not None:
        suffix = " (식염상당량에서 환산)" if getattr(values, "sodium_derived", False) else ""
        parts.append(f"나트륨 {_fmt_precision(sodium, Decimal('0.1'))} mg{suffix}")
    return " · ".join(parts)


def _can_use_portion(version, portion: ParsedPortion) -> bool:
    try:
        portion_multiplier(version, portion)
    except PortionError:
        return False
    return True


def _stored_quick_portions(version) -> list[tuple[str, ParsedPortion]]:
    raw_data = version.raw_data if isinstance(version.raw_data, dict) else {}
    items = raw_data.get("quick_portions")
    if not isinstance(items, list):
        return []
    result: list[tuple[str, ParsedPortion]] = []
    for item in items[:4]:
        if not isinstance(item, dict):
            continue
        try:
            amount = Decimal(str(item.get("amount")))
            unit = str(item.get("unit") or "")
            portion = ParsedPortion(amount, unit)
        except (InvalidOperation, ValueError):
            continue
        if not amount.is_finite() or amount <= 0 or not _can_use_portion(version, portion):
            continue
        label = str(item.get("label") or display_portion(portion)).strip()[:56]
        result.append((label, portion))
    return result


def _portion_keyboard(version, last_log: IntakeLog | None) -> InlineKeyboardMarkup:
    version_id = version.id
    buttons: list[InlineKeyboardButton] = []
    if last_log and last_log.input_amount is not None and last_log.input_unit:
        previous = ParsedPortion(last_log.input_amount, last_log.input_unit)
        buttons.append(
            InlineKeyboardButton(
                text=f"지난번처럼 {display_portion(previous)}",
                callback_data=f"portion:{version_id}:last",
            )
        )

    for index, (label, _) in enumerate(_stored_quick_portions(version)):
        buttons.append(
            InlineKeyboardButton(
                text=label,
                callback_data=f"portion:{version_id}:quick{index}",
            )
        )

    package = package_multiplier(version)
    if package is not None:
        full_text = "전부"
        half_text = "절반"
        if version.package_amount is not None and version.package_unit in {"g", "ml"}:
            full = ParsedPortion(version.package_amount, version.package_unit)
            half = ParsedPortion(version.package_amount / Decimal("2"), version.package_unit)
            full_text += f" {display_portion(full)}"
            half_text += f" {display_portion(half)}"
        buttons.extend(
            [
                InlineKeyboardButton(text=full_text, callback_data=f"portion:{version_id}:full"),
                InlineKeyboardButton(text=half_text, callback_data=f"portion:{version_id}:half"),
            ]
        )

    serving = ParsedPortion(Decimal("1"), "serving")
    if _can_use_portion(version, serving):
        serving_text = "1회분"
        if (
            version.package_amount is not None
            and version.package_unit in {"g", "ml"}
            and version.servings_per_package is not None
        ):
            serving_size = ParsedPortion(
                version.package_amount / version.servings_per_package,
                version.package_unit,
            )
            serving_text += f" {display_portion(serving_size)}"
        buttons.append(
            InlineKeyboardButton(text=serving_text, callback_data=f"portion:{version_id}:serving")
        )

    basis = ParsedPortion(version.basis_amount, version.basis_unit)
    if version.basis_unit != "serving":
        buttons.append(
            InlineKeyboardButton(
                text=f"기준량 {display_portion(basis)}",
                callback_data=f"portion:{version_id}:basis",
            )
        )
    buttons.append(
        InlineKeyboardButton(text="직접 입력", callback_data=f"portion:{version_id}:custom")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    )


def _food_results_keyboard(versions: list) -> InlineKeyboardMarkup:
    rows = []
    for version in versions:
        raw_data = version.raw_data if isinstance(version.raw_data, dict) else {}
        category = str(raw_data.get("category") or "").strip()
        basis = f"{_fmt(version.basis_amount)}{version.basis_unit}"
        detail = f"{_fmt(version.kcal)}kcal/{basis}"
        if category:
            detail = f"{category} · {detail}"
        label = f"{version.product.name} · {detail}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:62],
                    callback_data=f"food:{version.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _search_key(value: str) -> str:
    return normalize_search_term(value)


def _saved_product_score(query: str, version, user_id: int) -> int:
    query_key = _search_key(query)
    name_key = _search_key(version.product.name)
    brand_key = _search_key(version.product.brand or "")
    combined_key = brand_key + name_key
    if not query_key:
        return 0
    if query_key == name_key:
        score = 4_000
    elif query_key == combined_key:
        score = 3_800
    elif name_key.startswith(query_key):
        score = 3_000
    elif query_key in name_key:
        score = 2_500
    elif query_key in combined_key:
        score = 2_000
    else:
        query_terms = [_search_key(term) for term in query.split()]
        matched_terms = sum(term in combined_key for term in query_terms if term)
        score = matched_terms * 500
    score = max(
        score,
        search_term_score(query, getattr(version.product, "search_terms", ())),
    )
    if version.product.owner_telegram_id == user_id:
        score += 200
    return score


def _rank_saved_products(query: str, versions: list, user_id: int) -> list:
    return sorted(
        versions,
        key=lambda version: (_saved_product_score(query, version, user_id), version.id),
        reverse=True,
    )


def _saved_products_keyboard(versions: list, query: str) -> InlineKeyboardMarkup:
    rows = []
    for version in versions:
        brand = f" · {version.product.brand}" if version.product.brand else ""
        basis = f"{_fmt(version.basis_amount)}{version.basis_unit}"
        tag = matching_term(query, getattr(version.product, "search_terms", ()))
        query_key = _search_key(query)
        direct_text = _search_key(f"{version.product.name} {version.product.brand or ''}")
        tag_prefix = f"#{tag} · " if tag and query_key not in direct_text else ""
        label = f"{tag_prefix}{version.product.name}{brand} · {_fmt(version.kcal)}kcal/{basis}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:62],
                    callback_data=f"pick:{version.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _recent_products_keyboard(logs: list[IntakeLog]) -> InlineKeyboardMarkup:
    rows = []
    seen_version_ids: set[int] = set()
    for log in logs:
        version_id = log.product_version_id
        if version_id in seen_version_ids:
            continue
        seen_version_ids.add(version_id)
        portion_text = f"×{_fmt(log.multiplier)}"
        if log.input_amount is not None and log.input_unit:
            portion_text = display_portion(ParsedPortion(log.input_amount, log.input_unit))
        label = f"↻ {log.product_version.product.name} · {portion_text}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:62],
                    callback_data=f"pick:{version_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _rank_food_versions(query: str, versions: list) -> list:
    return sorted(
        versions,
        key=lambda version: (
            food_match_score(query, version.product.name),
            version.product.name,
        ),
        reverse=True,
    )


def _confirmation_keyboard(job_id: int, market: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if market == 'KR' else ''}🇰🇷 한국",
                    callback_data=f"market:{job_id}:KR",
                ),
                InlineKeyboardButton(
                    text=f"{'✅ ' if market == 'JP' else ''}🇯🇵 일본",
                    callback_data=f"market:{job_id}:JP",
                ),
            ],
            [
                InlineKeyboardButton(text="제품 정보 저장", callback_data=f"confirm:{job_id}"),
                InlineKeyboardButton(text="취소", callback_data=f"cancel:{job_id}"),
            ],
        ]
    )


def _can_correct_basis_unit(version, portion: ParsedPortion) -> bool:
    return (
        version.basis_unit in {"g", "ml"}
        and portion.unit in {"g", "ml"}
        and version.basis_unit != portion.unit
        and version.package_unit == portion.unit
    )


def _unit_correction_keyboard(version_id: int, portion: ParsedPortion) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{portion.unit} 기준으로 수정·기록",
                    callback_data=(
                        f"unitfix:{version_id}:{portion.amount.normalize()}:{portion.unit}"
                    ),
                ),
                InlineKeyboardButton(text="취소", callback_data="unitfix_cancel"),
            ]
        ]
    )


def _candidate_with_basis_unit(version, basis_unit: str) -> ProductCandidate:
    raw_data = dict(version.raw_data or {})
    raw_data["user_basis_unit_correction"] = {
        "from": version.basis_unit,
        "to": basis_unit,
    }
    return ProductCandidate(
        barcode=version.product.barcode,
        name=version.product.name,
        brand=version.product.brand,
        basis_amount=version.basis_amount,
        basis_unit=basis_unit,
        package_amount=version.package_amount,
        package_unit=version.package_unit,
        servings_per_package=version.servings_per_package,
        piece_count=version.piece_count,
        kcal=version.kcal,
        carbs_g=version.carbs_g,
        protein_g=version.protein_g,
        fat_g=version.fat_g,
        sodium_mg=getattr(version, "sodium_mg", None),
        salt_equivalent_g=getattr(version, "salt_equivalent_g", None),
        sodium_derived=getattr(version, "sodium_derived", False),
        salt_equivalent_derived=getattr(version, "salt_equivalent_derived", False),
        label_market=getattr(version, "label_market", "UNKNOWN"),
        label_language=getattr(version, "label_language", "unknown"),
        basis_text=getattr(version, "basis_text", None),
        basis_metric_amount=getattr(version, "basis_metric_amount", None),
        basis_metric_unit=getattr(version, "basis_metric_unit", None),
        basis_count_amount=getattr(version, "basis_count_amount", None),
        basis_count_unit=getattr(version, "basis_count_unit", None),
        estimated_values=getattr(version, "estimated_values", False),
        source="user_correction",
        verified=True,
        raw_data=raw_data,
    )


async def _download_photo(bot: Bot, message: Message) -> bytes:
    destination = BytesIO()
    await bot.download(message.photo[-1], destination=destination)
    return destination.getvalue()


def _recognition_is_complete(result: NutritionRecognition) -> bool:
    return result.product_name_found and result.label_found


def _recognition_follow_up_text(result: NutritionRecognition) -> str:
    if result.product_name_found and not result.label_found:
        return (
            f"제품명 ‘{result.product_name}’은 확인했습니다.\n"
            "영양정보 표가 부족합니다. 표시 기준과 kcal·탄수화물·단백질·지방이 "
            "선명하게 보이는 사진만 추가로 보내주세요."
        )
    if result.label_found and not result.product_name_found:
        return (
            "영양정보 표는 확인했습니다.\n"
            "제품명을 읽지 못했습니다. 제품명과 브랜드가 선명하게 보이는 앞면 사진만 "
            "추가로 보내주세요."
        )
    return (
        "바코드 외에 제품명과 영양정보를 충분히 읽지 못했습니다.\n"
        "제품 앞면과 영양정보 표를 각각 찍어 한 번에 앨범으로 보내거나, 앞면부터 "
        "차례로 보내주세요."
    )


async def _clear_inline_keyboard(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def _offer_version(
    context: BotContext, message: Message, version, *, user_id: int | None = None
) -> None:
    if user_id is None:
        user_id = message.from_user.id if message.from_user else 0
    async with context.sessions() as session:
        last_log = await get_last_portion(session, user_id, version.id)
    await message.answer(
        _product_text(version) + "\n\n얼마나 먹었나요?",
        reply_markup=_portion_keyboard(version, last_log),
    )


async def _prepare_recipe_extraction(
    context: BotContext,
    *,
    user_id: int,
    name_hint: str | None,
    raw_text: str,
    last_ai_request: dict[int, float],
) -> PreparedRecipeExtraction:
    if len(raw_text) > context.settings.recipe_ai_max_input_chars:
        raise RecipeError(
            f"레시피 입력은 최대 {context.settings.recipe_ai_max_input_chars:,}자입니다."
        )
    input_hash = recipe_input_hash(name_hint, raw_text)
    async with context.sessions() as session:
        await ensure_user(session, user_id, context.settings.app_timezone)
        cached = await get_recipe_parse_cache(
            session,
            user_id=user_id,
            input_hash=input_hash,
            parser_version=PARSER_VERSION,
        )
        await session.commit()
    if cached is not None:
        extraction = RecipeExtraction.model_validate(cached.result_json)
        if len(extraction.ingredients) > context.settings.recipe_max_ingredients:
            raise RecipeError("저장된 레시피 분석 결과의 재료 수가 현재 제한을 초과합니다.")
        return PreparedRecipeExtraction(
            extraction=extraction,
            input_hash=input_hash,
            used_ai=cached.used_ai,
            from_cache=True,
        )

    extraction = parse_structured_recipe(
        raw_text,
        name_hint=name_hint,
        max_ingredients=context.settings.recipe_max_ingredients,
    )
    if extraction is not None:
        async with context.sessions() as session:
            await save_recipe_parse_cache(
                session,
                user_id=user_id,
                input_hash=input_hash,
                parser_version=PARSER_VERSION,
                result_json=extraction.model_dump(mode="json"),
                used_ai=False,
            )
            await session.commit()
        return PreparedRecipeExtraction(
            extraction=extraction,
            input_hash=input_hash,
            used_ai=False,
            from_cache=False,
        )

    if context.recipe_parser is None:
        raise RecipeError(
            "자연어 레시피를 분석하려면 OPENAI_API_KEY가 필요합니다.\n"
            "AI 없이 사용하려면 재료를 ‘밥 300g’처럼 한 줄씩 입력해 주세요."
        )
    elapsed = monotonic() - last_ai_request.get(user_id, -10_000)
    cooldown = context.settings.recipe_ai_cooldown_seconds
    if elapsed < cooldown:
        raise RecipeError(
            f"AI 레시피 분석은 {cooldown - elapsed:.0f}초 뒤 다시 시도할 수 있습니다."
        )

    async with context.sessions() as session:
        usage, limit_error = await reserve_recipe_ai_usage(
            session,
            user_id=user_id,
            input_hash=input_hash,
            timezone_name=context.settings.app_timezone,
            daily_limit=context.settings.recipe_ai_daily_limit,
            monthly_limit=context.settings.recipe_ai_monthly_limit,
        )
        await session.commit()
    if usage is None:
        raise RecipeError(limit_error or "AI 레시피 분석 한도에 도달했습니다.")

    last_ai_request[user_id] = monotonic()
    try:
        result = await context.recipe_parser.parse(
            raw_text=raw_text,
            name_hint=name_hint,
            user_id=user_id,
        )
    except Exception as exc:
        logger.exception("OpenAI recipe parsing failed")
        async with context.sessions() as session:
            await finish_ai_usage(
                session,
                usage.id,
                status="failed",
                error=type(exc).__name__,
            )
            await session.commit()
        raise RecipeError(
            "AI 레시피 분석에 실패했습니다. 잠시 후 다시 시도하거나 재료를 한 줄씩 입력해 주세요."
        ) from exc

    async with context.sessions() as session:
        await finish_ai_usage(
            session,
            usage.id,
            status="completed",
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_tokens=result.total_tokens,
        )
        await save_recipe_parse_cache(
            session,
            user_id=user_id,
            input_hash=input_hash,
            parser_version=PARSER_VERSION,
            result_json=result.extraction.model_dump(mode="json"),
            used_ai=True,
        )
        await session.commit()
    return PreparedRecipeExtraction(
        extraction=result.extraction,
        input_hash=input_hash,
        used_ai=True,
        from_cache=False,
    )


async def _resolve_recipe(
    context: BotContext,
    *,
    user_id: int,
    prepared: PreparedRecipeExtraction,
) -> RecipeDraft:
    resolved: list[ResolvedRecipeIngredient] = []
    errors: list[str] = []
    async with context.sessions() as session:
        await ensure_user(session, user_id, context.settings.app_timezone)
        for ingredient in prepared.extraction.ingredients:
            if ingredient.amount is None or ingredient.unit == "unknown":
                errors.append(f"{ingredient.name}: 양 또는 단위가 없습니다.")
                continue

            cached = await search_recipe_products(
                session,
                owner_id=user_id,
                terms=search_terms(ingredient.name),
            )
            ranked = _rank_food_versions(ingredient.name, cached)
            version = next(
                (
                    item
                    for item in ranked
                    if food_match_score(ingredient.name, item.product.name) >= 900
                ),
                None,
            )
            catalog_error: str | None = None
            if version is None and context.food_catalog is not None:
                try:
                    candidates = await context.food_catalog.search(ingredient.name, limit=5)
                except MfdsCatalogError as exc:
                    candidates = []
                    catalog_error = str(exc)
                if candidates:
                    versions = [
                        await get_or_create_catalog_product(session, candidate)
                        for candidate in candidates
                    ]
                    version = _rank_food_versions(ingredient.name, versions)[0]
            if version is None and ranked:
                version = ranked[0]
            if version is None:
                detail = f" ({catalog_error})" if catalog_error else ""
                errors.append(f"{ingredient.name}: 식품 DB에서 찾지 못했습니다.{detail}")
                continue

            try:
                multiplier, _ = ingredient_multiplier(version, ingredient)
            except RecipeError as exc:
                errors.append(str(exc))
                continue
            totals = ingredient_totals(version, multiplier)
            resolved.append(
                ResolvedRecipeIngredient(
                    input_name=ingredient.name,
                    matched_name=version.product.name,
                    amount=ingredient.amount,
                    unit=ingredient.unit,
                    multiplier=multiplier,
                    version_id=version.id,
                    source=version.source,
                    totals=totals,
                )
            )
        await session.commit()

    if errors:
        details = "\n".join(f"· {error}" for error in errors[:10])
        if len(errors) > 10:
            details += f"\n· 그 외 {len(errors) - 10}개"
        raise RecipeError(
            "다음 재료는 계산하지 못했습니다. 이름을 더 간단히 쓰거나 g/ml로 바꿔 "
            f"다시 입력해 주세요.\n{details}"
        )
    total = sum_recipe_totals(resolved)
    return RecipeDraft(
        draft_id=uuid4().hex[:12],
        user_id=user_id,
        input_hash=prepared.input_hash,
        name=prepared.extraction.recipe_name,
        servings=prepared.extraction.servings,
        used_ai=prepared.used_ai,
        ingredients=tuple(resolved),
        total=total,
    )


def _recipe_draft_text(draft: RecipeDraft, *, from_cache: bool) -> str:
    if from_cache:
        analysis = "저장된 분석 재사용 · OpenAI 호출 없음"
    elif draft.used_ai:
        analysis = "AI가 재료 문장만 구조화 · 영양 계산은 Python"
    else:
        analysis = "정형 입력 분석 · OpenAI 호출 없음"
    lines = [f"🍳 {draft.name}", analysis, "", "재료 매칭"]
    for item in draft.ingredients:
        amount = display_recipe_amount(item.amount, item.unit)
        lines.append(
            f"· {item.input_name} {amount} → {item.matched_name} · {_fmt(item.totals.kcal)} kcal"
        )
    per_serving = draft.per_serving
    lines.extend(
        [
            "",
            f"전체 {_fmt(draft.total.kcal)} kcal · {_fmt(draft.servings)}인분",
            f"1인분 {_fmt(per_serving.kcal)} kcal",
            f"탄 {_fmt(per_serving.carbs_g)} g · 단 {_fmt(per_serving.protein_g)} g · "
            f"지 {_fmt(per_serving.fat_g)} g",
            "",
            "재료와 식품 DB 매칭이 맞는지 확인해 주세요.",
        ]
    )
    return "\n".join(lines)


def _recipe_confirmation_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="레시피 저장",
                    callback_data=f"recipe_save:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="취소",
                    callback_data=f"recipe_cancel:{draft_id}",
                ),
            ]
        ]
    )


def _recipe_candidate(draft: RecipeDraft) -> ProductCandidate:
    per_serving = draft.per_serving
    ingredients = [
        {
            "input_name": item.input_name,
            "matched_name": item.matched_name,
            "amount": str(item.amount),
            "unit": item.unit,
            "multiplier": str(item.multiplier),
            "product_version_id": item.version_id,
            "source": item.source,
            "kcal": str(item.totals.kcal),
            "carbs_g": str(item.totals.carbs_g),
            "protein_g": str(item.totals.protein_g),
            "fat_g": str(item.totals.fat_g),
        }
        for item in draft.ingredients
    ]
    return ProductCandidate(
        external_source="recipe",
        external_id=f"{draft.user_id}:{draft.input_hash[:56]}",
        name=draft.name,
        basis_amount=Decimal("1"),
        basis_unit="serving",
        servings_per_package=draft.servings,
        kcal=per_serving.kcal,
        carbs_g=per_serving.carbs_g,
        protein_g=per_serving.protein_g,
        fat_g=per_serving.fat_g,
        source="recipe",
        verified=True,
        estimated_values=True,
        basis_text=f"1인분 (전체 {_fmt(draft.servings)}인분)",
        raw_data={
            "recipe": {
                "input_hash": draft.input_hash,
                "parser_version": PARSER_VERSION,
                "used_ai": draft.used_ai,
                "servings": str(draft.servings),
                "ingredients": ingredients,
                "total": {
                    "kcal": str(draft.total.kcal),
                    "carbs_g": str(draft.total.carbs_g),
                    "protein_g": str(draft.total.protein_g),
                    "fat_g": str(draft.total.fat_g),
                },
            }
        },
    )


def _menu_draft_text(draft: MenuSearchDraft) -> str:
    evidence = draft.evidence
    cache_text = "저장된 최근 검색 결과" if draft.from_cache else "방금 확인한 검색 결과"
    return (
        f"{evidence.brand} · {evidence.menu_name}\n"
        f"영양 기준: {evidence.basis_text}\n"
        f"{_fmt(evidence.kcal)} kcal · 탄 {_fmt(evidence.carbs_g)} g · "
        f"단 {_fmt(evidence.protein_g)} g · 지 {_fmt(evidence.fat_g)} g\n\n"
        f"출처: {evidence.source_title or '브랜드 공식 영양정보'}\n"
        f"{evidence.source_url}\n"
        f"검증: {cache_text} · 신뢰도 {_fmt(evidence.confidence * 100)}%\n\n"
        "메뉴명·크기와 공식 페이지의 숫자를 확인한 뒤 저장해 주세요."
    )


def _menu_confirmation_keyboard(draft_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="공식 정보 저장",
                    callback_data=f"menu_save:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="취소",
                    callback_data=f"menu_cancel:{draft_id}",
                ),
            ]
        ]
    )


def _menu_candidate(draft: MenuSearchDraft) -> ProductCandidate:
    evidence = draft.evidence
    if not is_usable_menu_evidence(evidence, draft.searched_urls):
        raise MenuLookupError("공식 출처가 검증되지 않은 메뉴 정보입니다.")
    return ProductCandidate(
        external_source="brand_menu",
        external_id=draft.input_hash,
        name=evidence.menu_name or draft.query,
        brand=evidence.brand,
        basis_amount=evidence.basis_amount or Decimal("1"),
        basis_unit=evidence.basis_unit or "serving",
        servings_per_package=(Decimal("1") if evidence.basis_unit == "serving" else None),
        kcal=evidence.kcal or Decimal("0"),
        carbs_g=evidence.carbs_g or Decimal("0"),
        protein_g=evidence.protein_g or Decimal("0"),
        fat_g=evidence.fat_g or Decimal("0"),
        source="brand_menu",
        verified=True,
        estimated_values=False,
        basis_text=evidence.basis_text,
        raw_data={
            "official_menu": {
                "query": draft.query,
                "input_hash": draft.input_hash,
                "search_version": MENU_SEARCH_VERSION,
                "source_url": evidence.source_url,
                "source_title": evidence.source_title,
                "confidence": str(evidence.confidence),
                "evidence_text": evidence.evidence_text,
            }
        },
    )


def create_router(context: BotContext) -> Router:
    router = Router(name="calorie-bot")
    pending_portions: dict[int, int] = {}
    pending_recipe_names: dict[int, str | None] = {}
    recipe_drafts: dict[str, RecipeDraft] = {}
    recipe_ai_last_request: dict[int, float] = {}
    recipe_locks: dict[int, asyncio.Lock] = {}
    menu_drafts: dict[str, MenuSearchDraft] = {}
    menu_ai_last_request: dict[int, float] = {}
    menu_locks: dict[int, asyncio.Lock] = {}
    recognition_locks: dict[int, asyncio.Lock] = {}
    photo_album_buffers: dict[tuple[int, str], list[tuple[Message, bytes]]] = {}
    photo_album_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
    photo_album_overflow: set[tuple[int, str]] = set()

    async def process_photo_batch(message: Message, raw_images: list[bytes]) -> None:
        user_id = message.from_user.id if message.from_user else 0
        lock = recognition_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            async with context.sessions() as session:
                await ensure_user(session, user_id, context.settings.app_timezone)
                job = await get_active_job(session, user_id)
                await session.commit()
            if job is None:
                await _handle_barcode_photos(context, message, raw_images)
            else:
                await _handle_job_photos(context, message, job.id, raw_images)

    async def flush_photo_album(key: tuple[int, str]) -> None:
        current_task = asyncio.current_task()
        message: Message | None = None
        try:
            await asyncio.sleep(0.8)
            items = photo_album_buffers.pop(key, [])
            if not items:
                return
            message = items[-1][0]
            if key in photo_album_overflow:
                await message.answer("한 번에 최대 3장까지만 분석합니다. 앞의 3장을 사용합니다.")
            await process_photo_batch(message, [raw for _, raw in items])
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Photo album processing failed")
            items = photo_album_buffers.pop(key, [])
            target = message or (items[-1][0] if items else None)
            if target:
                await target.answer("사진 묶음을 처리하지 못했습니다. 다시 시도해 주세요.")
        finally:
            photo_album_overflow.discard(key)
            if photo_album_tasks.get(key) is current_task:
                photo_album_tasks.pop(key, None)

    async def process_saved_product_search(message: Message, raw_query: str) -> None:
        user_id = message.from_user.id if message.from_user else 0
        query = " ".join(raw_query.split()).strip()
        if not 1 <= len(query) <= 60:
            await message.answer("검색어는 1~60자로 입력해 주세요. 예: /search 닭가슴살")
            return
        async with context.sessions() as session:
            await ensure_user(session, user_id, context.settings.app_timezone)
            versions = await search_saved_products(
                session,
                owner_id=user_id,
                query=query,
                limit=40,
            )
            await session.commit()
        versions = _rank_saved_products(query, versions, user_id)[:8]
        if not versions:
            await message.answer(
                f"저장된 상품에서 ‘{query}’을(를) 찾지 못했습니다.\n"
                "포장식품은 바코드 사진으로 등록하고, 일반 음식은 /food 음식명으로 찾아보세요."
            )
            return
        await message.answer(
            f"‘{query}’ 저장 상품 검색 결과입니다. 제품명·브랜드·한일 태그를 확인했습니다. "
            "AI와 외부 API를 호출하지 않았습니다.",
            reply_markup=_saved_products_keyboard(versions, query),
        )

    async def process_recipe_message(
        message: Message,
        *,
        name_hint: str | None,
        raw_text: str,
    ) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer("재료를 분석하고 식품 DB에서 영양정보를 찾는 중입니다.")
        lock = recipe_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await message.answer("이미 이 레시피를 처리 중입니다. 잠시만 기다려 주세요.")
            return
        try:
            async with lock:
                prepared = await _prepare_recipe_extraction(
                    context,
                    user_id=user_id,
                    name_hint=name_hint,
                    raw_text=raw_text,
                    last_ai_request=recipe_ai_last_request,
                )
                draft = await _resolve_recipe(
                    context,
                    user_id=user_id,
                    prepared=prepared,
                )
        except RecipeError as exc:
            await message.answer(str(exc))
            return
        except Exception:
            logger.exception("Recipe processing failed")
            await message.answer("레시피를 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.")
            return

        for draft_id, old_draft in list(recipe_drafts.items()):
            if old_draft.user_id == user_id:
                recipe_drafts.pop(draft_id, None)
        recipe_drafts[draft.draft_id] = draft
        pending_recipe_names.pop(user_id, None)
        await message.answer(
            _recipe_draft_text(draft, from_cache=prepared.from_cache),
            reply_markup=_recipe_confirmation_keyboard(draft.draft_id),
        )

    async def process_menu_search(message: Message, *, query: str, input_hash: str) -> None:
        user_id = message.from_user.id if message.from_user else 0
        lock = menu_locks.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            await message.answer("이미 외식 메뉴를 검색 중입니다. 잠시만 기다려 주세요.")
            return
        async with lock:
            await process_menu_search_unlocked(message, query=query, input_hash=input_hash)

    async def process_menu_search_unlocked(
        message: Message, *, query: str, input_hash: str
    ) -> None:
        user_id = message.from_user.id if message.from_user else 0
        async with context.sessions() as session:
            cached = await get_menu_search_cache(
                session,
                input_hash=input_hash,
                search_version=MENU_SEARCH_VERSION,
                max_age_days=context.settings.menu_search_cache_days,
            )
        if cached is not None:
            evidence = MenuNutritionEvidence.model_validate(cached.result_json)
            searched_urls = tuple(cached.searched_urls or [])
            if not is_usable_menu_evidence(evidence, searched_urls):
                await message.answer(
                    "최근 검색에서 이 메뉴의 완전한 공식 영양정보를 찾지 못했습니다.\n"
                    f"{context.settings.menu_search_cache_days}일 동안 같은 검색은 AI를 다시 "
                    "호출하지 않습니다. 일반 음식값은 /food 로 찾아볼 수 있습니다."
                )
                return
            draft = MenuSearchDraft(
                draft_id=uuid4().hex[:16],
                user_id=user_id,
                query=query,
                input_hash=input_hash,
                evidence=evidence,
                searched_urls=searched_urls,
                from_cache=True,
            )
        else:
            if context.menu_searcher is None:
                await message.answer("외식 메뉴 검색에는 OPENAI_API_KEY 설정이 필요합니다.")
                return
            elapsed = monotonic() - menu_ai_last_request.get(user_id, -10_000)
            cooldown = context.settings.menu_ai_cooldown_seconds
            if elapsed < cooldown:
                await message.answer(
                    f"AI 외식 메뉴 검색은 {cooldown - elapsed:.0f}초 뒤 다시 시도할 수 있습니다."
                )
                return

            await message.answer(
                f"‘{query}’의 공식 브랜드 영양정보를 찾는 중입니다. 최대 1회만 검색합니다."
            )
            async with context.sessions() as session:
                await ensure_user(session, user_id, context.settings.app_timezone)
                usage, limit_error = await reserve_ai_usage(
                    session,
                    user_id=user_id,
                    feature="menu_lookup",
                    input_hash=input_hash,
                    timezone_name=context.settings.app_timezone,
                    daily_limit=context.settings.menu_ai_daily_limit,
                    monthly_limit=context.settings.menu_ai_monthly_limit,
                    feature_label="AI 외식 메뉴 검색",
                    global_daily_limit=context.settings.menu_ai_global_daily_limit,
                    global_monthly_limit=context.settings.menu_ai_global_monthly_limit,
                )
                await session.commit()
            if usage is None:
                await message.answer(limit_error or "AI 외식 메뉴 검색 한도에 도달했습니다.")
                return

            menu_ai_last_request[user_id] = monotonic()
            try:
                result = await context.menu_searcher.search(query=query, user_id=user_id)
            except Exception as exc:
                logger.exception("OpenAI official menu search failed")
                async with context.sessions() as session:
                    await finish_ai_usage(
                        session,
                        usage.id,
                        status="failed",
                        error=type(exc).__name__,
                    )
                    await session.commit()
                await message.answer("공식 메뉴 검색에 실패했습니다. 잠시 후 다시 시도해 주세요.")
                return

            async with context.sessions() as session:
                await finish_ai_usage(
                    session,
                    usage.id,
                    status="completed",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                )
                await save_menu_search_cache(
                    session,
                    input_hash=input_hash,
                    search_version=MENU_SEARCH_VERSION,
                    query=query,
                    result_json=result.evidence.model_dump(mode="json"),
                    searched_urls=list(result.searched_urls),
                )
                await session.commit()

            if not is_usable_menu_evidence(result.evidence, result.searched_urls):
                await message.answer(
                    "정확한 메뉴·제공량·칼로리·탄단지가 모두 적힌 공식 브랜드 자료를 "
                    "찾지 못했습니다. 추정값은 만들지 않았습니다.\n"
                    "일반 음식값은 /food 메뉴명으로 찾아볼 수 있습니다."
                )
                return
            draft = MenuSearchDraft(
                draft_id=uuid4().hex[:16],
                user_id=user_id,
                query=query,
                input_hash=input_hash,
                evidence=result.evidence,
                searched_urls=result.searched_urls,
                from_cache=False,
            )

        for draft_id, old_draft in list(menu_drafts.items()):
            if old_draft.user_id == user_id:
                menu_drafts.pop(draft_id, None)
        menu_drafts[draft.draft_id] = draft
        await message.answer(
            _menu_draft_text(draft),
            reply_markup=_menu_confirmation_keyboard(draft.draft_id),
        )

    @router.message(Command("whoami"))
    async def whoami(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(f"내 Telegram ID: {user_id}")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            await session.commit()
        await message.answer(
            "칼로리 기록 봇이 준비됐습니다.\n\n"
            "1) 바코드는 선명하게 찍되 화면 가득 확대하지 말고, 가능하면 제품명이나 "
            "영양정보 표도 함께 보이도록 사진을 보내세요.\n"
            "2) 처음 보는 제품은 같은 사진에서 제품명과 영양정보까지 자동으로 확인합니다.\n"
            "3) 정보가 부족할 때만 필요한 사진을 추가로 요청합니다. 여러 장은 앨범으로 "
            "한 번에 보낼 수 있습니다.\n"
            "4) 일반 음식은 /food, 외식 메뉴는 /menu, 요리는 /recipe 로 기록할 수 있습니다.\n"
            "5) 등록한 상품은 /search 또는 이름만 입력해 한·일 상품명과 관련 태그로 "
            "다시 찾을 수 있습니다.\n"
            "6) 숫자를 확인한 뒤 기록 버튼을 누르세요.\n\n"
            "명령어는 /help 에서 볼 수 있습니다."
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        await message.answer(
            "/ping — 봇·DB 상태 확인\n"
            "/today — 오늘 합계\n"
            "/recent — 최근 기록\n"
            "/undo — 마지막 기록 취소\n"
            "/goal 2000 250 130 60 — kcal/탄/단/지 목표\n"
            "/search 닭가슴살 — 저장된 상품명·브랜드·한일 태그 검색\n"
            "/food 삶은 달걀 — 일반 음식 검색\n"
            "/menu 스타벅스 카페 라떼 Tall — 외식 메뉴 공식 영양정보 검색\n"
            "/recipe 김치볶음밥 — 재료로 레시피 계산\n"
            "/barcode 8801234567890 — 바코드 숫자로 시작\n"
            "/manual 이름 | kcal | 탄수 | 단백질 | 지방 — 직접 기록\n"
            "/cancel — 진행 중인 입력 취소\n"
            "/whoami — 내 Telegram ID 확인\n\n"
            "신규 제품일 수 있으니 첫 사진은 바코드만 화면 가득 찍기보다 제품명이나 영양표도 "
            "함께 담아주세요. 봇이 같은 사진에서 제품명·영양표를 확인합니다. "
            "사진이 더 필요하면 부족한 항목만 안내하며, 최대 3장을 앨범으로 보낼 수 있습니다.\n\n"
            "섭취량 입력 예: 45g, 250ml, 2개/2個, 1本, 0.5봉/0.5袋, "
            "70%, 절반/半分"
        )

    @router.message(Command("ping"))
    async def ping(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        started = monotonic()
        database_started = monotonic()
        try:
            async with context.sessions() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            logger.exception("Database ping failed")
            await message.answer("⚠️ 봇은 응답 중이지만 DB 연결에 실패했습니다.")
            return

        database_ms = (monotonic() - database_started) * 1_000
        processing_ms = (monotonic() - started) * 1_000
        uptime = _format_uptime(monotonic() - context.started_at)
        await message.answer(
            "🏓 Pong!\n"
            f"봇: 정상 · 내부 처리 {processing_ms:.0f} ms\n"
            f"DB: 정상 · 조회 {database_ms:.0f} ms\n"
            f"가동 시간: {uptime}"
        )

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        async with context.sessions() as session:
            user = await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            summary = await get_daily_summary(session, user)
            await session.commit()
        totals = summary.totals
        lines = [
            f"오늘 {summary.item_count}건",
            f"{_fmt(totals.kcal)} kcal",
            f"탄 {_fmt(totals.carbs_g)} g · 단 {_fmt(totals.protein_g)} g · "
            f"지 {_fmt(totals.fat_g)} g",
        ]
        if summary.goals:
            goals = summary.goals
            lines.extend(
                [
                    "",
                    f"목표 {_fmt(goals.kcal)} kcal",
                    f"탄 {_fmt(goals.carbs_g)} g · 단 {_fmt(goals.protein_g)} g · "
                    f"지 {_fmt(goals.fat_g)} g",
                ]
            )
        await message.answer("\n".join(lines))

    @router.message(Command("recent"))
    async def recent(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            logs = await recent_logs(session, message.from_user.id)
            await session.commit()
        if not logs:
            await message.answer("아직 기록이 없습니다.")
            return
        lines = ["최근 기록"]
        for log in logs:
            name = log.product_version.product.name
            portion_text = f"×{_fmt(log.multiplier)}"
            if log.input_amount is not None and log.input_unit:
                portion_text = display_portion(ParsedPortion(log.input_amount, log.input_unit))
            lines.append(f"· {name} · {portion_text} — {_fmt(log.kcal)} kcal")
        lines.append("\n아래 버튼을 누르면 해당 상품의 섭취량을 다시 선택할 수 있습니다.")
        await message.answer(
            "\n".join(lines),
            reply_markup=_recent_products_keyboard(logs),
        )

    @router.message(Command("undo"))
    async def undo(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            log = await undo_last_intake(session, message.from_user.id)
            await session.commit()
        if log is None:
            await message.answer("취소할 기록이 없습니다.")
        else:
            await message.answer(
                f"취소됨: {log.product_version.product.name} · {_fmt(log.kcal)} kcal"
            )

    @router.message(Command("goal"))
    async def goal(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        parts = (message.text or "").split()[1:]
        if len(parts) != 4:
            await message.answer("형식: /goal 2000 250 130 60 (kcal 탄수 단백질 지방)")
            return
        try:
            kcal, carbs, protein, fat = [parse_positive_decimal(value) for value in parts]
        except ValueError as exc:
            await message.answer(str(exc))
            return
        async with context.sessions() as session:
            user = await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            await set_goals(session, user, kcal=kcal, carbs=carbs, protein=protein, fat=fat)
            await session.commit()
        await message.answer("일일 목표를 저장했습니다.")

    @router.message(Command("recipe"))
    async def recipe_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        user_id = message.from_user.id
        text_value = message.text or ""
        command_token = text_value.split(maxsplit=1)[0] if text_value.split() else "/recipe"
        body = text_value[len(command_token) :].strip()
        pending_portions.pop(user_id, None)
        pending_recipe_names.pop(user_id, None)
        for draft_id, draft in list(recipe_drafts.items()):
            if draft.user_id == user_id:
                recipe_drafts.pop(draft_id, None)

        if "\n" in body:
            await process_recipe_message(message, name_hint=None, raw_text=body)
            return
        pending_recipe_names[user_id] = body[:80] or None
        name_line = "" if body else "첫 줄에 레시피 이름을 적고, "
        await message.answer(
            f"{name_line}재료를 한 줄씩 보내주세요. 마지막에 총 인분을 적을 수 있습니다.\n\n"
            "예:\n"
            + ("" if body else "김치볶음밥\n")
            + "밥 420g\n김치 160g\n돼지고기 120g\n달걀 2개\n총 2인분\n\n"
            "자연어 문장도 가능하지만, 위 형식은 OpenAI를 호출하지 않습니다.\n"
            "취소하려면 /cancel"
        )

    @router.message(Command("menu"))
    async def menu_search(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        user_id = message.from_user.id
        text_value = message.text or ""
        command_token = text_value.split(maxsplit=1)[0] if text_value.split() else "/menu"
        raw_query = text_value[len(command_token) :].strip()
        try:
            query = normalize_menu_query(
                raw_query,
                max_chars=context.settings.menu_ai_max_query_chars,
            )
        except MenuLookupError as exc:
            await message.answer(
                f"형식: /menu 브랜드명 메뉴명 크기\n예: /menu 스타벅스 카페 라떼 Tall\n\n{exc}"
            )
            return

        pending_portions.pop(user_id, None)
        pending_recipe_names.pop(user_id, None)
        for draft_id, draft in list(menu_drafts.items()):
            if draft.user_id == user_id:
                menu_drafts.pop(draft_id, None)
        input_hash = menu_query_hash(query)
        async with context.sessions() as session:
            await ensure_user(session, user_id, context.settings.app_timezone)
            version = await find_product_by_external_id(session, "brand_menu", input_hash)
            await session.commit()
        if version is not None:
            await message.answer(
                "저장된 공식 메뉴 영양정보를 불러왔습니다. AI를 호출하지 않았습니다."
            )
            await _offer_version(context, message, version)
            return
        await process_menu_search(message, query=query, input_hash=input_hash)

    @router.message(Command("manual"))
    async def manual(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        pending_recipe_names.pop(message.from_user.id, None)
        body = (message.text or "").partition(" ")[2]
        parts = [part.strip() for part in body.split("|")]
        if len(parts) != 5 or not parts[0]:
            await message.answer("형식: /manual 닭가슴살 | 165 | 0 | 31 | 3.6")
            return
        try:
            values = [Decimal(value) for value in parts[1:]]
            if any(not value.is_finite() or value < 0 for value in values):
                raise ValueError
        except (InvalidOperation, ValueError):
            await message.answer("영양값은 0 이상의 숫자로 입력하세요.")
            return
        candidate = ProductCandidate(
            name=parts[0],
            basis_amount=Decimal("1"),
            basis_unit="serving",
            kcal=values[0],
            carbs_g=values[1],
            protein_g=values[2],
            fat_g=values[3],
            source="manual",
            verified=True,
        )
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            version = await create_product_version(
                session, candidate, owner_id=message.from_user.id
            )
            log = await add_intake(
                session,
                user_id=message.from_user.id,
                version=version,
                multiplier=Decimal("1"),
                input_amount=Decimal("1"),
                input_unit="serving",
            )
            await session.commit()
        await message.answer(f"기록됨: {candidate.name} · {_fmt(log.kcal)} kcal")

    @router.message(Command("search"))
    async def search_saved(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        user_id = message.from_user.id
        text_value = message.text or ""
        command_token = text_value.split(maxsplit=1)[0] if text_value.split() else "/search"
        query = text_value[len(command_token) :].strip()
        pending_portions.pop(user_id, None)
        pending_recipe_names.pop(user_id, None)
        await process_saved_product_search(message, query)

    @router.message(Command("food"))
    async def food_search(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        query = " ".join((message.text or "").partition(" ")[2].split())
        if len(query) < 2 or len(query) > 50:
            await message.answer("형식: /food 삶은 달걀 (2~50자 음식명)")
            return

        pending_portions.pop(message.from_user.id, None)
        pending_recipe_names.pop(message.from_user.id, None)
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            cached = await search_catalog_products(
                session,
                source=MFDS_SOURCE,
                terms=search_terms(query),
            )
            await session.commit()
        ranked_cached = _rank_food_versions(query, cached)
        strong_cached = [
            version
            for version in ranked_cached
            if food_match_score(query, version.product.name) >= 900
        ][:5]
        if strong_cached:
            await message.answer(
                f"‘{query}’ 검색 결과입니다. 먹은 음식과 가장 가까운 항목을 선택하세요.",
                reply_markup=_food_results_keyboard(strong_cached),
            )
            return

        if context.food_catalog is None:
            await message.answer(
                "일반 음식 검색용 MFDS_API_KEY가 아직 설정되지 않았습니다.\n"
                "공공데이터포털에서 식품영양성분DB 활용 신청 후 서버 .env에 키를 넣어주세요."
            )
            return

        await message.answer(f"‘{query}’을(를) 식약처 영양 DB에서 찾는 중입니다.")
        try:
            candidates = await context.food_catalog.search(query, limit=5)
        except MfdsCatalogError as exc:
            logger.warning("MFDS food search failed: %s", exc)
            if ranked_cached:
                await message.answer(
                    "식약처 DB 연결이 원활하지 않아 저장된 유사 항목을 보여드립니다.",
                    reply_markup=_food_results_keyboard(ranked_cached[:5]),
                )
            else:
                await message.answer(str(exc))
            return

        if not candidates:
            await message.answer(
                "검색 결과가 없습니다. ‘계란’ 대신 ‘달걀’처럼 다른 표현이나 "
                "더 짧은 음식명으로 다시 검색해 주세요."
            )
            return

        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            versions = [
                await get_or_create_catalog_product(session, candidate) for candidate in candidates
            ]
            await session.commit()
        versions = _rank_food_versions(query, versions)[:5]
        await message.answer(
            f"‘{query}’ 검색 결과입니다. 먹은 음식과 가장 가까운 항목을 선택하세요.\n"
            "조리법과 크기에 따라 실제 영양값은 달라질 수 있습니다.",
            reply_markup=_food_results_keyboard(versions),
        )

    @router.message(Command("barcode"))
    async def barcode_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        pending_recipe_names.pop(message.from_user.id, None)
        raw = (message.text or "").partition(" ")[2]
        try:
            barcode = normalize_barcode(raw)
        except ValueError as exc:
            await message.answer(f"{exc}\n예: /barcode 8801234567890")
            return
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            version = await find_product_by_barcode(session, barcode, message.from_user.id)
            await session.commit()
        if version:
            pending_portions.pop(message.from_user.id, None)
            await _offer_version(context, message, version)
            return

        candidate = await context.catalog.lookup(barcode)
        if candidate:
            async with context.sessions() as session:
                version = await create_product_version(session, candidate, owner_id=None)
                await session.commit()
            pending_portions.pop(message.from_user.id, None)
            await _offer_version(context, message, version)
            return

        await _begin_recognition_job(context, message.from_user.id, barcode)
        await message.answer(
            "처음 보는 제품입니다. 제품명과 영양정보 표가 함께 보이는 사진을 보내주세요.\n"
            "서로 다른 면이라면 사진을 최대 3장까지 앨범으로 한 번에 보내도 됩니다."
        )

    @router.message(Command("cancel"))
    async def cancel_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        paths: list[str | None] = []
        had_pending_portion = pending_portions.pop(message.from_user.id, None) is not None
        had_pending_recipe = message.from_user.id in pending_recipe_names
        pending_recipe_names.pop(message.from_user.id, None)
        removed_draft = False
        removed_album = False
        for key in [key for key in photo_album_buffers if key[0] == message.from_user.id]:
            photo_album_buffers.pop(key, None)
            photo_album_overflow.discard(key)
            task = photo_album_tasks.pop(key, None)
            if task:
                task.cancel()
            removed_album = True
        for draft_id, draft in list(recipe_drafts.items()):
            if draft.user_id == message.from_user.id:
                recipe_drafts.pop(draft_id, None)
                removed_draft = True
        for draft_id, draft in list(menu_drafts.items()):
            if draft.user_id == message.from_user.id:
                menu_drafts.pop(draft_id, None)
                removed_draft = True
        async with context.sessions() as session:
            job = await get_active_job(session, message.from_user.id)
            if job:
                job.state = "canceled"
                paths = [job.front_path, job.label_path]
            await session.commit()
        for path in paths:
            remove_private_image(path)
        await message.answer(
            "진행 중인 입력을 취소했습니다."
            if paths or had_pending_portion or had_pending_recipe or removed_draft or removed_album
            else "진행 중인 입력이 없습니다."
        )

    @router.message(F.photo)
    async def photo(message: Message, bot: Bot) -> None:
        if not await _guard(message, context.settings):
            return
        raw = await _download_photo(bot, message)
        pending_portions.pop(message.from_user.id, None)
        pending_recipe_names.pop(message.from_user.id, None)
        if message.media_group_id:
            key = (message.from_user.id, str(message.media_group_id))
            album = photo_album_buffers.setdefault(key, [])
            if len(album) < 3:
                album.append((message, raw))
            else:
                photo_album_overflow.add(key)
            previous_task = photo_album_tasks.get(key)
            if previous_task:
                previous_task.cancel()
            photo_album_tasks[key] = asyncio.create_task(
                flush_photo_album(key), name=f"photo-album-{key[0]}-{key[1]}"
            )
            return
        await process_photo_batch(message, [raw])

    @router.callback_query(F.data.startswith("portion:"))
    async def select_portion(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            _, version_raw, action = callback.data.split(":", 2)
            version_id = int(version_raw)
        except (ValueError, AttributeError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return

        async with context.sessions() as session:
            await ensure_user(session, callback.from_user.id, context.settings.app_timezone)
            version = await get_product_version(session, version_id, callback.from_user.id)
            if version is None:
                await callback.answer("상품 정보를 찾지 못했습니다.", show_alert=True)
                return
            if action == "custom":
                pending_portions[callback.from_user.id] = version_id
                await callback.answer()
                if callback.message:
                    await callback.message.answer(
                        "먹은 양을 입력해 주세요.\n"
                        "예: 45g, 250ml, 2개, 0.5봉, 70%, 절반\n"
                        "취소하려면 /cancel"
                    )
                return

            if action == "basis":
                portion = ParsedPortion(version.basis_amount, version.basis_unit)
            elif action == "full":
                portion = ParsedPortion(Decimal("1"), "package")
            elif action == "half":
                portion = ParsedPortion(Decimal("50"), "percent")
            elif action == "serving":
                portion = ParsedPortion(Decimal("1"), "serving")
            elif action == "last":
                last_log = await get_last_portion(session, callback.from_user.id, version_id)
                if last_log is None or last_log.input_amount is None or not last_log.input_unit:
                    await callback.answer("지난 섭취량을 찾지 못했습니다.", show_alert=True)
                    return
                portion = ParsedPortion(last_log.input_amount, last_log.input_unit)
            elif action.startswith("quick"):
                try:
                    quick_index = int(action.removeprefix("quick"))
                    _, portion = _stored_quick_portions(version)[quick_index]
                except (ValueError, IndexError):
                    await callback.answer("참고 섭취량을 찾지 못했습니다.", show_alert=True)
                    return
            else:
                await callback.answer("지원하지 않는 선택입니다.", show_alert=True)
                return

            try:
                multiplier = portion_multiplier(version, portion)
            except PortionError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            log = await add_intake(
                session,
                user_id=callback.from_user.id,
                version=version,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
            )
            await session.commit()

        pending_portions.pop(callback.from_user.id, None)
        await callback.answer("기록했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer(
                f"기록됨: {version.product.name} · {display_portion(portion)} · "
                f"{_fmt(log.kcal)} kcal"
            )

    @router.callback_query(F.data.startswith("pick:"))
    async def pick_saved_product(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            version_id = int((callback.data or "").partition(":")[2])
        except ValueError:
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return
        async with context.sessions() as session:
            version = await get_product_version(session, version_id, callback.from_user.id)
        if version is None:
            await callback.answer("상품 정보를 찾지 못했습니다.", show_alert=True)
            return
        pending_portions.pop(callback.from_user.id, None)
        await callback.answer()
        await _clear_inline_keyboard(callback)
        if callback.message:
            await _offer_version(
                context,
                callback.message,
                version,
                user_id=callback.from_user.id,
            )

    @router.callback_query(F.data.startswith("food:"))
    async def select_food(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            version_id = int((callback.data or "").split(":", 1)[1])
        except (ValueError, IndexError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return
        async with context.sessions() as session:
            version = await get_product_version(session, version_id, callback.from_user.id)
        if version is None or version.source != MFDS_SOURCE:
            await callback.answer("음식 정보를 찾지 못했습니다.", show_alert=True)
            return
        await callback.answer()
        await _clear_inline_keyboard(callback)
        if callback.message:
            await _offer_version(
                context,
                callback.message,
                version,
                user_id=callback.from_user.id,
            )

    @router.callback_query(F.data.startswith("log:"))
    async def log_portion(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            _, version_raw, multiplier_raw = callback.data.split(":", 2)
            version_id = int(version_raw)
            multiplier = parse_positive_decimal(multiplier_raw, maximum=Decimal("100"))
        except (ValueError, AttributeError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return
        async with context.sessions() as session:
            await ensure_user(session, callback.from_user.id, context.settings.app_timezone)
            version = await get_product_version(session, version_id, callback.from_user.id)
            if version is None:
                await callback.answer("상품 정보를 찾지 못했습니다.", show_alert=True)
                return
            log = await add_intake(
                session,
                user_id=callback.from_user.id,
                version=version,
                multiplier=multiplier,
            )
            await session.commit()
        await callback.answer("기록했습니다.")
        if callback.message:
            await callback.message.answer(
                f"기록됨: {version.product.name} ×{_fmt(multiplier)} · {_fmt(log.kcal)} kcal"
            )

    @router.callback_query(F.data.startswith("unitfix:"))
    async def correct_unit_and_log(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            _, version_raw, amount_raw, unit = callback.data.split(":", 3)
            version_id = int(version_raw)
            portion = ParsedPortion(
                parse_positive_decimal(amount_raw, maximum=Decimal("1000000")), unit
            )
        except (ValueError, AttributeError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return

        async with context.sessions() as session:
            await ensure_user(session, callback.from_user.id, context.settings.app_timezone)
            version = await get_product_version(session, version_id, callback.from_user.id)
            if version is None:
                await callback.answer("상품 정보를 찾지 못했습니다.", show_alert=True)
                return
            if not _can_correct_basis_unit(version, portion):
                await callback.answer(
                    "현재 상품 정보에는 이 단위 보정을 적용할 수 없습니다.",
                    show_alert=True,
                )
                return
            corrected = await create_product_version(
                session,
                _candidate_with_basis_unit(version, portion.unit),
                owner_id=callback.from_user.id,
            )
            try:
                multiplier = portion_multiplier(corrected, portion)
            except PortionError as exc:
                await callback.answer(str(exc), show_alert=True)
                return
            log = await add_intake(
                session,
                user_id=callback.from_user.id,
                version=corrected,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
            )
            await session.commit()

        pending_portions.pop(callback.from_user.id, None)
        await callback.answer("단위를 수정하고 기록했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer(
                f"기록됨: {corrected.product.name} · {display_portion(portion)} · "
                f"{_fmt(log.kcal)} kcal\n"
                f"앞으로 이 제품은 {_fmt(corrected.basis_amount)} {corrected.basis_unit} "
                "기준으로 계산합니다."
            )

    @router.callback_query(F.data == "unitfix_cancel")
    async def cancel_unit_correction(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        pending_portions.pop(callback.from_user.id, None)
        await callback.answer("취소했습니다.")
        await _clear_inline_keyboard(callback)

    @router.callback_query(F.data.startswith("market:"))
    async def correct_label_market(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            _, job_raw, market = (callback.data or "").split(":", 2)
            job_id = int(job_raw)
            if market not in {"KR", "JP"}:
                raise ValueError
        except ValueError:
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return

        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if (
                job is None
                or job.user_telegram_id != callback.from_user.id
                or job.state != "awaiting_confirm"
                or not job.result_json
            ):
                await callback.answer("만료되었거나 처리된 요청입니다.", show_alert=True)
                return
            result = NutritionRecognition.model_validate(job.result_json)
            result.label_market = market
            job.result_json = result.model_dump(mode="json")
            await session.commit()

        await callback.answer(f"{_market_text(market)} 표시 형식으로 설정했습니다.")
        if callback.message:
            try:
                await callback.message.edit_text(
                    _recognition_result_text(result),
                    reply_markup=_confirmation_keyboard(job_id, market),
                )
            except TelegramBadRequest:
                pass

    @router.callback_query(F.data.startswith("confirm:"))
    async def confirm_recognition(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            job_id = int(callback.data.split(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return
        paths: list[str | None] = []
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if (
                job is None
                or job.user_telegram_id != callback.from_user.id
                or job.state != "awaiting_confirm"
                or not job.result_json
            ):
                await callback.answer("만료되었거나 처리된 요청입니다.", show_alert=True)
                return
            result = NutritionRecognition.model_validate(job.result_json)
            candidate = _candidate_from_recognition(job.barcode, result)
            version = await create_product_version(
                session, candidate, owner_id=callback.from_user.id
            )
            job.state = "completed"
            paths = [job.front_path, job.label_path]
            await session.commit()
        for path in paths:
            remove_private_image(path)
        await callback.answer("저장했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer(
                "제품 정보를 저장했습니다. 다음부터 같은 바코드는 AI 호출 없이 불러옵니다."
            )
            await _offer_version(context, callback.message, version, user_id=callback.from_user.id)

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_recognition(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        try:
            job_id = int(callback.data.split(":", 1)[1])
        except (ValueError, AttributeError):
            await callback.answer("잘못된 요청입니다.", show_alert=True)
            return
        paths: list[str | None] = []
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job is None or job.user_telegram_id != callback.from_user.id:
                await callback.answer("요청을 찾지 못했습니다.", show_alert=True)
                return
            job.state = "canceled"
            paths = [job.front_path, job.label_path]
            await session.commit()
        for path in paths:
            remove_private_image(path)
        await callback.answer("취소했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer("인식 결과를 저장하지 않았습니다.")

    @router.callback_query(F.data.startswith("recipe_save:"))
    async def save_recipe(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        draft_id = (callback.data or "").partition(":")[2]
        draft = recipe_drafts.get(draft_id)
        if draft is None or draft.user_id != callback.from_user.id:
            await callback.answer("만료되었거나 처리된 레시피입니다.", show_alert=True)
            return
        async with context.sessions() as session:
            await ensure_user(session, callback.from_user.id, context.settings.app_timezone)
            version = await create_product_version(
                session,
                _recipe_candidate(draft),
                owner_id=callback.from_user.id,
            )
            await session.commit()
        recipe_drafts.pop(draft_id, None)
        await callback.answer("레시피를 저장했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer("1인분 영양정보로 저장했습니다. 먹은 양을 선택해 주세요.")
            await _offer_version(
                context,
                callback.message,
                version,
                user_id=callback.from_user.id,
            )

    @router.callback_query(F.data.startswith("recipe_cancel:"))
    async def cancel_recipe(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        draft_id = (callback.data or "").partition(":")[2]
        draft = recipe_drafts.get(draft_id)
        if draft is None or draft.user_id != callback.from_user.id:
            await callback.answer("만료되었거나 처리된 레시피입니다.", show_alert=True)
            return
        recipe_drafts.pop(draft_id, None)
        await callback.answer("취소했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer("레시피를 저장하지 않았습니다.")

    @router.callback_query(F.data.startswith("menu_save:"))
    async def save_menu(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        draft_id = (callback.data or "").partition(":")[2]
        draft = menu_drafts.get(draft_id)
        if draft is None or draft.user_id != callback.from_user.id:
            await callback.answer("만료되었거나 처리된 메뉴 검색입니다.", show_alert=True)
            return
        try:
            candidate = _menu_candidate(draft)
        except MenuLookupError as exc:
            menu_drafts.pop(draft_id, None)
            await callback.answer(str(exc), show_alert=True)
            await _clear_inline_keyboard(callback)
            return
        async with context.sessions() as session:
            await ensure_user(session, callback.from_user.id, context.settings.app_timezone)
            version = await get_or_create_catalog_product(session, candidate)
            await session.commit()
        menu_drafts.pop(draft_id, None)
        await callback.answer("공식 메뉴 정보를 저장했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer(
                "공식 출처와 함께 저장했습니다. 다음부터 같은 검색은 AI를 호출하지 않습니다."
            )
            await _offer_version(
                context,
                callback.message,
                version,
                user_id=callback.from_user.id,
            )

    @router.callback_query(F.data.startswith("menu_cancel:"))
    async def cancel_menu(callback: CallbackQuery) -> None:
        if not callback.from_user or not _allowed(callback.from_user.id, context.settings):
            await callback.answer("사용 권한이 없습니다.", show_alert=True)
            return
        draft_id = (callback.data or "").partition(":")[2]
        draft = menu_drafts.get(draft_id)
        if draft is None or draft.user_id != callback.from_user.id:
            await callback.answer("만료되었거나 처리된 메뉴 검색입니다.", show_alert=True)
            return
        menu_drafts.pop(draft_id, None)
        await callback.answer("취소했습니다.")
        await _clear_inline_keyboard(callback)
        if callback.message:
            await callback.message.answer("메뉴 정보를 저장하지 않았습니다.")

    @router.message(F.text)
    async def text_input(message: Message) -> None:
        if not message.from_user:
            return
        if (message.text or "").startswith("/"):
            return
        if not await _guard(message, context.settings):
            return
        if message.from_user.id in pending_recipe_names:
            await process_recipe_message(
                message,
                name_hint=pending_recipe_names[message.from_user.id],
                raw_text=message.text or "",
            )
            return
        if message.from_user.id not in pending_portions:
            if any(
                draft.user_id == message.from_user.id
                for draft in (*recipe_drafts.values(), *menu_drafts.values())
            ):
                return
            async with context.sessions() as session:
                job = await get_active_job(session, message.from_user.id)
            if job is not None:
                return
            await process_saved_product_search(message, message.text or "")
            return
        try:
            portion = parse_portion(message.text or "")
        except PortionError as exc:
            await message.answer(f"입력 형식을 확인해 주세요.\n{exc}")
            return

        version_id = pending_portions[message.from_user.id]
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            version = await get_product_version(session, version_id, message.from_user.id)
            if version is None:
                pending_portions.pop(message.from_user.id, None)
                await message.answer("상품 정보를 찾지 못했습니다. 바코드를 다시 보내주세요.")
                return
            try:
                multiplier = portion_multiplier(version, portion)
            except PortionError as exc:
                if _can_correct_basis_unit(version, portion):
                    pending_portions.pop(message.from_user.id, None)
                    await message.answer(
                        f"공개 DB의 영양 기준은 {_fmt(version.basis_amount)} "
                        f"{version.basis_unit}인데 포장 단위는 {version.package_unit}입니다.\n"
                        f"포장지 영양정보가 {_fmt(version.basis_amount)} {portion.unit} "
                        "기준인지 확인해 주세요. 맞다면 사용자 전용으로 단위를 수정한 뒤 "
                        f"{display_portion(portion)}를 기록합니다.",
                        reply_markup=_unit_correction_keyboard(version.id, portion),
                    )
                    return
                await message.answer(f"이 단위로 계산할 수 없습니다.\n{exc}")
                return
            log = await add_intake(
                session,
                user_id=message.from_user.id,
                version=version,
                multiplier=multiplier,
                input_amount=portion.amount,
                input_unit=portion.unit,
            )
            await session.commit()

        pending_portions.pop(message.from_user.id, None)
        await message.answer(
            f"기록됨: {version.product.name} · {display_portion(portion)} · {_fmt(log.kcal)} kcal"
        )

    return router


async def _handle_barcode_photos(
    context: BotContext, message: Message, raw_images: list[bytes]
) -> None:
    barcodes: list[str] = []
    for raw in raw_images:
        try:
            detected = await asyncio.to_thread(decode_barcodes, raw)
        except Exception:
            detected = []
        for barcode in detected:
            if barcode not in barcodes:
                barcodes.append(barcode)
    if not barcodes:
        await message.answer(
            "바코드를 읽지 못했습니다. 화면에 바코드가 크게 보이도록 다시 찍거나 "
            "/barcode 숫자로 입력하세요."
        )
        return
    barcode = barcodes[0]
    async with context.sessions() as session:
        version = await find_product_by_barcode(session, barcode, message.from_user.id)
    if version:
        await _offer_version(context, message, version)
        return

    candidate = await context.catalog.lookup(barcode)
    if candidate:
        async with context.sessions() as session:
            version = await create_product_version(session, candidate, owner_id=None)
            await session.commit()
        await _offer_version(context, message, version)
        return

    job = await _begin_recognition_job(context, message.from_user.id, barcode)
    await message.answer(
        f"바코드 {barcode}는 처음 봅니다. 같은 사진에서 제품명과 영양정보를 확인하는 중입니다."
    )
    await _handle_job_photos(context, message, job.id, raw_images)


def _unique_existing_paths(values: list[str | None]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        path = Path(value)
        if path.exists():
            result.append(path)
            seen.add(value)
    return result


def _store_job_paths(job: RecognitionJob, paths: list[Path]) -> None:
    retained = paths[-2:]
    job.front_path = str(retained[0]) if retained else None
    job.label_path = str(retained[1]) if len(retained) > 1 else None


def _remove_paths(paths: list[Path]) -> None:
    for path in paths:
        remove_private_image(str(path))


async def _prepare_recognition_images(
    context: BotContext, user_id: int, raw_images: list[bytes]
) -> list[Path]:
    paths: list[Path] = []
    try:
        for raw in raw_images[:3]:
            processed = await asyncio.to_thread(preprocess_image, raw)
            image_path = context.settings.uploads_dir / f"{user_id}-{uuid4().hex}.jpg"
            await asyncio.to_thread(write_private_image, image_path, processed)
            paths.append(image_path)
    except Exception:
        _remove_paths(paths)
        raise
    return paths


async def _handle_job_photos(
    context: BotContext,
    message: Message,
    job_id: int,
    raw_images: list[bytes],
) -> None:
    try:
        new_paths = await _prepare_recognition_images(context, message.from_user.id, raw_images)
    except Exception:
        await message.answer("이미지를 처리하지 못했습니다. JPG/PNG 사진으로 다시 보내주세요.")
        return

    selected_paths: list[Path] = []
    dropped_paths: list[Path] = []
    async with context.sessions() as session:
        job = await session.get(RecognitionJob, job_id)
        if job is None or job.user_telegram_id != message.from_user.id:
            _remove_paths(new_paths)
            await message.answer("인식 요청이 만료되었습니다. 바코드부터 다시 보내주세요.")
            return
        if job.state not in {"awaiting_front", "awaiting_label"}:
            _remove_paths(new_paths)
            await message.answer("현재 사진을 받을 단계가 아닙니다. /cancel 후 다시 시작해 주세요.")
            return
        existing_paths = _unique_existing_paths([job.front_path, job.label_path])
        all_paths = existing_paths + new_paths
        selected_paths = all_paths[-3:]
        dropped_paths = [path for path in all_paths if path not in selected_paths]
        _store_job_paths(job, selected_paths)
        job.state = "processing"
        await session.commit()
    _remove_paths(dropped_paths)

    if context.recognizer is None:
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job:
                job.state = "error"
                job.error = "OPENAI_API_KEY is not configured"
            await session.commit()
        _remove_paths(selected_paths)
        await message.answer(
            "사진은 받았지만 OPENAI_API_KEY가 설정되지 않았습니다. "
            ".env에 키를 넣고 재시작하거나 /manual 로 기록하세요."
        )
        return

    await message.answer(
        f"사진 {len(selected_paths)}장에서 제품명과 영양정보를 읽는 중입니다. 잠시만 기다려 주세요."
    )
    try:
        result = await context.recognizer.recognize_images(selected_paths)
    except Exception as exc:
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job and job.state != "canceled":
                job.state = "error"
                job.error = f"{type(exc).__name__}: {str(exc)[:400]}"
            await session.commit()
        _remove_paths(selected_paths)
        await message.answer(
            "영양정보를 확실히 읽지 못했습니다. /barcode로 다시 시작해 표를 더 가까이 "
            "찍거나 /manual 로 입력해 주세요."
        )
        return

    if _recognition_is_complete(result):
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job is None or job.state == "canceled":
                _remove_paths(selected_paths)
                return
            job.result_json = result.model_dump(mode="json")
            job.state = "awaiting_confirm"
            job.front_path = None
            job.label_path = None
            await session.commit()
        _remove_paths(selected_paths)
        await message.answer(
            _recognition_result_text(result),
            reply_markup=_confirmation_keyboard(job_id, result.label_market),
        )
        return

    retained_paths = selected_paths[-2:] if result.product_name_found or result.label_found else []
    async with context.sessions() as session:
        job = await session.get(RecognitionJob, job_id)
        if job is None or job.state == "canceled":
            _remove_paths(selected_paths)
            return
        job.result_json = None
        job.error = None
        job.state = "awaiting_label" if result.product_name_found else "awaiting_front"
        _store_job_paths(job, retained_paths)
        await session.commit()
    _remove_paths([path for path in selected_paths if path not in retained_paths])
    await message.answer(_recognition_follow_up_text(result))


def _candidate_from_recognition(
    barcode: str | None, result: NutritionRecognition
) -> ProductCandidate:
    package_amount = result.package_amount.amount if result.package_amount else None
    package_unit = result.package_amount.unit if result.package_amount else None
    salt = normalize_salt(result.nutrients.sodium_mg, result.nutrients.salt_equivalent_g)
    raw_data = result.model_dump(mode="json")
    raw_data["normalization"] = {
        "sodium_derived": salt.sodium_derived,
        "salt_equivalent_derived": salt.salt_equivalent_derived,
    }
    return ProductCandidate(
        barcode=barcode,
        name=result.product_name,
        brand=result.brand,
        basis_amount=result.nutrition_basis.amount,
        basis_unit=result.nutrition_basis.unit,
        package_amount=package_amount,
        package_unit=package_unit,
        servings_per_package=result.servings_per_package,
        piece_count=result.piece_count,
        kcal=result.nutrients.energy_kcal,
        carbs_g=result.nutrients.carbs_g,
        protein_g=result.nutrients.protein_g,
        fat_g=result.nutrients.fat_g,
        sodium_mg=salt.sodium_mg,
        salt_equivalent_g=salt.salt_equivalent_g,
        sodium_derived=salt.sodium_derived,
        salt_equivalent_derived=salt.salt_equivalent_derived,
        label_market=result.label_market,
        label_language=result.label_language,
        basis_text=result.nutrition_basis.raw_text or None,
        basis_metric_amount=result.nutrition_basis.metric_amount,
        basis_metric_unit=result.nutrition_basis.metric_unit,
        basis_count_amount=result.nutrition_basis.count_amount,
        basis_count_unit=result.nutrition_basis.count_unit,
        search_concepts=result.search_concepts,
        search_terms_ko=result.search_terms_ko,
        search_terms_ja=result.search_terms_ja,
        estimated_values=result.estimated_values,
        source="ai_label",
        verified=True,
        raw_data=raw_data,
    )


def _recognition_result_text(result: NutritionRecognition) -> str:
    candidate = _candidate_from_recognition(None, result)
    market = _market_text(result.label_market) or "❔ 형식 미확정"
    language = {"ko": "한국어", "ja": "일본어", "mixed": "혼합", "unknown": "미확인"}[
        result.label_language
    ]
    basis = f"{_fmt(candidate.basis_amount)} {candidate.basis_unit}"
    if candidate.basis_text:
        basis = f"{candidate.basis_text} → {basis}"
    lines = [
        f"AI 인식 결과 — 저장 전 확인\n{candidate.name}"
        f"{f' · {candidate.brand}' if candidate.brand else ''}",
        f"표시 형식: {market} · {language}",
        f"기준: {basis}",
        f"{_fmt(candidate.kcal)} kcal · 탄 {_fmt(candidate.carbs_g)} g · "
        f"단 {_fmt(candidate.protein_g)} g · 지 {_fmt(candidate.fat_g)} g",
    ]
    salt_text = _salt_text(candidate)
    if salt_text:
        lines.append(salt_text)
    lines.append(f"신뢰도: {_fmt(result.confidence * 100)}%")

    package_details = []
    if candidate.package_amount is not None and candidate.package_unit:
        package_details.append(f"총 {_fmt(candidate.package_amount)} {candidate.package_unit}")
    if candidate.servings_per_package is not None:
        package_details.append(f"{_fmt(candidate.servings_per_package)}회분")
    if candidate.piece_count is not None:
        package_details.append(f"{_fmt(candidate.piece_count)}개")
    if package_details:
        lines.append("포장 정보: " + " · ".join(package_details))

    search_tags = [
        term.term
        for term in build_product_search_terms(
            name=result.product_name,
            brand=result.brand,
            search_concepts=result.search_concepts,
            search_terms_ko=result.search_terms_ko,
            search_terms_ja=result.search_terms_ja,
            product_source="ai_label",
        )
        if term.locale in {"ko", "ja"}
    ][:8]
    if search_tags:
        lines.append("검색 태그: " + " · ".join(search_tags))

    warnings = recognition_warnings(result)
    if warnings:
        lines.append("\n주의: " + " ".join(warnings))
    return "\n".join(lines)


async def _begin_recognition_job(
    context: BotContext, user_id: int, barcode: str | None
) -> RecognitionJob:
    old_paths: list[str | None] = []
    async with context.sessions() as session:
        old_job = await get_active_job(session, user_id)
        if old_job:
            old_paths = [old_job.front_path, old_job.label_path]
        job = await start_job(session, user_id, barcode)
        await session.commit()
    for path in old_paths:
        remove_private_image(path)
    return job
