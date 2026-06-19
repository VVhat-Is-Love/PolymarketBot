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

Источник чисел (DIAG-1, вход 16 июня):
    Denver 94-95°F  consensus=91.04  dist=2.96 dir=ABOVE  → SKIP  (< 7.0°F)
    Austin 90-91°F  consensus=85.64  dist=4.36 dir=ABOVE  → SKIP  (< 7.0°F)

Для Chicago ниже-консенсусного кейса:
    consensus намеренно НЕ хардкодится — нужно подтвердить из journalctl/реплея,
    что фактическая below-dist > 3.5°F. Здесь consensus=75.5 — иллюстративный
    (dist=|71-75.5|=4.5 > 3.5). Перед приёмкой заменить на реальное из лога.
"""
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.strategy.live_trader import _tail_ksigma_ok


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
    consensus: float,
    unit: str,
    expected: str,
    ss: SimpleNamespace,
) -> bool:
    ok, near, dist, thr, k = _tail_ksigma_ok(bin_lo, bin_hi, consensus, unit, ss)
    sigma_gate = thr / k if k > 0 else 0.0
    direction = "ABOVE" if near >= consensus else "BELOW"
    verdict = "PASS" if ok else "SKIP"

    # Формат совпадает с live-логом (live_trader.py)
    log_line = (
        f"[tail_ksigma] {city} {bin_label}: "
        f"consensus={consensus:.2f}°{unit} "
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
    print("k-sigma gate verification (ENTRY-FIX Step 1+2)")
    print(f"  ORIG:    sigma={ORIG.tail_distance_sigma_f}°F  k_below={ORIG.tail_k_sigma}"
          f"  k_above={ORIG.tail_k_sigma_above}"
          f"  -> sym_threshold={ORIG.tail_k_sigma * ORIG.tail_distance_sigma_f:.2f}°F")
    print(f"  INTERIM: sigma={INTERIM.tail_distance_sigma_f}°F  k_below={INTERIM.tail_k_sigma}"
          f"  k_above={INTERIM.tail_k_sigma_above}"
          f"  -> above={INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f:.1f}°F"
          f"  below={INTERIM.tail_k_sigma * INTERIM.tail_distance_sigma_f:.1f}°F")

    # ------------------------------------------------------------------
    # Раздел 1 — ORIG: документация поведения 16 июня (k_above=0, sym k=2.0)
    # Показывает, что Denver был бы SKIP с sigma=1.7, но Austin проходил
    # ------------------------------------------------------------------
    r1 = _section(
        "РАЗДЕЛ 1 — ORIG (sigma=1.7, k_sym=2.0, threshold=3.4°F): поведение 16 июня",
        [
            dict(label="Denver 94-95: dist=2.96 < 3.40 => SKIP",
                 city="Denver", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0, consensus=91.04,
                 unit="F", expected="SKIP", ss=ORIG),
            dict(label="Austin 90-91: dist=4.36 >= 3.40 => PASS (с sigma=1.7 пропускал)",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0, consensus=85.64,
                 unit="F", expected="PASS", ss=ORIG),
        ],
    )

    # ------------------------------------------------------------------
    # Раздел 2 — INTERIM: sigma=3.5°F, k_above=2.0 → 7.0°F, k_below=1.0 → 3.5°F
    # ------------------------------------------------------------------
    r2 = _section(
        "РАЗДЕЛ 2 — INTERIM (sigma=3.5°F, above=7.0°F, below=3.5°F)",
        [
            # Denver: dir=ABOVE, dist=2.96 < 7.0 → SKIP
            dict(label="Denver 94-95: dist=2.96 dir=ABOVE < 7.0 => SKIP",
                 city="Denver", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0, consensus=91.04,
                 unit="F", expected="SKIP", ss=INTERIM),
            # Austin: dir=ABOVE, dist=4.36 < 7.0 → SKIP
            dict(label="Austin 90-91: dist=4.36 dir=ABOVE < 7.0 => SKIP",
                 city="Austin", bin_label="90-91°F",
                 bin_lo=90.0, bin_hi=91.0, consensus=85.64,
                 unit="F", expected="SKIP", ss=INTERIM),
            # Chicago BELOW: near=71, dist=|71-75.5|=4.5 >= 3.5 → PASS
            # ВАЖНО: consensus=75.5 иллюстративный. Подтвердить из journalctl:
            #   journalctl -u polymarket-bot | grep "tail_ksigma.*Chicago.*BELOW"
            #   Убедиться что dist в логе > 3.5°F
            dict(label="Chicago 70-71: dist=4.5 dir=BELOW >= 3.5 => PASS [consensus иллюстративный]",
                 city="Chicago", bin_label="70-71°F",
                 bin_lo=70.0, bin_hi=71.0, consensus=75.5,
                 unit="F", expected="PASS", ss=INTERIM),
            # Граница above: dist == 7.0 → PASS (включительно)
            dict(label="Граница ABOVE: dist=7.00 == 7.0 => PASS (включительно)",
                 city="TEST", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=94.0 - INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f,
                 unit="F", expected="PASS", ss=INTERIM),
            # Под границей above: dist = 6.99 < 7.0 → SKIP
            dict(label="Под границей ABOVE: dist=6.99 < 7.0 => SKIP",
                 city="TEST", bin_label="94-95°F",
                 bin_lo=94.0, bin_hi=95.0,
                 consensus=94.0 - INTERIM.tail_k_sigma_above * INTERIM.tail_distance_sigma_f + 0.01,
                 unit="F", expected="SKIP", ss=INTERIM),
            # Граница below: dist == 3.5 → PASS (включительно)
            dict(label="Граница BELOW: dist=3.5 == 3.5 => PASS (включительно)",
                 city="TEST", bin_label="70-71°F",
                 bin_lo=70.0, bin_hi=71.0,
                 consensus=71.0 + INTERIM.tail_k_sigma * INTERIM.tail_distance_sigma_f,
                 unit="F", expected="PASS", ss=INTERIM),
        ],
    )

    all_results = r1 + r2
    passed = sum(all_results)
    total = len(all_results)

    print(f"\n{'=' * 70}")
    overall = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} -- {overall}")
    print("=" * 70)

    if passed < total:
        print("\nГейт не принят. Проверить параметры в config.py.")
        return 1

    print("\nAccepted (dry-run). On server after deploy:")
    print("  journalctl -u polymarket-bot | grep tail_ksigma")
    print("  Each candidate must emit one line:")
    print("    [tail_ksigma] <city> <bin>: consensus=X near_edge=Y dist=Z dir=ABOVE/BELOW sigma=3.50 k=2.0/1.0 verdict=PASS/SKIP")
    print("  Denver/Austin -> SKIP (dir=ABOVE, dist < 7.0)")
    print("  Confirm Chicago: dist in log > 3.5F (dir=BELOW, not hardcoded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
