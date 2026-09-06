from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql://opentransit:opentransit@localhost:5435/opentransit"
    CITIES_DIR: Path = Path("cities")
    ADMIN_TOKEN: str = "change-me"
    CORS_ORIGINS: str = "*"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # Background work. Turn off for tests / read-only deployments.
    ENABLE_RT_POLLERS: bool = True
    ENABLE_STATIC_INGEST: bool = True
    INGEST_STOP_ROUTES: bool = True      # stream stop_times.txt once to learn stop -> routes
    STATIC_INGEST_ON_START: bool = True

    # Realtime memory budget
    VEHICLE_HISTORY_POINTS: int = 60     # ~15 min at 15 s per vehicle, kept in memory
    SIMPLIFY_TOLERANCE: float = 0.00018  # ~20 m Douglas-Peucker for network shapes

    # v1.5 analytics jobs (rollup + partitions + retention)
    ENABLE_ANALYTICS_JOBS: bool = True
    ANALYTICS_ROLLUP_SECONDS: int = 600

    OTP_TIMEOUT_S: float = 25.0
    PHOTON_TIMEOUT_S: float = 4.0


@lru_cache
def settings() -> Settings:
    s = Settings()
    if s.DATABASE_URL.startswith("postgres://"):
        s.DATABASE_URL = s.DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return s
