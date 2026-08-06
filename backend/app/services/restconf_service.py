"""RESTCONF configuration service (httpx).

Thin JSON/HTTP client for a device's RESTCONF root (Device.restconf_url,
e.g. https://10.0.0.1/restconf). Every call returns a RestconfResult with
enough detail (status code, request/response body, execution time) to be
persisted as a ProtocolOperation audit record.
"""
import json
import time
from dataclasses import dataclass

import httpx

RESTCONF_HEADERS = {
    "Content-Type": "application/yang-data+json",
    "Accept": "application/yang-data+json",
}


@dataclass
class RestconfResult:
    success: bool
    method: str
    url: str
    request_body: str | None
    response_body: str
    http_status: int | None
    execution_time_ms: float
    error: str | None = None


def _request(method: str, url: str, username: str, password: str, json_body: dict | None, timeout: float = 15.0):
    start = time.perf_counter()
    # json.dumps, not str(): str({...}) produces Python repr (single-quoted
    # keys, None/True/False) which is *not* valid JSON and corrupts the
    # audit trail (ProtocolOperation.request_payload) -- anyone replaying
    # or diffing a logged RESTCONF request body needs it to actually be
    # the JSON that was sent on the wire.
    body_str = None if json_body is None else json.dumps(json_body)
    try:
        with httpx.Client(verify=False, timeout=timeout) as client:
            resp = client.request(
                method,
                url,
                json=json_body,
                headers=RESTCONF_HEADERS,
                auth=(username, password),
            )
        elapsed = (time.perf_counter() - start) * 1000
        success = 200 <= resp.status_code < 300
        return RestconfResult(success, method, url, body_str, resp.text, resp.status_code, elapsed)
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - start) * 1000
        return RestconfResult(False, method, url, body_str, "", None, elapsed, error=str(exc))


def get(restconf_base: str, path: str, username: str, password: str) -> RestconfResult:
    return _request("GET", f"{restconf_base.rstrip('/')}/{path.lstrip('/')}", username, password, None)


def patch(restconf_base: str, path: str, username: str, password: str, payload: dict) -> RestconfResult:
    return _request("PATCH", f"{restconf_base.rstrip('/')}/{path.lstrip('/')}", username, password, payload)


def put(restconf_base: str, path: str, username: str, password: str, payload: dict) -> RestconfResult:
    return _request("PUT", f"{restconf_base.rstrip('/')}/{path.lstrip('/')}", username, password, payload)


def delete(restconf_base: str, path: str, username: str, password: str) -> RestconfResult:
    return _request("DELETE", f"{restconf_base.rstrip('/')}/{path.lstrip('/')}", username, password, None)
