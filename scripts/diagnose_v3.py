import sqlite3
db = sqlite3.connect("data/polymarket.db")

print("=== NYC and Miami high_temp bins for 2026-05-04 ===")
rows = db.execute("""
    SELECT mg.city, mg.unit, m.bin_label, m.bin_min, m.bin_max
    FROM markets m JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city IN ('New York','Miami') AND mg.metric='high_temp'
      AND mg.resolution_date='2026-05-04' AND m.bin_min IS NOT NULL
    ORDER BY mg.city, m.bin_min LIMIT 25
""").fetchall()
for r in rows:
    print(f"  {r[0]:10s} unit={r[1]!r} bin={r[2]!r:22s} min={r[3]} max={r[4]}")

print()
print("=== LA bin_label samples — are they F or C? ===")
rows = db.execute("""
    SELECT DISTINCT m.bin_label, m.bin_min, m.bin_max
    FROM markets m JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Los Angeles' AND mg.is_active=1 AND m.bin_min IS NOT NULL
    ORDER BY m.bin_min LIMIT 15
""").fetchall()
for r in rows:
    print(f"  bin={r[0]!r:25s} min={r[1]} max={r[2]}")

print()
print("=== Latest prices for Seoul high_temp 2026-05-04 ===")
rows = db.execute("""
    SELECT m.bin_label, m.bin_min, ms.price_yes, ms.best_ask, ms.best_bid
    FROM market_snapshots ms
    JOIN markets m ON ms.market_id = m.market_id
    JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Seoul' AND mg.metric='high_temp' AND mg.resolution_date='2026-05-04'
      AND ms.snapshot_time = (
          SELECT MAX(snapshot_time) FROM market_snapshots ms2 WHERE ms2.market_id = ms.market_id
      )
    ORDER BY m.bin_min
""").fetchall()
total = sum(r[2] for r in rows if r[2] is not None)
for r in rows:
    print(f"  bin={r[0]!r:15s} min={r[1]} price_yes={r[2]} ask={r[3]} bid={r[4]}")
print(f"  >>> SUM = {total:.3f} ({len(rows)} bins)")

db.close()
