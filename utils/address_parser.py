# utils/address_parser.py
# Public API:
#   - extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]
#       Returns: {"full","streetAddress","addressLocality","addressRegion","postalCode","source","url_final"}
#   - address_to_zillow_rb(addr: Dict[str, Optional[str]], default_state: str = "NC") -> str
#   - address_as_markdown_link(url: str, label: Optional[str] = None, parse_html: bool = True, timeout: float = 12.0)
#
# This module focuses on being resilient for IDX/Homespotter ("l.hms.pt", "idx.homespotter.com") pages.
# Strategy:
#   1) Follow redirects to the final URL.
#   2) Look for a single formatted address string in JSON/script tags.
#   3) Look for JSON objects with {line1/city/state/zip}, including many variant key names.
#   4) Probe JSON-LD <script type="application/ld+json"> blocks for an "address" object.
#   5) Probe microdata via itemprop attributes.
#   6) Fall back to meta <title> / og:title text parsing.
#   7) As a last resort, try to reconstruct from URL slug; if still unknown, don't invent a state —
#      the caller can inject defaults if desired.
#
from __future__ import annotations

import re
import json
from typing import Dict, Any, Optional, Tuple
from urllib.parse import urlparse, unquote
import requests

try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def _normalize(d: Dict[str, Optional[str]]) -> Optional[str]:
    """Return a single 'full' string if we have enough parts; else None."""
    street = (d.get("streetAddress") or "").strip()
    city   = (d.get("addressLocality") or "").strip()
    state  = (d.get("addressRegion") or "").strip()
    zipc   = (d.get("postalCode") or "").strip()
    if street and city and state:
        return f"{street}, {city}, {state}" + (f" {zipc}" if zipc else "")
    return None

def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s,-]", "", s).replace(",", "")
    s = re.sub(r"\s+", "-", s.strip())
    return s

SINGLE_STRING_PAT = re.compile(
    r'(?i)\b(\d{1,6}\s+[A-Za-z0-9\.\-\' ]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Court|Ct|Lane|Ln|Way|Terrace|Ter|Place|Pl|Highway|Hwy|Pkwy|Parkway|Circle|Cir)\b[^,]*,\s*[A-Za-z\.\-\' ]+?,\s*[A-Za-z]{2}(?:\s+\d{5}(?:-\d{4})?)?)\b'
)

def _pick_address_from_text(text: str) -> Optional[Dict[str, str]]:
    """Parse '123 Main St, City, ST 12345' into components; return None if it doesn't look like an address."""
    if not text:
        return None
    m = re.search(r'^\s*(.+?),\s*([A-Za-z\.\-\' ]+),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?\s*$', text.strip())
    if not m:
        m2 = SINGLE_STRING_PAT.search(text)
        if not m2:
            return None
        text = m2.group(1)
        m = re.search(r'^\s*(.+?),\s*([A-Za-z\.\-\' ]+),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?\s*$', text.strip())
        if not m:
            return None
    street, city, state, zipc = m.group(1), m.group(2), m.group(3), m.group(4) or ""
    return {"streetAddress": street, "addressLocality": city, "addressRegion": state, "postalCode": zipc}

def _first_match(json_text: str, keys: list[str]) -> Optional[str]:
    """Return first value for any of the keys in a JSON-ish blob; supports single or double quotes and optional nesting."""
    if not json_text:
        return None
    for k in keys:
        # direct "key":"value"
        for pat in (
            rf'["\']{re.escape(k)}["\']\s*:\s*["\']([^"\']+)["\']',
            rf'{re.escape(k)}\s*[:=]\s*["\']([^"\']+)["\']',  # sometimes without quotes on key
        ):
            m = re.search(pat, json_text, re.I)
            if m:
                v = m.group(1).strip()
                if v:
                    return v
    return None

def _addr_from_url_slug(url: str) -> Optional[Dict[str, str]]:
    if not url:
        return None
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        t = re.sub(r"[-+]", " ", m.group(1)).strip().title()
        cand = _pick_address_from_text(t)
        if cand:
            return cand
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        t = re.sub(r"[-+]", " ", m.group(1)).strip().title()
        cand = _pick_address_from_text(t)
        if cand:
            return cand
    return None

def _extract_meta_title_desc(soup) -> Optional[str]:
    # og:title
    tag = soup.find("meta", attrs={"property":"og:title"})
    if tag and tag.get("content"): return tag["content"].strip()
    # twitter:title
    tag = soup.find("meta", attrs={"name":"twitter:title"})
    if tag and tag.get("content"): return tag["content"].strip()
    # description as a fallback
    tag = soup.find("meta", attrs={"name":"description"})
    if tag and tag.get("content"): return tag["content"].strip()
    # title
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return None

def _addr_from_json_ld(soup) -> Optional[Dict[str, str]]:
    for tag in soup.find_all("script", attrs={"type":"application/ld+json"}):
        txt = (tag.string or tag.text or "").strip()
        if not txt:
            continue
        try:
            blob = json.loads(txt)
        except Exception:
            # Sometimes multiple JSON objects without an array wrapper; try to extract the first {...}
            m = re.search(r"(\{.*\})", txt, re.S)
            if not m:
                continue
            try:
                blob = json.loads(m.group(1))
            except Exception:
                continue
        def _address_from_obj(obj: Any) -> Optional[Dict[str, str]]:
            if not isinstance(obj, dict):
                return None
            # Direct address
            addr = obj.get("address")
            if isinstance(addr, dict):
                street = addr.get("streetAddress") or ""
                city   = addr.get("addressLocality") or ""
                state  = addr.get("addressRegion") or ""
                zipc   = addr.get("postalCode") or ""
                if street or city:
                    return {"streetAddress": street, "addressLocality": city, "addressRegion": state, "postalCode": zipc}
            # Sometimes under itemOffered.address
            item = obj.get("itemOffered") if isinstance(obj.get("itemOffered"), dict) else None
            if item and isinstance(item.get("address"), dict):
                a = item["address"]
                return {"streetAddress": a.get("streetAddress",""), "addressLocality": a.get("addressLocality",""),
                        "addressRegion": a.get("addressRegion",""), "postalCode": a.get("postalCode","")}
            return None
        # object or list
        if isinstance(blob, dict):
            cand = _address_from_obj(blob)
            if cand and _normalize(cand):
                return cand
        elif isinstance(blob, list):
            for o in blob:
                cand = _address_from_obj(o)
                if cand and _normalize(cand):
                    return cand
    return None

def _addr_from_microdata(soup) -> Optional[Dict[str, str]]:
    # Schema.org itemprops
    street = city = state = zipc = ""
    for attr, key in (
        ("itemprop","streetAddress"),
        ("itemprop","addressLocality"),
        ("itemprop","addressRegion"),
        ("itemprop","postalCode"),
    ):
        el = soup.find(attrs={attr:key})
        if not el: continue
        if key == "streetAddress": street = (el.get_text(" ", strip=True) or "").strip()
        elif key == "addressLocality": city = (el.get_text(" ", strip=True) or "").strip()
        elif key == "addressRegion": state = (el.get_text(" ", strip=True) or "").strip()
        elif key == "postalCode": zipc = (el.get_text(" ", strip=True) or "").strip()
    if street or (city and state):
        return {"streetAddress": street, "addressLocality": city, "addressRegion": state, "postalCode": zipc}
    return None

def _addr_from_hs_specific(soup) -> Optional[Dict[str, str]]:
    """
    Homespotter/IDX flavors often embed address in window.__INITIAL_STATE__ or similar.
    We scan scripts explicitly for those first and support many key names.
    """
    street_keys = ["streetAddress","address1","line1","addr1","address_line1","street","addressLine1"]
    city_keys   = ["addressLocality","city","locality","municipality","town","cityName","addressCity","localityName"]
    state_keys  = ["addressRegion","state","stateCode","region","province","stateOrProvince"]
    zip_keys    = ["postalCode","zip","zipCode","postal","postal_code"]
    full_keys   = ["formattedAddress","displayAddress","fullAddress","address","propertyAddress","full_address"]

    for tag in soup.find_all("script"):
        txt = (tag.string or tag.text or "").strip()
        if not txt:
            continue

        # 0) Single-string first (formattedAddress etc.)
        for key in full_keys:
            m = re.search(rf'["\']{re.escape(key)}["\']\s*:\s*["\']([^"\']+)["\']', txt, re.I)
            if m:
                cand = _pick_address_from_text(m.group(1))
                if cand and _normalize(cand):
                    return cand

        # 1) Object style keys
        s = _first_match(txt, street_keys)
        c = _first_match(txt, city_keys)
        st = _first_match(txt, state_keys)
        zp = _first_match(txt, zip_keys)
        if st and re.fullmatch(r"[A-Za-z]{2}", st): st = st.upper()
        if s and (c or st):
            cand = {"streetAddress": s, "addressLocality": (c or ""),
                    "addressRegion": (st or ""), "postalCode": (zp or "")}
            norm = _normalize(cand)
            if norm:
                return cand

        # 2) Nested objects: "address": { "line1": "...", "city": "...", ... }
        for m in re.finditer(r'"address"\s*:\s*\{(.*?)\}', txt, re.S|re.I):
            inner = m.group(1)
            s = _first_match(inner, street_keys)
            c = _first_match(inner, city_keys)
            st = _first_match(inner, state_keys)
            zp = _first_match(inner, zip_keys)
            if st and re.fullmatch(r"[A-Za-z]{2}", st): st = st.upper()
            if s and (c or st):
                cand = {"streetAddress": s, "addressLocality": (c or ""),
                        "addressRegion": (st or ""), "postalCode": (zp or "")}
                norm = _normalize(cand)
                if norm:
                    return cand
    return None

def _fetch_html(url: str, timeout: float = 12.0) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=timeout, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]:
    """
    Expand a URL and try *hard* to extract a postal address from the landing page.
    Works well with Homespotter IDX links and many other listing providers.
    Returns a dict containing components and a 'full' normalized string when possible.
    """
    final_url, html, _ = _fetch_html(url, timeout=timeout)
    info: Dict[str, Optional[str]] = {
        "full": None, "streetAddress": None, "addressLocality": None,
        "addressRegion": None, "postalCode": None, "source": None,
        "url_final": final_url
    }
    if not parse_html or not html or not BeautifulSoup:
        # URL slug fallback only
        a = _addr_from_url_slug(final_url)
        if a and _normalize(a):
            return {**info, **a, "full": _normalize(a), "source": "url-slug"}
        return info

    soup = BeautifulSoup(html, "html.parser")

    # 1) Homespotter/IDX scripts (very common)
    a = _addr_from_hs_specific(soup)
    if a and _normalize(a):
        return {**info, **a, "full": _normalize(a), "source": "hs-script"}

    # 2) JSON-LD
    a = _addr_from_json_ld(soup)
    if a and _normalize(a):
        return {**info, **a, "full": _normalize(a), "source": "json-ld"}

    # 3) Microdata / itemprop
    a = _addr_from_microdata(soup)
    if a and _normalize(a):
        return {**info, **a, "full": _normalize(a), "source": "microdata"}

    # 4) Meta title/desc
    t = _extract_meta_title_desc(soup)
    cand = _pick_address_from_text(t or "")
    if cand and _normalize(cand):
        return {**info, **cand, "full": _normalize(cand), "source": "meta-title"}

    # 5) URL slug fallback
    a = _addr_from_url_slug(final_url)
    if a and _normalize(a):
        return {**info, **a, "full": _normalize(a), "source": "url-slug"}

    return info

def address_to_zillow_rb(addr: Dict[str, Optional[str]], default_state: str = "NC") -> str:
    """
    Build a Zillow /homes/*_rb/ deeplink from parsed components.
    If street/city are missing, do NOT fabricate them; only append a default state if nothing else is known.
    """
    street = (addr.get("streetAddress") or "").strip()
    city   = (addr.get("addressLocality") or "").strip()
    state  = (addr.get("addressRegion") or "").strip() or ""
    zipc   = (addr.get("postalCode") or "").strip()

    parts = []
    if street:
        parts.append(street)
    loc = ", ".join([p for p in [city, (state or "")] if p])
    if loc:
        parts.append(loc)
    if zipc:
        if parts:
            parts[-1] = (parts[-1] + f" {zipc}")
        else:
            parts.append(zipc)

    if not parts:
        # Last resort: state-only
        st = state or default_state
        return f"https://www.zillow.com/homes/{_slugify(st)}_rb/"

    slug = _slugify(", ".join(parts))
    return f"https://www.zillow.com/homes/{slug}_rb/"

def address_as_markdown_link(url: str, label: Optional[str] = None, parse_html: bool = True, timeout: float = 12.0) -> Tuple[str, Dict[str, Optional[str]]]:
    info = extract_address(url, parse_html=parse_html, timeout=timeout)
    text = label or info.get("full")
    if not text:
        netloc = urlparse(info.get("url_final") or url).netloc or "Listing"
        text = netloc
    return f"[{text}]({info.get('url_final') or url})", info
