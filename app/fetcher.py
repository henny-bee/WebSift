from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings
from app.security import ScraperError, validate_safe_url
from app.storage import storage

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}

ALLOWED_MIME_PREFIXES = (
    "text/html",
    "application/xhtml+xml",
    "application/xml",
    "text/xml",
    "text/plain",
)

_global_sem = asyncio.Semaphore(settings.global_concurrency)
_domain_sems: dict[str, asyncio.Semaphore] = {}


def _get_domain_sem(domain: str) -> asyncio.Semaphore:
    sem = _domain_sems.get(domain)
    if sem is None:
        sem = asyncio.Semaphore(settings.per_domain_concurrency)
        _domain_sems[domain] = sem
    return sem


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    engine: str
    challenge_solved: bool = False
    truncated: bool = False


def _is_cf_challenge(status_code: int, headers: dict[str, str], body_sample: str) -> bool:
    if status_code not in {403, 503}:
        return False
    cf_mitigated = headers.get("cf-mitigated", "").lower()
    server = headers.get("server", "").lower()
    if "challenge" in cf_mitigated:
        return True
    if "cloudflare" in server and (
        "Just a moment..." in body_sample
        or "cf-turnstile" in body_sample
        or "_cf_chl_opt" in body_sample
    ):
        return True
    return False


def _check_content_type(content_type: str) -> None:
    mime = (content_type or "text/html").split(";")[0].strip().lower()
    if mime and not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise ScraperError(
            422,
            "unsupported_content_type",
            f"Unsupported content type: {mime!r}; only HTML/XML/text pages are supported",
        )


def _check_content_length_header(headers: dict[str, str], max_bytes: int) -> None:
    cl = headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > max_bytes:
        raise ScraperError(
            413,
            "response_too_large",
            f"Response Content-Length ({cl} bytes) exceeds limit ({max_bytes} bytes)",
        )


async def _solve_cf_iuam(url: str) -> tuple[str, str] | None:
    if not settings.cf_solver_enabled:
        return None
    validate_safe_url(url)
    payload: dict[str, str] = {"url": url}
    if settings.default_proxy_url:
        payload["proxy"] = settings.default_proxy_url
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(f"{settings.cf_solver_url.rstrip('/')}/iuam", json=payload)
            if resp.status_code != 200:
                return None
            data = resp.json()
            hdrs = data.get("headers") or {}
            cookie = hdrs.get("Cookie") or hdrs.get("cookie")
            ua = hdrs.get("User-Agent") or hdrs.get("user-agent")
            if cookie and ua:
                domain = urlparse(url).hostname or ""
                storage.set_cf_clearance(domain, cookie, ua)
                return cookie, ua
    except Exception:
        return None
    return None


async def _fetch_once_httpx(
    url: str,
    headers: dict[str, str],
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, dict[str, str], bytes]:
    timeout = httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            norm_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code in {301, 302, 303, 307, 308}:
                return resp.status_code, norm_headers, b""
            _check_content_type(norm_headers.get("content-type", ""))
            _check_content_length_header(norm_headers, max_bytes)
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(chunk_size=16384):
                total += len(chunk)
                if total > max_bytes:
                    raise ScraperError(
                        413,
                        "response_too_large",
                        f"Streamed response exceeded {max_bytes} bytes limit",
                    )
                chunks.append(chunk)
            return resp.status_code, norm_headers, b"".join(chunks)


async def _fetch_once_curl(
    url: str,
    headers: dict[str, str],
    max_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    async with CurlAsyncSession(
        impersonate="chrome",
        timeout=settings.read_timeout_s,
        allow_redirects=False,
         max_recv_speed=0,
         proxy=settings.default_proxy_url,
    ) as session:
        resp = await session.get(url, headers=headers, stream=True)
        try:
            norm_headers = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code in {301, 302, 303, 307, 308}:
                return resp.status_code, norm_headers, b""
            _check_content_type(norm_headers.get("content-type", ""))
            _check_content_length_header(norm_headers, max_bytes)
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_content():
                total += len(chunk)
                if total > max_bytes:
                    raise ScraperError(
                        413,
                        "response_too_large",
                        f"Streamed response exceeded {max_bytes} bytes limit",
                    )
                chunks.append(chunk)
            return resp.status_code, norm_headers, b"".join(chunks)
        finally:
            await resp.aclose()


async def fetch_page(
    url: str,
    *,
    max_bytes: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    limit = max_bytes or settings.max_body_bytes
    current_url = validate_safe_url(url)
    domain = urlparse(current_url).hostname or "default"

    async with _global_sem, _get_domain_sem(domain):
        headers = dict(DEFAULT_HEADERS)
        cached_cf = storage.get_cf_clearance(domain)
        if cached_cf:
            headers["Cookie"], headers["User-Agent"] = cached_cf

        use_curl = HAS_CURL_CFFI and transport is None
        engine = "curl_cffi" if use_curl else "httpx"
        challenge_solved = False

        for _hop in range(settings.max_redirects + 1):
            try:
                if use_curl:
                    status, resp_headers, raw_bytes = await _fetch_once_curl(
                        current_url, headers, limit
                    )
                else:
                    status, resp_headers, raw_bytes = await _fetch_once_httpx(
                        current_url, headers, limit, transport=transport
                    )
            except ScraperError:
                raise
            except httpx.TimeoutException as exc:
                raise ScraperError(504, "upstream_timeout", str(exc), retryable=True) from exc
            except Exception as exc:
                raise ScraperError(502, "fetch_failed", f"Fetch error: {exc}", retryable=True) from exc

            if status in {301, 302, 303, 307, 308}:
                location = resp_headers.get("location")
                if not location:
                    raise ScraperError(502, "fetch_failed", "Redirect missing Location header")
                next_url = urljoin(current_url, location)
                current_url = validate_safe_url(next_url)
                continue

            text = raw_bytes.decode("utf-8", errors="replace")
            if _is_cf_challenge(status, resp_headers, text[:4096]):
                solved = await _solve_cf_iuam(current_url)
                if solved:
                    headers["Cookie"], headers["User-Agent"] = solved
                    challenge_solved = True
                    if use_curl:
                        status, resp_headers, raw_bytes = await _fetch_once_curl(
                            current_url, headers, limit
                        )
                    else:
                        status, resp_headers, raw_bytes = await _fetch_once_httpx(
                            current_url, headers, limit, transport=transport
                        )
                    text = raw_bytes.decode("utf-8", errors="replace")

            if status in {401, 403}:
                raise ScraperError(
                    403,
                    "blocked_by_site_policy",
                    f"Upstream returned HTTP {status} for {current_url}",
                )
            if status >= 400:
                raise ScraperError(
                    502,
                    "fetch_failed",
                    f"Upstream returned HTTP {status} for {current_url}",
                    retryable=status >= 500,
                )

            return FetchResult(
                requested_url=url,
                final_url=current_url,
                status_code=status,
                content_type=resp_headers.get("content-type", "text/html"),
                html=text,
                engine=engine,
                challenge_solved=challenge_solved,
            )

        raise ScraperError(
            502, "fetch_failed", f"Exceeded maximum redirects ({settings.max_redirects})"
        )
