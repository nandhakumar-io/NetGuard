"""Multi-Factor Authentication service (NFR Security, FR-1).

TOTP (RFC 6238) via pyotp -- compatible with any standard authenticator app
(Google Authenticator, Authy, 1Password, etc). Secrets are stored on the
User row; in production these should additionally be encrypted at rest
(e.g. via the same Fernet approach used for config snapshots) rather than
stored in plaintext.
"""
import pyotp

from app.core.config import settings


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_email: str) -> str:
    """otpauth:// URI for QR-code enrollment in an authenticator app."""
    return pyotp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=settings.MFA_ISSUER_NAME)


def verify_code(secret: str, code: str) -> bool:
    """Verifies a 6-digit TOTP code, allowing 1 step (±30s) of clock drift."""
    if not secret or not code:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code.strip(), valid_window=1)
