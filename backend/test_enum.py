from app.core.database import engine

with engine.connect() as conn:
    res = conn.execute("SELECT typname FROM pg_type WHERE typtype = 'e'")
    for r in res:
        print(r)
