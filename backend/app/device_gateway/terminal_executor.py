"""Runs an interactive SSH/Telnet session against a real device, driven
entirely by NATS instead of a WebSocket. This is the code that used to
live in app.api.terminal (_try_ssh/_try_telnet/_PinningSSHClient) -- it
moved here, unchanged in its device-facing behavior, because the API
process must not itself hold network-device connectivity or resolve
device credentials (Section 3/8). command_guard is re-applied here too:
even though the API already runs the same guard before publishing a
line, the Gateway does not trust that -- an API RCE that forges
`terminal.session.<id>.in` messages directly must still hit the same
destructive-command deny-list a legitimate client would.

Session lifecycle:
  terminal.session.<id>.in   API -> Gateway   browser keystrokes
  terminal.session.<id>.out  Gateway -> API   device output
  terminal.session.<id>.ctl  either direction  lifecycle signals
"""
from __future__ import annotations

import asyncio
import logging

import asyncssh
import telnetlib3
from sqlalchemy.orm import Session

from app.models.device import Device
from app.schemas.terminal_job import (
    TERMINAL_SESSION_SUBJECT_PREFIX,
    TerminalControlMessage,
    TerminalSessionMessage,
)
from app.services import command_guard, credential_service

logger = logging.getLogger("netguard.device_gateway.terminal_executor")

SSH_CONNECT_TIMEOUT_SECONDS = 8
TELNET_CONNECT_TIMEOUT_SECONDS = 8
IDLE_TIMEOUT_SECONDS = 30 * 60  # hard cap on a single session's lifetime, matching Section 14


def _in_subject(session_id: str) -> str:
    return f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.in"


def _out_subject(session_id: str) -> str:
    return f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.out"


def _ctl_subject(session_id: str) -> str:
    return f"{TERMINAL_SESSION_SUBJECT_PREFIX}.{session_id}.ctl"


class _PinningSSHClient(asyncssh.SSHClient):
    """Trust-on-first-use host key pinning, persisted on the Device row.
    Moved verbatim from app.api.terminal -- see reset_ssh_host_key (still
    an API endpoint; it only flips a DB column, no device connectivity
    needed for that)."""

    def __init__(self, device: Device, db: Session):
        self._device = device
        self._db = db

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        fingerprint = key.get_fingerprint()
        pinned = self._device.ssh_host_key_fingerprint
        if pinned is None:
            self._device.ssh_host_key_fingerprint = fingerprint
            self._db.commit()
            return True
        if pinned != fingerprint:
            raise _HostKeyMismatchError(
                f"Host key for {host} changed (expected {pinned}, got {fingerprint}). "
                "Possible MITM, or the device was re-imaged/replaced -- if expected, "
                "an admin must clear the pin via POST /devices/{id}/ssh-host-key/reset."
            )
        return True


class _HostKeyMismatchError(Exception):
    pass


async def _publish_out(nc, session_id: str, data: str) -> None:
    await nc.publish(_out_subject(session_id), TerminalSessionMessage(data=data).model_dump_json().encode())


async def _publish_ctl(nc, session_id: str, event: str, detail: str = "") -> None:
    await nc.publish(
        _ctl_subject(session_id), TerminalControlMessage(event=event, detail=detail).model_dump_json().encode()
    )


async def run_session(nc, session_id: str, device: Device, username: str, requested_by: str) -> None:
    """Resolves credentials, connects (SSH, falling back to Telnet for
    lab devices exactly as the old API code did), and pumps bytes to/from
    NATS until the session ends. Never raises -- all failures are
    reported over `.ctl`/`.out` so the API side (and therefore the
    browser) always gets a clear reason.
    """
    guard_state = command_guard.LineGuardState(device_id=str(device.id), username=username)

    use_key_auth = (device.ssh_auth_method or "password") == "key"
    password: str | None = None
    client_keys: list | None = None
    try:
        if use_key_auth:
            key_pem, passphrase = credential_service.get_ssh_private_key(device)
            client_keys = [asyncssh.import_private_key(key_pem, passphrase=passphrase)]
        else:
            password = credential_service.get_ssh_password(device)
    except credential_service.CredentialNotFoundError as exc:
        await _publish_ctl(nc, session_id, "error", str(exc))
        return
    except asyncssh.KeyImportError as exc:
        await _publish_ctl(nc, session_id, "error", f"stored SSH private key could not be parsed ({exc})")
        return

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        ssh_ran = await _try_ssh(nc, session_id, device, username, db, password=password, client_keys=client_keys, guard_state=guard_state)
        if not ssh_ran:
            if device.lab_provider:
                await _try_telnet(nc, session_id, device, username, password or "", guard_state=guard_state)
            else:
                await _publish_ctl(
                    nc, session_id, "error",
                    "SSH is unavailable for this device and it is not a lab device, so this "
                    "session will NOT fall back to unencrypted Telnet.",
                )
    finally:
        db.close()
        await _publish_ctl(nc, session_id, "closed")


async def _pump_device_to_nats(stream, nc, session_id: str) -> None:
    try:
        while True:
            data = await stream.read(4096)
            if not data:
                break
            await _publish_out(nc, session_id, data)
    except (asyncssh.Error, OSError, ConnectionError):
        pass


async def _pump_nats_to_device(sub, write_fn, guard_state, nc, session_id: str) -> None:
    try:
        async for msg in sub.messages:
            try:
                incoming = TerminalSessionMessage.model_validate_json(msg.data)
            except Exception:  # noqa: BLE001 - malformed input, drop it
                continue
            local_echo, to_forward, blocked_rule = command_guard.feed_keystroke(guard_state, incoming.data)
            if local_echo:
                await _publish_out(nc, session_id, local_echo)
            if to_forward is not None:
                write_fn(to_forward)
            if blocked_rule:
                await _publish_ctl(nc, session_id, "blocked", blocked_rule)
    except Exception:  # noqa: BLE001
        pass


async def _try_ssh(nc, session_id, device, username, db, *, password, client_keys, guard_state) -> bool:
    try:
        async with asyncssh.connect(
            host=device.ip_address,
            username=username,
            password=password,
            known_hosts=None,
            client_factory=lambda: _PinningSSHClient(device, db),
            client_keys=client_keys,
            connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
        ) as conn:
            process = await conn.create_process(term_type="xterm-256color", term_size=(120, 40))
            await _publish_ctl(nc, session_id, "connected", f"ssh:{device.ip_address}")

            sub = await nc.subscribe(_in_subject(session_id))
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _pump_device_to_nats(process.stdout, nc, session_id),
                        _pump_nats_to_device(sub, process.stdin.write, guard_state, nc, session_id),
                    ),
                    timeout=IDLE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await _publish_ctl(nc, session_id, "closed", "idle timeout")
            finally:
                await sub.unsubscribe()
        return True
    except _HostKeyMismatchError as exc:
        # Deliberately no Telnet fallback here -- see the original
        # rationale in the pre-migration app.api.terminal module.
        await _publish_ctl(nc, session_id, "error", f"SECURITY WARNING: {exc}")
        return True
    except (asyncssh.Error, OSError, asyncio.TimeoutError, ConnectionRefusedError) as exc:
        await _publish_ctl(nc, session_id, "ssh_unavailable", str(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        await _publish_ctl(nc, session_id, "ssh_unavailable", f"unexpected error: {exc}")
        return False


async def _try_telnet(nc, session_id, device, username, password, *, guard_state) -> None:
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
        await _publish_ctl(nc, session_id, "error", f"Telnet also unavailable ({host}:{port}): {exc}")
        return

    await _publish_ctl(nc, session_id, "connected", f"telnet:{host}:{port}:{username}")
    sub = await nc.subscribe(_in_subject(session_id))
    try:
        await asyncio.wait_for(
            asyncio.gather(
                _pump_device_to_nats(reader, nc, session_id),
                _pump_nats_to_device(sub, writer.write, guard_state, nc, session_id),
            ),
            timeout=IDLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await _publish_ctl(nc, session_id, "closed", "idle timeout")
    finally:
        await sub.unsubscribe()
