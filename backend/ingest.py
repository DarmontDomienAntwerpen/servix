"""
ingest.py — Document ingestion pipeline for Servix.
Converts PDFs, DOCX, and images into searchable vector chunks stored in Qdrant.
Uses GPT-4o Vision per page to capture text, tables, error codes, and diagrams.
"""

import os
import io
import re
import base64
import logging
import hashlib
from pathlib import Path
from typing import Literal

import fitz  # PyMuPDF
import tiktoken
from docx import Document
from openai import AsyncOpenAI
from fastapi import UploadFile

from qdrant_setup import get_qdrant, COLLECTION_NAME

logger = logging.getLogger(__name__)

openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
tokenizer = tiktoken.get_encoding("cl100k_base")

CHUNK_SIZE = 600
CHUNK_OVERLAP = 100
EMBED_MODEL = "text-embedding-3-small"
VISION_MODEL = "gpt-4o"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".webp"}


# ── Vision extraction ─────────────────────────────────────────────────────────

async def extract_page_with_vision(image_bytes: bytes, source_hint: str = "") -> str:
    """
    Send a page image to GPT-4o Vision and extract all content.

    Captures text, tables, error codes, diagrams, and curves in a structured
    way that can be embedded and searched.

    Args:
        image_bytes: Raw PNG/JPEG bytes of the page
        source_hint: Filename or context to help the model understand the document

    Returns:
        Extracted text content of the page
    """
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "Dit is een pagina uit een technische handleiding voor HVAC, CV-ketels, "
        "warmtepompen, zonnepanelen of laadpalen.\n\n"
        "Extraheer ALLE inhoud van deze pagina:\n"
        "- Volledige tekst, exact zoals geschreven\n"
        "- Tabellen: converteer naar leesbaar tekst formaat (kolom: waarde)\n"
        "- Foutcodes en betekenissen\n"
        "- Technische specificaties en waarden\n"
        "- Beschrijf schema's, curves of diagrammen in woorden\n\n"
        "Geef enkel de geëxtraheerde inhoud, geen commentaar."
    )
    response = await openai.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }
        ],
        max_tokens=2000,
    )
    return response.choices[0].message.content or ""


# ── Document parsing ──────────────────────────────────────────────────────────

async def parse_pdf(file_bytes: bytes) -> list[dict]:
    """
    Convert each PDF page to an image and extract content via GPT-4o Vision.

    Returns:
        List of dicts with keys: page (int), text (str)
    """
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        text = await extract_page_with_vision(img_bytes)
        if text.strip():
            pages.append({"page": page_num + 1, "text": text})
        logger.info(f"  Extracted page {page_num + 1}/{len(doc)}")
    doc.close()
    return pages


async def parse_docx(file_bytes: bytes) -> list[dict]:
    """Extract text content from a DOCX file, grouped by page approximation."""
    doc = Document(io.BytesIO(file_bytes))
    full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return [{"page": 1, "text": full_text}]


async def parse_image(file_bytes: bytes) -> list[dict]:
    """Extract content from a single image file via GPT-4o Vision."""
    text = await extract_page_with_vision(file_bytes)
    return [{"page": 1, "text": text}]


async def parse_document(file_bytes: bytes, filename: str) -> list[dict]:
    """
    Route a document to the correct parser based on file extension.

    Args:
        file_bytes: Raw file content
        filename: Original filename (used to determine parser)

    Returns:
        List of page dicts with page number and extracted text
    """
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return await parse_pdf(file_bytes)
    elif ext in {".docx", ".doc"}:
        return await parse_docx(file_bytes)
    elif ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return await parse_image(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_text(text: str, page: int) -> list[dict]:
    """
    Split page text into overlapping chunks of ~600 tokens.

    Args:
        text: Extracted page text
        page: Page number for metadata

    Returns:
        List of chunk dicts with text and page number
    """
    tokens = tokenizer.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + CHUNK_SIZE, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = tokenizer.decode(chunk_tokens)
        chunks.append({"text": chunk_text, "page": page})
        if end == len(tokens):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ── Metadata extraction ───────────────────────────────────────────────────────

def extract_metadata(text: str, filename: str) -> dict:
    """
    Detect brand, model, and error codes from chunk text.

    Args:
        text: Chunk text to analyse
        filename: Source filename as fallback hint

    Returns:
        Metadata dict for Qdrant payload
    """
    brands = ["vaillant", "daikin", "intergas", "remeha", "bosch", "viessmann",
              "baxi", "saunier duval", "junkers", "worcester", "bulex", "ferroli"]
    detected_brand = next((b.capitalize() for b in brands if b in text.lower()), None)

    error_codes = re.findall(r'\b[A-Z]{1,2}[.\-]?\d{2,3}\b', text)

    return {
        "source_file": filename,
        "brand": detected_brand,
        "error_codes": list(set(error_codes)),
    }


# ── Embedding + upsert ────────────────────────────────────────────────────────

async def embed_chunks(chunks: list[str]) -> list[list[float]]:
    """
    Generate embeddings for a list of text chunks in batches.

    Args:
        chunks: List of text strings to embed

    Returns:
        List of embedding vectors
    """
    response = await openai.embeddings.create(
        model=EMBED_MODEL,
        input=chunks,
    )
    return [item.embedding for item in response.data]


async def upsert_to_qdrant(
    chunks: list[dict],
    filename: str,
    brand: str | None,
    model: str | None,
    document_type: str,
    language: str = "nl",
) -> int:
    """
    Embed chunks and upsert them into the Qdrant collection.

    Args:
        chunks: List of dicts with text and page keys
        filename: Source filename
        brand: Manually specified brand (overrides auto-detection)
        model: Manually specified device model
        document_type: E.g. 'servicehandleiding', 'installatiehandleiding'
        language: Document language code

    Returns:
        Number of chunks upserted
    """
    from qdrant_client.models import PointStruct

    texts = [c["text"] for c in chunks]
    embeddings = await embed_chunks(texts)
    client = get_qdrant()
    points = []

    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        meta = extract_metadata(chunk["text"], filename)
        chunk_id = int(hashlib.md5(
            f"{filename}_{chunk['page']}_{i}".encode()
        ).hexdigest(), 16) % (10**15)

        payload = {
            "text": chunk["text"],
            "source_file": filename,
            "brand": brand or meta["brand"],
            "model": model,
            "error_codes": meta["error_codes"],
            "document_type": document_type,
            "page": chunk["page"],
            "language": language,
        }
        points.append(PointStruct(id=chunk_id, vector=embedding, payload=payload))

    await client.upsert(collection_name=COLLECTION_NAME, points=points)
    return len(points)


# ── Public API ────────────────────────────────────────────────────────────────

KNOWN_BRANDS = [
    "vaillant", "daikin", "intergas", "remeha", "bosch", "viessmann",
    "baxi", "bulex", "ferroli", "saunier-duval", "junkers",
]


def detect_brand_from_path(filepath: str) -> str | None:
    """
    Detect brand from the parent folder name.

    Allows dropping a PDF in docs/vaillant/ without specifying brand manually.
    """
    parts = Path(filepath).parts
    for part in parts:
        if part.lower() in KNOWN_BRANDS:
            return part.capitalize()
    return None


async def ingest_file(
    file: UploadFile,
    brand: str | None = None,
    model: str | None = None,
    document_type: str = "handleiding",
    language: str = "nl",
) -> dict:
    """
    Full ingestion pipeline for an uploaded file.

    Parses the document, chunks the content, embeds and stores in Qdrant.

    Args:
        file: Uploaded file from FastAPI
        brand: Optional brand override (e.g. 'Vaillant')
        model: Optional model name (e.g. 'ecoTEC plus')
        document_type: Type of document
        language: Language code

    Returns:
        Dict with filename, pages processed, and chunks stored
    """
    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Bestandstype '{ext}' niet ondersteund. Gebruik: {', '.join(SUPPORTED_EXTENSIONS)}")

    # Auto-detect brand from folder structure if not explicitly provided
    if not brand:
        brand = detect_brand_from_path(file.filename)

    logger.info(f"Ingesting '{file.filename}' | brand={brand} | model={model}")
    file_bytes = await file.read()

    pages = await parse_document(file_bytes, file.filename)
    logger.info(f"  {len(pages)} pages extracted.")

    all_chunks = []
    for page_data in pages:
        all_chunks.extend(chunk_text(page_data["text"], page_data["page"]))

    count = await upsert_to_qdrant(all_chunks, file.filename, brand, model, document_type, language)
    logger.info(f"  {count} chunks stored in Qdrant.")

    return {"filename": file.filename, "pages": len(pages), "chunks": count}
