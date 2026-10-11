"""
memory.py — Conversation memory for Servix.
Manages per-session conversation history.
"""

import logging
from datetime import datetime

from db import get_pool

logger = logging.getLogger(__name__)


# ── Company & user management ─────────────────────────────────────────────────

async def get_or_create_user(phone_number: str, company_id: int) -> dict:
    """
    Fetch an existing technician by phone number or create a new one.

    Args:
        phone_number: WhatsApp phone number in E.164 format
        company_id: The company this technician belongs to

    Returns:
        Dict with user id and name
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM users WHERE phone_number = $1", phone_number
        )
        if row:
            return dict(row)

        row = await conn.fetchrow(
            "INSERT INTO users (phone_number, company_id) VALUES ($1, $2) RETURNING id, name",
            phone_number, company_id,
        )
        logger.info(f"New user created: phone={phone_number}, company_id={company_id}")
        return dict(row)


async def get_or_create_company(slug: str, name: str) -> int:
    """
    Fetch an existing company by slug or create it.

    Args:
        slug: URL-safe identifier (e.g. 'installaties-vermeersch')
        name: Display name of the company

    Returns:
        Company id
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM companies WHERE slug = $1", slug
        )
        if row:
            return row["id"]
        row = await conn.fetchrow(
            "INSERT INTO companies (slug, name) VALUES ($1, $2) RETURNING id",
            slug, name,
        )
        return row["id"]


# ── Conversation memory ───────────────────────────────────────────────────────

async def save_message(
    user_id: int,
    company_id: int,
    session_id: str,
    role: str,
    message: str,
) -> int:
    """
    Persist a single conversation message to PostgreSQL.

    Args:
        user_id: ID of the technician
        company_id: ID of the company
        session_id: Unique session identifier (groups messages per job)
        role: 'user' or 'assistant'
        message: Message content

    Returns:
        ID of the inserted conversation row
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (user_id, company_id, session_id, role, message)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            user_id, company_id, session_id, role, message,
        )
        return row["id"]


async def get_conversation_history(session_id: str, limit: int = 10) -> list[dict]:
    """
    Retrieve the last N messages for a session, ordered chronologically.

    Args:
        session_id: The session to retrieve messages for
        limit: Maximum number of messages to return

    Returns:
        List of dicts with role and message keys, oldest first
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, message, timestamp
            FROM conversations
            WHERE session_id = $1
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            session_id, limit,
        )
    return [{"role": r["role"], "content": r["message"]} for r in reversed(rows)]
