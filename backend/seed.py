"""
seed.py — Demo data for Servix.
Creates a demo installation company so /chat and /webhook have a company_id to attach to.

Usage: docker compose exec backend python seed.py
"""

import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()


async def seed():
    url = os.getenv("POSTGRES_URL", "postgresql://servix:servix@localhost:5432/servix")
    conn = await asyncpg.connect(url)

    print("🌱 Seeding Servix demo data...\n")

    company = await conn.fetchrow("""
        INSERT INTO companies (name, slug)
        VALUES ('Installaties Vermeersch', 'vermeersch')
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """)
    print(f"✓ Company: Installaties Vermeersch (id={company['id']})")

    await conn.close()

    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎉  Seed complete!

   Technician chat  →  http://localhost:8000
   Manual admin     →  http://localhost:8000/boss

   Demo flow:
   1. Open /boss → upload or scrape a manual
   2. Open /     → ask about a fault code
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


asyncio.run(seed())
