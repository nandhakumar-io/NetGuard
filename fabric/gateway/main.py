"""Fabric Gateway sidecar service.

Tiny FastAPI application wrapping the Hyperledger Fabric Gateway SDK.
Exposes three JSON endpoints consumed by fabric_gateway_client.py:

    POST /evidence               → CreateEvidence (chaincode)
    GET  /evidence/{id}          → GetEvidence    (chaincode)
    GET  /evidence/{id}/history  → GetEvidenceHistory (chaincode)
    GET  /healthz                → liveness probe

Running in mock mode
--------------------
Set ``FABRIC_MOCK_MODE=true`` (default when FABRIC_PEER_ENDPOINT is absent)
to run without a live Hyperledger Fabric network. The mock stores records
in-process (no persistence) and returns synthesized transaction IDs so every
backend feature works locally without a peer/orderer/CA stack.

Production setup
----------------
Required env vars:

    FABRIC_PEER_ENDPOINT    host:port of the peer to connect to
    FABRIC_MSP_ID           MSP ID of the org the gateway authenticates as
    FABRIC_CHANNEL          channel name (default: netguard-audit-channel)
    FABRIC_CHAINCODE        chaincode name (default: netguard-evidence)
    FABRIC_TLS_CERT_PATH    PEM file: peer TLS CA cert (server auth)
    FABRIC_CERT_PATH        PEM file: client identity / signing cert
    FABRIC_KEY_PATH         PEM file: client private key (never logged)
    FABRIC_GATEWAY_API_KEY  pre-shared secret the backend must send as
                            X-API-Key header (mandatory in production)

Security notes
--------------
* API key comparison uses hmac.compare_digest to prevent timing attacks.
* 500 responses never include the raw exception message in production
  (FABRIC_DEBUG_ERRORS=false by default).
* FABRIC_GATEWAY_API_KEY length is validated at startup; the sidecar refuses
  to start in production (non-mock) mode without a key of at least 32 chars.
* /healthz is NOT API-key protected (it's a liveness probe only and carries
  no data). All other routes require a valid API key.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger("fabric-gateway")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Configuration — read once at startup
# ---------------------------------------------------------------------------

FABRIC_MOCK_MODE: bool = os.environ.get("FABRIC_MOCK_MODE", "true").lower() in ("1", "true", "yes")
FABRIC_PEER_ENDPOINT: str = os.environ.get("FABRIC_PEER_ENDPOINT", "")
FABRIC_MSP_ID: str = os.environ.get("FABRIC_MSP_ID", "Org1MSP")
FABRIC_CHANNEL: str = os.environ.get("FABRIC_CHANNEL", "netguard-audit-channel")
FABRIC_CHAINCODE: str = os.environ.get("FABRIC_CHAINCODE", "netguard-evidence")
FABRIC_TLS_CERT_PATH: str = os.environ.get("FABRIC_TLS_CERT_PATH", "")
FABRIC_CERT_PATH: str = os.environ.get("FABRIC_CERT_PATH", "")
FABRIC_KEY_PATH: str = os.environ.get("FABRIC_KEY_PATH", "")
FABRIC_GATEWAY_API_KEY: str = os.environ.get("FABRIC_GATEWAY_API_KEY", "")
# Set FABRIC_DEBUG_ERRORS=true only in development to expose raw error details in 500s.
FABRIC_DEBUG_ERRORS: bool = os.environ.get("FABRIC_DEBUG_ERRORS", "false").lower() in ("1", "true", "yes")

# Minimum key length enforced for production (real Fabric) mode.
_MIN_API_KEY_LENGTH = 32

# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def _validate_startup() -> None:
    """Validate critical configuration at startup and refuse to start if the
    environment is insecure in production (non-mock) mode."""
    if FABRIC_MOCK_MODE:
        if not FABRIC_GATEWAY_API_KEY:
            logger.warning(
                "FABRIC_GATEWAY_API_KEY is not set — all requests are unauthenticated. "
                "Acceptable in development/mock mode; NEVER do this in production."
            )
        return  # Relaxed checks in mock mode

    # Production mode — hard requirements.
    errors: list[str] = []
    if not FABRIC_GATEWAY_API_KEY:
        errors.append("FABRIC_GATEWAY_API_KEY must be set in production mode")
    elif len(FABRIC_GATEWAY_API_KEY) < _MIN_API_KEY_LENGTH:
        errors.append(
            f"FABRIC_GATEWAY_API_KEY is too short ({len(FABRIC_GATEWAY_API_KEY)} chars); "
            f"minimum is {_MIN_API_KEY_LENGTH} characters"
        )
    if not FABRIC_PEER_ENDPOINT:
        errors.append("FABRIC_PEER_ENDPOINT must be set when FABRIC_MOCK_MODE=false")
    for var, path in (
        ("FABRIC_TLS_CERT_PATH", FABRIC_TLS_CERT_PATH),
        ("FABRIC_CERT_PATH", FABRIC_CERT_PATH),
        ("FABRIC_KEY_PATH", FABRIC_KEY_PATH),
    ):
        if not path:
            errors.append(f"{var} must be set when FABRIC_MOCK_MODE=false")
        elif not os.path.isfile(path):
            errors.append(f"{var}={path!r} does not exist")
    if errors:
        for e in errors:
            logger.critical("Startup validation failed: %s", e)
        raise SystemExit(1)

    logger.info(
        "fabric-gateway: production mode — peer=%s msp=%s channel=%s chaincode=%s",
        FABRIC_PEER_ENDPOINT, FABRIC_MSP_ID, FABRIC_CHANNEL, FABRIC_CHAINCODE,
    )


_validate_startup()

# ---------------------------------------------------------------------------
# In-process mock store (dev / CI use only — not persisted)
# ---------------------------------------------------------------------------

_mock_store: dict[str, dict[str, Any]] = {}          # evidence_id → record
_mock_history: dict[str, list[dict[str, Any]]] = {}  # evidence_id → history entries


class _DuplicateError(Exception):
    def __init__(self, evidence_id: str):
        super().__init__(f"Evidence {evidence_id} already exists on ledger")
        self.evidence_id = evidence_id


def _mock_submit(evidence_id: str, record: dict) -> dict:
    """Store + return a synthetic Fabric transaction response."""
    if evidence_id in _mock_store:
        raise _DuplicateError(evidence_id)
    tx_id = hashlib.sha256(f"{evidence_id}:{uuid.uuid4()}".encode()).hexdigest()
    response: dict[str, Any] = {
        "transaction_id": tx_id,
        "block_number": abs(hash(tx_id)) % 1_000_000,
        "evidence_id": evidence_id,
        "evidence_hash": record.get("evidence_hash", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _mock_store[evidence_id] = {**record, **response}
    _mock_history.setdefault(evidence_id, []).append(
        {
            "tx_id": tx_id,
            "timestamp": response["created_at"],
            "is_delete": False,
            "value": {k: v for k, v in record.items() if k != "evidence_hash"},
        }
    )
    return response


def _mock_get(evidence_id: str) -> dict | None:
    return _mock_store.get(evidence_id)


def _mock_history_get(evidence_id: str) -> list[dict]:
    return _mock_history.get(evidence_id, [])


# ---------------------------------------------------------------------------
# Fabric Gateway (production path)
# ---------------------------------------------------------------------------

_gateway = None


def _get_gateway():
    """Lazily initialise the Fabric Gateway gRPC client.

    Imported lazily so the mock path is usable without the SDK installed.
    """
    global _gateway  # noqa: PLW0603
    if _gateway is not None:
        return _gateway

    try:
        import grpc
        from grpc import ssl_channel_credentials
        from gateway import Gateway, connect  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "fabric-gateway Python package not installed. "
            "Install it or run with FABRIC_MOCK_MODE=true."
        ) from exc

    with open(FABRIC_TLS_CERT_PATH, "rb") as f:
        tls_cert = f.read()
    with open(FABRIC_CERT_PATH, "rb") as f:
        client_cert = f.read()
    with open(FABRIC_KEY_PATH, "rb") as f:
        client_key = f.read()

    credentials = ssl_channel_credentials(
        root_certificates=tls_cert,
        private_key=client_key,
        certificate_chain=client_cert,
    )
    channel = grpc.secure_channel(FABRIC_PEER_ENDPOINT, credentials)
    _gateway = connect(
        msp_id=FABRIC_MSP_ID,
        channel=channel,
        identity={"certificate": client_cert.decode(), "msp_id": FABRIC_MSP_ID},
        signer={"private_key": client_key.decode()},
    )
    return _gateway


def _fabric_submit(record: dict) -> dict:
    """Submit CreateEvidence transaction to the real chaincode."""
    import json
    gw = _get_gateway()
    network = gw.get_network(FABRIC_CHANNEL)
    contract = network.get_contract(FABRIC_CHAINCODE)
    tx = contract.submit("CreateEvidence", arguments=[json.dumps(record)])
    return {
        "transaction_id": tx.transaction_id,
        "block_number": getattr(tx, "block_number", None),
        "evidence_id": record.get("evidence_id"),
        "evidence_hash": record.get("evidence_hash", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _fabric_get(evidence_id: str) -> dict | None:
    """Query GetEvidence from the real chaincode."""
    import json
    gw = _get_gateway()
    contract = gw.get_network(FABRIC_CHANNEL).get_contract(FABRIC_CHAINCODE)
    try:
        return json.loads(contract.evaluate("GetEvidence", arguments=[evidence_id]))
    except Exception as exc:
        if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
            return None
        raise


def _fabric_history(evidence_id: str) -> list[dict]:
    """Query GetEvidenceHistory from the real chaincode."""
    import json
    gw = _get_gateway()
    contract = gw.get_network(FABRIC_CHANNEL).get_contract(FABRIC_CHAINCODE)
    try:
        data = json.loads(contract.evaluate("GetEvidenceHistory", arguments=[evidence_id]))
        return data if isinstance(data, list) else data.get("history", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="NetGuard Fabric Gateway Sidecar",
    # Disable docs/redoc — this is an internal-only service; exposing API
    # docs on an internal sidecar is unnecessary attack surface.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# ------ Authentication -------------------------------------------------------

def _check_api_key(request: Request) -> None:
    """Verify X-API-Key header using a timing-safe comparison.

    Uses hmac.compare_digest instead of == to prevent timing-oracle attacks
    where an attacker probes key prefixes by measuring response latency.

    In development (no key configured): logs a one-time startup warning and
    allows all requests — never allowed in production mode (blocked at startup).
    """
    if not FABRIC_GATEWAY_API_KEY:
        # Already warned at startup. Allow request in mock/dev mode.
        return

    provided = request.headers.get("X-API-Key", "")
    # hmac.compare_digest requires equal types and raises TypeError on empty
    # strings on some Pythons — normalize both sides to bytes.
    if not hmac.compare_digest(
        provided.encode("utf-8"),
        FABRIC_GATEWAY_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


# ------ Error handlers -------------------------------------------------------

@app.exception_handler(Exception)
async def _generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all for any unhandled exception.

    In production (FABRIC_DEBUG_ERRORS=false) we return a generic message to
    avoid leaking internal state (stack traces, file paths, peer addresses).
    Debug mode is only for development.
    """
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    detail = str(exc) if FABRIC_DEBUG_ERRORS else "Internal gateway error"
    return JSONResponse(status_code=500, content={"detail": detail})


# ------ Endpoints ------------------------------------------------------------

@app.get("/healthz")
async def health() -> dict:
    """Liveness probe — intentionally NOT API-key protected.

    Returns mode (mock/production) so operators can verify the sidecar is
    running in the expected mode without a full API key.
    """
    return {"status": "ok", "mock_mode": FABRIC_MOCK_MODE}


@app.post("/evidence", status_code=201)
async def create_evidence(
    payload: dict,
    _: None = Depends(_check_api_key),
) -> dict:
    """Submit a new evidence record to the Fabric ledger (or mock store).

    Returns 201 Created on success.
    Returns 409 Conflict if the evidence_id already exists on the ledger,
    allowing fabric_service to treat it as an idempotent no-op (Section 19).
    Returns 400 Bad Request if evidence_id is missing from the payload.
    Returns 502 Bad Gateway if the peer/chaincode returns an error.
    """
    evidence_id = payload.get("evidence_id")
    if not evidence_id:
        raise HTTPException(status_code=400, detail="evidenceId is required in the payload")

    logger.info("create_evidence: evidence_id=%s mock=%s", evidence_id, FABRIC_MOCK_MODE)

    try:
        if FABRIC_MOCK_MODE:
            result = _mock_submit(evidence_id, payload)
        else:
            result = _fabric_submit(payload)
    except _DuplicateError:
        raise HTTPException(
            status_code=409,
            detail=f"Evidence {evidence_id} already exists on ledger",
        )
    except Exception as exc:
        logger.exception("Ledger submit failed for evidence_id=%s", evidence_id)
        detail = str(exc) if FABRIC_DEBUG_ERRORS else "Ledger submission failed"
        raise HTTPException(status_code=502, detail=detail) from exc

    return result


@app.get("/evidence/{evidence_id}")
async def get_evidence(
    evidence_id: str,
    _: None = Depends(_check_api_key),
) -> dict:
    """Retrieve a single evidence record from the ledger.

    Returns 404 if not found (evidence not yet anchored or sidecar restarted
    between mock submit and this read in dev mode).
    """
    logger.debug("get_evidence: evidence_id=%s", evidence_id)

    try:
        if FABRIC_MOCK_MODE:
            record = _mock_get(evidence_id)
        else:
            record = _fabric_get(evidence_id)
    except Exception as exc:
        logger.exception("Ledger query failed for evidence_id=%s", evidence_id)
        detail = str(exc) if FABRIC_DEBUG_ERRORS else "Ledger query failed"
        raise HTTPException(status_code=502, detail=detail) from exc

    if record is None:
        raise HTTPException(status_code=404, detail=f"Evidence {evidence_id} not found on ledger")
    return record


@app.get("/evidence/{evidence_id}/history")
async def get_evidence_history(
    evidence_id: str,
    _: None = Depends(_check_api_key),
) -> dict:
    """Return the full ledger write-history for an evidence record.

    Always returns a dict with an ``evidence_id`` key and a ``history`` list
    (possibly empty if the record has not yet been anchored).
    """
    logger.debug("get_evidence_history: evidence_id=%s", evidence_id)

    try:
        if FABRIC_MOCK_MODE:
            history = _mock_history_get(evidence_id)
        else:
            history = _fabric_history(evidence_id)
    except Exception as exc:
        logger.exception("History query failed for evidence_id=%s", evidence_id)
        detail = str(exc) if FABRIC_DEBUG_ERRORS else "History query failed"
        raise HTTPException(status_code=502, detail=detail) from exc

    return {"evidence_id": evidence_id, "history": history}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="127.0.0.1",   # bind loopback by default; override with FABRIC_HOST env var
        port=int(os.environ.get("PORT", "9000")),
        log_level="info",
        # Never serve on 0.0.0.0 in production without TLS + API key enforced.
        # The docker-compose service is on netguard-internal only, which is
        # already restricted. For direct CLI runs, loopback is the safe default.
    )
