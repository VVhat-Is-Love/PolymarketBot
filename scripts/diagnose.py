from src.db.session import get_session
from src.db.models import Market, MarketGroup, MarketSnapshot
from sqlalchemy import select, desc

session = get_session()

# Check London markets (with and without bins)
print("--- London raw markets (first 10) ---")
rows = session.execute(
    select(Market, MarketGroup)
    .join(MarketGroup, Market.group_id == MarketGroup.group_id)
    .where(MarketGroup.city == "London", MarketGroup.is_active == True)
    .limit(10)
).all()
for m, g in rows:
    print(f"  q={m.question[:80]!r}")
    print(f"    bin_label={m.bin_label!r}  bin_min={m.bin_min}  bin_max={m.bin_max}  active={m.is_active}")

# Check the consensus temp that checklist produces for London vs bin range
print("\n--- What consensus_temp looks like for London ---")
from src.db.models import WeatherSnapshot
from sqlalchemy import desc as desc_
import statistics
snaps = session.execute(
    select(WeatherSnapshot)
    .where(
        WeatherSnapshot.city == "London",
        WeatherSnapshot.source.startswith("open_meteo:"),
    )
    .order_by(desc_(WeatherSnapshot.snapshot_time))
    .limit(9)
).scalars().all()
print(f"Open-Meteo snapshots for London: {len(snaps)}")
for s in snaps:
    print(f"  source={s.source}  date={s.forecast_date}  max={s.temp_forecast_max}  min={s.temp_forecast_min}")

# Show what bins Seoul/Tokyo look like (international)
for city in ["Seoul", "Tokyo", "Hong Kong"]:
    rows = session.execute(
        select(Market, MarketGroup)
        .join(MarketGroup, Market.group_id == MarketGroup.group_id)
        .where(MarketGroup.city == city, MarketGroup.is_active == True, Market.bin_min.isnot(None))
        .limit(4)
    ).all()
    if rows:
        unit = rows[0][1].unit
        print(f"\n--- {city} (unit={unit!r}) sample bins ---")
        for m, g in rows:
            print(f"  {m.bin_label!r:20s}  min={m.bin_min}  max={m.bin_max}")
    else:
        count = session.execute(
            select(Market, MarketGroup)
            .join(MarketGroup, Market.group_id == MarketGroup.group_id)
            .where(MarketGroup.city == city, MarketGroup.is_active == True)
        ).all()
        print(f"\n--- {city}: 0 bins, {len(count)} total markets ---")
        for m, g in count[:3]:
            print(f"  q={m.question[:70]!r}  bin_label={m.bin_label!r}")

session.close()
