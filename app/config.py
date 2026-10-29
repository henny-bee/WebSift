from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    env_file = Path(path)
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'\""))


_load_dotenv()


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    port: int = int(os.getenv("PORT", "8787"))
    api_bearer_token: str = os.getenv("API_BEARER_TOKEN", "")
    require_auth: bool = _env_bool("REQUIRE_AUTH", False)

    max_body_bytes: int = int(os.getenv("MAX_BODY_BYTES", str(5 * 1024 * 1024)))
    connect_timeout_s: float = float(os.getenv("CONNECT_TIMEOUT_S", "5.0"))
    read_timeout_s: float = float(os.getenv("READ_TIMEOUT_S", "15.0"))
    max_redirects: int = int(os.getenv("MAX_REDIRECTS", "5"))
    max_items_limit: int = int(os.getenv("MAX_ITEMS_LIMIT", "200"))
    global_concurrency: int = int(os.getenv("GLOBAL_CONCURRENCY", "2"))
    per_domain_concurrency: int = int(os.getenv("PER_DOMAIN_CONCURRENCY", "1"))
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
    allow_private_networks: bool = _env_bool("ALLOW_PRIVATE_NETWORKS", False)
    allowed_ports: set[int] = field(default_factory=lambda: {80, 443})

    sqlite_path: str = os.getenv("SQLITE_PATH", "data/scraper.db")
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "600"))

    crawl_max_pages_limit: int = int(os.getenv("CRAWL_MAX_PAGES_LIMIT", "50"))
    crawl_max_depth_limit: int = int(os.getenv("CRAWL_MAX_DEPTH_LIMIT", "3"))
    crawl_min_jitter_s: float = float(os.getenv("CRAWL_MIN_JITTER_S", "1.5"))
    crawl_max_jitter_s: float = float(os.getenv("CRAWL_MAX_JITTER_S", "4.0"))

    cf_solver_enabled: bool = _env_bool("CF_SOLVER_ENABLED", False)
    cf_solver_url: str = os.getenv("CF_SOLVER_URL", "http://cloudflare-solver:407")
    cf_clearance_ttl_seconds: int = int(os.getenv("CF_CLEARANCE_TTL_SECONDS", "1200"))
    browser_enabled: bool = _env_bool("BROWSER_ENABLED", False)
    default_proxy_url: str | None = os.getenv("PROXY_URL") or None
    rules_dir: str = os.getenv("RULES_DIR", str(Path(__file__).parent / "rules"))


settings = Settings()
