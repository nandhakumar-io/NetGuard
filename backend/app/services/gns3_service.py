"""GNS3 Lab Integration.

Thin REST client for a GNS3 controller (v2/v3 API, both use the same
`/v2/...` paths for the operations used here). This lets NetGuard treat a
GNS3 topology as a disposable, resettable test network: change requests can
be validated end-to-end (risk analysis -> deploy -> health monitor ->
rollback) against real virtual router/switch instances instead of
production hardware.

Design note: this module only talks to the GNS3 *controller* (project/node
lifecycle, console info). It never talks to a node's own management plane
directly -- that's `deployment_engine` / `protocol_manager`'s job, exactly
as it is for physical devices, once a node has been bootstrapped with a
management IP (see `lab_bootstrap_service`). That split is what lets every
other service in this app stay completely unaware that a device is
simulated.
"""
import time

import httpx

from app.core.config import settings


class GNS3Error(Exception):
    """Raised when the GNS3 controller can't be reached or returns an error."""


_cached_token: str | None = None


def _fetch_token() -> str:
    """Always makes a fresh login request -- callers should go through
    `_get_token()`, which caches the result. Kept separate so a forced
    refresh doesn't duplicate the request/validation logic below.
    """
    if not settings.GNS3_USERNAME or not settings.GNS3_PASSWORD:
        raise GNS3Error("GNS3 credentials not set in environment.")

    with httpx.Client(base_url=settings.GNS3_BASE_URL, timeout=settings.GNS3_REQUEST_TIMEOUT_SECONDS) as client:
        resp = client.post(
            "/v3/access/users/login",
            data={"username": settings.GNS3_USERNAME, "password": settings.GNS3_PASSWORD},
        )
        if resp.status_code >= 400:
            raise GNS3Error(f"Failed to authenticate with GNS3 controller: {resp.text}")
        token = resp.json().get("access_token")
        if not token:
            raise GNS3Error(
                "GNS3 controller login succeeded but the response had no access_token "
                "-- check GNS3_USERNAME/GNS3_PASSWORD and that user management is "
                "enabled on the controller."
            )
        return token


def _get_token(force_refresh: bool = False) -> str:
    """Cached token lookup. A permanent, never-invalidated cache used to
    live here -- once a bad or expired token was cached, every call for
    the rest of the process's life reused it and every locked endpoint
    failed forever. `force_refresh` (used by `_request`'s retry-on-401/403
    below) is what actually recovers from that instead of just masking it
    for one lucky request.
    """
    global _cached_token
    if force_refresh:
        _cached_token = None
    if _cached_token:
        return _cached_token
    _cached_token = _fetch_token()
    return _cached_token


def _headers(force_refresh: bool = False) -> dict:
    if not settings.GNS3_USERNAME or not settings.GNS3_PASSWORD:
        return {}
    return {"Authorization": f"Bearer {_get_token(force_refresh=force_refresh)}"}


def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Single entry point every controller call below goes through.

    Retries exactly once, with a freshly-fetched token, if the first
    attempt comes back 401/403. This covers both a genuinely expired
    token (GNS3's JWTs are short-lived) and a controller restart that
    invalidated every previously-issued token -- neither of which the old
    cache-forever `_client()` helper ever recovered from without a full
    backend process restart.
    """
    with httpx.Client(
        base_url=settings.GNS3_BASE_URL,
        timeout=settings.GNS3_REQUEST_TIMEOUT_SECONDS,
        headers=_headers(),
    ) as client:
        resp = client.request(method, path, **kwargs)

    if resp.status_code in (401, 403) and settings.GNS3_USERNAME and settings.GNS3_PASSWORD:
        with httpx.Client(
            base_url=settings.GNS3_BASE_URL,
            timeout=settings.GNS3_REQUEST_TIMEOUT_SECONDS,
            headers=_headers(force_refresh=True),
        ) as client:
            resp = client.request(method, path, **kwargs)

    return resp


def _host() -> str:
    """The address other services should use to reach node consoles --
    same host the controller URL points at, stripped of scheme/port."""
    return httpx.URL(settings.GNS3_BASE_URL).host


def check_status() -> dict:
    """Ping the controller (GET /v3/version). Raises GNS3Error if the
    server is unreachable or GNS3 integration is disabled.
    """
    if not settings.GNS3_ENABLED:
        raise GNS3Error("GNS3 integration is disabled (set GNS3_ENABLED=true in .env).")
    try:
        resp = _request("GET", "/v3/version")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Could not reach GNS3 controller at {settings.GNS3_BASE_URL}: {exc}") from exc


def list_projects() -> list[dict]:
    try:
        resp = _request("GET", "/v3/projects")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to list GNS3 projects: {exc}") from exc


def open_project(project_id: str, wait_seconds: float = 8.0) -> dict:
    """Opens (loads) a project so its nodes can be started.

    Opening is asynchronous on GNS3's side (computes/VMs have to spin up),
    so the POST returning success doesn't mean the project has actually
    flipped to "opened" yet -- a project with more nodes takes longer.
    This polls the project's status for up to `wait_seconds` instead of
    checking once immediately after the POST, which was racing (and
    losing) against larger projects.
    """
    try:
        resp = _request("POST", f"/v3/projects/{project_id}/open", json={})
        if resp.status_code not in (200, 201, 409):
            resp.raise_for_status()

        deadline = time.monotonic() + wait_seconds
        project: dict = {}
        while True:
            get_resp = _request("GET", f"/v3/projects/{project_id}")
            get_resp.raise_for_status()
            project = get_resp.json()
            if (project.get("status") or "").lower() in ("opened", "open"):
                return project
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to open GNS3 project {project_id}: {exc}") from exc

    raise GNS3Error(
        f"GNS3 project {project_id} did not report as opened within {wait_seconds:.0f}s "
        f"(status='{project.get('status')}'). Node actions require an opened project."
    )


def list_nodes(project_id: str) -> list[dict]:
    try:
        resp = _request("GET", f"/v3/projects/{project_id}/nodes")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to list nodes for GNS3 project {project_id}: {exc}") from exc


def get_node(project_id: str, node_id: str) -> dict:
    try:
        resp = _request("GET", f"/v3/projects/{project_id}/nodes/{node_id}")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to fetch GNS3 node {node_id}: {exc}") from exc


def start_node(project_id: str, node_id: str) -> dict:
    """Starts a node, first making sure its project is actually open --
    GNS3 returns 403 for node actions in a closed project, regardless of
    whether the frontend happened to open it earlier in the session (e.g.
    after a page reload, or if a different project was opened since).
    """
    try:
        open_project(project_id)
        resp = _request("POST", f"/v3/projects/{project_id}/nodes/{node_id}/start", json={})
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to start GNS3 node {node_id}: {exc}") from exc


def stop_node(project_id: str, node_id: str) -> dict:
    """Same "make sure the project is open first" guard as start_node --
    a node can only be running (and therefore stoppable) if its project is
    open, but this stays cheap and defensive rather than assuming that.
    """
    try:
        open_project(project_id)
        resp = _request("POST", f"/v3/projects/{project_id}/nodes/{node_id}/stop", json={})
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else {}
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to stop GNS3 node {node_id}: {exc}") from exc


_VENDOR_HINTS: list[tuple[str, str]] = [
    ("cisco", "cisco"), ("ios", "cisco"), ("csr", "cisco"), ("c7200", "cisco"),
    ("juniper", "juniper"), ("junos", "juniper"), ("vmx", "juniper"), ("vsrx", "juniper"),
    ("arista", "arista"), ("veos", "arista"), ("eos", "arista"),
    ("linux", "linux"), ("ubuntu", "linux"), ("alpine", "linux"), ("debian", "linux"),
]


def guess_vendor(node: dict) -> str:
    haystack = f"{node.get('name', '')} {node.get('node_type', '')} {node.get('template_id', '')}".lower()
    for hint, vendor in _VENDOR_HINTS:
        if hint in haystack:
            return vendor
    return "cisco"


def list_links(project_id: str) -> list[dict]:
    """Every link (cable) in a GNS3 project: `{"link_id": ..., "nodes": [
    {"node_id": ..., "adapter_number": ..., "port_number": ..., "label":
    {"text": ...}}, {"node_id": ..., ...}]}` -- exactly two entries per
    link for a point-to-point cable, which is all GNS3 topologies use.
    Used by app.services.topology_service to import GNS3's own
    known-accurate wiring as topology edges for lab devices, instead of
    only inferring adjacency from SNMP LLDP/CDP or shared-subnet guessing
    (which a lab device may not have run Discovery against yet, or may
    have no matching interface IPs for at all).
    """
    try:
        resp = _request("GET", f"/v3/projects/{project_id}/links")
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else []
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to list links for GNS3 project {project_id}: {exc}") from exc


def node_console_info(node: dict) -> tuple[str | None, int | None, str]:
    """Returns (console_host, console_port, console_type) for a GNS3 node."""
    console_type = (node.get("console_type") or "none").lower()
    port = node.get("console")
    return (_host() if port else None, port, console_type)
