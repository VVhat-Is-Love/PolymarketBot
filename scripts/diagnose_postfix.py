"""Post-fix verification."""
import sqlite3
db = sqlite3.connect("data/polymarket.db")

print("=== POST-FIX Q1: LA unit in DB ===")
rows = db.execute("SELECT city, unit, COUNT(*) as n FROM market_groups WHERE city='Los Angeles' GROUP BY unit").fetchall()
print("LA units:", rows)

print()
print("=== POST-FIX Q1: LA bins (should be Celsius) ===")
rows = db.execute("""
    SELECT mg.city, mg.unit, m.bin_label, m.bin_min, m.bin_max
    FROM markets m JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Los Angeles' AND mg.metric='high_temp'
      AND mg.resolution_date='2026-05-04' AND m.bin_min IS NOT NULL
    GROUP BY m.bin_label ORDER BY m.bin_min LIMIT 12
""").fetchall()
for r in rows:
    print(f"  {r[0]:12s} unit={r[1]!r} bin={r[2]!r:22s} min={r[3]}..{r[4]}")

print()
print("=== POST-FIX Q1: Other US cities bins (should be Fahrenheit) ===")
rows = db.execute("""
    SELECT mg.city, mg.unit, m.bin_label, m.bin_min, m.bin_max
    FROM markets m JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city IN ('New York','Miami','Chicago','Austin')
      AND mg.metric='high_temp' AND mg.resolution_date='2026-05-04'
      AND m.bin_min IS NOT NULL
    GROUP BY mg.city, m.bin_label ORDER BY mg.city, m.bin_min LIMIT 20
""").fetchall()
for r in rows:
    print(f"  {r[0]:12s} unit={r[1]!r} bin={r[2]!r:22s} min={r[3]}..{r[4]}")

print()
print("=== POST-FIX Q2: Tokyo prices — early-skip threshold ===")
rows = db.execute("""
    SELECT m.bin_label, m.bin_min, ms.price_yes
    FROM market_snapshots ms JOIN markets m ON ms.market_id = m.market_id
    JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Tokyo' AND mg.metric='high_temp' AND mg.resolution_date='2026-05-04'
      AND ms.snapshot_time=(SELECT MAX(s2.snapshot_time) FROM market_snapshots s2 WHERE s2.market_id=ms.market_id)
    ORDER BY m.bin_min
""").fetchall()
if rows:
    prices = [r[2] for r in rows if r[2] is not None]
    total = sum(prices)
    max_p = max(prices)
    print(f"  Tokyo: {len(rows)} bins, sum={total:.3f}, max_price={max_p:.3f}")
    print(f"  early-skip triggered? {max_p > 0.95} (threshold=0.95)")
else:
    print("  No Tokyo snapshot data for 2026-05-04")

print()
print("=== POST-FIX: LA target temp matching ===")
print("  If consensus=21.0 C, target=21.0 C (unit=C, no conversion)")
rows = db.execute("""
    SELECT DISTINCT m.bin_label, m.bin_min, m.bin_max
    FROM markets m JOIN market_groups mg ON m.group_id = mg.group_id
    WHERE mg.city='Los Angeles' AND mg.is_active=1 AND m.bin_min IS NOT NULL
    ORDER BY m.bin_min
""").fetchall()
print(f"  LA bins range: {rows[0][1]}..{rows[-1][2]} (Celsius)")
target = 21.0
for r in rows:
    hi = r[2] if r[2] is not None else r[1] + 1
    if r[1] <= target <= hi:
        print(f"  Match found: {r[0]!r} [{r[1]}, {hi}] for target={target}")
        break

db.close()
