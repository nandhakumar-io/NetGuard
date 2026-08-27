"""Seed a default admin user if no users exist yet.

Run from backend/:
    python seed_admin.py

Credentials: admin@netguard.io / Admin1234!
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.user import User, UserRole

EMAIL = "admin@netguard.io"
PASSWORD = "Admin1234!"

db = SessionLocal()
try:
    existing = db.query(User).filter(User.email == EMAIL).first()
    if existing:
        print(f"User '{EMAIL}' already exists — nothing to do.")
        sys.exit(0)

    user = User(
        email=EMAIL,
        full_name="NetGuard Admin",
        hashed_password=hash_password(PASSWORD),
        role=UserRole.NETWORK_ADMIN,
        mfa_enabled="false",
        # This is the platform bootstrap account -- it must be able to
        # manage tenants (POST/PATCH/DELETE /tenants is require_msp_staff-
        # gated) and see the cross-tenant NOC board from day one, since
        # it's the only account that exists until it creates others.
        # Without this, the "no option to create a tenant" symptom shows
        # up for anyone starting from a fresh install: the Tenants page
        # renders, but every write 403s for this account.
        is_msp_staff=True,
        # Admin bootstrap account created directly by this script, not
        # through the public registration form -- skip the approval
        # queue the same way POST /users (admin-created accounts) does.
        is_approved=True,
    )
    db.add(user)
    db.commit()
    print(f"Created admin user: {EMAIL}  /  {PASSWORD}")
finally:
    db.close()
