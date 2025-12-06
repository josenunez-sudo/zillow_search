# ui/run_tab.py
# Run tab with: hyperlinks-only results, clickable thumbnails, TOURED cross-check via Supabase,
# and a post-run "Add to client" action (like the Tours tab) — no auto-logging.

import os
import csv
import io
import re
import time
import json
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional deps ----------
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Robust address parser (IDX/Homespotter-safe) ----------
try:
    from utils.address_parser import address_as_markdown_link
except Exception:
    address_as_markdown_link = None

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

# ---------- Secrets/env ----------
for k in [
    "AZURE_SEARCH_ENDPOINT",
    "AZURE_SEARCH_INDEX",
    "AZURE_SEARCH_API_KEY",
    "BING_API_KEY",
    "BING_CUSTOM_CONFIG_ID",
    "GOOGLE_MAPS_API_KEY",
    "BITLY_TOKEN",
]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_INDEX = os.getenv("AZURE_SEARCH_INDEX", "")
AZURE_SEARCH_KEY = os.getenv("AZURE_SEARCH_API_KEY", "")
BING_API_KEY = os.getenv("BING_API_KEY", "")
BING_CUSTOM_ID = os.getenv("BING_CUSTOM_CONFIG_ID", "")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
BITLY_TOKEN = os.getenv("BITLY_TOKEN", "")
REQUEST_TIMEOUT = 12

# ---------- Styles ----------
st.markdown(
    """
<style>
.center-box {
  padding:10px 12px;
  background:transparent;
  border-radius:12px;
}
.link-list {
  margin:0.25rem 0 0 1.1rem;
  padding:0;
}
.badge {
  display:inline-block;
  font-size:11px;
  font-weight:800;
  padding:2px 6px;
  border-radius:999px;
  margin-left:6px;
  border:1px solid rgba(0,0,0,.15);
}
.badge.new {
  background:#dcfce7;
  color:#065f46;
  border-color:#86efac;
}
.badge.dup {
  background:#fee2e2;
  color:#7f1d1d;
  border-color:#fecaca;
}
.badge.tour {
  background:#e0f2fe;
  color:#075985;
  border-color:#7dd3fc;
}
.run-zone .stButton>button {
  background: linear-gradient(180deg, #0A84FF 0%, #0060DF 100%) !important;
  color:#fff !important;
  font-weight:800 !important;
  border:0 !important;
  border-radius:12px !important;
  box-shadow:0 10px 22px rgba(10,132,255,.35),0 2px 6px rgba(0,0,0,.18)!important;
}
</style>
""",
    unsafe_allow_html=True,
)

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False


# Slugify address exactly like the DB generated column
SLUG_KEEP = re.compile(r"[^\w\s,-]")


def address_to_slug(addr: str) -> str:
    if not addr:
        return ""
    s = SLUG_KEEP.sub("", addr.lower())
    s = re.sub(r"\s+", "-", s.strip())
    return s


def address_text_from_url(url: str) -> str:
    if not url:
        return ""
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
URL_KEYS = {
    "url",
    "link",
    "source url",
    "source_url",
    "listing url",
    "listing_url",
    "property url",
    "property_url",
    "href",
}


def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None


UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
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
    """
    If this is a Zillow /homes/..._rb/ URL, try to upgrade it to /homedetails/..._zpid/
    by scraping the page.

    For non-Zillow URLs, or URLs already on /homedetails/, return as-is.
    """
    if not url:
        return url

    # Only operate on Zillow URLs
    if "zillow.com" not in url or "/homedetails/" in url:
        return url

    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', r.text)
        return m.group(1) if m else url
    except Exception:
        return url


# Content extractors
def extract_any_mls_id(html: str) -> Optional[str]:
    if not html:
        return None
    for pat in [
        r'"mlsId"\s*:\s*"([A-Za-z0-9\-]{5,})"',
        r'"mls"\s*:\s*"([A-Za-z0-9\-]{5,})"',
        r'"listingId"\s*:\s*"([A-Za-z0-9\-]{5,})"',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return m.group(1)
    m = re.search(r'\bMLS[^A-Za-z0-9]{0,5}#?\s*([A-Za-z0-9\-]{5,})\b', html, re.I)
    return m.group(1) if m else None


def extract_address_from_html(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html:
        return out
    m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I)
    out["street"] = m.group(1) if m else ""
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I)
    out["city"] = m.group(1) if m else ""
    m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I)
    out["state"] = m.group(1) if m else ""
    m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I)
    out["zip"] = m.group(1) if m else ""
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
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


# Zillow canonicalization
ZPID_RE = re.compile(r"(\d{6,})_zpid", re.I)


def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r"[#?].*$", "", url)
    m_full = re.search(r"^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)", url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)


def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r"[?#].*$", "", url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base


# Address parsing & variants
ADDR_PRIMARY = {
    "full_address",
    "address",
    "property address",
    "property_address",
    "site address",
    "site_address",
    "street address",
    "street_address",
    "listing address",
    "listing_address",
    "location",
}
NUM_KEYS = {"street #", "street number", "street_no", "streetnum", "house_number", "number", "streetnumber"}
NAME_KEYS = {
    "street name",
    "street",
    "st name",
    "st_name",
    "road",
    "rd",
    "avenue",
    "ave",
    "blvd",
    "boulevard",
    "drive",
    "dr",
    "lane",
    "ln",
    "way",
    "terrace",
    "ter",
    "court",
    "ct",
    "place",
    "pl",
    "parkway",
    "pkwy",
    "square",
    "sq",
    "circle",
    "cir",
    "highway",
    "hwy",
    "route",
    "rt",
}
SUF_KEYS = {"suffix", "st suffix", "street suffix", "suffix1", "suffix2", "street_type", "street type"}
CITY_KEYS = {"city", "municipality", "town"}
STATE_KEYS = {"state", "st", "province", "region"}
ZIP_KEYS = {"zip", "zip code", "postal code", "postalcode", "zip_code", "postal_code"}
MLS_ID_KEYS = {"mls", "mls id", "mls_id", "mls #", "mls#", "mls number", "mlsnumber", "listing id", "listing_id"}
MLS_NAME_KEYS = {"mls name", "mls board", "mls provider", "source", "source mls", "mls source"}
PHOTO_KEYS = {"photo", "image", "photo url", "image url", "picture", "thumbnail", "thumb", "img", "img url", "img_url"}


def norm_key(k: str) -> str:
    return re.sub(r"\s+", " ", (k or "").strip().lower())


def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v:
                return v
    return ""


def extract_components(row):
    n = {norm_key(k): (str(v).strip() if v is not None else "") for k, v in row.items()}
    for k in n.keys():
        if k in ADDR_PRIMARY and n[k]:
            return {
                "street_raw": n[k],
                "city": "",
                "state": "",
                "zip": "",
                "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
                "mls_name": get_first_by_keys(n, MLS_NAME_KEYS),
            }
    num = get_first_by_keys(n, NUM_KEYS)
    name = get_first_by_keys(n, NAME_KEYS)
    suf = get_first_by_keys(n, SUF_KEYS)
    city = get_first_by_keys(n, CITY_KEYS)
    state = get_first_by_keys(n, STATE_KEYS)
    zipc = get_first_by_keys(n, ZIP_KEYS)
    street_raw = " ".join([x for x in [num, name, suf] if x]).strip()
    return {
        "street_raw": street_raw,
        "city": city,
        "state": state,
        "zip": zipc,
        "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
        "mls_name": get_first_by_keys(n, MLS_NAME_KEYS),
    }


LAND_LEAD_TOKENS = {"lot", "lt", "tract", "parcel", "blk", "block", "tbd"}
HWY_EXPAND = {r"\bhwy\b": "highway", r"\bus\b": "US"}
DIR_MAP = {"s": "south", "n": "north", "e": "east", "w": "west"}
LOT_REGEX = re.compile(r"\b(?:lot|lt)\s*[-#:]?\s*([A-Za-z0-9]+)\b", re.I)


def clean_land_street(street: str) -> str:
    if not street:
        return street
    s = street.strip()
    s = re.sub(r"^\s*0[\s\-]+", "", s)
    tokens = re.split(r"[\s\-]+", s)
    if tokens and tokens[0].lower() in LAND_LEAD_TOKENS:
        tokens = [t for t in tokens[1:] if t]
        s = " ".join(tokens)
    s_lower = f" {s.lower()} "
    for pat, repl in HWY_EXPAND.items():
        s_lower = re.sub(pat, f" {repl} ", s_lower)
    s = re.sub(r"\s+", " ", s_lower).strip()
    s = re.sub(r"[^\w\s/-]", "", s)
    return s


def compose_query_address(street, city, state, zipc, defaults):
    parts = [street]
    c = (city or defaults.get("city", "")).strip()
    stt = (state or defaults.get("state", "")).strip()
    z = (zipc or defaults.get("zip", "")).strip()
    if c:
        parts.append(c)
    if stt:
        parts.append(stt)
    if z:
        parts.append(z)
    return " ".join([p for p in parts if p]).strip()


def generate_address_variants(street, city, state, zipc, defaults):
    city = (city or defaults.get("city", "")).strip()
    st = (state or defaults.get("state", "")).strip()
    z = (zipc or defaults.get("zip", "")).strip()
    base = (street or "").strip()

    lot_match = LOT_REGEX.search(base)
    lot_num = lot_match.group(1) if lot_match else None

    core = base
    core = re.sub(r"\bu\.?s\.?\b", "US", core, flags=re.I)
    core = re.sub(r"\bhwy\b", "highway", core, flags=re.I)
    core = re.sub(r"\b([NSEW])\b", lambda m: DIR_MAP.get(m.group(1).lower(), m.group(1)), core, flags=re.I)

    variants = {core, re.sub(r"\bhighway\b", "hwy", core, flags=re.I)}

    lot_variants = set(variants)
    if lot_num:
        for v in list(variants):
            lot_variants.update({f"lot {lot_num} {v}", f"{v} lot {lot_num}", f"lot-{lot_num} {v}"})

    stripped_variants = {LOT_REGEX.sub("", v).strip() for v in list(lot_variants)}
    all_street_variants = lot_variants | stripped_variants

    out = []
    for sv in all_street_variants:
        parts = [sv] + [p for p in [city, st, z] if p]
        out.append(" ".join(parts))
    return [s for s in dict.fromkeys(out) if s.strip()]


# Search (Bing/Azure)
BING_WEB = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def url_matches_city_state(url: str, city: str = None, state: str = None) -> bool:
    u = (url or "")
    ok = True
    if state:
        st2 = state.upper().strip()
        if f"-{st2}-" not in u and f"/{st2.lower()}/" not in u:
            ok = False
    if city and ok:
        cs = f"-{_slug(city)}-"
        if cs not in u:
            ok = False
    return ok


def bing_search_items(query):
    key = BING_API_KEY
    custom = BING_CUSTOM_ID
    if not key:
        return []
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
        return (
            data.get("webPages", {}).get("value")
            if "webPages" in data
            else data.get("items", []) or []
        )
    except requests.RequestException:
        return []


MLS_HTML_PATTERNS = [
    lambda mid: rf'\bMLS[^A-Za-z0-9]{{0,5}}#?\s*{re.escape(mid)}\b',
    lambda mid: rf'\bMLS\s*#?\s*{re.escape(mid)}\b',
    lambda mid: rf'"mls"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*{re.escape(mid)}',
]


def page_contains_mls(html: str, mls_id: str) -> bool:
    for mk in MLS_HTML_PATTERNS:
        if re.search(mk(mls_id), html, re.I):
            return True
    return False


def page_contains_city_state(html: str, city: str = None, state: str = None) -> bool:
    ok = False
    if city and re.search(re.escape(city), html, re.I):
        ok = True
    if state and re.search(rf"\b{re.escape(state)}\b", html, re.I):
        ok = True
    return ok


def confirm_or_resolve_on_page(
    url: str, mls_id: str = None, required_city: str = None, required_state: str = None
):
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        html = r.text
        if mls_id and page_contains_mls(html, mls_id):
            return url, "mls_match"
        if page_contains_city_state(html, required_city, required_state) and "/homedetails/" in url:
            return url, "city_state_match"
        if url.endswith("_rb/") and "/homedetails/" not in url:
            cand = re.findall(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)[:8]
            for u in cand:
                try:
                    rr = requests.get(u, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
                    rr.raise_for_status()
                    h2 = rr.text
                    if (mls_id and page_contains_mls(h2, mls_id)):
                        return u, "mls_match"
                    if page_contains_city_state(h2, required_city, required_state):
                        return u, "city_state_match"
                except Exception:
                    continue
    except Exception:
        return None, None
    return None, None


def find_zillow_by_mls_with_confirmation(
    mls_id,
    required_state=None,
    required_city=None,
    mls_name=None,
    delay=0.35,
    require_match=False,
    max_candidates=20,
):
    if not (BING_API_KEY and mls_id):
        return None, None
    q_mls = [
        f'"MLS# {mls_id}" site:zillow.com',
        f'"{mls_id}" "MLS" site:zillow.com',
        f"{mls_id} site:zillow.com/homedetails",
    ]
    if mls_name:
        q_mls = [f'{q} "{mls_name}"' for q in q_mls] + q_mls
    seen, candidates = set(), []
    for q in q_mls:
        items = bing_search_items(q)
        for it in items:
            url = it.get("url") or it.get("link") or ""
            if not url or "zillow.com" not in url:
                continue
            if "/homedetails/" not in url and "/homes/" not in url:
                continue
            if require_match and not url_matches_city_state(url, required_city, required_state):
                continue
            if url in seen:
                continue
            seen.add(url)
            candidates.append(url)
            if len(candidates) >= max_candidates:
                break
        if len(candidates) >= max_candidates:
            break
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(
            u, mls_id=mls_id, required_city=required_city, required_state=required_state
        )
        if ok:
            return ok, mtype or "mls_match"
    return None, None


def azure_search_first_zillow(query_address):
    if not (AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX and AZURE_SEARCH_KEY):
        return None
    url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{AZURE_SEARCH_INDEX}/docs/search?api-version=2023-11-01"
    h = {"Content-Type": "application/json", "api-key": AZURE_SEARCH_KEY}
    try:
        r = requests.post(
            url,
            headers=h,
            data=json.dumps({"search": query_address, "top": 1}),
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or {}
        hits = data.get("value") or data.get("results") or []
        if not hits:
            return None
        doc = hits[0].get("document") or hits[0]
        for k in ("zillow_url", "zillowLink", "zillow", "url", "link"):
            v = doc.get(k) if isinstance(doc, dict) else None
            if isinstance(v, str) and "zillow.com" in v:
                return v
    except requests.RequestException:
        return None
    return None


def resolve_homedetails_with_bing_variants(
    address_variants,
    required_state=None,
    required_city=None,
    mls_id=None,
    delay=0.3,
    require_match=False,
):
    if not BING_API_KEY:
        return None, None
    candidates, seen = [], set()
    for qaddr in address_variants:
        queries = [
            f"{qaddr} site:zillow.com/homedetails",
            f'"{qaddr}" site:zillow.com/homedetails',
            f"{qaddr} land site:zillow.com/homedetails",
            f"{qaddr} lot site:zillow.com/homedetails",
        ]
        if mls_id:
            queries = [
                f'"MLS# {mls_id}" site:zillow.com/homedetails',
                f"{mls_id} site:zillow.com/homedetails",
                f'"{mls_id}" "MLS" site:zillow.com/homedetails',
            ] + queries
        for q in queries:
            items = bing_search_items(q)
            for it in items:
                url = it.get("url") or it.get("link") or ""
                if not url or "zillow.com" not in url:
                    continue
                if "/homedetails/" not in url and "/homes/" not in url:
                    continue
                if require_match and not url_matches_city_state(url, required_city, required_state):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                candidates.append(url)
            time.sleep(delay)
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(
            u, mls_id=mls_id, required_city=required_city, required_state=required_state
        )
        if ok:
            return ok, mtype or "city_state_match"
    return None, None


def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    c = (city or defaults.get("city", "")).strip()
    st_abbr = (state or defaults.get("state", "")).strip()
    z = (zipc or defaults.get("zip", "")).strip()
    slug_parts = [street]
    loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts:
        slug_parts.append(", ".join(loc_parts))
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


# ---------- Resolve from arbitrary source URL (PATCHED) ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str, str]) -> Tuple[str, str]:
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # 1) Try MLS→Zillow
    mls_id = extract_any_mls_id(html)
    if not mls_id:
        # also try to read trailing ID from URLs like .../tmlspar/10116790
        m = re.search(r"/([A-Za-z0-9]{6,})/?$", final_url)
        if m:
            mls_id = m.group(1)
    if mls_id:
        z1, _ = find_zillow_by_mls_with_confirmation(mls_id)
        if z1:
            return z1, ""

    # 2) Robust parser: pull address from the page and build a Zillow deeplink
    street = city = state = zipc = ""
    if address_as_markdown_link:
        try:
            _md, info = address_as_markdown_link(final_url, parse_html=True)
            street = (info.get("streetAddress") or "").strip()
            city = (info.get("addressLocality") or "").strip()
            state = (info.get("addressRegion") or "").strip()
            zipc = (info.get("postalCode") or "").strip()
        except Exception:
            pass
    if not (street or (city and state)):
        # fallback to lightweight extractor (older behavior)
        addr = extract_address_from_html(html)
        street = street or (addr.get("street", "") or "")
        city = city or (addr.get("city", "") or "")
        state = state or (addr.get("state", "") or "")
        zipc = zipc or (addr.get("zip", "") or "")

    if street or (city and state):
        used_addr = compose_query_address(street, city, state, zipc, defaults)
        zurl = construct_deeplink_from_parts(street or used_addr, city, state, zipc, defaults)
        return zurl, used_addr

    # 3) Title/desc → homedetails search
    title = extract_title_or_desc(html)
    if title:
        for q in [f'"{title}" site:zillow.com/homedetails', f"{title} site:zillow.com"]:
            items = bing_search_items(q)
            for it in items:
                u = it.get("url") or ""
                if "/homedetails/" in u:
                    return u, title

    # 4) Give up — but do NOT return a non-Zillow URL.
    # Let the caller fall back to its own logic instead.
    return "", ""


# Primary resolver
def process_single_row(
    row,
    *,
    delay=0.5,
    land_mode=True,
    defaults=None,
    require_state=True,
    mls_first=True,
    default_mls_name="",
    max_candidates=20,
):
    defaults = defaults or {"city": "", "state": "", "zip": ""}
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)
    comp = extract_components(row)
    street_raw = comp["street_raw"]
    street_clean = clean_land_street(street_raw) if land_mode else street_raw
    variants = generate_address_variants(
        street_raw, comp["city"], comp["state"], comp["zip"], defaults
    )
    if land_mode:
        variants = list(
            dict.fromkeys(
                variants
                + generate_address_variants(
                    street_clean, comp["city"], comp["state"], comp["zip"], defaults
                )
            )
        )
    query_address = (
        variants[0]
        if variants
        else compose_query_address(
            street_raw, comp["city"], comp["state"], comp["zip"], defaults
        )
    )
    deeplink = construct_deeplink_from_parts(
        street_raw, comp["city"], comp["state"], comp["zip"], defaults
    )
    required_state_val = defaults.get("state") if require_state else None
    required_city_val = comp["city"] or defaults.get("city")
    zurl, status = None, "fallback"
    mls_id = (comp.get("mls_id") or "").strip()
    mls_name = (comp.get("mls_name") or default_mls_name or "").strip()
    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id,
            required_state=required_state_val,
            required_city=required_city_val,
            mls_name=mls_name,
            delay=min(delay, 0.6),
            require_match=require_state,
            max_candidates=max_candidates,
        )
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z:
            zurl, status = z, "azure_hit"
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants,
            required_state=required_state_val,
            required_city=required_city_val,
            mls_id=mls_id or None,
            delay=min(delay, 0.6),
            require_match=require_state,
        )
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        zurl, status = deeplink, "deeplink_fallback"
    time.sleep(min(delay, 0.4))
    return {
        "input_address": query_address,
        "mls_id": mls_id,
        "zillow_url": zurl,
        "status": status,
        "csv_photo": csv_photo,
    }


# Enrichment
RE_PRICE = re.compile(
    r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I
)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)
RE_DESC = re.compile(
    r'"(?:description|homeDescription|marketingDescription)"\s*:\s*"([^"]+)"', re.I
)

KEY_HL = [
    ("new roof", "roof"),
    ("hvac", "hvac"),
    ("ac unit", "ac"),
    ("furnace", "furnace"),
    ("water heater", "water heater"),
    ("renovated", "renovated"),
    ("updated", "updated"),
    ("remodeled", "remodeled"),
    ("open floor plan", "open plan"),
    ("cul-de-sac", "cul-de-sac"),
    ("pool", "pool"),
    ("fenced", "fenced"),
    ("acre", "acre"),
    ("hoa", "hoa"),
    ("primary on main", "primary on main"),
    ("finished basement", "finished basement"),
]


def _tidy_txt(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def summarize_remarks(text: str, max_sent: int = 2) -> str:
    text = _tidy_txt(text)
    if not text:
        return ""
    sents = re.split(r"(?<=[\.\!\?])\s+", text)
    if len(sents) <= max_sent:
        return text
    pref_kw = [
        "updated",
        "renovated",
        "new",
        "roof",
        "hvac",
        "kitchen",
        "bath",
        "floor",
        "windows",
        "mechanicals",
        "acres",
        "acre",
        "lot",
        "school",
        "zoned",
        "hoa",
        "no hoa",
    ]
    scored = [
        (sum(1 for k in pref_kw if k in s.lower()), i, s)
        for i, s in enumerate(sents[:8])
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return " ".join([s for _, _, s in scored[:max_sent]])


def extract_highlights(text: str) -> List[str]:
    t = (text or "").lower()
    out = []
    for pat, label in KEY_HL:
        if pat in t:
            out.append(label)
    return list(dict.fromkeys(out))[:6]


async def _fetch_html_async(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return r.text
    except Exception:
        return ""
    return ""


def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html:
        return None
    for target_w in ("960", "1152", "768", "1536"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html,
            re.I,
        )
        if m:
            return m.group(1)
    m = re.search(r"srcset=['\"]([^'\"]*photos\.zillowstatic\.com[^'\"]+)['\"]", html, re.I)
    if m:
        cand = []
        for part in m.group(1).split(","):
            part = part.strip()
            m2 = re.match(
                r"(https://photos\.zillowstatic\.com/\S+)\s+(\d+)w", part, re.I
            )
            if m2:
                cand.append((int(m2.group(2)), m2.group(1)))
        if cand:
            up = [u for (w, u) in cand if w <= 1152]
            return (
                sorted(((w, u) for (w, u) in cand if w <= 1152), key=lambda x: x[0])[
                    -1
                ][1]
                if up
                else sorted(cand, key=lambda x: x[0])[-1][1]
            )
    m = re.search(
        r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I
    )
    return m.group(1) if m else None


def parse_listing_meta(html: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not html:
        return meta
    m = RE_PRICE.search(html)
    meta["price"] = m.group(1) if m else None
    m = RE_STATUS.search(html)
    meta["status"] = m.group(1) if m else None
    m = RE_BEDS.search(html)
    meta["beds"] = m.group(1) if m else None
    m = RE_BATHS.search(html)
    meta["baths"] = m.group(1) if m else None
    m = RE_SQFT.search(html)
    meta["sqft"] = m.group(1) if m else None
    m = RE_DESC.search(html)
    remark = m.group(1) if m else None
    if not remark:
        m2 = re.search(
            r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
            html,
            re.I,
        )
        if m2:
            remark = m2.group(1)
    meta["remarks"] = remark
    img = extract_zillow_first_image(html)
    if not img:
        m3 = re.search(
            r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]",
            html,
            re.I,
        )
        if m3:
            img = m3.group(1)
    meta["image_url"] = img
    meta["summary"] = summarize_remarks(remark or "")
    meta["highlights"] = extract_highlights(remark or "")
    return meta


async def enrich_results_async(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targets = [
        (i, r["zillow_url"])
        for i, r in enumerate(results)
        if "/homedetails/" in (r.get("zillow_url") or "")
    ]
    if not targets:
        return results
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
            if meta:
                results[i].update(meta)
    return results


# ---------- Images fallback ----------
def picture_for_result_with_log(
    query_address: str, zurl: str, csv_photo_url: Optional[str] = None
):
    log = {
        "url": zurl,
        "csv_provided": bool(csv_photo_url),
        "stage": None,
        "status_code": None,
        "html_len": None,
        "selected": None,
        "errors": [],
    }

    def _ok(u: str) -> bool:
        return isinstance(u, str) and (
            u.startswith("http://") or u.startswith("https://") or u.startswith("data:")
        )

    if csv_photo_url and _ok(csv_photo_url):
        log["stage"] = "csv_photo"
        log["selected"] = csv_photo_url
        return csv_photo_url, log

    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            log["status_code"] = r.status_code
            if r.ok:
                html = r.text
                log["html_len"] = len(html)
                zfirst = extract_zillow_first_image(html)
                if zfirst:
                    log["stage"] = "zillow_hero"
                    log["selected"] = zfirst
                    return zfirst, log
                for pat in [
                    r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"<meta[^>]+property=['\"]og:image:secure_url['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"\"image\"\s*:\s*\"(https?://[^\"]+)\"",
                    r"\"image\"\s*:\s*\[\s*\"(https?://[^\"]+)\"",
                ]:
                    m = re.search(pat, html, re.I)
                    if m:
                        log["stage"] = "og_image"
                        log["selected"] = m.group(1)
                        return m.group(1), log
        except Exception as e:
            log["errors"].append(f"fetch_err:{e!r}")

    try:
        key = GOOGLE_MAPS_API_KEY
        if key and query_address:
            from urllib.parse import quote_plus

            loc = quote_plus(query_address)
            sv = f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={key}"
            log["stage"] = "street_view"
            log["selected"] = sv
            return sv, log
        else:
            if not key:
                log["errors"].append("no_google_maps_key")
    except Exception as e:
        log["errors"].append(f"sv_err:{e!r}")

    log["stage"] = "none"
    return None, log


@st.cache_data(ttl=900, show_spinner=False)
def get_thumbnail_and_log(query_address: str, zurl: str, csv_photo_url: Optional[str]):
    return picture_for_result_with_log(query_address, zurl, csv_photo_url)


# ---------- Tracking + Bitly ----------
def make_trackable_url(url: str, client_tag: str, campaign_tag: str) -> str:
    client_tag = re.sub(r"[^a-z0-9\-]+", "", (client_tag or "").lower().replace(" ", "-"))
    campaign_tag = re.sub(r"[^a-z0-9\-]+", "", (campaign_tag or "").lower().replace(" ", "-"))
    frag = f"#aa={client_tag}.{campaign_tag}" if (client_tag or campaign_tag) else ""
    return (url or "") + (frag if url and frag else "")


def bitly_shorten(long_url: str) -> Optional[str]:
    token = BITLY_TOKEN
    if not token:
        return None
    try:
        r = requests.post(
            "https://api-ssl.bitly.com/v4/shorten",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"long_url": long_url},
            timeout=10,
        )
        if r.ok:
            return r.json().get("link")
    except Exception:
        return None
    return None


# ---------- Supabase sent lookups ----------
def _supabase_available():
    try:
        return bool(SUPABASE)
    except NameError:
        return False


@st.cache_data(ttl=300, show_spinner=False)
def get_already_sent_maps(client_tag: str):
    if not (_supabase_available() and client_tag.strip()):
        return set(), set(), {}, {}
    try:
        rows = (
            SUPABASE.table("sent")
            .select("canonical,zpid,url,sent_at")
            .eq("client", client_tag.strip())
            .limit(20000)
            .execute()
            .data
            or []
        )
        canon_set = {(r.get("canonical") or "").strip() for r in rows if r.get("canonical")}
        zpid_set = {(r.get("zpid") or "").strip() for r in rows if r.get("zpid")}
        canon_info: Dict[str, Dict[str, str]] = {}
        zpid_info: Dict[str, Dict[str, str]] = {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info:
                canon_info[c] = info
            if z and z not in zpid_info:
                zpid_info[z] = info
        return canon_set, zpid_set, canon_info, zpid_info
    except Exception:
        return set(), set(), {}, {}


# ---------- Tours cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def get_tour_slug_map(client_tag: str) -> Dict[str, Dict[str, str]]:
    if not (_supabase_available() and client_tag.strip()):
        return {}
    try:
        tours = (
            SUPABASE.table("tours")
            .select("id,tour_date")
            .eq("client", client_tag.strip())
            .order("tour_date", desc=True)
            .limit(5000)
            .execute()
            .data
            or []
        )
        if not tours:
            return {}
        ids = [t["id"] for t in tours if t.get("id")]
        stops: List[Dict[str, Any]] = []
        for i in range(0, len(ids), 50):
            batch = ids[i : i + 50]
            resp = (
                SUPABASE.table("tour_stops")
                .select("tour_id,address,address_slug,start,end,deeplink")
                .in_("tour_id", batch)
                .limit(20000)
                .execute()
            )
            stops.extend(resp.data or [])
        tdate = {t["id"]: (t.get("tour_date") or None) for t in tours}
        by_slug: Dict[str, Dict[str, str]] = {}
        for s in stops:
            slug = (s.get("address_slug") or address_to_slug(s.get("address", "")) or "").strip()
            if not slug:
                continue
            info = {
                "date": (tdate.get(s.get("tour_id")) or ""),
                "start": s.get("start", "") or "",
                "end": s.get("end", "") or "",
            }
            prev = by_slug.get(slug)
            if not prev or (info["date"] and prev.get("date") and str(info["date"]) > str(prev.get("date"))):
                by_slug[slug] = info
            elif not prev:
                by_slug = by_slug
        return by_slug
    except Exception:
        return {}


def mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info):
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not url:
            r["already_sent"] = False
        else:
            canon, zpid = canonicalize_zillow(url)
            reason = None
            sent_when = ""
            sent_url = ""
            if canon and canon in canon_set:
                reason = "canonical"
                meta = canon_info.get(canon, {})
                sent_when, sent_url = meta.get("sent_at", ""), meta.get("url", "")
            elif zpid and zpid in zpid_set:
                reason = "zpid"
                meta = zpid_info.get(zpid, {})
                sent_when, sent_url = meta.get("sent_at", ""), meta.get("url", "")
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
        canon = r.get("canonical")
        zpid = r.get("zpid")
        if not (canon and zpid):
            canon2, zpid2 = canonicalize_zillow(raw_url)
            canon = canon or canon2
            zpid = zpid or zpid2
        rows.append(
            {
                "client": (client_tag or "").strip(),
                "campaign": (campaign_tag or "").strip(),
                "url": raw_url,
                "canonical": canon,
                "zpid": zpid,
                "mls_id": (r.get("mls_id") or "").strip() or None,
                "address": (r.get("input_address") or "").strip() or None,
                "sent_at": now_iso,
            }
        )
    if not rows:
        return False, "No valid rows to log."
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)


# ---------- Clients registry helpers (cached) ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok():
        return []
    try:
        rows = (
            SUPABASE.table("clients")
            .select("id,name,name_norm,active")
            .order("name", desc=False)
            .execute()
            .data
            or []
        )
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []


def invalidate_clients_cache():
    try:
        fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


def upsert_client(name: str, active: bool = True, notes: str = None):
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        name_norm = _norm_tag(name)
        payload = {"name": name.strip(), "name_norm": name_norm, "active": active}
        if notes is not None:
            payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)


# ---------- Output builders ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = [
            "input_address",
            "mls_id",
            "url",
            "status",
            "price",
            "beds",
            "baths",
            "sqft",
            "already_sent",
            "dup_reason",
            "dup_sent_at",
            "toured",
            "toured_date",
            "toured_start",
            "toured_end",
        ]
        if include_notes:
            fields += ["summary", "highlights", "remarks"]
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields if k != "url"}
            row["url"] = pick_url(r)
            w.writerow(row)
        return s.getvalue(), "text/csv"

    if fmt == "html":
        items = []
        for r in rows:
            u = pick_url(r)
            if not u:
                continue
            items.append(
                f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>'
            )
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    lines = []
    for r in rows:
        u = pick_url(r)
        if u:
            lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")


# ---------- Batch dedupe for logging ----------
def _dedupe_results_for_logging(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        c, z = canonicalize_zillow(url) if url else ("", "")
        key = c or z or url
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        r2 = dict(r)
        if c:
            r2["canonical"] = c
        if z:
            r2["zpid"] = z
        out.append(r2)
    return out


# ---------- Main renderer ----------
def render_run_tab(state: dict):
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    colC, colK = st.columns([1.2, 1])
    with colC:
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]
        options = [NO_CLIENT] + names + [ADD_SENTINEL]
        sel_idx = st.selectbox(
            "Client (for badges only; logging happens later below)",
            list(range(len(options))),
            format_func=lambda i: options[i],
            index=0,
        )
        selected_client = None if sel_idx in (0, len(options) - 1) else active_clients[sel_idx - 1]

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
        campaign_tag_raw = st.text_input(
            "Campaign tag (used when you log later)",
            value=datetime.utcnow().strftime("%Y%m%d"),
        )

    c1, c2, c3, c4 = st.columns([1, 1, 1.25, 1.45])
    with c1:
        use_shortlinks = st.checkbox(
            "Use short links (Bitly)",
            value=False,
            help="Optional tracking; sharing uses clean Zillow links.",
        )
    with c2:
        enrich_details = st.checkbox("Enrich details", value=False)
    with c3:
        show_details = st.checkbox("Show details under results", value=False)
    with c4:
        only_show_new = st.checkbox(
            "Only show NEW for this client",
            value=bool(selected_client),
            help="Hides duplicates in the results view; logging happens later.",
        )
        if not selected_client:
            only_show_new = False

    table_view = st.checkbox(
        "Show results as table",
        value=False,
        help="Easier to scan details",
    )

    client_tag = _norm_tag(client_tag_raw)
    campaign_tag = _norm_tag(campaign_tag_raw)

    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")

    paste = st.text_area(
        "Paste addresses or links",
        placeholder=(
            "407 E Woodall St, Smithfield, NC 27577\n"
            "https://l.hms.pt/...\n"
            "123 US-301 S, Four Oaks, NC 27524"
        ),
        height=160,
        label_visibility="collapsed",
    )

    opt1, opt2, opt3 = st.columns([1.15, 1, 1.2])
    with opt1:
        remove_dupes = st.checkbox("Remove duplicates (pasted)", value=True)
    with opt2:
        trim_spaces = st.checkbox("Auto-trim (pasted)", value=True)
    with opt3:
        show_preview = st.checkbox("Show preview (pasted)", value=True)

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")
    st.markdown("</div>", unsafe_allow_html=True)

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean = []
    for ln in lines_raw:
        ln = ln.strip() if trim_spaces else ln
        if not ln:
            continue
        if remove_dupes and ln in lines_clean:
            continue
        if is_probable_url(ln):
            lines_clean.append(ln)
        else:
            if usaddress:
                parts = usaddress.tag(ln)[0]
                norm = (
                    parts.get("AddressNumber", "")
                    + " "
                    + " ".join(
                        [
                            parts.get(k, "")
                            for k in [
                                "StreetNamePreDirectional",
                                "StreetName",
                                "StreetNamePostType",
                                "OccupancyType",
                                "OccupancyIdentifier",
                            ]
                        ]
                    ).strip()
                )
                cityst = (
                    (
                        ", "
                        + parts.get("PlaceName", "")
                        + ", "
                        + parts.get("StateName", "")
                        + (
                            " " + parts.get("ZipCode", "")
                            if parts.get("ZipCode")
                            else ""
                        )
                    )
                    if (parts.get("PlaceName") or parts.get("StateName"))
                    else ""
                )
                lines_clean.append(re.sub(r"\s+", " ", (norm + cityst).strip()))
            else:
                lines_clean.append(ln)

    count_pasted = len(lines_clean)
    csv_count = 0
    if file is not None:
        try:
            # more tolerant decode to avoid errors on funky CSVs
            content_peek = file.getvalue().decode("utf-8-sig", errors="ignore")
            csv_reader = csv.DictReader(io.StringIO(content_peek))
            csv_count = sum(1 for _ in csv_reader)
        except Exception:
            csv_count = 0

    bits = [f"**{count_pasted}** pasted"]
    if file is not None:
        bits.append(f"**{csv_count}** CSV")
    st.caption(
        " • ".join(bits)
        + "  •  Paste short links or MLS pages too; we’ll resolve them to Zillow."
    )

    if show_preview and count_pasted:
        st.markdown("**Preview (pasted)** (first 5):")
        st.markdown(
            "<ul class='link-list'>"
            + "\n".join(
                [f"<li>{escape(p)}</li>" for p in lines_clean[:5]]
            )
            + ("<li>…</li>" if count_pasted > 5 else "")
            + "</ul>",
            unsafe_allow_html=True,
        )

    # POPPY RUN BUTTON
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown("</div>", unsafe_allow_html=True)

    # Results HTML list with copy-all (ALWAYS hyperlinks, tight spacing)
    def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
        li_html = []
        for r in results:
            href = (
                r.get("preview_url")
                or r.get("zillow_url")
                or r.get("display_url")
                or ""
            )
            if not href:
                continue
            safe_href = escape(href)
            link_txt = href  # keep URL text for best SMS unfurls

            # Badges: duplicate/new + toured
            badge_html = ""
            if client_selected:
                if r.get("already_sent"):
                    tip = (
                        f"Duplicate ({escape(r.get('dup_reason','') or '-')}); "
                        f"sent {escape(r.get('dup_sent_at') or '-')}"
                    )
                    badge_html += (
                        f' <span class="badge dup" title="{tip}">Duplicate</span>'
                    )
                else:
                    badge_html += (
                        ' <span class="badge new" title="New for this client">NEW</span>'
                    )
                if r.get("toured"):
                    dt = str(r.get("toured_date") or "")
                    tm = str(r.get("toured_start") or "")
                    title = ("Toured " + (dt + (" " + tm if tm else ""))).strip()
                    badge_html += (
                        ' <span class="badge tour" '
                        'title="{title}">TOURED</span>'
                    ).replace("{title}", escape(title))

            li_html.append(
                f'<li style="margin:0.2rem 0;"><a href="{safe_href}" '
                f'target="_blank" rel="noopener">{escape(link_txt)}</a>{badge_html}</li>'
            )

        items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

        copy_lines = []
        for r in results:
            u = (
                r.get("preview_url")
                or r.get("zillow_url")
                or r.get("display_url")
                or ""
            )
            if u:
                copy_lines.append(u.strip())
        copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

        html = f"""
<html><head><meta charset="utf-8" />
  <style>
    html,body {{
      margin:0;
      font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }}
    .results-wrap {{
      position:relative;
      box-sizing:border-box;
      padding:8px 120px 4px 0;
    }}
    ul.link-list {{
      margin:0 0 0.2rem 1.2rem;
      padding:0;
      list-style:disc;
    }}
    ul.link-list li {{
      margin:0.2rem 0;
    }}
    .copyall-btn {{
      position:absolute;
      top:0;
      right:8px;
      z-index:5;
      padding:6px 10px;
      height:26px;
      border:0;
      border-radius:10px;
      color:#fff;
      font-weight:700;
      background:#1d4ed8;
      cursor:pointer;
      opacity:.95;
    }}
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
          const prev=btn.textContent; btn.textContent='✓';
          setTimeout(()=>{{ btn.textContent=prev; }}, 900);
        }} catch(e) {{
          const prev=btn.textContent; btn.textContent='×';
          setTimeout(()=>{{ btn.textContent=prev; }}, 900);
        }}
      }});
    }})();
  </script>
</body></html>
"""
        est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
        components.html(html, height=est_h, scrolling=False)

    def _render_results_and_downloads(
        results: List[Dict[str, Any]],
        client_tag: str,
        campaign_tag: str,
        include_notes: bool,
        client_selected: bool,
    ):
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=client_selected)

        # Optional table
        if table_view:
            import pandas as pd

            cols = [
                "already_sent",
                "dup_reason",
                "dup_sent_at",
                "toured",
                "toured_date",
                "toured_start",
                "toured_end",
                "display_url",
                "zillow_url",
                "preview_url",
                "status",
                "price",
                "beds",
                "baths",
                "sqft",
                "mls_id",
                "input_address",
            ]
            df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
            st.dataframe(df, use_container_width=True, hide_index=True)

        # ---- Download
        fmt_options = ["txt", "csv", "md", "html"]
        prev_fmt = (st.session_state.get("__results__") or {}).get("fmt")
        default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
        fmt = st.selectbox("Download format", fmt_options, index=default_idx)
        payload, mime = build_output(results, fmt, use_display=True, include_notes=include_notes)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        tag = (
            "_"
            + re.sub(
                r"[^a-z0-9\-]+", "", (client_tag or "").lower().replace(" ", "-")
            )
            if client_tag
            else ""
        )
        st.download_button(
            "Export",
            data=payload,
            file_name=f"address_alchemist{tag}_{ts}.{fmt}",
            mime=mime,
            use_container_width=True,
        )
        st.session_state["__results__"] = {"results": results, "fmt": fmt}

        # ---- Thumbnails grid
        thumbs = []
        for r in results:
            img = r.get("image_url")
            if not img:
                img, _ = get_thumbnail_and_log(
                    r.get("input_address", ""),
                    r.get("preview_url") or r.get("zillow_url") or "",
                    r.get("csv_photo"),
                )
            if img:
                thumbs.append((r, img))
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i, (r, img) in enumerate(thumbs):
                with cols[i % 3]:
                    mls_id = (r.get("mls_id") or "").strip()
                    addr = (r.get("input_address") or "").strip()
                    url = (
                        r.get("preview_url")
                        or r.get("zillow_url")
                        or r.get("display_url")
                        or "#"
                    )
                    alt = addr or (f"MLS# {mls_id}" if mls_id else "Listing")

                    toured_badge = ""
                    if r.get("toured"):
                        dt = str(r.get("toured_date") or "")
                        tm = str(r.get("toured_start") or "")
                            # noqa
                        title = ("Toured " + (dt + (" " + tm if tm else ""))).strip()
                        toured_badge = (
                            f'<span class="badge tour" title="{escape(title)}">TOURED</span>'
                        )

                    st.markdown(
                        f"""
<a href="{escape(url)}" target="_blank" rel="noopener" style="text-decoration:none;">
  <img src="{escape(img)}" alt="{escape(alt)}"
       style="width:100%;height:auto;border-radius:12px;display:block;" />
</a>
<div class='img-label'>
  {('<strong>MLS#: ' + escape(mls_id) + '</strong><br/>' if mls_id else '')}
  <a href="{escape(url)}" target="_blank" rel="noopener">
    {escape(addr) if addr else "View listing"}
  </a>
  {toured_badge}
</div>
""",
                        unsafe_allow_html=True,
                    )

        # ============================
        # NEW: Add to client (optional)
        # ============================
        st.markdown("---")
        st.markdown("### Add to client (optional)")

        add_clients = fetch_clients(include_inactive=False)
        add_names = [c["name"] for c in add_clients]
        # Preselect the same client if present; otherwise first item
        default_idx = 0
        if client_tag:
            for i, c in enumerate(add_clients):
                if _norm_tag(c["name"]) == client_tag:
                    default_idx = i
                    break
        sel_add = st.selectbox(
            "Choose client to log to",
            list(range(len(add_clients))),
            format_func=lambda i: add_names[i],
            index=default_idx,
            key="__add_to_client_sel__",
        )
        add_client_name = add_names[sel_add]
        add_client_norm = _norm_tag(add_client_name)

        add_campaign = st.text_input(
            "Campaign tag for this batch",
            value=(campaign_tag or datetime.utcnow().strftime("%Y%m%d")),
            key="__add_campaign_tag__",
        )

        colAA, colBB, colCC = st.columns([1.2, 1.2, 1])
        with colAA:
            only_log_new = st.checkbox(
                "Only log NEW for this client",
                value=only_show_new,
                help="Skips URLs already sent to this client.",
            )
        with colBB:
            dedupe_batch = st.checkbox(
                "Deduplicate this batch (by property)", value=True
            )
        with colCC:
            pass

        if st.button(
            "Add ALL results to selected client",
            type="primary",
            use_container_width=True,
        ):
            try:
                items = results[:]
                # Batch dedupe (optional)
                if dedupe_batch:
                    items = _dedupe_results_for_logging(items)

                # Filter to NEW (optional)
                if only_log_new:
                    canon_set, zpid_set, _, _ = get_already_sent_maps(add_client_norm)
                    filt = []
                    for r in items:
                        url = (
                            r.get("preview_url")
                            or r.get("zillow_url")
                            or r.get("display_url")
                            or ""
                        ).strip()
                        c, z = canonicalize_zillow(url) if url else ("", "")
                        if (c and c in canon_set) or (z and z in zpid_set):
                            continue
                        filt.append(r)
                    items = filt

                if not items:
                    st.warning("Nothing to log (after filters).")
                else:
                    ok, msg = log_sent_rows(
                        items, add_client_norm, _norm_tag(add_campaign)
                    )
                    if ok:
                        st.success(
                            f"Logged {len(items)} item(s) to **{add_client_name}**."
                        )
                        # Update badges for the currently selected (view) client if it matches
                        if client_tag and client_tag == add_client_norm:
                            canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(
                                client_tag
                            )
                            updated = mark_duplicates(
                                results, canon_set, zpid_set, canon_info, zpid_info
                            )
                            st.session_state["__results__"] = {
                                "results": updated,
                                "fmt": st.session_state.get("__results__", {}).get(
                                    "fmt", "txt"
                                ),
                            }
                    else:
                        st.error(f"Log failed: {msg}")
            except Exception as e:
                st.error("Could not log to client.")
                with st.expander("Details"):
                    st.exception(e)

    # ---------- Run click ----------
    if clicked:
        try:
            rows_in: List[Dict[str, Any]] = []
            csv_rows_count = 0
            if file is not None:
                # tolerant decode to avoid crashing on CSV
                content = file.getvalue().decode("utf-8-sig", errors="ignore")
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

            defaults = {"city": "", "state": "", "zip": ""}
            total = len(rows_in)
            results: List[Dict[str, Any]] = []

            prog = st.progress(0, text="Resolving to Zillow…")
            for i, row in enumerate(rows_in, start=1):
                url_in = ""
                url_in = url_in or get_first_by_keys(row, URL_KEYS)
                url_in = url_in or row.get("source_url", "")
                if url_in and is_probable_url(url_in):
                    zurl, used_addr = resolve_from_source_url(url_in, defaults)
                    results.append(
                        {
                            "input_address": used_addr or row.get("address", "") or "",
                            "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
                            "zillow_url": zurl,
                            "status": "",
                            "csv_photo": get_first_by_keys(row, PHOTO_KEYS),
                        }
                    )
                else:
                    res = process_single_row(
                        row,
                        delay=0.45,
                        land_mode=True,
                        defaults=defaults,
                        require_state=True,
                        mls_first=True,
                        default_mls_name="",
                        max_candidates=20,
                    )
                    results.append(res)
                prog.progress(i / total, text=f"Resolved {i}/{total}")
            prog.progress(1.0, text="Links resolved")

            for r in results:
                for key in ("zillow_url", "display_url"):
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

            # ---- Mark duplicates & toured for the *view* client only (no logging here)
            client_selected = bool(client_tag.strip())
            tour_map = get_tour_slug_map(client_tag) if client_selected else {}
            if client_selected:
                canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(
                    client_tag
                )
                results = mark_duplicates(
                    results, canon_set, zpid_set, canon_info, zpid_info
                )
                for r in results:
                    info = tour_map.get(result_to_slug(r), {})
                    r["toured"] = bool(info)
                    r["toured_date"] = info.get("date") if info else ""
                    r["toured_start"] = info.get("start") if info else ""
                    r["toured_end"] = info.get("end") if info else ""
                if only_show_new:
                    results = [r for r in results if not r.get("already_sent")]
            else:
                for r in results:
                    r["already_sent"] = False
                    r["toured"] = False

            st.success(
                f"Processed {len(results)} item(s)"
                + (f" — CSV rows read: {csv_rows_count}" if file is not None else "")
            )

            _render_results_and_downloads(
                results,
                client_tag,
                campaign_tag,
                include_notes=enrich_details,
                client_selected=client_selected,
            )

        except Exception as e:
            st.error("We hit an error while processing.")
            with st.expander("Details"):
                st.exception(e)

    data = st.session_state.get("__results__") or {}
    results = data.get("results") or []
    if results and not clicked:
        _render_results_and_downloads(
            results,
            client_tag,
            campaign_tag,
            include_notes=False,
            client_selected=bool(client_tag.strip()),
        )
    else:
        if not clicked:
            st.info("Paste addresses or links (or upload CSV), then click **Run**.")


if __name__ == "__main__":
    render_run_tab(st.session_state)
