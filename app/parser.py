from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.schemas import (
    ContentBlock,
    ContentInfo,
    HeadingItem,
    ImageItem,
    LinkItem,
    PageInfo,
    PaginationInfo,
)
from app.security import normalize_canonical_url

_SRCSET_ENTRY_RE = re.compile(
    r"([^\s,]+(?:,\d+[^\s,]*)?)(?:\s+\d+(?:\.\d+)?[wx])?(?:\s*,\s*|$)"
)
_WHITESPACE_RE = re.compile(r"\s+")
_NEXT_TEXT_RE = re.compile(r"^\s*(next|next\s+page|older|selanjutnya|›|»|>)\s*$", re.I)


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", text).strip()


def resolve_base_url(tree: LexborHTMLParser, final_url: str) -> str:
    base_node = tree.css_first("head base[href]")
    if base_node:
        href = (base_node.attributes.get("href") or "").strip()
        if href:
            candidate = urljoin(final_url, href)
            if urlparse(candidate).scheme in {"http", "https"}:
                return candidate
    return final_url


def resolve_url(href: str | None, base_url: str) -> str | None:
    raw = (href or "").strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    full = urljoin(base_url, raw)
    parsed = urlparse(full)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return full


def extract_srcset_urls(srcset: str | None, base_url: str) -> list[str]:
    if not srcset:
        return []
    urls: list[str] = []
    for match in _SRCSET_ENTRY_RE.finditer(srcset.strip()):
        candidate = (match.group(1) or "").strip()
        if not candidate or candidate.isdigit():
            continue
        resolved = resolve_url(candidate, base_url)
        if resolved and resolved not in urls:
            urls.append(resolved)
    return urls


def extract_node_images(scope: LexborNode, base_url: str) -> list[ImageItem]:
    items: list[ImageItem] = []
    seen: set[str] = set()

    def _add(
        url: str | None,
        alt: str | None = None,
        caption: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if not url or url in seen or url.startswith("data:"):
            return
        # Skip 1x1 tracking pixels, blank spacer gifs, or lazy grey placeholders
        if any(
            tok in url.lower()
            for tok in ("spacer.gif", "pixel.gif", "1x1.", "placeholder.")
        ):
            return
        seen.add(url)
        items.append(ImageItem(url=url, alt=alt, caption=caption, width=width, height=height))

    for img in scope.css("img, picture source, noscript, [data-src], [data-lazy-src], [data-bg]"):
        tag = img.tag
        if tag == "noscript":
            inner_html = img.html or ""
            if "<img" in inner_html:
                sub = LexborHTMLParser(inner_html)
                for n_img in sub.css("img"):
                    src = resolve_url(
                        n_img.attributes.get("src") or n_img.attributes.get("data-src"),
                        base_url,
                    )
                    alt = clean_text(n_img.attributes.get("alt")) or None
                    _add(src, alt=alt)
            continue

        if tag == "source":
            for u in extract_srcset_urls(
                img.attributes.get("srcset") or img.attributes.get("data-srcset"),
                base_url,
            ):
                _add(u)
            continue

        if tag != "img":
            for attr_name in ("data-src", "data-lazy-src", "data-bg"):
                u = resolve_url(img.attributes.get(attr_name), base_url)
                if u:
                    _add(u)
            continue

        attrs = img.attributes
        img_cls = (attrs.get("class") or "").lower()
        src_peek = (attrs.get("src") or attrs.get("data-src") or "").lower()
        # Skip tiny inline avatar/badge icons (e.g. Tailwind size-2.5 / size-3.5 or /avatars/)
        if any(
            tok in img_cls
            for tok in ("size-2", "size-3", "size-4", "size-5", "w-3 ", "w-4 ", "h-3 ", "h-4 ", "avatar")
        ) or any(tok in src_peek for tok in ("/avatars/", "cdn-avatars.")):
            continue
        alt = clean_text(attrs.get("alt")) or None
        w_str, h_str = attrs.get("width") or "", attrs.get("height") or ""
        w_match, h_match = re.match(r"^\s*(\d+)", w_str), re.match(r"^\s*(\d+)", h_str)
        width = int(w_match.group(1)) if w_match else None
        height = int(h_match.group(1)) if h_match else None
        # Skip tracking pixels and tiny <= 24px inline UI/coin icons
        if (width is not None and width <= 24) or (height is not None and height <= 24):
            continue

        caption = None
        parent = img.parent
        while parent and parent.tag not in {"body", "html"}:
            if parent.tag == "figure":
                fc = parent.css_first("figcaption")
                if fc:
                    caption = clean_text(fc.text()) or None
                break
            parent = parent.parent

        candidates: list[str] = []
        for attr_name in ("data-src", "data-lazy-src", "data-original", "src"):
            val = resolve_url(attrs.get(attr_name), base_url)
            if val and val not in candidates:
                candidates.append(val)
        for srcset_attr in ("data-srcset", "srcset"):
            for u in extract_srcset_urls(attrs.get(srcset_attr), base_url):
                if u not in candidates:
                    candidates.append(u)

        for idx, u in enumerate(candidates):
            _add(
                u,
                alt=alt if idx == 0 else None,
                caption=caption if idx == 0 else None,
                width=width if idx == 0 else None,
                height=height if idx == 0 else None,
            )

    return items


def parse_json_ld(tree: LexborHTMLParser) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, dict):
            if "@graph" in obj and isinstance(obj["@graph"], list):
                for item in obj["@graph"]:
                    _collect(item)
            else:
                results.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    for node in tree.css('script[type*="ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            _collect(parsed)
        except Exception:
            continue
    return results


def detect_pagination_next(tree: LexborHTMLParser, base_url: str) -> str | None:
    for sel in (
        'link[rel="next"][href]',
        'a[rel="next"][href]',
        ".pagination a.next[href]",
        ".pagination a.next-page[href]",
        'a[aria-label*="Next" i][href]',
        "a.next[href]",
    ):
        node = tree.css_first(sel)
        if node:
            resolved = resolve_url(node.attributes.get("href"), base_url)
            if resolved:
                return resolved

    for a in tree.css(".pagination a[href], nav a[href], .pager a[href]"):
        txt = clean_text(a.text())
        if _NEXT_TEXT_RE.match(txt):
            resolved = resolve_url(a.attributes.get("href"), base_url)
            if resolved:
                return resolved
    return None


def parse_page_metadata(tree: LexborHTMLParser, base_url: str, final_url: str) -> PageInfo:
    title_node = tree.css_first("head > title, title")
    title = clean_text(title_node.text()) if title_node else None

    html_node = tree.css_first("html")
    language = (html_node.attributes.get("lang") or "").strip() or None if html_node else None

    canonical_node = tree.css_first('link[rel="canonical"][href]')
    canonical_url = (
        resolve_url(canonical_node.attributes.get("href"), base_url)
        if canonical_node
        else normalize_canonical_url(final_url)
    )

    description: str | None = None
    metadata: dict[str, Any] = {}

    for meta in tree.css("meta"):
        attrs = meta.attributes
        name = (attrs.get("name") or attrs.get("property") or "").strip().lower()
        content = clean_text(attrs.get("content"))
        if not name or not content:
            continue
        if name == "description" and not description:
            description = content
        elif name.startswith(("og:", "twitter:", "article:", "product:")):
            if name in {"og:image", "twitter:image"}:
                content = resolve_url(content, base_url) or content
            metadata[name] = content
        elif name in {"author", "keywords", "robots"}:
            metadata[name] = content

    if not title and "og:title" in metadata:
        title = metadata["og:title"]
    if not description and "og:description" in metadata:
        description = metadata["og:description"]

    next_url = detect_pagination_next(tree, base_url)
    pagination = PaginationInfo(next_url=next_url) if next_url else None

    return PageInfo(
        title=title,
        description=description,
        canonical_url=canonical_url,
        language=language,
        metadata=metadata,
        pagination=pagination,
    )


def parse_content(tree: LexborHTMLParser, content_mode: str = "main") -> ContentInfo:
    headings: list[HeadingItem] = []
    for h in tree.css("h1, h2, h3, h4, h5, h6"):
        txt = clean_text(h.text())
        if txt:
            level = int(h.tag[1])
            headings.append(HeadingItem(level=level, text=txt))

    scope: LexborNode | None = None
    if content_mode == "main":
        for sel in ("main", 'article[role="main"]', "article", '[role="main"]', "#content", ".main-content"):
            cand = tree.css_first(sel)
            if cand:
                scope = cand
                break
    if scope is None:
        scope = tree.body or tree.root

    blocks: list[ContentBlock] = []
    if scope:
        for node in scope.css("p, pre, ul, ol, table"):
            # Skip boilerplate nav/footer/sidebar blocks in main readable content
            if content_mode == "main":
                anc = node.parent
                skip = False
                while anc is not None and anc.mem_id != scope.mem_id:
                    anc_cls = (anc.attributes.get("class") or "").lower()
                    if anc.tag in {"nav", "footer", "aside", "header"} or "sidebar" in anc_cls:
                        skip = True
                        break
                    anc = anc.parent
                if skip:
                    continue

            tag = node.tag
            txt = clean_text(node.text()) if tag != "pre" else (node.text() or "").strip()
            if not txt:
                continue
            btype = {
                "p": "paragraph",
                "pre": "code",
                "ul": "list",
                "ol": "list",
                "table": "table",
            }.get(tag, "paragraph")
            blocks.append(ContentBlock(type=btype, text=txt))

    text_joined = "\n\n".join(b.text for b in blocks)
    if not text_joined and scope:
        text_joined = clean_text(scope.text())

    return ContentInfo(text=text_joined, headings=headings, blocks=blocks)


def parse_links(tree: LexborHTMLParser, base_url: str, final_url: str, max_items: int) -> list[LinkItem]:
    host = (urlparse(final_url).hostname or "").lower().removeprefix("www.")
    items: list[LinkItem] = []
    seen: set[str] = set()

    for a in tree.css("a[href]"):
        resolved = resolve_url(a.attributes.get("href"), base_url)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        link_host = (urlparse(resolved).hostname or "").lower().removeprefix("www.")
        internal = bool(host and (link_host == host or link_host.endswith("." + host)))
        text = clean_text(a.text()) or clean_text(a.attributes.get("title")) or ""
        rel = clean_text(a.attributes.get("rel")) or None
        items.append(LinkItem(url=resolved, text=text, internal=internal, rel=rel))
        if len(items) >= max_items:
            break
    return items
