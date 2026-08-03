import asyncio
import uuid

import asyncssh
import telnetlib3
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from sqlalchemy.orm import Session
from jose import JWTError

from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.device import Device
from app.models.user import User
from app.services import audit_service, credential_service

router = APIRouter(prefix="/devices", tags=["terminal"])

SSH_CONNECT_TIMEOUT_SECONDS = 8
TELNET_CONNECT_TIMEOUT_SECONDS = 8


async def read_from_ssh(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    try:
        while True:
            data = await process.stdout.read(4096)
            if not data:
                break
            await websocket.send_text(data)
    except Exception as e:
        print(f"SSH stream read error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def read_from_ws(process: asyncssh.SSHClientProcess, websocket: WebSocket) -> None:
    try:
        while True:
            data = await websocket.receive_text()
            process.stdin.write(data)
    except WebSocketDisconnect:
        process.stdin.close()
    except Exception as e:
        print(f"WebSocket read error: {e}")
        process.stdin.close()


async def read_from_telnet(reader: telnetlib3.TelnetReader, websocket: WebSocket) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await websocket.send_text(data)
    except Exception as e:
        print(f"Telnet stream read error: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def read_from_ws_telnet(writer: telnetlib3.TelnetWriter, websocket: WebSocket) -> None:
    try:
        while True:
            data = await websocket.receive_text()
            writer.write(data)
    except WebSocketDisconnect:
        writer.close()
    except Exception as e:
        print(f"WebSocket read error: {e}")
        writer.close()


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


async def _try_ssh(websocket: WebSocket, device: Device, username: str, password: str) -> bool:
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
            client_keys=None,
            connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        ) as conn:
            process = await conn.create_process(term_type="xterm-256color")
            await websocket.send_text(f"\r\n\x1b[32mConnected via SSH to {device.ip_address}.\x1b[0m\r\n")
            task1 = asyncio.create_task(read_from_ssh(process, websocket))
            task2 = asyncio.create_task(read_from_ws(process, websocket))
            await asyncio.gather(task1, task2)
        return True
    except (asyncssh.Error, OSError, asyncio.TimeoutError, ConnectionRefusedError) as exc:
        # Connection-phase failure (refused, timed out, no SSH server, auth
        # rejected because the daemon isn't even up yet, etc.) -- this is
        # exactly the case that should fall back to Telnet, not a hard
        # error. GNS3 lab nodes in particular have no SSH server at all
        # until they've been bootstrapped over their console.
        await websocket.send_text(
            f"\r\n\x1b[33mSSH unavailable ({exc}). Falling back to Telnet "
            f"-- this session will be unencrypted.\x1b[0m\r\n"
        )
        return False
    except Exception as exc:  # noqa: BLE001
        # Anything else (e.g. an asyncssh API/kwarg mismatch surfacing as
        # TypeError -- see the pty= kwarg bug this replaced) should still
        # degrade to Telnet rather than kill the session and look like an
        # unconditional "SSH failed" to the user.
        await websocket.send_text(
            f"\r\n\x1b[33mSSH session failed unexpectedly ({exc}). Falling back to Telnet "
            f"-- this session will be unencrypted.\x1b[0m\r\n"
        )
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
    task1 = asyncio.create_task(read_from_telnet(reader, websocket))
    task2 = asyncio.create_task(read_from_ws_telnet(writer, websocket))
    await asyncio.gather(task1, task2)


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
        ssh_ran = await _try_ssh(websocket, device, username, password)
        if not ssh_ran:
            await _try_telnet(websocket, device, username, password)
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31mConnection interrupted: {str(e)}\x1b[0m\r\n")
            await websocket.close()
        except Exception:
            pass