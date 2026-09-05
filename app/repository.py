from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import (
    AIUsage,
    IntakeLog,
    MenuSearchCache,
    Product,
    ProductSearchTerm,
    ProductVersion,
    RecipeParseCache,
    RecognitionJob,
    User,
    utc_now,
)
from app.nutrition import MacroTotals
from app.schemas import ProductCandidate
from app.search_tags import (
    build_product_search_terms,
    concept_keys_for_query,
    normalize_search_term,
)

ACTIVE_JOB_STATES = ("awaiting_front", "awaiting_label", "processing", "awaiting_confirm")


@dataclass(frozen=True)
class DailySummary:
    totals: MacroTotals
    goals: MacroTotals | None
    item_count: int


async def ensure_user(session: AsyncSession, telegram_id: int, timezone_name: str) -> User:
    user = await session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, timezone=timezone_name)
        session.add(user)
        await session.flush()
    return user


def _current_product_query(barcode: str, owner_id: int) -> Select[tuple[ProductVersion]]:
    return (
        select(ProductVersion)
        .join(Product)
        .options(joinedload(ProductVersion.product))
        .where(
            Product.barcode == barcode,
            ProductVersion.is_current.is_(True),
            or_(Product.owner_telegram_id.is_(None), Product.owner_telegram_id == owner_id),
        )
        .order_by(desc(Product.owner_telegram_id == owner_id), ProductVersion.created_at.desc())
        .limit(1)
    )


async def find_product_by_barcode(
    session: AsyncSession, barcode: str, owner_id: int
) -> ProductVersion | None:
    return await session.scalar(_current_product_query(barcode, owner_id))


async def find_product_by_external_id(
    session: AsyncSession, external_source: str, external_id: str
) -> ProductVersion | None:
    return await session.scalar(
        select(ProductVersion)
        .join(Product)
        .options(joinedload(ProductVersion.product))
        .where(
            Product.external_source == external_source,
            Product.external_id == external_id,
            ProductVersion.is_current.is_(True),
        )
        .order_by(ProductVersion.created_at.desc())
        .limit(1)
    )


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_catalog_products(
    session: AsyncSession,
    *,
    source: str,
    terms: list[str],
    limit: int = 40,
) -> list[ProductVersion]:
    clean_terms = [term.strip() for term in terms if term.strip()]
    if not clean_terms:
        return []
    name_filters = [
        Product.name.ilike(f"%{_escaped_like(term)}%", escape="\\") for term in clean_terms
    ]
    return list(
        (
            await session.scalars(
                select(ProductVersion)
                .join(Product)
                .options(joinedload(ProductVersion.product))
                .where(
                    Product.source == source,
                    ProductVersion.is_current.is_(True),
                    or_(*name_filters),
                )
                .order_by(ProductVersion.created_at.desc())
                .limit(limit)
            )
        ).all()
    )


def _saved_tag_filter(query: str):
    normalized_query = normalize_search_term(query)
    if not normalized_query:
        return None
    tag_filters = [
        ProductSearchTerm.normalized_term.ilike(
            f"%{_escaped_like(normalized_query)}%",
            escape="\\",
        )
    ]
    concepts = concept_keys_for_query(query)
    if concepts:
        tag_filters.append(ProductSearchTerm.concept_key.in_(concepts))
    return (
        select(ProductSearchTerm.id)
        .where(
            ProductSearchTerm.product_id == Product.id,
            or_(*tag_filters),
        )
        .exists()
    )


async def search_saved_products(
    session: AsyncSession,
    *,
    owner_id: int,
    query: str,
    limit: int = 40,
) -> list[ProductVersion]:
    clean_query = " ".join(query.split()).strip()
    if not clean_query:
        return []
    full_pattern = f"%{_escaped_like(clean_query)}%"
    token_filters = []
    for term in dict.fromkeys(clean_query.split()):
        pattern = f"%{_escaped_like(term)}%"
        alternatives = [
            Product.name.ilike(pattern, escape="\\"),
            Product.brand.ilike(pattern, escape="\\"),
        ]
        tag_filter = _saved_tag_filter(term)
        if tag_filter is not None:
            alternatives.append(tag_filter)
        token_filters.append(or_(*alternatives))
    filters = [
        Product.name.ilike(full_pattern, escape="\\"),
        Product.brand.ilike(full_pattern, escape="\\"),
        and_(*token_filters),
    ]
    if len(clean_query.split()) == 1:
        full_tag_filter = _saved_tag_filter(clean_query)
        if full_tag_filter is not None:
            filters.append(full_tag_filter)
    if clean_query.isdigit():
        filters.append(Product.barcode == clean_query)
    return list(
        (
            await session.scalars(
                select(ProductVersion)
                .join(Product)
                .options(joinedload(ProductVersion.product).selectinload(Product.search_terms))
                .where(
                    ProductVersion.is_current.is_(True),
                    or_(Product.owner_telegram_id.is_(None), Product.owner_telegram_id == owner_id),
                    or_(*filters),
                )
                .order_by(
                    desc(Product.owner_telegram_id == owner_id),
                    ProductVersion.created_at.desc(),
                )
                .limit(limit)
            )
        ).all()
    )


async def search_recipe_products(
    session: AsyncSession,
    *,
    owner_id: int,
    terms: list[str],
    limit: int = 40,
) -> list[ProductVersion]:
    clean_terms = [term.strip() for term in terms if term.strip()]
    if not clean_terms:
        return []
    name_filters = [
        Product.name.ilike(f"%{_escaped_like(term)}%", escape="\\") for term in clean_terms
    ]
    return list(
        (
            await session.scalars(
                select(ProductVersion)
                .join(Product)
                .options(joinedload(ProductVersion.product))
                .where(
                    ProductVersion.is_current.is_(True),
                    Product.source != "recipe",
                    or_(Product.owner_telegram_id.is_(None), Product.owner_telegram_id == owner_id),
                    or_(*name_filters),
                )
                .order_by(
                    desc(Product.owner_telegram_id == owner_id),
                    ProductVersion.created_at.desc(),
                )
                .limit(limit)
            )
        ).all()
    )


async def get_product_version(
    session: AsyncSession, version_id: int, owner_id: int
) -> ProductVersion | None:
    return await session.scalar(
        select(ProductVersion)
        .join(Product)
        .options(joinedload(ProductVersion.product))
        .where(
            ProductVersion.id == version_id,
            or_(Product.owner_telegram_id.is_(None), Product.owner_telegram_id == owner_id),
        )
    )


async def _ensure_product_search_terms(
    session: AsyncSession,
    *,
    product: Product,
    candidate: ProductCandidate,
) -> None:
    specs = build_product_search_terms(
        name=candidate.name,
        brand=candidate.brand,
        raw_data=candidate.raw_data,
        search_concepts=candidate.search_concepts,
        search_terms_ko=candidate.search_terms_ko,
        search_terms_ja=candidate.search_terms_ja,
        product_source=candidate.source,
    )
    if not specs:
        return
    existing_terms = set(
        (
            await session.scalars(
                select(ProductSearchTerm.normalized_term).where(
                    ProductSearchTerm.product_id == product.id
                )
            )
        ).all()
    )
    session.add_all(
        ProductSearchTerm(
            product_id=product.id,
            term=spec.term,
            normalized_term=spec.normalized_term,
            locale=spec.locale,
            kind=spec.kind,
            source=spec.source,
            concept_key=spec.concept_key,
            confidence=spec.confidence,
        )
        for spec in specs
        if spec.normalized_term not in existing_terms
    )


async def create_product_version(
    session: AsyncSession,
    candidate: ProductCandidate,
    *,
    owner_id: int | None,
) -> ProductVersion:
    product: Product | None = None
    if candidate.barcode:
        product = await session.scalar(
            select(Product).where(
                Product.barcode == candidate.barcode,
                Product.owner_telegram_id == owner_id,
            )
        )
    elif candidate.external_source and candidate.external_id:
        product = await session.scalar(
            select(Product).where(
                Product.external_source == candidate.external_source,
                Product.external_id == candidate.external_id,
            )
        )

    if product is None:
        product = Product(
            barcode=candidate.barcode,
            external_source=candidate.external_source,
            external_id=candidate.external_id,
            name=candidate.name,
            brand=candidate.brand,
            source=candidate.source,
            owner_telegram_id=owner_id,
        )
        session.add(product)
        await session.flush()
    else:
        product.name = candidate.name
        product.brand = candidate.brand
        await session.execute(
            update(ProductVersion)
            .where(ProductVersion.product_id == product.id)
            .values(is_current=False)
        )

    version = ProductVersion(
        product=product,
        basis_amount=candidate.basis_amount,
        basis_unit=candidate.basis_unit,
        package_amount=candidate.package_amount,
        package_unit=candidate.package_unit,
        servings_per_package=candidate.servings_per_package,
        piece_count=candidate.piece_count,
        label_market=candidate.label_market,
        label_language=candidate.label_language,
        basis_text=candidate.basis_text,
        basis_metric_amount=candidate.basis_metric_amount,
        basis_metric_unit=candidate.basis_metric_unit,
        basis_count_amount=candidate.basis_count_amount,
        basis_count_unit=candidate.basis_count_unit,
        kcal=candidate.kcal,
        carbs_g=candidate.carbs_g,
        protein_g=candidate.protein_g,
        fat_g=candidate.fat_g,
        sodium_mg=candidate.sodium_mg,
        salt_equivalent_g=candidate.salt_equivalent_g,
        sodium_derived=candidate.sodium_derived,
        salt_equivalent_derived=candidate.salt_equivalent_derived,
        estimated_values=candidate.estimated_values,
        source=candidate.source,
        verified=candidate.verified,
        raw_data=candidate.raw_data,
    )
    session.add(version)
    await _ensure_product_search_terms(session, product=product, candidate=candidate)
    await session.flush()
    return version


async def get_or_create_catalog_product(
    session: AsyncSession, candidate: ProductCandidate
) -> ProductVersion:
    if not candidate.external_source or not candidate.external_id:
        raise ValueError("공개 카탈로그 항목에는 외부 출처와 ID가 필요합니다.")
    existing = await find_product_by_external_id(
        session, candidate.external_source, candidate.external_id
    )
    if existing is not None:
        return existing
    return await create_product_version(session, candidate, owner_id=None)


async def add_intake(
    session: AsyncSession,
    *,
    user_id: int,
    version: ProductVersion,
    multiplier: Decimal,
    input_amount: Decimal | None = None,
    input_unit: str | None = None,
    consumed_at: datetime | None = None,
) -> IntakeLog:
    totals = MacroTotals(
        kcal=version.kcal,
        carbs_g=version.carbs_g,
        protein_g=version.protein_g,
        fat_g=version.fat_g,
    ).scaled(multiplier)
    log = IntakeLog(
        user_telegram_id=user_id,
        product_version_id=version.id,
        multiplier=multiplier,
        input_amount=input_amount,
        input_unit=input_unit,
        kcal=totals.kcal,
        carbs_g=totals.carbs_g,
        protein_g=totals.protein_g,
        fat_g=totals.fat_g,
        consumed_at=consumed_at or utc_now(),
    )
    session.add(log)
    await session.flush()
    return log


async def get_last_portion(
    session: AsyncSession, user_id: int, version_id: int
) -> IntakeLog | None:
    return await session.scalar(
        select(IntakeLog)
        .where(
            IntakeLog.user_telegram_id == user_id,
            IntakeLog.product_version_id == version_id,
            IntakeLog.voided_at.is_(None),
            IntakeLog.input_amount.is_not(None),
            IntakeLog.input_unit.is_not(None),
        )
        .order_by(IntakeLog.consumed_at.desc())
        .limit(1)
    )


async def get_daily_summary(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> DailySummary:
    tz = ZoneInfo(user.timezone)
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = local_start.astimezone(UTC)
    end_utc = (local_start + timedelta(days=1)).astimezone(UTC)
    logs = (
        await session.scalars(
            select(IntakeLog).where(
                IntakeLog.user_telegram_id == user.telegram_id,
                IntakeLog.voided_at.is_(None),
                IntakeLog.consumed_at >= start_utc,
                IntakeLog.consumed_at < end_utc,
            )
        )
    ).all()
    totals = MacroTotals(
        kcal=sum((row.kcal for row in logs), Decimal("0")),
        carbs_g=sum((row.carbs_g for row in logs), Decimal("0")),
        protein_g=sum((row.protein_g for row in logs), Decimal("0")),
        fat_g=sum((row.fat_g for row in logs), Decimal("0")),
    )
    goals = None
    if all(
        goal is not None
        for goal in (user.kcal_goal, user.carb_goal, user.protein_goal, user.fat_goal)
    ):
        goals = MacroTotals(
            kcal=user.kcal_goal or Decimal("0"),
            carbs_g=user.carb_goal or Decimal("0"),
            protein_g=user.protein_goal or Decimal("0"),
            fat_g=user.fat_goal or Decimal("0"),
        )
    return DailySummary(totals=totals, goals=goals, item_count=len(logs))


async def recent_logs(session: AsyncSession, user_id: int, limit: int = 8) -> list[IntakeLog]:
    return list(
        (
            await session.scalars(
                select(IntakeLog)
                .options(joinedload(IntakeLog.product_version).joinedload(ProductVersion.product))
                .where(
                    IntakeLog.user_telegram_id == user_id,
                    IntakeLog.voided_at.is_(None),
                )
                .order_by(IntakeLog.consumed_at.desc())
                .limit(limit)
            )
        ).all()
    )


async def undo_last_intake(session: AsyncSession, user_id: int) -> IntakeLog | None:
    log = await session.scalar(
        select(IntakeLog)
        .options(joinedload(IntakeLog.product_version).joinedload(ProductVersion.product))
        .where(IntakeLog.user_telegram_id == user_id, IntakeLog.voided_at.is_(None))
        .order_by(IntakeLog.consumed_at.desc())
        .limit(1)
    )
    if log is not None:
        log.voided_at = utc_now()
    return log


async def get_active_job(session: AsyncSession, user_id: int) -> RecognitionJob | None:
    return await session.scalar(
        select(RecognitionJob)
        .where(
            RecognitionJob.user_telegram_id == user_id,
            RecognitionJob.state.in_(ACTIVE_JOB_STATES),
        )
        .order_by(RecognitionJob.created_at.desc())
        .limit(1)
    )


async def start_job(
    session: AsyncSession, user_id: int, barcode: str | None = None
) -> RecognitionJob:
    old_jobs = (
        await session.scalars(
            select(RecognitionJob).where(
                RecognitionJob.user_telegram_id == user_id,
                RecognitionJob.state.in_(ACTIVE_JOB_STATES),
            )
        )
    ).all()
    for job in old_jobs:
        job.state = "canceled"
    job = RecognitionJob(
        user_telegram_id=user_id,
        barcode=barcode,
        state="awaiting_front",
    )
    session.add(job)
    await session.flush()
    return job


async def set_goals(
    session: AsyncSession,
    user: User,
    *,
    kcal: Decimal,
    carbs: Decimal,
    protein: Decimal,
    fat: Decimal,
) -> None:
    user.kcal_goal = kcal
    user.carb_goal = carbs
    user.protein_goal = protein
    user.fat_goal = fat


async def get_recipe_parse_cache(
    session: AsyncSession,
    *,
    user_id: int,
    input_hash: str,
    parser_version: str,
) -> RecipeParseCache | None:
    return await session.scalar(
        select(RecipeParseCache).where(
            RecipeParseCache.user_telegram_id == user_id,
            RecipeParseCache.input_hash == input_hash,
            RecipeParseCache.parser_version == parser_version,
        )
    )


async def save_recipe_parse_cache(
    session: AsyncSession,
    *,
    user_id: int,
    input_hash: str,
    parser_version: str,
    result_json: dict,
    used_ai: bool,
) -> RecipeParseCache:
    cached = await get_recipe_parse_cache(
        session,
        user_id=user_id,
        input_hash=input_hash,
        parser_version=parser_version,
    )
    if cached is None:
        cached = RecipeParseCache(
            user_telegram_id=user_id,
            input_hash=input_hash,
            parser_version=parser_version,
            result_json=result_json,
            used_ai=used_ai,
        )
        session.add(cached)
    else:
        cached.result_json = result_json
        cached.used_ai = used_ai
    await session.flush()
    return cached


def _usage_window(timezone_name: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    return day_start.astimezone(UTC), month_start.astimezone(UTC)


async def reserve_recipe_ai_usage(
    session: AsyncSession,
    *,
    user_id: int,
    input_hash: str,
    timezone_name: str,
    daily_limit: int,
    monthly_limit: int,
) -> tuple[AIUsage | None, str | None]:
    return await reserve_ai_usage(
        session,
        user_id=user_id,
        feature="recipe_parse",
        input_hash=input_hash,
        timezone_name=timezone_name,
        daily_limit=daily_limit,
        monthly_limit=monthly_limit,
        feature_label="AI 레시피 분석",
    )


async def reserve_ai_usage(
    session: AsyncSession,
    *,
    user_id: int,
    feature: str,
    input_hash: str,
    timezone_name: str,
    daily_limit: int,
    monthly_limit: int,
    feature_label: str,
    global_daily_limit: int | None = None,
    global_monthly_limit: int | None = None,
) -> tuple[AIUsage | None, str | None]:
    day_start, month_start = _usage_window(timezone_name)
    base = (
        AIUsage.user_telegram_id == user_id,
        AIUsage.feature == feature,
    )
    daily_count = await session.scalar(
        select(func.count(AIUsage.id)).where(*base, AIUsage.created_at >= day_start)
    )
    if daily_limit <= 0 or (daily_count or 0) >= daily_limit:
        return None, f"오늘의 {feature_label} 한도에 도달했습니다. 내일 다시 시도해 주세요."
    monthly_count = await session.scalar(
        select(func.count(AIUsage.id)).where(*base, AIUsage.created_at >= month_start)
    )
    if monthly_limit <= 0 or (monthly_count or 0) >= monthly_limit:
        return None, f"이번 달의 {feature_label} 한도에 도달했습니다."
    feature_filter = (AIUsage.feature == feature,)
    if global_daily_limit is not None:
        global_daily_count = await session.scalar(
            select(func.count(AIUsage.id)).where(
                *feature_filter,
                AIUsage.created_at >= day_start,
            )
        )
        if global_daily_limit <= 0 or (global_daily_count or 0) >= global_daily_limit:
            return None, f"오늘의 서비스 전체 {feature_label} 한도에 도달했습니다."
    if global_monthly_limit is not None:
        global_monthly_count = await session.scalar(
            select(func.count(AIUsage.id)).where(
                *feature_filter,
                AIUsage.created_at >= month_start,
            )
        )
        if global_monthly_limit <= 0 or (global_monthly_count or 0) >= global_monthly_limit:
            return None, f"이번 달의 서비스 전체 {feature_label} 한도에 도달했습니다."
    usage = AIUsage(
        user_telegram_id=user_id,
        feature=feature[:32],
        input_hash=input_hash,
        status="started",
    )
    session.add(usage)
    await session.flush()
    return usage, None


async def get_menu_search_cache(
    session: AsyncSession,
    *,
    input_hash: str,
    search_version: str,
    max_age_days: int,
) -> MenuSearchCache | None:
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    return await session.scalar(
        select(MenuSearchCache).where(
            MenuSearchCache.input_hash == input_hash,
            MenuSearchCache.search_version == search_version,
            MenuSearchCache.created_at >= cutoff,
        )
    )


async def save_menu_search_cache(
    session: AsyncSession,
    *,
    input_hash: str,
    search_version: str,
    query: str,
    result_json: dict,
    searched_urls: list[str],
) -> MenuSearchCache:
    cached = await session.scalar(
        select(MenuSearchCache).where(MenuSearchCache.input_hash == input_hash)
    )
    if cached is None:
        try:
            async with session.begin_nested():
                cached = MenuSearchCache(
                    input_hash=input_hash,
                    search_version=search_version,
                    query=query[:160],
                    result_json=result_json,
                    searched_urls=searched_urls[:20],
                )
                session.add(cached)
                await session.flush()
        except IntegrityError:
            cached = await session.scalar(
                select(MenuSearchCache).where(MenuSearchCache.input_hash == input_hash)
            )
            if cached is None:
                raise
    cached.search_version = search_version
    cached.query = query[:160]
    cached.result_json = result_json
    cached.searched_urls = searched_urls[:20]
    cached.created_at = utc_now()
    await session.flush()
    return cached


async def finish_ai_usage(
    session: AsyncSession,
    usage_id: int,
    *,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    error: str | None = None,
) -> None:
    usage = await session.get(AIUsage, usage_id)
    if usage is None:
        return
    usage.status = status[:24]
    usage.input_tokens = max(0, input_tokens)
    usage.output_tokens = max(0, output_tokens)
    usage.total_tokens = max(0, total_tokens)
    usage.error = error[:500] if error else None
    await session.flush()
