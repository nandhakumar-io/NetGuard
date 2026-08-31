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

    # Device-credential-only Fernet key (Section 4 follow-up). Scoped to
    # exactly the six Device SSH/SNMP `*_encrypted` columns -- see
    # app.core.crypto's "Device-credential scope" section for why this is
    # a separate key from SECRET_ENCRYPTION_KEY(S) rather than reusing it.
    # docker-compose.yaml injects this ONLY into `device-gateway` (and
    # `migrate`, transiently, for the one-time re-encryption migration) --
    # never into `api` or the Celery workers.
    DEVICE_CREDENTIAL_ENCRYPTION_KEY: str | None = None
    DEVICE_CREDENTIAL_ENCRYPTION_KEYS: str | None = None

    # HMAC key shared ONLY between the API (publisher) and the Device
    # Gateway (consumer) -- see app.schemas.device_job. Deliberately a
    # separate secret from SECRET_KEY (JWT signing) and
    # SECRET_ENCRYPTION_KEY(S) (device-credential DB encryption): a leak
    # of any one of these three must not automatically compromise the
    # other two. Dev-only fallback below; production must set this via
    # env/secret store (see validate_production_secrets).
    DEVICE_JOB_SIGNING_KEY: str = "change-me-device-job-signing-key"

    # Feature flag for the Phase 3 migration (see app/device_gateway/).
    # When true, migrated API endpoints publish a signed job to the Device
    # Gateway over NATS instead of talking to the device in-process via
    # ProtocolManager.
    # Secure-by-default: route device connectivity through the Device
    # Gateway. Deliberately defaults to True (not False) -- the whole
    # point of the Gateway is that the API/worker processes shouldn't be
    # able to reach devices or decrypt credentials at all, so an
    # operator who forgets to set this should get the safe path, not the
    # legacy in-process one. Set to false only for local dev without a
    # gateway container running.
    DEVICE_GATEWAY_ENABLED: bool = True

    # OpenBao (Section 4). Only ever meaningfully set on the
    # device-gateway container -- see docker-compose.yaml, which does
    # NOT inject OPENBAO_ROLE_ID/OPENBAO_SECRET_ID into the `api`
    # service. Left unset here means "OpenBao not configured in this
    # process", which app.device_gateway.openbao_client.get_client()
    # treats as "fall back to the legacy DB-Fernet credential path" --
    # not an error, so a Gateway can be deployed before OpenBao is fully
    # rolled out.
    OPENBAO_ADDR: str | None = None
    OPENBAO_ROLE_ID: str | None = None
    OPENBAO_SECRET_ID: str | None = None
    OPENBAO_MOUNT: str = "netguard-devices"
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
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost"

    # Threat model T17 (DoS): app-level defaults; a single client (incl. an
    # unauthenticated one hitting /auth/login) previously had no limit at all.
    RATE_LIMIT_DEFAULT: str = "120/minute"
    MAX_REQUEST_BODY_BYTES: int = 10 * 1024 * 1024  # 10 MiB

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
        if self.DEVICE_JOB_SIGNING_KEY == "change-me-device-job-signing-key":
            problems.append("DEVICE_JOB_SIGNING_KEY is still the default placeholder")
        if self.CORS_ALLOWED_ORIGINS.strip() in ("", "*"):
            problems.append("CORS_ALLOWED_ORIGINS is unset or \"*\" -- set it to your real frontend origin(s)")

        if problems:
            raise RuntimeError(
                "Refusing to start with ENVIRONMENT=production and insecure defaults: "
                + "; ".join(problems)
            )

    def validate_device_gateway_secrets(self) -> None:
        """Called once at Device Gateway startup only (see
        app.device_gateway.main) -- deliberately NOT folded into
        validate_production_secrets() above, since that runs in every
        process (api, every Celery worker) via the shared Settings
        object, and DEVICE_CREDENTIAL_ENCRYPTION_KEY is meaningful only
        in the one process meant to hold it. Requiring it globally would
        either force every process to have it (defeating the point of
        scoping it to the Gateway) or silently skip validation
        everywhere (defeating the point of validating it at all)."""
        if self.ENVIRONMENT != "production":
            return
        if not self.DEVICE_CREDENTIAL_ENCRYPTION_KEY and not self.DEVICE_CREDENTIAL_ENCRYPTION_KEYS:
            raise RuntimeError(
                "Refusing to start Device Gateway with ENVIRONMENT=production: "
                "DEVICE_CREDENTIAL_ENCRYPTION_KEY(S) is unset (falls back to a key "
                "committed in app/core/crypto.py)"
            )

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    MFA_CHALLENGE_EXPIRE_MINUTES: int = 5

    # Security PIN step-up (app.core.security.create_pin_step_up_token):
    # how long a single "PIN verified" token is good for before the next
    # terminal open / critical action has to re-verify. Short on purpose --
    # this is a just-in-time proof, not a session.
    PIN_STEP_UP_EXPIRE_MINUTES: int = 5
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

    # Section 5 hardening: the `api` account credentials nats-server.conf
    # grants to the app tier (api + all Celery workers). NATS_URL carries
    # no embedded credentials on purpose -- these are passed to
    # nats.connect(user=..., password=...) explicitly at each call site
    # so they never end up logged in a connection-string dump. Not
    # required (None) so that a plain dev NATS with --auth off, e.g. a
    # bare `nats:2.10-alpine` without nats-server.conf, still works
    # locally without these being set.
    NATS_API_USER: str | None = None
    NATS_API_PASSWORD: str | None = None

    # Section 5: the GATEWAY account nats-server.conf grants to
    # device-gateway only (subscribe jobs.request/terminal.open, publish
    # jobs.result.>/terminal.result.>/terminal.session.*.{out,ctl}).
    # device-gateway's own process is the only one meant to ever read
    # these two values (see device_gateway/main.py) -- deliberately
    # named/scoped separately from NATS_API_* rather than reusing it, so
    # a leaked API .env can never be replayed as gateway credentials.
    NATS_GATEWAY_USER: str | None = None
    NATS_GATEWAY_PASSWORD: str | None = None

    # Time-series store backing the SNMP Health Dashboard (device/interface
    # metrics) -- replaces the old device_metrics/interface_metrics Postgres
    # tables. See app/core/vm_client.py.
    VICTORIAMETRICS_URL: str = "http://localhost:8428"

    SLACK_WEBHOOK_URL: str | None = None

    # Master switch for outbound remote-syslog forwarding (see
    # app.services.syslog_forward_service / app.models.syslog_destination).
    # Per-destination enabled/disabled + severity filtering happens on top
    # of this; this flag exists so forwarding can be killed fleet-wide
    # without touching every configured destination row (e.g. during an
    # incident where a collector is the thing that's down).
    SYSLOG_FORWARDING_ENABLED: bool = True
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

    # Mobile push notifications (see app.services.push_service). Only
    # PUSHOVER_APP_TOKEN is global -- it identifies *NetGuard itself* to
    # Pushover's API and is the same for every user's subscription. NTFY
    # needs no app-level credential since a subscription's `target` is
    # already the full topic URL to POST to (self-hosted ntfy instances
    # work the same way, just with a different host in that URL).
    PUSHOVER_APP_TOKEN: str | None = None

    # Browser push (Web Push API / VAPID -- see app.services.push_service
    # ._send_browser). VAPID_PUBLIC_KEY is handed to the frontend so it can
    # call pushManager.subscribe(); VAPID_PRIVATE_KEY signs outbound pushes
    # server-side and must never be exposed to a client. Both unset =
    # GET /push-subscriptions/vapid-public-key reports the feature as
    # unavailable and the Push Notifications page hides the Browser option,
    # same "degrade gracefully" pattern as the other optional integrations
    # in this file. Generate a pair with `vapid --gen` (py-vapid, a
    # pywebpush dependency) or the equivalent openssl ES256 keypair.
    VAPID_PUBLIC_KEY: str | None = None
    VAPID_PRIVATE_KEY: str | None = None
    # Contact URL/email required by the Web Push protocol's VAPID "sub"
    # claim so a push service operator has a way to reach whoever's
    # sending pushes if something goes wrong.
    VAPID_CONTACT_EMAIL: str = "admin@example.com"

    # NetBox pull-sync (see app.services.netbox_service). Both unset =
    # sync endpoint returns a clear "not configured" error instead of
    # attempting a request with no credentials.
    NETBOX_URL: str | None = None  # e.g. "https://netbox.corp.example.com"
    NETBOX_TOKEN: str | None = None
    NETBOX_VERIFY_SSL: bool = True
    NETBOX_TIMEOUT_SECONDS: float = 15.0

    # NETCONF session connect timeout (app.services.netconf_service._connect).
    # Was hardcoded to 30s -- fine for a single interactive config push,
    # but the Devices/DeviceDetail pages call this synchronously on every
    # config/interface fetch, so a fleet with several NETCONF devices
    # that are slow or unreachable (a device mid-reboot, an ACL blocking
    # port 830, one flaky EX3400) stacks up multiple 30s hangs on what's
    # supposed to be a live page load. 10s is enough for a healthy
    # NETCONF listener to complete the SSH+hello handshake; a device that
    # hasn't responded by then is failing, not just slow, and the
    # Interfaces tab's SNMP fallback (see api.config_management.
    # view_interfaces) means a NETCONF timeout no longer has to mean an
    # empty page for that tab specifically.
    NETCONF_CONNECT_TIMEOUT_SECONDS: float = 10.0

    # RPC reply timeout used for operations issued *after* the session is
    # already up (ncclient's manager.connect(timeout=...) doubles as both
    # the SSH connect timeout above AND the default per-RPC reply timeout
    # for the rest of that session -- see ncclient.manager.Manager.timeout
    # -- so leaving it at NETCONF_CONNECT_TIMEOUT_SECONDS meant every RPC,
    # not just the handshake, got cut off at 10s). <get-config> is
    # comparatively light (it's just handing back provisioned config) and
    # was surviving under 10s in practice, if slowly; Junos's
    # <get-interface-information> operational RPC has to gather live
    # per-interface counters/state for every port on the box, which on an
    # EX3400 (24-48 ports) routinely runs past 10s even on a healthy,
    # otherwise-responsive switch -- see
    # netconf_service.get_junos_interface_information. That RPC (and any
    # other post-connect NETCONF call) now gets this longer budget instead
    # of the connect one.
    NETCONF_OPERATION_TIMEOUT_SECONDS: float = 45.0

    # A device whose SSH/NETCONF daemon is momentarily slow to accept a
    # new session (seen in the field on EX2300 in particular -- a
    # fanless, low-CPU switch that can take a beat to accept a new SSH
    # session under any load) can miss the connect-timeout window above
    # on a first attempt and still be perfectly reachable a moment
    # later. Previously a single missed handshake fell straight through
    # to the Interfaces tab's SNMP fallback (or a hard failure for
    # config push/backup/drift) even though the device was fine --
    # retrying, after a short pause, absorbs that without paying
    # the cost of a much longer single timeout for every device on
    # every call. Set to 0 to disable and preserve the old
    # single-attempt behavior.
    #
    # Bumped 1 -> 2 retries (3 attempts total) after field reports of
    # EX2300s that still missed a *second* back-to-back attempt under
    # sustained management-plane load (e.g. a bulk SNMP poll landing in
    # the same window as a NETCONF fetch). The delay between attempts
    # now also backs off (delay * 2**attempt: 2s, then 4s) instead of
    # retrying at a fixed interval, so a genuinely busy switch gets more
    # room on the later attempts instead of being hit again immediately.
    # See _connect() in netconf_service.py.
    NETCONF_CONNECT_RETRIES: int = 2
    NETCONF_CONNECT_RETRY_DELAY_SECONDS: float = 2.0

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

    # Approval SLA Slack/Teams reminders: fraction of the SLA window
    # remaining at which a "due soon" countdown is posted to the
    # approver's channel (in addition to the always-once "overdue" post).
    # 0.25 = posted once the timer has 25% of its window left.
    APPROVAL_SLA_WARNING_FRACTION: float = 0.25
    APPROVAL_SLA_NOTIFY_SWEEP_INTERVAL_SECONDS: int = 300

    # Daily hour (UTC) at which recurring maintenance schedules
    # (patch Tuesdays, monthly firmware windows) are materialized into
    # concrete MaintenanceWindow rows for the coming horizon.
    RECURRING_WINDOW_GENERATION_HOUR_UTC: int = 1

    # Base URL of the frontend, used to build deep links in Slack/Teams/
    # ntfy messages (approval SLA reminders, war-room links, alert
    # acknowledge/escalate buttons, ...) back to the in-app view of
    # whatever the message is about.
    #
    # MUST be overridden via the FRONTEND_URL env var to your actual
    # public/LAN-reachable frontend URL in any real deployment -- the
    # localhost default below only resolves on the machine running the
    # backend itself. Left as-is, every action-button link sent to ntfy
    # (opened on a phone, on a different network) or Slack/Teams (opened
    # by whoever's reading the channel, not the server) will point at
    # *their* localhost and fail to load. See validate_notification_urls()
    # in app.main for the startup warning that catches this.
    FRONTEND_URL: str = "http://localhost:5173"

    # Public/LAN-reachable base URL of the *backend* API -- distinct from
    # FRONTEND_URL because ntfy's http action buttons (see
    # push_service._ntfy_actions_header) POST straight to the API, not
    # the frontend, and the two are commonly on different hosts/ports
    # (e.g. api.netguard.example.com vs netguard.example.com, or the
    # frontend behind Traefik on :80 while the API stays on :8000).
    # Falls back to FRONTEND_URL's host with the typical dev API port
    # only for local development; MUST be overridden in any real
    # deployment or every "Acknowledge" button ntfy sends will try to
    # reach the phone's own localhost and silently fail.
    API_BASE_URL: str = "http://localhost:8000"

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

    # Weekly Change Request Digest (app.services.change_request_digest) --
    # separate on/off switch and delivery hour from the compliance report
    # above so either can be toggled or retimed independently.
    CHANGE_REQUEST_DIGEST_WEEKLY_ENABLED: bool = True
    CHANGE_REQUEST_DIGEST_HOUR_UTC: int = 7
    CHANGE_REQUEST_DIGEST_WEEKLY_WINDOW_DAYS: int = 7

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
    # Standalone (manually-added, source="manual") wireless AP SNMP poll --
    # see run_standalone_ap_poll_sweep_task / wireless_service.poll_standalone_ap.
    # These APs have no controller to be swept alongside, so they need
    # their own cadence or client_count/SSID data just goes stale forever.
    STANDALONE_AP_POLL_INTERVAL_SECONDS: int = 120
    STANDALONE_AP_POLL_JITTER_SECONDS: int = 15

    # Escalation Policies: how often app.tasks.run_escalation_sweep_task
    # scans unacknowledged alerts and escalates any that have breached an
    # enabled EscalationPolicy's unack_minutes threshold. Kept short (well
    # under the smallest realistic unack_minutes) so an escalation fires
    # close to the threshold instead of up to a full sweep interval late.
    ESCALATION_SWEEP_INTERVAL_SECONDS: int = 60

    # JIT Access: how often app.tasks.run_jit_expiry_notify_sweep_task scans
    # ACTIVE elevations for ones about to lapse / already lapsed. Kept short
    # relative to JIT_EXPIRY_WARNING_MINUTES so the "expiring soon" notice
    # arrives with most of that window still intact, not seconds before.
    JIT_EXPIRY_SWEEP_INTERVAL_SECONDS: int = 60

    # How far ahead of expires_at to fire the "grant expiring soon"
    # notification (see jit_service.sweep_expiry_notifications). 8h is the
    # longest a grant can ever run (jit_service.MAX_DURATION_MINUTES), so 10
    # minutes' notice is proportionate even for the shortest routine grants;
    # dangerous grants are capped at 60m (DANGER_MAX_DURATION_MINUTES) and
    # still get the same flat warning window.
    JIT_EXPIRY_WARNING_MINUTES: int = 10

    # GitOps (config-as-code): how often the periodic safety-net re-pull
    # (app.tasks.run_gitops_auto_sync_sweep_task) runs, independent of any
    # webhook. Webhook-triggered syncs happen immediately regardless of
    # this interval.
    GITOPS_AUTO_SYNC_INTERVAL_SECONDS: int = 900
    REACHABILITY_PING_TIMEOUT_SECONDS: float = 1.0
    SNMP_METRIC_RETENTION_DAYS: int = 30

    # IPAM: how often app.tasks.run_subnet_rescan_sweep_task ticks to
    # check which Subnets are due for an automatic nmap re-scan (per-
    # subnet cadence set via Subnet.auto_rescan_enabled/
    # rescan_interval_hours -- same "beat ticks often, per-entity cadence
    # decides who's actually due" shape as REACHABILITY_POLL_INTERVAL_SECONDS
    # above). Keeps subnet utilization/scanned-host data from silently
    # going stale between someone manually clicking "Scan" on the IPAM page.
    IPAM_RESCAN_SWEEP_INTERVAL_SECONDS: int = 900
    # How often beat checks for due DiscoverySchedule rows -- individual
    # schedules fire on their own interval_minutes cadence (see
    # app.tasks.run_discovery_schedule_sweep_task), this just bounds the
    # worst-case delay between a schedule becoming due and actually firing.
    DISCOVERY_SCHEDULE_SWEEP_INTERVAL_SECONDS: int = 300
    # IPAM: how often app.tasks.run_ipam_conflict_alert_sweep_task calls
    # app.services.ipam_service.fleet_conflicts and raises an alert for
    # any newly-seen conflict, so a duplicate static IP assignment shows
    # up in Alerts / the notification bell instead of only being visible
    # to someone who happens to open the IPAM page.
    IPAM_CONFLICT_ALERT_SWEEP_INTERVAL_SECONDS: int = 300

    # Flow-based traffic alerting (app.services.flow_service.evaluate_flow_alert_rules,
    # AlertRuleMetric.FLOW_TOP_TALKER_BYTES / FLOW_NEW_TALKER): how often
    # app.tasks.run_flow_alert_sweep_task ticks, and the size of the
    # rolling window each tick evaluates rules against. Kept as global
    # settings rather than a per-AlertRule field so the existing Alert
    # Rules schema/UI didn't need a new column just for these two metrics
    # -- every flow rule shares one window, same as every device-poll
    # rule shares one poll cadence.
    FLOW_ALERT_SWEEP_INTERVAL_SECONDS: int = 300
    FLOW_ALERT_WINDOW_MINUTES: int = 60

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
    # without needing Redis/a Celery worker/beat running.    # This is what makes SNMP monitoring work out of the box for local dev / demos.
    # Set to False once Celery worker + beat are actually running (e.g. in
    # production) so devices aren't polled twice on the same interval.
    SNMP_INPROCESS_POLLING_ENABLED: bool = False

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
    CELERY_TASK_ALWAYS_EAGER: bool = False

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

    # SNMP trap receiving -- linkDown/linkUp/coldStart/etc. traps arrive
    # unsolicited, in real time, instead of waiting for the next
    # SNMP_POLL_INTERVAL_SECONDS poll to notice the same thing. This is
    # what gets "port went down" from a worst-case 60s poll-driven delay
    # down to sub-second. Default port 1620, not the standard 162, for
    # the same reason SYSLOG_UDP_PORT isn't 514 -- binds without root/
    # CAP_NET_BIND_SERVICE in a normal container. Point real devices'
    # `snmp-server host <this ip> version 2c <community>` (or the
    # equivalent Junos/EOS trap-target config) at 1620, or front it with
    # a relay/port-forward to 162 if the deployment needs the standard
    # port. See app.services.trap_service.start_trap_listener.
    SNMP_TRAP_LISTENER_ENABLED: bool = True
    SNMP_TRAP_UDP_HOST: str = "0.0.0.0"
    SNMP_TRAP_UDP_PORT: int = 1620
    # Comma-separated list of SNMPv1/v2c community strings this receiver
    # accepts traps under. Deliberately separate from any single
    # device's configured polling community (Device.snmp_version /
    # credential_service) -- fleets very commonly use a shared trap
    # community across every device regardless of each device's own
    # polling credentials, and pysnmp's trap receiver validates the
    # community in the packet itself rather than per-source-device, so
    # there's no clean way to reuse per-device polling creds here
    # anyway. "public" is the near-universal vendor default for trap
    # destinations; add the real one(s) in production.
    SNMP_TRAP_COMMUNITIES: str = "public"

    # --- gNMI streaming telemetry (dial-in, alongside SNMP polling) ---
    # Unlike NetFlow/sFlow above (devices dial *out* to us on a fixed
    # port) gNMI is dial-in: NetGuard opens one long-lived SUBSCRIBE
    # session per gnmi-enabled device (see app.services.gnmi_service),
    # so there's no shared listener port to configure here -- only the
    # supervisor loop's own behavior.
    GNMI_INPROCESS_STREAMING_ENABLED: bool = True
    # Wire-level default for Device.gnmi_sample_interval_ms when a
    # device doesn't override it -- 1s is a large step down from
    # SNMP_POLL_INTERVAL_SECONDS's 60s default without being so tight
    # it floods slower control-plane CPUs on a big interface count.
    GNMI_DEFAULT_SAMPLE_INTERVAL_MS: int = 1000
    # A dropped SUBSCRIBE session (device reboot, TCP reset, transient
    # network blip) reconnects on this backoff rather than busy-looping
    # a TCP+TLS handshake against an unreachable device every second.
    GNMI_RECONNECT_BACKOFF_SECONDS: int = 10
    # How often the supervisor loop re-reads the device table to notice
    # a device newly flagged supports_gnmi=true (or flipped off) without
    # requiring a process restart.
    GNMI_DEVICE_ROSTER_REFRESH_SECONDS: int = 30
    # gnmi_service buffers streamed updates and flushes to
    # InterfaceMetric on this cadence rather than one INSERT per
    # SUBSCRIBE update -- a device streaming at a 1s sample interval
    # across dozens of interfaces would otherwise write far more rows/
    # sec than the Health/Interface pages need to render a smooth
    # sub-minute graph, and far more than the DB needs to absorb.
    GNMI_METRIC_FLUSH_INTERVAL_SECONDS: int = 5
    # Raw flow-record volume is far higher than DeviceMetric/syslog
    # volume (every conversation, not every poll interval), so retention
    # defaults short -- long enough for "what happened this week"
    # top-talker/conversation analysis, not for long-term archival.
    FLOW_RETENTION_DAYS: int = 7

    # How often the in-process loop in app.main captures a
    # TopologySnapshot for historical "what changed in the network graph
    # since <period>" diffing (app.services.topology_service.
    # diff_snapshots), AND raises the topology-change alert (device/link
    # appeared or disappeared) by diffing against the previous snapshot.
    # Used to default to once/day (86400s), which is fine for the
    # historical "since last week" diff view but meant a link/device
    # drop could sit undetected on the Topology page -- and unalerted --
    # for up to 24h, the opposite of "topology should update immediately"
    # (SRS: near-real-time topology change alerting). 30s keeps the graph
    # and its alert current without materially growing
    # topology_snapshots row count (device/link-count rows, not raw
    # metrics).
    TOPOLOGY_SNAPSHOT_ENABLED: bool = True
    TOPOLOGY_SNAPSHOT_INTERVAL_SECONDS: int = 30

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

    # Backup storage (app.services.backup_service): where on-demand/
    # scheduled `pg_dump` backups of NetGuard's own application database
    # are written -- referenced by backup_service and BackupJob.file_path
    # but was missing from Settings entirely (an undeclared attribute
    # access on a pydantic BaseSettings instance raises AttributeError,
    # not a default) -- every backup attempt was failing before it wrote
    # a single byte. Defaults under the app's working directory; point it
    # at a mounted volume in production the same way BACKUP_RETENTION_*
    # below assumes persistent storage across container restarts.
    BACKUP_STORAGE_DIR: str = "./data/backups"
    BACKUP_RETENTION_DAYS: int = 30
    BACKUP_RETENTION_MIN_COUNT: int = 5
    # How many completed backups _prune_old_backups keeps before deleting
    # the oldest. Was referenced by backup_service._prune_old_backups
    # without ever being declared here -- same "undeclared attribute ->
    # AttributeError" trap as BACKUP_STORAGE_DIR above, just not yet
    # caught since pruning only runs after a successful backup.
    BACKUP_RETENTION_COUNT: int = 14
    # Hard ceiling on how long a single pg_dump run is allowed to take
    # before it's killed and the backup marked failed. Also referenced
    # without being declared -- every backup attempt raised AttributeError
    # the moment it reached the subprocess.run() call, before pg_dump even
    # started, which (being neither FileNotFoundError, TimeoutExpired, nor
    # CalledProcessError) escaped run_database_backup's except clauses
    # entirely and left the BackupJob row stuck on status="running"
    # forever with a raw 500 ("Failed to start backup") on the response.
    BACKUP_PGDUMP_TIMEOUT_SECONDS: int = 300

    # Privileged terminal session recording (app.services.
    # session_recording_service, app.api.terminal): every interactive
    # SSH/Telnet device terminal session is transcribed (keystrokes in,
    # device output out, both timestamped) to a JSON-Lines file under
    # this directory, with a TerminalSessionRecording row pointing at it
    # -- see that model's docstring for why (PCI/SOC2 privileged-access
    # session recording, not just start/stop audit log entries). Kept as
    # its own directory rather than reusing BACKUP_STORAGE_DIR since
    # these have a different (typically much longer, compliance-driven)
    # retention policy and access-control surface -- only SECURITY/
    # NETWORK_ADMIN can read recordings back, vs. any admin for DB
    # backups.
    TERMINAL_SESSION_RECORDING_ENABLED: bool = True
    TERMINAL_RECORDING_DIR: str = "./data/terminal-recordings"
    TERMINAL_RECORDING_RETENTION_DAYS: int = 365

    GNS3_ENABLED: bool = False
    GNS3_BASE_URL: str = "http://localhost:3080"
    GNS3_USERNAME: str | None = None
    GNS3_PASSWORD: str | None = None
    GNS3_REQUEST_TIMEOUT_SECONDS: float = 10.0
    GNS3_CONSOLE_READY_TIMEOUT_SECONDS: float = 45.0

    # Google OIDC login (see app.services.sso_service / app.api.sso).
    # Unset GOOGLE_CLIENT_ID = SSO login disabled; the frontend hides the
    # "Sign in with Google" button and the endpoints 404-equivalent
    # (400) rather than silently misconfiguring an OAuth flow with a
    # blank client id.
    GOOGLE_CLIENT_ID: str | None = None
    GOOGLE_CLIENT_SECRET: str | None = None
    # Must exactly match a redirect URI registered in the Google Cloud
    # Console OAuth client, e.g. "https://netguard.example.com/api/v1/sso/google/callback".
    GOOGLE_REDIRECT_URI: str | None = None
    # Optional Google Workspace hosted-domain restriction (`hd` claim) --
    # e.g. "acme.com" so only that org's Workspace accounts can complete
    # login, blocking any personal @gmail.com account from ever reaching
    # the callback even if they somehow guess/share the redirect URI.
    GOOGLE_ALLOWED_HD: str | None = None
    # Role newly-provisioned SSO users get on first login, before any
    # group-to-role mapping below is applied. Deliberately the lowest
    # privilege role, same "never trust the client/IdP with admin by
    # default" posture as local /auth/register's sanitized_role().
    SSO_DEFAULT_ROLE: str = "network_engineer"
    # Google Workspace group email -> NetGuard UserRole, e.g.
    # '{"netguard-admins@acme.com": "network_admin", "netguard-noc@acme.com": "noc_engineer"}'.
    # Requires Workspace Admin SDK / Directory API access (a service
    # account with domain-wide delegation) to resolve a user's group
    # membership -- see sso_service.resolve_role_from_groups. Left as an
    # opt-in JSON blob rather than a first-class settings block because
    # most deployments start with SSO_DEFAULT_ROLE alone and only wire up
    # group mapping once onboarding/offboarding hygiene becomes a
    # requirement.
    SSO_GROUP_ROLE_MAP: str | None = None
    GOOGLE_WORKSPACE_SERVICE_ACCOUNT_JSON: str | None = None
    GOOGLE_WORKSPACE_ADMIN_EMAIL: str | None = None

    # --- Identity provider selection (Section 1) ---
    # "local": email/password + TOTP only (app.api.auth), as today.
    # "oidc": adds Keycloak login (app.api.keycloak_sso) as an available
    # option alongside local auth -- local auth is deliberately never
    # fully disabled by this switch (see Section 1: "preserve existing
    # local authentication as an optional fallback/development mode").
    # AUTH_PROVIDER only controls what the frontend advertises/prefers;
    # it does not gate which login endpoints exist.
    AUTH_PROVIDER: str = "local"

    # Keycloak / generic OIDC login (see app.services.oidc_service /
    # app.api.keycloak_sso). Separate from the Google SSO settings above
    # -- both providers can be enabled at once during a migration window
    # (Section 1: "Google SSO should eventually be migrated behind
    # Keycloak"), each provisioning/linking NetGuard users independently
    # via the same find_or_create_user() used for Google, with
    # provider="keycloak" so the two never collide on sso_subject.
    #
    # OIDC_ISSUER, e.g. "https://auth.example.internal/realms/netguard".
    # JWKS URL, token endpoint, and authorization endpoint are all
    # derived from this at startup via the issuer's
    # /.well-known/openid-configuration document rather than configured
    # individually -- one fewer place for the JWKS URL in particular to
    # drift out of sync with the issuer, which would otherwise let an
    # operator accidentally validate tokens against a stale/wrong key set.
    OIDC_ISSUER: str | None = None
    OIDC_CLIENT_ID: str | None = None
    # Confidential client secret. Kept in addition to PKCE (defense in
    # depth -- see oidc_service.py's docstring on why this deployment
    # uses both rather than treating them as alternatives), never sent
    # to the browser.
    OIDC_CLIENT_SECRET: str | None = None
    # Must exactly match a redirect URI registered on the Keycloak client,
    # e.g. "https://netguard.example.com/api/v1/sso/keycloak/callback".
    OIDC_REDIRECT_URI: str | None = None
    # Expected `aud` claim. Defaults to OIDC_CLIENT_ID (the common case)
    # but is separately configurable because some Keycloak setups issue
    # tokens with a distinct API audience via an audience mapper.
    OIDC_AUDIENCE: str | None = None
    # How long a fetched JWKS key set is trusted before re-fetching, so
    # Keycloak signing-key rotation is picked up automatically without a
    # NetGuard restart, without hitting the JWKS endpoint on every login.
    OIDC_JWKS_CACHE_SECONDS: int = 3600
    OIDC_DEFAULT_ROLE: str = "network_engineer"
    OIDC_GROUP_ROLE_MAP: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
