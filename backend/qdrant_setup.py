"""
qdrant_setup.py — Qdrant vector database client and collection initialisation for Servix.
Creates the servix_manuals collection with 1536-dimension vectors (OpenAI text-embedding-3-small).
"""

import os
import logging
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

logger = logging.getLogger(__name__)

_client: AsyncQdrantClient | None = None

COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "servix_manuals")
VECTOR_SIZE = 1536


# ── Client ────────────────────────────────────────────────────────────────────

def get_qdrant() -> AsyncQdrantClient:
    """Return the active Qdrant client, raising if not yet initialised."""
    if _client is None:
        raise RuntimeError("Qdrant client is not initialised. Call init_qdrant() first.")
    return _client


async def init_qdrant() -> None:
    """
    Initialise the Qdrant async client and ensure the collection exists.

    Safe to call multiple times — skips creation if collection already exists.
    """
    global _client

    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    _client = AsyncQdrantClient(url=qdrant_url)

    existing = await _client.get_collections()
    existing_names = [c.name for c in existing.collections]

    if COLLECTION_NAME not in existing_names:
        await _client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=VECTOR_SIZE,
                distance=Distance.COSINE,
            ),
        )
        logger.info(f"Qdrant collection '{COLLECTION_NAME}' created.")
    else:
        logger.info(f"Qdrant collection '{COLLECTION_NAME}' already exists.")
