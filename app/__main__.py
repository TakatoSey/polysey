import asyncio

from .bot import TelegramApp
from .config import get_settings
from .db import SessionLocal, init_db
from .engine import CopyEngine
from .logging import configure_logging
from .polymarket import PolymarketClient
from .repository import add_leader, get_leader


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    await init_db()
    async with SessionLocal() as session:
        if settings.default_leader_address:
            # Seed the configured leader once; do not silently re-enable one
            # the user intentionally disabled from the Telegram panel.
            if not await get_leader(session, settings.default_leader_address):
                await add_leader(session, settings.default_leader_address)
        await session.commit()
    client = PolymarketClient(settings)
    await client.start()
    engine = CopyEngine(settings, client)
    telegram = TelegramApp(settings, engine)
    try:
        await asyncio.gather(engine.run(), telegram.run(), telegram.notify_loop())
    finally:
        await engine.stop()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
