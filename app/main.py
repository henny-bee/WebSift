from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from selectolax.lexbor import LexborHTMLParser

from app.config import settings
from app.crawler import create_crawl_id, run_crawl_job
from app.extractor import extract_collections
from app.fetcher import fetch_page
from app.parser import (
    extract_node_images,
    parse_content,
    parse_json_ld,
    parse_links,
    parse_page_metadata,
    resolve_base_url,
)
from app.schemas import (
    ContentInfo,
    CrawlJobStatus,
    CrawlRequest,
    CrawlResultsPage,
    DiagnosticsInfo,
    ErrorDetail,
    PageInfo,
    ScrapeRequest,
    ScrapeResponse,
)
from app.security import ScraperError, enforce_auth_and_rate_limit, validate_safe_url
from app.storage import storage

app = FastAPI(
    title="Universal Web Scraper API",
    version="2.3.0",
    description="Deterministic, low-RAM multi-region web scraper API",
)

_background_tasks: set[asyncio.Task[Any]] = set()


@app.exception_handler(ScraperError)
async def scraper_error_handler(_request: Request, exc: ScraperError) -> JSONResponse:
    body = ErrorDetail(
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        details=exc.details,
    )
    return JSONResponse(status_code=exc.status_code, content=body.model_dump())


def _cache_key(req: ScrapeRequest) -> str:
    payload = {
        "url": req.url,
        "extract": sorted(req.extract),
        "regions": sorted(req.include_regions),
        "content_mode": req.content_mode,
        "max_items": req.max_items,
        "images": req.include_image_urls,
        "types": sorted(req.item_types),
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def execute_scrape(
    req: ScrapeRequest,
    *,
    html_override: str | None = None,
    final_url_override: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ScrapeResponse:
    max_items = min(req.max_items, settings.max_items_limit)
    key = _cache_key(req)

    if not req.bypass_cache and html_override is None and transport is None:
        cached = storage.get_cached_page(key)
        if cached:
            return ScrapeResponse.model_validate(cached)

    if html_override is not None:
        final_url = final_url_override or req.url
        html = html_override
        engine = "fixture"
        challenge_solved = False
    else:
        fetched = await fetch_page(req.url, transport=transport)
        final_url = fetched.final_url
        html = fetched.html
        engine = fetched.engine
        challenge_solved = fetched.challenge_solved

    tree = LexborHTMLParser(html)
    base_url = resolve_base_url(tree, final_url)
    sections = set(req.extract)

    page_info = parse_page_metadata(tree, base_url, final_url) if "metadata" in sections else PageInfo()
    structured = parse_json_ld(tree) if ("structured_data" in sections or "collections" in sections) else []

    images = (
        extract_node_images(tree.body or tree.root, base_url)[:max_items]  # type: ignore[arg-type]
        if ("images" in sections and req.include_image_urls and (tree.body or tree.root))
        else []
    )

    # Strip inline script/style/svg/template tags so CSS-in-JS never pollutes .text()
    tree.strip_tags(["script", "style", "svg", "template"])

    content_info = ContentInfo()
    if "text" in sections or "headings" in sections:
        parsed_content = parse_content(tree, content_mode=req.content_mode)
        content_info = ContentInfo(
            text=parsed_content.text if "text" in sections else "",
            headings=parsed_content.headings if "headings" in sections else [],
            blocks=parsed_content.blocks if "text" in sections else [],
        )

    links = parse_links(tree, base_url, final_url, max_items=max_items) if "links" in sections else []

    collections = []
    regions_scanned: list[str] = []
    detected_occ = 0
    unique_cnt = 0
    dedup_occ = 0

    if "collections" in sections:
        collections, regions_scanned, detected_occ, unique_cnt, dedup_occ = extract_collections(
            tree,
            structured,
            base_url,
            final_url,
            include_regions=req.include_regions,
            item_types=req.item_types,
            include_images=req.include_image_urls,
            max_items=max_items,
        )

    pagination_detected = bool(page_info.pagination and page_info.pagination.next_url)
    warnings: list[str] = []
    if "collections" in sections and not collections:
        warnings.append("no_collection_items_detected")

    resp = ScrapeResponse(
        requested_url=req.url,
        final_url=final_url,
        status="ok",
        page=page_info,
        content=content_info,
        links=links,
        images=images,
        structured_data=structured if "structured_data" in sections else [],
        collections=collections,
        diagnostics=DiagnosticsInfo(
            fetch_engine=engine,
            challenge_solved=challenge_solved,
            partial=False,
            truncated=False,
            regions_scanned=regions_scanned,
            detected_card_occurrences=detected_occ,
            unique_items=unique_cnt,
            deduplicated_occurrences=dedup_occ,
            pagination_detected=pagination_detected,
            warnings=warnings,
        ),
    )

    if not req.bypass_cache and html_override is None and transport is None:
        storage.set_cached_page(key, resp.model_dump())

    return resp


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": "2.3.0",
        "cf_solver_enabled": settings.cf_solver_enabled,
        "browser_enabled": settings.browser_enabled,
    }


@app.post("/v1/scrape", response_model=ScrapeResponse, dependencies=[Depends(enforce_auth_and_rate_limit)])
async def scrape_endpoint(req: ScrapeRequest) -> ScrapeResponse:
    return await execute_scrape(req)


@app.post(
    "/v1/crawls",
    status_code=202,
    response_model=CrawlJobStatus,
    dependencies=[Depends(enforce_auth_and_rate_limit)],
)
async def start_crawl(req: CrawlRequest) -> CrawlJobStatus:
    seed_url = validate_safe_url(req.url)
    max_pages = min(req.max_pages, settings.crawl_max_pages_limit)
    max_depth = min(req.max_depth, settings.crawl_max_depth_limit)
    job_id = create_crawl_id()
    storage.create_crawl_job(job_id, seed_url, max_pages, max_depth)

    task = asyncio.create_task(run_crawl_job(job_id, req, execute_scrape))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return CrawlJobStatus(
        job_id=job_id,
        seed_url=seed_url,
        status="queued",
        pages_visited=0,
        max_pages=max_pages,
        max_depth=max_depth,
        errors=[],
    )


@app.get(
    "/v1/crawls/{job_id}",
    response_model=CrawlJobStatus,
    dependencies=[Depends(enforce_auth_and_rate_limit)],
)
async def get_crawl_status(job_id: str) -> CrawlJobStatus:
    job = storage.get_crawl_job(job_id)
    if not job:
        raise ScraperError(404, "invalid_request", f"Crawl job {job_id!r} not found")
    return CrawlJobStatus(**job)


@app.get(
    "/v1/crawls/{job_id}/results",
    response_model=CrawlResultsPage,
    dependencies=[Depends(enforce_auth_and_rate_limit)],
)
async def get_crawl_results(
    job_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
) -> CrawlResultsPage:
    job = storage.get_crawl_job(job_id)
    if not job:
        raise ScraperError(404, "invalid_request", f"Crawl job {job_id!r} not found")
    items, next_cursor = storage.get_crawl_results(job_id, cursor=cursor, limit=limit)
    return CrawlResultsPage(
        job_id=job_id,
        status=job["status"],
        results=[ScrapeResponse.model_validate(it) for it in items],
        next_cursor=next_cursor,
    )


@app.delete(
    "/v1/crawls/{job_id}",
    response_model=CrawlJobStatus,
    dependencies=[Depends(enforce_auth_and_rate_limit)],
)
async def cancel_crawl(job_id: str) -> CrawlJobStatus:
    job = storage.get_crawl_job(job_id)
    if not job:
        raise ScraperError(404, "invalid_request", f"Crawl job {job_id!r} not found")
    storage.update_crawl_job(job_id, status="cancelled")
    updated = storage.get_crawl_job(job_id)
    return CrawlJobStatus(**updated)  # type: ignore[arg-type]
