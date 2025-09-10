# app.py — Minimalist Zillow Deeplink Finder
# one central input box, one Run button (no icon), results beneath, bulleted list preview, simple format download

import os, csv, io, re, time, json
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional

import requests
import streamlit as st

# ----------------------------
# Page config (minimal)
# ----------------------------
st.set_page_config(page_title="Zillow Deeplink Finder", layout="centered")

# ----------------------------
# Load optional secrets -> env
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
BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"
REQUEST_TIMEOUT = 12

# ----------------------------
# Core helpers (kept from your app; trimmed where possible)
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
    for k in list(n.keys()):
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

# land/LOT handling
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
        if mls_id and page_contains_mls(html, mls_id): return url, "mls_match"
        if page_contains_city_state(html, required_city, required_state) and "/homedetails/" in url: return url, "city_state_match"
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

def find_zillow_by_mls_with_confirmation(mls_id, required_state=None, required_city=None, mls_name=None, delay=0.3, require_match=False, max_candidates=20):
    if not (BING_API_KEY and mls_id): return None, None
    q_mls = [f'"MLS# {mls_id}" site:zillow.com', f'"{mls_id}" "MLS" site:zillow.com', f'{mls_id} site:zillow.com/homedetails']
    if mls_name: q_mls = [f'{q} "{mls_name}"' for q in q_mls] + q_mls
    seen, candidates = set(), []
    for q in q_mls:
        for it in bing_search_items(q):
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
        if ok: return ok, (mtype or "mls_match")
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

def process_single_row(row, *, delay, land_mode, defaults, require_state, mls_first, default_mls_name, max_candidates):
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
        zurl, mtype = find_zillow_by_mls_with_confirmation(mls_id, required_state=required_state_val, required_city=required_city_val,
                                                           mls_name=mls_name, delay=min(delay, 0.6), require_match=require_state,
                                                           max_candidates=max_candidates)
        if zurl: status = "mls_match" if mtype == "mls_match" else "city_state_match"
    if not zurl:
        z = azure_search_first_zillow(query_address)
        if z: zurl, status = z, "azure_hit"
    if not zurl:
        # address variant search could be added back here if desired; keeping minimal
        zurl, status = deeplink, "deeplink_fallback"
    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": mls_id, "zillow_url": zurl, "note": note, "status": status}

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
    # txt = bulleted list as requested
    lines = [f"- {r['zillow_url']}" for r in rows if r.get("zillow_url")]
    return ("\n".join(lines) + "\n"), "text/plain"

# ----------------------------
# MINIMAL UI
# ----------------------------

# soft CSS to center & tighten
st.markdown("""
<style>
.block-container { max-width: 860px; }
input, textarea { font-size: 15px !important; }
.run-btn button { border-radius: 10px; height: 42px; font-weight: 600; }
.center-box { border: 1px solid rgba(0,0,0,.08); border-radius: 14px; padding: 18px; }
.small { color: #666; font-size: 12.5px; }
</style>
""", unsafe_allow_html=True)

st.write("")  # tiny spacer
st.markdown("### Zillow Deeplink Finder")

# One central box: paste OR drag CSV
with st.container():
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    paste = st.text_area("Paste addresses (one per line) OR upload a CSV below",
                         placeholder="407 E Woodall St, Smithfield, NC 27577\n13 Herndon Ct, Clayton, NC 27520",
                         height=120, label_visibility="visible")
    file = st.file_uploader("Upload CSV (headers ok; at least an address column)", type=["csv"], label_visibility="visible")
    st.markdown('<div class="small">Tips: If CSV has city/state/zip & MLS, matching is stronger. If not, set defaults below.</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

# Advanced (collapsed by default)
with st.expander("Advanced defaults (optional)"):
    col1, col2, col3 = st.columns(3)
    with col1: default_city  = st.text_input("Default City", value="")
    with col2: default_state = st.text_input("Default State (2 letters)", value="")
    with col3: default_zip   = st.text_input("Default Zip", value="")
    land_mode = st.checkbox("Optimize for land/LOT listings", value=True)
    require_state = st.checkbox("Require state match in results", value=True)
    mls_first = st.checkbox("Prefer MLS ID matching", value=True)
    default_mls_name = st.text_input("Default MLS Board (e.g., TMLS)", value="")
    delay = st.slider("Network delay between lookups (sec)", 0.0, 2.0, 0.5, 0.1)

# Single Run button (no icon), centered behaviorally by spacing
col_l, col_c, col_r = st.columns([1,2,1])
with col_c:
    run = st.container()
    with run:
        clicked = st.button("Run", use_container_width=True)

results = None
rows_in: List[Dict[str, Any]] = []

# Parse inputs when Run is clicked
if clicked:
    try:
        # priority: CSV; else pasted lines
        if file is not None:
            content = file.read().decode("utf-8-sig")
            rows_in = list(csv.DictReader(io.StringIO(content)))
            if not rows_in:
                st.error("No rows detected in CSV. Ensure a header row and at least one data row.")
        else:
            lines = [ln.strip() for ln in (paste or "").splitlines() if ln.strip()]
            if not lines:
                st.error("Please paste at least one address or upload a CSV.")
            else:
                rows_in = [{"address": ln} for ln in lines]

        if rows_in:
            defaults = {"city": default_city.strip(), "state": default_state.strip(), "zip": default_zip.strip()}

            # process
            results = []
            prog = st.progress(0, text="Processing…")
            for i, row in enumerate(rows_in, start=1):
                results.append(process_single_row(
                    row,
                    delay=delay, land_mode=land_mode, defaults=defaults,
                    require_state=require_state, mls_first=mls_first,
                    default_mls_name=default_mls_name.strip(), max_candidates=20
                ))
                prog.progress(i/len(rows_in), text=f"Processed {i}/{len(rows_in)}")
            prog.progress(1.0, text="Done")

            st.session_state["__results__"] = {"results": results, "defaults": defaults}

    except Exception as e:
        st.error("We hit an error while processing.")
        with st.expander("Details"):
            st.exception(e)

# RESULTS appear immediately below the button
data = st.session_state.get("__results__")
if data:
    results = data["results"]

    # Always show bulleted list preview (TXT-style)
    st.markdown("#### Results")
    bullets = "\n".join([f"- {r['zillow_url']}" for r in results if r.get("zillow_url")]) + "\n"
    st.code(bullets, language="text")

    # After results: choose download format (default txt) + download button
    fmt = st.selectbox("Download format", ["txt","csv","md","html"], index=0)
    payload, mime = build_output(results, fmt)
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    st.download_button("Download", data=payload, file_name=f"zillow_links_{ts}.{fmt}", mime=mime, use_container_width=True)
