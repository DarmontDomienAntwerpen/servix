"""
scraper.py — Automatic PDF discovery and ingestion from manufacturer documentation pages.

Uses Playwright (headless Chromium) to render JavaScript-heavy manufacturer sites,
finds all PDF links, downloads and ingests them into Qdrant.
"""

import asyncio
import io
import logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from fastapi import UploadFile

from ingest import ingest_file, detect_brand_from_path

logger = logging.getLogger(__name__)

_seen_urls: set[str] = set()

CRAWL_DELAY = 2.0  # seconds between requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ── Page rendering ────────────────────────────────────────────────────────────

async def render_page(url: str) -> str | None:
    """
    Load a page with Playwright (full JS execution) and return rendered HTML.

    Waits for network to be idle so dynamic content is fully loaded.
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.set_extra_http_headers(HEADERS)
            await page.goto(url, wait_until="networkidle", timeout=30000)
            # Extra wait for lazy-loaded content
            await page.wait_for_timeout(2000)
            html = await page.content()
            await browser.close()
            return html
    except Exception as e:
        logger.warning(f"Playwright failed for {url}: {e}")
        return None


# ── PDF link extraction ───────────────────────────────────────────────────────

def extract_pdf_links(html: str, base_url: str) -> list[str]:
    """Find all PDF download links in rendered HTML."""
    soup = BeautifulSoup(html, "html.parser")
    pdf_urls = []

    # Standard <a href> links
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        absolute = urljoin(base_url, href)
        if _is_pdf_url(absolute) and absolute not in _seen_urls:
            pdf_urls.append(absolute)

    # data-href, data-file, data-url attributes (used by some manufacturer portals)
    for tag in soup.find_all(True):
        for attr in ("data-href", "data-file", "data-url", "data-download"):
            val = tag.get(attr, "")
            if val:
                absolute = urljoin(base_url, val)
                if _is_pdf_url(absolute) and absolute not in _seen_urls:
                    pdf_urls.append(absolute)

    return list(set(pdf_urls))


def extract_subpage_links(html: str, base_url: str, allowed_domain: str) -> list[str]:
    """Find internal sub-pages worth crawling for more PDFs."""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    keywords = {
        "download", "document", "manual", "handleiding", "techni",
        "product", "support", "ondersteuning", "service", "documentation",
        "brochure", "installatie", "gebruiks",
    }

    for a in soup.find_all("a", href=True):
        absolute = urljoin(base_url, a["href"])
        parsed = urlparse(absolute)
        if parsed.netloc != allowed_domain:
            continue
        if any(k in absolute.lower() for k in keywords):
            links.append(absolute)

    return list(set(links))


def _is_pdf_url(url: str) -> bool:
    parsed = urlparse(url.lower())
    return (
        parsed.path.endswith(".pdf")
        or "/pdf/" in parsed.path
        or "filetype=pdf" in parsed.query
        or "format=pdf" in parsed.query
        or "type=pdf" in parsed.query
    )


# ── Download + ingest ─────────────────────────────────────────────────────────

async def download_and_ingest(
    client: httpx.AsyncClient,
    pdf_url: str,
    brand: str | None,
    document_type: str,
    language: str,
) -> dict:
    """Download a single PDF and push it through the ingest pipeline."""
    _seen_urls.add(pdf_url)

    filename = pdf_url.split("/")[-1].split("?")[0] or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    resolved_brand = brand or detect_brand_from_path(pdf_url)

    try:
        r = await client.get(pdf_url, headers=HEADERS, follow_redirects=True, timeout=60)
        r.raise_for_status()

        content_type = r.headers.get("content-type", "")
        if "pdf" not in content_type and not pdf_url.lower().endswith(".pdf"):
            return {"url": pdf_url, "status": "skipped", "reason": "not a PDF"}

        if len(r.content) < 1000:
            return {"url": pdf_url, "status": "skipped", "reason": "file too small"}

        mock_file = UploadFile(filename=filename, file=io.BytesIO(r.content))
        result = await ingest_file(
            mock_file,
            brand=resolved_brand,
            document_type=document_type,
            language=language,
        )
        logger.info(f"✓ {filename}: {result['pages']}p {result['chunks']} chunks")
        return {"url": pdf_url, "status": "ok", **result}

    except Exception as e:
        logger.error(f"✗ {pdf_url}: {e}")
        return {"url": pdf_url, "status": "error", "error": str(e)}


# ── Main entry point ──────────────────────────────────────────────────────────

async def scrape_and_ingest(
    start_url: str,
    brand: str | None = None,
    document_type: str = "handleiding",
    language: str = "nl",
    max_pdfs: int = 200,
    depth: int = 2,
) -> dict:
    """
    Scrape a manufacturer documentation page and ingest all found PDFs.

    Uses Playwright for full JS rendering — works on modern manufacturer sites.
    Crawls up to `depth` levels deep, respects rate limits.
    """
    _seen_urls.clear()
    allowed_domain = urlparse(start_url).netloc
    pdf_urls: list[str] = []

    logger.info(f"Scraping: {start_url}")

    # Level 1 — render and extract from starting page
    html = await render_page(start_url)
    if not html:
        return {"status": "error", "detail": f"Could not load {start_url}"}

    pdf_urls.extend(extract_pdf_links(html, start_url))
    logger.info(f"Level 1: {len(pdf_urls)} PDFs on start page")

    # Level 2 — follow sub-pages
    if depth >= 2:
        subpages = extract_subpage_links(html, start_url, allowed_domain)
        logger.info(f"Level 2: following {min(len(subpages), 30)} sub-pages")

        for sub_url in subpages[:30]:
            await asyncio.sleep(CRAWL_DELAY)
            sub_html = await render_page(sub_url)
            if sub_html:
                new = [u for u in extract_pdf_links(sub_html, sub_url) if u not in pdf_urls]
                pdf_urls.extend(new)
                if new:
                    logger.info(f"  +{len(new)} from {sub_url}")

    pdf_urls = list(set(pdf_urls))[:max_pdfs]
    logger.info(f"Total: {len(pdf_urls)} unique PDFs to ingest")

    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i, url in enumerate(pdf_urls, 1):
            logger.info(f"[{i}/{len(pdf_urls)}] {url[-70:]}")
            result = await download_and_ingest(client, url, brand, document_type, language)
            results.append(result)
            await asyncio.sleep(CRAWL_DELAY)

    ok    = [r for r in results if r["status"] == "ok"]
    skip  = [r for r in results if r["status"] == "skipped"]
    error = [r for r in results if r["status"] == "error"]

    return {
        "status": "ok",
        "start_url": start_url,
        "found": len(pdf_urls),
        "ingested": len(ok),
        "skipped": len(skip),
        "errors": len(error),
        "total_chunks": sum(r.get("chunks", 0) for r in ok),
        "results": results,
    }
