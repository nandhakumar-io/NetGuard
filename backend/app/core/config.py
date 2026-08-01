from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# pydantic-settings' env_file="./.env" below only populates the declared
# fields on the Settings class -- it's a self-contained mechanism and does
# NOT set anything into os.environ. Several places in the codebase (notably
# app.services.credential_service, which resolves NETGUARD_CRED_* secret-store
# refs) read os.environ directly, bypassing Settings entirely, so without
# this call those lookups silently miss anything that only lives in .env.
# This module is imported first by both the FastAPI process (app.main) and
# the Celery worker/beat process (app.celery_app), so loading it here covers
# both rather than just the API process.
load_dotenv()


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

    # AI Configuration Analyzer backend (SRS 6.2 / FR-6): "rules" is the
    # deterministic v1 scorer (regex + network-aware checks). "llm" layers
    # an optional model-backed review on top of the same deterministic
    # checks -- see app.services.risk_engine.LLMScorer -- and silently
    # degrades to "rules" behavior if ANTHROPIC_API_KEY is unset or the
    # call fails, so switching this never makes analysis unavailable.
    RISK_ENGINE_BACKEND: str = "rules"
    ANTHROPIC_API_KEY: str | None = None

    # Critical Risk changes (score > RISK_MEDIUM_MAX) require a second,
    # distinct Network Administrator approval before deployment is
    # enqueued (SRS 6.2 / FR-6). See app.api.change_requests.approve_change_request.
    RISK_CRITICAL_DUAL_APPROVAL_ENABLED: bool = True

    # Blast-radius dual approval (SRS 6.2 / FR-6 extension): independent of
    # risk score, a change fanned out to more than this many devices
    # (device_id + additional_device_ids) also requires two distinct
    # Network Administrator approvals -- a Low/Medium Risk change pushed to
    # 50 devices is still high blast-radius even if no single device's diff
    # looks scary. See app.api.change_requests.create_change_request.
    RISK_BLAST_RADIUS_DUAL_APPROVAL_THRESHOLD: int = 5

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

    # Compliance report scheduling (SRS: compliance reporting -- turns the
    # on-demand GET /reports/compliance endpoint into a recurring artifact
    # instead of something someone has to remember to pull). Both run via
    # Celery beat -- see app.celery_app -- and email the rendered report
    # through app.services.notification_service's existing SMTP config to
    # NOTIFY_EMAIL_RECIPIENTS (skipped, not an error, if SMTP isn't
    # configured -- same "optional channel" policy as every other
    # notification). Independently toggleable since some deployments may
    # only want one cadence.
    COMPLIANCE_REPORT_WEEKLY_ENABLED: bool = True
    COMPLIANCE_REPORT_MONTHLY_ENABLED: bool = True
    COMPLIANCE_REPORT_HOUR_UTC: int = 6
    COMPLIANCE_REPORT_WEEKLY_WINDOW_DAYS: int = 7
    COMPLIANCE_REPORT_MONTHLY_WINDOW_DAYS: int = 30

    # Deployment pipeline circuit breaker: a device that fails deployment
    # (FAILED or ROLLED_BACK outcome) this many times in a row -- counting
    # only the latest attempt per distinct ChangeRequest, so Celery infra
    # retries within one CR don't inflate the count -- is auto-flagged
    # unstable and blocked from further automated deploys until a Network
    # Administrator reviews it and clears the flag (POST
    # /devices/{id}/clear-unstable-flag). Protects against a flapping
    # device silently eating retries/rollbacks forever. See
    # app.services.pipeline_service._check_circuit_breaker.
    DEPLOYMENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3

    # SNMP Monitoring (Health Dashboard): how often app.tasks.run_snmp_poll_sweep_task
    # fans out one snmp_poll_task per SNMP-enabled device, and how long
    # DeviceMetric history is retained for the historical charts.
    SNMP_POLL_INTERVAL_SECONDS: int = 60
    SNMP_TIMEOUT_SECONDS: float = 3.0
    SNMP_METRIC_RETENTION_DAYS: int = 30

    # When true, the FastAPI process itself runs a lightweight asyncio loop
    # that polls every SNMP-enabled device every SNMP_POLL_INTERVAL_SECONDS
    # -- functionally the same as app.tasks.run_snmp_poll_sweep_task, but
    # without needing Redis/a Celery worker/beat running. This is what
    # makes SNMP monitoring work out of the box for local dev / demos.
    # Set to False once Celery worker + beat are actually running (e.g. in
    # production) so devices aren't polled twice on the same interval.
    SNMP_INPROCESS_POLLING_ENABLED: bool = True

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

    LOCAL_LLM_BASE_URL: str = "http://localhost:11434/v1"   
    LOCAL_LLM_MODEL: str = "llama3.1:8b"


    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()