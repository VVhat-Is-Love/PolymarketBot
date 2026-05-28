import uuid
from datetime import date, datetime
from sqlalchemy import (
    String, Text, Float, Boolean, Date, DateTime,
    Integer, ForeignKey, UniqueConstraint, Index, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BotState(Base):
    """Key-value store for persistent bot state (emergency stop, etc.)."""
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class MarketGroup(Base):
    __tablename__ = "market_groups"

    group_id: Mapped[str] = mapped_column(String, primary_key=True)
    city: Mapped[str] = mapped_column(String, index=True)
    metric: Mapped[str] = mapped_column(String)  # 'high_temp' | 'low_temp' | 'precipitation'
    resolution_date: Mapped[date] = mapped_column(Date)
    weather_station: Mapped[str | None] = mapped_column(String, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(1), nullable=True)  # 'F' | 'C'
    event_volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    markets: Mapped[list["Market"]] = relationship("Market", back_populates="group")


class Market(Base):
    __tablename__ = "markets"

    market_id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_groups.group_id"), nullable=True
    )
    question: Mapped[str] = mapped_column(Text)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    bin_label: Mapped[str | None] = mapped_column(String, nullable=True)
    bin_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    bin_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_id_yes: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    token_id_no: Mapped[str | None] = mapped_column(String, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    group: Mapped["MarketGroup | None"] = relationship("MarketGroup", back_populates="markets")
    snapshots: Mapped[list["MarketSnapshot"]] = relationship(
        "MarketSnapshot", back_populates="market"
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("market_id", "snapshot_time", name="uq_market_snapshot"),
        Index("ix_snapshot_market_time", "market_id", "snapshot_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.market_id"))

    best_bid: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_bid_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_ask_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread: Mapped[float | None] = mapped_column(Float, nullable=True)

    price_yes: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_no: Mapped[float | None] = mapped_column(Float, nullable=True)

    source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # 'gamma' | 'clob'

    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )

    market: Mapped["Market"] = relationship("Market", back_populates="snapshots")


class WeatherSnapshot(Base):
    __tablename__ = "weather_snapshots"
    __table_args__ = (
        UniqueConstraint("city", "source", "forecast_date", "snapshot_time", name="uq_weather_snapshot"),
        Index("ix_weather_city_time", "city", "snapshot_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    city: Mapped[str] = mapped_column(String)
    weather_station: Mapped[str | None] = mapped_column(String, nullable=True)

    temp_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_forecast_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_forecast_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity: Mapped[float | None] = mapped_column(Float, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)

    forecast_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # None for current-weather snapshots
    source: Mapped[str] = mapped_column(String)
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("market_groups.group_id"), index=True)

    decision_time: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    consensus_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    sum_of_prices: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_edge: Mapped[float | None] = mapped_column(Float, nullable=True)

    basket_json: Mapped[str] = mapped_column(Text)
    virtual_stake_usd: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String, server_default="open", index=True)
    # 'open' | 'resolved_win' | 'resolved_partial' | 'resolved_loss'
    # 'resolved_basket_miss' | 'cancelled'
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    winning_market_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Resolution provenance
    resolved_via: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 'polymarket_api' | 'weather_api' | 'manual'
    basket_miss: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Entry-time snapshots (denormalised for post-hoc analysis)
    bin_volumes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    consensus_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    market_type: Mapped[str | None] = mapped_column(String, nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'basket_wide' | 'basket_narrow' | 'tail_no' | 'manual'
    strategy_name: Mapped[str] = mapped_column(
        String(16), server_default="basket_wide", default="basket_wide"
    )


class RejectedMarket(Base):
    __tablename__ = "rejected_markets"
    __table_args__ = (
        Index("ix_rejected_group", "group_id"),
        Index("ix_rejected_at", "rejected_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(String)
    group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DailySummary(Base):
    __tablename__ = "daily_summaries"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    trades_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    basket_misses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    open_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hwm: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LiveTrade(Base):
    """Individual live order for one bin inside a basket."""
    __tablename__ = "live_trades"
    __table_args__ = (
        Index("ix_live_trades_group", "group_id"),
        Index("ix_live_trades_status", "status"),
        Index("ix_live_trades_placed_at", "placed_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    group_id: Mapped[str | None] = mapped_column(
        ForeignKey("market_groups.group_id"), nullable=True
    )
    market_id: Mapped[str] = mapped_column(String)
    bin_label: Mapped[str] = mapped_column(String)        # e.g. '80-81°F'
    side: Mapped[str] = mapped_column(String(4), server_default="buy")
    order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    stake_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 'pending' | 'open' | 'filled' | 'cancelled' | 'expired'
    status: Mapped[str] = mapped_column(String, server_default="pending")
    # 'center' | 'plus1' | 'minus1' | 'plus2' | 'minus2'
    basket_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # 'basket_wide' | 'basket_narrow' | 'tail_no' | 'manual'
    strategy_name: Mapped[str] = mapped_column(
        String(16), server_default="basket_wide", default="basket_wide"
    )
    placed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    actual_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    city: Mapped[str | None] = mapped_column(String, nullable=True)

    # Polymarket matching keys (G2-1: PnL reconciliation via Data API)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    token_id: Mapped[str | None] = mapped_column(String, nullable=True)
    basket_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    reconciled_with_polymarket: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )


class ChecklistEvaluation(Base):
    __tablename__ = "checklist_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("market_groups.group_id"), index=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)

    rules_ok: Mapped[bool] = mapped_column(Boolean)
    consensus_ok: Mapped[bool] = mapped_column(Boolean)
    sum_of_prices_ok: Mapped[bool] = mapped_column(Boolean)
    stability_ok: Mapped[bool] = mapped_column(Boolean)
    edge_ok: Mapped[bool] = mapped_column(Boolean)

    consensus_temp: Mapped[float | None] = mapped_column(Float, nullable=True)
    sum_of_prices: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_edge: Mapped[float | None] = mapped_column(Float, nullable=True)

    passed: Mapped[bool] = mapped_column(Boolean)
    triggered_paper_trade: Mapped[bool] = mapped_column(Boolean, server_default="0")
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
