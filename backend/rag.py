"""
rag.py — Retrieval-Augmented Generation pipeline for Servix.
Searches Qdrant to find relevant manual chunks for a technician's query.
Uses multiquery strategy: generates 3 query variants to improve recall.
"""

import os
import asyncio
import logging
from openai import AsyncOpenAI
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny

from qdrant_setup import get_qdrant, COLLECTION_NAME
from prompts import MULTIQUERY_PROMPT

logger = logging.getLogger(__name__)

openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

EMBED_MODEL = "text-embedding-3-small"
TOP_K = 5
SCORE_THRESHOLD = 0.35


# ── Embedding ─────────────────────────────────────────────────────────────────

async def embed_query(query: str) -> list[float]:
    """Generate an embedding vector for a single query string."""
    response = await openai.embeddings.create(model=EMBED_MODEL, input=[query])
    return response.data[0].embedding


# ── Multiquery generation ─────────────────────────────────────────────────────

async def generate_query_variants(query: str) -> list[str]:
    """
    Use GPT to generate 3 alternative phrasings of the query.

    Improves recall by covering different ways the same question might
    be phrased in a technical manual.

    Args:
        query: The technician's original question

    Returns:
        List of 3 alternative query strings
    """
    try:
        response = await openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": MULTIQUERY_PROMPT.format(query=query)}
            ],
            max_tokens=200,
            temperature=0.3,
        )
        lines = response.choices[0].message.content.strip().split("\n")
        return [line.strip() for line in lines if line.strip()][:3]
    except Exception as e:
        logger.warning(f"Multiquery generation failed, using original query: {e}")
        return []


# ── Qdrant search ─────────────────────────────────────────────────────────────

async def search_once(
    vector: list[float],
    qdrant_filter: Filter | None = None,
) -> list[dict]:
    """
    Run a single vector search in Qdrant.

    Args:
        vector: Query embedding
        qdrant_filter: Optional payload filter

    Returns:
        List of result dicts with text, score, and metadata
    """
    client = get_qdrant()
    results = await client.search(
        collection_name=COLLECTION_NAME,
        query_vector=vector,
        limit=TOP_K,
        score_threshold=SCORE_THRESHOLD,
        query_filter=qdrant_filter,
        with_payload=True,
    )
    return [
        {
            "text": r.payload.get("text", ""),
            "score": r.score,
            "source_file": r.payload.get("source_file", ""),
            "brand": r.payload.get("brand"),
            "model": r.payload.get("model"),
            "page": r.payload.get("page"),
            "document_type": r.payload.get("document_type"),
            "error_codes": r.payload.get("error_codes", []),
        }
        for r in results
    ]


# ── Filter builders ───────────────────────────────────────────────────────────

def _filter_by_error_code(error_code: str) -> Filter:
    """Match chunks that contain this exact error code in their payload."""
    return Filter(
        must=[FieldCondition(key="error_codes", match=MatchAny(any=[error_code.upper()]))]
    )


def _filter_by_brand(brand: str) -> Filter:
    """Match chunks from a specific brand."""
    return Filter(
        must=[FieldCondition(key="brand", match=MatchValue(value=brand))]
    )


# ── Public search API ─────────────────────────────────────────────────────────

async def search_manual(
    query: str,
    brand: str | None = None,
    error_code: str | None = None,
) -> list[dict]:
    """
    Search manuals using a 3-tier multiquery RAG strategy.

    Search order:
    1. Exact error code match (if provided)
    2. Brand-filtered semantic search (if brand provided)
    3. Full semantic search across all manuals

    Generates 3 query variants in parallel with the main query,
    deduplicates results, and returns the top 5 by score.

    Args:
        query: The technician's question or error description
        brand: Optional brand filter (e.g. 'Vaillant')
        error_code: Optional error code to prioritise (e.g. 'F.28')

    Returns:
        Ranked list of up to 5 relevant chunks with metadata
    """
    # Generate query variants in parallel with embedding the original
    variants_task = asyncio.create_task(generate_query_variants(query))
    main_vector = await embed_query(query)
    variants = await variants_task

    all_queries = [query] + variants
    vectors = [main_vector] + await asyncio.gather(*[embed_query(v) for v in variants])

    results: list[dict] = []

    # Tier 1: exact error code match
    if error_code:
        ec_filter = _filter_by_error_code(error_code)
        ec_results = await search_once(main_vector, qdrant_filter=ec_filter)
        results.extend(ec_results)
        logger.info(f"Error code filter '{error_code}': {len(ec_results)} hits")

    # Tier 2: brand-filtered search
    if brand and len(results) < TOP_K:
        brand_filter = _filter_by_brand(brand)
        brand_results = await asyncio.gather(*[
            search_once(vec, qdrant_filter=brand_filter) for vec in vectors
        ])
        for batch in brand_results:
            results.extend(batch)

    # Tier 3: fallback — no filter
    if len(results) < TOP_K:
        fallback_results = await asyncio.gather(*[
            search_once(vec) for vec in vectors
        ])
        for batch in fallback_results:
            results.extend(batch)

    # Deduplicate by source_file + page, keep highest score
    seen: dict[str, dict] = {}
    for r in results:
        key = f"{r['source_file']}_{r['page']}"
        if key not in seen or r["score"] > seen[key]["score"]:
            seen[key] = r

    ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:TOP_K]
