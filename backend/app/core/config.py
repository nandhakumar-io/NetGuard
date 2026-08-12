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
    DEMO_MODE: bool = False
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: str = "change-me-to-a-long-random-string"
    # Fernet key (see app.core.crypto) encrypting SNMP credentials stored
    # in the database (Device.snmp_*_encrypted). Falls back to a fixed
    # dev-only key if unset -- production deployments should set this.
    SECRET_ENCRYPTION_KEY: str | None = None
    SECRET_ENCRYPTION_KEYS: str | None = None
    # Comma-separated list of exact frontend origins allowed to call this
    # API with credentials, e.g. "https://netguard.example.com". Never
    # use "*" here together with allow_credentials=True (see main.py) --
    # that combination is either rejected outright by the browser or, if
    # ever "fixed" by reflecting the request Origin instead of a literal
    # "*", becomes a full credential-theft CSRF hole on every endpoint.
    # Defaults to the Vite dev server origin so local development keeps
    # working without extra setup.
    # http://localhost (no port) covers the docker-compose stack, where
    # the frontend is now reached through Traefik on :80 rather than the
    # Vite dev server's :5173 directly -- see docker/docker-compose.yml.
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost,https://netguard.notoriousdev.in"

    @property
    def cors_allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    def validate_production_secrets(self) -> None:
        """Called once at startup (see app.main lifespan). Refuses to boot
        in production with a secret still set to its dev-only default --
        an unset SECRET_KEY means anyone who has read this open-source
        codebase can forge access tokens for any user; an unset
        SECRET_ENCRYPTION_KEY means every stored SNMP credential is
        encrypted with a key baked into source control. Failing loudly
        here beats failing silently and finding out during an incident.
        """
        if self.ENVIRONMENT != "production":
            return

        problems = []
        if self.SECRET_KEY == "change-me-to-a-long-random-string":
            problems.append("SECRET_KEY is still the default placeholder")
        if not self.SECRET_ENCRYPTION_KEY:
            problems.append("SECRET_ENCRYPTION_KEY is unset (falls back to a key committed in app/core/crypto.py)")
        if self.CORS_ALLOWED_ORIGINS.strip() in ("", "*"):
            problems.append("CORS_ALLOWED_ORIGINS is unset or \"*\" -- set it to your real frontend origin(s)")

        if problems:
            raise RuntimeError(
                "Refusing to start with ENVIRONMENT=production and insecure defaults: "
                + "; ".join(problems)
            )

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    MFA_CHALLENGE_EXPIRE_MINUTES: int = 5
    MFA_ISSUER_NAME: str = "NetGuard"

    # Login lockout (FR-1 / NFR Security): brute-force protection
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    DATABASE_URL: str = "postgresql+psycopg2://netguard:netguard@localhost:5432/netguard"

    # Redundant safety net in app.main's lifespan (see comment there) for
    # local `uvicorn app.main:app` runs that bypass entrypoint.sh. Set to
    # False on every scaled-out container (api replicas, collector, worker)
    # alongside entrypoint.sh's SKIP_MIGRATIONS=true, so N replicas don't
    # also race each other via this fallback path.
    AUTO_MIGRATE_ON_STARTUP: bool = True

    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # Event bus (dashboard/topology/alerts/notifications live-update fan-out).
    # Replaces the old Redis pub/sub channels -- see app/services/event_bus.py.
    NATS_URL: str = "nats://localhost:4222"

    # Time-series store backing the SNMP Health Dashboard (device/interface
    # metrics) -- replaces the old device_metrics/interface_metrics Postgres
    # tables. See app/core/vm_client.py.
    VICTORIAMETRICS_URL: str = "http://localhost:8428"

    SLACK_WEBHOOK_URL: str | None = None
    TEAMS_WEBHOOK_URL: str | None = None

    # Two-way ChatOps (FR: approve/reject a change request, trigger a
    # rollback, query device status from Slack/Teams instead of the UI).
    # SLACK_SIGNING_SECRET verifies every inbound Slack request (slash
    # command + interactive button click) came from Slack, per Slack's
    # request-signing scheme -- required for /chatops/slack/* to accept
    # anything. SLACK_BOT_TOKEN (xoxb-...) is only needed if you want
    # NetGuard to edit the original message in place (e.g. swap the
    # Approve/Reject buttons for "Approved by Priya"); without it,
    # NetGuard still replies, just as a new message via response_url.
    SLACK_SIGNING_SECRET: str | None = None
    SLACK_BOT_TOKEN: str | None = None
    # Shared secret configured on a Microsoft Teams "Outgoing Webhook"
    # connector (Teams calls this the HMAC security token). Every inbound
    # POST /chatops/teams/commands is HMAC-SHA256-verified against it.
    TEAMS_OUTGOING_WEBHOOK_SECRET: str | None = None

    # Telegram Bot API integration (FR-11 extension). TELEGRAM_BOT_TOKEN
    # is the bot token from @BotFather; TELEGRAM_CHAT_ID is the chat/group
    # ID to send notifications to. Both must be set for Telegram delivery
    # to activate. This is the global/env-var-based Telegram fallback;
    # user-created WebhookEndpoint rows of type "telegram" are also
    # supported and are configured per-instance via the /webhooks CRUD API.
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    # NetBox pull-sync (see app.services.netbox_service). Both unset =
    # sync endpoint returns a clear "not configured" error instead of
    # attempting a request with no credentials.
    NETBOX_URL: str | None = None  # e.g. "https://netbox.corp.example.com"
    NETBOX_TOKEN: str | None = None
    NETBOX_VERIFY_SSL: bool = True
    NETBOX_TIMEOUT_SECONDS: float = 15.0

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
    # degrades to "rules" behavior if the configured provider has no
    # credential/isn't reachable or the call fails, so switching this
    # never makes analysis unavailable.
    RISK_ENGINE_BACKEND: str = "rules"

    # Which model provider LLMScorer calls when RISK_ENGINE_BACKEND == "llm".
    # "anthropic" (default, hosted) or "ollama" (any locally-running Ollama
    # server -- e.g. `ollama serve`, reachable at OLLAMA_BASE_URL). Only one
    # provider is active at a time.
    RISK_ENGINE_LLM_PROVIDER: str = "anthropic"
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"

    # Local Ollama server (RISK_ENGINE_LLM_PROVIDER == "ollama"). No API key
    # needed -- Ollama's HTTP API is unauthenticated by default, so this is
    # just where to find it. OLLAMA_BASE_URL defaults to Ollama's standard
    # local port; point it at a remote host if Ollama isn't running on the
    # same machine as the backend (e.g. "http://ollama-host:11434"). Pull
    # OLLAMA_MODEL on that server first (`ollama pull llama3.1`) -- an
    # unpulled model errors out the same as any other LLM-call failure and
    # falls back to the rule-based score.
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1"
    OLLAMA_TIMEOUT_SECONDS: float = 30.0

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

    # Approval workflow SLA timers, in hours, keyed by ChangePriority --
    # how long a change request may sit in PENDING_APPROVAL before the
    # approval queue (GET /change-requests/pending-approvals) flags it as
    # overdue. Emergency changes get the tightest window, Low the loosest.
    APPROVAL_SLA_HOURS: dict[str, float] = {
        "emergency": 1.0,
        "high": 4.0,
        "medium": 24.0,
        "low": 72.0,
    }

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
    # Device reachability (ping) sweep -- separate from and independent of
    # SNMP polling, since Device.status previously only ever got set to
    # ONLINE by the GNS3 lab-import path and was never otherwise touched,
    # leaving every manually-added ("non-lab") device stuck at UNKNOWN
    # forever regardless of whether it was actually reachable.
    REACHABILITY_POLL_INTERVAL_SECONDS: int = 60

    # Escalation Policies: how often app.tasks.run_escalation_sweep_task
    # scans unacknowledged alerts and escalates any that have breached an
    # enabled EscalationPolicy's unack_minutes threshold. Kept short (well
    # under the smallest realistic unack_minutes) so an escalation fires
    # close to the threshold instead of up to a full sweep interval late.
    ESCALATION_SWEEP_INTERVAL_SECONDS: int = 60

    # GitOps (config-as-code): how often the periodic safety-net re-pull
    # (app.tasks.run_gitops_auto_sync_sweep_task) runs, independent of any
    # webhook. Webhook-triggered syncs happen immediately regardless of
    # this interval.
    GITOPS_AUTO_SYNC_INTERVAL_SECONDS: int = 900
    REACHABILITY_PING_TIMEOUT_SECONDS: float = 1.0
    SNMP_METRIC_RETENTION_DAYS: int = 30

    # Discovery at Scale: rather than firing every due device's poll task
    # at the exact same instant every sweep tick (fine for a handful of
    # devices, a thundering herd of simultaneous SNMP walks / ICMP bursts
    # against the management network at hundreds), each task in the sweep
    # is given a random `countdown` spread across this window -- see
    # app.tasks.run_snmp_poll_sweep_task / run_reachability_sweep_task.
    # Kept well under the poll interval itself so devices still end up
    # polled roughly on cadence, just not all in the same second.
    SNMP_POLL_JITTER_SECONDS: int = 20
    REACHABILITY_POLL_JITTER_SECONDS: int = 15

    # When true, the FastAPI process itself runs a lightweight asyncio loop
    # that polls every SNMP-enabled device every SNMP_POLL_INTERVAL_SECONDS
    # -- functionally the same as app.tasks.run_snmp_poll_sweep_task, but
    # without needing Redis/a Celery worker/beat running. This is what
    # makes SNMP monitoring work out of the box for local dev / demos.
    # Set to False once Celery worker + beat are actually running (e.g. in
    # production) so devices aren't polled twice on the same interval.
    SNMP_INPROCESS_POLLING_ENABLED: bool = True

    # When true, every Celery task (.delay()/.apply_async(), including
    # chain()/chord() used by the multi-device deployment pipeline) runs
    # synchronously in-process the moment it's called, instead of being
    # published to Redis for a separate `celery worker` process to pick
    # up. This is Celery's own supported "eager" mode -- it's what makes
    # approving a change request (or rolling one back) actually deploy
    # something when no Redis/Celery worker is running, which is the
    # normal case for this prototype. Turn it off once a real worker +
    # Redis are deployed, so deploys go through the proper async queue
    # and don't block the approving admin's HTTP request.
    CELERY_TASK_ALWAYS_EAGER: bool = True

    # GNS3 Lab Integration: lets change requests be validated end-to-end
    # (deploy -> health monitor -> rollback) against real virtual routers
    # running in a GNS3 topology instead of production hardware. GNS3_ENABLED
    # just toggles whether the /gns3 API surfaces itself as available; the
    # controller URL/credentials point at an already-running GNS3 server
    # (local gns3server process or a GNS3 VM), which this app never starts
    # or manages itself.
    # Syslog collection (data-completeness: SNMP polling never sees auth
    # failures, hardware fault log lines, or ACL deny hits -- those are
    # syslog-only). Default port 1514, not the standard 514, so this binds
    # without needing root/CAP_NET_BIND_SERVICE in a normal container --
    # point real devices' `logging host` at 1514, or front it with a
    # relay/port-forward to 514 if the deployment needs the standard port.
    # See app.services.syslog_service.start_syslog_listener.
    SYSLOG_LISTENER_ENABLED: bool = True
    SYSLOG_UDP_HOST: str = "0.0.0.0"
    SYSLOG_UDP_PORT: int = 1514
    # How long syslog_messages are retained before the nightly cleanup
    # sweep prunes them -- raw syslog volume is much higher than
    # DeviceMetric rows, so this defaults shorter than
    # SNMP_METRIC_RETENTION_DAYS.
    SYSLOG_RETENTION_DAYS: int = 14

    # NetFlow v5/v9 + IPFIX collection (traffic-flow visibility -- "top
    # talkers", top conversations, protocol mix, and traffic-aware
    # alerting, none of which SNMP polling or syslog can answer since
    # neither carries per-flow src/dst/port/byte-count detail). Default
    # port 2055 is the common NetFlow/IPFIX convention (Cisco default);
    # point device `ip flow-export destination <this host> 2055` /
    # equivalent IPFIX exporter config at it. See
    # app.services.flow_service.start_flow_listener.
    NETFLOW_LISTENER_ENABLED: bool = True
    NETFLOW_UDP_HOST: str = "0.0.0.0"
    NETFLOW_UDP_PORT: int = 2055
    # sFlow uses a distinct well-known port (6343) and datagram format
    # (sampled raw packet headers + interface counters, not NetFlow-style
    # flow records) from the same vendors/devices that may *also* export
    # NetFlow/IPFIX, so it gets its own listener/port rather than sharing
    # NETFLOW_UDP_PORT.
    SFLOW_LISTENER_ENABLED: bool = True
    SFLOW_UDP_HOST: str = "0.0.0.0"
    SFLOW_UDP_PORT: int = 6343
    # Raw flow-record volume is far higher than DeviceMetric/syslog
    # volume (every conversation, not every poll interval), so retention
    # defaults short -- long enough for "what happened this week"
    # top-talker/conversation analysis, not for long-term archival.
    FLOW_RETENTION_DAYS: int = 7

    # How often the in-process loop in app.main captures a
    # TopologySnapshot for historical "what changed in the network graph
    # since <period>" diffing (app.services.topology_service.
    # diff_snapshots). Independent of SNMP_POLL_INTERVAL_SECONDS since a
    # topology snapshot is cheap and doesn't need poll-frequency
    # granularity -- default once/day is enough to answer "since last
    # week" without ballooning topology_snapshots row count.
    TOPOLOGY_SNAPSHOT_ENABLED: bool = True
    TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS: int = 86400

    # Configuration Snapshot retention (Self-Healing Rollback Engine /
    # SRS 10 git-style version control). Snapshots are taken automatically
    # before every deployment/restore/rollback (see pipeline_service,
    # config_management.restore_config, rollback_service) in addition to
    # on-demand backups, so history grows quickly for an active fleet.
    # Policy, enforced by snapshot_service.purge_expired_snapshots (run
    # nightly via Celery beat -- see celery_app.beat_schedule
    # "snapshot-retention-sweep"):
    #   - a snapshot older than SNAPSHOT_RETENTION_DAYS is eligible for
    #     deletion...
    #   - ...unless it's one of the SNAPSHOT_RETENTION_MIN_PER_DEVICE most
    #     recent snapshots for its device (age alone never drops a device
    #     below this floor of restorable history), or it's referenced by
    #     a ChangeRequest.rollback_snapshot_id (so a rollback's own
    #     "what did we restore" audit trail is never invalidated out from
    #     under it).
    # The policy is intentionally visible, not just enforced silently --
    # see GET /snapshots/retention-policy (app.api.config_management) and
    # the "Snapshot Retention" panel on the Backups tab.
    SNAPSHOT_RETENTION_DAYS: int = 90
    SNAPSHOT_RETENTION_MIN_PER_DEVICE: int = 10
    SNAPSHOT_RETENTION_SWEEP_HOUR_UTC: int = 3

    # Terminal command allow/deny-listing (app.api.terminal): lines the
    # user sends to the web terminal are buffered until Enter and checked
    # against TERMINAL_BLOCKED_COMMAND_PATTERNS (case-insensitive regex,
    # matched against the trimmed line) before being forwarded to the
    # device. A match is never sent -- these are destructive/disruptive
    # commands (reload, factory erase, etc.) that should go through the
    # existing Change Request / approval-gated pipeline instead of being
    # run ad hoc from an interactive shell. Set
    # TERMINAL_COMMAND_FILTER_ENABLED=False to disable filtering entirely
    # (e.g. for a lab-only deployment where this would just be friction).
    TERMINAL_COMMAND_FILTER_ENABLED: bool = True
    TERMINAL_BLOCKED_COMMAND_PATTERNS: list[str] = [
        r"^reload\b",
        r"^reload\s+in\b",
        r"^write\s+erase\b",
        r"^erase\s+",
        r"^format\s+",
        r"^delete\s+/force",
        r"^delete\s+/recursive",
        r"^\s*no\s+boot\s+system\b",
        r"^request\s+system\s+reboot\b",
        r"^request\s+system\s+halt\b",
        r"^request\s+vmhost\s+reboot\b",
        r"^system\s+reboot\b",
        r"^system\s+halt\b",
    ]

    GNS3_ENABLED: bool = False
    GNS3_BASE_URL: str = "http://localhost:3080"
    GNS3_USERNAME: str | None = None
    GNS3_PASSWORD: str | None = None
    GNS3_REQUEST_TIMEOUT_SECONDS: float = 10.0
    GNS3_CONSOLE_READY_TIMEOUT_SECONDS: float = 45.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
