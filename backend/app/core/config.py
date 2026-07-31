from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "NetGuard"
    ENVIRONMENT: str = "development"
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "change-me-to-a-long-random-string"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    MFA_CHALLENGE_EXPIRE_MINUTES: int = 5
    MFA_ISSUER_NAME: str = "NetGuard"

    # Login lockout (FR-1 / NFR Security): brute-force protection
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    DATABASE_URL: str = "postgresql+psycopg2://netguard:netguard@localhost:5432/netguard"

    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    SLACK_WEBHOOK_URL: str | None = None
    TEAMS_WEBHOOK_URL: str | None = None

    # Risk score thresholds (0-100)
    RISK_LOW_MAX: int = 30
    RISK_MEDIUM_MAX: int = 70

    # Real-Time Health Monitoring (FR-9 / SRS 6.7): "Monitoring window is
    # configurable" -- these two knobs are that configuration. After a
    # deploy, the health suite is polled every POLL_INTERVAL_SECONDS for up
    # to WINDOW_SECONDS (or until a poll fails, which fails fast and
    # triggers rollback immediately rather than waiting out the window).
    HEALTH_MONITOR_WINDOW_SECONDS: int = 60
    HEALTH_MONITOR_POLL_INTERVAL_SECONDS: int = 15

    # Configuration Drift Detection: hour (UTC, 0-23) the nightly
    # automated drift sweep runs at via Celery beat (see app.celery_app).
    DRIFT_SWEEP_HOUR_UTC: int = 2


    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()