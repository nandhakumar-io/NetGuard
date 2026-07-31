from app.core.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print("Postgres ENUM types:")
    res = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e';"))
    for r in res:
        print(r[0])
