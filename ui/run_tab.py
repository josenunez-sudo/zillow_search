# ui/run_tab.py
# Run tab — paste addresses or ANY listing links → Zillow /homedetails/ URLs
# Improvements:
# - Homespotter microservice used first.
# - Robust Homespotter address extraction from JSON & microdata.
# - Avoids using marketing titles ("4 beds 2 baths...") as street.
# - City-only fallback builds city search: /homes/<City>-<ST>_rb/ (no more /homes/nc_rb/).

import os, io, re, csv, json, time
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import streamlit as st
import streamlit.components.v1 as components

# --- Optional: safe import of clients helpers (works even if clients tab is absent) ---
try:
    from ui.clients_tab import fetch_clients, upsert_client
except Exception:
    try:
        from clients_tab import fetch_clients, upsert_client  # type: ignore
    except Exception:
        def fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:  # type: ignore
            return []
        def upsert_client(name: str, active: bool = True):  # type: ignore
            return False, "Clients module not available"

# ---------- Rerun helper ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Secrets/env ----------
for k in [
    "AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY",
    "BING_API_KEY","BING_CUSTOM_CONFIG_ID","GOOGLE_MAPS_API_KEY","BITLY_TOKEN",
    "HS_RESOLVER_URL","HS_RESOLVER_KEY"  # ← NEW
]:
    try:
        if hasattr(st, "secrets") and k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

BING_API_KEY    = os.getenv("BING_API_KEY","")
BING_CUSTOM_ID  = os.getenv("BING_CUSTOM_CONFIG_ID","")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY","")
BITLY_TOKEN     = os.getenv("BITLY_TOKEN","")

# Microservice: headless Homespotter resolver
HS_RESOLVER_URL = os.getenv("HS_RESOLVER_URL","").rstrip("/")
HS_RESOLVER_KEY = os.getenv("HS_RESOLVER_KEY","")

REQUEST_TIMEOUT = 12

# ---------- Styles ----------
st.markdown("""
<style>
.center-box { padding:10px 12px; background:transparent; border-radius:12px; }
.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.new { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.dup { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
.run-zone .stButton>button { background: linear-gradient(180deg, #0A84FF 0%, #0060DF 100%) !important; color:#fff !important; font-weight:800 !important; border:0 !important; border-radius:12px !important; box-shadow:0 10px 22px rgba(10,132,255,.35),0 2px 6px rgba(0,0,0,.18)!important; }
</style>
""", unsafe_allow_html=True)

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

# --- JSON-LD blocks helper ---
def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I|re.S):
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

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Generic extractor (works for many broker/MLS/IDX pages).
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html:
        return out

    # Prefer JSON-LD Organization/Place/Residence/RealEstateListing
    blocks = _jsonld_blocks(html)
    for b in blocks:
        addr = None
        if isinstance(b, dict):
            if isinstance(b.get("itemOffered"), dict):
                addr = b["itemOffered"].get("address")
            addr = addr or b.get("address")
        if isinstance(addr, dict):
            out["street"] = out["street"] or addr.get("streetAddress","")
            out["city"]   = out["city"]   or addr.get("addressLocality","")
            out["state"]  = out["state"]  or (addr.get("addressRegion") or "")[:2]
            out["zip"]    = out["zip"]    or addr.get("postalCode","")
            if out["street"] and out["city"] and out["state"]:
                break

    # Fallback: explicit keys
    if not out["street"]:
        m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I); out["street"] = out["street"] or (m.group(1) if m else "")
    if not out["city"]:
        m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I); out["city"] = out["city"] or (m.group(1) if m else "")
    if not out["state"]:
        m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I); out["state"] = out["state"] or (m.group(1) if m else "")
    if not out["zip"]:
        m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I); out["zip"] = out["zip"] or (m.group(1) if m else "")

    # DO NOT use generic titles as a street — they produce junk slugs
    return out

def extract_title_or_desc(html: str) -> str:
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m: return re.sub(r'\s+', ' ', m.group(1)).strip()
    return ""

# Zillow canonicalization
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)
def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    Try to convert /homes/*_rb/ pages to canonical /homedetails/.../_zpid/.
    Best-effort only.
    """
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok: return url
        html = r.text
        # direct anchor
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m: return m.group(1)
        # canonical link
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m: return m.group(1)
        return url
    except Exception:
        return url

def make_preview_url(url: str) -> str:
    if not url: return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ---------- Address helpers ----------
DIR_MAP = {'s':'south','n':'north','e':'east','w':'west'}
ROAD_WORDS = r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|way|blvd|boulevard|ct|court|pl|place|pkwy|parkway|hwy|highway|cir|circle)"
MARKETING_HINTS = re.compile(r"\b(beds?|baths?|homespotter|for\s*\$|price|mls)\b", re.I)

def _slug(text:str) -> str: return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

def is_address_like(s: str) -> bool:
    if not s: return False
    if MARKETING_HINTS.search(s or ""):
        return False
    s2 = s.strip()
    # number + road word OR comma-separated "..., City, ST ..."
    if re.search(r"\d{1,6}\s+[\w\.\- ]+\b" + ROAD_WORDS + r"\b", s2, re.I):
        return True
    if re.search(r"^[^,]+,\s*[^,]+,\s*[A-Z]{2}(?:\s+\d{5})?$", s2):
        return True
    return False

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
    core = base
    core = re.sub(r'\bu\.?s\.?\b', 'US', core, flags=re.I)
    core = re.sub(r'\bhwy\b', 'highway', core, flags=re.I)
    core = re.sub(r'\b([NSEW])\b', lambda m: DIR_MAP.get(m.group(1).lower(), m.group(1)), core, flags=re.I)
    variants = {core, re.sub(r'\bhighway\b', 'hwy', core, flags=re.I)}
    out = []
    for sv in variants:
        parts = [sv] + [p for p in [city, st, z] if p]
        out.append(" ".join(parts))
    return [s for s in dict.fromkeys(out) if s.strip()]

def construct_city_deeplink(city: str, state: str) -> str:
    city_slug = _slug(city) if city else ""
    st = (state or "").strip()
    if city_slug and st:
        return f"https://www.zillow.com/homes/{city_slug}-{st}_rb/"
    if st:
        return f"https://www.zillow.com/homes/{st.lower()}_rb/"
    return "https://www.zillow.com/homes/"

def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    """
    Only build a street-based deeplink if we have a *street-like* string.
    Otherwise, return a **city-only** deeplink (prevents junk slugs like “4-beds-…”).
    """
    street = (street or "").strip()
    city   = (city or "").strip()
    state  = (state or "").strip()
    if not is_address_like(street):
        # City search fallback
        return construct_city_deeplink(city, state)

    # Build street-based deeplink
    c = city
    st_abbr = state
    z = (zipc or "").strip()
    slug_parts = [street]; loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts: slug_parts.append(", ".join(loc_parts))
    if z:
        if slug_parts: slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else: slug_parts.append(z)
    slug = ", ".join(slug_parts)
    a = slug.lower(); a = re.sub(r"[^\w\s,-]", "", a).replace(",", ""); a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---------- Bing search (address → Zillow) ----------
BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"

def url_matches_city_state(url:str, city:str=None, state:str=None) -> bool:
    u = (url or '').lower()
    ok = True
    if state:
        st2 = state.lower().strip()
        if f"-{st2}-" not in u and f"/{st2}/" not in u: ok = False
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

def resolve_homedetails_with_bing_variants(address_variants, required_state=None, required_city=None, delay=0.3):
    if not BING_API_KEY: return None, None
    candidates, seen = [], set()
    for qaddr in address_variants:
        queries = [
            f'{qaddr} site:zillow.com/homedetails',
            f'"{qaddr}" site:zillow.com/homedetails',
            f'{qaddr} site:zillow.com',
        ]
        for q in queries:
            items = bing_search_items(q)
            for it in (items or []):
                url = (it.get("url") or it.get("link") or "").strip()
                if not url or "zillow.com" not in url: continue
                if "/homedetails/" not in url and "/homes/" not in url: continue
                if required_state or required_city:
                    if not url_matches_city_state(url, required_city, required_state): continue
                if url in seen: continue
                seen.add(url); candidates.append(url)
            time.sleep(delay)
    for u in candidates:
        if "/homedetails/" in u:
            return u, "city_state_match"
    return None, None

# ---------- Homespotter microservice ----------
def _is_homespotter_like(u: str) -> bool:
    try:
        from urllib.parse import urlparse
        h = (urlparse(u).hostname or "").lower()
        return any(dom in h for dom in ("l.hms.pt", "idx.homespotter.com", "homespotter.com"))
    except Exception:
        return False

def resolve_hs_service(source_url: str):
    """
    Call the headless resolver microservice.
    Returns: (zillow_url | None, address | None, status_str)
    """
    if not (HS_RESOLVER_URL and source_url):
        return None, None, "service_not_configured"

    headers = {"Content-Type": "application/json"}
    if HS_RESOLVER_KEY:
        headers["X-API-Key"] = HS_RESOLVER_KEY

    try:
        r = requests.post(
            f"{HS_RESOLVER_URL}/resolve",
            json={"url": source_url},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            return None, None, f"http_{r.status_code}"
        data = r.json() or {}
        z = (data.get("zillow_url") or "").strip() or None
        addr = (data.get("address") or "").strip() or None
        status = data.get("status") or ("ok" if z else "no_match")
        return z, addr, status
    except Exception as e:
        return None, None, f"error:{type(e).__name__}"

# ---------- EXTRA: Homespotter-specific HTML address extractor ----------
def _extract_hs_address(html: str) -> Dict[str, str]:
    """
    Try hard to get a proper street address from Homespotter pages.
    We avoid using marketing titles (beds/baths/price) as street.
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}

    if not html:
        return out

    # 1) JSON keys commonly used by Homespotter/IDX
    key_patterns = [
        r'"formattedAddress"\s*:\s*"([^"]+)"',
        r'"addressLine1"\s*:\s*"([^"]+)"',
        r'"address1"\s*:\s*"([^"]+)"',
        r'"line1"\s*:\s*"([^"]+)"',
        r'"street"\s*:\s*"([^"]+)"',
    ]
    for pat in key_patterns:
        m = re.search(pat, html, re.I)
        if m:
            cand = m.group(1).strip()
            if is_address_like(cand):
                out["street"] = cand
                break
            # if it's a whole "123 Main St, Erwin, NC 28339" string, split:
            if re.search(r",\s*[A-Za-z ]+,\s*[A-Z]{2}", cand):
                out["street"] = cand

    # City/State/Zip keys
    city_keys  = [r'"addressLocality"\s*:\s*"([^"]+)"', r'"city"\s*:\s*"([^"]+)"']
    state_keys = [r'"addressRegion"\s*:\s*"([A-Z]{2})"', r'"state(?:OrProvince)?"\s*:\s*"([A-Z]{2})"']
    zip_keys   = [r'"postal(?:Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"']

    for pat in city_keys:
        m = re.search(pat, html, re.I)
        if m: out["city"] = out["city"] or m.group(1).strip()
    for pat in state_keys:
        m = re.search(pat, html, re.I)
        if m: out["state"] = out["state"] or m.group(1).strip()
    for pat in zip_keys:
        m = re.search(pat, html, re.I)
        if m: out["zip"] = out["zip"] or m.group(1).strip()

    # 2) Microdata fallbacks
    if not out["street"]:
        m = re.search(r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', html, re.I)
        if m:
            cand = m.group(1).strip()
            if is_address_like(cand):
                out["street"] = cand

    if not out["city"]:
        m = re.search(r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', html, re.I)
        if m: out["city"] = m.group(1).strip()

    if not out["state"]:
        m = re.search(r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', html, re.I)
        if m: out["state"] = m.group(1).strip()

    if not out["zip"]:
        m = re.search(r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', html, re.I)
        if m: out["zip"] = m.group(1).strip()

    # 3) As a very last resort, parse a “Street, City, ST (ZIP)” from title-like text
    if not out["street"]:
        # Never treat marketing titles as street
        for pat in [r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"<title>\s*([^<]+?)\s*</title>"]:
            m = re.search(pat, html, re.I)
            if not m: 
                continue
            title = m.group(1).strip()
            if MARKETING_HINTS.search(title):
                continue
            # Try to split: "123 Main St, Erwin, NC 28339 ..."
            m2 = re.search(r"^\s*([^,]+),\s*([^,]+),\s*([A-Z]{2})(?:\s+(\d{5}))?", title)
            if m2:
                out["street"] = out["street"] or m2.group(1).strip()
                out["city"]   = out["city"]   or m2.group(2).strip()
                out["state"]  = out["state"]  or m2.group(3).strip()
                out["zip"]    = out["zip"]    or (m2.group(4).strip() if m2.group(4) else "")
                break

    return out

# ---------- Resolve from arbitrary source URL (address-first; HS service first) ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    """
    Given any listing URL (Homespotter short/idx link, MLS page, brokerage page),
    return a Zillow /homedetails/.../_zpid/ URL when possible + a human-ish address string.
    """
    # 0) If it’s a Homespotter-ish link, try the headless service first.
    if _is_homespotter_like(source_url):
        z, addr, stcode = resolve_hs_service(source_url)
        if z:
            return z, (addr or "")

    # 1) Expand redirects and fetch the page
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # 2) If expansion landed on an idx.homespotter.com page, try service again
    if _is_homespotter_like(final_url):
        z, addr, stcode = resolve_hs_service(final_url)
        if z:
            return z, (addr or "")

        # If service still didn’t return, run our HS-special extractor
        hs_addr = _extract_hs_address(html)
        street = hs_addr.get("street","") or ""
        city   = hs_addr.get("city","") or ""
        state  = hs_addr.get("state","") or ""
        zipc   = hs_addr.get("zip","") or ""

        if is_address_like(street):
            variants = generate_address_variants(street, city, state, zipc, defaults)
            z2, _ = resolve_homedetails_with_bing_variants(variants, required_state=(state or None), required_city=(city or None))
            if z2:
                return z2, compose_query_address(street, city, state, zipc, defaults)
            # city-only deeplink if Zillow not found
            return construct_deeplink_from_parts(street, city, state, zipc, defaults), compose_query_address(street, city, state, zipc, defaults)
        else:
            # We have at least city/state? Build a city search deeplink
            if city or state:
                return construct_city_deeplink(city, state), compose_query_address("", city, state, zipc, defaults)
            # If truly nothing, fall through to generic handler

    # 3) Generic page (non-HS)
    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = addr.get("state","") or ""
    zipc   = addr.get("zip","") or ""
    if is_address_like(street) or (city and state):
        variants = generate_address_variants(street or "", city, state, zipc, defaults)
        z2, _ = resolve_homedetails_with_bing_variants(variants, required_state=(state or None), required_city=(city or None))
        if z2:
            return z2, compose_query_address(street, city, state, zipc, defaults)

    # 4) Title → Bing (very loose), but DO NOT treat title as street
    title = extract_title_or_desc(html)
    if title and not MARKETING_HINTS.search(title):
        for q in [f'"{title}" site:zillow.com/homedetails', f'{title} site:zillow.com/homedetails']:
            items = bing_search_items(q)
            for it in (items or []):
                u = (it.get("url") or "")
                if "/homedetails/" in u:
                    return u, (street or title)

    # 5) Fallback: city-only deeplink if we have city/state; else expanded URL
    if city or state:
        return construct_city_deeplink(city, state), compose_query_address("", city, state, zipc, defaults)

    return final_url, ""

# ---------- Parsers for pasted input ----------
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

def norm_key(k:str) -> str: return re.sub(r"\s+"," ", (k or "").strip().lower())
def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v: return v
    return ""

def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Accepts:
      - CSV with headers (address fields OR url field)
      - Plain list (one URL or one address per line)
    Returns a list of dict rows.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV first (requires a header line)
    try:
        sample = text.splitlines()
        if len(sample) >= 2 and ("," in sample[0] or "\t" in sample[0]):
            # auto-detect delimiter
            dialect = csv.Sniffer().sniff(sample[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            rows = [dict(r) for r in reader]
            if rows:
                return rows
    except Exception:
        pass

    # Fallback: one item per line
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if is_probable_url(s):
            rows.append({"url": s})
        else:
            rows.append({"address": s})
    return rows

def _detect_source_url(row: Dict[str, Any]) -> Optional[str]:
    for k, v in row.items():
        if norm_key(k) in URL_KEYS and is_probable_url(str(v)):
            return str(v).strip()
    for k in ("url", "source", "href", "link"):
        if is_probable_url(str(row.get(k, ""))):
            return str(row.get(k)).strip()
    return None

# ---------- Output ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    def pick_url(r):
        raw = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        return raw

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
        if not href: 
            continue
        safe_href = escape(href)
        link_txt = href
        li_html.append(
            f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a></li>'
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

# ---------- MAIN: render_run_tab ----------
def render_run_tab(state: dict):
    NO_CLIENT = "➤ No client (no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    st.header("Run")

    # -- Client & campaign (kept simple; no logging in this trimmed version)
    try:
        clients = fetch_clients(include_inactive=False) or []
    except Exception:
        clients = []
    client_names = [c.get("name","") for c in clients if c.get("name")]
    options = [NO_CLIENT] + client_names + [ADD_SENTINEL]

    with st.container():
        c1, c2 = st.columns([2, 1])
        with c1:
            chosen = st.selectbox("Client", options, index=0)
        with c2:
            campaign = st.text_input("Campaign tag (optional)", value=state.get("campaign",""))

        if chosen == ADD_SENTINEL:
            new_name = st.text_input("New client name")
            if st.button("Create client", type="primary"):
                ok, msg = upsert_client(new_name.strip(), active=True)
                if ok:
                    st.success(f"Added “{new_name}”.")
                    _safe_rerun()
                else:
                    st.error(msg or "Could not add client.")
            return

    st.subheader("Paste rows")
    st.caption("Paste CSV with address fields or URLs, **or** paste one URL/address per line. Homespotter links supported via the headless resolver.")
    paste = st.text_area("Input", height=180, placeholder="e.g. https://l.hms.pt/... or https://idx.homespotter.com/... or 407 E Woodall St, ...")

    with st.expander("Advanced"):
        require_city_state = st.checkbox("Bias to city/state match when searching", value=True)
        include_notes = st.checkbox("Include notes when exporting CSV", value=False)

    run_col, export_col = st.columns([1, 2])
    with run_col:
        run_btn = st.button("🚀 Run", type="primary", use_container_width=True)

    results: List[Dict[str, Any]] = []

    if run_btn:
        rows_in: List[Dict[str, Any]] = _rows_from_paste(paste)
        if not rows_in:
            st.warning("Nothing to process.")
            return

        with st.status("Resolving…", expanded=True) as st_status:
            st.write(f"Parsed **{len(rows_in)}** row(s).")

            defaults = {"city":"", "state":"", "zip":""}
            total = len(rows_in)
            prog = st.progress(0.0, text="Working…")
            for i, row in enumerate(rows_in, start=1):
                src_url = _detect_source_url(row)
                if src_url:
                    zurl, inferred_addr = resolve_from_source_url(src_url, defaults)
                    # Always try to upgrade/canonicalize
                    zurl = upgrade_to_homedetails_if_needed(zurl)
                    canon, _ = canonicalize_zillow(zurl)
                    zurl = canon or zurl

                    results.append({
                        "input_address": inferred_addr or row.get("address") or row.get("full_address") or "",
                        "zillow_url": zurl,
                        "preview_url": zurl,
                        "display_url": zurl,
                        "status": "source_url",
                    })
                else:
                    # Address-only row → try address search; fallback to /homes/_rb (but street-like only)
                    street = row.get("address","") or row.get("full_address","") or ""
                    if street:
                        variants = generate_address_variants(street, "", "", "", defaults)
                        z2, _ = resolve_homedetails_with_bing_variants(
                            variants,
                            required_state=None if not require_city_state else "",
                            required_city=None if not require_city_state else ""
                        )
                        if z2:
                            z2 = upgrade_to_homedetails_if_needed(z2)
                            canon, _ = canonicalize_zillow(z2); z2 = canon or z2
                            results.append({
                                "input_address": street, "zillow_url": z2, "preview_url": z2, "display_url": z2, "status":"addr_match"
                            })
                        else:
                            # If no homedetails: build city-only deeplink (if we can parse a city/st from the string)
                            m = re.search(r",\s*([^,]+),\s*([A-Z]{2})", street)
                            if m:
                                city, st_abbr = m.group(1).strip(), m.group(2).strip()
                                rb = construct_city_deeplink(city, st_abbr)
                            else:
                                rb = construct_deeplink_from_parts(street, "", "", "", defaults)
                            results.append({
                                "input_address": street, "zillow_url": rb, "preview_url": rb, "display_url": rb, "status":"deeplink_fallback"
                            })
                    else:
                        results.append({"input_address":"","zillow_url":"","preview_url":"","display_url":"","status":"skip"})
                prog.progress(i/total, text=f"Resolved {i}/{total}")

            st_status.update(label="Done", state="complete")

        st.subheader("Results")
        results_list_with_copy_all(results)

        # Export
        with export_col:
            fmt = st.radio("Export format", ["txt", "md", "html", "csv"], horizontal=True, index=0)
            payload, mime = build_output(results, fmt=fmt, use_display=True, include_notes=include_notes)
            st.download_button(
                "Download",
                data=payload.encode("utf-8"),
                file_name=f"results.{fmt}",
                mime=mime,
                use_container_width=True,
            )

        st.divider()

    # ---------- Fix properties ----------
    st.subheader("Fix properties")
    st.caption("Paste any listing links (Homespotter/IDX/MLS). I’ll output clean canonical **/homedetails/** Zillow URLs.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                # Homespotter? Ask the microservice first
                if _is_homespotter_like(best):
                    z, addr, sc = resolve_hs_service(best)
                    if z:
                        best = z
                    else:
                        # fallback through HTML/address heuristics
                        z2, _addr2 = resolve_from_source_url(best, {"city":"", "state":"", "zip":""})
                        best = z2 or best
                else:
                    z2, _addr2 = resolve_from_source_url(best, {"city":"", "state":"", "zip":""})
                    best = z2 or best

                # Try to upgrade to canonical homedetails if possible
                best = upgrade_to_homedetails_if_needed(best) or best
                # Canonicalize (strip params, ensure /homedetails/.../_zpid/)
                canon, _ = canonicalize_zillow(best)
                best = canon or best
            except Exception:
                pass

            fixed.append(best)
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# (optional) Allow running this module directly for local testing:
if __name__ == "__main__":
    render_run_tab({})
