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
import httpx

from app.core.config import settings


class GNS3Error(Exception):
    """Raised when the GNS3 controller can't be reached or returns an error."""


def _client() -> httpx.Client:
    auth = None
    if settings.GNS3_USERNAME and settings.GNS3_PASSWORD:
        auth = (settings.GNS3_USERNAME, settings.GNS3_PASSWORD)
    return httpx.Client(
        base_url=settings.GNS3_BASE_URL,
        timeout=settings.GNS3_REQUEST_TIMEOUT_SECONDS,
        auth=auth,
    )


def _host() -> str:
    """The address other services should use to reach node consoles --
    same host the controller URL points at, stripped of scheme/port."""
    return httpx.URL(settings.GNS3_BASE_URL).host


def check_status() -> dict:
    """Ping the controller (GET /v2/version). Raises GNS3Error if the
    server is unreachable or GNS3 integration is disabled.
    """
    if not settings.GNS3_ENABLED:
        raise GNS3Error("GNS3 integration is disabled (set GNS3_ENABLED=true in .env).")
    try:
        with _client() as client:
            resp = client.get("/v2/version")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Could not reach GNS3 controller at {settings.GNS3_BASE_URL}: {exc}") from exc


def list_projects() -> list[dict]:
    try:
        with _client() as client:
            resp = client.get("/v2/projects")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to list GNS3 projects: {exc}") from exc


def open_project(project_id: str) -> dict:
    """Opens (loads) a project so its nodes can be started."""
    try:
        with _client() as client:
            resp = client.post(f"/v2/projects/{project_id}/open")
            if resp.status_code not in (200, 201, 409):
                resp.raise_for_status()
            get_resp = client.get(f"/v2/projects/{project_id}")
            get_resp.raise_for_status()
            return get_resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to open GNS3 project {project_id}: {exc}") from exc


def list_nodes(project_id: str) -> list[dict]:
    try:
        with _client() as client:
            resp = client.get(f"/v2/projects/{project_id}/nodes")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to list nodes for GNS3 project {project_id}: {exc}") from exc


def get_node(project_id: str, node_id: str) -> dict:
    try:
        with _client() as client:
            resp = client.get(f"/v2/projects/{project_id}/nodes/{node_id}")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to fetch GNS3 node {node_id}: {exc}") from exc


def start_node(project_id: str, node_id: str) -> dict:
    try:
        with _client() as client:
            resp = client.post(f"/v2/projects/{project_id}/nodes/{node_id}/start")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise GNS3Error(f"Failed to start GNS3 node {node_id}: {exc}") from exc


def stop_node(project_id: str, node_id: str) -> dict:
    try:
        with _client() as client:
            resp = client.post(f"/v2/projects/{project_id}/nodes/{node_id}/stop")
            resp.raise_for_status()
            return resp.json()
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


def node_console_info(node: dict) -> tuple[str | None, int | None, str]:
    """Returns (console_host, console_port, console_type) for a GNS3 node."""
    console_type = (node.get("console_type") or "none").lower()
    port = node.get("console")
    return (_host() if port else None, port, console_type)