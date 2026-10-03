import asyncio
import urllib.parse
import nats
import sys
from app.core.config import settings

async def test():
    NATS_URL = f"nats://{settings.NATS_API_USER}:{urllib.parse.quote_plus(settings.NATS_API_PASSWORD)}@localhost:4222"
    print(f"URL: {NATS_URL}")
    try:
        nc = await nats.connect(servers=[NATS_URL])
        print("SUCCESS! (quote_plus)")
        await nc.close()
        return
    except Exception as e:
        print(f"FAILED quote_plus: {e}")

    NATS_URL2 = f"nats://{settings.NATS_API_USER}:{urllib.parse.quote(settings.NATS_API_PASSWORD)}@localhost:4222"
    try:
        nc = await nats.connect(servers=[NATS_URL2])
        print("SUCCESS! (quote)")
        await nc.close()
        return
    except Exception as e:
        print(f"FAILED quote: {e}")

asyncio.run(test())
