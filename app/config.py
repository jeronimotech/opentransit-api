from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = "postgresql://opentransit:opentransit@localhost:5435/opentransit"
    CITIES_DIR: Path = Path("cities")
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

    # Public base URL of the web client, used to build links meant for a person
    # rather than for a client (a shared trip). Per-city override: `share.webBaseUrl`.
    WEB_BASE_URL: str | None = None

    # v1.11 admin accounts. People sign in with an email and a password; ADMIN_TOKEN survives only as a
    # machine credential for CI and scripts, and a deployment can switch it off entirely.
    ADMIN_TOKEN: str = "change-me"
    ADMIN_TOKEN_ENABLED: bool = True
    ADMIN_SESSION_HOURS: int = 12
    # One-shot seed for a fresh deployment: creates the first owner and then refuses to do anything,
    # so the variables can be left in place (or removed) once a real account exists.
    ADMIN_BOOTSTRAP_EMAIL: str | None = None
    ADMIN_BOOTSTRAP_PASSWORD: str | None = None
    ADMIN_BOOTSTRAP_NAME: str = ""

    OTP_TIMEOUT_S: float = 25.0
    PHOTON_TIMEOUT_S: float = 4.0
    # Public Photon instances reject requests carrying a library's default User-Agent
    # (python-httpx/... gets a 403). Always identify the deployment.
    GEOCODER_USER_AGENT: str = "opentransit-api (+https://github.com/jeronimotech/opentransit-api)"


@lru_cache
def settings() -> Settings:
    s = Settings()
    if s.DATABASE_URL.startswith("postgres://"):
        s.DATABASE_URL = s.DATABASE_URL.replace("postgres://", "postgresql://", 1)
    return s
