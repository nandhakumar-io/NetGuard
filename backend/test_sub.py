from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.deps import get_current_user
from app.main import app
from app.models.user import User

client = TestClient(app)
db = SessionLocal()
user = db.query(User).first()
if not user:
    print("No user found")
    exit(1)

# we need to authenticate
def override_get_current_user():
    return user

app.dependency_overrides[get_current_user] = override_get_current_user

# test browser sub
payload = {
    "label": "Test Browser",
    "provider": "browser",
    "endpoint": "https://fcm.googleapis.com/fcm/send/foo",
    "p256dh": "dummy-p256dh",
    "auth": "dummy-auth",
    "include_non_critical": False,
}

response = client.post("/api/v1/push-subscriptions", json=payload)
print(response.status_code)
print(response.text)

# test ntfy
payload_ntfy = {
    "label": "Test Ntfy",
    "provider": "ntfy",
    "target": "https://ntfy.sh/test",
    "include_non_critical": False,
}
response = client.post("/api/v1/push-subscriptions", json=payload_ntfy)
print(response.status_code)
print(response.text)
