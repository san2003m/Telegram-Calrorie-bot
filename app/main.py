from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from app.ai_recognition import NutritionRecognizer
from app.catalog import OpenFoodFactsCatalog
from app.config import get_settings
from app.db import Database
from app.health import create_health_app
from app.image_tools import cleanup_old_uploads
from app.logging_config import configure_logging
from app.mfds_catalog import MfdsFoodCatalog
from app.recipe_ai import RecipeAIParser
from app.telegram import BotContext, create_router


async def run() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN이 비어 있습니다. .env 설정을 확인하세요.")
    settings.ensure_directories()
    removed_uploads = cleanup_old_uploads(settings.uploads_dir)
    if removed_uploads:
        logging.getLogger(__name__).info("Removed %s stale upload(s)", removed_uploads)

    database = Database(settings.database_url)
    await database.create_schema()
    recognizer = (
        NutritionRecognizer(settings.openai_api_key, settings.openai_model)
        if settings.openai_api_key
        else None
    )
    context = BotContext(
        settings=settings,
        sessions=database.sessions,
        catalog=OpenFoodFactsCatalog(settings.openfoodfacts_user_agent),
        food_catalog=(
            MfdsFoodCatalog(
                settings.mfds_api_key,
                timeout_seconds=settings.mfds_api_timeout_seconds,
            )
            if settings.mfds_api_key
            else None
        ),
        recognizer=recognizer,
        recipe_parser=(
            RecipeAIParser(
                settings.openai_api_key,
                settings.openai_recipe_model,
                max_output_tokens=settings.recipe_ai_max_output_tokens,
                max_ingredients=settings.recipe_max_ingredients,
            )
            if settings.openai_api_key
            else None
        ),
    )

    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(create_router(context))
    await bot.set_my_commands(
        [
            BotCommand(command="ping", description="봇·DB 상태 확인"),
            BotCommand(command="today", description="오늘 칼로리·탄단지"),
            BotCommand(command="recent", description="최근 기록"),
            BotCommand(command="undo", description="마지막 기록 취소"),
            BotCommand(command="goal", description="일일 목표 설정"),
            BotCommand(command="food", description="일반 음식 검색"),
            BotCommand(command="recipe", description="재료로 레시피 계산"),
            BotCommand(command="manual", description="직접 기록"),
            BotCommand(command="cancel", description="진행 중인 입력 취소"),
            BotCommand(command="help", description="사용법"),
        ]
    )

    health_server = uvicorn.Server(
        uvicorn.Config(
            create_health_app(
                database.sessions,
                owner_telegram_id=settings.owner_telegram_id,
                app_timezone=settings.app_timezone,
            ),
            host=settings.health_host,
            port=settings.health_port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    health_task = asyncio.create_task(health_server.serve(), name="health-server")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        health_server.should_exit = True
        await health_task
        await bot.session.close()
        await database.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    logging.getLogger(__name__).info(
        "Persistent logging enabled: file=%s max_bytes=%s backup_count=%s",
        settings.log_file,
        settings.log_max_bytes,
        settings.log_backup_count,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
