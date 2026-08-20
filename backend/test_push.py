import sys

sys.path.append("/home/kenpachi-zaraki/NetGuard/backend")

from app.core.database import SessionLocal
from app.models.push_subscription import PushProvider, PushSubscription
from app.models.user import User

db = SessionLocal()
user = db.query(User).first()

sub = PushSubscription(
    user_id=user.id,
    label="Test",
    provider=PushProvider.NTFY,
    target="https://ntfy.sh/foo",
    include_non_critical=False,
    include_actions=None
)
db.add(sub)
try:
    db.commit()
    print("Success")
except Exception as e:
    print("Database Exception:", type(e).__name__, e)
    db.rollback()
