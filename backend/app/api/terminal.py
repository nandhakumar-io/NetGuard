import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import nats
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import check_pin_step_up_ws, get_current_user_ws, require_roles
from app.models.device import Device
from app.models.user import User, UserRole
from app.schemas.terminal_job import (
    TERMINAL_OPEN_SUBJECT,
    TERMINAL_RESULT_SUBJECT_PREFIX,
    TERMINAL_SESSION_SUBJECT_PREFIX,
    TerminalControlMessage,
    TerminalOpenRequest,
    TerminalOpenResult,
    TerminalSessionMessage,
    sign,
)
from app.services import (
    audit_service,
    jit_service,
    session_recording_service,
)

router = APIRouter(prefix="/devices", tags=["terminal"])

TERMINAL_OPEN_TIMEOUT_SECONDS = 15.0
TERMINAL_OPEN_TTL_SECONDS = 30

# Interactive shell access to real network gear -- deliberately narrower
# than "any authenticated user". AUDITOR and SECURITY exist to *review*
# device state (RBAC matrix in app/api/rbac.py lists them as read-only
# roles across the fleet), not drive a live CLI session; this endpoint
# used to accept any valid token with no role check at all, which handed
# every auditor account de facto full device control regardless of what
# the rest of the app told them they could do.
#
# Kept in sync with app.device_gateway.validator.TERMINAL_ALLOWED_ROLES,
# which independently re-checks this same thing -- see that module for
# why it's duplicated rather than imported.
TERMINAL_ALLOWED_ROLES = (UserRole.NETWORK_ADMIN, UserRole.NETWORK_ENGINEER, UserRole.NOC_ENGINEER)


DEMO_CANNED_RESPONSES: dict[str, str] = {
    "show version": (
        "Cisco IOS Software, IOS-XE Software\r\n"
        "ROM: Bootstrap program is IOS-XE boot loader\r\n"
        "Uptime is 42 weeks, 3 days, 6 hours, 12 minutes\r\n"
        "System image file is \"flash:packages.conf\"\r\n\r\n"
        "cisco WS-C3850-24T (MIPS) processor with 4194304K bytes of memory.\r\n"
    ),
    "show ip interface brief": (
        "Interface              IP-Address      OK? Method Status                Protocol\r\n"
        "GigabitEthernet0/0     10.10.0.1       YES NVRAM  up                    up\r\n"
        "GigabitEthernet0/1     10.10.1.1       YES NVRAM  up                    up\r\n"
        "GigabitEthernet0/2     unassigned      YES NVRAM  administratively down down\r\n"
    ),
    "show running-config": (
        "! Demo device -- running-config is simulated, not a real device\r\n"
        "hostname demo-core-sw01\r\n"
        "!\r\n"
        "interface GigabitEthernet0/1\r\n"
        " description UPLINK-TO-CORE\r\n"
        " ip address 10.10.1.1 255.255.255.0\r\n"
        "!\r\n"
        "end\r\n"
    ),
    "show cdp neighbors": (
        "Device ID        Local Intrfce     Capability  Platform  Port ID\r\n"
        "demo-dist-sw01    Gig 0/1           R S I       WS-C3850  Gig 1/1\r\n"
        "demo-edge-fw01    Gig 0/2           R S I       ASA5525   Gig 0/0\r\n"
    ),
}
DEMO_HELP_TEXT = (
    "This is a simulated demo session -- not a real device. Try: "
    + ", ".join(f"`{c}`" for c in DEMO_CANNED_RESPONSES)
    + "."
)


async def _run_demo_terminal_session(websocket: WebSocket, device: Device, user: User, db: Session) -> None:
    """Demo Mode's stand-in for a real device session (see
    app.core.demo_mode). Never opens a real network connection --
    everything here is a fixed, canned transcript keyed off a handful of
    common read-only commands, so the public demo can show off the
    Terminal feature without asyncssh ever touching a real device (which
    the demo dataset's fake IPs couldn't reach anyway) and without
    needing real SSH credentials to exist for the demo device at all.
    Read-only by construction: there's no code path here that forwards
    anything to a real device, so there's nothing for command_guard to
    even need to block.
    """
    audit_service.record_event(
        db, actor=user.email, action="Terminal Session Opened (Demo)", result="Started",
        device_hostname=device.hostname,
    )

    prompt = f"{device.hostname}#"
    await websocket.send_text(
        f"\r\n\x1b[36mConnected to {device.hostname} (demo session -- not a real device).\x1b[0m\r\n"
    )
    await websocket.send_text(f"\r\n{DEMO_HELP_TEXT}\r\n\r\n{prompt} ")

    line = ""
    try:
        while True:
            data = await websocket.receive_text()
            for ch in data:
                if ch in ("\r", "\n"):
                    await websocket.send_text("\r\n")
                    command = line.strip().lower()
                    line = ""
                    if not command:
                        pass
                    elif command in ("exit", "quit", "logout"):
                        await websocket.send_text("\r\n\x1b[33mDemo session ended.\x1b[0m\r\n")
                        await websocket.close(code=1000, reason="demo_session_exit")
                        return
                    elif command in DEMO_CANNED_RESPONSES:
                        await websocket.send_text(DEMO_CANNED_RESPONSES[command].replace("\n", "\r\n"))
                    else:
                        await websocket.send_text(
                            f"% Not simulated in this demo.\r\n{DEMO_HELP_TEXT}\r\n"
                        )
                    await websocket.send_text(f"\r\n{prompt} ")
                elif ch in ("\x7f", "\b"):  # backspace/delete
                    if line:
                        line = line[:-1]
                        await websocket.send_text("\b \b")
                else:
                    line += ch
                    await websocket.send_text(ch)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="demo_session_ended")

async def _run_real_terminal_session(
    websocket: WebSocket, device: Device, user: User, db: Session,
    recorder: "session_recording_service.SessionRecorder",
) -> None:
    """Opens a terminal session via the Device Gateway instead of
    connecting to the device directly (Section 3/8/14). The API never
    resolves a device credential or opens a device-facing socket here --
    it publishes a signed, short-lived TerminalOpenRequest, waits for the
    Gateway's independent authorization decision, and then relays raw
    bytes over `terminal.session.<id>.{in,out,ctl}` for the lifetime of
    the session. See app.device_gateway.terminal_executor for the code
    that now actually talks to the device.
    """
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    active_elevation = jit_service.current_active_elevation(db, user.id)

    req = TerminalOpenRequest(
        session_id=session_id,
        tenant_id=str(device.tenant_id),
        device_id=str(device.id),
        requested_by=str(user.id),
        jit_elevation_id=str(active_elevation.id) if active_elevation else None,
        issued_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=TERMINAL_OPEN_TTL_SECONDS)).isoformat(),
    )
    req = sign(req, settings.DEVICE_JOB_SIGNING_KEY)

    nc = await nats.connect(
        servers=[settings.NATS_URL],
        name="netguard-api-terminal-relay",
        user=settings.NATS_API_USER,
        password=settings.NATS_API_PASSWORD,
    )
    try:
        result_sub = await nc.subscribe(f"{TERMINAL_RESULT_SUBJECT_PREFIX}.{session_id}")
        await nc.publish(TERMINAL_OPEN_SUBJECT, req.model_dump_json().encode("utf-8"))
        try:
            msg = await result_sub.next_msg(timeout=TERMINAL_OPEN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await websocket.send_text(
                "\r\n\x1b[31mError: Device Gateway did not respond in time (unavailable, or the "
                "request was rejected during independent validation).\x1b[0m\r\n"
            )
            await websocket.close(code=1011, reason="device_gateway_timeout")
            return
        finally:
            with contextlib.suppress(Exception):
                await result_sub.unsubscribe()

        result = TerminalOpenResult(**json.loads(msg.data.decode("utf-8")))
        if not result.accepted:
            await websocket.send_text(f"\r\n\x1b[31mError: {result.error}\x1b[0m\r\n")
            await websocket.close(code=1008, reason="terminal_open_rejected")
            return

        await websocket.send_text(f"\r\n\x1b[36mInitiating secure shell connection to {device.ip_address}...\x1b[0m\r\n")

        in_subject = f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.in"
        out_subject = f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.out"
        ctl_subject = f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.ctl"

        closed = asyncio.Event()

        async def _on_out(msg):
            try:
                payload = TerminalSessionMessage.model_validate_json(msg.data)
            except Exception:  # noqa: BLE001
                return
            recorder.record_output(payload.data)
            with contextlib.suppress(Exception):
                await websocket.send_text(payload.data)

        async def _on_ctl(msg):
            try:
                payload = TerminalControlMessage.model_validate_json(msg.data)
            except Exception:  # noqa: BLE001
                return
            if payload.event == "connected":
                recorder.set_protocol(payload.detail.split(":", 1)[0] if payload.detail else "unknown")
            elif payload.event in ("error", "ssh_unavailable"):
                with contextlib.suppress(Exception):
                    await websocket.send_text(f"\r\n\x1b[31mError: {payload.detail}\x1b[0m\r\n")
            elif payload.event == "blocked":
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        f"\x1b[41m\x1b[97m *** BLOCKED: '{payload.detail}' is a destructive command and "
                        f"was NOT sent to the device. *** \x1b[0m\r\n"
                    )
                audit_service.record_event(
                    db, actor=user.email, action="Terminal Command Blocked", result=payload.detail,
                    device_hostname=device.hostname,
                )
            elif payload.event == "closed":
                closed.set()

        out_sub = await nc.subscribe(out_subject, cb=_on_out)
        ctl_sub = await nc.subscribe(ctl_subject, cb=_on_ctl)

        try:
            recv_task = asyncio.ensure_future(_pump_ws_to_nats(websocket, nc, in_subject, recorder))
            close_task = asyncio.ensure_future(closed.wait())
            done, pending = await asyncio.wait(
                {recv_task, close_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        finally:
            with contextlib.suppress(Exception):
                await out_sub.unsubscribe()
            with contextlib.suppress(Exception):
                await ctl_sub.unsubscribe()
    finally:
        await nc.close()


async def _pump_ws_to_nats(websocket: WebSocket, nc, in_subject: str, recorder) -> None:
    """Browser -> Gateway. No command_guard call here -- the Gateway
    re-applies the same guard on this exact subject before forwarding
    anything to the device (see terminal_executor._pump_nats_to_device),
    so this loop's only job is to move bytes and record them. Running the
    guard twice would just mean redoing work the Gateway does not trust
    anyway; the authoritative check lives on the Gateway side of the
    trust boundary."""
    try:
        while True:
            data = await websocket.receive_text()
            recorder.record_input(data)
            await nc.publish(in_subject, TerminalSessionMessage(data=data).model_dump_json().encode("utf-8"))
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass


@router.post("/{device_id}/ssh-host-key/reset")
def reset_ssh_host_key(
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(UserRole.NETWORK_ADMIN)),
):
    """Clears a device's pinned SSH host key fingerprint, so the *next*
    terminal connection re-pins whatever key is presented (see
    _PinningSSHClient). Only for deliberate cases -- device re-imaged,
    hardware replaced, host key intentionally rotated -- since clearing
    the pin re-opens the TOFU window until the next connection. Restricted
    to network_admin and audit-logged, same as other admin-surface
    credential/device actions.
    """
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.ssh_host_key_fingerprint = None
    db.commit()

    audit_service.record_event(
        db, actor=current_user.email, tenant_id=current_user.tenant_id, action="SSH Host Key Reset", result="Success",
        device_hostname=device.hostname,
    )
    return {"device_id": str(device.id), "ssh_host_key_fingerprint": None}

@router.websocket("/{device_id}/terminal")
async def device_terminal(
    websocket: WebSocket,
    device_id: uuid.UUID,
    token: str = Query(""),
    pin_token: str = Query(""),
    db: Session = Depends(get_db),
):
    """Interactive device terminal over a WebSocket. NetGuard's own
    identity/authz checks (auth, role, PIN step-up, tenant/device lookup)
    still happen here -- see Section 2, "NetGuard continues to handle...
    terminal authorization". The actual device connection no longer
    happens in this process: _run_real_terminal_session relays to the
    Device Gateway over NATS, which independently re-validates this
    request and is the only component that resolves the device
    credential and opens a socket to the device (Section 3/8/14).
    """
    user = get_current_user_ws(token, db)
    if not user:
        await websocket.close(code=1008)  # Policy Violation
        return
    if user.role not in TERMINAL_ALLOWED_ROLES:
        await websocket.close(code=1008, reason="Role not permitted to open a device terminal")
        return
    if not check_pin_step_up_ws(pin_token or None, user):
        await websocket.close(code=1008, reason="Security PIN verification required")
        return

    await websocket.accept()

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        await websocket.send_text("\r\n\x1b[31mError: Device not found in inventory.\x1b[0m\r\n")
        await websocket.close()
        return

    if not device.ip_address or device.ip_address == "0.0.0.0":
        await websocket.send_text("\r\n\x1b[31mError: Device has no valid IP address assigned. Please sync or bootstrap it.\x1b[0m\r\n")
        await websocket.close()
        return

    if settings.DEMO_MODE:
        await _run_demo_terminal_session(websocket, device, user, db)
        return

    audit_service.record_event(
        db, actor=user.email, action="Terminal Session Opened", result="Started",
        device_hostname=device.hostname,
    )

    active_elevation = jit_service.current_active_elevation(db, user.id)
    recorder = session_recording_service.SessionRecorder(
        db, device_id=device.id, device_hostname=device.hostname,
        user_id=user.id, actor_email=user.email,
        jit_elevation_id=active_elevation.id if active_elevation else None,
    )

    try:
        await _run_real_terminal_session(websocket, device, user, db, recorder)
    except Exception as e:
        with contextlib.suppress(Exception):
            await websocket.send_text(f"\r\n\x1b[31mConnection interrupted: {e!s}\x1b[0m\r\n")
            await websocket.close()
    finally:
        recorder.close(reason="session_ended")
