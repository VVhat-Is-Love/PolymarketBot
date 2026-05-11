from datetime import date, datetime
from sqlalchemy.orm import Session
from sqlalchemy import select

from src.db.models import MarketGroup, Market, MarketSnapshot, WeatherSnapshot, PaperTrade, ChecklistEvaluation


class MarketGroupRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, group: MarketGroup) -> MarketGroup:
        existing = self.session.get(MarketGroup, group.group_id)
        if existing:
            existing.event_volume = group.event_volume
            existing.unit = group.unit or existing.unit
            self.session.commit()
            return existing
        self.session.add(group)
        self.session.commit()
        self.session.refresh(group)
        return group

    def get_all(self) -> list[MarketGroup]:
        return list(self.session.execute(select(MarketGroup)).scalars().all())

    def deactivate_resolved(self) -> int:
        """Mark groups whose resolution_date has passed as inactive, cascade to markets."""
        today = date.today()
        expired = list(
            self.session.execute(
                select(MarketGroup).where(
                    MarketGroup.resolution_date < today,
                    MarketGroup.is_active == True,  # noqa: E712
                )
            )
            .scalars()
            .all()
        )
        for group in expired:
            group.is_active = False
            for market in group.markets:
                market.is_active = False
        if expired:
            self.session.commit()
        return len(expired)


class MarketRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, market: Market) -> Market:
        existing = self.session.get(Market, market.market_id)
        if existing:
            existing.volume = market.volume
            existing.is_active = market.is_active
            existing.condition_id = market.condition_id or existing.condition_id
            existing.token_id_yes = market.token_id_yes or existing.token_id_yes
            existing.token_id_no = market.token_id_no or existing.token_id_no
            # Backfill bins if previously unparseable (e.g. "6°C" point format)
            if existing.bin_min is None and market.bin_min is not None:
                existing.bin_min = market.bin_min
            if existing.bin_max is None and market.bin_max is not None:
                existing.bin_max = market.bin_max
            existing.updated_at = datetime.utcnow()
            self.session.commit()
            return existing
        self.session.add(market)
        self.session.commit()
        self.session.refresh(market)
        return market

    def get_active(self) -> list[Market]:
        return list(
            self.session.execute(
                select(Market).where(Market.is_active == True)  # noqa: E712
            )
            .scalars()
            .all()
        )


class MarketSnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def insert(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        self.session.add(snapshot)
        try:
            self.session.commit()
            self.session.refresh(snapshot)
        except Exception:
            self.session.rollback()
        return snapshot


class WeatherSnapshotRepository:
    def __init__(self, session: Session):
        self.session = session

    def insert(self, snapshot: WeatherSnapshot) -> WeatherSnapshot:
        self.session.add(snapshot)
        try:
            self.session.commit()
            self.session.refresh(snapshot)
        except Exception:
            self.session.rollback()
        return snapshot
