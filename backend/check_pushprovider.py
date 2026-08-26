import psycopg2

try:
    conn = psycopg2.connect("postgresql://netguard:netguard123@localhost:5432/netguard")
    cur = conn.cursor()
    cur.execute("SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_enum.enumtypid = pg_type.oid WHERE typname = 'pushprovider'")
    rows = cur.fetchall()
    print("pushprovider enum values:", [row[0] for row in rows])
    conn.close()
except Exception as e:
    print("Error:", e)
