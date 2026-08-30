from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Seoul")
    kcal_goal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    carb_goal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    protein_goal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    fat_goal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    logs: Mapped[list[IntakeLog]] = relationship(back_populates="user")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(240))
    brand: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="user")
    owner_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    versions: Mapped[list[ProductVersion]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("barcode", "owner_telegram_id", name="uq_product_barcode_owner"),
    )


class ProductVersion(Base):
    __tablename__ = "product_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    basis_amount: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"))
    basis_unit: Mapped[str] = mapped_column(String(24), default="serving")
    package_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    package_unit: Mapped[str | None] = mapped_column(String(24), nullable=True)
    servings_per_package: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    piece_count: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    kcal: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    carbs_g: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    protein_g: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    fat_g: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(32), default="user")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    product: Mapped[Product] = relationship(back_populates="versions")
    logs: Mapped[list[IntakeLog]] = relationship(back_populates="product_version")

    __table_args__ = (Index("ix_product_version_current", "product_id", "is_current"),)


class IntakeLog(Base):
    __tablename__ = "intake_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    product_version_id: Mapped[int] = mapped_column(ForeignKey("product_versions.id"))
    multiplier: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("1"))
    input_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    input_unit: Mapped[str | None] = mapped_column(String(24), nullable=True)
    kcal: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    carbs_g: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    protein_g: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    fat_g: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="logs")
    product_version: Mapped[ProductVersion] = relationship(back_populates="logs")


class RecognitionJob(Base):
    __tablename__ = "recognition_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_telegram_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    barcode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="awaiting_front", index=True)
    front_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
