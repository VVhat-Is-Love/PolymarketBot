"""
k·σ distance gate verification — реальные числа входа 16 июня 2026.

Запуск на сервере:
    su - bot -c "cd /opt/polymarket-bot/app && /opt/polymarket-bot/venv/bin/python scripts/verify_ksigma_gate.py"

Или локально:
    python scripts/verify_ksigma_gate.py

Источник чисел (DIAG-1):
    Denver  94-95°F  consensus=91.04°F  near_edge=94°F  dist=2.96°F  → ожидаем SKIP
    Austin  90-91°F  consensus=85.64°F  near_edge=90°F  dist=4.36°F  → ожидаем PASS
    Ankara  86-87°F  consensus=79.90°F  near_edge=86°F  dist=6.10°F  → ожидаем PASS

Критерий приёмки: Denver = SKIP (это доказательство, что гейт фильтрует).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)

from src.strategy.live_trader import _tail_ksigma_ok
from src.strategy.config import StrategySettings


def _run_case(
    label: str,
    city: str,
    bin_label: str,
    bin_lo: float,
    bin_hi: float,
    consensus: float,
    unit: str,
    expected: str,          # "PASS" or "SKIP"
    ss: StrategySettings,
) -> bool:
    ok, near, dist, thr, k = _tail_ksigma_ok(bin_lo, bin_hi, consensus, unit, ss)
    sigma_gate = thr / k if k > 0 else 0.0
    verdict = "PASS" if ok else "SKIP"

    # Exact format matching the new live log line
    log_line = (
        f"[tail_ksigma] {city} {bin_label}: "
        f"consensus={consensus:.2f}°{unit} "
        f"near_edge={near:.2f}°{unit} "
        f"dist={dist:.2f}°{unit} "
        f"sigma={sigma_gate:.2f}°{unit} "
        f"k={k:.1f} verdict={verdict}"
    )

    passed = verdict == expected
    status = "PASS" if passed else "FAIL"
    print(f"\n[{status}] {label}")
    print(f"  {log_line}")
    print(f"  expected={expected}  got={verdict}  threshold={thr:.2f}°{unit}")
    if not passed:
        print(f"  !! MISMATCH — gate not filtering correctly")
    return passed


def main() -> int:
    ss = StrategySettings()

    print("=" * 70)
    print("k-sigma gate verification — числа входа 16 июня 2026")
    print(f"Config: tail_distance_sigma_f={ss.tail_distance_sigma_f}  "
          f"tail_k_sigma={ss.tail_k_sigma}  "
          f"tail_k_sigma_above={getattr(ss, 'tail_k_sigma_above', 0.0)}")
    print(f"Default threshold = {ss.tail_k_sigma * ss.tail_distance_sigma_f:.2f}°F")
    print("=" * 70)

    results = [
        # Denver 94-95°F: dist=2.96 < threshold=3.4 → SKIP (ошибочно вошёл 16-го)
        _run_case(
            label="Denver 94-95°F (должен SKIP — был ошибочный вход)",
            city="Denver", bin_label="94-95°F",
            bin_lo=94.0, bin_hi=95.0,
            consensus=91.04,   # near_edge=94.0, dist=2.96°F
            unit="F", expected="SKIP", ss=ss,
        ),
        # Austin 90-91°F: dist=4.36 >= threshold=3.4 → PASS (легитимный вход)
        _run_case(
            label="Austin 90-91°F (должен PASS — легитимный вход)",
            city="Austin", bin_label="90-91°F",
            bin_lo=90.0, bin_hi=91.0,
            consensus=85.64,   # near_edge=90.0, dist=4.36°F
            unit="F", expected="PASS", ss=ss,
        ),
        # Ankara 86-87°F: dist=6.1 >= threshold=3.4 → PASS
        _run_case(
            label="Ankara 86-87°F (должен PASS)",
            city="Ankara", bin_label="86-87°F",
            bin_lo=86.0, bin_hi=87.0,
            consensus=79.90,   # near_edge=86.0, dist=6.10°F
            unit="F", expected="PASS", ss=ss,
        ),
        # Граничный случай: dist == threshold (2.96 = 3.4 - нет; тест на ==threshold)
        _run_case(
            label="Граница: dist==threshold (must PASS, граница включена)",
            city="TEST", bin_label="94-95°F",
            bin_lo=94.0, bin_hi=95.0,
            consensus=94.0 - ss.tail_k_sigma * ss.tail_distance_sigma_f,  # dist == threshold exactly
            unit="F", expected="PASS", ss=ss,
        ),
        # Ниже границы: dist == threshold - 0.01 (must SKIP)
        _run_case(
            label="Ниже границы: dist=threshold-0.01 (must SKIP)",
            city="TEST", bin_label="94-95°F",
            bin_lo=94.0, bin_hi=95.0,
            consensus=94.0 - ss.tail_k_sigma * ss.tail_distance_sigma_f + 0.01,
            unit="F", expected="SKIP", ss=ss,
        ),
    ]

    print("\n" + "=" * 70)
    passed = sum(results)
    total = len(results)
    overall = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} -- {overall}")
    print("=" * 70)

    if passed < total:
        print("\nk-sigma gate не принят. Проверить tail_distance_sigma_f / tail_k_sigma в config.")
        return 1

    print("\nk-sigma gate принят (dry-run). Для подтверждения на сервере:")
    print("  journalctl -u polymarket-bot --since '2026-06-16 09:30' --until '2026-06-16 10:00'")
    print("  | grep tail_ksigma")
    print("\nПосле деплоя нового кода каждый кандидат должен давать строку:")
    print("  [tail_ksigma] <city> <bin>: consensus=... near_edge=... dist=... verdict=PASS/SKIP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
