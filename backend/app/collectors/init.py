"""Standalone single-purpose collector entrypoints.

Each module here (syslog_collector, flow_collector) is a minimal ASGI app
-- NOT app.main's FastAPI app -- exposing only /healthz and /readyz, whose
sole job is to keep one specific UDP listener alive for the process's
whole lifetime. They exist so the syslog and NetFlow/sFlow listeners can
be deployed, scaled, restarted, and (for NetFlow/sFlow, which share
in-memory template state -- see flow_service._TEMPLATES) reasoned about
independently of both the api tier and each other, instead of being one
more thing bundled into app.main's lifespan alongside SNMP polling and
topology snapshots (see the `collector` docker-compose service, which
still owns those two).

Deliberately do NOT import app.main or app.api.router: those pull in
every API route module (and everything each one imports) just to reach
a startup hook these processes don't need, which would make an
already-narrow container noticeably slower to start for no benefit.
"""
