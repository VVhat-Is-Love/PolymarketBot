"""One-time script: re-parse bin_min/bin_max for markets that have bin_label but bin_min=None."""
from src.db.session import get_session
from src.db.models import Market
from src.market.parsers import parse_bin_range
from sqlalchemy import select

session = get_session()

markets = session.execute(
    select(Market).where(Market.bin_label.isnot(None), Market.bin_min.is_(None))
).scalars().all()

print(f"Markets with bin_label but no bin_min: {len(markets)}")

updated = 0
for m in markets:
    bin_min, bin_max = parse_bin_range(m.bin_label)
    if bin_min is not None or bin_max is not None:
        m.bin_min = bin_min
        m.bin_max = bin_max
        updated += 1

session.commit()
session.close()
print(f"Updated {updated} markets with parsed bin ranges.")
