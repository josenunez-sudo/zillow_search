# app.py — Address Alchemist
# - AVIF favicon (/mnt/data/link.avif) via pillow-avif-plugin
# - Title uses Barlow Condensed (white)
# - Minimal input area with live count, de-dup, trim, preview
# - Clickable bulleted results (open in new tab)
# - Images section shows if ANY item has an image
# - Image selection priority: CSV Photo > Zillow hero > og:image > Street View
# - Image Log includes CSV presence, chosen stage, errors
# - Safe rerender with remembered format and filenames

import os, csv, io, re, time, json
from datetime import datetime
from typing import List, Dict, Any, Optional
from html import escape

import requests
import streamlit as st
from PIL import Image

# ----------------------------
# Favicon: load AVIF and convert to PNG bytes for Streamlit
# ----------------------------
# requirements.txt must include: pillow-avif-plugin>=1.4.7
try:
    import pillow_avif  # noqa: F401  # registers AVIF with Pillow
except Exception:
    pillow_avif = None

def _page_icon_from_avif(path: str):
    if not os.path.exists(path):
        return "⚗️"  # fallback emoji
    try:
        im = Image.open(path)
        im.load()
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return "⚗️"

# ----------------------------
# Page setup & styles
# ----------------------------
st.set_page_config(
    page_title="Address Alchemist",
    page_icon=_page_icon_from_avif("/mnt/data/link.avif"),
    layout="centered",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&display=swap');

.block-container { max-width: 760px; }
.center-box { border: 1px solid rgba(0,0,0,.08); border-radius: 12px; padding: 16px; }

.app-title {
  font-family: 'Barlow Condensed', system-ui, -apple-system, Segoe UI, Roboto, 'Helvetica Neue', Arial, sans-serif;
  font-weight: 800;
  font-size: 2.1rem;
  line-height: 1.1;
  letter-spacing: -0.01em;
  color: #ffffff;
  margin: 0 0 8px 0;
  text-shadow: 0 1px 2px rgba(0,0,0,.25);
}
.app-sub { color: #6b7280; margin: 0 0 12px 0; }

.small { color: #6b7280; font-size: 12.5px; margin-top: 6px; }
ul.link-list { margin: 0 0 .5rem 1.2rem; padding: 0; }
ul.link-list li { margin: 0.2rem 0; }

.stButton>button {
  width: 100%;
  height: 48px;
  padding: 0.65rem 1rem;
  border-radius: 12px;
  border: 0;
  color: #fff !important;
  font-weight: 700;
  letter-spacing: .02em;
  background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%);
  box-shadow: 0 8px 24px rgba(37,99,235,.35), 0 2px 6px rgba(0,0,0,.12);
  transition: transform .06s ease, box-shadow .2s ease, filter .2s ease;
}
.stButton>button:hover {
  transform: translateY(-1px);
  box-shadow: 0 10px 28px rgba(37,99,235,.45), 0 2px 8px rgba(0,0,0,.14);
  filter: brightness(1.03);
}
.stButton>button:active {
  transform: translateY(0);
  filter: brightness(.98);
  box-shadow: 0 6px 18px rgba(37,99,235,.35), 0 1px 4px rgba(0,0,0,.12);
}
.stButton>button:focus-visible {
  outline: 3px solid #93c5fd;
  outline-offset: 2px;
}

.stDownloadButton>button {
  width: 100%;
  height: 48px;
  padding: 0.65rem 1rem;
  border-radius: 12px;
  border: 0;
  color: #fff !important;
  font-weight: 700;
  letter-spacing: .02em;
  background: linear-gradient(180deg, #16a34a 0%, #15803d 100%);
  box-shadow: 0 8px 24px rgba(22,163,74,.35), 0 2px 6px rgba(0,0,0,.12);
  transition: transform .06s ease, box-shadow .2s ease, filter .2s ease;
}
.stDownloadButton>button:hover {
  transform: translateY(-1px);
  box-shadow: 0 10px 28px rgba(22,163,74,.45), 0 2px 8px rgba(0,0,0,.14);
  filter: brightness(1.03);
}
.stDownloadButton>button:active {
  transform: translateY(0);
  filter: brightness(.98);
  box-shadow: 0 6px 18px rgba(22,163,74,.35), 0 1px 4px rgba(0,0,0,.12);
}
.stDownloadButton>button:focus-visible {
  outline: 3px solid #86efac;
  outline-offset: 2px;
}

textarea { border-radius: 10px !important; }
textarea:focus { outline: 3px solid #93c5fd !important; outline-offset: 2px; }
[data-testid="stFileUploadClearButton"] { display: none !important; }
</style>
""", unsafe_allow_html=True)

# ----------------------------
# Env/secrets (optional)
# ----------------------------
for k in ["AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY",
          "BING_API_KEY","BING_CUSTOM_CONFIG_ID","GOOGLE_MAPS_API_KEY"]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT","").rstrip("/")
AZURE_SEARCH_INDEX    = os.getenv("AZURE_SEARCH_INDEX","")
AZURE_SEARCH_KEY      = os.getenv("AZURE_SEARCH_API_KEY","")
BING_API_KEY          = os.getenv("BING_API_KEY","")
BING_CUSTOM_ID        = os.getenv("BING_CUSTOM_CONFIG_ID","")
GOOGLE_MAPS_API_KEY   = os.getenv("GOOGLE_MAPS_API_KEY","")
REQUEST_TIMEOUT       = 12

# ----------------------------
# Address parsing + helpers
# ----------------------------
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

# Land/LOT handling
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
        tokens = tokens[1:]
        s = " ".join(tokens)
    s_lower = f" {s.lower()} "
    for pat,repl in HWY_EXPAND.items():
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

# ----------------------------
# Web fetch helpers
# ----------------------------
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

BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"

def bing_search_items(query):
    key = os.getenv("BING_API_KEY",""); custom = os.getenv("BING_CUSTOM_CONFIG_ID","")
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
        if re.search(mk(mls_id), html, re.I): return True
    return False

def page_contains_city_state(html:str, city:str=None, state:str=None) -> bool:
    ok = False
    if city and re.search(re.escape(city), html, re.I): ok = True
    if state and re.search(rf'\b{re.escape(state)}\b', html, re.I): ok = True
    return ok

def confirm_or_resolve_on_page(url:str, mls_id:str=None, required_city:str=None, required_state:str=None):
    try:
        r = _fetch(url); r.raise_for_status()
        html = r.text
        if mls_id and page_contains_mls(html, mls_id):
            return url, "mls_match"
        if page_contains_city_state(html, required_city, required_state) and "/homedetails/" in url:
            return url, "city_state_match"
        if url.endswith("_rb/") and "/homedetails/" not in url:
            cand = re.findall(r'href="(https://www\\.zillow\\.com/homedetails/[^"]+)"', html)[:8]
            for u in cand:
                try:
                    rr = _fetch(u); rr.raise_for_status()
                    h2 = rr.text
                    if (mls_id and page_contains_mls(h2, mls_id)): return u, "mls_match"
                    if page_contains_city_state(h2, required_city, required_state): return u, "city_state_match"
                except Exception:
                    continue
    except Exception:
        return None, None
    return None, None

def find_zillow_by_mls_with_confirmation(mls_id, required_state=None, required_city=None, mls_name=None, delay=0.35, require_match=False, max_candidates=20):
    key = os.getenv("BING_API_KEY","")
    if not (key and mls_id): return None, None
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
    key = os.getenv("BING_API_KEY","")
    if not key: return None, None
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
                if require_match and not url_matches_city_state(url, required_city, required_state):
                    continue
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
    z = (zipc or defaults.get("zip","")).strip()
    slug_parts = [street]
    loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts: slug_parts.append(", ".join(loc_parts))
    if z:
        if slug_parts: slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else: slug_parts.append(z)
    slug = ", ".join(slug_parts)
    a = slug.lower(); a = re.sub(r"[^\w\s,-]", "", a).replace(",", ""); a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ----------------------------
# Zillow-first image helper
# ----------------------------
def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html:
        return None
    for target_w in ("960", "1152", "768", "1536"):
        m = re.search(
            rf'<img[^>]+src=["\'](https://photos\.zillowstatic\.com/fp/[^"\']+-cc_ft_{target_w}\.(?:jpg|webp))["\']',
            html, re.I
        )
        if m:
            return m.group(1)
    srcset_match = re.search(
        r'srcset=["\']([^"\']*photos\.zillowstatic\.com[^"\']+)["\']',
        html, re.I
    )
    if srcset_match:
        srcset = srcset_match.group(1)
        candidates = []
        for part in srcset.split(","):
            part = part.strip()
            m = re.match(r'(https://photos\.zillowstatic\.com/\S+)\s+(\d+)w', part, re.I)
            if m:
                url, w = m.group(1), int(m.group(2))
                candidates.append((w, url))
        if candidates:
            up_to = [u for (w,u) in candidates if w <= 1152]
            if up_to:
                return sorted(((w,u) for (w,u) in candidates if w <= 1152), key=lambda x: x[0])[-1][1]
            return sorted(candidates, key=lambda x: x[0])[-1][1]
    m = re.search(r'(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(?:jpg|webp))', html, re.I)
    if m:
        return m.group(1)
    return None

# ----------------------------
# Single-row pipeline (now captures CSV photo)
# ----------------------------
def process_single_row(row, *, delay=0.5, land_mode=True, defaults=None,
                       require_state=True, mls_first=True, default_mls_name="", max_candidates=20):
    defaults = defaults or {"city":"", "state":"", "zip":""}

    # Capture CSV photo URL if provided (header: Photo/Image/etc.)
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)

    comp = extract_components(row)
    street_raw = comp["street_raw"]
    street_clean = clean_land_street(street_raw) if land_mode else street_raw

    variants = generate_address_variants(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    if land_mode:
        variants = list(dict.fromkeys(variants + generate_address_variants(street_clean, comp["city"], comp["state"], comp["zip"], defaults)))

    query_address = variants[0] if variants else compose_query_address(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    deeplink = construct_deeplink_from_parts(street_raw, comp["city"], comp["state"], comp["zip"], defaults)

    note = ""
    if not ((comp["city"] or defaults.get("city")) and (comp["state"] or defaults.get("state"))):
        note = "No city/state provided — deeplink is nationwide search."

    required_state_val = defaults.get("state") if require_state else None
    required_city_val  = comp["city"] or defaults.get("city")

    zurl, status = None, "fallback"
    mls_id   = (comp.get("mls_id") or "").strip()
    mls_name = (comp.get("mls_name") or default_mls_name or "").strip()

    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id, required_state=required_state_val, required_city=required_city_val,
            mls_name=mls_name, delay=min(delay, 0.6), require_match=require_state,
            max_candidates=max_candidates
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
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"

    if not zurl:
        zurl, status = deeplink, "deeplink_fallback"

    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "note": note, "status": status,
            "csv_photo": csv_photo}

# ----------------------------
# Image selection + LOGGING (CSV first)
# ----------------------------
def picture_for_result_with_log(query_address: str, zurl: str, csv_photo_url: Optional[str] = None):
    """
    Returns (image_url: Optional[str], log: Dict[str, Any])
    log fields: url, csv_provided(bool), stage, status_code, html_len, selected, errors[list]
    """
    log = {"url": zurl, "csv_provided": bool(csv_photo_url), "stage": None,
           "status_code": None, "html_len": None, "selected": None, "errors": []}

    # 0) CSV Photo (if provided)
    def _looks_like_url(u: str) -> bool:
        return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))

    if csv_photo_url and _looks_like_url(csv_photo_url):
        log["stage"] = "csv_photo"
        log["selected"] = csv_photo_url
        return csv_photo_url, log

    # 1) Zillow page hero / og:image
    if zurl and "/homedetails/" in zurl:
        try:
            r = _fetch(zurl)
            log["status_code"] = r.status_code
            if r.ok:
                html = r.text
                log["html_len"] = len(html)
                zfirst = extract_zillow_first_image(html)
                if zfirst:
                    log["stage"] = "zillow_hero"
                    log["selected"] = zfirst
                    return zfirst, log

                # Fall back to og:image / JSON image hints
                for pat in [
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                    r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)["\']',
                    r'"image"\s*:\s*"(https?://[^"]+)"',
                    r'"image"\s*:\s*\[\s*"(https?://[^"]+)"',
                ]:
                    m = re.search(pat, html, re.I)
                    if m:
                        log["stage"] = "og_image"
                        log["selected"] = m.group(1)
                        return m.group(1), log
            else:
                log["errors"].append(f"http_error:{r.status_code}")
        except Exception as e:
            log["errors"].append(f"fetch_err:{e!r}")

    # 2) Street View fallback
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

# ----------------------------
# Output (downloads)
# ----------------------------
def build_output(rows: List[Dict[str, Any]], fmt: str):
    if fmt == "csv":
        s = io.StringIO()
        # include csv_photo in CSV export for traceability
        w = csv.DictWriter(s, fieldnames=["input_address","mls_id","zillow_url","note","status","csv_photo"])
        w.writeheader(); w.writerows(rows)
        return s.getvalue(), "text/csv"
    if fmt == "md":
        lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
        return ("\n".join(lines) + "\n"), "text/markdown"
    if fmt == "html":
        items = [f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{r["zillow_url"]}</a></li>'
                 for r in rows if r.get("zillow_url")]
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"
    # txt (default) — ALWAYS bulleted list
    lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
    return ("\n".join(lines) + "\n"), "text/plain"

# ----------------------------
# UI — Title + Input Section
# ----------------------------
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses → get verified Zillow links</p>', unsafe_allow_html=True)

st.markdown('<div class="center-box">', unsafe_allow_html=True)
st.markdown("**Paste addresses** (one per line) _or_ **drop a CSV**")

paste = st.text_area(
    label="Paste addresses",
    label_visibility="collapsed",
    placeholder="407 E Woodall St, Smithfield, NC 27577\n13 Herndon Ct, Clayton, NC 27520\n123 US-301 S, Four Oaks, NC 27524",
    height=160,
    key="input_paste"
)

opt1, opt2, opt3 = st.columns([1.1, 1, 1.2])
with opt1:
    remove_dupes = st.checkbox("Remove duplicates", value=True)
with opt2:
    trim_spaces = st.checkbox("Auto-trim", value=True)
with opt3:
    show_preview = st.checkbox("Show preview", value=True)

file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")

# Live parsing of pasted lines
lines_raw = (paste or "").splitlines()
lines = [ln.strip() if trim_spaces else ln for ln in lines_raw]
lines = [ln for ln in lines if ln.strip()]
if remove_dupes:
    lines = list(dict.fromkeys(lines))

count = len(lines)
dupe_count = max(0, len([l for l in lines_raw if (l.strip() if trim_spaces else l)]) - count)
helper_bits = [f"**{count}** line{'s' if count != 1 else ''} detected"]
if remove_dupes and dupe_count:
    helper_bits.append(f"{dupe_count} duplicate{'s' if dupe_count != 1 else ''} removed")
st.caption(" • ".join(helper_bits) + "  •  Tip: one address per line. CSV needs a header row.")

if show_preview and count:
    st.markdown("**Preview** (first 5):")
    preview = lines[:5]
    st.markdown(
        "<ul class='link-list'>" +
        "\n".join([f"<li>{escape(p)}</li>" for p in preview]) +
        ("<li>…</li>" if count > 5 else "") +
        "</ul>",
        unsafe_allow_html=True
    )
st.markdown('</div>', unsafe_allow_html=True)

# ----------------------------
# Run button
# ----------------------------
clicked = st.button("Run", use_container_width=True)

# ----------------------------
# Render helper
# ----------------------------
def _render_results_and_downloads(results: List[Dict[str, Any]]):
    # Results list
    st.markdown("#### Results")
    items = [f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{r["zillow_url"]}</a></li>'
             for r in results if r.get("zillow_url")]
    st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

    # Export
    fmt_options = ["txt","csv","md","html"]
    prev_fmt = (st.session_state.get("__results__") or {}).get("fmt")
    default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
    fmt = st.selectbox("Download format", fmt_options, index=default_idx)
    payload, mime = build_output(results, fmt)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    st.download_button("Export", data=payload,
                       file_name=f"address_alchemist_links_{ts}.{fmt}",
                       mime=mime, use_container_width=True)
    st.session_state["__results__"] = {"results": results, "fmt": fmt}

    # Images + LOG (CSV photo respected)
    thumbs_logs = [get_thumbnail_and_log(r["input_address"], r["zillow_url"], r.get("csv_photo")) for r in results]
    imgs = [(r, tup[0]) for r, tup in zip(results, thumbs_logs) if tup[0]]

    if imgs:
        st.markdown("#### Images")
        cols = st.columns(3)
        for i, (r, img) in enumerate(imgs):
            with cols[i % 3]:
                st.image(img, use_column_width=True)
                st.caption(r.get("input_address") or "Listing")

    # Image Log (always shown)
    st.markdown("#### Image Log")
    log_rows = []
    for idx, (img, log) in enumerate(thumbs_logs, start=1):
        log_rows.append({
            "#": idx,
            "CSV Photo?": "yes" if (log or {}).get("csv_provided") else "no",
            "Stage": (log or {}).get("stage"),
            "Selected": img or "",
            "HTTP": (log or {}).get("status_code"),
            "HTML len": (log or {}).get("html_len"),
            "Zillow URL": (log or {}).get("url"),
            "Errors": "; ".join((log or {}).get("errors") or []),
        })
    st.dataframe(log_rows, use_container_width=True)

# ----------------------------
# Run pipeline
# ----------------------------
if clicked:
    try:
        # Prefer CSV; otherwise pasted lines
        if file is not None:
            content = file.read().decode("utf-8-sig")
            rows_in = list(csv.DictReader(io.StringIO(content)))
            if not rows_in:
                st.error("No rows detected in CSV. Ensure a header row and at least one data row.")
                st.stop()
        else:
            if not lines:
                st.error("Please paste at least one address or upload a CSV.")
                st.stop()
            # for pasted input, no csv_photo available
            rows_in = [{"address": ln} for ln in lines]

        defaults = {"city":"", "state":"", "zip":""}
        results: List[Dict[str, Any]] = []
        prog = st.progress(0, text="Processing…")
        for i, row in enumerate(rows_in, start=1):
            res = process_single_row(
                row,
                delay=0.5,
                land_mode=True,
                defaults=defaults,
                require_state=True,
                mls_first=True,
                default_mls_name="",
                max_candidates=20
            )
            results.append(res)
            prog.progress(i/len(rows_in), text=f"Processed {i}/{len(rows_in)}")
        prog.progress(1.0, text="Done")

        _render_results_and_downloads(results)

    except Exception as e:
        st.error("We hit an error while processing.")
        with st.expander("Details"):
            st.exception(e)

# ----------------------------
# Keep last results visible on rerender
# ----------------------------
data = st.session_state.get("__results__") or {}
results = data.get("results") or []
if results and not clicked:
    _render_results_and_downloads(results)
else:
    if not clicked:
        st.info("Paste addresses or upload a CSV, then click **Run**.")
