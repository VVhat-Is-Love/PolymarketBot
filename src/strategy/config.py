from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StrategySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    strategy_min_volume_usd: float = Field(default=5_000.0)
    strategy_min_edge_percent: float = Field(default=10.0)
    strategy_max_basket_sum: float = Field(default=0.96)
    strategy_min_model_agreement: int = Field(default=3)
    strategy_max_model_disagreement_c: float = Field(default=1.0)
    strategy_basket_neighbors: int = Field(default=1)
    strategy_basket_include_upside: bool = Field(default=True)
    strategy_virtual_stake_usd: float = Field(default=10.0)
    strategy_time_horizon_hours: int = Field(default=48)
    strategy_min_hours_to_close: int = Field(default=6)

    # Cold-start / quality filters (Task 2 + 4)
    strategy_min_bin_volume_usd: float = Field(default=200.0)
    strategy_min_market_age_hours: float = Field(default=2.0)
    strategy_min_bin_price: float = Field(default=0.01)
    strategy_max_edge: float = Field(default=2.0)  # 200% — above this = cold-start artefact

    # Per-bin edge filter (diagnostic only — not used for bin selection)
    min_edge_threshold: float = Field(default=0.05)   # kept for backwards compat
    edge_sigma_c: float = Field(default=1.5)           # Gaussian sigma in °C for diagnostics

    # Basket stake floor — guarantee a minimum entry size per basket before trim-logic.
    # floor = max(min_basket_stake_usd, wallet_balance × min_basket_stake_pct),
    # then capped at MAX_SINGLE_BET_USD so it never exceeds the hard risk limit.
    min_basket_stake_usd: float = Field(default=3.0)   # absolute floor in USD
    min_basket_stake_pct: float = Field(default=0.10)  # pct of live wallet balance

    # -------------------------------------------------------------------------
    # Strategy A — basket_wide (favourite + neighbours, up to basket_max_bins)
    # -------------------------------------------------------------------------
    basket_max_bins: int = Field(default=5)            # max bins in wide basket (basket_builder)
    basket_min_bins: int = Field(default=1)            # min bins required to evaluate EV
    basket_required_edge: float = Field(default=0.05)  # min EV gate: min(P_model,P_mkt) - Σask
    basket_min_market_prob: float = Field(default=0.80) # min normalised market P(win in basket)
    basket_max_cost: float = Field(default=0.95)       # max Σask (cost guard before EV calc)
    bankroll_fraction_per_basket: float = Field(default=0.10)  # wallet fraction per basket
    max_basket_cost_usd: float = Field(default=5.0)    # hard cap on basket cost in USD
    min_order_notional_usd: float = Field(default=1.0) # min USD per individual bin order
    allow_single_favorite: bool = Field(default=False) # single-favourite fallback in decide_basket

    # -------------------------------------------------------------------------
    # Strategy B — basket_narrow (favourite ± 1 neighbour, looser EV gate)
    # -------------------------------------------------------------------------
    basket_narrow_required_edge: float = Field(default=0.03)
    basket_narrow_min_market_prob: float = Field(default=0.65)

    # -------------------------------------------------------------------------
    # Strategy C — tail_no (NO time-decay on longshot bins)
    # -------------------------------------------------------------------------
    tail_max_yes_price: float = Field(default=0.10)    # only bins priced YES ≤ this
    tail_min_yes_price: float = Field(default=0.02)    # ignore dust below this
    tail_required_edge: float = Field(default=0.03)    # min P(NO) - no_ask
    tail_max_hours_to_close: float = Field(default=30.0)  # enter ≤ N hours before close
    tail_min_hours_to_close: float = Field(default=3.0)   # don't enter < N hours (too late)
    tail_kelly_fraction: float = Field(default=0.20)   # fractional Kelly multiplier
    tail_max_position_pct: float = Field(default=0.20) # max single NO position as pct of wallet
    tail_max_position_usd: float = Field(default=12.0) # hard cap per NO position
    tail_max_portfolio_pct: float = Field(default=0.60) # max total NO portfolio as pct of wallet
    tail_sigma_c: float = Field(default=2.0)           # wider Gaussian sigma for tail model (°C)


strategy_settings = StrategySettings()
