# ui/run_tab.py
# Run tab with: hyperlinks-only results, clickable thumbnails, TOURED cross-check via Supabase,
# post-run "Add to client", a bottom "Fix properties" section, and NC forced as the state everywhere.

import os, csv, io, re, time, json, asyncio, sys
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, urljoin, parse_qs, unquote

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# --- Safe import of clients helpers (avoids NameError for fetch_clients/upsert_client) ---
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

# ---------- Optional deps ----------
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Rerun helper ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Supabase ----------
try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = Any  # type: ignore

@st.cache_resource
def get_supabase() -> Optional["Client"]:
    if create_client is None:
        return None
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = get_supabase()

# ---------- Secrets/env ----------
for k in ["AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY",
          "BING_API_KEY","BING_CUSTOM_CONFIG_ID","GOOGLE_MAPS_API_KEY","BITLY_TOKEN"]:
    try:
        if k in st.secrets and st.secrets[k]: os.environ[k] = st.secrets[k]
    except Exception:
        pass
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT","").rstrip("/")
AZURE_SEARCH_INDEX    = os.getenv("AZURE_SEARCH_INDEX","")
AZURE_SEARCH_KEY      = os.getenv("AZURE_SEARCH_API_KEY","")
BING_API_KEY          = os.getenv("BING_API_KEY","")
BING_CUSTOM_ID        = os.getenv("BING_CUSTOM_CONFIG_ID","")
GOOGLE_MAPS_API_KEY   = os.getenv("GOOGLE_MAPS_API_KEY","")
BITLY_TOKEN           = os.getenv("BITLY_TOKEN","")
REQUEST_TIMEOUT       = 12

# ---------- Styles ----------
st.markdown("""
<style>
.center-box { padding:10px 12px; background:transparent; border-radius:12px; }
.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.new { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.dup { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
.badge.tour { background:#e0f2fe; color:#075985; border-color:#7dd3fc; }
.run-zone .stButton>button { background: linear-gradient(180deg, #0A84FF 0%, #0060DF 100%) !important; color:#fff !important; font-weight:800 !important; border:0 !important; border-radius:12px !important; box-shadow:0 10px 22px rgba(10,132,255,.35),0 2px 6px rgba(0,0,0,.18)!important; }
</style>
""", unsafe_allow_html=True)

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

# Slugify address exactly like the DB generated column
SLUG_KEEP = re.compile(r"[^\w\s,-]")
def address_to_slug(addr: str) -> str:
    if not addr: return ""
    s = SLUG_KEEP.sub("", addr.lower())
    s = re.sub(r"\s+", "-", s.strip())
    return s

def address_text_from_url(url: str) -> str:
    if not url: return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

def result_to_slug(r: Dict[str, Any]) -> str:
    addr = (r.get("input_address") or "").strip()
    if not addr:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        addr = address_text_from_url(url)
    return address_to_slug(addr)

# URL helpers
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}
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
    out = []
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

# --- NEW: more aggressive upgrade to canonical homedetails ---
def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if not r.ok:
            return url
        html = r.text

        # 1) Direct anchors to homedetails
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)

        # 2) Canonical link
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)

        # 3) JSON hints (canonicalUrl / url)
        for pat in [
            r'"canonicalUrl"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
            r'"url"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)

        # 4) Reconstruct using zpid + address
        mz = re.search(r'"zpid"\s*:\s*(\d+)', html)
        street = city = state = None
        for blk in _jsonld_blocks(html):
            if isinstance(blk, dict):
                addr = blk.get("address") or blk.get("itemOffered", {}).get("address")
                if isinstance(addr, dict):
                    street = street or addr.get("streetAddress")
                    city   = city   or addr.get("addressLocality")
                    state  = state  or addr.get("addressRegion")
        if not (street and city and state):
            m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I); street = street or (m.group(1) if m else None)
            m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I); city   = city   or (m.group(1) if m else None)
            m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I); state  = state  or (m.group(1) if m else None)
        if mz:
            zpid = mz.group(1)
            parts = []
            if street: parts.append(street)
            if city and state: parts.append(f"{city} {state}")
            slug_src = " ".join(parts) or zpid
            slug = re.sub(r'[^A-Za-z0-9]+', '-', slug_src).strip('-').lower()
            return f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
    except Exception:
        return url
    return url

# Content extractors
RE_PRICE  = re.compile(r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS   = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS  = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT   = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)
RE_DESC   = re.compile(r'"(?:description|homeDescription|marketingDescription)"\s*:\s*"([^"]+)"', re.I)

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")

def _find_maps_address_in_html(html: str) -> Optional[str]:
    if not html:
        return None
    # Google Maps links/embeds often leak the full address
    for pat in [
        r'href=["\']https?://(?:www\.)?google\.[^"\']+/maps[^"\']*?[\?&](?:q|query|daddr|destination)=([^"\']+)["\']',
        r'src=["\']https?://(?:maps\.googleapis|www\.google)\.[^"\']+/maps[^"\']*?[\?&](?:q|query|center|daddr)=([^"\']+)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            try:
                s = unquote(m.group(1))
                # trim coords like "37.7,-122.4" – keep only address-like
                if re.search(r"[A-Za-z]", s):
                    return re.sub(r"\s+", " ", s).strip()
            except Exception:
                pass
    return None

def extract_address_from_html(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html: return out

    # 0) Google Maps leak
    maps_addr = _find_maps_address_in_html(html)
    if maps_addr and ("," in maps_addr or re.search(r"\b[A-Z]{2}\b", maps_addr)):
        # Try to split "123 Main St, Raleigh, NC 27601"
        m = re.search(r'^(.*?),\s*([A-Za-z .\-]{2,40}),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?', maps_addr)
        if m:
            out["street"] = m.group(1).strip()
            out["city"]   = m.group(2).strip()
            out["state"]  = (m.group(3) or "").strip()
            out["zip"]    = (m.group(4) or "").strip()
            return out

    # 1) JSON-LD / microdata
    try:
        blocks = _jsonld_blocks(html)
        for b in blocks:
            addr = b.get("address") or b.get("itemOffered", {}).get("address") if isinstance(b, dict) else None
            if isinstance(addr, dict):
                street = addr.get("streetAddress") or ""
                city   = addr.get("addressLocality") or ""
                state  = (addr.get("addressRegion") or addr.get("addressCountry") or "")[:2]
                zipc   = addr.get("postalCode") or ""
                if street or (city and state):
                    out.update({"street": street, "city": city, "state": state, "zip": zipc})
                    if out["street"]: return out
    except Exception:
        pass

    # 2) Next.js/Redux JSON blobs (__NEXT_DATA__, window.__INITIAL_STATE__, etc.)
    json_candidates = re.findall(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>|'
        r'window\.__INITIAL_STATE__\s*=\s*({.*?});|'
        r'var\s+__INITIAL_STATE__\s*=\s*({.*?});|'
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html, re.I | re.S
    )
    for tup in json_candidates:
        blob = next((t for t in tup if t and t.strip().startswith("{")), "")
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        def _dig(d):
            if isinstance(d, dict):
                # Common shapes
                for key in ["address", "propertyAddress", "location", "listingAddress", "displayAddress"]:
                    a = d.get(key)
                    if isinstance(a, dict):
                        street = a.get("street") or a.get("streetAddress") or a.get("line1") or a.get("address1") or ""
                        city   = a.get("city") or a.get("locality") or a.get("addressLocality") or ""
                        state  = a.get("state") or a.get("stateCode") or a.get("region") or a.get("addressRegion") or a.get("stateOrProvince") or ""
                        zipc   = a.get("postalCode") or a.get("zip") or a.get("postal") or ""
                        if street or (city and state):
                            return {"street": street, "city": city, "state": state[:2], "zip": zipc}
                # Flat
                street = d.get("streetAddress") or d.get("address1") or d.get("addr1") or ""
                city   = d.get("city") or d.get("addressCity") or ""
                state  = d.get("state") or d.get("addressState") or d.get("stateOrProvince") or ""
                zipc   = d.get("postalCode") or d.get("zip") or ""
                if street or (city and state):
                    return {"street": street, "city": city, "state": state[:2], "zip": zipc}
                for v in d.values():
                    got = _dig(v) if isinstance(v, dict) else None
                    if got: return got
                    if isinstance(v, list):
                        for it in v:
                            got = _dig(it) if isinstance(it, dict) else None
                            if got: return got
            return None
        got = _dig(data)
        if got:
            return got

    # 3) Loose visible patterns (no strict requirement for street number)
    text = _strip_html(html)
    # Full address line
    m = re.search(r'(\d{1,6}\s+[^\n,]{3,80}),\s*([A-Za-z .\-]{2,40}),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?', text)
    if m:
        out["street"] = m.group(1)
        out["city"]   = m.group(2)
        out["state"]  = m.group(3)
        out["zip"]    = (m.group(4) or "")
        return out
    # City, ST ZIP
    m = re.search(r'\b([A-Za-z .\-]{2,40}),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)\b', text)
    if m and not out["city"]:
        out["city"], out["state"], out["zip"] = m.group(1), m.group(2), m.group(3)
    # City, ST (no zip)
    m = re.search(r'\b([A-Za-z .\-]{2,40}),\s*([A-Z]{2})\b', text)
    if m and not out["city"]:
        out["city"], out["state"] = m.group(1), m.group(2)

    # 4) Title/meta fallback
    if not out["street"]:
        for pat in [
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<title>\s*([^<]+?)\s*</title>",
            r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
        ]:
            m = re.search(pat, html, re.I)
            if m:
                title = re.sub(r'\s+', ' ', m.group(1)).strip()
                # "123 Main St, Raleigh, NC 27601" or "Subdivision, Raleigh, NC"
                m2 = re.search(r'^(.*?),\s*([A-Za-z .\-]{2,40}),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?', title)
                if m2:
                    out["street"] = m2.group(1).strip()
                    out["city"]   = m2.group(2).strip()
                    out["state"]  = (m2.group(3) or "").strip()
                    out["zip"]    = (m2.group(4) or "").strip()
                    break

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

# get MLS id directly from URL path/query (kept for other sources)
def extract_mls_id_from_url(u: str) -> Optional[str]:
    if not u:
        return None
    try:
        m = re.search(r'/(\d{6,})(?:[/?#]|$)', u)
        if m:
            return m.group(1)
        m = re.search(r'(?i)(?:mls|listing|id|listing_id)=([A-Za-z0-9\-]{6,})', u)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None

# Zillow canonicalization
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)
def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

# Permissive URL state check
def url_matches_city_state(url: str, city: Optional[str] = None, state: Optional[str] = None) -> bool:
    u = (url or '').lower()
    if not u:
        return False
    st2 = (state or "NC").lower().strip()
    return (f"-{st2}-" in u) or (f"/{st2}/" in u)

# Address parsing & variants
ADDR_PRIMARY = {"full_address","address","property address","property_address","site address","site_address",
                "street address","street_address","listing address","listing_address","location"}
NUM_KEYS   = {"street #","street number","street_no","streetnum","house_number","number","streetnumber"}
NAME_KEYS  = {"street name","street","st name","st_name","road","rd","avenue","ave","blvd","boulevard",
              "drive","dr","lane","ln","way","terrace","ter","court","ct","place","pl","parkway","pkwy",
              "square","sq","circle","cir","highway","hwy","route","rt"}
SUF_KEYS   = {"suffix","st suffix","street suffix","suffix1","suffix2","street_type","street type"}
CITY_KEYS  = {"city","municipality","town"}
STATE_KEYS = {"state","st","province","region"}
ZIP_KEYS   = {"zip","zip code","postal code","postalcode","zip_code","postal_code"}
MLS_ID_KEYS   = {"mls","mls id","mls_id","mls #","mls#","mls number","mlsnumber","listing id","listing_id"}
MLS_NAME_KEYS = {"mls name","mls board","mls provider","source","source mls","mls source"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

def norm_key(k:str) -> str: return re.sub(r"\s+"," ", (k or "").strip().lower())
def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v: return v
    return ""

def extract_components(row):
    n = { norm_key(k): (str(v).strip() if v is not None else "") for k,v in row.items() }
    for k in n.keys():
        if k in ADDR_PRIMARY and n[k]:
            return {"street_raw": n[k], "city":"", "state":"", "zip":"",
                    "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
                    "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)}
    num   = get_first_by_keys(n, NUM_KEYS)
    name  = get_first_by_keys(n, NAME_KEYS)
    suf   = get_first_by_keys(n, SUF_KEYS)
    city  = get_first_by_keys(n, CITY_KEYS)
    state = get_first_by_keys(n, STATE_KEYS)
    zipc  = get_first_by_keys(n, ZIP_KEYS)
    street_raw = " ".join([x for x in [num,name,suf] if x]).strip()
    return {"street_raw": street_raw, "city": city, "state": state, "zip": zipc,
            "mls_id": get_first_by_keys(n, MLS_ID_KEYS), "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)}

LAND_LEAD_TOKENS = {"lot","lt","tract","parcel","blk","block","tbd"}
HWY_EXPAND = {r"\bhwy\b":"highway", r"\bus\b":"US"}
DIR_MAP = {'s':'south','n':'north','e':'east','w':'west'}
LOT_REGEX = re.compile(r'\b(?:lot|lt)\s*[-#:]?\s*([A-Za-z0-9]+)\b', re.I)

def clean_land_street(street:str) -> str:
    if not street: return street
    s = street.strip()
    s = re.sub(r"^\s*0[\s\-]+", "", s)
    tokens = re.split(r"[\s\-]+", s)
    if tokens and tokens[0].lower() in LAND_LEAD_TOKENS:
        tokens = [t for t in tokens [1:] if t]; s = " ".join(tokens)
    s_lower = f" {s.lower()} "
    for pat,repl in HWY_EXPAND.items(): s_lower = re.sub(pat, f" {repl} ", s_lower)
    s = re.sub(r"\s+", " ", s_lower).strip()
    s = re.sub(r"[^\w\s/-]", "", s)
    return s

def compose_query_address(street, city, state, zipc, defaults):
    parts = [street]
    c  = (city  or defaults.get("city","")).strip()
    stt = (state or defaults.get("state","NC")).strip()
    z  = (zipc  or defaults.get("zip","")).strip()
    if c: parts.append(c)
    if stt: parts.append(stt)
    if z: parts.append(z)
    return " ".join([p for p in parts if p]).strip()

def generate_address_variants(street, city, state, zipc, defaults):
    city = str(city or defaults.get("city","")).strip()
    st   = (state or defaults.get("state","NC")).strip() or "NC"
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
        out.append(" ".join(parts).strip())
    # If no street, still try just "city st [zip]" variant so we can at least hit a Zillow city page
    if not base and (city or z):
        out.append(" ".join([p for p in [city, st, z] if p]))
    return [s for s in dict.fromkeys(out) if s.strip()]

# Search (Bing/Azure)
BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"
def _slug(text:str) -> str: return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

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

MLS_HTML_PATTERNS = [
    lambda mid: rf'\bMLS[^A-Za-z0-9]{{0,5}}#?\s*{re.escape(mid)}\b',
    lambda mid: rf'\bMLS\s*#?\s*{re.escape(mid)}\b',
    lambda mid: rf'"mls"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*{re.escape(mid)}',
]
def page_contains_mls(html:str, mls_id:str) -> bool:
    for mk in MLS_HTML_PATTERNS:
        if re.search(mk(mls_id), html, re.I): return True
    return False

def page_contains_city_state(html:str, city:str=None, state:str=None) -> bool:
    ok = False
    if city and re.search(re.escape(city), html, re.I): ok = True
    if (state or "NC") and re.search(rf'\b{re.escape(state or "NC")}\b', html, re.I): ok = True
    return ok

def confirm_or_resolve_on_page(url:str, mls_id:str=None, required_city:str=None, required_state:str="NC"):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); r.raise_for_status()
        html = r.text
        if mls_id and page_contains_mls(html, mls_id): return url, "mls_match"
        if page_contains_city_state(html, required_city, required_state) and "/homedetails/" in url:
            return url, "city_state_match"
        if url.endswith("_rb/") and "/homedetails/" not in url:
            cand = re.findall(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)[:8]
            for u in cand:
                try:
                    rr = requests.get(u, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); rr.raise_for_status()
                    h2 = rr.text
                    if (mls_id and page_contains_mls(h2, mls_id)): return u, "mls_match"
                    if page_contains_city_state(h2, required_city, required_state): return u, "city_state_match"
                except Exception:
                    continue
    except Exception:
        return None, None
    return None, None

# Candidate search by address variants (no MLS)
def resolve_homedetails_with_bing_variants(address_variants, required_state="NC", required_city=None, mls_id=None, delay=0.3, require_match=True):
    if not BING_API_KEY: return None, None
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
            for it in (items or []):
                url = (it.get("url") or it.get("link") or "").strip()
                if not url or "zillow.com" not in url: continue
                if "/homedetails/" not in url and "/homes/" not in url: continue
                if url in seen: continue
                seen.add(url); candidates.append(url)
            time.sleep(delay)
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok: return ok, mtype or "city_state_match"
    return None, None

# Azure search (optional)
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

# Construct rb deeplink (try to keep it meaningful)
def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    c = (city or defaults.get("city","")); c = c.strip() if isinstance(c, str) else ""
    st_abbr = (state or defaults.get("state","NC")).strip() or "NC"
    z = (zipc  or defaults.get("zip","")); z = z.strip() if isinstance(z, str) else ""
    if not (street or c):  # avoid useless "nc_rb"
        return ""
    slug_parts = [p for p in [street] if p]
    loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts: slug_parts.append(", ".join(loc_parts))
    if z:
        if slug_parts:
            slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else:
            slug_parts.append(z)
    slug = ", ".join(slug_parts)
    a = slug.lower(); a = re.sub(r"[^\w\s,-]", "", a).replace(",", ""); a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# --------- Client-side redirect follower ----------
def _extract_client_redirect(html: str, base_url: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\']\s*\d+\s*;\s*url=([^"\']+)["\']', html, re.I)
    if m:
        return urljoin(base_url, m.group(1).strip())
    for pat in [
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.assign\(\s*["\']([^"\']+)["\']\s*\)',
        r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return urljoin(base_url, m.group(1).strip())
    for pat in [
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return urljoin(base_url, m.group(1).strip())
    return None

def _expand_url_follow(url: str, max_hops: int = 8) -> Tuple[str, str, int, List[str]]:
    chain: List[str] = []
    current = url
    visited = set()
    for _ in range(max_hops):
        if current in visited:
            break
        visited.add(current)
        chain.append(current)
        try:
            r = requests.get(current, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except Exception:
            return current, "", 0, chain
        if 300 <= r.status_code < 400 and r.headers.get("Location"):
            current = urljoin(current, r.headers["Location"])
            continue
        html = ""
        if 200 <= r.status_code < 300:
            try:
                html = r.text
            except Exception:
                html = ""
        else:
            try:
                rr = requests.get(current, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                return rr.url or current, (rr.text if rr.ok else ""), rr.status_code, chain + [rr.url or current]
            except Exception:
                return current, "", r.status_code, chain
        nxt = _extract_client_redirect(html, current)
        if nxt and nxt not in visited:
            current = nxt
            continue
        if not html:
            try:
                rr = requests.get(current, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                return rr.url or current, (rr.text if rr.ok else ""), rr.status_code, chain + [rr.url or current]
            except Exception:
                return current, "", r.status_code, chain
        return current, html, r.status_code, chain
    try:
        rr = requests.get(current, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return rr.url or current, (rr.text if rr.ok else ""), rr.status_code, chain + [rr.url or current]
    except Exception:
        return current, "", 0, chain

# --------- Homespotter / l.hms.pt helpers ----------
def _is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return any(p in h for p in ["l.hms.pt", "hms.pt", "idx.homespotter.com", "homespotter.com"])
    except Exception:
        return False

def _find_zillow_link_in_html(html: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'href=["\'](https?://(?:www\.)?zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
    if m:
        return m.group(1)
    m = re.search(r'href=["\'](https?://(?:www\.)?zillow\.com/homes/[^"\']+?_rb/?)["\']', html, re.I)
    if m:
        return m.group(1)
    for pat in [
        r'"canonicalUrl"\s*:\s*"(https?://(?:www\.)?zillow\.com/homedetails/[^"]+)"',
        r'"url"\s*:\s*"(https?://(?:www\.)?zillow\.com/homedetails/[^"]+)"',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None

def _try_alt_hs_variants(url: str) -> List[Tuple[str,str]]:
    variants = []
    for suffix in ["?amp=1", "?output=1", "/amp", "/?view=amp", "?view=print", "/print", "?share=1", "?amp"]:
        if url.endswith("/"):
            variants.append(url.rstrip("/") + suffix)
        else:
            variants.append(url + suffix)
    out: List[Tuple[str,str]] = []
    for v in variants:
        try:
            r = requests.get(v, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.ok and r.text:
                out.append((r.url, r.text))
        except Exception:
            pass
    return out

def _extract_addr_homespotter(html: str) -> Dict[str, str]:
    addr = extract_address_from_html(html)
    return addr

# ---------- Helper referenced earlier ----------
def extract_any_mls_id(html: str) -> Optional[str]:
    if not html:
        return None
    pats = [
        r'\bMLS\s*#\s*([A-Za-z0-9\-]{5,})\b',
        r'\bMLS\s*ID\s*[:#]?\s*([A-Za-z0-9\-]{5,})\b',
        r'"mlsId"\s*:\s*"([A-Za-z0-9\-]{5,})"',
        r'"mls_id"\s*:\s*"([A-Za-z0-9\-]{5,})"',
        r'data-mls(?:number|id)=["\']([A-Za-z0-9\-]{5,})["\']',
        r'\bListing\s*#\s*([A-Za-z0-9\-]{5,})\b',
    ]
    for pat in pats:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    return None

# ---------- Resolve from arbitrary source URL (address-first; no MLS for HS) ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    final_url, html, _code, chain = _expand_url_follow(source_url)

    # Direct Zillow link present?
    zl = _find_zillow_link_in_html(html)
    if zl:
        zl = upgrade_to_homedetails_if_needed(zl)
        canon, _ = canonicalize_zillow(zl)
        return (canon or zl), address_text_from_url(canon or zl)

    if _is_homespotter_like(source_url) or _is_homespotter_like(final_url):
        # Try alt, more static variants first
        for url_alt, html_alt in _try_alt_hs_variants(final_url):
            zl = _find_zillow_link_in_html(html_alt)
            if zl:
                zl = upgrade_to_homedetails_if_needed(zl)
                canon, _ = canonicalize_zillow(zl)
                return (canon or zl), address_text_from_url(canon or zl)
            addr_alt = _extract_addr_homespotter(html_alt)
            if any(addr_alt.values()):
                street = (addr_alt.get("street") or "").strip()
                city   = (addr_alt.get("city") or "").strip()
                state  = (addr_alt.get("state") or defaults.get("state","NC")).strip() or "NC"
                zipc   = (addr_alt.get("zip") or "").strip()
                variants = generate_address_variants(street, city, state or "NC", zipc, {"state":"NC"})
                z2, _ = resolve_homedetails_with_bing_variants(
                    variants, required_state="NC", required_city=(city or None), mls_id=None
                )
                if z2:
                    return z2, compose_query_address(street, city, "NC", zipc, {"state":"NC"})

        # Address from current HTML
        hs_addr = _extract_addr_homespotter(html)
        street = (hs_addr.get("street") or "").strip()
        city   = (hs_addr.get("city") or "").strip()
        state  = (hs_addr.get("state") or defaults.get("state","NC")).strip() or "NC"
        zipc   = (hs_addr.get("zip") or "").strip()

        # Last hops: scan for Zillow link or address
        if not (street or city or zipc):
            for hop in reversed(chain[-3:]):
                try:
                    r = requests.get(hop, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                    if not r.ok:
                        continue
                    zl = _find_zillow_link_in_html(r.text)
                    if zl:
                        zl = upgrade_to_homedetails_if_needed(zl)
                        canon, _ = canonicalize_zillow(zl)
                        return (canon or zl), address_text_from_url(canon or zl)
                    tmp = _extract_addr_homespotter(r.text)
                    if any(tmp.values()):
                        street = street or tmp.get("street","")
                        city   = city   or tmp.get("city","")
                        state  = state  or tmp.get("state","") or "NC"
                        zipc   = zipc   or tmp.get("zip","")
                        break
                except Exception:
                    pass

        # Zillow search by address (NC enforced)
        if street or city or zipc:
            variants = generate_address_variants(street, city, state or "NC", zipc, {"state":"NC"})
            z2, _ = resolve_homedetails_with_bing_variants(
                variants, required_state="NC", required_city=(city or None), mls_id=None
            )
            if z2:
                return z2, compose_query_address(street, city, "NC", zipc, {"state":"NC"})

            # As last resort: meaningful /homes/*_rb/ only if we have at least a city
            rb = construct_deeplink_from_parts(street or "", city, "NC", zipc, {"state":"NC"})
            if rb:
                # Try to upgrade the rb page to a specific /homedetails/ if possible
                rb_up = upgrade_to_homedetails_if_needed(rb) or rb
                return rb_up, compose_query_address(street or "", city, "NC", zipc, {"state":"NC"})
            return "", ""  # avoid returning idx or bare "nc_rb"

        # No address found at all
        return "", ""

    # Generic (non-HS) pages
    zl = _find_zillow_link_in_html(html)
    if zl:
        zl = upgrade_to_homedetails_if_needed(zl)
        canon, _ = canonicalize_zillow(zl)
        return (canon or zl), address_text_from_url(canon or zl)

    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = addr.get("state","") or defaults.get("state","NC")
    zipc   = addr.get("zip","") or ""

    variants = generate_address_variants(street, city, state or "NC", zipc, {"state":"NC"})
    z2, _ = resolve_homedetails_with_bing_variants(
        variants, required_state="NC", required_city=city or None, mls_id=None
    )
    if z2:
        return z2, compose_query_address(street, city, "NC", zipc, {"state":"NC"})

    title = extract_title_or_desc(html)
    if title:
        for q in [f'"{title}" site:zillow.com/homedetails', f'{title} site:zillow.com']:
            items = bing_search_items(q)
            for it in items or []:
                u = (it.get("url") or "").strip()
                if "/homedetails/" in u and url_matches_city_state(u, None, "NC"):
                    return u, title

    rb = construct_deeplink_from_parts(street or title or "", city, "NC", zipc, {"state":"NC"})
    if rb:
        rb_up = upgrade_to_homedetails_if_needed(rb) or rb
        return rb_up, compose_query_address(street or title or "", city, "NC", zipc, {"state":"NC"})

    return "", ""

# ---------- Primary resolver (NC forced) ----------
def process_single_row(row, *, delay=0.5, land_mode=True, defaults=None,
                       require_state=True, mls_first=True, default_mls_name="", max_candidates=20):
    defaults = {"city":"", "state":"NC", "zip":""}
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)
    comp = extract_components(row)
    street_raw = comp["street_raw"]
    street_clean = clean_land_street(street_raw) if land_mode else street_raw
    variants = generate_address_variants(street_raw, comp["city"], comp["state"] or "NC", comp["zip"], defaults)
    if land_mode:
        variants = list(dict.fromkeys(variants + generate_address_variants(street_clean, comp["city"], comp["state"] or "NC", comp["zip"], defaults)))
    query_address = variants[0] if variants else compose_query_address(street_raw, comp["city"], "NC", comp["zip"], defaults)
    deeplink = construct_deeplink_from_parts(street_raw, comp["city"], "NC", comp["zip"], defaults)
    required_state_val = "NC" if require_state else None
    required_city_val  = comp["city"] or defaults.get("city")
    zurl, status = None, "fallback"
    mls_id   = (comp.get("mls_id") or "").strip()
    mls_name = (comp.get("mls_name") or default_mls_name or "").strip()
    # MLS path kept behind flag (not used for Homespotter anyway)
    if mls_first and mls_id:
        zurl, mtype = None, None  # disabled by user request for HS
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z and url_matches_city_state(z, required_city_val, "NC"): zurl, status = z, "azure_hit"
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants, required_state="NC", required_city=required_city_val,
            mls_id=None, delay=min(delay, 0.6), require_match=True
        )
        if zurl: status = "city_state_match"
    if not zurl:
        if deeplink:
            zurl, status = upgrade_to_homedetails_if_needed(deeplink) or deeplink, "deeplink_fallback"
        else:
            zurl, status = "", "no_match"
    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": "", "zillow_url": zurl, "status": status, "csv_photo": csv_photo}

# ---------- Text summarization / details ----------
KEY_HL = [("new roof","roof"),("hvac","hvac"),("ac unit","ac"),("furnace","furnace"),("water heater","water heater"),
          ("renovated","renovated"),("updated","updated"),("remodeled","remodeled"),("open floor plan","open plan"),
          ("cul-de-sac","cul-de-sac"),("pool","pool"),("fenced","fenced"),("acre","acre"),("hoa","hoa"),
          ("primary on main","primary on main"),("finished basement","finished basement")]
def _tidy_txt(s: str) -> str: return re.sub(r'\s+', ' ', (s or '')).strip()
def summarize_remarks(text: str, max_sent: int = 2) -> str:
    text = _tidy_txt(text)
    if not text: return ""
    sents = re.split(r'(?<=[\.!?])\s+', text)
    if len(sents) <= max_sent: return text
    pref_kw = ["updated","renovated","new","roof","hvac","kitchen","bath","floor","windows","mechanicals","acres","acre","lot","school","zoned","hoa","no hoa"]
    scored = [(sum(1 for k in pref_kw if k in s.lower()), i, s) for i,s in enumerate(sents[:8])]
    scored.sort(key=lambda x:(-x[0], x[1]))
    return " ".join([s for _,_,s in scored[:max_sent]])
def extract_highlights(text: str) -> List[str]:
    t = (text or "").lower(); out=[]
    for pat,label in KEY_HL:
        if pat in t: out.append(label)
    return list(dict.fromkeys(out))[:6]

# ---------- Async fetching / parsing ----------
async def _fetch_html_async(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200: return r.text
    except Exception:
        return ""
    return ""
def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html: return None
    for target_w in ("960","1152","768","1536"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html, re.I
        )
        if m: return m.group(1)
    m = re.search(r"srcset=['\"]([^'\"]*photos\.zillowstatic\.com[^'\"]+)['\"]", html, re.I)
    if m:
        cand=[]
        for part in m.group(1).split(","):
            part=part.strip(); m2=re.match(r"(https://photos\.zillowstatic\.com/\S+)\s+(\d+)w", part, re.I)
            if m2: cand.append((int(m2.group(2)), m2.group(1)))
        if cand:
            up=[u for (w,u) in cand if w<=1152]
            return (sorted(((w,u) for (w,u) in cand if w<=1152), key=lambda x:x[0])[-1][1] if up
                    else sorted(cand, key=lambda x:x[0])[-1][1])
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None
def parse_listing_meta(html: str) -> Dict[str, Any]:
    meta = {}
    if not html: return meta
    m = RE_PRICE.search(html);   meta["price"]  = m.group(1) if m else None
    m = RE_STATUS.search(html);  meta["status"] = m.group(1) if m else None
    m = RE_BEDS.search(html);    meta["beds"]   = m.group(1) if m else None
    m = RE_BATHS.search(html);   meta["baths"]  = m.group(1) if m else None
    m = RE_SQFT.search(html);    meta["sqft"]   = m.group(1) if m else None
    m = RE_DESC.search(html);    remark = m.group(1) if m else None
    if not remark:
        m2 = re.search(r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
        if m2: remark = m2.group(1)
    meta["remarks"] = remark
    img = extract_zillow_first_image(html)
    if not img:
        m3 = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
        if m3: img = m3.group(1)
    meta["image_url"] = img
    meta["summary"] = summarize_remarks(remark or "")
    meta["highlights"] = extract_highlights(remark or "")
    return meta
async def enrich_results_async(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targets = [(i, r["zillow_url"]) for i, r in enumerate(results) if "/homedetails/" in (r.get("zillow_url") or "")]
    if not targets: return results
    limits = min(12, max(4, len(targets)))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        sem = asyncio.Semaphore(limits)
        async def task(i, url):
            async with sem:
                html = await _fetch_html_async(client, url)
                return i, parse_listing_meta(html)
        coros = [task(i, url) for i, url in targets]
        for fut in asyncio.as_completed(coros):
            i, meta = await fut
            if meta: results[i].update(meta)
    return results

# ---------- Images fallback ----------
def picture_for_result_with_log(query_address: str, zurl: str, csv_photo_url: Optional[str] = None):
    log = {"url": zurl
