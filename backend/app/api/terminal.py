import asyncio
import asyncssh
import uuid
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from sqlalchemy.orm import Session
from jose import JWTError
from app.core.database import get_db
from app.models.device import Device
from app.models.user import User
from app.core.security import decode_access_token
from app.services import credential_service

router = APIRouter(prefix="/devices", tags=["terminal"])

async def read_from_ssh(process: asyncssh.SSHClientProcess, websocket: WebSocket):
    try:
        while True:
            data = await process.stdout.read(4096)
            if not data:
                break
            # WebSockets can send string/text frames seamlessly to xterm.js
            await websocket.send_text(data)
    except Exception as e:
        print(f"SSH stream read error: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass

async def read_from_ws(process: asyncssh.SSHClientProcess, websocket: WebSocket):
    try:
        while True:
            data = await websocket.receive_text()
            process.stdin.write(data)
    except WebSocketDisconnect:
        process.stdin.close()
    except Exception as e:
        print(f"WebSocket read error: {e}")
        process.stdin.close()

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


@router.websocket("/{device_id}/terminal")
async def device_terminal(
    websocket: WebSocket,
    device_id: uuid.UUID,
    token: str = Query(""),
    db: Session = Depends(get_db)
):
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

    if not device.ssh_credential_ref:
        await websocket.send_text("\r\n\x1b[31mError: No SSH credentials mapped to this device.\x1b[0m\r\n")
        await websocket.close()
        return

    try:
        env_password = credential_service.get_ssh_password(device)
    except credential_service.CredentialNotFoundError as exc:
        await websocket.send_text(f"\r\n\x1b[31mError: {exc}\x1b[0m\r\n")
        await websocket.close()
        return

    await websocket.send_text(f"\r\n\x1b[36mInitiating secure shell connection to {device.ip_address}...\x1b[0m\r\n")

    try:
        # Create persistent asyncssh transport
        async with asyncssh.connect(
            host=device.ip_address,
            username=device.ssh_username or "admin",
            password=env_password,
            known_hosts=None,
            client_keys=None
        ) as conn:
            # Request fully interactive PTY (pseudo-terminal) shell matching xterm dimensions if specified
            process = await conn.create_process(term_type='xterm-256color', pty=True)
            
            task1 = asyncio.create_task(read_from_ssh(process, websocket))
            task2 = asyncio.create_task(read_from_ws(process, websocket))
            
            await asyncio.gather(task1, task2)
            
    except Exception as e:
        try:
            await websocket.send_text(f"\r\n\x1b[31mConnection interrupted: {str(e)}\x1b[0m\r\n")
            await websocket.close()
        except:
            pass