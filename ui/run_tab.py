# ui/run_tab.py
# Address Alchemist — RUN TAB ONLY
# Paste addresses OR arbitrary listing links (Homespotter/IDX OK) → Zillow.
# Guarantees: never return HS/IDX link; never produce /homes/nc_rb/; tries to upgrade to /homedetails/..._zpid/.

from __future__ import annotations

import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, parse_qs, unquote

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ---------------------------
# Config & Secrets
# ---------------------------
def _get_secret(name: str, default: str = "") -> str:
    try:
        if hasattr(st, "secrets") and name in st.secrets and st.secrets[name]:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)

AZURE_SEARCH_ENDPOINT = _get_secret("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_INDEX    = _get_secret("AZURE_SEARCH_INDEX", "")
AZURE_SEARCH_KEY      = _get_secret("AZURE_SEARCH_API_KEY", "")
BING_API_KEY          = _get_secret("BING_API_KEY", "")
BING_CUSTOM_ID        = _get_secret("BING_CUSTOM_CONFIG_ID", "")

REQUEST_TIMEOUT       = 12

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://www.google.com/",
}

# ---------------------------
# HTTP helpers
# ---------------------------
def _get(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    # same as _get but named for clarity
    return _get(url)

async def _fetch_html_async(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        if r.status_code == 200:
            return r.text
    except Exception:
        pass
    return ""

# ---------------------------
# Parsing utils
# ---------------------------
def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            blob = m.group(1)
            try:
                data = json.loads(blob)
                if isinstance(data, list):
                    out.extend([d for d in data if isinstance(d, dict)])
                elif isinstance(data, dict):
                    out.append(data)
            except Exception:
                continue
    except Exception:
        pass
    return out

def extract_title_or_desc(html: str) -> str:
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def _strong_full_address_fallback(html: str) -> Optional[Tuple[str, str, str, str]]:
    # e.g. "123 Main St, Raleigh, NC 27601"
    pat = re.compile(
        r'(\d{1,6}\s+[A-Za-z0-9\.\-\'/ ]+),\s*([A-Za-z\.\-\' ]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)',
        re.I
    )
    m = pat.search(html or "")
    if not m:
        return None
    street, city, state, zipc = m.group(1).strip(), m.group(2).strip(), m.group(3).strip(), m.group(4).strip()
    if len(street.split()) < 2:
        return None
    return street, city, state, zipc

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Robust address extractor for Homespotter/IDX/Zillow pages.
    Order: JSON-LD → common HS/IDX script blobs → microdata → og:title/title → strong fallback.
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html:
        return out

    # 1) JSON-LD (RealEstateListing/Product/Residence)
    try:
        for b in _jsonld_blocks(html):
            if not isinstance(b, dict):
                continue
            addr = b.get("address") or (b.get("itemOffered", {}) if isinstance(b.get("itemOffered"), dict) else {}).get("address")
            if isinstance(addr, dict):
                street = addr.get("streetAddress") or addr.get("street") or ""
                city   = addr.get("addressLocality") or addr.get("city") or ""
                state  = addr.get("addressRegion") or addr.get("state") or ""
                zipc   = addr.get("postalCode") or addr.get("zip") or ""
                if state and len(state) > 2:
                    m = re.search(r'\b([A-Za-z]{2})\b', state)
                    state = (m.group(1) if m else state)[:2]
                if street or (city and state):
                    out.update({"street": street.strip(), "city": city.strip(), "state": state[:2].strip(), "zip": zipc.strip()})
                    if street:
                        return out
    except Exception:
        pass

    # 2) HS/IDX common JSON blobs (lots of variants observed)
    blobs = [
        (r'"address(?:Line1|1)?"\s*:\s*"([^"]+)"', "street"),
        (r'"displayAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"fullAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"formattedAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"address"\s*:\s*"([^"]+)"', "street"),
        (r'"city"\s*:\s*"([^"]+)"', "city"),
        (r'"state(?:OrProvince)?"\s*:\s*"([A-Za-z]{2,})"', "state"),
        (r'"postal(?:Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
        # Sometimes single JSON object holds everything:
        (r'"streetAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city"),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2,})"', "state"),
    ]
    for pat, key in blobs:
        m = re.search(pat, html, re.I)
        if m and not out.get(key):
            out[key] = m.group(1).strip()

    # 3) Microdata
    micro = [
        (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street"),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city"),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2,})', "state"),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip"),
    ]
    for pat, key in micro:
        if out.get(key):
            continue
        m = re.search(pat, html, re.I)
        if m:
            out[key] = m.group(1).strip()

    if out.get("state") and len(out["state"]) > 2:
        m = re.search(r'\b([A-Za-z]{2})\b', out["state"])
        if m:
            out["state"] = m.group(1)

    # 4) Title/og:title
    if not out["street"]:
        title = extract_title_or_desc(html)
        if title and re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
            out["street"] = title.strip()

    # 5) Very strong full-address fallback
    if not out["street"]:
        tup = _strong_full_address_fallback(html)
        if tup:
            street, city, state, zipc = tup
            out["street"], out["city"], out["state"], out["zip"] = street, city, state, zipc

    for k in ("street", "city", "state", "zip"):
        out[k] = (out.get(k) or "").strip()
    return out

def _homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return any(k in h for k in ("l.hms.pt", "homespotter", "idx.homespotter.com", "hs_"))
    except Exception:
        return False

def _alt_variants(u: str) -> List[str]:
    # Try AMP-like / embed variants that often contain simpler markup with the address
    qs = ["amp=1", "output=amp", "client=web", "view=embed"]
    out = []
    base, html_q = re.sub(r"[?#].*$", "", u), ""
    for q in qs:
        out.append(u + (("&" if "?" in u else "?") + q))
    if not u.endswith("/"):
        out.append(u + "/")
    return out

# ---------------------------
# Zillow helpers
# ---------------------------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        html = r.text

        # Direct anchor
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)

        # Canonical
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)

        # JSON hints
        for pat in [
            r'"canonicalUrl"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
            r'"url"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)

        # Rebuild if zpid available
        mz = re.search(r'"zpid"\s*:\s*(\d+)', html)
        if mz:
            zpid = mz.group(1)
            street = city = state = None
            for blk in _jsonld_blocks(html):
                addr = blk.get("address") or (blk.get("itemOffered", {}) if isinstance(blk.get("itemOffered"), dict) else {}).get("address")
                if isinstance(addr, dict):
                    street = street or addr.get("streetAddress")
                    city   = city   or addr.get("addressLocality")
                    state  = state  or addr.get("addressRegion")
            if street and city and state:
                slug_src = f"{street} {city} {state}"
                slug = re.sub(r'[^A-Za-z0-9]+', '-', slug_src).strip('-').lower()
                return f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
    except Exception:
        return url
    return url

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ---------------------------
# Address variants & helpers
# ---------------------------
DIR_MAP = {'s':'south','n':'north','e':'east','w':'west'}
LOT_REGEX = re.compile(r'\b(?:lot|lt)\s*[-#:]?\s*([A-Za-z0-9]+)\b', re.I)

def compose_query_address(street, city, state, zipc, defaults):
    parts = [street]
    c  = (city  or defaults.get("city","")).strip()
    stt = (state or defaults.get("state","")).strip()
    z  = (zipc  or defaults.get("zip","")).strip()
    if c: parts.append(c)
    if stt: parts.append(stt)
    if z: parts.append(z)
    return " ".join([p for p in parts if p]).strip()

def generate_address_variants(street, city, state, zipc, defaults):
    city = (city or defaults.get("city","")).strip()
    st   = (state or defaults.get("state","")).strip()
    z    = (zipc or defaults.get("zip","")).strip()
    base = (street or "").strip()
    lot_match = LOT_REGEX.search(base); lot_num = lot_match.group(1) if lot_match else None
    core = base
    core = re.sub(r'\bu\.?s\.?\b', 'US', core, flags=re.I)
    core = re.sub(r'\bhwy\b', 'highway', core, flags=re.I)
    core = re.sub(r'\b([NSEW])\b', lambda m: DIR_MAP.get(m.group(1).lower(), m.group(1)), core, flags=re.I)
    variants = {core, re.sub(r'\bhighway\b', 'hwy', core, flags=re.I)}
    lot_variants = set(variants)
    if lot_num:
        for v in list(variants):
            lot_variants.update({f"lot {lot_num} {v}", f"{v} lot {lot_num}", f"lot-{lot_num} {v}"})
    stripped_variants = { LOT_REGEX.sub('', v).strip() for v in list(lot_variants) }
    all_street_variants = lot_variants | stripped_variants
    out = []
    for sv in all_street_variants:
        parts = [sv] + [p for p in [city, st, z] if p]
        out.append(" ".join(parts))
    return [s for s in dict.fromkeys(out) if s.strip()]

# ---------------------------
# Search (Azure/Bing) — optional
# ---------------------------
BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"

def _slug(text:str) -> str: return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

def url_matches_city_state(url:str, city:str=None, state:str=None) -> bool:
    u = (url or '')
    ok = True
    if state:
        st2 = state.upper().strip()
        if f"-{st2}-" not in u and f"/{st2.lower()}/" not in u: ok = False
    if city and ok:
        cs = f"-{_slug(city)}-"
        if cs not in u: ok = False
    return ok

def bing_search_items(query):
    key = BING_API_KEY; custom = BING_CUSTOM_ID
    if not key: return []
    h = {"Ocp-Apim-Subscription-Key": key}
    try:
        if custom:
            p = {"q": query, "customconfig": custom, "mkt": "en-US", "count": 15}
            r = requests.get(BING_CUSTOM, headers=h, params=p, timeout=REQUEST_TIMEOUT)
        else:
            p = {"q": query, "mkt": "en-US", "count": 15, "responseFilter": "Webpages"}
            r = requests.get(BING_WEB, headers=h, params=p, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("webPages", {}).get("value") if "webPages" in data else data.get("items", []) or []
    except requests.RequestException:
        return []

def azure_search_first_zillow(query_address):
    if not (AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX and AZURE_SEARCH_KEY): return None
    url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{AZURE_SEARCH_INDEX}/docs/search?api-version=2023-11-01"
    h = {"Content-Type":"application/json","api-key":AZURE_SEARCH_KEY}
    try:
        r = requests.post(url, headers=h, data=json.dumps({"search": query_address, "top": 1}), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}; hits = data.get("value") or data.get("results") or []
        if not hits: return None
        doc = hits[0].get("document") or hits[0]
        for k in ("zillow_url","zillowLink","zillow","url","link"):
            v = doc.get(k) if isinstance(doc, dict) else None
            if isinstance(v, str) and "zillow.com" in v: return v
    except requests.RequestException:
        return None
    return None

def page_contains_city_state(html:str, city:str=None, state:str=None) -> bool:
    ok = False
    if city and re.search(re.escape(city), html, re.I): ok = True
    if state and re.search(rf'\b{re.escape(state)}\b', html, re.I): ok = True
    return ok

def confirm_or_resolve_on_page(url:str, required_city:str=None, required_state:str=None):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
        html = r.text
        if "/homedetails/" in url:
            return url, "homedetails"
        if url.endswith("_rb/") and "/homedetails/" not in url:
            cand = re.findall(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)[:12]
            for u in cand:
                try:
                    rr = requests.get(u, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); rr.raise_for_status()
                    h2 = rr.text
                    if page_contains_city_state(h2, required_city, required_state):
                        return u, "city_state_match"
                except Exception:
                    continue
    except Exception:
        return None, None
    return None, None

def resolve_homedetails_with_bing_variants(address_variants, required_state=None, required_city=None, delay=0.3, require_match=False):
    if not BING_API_KEY:
        return None, None
    candidates, seen = [], set()
    for qaddr in address_variants:
        queries = [
            f'{qaddr} site:zillow.com/homedetails',
            f'"{qaddr}" site:zillow.com/homedetails',
            f'{qaddr} land site:zillow.com/homedetails',
            f'{qaddr} lot site:zillow.com/homedetails',
        ]
        for q in queries:
            items = bing_search_items(q)
            for it in items or []:
                url = (it.get("url") or it.get("link") or "").strip()
                if not url or "zillow.com" not in url: continue
                if "/homedetails/" not in url and "/homes/" not in url: continue
                if require_match and not url_matches_city_state(url, required_city, required_state): continue
                if url in seen: continue
                seen.add(url); candidates.append(url)
            time.sleep(delay)
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, required_city=required_city, required_state=required_state)
        if ok: return ok, mtype or "city_state_match"
    return None, None

def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    """
    Only build /homes/<slug>_rb/ when we actually have a street or city (prevents /homes/nc_rb/).
    """
    c = (city or defaults.get("city","")).strip()
    st_abbr = (state or defaults.get("state","")).strip()
    z = (zipc  or defaults.get("zip","")).strip()
    if not (street or c):
        return ""  # <- critical: do not produce /homes/nc_rb/

    slug_parts = []
    s = (street or "").strip()
    if s: slug_parts.append(s)
    loc = ", ".join([p for p in [c, st_abbr] if p])
    if loc: slug_parts.append(loc)
    if z:
        if slug_parts:
            slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else:
            slug_parts.append(z)

    slug = ", ".join(slug_parts)
    a = slug.lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---------------------------
# Core resolver
# ---------------------------
def _extract_address_with_variants(final_url: str, html: str) -> Dict[str, str]:
    addr = extract_address_from_html(html)
    if addr.get("street") or addr.get("city") or addr.get("state") or addr.get("zip"):
        return addr

    # Try alternate variants (AMP/embed) for HS/IDX
    if _homespotter_like(final_url):
        for alt in _alt_variants(final_url):
            try:
                u2, h2, _ = _get(alt)
                a2 = extract_address_from_html(h2)
                if a2.get("street") or a2.get("city") or a2.get("state") or a2.get("zip"):
                    return a2
            except Exception:
                continue

    # Try querystring hints
    try:
        q = parse_qs(urlparse(final_url).query)
        for key in ("address","addr","q","query","location","street"):
            if q.get(key):
                candidate = q[key][0]
                if len(candidate) > 8:
                    return {"street": candidate, "city":"", "state":"", "zip":""}
    except Exception:
        pass

    # Title w/ zip
    tit = extract_title_or_desc(html)
    if tit and re.search(r"\d{5}", tit):
        return {"street": tit, "city":"", "state":"", "zip":""}

    return {"street": "", "city": "", "state": "", "zip": ""}

def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str, str]:
    """
    Returns (zillow_url, query_address, status)
    Status: 'homedetails' | 'variant' | 'deeplink' | 'unresolved'
    - Never returns a Homespotter/IDX link.
    - Never returns /homes/nc_rb/.
    """
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # Zillow already? normalize/upgrade and return.
    if "zillow.com" in (final_url or ""):
        cleaned = upgrade_to_homedetails_if_needed(final_url)
        return cleaned, "", "homedetails" if "/homedetails/" in cleaned else "variant"

    # Hard rule: do not return HS/IDX
    # Extract address (robust), possibly via alt variants
    addr = _extract_address_with_variants(final_url, html)
    street = (addr.get("street") or "").strip()
    city   = (addr.get("city") or "").strip()
    state  = (addr.get("state") or "").strip()
    zipc   = (addr.get("zip") or "").strip()

    if not (street or city or state or zipc):
        # gated page and no address found → unresolved (return empty, not HS)
        return "", "", "unresolved"

    query_addr = compose_query_address(street, city, state, zipc, defaults)

    # Azure (if configured)
    if query_addr:
        z = azure_search_first_zillow(query_addr)
        if z and url_matches_city_state(z, city or None, state or None):
            z = upgrade_to_homedetails_if_needed(z)
            return z, query_addr, "homedetails" if "/homedetails/" in z else "variant"

    # Bing search for /homedetails/ (if key present)
    if street or city:
        variants = generate_address_variants(street, city, state, zipc, defaults)
        zurl, _ = resolve_homedetails_with_bing_variants(
            variants,
            required_state=state or None,
            required_city=(city or None),
            delay=0.3,
            require_match=True
        )
        if zurl:
            zurl = upgrade_to_homedetails_if_needed(zurl)
            return zurl, compose_query_address(street, city, state, zipc, defaults), "homedetails" if "/homedetails/" in zurl else "variant"

    # Last resort: build /homes/<address>_rb/ ONLY if we truly have address, then try to upgrade
    deeplink = construct_deeplink_from_parts(street, city, state, zipc, defaults)
    if deeplink:
        best, _ = confirm_or_resolve_on_page(deeplink, required_city=city or None, required_state=state or None)
        if best:
            best = upgrade_to_homedetails_if_needed(best)
            return best, compose_query_address(street, city, state, zipc, defaults), "homedetails" if "/homedetails/" in best else "variant"
        return deeplink, compose_query_address(street, city, state, zipc, defaults), "deeplink"

    # Shouldn’t happen, but if no deeplink allowed, mark unresolved
    return "", "", "unresolved"

# ---------------------------
# Output helpers
# ---------------------------
def build_output(rows: List[Dict[str, Any]], fmt: str) -> Tuple[str, str]:
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","url","status"]
        s = io.StringIO(); w = csv.DictWriter(s, fieldnames=fields); w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields if k != "url"}
            row["url"] = pick_url(r)
            w.writerow(row)
        return s.getvalue(), "text/csv"

    if fmt == "html":
        items = []
        for r in rows:
            u = pick_url(r)
            if not u: continue
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    lines = []
    for r in rows:
        u = pick_url(r)
        if u: lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")

def results_list_with_copy_all(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        label = href if href else "(unresolved)"
        safe_href = escape(href) if href else "#"
        li_html.append(
            f'<li style="margin:0.2rem 0;">' +
            (f'<a href="{safe_href}" target="_blank" rel="noopener">{escape(label)}</a>' if href else f"{escape(label)}") +
            ("" if r.get("status") in ("","homedetails","variant","deeplink") else f" <span style='font-size:11px;color:#64748b'>[{escape(r.get('status'))}]</span>") +
            "</li>"
        )

    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u:
            copy_lines.append(u.strip())
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }}
      </style>
    </head><body>
      <div class="results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
        <ul class="link-list" id="resultsList">{items_html}</ul>
      </div>
      <script>
        (function(){{
          const btn=document.getElementById('copyAll');
          const text = "{copy_text}".replaceAll("\\n", "\\n");
          btn.addEventListener('click', async () => {{
            try {{
              await navigator.clipboard.writeText(text);
              const prev=btn.textContent; btn.textContent='✓'; setTimeout(()=>{{ btn.textContent=prev; }}, 900);
            }} catch(e) {{
              const prev=btn.textContent; btn.textContent='×'; setTimeout(()=>{{ btn.textContent=prev; }}, 900);
            }}
          }});
        }})();
      </script>
    </body></html>"""
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

# ---------------------------
# Main UI
# ---------------------------
def render_run_tab(state: Optional[dict] = None):
    st.header("Run")
    st.caption("Paste addresses or **any listing links** (Homespotter/IDX OK) → Zillow. We’ll never hand back gated HS links.")

    col1, col2, col3 = st.columns([1.1, 1, 1.2])
    with col1:
        remove_dupes = st.checkbox("Remove duplicates", value=True)
    with col2:
        trim_spaces  = st.checkbox("Auto-trim", value=True)
    with col3:
        show_preview = st.checkbox("Show preview", value=True)

    paste = st.text_area(
        "Paste addresses or links",
        placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/403/340/10127718/74461375/1091612/GI\n123 US-301 S, Four Oaks, NC 27524",
        height=160
    )
    file = st.file_uploader("Upload CSV (optional)", type=["csv"], label_visibility="collapsed")

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean: List[str] = []
    for ln in lines_raw:
        ln = ln.strip() if trim_spaces else ln
        if not ln:
            continue
        if remove_dupes and ln in lines_clean:
            continue
        lines_clean.append(ln)

    if show_preview and lines_clean:
        st.markdown("**Preview (first 5):**")
        st.markdown(
            "<ul class='link-list'>" + "\n".join([f"<li>{escape(p)}</li>" for p in lines_clean[:5]]) + ("<li>…</li>" if len(lines_clean) > 5 else "") + "</ul>",
            unsafe_allow_html=True
        )

    clicked = st.button("🚀 Resolve to Zillow", use_container_width=True)
    if clicked:
        rows_in: List[Dict[str, Any]] = []
        if file is not None:
            try:
                content = file.getvalue().decode("utf-8-sig")
                reader = list(csv.DictReader(io.StringIO(content)))
                rows_in.extend(reader)
            except Exception as e:
                st.warning(f"CSV read failed: {e}")

        for item in lines_clean:
            if item.strip():
                rows_in.append({"source_or_address": item})

        if not rows_in:
            st.error("Please paste at least one address or link and/or upload a CSV.")
            return

        defaults = {"city":"", "state":"", "zip":""}
        total = len(rows_in)
        results: List[Dict[str, Any]] = []

        prog = st.progress(0, text="Resolving…")
        for i, row in enumerate(rows_in, start=1):
            val = (row.get("source_or_address") or "").strip()
            if not val:
                results.append({"input_address":"", "zillow_url":"", "preview_url":"", "status":"no_input"})
                prog.progress(i/total, text=f"Processed {i}/{total}")
                continue

            if re.match(r"^[a-z]+://", val):  # URL path
                zurl, used_addr, stat = resolve_from_source_url(val, defaults)
                # Never return Homespotter; if unresolved, leave url empty
                preview = make_preview_url(zurl) if zurl else ""
                results.append({
                    "input_address": used_addr or "",
                    "zillow_url": zurl,
                    "preview_url": preview,
                    "status": stat
                })
            else:  # Freeform address → build deeplink only when we have useful text
                street = val
                deeplink = construct_deeplink_from_parts(street, "", "", "", defaults)
                results.append({
                    "input_address": val,
                    "zillow_url": deeplink or "",
                    "preview_url": make_preview_url(deeplink) if deeplink else "",
                    "status": "deeplink" if deeplink else "unresolved"
                })
            prog.progress(i/total, text=f"Resolved {i}/{total}")
        prog.progress(1.0, text="Done")

        # Final normalization
        for r in results:
            z = r.get("zillow_url")
            if z:
                r["zillow_url"] = upgrade_to_homedetails_if_needed(z)
                r["preview_url"] = make_preview_url(r["zillow_url"])

        st.success(f"Processed {len(results)} item(s)")
        st.markdown("#### Results")
        results_list_with_copy_all(results)

        fmt = st.selectbox("Download format", ["txt","csv","md","html"], index=0)
        payload, mime = build_output(results, fmt)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        st.download_button("Export", data=payload, file_name=f"address_alchemist_{ts}.{fmt}", mime=mime, use_container_width=True)

    # =========================
    # Fix links (restored)
    # =========================
    st.divider()
    st.subheader("Fix links")
    st.caption("Paste HS/IDX/Zillow links. Returns clean Zillow links. If a link can’t be resolved (gated), it’s marked `(unresolved)` — we won’t hand back HS links.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        annotated: List[str] = []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            final = ""
            try:
                if "zillow.com" in u:
                    final = upgrade_to_homedetails_if_needed(u) or ""
                else:
                    z, _addr, stat = resolve_from_source_url(u, {"city":"", "state":"", "zip":""})
                    final = upgrade_to_homedetails_if_needed(z) if z else ""
                # Guard against bogus deeplinks
                if final.endswith("/homes/_rb/") or final.endswith("/homes/nc_rb/"):
                    final = ""
            except Exception:
                final = ""

            if final:
                fixed.append(final)
                annotated.append(final)
            else:
                annotated.append(f"{u}  (unresolved)")

            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        st.markdown("**Resolved**")
        if fixed:
            items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
            st.markdown(items, unsafe_allow_html=True)
            st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")
        else:
            st.write("_None_")

        st.markdown("**All (with status)**")
        items2 = "\n".join([f"- {escape(x)}" for x in annotated])
        st.markdown(items2, unsafe_allow_html=True)

# Allow running directly
if __name__ == "__main__":
    render_run_tab()
