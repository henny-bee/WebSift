from __future__ import annotations

import asyncio
import random
import uuid
from collections import deque
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.schemas import CrawlRequest, ScrapeRequest, ScrapeResponse
from app.security import normalize_canonical_url, validate_safe_url
from app.storage import storage


async def run_crawl_job(
    job_id: str,
    req: CrawlRequest,
    scrape_fn,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    max_pages = min(req.max_pages, settings.crawl_max_pages_limit)
    max_depth = min(req.max_depth, settings.crawl_max_depth_limit)
    seed_url = validate_safe_url(req.url)
    seed_host = (urlparse(seed_url).hostname or "").lower().removeprefix("www.")

    queue: deque[tuple[str, int]] = deque([(seed_url, 0)])
    seen: set[str] = {normalize_canonical_url(seed_url) or seed_url}
    visited = 0
    errors: list[str] = []

    storage.update_crawl_job(job_id, status="running", pages_visited=0, errors=[])

    while queue and visited < max_pages:
        job_state = storage.get_crawl_job(job_id)
        if not job_state or job_state["status"] == "cancelled":
            return

        current_url, depth = queue.popleft()
        if visited > 0 and transport is None:
            await asyncio.sleep(
                random.uniform(settings.crawl_min_jitter_s, settings.crawl_max_jitter_s)
            )

        try:
            page_req = ScrapeRequest(
                url=current_url,
                extract=req.extract,
                bypass_cache=False,
            )
            result: ScrapeResponse = await scrape_fn(page_req, transport=transport)
            visited += 1
            storage.append_crawl_result(job_id, current_url, result.model_dump())
            storage.update_crawl_job(job_id, pages_visited=visited, errors=errors)

            if depth < max_depth:
                candidate_urls: list[str] = []
                if result.page.pagination and result.page.pagination.next_url:
                    candidate_urls.append(result.page.pagination.next_url)
                for link in result.links:
                    if not req.same_domain_only or link.internal:
                        candidate_urls.append(link.url)

                for cand in candidate_urls:
                    norm = normalize_canonical_url(cand, result.final_url)
                    if not norm or norm in seen:
                        continue
                    cand_host = (urlparse(norm).hostname or "").lower().removeprefix("www.")
                    if req.same_domain_only and cand_host != seed_host and not cand_host.endswith("." + seed_host):
                        continue
                    seen.add(norm)
                    queue.append((norm, depth + 1))

        except Exception as exc:
            errors.append(f"{current_url}: {exc}")
            storage.update_crawl_job(job_id, pages_visited=visited, errors=errors)

    final_state = storage.get_crawl_job(job_id)
    if final_state and final_state["status"] != "cancelled":
        final_status = "completed" if visited > 0 and not errors else ("partial" if visited > 0 else "failed")
        storage.update_crawl_job(job_id, status=final_status, pages_visited=visited, errors=errors)


def create_crawl_id() -> str:
    return f"crawl_{uuid.uuid4().hex[:12]}"
