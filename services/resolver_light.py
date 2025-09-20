# services/resolver_light.py
# Lightweight helpers used by Clients tab to avoid UI import cycles.

from typing import Tuple, Optional
import re
import requests

REQUEST_TIMEOUT = 12

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def is_probable_url(s: str) -> bool:
    """True if the string looks like a URL (http/https or scheme://)."""
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

_ZPID_RE = re.compile(r"(\d{6,})_zpid", re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    """
    Returns (canonical_url, zpid|None) for Zillow links.
    If not a Zillow link, canonical_url is the url without query/fragment.
    """
    if not url:
        return "", None
    base = re.sub(r"[#?].*$", "", url)
    m_full = re.search(r"^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)", url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = _ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    """Strips query/fragment; if homedetails, returns the homedetails canonical."""
    if not url:
        return ""
    base = re.sub(r"[#?].*$", "", url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    """Follow redirects and fetch HTML; returns (final_url, html, status_code)."""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    If a Zillow page is not homedetails, try to extract a homedetails link from the HTML.
    Otherwise return the input url unchanged.
    """
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', r.text)
        return m.group(1) if m else url
    except Exception:
        return url

def resolve_from_source_url(source_url: str) -> Tuple[str, str]:
    """
    Minimal resolver for arbitrary listing links used in Clients tab cross-checks.
    - Follow redirects
    - If the final page is a Zillow homedetails page, return it
    - Else, if a homedetails link is present in the HTML, return that
    Returns: (resolved_url, optional_display_text)  # display_text is usually ""
    """
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    if "/homedetails/" in (final_url or "") and "zillow.com" in (final_url or ""):
        return final_url, ""
    if html:
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)
        if m:
            return m.group(1), ""
    return final_url or source_url, ""
