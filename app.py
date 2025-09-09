import os, csv, io, re, time, json, streamlit as st
from datetime import datetime
import requests
from urllib.parse import quote_plus

# ---- Load optional secrets into env ----
for k in ["AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY",
          "BING_API_KEY","BING_CUSTOM_CONFIG_ID","GOOGLE_MAPS_API_KEY"]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

# ---- Config (from env) ----
AZURE_SEARCH_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT","").rstrip("/")
AZURE_SEARCH_INDEX    = os.getenv("AZURE_SEARCH_INDEX","")
AZURE_SEARCH_KEY      = os.getenv("AZURE_SEARCH_API_KEY","")
BING_API_KEY          = os.getenv("BING_API_KEY","")
BING_CUSTOM_ID        = os.getenv("BING_CUSTOM_CONFIG_ID","")
GOOGLE_MAPS_API_KEY   = os.getenv("GOOGLE_MAPS_API_KEY","")

BING_WEB    = "https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM = "https://api.bing.microsoft.com/v7.0/custom/search"

# ---- Address parsing helpers ----
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

# Land-oriented cleaning
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

def norm_key(k): return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v:
                return v
    return ""

def extract_components(row):
    """Return components: street_raw, city, state, zip, county (when present)."""
    n = { norm_key(k): (str(v).strip() if v is not None else "") for k,v in row.items() }

    # A) Full-address field present
    for k in list(n.keys()):
        if k in ADDR_PRIMARY and n[k]:
            return {"street_raw": n[k], "city":"", "state":"", "zip":"", "county":""}

    # B) Assemble from parts
    num  = get_first_by_keys(n, NUM_KEYS)
    name = get_first_by_keys(n, NAME_KEYS)
    suf  = get_first_by_keys(n, SUF_KEYS)
    city = get_first_by_keys(n, CITY_KEYS)
    state= get_first_by_keys(n, STATE_KEYS)
    zipc = get_first_by_keys(n, ZIP_KEYS)
    county = get_first_by_keys(n, COUNTY_KEYS)

    street_parts = [x for x in [num, name, suf] if x]
    street_raw = " ".join(street_parts).strip()

    return {"street_raw": street_raw, "city": city, "state": state, "zip": zipc, "county": county}

def clean_land_street(street:str) -> str:
    """Land mode: strip '0', LOT/TRACT prefixes, expand HWY/rd/etc., normalize spacing."""
    if not street: return street
    s = street.strip()

    # Remove leading '0 ' (MLS often uses 0 when no house number)
    s = re.sub(r"^\s*0[\s\-]+", "", s)

    # Remove leading tokens: "Lot 6", "Tract One", "Parcel A", etc.
    tokens = re.split(r"[\s\-]+", s)
    if tokens and tokens[0].lower() in LAND_LEAD_TOKENS:
        tokens = tokens[1:]
        if tokens and re.match(r"^(?:[A-Za-z]|\d+|one|two|three|four|five|six|seven|eight|nine|ten)$", tokens[0], re.I):
            tokens = tokens[1:]
        s = " ".join(tokens)

    # Expand abbreviations (case-insensitive)
    s_lower = f" {s.lower()} "
    for pat, repl in HWY_EXPAND.items():
        s_lower = re.sub(pat, f" {repl} ", s_lower)
    s = re.sub(r"\s+", " ", s_lower).strip()

    # Remove dangling punctuation
    s = re.sub(r"[^\w\s/-]", "", s)
    return s

def compose_query_address(street, city, state, zipc, defaults):
    """Create a strong query string for Bing (street + city + state + zip if available)."""
    parts = [street]
    c = city or defaults.get("city","")
    st_abbr = state or defaults.get("state","")
    z = zipc or defaults.get("zip","")
    if c: parts.append(c)
    if st_abbr: parts.append(st_abbr)
    if z: parts.append(z)
    return " ".join([p for p in parts if p]).strip()

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
        slug_parts[-1] = f"{slug_parts[-1]} {z}" if slug_parts else z

    slug = ", ".join(slug_parts)
    a = slug.lower()
    a = re.sub(r"[^\w\s,-]", "", a)
    a = a.replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---- Bing filtering helpers (avoid wrong states/cities) ----
def _slug(text:str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')

def url_matches_city_state(url:str, city:str=None, state:str=None) -> bool:
    """Heuristic: Zillow homedetails paths usually contain -City- and -ST- in the slug."""
    u = (url or '')
    ok = True
    if state:
        st = state.upper().strip()
        if f"-{st}-" not in u and f"/{st.lower()}/" not in u:
            ok = False
    if city and ok:
        cs = f"-{_slug(city)}-"
        if cs not in u:
            ok = False
    return ok

def best_from_bing_items(items, required_city=None, required_state=None):
    if not items: return None
    def score(it):
        url = it.get("url") or it.get("link") or ""
        s = 0
        if "zillow.com" in url: s += 1
        if "/homedetails/" in url: s += 3
        if "/homes/" in url: s += 1
        if "zpid" in url: s += 1
        if url_matches_city_state(url, required_city, required_state): s += 3
        return s
    # If a state is required, hard-filter first
    filtered = []
    if required_state:
        for it in items:
            u = it.get("url") or it.get("link") or ""
            if url_matches_city_state(u, required_city, required_state):
                filtered.append(it)
    ranked = sorted(filtered or items, key=score, reverse=True)
    top = ranked[0]
    return top.get("url") or top.get("link") or ""

def resolve_homedetails_with_bing(query_address, required_state=None, required_city=None, delay=0.25):
    if not BING_API_KEY: return None
    h = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
    qset = [
        f'{query_address} site:zillow.com/homedetails',
        f'{query_address} zpid site:zillow.com',
        f'"{query_address}" site:zillow.com/homedetails',
        f'{query_address} land site:zillow.com/homedetails',
        f'{query_address} lot site:zillow.com/homedetails',
    ]
    for q in qset:
        try:
            if BING_CUSTOM_ID:
                p = {"q": q, "customconfig": BING_CUSTOM_ID, "mkt": "en-US", "count": 10}
                r = requests.get(BING_CUSTOM, headers=h, params=p, timeout=20)
            else:
                p = {"q": q, "mkt": "en-US", "count": 10, "responseFilter": "Webpages"}
                r = requests.get(BING_WEB, headers=h, params=p, timeout=20)
            r.raise_for_status()
            data = r.json()
            items = data.get("webPages", {}).get("value") if "webPages" in data else data.get("items", [])
            url = best_from_bing_items(items or [], required_city=required_city, required_state=required_state)
            if url and "/homedetails/" in url and url_matches_city_state(url, required_city, required_state):
                return url
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) in (429,500,502,503,504):
                time.sleep(1.0); continue
            break
        except requests.RequestException:
            time.sleep(0.8); continue
        finally:
            time.sleep(delay)
    return None

# ---- Azure (optional) ----
def azure_search_first_zillow(query_address):
    if not (AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX and AZURE_SEARCH_KEY):
        return None
    url = f"{AZURE_SEARCH_ENDPOINT}/indexes/{AZURE_SEARCH_INDEX}/docs/search?api-version=2023-11-01"
    h = {"Content-Type":"application/json","api-key":AZURE_SEARCH_KEY}
    try:
        r = requests.post(url, headers=h, data=json.dumps({"search": query_address, "top": 1}), timeout=20)
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

# ---- Image helpers (best-effort) ----
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def _fetch(url, timeout=8):
    return requests.get(url, headers=UA_HEADERS, timeout=timeout)

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
    loc = quote_plus(addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=400x300&location={loc}&key={GOOGLE_MAPS_API_KEY}"

def picture_for_result(query_address, zurl):
    """Prefer homedetails og:image; else Street View of the query address."""
    img = None
    if zurl and "/homedetails/" in zurl:
        img = fetch_og_image(zurl)
    if not img:
        img = street_view_image(query_address)
    return img

@st.cache_data(ttl=3600, show_spinner=False)
def get_thumbnail(query_address, zurl):
    return picture_for_result(query_address, zurl)

# ---- Core processing ----
def process_rows(rows, delay, land_mode, defaults, require_state):
    out = []
    for row in rows:
        comp = extract_components(row)
        street = comp["street_raw"]
        if land_mode:
            street = clean_land_street(street)

        # Build query & deeplink WITH location
        query_address = compose_query_address(street, comp["city"], comp["state"], comp["zip"], defaults)
        deeplink = construct_deeplink_from_parts(street, comp["city"], comp["state"], comp["zip"], defaults)

        # 1) Azure (optional)
        zurl = azure_search_first_zillow(query_address)

        # 2) Bing → /homedetails/ with city/state filtering
        if not zurl:
            required_state_val = defaults.get("state") if require_state else None
            required_city_val  = comp["city"] or defaults.get("city")
            hd = resolve_homedetails_with_bing(
                query_address,
                required_state=required_state_val,
                required_city=required_city_val
            )
            zurl = hd

        # 3) Fallback to the constructed (location-aware) _rb deeplink
        if not zurl:
            zurl = deeplink

        out.append({"input_address": query_address, "zillow_url": zurl})
        time.sleep(min(delay, 0.5))
    return out

def build_output(rows, fmt):
    if fmt == "csv":
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["input_address","zillow_url"])
        w.writeheader(); w.writerows(rows)
        return s.getvalue(), "text/csv"
    if fmt == "md":
        text = "\n".join([f"- {r['zillow_url']}" for r in rows if r['zillow_url']]) + "\n"
        return text, "text/markdown"
    if fmt == "html":
        items = [f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{r["zillow_url"]}</a></li>'
                 for r in rows if r["zillow_url"]]
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"
    # txt (raw deeplinks, one per line)
    text = "\n".join([r['zillow_url'] for r in rows if r['zillow_url']]) + "\n"
    return text, "text/plain"

# ---- Streamlit UI ----
st.set_page_config(page_title="Zillow Deeplink Finder (Land-aware)", page_icon="🏠", layout="wide")
st.title("🏠 Zillow Deeplink Finder (Land-aware)")
st.caption("Upload a CSV → returns raw Zillow deeplinks. Land mode cleans LOT/TRACT/0 prefixes and appends city/state. Thumbnails are best-effort.")

c1, c2, c3, c4, c5 = st.columns([1,1,1,1,1])
with c1:
    fmt   = st.selectbox("Download format", ["txt","csv","md","html"], index=0)
with c2:
    delay = st.slider("Delay (seconds)", 0.0, 2.0, 0.4, 0.1)
with c3:
    land_mode = st.checkbox("Land mode (clean & expand)", value=True)
with c4:
    require_state = st.checkbox("Require state match in results", value=True)
with c5:
    show_images = st.checkbox("Show thumbnails", value=True)

st.subheader("Fallback location (used when CSV is missing fields)")
d1, d2, d3 = st.columns([1,0.5,0.6])
with d1:
    default_city  = st.text_input("Default City", value="")
with d2:
    default_state = st.text_input("Default State (2 letters)", value="")
with d3:
    default_zip   = st.text_input("Default Zip", value="")

defaults = {"city": default_city.strip(), "state": default_state.strip(), "zip": default_zip.strip()}

file = st.file_uploader("Upload CSV (must include a header row)", type=["csv"])

if file:
    try:
        content = file.read().decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(content)))
        results = process_rows(rows, delay=delay, land_mode=land_mode, defaults=defaults, require_state=require_state)
        st.success(f"Processed {len(results)} rows.")

        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        name = f"zillow_links_{ts}.{fmt}"
        payload, mime = build_output(results, fmt)
        st.download_button("Download result", data=payload, file_name=name, mime=mime)

        # --- RESULTS (thumbnails + links) ---
        if show_images:
            st.subheader("Results")
            for r in results:
                col1, col2 = st.columns([1, 4], gap="small")
                img_url = get_thumbnail(r["input_address"], r["zillow_url"])
                if img_url:
                    try:
                        col1.image(img_url, use_column_width=True)
                    except Exception:
                        col1.empty()
                else:
                    col1.empty()
                col2.write(r["zillow_url"])
        else:
            st.dataframe(results, use_container_width=True)

        if fmt in ("md","txt"):
            st.subheader("Preview")
            st.code(payload, language="markdown" if fmt=="md" else "text")

    except Exception as e:
        st.error(f"Error: {e}")
else:
    st.info("Set Default City/State (or include them per row), then upload your CSV.")
