from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.config import settings
from app.parser import clean_text, extract_node_images, resolve_url
from app.schemas import CollectionGroup, CollectionItem, RegionType
from app.security import normalize_canonical_url

_REL_TIME_RE = re.compile(
    r"\b(\d+\s+(?:sec(?:ond)?|min(?:ute)?|hour|day|week|month|year)s?\s+ago"
    r"|yesterday|today|just\s+now"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?(?:\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)\b",
    re.I,
)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b")
_COMMENTS_RE = re.compile(r"\b(\d[\d,]*)\s*(?:comments?|replies|komentar)\b", re.I)
_PRICE_RE = re.compile(
    r"(?:(USD|EUR|GBP|JPY|IDR|Rp|\$|€|£|¥)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(USD|EUR|GBP|IDR))"
    r"(?![\d.,]|\s*(?:million|billion|trillion|mil|bil|bn|mn|m|b|k|%)\b)",
    re.I,
)
_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "RP": "IDR"}
_IGNORE_LINK_TEXT = {
    "more",
    "read more",
    "view more",
    "see all",
    "comments",
    "comment",
    "reply",
    "login",
    "sign in",
    "register",
    "next",
    "prev",
    "previous",
}


def _load_domain_rule(hostname: str) -> dict[str, Any] | None:
    rules_path = Path(settings.rules_dir)
    if not rules_path.is_dir():
        return None
    yaml_files = list(rules_path.glob("*.yaml")) + list(rules_path.glob("*.yml"))
    if not yaml_files:
        return None
    try:
        import yaml  # Optional dependency; only loaded when rule files exist
    except ImportError:
        return None

    host = hostname.lower().removeprefix("www.")
    for fpath in yaml_files:
        try:
            data = yaml.safe_load(fpath.read_text(encoding="utf-8")) or {}
            domains = [d.lower().removeprefix("www.") for d in data.get("domains", [])]
            if host in domains:
                return data
        except Exception:
            continue
    return None


def _parse_price(text: str) -> dict[str, Any] | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    sym = (m.group(1) or m.group(4) or "").upper()
    raw_num = m.group(2) or m.group(3) or ""
    currency = _CURRENCY_MAP.get(sym, sym)
    norm_num = raw_num.replace(",", "") if "." in raw_num else raw_num.replace(".", "").replace(",", ".")
    try:
        amount = float(norm_num)
    except ValueError:
        return None
    return {"amount": amount, "currency": currency, "display": m.group(0).strip()}


def _extract_jsonld_items(
    structured_data: list[dict[str, Any]],
    base_url: str,
) -> list[CollectionItem]:
    items: list[CollectionItem] = []
    pos = 1

    for obj in structured_data:
        raw_type = obj.get("@type")
        ld_type = raw_type[0] if isinstance(raw_type, list) and raw_type else str(raw_type or "")

        if ld_type in {"NewsArticle", "BlogPosting", "Article"}:
            title = clean_text(str(obj.get("headline") or obj.get("name") or ""))
            url = resolve_url(str(obj.get("url") or ""), base_url)
            if not title:
                continue
            imgs: list[str] = []
            raw_img = obj.get("image")
            if isinstance(raw_img, str):
                u = resolve_url(raw_img, base_url)
                if u:
                    imgs.append(u)
            elif isinstance(raw_img, dict) and raw_img.get("url"):
                u = resolve_url(str(raw_img["url"]), base_url)
                if u:
                    imgs.append(u)
            elif isinstance(raw_img, list):
                for entry in raw_img:
                    u = resolve_url(entry.get("url") if isinstance(entry, dict) else str(entry), base_url)
                    if u and u not in imgs:
                        imgs.append(u)

            author_val = obj.get("author")
            author = None
            if isinstance(author_val, dict):
                author = clean_text(str(author_val.get("name") or "")) or None
            elif isinstance(author_val, list) and author_val:
                first = author_val[0]
                author = clean_text(str(first.get("name") if isinstance(first, dict) else first)) or None
            elif isinstance(author_val, str):
                author = clean_text(author_val) or None

            items.append(
                CollectionItem(
                    type="article",
                    title=title,
                    description=clean_text(str(obj.get("description") or "")) or None,
                    url=url,
                    image_url=imgs[0] if imgs else None,
                    images=imgs,
                    attributes={
                        "author": author,
                        "published_at": obj.get("datePublished"),
                        "published_at_text": None,
                        "placements": [{"region": "main", "section": "JSON-LD", "position": pos}],
                    },
                    extraction_method="jsonld",
                    position=pos,
                )
            )
            pos += 1

        elif ld_type == "Product":
            title = clean_text(str(obj.get("name") or ""))
            if not title:
                continue
            url = resolve_url(str(obj.get("url") or ""), base_url)
            imgs = []
            raw_img = obj.get("image")
            if isinstance(raw_img, str):
                u = resolve_url(raw_img, base_url)
                if u:
                    imgs.append(u)
            elif isinstance(raw_img, list):
                for entry in raw_img:
                    u = resolve_url(entry.get("url") if isinstance(entry, dict) else str(entry), base_url)
                    if u:
                        imgs.append(u)

            offers = obj.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            price_obj = None
            availability = None
            if isinstance(offers, dict):
                p_val = offers.get("price")
                curr = offers.get("priceCurrency") or "USD"
                if p_val is not None:
                    try:
                        amt = float(str(p_val).replace(",", ""))
                        price_obj = {"amount": amt, "currency": curr, "display": f"{curr} {p_val}"}
                    except ValueError:
                        pass
                avail_raw = str(offers.get("availability") or "")
                if avail_raw:
                    availability = avail_raw.rsplit("/", 1)[-1]
                if not url and offers.get("url"):
                    url = resolve_url(str(offers["url"]), base_url)

            brand_val = obj.get("brand")
            brand = (
                clean_text(str(brand_val.get("name") if isinstance(brand_val, dict) else brand_val))
                if brand_val
                else None
            )

            items.append(
                CollectionItem(
                    type="product",
                    title=title,
                    description=clean_text(str(obj.get("description") or "")) or None,
                    url=url,
                    image_url=imgs[0] if imgs else None,
                    images=imgs,
                    attributes={
                        "brand": brand,
                        "price": price_obj,
                        "availability": availability,
                        "sku": obj.get("sku"),
                        "placements": [{"region": "main", "section": "JSON-LD", "position": pos}],
                    },
                    extraction_method="jsonld",
                    position=pos,
                )
            )
            pos += 1

        elif ld_type in {"Organization", "LocalBusiness"}:
            name = clean_text(str(obj.get("name") or ""))
            if name:
                raw_logo = obj.get("logo")
                logo_str = (
                    str(raw_logo.get("url") or raw_logo.get("contentUrl") or "")
                    if isinstance(raw_logo, dict)
                    else str(raw_logo or "")
                )
                logo_url = resolve_url(logo_str, base_url) if logo_str else None
                items.append(
                    CollectionItem(
                        type="organization",
                        title=name,
                        description=clean_text(str(obj.get("description") or "")) or None,
                        url=resolve_url(str(obj.get("url") or ""), base_url),
                        image_url=logo_url,
                        images=[logo_url] if logo_url else [],
                        attributes={
                            "email": obj.get("email"),
                            "phone": obj.get("telephone"),
                            "address": obj.get("address"),
                            "placements": [{"region": "main", "section": "JSON-LD", "position": pos}],
                        },
                        extraction_method="jsonld",
                        position=pos,
                    )
                )
                pos += 1

    return items


def _is_inside_any(node: LexborNode, container_ids: set[int]) -> bool:
    if not container_ids:
        return False
    curr: LexborNode | None = node
    while curr is not None:
        if curr.mem_id in container_ids:
            return True
        curr = curr.parent
    return False


def _is_hidden_node(node: LexborNode) -> bool:
    curr: LexborNode | None = node
    while curr is not None and curr.tag not in {"body", "html"}:
        attrs = curr.attributes
        node_id = attrs.get("id") or ""
        # React 19 / Next.js streaming SSR places main content inside <div hidden id="S:...">
        if "hidden" in attrs and not node_id.startswith("S:"):
            return True
        if "x-cloak" in attrs:
            return True
        style = (attrs.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        cls = (attrs.get("class") or "").lower()
        if any(tok in cls for tok in ("modal", "popup", "dropdown-menu")):
            return True
        if (attrs.get("role") or "").lower() == "dialog":
            return True
        curr = curr.parent
    return False


def _find_sidebar_roots(tree: LexborHTMLParser) -> list[LexborNode]:
    roots: list[LexborNode] = []
    root_ids: set[int] = set()
    for sel in (
        "aside",
        '[role="complementary"]',
        "#sidebar",
        "#aside",
        ".sidebar",
        ".right-column",
        ".side-col",
        ".widget-slide-block",
    ):
        for node in tree.css(sel):
            # Never treat a page-wide wrapper containing <main> or <h1> as a sidebar
            if node.css_first("main, h1"):
                continue
            cls = (node.attributes.get("class") or "").lower()
            if any(w in cls for w in ("sidebar-page", "sidebar-layout", "sidebar-wrapper", "has-sidebar", "with-sidebar")):
                continue
            if not _is_inside_any(node, root_ids):
                roots.append(node)
                root_ids.add(node.mem_id)
    return roots


def _extract_card_from_node(
    card: LexborNode,
    base_url: str,
    region: RegionType,
    section: str,
    position: int,
    include_images: bool = True,
    method: str = "generic_dom",
) -> CollectionItem | None:
    # 1. Identify primary title & link
    title_link: LexborNode | None = card if (card.tag == "a" and card.attributes.get("href")) else None
    if title_link is None:
        for sel in (
            "h1 a[href], h2 a[href], h3 a[href], h4 a[href]",
            ".title a[href], .news-title a[href], p.title a[href]",
            "a.title[href], a.news-title[href], a.product-title[href], a.thread-title[href]",
            'a[data-component="story-link"][href], a[class*="storyLink"][href]',
            "td a[href]",
        ):
            for cand in card.css(sel):
                txt = clean_text(cand.text())
                href = cand.attributes.get("href") or ""
                if txt and txt.lower() not in _IGNORE_LINK_TEXT and not href.startswith("#"):
                    title_link = cand
                    break
            if title_link:
                break

    # Also support <a href="..."><h2>Title</h2></a> where <a> wraps the heading
    if title_link is None:
        for h_in_a in card.css("a[href] h1, a[href] h2, a[href] h3, a[href] h4"):
            anc_a = h_in_a.parent
            while anc_a is not None and anc_a.tag != "a":
                anc_a = anc_a.parent
            if anc_a is not None and not _is_hidden_node(anc_a):
                txt = clean_text(h_in_a.text())
                href = anc_a.attributes.get("href") or ""
                if txt and txt.lower() not in _IGNORE_LINK_TEXT and not href.startswith("#"):
                    title_link = anc_a
                    break

    if title_link is None:
        all_links = [
            cand
            for cand in card.css("a[href]")
            if not _is_hidden_node(cand)
            and len(clean_text(cand.text())) >= 6
            and clean_text(cand.text()).lower() not in _IGNORE_LINK_TEXT
            and not (cand.attributes.get("href") or "").strip().startswith("#")
            and not any(
                k in (cand.attributes.get("class") or "").lower()
                for k in ("tag", "comment", "author", "imageoverlay", "linkedimage")
            )
            and not any(
                p in (cand.attributes.get("href") or "")
                for p in ("/profile/", "/tag/", "/genre/", "/coins/", "/terms", "/privacy")
            )
        ]
        # Prefer links that do NOT wrap <figure>/<picture>/<img> when text-only headline links exist
        non_media_links = [
            a for a in all_links if not a.css_first("figure, picture, img")
        ]
        if non_media_links:
            title_link = non_media_links[0]
        elif all_links:
            title_link = all_links[0]

    if title_link is None:
        return None

    # Handle modern cards where <a> wraps both a headline node and a summary/snippet node
    description: str | None = None
    explicit_hl = title_link.css_first(
        '[data-component="headline"], [data-testid="headline"], [data-testid="card-headline"], '
        '[class*="Headline"], [class*="headline"], h1, h2, h3, h4'
    )
    explicit_sum = title_link.css_first(
        '[data-component*="summary"], [data-testid="card-description"], '
        '[class*="Summary_summary"], [class*="summary"], [class*="snippet"], p'
    )
    if explicit_hl and clean_text(explicit_hl.text()):
        title = clean_text(explicit_hl.text())
        if explicit_sum:
            s_txt = clean_text(explicit_sum.text())
            if s_txt and s_txt != title:
                description = s_txt
    else:
        link_block_children = [
            ch
            for ch in title_link.iter(include_text=False)
            if ch.tag in {"div", "p", "section", "h1", "h2", "h3", "h4", "h5", "span"}
            and clean_text(ch.text())
        ]
        if len(link_block_children) >= 2:
            title = clean_text(link_block_children[0].text())
            second_txt = clean_text(link_block_children[1].text())
            if second_txt and second_txt != title and len(second_txt) >= 12:
                description = second_txt
        else:
            title = clean_text(title_link.text()) or clean_text(title_link.attributes.get("title"))

    url = resolve_url(title_link.attributes.get("href"), base_url)
    if not title or not url:
        return None

    # 2. Card-local images ONLY (never page og:image)
    imgs = [img.url for img in extract_node_images(card, base_url)] if include_images else []
    image_url = imgs[0] if imgs else None

    # 3. Extract tags (including visible topic/coin badges)
    tags: list[str] = []
    for tag_a in card.css(
        '.tags a, .tag-list a, .topics a, [class*="tag"] a, a[href*="/tag/"], a[href*="/genre/"], a[href*="/coins/"], .badge, .category'
    ):
        if tag_a.mem_id == title_link.mem_id or _is_hidden_node(tag_a):
            continue
        t_txt = clean_text(tag_a.text())
        if t_txt and t_txt not in tags and len(t_txt) <= 40:
            tags.append(t_txt)

    # 4. Extract description / snippet if not already split from title_link
    if not description:
        for desc_sel in (
            ".text",
            ".description",
            ".summary",
            ".snippet",
            ".excerpt",
            ".news-snippet",
            "p",
        ):
            for d_node in card.css(desc_sel):
                if _is_inside_any(title_link, {d_node.mem_id}) or _is_hidden_node(d_node):
                    continue
                d_cls = (d_node.attributes.get("class") or "").lower()
                if any(k in d_cls for k in ("info", "meta", "title", "tag", "author", "price")):
                    continue
                d_txt = clean_text(d_node.text())
                if d_txt and d_txt != title and len(d_txt) >= 12:
                    description = d_txt
                    break
            if description:
                break

    # 5. Extract author, dates, comments_count, price
    author: str | None = None
    for auth_node in card.css(
        'a[href*="/profile/"], a[href*="/user/"], a[href*="/author/"], [rel="author"], .author'
    ):
        if auth_node.mem_id != title_link.mem_id and not _is_hidden_node(auth_node):
            a_txt = clean_text(auth_node.text())
            if a_txt and len(a_txt) <= 50:
                author = a_txt
                break

    published_at: str | None = None
    published_at_text: str | None = None
    time_node = card.css_first("time")
    if time_node:
        dt_attr = clean_text(time_node.attributes.get("datetime"))
        if dt_attr and _ISO_DATE_RE.search(dt_attr):
            published_at = dt_attr
        t_txt = clean_text(time_node.text())
        if t_txt:
            published_at_text = t_txt

    card_full_text = clean_text(card.text())
    if not published_at_text:
        for leaf in card.css("div, span, time, p"):
            if _is_hidden_node(leaf):
                continue
            leaf_txt = clean_text(leaf.text())
            if len(leaf_txt) <= 40 and _REL_TIME_RE.fullmatch(leaf_txt):
                published_at_text = leaf_txt
                # If author not yet found, check the immediate previous sibling (e.g. publisher name)
                if not author and leaf.prev:
                    prev_node = leaf.prev
                    while prev_node is not None and prev_node.tag == "-text":
                        prev_node = prev_node.prev
                    if prev_node is not None and not prev_node.css_first("a[href]"):
                        cand_auth = clean_text(prev_node.text())
                        if 2 <= len(cand_auth) <= 45 and cand_auth != title:
                            author = cand_auth
                break
    if not published_at_text:
        m_rel = _REL_TIME_RE.search(card_full_text)
        if m_rel:
            published_at_text = m_rel.group(1).strip()
    if not published_at:
        m_iso = _ISO_DATE_RE.search(card_full_text)
        if m_iso:
            published_at = m_iso.group(0)

    comments_count: int | None = None
    for c_node in card.css('a[href*="#comment"], a[href*="/comments"], .comments, .replies'):
        c_txt = clean_text(c_node.text())
        m_num = re.search(r"\d[\d,]*", c_txt)
        if m_num:
            comments_count = int(m_num.group(0).replace(",", ""))
            break
    if comments_count is None:
        m_com = _COMMENTS_RE.search(card_full_text)
        if m_com:
            comments_count = int(m_com.group(1).replace(",", ""))

    # Table row fallback for forum threads (e.g. Replies column)
    if card.tag == "tr" and comments_count is None:
        tds = card.css("td")
        for td in tds[1:]:
            td_txt = clean_text(td.text())
            if td_txt.isdigit():
                comments_count = int(td_txt)
                break

    price_node = card.css_first('.price, [class*="price"], [itemprop="price"]')
    price = _parse_price(clean_text(price_node.text())) if price_node else None
    card_cls = (card.attributes.get("class") or "").lower()
    url_lower = (url or "").lower()
    sec_lower = section.lower()
    is_news_like = bool(
        published_at
        or published_at_text
        or comments_count is not None
        or re.search(r"/20\d{2}/\d{2}/", url_lower)
        or any(
            k in card_cls or k in url_lower or k in sec_lower
            for k in (
                "news",
                "article",
                "story",
                "post",
                "blog",
                "/news/",
                "/articles/",
                "/feeds/",
                "/papers/",
                "/technology/",
                "/business/",
                "/world/",
            )
        )
    )
    if price is None and not is_news_like:
        price = _parse_price(card_full_text)

    # 6. Determine item type deterministically
    if (price is not None and not is_news_like) or (
        not is_news_like
        and (
            any(k in card_cls for k in ("product-card", "product-item"))
            or any(k in url_lower for k in ("/product/", "/products/", "/shop/"))
        )
    ):
        item_type = "product"
    elif (
        card.tag == "tr"
        or any(k in url_lower for k in ("/threads/", "/topic/", "/forum/"))
    ) and "/articles/" not in url_lower:
        item_type = "forum_thread"
    elif is_news_like or author or tags:
        item_type = "article"
    else:
        item_type = "generic_card"

    attrs: dict[str, Any] = {
        "placements": [{"region": region, "section": section, "position": position}],
    }
    if item_type == "product":
        attrs["price"] = price
        attrs["brand"] = None
        attrs["availability"] = "in_stock" if "in stock" in card_full_text.lower() else None
    elif item_type == "forum_thread":
        attrs["author"] = author
        attrs["replies"] = comments_count
        attrs["last_activity_text"] = published_at_text
    elif item_type == "article":
        attrs["author"] = author
        attrs["published_at"] = published_at
        attrs["published_at_text"] = published_at_text
        attrs["comments_count"] = comments_count
        attrs["tags"] = tags
    else:
        if author:
            attrs["author"] = author
        if published_at_text:
            attrs["published_at_text"] = published_at_text

    return CollectionItem(
        type=item_type,
        title=title,
        description=description,
        url=url,
        image_url=image_url,
        images=imgs,
        attributes=attrs,
        extraction_method=method,  # type: ignore[arg-type]
        position=position,
    )


def _extract_sidebar_cards(
    sidebar_roots: list[LexborNode],
    base_url: str,
    include_images: bool,
) -> list[CollectionItem]:
    extracted: list[CollectionItem] = []

    for root in sidebar_roots:
        # Look for titled sub-sections inside the sidebar first
        sections: list[tuple[str, LexborNode]] = []
        for sec in root.css(
            "section, .widget, .widget-slide-block, .sidebar-section, .side-block, div"
        ):
            heading = sec.css_first("h2, h3, h4, .widget-header, .section-title, .header")
            if not heading or not heading.parent or heading.parent.mem_id != sec.mem_id:
                continue
            cards_in_sec = sec.css("li, article, .item, .card, .ranking-unit, .sidebar-item")
            if cards_in_sec:
                sec_title = clean_text(heading.text()) or "Sidebar"
                # Strip trailing 'More' link text from heading
                sec_title = re.sub(r"\s*\bMore\s*$", "", sec_title, flags=re.I).strip() or "Sidebar"
                sections.append((sec_title, sec))

        if not sections:
            heading = root.css_first("h2, h3, h4, .widget-header, .section-title")
            if heading and heading.parent and heading.parent.mem_id == root.mem_id:
                sec_title = clean_text(heading.text()) or "Sidebar"
                sec_title = re.sub(r"\s*\bMore\s*$", "", sec_title, flags=re.I).strip() or "Sidebar"
            else:
                sec_title = "Sidebar"
            sections.append((sec_title, root))

        seen_ids: set[int] = set()
        for sec_title, sec_node in sections:
            candidates = sec_node.css(
                "li, article, .herald.box, .ranking-unit, .sidebar-item, .card, .item"
            )
            if not candidates:
                candidates = [
                    d for d in sec_node.css("div")
                    if d.mem_id != sec_node.mem_id and d.css_first("a[href]")
                ]
            pos = 1
            for cand in candidates:
                if _is_inside_any(cand, seen_ids):
                    continue
                item = _extract_card_from_node(
                    cand,
                    base_url,
                    region="sidebar",
                    section=sec_title,
                    position=pos,
                    include_images=include_images,
                )
                if item:
                    # Skip bare 1-2 word navigation menu links without images/descriptions in sidebars
                    if (
                        not item.image_url
                        and not item.description
                        and len(item.title.split()) <= 2
                        and len(item.title) < 25
                    ):
                        continue
                    seen_ids.add(cand.mem_id)
                    extracted.append(item)
                    pos += 1

    return extracted


def _extract_main_cards(
    tree: LexborHTMLParser,
    sidebar_roots: list[LexborNode],
    base_url: str,
    include_images: bool,
) -> list[CollectionItem]:
    sidebar_ids = {n.mem_id for n in sidebar_roots}
    excluded_ids = set(sidebar_ids)
    for nav_node in tree.css("nav, header, footer, .pagination, .breadcrumb, .menu"):
        if not _is_inside_any(nav_node, excluded_ids):
            excluded_ids.add(nav_node.mem_id)

    main_root: LexborNode = tree.body or tree.root  # type: ignore[assignment]
    content_lead: LexborNode = main_root
    for sel in ("main", '[role="main"]', "#content", ".main-content", ".left-column", ".content-left"):
        cand = tree.css_first(sel)
        if cand and not _is_inside_any(cand, sidebar_ids):
            content_lead = cand
            break

    h1_node = content_lead.css_first("h1") or main_root.css_first("h1")
    if h1_node and not h1_node.css_first("a[href]"):
        section_name = clean_text(h1_node.text()) or "Main"
    else:
        section_name = "Main"

    card_selectors = (
        "article",
        ".news-unit-rect",
        ".news-unit",
        ".herald.box",
        ".card",
        ".product-card",
        ".product-item",
        ".post-item",
        ".article-card",
        ".listing-item",
        "table tbody tr",
        "table tr",
    )

    candidate_nodes: list[LexborNode] = []
    candidate_ids: set[int] = set()

    # Nearest coherent ancestor climb for explicit story-link components
    for slink in main_root.css('a[data-component="story-link"][href]'):
        if _is_hidden_node(slink) or _is_inside_any(slink, excluded_ids):
            continue
        card_anc: LexborNode = slink
        while card_anc.parent is not None and card_anc.parent.mem_id != main_root.mem_id:
            if len(card_anc.parent.css('a[data-component="story-link"][href]')) > 1:
                break
            card_anc = card_anc.parent
        if not _is_inside_any(card_anc, candidate_ids):
            candidate_nodes.append(card_anc)
            candidate_ids.add(card_anc.mem_id)

    for sel in card_selectors:
        for node in main_root.css(sel):
            if (
                _is_hidden_node(node)
                or _is_inside_any(node, excluded_ids)
                or _is_inside_any(node, candidate_ids)
            ):
                continue
            # Skip outer <article> page/section wrappers that contain multiple cards or headlines
            if node.tag == "article" and (
                len(node.css("article")) > 1
                or len(node.css("h2 a[href], h3 a[href], a[href] h2, a[href] h3")) > 1
            ):
                continue
            # Skip horizontal category tab bars disguised as <article>
            if (
                node.tag == "article"
                and not node.css_first("h1, h2, h3, h4, img, time, p")
                and len(node.css("a[href]")) > 5
            ):
                continue
            if node.tag == "tr" and (node.css_first("th") or len(node.css("td")) < 2):
                continue
            candidate_nodes.append(node)
            candidate_ids.add(node.mem_id)

    # Supplement with h2/h3 linked cards when pages mix <article> with <li>/<div>/<a> stream items
    heading_links: list[LexborNode] = list(main_root.css("h2 a[href], h3 a[href]"))
    for h_inside in main_root.css("a[href] h2, a[href] h3"):
        anc_a = h_inside.parent
        while anc_a is not None and anc_a.tag != "a":
            anc_a = anc_a.parent
        if anc_a is not None:
            heading_links.append(anc_a)

    for hlink in heading_links:
        if (
            _is_hidden_node(hlink)
            or _is_inside_any(hlink, excluded_ids)
            or _is_inside_any(hlink, candidate_ids)
        ):
            continue
        # Start at hlink itself when <a href> wraps the card container (<h2>, <p>, <img>)
        card_anc: LexborNode | None = hlink
        while (
            card_anc is not None
            and card_anc.parent is not None
            and card_anc.parent.mem_id != main_root.mem_id
        ):
            parent_h_count = len(
                card_anc.parent.css("h1 a[href], h2 a[href], h3 a[href], a[href] h2, a[href] h3")
            )
            if parent_h_count > 1:
                break
            card_anc = card_anc.parent
        if card_anc is not None and not _is_inside_any(card_anc, candidate_ids):
            candidate_nodes.append(card_anc)
            candidate_ids.add(card_anc.mem_id)

    # Generic repeated sibling container detector (handles Tailwind/utility-class grids & lists)
    if len(candidate_nodes) < 2:
        sibling_groups: list[tuple[LexborNode, list[LexborNode]]] = []
        for parent in main_root.css("ul, ol, section, div"):
            if _is_hidden_node(parent) or _is_inside_any(parent, excluded_ids):
                continue
            direct_children = [
                ch
                for ch in parent.iter(include_text=False)
                if ch.tag in {"li", "div", "article"} and not _is_hidden_node(ch)
            ]
            if len(direct_children) < 2:
                continue
            card_like_children = [
                ch
                for ch in direct_children
                if not _is_inside_any(ch, excluded_ids)
                and ch.css_first("a[href]")
                and not ch.css_first("h1")
                and len(clean_text(ch.text())) >= 35
                and (
                    ch.css_first("img")
                    or ch.css_first("p")
                    or _REL_TIME_RE.search(clean_text(ch.text()))
                )
            ]
            # Require majority of direct children in the container to be cards (avoids page wrappers)
            if len(card_like_children) >= 2 and len(card_like_children) >= len(direct_children) * 0.6:
                sibling_groups.append((parent, card_like_children))

        # Keep only innermost sibling groups (skip outer wrappers that contain a matched child grid)
        group_parent_ids = {p.mem_id for p, _ in sibling_groups}
        for parent, cards in sibling_groups:
            other_ids = group_parent_ids - {parent.mem_id}
            if any(_is_inside_any(other_p, {parent.mem_id}) for other_p, _ in sibling_groups if other_p.mem_id in other_ids):
                continue
            for ch in cards:
                if not _is_inside_any(ch, candidate_ids):
                    candidate_nodes.append(ch)
                    candidate_ids.add(ch.mem_id)

    extracted: list[CollectionItem] = []
    pos = 1
    for node in candidate_nodes:
        item = _extract_card_from_node(
            node,
            base_url,
            region="main",
            section=section_name,
            position=pos,
            include_images=include_images,
        )
        if item:
            extracted.append(item)
            pos += 1

    return extracted


def _merge_and_dedupe_items(
    raw_items: list[CollectionItem],
    max_items: int,
) -> tuple[list[CollectionItem], int]:
    merged_by_key: dict[str, CollectionItem] = {}
    ordered_keys: list[str] = []
    deduped_count = 0

    for item in raw_items:
        canon_key = normalize_canonical_url(item.url or "") or f"__title__:{item.type}:{item.title.lower()}"
        existing = merged_by_key.get(canon_key)
        if existing is None:
            item.position = len(ordered_keys) + 1
            merged_by_key[canon_key] = item
            ordered_keys.append(canon_key)
            continue

        deduped_count += 1
        # Merge placements
        existing_placements: list[dict[str, Any]] = existing.attributes.setdefault("placements", [])
        for p in item.attributes.get("placements", []):
            if p not in existing_placements:
                existing_placements.append(p)

        # Retain richest verified fields across regions
        if not existing.description and item.description:
            existing.description = item.description
        if not existing.image_url and item.image_url:
            existing.image_url = item.image_url
        for img_u in item.images:
            if img_u not in existing.images:
                existing.images.append(img_u)

        for k, v in item.attributes.items():
            if k == "placements":
                continue
            curr_v = existing.attributes.get(k)
            if curr_v in (None, [], "") and v not in (None, [], ""):
                existing.attributes[k] = v
            elif k == "tags" and isinstance(curr_v, list) and isinstance(v, list):
                for t in v:
                    if t not in curr_v:
                        curr_v.append(t)

    final_items = [merged_by_key[k] for k in ordered_keys[:max_items]]
    return final_items, deduped_count


def _extract_by_domain_rule(
    tree: LexborHTMLParser,
    rule: dict[str, Any],
    base_url: str,
    include_regions: list[RegionType],
    include_images: bool,
) -> tuple[list[CollectionItem], list[str]]:
    items: list[CollectionItem] = []
    regions_scanned: list[str] = []
    default_type = str(rule.get("item_type") or "article")

    for reg_cfg in rule.get("regions", []):
        region: RegionType = reg_cfg.get("region", "main")
        if region not in include_regions:
            continue
        if region not in regions_scanned:
            regions_scanned.append(region)
        card_sel = reg_cfg.get("card_selector")
        if not card_sel:
            continue
        sec_name = str(reg_cfg.get("section") or region.capitalize())
        pos = 1
        for node in tree.css(card_sel):
            item = _extract_card_from_node(
                node,
                base_url,
                region=region,
                section=sec_name,
                position=pos,
                include_images=include_images,
                method="domain_rule",
            )
            if item:
                item.type = default_type
                items.append(item)
                pos += 1
    return items, regions_scanned


def extract_collections(
    tree: LexborHTMLParser,
    structured_data: list[dict[str, Any]],
    base_url: str,
    final_url: str,
    *,
    include_regions: list[RegionType],
    item_types: list[str],
    include_images: bool,
    max_items: int,
) -> tuple[list[CollectionGroup], list[str], int, int, int]:
    hostname = urlparse(final_url).hostname or ""
    domain_rule = _load_domain_rule(hostname)

    sidebar_roots = _find_sidebar_roots(tree)
    regions_scanned: list[str] = []
    raw_dom_items: list[CollectionItem] = []

    if domain_rule:
        raw_dom_items, regions_scanned = _extract_by_domain_rule(
            tree, domain_rule, base_url, include_regions, include_images
        )
    else:
        if "main" in include_regions:
            regions_scanned.append("main")
            raw_dom_items.extend(_extract_main_cards(tree, sidebar_roots, base_url, include_images))

        if "sidebar" in include_regions and sidebar_roots:
            regions_scanned.append("sidebar")
            raw_dom_items.extend(_extract_sidebar_cards(sidebar_roots, base_url, include_images))

    jsonld_items = _extract_jsonld_items(structured_data, base_url)
    if raw_dom_items and "organization" not in item_types:
        # Omit publisher-level Organization JSON-LD noise when page already has content cards
        jsonld_items = [it for it in jsonld_items if it.type != "organization"]
    combined_raw = raw_dom_items + jsonld_items if raw_dom_items else jsonld_items
    detected_occurrences = len(raw_dom_items) if raw_dom_items else len(jsonld_items)

    merged_items, _ = _merge_and_dedupe_items(combined_raw, max_items=max_items)
    if not include_images:
        for it in merged_items:
            it.image_url = None
            it.images = []

    if "auto" not in item_types:
        allowed = set(item_types)
        merged_items = [it for it in merged_items if it.type in allowed]

    unique_count = len(merged_items)
    deduped_occurrences = max(0, detected_occurrences - unique_count)

    groups_map: dict[str, list[CollectionItem]] = {}
    for it in merged_items:
        groups_map.setdefault(it.type, []).append(it)

    collections = [CollectionGroup(type=t, items=its) for t, its in groups_map.items()]
    return collections, regions_scanned, detected_occurrences, unique_count, deduped_occurrences
