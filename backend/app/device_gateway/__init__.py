"""The Device Gateway (Section 3 of the hardening spec).

Only this process is expected to hold network-management connectivity
and OpenBao device-credential access. It consumes signed job requests
from NATS (published by app.services.device_job_service, running inside
the API process), independently re-validates every one of them against
the database (see .validator), executes authorized jobs against real
devices (see .executor), and publishes the result back.

Run as: `python -m app.device_gateway.main`
"""
