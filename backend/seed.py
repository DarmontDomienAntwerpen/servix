"""
seed.py — Demo data voor Servix.
Maakt het testbedrijf aan zodat /chat en /webhook een company_id hebben om aan te koppelen.

Gebruik: python seed.py
"""

import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()


async def seed():
    url = os.getenv("POSTGRES_URL", "postgresql://servix:servix@localhost:5432/servix")
    conn = await asyncpg.connect(url)

    print("🌱 Servix seed gestart...\n")

    company = await conn.fetchrow("""
        INSERT INTO companies (name, slug)
        VALUES ('Installaties Vermeersch', 'vermeersch')
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """)
    print(f"✓ Bedrijf: Installaties Vermeersch (id={company['id']})")

    await conn.close()

    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎉  Seed klaar!

   Monteur UI       →  http://localhost:8000
   Handleidingen    →  http://localhost:8000/boss

   Demo-flow:
   1. Open /boss → upload of scrape een handleiding
   2. Open /     → stel een vraag over een foutcode
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


asyncio.run(seed())
