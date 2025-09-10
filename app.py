# app.py — Ultra-Minimal Zillow Deeplink Finder (images-if-any + dropdown+export)
# One central input, one Run button, clickable bulleted results, format dropdown, Export button, and Images section if ANY image exists.

import os, csv, io, re, time, json
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple

import requests
import streamlit as st

# ----------------------------
# Minimal page setup & styles
# ----------------------------
st.set_page_config(page_title="Zillow Deeplink Finder", layout="centered")

st.markdown("""
<style>
/* Layout */
.block-container { max-width: 760px; }
.center-box { border: 1px solid rgba(0,0,0,.08); border-radius: 12px; padding: 16px; }

/* Text */
.small { color: #6b7280; font-size: 12.5px; margin-top: 6px; }
ul.link-list { margin: 0 0 .5rem 1.2rem; padding: 0; }
ul.link-list li { margin: 0.2rem 0; }

/* Buttons — make them pop */
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

/* Download (Export) button — green theme */
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

/* Hide ONLY the uploader clear button (if present) */
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
            return {"street_raw": n[k], "city":"", "state":"", "zip":"", "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
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
# Web search + confirmation
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

UA_HEADERS = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}
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
            cand = re.findall(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', html)[:8]
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

# Single-row pipeline (with strong defaults)
def process_single_row(row, *, delay=0.5, land_mode=True, defaults=None,
                       require_state=True, mls_first=True, default_mls_name="", max_candidates=20):
    defaults = defaults or {"city":"", "state":"", "zip":""}
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

    # 1) MLS-first
    if mls_first and mls_id:
        zurl, mtype = find_zillow_by_mls_with_confirmation(
            mls_id, required_state=required_state_val, required_city=required_city_val,
            mls_name=mls_name, delay=min(delay, 0.6), require_match=require_state,
            max_candidates=max_candidates
        )
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"

    # 2) Azure optional
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z: zurl, status = z, "azure_hit"

    # 3) Address variants via Bing
    if not zurl:
        zurl, mtype = resolve_homedetails_with_bing_variants(
            variants, required_state=required_state_val, required_city=required_city_val,
            mls_id=mls_id or None, delay=min(delay, 0.6), require_match=require_state
        )
        if zurl:
            status = "mls_match" if mtype == "mls_match" else "city_state_match"

    # 4) Fallback deeplink
    if not zurl:
        zurl, status = deeplink, "deeplink_fallback"

    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "note": note, "status": status}

# ----------------------------
# Images (best-effort) + cache
# ----------------------------
def fetch_og_image(url: str) -> Optional[str]:
    """Try to extract og:image (or similar) from homedetails page."""
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
            if m:
                return m.group(1)
    except Exception:
        return None
    return None

def street_view_image(addr: str) -> Optional[str]:
    """Fallback via Google Street View Static API (requires GOOGLE_MAPS_API_KEY)."""
    key = GOOGLE_MAPS_API_KEY
    if not key or not addr:
        return None
    from urllib.parse import quote_plus
    loc = quote_plus(addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={key}"

def picture_for_result(query_address: str, zurl: str) -> Optional[str]:
    img = None
    if zurl and "/homedetails/" in zurl:
        img = fetch_og_image(zurl)
    if not img:
        img = street_view_image(query_address)
    return img

@st.cache_data(ttl=3600, show_spinner=False)
def get_thumbnail(query_address: str, zurl: str) -> Optional[str]:
    return picture_for_result(query_address, zurl)

# ----------------------------
# Output (downloads)
# ----------------------------
def build_output(rows: List[Dict[str, Any]], fmt: str):
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
    # txt (default) — ALWAYS bulleted list
    lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
    return ("\n".join(lines) + "\n"), "text/plain"

# ----------------------------
# Minimal UI
# ----------------------------
st.markdown("### Zillow Deeplink Finder")

st.markdown('<div class="center-box">', unsafe_allow_html=True)
paste = st.text_area(
    "Paste addresses (one per line) or upload a CSV",
    placeholder="407 E Woodall St, Smithfield, NC 27577\n13 Herndon Ct, Clayton, NC 27520",
    height=120
)
file  = st.file_uploader("Upload CSV", type=["csv"])
st.markdown('<div class="small">CSV can include columns for address/city/state/zip and optional MLS ID.</div>', unsafe_allow_html=True)
st.markdown('</div>', unsafe_allow_html=True)

clicked = st.button("Run", use_container_width=True)

if clicked:
    try:
        # Prefer CSV; otherwise parse pasted lines
        if file is not None:
            content = file.read().decode("utf-8-sig")
            rows_in = list(csv.DictReader(io.StringIO(content)))
            if not rows_in:
                st.error("No rows detected in CSV. Ensure a header row and at least one data row.")
                st.stop()
        else:
            lines = [ln.strip() for ln in (paste or "").splitlines() if ln.strip()]
            if not lines:
                st.error("Please paste at least one address or upload a CSV.")
                st.stop()
            rows_in = [{"address": ln} for ln in lines]

        defaults = {"city":"", "state":"", "zip":""}  # minimal UI: no defaults panel
        results: List[Dict[str, Any]] = []
        prog = st.progress(0, text="Processing…")
        for i, row in enumerate(rows_in, start=1):
            res = process_single_row(
                row,
                delay=0.5,             # sensible default
                land_mode=True,        # good for LOT/land
                defaults=defaults,
                require_state=True,
                mls_first=True,
                default_mls_name="",
                max_candidates=20
            )
            results.append(res)
            prog.progress(i/len(rows_in), text=f"Processed {i}/{len(rows_in)}")
        prog.progress(1.0, text="Done")

        # ---- RESULTS (clickable bulleted list) ----
        st.markdown("#### Results")
        items = [f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{r["zillow_url"]}</a></li>'
                 for r in results if r.get("zillow_url")]
        st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

        # ---- Format dropdown + Export button directly under it ----
        fmt_options = ["txt","csv","md","html"]
        fmt = st.selectbox("Download format", fmt_options, index=0)
        payload, mime = build_output(results, fmt)
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        st.download_button("Export", data=payload, file_name=f"zillow_links_{ts}.{fmt}", mime=mime, use_container_width=True)

        # ---- Images section (appears if ANY link has an image) ----
        thumbs: List[Optional[str]] = [get_thumbnail(r["input_address"], r["zillow_url"]) for r in results]
        imgs = [(r, img) for r, img in zip(results, thumbs) if img]
        if imgs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i, (r, img) in enumerate(imgs):
                with cols[i % 3]:
                    st.image(img, use_column_width=True)
                    st.caption(r["input_address"] if r.get("input_address") else "Listing")

        # Persist for rerender (always include fmt)
        st.session_state["__results__"] = {"results": results, "fmt": fmt}

    except Exception as e:
        st.error("We hit an error while processing.")
        with st.expander("Details"):
            st.exception(e)

# ----------------------------
# Keep last results visible on rerender (KeyError-proof)
# ----------------------------
fmt_options = ["txt","csv","md","html"]
data = st.session_state.get("__results__") or {}
results = data.get("results") or []

if results and not clicked:
    st.markdown("#### Results")
    items = [f'<li><a href="{r.get("zillow_url","")}" target="_blank" rel="noopener">{r.get("zillow_url","")}</a></li>'
             for r in results if r.get("zillow_url")]
    st.markdown("<ul class='link-list'>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

    prev_fmt = data.get("fmt")
    default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
    fmt = st.selectbox("Download format", fmt_options, index=default_idx)

    payload, mime = build_output(results, fmt)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    st.download_button("Export", data=payload,
                       file_name=f"zillow_links_{ts}.{fmt}", mime=mime, use_container_width=True)

    # Images section (appears if ANY link has an image)
    thumbs: List[Optional[str]] = [get_thumbnail(r.get("input_address",""), r.get("zillow_url","")) for r in results]
    imgs = [(r, img) for r, img in zip(results, thumbs) if img]
    if imgs:
        st.markdown("#### Images")
        cols = st.columns(3)
        for i, (r, img) in enumerate(imgs):
            with cols[i % 3]:
                st.image(img, use_column_width=True)
                st.caption(r.get("input_address","Listing"))

    # keep session fmt in sync
    st.session_state["__results__"]["fmt"] = fmt
else:
    if not clicked:
        st.info("Paste addresses or upload a CSV, then click **Run**.")
