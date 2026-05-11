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


strategy_settings = StrategySettings()
