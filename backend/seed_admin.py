"""Seed a default admin user if no users exist yet.

Run from backend/:
    python seed_admin.py

Credentials: admin@netguard.io / Admin1234!
"""
import sys
import os

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
    )
    db.add(user)
    db.commit()
    print(f"Created admin user: {EMAIL}  /  {PASSWORD}")
finally:
    db.close()
