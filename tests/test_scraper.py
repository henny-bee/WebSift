from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.fetcher import fetch_page
from app.main import app, execute_scrape
from app.schemas import CrawlRequest, ScrapeRequest
from app.crawler import create_crawl_id, run_crawl_job
from app.security import ScraperError, is_public_ip, validate_safe_url
from app.storage import storage

FIXTURES = Path(__file__).parent / "fixtures"
client = TestClient(app)


def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_multi_region_mal_style_listing_and_dedupe() -> None:
    html = (FIXTURES / "mal_news_multi_region.html").read_text(encoding="utf-8")
    req = ScrapeRequest(url="https://example.com/news")
    res = await execute_scrape(req, html_override=html)

    assert res.status == "ok"
    assert res.page.title == "Example Anime & Industry News"
    assert res.page.pagination is not None
    assert res.page.pagination.next_url == "https://example.com/news?p=2"
    assert res.diagnostics.pagination_detected is True
    assert res.diagnostics.regions_scanned == ["main", "sidebar"]

    # 2 main cards + 2 sidebar cards = 4 occurrences, 1 shared URL (/news/101) -> 3 unique items
    assert res.diagnostics.detected_card_occurrences == 4
    assert res.diagnostics.unique_items == 3
    assert res.diagnostics.deduplicated_occurrences == 1

    assert len(res.collections) == 1
    group = res.collections[0]
    assert group.type == "article"
    assert len(group.items) == 3

    first = group.items[0]
    assert first.title == "Winter 2027 Visual Revealed"
    assert first.url == "https://example.com/news/101"
    assert first.image_url == "https://cdn.example.com/thumb-101.webp"
    assert first.attributes["author"] == "EditorA"
    assert first.attributes["published_at_text"] == "7 hours ago"
    assert first.attributes["published_at"] is None
    assert first.attributes["comments_count"] == 14
    assert first.attributes["tags"] == ["Anime", "Winter 2027"]
    assert first.attributes["placements"] == [
        {"region": "main", "section": "Anime News", "position": 1},
        {"region": "sidebar", "section": "Winter 2027", "position": 1},
    ]

    third = group.items[2]
    assert third.title == "Fantasy Light Novel Gets TV Series"
    assert third.url == "https://example.com/news/103"
    assert third.image_url == "https://cdn.example.com/thumb-103.webp"
    assert third.attributes["placements"] == [
        {"region": "sidebar", "section": "New Anime", "position": 1},
    ]


@pytest.mark.anyio
async def test_table_based_forum_listing() -> None:
    html = (FIXTURES / "forum_table_listing.html").read_text(encoding="utf-8")
    req = ScrapeRequest(url="https://forum.example.com/general")
    res = await execute_scrape(req, html_override=html)

    assert len(res.collections) == 1
    threads = res.collections[0]
    assert threads.type == "forum_thread"
    assert len(threads.items) == 2
    assert threads.items[0].title == "Best self-hosted Python HTML parsers in 2026?"
    assert threads.items[0].url == "https://forum.example.com/threads/501"
    assert threads.items[0].attributes["author"] == "dev_tiar"
    assert threads.items[0].attributes["replies"] == 42
    assert threads.items[0].attributes["last_activity_text"] == "3 hours ago"


@pytest.mark.anyio
async def test_ecommerce_jsonld_and_no_og_image_leak() -> None:
    html = (FIXTURES / "ecommerce_jsonld.html").read_text(encoding="utf-8")
    req = ScrapeRequest(url="https://shop.example.com/hardware")
    res = await execute_scrape(req, html_override=html)

    assert res.page.metadata.get("og:image") == "https://shop.example.com/assets/store-og.webp"
    assert len(res.collections) == 1
    products = res.collections[0].items
    assert len(products) == 2

    # First product merged DOM + JSON-LD fields
    p1 = products[0]
    assert p1.title == "Laptop Alpha 14"
    assert p1.image_url == "https://cdn.example.com/products/alpha-14.webp"
    assert p1.attributes["brand"] == "ExampleTech"
    assert p1.attributes["sku"] == "ALP-14"
    assert p1.attributes["price"]["amount"] == 799.0

    # Second product has no local image; MUST NOT inherit page og:image
    p2 = products[1]
    assert p2.title == "USB4 Pro Dock (No Photo Yet)"
    assert p2.image_url is None
    assert p2.images == []
    assert p2.attributes["price"]["amount"] == 129.0


def test_ssrf_url_and_ip_validation() -> None:
    assert is_public_ip("93.184.216.34") is True
    assert is_public_ip("127.0.0.1") is False
    assert is_public_ip("10.0.0.1") is False
    assert is_public_ip("192.168.1.50") is False
    assert is_public_ip("169.254.169.254") is False
    assert is_public_ip("100.64.0.1") is False
    assert is_public_ip("::1") is False

    for bad_url in (
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://localhost:8000/admin",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://user:pass@example.com/",
        "https://example.com:8443/secret",
    ):
        with pytest.raises(ScraperError):
            validate_safe_url(bad_url, allow_private=False)


@pytest.mark.anyio
async def test_streaming_byte_cap_and_mime_guard() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/too-big":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"x" * 2048,
            )
        if request.url.path == "/binary":
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"\x89PNG\r\n",
            )
        if request.url.path == "/redirect-ssrf":
            return httpx.Response(
                302,
                headers={"location": "http://169.254.169.254/computeMetadata/v1/"},
            )
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")

    transport = httpx.MockTransport(handler)
    with patch("app.security.socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]

        # 1. Stream exceeds max_bytes -> 413 response_too_large
        with pytest.raises(ScraperError) as exc_info:
            await fetch_page("https://example.com/too-big", max_bytes=512, transport=transport)
        assert exc_info.value.status_code == 413
        assert exc_info.value.code == "response_too_large"

        # 2. Unsupported MIME type -> 422 unsupported_content_type
        with pytest.raises(ScraperError) as exc_mime:
            await fetch_page("https://example.com/binary", transport=transport)
        assert exc_mime.value.status_code == 422
        assert exc_mime.value.code == "unsupported_content_type"

        # 3. Redirect to GCP metadata IP -> 403 blocked
        with pytest.raises(ScraperError) as exc_redir:
            await fetch_page("https://example.com/redirect-ssrf", transport=transport)
        assert exc_redir.value.status_code == 403


@pytest.mark.anyio
async def test_bounded_crawl_lifecycle() -> None:
    page1_html = """
    <html><head><title>Page 1</title><link rel="next" href="https://example.com/page-2"></head>
    <body><main><h1>P1</h1><p>Welcome</p></main></body></html>
    """
    page2_html = """
    <html><head><title>Page 2</title></head>
    <body><main><h1>P2</h1><p>Second page</p></main></body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page-2":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=page2_html)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=page1_html)

    transport = httpx.MockTransport(handler)
    with patch("app.security.socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        job_id = create_crawl_id()
        storage.create_crawl_job(job_id, "https://example.com/page-1", max_pages=2, max_depth=1)
        crawl_req = CrawlRequest(url="https://example.com/page-1", max_pages=2, max_depth=1)
        await run_crawl_job(job_id, crawl_req, execute_scrape, transport=transport)

        job = storage.get_crawl_job(job_id)
        assert job is not None
        assert job["status"] == "completed"
        assert job["pages_visited"] == 2

        results, next_cursor = storage.get_crawl_results(job_id, cursor=0, limit=10)
        assert len(results) == 2
        assert next_cursor is None
        assert results[0]["page"]["title"] == "Page 1"
        assert results[1]["page"]["title"] == "Page 2"
