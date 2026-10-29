# Universal Web Scraper API

A self-hosted Python REST API that extracts structured page data and typed card collections (`article`, `product`, `forum_thread`, `documentation_entry`, `organization`, `generic_card`) from web pages without using LLMs or external AI APIs. Built to run within a 4 GB RAM shared VM alongside Docker and Caddy.

## Features

- **Single-page extraction (`POST /v1/scrape`):** Returns page metadata, Open Graph, JSON-LD, readable text blocks, headings, links, images (`src`, `data-src`, `srcset`, `<picture>`, `<noscript>`), pagination (`page.pagination.next_url`), and repeated content cards.
- **Zero-config multi-region card detection:** Scans both `main` and `sidebar` regions, pairs each card with its own local thumbnail and metadata (`author`, `published_at`, `published_at_text`, `comments_count`, `tags`, `price`), and merges duplicate URLs across regions into `attributes.placements[]`.
- **Tiered HTTP fetching:**
  1. **Tier 1 (`curl_cffi` + `httpx`):** Uses Chrome TLS/JA3 and HTTP/2 fingerprinting by default, with automatic zero-RAM rotation to `safari` TLS on HTTP 401/403 blocks and a 10 MB streaming byte cap.
  2. **Tier 2 (`cloudflare-solver`, optional):** Docker sidecar (`port 407`) invoked only on HTTP 403/503 Cloudflare IUAM/Turnstile challenges, caching `cf_clearance` and `User-Agent` per domain in SQLite.
- **SSRF and DNS rebinding guard:** Blocks private RFC1918 networks, loopback, link-local, CGNAT, and cloud metadata (`169.254.169.254`) on initial requests and on every manual redirect hop.
- **Async site crawl (`POST /v1/crawls`):** Bounded multi-page crawler backed by SQLite WAL with politeness jitter, per-domain concurrency limits, cursor pagination, and cooperative cancellation.
- **Optional YAML domain overrides:** Works out of the box with zero YAML files; custom selector rules can be placed in `app/rules/*.yaml` when needed.

## Project Structure

```text
├── app/
│   ├── main.py          # FastAPI routes (/v1/scrape, /v1/crawls, /healthz)
│   ├── config.py        # Environment and .env configuration
│   ├── schemas.py       # Pydantic request and response contracts
│   ├── security.py      # Bearer auth, rate limit, SSRF/IP validator
│   ├── fetcher.py       # Streaming curl_cffi/httpx client and solver hook
│   ├── parser.py        # Layer 1 DOM metadata, JSON-LD, text, links, images
│   ├── extractor.py     # Layer 2/2b/3 multi-region card and typed extractor
│   ├── crawler.py       # Async multi-page crawl worker
│   └── storage.py       # SQLite WAL persistence (cache, cf_clearance, jobs)
├── tests/
│   ├── fixtures/        # HTML test fixtures (multi-region, forum table, e-commerce)
│   └── test_scraper.py  # Pytest suite
├── compose.yaml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── LICENSE
```

## Quick Start

### Local Development

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install fastapi "uvicorn[standard]" httpx selectolax curl_cffi pytest anyio

cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8787 --workers 1
```

### Docker Compose

```bash
# Standard static mode (low RAM)
docker compose up -d --build

# With optional cloudflare-solver sidecar
docker compose --profile solver up -d --build
```

## API Usage

### 1. Scrape a Single Page (`POST /v1/scrape`)

```bash
curl -X POST http://localhost:8787/v1/scrape \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/news/",
    "extract": ["metadata", "text", "headings", "links", "images", "structured_data", "collections"],
    "include_regions": ["main", "sidebar"],
    "max_items": 50
  }'
```

### 2. Start an Async Crawl (`POST /v1/crawls`)

```bash
# Start job
curl -X POST http://localhost:8787/v1/crawls \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/catalog", "max_pages": 5, "max_depth": 1}'

# Check status
curl http://localhost:8787/v1/crawls/<job_id>

# Fetch paginated results
curl "http://localhost:8787/v1/crawls/<job_id>/results?limit=10&cursor=0"

# Cancel job
curl -X DELETE http://localhost:8787/v1/crawls/<job_id>
```

## Running Tests

```bash
pytest -v
```

## License

Released under the [MIT License](./LICENSE).
