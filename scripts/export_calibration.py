"""Export calibration_log to CSV for strategy analysis.

Usage:
    python scripts/export_calibration.py [output.csv] [--days N] [--city CITY] [--pass-only]

Options:
    output.csv      Output path (default: calibration_export_YYYYMMDD.csv)
    --days N        Limit to last N days of forecast_date (default: all)
    --city CITY     Filter to one city (case-insensitive)
    --pass-only     Only include rows where gate_verdict='PASS'
    --resolved-only Only include rows with actual_temp_resolve filled

Columns in output:
    id, timestamp, city, forecast_date, bin_label,
    consensus_temp, sigma_used, sigma_spread,
    bin_near_boundary, bin_far_boundary, bin_distance_sigma,
    gate_verdict, gate_direction, k_sigma_threshold,
    model_p_no, market_no_ask, market_yes_price, volume_usd,
    models_count, bot_traded,
    resolved_outcome, actual_temp_resolve, resolved_at
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, and_
from src.db.session import get_session
from src.db.models import CalibrationLog


_COLUMNS = [
    "id", "timestamp", "city", "forecast_date", "bin_label",
    "consensus_temp", "sigma_used", "sigma_spread",
    "bin_near_boundary", "bin_far_boundary", "bin_distance_sigma",
    "gate_verdict", "gate_direction", "k_sigma_threshold",
    "model_p_no", "market_no_ask", "market_yes_price", "volume_usd",
    "models_count", "bot_traded",
    "resolved_outcome", "actual_temp_resolve", "resolved_at",
]


def main() -> None:
    p = argparse.ArgumentParser(description="Export calibration_log to CSV")
    p.add_argument("output", nargs="?", help="Output CSV path")
    p.add_argument("--days", type=int, default=None, help="Limit to last N days")
    p.add_argument("--city", default=None, help="Filter by city")
    p.add_argument("--pass-only", action="store_true", help="Only gate_verdict=PASS rows")
    p.add_argument("--resolved-only", action="store_true",
                   help="Only rows with actual_temp_resolve filled")
    args = p.parse_args()

    out_path = args.output or f"calibration_export_{date.today().strftime('%Y%m%d')}.csv"

    session = get_session()
    try:
        stmt = select(CalibrationLog).order_by(CalibrationLog.forecast_date, CalibrationLog.city,
                                               CalibrationLog.timestamp)
        filters = []
        if args.days:
            cutoff = date.today() - timedelta(days=args.days)
            filters.append(CalibrationLog.forecast_date >= cutoff)
        if args.city:
            filters.append(CalibrationLog.city.ilike(f"%{args.city}%"))
        if args.pass_only:
            filters.append(CalibrationLog.gate_verdict == "PASS")
        if args.resolved_only:
            filters.append(CalibrationLog.actual_temp_resolve.is_not(None))
        if filters:
            stmt = stmt.where(and_(*filters))

        rows = session.execute(stmt).scalars().all()
    finally:
        session.close()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: getattr(row, col, None) for col in _COLUMNS})

    print(f"Exported {len(rows)} rows → {out_path}")


if __name__ == "__main__":
    main()
