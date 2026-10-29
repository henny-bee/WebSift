from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

ExtractSection = Literal[
    "metadata",
    "text",
    "headings",
    "links",
    "images",
    "structured_data",
    "collections",
]
RegionType = Literal["main", "sidebar"]
RenderJSMode = Literal["never", "auto", "always"]
ContentMode = Literal["main", "full"]
ItemType = Literal[
    "auto",
    "article",
    "product",
    "documentation_entry",
    "forum_thread",
    "organization",
    "generic_card",
]


class ScrapeRequest(BaseModel):
    url: str
    mode: Literal["page"] = "page"
    extract: list[ExtractSection] = Field(
        default_factory=lambda: [
            "metadata",
            "text",
            "headings",
            "links",
            "images",
            "structured_data",
            "collections",
        ]
    )
    include_regions: list[RegionType] = Field(default_factory=lambda: ["main", "sidebar"])
    render_js: RenderJSMode = "never"
    content_mode: ContentMode = "main"
    max_items: int = Field(default=100, ge=1, le=500)
    include_image_urls: bool = True
    item_types: list[ItemType] = Field(default_factory=lambda: ["auto"])
    bypass_cache: bool = False


class PaginationInfo(BaseModel):
    next_url: str | None = None


class PageInfo(BaseModel):
    title: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    pagination: PaginationInfo | None = None


class HeadingItem(BaseModel):
    level: int
    text: str


class ContentBlock(BaseModel):
    type: str
    text: str


class ContentInfo(BaseModel):
    text: str = ""
    headings: list[HeadingItem] = Field(default_factory=list)
    blocks: list[ContentBlock] = Field(default_factory=list)


class LinkItem(BaseModel):
    url: str
    text: str
    internal: bool
    rel: str | None = None


class ImageItem(BaseModel):
    url: str
    alt: str | None = None
    caption: str | None = None
    width: int | None = None
    height: int | None = None


class Placement(BaseModel):
    region: RegionType
    section: str
    position: int


class CollectionItem(BaseModel):
    type: str
    title: str
    description: str | None = None
    url: str | None = None
    image_url: str | None = None
    images: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    extraction_method: Literal["domain_rule", "jsonld", "type_plugin", "generic_dom"]
    position: int


class CollectionGroup(BaseModel):
    type: str
    items: list[CollectionItem] = Field(default_factory=list)


class DiagnosticsInfo(BaseModel):
    fetch_engine: str = "httpx"
    challenge_solved: bool = False
    partial: bool = False
    truncated: bool = False
    regions_scanned: list[str] = Field(default_factory=list)
    detected_card_occurrences: int = 0
    unique_items: int = 0
    deduplicated_occurrences: int = 0
    pagination_detected: bool = False
    warnings: list[str] = Field(default_factory=list)


class ScrapeResponse(BaseModel):
    schema_version: str = "1.0"
    requested_url: str
    final_url: str
    status: Literal["ok", "partial"] = "ok"
    page: PageInfo = Field(default_factory=PageInfo)
    content: ContentInfo = Field(default_factory=ContentInfo)
    links: list[LinkItem] = Field(default_factory=list)
    images: list[ImageItem] = Field(default_factory=list)
    structured_data: list[dict[str, Any]] = Field(default_factory=list)
    collections: list[CollectionGroup] = Field(default_factory=list)
    diagnostics: DiagnosticsInfo = Field(default_factory=DiagnosticsInfo)


class CrawlRequest(BaseModel):
    url: str
    max_pages: int = Field(default=10, ge=1, le=100)
    max_depth: int = Field(default=2, ge=0, le=5)
    same_domain_only: bool = True
    extract: list[ExtractSection] = Field(
        default_factory=lambda: ["metadata", "links", "images", "collections"]
    )


class CrawlJobStatus(BaseModel):
    job_id: str
    seed_url: str
    status: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    pages_visited: int = 0
    max_pages: int
    max_depth: int
    errors: list[str] = Field(default_factory=list)


class CrawlResultsPage(BaseModel):
    job_id: str
    status: str
    results: list[ScrapeResponse]
    next_cursor: int | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
