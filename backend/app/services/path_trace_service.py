"""Hop-by-hop path/route tracing (NetPath-style).

Two hop sources, tried in order -- see app.models.path_trace.PathTrace's
docstring for the full rationale:

  1. Real `traceroute`/`tracepath` (whichever is present in this runtime),
     parsed into per-hop IP + RTT.
  2. Topology-graph fallback: BFS over the same adjacency graph the
     Topology page already renders (app.services.topology_service,
     built from confirmed LLDP/CDP/GNS3 links), probing each intermediate
     *managed* device with reachability_service.is_reachable() + a
     best-effort ICMP RTT sample.

Fallback (2) is the one that actually works in this app's own runtime in
practice: a locked-down container frequently has neither `traceroute` nor
raw-socket privileges, which is exactly why reachability_service already
treats ICMP as a secondary signal behind a plain TCP connect for
Device.status. Path tracing inherits that same tolerance rather than
just returning "traceroute not available" with nothing else to show.
"""
from __future__ import annotations

import ipaddress
import platform
import re
import shutil
import socket
import subprocess
import uuid
from collections import deque

from sqlalchemy.orm import Session

from app.models.device import Device
from app.models.path_trace import HopStatus, PathHop, PathTrace, PathTraceStatus
from app.services import reachability_service, topology_service

_TRACEROUTE_TIMEOUT_SECONDS = 15
_MAX_HOPS = 30

# A hop's RTT counts as "degraded" (rather than plain "ok") once it's this
# slow, or if it lost part of its probe samples -- same rough threshold
# family as the existing SNMP interface-utilization amber/red bands, kept
# here rather than imported since path-trace RTT and interface-utilization
# percentage aren't the same unit and shouldn't be coupled.
_DEGRADED_RTT_MS = 150.0
_DEGRADED_LOSS_PCT = 20.0


def _hop_status_for(rtt_ms: float | None, loss_pct: float | None) -> HopStatus:
    if rtt_ms is None:
        return HopStatus.TIMEOUT
    if (loss_pct or 0) >= _DEGRADED_LOSS_PCT or rtt_ms >= _DEGRADED_RTT_MS:
        return HopStatus.DEGRADED
    return HopStatus.OK


def _resolve_target_ip(target_input: str) -> str | None:
    try:
        ipaddress.ip_address(target_input)
        return target_input
    except ValueError:
        pass
    try:
        return socket.gethostbyname(target_input)
    except OSError:
        return None


def _icmp_rtt_ms(ip_address: str, timeout: float = 1.0, count: int = 2) -> tuple[float | None, float]:
    """Best-effort average RTT + packet loss % over `count` ICMP pings.
    Returns (None, 100.0) if the `ping` binary is unavailable or every
    probe was lost -- callers treat a None RTT as HopStatus.TIMEOUT,
    exactly like a genuinely silent hop, since this app's runtime can't
    tell "no ping binary" apart from "hop didn't answer" any more
    precisely than that without raw sockets.
    """
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(int(timeout * 1000)) if is_windows else str(max(1, int(timeout)))

    try:
        result = subprocess.run(
            ["ping", count_flag, str(count), timeout_flag, timeout_value, ip_address],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout * count + 3,
            text=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, 100.0

    output = result.stdout or ""
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet)?\s*loss", output)
    loss_pct = float(loss_match.group(1)) if loss_match else (0.0 if result.returncode == 0 else 100.0)

    rtt_match = re.search(r"(?:rtt|round-trip).*=\s*[\d.]+/([\d.]+)/", output)
    if not rtt_match:
        # Windows: "Average = 23ms"
        rtt_match = re.search(r"Average\s*=\s*(\d+)ms", output)
    rtt_ms = float(rtt_match.group(1)) if rtt_match else None

    if loss_pct >= 100.0:
        rtt_ms = None
    return rtt_ms, loss_pct


# --- Method 1: real traceroute -------------------------------------------

_TRACEROUTE_LINE_RE = re.compile(
    r"^\s*(?P<hop>\d+)\s+"
    r"(?:(?P<host>\S+)\s+\((?P<ip>[\d.]+)\)|(?P<ip_only>[\d.]+)|(?P<star>\*))"
    r"(?P<rtts>(?:\s+[\d.]+\s*ms)*)"
)


def _run_traceroute(target_ip: str) -> list[dict] | None:
    """Returns parsed hops from a real traceroute/tracepath run, or None
    if neither binary is present or the run failed outright (caller
    falls back to the topology method in that case)."""
    binary = shutil.which("traceroute") or shutil.which("tracepath")
    if not binary:
        return None

    cmd = [binary, "-n", "-m", str(_MAX_HOPS), target_ip] if "traceroute" in binary else [binary, "-n", target_ip]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=_TRACEROUTE_TIMEOUT_SECONDS, text=True
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    hops: list[dict] = []
    for line in (result.stdout or "").splitlines():
        m = _TRACEROUTE_LINE_RE.match(line)
        if not m:
            continue
        ip = m.group("ip") or m.group("ip_only")
        rtts = [float(x) for x in re.findall(r"([\d.]+)\s*ms", m.group("rtts") or "")]
        avg_rtt = sum(rtts) / len(rtts) if rtts else None
        # A traceroute hop line lists up to 3 probes; missing probes
        # (fewer "ms" tokens than sent) count as partial loss for that hop.
        probes_sent = 3
        loss_pct = round((1 - (len(rtts) / probes_sent)) * 100, 1) if not m.group("star") else 100.0
        hops.append({"hop_index": int(m.group("hop")), "ip_address": ip, "rtt_ms": avg_rtt, "loss_pct": loss_pct})
    return hops or None


# --- Method 2: topology-graph BFS fallback --------------------------------


def _shortest_device_path(db: Session, source_device: Device, target_device: Device) -> list[Device] | None:
    """BFS over the existing topology graph (topology_service.build_topology)
    from source to target device, returning the ordered list of Device
    rows along the shortest confirmed/inferred path, or None if they're
    not connected in the graph this app currently knows about (e.g. no
    LLDP/CDP discovery run yet, or genuinely on separate unlinked
    segments)."""
    graph = topology_service.build_topology(db)
    adjacency: dict[str, set[str]] = {}
    for edge in graph.edges:
        adjacency.setdefault(edge.source, set()).add(edge.target)
        adjacency.setdefault(edge.target, set()).add(edge.source)

    start, goal = str(source_device.id), str(target_device.id)
    if start == goal:
        return [source_device]

    visited = {start}
    queue: deque[list[str]] = deque([[start]])
    while queue:
        path = queue.popleft()
        for neighbor in adjacency.get(path[-1], ()):
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == goal:
                devices_by_id = {str(d.id): d for d in db.query(Device).filter(Device.id.in_(new_path)).all()}
                return [devices_by_id[node_id] for node_id in new_path if node_id in devices_by_id]
            visited.add(neighbor)
            queue.append(new_path)
    return None


def _topology_trace(db: Session, source_device: Device, target_device: Device | None, target_ip: str) -> list[dict]:
    """Builds hops from the topology graph when a real traceroute isn't
    available. If target_device is a managed device reachable via the
    graph, walks that exact device path; otherwise falls back to a
    single "last known hop" entry -- the source device itself -- plus a
    final synthetic hop probing the raw target IP directly, since with
    no graph path to walk there's nothing else this app can claim to
    know about the intermediate hops without fabricating them.
    """
    device_path: list[Device] | None = None
    if target_device is not None:
        device_path = _shortest_device_path(db, source_device, target_device)

    hops: list[dict] = []
    if device_path:
        for idx, device in enumerate(device_path, start=1):
            rtt_ms, loss_pct = _icmp_rtt_ms(device.ip_address)
            if rtt_ms is None and reachability_service.is_reachable(device.ip_address, timeout=1.0):
                # TCP-reachable but ICMP gave nothing (no `ping` binary, or
                # the device answers TCP but not ICMP) -- still a live hop,
                # just without an RTT sample to show.
                loss_pct = 0.0
            hops.append(
                {
                    "hop_index": idx,
                    "ip_address": device.ip_address,
                    "hostname": device.hostname,
                    "device_id": device.id,
                    "rtt_ms": rtt_ms,
                    "loss_pct": loss_pct,
                }
            )
        return hops

    # No graph path (or target isn't a managed device at all) -- this app
    # can only honestly report the source device as hop 1 and the target
    # itself as the final hop, both independently probed. No fabricated
    # intermediate hops.
    rtt_source, loss_source = _icmp_rtt_ms(source_device.ip_address)
    hops.append(
        {
            "hop_index": 1,
            "ip_address": source_device.ip_address,
            "hostname": source_device.hostname,
            "device_id": source_device.id,
            "rtt_ms": rtt_source,
            "loss_pct": loss_source,
        }
    )
    rtt_target, loss_target = _icmp_rtt_ms(target_ip)
    hops.append(
        {
            "hop_index": 2,
            "ip_address": target_ip,
            "hostname": target_device.hostname if target_device else None,
            "device_id": target_device.id if target_device else None,
            "rtt_ms": rtt_target,
            "loss_pct": loss_target,
        }
    )
    return hops


# --- Entry point -----------------------------------------------------------


def run_trace(
    db: Session,
    *,
    source_device_id: uuid.UUID | None,
    target_input: str,
    target_device_id: uuid.UUID | None,
    requested_by: str | None,
) -> PathTrace:
    """Runs one path trace and persists the full result (PathTrace +
    ordered PathHop rows). `target_input` is whatever the operator typed
    (hostname, IP, or picked from the managed-device list) -- kept
    verbatim on the trace even after resolution, so a trace against a
    since-renamed/deleted device is still legible in history.
    """
    source_device = db.get(Device, source_device_id) if source_device_id else None
    target_device = db.get(Device, target_device_id) if target_device_id else None

    target_ip = target_device.ip_address if target_device else _resolve_target_ip(target_input)

    trace = PathTrace(
        source_device_id=source_device.id if source_device else None,
        source_ip=source_device.ip_address if source_device else "unknown",
        target_device_id=target_device.id if target_device else None,
        target_input=target_input,
        target_resolved_ip=target_ip,
        requested_by=requested_by,
    )

    if target_ip is None:
        trace.status = PathTraceStatus.FAILED
        trace.hop_source = "topology"
        trace.total_hops = 0
        db.add(trace)
        db.commit()
        db.refresh(trace)
        return trace

    raw_hops = _run_traceroute(target_ip)
    hop_source = "traceroute"
    device_by_ip = {d.ip_address: d for d in db.query(Device).all()}

    if raw_hops is None:
        hop_source = "topology"
        if source_device is None:
            # No managed source device to anchor the graph walk from, and
            # no traceroute available -- the only thing left to honestly
            # report is a direct probe of the target itself.
            rtt, loss = _icmp_rtt_ms(target_ip)
            raw_hops = [
                {
                    "hop_index": 1,
                    "ip_address": target_ip,
                    "hostname": target_device.hostname if target_device else None,
                    "device_id": target_device.id if target_device else None,
                    "rtt_ms": rtt,
                    "loss_pct": loss,
                }
            ]
        else:
            raw_hops = _topology_trace(db, source_device, target_device, target_ip)

    hop_rows: list[PathHop] = []
    reached_target = False
    for h in raw_hops:
        ip = h.get("ip_address")
        device = h.get("device_id") and db.get(Device, h["device_id"]) or (device_by_ip.get(ip) if ip else None)
        rtt_ms = h.get("rtt_ms")
        loss_pct = h.get("loss_pct")
        status = _hop_status_for(rtt_ms, loss_pct)
        hop_rows.append(
            PathHop(
                hop_index=h["hop_index"],
                ip_address=ip,
                hostname=h.get("hostname") or (device.hostname if device else None),
                device_id=device.id if device else None,
                rtt_ms=rtt_ms,
                packet_loss_pct=loss_pct,
                status=status,
            )
        )
        if ip == target_ip:
            reached_target = True

    trace.hop_source = hop_source
    trace.total_hops = len(hop_rows)
    trace.reached_target = reached_target
    trace.status = (
        PathTraceStatus.COMPLETE
        if reached_target
        else (PathTraceStatus.PARTIAL if hop_rows else PathTraceStatus.FAILED)
    )
    trace.hops = hop_rows

    db.add(trace)
    db.commit()
    db.refresh(trace)
    return trace
