"""Fix Los Angeles market_groups: unit was 'F' but Polymarket bins are in Celsius."""
import sqlite3

db = sqlite3.connect("data/polymarket.db")
cur = db.execute(
    "UPDATE market_groups SET unit='C' WHERE city='Los Angeles' AND unit='F'"
)
db.commit()
print(f"Updated {cur.rowcount} Los Angeles market_groups: unit F to C")
db.close()
