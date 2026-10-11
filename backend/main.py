"""
main.py — FastAPI application entrypoint for Servix.
Handles app lifecycle, health checks, and WhatsApp webhook routing.
"""

import os
import logging
from contextlib import asynccontextmanager

from datetime import date
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

from typing import Optional

from db import init_db, close_db, get_pool
from qdrant_setup import init_qdrant
from whatsapp import parse_incoming_message, send_message, validate_signature
from ingest import ingest_file, SUPPORTED_EXTENSIONS
from agent import run_agent
from memory import get_or_create_user

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Servix backend...")
    await init_db()
    await init_qdrant()
    logger.info("Servix backend ready.")
    yield
    await close_db()
    logger.info("Servix backend stopped.")


app = FastAPI(title="Servix API", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Frontend ─────────────────────────────────────────────────────────────────

@app.get("/")
async def serve_ui():
    return FileResponse("static/index.html")


# ── Chat (test UI) ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    session_id: str = ""


@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    """Direct chat endpoint for the test UI — bypasses WhatsApp."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        company = await conn.fetchrow("SELECT id FROM companies LIMIT 1")

    if not company:
        raise HTTPException(status_code=503, detail="Geen bedrijf geconfigureerd.")

    user = await get_or_create_user(phone_number="ui_tester", company_id=company["id"])
    session_id = req.session_id or f"ui_{date.today().isoformat()}"

    context = {
        "user_id": user["id"],
        "company_id": company["id"],
        "session_id": session_id,
    }

    response = await run_agent(user_message=req.message, context=context)
    return {"response": response}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Check that the API, database, and Qdrant are reachable."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_status = "ok"
    except Exception as e:
        logger.error(f"Health check DB failed: {e}")
        db_status = "error"

    return {
        "status": "ok",
        "postgres": db_status,
        "qdrant": "ok",
    }


# ── WhatsApp webhook (Twilio) ─────────────────────────────────────────────────

@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive and route incoming WhatsApp messages via Twilio.

    Validates the Twilio request signature, parses the form-encoded payload,
    and dispatches to the agent. Always returns 200 quickly — Twilio will
    retry if we're too slow.
    """
    form = await request.form()
    form_dict = dict(form)

    signature = request.headers.get("X-Twilio-Signature", "")
    if not validate_signature(str(request.url), form_dict, signature):
        logger.warning("Invalid Twilio signature, rejecting webhook request.")
        raise HTTPException(status_code=403, detail="Invalid signature.")

    message = parse_incoming_message(form_dict)

    if not message:
        return {"status": "ignored"}

    sender = message["from"]
    msg_type = message["type"]

    logger.info(f"Incoming {msg_type} message from {sender}")

    if msg_type not in ("text", "image", "audio"):
        return {"status": "ignored"}

    try:
        pool = await get_pool()

        # Resolve company — default fallback voor nu, multi-tenant routing koppelt dit later
        async with pool.acquire() as conn:
            company = await conn.fetchrow("SELECT id FROM companies LIMIT 1")

        if not company:
            logger.warning(f"No company configured yet, dropping message from {sender}")
            return {"status": "no_company"}

        company_id = company["id"]
        user = await get_or_create_user(phone_number=sender, company_id=company_id)

        # Session = monteur + datum (één sessie per dag)
        today = date.today().isoformat()
        session_id = f"{sender}_{today}"

        context = {
            "user_id": user["id"],
            "company_id": company_id,
            "session_id": session_id,
        }

        response_text = await run_agent(
            user_message=message["text"] or f"[{msg_type} bericht ontvangen]",
            context=context,
        )
        await send_message(to=sender, text=response_text)

    except Exception as e:
        logger.error(f"Agent failed for {sender}: {e}")
        await send_message(
            to=sender,
            text="⚠️ Er is een technisch probleem opgetreden. Probeer opnieuw.",
        )

    return {"status": "ok"}


# ── Document upload ───────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    brand: Optional[str] = None,
    model: Optional[str] = None,
    document_type: str = "handleiding",
    language: str = "nl",
):
    """
    Upload a manual or document to the Qdrant knowledge base.

    Supported formats: PDF, DOCX, JPG, PNG, WEBP.
    Uses GPT-4o Vision per page — captures text, tables, error codes, and diagrams.
    """
    try:
        result = await ingest_file(file, brand=brand, model=model,
                                   document_type=document_type, language=language)
        return {"status": "ok", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Upload failed for '{file.filename}': {e}")
        raise HTTPException(status_code=500, detail="Verwerking mislukt.")


@app.post("/upload/scrape")
async def scrape_documents(
    url: str,
    brand: Optional[str] = None,
    document_type: str = "handleiding",
    language: str = "nl",
    max_pdfs: int = 200,
    depth: int = 2,
):
    """
    Scrape a manufacturer documentation page and auto-ingest all found PDFs.

    Point it at a downloads/documentation page, it crawls up to 2 levels deep,
    finds all PDFs, downloads and ingests them automatically.
    """
    from scraper import scrape_and_ingest
    try:
        result = await scrape_and_ingest(
            start_url=url,
            brand=brand,
            document_type=document_type,
            language=language,
            max_pdfs=max_pdfs,
            depth=depth,
        )
        return result
    except Exception as e:
        logger.error(f"Scrape failed for '{url}': {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/boss")
async def serve_boss():
    return FileResponse("static/boss.html")


# ── Document upload ───────────────────────────────────────────────────────────

@app.post("/upload/folder")
async def upload_folder(folder: str = "docs"):
    """
    Ingest all documents from a docs/ subfolder on the server.

    Useful for bulk uploading an entire brand folder.
    Example: POST /upload/folder?folder=vaillant
    """
    from ingest import ingest_file, detect_brand_from_path, SUPPORTED_EXTENSIONS
    from pathlib import Path
    import io
    from fastapi import UploadFile
    from starlette.datastructures import UploadFile as StarletteUploadFile

    base = Path("/app/docs") / folder
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Map '{folder}' niet gevonden.")

    results = []
    for filepath in base.rglob("*"):
        if filepath.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        file_bytes = filepath.read_bytes()
        mock_file = UploadFile(
            filename=str(filepath.relative_to(Path("/app/docs"))),
            file=io.BytesIO(file_bytes),
        )
        try:
            result = await ingest_file(mock_file)
            results.append({"file": filepath.name, **result})
        except Exception as e:
            logger.error(f"Bulk ingest failed for {filepath.name}: {e}")
            results.append({"file": filepath.name, "error": str(e)})

    return {"status": "ok", "processed": len(results), "results": results}
