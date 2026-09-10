"""Public Polymarket market pages. A slug we never stored is not invented."""

import html


def market_url(slug: str, event_slug: str = "") -> str | None:
    slug = (slug or "").strip("/")
    if not slug:
        return None
    return f"https://polymarket.com/event/{(event_slug or slug).strip('/')}/{slug}"


def market_link(title: str, slug: str = "", event_slug: str = "") -> str:
    """The title itself, linked when the market page is known."""
    url = market_url(slug, event_slug)
    safe_title = html.escape(title)
    if not url:
        return safe_title
    return f'<a href="{html.escape(url, quote=True)}">{safe_title}</a>'
