"""
Диагностика: смотрим реальную структуру рынков Polymarket.
Запуск: python debug_markets.py
"""
import json
from src.market.clob_client import ClobClient

client = ClobClient()

print("Загружаем первые 200 рынков...")
data = client.get_markets(limit=100)
markets = data.get("data", [])
data2 = client.get_markets(next_cursor=data.get("next_cursor", ""), limit=100)
markets += data2.get("data", [])

print(f"Получено: {len(markets)} рынков\n")

# Структура первого рынка
print("=== СТРУКТУРА ПЕРВОГО РЫНКА (ключи) ===")
if markets:
    print(json.dumps({k: type(v).__name__ for k, v in markets[0].items()}, indent=2))
    print()

# Как выглядят теги?
print("=== ПРИМЕРЫ ТЕГОВ (первые 10 рынков) ===")
for m in markets[:10]:
    tags = m.get("tags", [])
    print(f"  tags={tags!r}  (type={type(tags).__name__})")

print()

# Ищем рынки со словом weather/temperature в любом поле
print("=== РЫНКИ СОДЕРЖАЩИЕ 'weather' или 'temp' (в любом поле) ===")
found = []
for m in markets:
    text = json.dumps(m).lower()
    if "weather" in text or "temperature" in text or "high temp" in text:
        found.append(m)

if found:
    for m in found[:5]:
        print(f"  question: {m.get('question', '')[:80]}")
        print(f"  tags:     {m.get('tags')}")
        print(f"  slug:     {m.get('market_slug', '')[:50]}")
        print(f"  volume:   {m.get('volume')}")
        print()
else:
    print("  Не найдено ни одного — смотрим топ по volume:\n")
    by_vol = sorted(markets, key=lambda x: float(x.get("volume") or 0), reverse=True)
    for m in by_vol[:5]:
        print(f"  question: {m.get('question', '')[:80]}")
        print(f"  tags:     {m.get('tags')}")
        print(f"  volume:   {m.get('volume')}")
        print()

# Как называется поле с объёмом?
print("=== ПОЛЯ С ЧИСЛАМИ (первый рынок) ===")
if markets:
    for k, v in markets[0].items():
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "").isdigit()):
            print(f"  {k} = {v}")
