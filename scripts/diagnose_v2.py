import sqlite3

db = sqlite3.connect("data/polymarket.db")
db.row_factory = sqlite3.Row

print("=" * 70)
print("Q1: US city bins for 2026-05-04")
print("=" * 70)
rows = db.execute("""
    SELECT mg.city, mg.unit, m.bin_label, m.bin_min, m.bin_max
    FROM markets m
    JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city IN ('New York','Los Angeles','Chicago','Austin','Miami')
      AND mg.metric='high_temp'
      AND mg.resolution_date='2026-05-04'
      AND m.bin_min IS NOT NULL
    ORDER BY mg.city, m.bin_min
    LIMIT 50
""").fetchall()
for r in rows:
    print(f"  {r['city']:12s} unit={r['unit']!r:4s} bin={r['bin_label']!r:20s} min={r['bin_min']}  max={r['bin_max']}")

print()
print("=" * 70)
print("Q2: Tokyo high_temp 2026-05-04 — latest prices per bin")
print("=" * 70)
rows = db.execute("""
    SELECT m.bin_label, m.bin_min, m.bin_max,
           ms.price_yes, ms.best_ask, ms.best_bid, ms.snapshot_time
    FROM market_snapshots ms
    JOIN markets m ON ms.market_id = m.market_id
    JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Tokyo' AND mg.metric='high_temp' AND mg.resolution_date='2026-05-04'
      AND ms.snapshot_time = (
          SELECT MAX(snapshot_time) FROM market_snapshots ms2 WHERE ms2.market_id = ms.market_id
      )
    ORDER BY m.bin_min
""").fetchall()
if rows:
    total = sum(r['price_yes'] for r in rows if r['price_yes'] is not None)
    for r in rows:
        print(f"  bin={r['bin_label']!r:20s} min={r['bin_min']}  price_yes={r['price_yes']}  ask={r['best_ask']}  bid={r['best_bid']}")
    print(f"  >>> SUM price_yes = {total:.4f}  ({len(rows)} bins)")
else:
    print("  No snapshot data found for Tokyo 2026-05-04")

print()
print("=" * 70)
print("Q3: Active groups — what paper engine sees")
print("=" * 70)
rows = db.execute("""
    SELECT mg.city, mg.metric, mg.resolution_date, mg.unit, mg.is_active,
           COUNT(m.market_id) AS bins,
           SUM(CASE WHEN m.bin_min IS NULL THEN 1 ELSE 0 END) AS bins_without_range
    FROM market_groups mg
    LEFT JOIN markets m ON m.group_id = mg.group_id
    WHERE mg.is_active = 1
    GROUP BY mg.group_id
    ORDER BY mg.resolution_date DESC, mg.city
""").fetchall()
for r in rows:
    flag = " *** NO BINS ***" if r['bins_without_range'] == r['bins'] else ""
    flag2 = " [past!]" if r['resolution_date'] < '2026-05-03' else ""
    print(f"  {r['city']:12s} {r['metric']:10s} {r['resolution_date']} unit={r['unit']!r:4s} "
          f"bins={r['bins']:3d} missing={r['bins_without_range']}{flag}{flag2}")

db.close()
