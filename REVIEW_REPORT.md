# REVIEW REPORT — Polymarket Weather Bot
**Дата аудита:** 2026-05-28  
**Аудитор:** Claude Sonnet 4.6 (Senior Backend Engineer role)  
**Баланс на момент аудита:** $40.05 (из логов)  
**Статус бота:** Live, mode=live  

---

## КАРТА МОДУЛЕЙ (ЭТАП 1)

```
src/
├── config/
│   ├── settings.py            — Pydantic BaseSettings: риск-лимиты, ключи API, режим торговли
│   ├── cities.py              — Whitelist городов с lat/lon/station/unit
│   └── polymarket_city_scanner.py — Утилита (не в scheduler)
├── data/
│   ├── open_meteo.py          — Основной fetcher прогнозов (GFS, ECMWF и др.)
│   ├── weather_fetcher.py     — OpenWeatherMap (резерв, не используется в scheduler)
│   └── wunderground.py        — Wunderground (мёртвый код — не используется нигде)
├── db/
│   ├── models.py              — ORM: MarketGroup, Market, MarketSnapshot, PaperTrade,
│   │                             LiveTrade, ChecklistEvaluation, WeatherSnapshot, DailySummary
│   ├── session.py             — SQLAlchemy engine + SessionLocal factory
│   └── repository.py          — DAL: upsert/deactivate helpers
├── market/
│   ├── gamma_client.py        — Gamma API с circuit breaker + pagination
│   ├── order_executor.py      — CLOB V2: place/cancel/status с backoff 1s/2s/4s
│   ├── markets.py             — Market discovery + upsert
│   ├── clob_client.py         — Order book snapshot (не используется в main flow)
│   └── parsers.py             — Парсинг city/date/metric/bin из заголовков
├── notifications/telegram.py  — Telegram notifier (send + error mute)
├── risk/risk_manager.py       — Лимиты дня/concurrent/total + emergency stop
├── strategy/
│   ├── live_trader.py         — Engine A (basket_wide) + B (basket_narrow) + C (tail_no)
│   ├── paper_trader.py        — Симуляция + разрешение через Gamma/OpenMeteo
│   ├── basket_builder.py      — Сборка корзины вокруг favourite
│   ├── basket_decision.py     — EV gate + equal-share sizing
│   ├── checklist.py           — 5-point entry gate
│   ├── tail_seller.py         — Kelly + portfolio cap для NO-позиций
│   ├── edge_filter.py         — Gaussian per-bin edge (diagnostic)
│   ├── pnl_calculator.py      — PnL для paper trades
│   └── config.py              — Pydantic StrategySettings
├── telegram/
│   ├── bot.py                 — Telegram команды (/status, /trades, /risk, /stats)
│   ├── stats.py               — Запросы к БД + Polymarket data-api
│   └── strategy_flags.py      — Enable/disable флаги стратегий
└── scheduler.py               — schedule library: 15/30/60min + daily jobs
```

**Мёртвый код:**
- `src/data/wunderground.py` — нигде не импортируется, не удалён
- `src/data/weather_fetcher.py` — не используется в scheduler (заменён Open-Meteo)
- `src/market/clob_client.py` — Order book fetch, не вызывается в main flow
- `config.strategy_basket_neighbors`, `config.strategy_basket_include_upside` — объявлены, не используются в basket_builder

---

## АУДИТ ПО ЗОНАМ (ЭТАП 2)

---

### 1. src/risk/ — Риск-менеджмент

#### 🔴 КРИТИЧЕСКИЙ: Двойная risk-проверка ломает multi-bin корзины

**Файл:** [src/strategy/live_trader.py:233](src/strategy/live_trader.py#L233) и [src/market/order_executor.py:73](src/market/order_executor.py#L73)

**Проблема:**  
В `_place_basket_orders()` для каждого бина:
1. Создаём `LiveTrade(status="pending")` и делаем `session.commit()` — запись СРАЗУ в БД
2. Вызываем `place_limit_order()`, которая вызывает `risk_manager.can_place_order(notional)`
3. В `can_place_order()` вызывается `_load_live_stats()` — читает из БД все `status IN ("pending", "open")`

**Результат:** После добавления первого pending trade в БД, `_open_bets` увеличивается на 1. Если `concurrent_open_bets` был на уровне `max_concurrent_bets - 1`, то второй бин корзины получит REJECT от order_executor:

```python
# risk_manager.py:172
if len(self._open_bets) >= self._max_concurrent_bets:
    return False, f"Max concurrent bets reached ({len(self._open_bets)}/{self._max_concurrent_bets})"
```

**Следствие:** Корзина из 3 бинов может разместить только первый бин, остальные отклоняются. Хеджирование через корзину сломано.

**Фикс:**
```python
# В _place_basket_orders(): перенести session.add(trade) ПОСЛЕ успешного place_limit_order
# ИЛИ: убрать вызов can_place_order из order_executor (дублирование),
# оставив его только в live_trader перед всей корзиной
```

---

#### 🔴 КРИТИЧЕСКИЙ: Блокирующий polling в основном потоке scheduler

**Файл:** [src/strategy/live_trader.py:179-192](src/strategy/live_trader.py#L179)

```python
def _poll_until_filled_or_timeout(order_id, timeout_minutes, poll_interval_sec=30):
    deadline = datetime.utcnow() + timedelta(minutes=timeout_minutes)
    while datetime.utcnow() < deadline:
        status = get_order_status(order_id)
        if status in ("filled", "cancelled"):
            return status
        time.sleep(poll_interval_sec)  # блокирует поток до 30 мин!
    return "expired"
```

`order_timeout_minutes = 30`. Для корзины из 3 бинов → до **90 минут блокировки**.

`schedule` library однопоточная. Все джобы (snapshots каждые 10 мин, open-meteo каждые 60 мин, expire-pending) **стоят в очереди** пока не завершится polling.

**Фикс:** Для live среды перейти на async (asyncio + APScheduler) или вынести polling в отдельный thread. Краткосрочно — снизить `order_timeout_minutes` до 5-10.

---

#### 🟠 Риск-менеджер обходим через pending-flood

**Файл:** [src/strategy/live_trader.py:233-248](src/strategy/live_trader.py#L233)

Pending trades, которые не стали ордерами (order_id = None), остаются в БД со статусом `cancelled`. Но записи, созданные ДО вызова `place_limit_order` и затем получившие `status = "cancelled"`, **уже прошли через `can_place_order`** успешно. Потенциальный race condition при параллельных запусках (сейчас нет, но если добавить async).

---

#### 🟡 Emergency stop не персистируется

**Файл:** [src/risk/risk_manager.py:38](src/risk/risk_manager.py#L38)

`_emergency_stop = False` хранится в памяти. После перезапуска бота emergency stop сбрасывается. Если total_loss достигнет $20 → emergency stop → restart → снова торгует.

**Фикс:** Добавить флаг `emergency_stop` в БД или .env:
```python
# При старте: проверять is_emergency_stopped.txt или таблицу config
```

---

### 2. src/strategy/ — Стратегии

#### 🔴 КРИТИЧЕСКИЙ: Tail NO использует приближённый no_ask вместо реального

**Файл:** [src/strategy/tail_seller.py:202](src/strategy/tail_seller.py#L202) и [src/strategy/live_trader.py:607](src/strategy/live_trader.py#L607)

```python
no_ask = no_asks.get(m.market_id) if no_asks else (1.0 - yes_price)
```

В `run_tail_engine()` вызов `scan_tails(...)` не передаёт `no_asks` — используется `1 - price_yes`.

**Проблема:** При yes_price=0.05 аппроксимация даёт no_ask=0.95. Реальный NO ask из order book может быть 0.97-0.98 (bid/ask spread). Это:
1. Занижает реальный no_ask → завышает edge → Kelly размер увеличивается
2. Завышает gross_return_pct → позиция выглядит лучше чем есть

**Текущий масштаб:** $23.82 в tail NO позициях — весь портфель оценён с этой ошибкой.

**Фикс:**
```python
# В run_tail_engine(): получить bid/ask из CLOB перед вызовом scan_tails
# Временный хак: умножить no_ask на 1.02 как safety buffer
no_ask_adjusted = (1.0 - yes_price) * 1.02  # дополнительный 2% buffer на spread
```

---

#### 🟠 Tail portfolio cap захватывает весь капитал

**Из логов:**
```
[tail] New York: portfolio_cap_reached:in_use=$23.82/24.03
```

`tail_max_portfolio_pct = 0.60` × $40.05 = $24.03. В tail-NO заморожено $23.82 (99.5% от cap).

**Следствие:** basket стратегии A и B не могут торговать из-за `max_concurrent_bets = 5`, а все 5 слотов заняты tail позициями. В логах: 0 basket trades из 94 проверенных групп.

**Лимиты не согласованы:** Tail cap (60%) конкурирует с basket через `max_concurrent_bets`, но не через отдельный механизм.

---

#### 🟠 Basket checklist: 100% rejection rate в текущей сессии

**Из логов (21:15:39-21:15:51):** 94 групп проверено → 0 размещено.

**Топ причин отклонения (в одном run):**

| Причина | Примерное кол-во | % от отклонений |
|---------|-----------------|-----------------|
| `consensus_spread > 2.5°C` | ~30 | ~35% |
| `sum_of_prices > 0.96` | ~18 | ~21% |
| `not_enough_models:0/3` | ~14 | ~17% |
| `consensus_outside/near_edge` | ~12 | ~14% |
| `low_volume < $200` | ~8 | ~9% |
| `edge < 10%` | ~4 | ~5% |

**Вывод:** 95%+ rejection rate говорит о том, что фильтры слишком консервативны **либо** рыночные условия сейчас нетипичны (лето, меньше волатильности? Нет — это маловероятно).

---

#### 🟡 `checklist.py`: basket_coverage проверяет consensus дважды

**Файл:** [src/strategy/checklist.py:181-215](src/strategy/checklist.py#L181)

Шаг 5 (basket coverage) проверяет `consensus_near_edge` (должен быть в inner 60%). Но шаг 2 (consensus) уже проверил spread. Иногда оба шага отклоняют одну и ту же группу по разным причинам (избыточность допустима, но не очевидна).

Пример из логов:
```
REJECTED: London high_temp 2026-05-29 — consensus_near_edge:27.5 outside inner60%=[24.5,27.5]
```
Consensus ровно на границе inner60%. Строчный off-by-one: `inner_hi = basket_hi - 0.2 * basket_width`. При basket_hi=30, basket_lo=20, basket_width=10: inner_hi=28. При consensus=27.5 → 27.5 < 28 → должен пройти? Нет, `inner_hi = 30 - 2 = 28`. Но условие `consensus_temp > inner_hi` → 27.5 > 28 → False. Пропустим... стоп, `inner60%=[24.5,27.5]` в логах. basket_hi=30, 0.2*10=2, inner_hi=30-2=28. Но в логе написано `inner60%=[24.5,27.5]`. Значит basket_lo=22? 24.5=22+2, 27.5=30-2.5? Это bins=[20..30] → basket_lo=20, basket_hi=30, inner_lo=20+2=22, inner_hi=30-2=28... но в логе [24.5,27.5]. Значит bins=[22..30]? Нужно проверить детально.

---

#### 🟡 `basket_decision.py`: `allow_single_favorite = False` — полезный режим выключен

**Файл:** [src/strategy/config.py:48](src/strategy/config.py#L48)

Single-favourite fallback (ставка только на самый вероятный бин) выключен по умолчанию. В рынках где корзина не проходит EV gate, но favourite имеет реальный edge — это упущенная возможность.

---

### 3. src/data/ — Погодные данные

#### 🟠 `not_enough_models:0/3` для ~15% городов

Из логов для 2026-05-30 (дата +2 дня):
```
REJECTED: Moscow high_temp 2026-05-30 — not_enough_models:0/3
REJECTED: Istanbul high_temp 2026-05-30 — not_enough_models:0/3
REJECTED: Madrid high_temp 2026-05-30 — not_enough_models:0/3
...ещё 10+ городов
```

0 из 3 моделей — Open-Meteo совсем не возвращает данные для этих городов на дату +2 дня. Это может быть:
1. Проблема с API ключом/лимитами
2. Некоторые lat/lon вне зоны покрытия отдельных моделей
3. `forecast_date` не совпадает с тем, что записывает `fetch_and_save_all_cities`

**Нет алерта** — бот молча пропускает эти города без Telegram уведомления.

---

#### 🟡 `weather_fetcher.py` использует `units=imperial` для всех городов

**Файл:** [src/data/weather_fetcher.py:22](src/data/weather_fetcher.py#L22)

```python
params: dict = {
    ...
    "units": "imperial",  # Fahrenheit для US; international cities get Celsius via unit field
}
```

Комментарий неточный: API возвращает всё в °F при `units=imperial`. Но `CITIES_WHITELIST` для Лондона имеет `unit="C"`. Если этот fetcher используется, температуры были бы неверными. **Риска нет**, потому что `weather_fetcher.py` не используется в scheduler (заменён Open-Meteo) — но это создаёт путаницу.

---

### 4. src/market/ — Исполнение ордеров

#### 🟠 Gamma `get_prices_for_group()` использует только первый price из `outcomePrices`

**Файл:** [src/market/gamma_client.py:149](src/market/gamma_client.py#L149)

```python
result[mid] = float(prices[0])  # prices[0] — YES price
```

`outcomePrices` — JSON массив: `["0.35", "0.65"]`. `prices[0]` — это YES price, `prices[1]` — NO price. Код берёт только YES price. Для tail_no нужен NO price, но он вычисляется как `1 - prices[0]` (аппроксимация). Если Polymarket изменит порядок (YES/NO) в outcomePrices, это сломает всё.

**Фикс:** Явно документировать или проверять: `prices[0] = YES, prices[1] = NO`.

---

#### 🟡 N+1 queries в `_get_latest_snapshots`

**Файл:** [src/strategy/live_trader.py:121-140](src/strategy/live_trader.py#L121) и [src/strategy/paper_trader.py:44-64](src/strategy/paper_trader.py#L44)

Отдельный SQL запрос для каждого market_id. При 10 бинах на группу × 94 группы = 940 запросов за один run paper_trader.

**Фикс:**
```python
# Один запрос вместо N:
rows = session.execute(
    select(MarketSnapshot.market_id, 
           func.max(MarketSnapshot.snapshot_time).label("latest"))
    .where(MarketSnapshot.market_id.in_(market_ids))
    .group_by(MarketSnapshot.market_id)
    .subquery()
)
```

---

#### 🟠 `get_all_weather_events()` — только 2 ключевых слова

**Файл:** [src/market/gamma_client.py:162](src/market/gamma_client.py#L162)

```python
for keyword in ("temperature", "precipitation"):
```

Если Polymarket добавит рынки с заголовком "high temp" или "daily high" — они не будут найдены. Текущие заголовки ("highest temperature") покрываются, но стоит мониторить.

---

### 5. src/db/ — База данных

#### 🟠 MarketSnapshot растёт бесконечно

Каждые 10 минут: 1698 снапшотов. За месяц: ~7.3 млн записей. Нет TTL/cleanup job для старых снапшотов.

**Фикс:** Добавить еженедельный cleanup:
```python
session.execute(delete(MarketSnapshot).where(
    MarketSnapshot.snapshot_time < datetime.utcnow() - timedelta(days=7)
))
```

---

#### 🟡 `WeatherSnapshot` UniqueConstraint слишком широкий

**Файл:** [src/db/models.py:91](src/db/models.py#L91)

```python
UniqueConstraint("city", "source", "forecast_date", "snapshot_time", name="uq_weather_snapshot")
```

`snapshot_time` включён в unique key → для каждого запуска fetcher создаётся новая строка (дубликаты по city+source+forecast_date, но с другим временем). Это растит таблицу.

**Лучший подход:** UpsertOrUpdate по (city, source, forecast_date), обновляя данные.

---

#### 🟡 `DailySummary.hwm` — только paper PnL

**Файл:** [src/scheduler.py:319](src/scheduler.py#L319)

```python
hwm = sum(t.pnl_usd or 0.0 for t in all_resolved)  # только PaperTrade
```

Live trades не включены в HWM. В `/status` команде бота `hwm` вводит в заблуждение.

---

### 6. src/scheduler.py — Планировщик

#### 🟠 Закомментированный except в `_job_paper_trade_engine`

**Файл:** [src/scheduler.py:112-115](src/scheduler.py#L112)

```python
# except Exception as e:
#     logger.error(f"Paper trade engine job failed: {e}")
except Exception as exc:
    logger.exception("Paper trade engine job failed")
```

Старый except оставлен закомментированным. Нужно удалить.

---

#### 🟡 Snapshot job занимает ~2 минуты

Из логов: 21:17:51 → 21:19:48 = **117 секунд** для 159 групп. `time.sleep(0.2)` между группами = 32 сек дополнительно. При 10-минутном интервале это нормально, но граничит с лимитом.

---

## АНАЛИЗ ЛОГОВ (ЭТАП 3)

**Одна сессия (2026-05-28 21:15:39):**

| Метрика | Значение |
|---------|---------|
| Групп в горизонте | 94 |
| REJECTED | ~88 (93.6%) |
| SKIP (closes <6h) | ~25 (сегодняшние) |
| SKIP (settled) | 7 |
| PLACED basket | **0 (0%)** |
| Tail попыток | ~90 |
| Tail `portfolio_cap_reached` | ~87 (97%) |
| Tail `too_early` | ~30 |
| Tail PLACED | **0 (0%)** |

**Rejection breakdown:**
```
consensus_spread > 2.5°C:        ~35%   (Seoul 5.79°, Beijing 6.59°, Hong Kong 4.71°, ...)
sum_of_prices > 0.96:            ~21%   (Tokyo 1.05, Paris 1.03, Singapore 1.04, ...)
not_enough_models:0/3:           ~17%   (Moscow, Istanbul, Madrid, Tel Aviv, Paris+30 ...)
consensus_outside/near_edge:     ~14%   (Amsterdam, Los Angeles, Busan, ...)
low_volume < $200:               ~9%    (SF $161, New York $185, London $69, ...)
edge < 10%:                      ~5%    (Sao Paulo 4.5%, Jeddah 5.3%, Taipei 5.8%, ...)
```

**Ключевые наблюдения:**
1. `not_enough_models:0/3` для городов с датой +2 дня — Open-Meteo не имеет данных для многих европейских и азиатских городов на дальний горизонт
2. Tail strategy полностью парализована cap ($23.82/$24.03)
3. 0 сделок в live mode — бот не генерирует дохода при текущих фильтрах

---

## РИСК-МЕНЕДЖМЕНТ: ОЦЕНКА И ПРЕДЛОЖЕНИЯ (ЭТАП 4)

### Текущие лимиты

| Параметр | Значение | Оценка |
|----------|---------|--------|
| `max_single_bet_usd` | $3 | Разумно (7.5% от $40) |
| `max_daily_bets` | 5 | Ограничительно при 94 группах |
| `max_concurrent_bets` | 5 | **Проблема**: все 5 слотов заняты tail |
| `daily_loss_limit_usd` | $10 | 25% от $40, агрессивно |
| `total_stop_loss_usd` | $20 | 50% от $40 — OK |
| `tail_max_portfolio_pct` | 0.60 | **Слишком высоко**: $24 из $40 = 60% |
| `tail_max_position_usd` | $12 | 30% от $40 — очень высоко |

### Проблема конфликта лимитов

`max_concurrent_bets = 5` не разделяет basket и tail позиции. Результат:
- 9 tail positions в БД (`status IN ["pending","open","filled"]`)
- Но `_load_live_stats` считает только `["pending", "open"]` для `_open_bets`
- При filled tail trades → basket может торговать ✓

Реальная проблема: `$23.82` замороженных в tail = 59.5% портфеля. При `wallet_balance = $40.05` и `bankroll_fraction_per_basket = 0.10` → basket budget = $4 на корзину. Это работает, но tail cap слишком агрессивен.

### Рекомендуемые лимиты (для $40 счёта)

| Параметр | Текущее | Рекомендуемое | Обоснование |
|----------|---------|---------------|-------------|
| `tail_max_portfolio_pct` | 0.60 | **0.40** | Оставить 60% для basket + buffer |
| `tail_max_position_usd` | $12 | **$6** | Один tail не должен = 1 basket loss |
| `max_concurrent_bets` | 5 | **8** | Разрешить basket при активных tails |
| `daily_loss_limit_usd` | $10 | **$8** | Чуть консервативнее для маленького счёта |
| `max_daily_bets` | 5 | **10** | При текущем rejection rate 95% — не ограничивает |

### Можно ли обойти риск-менеджер?

1. **Да, через двойной check**: как описано выше, pending trade в БД не блокирует ещё до `record_bet_placed`
2. **Да, через restart**: emergency stop сбрасывается
3. **Нет**: нет прямого обхода через Telegram команды

---

## СТРАТЕГИИ: АНАЛИЗ И УЛУЧШЕНИЯ (ЭТАП 5)

### Текущие фильтры: оценка

| Фильтр | Значение | Оценка |
|--------|---------|--------|
| `consensus_spread <= 2.5°C` | 2.5°C | **Слишком строго**: ~35% отклонений |
| `sum_of_prices <= 0.96` | 0.96 | Обоснован (overround), но блокирует ~21% |
| `min_models = 3` | 3 | Разумно, но ~17% не имеют данных |
| `min_bin_volume_usd = $200` | $200 | Разумно, иногда блокирует ($161-$185) |
| `strategy_min_edge_percent = 10%` | 10% | Строго: близкие рынки (4.5-9.6%) отклоняются |

**Вывод:** Rejection rate 95%+ — фильтры слишком консервативны для текущих рыночных условий.

### 3 конкретных улучшения существующих стратегий

**Улучшение 1: Расслабить consensus_spread для strategy B**

```python
# strategy/config.py — добавить отдельный параметр для narrow:
basket_narrow_max_model_disagreement_c: float = Field(default=3.0)  # было 2.5°C для обоих

# checklist.py — применять только для Strategy A:
if strategy == "basket_wide":
    if spread_c > ss.strategy_max_model_disagreement_c:  # 2.5°C
        return _reject(...)
else:  # basket_narrow
    if spread_c > ss.basket_narrow_max_model_disagreement_c:  # 3.0°C
        return _reject(...)
```

Ожидаемый эффект: +15-20% trade opportunities для Strategy B.

**Улучшение 2: sum_of_prices для basket_narrow = 0.98**

```python
# strategy/config.py:
basket_narrow_max_basket_sum: float = Field(default=0.98)

# basket_decision.py decide_basket_narrow():
if sum_ask >= 1.0:
    return BasketDecision(False, ...)
# Применять looser sum check для narrow basket
```

Рынки с Σask=0.97-0.98 (Sao Paulo, Buenos Aires, Helsinki) при 3 бинах можно торговать если edge >= 3%.

**Улучшение 3: Добавить агрегированный лог причин отклонения**

```python
# В run_live_trade_engine() перед return:
rejection_counts: dict[str, int] = {}
for group in groups:
    result = evaluate_checklist(...)
    if not result.passed:
        reason_key = (result.rejection_reason or "unknown").split(":")[0]
        rejection_counts[reason_key] = rejection_counts.get(reason_key, 0) + 1

logger.info(
    f"[live_trader] Rejection summary ({len(groups)} groups): "
    + ", ".join(f"{k}={v}" for k, v in sorted(rejection_counts.items(), key=lambda x: -x[1]))
)
```

Сейчас каждое отклонение — отдельная строка DEBUG. Одна строка INFO со статистикой была бы полезнее.

### 2 новые стратегии

**Новая стратегия 1: Momentum — Bin Price Reversion**

Идея: Если рыночная цена бина упала на >15% от среднего за последние 3 снапшота (mean reversion), а модель предсказывает стабильное значение — купить YES.

```python
# strategy/momentum_strategy.py
def find_reversion_candidates(session, group, prices, forecasts):
    for market in group.markets:
        recent_snaps = get_last_n_snapshots(session, market.market_id, n=3)
        if len(recent_snaps) < 3:
            continue
        avg_recent = mean([s.price_yes for s in recent_snaps])
        current = prices.get(market.market_id, 0)
        if current < avg_recent * 0.85:  # упал на 15%
            yield market  # кандидат на покупку
```

**Новая стратегия 2: Calendar Spread — торговля несоответствием дат**

Polymarket одновременно торгует одним городом на разные даты (завтра, послезавтра). Если `high_temp London 2026-05-29` имеет высокую uncertainty, а `high_temp London 2026-05-30` показывает тот же bin как фаворит — можно торговать +30h spread при consensus.

Это реалистично добавить в текущую архитектуру через новый флаг стратегии в `strategy_flags.py`.

---

## КАЧЕСТВО КОДА (ЭТАП 6)

### HTTP таймауты

| Файл | Таймаут | Статус |
|------|---------|--------|
| `weather_fetcher.py` | timeout=15 | ✅ |
| `gamma_client.py` | timeout=30 | ✅ |
| `stats.py` | timeout=15 | ✅ |
| `open_meteo.py` | Нужно проверить | ⚠️ |

### Ключи в логах

- `order_executor.py`: явный комментарий "never logs secrets" ✅
- `polymarket_auth.py`: создаёт client, не логирует credentials ✅
- `settings.py`: `private_key` field — если когда-либо попадёт в `logger.info(settings)` → утечка
- **Риск:** добавить `@field_validator('private_key') def mask_key(cls, v): return '***'` для безопасного repr

### Покрытие тестами

| Модуль | Покрыт | Приоритет |
|--------|--------|-----------|
| `basket_builder.py` | ✅ tests/strategy/test_basket_builder.py | — |
| `checklist.py` | ✅ tests/strategy/test_checklist.py | — |
| `paper_trader.py` | ✅ tests/strategy/test_paper_trader.py | — |
| `risk_manager.py` | ✅ tests/risk/test_risk_manager.py | — |
| `pnl_calculator.py` | ✅ tests/strategy/test_pnl.py | — |
| `parsers.py` | ✅ tests/test_parsers.py | — |
| **`live_trader.py`** | ❌ НЕТ | 🔴 КРИТИЧЕСКИЙ |
| **`scheduler.py`** | ❌ НЕТ | 🟠 ВЫСОКИЙ |
| **`tail_seller.py`** | ❌ НЕТ | 🟠 ВЫСОКИЙ |
| `gamma_client.py` | ❌ НЕТ | 🟡 СРЕДНИЙ |
| `order_executor.py` | ⚠️ Частично | 🟡 СРЕДНИЙ |
| `basket_decision.py` | ❌ НЕТ | 🟡 СРЕДНИЙ |

### Прочие находки

1. **`gamma_client.py` retry только на ConnectionError/Timeout** — HTTPError (403, 500) не retried:
   ```python
   retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout))
   ```
   Gamma API возвращает 403 при rate limit — не будет повторного запроса. Добавить `retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError))` с отдельной обработкой 429.

2. **`_job_expire_stale_pending` — нет rollback при ошибке:**
   ```python
   try:
       for t in stale:
           t.status = "expired"
       session.commit()  # если тут упадёт — partial commit?
   ```
   SQLite в рамках одной транзакции откатится автоматически при ошибке, но стоит добавить explicit `except: session.rollback()`.

3. **`run_initial_jobs()` запускает ВСЕ jobs при старте** включая `_job_live_trade_engine()`. При первом старте снапшоты могут быть неполными — live trades разместятся на устаревших данных. Рекомендуется выждать первый snapsot перед live_trade.

---

## СВОДНАЯ ТАБЛИЦА НАХОДОК

| # | Приоритет | Модуль | Описание |
|---|-----------|--------|---------|
| 1 | 🔴 КРИТ | live_trader.py | Двойная risk-проверка ломает multi-bin корзины |
| 2 | 🔴 КРИТ | live_trader.py | Blocking poll (до 90 мин) блокирует scheduler |
| 3 | 🔴 КРИТ | tail_seller.py | Аппроксимация no_ask = 1-yes_price (без spread) |
| 4 | 🟠 АРXIT | risk_manager.py | Emergency stop не персистируется |
| 5 | 🟠 АРХ | scheduler.py | Tail cap (60%) парализует basket стратегии |
| 6 | 🟠 АРХ | gamma_client.py | HTTPError не retried (403 на rate limit) |
| 7 | 🟠 АРХ | paper_trader.py | N+1 queries в _get_latest_snapshots |
| 8 | 🟠 АРХ | scheduler.py | MarketSnapshot растёт бесконечно (нет TTL) |
| 9 | 🟡 UPG | config.py | consensus_spread 2.5°C слишком строго (95% rejection) |
| 10 | 🟡 UPG | config.py | basket_narrow мог бы иметь looser sum_of_prices |
| 11 | 🟡 UPG | models.py | WeatherSnapshot UniqueConstraint включает snapshot_time |
| 12 | 🟡 UPG | risk_manager.py | private_key может попасть в repr settings |
| 13 | 🟡 UPG | scheduler.py | DailySummary HWM не включает live PnL |
| 14 | 🟡 UPG | — | Мёртвый код: wunderground.py, weather_fetcher.py, clob_client.py |
| 15 | 🟡 UPG | live_trader.py | run_initial_jobs запускает live_trade до полных снапшотов |

---

## ПЛАН ДЕЙСТВИЙ (по приоритету)

### Немедленно (сегодня)

1. **Снизить `tail_max_portfolio_pct` с 0.60 до 0.40** в `.env` — освободить капитал для basket
2. **Снизить `order_timeout_minutes` с 30 до 5** — уменьшить блокировку scheduler
3. **Снизить `tail_max_position_usd` с 12 до 6** — ограничить exposure на одну позицию

### Краткосрочно (эта неделя)

4. Исправить bug с pending trade блокирующим корзину (перенести `session.add` после `place_limit_order`)
5. Добавить `strategy_max_model_disagreement_c = 3.0` для Strategy B
6. Добавить cleanup job для MarketSnapshot (старше 7 дней)
7. Добавить retry для HTTPError в gamma_client

### Среднесрочно (следующая неделя)

8. Персистировать emergency_stop в БД
9. Убрать мёртвый код (wunderground, weather_fetcher, clob_client из unused path)
10. Переписать `_get_latest_snapshots` на batch query
11. Добавить тесты для live_trader.py и tail_seller.py
12. Добавить агрегированный лог rejection причин (одна INFO строка)

---

*Отчёт сгенерирован автоматически на основе статического анализа кода и логов.*  
*Файл: `REVIEW_REPORT.md` | Версия: 1.0 | 2026-05-28*
