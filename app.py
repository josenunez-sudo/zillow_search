# app.py — Zillow Deeplink Finder (MLS-confirmed, land-aware)
# UX/UI refactor with sidebar, tabs, batching, caching, retries, multi-format export, copy helpers

import os, csv, io, re, time, json, zipfile, hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st

# =========================
# Page config & THEME TIP
# =========================
st.set_page_config(
    page_title="Zillow Deeplink Finder",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# (Optional) Add this to .streamlit/config.toml for a sleek dark theme:
# [theme]
# primaryColor="#2E7D32"
# textColor="#FFFFFF"
# backgroundColor="#0E1117"
# secondaryBackgroundColor="#161A22"

# =========================
# Load optional secrets -> env
# =========================
for k in ["AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY",
          "BING_API_KEY","BING_CUSTOM_CONFIG_ID","GOOGLE_MAPS_API_KEY"]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

# =========================
# Config (from env)
# =========================
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT","").rstrip("/")
AZURE_SEARCH_INDEX    = os.getenv("AZURE_SEARCH_INDEX","")
AZURE_SEARCH_KEY      = os.getenv("AZURE_SEARCH_API_KEY","")

BING_API_KEY          = os.getenv("BING_API_KEY","")
BING_CUSTOM_ID        = os.getenv("BING_CUSTOM_CONFIG_ID","")

GOOGLE_MAPS_API_KEY   = os.getenv("GOOGLE_MAPS_API_KEY","")

BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"

REQUEST_TIMEOUT = 12

# =========================
# CSV field keys
# =========================
ADDR_PRIMARY = {
    "full_address","address","property address","property_address","site address","site_address",
    "street address","street_address","listing address","listing_address","location"
}
NUM_KEYS   = {"street #","street number","street_no","streetnum","house_number","number","streetnumber"}
NAME_KEYS  = {"street name","street","st name","st_name","road","rd","avenue","ave","blvd","boulevard",
              "drive","dr","lane","ln","way","terrace","ter","court","ct","place","pl","parkway","pkwy",
              "square","sq","circle","cir","highway","hwy","route","rt"}
SUF_KEYS   = {"suffix","st suffix","street suffix","suffix1","suffix2","street_type","street type"}
UNIT_KEYS  = {"unit","apt","apartment","suite","ste","lot","unit #","unit number","apt #","apt number"}
CITY_KEYS  = {"city","municipality","town"}
STATE_KEYS = {"state","st","province","region"}
ZIP_KEYS   = {"zip","zip code","postal code","postalcode","zip_code","postal_code"}
COUNTY_KEYS= {"county","county name"}

MLS_ID_KEYS   = {"mls","mls id","mls_id","mls #","mls#","mls number","mlsnumber","listing id","listing_id"}
MLS_NAME_KEYS = {"mls name","mls board","mls provider","source","source mls","mls source"}

# =========================
# Normalizers
# =========================
def norm_key(k:str) -> str:
    return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v:
                return v
    return ""

def extract_components(row):
    """Return components: street_raw, city, state, zip, county, mls_id, mls_name."""
    n = { norm_key(k): (str(v).strip() if v is not None else "") for k,v in row.items() }

    # Full address?
    for k in list(n.keys()):
        if k in ADDR_PRIMARY and n[k]:
            return {
                "street_raw": n[k],
                "city": "", "state": "", "zip": "", "county": "",
                "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
                "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)
            }

    # Build from parts
    num    = get_first_by_keys(n, NUM_KEYS)
    name   = get_first_by_keys(n, NAME_KEYS)
    suf    = get_first_by_keys(n, SUF_KEYS)
    city   = get_first_by_keys(n, CITY_KEYS)
    state  = get_first_by_keys(n, STATE_KEYS)
    zipc   = get_first_by_keys(n, ZIP_KEYS)
    county = get_first_by_keys(n, COUNTY_KEYS)

    street_parts = [x for x in [num, name, suf] if x]
    street_raw = " ".join(street_parts).strip()

    return {
        "street_raw": street_raw, "city": city, "state": state, "zip": zipc, "county": county,
        "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
        "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)
    }

# =========================
# Land-aware cleanup + variants
# =========================
LAND_LEAD_TOKENS = {"lot","lt","tract","parcel","blk","block","tbd"}
HWY_EXPAND = {
    r"\bhwy\b": "highway",
    r"\bus\b": "US",
    r"\bnc\b": "NC",
    r"\bsr\b": "state route",
    r"\brt\b": "route",
    r"\brd\b": "road",
    r"\bdr\b": "drive",
    r"\bave\b": "avenue",
    r"\bct\b": "court",
    r"\bln\b": "lane",
    r"\bpkwy\b": "parkway",
    r"\bsq\b": "square",
    r"\bcir\b": "circle",
}
DIR_MAP = {'s':'south','n':'north','e':'east','w':'west'}
LOT_REGEX = re.compile(r'\b(?:lot|lt)\s*[-#:]?\s*([A-Za-z0-9]+)\b', re.I)

def clean_land_street(street:str) -> str:
    """A 'clean' form (for some variants). We will ALSO keep LOT-preserving variants elsewhere."""
    if not street: return street
    s = street.strip()
    s = re.sub(r"^\s*0[\s\-]+", "", s)  # drop leading "0 "
    tokens = re.split(r"[\s\-]+", s)
    if tokens and tokens[0].lower() in LAND_LEAD_TOKENS:
        tokens = tokens[1:]
        if tokens and re.match(r"^(?:[A-Za-z]|\d+|one|two|three|four|five|six|seven|eight|nine|ten)$", tokens[0], re.I):
            tokens = tokens[1:]
        s = " ".join(tokens)
    s_lower = f" {s.lower()} "
    for pat, repl in HWY_EXPAND.items():
        s_lower = re.sub(pat, f" {repl} ", s_lower)
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
    """
    Yield multiple street variants (keep & strip LOT; Hwy/Highway; US/U.S.; S/South),
    each appended with city/state/zip if available.
    """
    city = (city or defaults.get("city","")).strip()
    st   = (state or defaults.get("state","")).strip()
    z    = (zipc or defaults.get("zip","")).strip()

    base = (street or "").strip()

    # detect lot number (if present)
    lot_match = LOT_REGEX.search(base)
    lot_num = lot_match.group(1) if lot_match else None

    # normalize core tokens for mixing
    core = base
    core = re.sub(r'\bu\.?s\.?\b', 'US', core, flags=re.I)
    core = re.sub(r'\bhwy\b', 'highway', core, flags=re.I)
    core = re.sub(r'\b([NSEW])\b', lambda m: DIR_MAP.get(m.group(1).lower(), m.group(1)), core, flags=re.I)

    # core variants
    variants = set()
    variants.add(core)
    variants.add(re.sub(r'\bhighway\b', 'hwy', core, flags=re.I))
    variants.add(re.sub(r'\bUS\b', 'U.S.', core, flags=re.I))
    variants.add(re.sub(r'\bUS\b\s*(\d+)', r'US-\1', core, flags=re.I))  # US-301

    # short directions
    variants |= { re.sub(r'\bsouth\b', 's', v, flags=re.I) for v in list(variants) }

    # LOT-preserved
    lot_variants = set(variants)
    if lot_num:
        for v in list(variants):
            lot_variants.add(f"lot {lot_num} {v}")
            lot_variants.add(f"{v} lot {lot_num}")
            lot_variants.add(f"lot-{lot_num} {v}")

    # LOT-stripped
    stripped_variants = { LOT_REGEX.sub('', v).strip() for v in list(lot_variants) }

    all_street_variants = set()
    all_street_variants |= lot_variants
    all_street_variants |= stripped_variants

    # attach location
    out = []
    for sv in all_street_variants:
        parts = [sv]
        if city: parts.append(city)
        if st:   parts.append(st)
        if z:    parts.append(z)
        out.append(" ".join(p for p in parts if p))
    # unique + keep order
    return [s for s in dict.fromkeys(out) if s.strip()]

# =========================
# Bing search helpers
# =========================
def _slug(text:str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

def url_matches_city_state(url:str, city:str=None, state:str=None) -> bool:
    u = (url or '')
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
    if not BING_API_KEY: return []
    h = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
    try:
        if BING_CUSTOM_ID:
            p = {"q": query, "customconfig": BING_CUSTOM_ID, "mkt": "en-US", "count": 15}
            r = requests.get(BING_CUSTOM, headers=h, params=p, timeout=REQUEST_TIMEOUT)
        else:
            p = {"q": query, "mkt": "en-US", "count": 15, "responseFilter": "Webpages"}
            r = requests.get(BING_WEB, headers=h, params=p, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("webPages", {}).get("value") if "webPages" in data else data.get("items", []) or []
    except requests.RequestException:
        return []

# =========================
# Fetch + confirm on-page (MLS or City/State)
# =========================
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
def _fetch(url, timeout=REQUEST_TIMEOUT):
    return requests.get(url, headers=UA_HEADERS, timeout=timeout)

MLS_HTML_PATTERNS = [
    lambda mid: rf'\bMLS[^A-Za-z0-9]{{0,5}}#?\s*{re.escape(mid)}\b',
    lambda mid: rf'\bMLS\s*#?\s*{re.escape(mid)}\b',
    lambda mid: rf'"mls"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*"{re.escape(mid)}"',
    lambda mid: rf'"mlsId"\s*:\s*{re.escape(mid)}',
]

def page_contains_mls(html:str, mls_id:str) -> bool:
    for mk in MLS_HTML_PATTERNS:
        if re.search(mk(mls_id), html, re.I):
            return True
    return False

def page_contains_city_state(html:str, city:str=None, state:str=None) -> bool:
    ok = False
    if city and re.search(re.escape(city), html, re.I):
        ok = True
    if state and re.search(rf'\b{re.escape(state)}\b', html, re.I):
        ok = True
    return ok

def confirm_or_resolve_on_page(url:str, mls_id:str=None, required_city:str=None, required_state:str=None) -> Tuple[Optional[str], Optional[str]]:
    """
    Accept if:
      - the page contains the MLS id ("mls_match"), OR
      - the page contains the required city/state tokens ("city_state_match").
    If it's a search (_rb) page, also try the first few homedetails links within.
    Returns: (accepted_url, match_type | None)
    """
    try:
        r = _fetch(url); r.raise_for_status()
        html = r.text

        # Direct accept?
        if mls_id and page_contains_mls(html, mls_id):
            return url, "mls_match"
        if page_contains_city_state(html, required_city, required_state):
            if "/homedetails/" in url:
                return url, "city_state_match"

        # If search page, drill down
        if url.endswith("_rb/") and "/homedetails/" not in url:
            cand = re.findall(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)[:8]
            for u in cand:
                try:
                    rr = _fetch(u); rr.raise_for_status()
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

# =========================
# Resolvers (MLS-first & variants)
# =========================
def find_zillow_by_mls_with_confirmation(mls_id, required_state=None, required_city=None, mls_name=None, delay=0.35, require_match=False, max_candidates=20):
    """Search Bing for MLS id, collect Zillow candidates, open pages and confirm MLS OR City/State appears."""
    if not (BING_API_KEY and mls_id): return None, None
    q_mls = [
        f'"MLS# {mls_id}" site:zillow.com',
        f'"{mls_id}" "MLS" site:zillow.com',
        f'{mls_id} site:zillow.com/homedetails',
    ]
    if mls_name:
        q_mls = [f'{q} "{mls_name}"' for q in q_mls] + q_mls

    seen, candidates = set(), []
    for q in q_mls:
        items = bing_search_items(q)
        for it in items:
            url = it.get("url") or it.get("link") or ""
            if not url or "zillow.com" not in url: continue
            if "/homedetails/" not in url and "/homes/" not in url: continue
            if require_match and not url_matches_city_state(url, required_city, required_state): 
                continue
            if url in seen: continue
            seen.add(url); candidates.append(url)
            if len(candidates) >= max_candidates: break
        if len(candidates) >= max_candidates: break

    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok:
            return ok, mtype or "mls_match"
    return None, None

def resolve_homedetails_with_bing_variants(address_variants, required_state=None, required_city=None, mls_id=None, delay=0.3, require_match=False):
    """Try multiple address variants; collect candidates; confirm MLS OR City/State on page."""
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
            mls_q = [
                f'"MLS# {mls_id}" site:zillow.com/homedetails',
                f'{mls_id} site:zillow.com/homedetails',
                f'"{mls_id}" "MLS" site:zillow.com/homedetails',
            ]
            queries = mls_q + queries

        for q in queries:
            items = bing_search_items(q)
            for it in items:
                url = it.get("url") or it.get("link") or ""
                if not url or "zillow.com" not in url: continue
                if "/homedetails/" not in url and "/homes/" not in url: continue
                if require_match and not url_matches_city_state(url, required_city, required_state):
                    continue
                if url in seen: continue
                seen.add(url); candidates.append(url)
            time.sleep(delay)

    # Confirm per-candidate
    for u in candidates:
        time.sleep(delay)
        ok, mtype = confirm_or_resolve_on_page(u, mls_id=mls_id, required_city=required_city, required_state=required_state)
        if ok:
            return ok, mtype or "city_state_match"
    return None, None

# =========================
# Azure (optional)
# =========================
def azure_search_first_zillow(query_address):
    if not (AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX and AZURE_SEARCH_KEY):
        return None
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
            if isinstance(v, str) and "zillow.com" in v:
                return v
    except requests.RequestException:
        return None
    return None

# =========================
# Images (best-effort)
# =========================
def fetch_og_image(url):
    """Return an og:image (or similar) from homedetails page if present."""
    try:
        r = _fetch(url); r.raise_for_status()
        html = r.text
        for pat in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
            r'"image"\s*:\s*"(https?://[^"]+)"',
            r'"image"\s*:\s*\[\s*"(https?://[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m: return m.group(1)
    except Exception:
        return None
    return None

def street_view_image(addr):
    """Optional fallback via Google Street View Static API."""
    if not GOOGLE_MAPS_API_KEY: return None
    from urllib.parse import quote_plus
    loc = quote_plus(addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=400x300&location={loc}&key={GOOGLE_MAPS_API_KEY}"

def picture_for_result(query_address, zurl):
    """Prefer homedetails og:image; else Street View."""
    img = None
    if zurl and "/homedetails/" in zurl:
        img = fetch_og_image(zurl)
    if not img:
        img = street_view_image(query_address)
    return img

@st.cache_data(ttl=3600, show_spinner=False)
def get_thumbnail(query_address, zurl):
    return picture_for_result(query_address, zurl)

# =========================
# Deeplink builder (always appends location if available)
# =========================
def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    """Zillow search deeplink that includes city/state (and zip when present)."""
    c = (city or defaults.get("city","")).strip()
    st_abbr = (state or defaults.get("state","")).strip()
    z = (zipc or defaults.get("zip","")).strip()

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
    a = re.sub(r"[^\w\s,-]", "", a)
    a = a.replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# =========================
# Output builders
# =========================
def build_output(rows: List[Dict[str, Any]], fmt: str) -> Tuple[str, str]:
    """
    Build downloadable output. TXT and MD are bulleted and DO NOT include notes
    (for client-facing copy/paste). HTML/CSV unchanged.
    """
    if fmt == "csv":
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["input_address","mls_id","zillow_url","note","status"])
        w.writeheader(); w.writerows(rows)
        return s.getvalue(), "text/csv"

    if fmt == "md":
        lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
        return ("\n".join(lines) + "\n"), "text/markdown"

    if fmt == "html":
        items = [f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{r["zillow_url"]}</a></li>'
                 for r in rows if r.get("zillow_url")]
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    # txt (bulleted, no notes)
    if fmt == "txt":
        lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
        return ("\n".join(lines) + "\n"), "text/plain"

# Build a ZIP with all formats
def build_zip_all_formats(rows: List[Dict[str, Any]]) -> bytes:
    fmts = ["txt","csv","md","html"]
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for fmt in fmts:
            payload, mime = build_output(rows, fmt)
            zf.writestr(f"zillow_links_{ts}.{fmt}", payload)
    mem.seek(0)
    return mem.read()

# =========================
# Core processing (single row)
# =========================
def process_single_row(row, delay, land_mode, defaults, require_state, mls_first, default_mls_name, max_candidates) -> Dict[str, Any]:
    comp = extract_components(row)

    street_raw = comp["street_raw"]
    street_clean = clean_land_street(street_raw) if land_mode else street_raw

    # Build address variants from ORIGINAL (preserving LOT), plus city/state/zip
    variants = generate_address_variants(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    if land_mode:
        variants = list(dict.fromkeys(
            variants + generate_address_variants(street_clean, comp["city"], comp["state"], comp["zip"], defaults)
        ))

    # Use first variant for display/StreetView
    query_address = variants[0] if variants else compose_query_address(street_raw, comp["city"], comp["state"], comp["zip"], defaults)

    # Build a LOT-preserving deeplink that **always** appends location if provided in row/defaults
    deeplink = construct_deeplink_from_parts(street_raw, comp["city"], comp["state"], comp["zip"], defaults)

    # Optional internal note (not shown in client outputs)
    note = ""
    if not ((comp["city"] or defaults.get("city")) and (comp["state"] or defaults.get("state"))):
        note = "No city/state provided — deeplink is nationwide search."

    # Filters for search
    required_state_val = defaults.get("state") if require_state else None
    required_city_val  = comp["city"] or defaults.get("city")

    zurl, status = None, "fallback"
    mls_id   = (comp.get("mls_id") or "").strip()
    mls_name = (comp.get("mls_name") or default_mls_name or "").strip()

    # 1) MLS-first with on-page confirmation (MLS or City/State)
    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id, required_state=required_state_val, required_city=required_city_val,
            mls_name=mls_name, delay=min(delay, 0.6), require_match=require_state,
            max_candidates=max_candidates
        )
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"

    # 2) Azure (optional)
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z:
            zurl, status = z, "azure_hit"

    # 3) Address-based (variants) with page confirmation bias
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants, required_state=required_state_val, required_city=required_city_val,
            mls_id=mls_id or None, delay=min(delay, 0.6), require_match=require_state
        )
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"

    # 4) Fallback
    if not zurl:
        zurl, status = deeplink, "deeplink_fallback"

    time.sleep(min(delay, 0.5))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "note": note, "status": status}

# =========================
# Deterministic caching
# =========================
def run_key(rows: List[Dict[str, Any]], settings: dict) -> str:
    # Keep only stable keys to avoid massive hash variance
    safe_rows = [{k:str(v) for k,v in r.items()} for r in rows]
    blob = json.dumps({"rows":safe_rows, "settings":settings}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()

@st.cache_data(ttl=3600, show_spinner=False)
def cached_process(rows: List[Dict[str, Any]], settings: dict) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        out.append(process_single_row(r, **settings))
    return out

# =========================
# Batched processing with progress + optional retry queue
# =========================
def process_rows_batched(rows: List[Dict[str, Any]], settings: dict, chunk_size: int = 10, retry: bool = True):
    results: List[Dict[str, Any]] = []
    total = len(rows)
    p = st.progress(0, text="Processing…")

    for i, row in enumerate(rows, start=1):
        res = process_single_row(row, **settings)
        results.append(res)
        p.progress(i/total, text=f"Processed {i}/{total}")

        # modest pause between chunks to be rate-limit friendly
        if i % chunk_size == 0:
            time.sleep(0.4)

    # Retry queue: anything that fell back to deeplink, try again with slower delay
    if retry:
        to_retry_idx = [idx for idx, r in enumerate(results) if r["status"] in ("deeplink_fallback")]
        if to_retry_idx:
            st.info(f"Retrying {len(to_retry_idx)} tough rows with a slower delay…")
            slow_settings = settings.copy()
            slow_settings["delay"] = max(settings.get("delay", 0.5), 1.0)
            for idx in to_retry_idx:
                results[idx] = process_single_row(rows[idx], **slow_settings)
                p.progress((idx+1)/total, text=f"Retry {idx+1}/{total}")

    p.progress(1.0, text="Done")
    return results

# =========================
# UI Helpers
# =========================
def secrets_banner():
    missing = []
    if not BING_API_KEY: missing.append("BING_API_KEY")
    if not GOOGLE_MAPS_API_KEY: missing.append("GOOGLE_MAPS_API_KEY (for Street View fallback)")
    # Azure is optional
    if missing:
        st.warning("Missing keys: " + ", ".join(missing) + ". The app will still work, but results or thumbnails may be limited.", icon="⚠️")

def validate_state(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if not re.match(r"^[A-Za-z]{2}$", s):
        st.error("Default State must be a 2-letter abbreviation (e.g., NC).")
        return ""
    return s.upper()

def status_chip(status: str) -> str:
    # lightweight chips
    label = {
        "mls_match":"✅ MLS match",
        "city_state_match":"🟩 City/State match",
        "azure_hit":"🟦 Azure hit",
        "deeplink_fallback":"⚠️ Fallback search",
        "fallback":"⚠️ Fallback",
    }.get(status, "ℹ️")
    color = {
        "mls_match":"#16a34a",
        "city_state_match":"#22c55e",
        "azure_hit":"#3b82f6",
        "deeplink_fallback":"#f59e0b",
        "fallback":"#f59e0b",
    }.get(status, "#6b7280")
    return f"""<span style="
        display:inline-block;
        padding:2px 8px; border-radius:999px;
        background:{color}33; color:{color}; font-size:12px; font-weight:600;">
        {label}</span>"""

def card(md: str):
    st.markdown(
        f"""
        <div style="border:1px solid rgba(49,51,63,.2); border-radius:12px; padding:12px; margin-bottom:10px;">
            {md}
        </div>
        """,
        unsafe_allow_html=True
    )

# =========================
# Sidebar (controls)
# =========================
with st.sidebar:
    st.header("Settings")
    fmt   = st.selectbox("Download format", ["txt","csv","md","html"], index=0)
    delay = st.slider("Delay between lookups (seconds)", 0.0, 2.0, 0.5, 0.1,
                      help="Use higher values if you hit rate limits.")
    land_mode = st.checkbox("Optimize for land/LOT listings", value=True,
                            help="Cleans LOT/highway tokens and tries LOT-preserving variants.")
    require_state = st.checkbox("Only accept results matching the State", value=True,
                                help="Safer but may reduce matches in some cases.")
    mls_first = st.checkbox("Prefer MLS ID matching", value=True)
    default_city  = st.text_input("Default City", value="")
    default_state = validate_state(st.text_input("Default State (2 letters)", value=""))
    default_zip   = st.text_input("Default Zip", value="")
    default_mls_name = st.text_input("Default MLS Board (e.g., TMLS)", value="")
    max_candidates = st.number_input("Max MLS candidates to check", min_value=5, max_value=40, value=20, step=1)

    st.divider()
    st.caption("Advanced")
    batch_size = st.number_input("Batch size", min_value=5, max_value=50, value=10, step=1)
    retry_harder = st.checkbox("Retry fallbacks with slower delay", value=True)
    run = st.button("▶️ Run lookup", use_container_width=True)

# =========================
# Top-level UI
# =========================
st.title("🏠 Zillow Deeplink Finder (MLS-confirmed, land-aware)")
st.caption("Upload → Review → Download. MLS confirmation, LOT-safe variants, and location-appended deeplinks.")

secrets_banner()

tab_upload, tab_results, tab_debug = st.tabs(["Upload CSV", "Results", "Debug"])

# Template + paste option
with tab_upload:
    st.download_button(
        "📄 Download CSV template",
        data="address,city,state,zip,mls_id,mls_name\n123 Main St,Anytown,NC,27601,123456,TMLS\n",
        file_name="zillow_template.csv", mime="text/csv"
    )

    with st.expander("No CSV? Paste addresses (one per line)"):
        pasted = st.text_area("Addresses", placeholder="407 E Woodall St, Smithfield, NC 27577\n13 Herndon Ct, Clayton, NC 27520")
        if pasted and st.button("Convert to CSV rows"):
            rows = [{"address": line.strip()} for line in pasted.splitlines() if line.strip()]
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["address"])
            w.writeheader()
            for r in rows: w.writerow(r)
            st.download_button("Download generated CSV", data=s.getvalue(), file_name="addresses.csv", mime="text/csv")

    file = st.file_uploader("Upload CSV (must include a header row)", type=["csv"])

    if "results_bundle" not in st.session_state:
        st.session_state["results_bundle"] = None

    if file and run:
        try:
            raw = file.read().decode("utf-8-sig")
            rows_in = list(csv.DictReader(io.StringIO(raw)))
            if not rows_in:
                st.error("No rows detected. Make sure your CSV has a header row and at least one line of data.")
            else:
                # Settings pack
                settings = dict(
                    delay=delay,
                    land_mode=land_mode,
                    defaults={"city": default_city.strip(), "state": default_state.strip(), "zip": default_zip.strip()},
                    require_state=require_state,
                    mls_first=mls_first,
                    default_mls_name=default_mls_name.strip(),
                    max_candidates=int(max_candidates)
                )

                # Cache key (for deterministic caching)
                key = run_key(rows_in, settings)

                with st.status("Processing rows…", expanded=True) as status:
                    st.write(f"Loaded **{len(rows_in)}** rows.")
                    # Prefer cached results when available
                    try_cached = cached_process(rows_in, settings) if delay <= 0.6 and not retry_harder else None
                    if try_cached:
                        results = try_cached
                    else:
                        results = process_rows_batched(rows_in, settings, chunk_size=int(batch_size), retry=retry_harder)

                    status.update(label="Building downloads…", state="running")
                    # Primary single-format payload
                    payload, mime = build_output(results, fmt)
                    # Multi-format ZIP
                    zip_bytes = build_zip_all_formats(results)
                    status.update(label="Done ✅", state="complete")

                # Save to session
                st.session_state["results_bundle"] = {
                    "rows_in": rows_in,
                    "results": results,
                    "payload": payload,
                    "mime": mime,
                    "fmt": fmt,
                    "zip_bytes": zip_bytes
                }
                st.toast(f"Processed {len(results)} rows", icon="✅")
        except Exception as e:
            st.error("We hit a snag while processing your file.")
            with st.expander("Show error details"):
                st.exception(e)

with tab_results:
    data = st.session_state.get("results_bundle")
    if not data:
        st.info("Upload a CSV and click **Run lookup** to see results.")
    else:
        results = data["results"]
        fmt = data["fmt"]

        # Sticky downloads
        dl_col1, dl_col2 = st.columns([1,1])
        with dl_col1:
            st.download_button("⬇️ Download result",
                data=data["payload"],
                file_name=f"zillow_links_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.{fmt}",
                mime=data["mime"], use_container_width=True)
        with dl_col2:
            st.download_button("🗂️ Download all formats (.zip)",
                data=data["zip_bytes"],
                file_name=f"zillow_links_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.zip",
                mime="application/zip", use_container_width=True)

        st.markdown("### Visual grid")
        cols = st.columns(3)
        for i, r in enumerate(results):
            img_url = get_thumbnail(r["input_address"], r["zillow_url"])
            badge = status_chip(r.get("status",""))
            body = f"""
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div><strong>{('MLS#: ' + r['mls_id']) if r.get('mls_id') else 'Listing'}</strong></div>
                    <div>{badge}</div>
                </div>
                <div style="margin:6px 0;"><a href="{r['zillow_url']}" target="_blank" rel="noopener">Open on Zillow</a></div>
                <div style="color:#9aa0a6; font-size:12px;">{r['input_address']}</div>
            """
            with cols[i % 3]:
                if img_url:
                    try:
                        st.image(img_url, use_column_width=True)
                    except Exception:
                        pass
                card(body)

        # Sortable table view
        st.markdown("### Table view")
        try:
            import pandas as pd
            df = pd.DataFrame(results)
            df = df.rename(columns={
                "input_address":"Input Address",
                "mls_id":"MLS ID",
                "zillow_url":"Zillow URL",
                "note":"Note",
                "status":"Status"
            })
            st.dataframe(df[["Status","MLS ID","Input Address","Zillow URL"]], use_container_width=True, hide_index=True)
        except Exception:
            st.write("Table view unavailable.")

        # Copy helpers (for TXT/MD)
        st.markdown("### Copy-friendly previews")
        for want_fmt in ("txt","md"):
            payload, _ = build_output(results, want_fmt)
            st.caption(f"{want_fmt.upper()} preview (copy from below):")
            st.text_area(f"{want_fmt.upper()} output", payload, height=150, label_visibility="collapsed")

with tab_debug:
    data = st.session_state.get("results_bundle")
    if not data:
        st.info("Run a file first to see debug info.")
    else:
        defaults = {
            "city": default_city.strip(),
            "state": default_state.strip(),
            "zip": default_zip.strip(),
        }
        results = data["results"]
        rows_in = data["rows_in"]

        st.markdown("#### Diagnostics summary")
        verify = st.checkbox("Verify pages live (slow: fetch each final URL)")

        def has_token(s, token):
            return (token and token.lower() in (s or "").lower())

        dbg_rows = []
        for r in results:
            url = r["zillow_url"]
            is_homedetails = ("/homedetails/" in url)
            city = defaults.get("city")
            state = defaults.get("state")
            row_dbg = {
                "MLS": r.get("mls_id") or "",
                "Final URL": url,
                "Type": "homedetails" if is_homedetails else "_rb search",
                "City token in URL": "yes" if has_token(url, city) else "no",
                "State token in URL": "yes" if (state and (f"-{state.lower()}-" in url or f"/{state.lower()}/" in url)) else "no",
                "Status": r.get("status",""),
                "Note": r.get("note") or ""
            }
            if verify:
                try:
                    resp = requests.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
                    html = resp.text if resp.ok else ""
                    onpage_city = "yes" if (city and re.search(re.escape(city), html, re.I)) else "no"
                    onpage_state = "yes" if (state and re.search(rf'\b{re.escape(state)}\b', html, re.I)) else "no"
                    mls = r.get("mls_id") or ""
                    onpage_mls = "n/a"
                    if mls:
                        onpage_mls = "yes" if re.search(rf'\bMLS[^A-Za-z0-9]{{0,5}}#?\s*{re.escape(mls)}\b', html, re.I) else "no"
                    row_dbg.update({"On-page City": onpage_city, "On-page State": onpage_state, "On-page MLS": onpage_mls})
                except Exception:
                    row_dbg.update({"On-page City":"err","On-page State":"err","On-page MLS":"err"})
            dbg_rows.append(row_dbg)

        try:
            import pandas as pd
            st.dataframe(pd.DataFrame(dbg_rows), use_container_width=True)
        except Exception:
            st.write(dbg_rows)

        st.caption("Address variants (first 5 rows):")
        try:
            for i, raw_row in enumerate(rows_in[:5]):
                comp = extract_components(raw_row)
                street_raw = comp["street_raw"]
                street_clean = clean_land_street(street_raw) if land_mode else street_raw
                v = generate_address_variants(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
                if land_mode:
                    v = list(dict.fromkeys(v + generate_address_variants(street_clean, comp["city"], comp["state"], comp["zip"], defaults)))
                st.write(f"Row {i+1} — street_raw: `{street_raw}`")
                st.code("\n".join(v[:6]) or "(no variants)", language="text")
        except Exception:
            st.write("Variant preview unavailable.")
