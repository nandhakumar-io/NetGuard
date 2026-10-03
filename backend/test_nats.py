import asyncio
import urllib.parse
import nats
import os
from app.core.config import settings

async def test():
    print(f"Trying to connect with url: {settings.NATS_URL}")
    print(f"User={settings.NATS_API_USER}, Password starts with={settings.NATS_API_PASSWORD[:3]}")
    try:
        nc = await nats.connect(
            servers=[settings.NATS_URL],
            user=settings.NATS_API_USER,
            password=settings.NATS_API_PASSWORD
        )
        print("CONNECTED WITH KWARGS!")
        await nc.close()
    except Exception as e:
        print(f"FAILED WITH KWARGS: {e}")
        try:
            nc = await nats.connect(servers=[settings.NATS_URL])
            print("CONNECTED JUST WITH URL")
            await nc.close()
        except Exception as e2:
            print(f"FAILED JUST WITH URL: {e2}")
            try:
                nc = await nats.connect(servers=["nats://localhost:4222"], user=settings.NATS_API_USER, password=settings.NATS_API_PASSWORD)
                print("CONNECTED JUST WITH KWARGS ON LOCALHOST:4222")
                await nc.close()
            except Exception as e3:
                print(f"FAILED JUST WITH KWARGS ON LOCALHOST: {e3}")

asyncio.run(test())
