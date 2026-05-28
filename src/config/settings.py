from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    total_deployed_cap_usd: float = Field(default=40.0)
    basket_max_usd: float = Field(default=26.0)
    tail_max_usd: float = Field(default=18.0)

    trading_mode: str = Field(default="paper")  # 'paper' | 'live'
    # Fallback balance if CLOB API balance query fails (Level 1 auth limitation).
    # Set to your actual Polymarket USDC balance so Kelly stake is calculated correctly.
    live_wallet_balance_usd: float = Field(default=0.0)

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
