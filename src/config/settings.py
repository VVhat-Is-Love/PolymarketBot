import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Dev/prod split: point at a non-prod env file with
    #   BOT_ENV_FILE=.env.dev   (e.g. NOTIFICATIONS_ENABLED=false, no admin ids)
    # so local development and manual script runs never touch the live Telegram.
    model_config = SettingsConfigDict(
        env_file=os.getenv("BOT_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === STAGE 1 ===
    openweather_api_key: str = Field(default="")
    database_url: str = Field(default="sqlite:///./data/polymarket.db")
    log_level: str = Field(default="INFO")
    log_path: str = Field(default="./logs/bot.log")

    # === STAGE 2 ===
    # (strategy fields live in src/strategy/config.py)

    # === STAGE 3 ===
    polymarket_api_key: str = Field(default="")
    polymarket_api_secret: str = Field(default="")
    polymarket_api_passphrase: str = Field(default="")
    polymarket_host: str = Field(default="https://clob.polymarket.com")

    chain_id: int = Field(default=137)
    polygon_rpc_url: str = Field(default="")
    private_key: str = Field(default="")
    proxy_wallet_address: str = Field(default="")
    eoa_wallet_address: str = Field(default="")

    telegram_bot_token: str = Field(default="")
    admin_telegram_id: str = Field(default="")
    # Master kill-switch for outbound Telegram. Set NOTIFICATIONS_ENABLED=false in
    # a dev/test env to silence the notifier even when a token/admin id is present.
    notifications_enabled: bool = Field(default=True)

    # Risk Management
    max_single_bet_usd: float = Field(default=3.0)
    max_daily_bets: int = Field(default=10)
    max_concurrent_bets: int = Field(default=15)   # legacy; separate caps used below
    daily_loss_limit_usd: float = Field(default=10.0)
    total_stop_loss_usd: float = Field(default=20.0)
    order_timeout_minutes: int = Field(default=5)
    kelly_fraction: float = Field(default=0.25)

    # Separate capital caps per strategy
    max_basket_legs_open: int = Field(default=12)
    max_tail_positions: int = Field(default=3)
    # Equity-based caps (fraction of cash+MTM equity) — replace the absolute USD caps below
    total_deployed_pct: float = Field(default=0.50)     # max total deployed / equity
    tail_deployed_pct: float = Field(default=0.50)      # max tail_no deployed / equity
    max_tail_zone_positions: int = Field(default=3)     # anti-correlation: max tail per weather zone
    # Deprecated absolute caps — kept so existing .env lines don't break on load;
    # no longer enforced in can_place_basket / can_place_tail.
    total_deployed_cap_usd: float = Field(default=40.0)
    basket_max_usd: float = Field(default=26.0)
    tail_max_usd: float = Field(default=18.0)

    trading_mode: str = Field(default="paper")  # 'paper' | 'live'

    # Paper trading bankroll / risk limits (separate virtual budget, never touches live)
    paper_bankroll_usd: float = Field(default=100.0)
    paper_total_stop_loss_usd: float = Field(default=20.0)
    paper_daily_loss_limit_usd: float = Field(default=10.0)
    # Fallback balance if CLOB API balance query fails (Level 1 auth limitation).
    # Set to your actual Polymarket USDC balance so Kelly stake is calculated correctly.
    live_wallet_balance_usd: float = Field(default=0.0)

    # Auto-redeem (G4-17): on-chain redemption of won positions that reached
    # resolution without an early-exit sell (no bid / market closed).
    # Every broadcast is preflight-simulated (eth_call); a malformed call reverts
    # in simulation and is skipped without spending gas.
    auto_redeem_enabled: bool = Field(default=True)
    redeem_min_matic: float = Field(default=0.02)   # skip if gas balance below this

    # On-chain position import (G4-26): reconcile the DB from /activity as the
    # source of truth for position EXISTENCE. Classification (open vs
    # pending_redeem vs skip) was eye-checked against the Polymarket UI and is
    # now LIVE (writes applied). The job is idempotent — it reuses the canonical
    # row per token and skips redeemed/orphan tokens — so it runs safely every
    # cycle. Set ONCHAIN_IMPORT_DRY_RUN=true to revert to log-only.
    onchain_import_dry_run: bool = Field(default=False)
    onchain_import_min_usd: float = Field(default=1.0)  # below this = noise, skip
    # Polymarket Polygon contracts (mainnet)
    ctf_address: str = Field(default="0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
    neg_risk_adapter_address: str = Field(default="0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
    usdc_collateral_address: str = Field(default="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

    # Order entry price mode (G2-3)
    # 'ask' = best_ask + entry_tick (marketable limit, higher fill rate)
    # 'mid' = passive limit at mid-price (cheaper but may not fill on thin markets)
    entry_price_mode: str = Field(default="ask")
    entry_tick: float = Field(default=0.01)  # added on top of best_ask in 'ask' mode

    # Notification routing (G2-4)
    # Comma-separated Telegram chat IDs for each audience
    paper_notify_chat_ids: str = Field(default="")   # owner only
    live_notify_chat_ids: str = Field(default="")    # owner + client
    client_chat_ids: str = Field(default="")         # client receives only reconciled PnL

    # === STAGE 4+ ===
    anthropic_api_key: str = Field(default="")
    redis_url: str = Field(default="")


    def __repr__(self) -> str:
        d = self.model_dump()
        for secret_field in ("private_key", "polymarket_api_key", "polymarket_api_secret",
                              "polymarket_api_passphrase", "telegram_bot_token", "openweather_api_key"):
            if d.get(secret_field):
                d[secret_field] = "***"
        return f"Settings({d})"


settings = Settings()
