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
    # Every city gets its own subdomain, so listing origins one by one means a new city
    # is a browser error nobody sees until a person opens the site — which is exactly
    # how toronto.opentransit.tech shipped broken. A pattern covers the ones to come.
    CORS_ORIGIN_REGEX: str | None = None

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

    # v1.12 sign in with Google / Microsoft (OpenID Connect, authorization code + PKCE).
    # Both providers stay off unless their client id *and* secret are set here, per deployment; the
    # login screen only shows a button for a provider that is actually configured. Secrets never live
    # in the repository — see .env.example and docs/DEPLOY-RAILWAY.md.
    OIDC_REDIRECT_BASE: str | None = None      # public origin of the web client; falls back to WEB_BASE_URL
    OIDC_STATE_TTL_SECONDS: int = 600          # how long a started sign-in stays completable
    OIDC_ALLOW_INSECURE_HTTP: bool = False     # localhost development only

    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None

    MICROSOFT_CLIENT_ID: str | None = None
    MICROSOFT_CLIENT_SECRET: str | None = None
    # Your tenant GUID (recommended), a verified domain, or common/organizations/consumers. Anything
    # other than a GUID also needs MICROSOFT_ALLOWED_TENANT_IDS, because Entra signs every tenant on
    # earth with the same keys: without an allowlist, a stranger's tenant could mint a token naming
    # one of your operators. See SECURITY.md.
    MICROSOFT_TENANT: str = ""
    MICROSOFT_ALLOWED_TENANT_IDS: str = ""     # comma-separated tenant GUIDs allowed to sign in

    # OFF BY DEFAULT, AND IT SHOULD STAY THAT WAY. When set, a verified provider email whose domain is
    # listed here creates an account on first sign-in instead of being refused. That hands an admin
    # account to anybody who can get an address at that domain. SECURITY.md spells out the risk.
    OIDC_AUTO_PROVISION_DOMAINS: str = ""
    OIDC_AUTO_PROVISION_ROLE: str = "viewer"
    OIDC_AUTO_PROVISION_CITIES: str = ""       # comma-separated city ids; empty means every city

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
