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

    # Late-window entry gate — only enter baskets within N hours of resolution
    # None/0 = disabled (enter at any time within the time horizon)
    basket_entry_window_hours: float = Field(default=0.0)  # 0 = disabled

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
    tail_max_position_pct: float = Field(default=0.20) # max single NO position as pct of wallet (Kelly sizing only)
    tail_max_position_usd: float = Field(default=6.0)  # hard cap per NO position
    # tail_max_portfolio_pct removed — portfolio cap now comes from settings.tail_deployed_pct (equity-based)
    tail_sigma_c: float = Field(default=2.0)           # wider Gaussian sigma for tail model (°C)
    # Interim guards for no_ask approximation (P0-3 / until P1-5 provides real order book)
    tail_min_no_ask: float = Field(default=0.85)       # below = event too likely to be worth it
    tail_max_no_ask: float = Field(default=0.94)       # above = payoff too thin for 100% loss risk
    tail_slippage_buffer: float = Field(default=0.01)  # buffer added on top of approximated ask
    tail_min_model_no: float = Field(default=0.93)    # min P(NO) from Gaussian model for inline tail
    # k·σ distance gate for tail NO entries (run_tail_engine, before placement).
    # Near bin boundary must be ≥ k·σ from model consensus to qualify as a true tail.
    # Prevents entries on bins adjacent to consensus that carry hidden realisation risk
    # (e.g. Austin 94-95°F bin when consensus is 93°F — only 1°F / ~0.6σ away).
    #
    # tail_distance_sigma_f  — σ in °F (C-markets auto-convert).  Start rough at 1.7°F;
    #                          refine with σ-backtest once ≥30 resolved tail trades.
    # tail_k_sigma           — symmetric k (applies below consensus, and above if
    #                          tail_k_sigma_above is 0).  Default 2.0 → gate = 3.4°F.
    # tail_k_sigma_above     — k for bins ABOVE consensus.  0 = use tail_k_sigma.
    #                          US high-temp is right-skewed → above-consensus tails
    #                          are riskier; set > tail_k_sigma to tighten that side.
    tail_distance_sigma_f: float = Field(default=1.7)   # σ in °F (≈ 0.94°C)
    tail_k_sigma: float = Field(default=2.0)             # k below consensus
    tail_k_sigma_above: float = Field(default=0.0)       # k above consensus; 0 = use tail_k_sigma

    # Early exit: when an open NO position's bid reaches this, SELL to lock ~98%
    # profit, free capital, and remove residual resolution risk (vs waiting for REDEEM).
    tail_early_exit_price: float = Field(default=0.985)

    # ── A2: tz-aware tail exit (three branches in check order) ───────────────
    # 1. Take-profit — ANY time: NO-bid ≥ this → SELL at bid (near-par, wrap capital)
    tail_take_profit_price: float = Field(default=0.99)
    # 2. Hard floor — ONLY after the local temperature peak: NO-bid ≤ this →
    #    market-dump SELL. Before the peak a dip is intraday noise that recovers.
    tail_hard_floor_price: float = Field(default=0.45)
    tail_hard_floor_sweep_price: float = Field(default=0.01)  # sweep limit = market dump
    # 3. Time-gate — after the peak AND NO-bid < this → SELL at bid (cut the loser)
    tail_time_gate_price: float = Field(default=0.55)
    # Local hour (city time) at/after which the floor + time-gate arm (post-peak).
    tail_local_peak_hour: int = Field(default=16)
    # Adaptive polling: a position post-peak OR with bid below this is "hot".
    tail_hot_bid_threshold: float = Field(default=0.65)
    tail_poll_hot_minutes: int = Field(default=5)
    tail_poll_normal_minutes: int = Field(default=10)
    # Debounce: require this many consecutive below-threshold polls before
    # time_gate / hard_floor fires (prevents false exits on thin-book bid spikes).
    tail_exit_debounce_polls: int = Field(default=3)
    # Min-position-age: block time_gate / hard_floor for positions younger than
    # this many hours (fresh positions on thin books produce false signals).
    tail_min_position_age_hours: float = Field(default=4.0)


strategy_settings = StrategySettings()
