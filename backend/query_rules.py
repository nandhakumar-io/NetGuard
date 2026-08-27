import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.alert_rule import AlertRule

db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg2://netguard:admin@localhost:5432/netguard")
engine = create_engine(db_url)
SessionLocal = sessionmaker(bind=engine)
db = SessionLocal()

rules = db.query(AlertRule).all()
for r in rules:
    print(f"Rule: id={r.id}, name={r.name}, metric={r.metric}, operator={r.operator}, threshold={r.threshold}, enabled={r.enabled}")
