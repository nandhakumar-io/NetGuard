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

    # Email notifications (FR-11): sent via SMTP using these settings. Email
    # sending is skipped (not an error) whenever SMTP_HOST or
    # NOTIFY_EMAIL_RECIPIENTS is unset, same "optional channel" behavior as
    # the Slack/Teams webhooks above.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_FROM_EMAIL: str = "netguard@localhost"
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT_SECONDS: float = 5.0
    # Comma-separated list of recipient addresses, e.g. "noc@corp.com,oncall@corp.com"
    NOTIFY_EMAIL_RECIPIENTS: str | None = None

    # In-app Notification Center (FR-11): persists every notification event
    # and pushes it over WebSocket (see app.api.notifications). Purely
    # additive/optional -- disabling it does not affect Slack/Teams/Email.
    NOTIFICATIONS_INAPP_ENABLED: bool = True

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

    # SNMP Monitoring (Health Dashboard): how often app.tasks.run_snmp_poll_sweep_task
    # fans out one snmp_poll_task per SNMP-enabled device, and how long
    # DeviceMetric history is retained for the historical charts.
    SNMP_POLL_INTERVAL_SECONDS: int = 60
    SNMP_TIMEOUT_SECONDS: float = 3.0
    SNMP_METRIC_RETENTION_DAYS: int = 30

    # GNS3 Lab Integration: lets change requests be validated end-to-end
    # (deploy -> health monitor -> rollback) against real virtual routers
    # running in a GNS3 topology instead of production hardware. GNS3_ENABLED
    # just toggles whether the /gns3 API surfaces itself as available; the
    # controller URL/credentials point at an already-running GNS3 server
    # (local gns3server process or a GNS3 VM), which this app never starts
    # or manages itself.
    GNS3_ENABLED: bool = False
    GNS3_BASE_URL: str = "http://localhost:3080"
    GNS3_USERNAME: str | None = None
    GNS3_PASSWORD: str | None = None
    GNS3_REQUEST_TIMEOUT_SECONDS: float = 10.0
    GNS3_CONSOLE_READY_TIMEOUT_SECONDS: float = 45.0


    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()