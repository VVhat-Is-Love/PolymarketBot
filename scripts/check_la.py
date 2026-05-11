import sqlite3
db = sqlite3.connect("data/polymarket.db")
rows = db.execute("SELECT city, unit, COUNT(*) as n FROM market_groups WHERE city='Los Angeles' GROUP BY unit").fetchall()
print("LA units in DB:", rows)
# Fix it directly
cur = db.execute("UPDATE market_groups SET unit='C' WHERE city='Los Angeles'")
db.commit()
print(f"Fixed {cur.rowcount} rows")
db.close()
