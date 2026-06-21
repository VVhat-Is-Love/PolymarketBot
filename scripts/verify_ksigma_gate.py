"""
k·sigma distance gate verification — реальные числа входа 16 июня 2026.

Запуск на сервере:
    su - bot -c "cd /opt/polymarket-bot/app && \
        /opt/polymarket-bot/venv/bin/python scripts/verify_ksigma_gate.py"

Или локально:
    python scripts/verify_ksigma_gate.py

Параметры (INTERIM, Step 2):
    tail_distance_sigma_f = 3.5°F  — честная оценка RMSE прогноза
    tail_k_sigma_above    = 2.0    → порог ABOVE = 7.0°F
    tail_k_sigma (below)  = 1.0    → порог BELOW = 3.5°F

ВАЖНО — формат consensus_temp:
    Open-Meteo всегда возвращает температуру в °C.
    Бины US-рынков в °F. Функция _tail_ksigma_ok принимает consensus в °C
    и конвертирует внутри для сравнения с bin-границами.
    ВСЕ кейсы ниже передают consensus в °C (как из реального API).
    Раздел 3 явно тестирует смесь единиц — именно она не поймала баг
    при synthetic-тестировании 8/8 (все были в одной единице).

    T1 FIX: unit НЕ передаётся в тест — определяется из bin_label (как в проде).
    Это ловит баг, когда group.unit="C" для US-рынков ("°F" в bin_label → "F").

Источник чисел (DIAG-1, вход 16 июня):
    Denver 94-95°F  consensus=32.8°C (=91.04°F)  dist=2.96°F dir=ABOVE  → SKIP
    Austin 90-91°F  consensus=29.8°C (=85.64°F)  dist=4.36°F dir=ABOVE  → SKIP
"""
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.live_trader import _tail_ksigma_ok, _unit_from_label


# ---------------------------------------------------------------------------
# Вспомогательные конвертации
# ---------------------------------------------------------------------------

def _f_to_c(f: float) -> float:
    """°F → °C (используется для конвертации legacy °F-consensus в тест-кейсах)."""
    return (f - 32.0) / 1.8


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


# ---------------------------------------------------------------------------
# Параметры: явные, независимые от production config
# ---------------------------------------------------------------------------

def _make_ss(sigma_f: float, k_below: float, k_above: float) -> SimpleNamespace:
    return SimpleNamespace(
        tail_distance_sigma_f=sigma_f,
        tail_k_sigma=k_below,
        tail_k_sigma_above=k_above,
    )


# Исходные (bug-era): для документации поведения 16 июня
ORIG    = _make_ss(sigma_f=1.7, k_below=2.0, k_above=0.0)

# INTERIM Step 2: sigma=3.5°F, above=7.0°F, below=3.5°F
INTERIM = _make_ss(sigma_f=3.5, k_below=1.0, k_above=2.0)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_case(
    label: str,
    city: str,
    bin_label: str,
    bin_lo: float,
    bin_hi: float,
    consensus: float,    # ВСЕГДА в °C (как из Open-Meteo)
    expected: str,
    ss: SimpleNamespace,
) -> bool:
    # T1-GATE-UNIT-DETECTION: derive unit from bin_label exactly as prod code does.
    # No explicit 'unit' param — test must not hardcode "F"/"C" to catch DB-field bugs.
    unit = _unit_from_label(bin_label) or "F"

    ok, near, dist, thr, k = _tail_ksigma_ok(bin_lo, bin_hi, consensus, unit, ss)
    sigma_gate = thr / k if k > 0 else 0.0

    # Отображаем consensus в единице рынка (как в live-логе)
    consensus_in_unit = _c_to_f(consensus) if unit == "F" else consensus
    direction = "ABOVE" if near >= consensus_in_unit else "BELOW"
    verdict = "PASS" if ok else "SKIP"

    # Формат совпадает с live-логом после GATE-UNIT-DETECTION fix (live_trader.py)
    log_line = (
        f"[tail_ksigma] {city} {bin_label}: "
        f"consensus={consensus_in_unit:.2f}°{unit} "
        f"({consensus:.2f}°C) "
        f"near_edge={near:.2f}°{unit} "
        f"dist={dist:.2f}°{unit} "
        f"dir={direction} "
        f"sigma={sigma_gate:.2f}°{unit} "
        f"k={k:.1f} verdict={verdict}"
    )

    passed = verdict == expected
    status = "PASS" if passed else "FAIL"
    print(f"\n  [{status}] {label}")
    print(f"    {log_line}")
    print(f"    expected={expected}  threshold={thr:.2f}°{unit}", end="")
    print("  !! MISMATCH" if not passed else "")
    return passed


def _section(title: str, cases: list) -> list:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")
    return [_run_case(**c) for c in cases]


def main() -> int:
    print("=" * 70)
    print("k-sigma gate verification (ENTRY-FIX + GATE-UNIT-BUG + GATE-UNIT-DETECTION fix)")
    print(f"  ORIG:    sigma={ORIG.tail_distance_sigma_f}°F  k_below={ORIG.tail_k_sigma}"
          f"  k_above={ORIG.tail_k_sigma_above}"
          f"  -> sym_threshold={ORIG.tail_k_sigma * ORIG.tail_distance_sigma_f:.2f}°F")
    print(f"  INTERIM: sigma={INTERIM.tail_distance_sigma_f}°F  k_below={INTERIM.tail_k_sigma}"
          f"  k_above={INTERIM.tail_k_sigma_above}"
          f"  -> above={INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f:.1f}°F"
          f"  below={INTERIM.tail_k_sigma * INTERIM.tail_distance_sigma_f:.1f}°F")
    print("  consensus: ALL inputs in °C (Open-Meteo format)")
    print("  unit: derived from bin_label (T1 fix — NOT hardcoded in test cases)")

    # ------------------------------------------------------------------
    # Раздел 1 — ORIG: документация поведения 16 июня (k_above=0, sym k=2.0)
    # consensus в °C (32.8 = 91.04°F, 29.8 = 85.64°F)
    # ------------------------------------------------------------------
    r1 = _section(
        "РАЗДЕЛ 1 — ORIG (sigma=1.7, k_sym=2.0, threshold=3.4°F): поведение 16 июня",
        [
            dict(label="Denver 94-95: dist=2.96F < 3.40F => SKIP",
                 city="Denver", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=32.8,           # 32.8°C = 91.04°F
                 expected="SKIP", ss=ORIG),
            dict(label="Austin 90-91: dist=4.36F >= 3.40F => PASS (с sigma=1.7 пропускал)",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0,
                 consensus=29.8,           # 29.8°C = 85.64°F
                 expected="PASS", ss=ORIG),
        ],
    )

    # ------------------------------------------------------------------
    # Раздел 2 — INTERIM: sigma=3.5°F, k_above=2.0 → 7.0°F, k_below=1.0 → 3.5°F
    # ------------------------------------------------------------------
    r2 = _section(
        "РАЗДЕЛ 2 — INTERIM (sigma=3.5°F, above=7.0°F, below=3.5°F)",
        [
            # Denver: dir=ABOVE, dist=2.96F < 7.0F → SKIP
            dict(label="Denver 94-95: dist=2.96F dir=ABOVE < 7.0F => SKIP",
                 city="Denver", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=32.8,           # 32.8°C = 91.04°F
                 expected="SKIP", ss=INTERIM),
            # Austin: dir=ABOVE, dist=4.36F < 7.0F → SKIP
            dict(label="Austin 90-91: dist=4.36F dir=ABOVE < 7.0F => SKIP",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0,
                 consensus=29.8,           # 29.8°C = 85.64°F
                 expected="SKIP", ss=INTERIM),
            # Chicago BELOW: near=71F, consensus=75.5F=24.17°C, dist=4.5F >= 3.5F → PASS
            dict(label="Chicago 70-71: dist=4.5F dir=BELOW >= 3.5F => PASS [consensus иллюстративный]",
                 city="Chicago", bin_label="70-71°F",
                 bin_lo=70.0, bin_hi=71.0,
                 consensus=_f_to_c(75.5),  # 24.167°C = 75.5°F (иллюстративно)
                 expected="PASS", ss=INTERIM),
            # Граница above: dist == 7.0 → PASS (включительно)
            dict(label="Граница ABOVE: dist=7.00F == 7.0F => PASS (включительно)",
                 city="TEST", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=_f_to_c(94.0 - INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f),
                 expected="PASS", ss=INTERIM),
            # Под границей above: dist = 6.99 < 7.0 → SKIP
            dict(label="Под границей ABOVE: dist=6.99F < 7.0F => SKIP",
                 city="TEST", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=_f_to_c(94.0 - INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f + 0.01),
                 expected="SKIP", ss=INTERIM),
            # Граница below: dist == 3.5 → PASS (включительно)
            dict(label="Граница BELOW: dist=3.5F == 3.5F => PASS (включительно)",
                 city="TEST", bin_label="70-71°F",
                 bin_lo=70.0, bin_hi=71.0,
                 consensus=_f_to_c(71.0 + INTERIM.tail_k_sigma * INTERIM.tail_distance_sigma_f),
                 expected="PASS", ss=INTERIM),
        ],
    )

    # ------------------------------------------------------------------
    # Раздел 3 — REAL UNITS: смесь °C / °F как в реальном API
    #
    # Это тесты, которые ПОЙМАЛИ БЫ GATE-UNIT-BUG (был зафиксирован 20 июня):
    #   consensus всегда °C от Open-Meteo, бины US в °F.
    #   До фикса: |84°F - 31.47°C| = 52.53 → всегда PASS.
    #   После фикса: consensus конвертируется в °F перед расчётом.
    #
    # Кейс Dallas 84-85F с consensus=31.47°C особо важен:
    #   ДО фикса лог показывал: dist=52.53 dir=ABOVE PASS (баг)
    #   ПОСЛЕ фикса: consensus=88.65°F → BELOW, dist=3.65°F > 3.5°F → PASS (корректно)
    # ------------------------------------------------------------------
    r3 = _section(
        "РАЗДЕЛ 3 — REAL UNITS (consensus °C от Open-Meteo, бины °F) — BUG CATCHER",
        [
            # Austin: consensus=29.8°C (Open-Meteo raw), bin=90°F
            # Без конвертации: 29.8 vs 90 → dist=60.2 → PASS (БАГ)
            # С конвертацией: 29.8°C=85.64°F, dist=4.36°F < 7°F → SKIP (правильно)
            dict(label="Austin 90-91F: consensus=29.8°C (raw Open-Meteo) => ABOVE dist=4.36F < 7F => SKIP",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0,
                 consensus=29.8,   # °C как из API, НЕ конвертированный
                 expected="SKIP", ss=INTERIM),
            # Denver: consensus=32.8°C, bin=94°F
            # Без конвертации: 32.8 vs 94 → dist=61.2 → PASS (БАГ)
            # С конвертацией: 32.8°C=91.04°F, dist=2.96°F < 7°F → SKIP (правильно)
            dict(label="Denver 94-95F: consensus=32.8°C (raw Open-Meteo) => ABOVE dist=2.96F < 7F => SKIP",
                 city="Denver", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=32.8,
                 expected="SKIP", ss=INTERIM),
            # Dallas 84-85F: BELOW (consensus выше бина), должен PASS при наших порогах
            # consensus=31.47°C → 88.65°F; bin 84-85°F → BELOW, dist=3.65°F > 3.5°F → PASS
            # Без конвертации: 31.47 vs 84 → dist=52.53 dir=ABOVE (!!!!) → PASS (баг — но правильный verdict по случайности, неверный dir)
            # С конвертацией: dir=BELOW, dist=3.65F > 3.5F → PASS (правильно)
            dict(label="Dallas 84-85F: consensus=31.47°C => BELOW dist=3.65F > 3.5F => PASS",
                 city="Dallas", bin_label="84-85°F",
                 bin_lo=84.0, bin_hi=85.0,
                 consensus=31.47,  # 31.47°C = 88.65°F (выше бина → BELOW)
                 expected="PASS", ss=INTERIM),
            # Chengdu °C-рынок — контроль: consensus и бины оба в °C, конвертация не нужна
            # consensus=29°C, bin=29-30°C, ABOVE (bin_lo=29 >= consensus=29), dist=0 < 1.94°C → SKIP
            dict(label="Chengdu 29-30C: consensus=29.0°C (°C-рынок, контроль) => ABOVE dist=0 < 1.94C => SKIP",
                 city="Chengdu", bin_label="29-30°C",
                 bin_lo=29.0, bin_hi=30.0,
                 consensus=29.0,
                 expected="SKIP", ss=INTERIM),
        ],
    )

    # ------------------------------------------------------------------
    # Раздел 4 — T1-GATE-UNIT-DETECTION: unit auto-detect от bin_label
    #
    # Приёмочный тест для T1: бот определяет unit из bin_label (НЕ из group.unit).
    # Имитирует ситуацию Seattle, где group.unit="C" в БД, но bin_label="74-75°F".
    #
    # До T1-fix: _unit = group.unit = "C" → consensus 27.44°C НЕ конвертируется →
    #   bin_lo=74 трактуется как °C → above=(74>=27.44)=True → near=74, dist=46.56°C → PASS (баг)
    # После T1-fix: unit="F" (из "°F" в bin_label) → consensus конвертируется в 81.39°F →
    #   above=(74>=81.39)=False → BELOW → near=hi=75°F, dist=|75-81.39|=6.39°F
    #   k_eff=1.0 (below), threshold=3.5°F → 6.39 >= 3.5 → PASS (верно: Seattle BELOW консенсуса)
    # ------------------------------------------------------------------
    r4 = _section(
        "РАЗДЕЛ 4 — T1 GATE-UNIT-DETECTION (unit авто из bin_label, не из group.unit)",
        [
            # Seattle 74-75°F: group.unit="C" в БД (баг), но bin_label="74-75°F" → unit="F" (авто)
            # consensus=27.44°C = 81.39°F; bin 74-75°F ниже консенсуса → BELOW
            # near=75°F (hi), dist=6.39°F > 3.5°F → PASS
            dict(label="Seattle 74-75F: consensus=27.44°C => unit=F (auto), BELOW dist=6.39F > 3.5F => PASS",
                 city="Seattle", bin_label="74-75°F",
                 bin_lo=74.0, bin_hi=75.0,
                 consensus=27.44,  # 27.44°C = 81.39°F (из живого лога 21 июня)
                 expected="PASS", ss=INTERIM),
            # Austin (повтор из р3 — явная проверка T1-пути, без unit-параметра)
            dict(label="Austin 90-91F: unit=F (auto из bin_label), consensus=29.8°C => SKIP",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0,
                 consensus=29.8,
                 expected="SKIP", ss=INTERIM),
            # Chengdu °C-рынок через T1-путь — bin_label="29-30°C" → unit="C" (auto)
            dict(label="Chengdu 29-30C: unit=C (auto из bin_label), контроль => SKIP",
                 city="Chengdu", bin_label="29-30°C",
                 bin_lo=29.0, bin_hi=30.0,
                 consensus=29.0,
                 expected="SKIP", ss=INTERIM),
        ],
    )

    all_results = r1 + r2 + r3 + r4
    passed = sum(all_results)
    total = len(all_results)

    print(f"\n{'=' * 70}")
    overall = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} -- {overall}")
    print("=" * 70)

    if passed < total:
        print("\nГейт не принят. Проверить unit-конвертацию в _tail_ksigma_ok (live_trader.py).")
        return 1

    print("\nAccepted. On server after deploy:")
    print("  journalctl -u polymarket-bot | grep tail_ksigma")
    print("  Формат лога (после T1 GATE-UNIT-DETECTION fix):")
    print("    [tail_ksigma] Seattle 74-75°F: consensus=81.39°F (27.44°C) near_edge=75°F dist=6.39°F dir=BELOW ...")
    print("  US-бины: unit=F определяется из bin_label (НЕ из group.unit)")
    print("  Austin/Denver -> SKIP (dir=ABOVE, dist < 7.0F)")
    print("  Dallas -> PASS (dir=BELOW, dist > 3.5F)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
