import sqlite3
conn = sqlite3.connect("data/polymarket.db")
cur = conn.cursor()
cur.execute("UPDATE market_groups SET is_active=1 WHERE is_active IS NULL AND resolution_date >= date('now')")
print(f"Fixed {cur.rowcount} rows (NULL -> active=1)")
conn.commit()
conn.close()
