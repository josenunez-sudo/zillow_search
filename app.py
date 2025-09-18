# Address Alchemist — paste addresses AND arbitrary listing links → Zillow
# Preview-first sharing + optional tracking + always log sent_at
# Clients tab: always show lists; inline client report (addresses as hyperlinks).

import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

# ---------- Optional deps ----------
try:
    import pillow_avif  # noqa: F401
except Exception:
    pass
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
from supabase import create_client, Client

@st.cache_resource
def get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = get_supabase()

# ---------- Page & global styles ----------
def _page_icon_from_avif(path: str):
    if not os.path.exists(path):
        return "⚗️"
    try:
        im = Image.open(path); im.load()
        if im.mode not in ("RGB", "RGBA"): im = im.convert("RGBA")
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return "⚗️"

st.set_page_config(
    page_title="Address Alchemist",
    page_icon=_page_icon_from_avif("/mnt/data/link.avif"),
    layout="centered",
)

# Base styles (apply to Streamlit page)
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;800&display=swap');
.block-container { max-width: 980px; }
.app-title { font-family: 'Barlow Condensed', system-ui; font-weight: 800; font-size: 2.1rem; margin: 0 0 8px; }
.app-sub { color:#6b7280; margin:0 0 12px; }
.center-box { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:16px; }
.small { color:#6b7280; font-size:12.5px; margin-top:6px; }
ul.link-list { margin:0 0 .5rem 1.2rem; padding:0; }
textarea { border-radius:10px !important; }
textarea:focus { outline:3px solid #93c5fd !important; outline-offset:2px; }
[data-testid="stFileUploadClearButton"] { display:none !important; }
.detail { font-size:14.5px; margin:8px 0 0 0; line-height:1.35; }
.hl { display:inline-block; background:#f1f5f9; border-radius:8px; padding:2px 6px; margin-right:6px; font-size:12px; }
.badge { display:inline-block; font-size:12px; font-weight:700; padding:2px 8px; border-radius:999px; margin-left:8px; background:#dcfce7; color:#166534; }
.badge.dup { background:#fee2e2; color:#991b1b; }

/* Theme variables */
:root {
  --text-strong: #0f172a;
  --text-muted:  #475569;
  --chip-active-bg:  #dcfce7; --chip-active-fg:#166534;
  --chip-inactive-bg:#fee2e2; --chip-inactive-fg:#991b1b;
  --row-border: #e2e8f0;
  --row-hover:  #f8fafc;
  --tooltip-bg:#0b1220; --tooltip-fg:#f8fafc;
}
html[data-theme="dark"], .stApp [data-theme="dark"] {
  --text-strong: #f8fafc;
  --text-muted:  #cbd5e1;
  --chip-active-bg:  #064e3b; --chip-active-fg:#a7f3d0;
  --chip-inactive-bg:#7f1d1d; --chip-inactive-fg:#fecaca;
  --row-border: #0b1220;
  --row-hover:  #0f172a;
  --tooltip-bg:#0b1220; --tooltip-fg:#f8fafc;
}

/* Section heading */
.clients-h3 { color: var(--text-muted); font-weight: 700; margin: 8px 0 6px; }

/* For any non-iframe client rows you might render in the future */
.client-row { padding: 10px 8px; border-bottom: 1px solid var(--row-border); display:flex; align-items:center; justify-content:space-between; }
.client-main { display:flex; align-items:center; gap:8px; min-width:0; }
.client-name { color: var(--text-strong); font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.client-status { font-size:11px; padding:1px 6px; border-radius:999px; background:#e2e8f0; color:var(--text-strong); }
.client-status.active   { background: var(--chip-active-bg);   color: var(--chip-active-fg); }
.client-status.inactive { background: var(--chip-inactive-bg); color: var(--chip-inactive-fg); }

/* Tiny inline icon buttons (no text) */
.action-bar { display:inline-flex; gap:4px; align-items:center; }
.icon-btn {
  border:0; background:transparent; padding:2px 6px; border-radius:6px;
  font-size:12px; line-height:1; cursor:pointer; color:#64748b;
  text-decoration:none; display:inline-flex; align-items:center; justify-content:center;
}
.icon-btn:hover { background:var(--row-hover); color:var(--text-strong); }

/* Tooltips for icon buttons */
.icon-btn[data-tip] { position:relative; }
.icon-btn[data-tip]:hover::after {
  content: attr(data-tip);
  position:absolute; top:-28px; right:0;
  background:var(--tooltip-bg); color:var(--tooltip-fg);
  font-size:10px; font-weight:700;
  padding:4px 6px; border-radius:6px; white-space:nowrap;
  box-shadow:0 6px 18px rgba(0,0,0,.18);
  pointer-events:none;
}
.icon-btn[data-tip]:hover::before {
  content:""; position:absolute; top:-6px; right:8px;
  border:5px solid transparent; border-top-color:var(--tooltip-bg);
}

/* Danger hover color for delete */
.icon-btn.danger:hover { background:#fee2e2; color:#991b1b; }
</style>
""", unsafe_allow_html=True)

# Extra theme-safe overrides for legibility
st.markdown("""
<style>
:root {
  --aa-text-strong: #0b1220;
  --aa-text-muted:  #475569;
  --aa-chip-active-bg:  #dcfce7; --aa-chip-active-fg:#166534;
  --aa-chip-inactive-bg:#fee2e2; --aa-chip-inactive-fg:#991b1b;
}
html[data-theme="dark"], .stApp [data-theme="dark"] {
  --aa-text-strong: #f8fafc;
  --aa-text-muted:  #cbd5e1;
  --aa-chip-active-bg:  #064e3b; --aa-chip-active-fg:#a7f3d0;
  --aa-chip-inactive-bg: #7f1d1d; --aa-chip-inactive-fg:#fecaca;
}
@media (prefers-color-scheme: dark) {
  :root { --aa-text-strong: #f8fafc; --aa-text-muted: #cbd5e1; }
}
.client-name { color: var(--aa-text-strong) !important; font-weight: 700; }
.client-status { color: var(--aa-text-strong) !important; font-size:11px !important; }
</style>
""", unsafe_allow_html=True)

# ---------- Debug toggle ----------
def _get_debug_mode() -> bool:
    try:
        qp = st.query_params if hasattr(st, "query_params") else st.experimental_get_query_params()
        raw = qp.get("debug", "")
        val = raw[0] if isinstance(raw, list) and raw else raw
        token = (str(val) or os.getenv("AA_DEBUG", ""))
    except Exception:
        token = os.getenv("AA_DEBUG", "")
    return str(token).lower() in ("1","true","yes","on")
DEBUG_MODE = _get_debug_mode()

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

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

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

def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url: return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok: return url
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', r.text)
        return m.group(1) if m else url
    except Exception:
        return url

# Content extractors
def extract_any_mls_id(html: str) -> Optional[str]:
    if not html: return None
    for pat in [r'"mlsId"\s*:\s*"([A-Za-z0-9\-]{5,})"',
                r'"mls"\s*:\s*"([A-Za-z0-9\-]{5,})"',
                r'"listingId"\s*:\s*"([A-Za-z0-9\-]{5,})"']:
        m = re.search(pat, html, re.I)
        if m: return m.group(1)
    m = re.search(r'\bMLS[^A-Za-z0-9]{0,5}#?\s*([A-Za-z0-9\-]{5,})\b', html, re.I)
    return m.group(1) if m else None

def extract_address_from_html(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html: return out
    m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I); out["street"] = m.group(1) if m else ""
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I); out["city"] = m.group(1) if m else ""
    m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I); out["state"] = m.group(1) if m else ""
    m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I); out["zip"] = m.group(1) if m else ""
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

# Zillow canonicalization
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)
def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base

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
        tokens = tokens[1:]; s = " ".join(tokens)
    s_lower = f" {s.lower()} "
    for pat,repl in HWY_EXPAND.items(): s_lower = re.sub(pat, f" {repl} ", s_lower)
    s = re.sub(r"\s+", " ", s_lower).strip()
    s = re.sub(r"[^\w\s/-]", "", s)
    return s

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

# Search (Bing/Azure)
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
    if state and re.search(rf'\b{re.escape(state)}\b', html, re.I): ok = True
    return ok

def confirm_or_resolve_on_page(url:str, mls_id:str=None, required_city:str=None, required_state:str=None):
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

def find_zillow_by_mls_with_confirmation(mls_id, required_state=None, required_city=None, mls_name=None, delay=0.35, require_match=False, max_candidates=20):
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
        for it in items:
            url = it.get("url") or it.get("link") or ""
            if not url or "zillow.com" not in url: continue
            if "/homedetails/" not in url and "/homes/" not in url: continue
            if require_match and not url_matches_city_state(url, required_city, required_state): continue
            if url in seen: continue
            seen.add(url); candidates.append(url)
            if len(candidates) >= max_candidates: break
        if len(candidates) >= max_candidates: break
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok: return ok, mtype or "mls_match"
    return None, None

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

def resolve_homedetails_with_bing_variants(address_variants, required_state=None, required_city=None, mls_id=None, delay=0.3, require_match=False):
    if not BING_API_KEY: return None, None
    candidates, seen = [], set()
    for qaddr in address_variants:
        queries = [
            f'{qaddr} site:zillow.com/homedetails',
            f'"{qaddr}" site:zillow.com/homedetails',
            f'{qaddr} land site:zillow.com/homedetails',
            f'{qaddr} lot site:zillow.com/homedetails',
        ]
        if mls_id:
            queries = [
                f'"MLS# {mls_id}" site:zillow.com/homedetails',
                f'{mls_id} site:zillow.com/homedetails',
                f'"{mls_id}" "MLS" site:zillow.com/homedetails',
            ] + queries
        for q in queries:
            items = bing_search_items(q)
            for it in items:
                url = it.get("url") or it.get("link") or ""
                if not url or "zillow.com" not in url: continue
                if "/homedetails/" not in url and "/homes/" not in url: continue
                if require_match and not url_matches_city_state(url, required_city, required_state): continue
                if url in seen: continue
                seen.add(url); candidates.append(url)
            time.sleep(delay)
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok: return ok, mtype or "city_state_match"
    return None, None

def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    c = (city or defaults.get("city","")).strip()
    st_abbr = (state or defaults.get("state","")).strip()
    z = (zipc  or defaults.get("zip","")).strip()
    slug_parts = [street]; loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts: slug_parts.append(", ".join(loc_parts))
    if z:
        if slug_parts: slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else: slug_parts.append(z)
    slug = ", ".join(slug_parts)
    a = slug.lower(); a = re.sub(r"[^\w\s,-]", "", a).replace(",", ""); a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# Resolve from arbitrary source URL
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    mls_id = extract_any_mls_id(html)
    if mls_id:
        z1, _ = find_zillow_by_mls_with_confirmation(mls_id)
        if z1: return z1, ""
    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city, state, zipc = addr.get("city",""), addr.get("state",""), addr.get("zip","")
    if street or (city and state):
        variants = generate_address_variants(street or "", city, state, zipc, defaults)
        z2, _ = resolve_homedetails_with_bing_variants(variants, required_state=state or None, required_city=city or None)
        if z2: return z2, compose_query_address(street, city, state, zipc, defaults)
    title = extract_title_or_desc(html)
    if title:
        for q in [f'"{title}" site:zillow.com/homedetails', f'{title} site:zillow.com']:
            items = bing_search_items(q)
            for it in items:
                u = it.get("url") or ""
                if "/homedetails/" in u: return u, title
    if city or state or street:
        return construct_deeplink_from_parts(street or title or "", city, state, zipc, defaults), compose_query_address(street or title or "", city, state, zipc, defaults)
    return final_url, ""

# Primary resolver
def process_single_row(row, *, delay=0.5, land_mode=True, defaults=None,
                       require_state=True, mls_first=True, default_mls_name="", max_candidates=20):
    defaults = defaults or {"city":"", "state":"", "zip":""}
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)
    comp = extract_components(row)
    street_raw = comp["street_raw"]
    street_clean = clean_land_street(street_raw) if land_mode else street_raw
    variants = generate_address_variants(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    if land_mode:
        variants = list(dict.fromkeys(variants + generate_address_variants(street_clean, comp["city"], comp["state"], comp["zip"], defaults)))
    query_address = variants[0] if variants else compose_query_address(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    deeplink = construct_deeplink_from_parts(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    required_state_val = defaults.get("state") if require_state else None
    required_city_val  = comp["city"] or defaults.get("city")
    zurl, status = None, "fallback"
    mls_id   = (comp.get("mls_id") or "").strip()
    mls_name = (comp.get("mls_name") or default_mls_name or "").strip()
    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id, required_state=required_state_val, required_city=required_city_val,
            mls_name=mls_name, delay=min(delay, 0.6), require_match=require_state, max_candidates=max_candidates
        )
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z: zurl, status = z, "azure_hit"
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants, required_state=required_state_val, required_city=required_city_val,
            mls_id=mls_id or None, delay=min(delay, 0.6), require_match=require_state
        )
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        zurl, status = deeplink, "deeplink_fallback"
    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "status": status, "csv_photo": csv_photo}

# Enrichment (regex strings fixed)
RE_PRICE  = re.compile(r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS   = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS  = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT   = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)
RE_DESC   = re.compile(r'"(?:description|homeDescription|marketingDescription)"\s*:\s*"([^"]+)"', re.I)

KEY_HL = [("new roof","roof"),("hvac","hvac"),("ac unit","ac"),("furnace","furnace"),("water heater","water heater"),
          ("renovated","renovated"),("updated","updated"),("remodeled","remodeled"),("open floor plan","open plan"),
          ("cul-de-sac","cul-de-sac"),("pool","pool"),("fenced","fenced"),("acre","acre"),("hoa","hoa"),
          ("primary on main","primary on main"),("finished basement","finished basement")]
def _tidy_txt(s: str) -> str: return re.sub(r'\s+', ' ', (s or '')).strip()
def summarize_remarks(text: str, max_sent: int = 2) -> str:
    text = _tidy_txt(text)
    if not text: return ""
    sents = re.split(r'(?<=[\.\!\?])\s+', text)
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
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(?:jpg|webp))", html, re.I)
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
    log = {"url": zurl, "csv_provided": bool(csv_photo_url), "stage": None, "status_code": None, "html_len": None, "selected": None, "errors": []}
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
        r = requests.post("https://api-ssl.bitly.com/v4/shorten",
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
    """
    Returns:
      canon_set, zpid_set, canon_info, zpid_info
    where *_info map -> {"sent_at": "...", "url": "..."} for tooltip context.
    """
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

def mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info):
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not url:
            r["already_sent"] = False; continue
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

# ---------- Clients registry helpers (cached) ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def upsert_client(name: str, active: bool = True, notes: str = None):
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        name_norm = _norm_tag(name)
        payload = {"name": name.strip(), "name_norm": name_norm, "active": active}
        if notes is not None: payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---- Query-params helpers ----
def _qp_get(name, default=None):
    try:
        qp = st.query_params
        val = qp.get(name, default)
        if isinstance(val, list) and val:
            return val[0]
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        return (qp.get(name, [default]) or [default])[0]

def _qp_set(**kwargs):
    try:
        st.query_params.update(kwargs)
    except Exception:
        st.experimental_set_query_params(**kwargs)

act = _qp_get("act", "")
cid = _qp_get("id", "")
arg = _qp_get("arg", "")
report_norm = _qp_get("report", "")

if act and cid:
    try:
        cid_int = int(cid)
    except Exception:
        cid_int = 0

    if cid_int:
        if act == "toggle":
            rows = SUPABASE.table("clients").select("active").eq("id", cid_int).limit(1).execute().data or []
            cur = rows[0]["active"] if rows else True
            toggle_client_active(cid_int, (not cur))
        elif act == "rename" and arg:
            rename_client(cid_int, arg)
        elif act == "delete":
            delete_client(cid_int)
    _qp_set()  # clear params
    _safe_rerun()

# ---------- Output builders ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft","already_sent","dup_reason","dup_sent_at"]
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

# ---------- Header ----------
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

# Tabs
tab_run, tab_clients = st.tabs(["Run", "Clients"])

# ---------- RUN TAB ----------
with tab_run:
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    colC, colK = st.columns([1.2, 1])
    with colC:
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]
        options = [NO_CLIENT] + names + [ADD_SENTINEL]
        sel_idx = st.selectbox("Client", list(range(len(options))), format_func=lambda i: options[i], index=0)
        selected_client = None if sel_idx in (0, len(options)-1) else active_clients[sel_idx-1]

        if options[sel_idx] == ADD_SENTINEL:
            new_cli = st.text_input("New client name", key="__add_client_name__")
            if st.button("Add client", use_container_width=True, key="__add_client_btn__"):
                ok, msg = upsert_client(new_cli, active=True)
                if ok:
                    st.success("Client added.")
                    _safe_rerun()
                else:
                    st.error(f"Add failed: {msg}")

        client_tag_raw = (selected_client["name"] if selected_client else "")
    with colK:
        campaign_tag_raw = st.text_input("Campaign tag", value=datetime.utcnow().strftime("%Y%m%d"))

    c1, c2, c3, c4 = st.columns([1,1,1.25,1.45])
    with c1:
        use_shortlinks = st.checkbox("Use short links (Bitly)", value=False, help="Optional tracking; sharing uses clean Zillow links.")
    with c2:
        enrich_details = st.checkbox("Enrich details", value=True)
    with c3:
        show_details = st.checkbox("Show details under results", value=False)
    with c4:
        only_show_new = st.checkbox(
            "Only show NEW for this client",
            value=bool(selected_client),
            help="Hide duplicates. Disabled when 'No client' is selected."
        )
        if not selected_client:
            only_show_new = False

    table_view = st.checkbox("Show results as table", value=True, help="Easier to scan details")

    client_tag = _norm_tag(client_tag_raw)
    campaign_tag = _norm_tag(campaign_tag_raw)

    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area("Paste addresses or links", placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524", height=160, label_visibility="collapsed")
    opt1, opt2, opt3 = st.columns([1.15, 1, 1.2])
    with opt1:
        remove_dupes = st.checkbox("Remove duplicates (pasted)", value=True)
    with opt2:
        trim_spaces = st.checkbox("Auto-trim (pasted)", value=True)
    with opt3:
        show_preview = st.checkbox("Show preview (pasted)", value=True)

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")
    st.markdown('</div>', unsafe_allow_html=True)

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean = []
    for ln in lines_raw:
        ln = ln.strip() if trim_spaces else ln
        if not ln: continue
        if remove_dupes and ln in lines_clean: continue
        if is_probable_url(ln):
            lines_clean.append(ln)
        else:
            if usaddress:
                parts = usaddress.tag(ln)[0]
                norm = (parts.get("AddressNumber","") + " " +
                        " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())
                cityst = ((", " + parts.get("PlaceName","") + ", " + parts.get("StateName","") +
                           (" " + parts.get("ZipCode","") if parts.get("ZipCode") else "")) if (parts.get("PlaceName") or parts.get("StateName")) else "")
                lines_clean.append(re.sub(r"\s+"," ", (norm + cityst).strip()))
            else:
                lines_clean.append(ln)

    count_pasted = len(lines_clean)
    csv_count = 0
    if file is not None:
        try:
            content_peek = file.getvalue().decode("utf-8-sig")
            csv_reader = csv.DictReader(io.StringIO(content_peek))
            csv_count = sum(1 for _ in csv_reader)
        except Exception:
            csv_count = 0

    bits = [f"**{count_pasted}** pasted"]
    if file is not None: bits.append(f"**{csv_count}** CSV")
    st.caption(" • ".join(bits) + "  •  Paste short links or MLS pages too; we’ll resolve them to Zillow.")

    if show_preview and count_pasted:
        st.markdown("**Preview (pasted)** (first 5):")
        st.markdown("<ul class='link-list'>" + "\n".join([f"<li>{escape(p)}</li>" for p in lines_clean[:5]]) + ("<li>…</li>" if count_pasted > 5 else "") + "</ul>", unsafe_allow_html=True)

    clicked = st.button("Run", use_container_width=True)

    # Results HTML list with copy-all (ALWAYS preview links for best unfurl)
    def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
        li_html = []
        for r in results:
            href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
            if not href: continue
            safe_href = escape(href)

            badge_html = ""
            if client_selected:
                if r.get("already_sent"):
                    tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
                    badge_html = f' <span class="badge dup" title="{tip}">Duplicate</span>'
                else:
                    badge_html = ' <span class="badge" title="New for this client">New</span>'

            detail_html = ""
            if show_details:
                status = r.get("status") or "-"
                price  = ("$" + r["price"]) if r.get("price") else "-"
                bb     = f"{r.get('beds','-')}/{r.get('baths','-')}"
                sqft   = r.get("sqft") or "-"
                detail_html = (f"<div class='detail'><b>Status:</b> {escape(status)} • "
                               f"<b>Price:</b> {escape(price)} • "
                               f"<b>Beds/Baths:</b> {escape(str(bb))} • "
                               f"<b>SqFt:</b> {escape(str(sqft))}</div>")
                if r.get("summary") or r.get("highlights"):
                    hlt = " ".join([f"<span class='hl'>{escape(h)}</span>" for h in (r.get("highlights") or [])])
                    detail_html += f"<div class='detail'>{escape(r.get('summary') or '')} {hlt}</div>"

            li_html.append(f'<li><a href="{safe_href}" target="_blank" rel="noopener">{safe_href}</a>{badge_html}{detail_html}</li>')

        items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

        copy_lines = []
        for r in results:
            u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
            if u: copy_lines.append(u.strip())
        copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

        html = f"""
        <html><head><meta charset="utf-8" />
          <style>
            html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
            .results-wrap {{ position:relative; box-sizing:border-box; padding:12px 132px 8px 0; }}
            ul.link-list {{ margin:0 0 .5rem 1.2rem; padding:0; list-style:disc; }}
            ul.link-list li {{ margin:0.45rem 0; }}
            .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:8px 12px; height:28px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:0; transform:translateY(-2px); transition:opacity .18s ease, transform .06s ease; }}
            .results-wrap:hover .copyall-btn {{ opacity:1; transform:translateY(0); }}
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
        est_h = max(120, min(52 * max(1, len(li_html)) + (60 if show_details else 24), 1400))
        components.html(html, height=est_h, scrolling=False)

    def _render_results_and_downloads(results: List[Dict[str, Any]], client_tag: str, campaign_tag: str, include_notes: bool, client_selected: bool):
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=client_selected)

        if table_view:
            import pandas as pd
            cols = ["already_sent","dup_reason","dup_sent_at","display_url","zillow_url","preview_url","status","price","beds","baths","sqft","mls_id","input_address"]
            df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
            st.dataframe(df, use_container_width=True, hide_index=True)

        fmt_options = ["txt","csv","md","html"]
        prev_fmt = (st.session_state.get("__results__") or {}).get("fmt")
        default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
        fmt = st.selectbox("Download format", fmt_options, index=default_idx)
        payload, mime = build_output(results, fmt, use_display=True, include_notes=include_notes)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        tag = ("_" + re.sub(r'[^a-z0-9\-]+','', (client_tag or "").lower().replace(" ","-"))) if client_tag else ""
        st.download_button("Export", data=payload, file_name=f"address_alchemist{tag}_{ts}.{fmt}", mime=mime, use_container_width=True)
        st.session_state["__results__"] = {"results": results, "fmt": fmt}

        thumbs=[]
        for r in results:
            img = r.get("image_url")
            if not img:
                img, _ = get_thumbnail_and_log(r.get("input_address",""), r.get("preview_url") or r.get("zillow_url") or "", r.get("csv_photo"))
            if img: thumbs.append((r,img))
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i,(r,img) in enumerate(thumbs):
                with cols[i%3]:
                    st.image(img, use_container_width=True)
                    mls_id = (r.get("mls_id") or "").strip()
                    addr = (r.get("input_address") or "").strip()
                    url = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "#"
                    mls_html = f"<strong>MLS#: {escape(mls_id)}</strong>" if mls_id else ""
                    link_text = escape(addr) if addr else "View listing"
                    st.markdown(
                        f"<div class='img-label'>{mls_html}<br/><a href='{escape(url)}' target='_blank' rel='noopener'>{link_text}</a></div>",
                        unsafe_allow_html=True
                    )

    if clicked:
        try:
            rows_in: List[Dict[str, Any]] = []
            csv_rows_count = 0
            if file is not None:
                content = file.getvalue().decode("utf-8-sig")
                reader = list(csv.DictReader(io.StringIO(content)))
                csv_rows_count = len(reader)
                rows_in.extend(reader)
            for item in lines_clean:
                if is_probable_url(item):
                    rows_in.append({"source_url": item})
                else:
                    rows_in.append({"address": item})

            if not rows_in:
                st.error("Please paste at least one address or link and/or upload a CSV.")
                st.stop()

            defaults = {"city":"", "state":"", "zip":""}
            total = len(rows_in)
            results: List[Dict[str, Any]] = []

            prog = st.progress(0, text="Resolving to Zillow…")
            for i, row in enumerate(rows_in, start=1):
                url_in = ""
                url_in = url_in or get_first_by_keys(row, URL_KEYS)
                url_in = url_in or row.get("source_url","")
                if url_in and is_probable_url(url_in):
                    zurl, used_addr = resolve_from_source_url(url_in, defaults)
                    results.append({
                        "input_address": used_addr or row.get("address","") or "",
                        "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
                        "zillow_url": zurl,
                        "status": "",
                        "csv_photo": get_first_by_keys(row, PHOTO_KEYS)
                    })
                else:
                    res = process_single_row(row, delay=0.45, land_mode=True, defaults=defaults,
                                             require_state=True, mls_first=True, default_mls_name="", max_candidates=20)
                    results.append(res)
                prog.progress(i/total, text=f"Resolved {i}/{total}")
            prog.progress(1.0, text="Links resolved")

            for r in results:
                for key in ("zillow_url","display_url"):
                    if r.get(key):
                        r[key] = upgrade_to_homedetails_if_needed(r[key])

            if enrich_details:
                st.write("Enriching details (parallel)…")
                results = asyncio.run(enrich_results_async(results))

            for r in results:
                base = r.get("zillow_url")
                r["preview_url"] = make_preview_url(base) if base else ""
                display = make_trackable_url(base, client_tag, campaign_tag) if base else base
                if use_shortlinks and display:
                    short = bitly_shorten(display)
                    r["display_url"] = short or display
                else:
                    r["display_url"] = display or base

            client_selected = bool(client_tag.strip())
            if client_selected:
                canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(client_tag)
                results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info)
                if only_show_new:
                    results = [r for r in results if not r.get("already_sent")]
                if SUPABASE and results:
                    ok_log, info_log = log_sent_rows(results, client_tag, campaign_tag)
                    st.success("Logged to Supabase.") if ok_log else st.warning(f"Supabase log skipped/failed: {info_log}")
            else:
                for r in results:
                    r["already_sent"] = False

            st.success(f"Processed {len(results)} item(s)" + (f" — CSV rows read: {csv_count}" if file is not None else ""))

            _render_results_and_downloads(results, client_tag, campaign_tag, include_notes=enrich_details, client_selected=client_selected)

        except Exception as e:
            st.error("We hit an error while processing.")
            with st.expander("Details"): st.exception(e)

    data = st.session_state.get("__results__") or {}
    results = data.get("results") or []
    if results and not clicked:
        _render_results_and_downloads(results, client_tag, campaign_tag, include_notes=False, client_selected=bool(client_tag.strip()))
    else:
        if not clicked:
            st.info("Paste addresses or links (or upload CSV), then click **Run**.")

# ---------- Sent reports ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    """
    Fetch sent rows for a given normalized client name from Supabase.
    Returns list of dicts: [{url, address, sent_at, campaign, mls_id, canonical, zpid}, ...]
    """
    if not (_supabase_available() and client_norm.strip()):
        return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = SUPABASE.table("sent")\
            .select(cols)\
            .eq("client", client_norm.strip())\
            .order("sent_at", desc=True)\
            .limit(limit)\
            .execute()
        return resp.data or []
    except Exception:
        return []

def _render_client_report_view(client_display_name: str, client_norm: str, *, show_header: bool = True):
    """Render a report: address as hyperlink → Zillow, with Campaign filter and Search box.
       If show_header=False, suppress the title line."""
    if show_header:
        st.markdown(f"### Report: {escape(client_display_name)}", unsafe_allow_html=True)

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # Campaign list (preserve first-seen order)
    seen = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen:
            seen.append(c)
    campaign_labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen]
    campaign_keys   = [None] + seen

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign", list(range(len(campaign_labels))),
                               format_func=lambda i: campaign_labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = campaign_keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = q.strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    def _match(row) -> bool:
        if sel_campaign is not None:
            if (row.get("campaign") or "").strip() != sel_campaign:
                return False
        if not q_norm:
            return True
        addr = (row.get("address") or "").lower()
        mls  = (row.get("mls_id") or "").lower()
        url  = (row.get("url") or "").lower()
        return (q_norm in addr) or (q_norm in mls) or (q_norm in url)

    rows_f = [r for r in rows if _match(r)]
    count = len(rows_f)

    st.caption(f"{count} matching listing{'s' if count!=1 else ''}")

    if not rows_f:
        st.info("No results match the current filters.")
        return

    items_html = []
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or "View on Zillow"
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()
        chip = ""
        if sel_campaign is None and camp:
            chip = f"<span style='font-size:11px; font-weight:700; padding:2px 6px; border-radius:999px; background:#e2e8f0; margin-left:6px;'>{escape(camp)}</span>"
        items_html.append(
            f"""<li>
                  <a href="{escape(url)}" target="_blank" rel="noopener">{escape(addr)}</a>
                  <span style="color:#64748b; font-size:12px; margin-left:6px;">{escape(sent_at)}</span>
                  {chip}
                </li>"""
        )
    html = "<ul class='link-list'>" + "\n".join(items_html) + "</ul>"
    st.markdown(html, unsafe_allow_html=True)

    with st.expander("Export filtered report"):
        import pandas as pd, io
        df = pd.DataFrame(rows_f)
        csv_buf = io.StringIO()
        df.to_csv(csv_buf, index=False)
        st.download_button(
            "Download CSV",
            data=csv_buf.getvalue(),
            file_name=f"client_report_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=False
        )



# ---------- Smooth-scroll helper ----------
def _scroll_to(element_id: str):
    components.html(
        f"""
        <script>
          const el = parent.document.getElementById("{element_id}");
          if (el) {{
            el.scrollIntoView({{behavior: "smooth", block: "start"}});
          }}
        </script>
        """,
        height=0,
    )

# ---------- INLINE (non-iframe) client row with tiny icon buttons ----------
def _client_row_html_inline(name: str, norm: str, cid: int, active: bool):
    """
    Renders a client row directly in the page (NOT in an iframe) so clicks update the
    parent URL query params reliably. Icons are tiny and show tooltip on hover.
    """
    status = "active" if active else "inactive"
    view_toggle_label = "Deactivate" if active else "Activate"
    view_toggle_icon  = "○" if active else "●"

    # Links use GET params that the top-of-file handler processes.
    # Report link sets ?report=<norm>&scroll=1 and jumps to the report anchor.
    html = f"""
<div class="client-row">
  <div class="client-main">
    <span class="client-name">{escape(name)}</span>
    <span class="client-status {status}">{status}</span>
  </div>
  <div class="action-bar">
    <a class="icon-btn" data-tip="View report"
       href="?report={escape(norm)}&scroll=1#report_anchor">▦</a>
    <a class="icon-btn" data-tip="Rename"
       href="#"
       onclick="
         const newName = prompt('Rename client: {escape(name)}','{escape(name)}');
         if (newName && newName.trim()) {{
           const u = new URL(window.location.href);
           u.searchParams.set('act','rename');
           u.searchParams.set('id','{cid}');
           u.searchParams.set('arg', newName.trim());
           window.location.href = u.toString();
         }}
         return false;
       ">✎</a>
    <a class="icon-btn" data-tip="{view_toggle_label}"
       href="?act=toggle&id={cid}">{view_toggle_icon}</a>
    <a class="icon-btn danger" data-tip="Delete"
       href="#"
       onclick="
         if (confirm('Delete {escape(name)}? This cannot be undone.')) {{
           const u = new URL(window.location.href);
           u.searchParams.set('act','delete');
           u.searchParams.set('id','{cid}');
           window.location.href = u.toString();
         }}
         return false;
       ">⌫</a>
  </div>
</div>
"""
    st.markdown(html, unsafe_allow_html=True)

# ---------- CLIENTS TAB ----------
with tab_clients:
    st.subheader("Clients")
    st.caption("Manage active and inactive clients. “test test” is always hidden.")

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    # ---- Session state ----
    if "report_client" not in st.session_state:
        st.session_state["report_client"] = None       # holds name_norm for the selected report
    if "rename_open_id" not in st.session_state:
        st.session_state["rename_open_id"] = None      # which row is currently in rename mode

    # ---- Compact CSS for this tab ----
    st.markdown("""
    <style>
      /* Row + layout */
      .row-wrap { padding:8px 6px; border-bottom:1px solid rgba(0,0,0,.08); }
      .row-line { display:flex; align-items:center; justify-content:space-between; gap:8px; }
      .left-stack { display:flex; align-items:center; gap:10px; min-width:0; }
      .name-strong { font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      /* Green Active pill */
      .tag-active { font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; background:#dcfce7; color:#166534; }
      .tag-inactive { font-size:11px; font-weight:700; padding:2px 8px; border-radius:999px; background:#e2e8f0; color:#334155; }
      /* Make icon buttons compact (affects only small buttons rendered hereafter) */
      div[data-testid="stButton"] button {
        padding: 2px 8px;
        height: 28px;
        font-size: 14px;
        line-height: 1;
      }
    </style>
    """, unsafe_allow_html=True)

def render_client_row(c):
    # --- Row shell ---
    st.markdown('<div class="row-wrap"><div class="row-line">', unsafe_allow_html=True)

    # LEFT: name + active pill
    left_col, right_col = st.columns([0.75, 0.25])
    with left_col:
        st.markdown(
            f"""
            <div class="left-stack">
                <span class="name-strong">{escape(c['name'])}</span>
                <span class="{'tag-active' if c['active'] else 'tag-inactive'}">
                    {'Active' if c['active'] else 'Inactive'}
                </span>
            </div>
            """,
            unsafe_allow_html=True
        )

    # RIGHT: four icon buttons in a single flow (no per-icon subcolumns)
    with right_col:
        row_cls = f"icon-row-{c['id']}"
        # Row-scoped CSS to make st.button wrappers render inline and compact
        st.markdown(
            f"""
            <style>
              .{row_cls} div[data-testid="stButton"] {{
                display: inline-block;
                margin-right: 6px;
              }}
              .{row_cls} div[data-testid="stButton"] button {{
                padding: 2px 8px;
                height: 28px;
                font-size: 14px;
                line-height: 1;
              }}
            </style>
            <div class="{row_cls}"></div>
            """,
            unsafe_allow_html=True
        )

        # Place buttons sequentially; CSS above makes them inline side-by-side
        # ▦ Report
        if st.button("▦", key=f"report_{c['id']}", help="View report", use_container_width=False):
            st.session_state["report_client"] = c.get("name_norm")

        # ✎ Rename (toggle inline rename controls)
        if st.button("✎", key=f"rename_{c['id']}", help="Rename client", use_container_width=False):
            st.session_state["rename_open_id"] = c["id"] if st.session_state["rename_open_id"] != c["id"] else None

        # ○/● Toggle Active
        toggle_icon = "●" if c["active"] else "○"
        toggle_help = "Deactivate" if c["active"] else "Activate"
        if st.button(toggle_icon, key=f"toggle_{c['id']}", help=toggle_help, use_container_width=False):
            toggle_client_active(c["id"], not c["active"])
            _safe_rerun()

        # ⌫ Delete
        if st.button("⌫", key=f"delete_{c['id']}", help="Delete client", use_container_width=False):
            delete_client(c["id"])
            _safe_rerun()

    st.markdown('</div></div>', unsafe_allow_html=True)

    # Inline rename controls (only when opened for this id)
    if st.session_state.get("rename_open_id") == c["id"]:
        new_name = st.text_input("New name", value=c["name"], key=f"rename_input_{c['id']}")
        col_ok, col_cancel = st.columns([1,1])
        with col_ok:
            if st.button("Save name", key=f"rename_save_{c['id']}"):
                ok, msg = rename_client(c["id"], new_name)
                if ok:
                    st.session_state["rename_open_id"] = None
                    _safe_rerun()
                else:
                    st.error(msg)
        with col_cancel:
            if st.button("Cancel", key=f"rename_cancel_{c['id']}"):
                st.session_state["rename_open_id"] = None


    colA, colB = st.columns(2)

    # Active list
    with colA:
        st.markdown("### Active")
        if not active:
            st.write("_No active clients_")
        else:
            for c in active:
                render_client_row(c)

    # Inactive list
    with colB:
        st.markdown("### Inactive")
        if not inactive:
            st.write("_No inactive clients_")
        else:
            for c in inactive:
                render_client_row(c)

    # ---- REPORT SECTION INLINE (below lists) ----
    if st.session_state["report_client"]:
        client_norm = st.session_state["report_client"]
        display_name = next(
            (c["name"] for c in all_clients if c.get("name_norm") == client_norm),
            client_norm
        )

        st.markdown("---")
        # Single concise header (no duplication)
        st.markdown(f"### Report: {escape(display_name)}", unsafe_allow_html=True)

        # Render the report without its own header (avoid redundancy)
        _render_client_report_view(display_name, client_norm, show_header=False)

        # Close report button
        close_cols = st.columns([1, 6])
        with close_cols[0]:
            if st.button("Close report", key="close_report"):
                st.session_state["report_client"] = None
