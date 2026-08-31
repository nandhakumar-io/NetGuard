"""Job envelope for opening an interactive terminal session against a
device, exchanged between the API (publisher) and the Device Gateway
(consumer/executor) over NATS.

This is the streaming counterpart to app.schemas.device_job. The
request/response job model there doesn't fit an interactive shell, so
this module defines a small SESSION protocol instead:

  1. API publishes a signed `TerminalOpenRequest` on `terminal.open`.
  2. Gateway independently validates it (see
     device_gateway/validator.py:validate_terminal_open), resolves the
     device credential itself (the API never sees it), opens the real
     SSH/Telnet connection, and publishes a `TerminalOpenResult` on
     `terminal.result.<session_id>`.
  3. From then on, bytes flow as plain published messages on
     `terminal.session.<session_id>.in` (browser -> device, published by
     the API) and `terminal.session.<session_id>.out` (device -> browser,
     published by the Gateway). `terminal.session.<session_id>.ctl`
     carries session lifecycle signals (closed/error) in either
     direction.

Same signing scheme as device_job.py (HMAC-SHA256 with
DEVICE_JOB_SIGNING_KEY) and the same trust model: the Gateway does not
take the API's word for anything in this envelope -- it re-derives
tenant/device/role/JIT authorization from the database itself.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from pydantic import BaseModel

TERMINAL_OPEN_SUBJECT = "terminal.open"
TERMINAL_RESULT_SUBJECT_PREFIX = "terminal.result"
TERMINAL_SESSION_SUBJECT_PREFIX = "terminal.session"


class TerminalOpenRequest(BaseModel):
    session_id: str  # uuid4, unique per session -- keys all terminal.session.<id>.* subjects
    tenant_id: str
    device_id: str

    requested_by: str  # user id, not email -- stable identifier
    jit_elevation_id: str | None = None

    issued_at: str  # ISO8601 UTC
    expires_at: str  # ISO8601 UTC -- short window to open; the session itself has no separate TTL
    # beyond the Gateway's own idle-timeout once connected (see terminal_executor.py).

    signature: str = ""


class TerminalOpenResult(BaseModel):
    session_id: str
    accepted: bool
    protocol: str | None = None  # "ssh" | "telnet", once known
    error: str | None = None
    banner: str | None = None  # first line to show the user, e.g. "Connected via SSH to 10.0.0.1."


class TerminalSessionMessage(BaseModel):
    """Envelope for both `.in` and `.out` subjects. Not signed -- the
    subject itself is the capability (only the Gateway and the one API
    process that opened this session know the session_id and are allowed
    to publish on it; NATS subject-level ACLs, not this schema, are the
    actual boundary enforcing that -- see nats-server.conf)."""

    data: str


class TerminalControlMessage(BaseModel):
    event: str  # "closed" | "error" | "blocked"
    detail: str = ""


def _canonical_payload(req: TerminalOpenRequest) -> bytes:
    data = req.model_dump(exclude={"signature"})
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(req: TerminalOpenRequest, key: str) -> TerminalOpenRequest:
    mac = hmac.new(key.encode("utf-8"), _canonical_payload(req), hashlib.sha256).hexdigest()
    req.signature = mac
    return req


def verify_signature(req: TerminalOpenRequest, key: str) -> bool:
    if not req.signature:
        return False
    expected = hmac.new(key.encode("utf-8"), _canonical_payload(req), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, req.signature)


def is_expired(req: TerminalOpenRequest, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(req.expires_at)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires
