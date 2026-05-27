import sqlite3, sys
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect("data/polymarket.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Latest per source (deduplicated)
cur.execute("""
    SELECT source, MAX(snapshot_time) as snap, temp_forecast_max, temp_forecast_min
    FROM weather_snapshots
    WHERE city = 'Tokyo' AND forecast_date = '2026-05-26'
    GROUP BY source
    ORDER BY source
""")
rows = cur.fetchall()
print("=== Tokyo / 2026-05-26 — latest per model ===")
for r in rows:
    print(f"  {r['source']:<30}  max={r['temp_forecast_max']:.2f}C  min={r['temp_forecast_min']:.2f}C  snap={r['snap']}")

# Summary
cur.execute("""
    SELECT COUNT(DISTINCT source) as src_cnt, COUNT(*) as total,
           MIN(temp_forecast_max) as mn, MAX(temp_forecast_max) as mx,
           AVG(temp_forecast_max) as avg,
           MIN(snapshot_time) as first_snap, MAX(snapshot_time) as last_snap
    FROM weather_snapshots
    WHERE city = 'Tokyo' AND forecast_date = '2026-05-26'
""")
r = cur.fetchone()
print(f"\n=== Summary ===")
print(f"  Sources: {r['src_cnt']}  |  Total rows: {r['total']}")
print(f"  temp_forecast_max: min={r['mn']:.2f}C  max={r['mx']:.2f}C  avg={r['avg']:.2f}C")
print(f"  Snapshots: {r['first_snap']}  ->  {r['last_snap']}")

conn.close()
