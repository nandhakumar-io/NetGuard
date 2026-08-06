import sys
import traceback

from app.api.dashboard import _compute_summary
from app.core.database import SessionLocal

db = SessionLocal()
try:
    res = _compute_summary(db)
    print("SUCCESS")
    print(res)
except Exception as e:
    print(f"FAILED: {e}")
    traceback.print_exc(file=sys.stdout)
finally:
    db.close()
