# ui/run_tab.py
# Address Alchemist — paste addresses AND arbitrary listing links → Zillow
# CSV upload preserved • Images section restored • Fix-links tool restored
# Homespotter-first via microservice • Title fallback (“… in City, ST …”) → safe deeplink

import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import unquote, urlparse, quote_plus

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional deps ----------
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Safe client helpers (optional) ----------
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

# ---------- Supabase (optional; safe if not configured) ----------
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

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except Exception:
        return False

# ---------- Secrets/env ----------
for k in [
    "BING_API_KEY","BING_CUSTOM_CONFIG_ID",
    "GOOGLE_MAPS_API_KEY","BITLY_TOKEN",
    "HS_ADDRESS_RESOLVER_URL", "HS_RESOLVER_URL"
]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

BING_API_KEY   = os.getenv("BING_API_KEY","")  # optional
BING_CUSTOM_ID = os.getenv("BING_CUSTOM_CONFIG_ID","")  # optional
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY","")
BITLY_TOKEN    = os.getenv("BITLY_TOKEN","")
HS_RESOLVER    = os.getenv("HS_ADDRESS_RESOLVER_URL") or os.getenv("HS_RESOLVER_URL") or ""  # <- your microservice
REQUEST_TIMEOUT = 12

# ---------- Styles ----------
st.markdown("""
<style>
.block-container { max-width: 980px; }
.app-title { font-weight: 800; font-size: 2rem; margin: 0 0 6px; }
.app-sub { color:#6b7280; margin:0 0 12px; }
.center-box { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:14px; }
.small { color:#6b7280; font-size:12.5px; margin-top:6px; }
ul.link-list { margin:0 0 .5rem 1.2rem; padding:0; }
.badge { display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; }
.badge.dup { background:#fee2e2; color:#991b1b; }
.badge.new { background:#dcfce7; color:#065f46; border:1px solid rgba(5,150,105,.35); text-transform:uppercase; }
.run-zone .stButton>button {
  background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important; color:#fff !important; font-weight:800 !important;
  border:0 !important; border-radius:12px !important; box-shadow:0 8px 20px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.15)!important;
}
</style>
""", unsafe_allow_html=True)

# ---------- Helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href","source"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}
MLS_ID_KEYS   = {"mls","mls id","mls_id","mls #","mls#","mls number","mlsnumber","listing id","listing_id"}
MLS_NAME_KEYS = {"mls name","mls board","mls provider","source","source mls","mls source"}

def norm_key(k:str) -> str:
    return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v: return v
    return ""

def is_probable_url(s: str) -> bool:
    s = (s or "").strip().lower()
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

# ---------- JSON-LD blocks helper ----------
def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html: return out
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

# ---------- Content extractors ----------
def extract_title_or_desc(html: str) -> str:
    if not html:
        return ""
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]twitter:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+?)\s*</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def extract_address_from_html(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html: return out
    # JSON-LD first
    try:
        blocks = _jsonld_blocks(html)
        for b in blocks:
            addr = b.get("address") or b.get("itemOffered", {}).get("address") if isinstance(b, dict) else None
            if isinstance(addr, dict):
                if addr.get("streetAddress"): out["street"] = addr.get("streetAddress","")
                if addr.get("addressLocality"): out["city"] = addr.get("addressLocality","")
                if addr.get("addressRegion"): out["state"] = addr.get("addressRegion","")
                if addr.get("postalCode"): out["zip"] = addr.get("postalCode","")
                if out["street"] or (out["city"] and out["state"]):
                    return out
    except Exception:
        pass
    # Plain JSON / meta
    m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I);           out["street"] = out["street"] or (m.group(1) if m else "")
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I);         out["city"]   = out["city"]   or (m.group(1) if m else "")
    m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I);     out["state"]  = out["state"]  or (m.group(1) if m else "")
    m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I);   out["zip"]    = out["zip"]    or (m.group(1) if m else "")
    return out

def _guess_address_from_title(title: str) -> Dict[str, str]:
    """
    Best-effort parse of titles like:
    '4 beds, 2 baths for $265000 in Erwin, NC — Homespotter'
    or '123 Main St, Erwin, NC 28339 | ...'
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not title:
        return out
    m = re.search(r"\bin\s+([A-Za-z .'\-]+?),\s*([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?\b", title)
    if m:
        out["city"] = m.group(1).strip()
        out["state"] = m.group(2).strip()
        if m.lastindex and m.lastindex >= 3 and m.group(3):
            out["zip"] = m.group(3).strip()
    if not out["city"]:
        m = re.search(r"^\s*([0-9A-Za-z #.\-'/]+?),\s*([A-Za-z .'\-]+?),\s*([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?", title)
        if m:
            out["street"] = m.group(1).strip()
            out["city"]   = m.group(2).strip()
            out["state"]  = m.group(3).strip()
            if m.lastindex and m.lastindex >= 4 and m.group(4):
                out["zip"] = m.group(4).strip()
    if out["city"] and not out["street"]:
        lead = title.split(" in ", 1)[0]
        m2 = re.search(r"([0-9]{1,6}\s+[A-Za-z0-9 .'\-/#]+)", lead)
        if m2:
            out["street"] = m2.group(1).strip()
    return out

# ---------- Zillow canonicalization ----------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

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

# ---------- Deeplink builder (blocks 'listing-####' junk) ----------
def construct_deeplink_from_parts(street: str, city: str, state: str, zipc: str, defaults: Dict[str,str]) -> str:
    def _slugify(s: str) -> str:
        a = s.lower()
        a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
        return re.sub(r"\s+", "-", a.strip())
    st_abbr = (state or defaults.get("state","") or "").strip()
    c = (city or defaults.get("city","") or "").strip()
    z = (zipc or defaults.get("zip","") or "").strip()
    # Never allow 'listing-####' to be the slug
    street = street or ""
    if re.match(r"(?i)\s*listing[- ]?\d{4,}\s*$", street):
        street = ""
    slug_parts = []
    if street: slug_parts.append(street)
    loc = ", ".join([p for p in [c, st_abbr] if p])
    if loc: slug_parts.append(loc)
    if z:
        if slug_parts:
            slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else:
            slug_parts.append(z)
    slug = ", ".join(slug_parts)
    if not slug.strip() and (c or st_abbr):  # city/state only
        slug = ", ".join([p for p in [c, st_abbr, z] if p])
    if not slug.strip():
        return ""
    return f"https://www.zillow.com/homes/{_slugify(slug)}_rb/"

# ---------- Try to upgrade /homes/..._rb/ → /homedetails/..._zpid/ ----------
def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        html = r.text
        # Direct anchors
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        # Canonical link
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        # JSON hints
        for pat in [
            r'"canonicalUrl"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
            r'"url"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)
    except Exception:
        return url
    return url

# ---------- Homespotter resolver (microservice) ----------
def _call_hs_resolver(u: str, state_default: str = "NC") -> Tuple[Optional[str], Optional[str]]:
    if not HS_RESOLVER:
        return None, None
    try:
        # 1) GET /resolve?u=<url>
        ep = HS_RESOLVER
        if not re.search(r"/resolve/?$", ep):
            # allow both base or exact; support either
            if ep.endswith("/"):
                ep = ep + "resolve"
            else:
                ep = ep + "/resolve"
        resp = requests.get(ep, params={"u": u}, headers={"Accept": "application/json, text/plain;q=0.9, */*;q=0.8"}, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        # a) If it redirected directly to Zillow:
        if "zillow.com" in (resp.url or "").lower():
            return resp.url, ""
        # b) JSON payload?
        try:
            data = resp.json()
            # supported keys
            for key in ("zillow_url", "zillow", "zillow_deeplink"):
                if isinstance(data.get(key), str) and "zillow.com" in data[key]:
                    return data[key], data.get("address") or ""
            # build deeplink from address fields
            street = data.get("street") or data.get("address") or ""
            city   = data.get("city","")
            state  = data.get("state") or state_default
            zipc   = data.get("zip","")
            if street or (city and state):
                deeplink = construct_deeplink_from_parts(street, city, state, zipc, {"state": state_default})
                if deeplink:
                    return upgrade_to_homedetails_if_needed(deeplink) or deeplink, " ".join([p for p in [street, city, state, zipc] if p]).strip()
        except ValueError:
            pass
        # c) Text payload: first URL?
        txt = resp.text or ""
        m = re.search(r"https?://[^\s\"'><]+", txt)
        if m and "zillow.com" in m.group(0):
            return m.group(0), ""
    except Exception:
        return None, None
    return None, None

# ---------- Thumbnails / enrichment ----------
RE_PRICE  = re.compile(r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS   = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS  = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT   = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)
RE_DESC   = re.compile(r'"(?:description|homeDescription|marketingDescription)"\s*:\s*"([^"]+)"', re.I)

def _tidy_txt(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip()

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
    KEY_HL = [("new roof","roof"),("hvac","hvac"),("ac unit","ac"),("furnace","furnace"),("water heater","water heater"),
              ("renovated","renovated"),("updated","updated"),("remodeled","remodeled"),("open floor plan","open plan"),
              ("cul-de-sac","cul-de-sac"),("pool","pool"),("fenced","fenced"),("acre","acre"),("hoa","hoa"),
              ("primary on main","primary on main"),("finished basement","finished basement")]
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
    for target_w in ("1152","960","768","1536"):
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

# ---------- Thumbnail helper with Street View fallback ----------
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

# ---------- Parsers for pasted/CSV ----------
def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    # Try CSV first (requires a header line)
    try:
        sample = text.splitlines()
        if len(sample) >= 2 and ("," in sample[0] or "\t" in sample[0]):
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
            rows.append({"source_url": s})
        else:
            rows.append({"address": s})
    return rows

def _detect_source_url(row: Dict[str, Any]) -> Optional[str]:
    for k, v in row.items():
        if norm_key(k) in URL_KEYS and is_probable_url(str(v)):
            return str(v).strip()
    for k in ("url", "source", "href", "link", "source_url"):
        if is_probable_url(str(row.get(k, ""))):
            return str(row.get(k)).strip()
    return None

# ---------- Main resolver ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    """
    1) Homespotter: call HS microservice first (if configured).
    2) Fetch page → parse JSON-LD/microdata.
    3) If missing, parse <title>/og:title> to guess City/State (+ street when present).
    4) Build safe /homes/..._rb/ deeplink (never 'listing-####'); try to upgrade to /homedetails/.
    5) If nothing confident, return expanded URL.
    """
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # Microservice first for Homespotter-like hosts
    try:
        host = (urlparse(final_url).hostname or "").lower()
    except Exception:
        host = ""
    is_hs = any(h in host for h in ("l.hms.pt", "homespotter", "idx.homespotter.com"))
    if is_hs and HS_RESOLVER:
        best, human = _call_hs_resolver(final_url, state_default=(defaults.get("state") or "NC"))
        if best:
            return best, (human or "")

    # Parse structured address
    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = addr.get("state","") or (defaults.get("state") or "")
    zipc   = addr.get("zip","") or ""

    # If still thin, use title/og:title fallback
    if not (street and city and state):
        title = extract_title_or_desc(html)
        if title:
            guess = _guess_address_from_title(title)
            street = street or guess.get("street","") or ""
            city   = city   or guess.get("city","")   or ""
            state  = state  or guess.get("state","")  or (defaults.get("state") or "")
            zipc   = zipc   or guess.get("zip","")    or ""

    deeplink = construct_deeplink_from_parts(street, city, state, zipc, defaults)
    if deeplink:
        z = upgrade_to_homedetails_if_needed(deeplink) or deeplink
        human = " ".join([p for p in [street, city, state, zipc] if p]).strip()
        return z, human

    # City + State only
    if city and state:
        z2 = construct_deeplink_from_parts("", city, state, zipc, defaults)
        if z2:
            z2 = upgrade_to_homedetails_if_needed(z2) or z2
            return z2, " ".join([p for p in [city, state, zipc] if p]).strip()

    # Fallback: expanded source url (never naked https://www.zillow.com/homes/)
    return final_url, ""

# ---------- Row processor (addresses from CSV/lines) ----------
def process_single_row(row, *, land_mode=True, defaults=None):
    defaults = defaults or {"city":"", "state":"", "zip":""}
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)
    addr_text = (row.get("address") or row.get("full_address") or "").strip()
    if not addr_text:
        # if row only has URL, let resolve_from_source_url handle it
        src = _detect_source_url(row)
        if src:
            z, human = resolve_from_source_url(src, defaults)
            return {"input_address": human or "", "mls_id": get_first_by_keys(row, MLS_ID_KEYS), "zillow_url": z, "status": "source_url", "csv_photo": csv_photo}

    # Normalize freeform address a bit (optional)
    street, city, state, zipc = "", "", defaults.get("state") or "", ""
    if usaddress and addr_text:
        try:
            parts = usaddress.tag(addr_text)[0]
            street = (parts.get("AddressNumber","") + " " +
                      " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip()).strip()
            city   = parts.get("PlaceName","") or ""
            state  = parts.get("StateName","") or state
            zipc   = parts.get("ZipCode","") or ""
        except Exception:
            street = addr_text
    else:
        street = addr_text

    deeplink = construct_deeplink_from_parts(street, city, state, zipc, defaults)
    z = upgrade_to_homedetails_if_needed(deeplink) if deeplink else ""
    return {
        "input_address": " ".join([p for p in [street, city, state, zipc] if p]).strip(),
        "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
        "zillow_url": z or deeplink or "",
        "status": "address_row",
        "csv_photo": csv_photo
    }

# ---------- Tracking + Bitly (optional) ----------
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

# ---------- UI helpers ----------
def results_list_with_copy_all(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        href = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not href:
            continue
        safe_href = escape(href)
        link_txt = href
        li_html.append(f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a></li>')

    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"
    copy_lines = []
    for r in results:
        u = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if u: copy_lines.append(u)
    copy_text = "\n".join(copy_lines) + ("\n" if copy_lines else "")
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
          const text=`{copy_text.replace("\\","\\\\").replace("`","\\`")}`;
          btn.addEventListener('click', async()=>{{
            try{{ await navigator.clipboard.writeText(text); const p=btn.textContent; btn.textContent='✓'; setTimeout(()=>btn.textContent=p,900); }}
            catch(e){{ const p=btn.textContent; btn.textContent='×'; setTimeout(()=>btn.textContent=p,900); }}
          }});
        }})();
      </script>
    </body></html>
    """
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

def _thumbnails_grid(results: List[Dict[str, Any]], columns: int = 3):
    if not results:
        return
    cols = st.columns(columns)
    for i, r in enumerate(results):
        u = (r.get("preview_url") or r.get("zillow_url") or "").strip()
        img, _log = get_thumbnail_and_log(r.get("input_address",""), u, r.get("csv_photo"))
        html = ""
        safe_u = escape(u)
        if img:
            safe_img = escape(img)
            html = f'<a href="{safe_u}" target="_blank" rel="noopener"><img src="{safe_img}" alt="thumbnail" style="width:100%;height:auto;border-radius:12px"/></a>'
        else:
            html = f'<a href="{safe_u}" target="_blank" rel="noopener">{escape(safe_u)}</a>'
        with cols[i % columns]:
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
    st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
    st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

    NO_CLIENT = "➤ No client (no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    # Client + campaign
    clients = []
    try:
        clients = fetch_clients(include_inactive=False) or []
    except Exception:
        clients = []
    client_names = [c.get("name","") for c in clients if c.get("name")]
    options = [NO_CLIENT] + client_names + [ADD_SENTINEL]

    c1, c2 = st.columns([1.4, 1])
    with c1:
        chosen = st.selectbox("Client", options, index=0)
    with c2:
        campaign = st.text_input("Campaign tag (optional)", value=state.get("campaign",""))

    if chosen == ADD_SENTINEL:
        new_name = st.text_input("New client name")
        if st.button("Create client"):
            ok, msg = upsert_client(new_name.strip(), active=True)
            if ok:
                st.success(f"Added “{new_name}”.")
                _safe_rerun()
            else:
                st.error(msg or "Could not add client.")
        return

    client_tag = "" if chosen == NO_CLIENT else chosen

    # Paste + CSV
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area("Input", height=160, placeholder="Address or Homespotter link per line…", label_visibility="collapsed")
    file = st.file_uploader("Upload CSV (address column or url column)", type=["csv"], label_visibility="collapsed")
    st.markdown('</div>', unsafe_allow_html=True)

    # Options
    cA, cB, cC = st.columns([1,1,1.2])
    with cA:
        use_shortlinks = st.checkbox("Use Bitly tracking", value=False)
    with cB:
        enrich_details = st.checkbox("Enrich images/details", value=True)
    with cC:
        show_images = st.checkbox("Show images", value=True)

    # Run
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    run_btn = st.button("🚀 Run", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    results: List[Dict[str, Any]] = []
    if run_btn:
        # Collect rows
        rows_in: List[Dict[str, Any]] = []
        # CSV first
        if file is not None:
            try:
                content = file.getvalue().decode("utf-8-sig", errors="replace")
                reader = csv.DictReader(io.StringIO(content))
                rows_in.extend(list(reader))
            except Exception as e:
                st.warning(f"CSV read error: {e}")
        # Paste
        rows_in.extend(_rows_from_paste(paste or ""))

        if not rows_in:
            st.warning("Nothing to process.")
            return

        defaults = {"city":"", "state":"NC", "zip":""}  # You can change default state here
        prog = st.progress(0, text="Resolving…")
        total = len(rows_in)

        for i, row in enumerate(rows_in, start=1):
            src_url = _detect_source_url(row)
            if src_url:
                zurl, human = resolve_from_source_url(src_url, defaults)
                results.append({
                    "input_address": human or (row.get("address") or row.get("full_address") or ""),
                    "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
                    "zillow_url": zurl,
                    "status": "source_url",
                    "csv_photo": get_first_by_keys(row, PHOTO_KEYS),
                })
            else:
                results.append(process_single_row(row, land_mode=True, defaults=defaults))
            prog.progress(i/total, text=f"Resolved {i}/{total}")
        prog.progress(1.0, text="Done")

        # Preview + display URLs
        for r in results:
            base = (r.get("zillow_url") or "").strip()
            if base:
                r["preview_url"] = (canonicalize_zillow(base)[0] or base)
                disp = make_trackable_url(base, client_tag, campaign) if (client_tag or campaign) else base
                r["display_url"] = bitly_shorten(disp) or disp
            else:
                r["preview_url"] = ""
                r["display_url"] = ""

        # Enrich + images
        if enrich_details:
            try:
                results = asyncio.run(enrich_results_async(results))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                results = loop.run_until_complete(enrich_results_async(results))
                loop.close()

        # Output
        st.subheader("Results")
        results_list_with_copy_all(results)

        if show_images:
            st.subheader("Images")
            _thumbnails_grid(results, columns=3)

        # Download
        st.subheader("Export")
        fmt = st.radio("Format", ["txt","md","html","csv"], horizontal=True, index=0)
        def _build_output(rows: List[Dict[str, Any]], fmt: str):
            def pick_url(r):
                return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
            if fmt == "csv":
                fields = ["input_address","mls_id","url","status","price","beds","baths","sqft","summary","highlights","remarks"]
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
                    if u:
                        items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
                return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"
            lines = [pick_url(r) for r in rows if pick_url(r)]
            payload = "\n".join(lines) + ("\n" if lines else "")
            return payload, ("text/markdown" if fmt == "md" else "text/plain")
        payload, mime = _build_output(results, fmt)
        st.download_button("Download", data=payload.encode("utf-8"), file_name=f"results.{fmt}", mime=mime, use_container_width=True)

    st.divider()

    # ---------- Fix links ----------
    st.subheader("Fix properties")
    st.caption("Paste any Zillow/Homespotter/etc. links. I’ll output clean canonical **/homedetails/** or safe **/homes/** deeplinks.")
    fix_text = st.text_area("Links to fix", height=140, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        defaults = {"state":"NC", "city":"", "zip":""}
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                # Prefer full resolver (handles Homespotter)
                z, _ = resolve_from_source_url(best, defaults)
                best = z or best
                if best:
                    best = upgrade_to_homedetails_if_needed(best) or best
            except Exception:
                pass
            # Clean up canonical form
            canon, _zpid = canonicalize_zillow(best)
            fixed.append(canon or best)
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")

# (optional) Allow local testing via: streamlit run ui/run_tab.py
if __name__ == "__main__":
    render_run_tab(st.session_state)
