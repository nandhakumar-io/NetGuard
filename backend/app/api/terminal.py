import asyncio
import contextlib
import uuid

import asyncssh
import telnetlib3
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_roles
from app.core.security import decode_access_token
from app.models.device import Device
from app.models.user import User, UserRole
from app.services import audit_service, credential_service

router = APIRouter(prefix="/devices", tags=["terminal"])

SSH_CONNECT_TIMEOUT_SECONDS = 8
TELNET_CONNECT_TIMEOUT_SECONDS = 8


async def read_from_ssh(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    """Pumps device -> browser. Deliberately does NOT close the websocket
    itself on exit (device EOF, SSH channel drop, etc.) -- it used to, but
    that raced with read_from_ws below: if this task closed the socket
    while read_from_ws was mid-await on websocket.receive_text(), Starlette
    raises a bare RuntimeError (not WebSocketDisconnect) for a
    server-initiated close, which read_from_ws's `except Exception` catches
    but still leaves both tasks tearing the connection down independently
    -- one clean, one not -- producing an unclean TCP close that the
    browser reports as a generic 'error' event with no reason instead of a
    graceful 'close'. The caller (_try_ssh) now owns the single close, with
    an explicit reason, after both tasks are done/cancelled.
    """
    try:
        while True:
            data = await process.stdout.read(4096)
            if not data:
                break
            await websocket.send_text(data)
    except (asyncssh.Error, OSError, ConnectionError) as e:
        print(f"SSH stream read error: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"SSH stream read error (unexpected): {e}")


async def read_from_ws(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    try:
        while True:
            data = await websocket.receive_text()
            process.stdin.write(data)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # Starlette raises this (not WebSocketDisconnect) when the socket
        # was already closed from our side rather than by the client --
        # expected once read_from_ssh's loop ends and the caller closes up.
        pass
    except Exception as e:  # noqa: BLE001
        print(f"WebSocket read error: {e}")
    finally:
        with contextlib.suppress(Exception):
            process.stdin.close()


async def read_from_telnet(reader: telnetlib3.TelnetReader, websocket: WebSocket) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await websocket.send_text(data)
    except Exception as e:  # noqa: BLE001
        print(f"Telnet stream read error: {e}")


async def read_from_ws_telnet(writer: telnetlib3.TelnetWriter, websocket: WebSocket) -> None:
    try:
        while True:
            data = await websocket.receive_text()
            writer.write(data)
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"WebSocket read error: {e}")
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _run_pumped_session(
    websocket: WebSocket,
    read_task_coro,
    write_task_coro,
    protocol_label: str,
) -> None:
    """Runs the device-read and browser-write pump tasks together, and is
    the SOLE owner of closing the websocket once the session ends -- for
    whichever reason ends first (device dropped the connection, browser
    disconnected, either pump errored). Using asyncio.wait(FIRST_COMPLETED)
    instead of asyncio.gather means we don't wait on (or get tripped up by)
    the second task once the session is clearly over, and we always send a
    real close frame with a reason instead of letting the socket die
    uncleanly -- which is what the browser was surfacing as the opaque
    'Connection dropped by server' error regardless of the actual cause.
    """
    read_task = asyncio.create_task(read_task_coro)
    write_task = asyncio.create_task(write_task_coro)

    done, pending = await asyncio.wait({read_task, write_task}, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    # Surface why the session ended, if the finished task raised.
    reason = f"{protocol_label} session ended"
    for task in done:
        exc = task.exception() if task.done() and not task.cancelled() else None
        if exc:
            reason = f"{protocol_label} session ended: {exc}"

    with contextlib.suppress(Exception):
        await websocket.send_text(f"\r\n\x1b[33m*** {reason} ***\x1b[0m\r\n")
    with contextlib.suppress(Exception):
        await websocket.close(code=1000, reason=reason[:120])


def get_current_user_ws(token: str, db: Session) -> User | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        email: str | None = payload.get("sub")
        if not email:
            return None
        return db.query(User).filter(User.email == email).first()
    except JWTError:
        return None


class _HostKeyMismatchError(Exception):
    """Raised when a device's presented SSH host key doesn't match the
    fingerprint pinned on first connection -- see _PinningSSHClient."""


class _PinningSSHClient(asyncssh.SSHClient):
    """Trust-on-first-use host key verification.

    Replaces known_hosts=None (which silently accepts *any* host key on
    *every* connection -- meaning an on-path attacker can MITM the
    terminal transparently and capture device credentials + full session
    content). On a device's first terminal connection we pin the
    SHA256 fingerprint of the key it presents; every later connection
    must present the same key or the connection is aborted before any
    credential is sent. A real host key rotation (device re-imaged,
    replaced) requires an explicit reset via
    POST /devices/{id}/ssh-host-key/reset rather than silently
    re-trusting whatever shows up next.
    """

    def __init__(self, device: Device, db: Session):
        self._device = device
        self._db = db

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        fingerprint = key.get_fingerprint("sha256")

        if not self._device.ssh_host_key_fingerprint:
            self._device.ssh_host_key_fingerprint = fingerprint
            self._db.commit()
            return True

        if fingerprint != self._device.ssh_host_key_fingerprint:
            raise _HostKeyMismatchError(
                f"SSH host key for {host} changed since it was first trusted "
                f"(pinned {self._device.ssh_host_key_fingerprint}, presented "
                f"{fingerprint}). Refusing to connect -- this could mean the "
                f"device was legitimately re-imaged, or that traffic is being "
                f"intercepted. If this is expected, clear the pinned key via "
                f"POST /devices/{self._device.id}/ssh-host-key/reset."
            )
        return True


async def _try_ssh(websocket: WebSocket, device: Device, username: str, password: str, db: Session) -> bool:
    """Returns True if an SSH session ran (successfully or not, but far
    enough that telnet should NOT also be attempted -- e.g. the user
    disconnected mid-session). Returns False only for a connection-phase
    failure, which is the signal to fall back to Telnet.
    """
    try:
        async with asyncssh.connect(
            host=device.ip_address,
            username=username,
            password=password,
            known_hosts=None,
            client_factory=lambda: _PinningSSHClient(device, db),
            client_keys=None,
            connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        ) as conn:
            process = await conn.create_process(term_type="xterm-256color", term_size=(120, 40))
            await websocket.send_text(f"\r\n\x1b[32mConnected via SSH to {device.ip_address}.\x1b[0m\r\n")
            await _run_pumped_session(
                websocket,
                read_from_ssh(process, websocket),
                read_from_ws(process, websocket),
                protocol_label="SSH",
            )
        return True
    except _HostKeyMismatchError as exc:
        # Deliberately does NOT fall back to Telnet. If we fell back here,
        # an attacker doing the MITM in the first place could simply block
        # SSH and let the session silently downgrade to unencrypted
        # Telnet, defeating the whole point of pinning. This must be a
        # hard stop the operator has to consciously clear.
        await websocket.send_text(f"\r\n\x1b[31m*** SECURITY WARNING: {exc} ***\x1b[0m\r\n")
        await websocket.close(code=1008, reason="ssh_host_key_mismatch")
        return True
    except (asyncssh.Error, OSError, asyncio.TimeoutError, ConnectionRefusedError) as exc:
        # Connection-phase failure (refused, timed out, no SSH server, auth
        # rejected because the daemon isn't even up yet, etc.). Whether
        # this actually falls back to Telnet is decided by the caller
        # based on device.lab_provider -- see device_terminal -- not here,
        # since this message shouldn't promise a Telnet fallback that
        # won't happen for non-lab devices.
        await websocket.send_text(f"\r\n\x1b[33mSSH unavailable ({exc}).\x1b[0m\r\n")
        return False
    except Exception as exc:  # noqa: BLE001
        # Anything else (e.g. an asyncssh API/kwarg mismatch surfacing as
        # TypeError -- see the pty= kwarg bug this replaced) should still
        # be treated as a connection-phase failure rather than killing the
        # session outright.
        await websocket.send_text(f"\r\n\x1b[33mSSH session failed unexpectedly ({exc}).\x1b[0m\r\n")
        return False


async def _try_telnet(websocket: WebSocket, device: Device, username: str, password: str) -> None:
    # Lab devices (see Device.console_host/console_port) expose their
    # console only via the GNS3 controller's per-node telnet port -- that's
    # the only thing reachable before the node has SSH configured at all.
    # Everything else falls back to the device's own management IP on the
    # standard telnet port.
    if device.console_host and device.console_port:
        host, port = device.console_host, device.console_port
    else:
        host, port = device.ip_address, 23

    try:
        reader, writer = await asyncio.wait_for(
            telnetlib3.open_connection(host, port, term="xterm-256color"),
            timeout=TELNET_CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        await websocket.send_text(
            f"\r\n\x1b[31mTelnet also unavailable ({host}:{port}): {exc}\x1b[0m\r\n"
            f"\r\n\x1b[31mNo reachable terminal protocol for this device.\x1b[0m\r\n"
        )
        await websocket.close()
        return

    await websocket.send_text(
        f"\r\n\x1b[36mConnected via Telnet to {host}:{port}. "
        f"Log in manually below (username: {username}).\x1b[0m\r\n"
    )
    await _run_pumped_session(
        websocket,
        read_from_telnet(reader, websocket),
        read_from_ws_telnet(writer, websocket),
        protocol_label="Telnet",
    )


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
        db, actor=current_user.email, action="SSH Host Key Reset", result="Success",
        device_hostname=device.hostname,
    )
    return {"device_id": str(device.id), "ssh_host_key_fingerprint": None}


@router.websocket("/{device_id}/terminal")
async def device_terminal(
    websocket: WebSocket,
    device_id: uuid.UUID,
    token: str = Query(""),
    db: Session = Depends(get_db),
):
    """Interactive device terminal over a WebSocket. Tries SSH first
    (encrypted, the preferred path for anything with a real management
    plane); if the SSH connection phase itself fails -- refused, timed
    out, no SSH server at all -- falls back to Telnet with an explicit
    on-screen warning that the session is unencrypted, rather than just
    failing outright. This matters most for GNS3 lab nodes, which have no
    SSH server until they've been bootstrapped over their console, but
    also covers older/physical gear that was never configured for SSH.
    """
    user = get_current_user_ws(token, db)
    if not user:
        await websocket.close(code=1008)  # Policy Violation
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

    # No premature check on device.ssh_credential_ref here -- that's only
    # the legacy env-var-ref field. get_ssh_password() below already checks
    # every real credential source in priority order (ssh_password_encrypted
    # first -- the one the SSH Credentials modal actually writes to, then
    # ssh_credential_ref, then a dev-only default) and raises
    # CredentialNotFoundError itself if none resolve. Gating on
    # ssh_credential_ref alone used to reject every device whose operator
    # set a password the normal way (via the modal) but never touched the
    # legacy ref field, even though get_ssh_password() would have found it
    # fine -- the Terminal button failed with "No SSH credentials mapped"
    # for exactly the devices that actually had credentials configured.
    try:
        password = credential_service.get_ssh_password(device)
    except credential_service.CredentialNotFoundError as exc:
        await websocket.send_text(f"\r\n\x1b[31mError: {exc}\x1b[0m\r\n")
        await websocket.close()
        return

    username = device.ssh_username or "admin"
    audit_service.record_event(
        db, actor=user.email, action="Terminal Session Opened", result="Started",
        device_hostname=device.hostname,
    )

    await websocket.send_text(f"\r\n\x1b[36mInitiating secure shell connection to {device.ip_address}...\x1b[0m\r\n")

    try:
        ssh_ran = await _try_ssh(websocket, device, username, password, db)
        if not ssh_ran:
            # Telnet is unencrypted -- credentials and full session
            # content go over the wire in cleartext. That's an accepted
            # trade-off for GNS3 lab nodes (device.lab_provider set),
            # which frequently have no SSH server until bootstrapped over
            # console and live in an isolated lab environment. It is NOT
            # acceptable for real production/physical devices reachable
            # over the actual network -- silently downgrading those to
            # Telnet just because SSH happened to be unreachable (refused,
            # timed out, misconfigured) would hand any on-path attacker
            # credentials for real infrastructure. Refuse instead and
            # surface the failure so an admin fixes SSH access.
            if device.lab_provider:
                await _try_telnet(websocket, device, username, password)
            else:
                await websocket.send_text(
                    "\r\n\x1b[31mSSH is unavailable for this device and it is not a lab "
                    "device, so this session will NOT fall back to unencrypted "
                    "Telnet. Fix SSH access (service, host key, credentials) and "
                    "retry.\x1b[0m\r\n"
                )
                await websocket.close(code=1011, reason="ssh_unavailable_no_telnet_fallback")
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31mConnection interrupted: {e!s}\x1b[0m\r\n")
            await websocket.close()
        except Exception:
            pass
