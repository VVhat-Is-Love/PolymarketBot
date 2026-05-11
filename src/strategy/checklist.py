import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from src.db.models import MarketGroup, Market, WeatherSnapshot
from src.strategy.config import strategy_settings as ss
from src.strategy.basket_builder import BasketItem, build_basket
from src.strategy.pnl_calculator import calculate_edge


@dataclass
class ChecklistResult:
    passed: bool
    rules_ok: bool
    consensus_ok: bool
    sum_of_prices_ok: bool
    stability_ok: bool
    edge_ok: bool
    consensus_temp: float | None = None
    sum_of_prices: float | None = None
    estimated_edge: float | None = None
    basket: list[BasketItem] = field(default_factory=list)
    rejection_reason: str | None = None
    # Populated for cold-start / quality rejections → saved to rejected_markets table
    bin_volumes_snapshot: dict[str, float | None] | None = None
    consensus_offset: float | None = None


def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def _safe_float(val) -> float | None:
    """Convert val to float; return None for non-numeric values (guards against mocks in tests)."""
    if val is None:
        return None
    try:
        result = float(val)
        # float(MagicMock()) succeeds but returns 1.0 — detect by checking if val is a real number
        if not isinstance(val, (int, float)):
            return None
        return result
    except (TypeError, ValueError):
        return None


def evaluate_checklist(
    group: MarketGroup,
    markets: list[Market],
    prices: dict[str, float | None],
    forecasts: list[WeatherSnapshot],
) -> ChecklistResult:
    """Run the 5-point entry checklist and return a detailed result."""

    def _reject(
        reason: str,
        *,
        rules_ok: bool = False,
        consensus_ok: bool = False,
        consensus_temp: float | None = None,
        bin_volumes_snapshot: dict | None = None,
        consensus_offset: float | None = None,
    ) -> ChecklistResult:
        return ChecklistResult(
            passed=False,
            rules_ok=rules_ok,
            consensus_ok=consensus_ok,
            sum_of_prices_ok=False,
            stability_ok=False,
            edge_ok=False,
            consensus_temp=consensus_temp,
            rejection_reason=reason,
            bin_volumes_snapshot=bin_volumes_snapshot,
            consensus_offset=consensus_offset,
        )

    # ── 1. Rules ──────────────────────────────────────────────────────
    rules_ok = bool(group.weather_station)
    if not rules_ok:
        return _reject("no_weather_station")

    # ── 2. Consensus ──────────────────────────────────────────────────
    if len(forecasts) < ss.strategy_min_model_agreement:
        return _reject(
            f"not_enough_models:{len(forecasts)}/{ss.strategy_min_model_agreement}",
            rules_ok=True,
        )

    if group.metric == "high_temp":
        raw_temps = [f.temp_forecast_max for f in forecasts if f.temp_forecast_max is not None]
    elif group.metric == "low_temp":
        raw_temps = [f.temp_forecast_min for f in forecasts if f.temp_forecast_min is not None]
    else:
        return _reject("precipitation_not_supported_yet", rules_ok=True)

    if len(raw_temps) < ss.strategy_min_model_agreement:
        return _reject("missing_model_data", rules_ok=True)

    spread_c = max(raw_temps) - min(raw_temps)
    consensus_ok = spread_c <= ss.strategy_max_model_disagreement_c
    consensus_temp_c = statistics.mean(raw_temps)
    consensus_temp = _c_to_f(consensus_temp_c) if (group.unit or "F") == "F" else consensus_temp_c

    if not consensus_ok:
        return _reject(
            f"consensus_spread={spread_c:.2f}°C>{ss.strategy_max_model_disagreement_c}°C",
            rules_ok=True,
            consensus_temp=consensus_temp,
        )

    # ── 3. Build basket ───────────────────────────────────────────────
    basket = build_basket(markets, consensus_temp, prices, group.unit or "F")
    if not basket:
        return ChecklistResult(
            passed=False,
            rules_ok=True,
            consensus_ok=True,
            sum_of_prices_ok=False,
            stability_ok=True,
            edge_ok=False,
            consensus_temp=consensus_temp,
            rejection_reason="temp_outside_market_range",
        )

    # Collect volumes snapshot for rejected_markets logging
    bin_volumes_snapshot = {
        item.market.market_id: _safe_float(getattr(item.market, "volume", None))
        for item in basket
    }

    # ── 4a. Cold-start: minimum bin price ─────────────────────────────
    min_bin_price = _safe_float(ss.strategy_min_bin_price) or 0.0
    if min_bin_price > 0:
        for item in basket:
            price = item.price_yes
            if price is not None and price < min_bin_price:
                return _reject(
                    f"cold_start_price:{item.market.bin_label}={price:.4f}<{min_bin_price}",
                    rules_ok=True,
                    consensus_ok=True,
                    consensus_temp=consensus_temp,
                    bin_volumes_snapshot=bin_volumes_snapshot,
                )

    # ── 4b. Minimum bin volume ────────────────────────────────────────
    min_vol = _safe_float(ss.strategy_min_bin_volume_usd) or 0.0
    if min_vol > 0:
        for item in basket:
            vol = _safe_float(getattr(item.market, "volume", None))
            if vol is not None and vol < min_vol:
                return _reject(
                    f"low_volume:{item.market.bin_label}=${vol:.0f}<${min_vol:.0f}",
                    rules_ok=True,
                    consensus_ok=True,
                    consensus_temp=consensus_temp,
                    bin_volumes_snapshot=bin_volumes_snapshot,
                )

    # ── 4c. Market age ────────────────────────────────────────────────
    min_age_h = _safe_float(ss.strategy_min_market_age_hours) or 0.0
    if min_age_h > 0:
        now_dt = datetime.utcnow()
        for item in basket:
            created = getattr(item.market, "created_at", None)
            if created is not None:
                try:
                    age_h = (now_dt - created).total_seconds() / 3600
                    if age_h < min_age_h:
                        return _reject(
                            f"market_too_young:{item.market.bin_label}={age_h:.1f}h<{min_age_h}h",
                            rules_ok=True,
                            consensus_ok=True,
                            consensus_temp=consensus_temp,
                            bin_volumes_snapshot=bin_volumes_snapshot,
                        )
                except (TypeError, AttributeError):
                    pass

    # ── 5. Basket coverage: consensus must be in middle 60% of basket ─
    basket_bin_mins = [item.market.bin_min for item in basket if item.market.bin_min is not None]
    basket_bin_maxes = [
        (item.market.bin_max if item.market.bin_max is not None else item.market.bin_min + 1)
        for item in basket
        if item.market.bin_min is not None
    ]
    if basket_bin_mins and basket_bin_maxes:
        basket_lo = min(basket_bin_mins)
        basket_hi = max(basket_bin_maxes)
        basket_width = basket_hi - basket_lo
        basket_center = (basket_lo + basket_hi) / 2
        consensus_offset = consensus_temp - basket_center

        if basket_width > 0:
            inner_lo = basket_lo + 0.2 * basket_width
            inner_hi = basket_hi - 0.2 * basket_width

            if consensus_temp < basket_lo or consensus_temp > basket_hi:
                return _reject(
                    f"consensus_outside_basket:{consensus_temp:.1f} not in "
                    f"[{basket_lo:.1f},{basket_hi:.1f}]",
                    rules_ok=True,
                    consensus_ok=True,
                    consensus_temp=consensus_temp,
                    consensus_offset=consensus_offset,
                )
            if consensus_temp < inner_lo or consensus_temp > inner_hi:
                return _reject(
                    f"consensus_near_edge:{consensus_temp:.1f} outside "
                    f"inner60%=[{inner_lo:.1f},{inner_hi:.1f}]",
                    rules_ok=True,
                    consensus_ok=True,
                    consensus_temp=consensus_temp,
                    consensus_offset=consensus_offset,
                )
    else:
        basket_center = None
        consensus_offset = None

    # ── 6. Sum of prices ──────────────────────────────────────────────
    basket_prices = [item.price_yes for item in basket if item.price_yes is not None]
    sum_prices = sum(basket_prices)
    sum_ok = sum_prices <= ss.strategy_max_basket_sum

    # ── 7. Stability (same as consensus for MVP) ──────────────────────
    stability_ok = consensus_ok

    # ── 8. Edge: reject both too-low and too-high (cold-start artefact) ──
    edge = calculate_edge(basket_prices)

    max_edge = _safe_float(ss.strategy_max_edge) or 0.0
    if max_edge > 0 and edge > max_edge:
        return ChecklistResult(
            passed=False,
            rules_ok=True,
            consensus_ok=True,
            sum_of_prices_ok=sum_ok,
            stability_ok=True,
            edge_ok=False,
            consensus_temp=consensus_temp,
            sum_of_prices=sum_prices,
            estimated_edge=edge,
            basket=basket,
            rejection_reason=f"edge_too_high:{edge:.1%}>{max_edge:.1%}",
            bin_volumes_snapshot=bin_volumes_snapshot,
            consensus_offset=consensus_offset,
        )

    edge_ok = edge >= (ss.strategy_min_edge_percent / 100.0)

    passed = rules_ok and consensus_ok and sum_ok and stability_ok and edge_ok

    rejection = None
    if not passed:
        if not sum_ok:
            rejection = f"sum_of_prices={sum_prices:.2f}>{ss.strategy_max_basket_sum}"
        elif not edge_ok:
            rejection = f"edge={edge:.1%}<{ss.strategy_min_edge_percent}%"

    return ChecklistResult(
        passed=passed,
        rules_ok=rules_ok,
        consensus_ok=consensus_ok,
        sum_of_prices_ok=sum_ok,
        stability_ok=stability_ok,
        edge_ok=edge_ok,
        consensus_temp=consensus_temp,
        sum_of_prices=sum_prices,
        estimated_edge=edge,
        basket=basket,
        rejection_reason=rejection,
        bin_volumes_snapshot=bin_volumes_snapshot,
        consensus_offset=consensus_offset,
    )
