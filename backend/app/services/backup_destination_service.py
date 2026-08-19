"""Off-site upload of completed database backups to AWS S3, Azure Blob
Storage, or a remote server over SFTP. See app.models.backup_destination
for the storage model and app.services.backup_service for where uploads
are triggered (right after a local pg_dump completes successfully).

Each cloud SDK is an optional import: boto3 (S3) and azure-storage-blob
(Azure) aren't hard dependencies of the app, the same pattern already
used for `user_agents` in app.services.session_device -- a NetGuard
install that never configures a cloud destination shouldn't need either
package installed. `paramiko` (SFTP) *is* already a hard dependency
(device SSH/SFTP file transfer elsewhere in the app), so that path always
works.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import paramiko

from app.core import crypto

logger = logging.getLogger(__name__)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - optional, only needed for S3 destinations
    boto3 = None
    BotoCoreError = ClientError = Exception

try:
    from azure.core.exceptions import AzureError
    from azure.storage.blob import BlobServiceClient
except ImportError:  # pragma: no cover - optional, only needed for Azure destinations
    BlobServiceClient = None
    AzureError = Exception


DESTINATION_TYPES = ("s3", "azure_blob", "sftp")

# Which config keys each destination type expects inside the Fernet-
# encrypted JSON blob (BackupDestination.config_encrypted). Secrets are
# marked so the API layer can mask them back to the frontend on read.
DESTINATION_FIELDS: dict[str, list[tuple[str, bool]]] = {
    # (field name, is_secret)
    "s3": [
        ("bucket", False), ("region", False), ("access_key_id", False),
        ("secret_access_key", True), ("prefix", False), ("endpoint_url", False),
    ],
    "azure_blob": [
        ("account_name", False), ("account_key", True),
        ("connection_string", True), ("container", False), ("prefix", False),
    ],
    "sftp": [
        ("host", False), ("port", False), ("username", False),
        ("password", True), ("private_key", True), ("remote_dir", False),
    ],
}


class DestinationError(Exception):
    """Raised for a connectivity/config problem with a destination --
    either an upload that couldn't complete or a failed connection test."""


def encrypt_config(type_: str, raw: dict[str, Any]) -> str:
    fields = {name for name, _ in DESTINATION_FIELDS.get(type_, [])}
    cleaned = {k: v for k, v in raw.items() if k in fields and v not in (None, "")}
    return crypto.encrypt(json.dumps(cleaned))


def decrypt_config(config_encrypted: str) -> dict[str, Any]:
    plaintext = crypto.decrypt(config_encrypted)
    if not plaintext:
        return {}
    try:
        return json.loads(plaintext)
    except (ValueError, TypeError):
        return {}


def masked_config(type_: str, config: dict[str, Any]) -> dict[str, Any]:
    """Same shape as the stored config, but every secret field is
    replaced with a boolean "is this set" flag rather than the real
    value -- for GET responses. The frontend uses this to show "•••• set"
    without the plaintext secret ever leaving this process again."""
    result: dict[str, Any] = {}
    for name, is_secret in DESTINATION_FIELDS.get(type_, []):
        if name not in config:
            continue
        result[name] = bool(config[name]) if is_secret else config[name]
    return result


def _s3_client(config: dict[str, Any]):
    if boto3 is None:
        raise DestinationError(
            "The boto3 package isn't installed on this server. Add `boto3` to "
            "backend/requirements.txt and rebuild to enable S3 destinations."
        )
    kwargs: dict[str, Any] = {}
    if config.get("region"):
        kwargs["region_name"] = config["region"]
    if config.get("endpoint_url"):  # S3-compatible stores (MinIO, Wasabi, ...)
        kwargs["endpoint_url"] = config["endpoint_url"]
    if config.get("access_key_id") and config.get("secret_access_key"):
        kwargs["aws_access_key_id"] = config["access_key_id"]
        kwargs["aws_secret_access_key"] = config["secret_access_key"]
    return boto3.client("s3", **kwargs)


def _azure_client(config: dict[str, Any]) -> "BlobServiceClient":
    if BlobServiceClient is None:
        raise DestinationError(
            "The azure-storage-blob package isn't installed on this server. Add "
            "`azure-storage-blob` to backend/requirements.txt and rebuild to enable "
            "Azure destinations."
        )
    if config.get("connection_string"):
        return BlobServiceClient.from_connection_string(config["connection_string"])
    if config.get("account_name") and config.get("account_key"):
        account_url = f"https://{config['account_name']}.blob.core.windows.net"
        return BlobServiceClient(account_url=account_url, credential=config["account_key"])
    raise DestinationError("Azure destination needs either a connection string or an account name + key.")


def _sftp_client(config: dict[str, Any]) -> tuple[paramiko.SFTPClient, paramiko.Transport]:
    host = config.get("host")
    if not host:
        raise DestinationError("SFTP destination is missing a host.")
    port = int(config.get("port") or 22)

    transport = paramiko.Transport((host, port))
    try:
        if config.get("private_key"):
            key = paramiko.RSAKey.from_private_key(io.StringIO(config["private_key"]))
            transport.connect(username=config.get("username"), pkey=key)
        else:
            transport.connect(username=config.get("username"), password=config.get("password"))
    except Exception as exc:  # noqa: BLE001 - surfaced as a DestinationError below
        transport.close()
        raise DestinationError(f"SFTP connection failed: {exc}") from exc

    return paramiko.SFTPClient.from_transport(transport), transport


def upload_to_destination(type_: str, config: dict[str, Any], local_path: Path, remote_filename: str) -> None:
    """Uploads local_path to the given destination. Raises DestinationError
    on any failure; never raises for "destination unreachable" vs. "auth
    failed" differently -- the caller (backup_service) only needs to know
    success/failure plus a human-readable reason to store on the job row.
    """
    if type_ == "s3":
        client = _s3_client(config)
        key = f"{config['prefix'].strip('/')}/{remote_filename}" if config.get("prefix") else remote_filename
        try:
            client.upload_file(str(local_path), config["bucket"], key)
        except (BotoCoreError, ClientError) as exc:
            raise DestinationError(f"S3 upload failed: {exc}") from exc

    elif type_ == "azure_blob":
        client = _azure_client(config)
        blob_name = f"{config['prefix'].strip('/')}/{remote_filename}" if config.get("prefix") else remote_filename
        try:
            container_client = client.get_container_client(config["container"])
            with open(local_path, "rb") as fh:
                container_client.upload_blob(name=blob_name, data=fh, overwrite=True)
        except AzureError as exc:
            raise DestinationError(f"Azure Blob upload failed: {exc}") from exc

    elif type_ == "sftp":
        sftp, transport = _sftp_client(config)
        try:
            remote_dir = (config.get("remote_dir") or ".").rstrip("/") or "/"
            remote_path = f"{remote_dir}/{remote_filename}"
            sftp.put(str(local_path), remote_path)
        except Exception as exc:  # noqa: BLE001
            raise DestinationError(f"SFTP upload failed: {exc}") from exc
        finally:
            sftp.close()
            transport.close()

    else:
        raise DestinationError(f"Unknown destination type: {type_}")


def test_connection(type_: str, config: dict[str, Any]) -> None:
    """Lightweight connectivity/auth check without uploading anything --
    backs the "Test" button on the Cloud Destinations panel. Raises
    DestinationError with a human-readable reason on any failure.
    """
    if type_ == "s3":
        client = _s3_client(config)
        try:
            client.head_bucket(Bucket=config["bucket"])
        except (BotoCoreError, ClientError) as exc:
            raise DestinationError(f"Could not reach bucket '{config.get('bucket')}': {exc}") from exc

    elif type_ == "azure_blob":
        client = _azure_client(config)
        try:
            container_client = client.get_container_client(config["container"])
            container_client.get_container_properties()
        except AzureError as exc:
            raise DestinationError(f"Could not reach container '{config.get('container')}': {exc}") from exc

    elif type_ == "sftp":
        sftp, transport = _sftp_client(config)
        try:
            sftp.listdir(config.get("remote_dir") or ".")
        except Exception as exc:  # noqa: BLE001
            raise DestinationError(f"Could not list remote directory: {exc}") from exc
        finally:
            sftp.close()
            transport.close()

    else:
        raise DestinationError(f"Unknown destination type: {type_}")
