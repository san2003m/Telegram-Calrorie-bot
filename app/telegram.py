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
    finish_ai_usage,
    get_active_job,
    get_daily_summary,
    get_last_portion,
    get_or_create_catalog_product,
    get_product_version,
    get_recipe_parse_cache,
    recent_logs,
    reserve_recipe_ai_usage,
    save_recipe_parse_cache,
    search_catalog_products,
    search_recipe_products,
    set_goals,
    start_job,
    undo_last_intake,
)
from app.schemas import NutritionRecognition, ProductCandidate, RecipeExtraction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotContext:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    catalog: OpenFoodFactsCatalog
    food_catalog: MfdsFoodCatalog | None
    recognizer: NutritionRecognizer | None
    recipe_parser: RecipeAIParser | None = None
    started_at: float = field(default_factory=monotonic)


@dataclass(frozen=True)
class PreparedRecipeExtraction:
    extraction: RecipeExtraction
    input_hash: str
    used_ai: bool
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
        lines.append("포장 정보: " + " · ".join(package_details))
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


def create_router(context: BotContext) -> Router:
    router = Router(name="calorie-bot")
    pending_portions: dict[int, int] = {}
    pending_recipe_names: dict[int, str | None] = {}
    recipe_drafts: dict[str, RecipeDraft] = {}
    recipe_ai_last_request: dict[int, float] = {}
    recipe_locks: dict[int, asyncio.Lock] = {}

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
            "1) 바코드를 크게 찍어 보내세요.\n"
            "2) 처음 보는 제품이면 안내에 따라 앞면과 영양정보 표를 보내세요.\n"
            "3) 일반 음식은 /food, 요리는 /recipe 로 기록할 수 있습니다.\n"
            "4) 숫자를 확인한 뒤 기록 버튼을 누르세요.\n\n"
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
            "/food 삶은 달걀 — 일반 음식 검색\n"
            "/recipe 김치볶음밥 — 재료로 레시피 계산\n"
            "/barcode 8801234567890 — 바코드 숫자로 시작\n"
            "/manual 이름 | kcal | 탄수 | 단백질 | 지방 — 직접 기록\n"
            "/cancel — 진행 중인 입력 취소\n"
            "/whoami — 내 Telegram ID 확인\n\n"
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
        await message.answer("\n".join(lines))

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
        await message.answer("처음 보는 제품입니다. 제품 앞면 사진을 보내주세요.")

    @router.message(Command("cancel"))
    async def cancel_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
        paths: list[str | None] = []
        had_pending_portion = pending_portions.pop(message.from_user.id, None) is not None
        had_pending_recipe = message.from_user.id in pending_recipe_names
        pending_recipe_names.pop(message.from_user.id, None)
        removed_draft = False
        for draft_id, draft in list(recipe_drafts.items()):
            if draft.user_id == message.from_user.id:
                recipe_drafts.pop(draft_id, None)
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
            if paths or had_pending_portion or had_pending_recipe or removed_draft
            else "진행 중인 입력이 없습니다."
        )

    @router.message(F.photo)
    async def photo(message: Message, bot: Bot) -> None:
        if not await _guard(message, context.settings):
            return
        raw = await _download_photo(bot, message)
        pending_portions.pop(message.from_user.id, None)
        pending_recipe_names.pop(message.from_user.id, None)
        async with context.sessions() as session:
            await ensure_user(session, message.from_user.id, context.settings.app_timezone)
            job = await get_active_job(session, message.from_user.id)
            await session.commit()

        if job is None:
            await _handle_barcode_photo(context, message, raw)
            return
        await _handle_job_photo(context, message, job.id, raw)

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

    @router.message(F.text)
    async def custom_portion(message: Message) -> None:
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


async def _handle_barcode_photo(context: BotContext, message: Message, raw: bytes) -> None:
    try:
        barcodes = await asyncio.to_thread(decode_barcodes, raw)
    except Exception:
        barcodes = []
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

    await _begin_recognition_job(context, message.from_user.id, barcode)
    await message.answer(
        f"바코드 {barcode}는 처음 봅니다.\n제품 이름이 보이는 앞면 사진을 보내주세요."
    )


async def _handle_job_photo(context: BotContext, message: Message, job_id: int, raw: bytes) -> None:
    try:
        processed = await asyncio.to_thread(preprocess_image, raw)
    except Exception:
        await message.answer("이미지를 처리하지 못했습니다. JPG/PNG 사진으로 다시 보내주세요.")
        return
    image_path = context.settings.uploads_dir / f"{message.from_user.id}-{uuid4().hex}.jpg"
    await asyncio.to_thread(write_private_image, image_path, processed)

    front_path: Path | None = None
    label_path: Path | None = None
    async with context.sessions() as session:
        job = await session.get(RecognitionJob, job_id)
        if job is None or job.user_telegram_id != message.from_user.id:
            remove_private_image(str(image_path))
            await message.answer("인식 요청이 만료되었습니다. 바코드부터 다시 보내주세요.")
            return
        if job.state == "awaiting_front":
            job.front_path = str(image_path)
            job.state = "awaiting_label"
            await session.commit()
            await message.answer(
                "앞면을 저장했습니다. 이제 열량·탄수화물·단백질·지방과 "
                "나트륨 또는 食塩相当量이 보이도록 영양정보 표를 가까이 찍어 보내주세요."
            )
            return
        if job.state != "awaiting_label" or not job.front_path:
            remove_private_image(str(image_path))
            await message.answer("현재 사진을 받을 단계가 아닙니다. /cancel 후 다시 시작해 주세요.")
            return
        job.label_path = str(image_path)
        job.state = "processing"
        front_path = Path(job.front_path)
        label_path = image_path
        await session.commit()

    if context.recognizer is None:
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job:
                job.state = "error"
                job.error = "OPENAI_API_KEY is not configured"
            await session.commit()
        remove_private_image(str(front_path))
        remove_private_image(str(label_path))
        await message.answer(
            "사진은 받았지만 OPENAI_API_KEY가 설정되지 않았습니다. "
            ".env에 키를 넣고 재시작하거나 /manual 로 기록하세요."
        )
        return

    await message.answer("영양정보를 읽는 중입니다. 잠시만 기다려 주세요.")
    try:
        result = await context.recognizer.recognize(front_path, label_path)
        if not result.label_found:
            raise ValueError("영양정보 표를 확실히 읽지 못했습니다.")
    except Exception as exc:
        async with context.sessions() as session:
            job = await session.get(RecognitionJob, job_id)
            if job:
                job.state = "error"
                job.error = f"{type(exc).__name__}: {str(exc)[:400]}"
            await session.commit()
        remove_private_image(str(front_path))
        remove_private_image(str(label_path))
        await message.answer(
            "영양정보를 확실히 읽지 못했습니다. /barcode로 다시 시작해 표를 더 가까이 "
            "찍거나 /manual 로 입력해 주세요."
        )
        return

    async with context.sessions() as session:
        job = await session.get(RecognitionJob, job_id)
        if job is None:
            return
        job.result_json = result.model_dump(mode="json")
        job.state = "awaiting_confirm"
        await session.commit()
    await message.answer(
        _recognition_result_text(result),
        reply_markup=_confirmation_keyboard(job_id, result.label_market),
    )


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
