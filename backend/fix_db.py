import sqlalchemy as sa

from app.core.database import Base, engine

print("Starting DB fix script...")

try:
    print("Creating Enums...")
    for enum_stmt in [
        "CREATE TYPE driftbaseline AS ENUM ('golden_config', 'previous_backup')",
        "CREATE TYPE alertsource AS ENUM ('snmp_trap', 'health_poll', 'drift', 'protocol_failure')",
        "CREATE TYPE alertseverity AS ENUM ('critical', 'warning', 'info')",
        "CREATE TYPE devicevendor AS ENUM ('cisco', 'juniper', 'arista', 'linux')",
        "CREATE TYPE devicestatus AS ENUM ('online', 'offline', 'degraded', 'unknown')"
    ]:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text(enum_stmt))
        except Exception:
            pass

    print("Adding missing columns...")
    for col_stmt in [
        "ALTER TABLE devices ADD COLUMN platform VARCHAR",
        "ALTER TABLE config_drifts ADD COLUMN baseline driftbaseline"
    ]:
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(sa.text(col_stmt))
        except Exception:
            pass

    print("Creating tables via Base.metadata.create_all...")
    Base.metadata.create_all(bind=engine)
    print("Database patched completely!")

except Exception as e:
    print(f"Script failed entirely: {e}")
