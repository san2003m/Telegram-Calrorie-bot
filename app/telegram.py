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
from app.models import IntakeLog, RecognitionJob
from app.nutrition import parse_positive_decimal, recognition_warnings
from app.portion import (
    ParsedPortion,
    PortionError,
    display_portion,
    package_multiplier,
    parse_portion,
    portion_multiplier,
)
from app.repository import (
    add_intake,
    create_product_version,
    ensure_user,
    find_product_by_barcode,
    get_active_job,
    get_daily_summary,
    get_last_portion,
    get_product_version,
    recent_logs,
    set_goals,
    start_job,
    undo_last_intake,
)
from app.schemas import NutritionRecognition, ProductCandidate

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotContext:
    settings: Settings
    sessions: async_sessionmaker[AsyncSession]
    catalog: OpenFoodFactsCatalog
    recognizer: NutritionRecognizer | None
    started_at: float = field(default_factory=monotonic)


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
    normalized = value.quantize(Decimal("0.1"))
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
    lines = [
        f"{product.name}{brand}\n영양 기준: {_fmt(version.basis_amount)} {version.basis_unit}",
        f"{_fmt(version.kcal)} kcal · 탄 {_fmt(version.carbs_g)} g · "
        f"단 {_fmt(version.protein_g)} g · 지 {_fmt(version.fat_g)} g",
    ]
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


def _can_use_portion(version, portion: ParsedPortion) -> bool:
    try:
        portion_multiplier(version, portion)
    except PortionError:
        return False
    return True


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


def _confirmation_keyboard(job_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="제품 정보 저장", callback_data=f"confirm:{job_id}"),
                InlineKeyboardButton(text="취소", callback_data=f"cancel:{job_id}"),
            ]
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


def create_router(context: BotContext) -> Router:
    router = Router(name="calorie-bot")
    pending_portions: dict[int, int] = {}

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
            "3) 숫자를 확인한 뒤 기록 버튼을 누르세요.\n\n"
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
            "/barcode 8801234567890 — 바코드 숫자로 시작\n"
            "/manual 이름 | kcal | 탄수 | 단백질 | 지방 — 직접 기록\n"
            "/cancel — 진행 중인 사진 인식 취소\n"
            "/whoami — 내 Telegram ID 확인\n\n"
            "섭취량 입력 예: 45g, 250ml, 2개, 0.5봉, 70%, 절반"
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

    @router.message(Command("manual"))
    async def manual(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
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

    @router.message(Command("barcode"))
    async def barcode_command(message: Message) -> None:
        if not await _guard(message, context.settings):
            return
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
            if paths or had_pending_portion
            else "진행 중인 입력이 없습니다."
        )

    @router.message(F.photo)
    async def photo(message: Message, bot: Bot) -> None:
        if not await _guard(message, context.settings):
            return
        raw = await _download_photo(bot, message)
        pending_portions.pop(message.from_user.id, None)
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

    @router.message(F.text)
    async def custom_portion(message: Message) -> None:
        if not message.from_user or message.from_user.id not in pending_portions:
            return
        if (message.text or "").startswith("/"):
            return
        if not await _guard(message, context.settings):
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
                "앞면을 저장했습니다. 이제 열량·탄수화물·단백질·지방이 보이도록 "
                "영양정보 표를 가까이 찍어 보내주세요."
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
    warnings = recognition_warnings(result)
    candidate = _candidate_from_recognition(None, result)
    result_text = (
        f"AI 인식 결과 — 저장 전 확인\n{candidate.name}"
        f"{f' · {candidate.brand}' if candidate.brand else ''}\n"
        f"기준: {_fmt(candidate.basis_amount)} {candidate.basis_unit}\n"
        f"{_fmt(candidate.kcal)} kcal · 탄 {_fmt(candidate.carbs_g)} g · "
        f"단 {_fmt(candidate.protein_g)} g · 지 {_fmt(candidate.fat_g)} g\n"
        f"신뢰도: {_fmt(result.confidence * 100)}%"
    )
    package_details = []
    if candidate.package_amount is not None and candidate.package_unit:
        package_details.append(f"총 {_fmt(candidate.package_amount)} {candidate.package_unit}")
    if candidate.servings_per_package is not None:
        package_details.append(f"{_fmt(candidate.servings_per_package)}회분")
    if candidate.piece_count is not None:
        package_details.append(f"{_fmt(candidate.piece_count)}개")
    if package_details:
        result_text += "\n포장 정보: " + " · ".join(package_details)
    if warnings:
        result_text += "\n\n주의: " + " ".join(warnings)
    await message.answer(result_text, reply_markup=_confirmation_keyboard(job_id))


def _candidate_from_recognition(
    barcode: str | None, result: NutritionRecognition
) -> ProductCandidate:
    package_amount = result.package_amount.amount if result.package_amount else None
    package_unit = result.package_amount.unit if result.package_amount else None
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
        source="ai_label",
        verified=True,
        raw_data=result.model_dump(mode="json"),
    )


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
