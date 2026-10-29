from __future__ import annotations

import ipaddress
import socket
import time
from collections import defaultdict, deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from fastapi import Header, HTTPException, Request

from app.config import settings

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
}


class ScraperError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    # Block Carrier-Grade NAT (100.64.0.0/10) and cloud metadata explicitly
    if isinstance(ip, ipaddress.IPv4Address):
        if ip in ipaddress.IPv4Network("100.64.0.0/10"):
            return False
        if ip_str == "169.254.169.254":
            return False
    return ip.is_global


def validate_safe_url(url: str, *, allow_private: bool | None = None) -> str:
    if allow_private is None:
        allow_private = settings.allow_private_networks

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ScraperError(400, "invalid_request", f"Unsupported URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ScraperError(400, "invalid_request", "URL is missing a valid hostname")
    if parsed.username or parsed.password:
        raise ScraperError(400, "invalid_request", "URL credentials are not permitted")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ScraperError(400, "invalid_request", "Invalid URL port") from exc

    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if not allow_private and effective_port not in settings.allowed_ports:
        raise ScraperError(
            403,
            "authentication_or_policy",
            f"Port {effective_port} is not permitted by network policy",
        )

    host = parsed.hostname.lower()
    if not allow_private and host in {"localhost", "metadata.google.internal", "metadata"}:
        raise ScraperError(403, "authentication_or_policy", f"Disallowed target host: {host}")

    if not allow_private:
        try:
            ipaddress.ip_address(host)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False

        if is_ip_literal:
            if not is_public_ip(host):
                raise ScraperError(
                    403,
                    "authentication_or_policy",
                    f"Target IP {host} is non-public and blocked by SSRF policy",
                )
        else:
            try:
                addr_info = socket.getaddrinfo(host, effective_port, type=socket.SOCK_STREAM)
            except socket.gaierror as exc:
                raise ScraperError(
                    502, "fetch_failed", f"DNS resolution failed for {host}", retryable=True
                ) from exc

            if not addr_info:
                raise ScraperError(502, "fetch_failed", f"No DNS records for {host}")

            for entry in addr_info:
                ip_str = entry[4][0]
                if not is_public_ip(ip_str):
                    raise ScraperError(
                        403,
                        "authentication_or_policy",
                        f"Target {host} resolves to non-public IP {ip_str}",
                    )

    return urlunparse(parsed._replace(fragment=""))


def normalize_canonical_url(url: str, base_url: str | None = None) -> str | None:
    raw = (url or "").strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    resolved = urljoin(base_url, raw) if base_url else raw
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    filtered_q = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    clean_path = parsed.path or "/"
    if len(clean_path) > 1 and clean_path.endswith("/"):
        clean_path = clean_path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            clean_path,
            parsed.params,
            urlencode(filtered_q, doseq=True),
            "",
        )
    )


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= settings.rate_limit_per_minute:
            raise ScraperError(
                429,
                "authentication_or_policy",
                "Rate limit exceeded; slow down requests",
                retryable=True,
            )
        window.append(now)


rate_limiter = RateLimiter()


async def enforce_auth_and_rate_limit(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    if settings.require_auth or settings.api_bearer_token:
        if settings.require_auth:
            if not authorization or not authorization.startswith("Bearer "):
                raise ScraperError(401, "authentication_or_policy", "Missing Bearer token")
            token = authorization.removeprefix("Bearer ").strip()
            if token != settings.api_bearer_token:
                raise ScraperError(403, "authentication_or_policy", "Invalid Bearer token")
    client_ip = request.client.host if request.client else "local"
    rate_limiter.check(client_ip)
