import asyncio
import sys
sys.path.insert(0, '.')

async def main():
    import app.models  # registers all models
    from app.core.database import init_db, engine
    from sqlalchemy import text

    await init_db()

    async with engine.connect() as conn:
        result = await conn.execute(text("SHOW TABLES LIKE 'agent%'"))
        tables = [r[0] for r in result.fetchall()]
        print("Agent tables:", tables)

    await engine.dispose()
    print("Done")

asyncio.run(main())
