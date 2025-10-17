# ui/run_tab.py
# Run tab with: hyperlinks-only results, clickable thumbnails, TOURED cross-check via Supabase,
# post-run "Add to client", a bottom "Fix properties" section, and NC forced as the state everywhere.

import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse

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
    from urllib.parse import unquote
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

# --- NEW: more aggressive upgrade to canonical homedetails ---
def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    Upgrade Zillow /homes/..._rb/ to canonical /homedetails/.../_zpid/ when possible.
    """
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

def extract_address_from_html(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html: return out
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
    m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I); out["street"] = out["street"] or (m.group(1) if m else "")
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I); out["city"] = out["city"] or (m.group(1) if m else "")
    m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I); out["state"] = out["state"] or (m.group(1) if m else "")
    m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I); out["zip"] = out["zip"] or (m.group(1) if m else "")
    if not out["street"]:
        for pat in [
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<title>\s*([^<]+?)\s*</title>",
        ]:
            m = re.search(pat, html, re.I)
            if m:
                title = m.group(1)
                if re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
                    out["street"] = title
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

# ---------- get MLS id directly from URL path/query (Homespotter/IDX fix) ----------
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

# ---------- Permissive URL state check (only state required; confirm on page) ----------
def url_matches_city_state(url: str, city: Optional[str] = None, state: Optional[str] = None) -> bool:
    u = (url or '').lower()
    if not u:
        return False
    st2 = (state or "NC").lower().strip()
    return (f"-{st2}-" in u) or (f"/{st2}/" in u)

# ---------- Address parsing & variants ----------
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
        tokens = [t for t in tokens[1:] if t]; s = " ".join(tokens)
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
    # Force NC whenever state is missing
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
        out.append(" ".join(parts))
    return [s for s in dict.fromkeys(out) if s.strip()]

# ---------- Search (Bing/Azure) ----------
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

# ---------- Candidate search by MLS (generic) ----------
def find_zillow_by_mls_with_confirmation(mls_id, required_state="NC", required_city=None, mls_name=None, delay=0.35, require_match=True, max_candidates=20):
    if not (BING_API_KEY and mls_id): return None, None
    q_mls = [
        f'"MLS# {mls_id}" site:zillow.com',
        f'"{mls_id}" "MLS" site:zillow.com',
        f'{mls_id} site:zillow.com/homedetails',
    ]
    if mls_name: q_mls = [f'{q} "{mls_name}"' for q in q_mls] + q_mls
    seen, candidates = set(), []
    for q in q_mls:
        items = bing_search_items(q)
        for it in (items or []):
            url = (it.get("url") or it.get("link") or "").strip()
            if not url or "zillow.com" not in url: continue
            if "/homedetails/" not in url and "/homes/" not in url: continue
            if url in seen: continue
            seen.add(url); candidates.append(url)
            if len(candidates) >= max_candidates: break
        if len(candidates) >= max_candidates: break
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok: return ok, mtype or "mls_match"
    return None, None

# ---------- Candidate search by address variants ----------
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

# ---------- Azure search ----------
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

# ---------- Construct rb deeplink (only if we have something) ----------
def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    c = (city or defaults.get("city","")); c = c.strip() if isinstance(c, str) else ""
    st_abbr = (state or defaults.get("state","NC")).strip() or "NC"
    z = (zipc  or defaults.get("zip","")); z = z.strip() if isinstance(z, str) else ""
    street = (street or "").strip()
    # If we have nothing meaningful, don't fabricate /homes/nc_rb/
    if not any([street, c, z]):
        return ""
    slug_parts = [street] if street else []
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

# --------- Special: Homespotter / l.hms.pt resolver helpers ----------
def _is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return ("l.hms.pt" in h) or ("idx.homespotter.com" in h) or ("homespotter" in h)
    except Exception:
        return False

def _extract_addr_homespotter(html: str) -> Dict[str, str]:
    """
    Try hard to pull address parts from Homespotter / IDX pages.
    We ignore MLS for this flow (address-based only).
    """
    # 1) JSON-LD / microdata
    addr = extract_address_from_html(html)

    # 2) Common JSON blobs used by Homespotter/IDX
    if not addr.get("street"):
        # window.__INITIAL_STATE__ / __NEXT_DATA__ / dataLayer
        for m in re.finditer(r'<script[^>]*>(.*?)</script>', html, flags=re.I|re.S):
            blob = m.group(1) or ""
            if not blob or ("{" not in blob and "[" not in blob): 
                continue
            j = None
            try:
                # Weed out non-JSON
                cand = blob.strip()
                # Heuristic slices for large blobs
                starts = [cand.find("{"), cand.find("[")]
                starts = [x for x in starts if x >= 0]
                if not starts:
                    continue
                cand = cand[min(starts):]
                # Try several closing trims
                for cut in range(0, 3):
                    try:
                        j = json.loads(cand)
                        break
                    except Exception:
                        cand = re.sub(r';\s*$', '', cand).strip()
                if not j:
                    continue
            except Exception:
                j = None

            def _dig(d: Any, keys: List[str]) -> Optional[str]:
                if isinstance(d, dict):
                    for k in keys:
                        if k in d and isinstance(d[k], (str, int)):
                            v = str(d[k]).strip()
                            if v: return v
                    for v in d.values():
                        r = _dig(v, keys)
                        if r: return r
                elif isinstance(d, list):
                    for it in d:
                        r = _dig(it, keys)
                        if r: return r
                return None

            if j:
                street = _dig(j, ["streetAddress","street","addressLine1","line1","address1"])
                city   = _dig(j, ["addressLocality","city","locality"])
                state  = _dig(j, ["addressRegion","state","stateOrProvince"])
                zipc   = _dig(j, ["postalCode","zip","zipcode"])
                if street or city:
                    addr["street"] = addr.get("street") or (street or "")
                    addr["city"]   = addr.get("city")   or (city or "")
                    # we will force NC outside, but capture state/zip if present
                    addr["state"]  = addr.get("state")  or (state or "")
                    addr["zip"]    = addr.get("zip")    or (zipc or "")
                    if addr["street"]:
                        break

    # 3) Meta title fallback
    if not addr.get("street"):
        title = extract_title_or_desc(html)
        if title and re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
            addr["street"] = title

    # Normalize dict keys
    return {
        "street": addr.get("street","").strip(),
        "city":   addr.get("city","").strip(),
        "state":  addr.get("state","").strip(),
        "zip":    addr.get("zip","").strip(),
    }

# ---------- Helper: search Zillow by ADDRESS ONLY ----------
def search_zillow_by_address_only(street: str, city: str, state: str, zipc: str, defaults: Dict[str, str]):
    query_address = compose_query_address(street, city, state, zipc, defaults)
    variants = generate_address_variants(street, city, state, zipc, defaults)
    # Try Azure first
    z = azure_search_first_zillow(query_address)
    if z and ("/homedetails/" in z or "/homes/" in z) and url_matches_city_state(z, city or None, "NC"):
        return z, query_address
    # Fall back to Bing variants
    z2, _ = resolve_homedetails_with_bing_variants(
        variants, required_state="NC", required_city=(city or None), mls_id=None, delay=0.3, require_match=True
    )
    if z2:
        return z2, query_address
    return None, query_address

# ---------- Try to pull *any* MLS id from arbitrary HTML (generic path only) ----------
def extract_any_mls_id(html: str) -> Optional[str]:
    """
    Try hard to pull an MLS id out of arbitrary HTML.
    Returns the first plausible MLS id as a string, else None.
    """
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

# ---------- Resolve from arbitrary source URL (NC forced) ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # If it's already a Zillow link, just canonicalize/upgrade
    if "zillow.com" in (final_url or ""):
        return upgrade_to_homedetails_if_needed(final_url), ""

    # Homespotter/IDX path: ADDRESS-ONLY (do not use MLS)
    if _is_homespotter_like(final_url):
        hs = _extract_addr_homespotter(html)
        street = hs.get("street","") or ""
        city   = hs.get("city","") or ""
        # Enforce NC regardless of what Homespotter says
        state  = "NC"
        zipc   = hs.get("zip","") or ""
        # If we have at least street or city, proceed
        if street or city or zipc:
            z, q = search_zillow_by_address_only(street, city, state, zipc, {"state":"NC"})
            if z:
                return z, q
            # As last resort try a deeplink only if we truly have something
            rb = construct_deeplink_from_parts(street or "", city, "NC", zipc, {"state":"NC"})
            if rb:
                return rb, q
            # If nothing, fall back to original URL
            return final_url, q
        else:
            # No address found at all
            return final_url, ""

    # Generic pages: allow MLS OR address paths
    # 1) Try MLS (works well for many brokerage pages)
    mls_id = extract_any_mls_id(html) or extract_mls_id_from_url(final_url) or extract_mls_id_from_url(source_url)
    if mls_id:
        z1, _ = find_zillow_by_mls_with_confirmation(mls_id, required_state="NC")
        if z1:
            return z1, ""

    # 2) Address extraction from page
    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = addr.get("state","") or "NC"
    zipc   = addr.get("zip","") or ""
    if street or city or zipc:
        z2, q2 = search_zillow_by_address_only(street, city, "NC", zipc, {"state":"NC"})
        if z2:
            return z2, q2
    # 3) Title → Bing
    title = extract_title_or_desc(html)
    if title:
        for q in [f'"{title}" site:zillow.com/homedetails', f'{title} site:zillow.com']:
            items = bing_search_items(q)
            for it in items or []:
                u = it.get("url") or ""
                if "/homedetails/" in u and url_matches_city_state(u, None, "NC"):
                    return u, title

    # 4) rb deeplink only if we know something; otherwise keep original URL
    if street or city or zipc or title:
        rb = construct_deeplink_from_parts(street or title or "", city, "NC", zipc, {"state":"NC"})
        if rb:
            return rb, compose_query_address(street or title or "", city, "NC", zipc, {"state":"NC"})

    # Fallback: expanded URL
    return final_url, ""

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
    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id, required_state=required_state_val, required_city=required_city_val,
            mls_name=mls_name, delay=min(delay, 0.6), require_match=True, max_candidates=max_candidates
        )
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z and url_matches_city_state(z, required_city_val, "NC"): zurl, status = z, "azure_hit"
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants, required_state="NC", required_city=required_city_val,
            mls_id=mls_id or None, delay=min(delay, 0.6), require_match=True
        )
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        # only use deeplink if it is meaningful (construct_deeplink returns "" if nothing)
        zurl = deeplink or ""
        status = "deeplink_fallback" if zurl else "fallback"
    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "status": status, "csv_photo": csv_photo}

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
    meta: Dict[str, Any] = {}
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
    log = {
        "url": zurl,
        "csv_provided": bool(csv_photo_url),
        "stage": None,
        "status_code": None,
        "html_len": None,
        "selected": None,
        "errors": []
    }
    def _ok(u:str)->bool: return isinstance(u,str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))
    if csv_photo_url and _ok(csv_photo_url):
        log["stage"]="csv_photo"; log["selected"]=csv_photo_url; return csv_photo_url, log
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); log["status_code"]=r.status_code
            if r.ok:
                html=r.text; log["html_len"]=len(html)
                zfirst=extract_zillow_first_image(html)
                if zfirst: log["stage"]="zillow_hero"; log["selected"]=zfirst; return zfirst, log
                for pat in [
                    r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"<meta[^>]+property=['\"]og:image:secure_url['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"\"image\"\s*:\s*\"(https?://[^\"]+)\"",
                    r"\"image\"\s*:\s*\[\s*\"(https?://[^\"]+)\"",
                ]:
                    m = re.search(pat, html, re.I)
                    if m: log["stage"]="og_image"; log["selected"]=m.group(1); return m.group(1), log
        except Exception as e:
            log["errors"].append(f"fetch_err:{e!r}")
    try:
        key = GOOGLE_MAPS_API_KEY
        if key and query_address:
            from urllib.parse import quote_plus
            loc = quote_plus(query_address)
            sv = f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={key}"
            log["stage"]="street_view"; log["selected"]=sv; return sv, log
        else:
            if not key: log["errors"].append("no_google_maps_key")
    except Exception as e:
        log["errors"].append(f"sv_err:{e!r}")
    log["stage"]="none"; return None, log

@st.cache_data(ttl=900, show_spinner=False)
def get_thumbnail_and_log(query_address: str, zurl: str, csv_photo_url: Optional[str]):
    return picture_for_result_with_log(query_address, zurl, csv_photo_url)

# ---------- Tracking + Bitly ----------
def make_trackable_url(url: str, client_tag: str, campaign_tag: str) -> str:
    client_tag = re.sub(r'[^a-z0-9\-]+','', (client_tag or "").lower().replace(" ","-"))
    campaign_tag = re.sub(r'[^a-z0-9\-]+','', (campaign_tag or "").lower().replace(" ","-"))
    frag = f"#aa={client_tag}.{campaign_tag}" if (client_tag or campaign_tag) else ""
    return (url or "") + (frag if url and frag else "")

def bitly_shorten(long_url: str) -> Optional[str]:
    token = BITLY_TOKEN
    if not token: return None
    try:
        r = requests.post("https://api-ssl.bit.ly/v4/shorten".replace("bit.ly","bitly.com"),
                          headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
                          json={"long_url": long_url}, timeout=10)
        if r.ok: return r.json().get("link")
    except Exception:
        return None
    return None

# ---------- Supabase sent lookups ----------
def _supabase_available():
    try: return bool(SUPABASE)
    except NameError: return False

@st.cache_data(ttl=300, show_spinner=False)
def get_already_sent_maps(client_tag: str):
    if not (_supabase_available() and client_tag.strip()):
        return set(), set(), {}, {}
    try:
        rows = SUPABASE.table("sent").select("canonical,zpid,url,sent_at").eq("client", client_tag.strip()).limit(20000).execute().data or []
        canon_set = { (r.get("canonical") or "").strip() for r in rows if r.get("canonical") }
        zpid_set  = { (r.get("zpid") or "").strip() for r in rows if r.get("zpid") }
        canon_info: Dict[str, Dict[str,str]] = {}
        zpid_info:  Dict[str, Dict[str,str]] = {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info: canon_info[c] = info
            if z and z not in zpid_info:  zpid_info[z]  = info
        return canon_set, zpid_set, canon_info, zpid_info
    except Exception:
        return set(), set(), {}, {}

# ---------- Tours cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def get_tour_slug_map(client_tag: str) -> Dict[str, Dict[str, str]]:
    if not (_supabase_available() and client_tag.strip()):
        return {}
    try:
        tours = SUPABASE.table("tours")\
            .select("id,tour_date")\
            .eq("client", client_tag.strip())\
            .order("tour_date", desc=True)\
            .limit(5000)\
            .execute().data or []
        if not tours: return {}
        ids = [t["id"] for t in tours if t.get("id")]
        stops: List[Dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i+50]
            resp = SUPABASE.table("tour_stops")\
                .select("tour_id,address,address_slug,start,end,deeplink")\
                .in_("tour_id", batch)\
                .limit(20000)\
                .execute()
            stops.extend(resp.data or [])
        tdate = {t["id"]: (t.get("tour_date") or None) for t in tours}
        by_slug: Dict[str, Dict[str,str]] = {}
        for s in stops:
            slug = (s.get("address_slug") or address_to_slug(s.get("address","")) or "").strip()
            if not slug: continue
            info = {"date": (tdate.get(s.get("tour_id")) or ""), "start": s.get("start","") or "", "end": s.get("end","") or ""}
            prev = by_slug.get(slug)
            if not prev or (info["date"] and prev.get("date") and str(info["date"]) > str(prev.get("date"))):
                by_slug = {**by_slug, slug: info}
            elif not prev:
                by_slug = {**by_slug, slug: info}
        return by_slug
    except Exception:
        return {}

# ---------- Dedupe markers ----------
def mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info):
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not url:
            r["already_sent"] = False
        else:
            canon, zpid = canonicalize_zillow(url)
            reason = None; sent_when = ""; sent_url = ""
            if canon and canon in canon_set:
                reason = "canonical"; meta = canon_info.get(canon, {})
                sent_when, sent_url = meta.get("sent_at",""), meta.get("url","")
            elif zpid and zpid in zpid_set:
                reason = "zpid"; meta = zpid_info.get(zpid, {})
                sent_when, sent_url = meta.get("sent_at",""), meta.get("url","")
            r["canonical"] = canon
            r["zpid"] = zpid
            r["already_sent"] = bool(reason)
            r["dup_reason"] = reason
            r["dup_sent_at"] = sent_when
            r["dup_original_url"] = sent_url
    return results

# ---------- Logging ----------
def log_sent_rows(results: List[Dict[str, Any]], client_tag: str, campaign_tag: str):
    if not SUPABASE or not results:
        return False, "Supabase not configured or no results."
    rows = []
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for r in results:
        raw_url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not raw_url:
            continue
        canon = r.get("canonical"); zpid = r.get("zpid")
        if not (canon and zpid):
            canon2, zpid2 = canonicalize_zillow(raw_url)
            canon = canon or canon2; zpid = zpid or zpid2
        rows.append({
            "client":     (client_tag or "").strip(),
            "campaign":   (campaign_tag or "").strip(),
            "url":        raw_url,
            "canonical":  canon,
            "zpid":       zpid,
            "mls_id":     (r.get("mls_id") or "").strip() or None,
            "address":    (r.get("input_address") or "").strip() or None,
            "sent_at":    now_iso,
        })
    if not rows: return False, "No valid rows to log."
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- URL canonical pickers ----------
def pick_canonical_url(raw: str) -> str:
    if not raw:
        return raw
    canon, _ = canonicalize_zillow(upgrade_to_homedetails_if_needed(raw))
    return canon or raw

def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    def pick_url(r):
        raw = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        return pick_canonical_url(raw)

    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft",
                  "already_sent","dup_reason","dup_sent_at","toured","toured_date","toured_start","toured_end"]
        if include_notes:
            fields += ["summary","highlights","remarks"]
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

# ---------- Batch dedupe for logging ----------
def _dedupe_results_for_logging(results: List[Dict[str,Any]]) -> List[Dict[str,Any]]:
    out, seen = [], set()
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        c, z = canonicalize_zillow(url) if url else ("","")
        key = c or z or url
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        r = dict(r)
        if c: r["canonical"] = c
        if z: r["zpid"] = z
        out.append(r)
    return out

# ---------- Results list ----------
def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
    li_html = []
    for r in results:
        href_raw = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href_raw:
            continue
        href = pick_canonical_url(href_raw)
        safe_href = escape(href)
        link_txt = href
        badge_html = ""
        if client_selected:
            if r.get("already_sent"):
                tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
                badge_html += f' <span class="badge dup" title="{tip}">Duplicate</span>'
            else:
                badge_html += ' <span class="badge new" title="New for this client">NEW</span>'
            if r.get("toured"):
                dt = str(r.get("toured_date") or "")
                tm = str(r.get("toured_start") or "")
                title = ("Toured " + (dt + (" " + tm if tm else ""))).strip()
                badge_html += (
                    ' <span class="badge tour" title="{}">TOURED</span>'
                ).format(escape(title))
        li_html.append(
            '<li style="margin:0.2rem 0;"><a href="{0}" target="_blank" rel="noopener">{1}</a>{2}</li>'.format(
                safe_href, escape(link_txt), badge_html
            )
        )
    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"
    copy_lines = []
    for r in results:
        u_raw = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u_raw:
            copy_lines.append(pick_canonical_url(u_raw).strip())
    copy_text = "\n".join(copy_lines) + ("\n" if copy_lines else "")
    html_tpl = """
    <html><head><meta charset="utf-8" />
      <style>
        html,body { margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }
        .results-wrap { position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }
        ul.link-list { margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }
        ul.link-list li { margin:0.2rem 0; }
        .copyall-btn { position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }
      </style>
    </head><body>
      <div class="results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
        <ul class="link-list" id="resultsList">__ITEMS__</ul>
      </div>
      <script>
        (function() {
          const btn = document.getElementById('copyAll');
          const text = `__COPY__`;
          btn.addEventListener('click', async () => {
            try {
              await navigator.clipboard.writeText(text);
              const prev = btn.textContent; btn.textContent='✓'; setTimeout(() => { btn.textContent = prev; }, 900);
            } catch(e) {
              const prev = btn.textContent; btn.textContent='×'; setTimeout(() => { btn.textContent = prev; }, 900);
            }
          });
        })();
      </script>
    </body></html>
    """
    html = html_tpl.replace("__ITEMS__", items_html).replace("__COPY__", copy_text.replace("\\", "\\\\").replace("`", "\\`"))
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

# ---------- make_preview_url ----------
def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ---------- Parsers for pasted input ----------
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

# ---------- Run pipeline ----------
def _process_rows(rows: List[Dict[str, Any]], *, land_mode=True, delay=0.4,
                  require_state=True, mls_first=True, default_mls_name="",
                  client_tag: str = "", campaign_tag: str = "") -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    defaults = {"state": "NC", "city": "", "zip": ""}

    for row in rows:
        src_url = _detect_source_url(row)
        if src_url:
            zurl, inferred_addr = resolve_from_source_url(src_url, defaults)
            results.append({
                "input_address": inferred_addr or row.get("address") or row.get("full_address") or "",
                "mls_id": row.get("mls_id", ""),
                "zillow_url": zurl,
                "status": "source_url",
                "preview_url": zurl,
                "display_url": zurl,
                "csv_photo": get_first_by_keys(row, PHOTO_KEYS),
            })
        else:
            results.append(process_single_row(
                row,
                delay=delay,
                land_mode=land_mode,
                defaults=defaults,
                require_state=require_state,
                mls_first=mls_first,
                default_mls_name=default_mls_name
            ))

    # Enrich (price, beds, hero image, etc.)
    try:
        results = asyncio.run(enrich_results_async(results))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(enrich_results_async(results))
        loop.close()

    # Client duplicate/tour markers
    client_selected = bool(client_tag and client_tag.strip())
    if client_selected:
        canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(client_tag)
        results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info)

        tour_map = get_tour_slug_map(client_tag)
        for r in results:
            slug = result_to_slug(r)
            if slug and slug in tour_map:
                info = tour_map[slug]
                r["toured"] = True
                r["toured_date"] = info.get("date") or ""
                r["toured_start"] = info.get("start") or ""
                r["toured_end"] = info.get("end") or ""
            else:
                r["toured"] = False

    # Add trackable + short if campaign provided
    if campaign_tag:
        for r in results:
            u = r.get("zillow_url") or ""
            if not u:
                continue
            tracked = make_trackable_url(u, client_tag, campaign_tag)
            short = bitly_shorten(tracked) or tracked
            r["display_url"] = short
            r["preview_url"] = tracked
        if client_selected:
            canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(client_tag)
            results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info)

    return results

def _thumb_cell(r: Dict[str, Any]) -> str:
    u = (r.get("preview_url") or r.get("zillow_url") or "").strip()
    if not u:
        return ""
    img, _log = get_thumbnail_and_log(r.get("input_address", ""), u, r.get("csv_photo"))
    img = img or ""
    safe_u = escape(pick_canonical_url(u))
    if img:
        safe_img = escape(img)
        return f'<a href="{safe_u}" target="_blank" rel="noopener"><img src="{safe_img}" alt="thumbnail" style="width:100%;height:auto;border-radius:12px"/></a>'
    return f'<a href="{safe_u}" target="_blank" rel="noopener">{escape(safe_u)}</a>'

def _thumbnails_grid(results: List[Dict[str, Any]], ncols: int = 3):
    if not results:
        st.info("No results.")
        return
    cols = st.columns(ncols)
    for i, r in enumerate(results):
        html = _thumb_cell(r)
        with cols[i % ncols]:
            st.markdown(
                f"""
                <div style="border:1px solid rgba(0,0,0,.08);border-radius:14px;padding:8px;margin-bottom:10px">
                    {html}
                    <div style="font-size:12px;opacity:.8;margin-top:6px">
                        {escape(r.get('summary') or '')}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

# ---------- Main renderer ----------
def render_run_tab(state: dict):
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    st.header("Run")

    # -- Client & campaign
    clients = []
    try:
        clients = fetch_clients(include_inactive=False) or []
    except Exception:
        clients = []
    client_names = [c.get("name", "") for c in clients if c.get("name")]
    options = [NO_CLIENT] + client_names + [ADD_SENTINEL]

    with st.container():
        c1, c2, c3 = st.columns([2, 1, 1])

        with c1:
            chosen = st.selectbox("Client", options, index=0)
        with c2:
            campaign = st.text_input("Campaign tag (optional)", value=state.get("campaign", ""))
        with c3:
            land_mode = st.checkbox("Land mode", value=True)

        if chosen == ADD_SENTINEL:
            new_name = st.text_input("New client name")
            if st.button("Create client", type="primary", use_container_width=False, help="Add new active client"):
                ok, msg = upsert_client(new_name.strip(), active=True)
                if ok:
                    st.success(f"Added “{new_name}”.")
                    _safe_rerun()
                else:
                    st.error(msg or "Could not add client.")
            return  # wait for rerun

    client_tag = "" if chosen == NO_CLIENT else chosen
    if client_tag:
        st.caption(f"Logging and duplicate detection enabled for **{client_tag}**.")

    # -- Paste area & options
    st.subheader("Paste rows")
    st.caption("Paste CSV with address fields or URLs, **or** paste one URL/address per line.")
    paste = st.text_area("Input", height=180, placeholder="e.g. 407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524")

    with st.expander("Advanced"):
        delay = st.slider("Politeness delay (seconds)", min_value=0.0, max_value=1.0, value=0.35, step=0.05)
        require_state = st.checkbox("Require NC state match", value=True)
        mls_first = st.checkbox("Try MLS lookup first (generic sources only — Homespotter uses address)", value=True)
        default_mls_name = st.text_input("Default MLS name to bias search (optional)", value="")
        include_notes = st.checkbox("Include notes (summary/highlights/remarks) when exporting CSV", value=False)

    run_col, export_col = st.columns([1, 2])
    with run_col:
        run_btn = st.button("🚀 Run", type="primary", use_container_width=True)
    results: List[Dict[str, Any]] = []

    if run_btn:
        # Build rows from paste (also supports raw URLs and addresses)
        rows_in: List[Dict[str, Any]] = _rows_from_paste(paste)

        # If usaddress is available, normalize freeform address rows a bit
        if usaddress:
            normalized = []
            for row in rows_in:
                if "address" in row and row.get("address"):
                    try:
                        parts = usaddress.tag(row["address"])[0]
                        norm = (parts.get("AddressNumber","") + " " +
                                " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())
                        cityst = ((", " + parts.get("PlaceName","") + ", " + parts.get("StateName","") +
                                   (" " + parts.get("ZipCode","") if parts.get("ZipCode") else "")) if (parts.get("PlaceName") or parts.get("StateName")) else "")
                        row["address"] = re.sub(r"\s+"," ", (norm + cityst).strip())
                    except Exception:
                        pass
                normalized.append(row)
            rows_in = normalized

        if not rows_in:
            st.warning("Nothing to process.")
            return

        with st.status("Resolving properties…", expanded=True) as st_status:
            st.write(f"Parsed **{len(rows_in)}** row(s). Searching Zillow…")
            results = _process_rows(
                rows_in,
                land_mode=land_mode,
                delay=delay,
                require_state=require_state,
                mls_first=mls_first,
                default_mls_name=default_mls_name,
                client_tag=client_tag,
                campaign_tag=campaign,
            )
            st_status.update(label="Done", state="complete")

        st.subheader("Results")
        # Copyable list
        results_list_with_copy_all(results, client_selected=bool(client_tag))
        # Thumbs
        _thumbnails_grid(results, ncols=3)

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

        # Post-run: add to client (log "sent")
        if client_tag:
            deduped = _dedupe_results_for_logging(results)
            if st.button(f"Add {len(deduped)} to client log", type="secondary"):
                ok, msg = log_sent_rows(deduped, client_tag=client_tag, campaign_tag=campaign or "")
                if ok:
                    st.success("Logged.")
                else:
                    st.error(msg or "Failed to log.")

        st.divider()

    # ---------- Fix properties ----------
    st.subheader("Fix properties")
    st.caption("Paste any Zillow links (or /homes/*_rb/ deeplinks). I’ll output clean canonical **/homedetails/** URLs.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                best = upgrade_to_homedetails_if_needed(best) or best
                if "/homedetails/" not in (best or ""):
                    # Try to resolve from source (handles Homespotter too)
                    z, _addr = resolve_from_source_url(best, {"state":"NC"})
                    best = z or best
                    if best:
                        best = upgrade_to_homedetails_if_needed(best)
            except Exception:
                pass
            fixed.append(pick_canonical_url(best))
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")

# (optional) Allow running this module directly for local testing:
if __name__ == "__main__":
    # minimal shim so you can `streamlit run ui/run_tab.py`
    render_run_tab({})
