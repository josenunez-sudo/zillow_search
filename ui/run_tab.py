# ui/run_tab.py
# Address → Zillow resolver with Homespotter microservice, CSV upload, images, and "Fix properties" tools.

import os, re, io, csv, json, time, asyncio
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from html import escape
from urllib.parse import urlparse, unquote

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional deps ----------
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Styles ----------
st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:16px; }
ul.link-list { margin:0 0 .5rem 1.2rem; padding:0; }
.badge { display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; }
.badge.new { background:#dcfce7; color:#065f46; border:1px solid rgba(5,150,105,.35); }
.badge.dup { background:#fee2e2; color:#991b1b; }
.run-zone .stButton > button {
  background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important;
  color: #fff !important;
  font-weight: 800 !important;
  border: 0 !important;
  border-radius: 12px !important;
  box-shadow: 0 8px 20px rgba(29,78,216,.35), 0 2px 6px rgba(0,0,0,.15) !important;
}
</style>
""", unsafe_allow_html=True)

# ---------- Rerun helper ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Secrets/env ----------
for k in ["GOOGLE_MAPS_API_KEY", "HS_ADDRESS_RESOLVER_URL"]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
HS_RESOLVER = os.getenv("HS_ADDRESS_RESOLVER_URL", "").strip()

REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ---------- Utilities ----------
def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html:
        return out
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        blob = m.group(1)
        try:
            data = json.loads(blob)
            if isinstance(data, list):
                out.extend([d for d in data if isinstance(d, dict)])
            elif isinstance(data, dict):
                out.append(data)
        except Exception:
            continue
    return out

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Prefer JSON-LD; fall back to common microdata/meta tags.
    Returns dict {street, city, state, zip}
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html:
        return out

    # JSON-LD
    try:
        for blk in _jsonld_blocks(html):
            if not isinstance(blk, dict):
                continue
            addr = blk.get("address") or blk.get("itemOffered", {}).get("address")
            if isinstance(addr, dict):
                out["street"] = out["street"] or (addr.get("streetAddress") or "")
                out["city"]   = out["city"]   or (addr.get("addressLocality") or "")
                st_v          = (addr.get("addressRegion") or addr.get("addressCountry") or "")
                out["state"]  = out["state"]  or (st_v[:2] if st_v else "")
                out["zip"]    = out["zip"]    or (addr.get("postalCode") or "")
                if out["street"] and out["city"] and out["state"]:
                    return out
    except Exception:
        pass

    # Microdata / meta
    pats = [
        (r'"streetAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city"),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
        (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street"),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city"),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', "state"),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip"),
    ]
    for pat, key in pats:
        m = re.search(pat, html, re.I)
        if m and not out.get(key):
            out[key] = m.group(1).strip()

    return out

# ---------- Zillow URL helpers ----------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    If given a non-homedetails Zillow URL, fetch and try to find the canonical /homedetails/ link.
    """
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        html = r.text
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        # last resort: if page exposes "zpid" + address, synthesize
        mz = re.search(r'"zpid"\s*:\s*(\d+)', html)
        if mz:
            zpid = mz.group(1)
            addr = extract_address_from_html(html)
            street = addr.get("street") or ""
            city   = addr.get("city") or ""
            state  = addr.get("state") or ""
            parts = []
            if street: parts.append(street)
            if city and state: parts.append(f"{city} {state}")
            slug_src = " ".join(parts) or zpid
            slug = re.sub(r'[^A-Za-z0-9]+', '-', slug_src).strip('-').lower()
            return f"https://www.zillow.com/homedetails/{slug}/{zpid}_zpid/"
    except Exception:
        return url
    return url

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ---------- Microservice: Homespotter resolver ----------
def _call_hs_resolver(u: str, state_default: str = "NC") -> Tuple[Optional[str], Optional[str]]:
    """
    Calls your microservice /resolve and returns (best_url, human_address_str) or (None, None).
    Human address is "street city state zip" if available.
    """
    if not (HS_RESOLVER and u):
        return None, None
    try:
        r = requests.get(
            HS_RESOLVER,
            params={"u": u, "state_default": state_default, "try_homedetails": 1},
            headers=UA_HEADERS,
            timeout=REQUEST_TIMEOUT
        )
        j = r.json() if r.ok else {}
        best = j.get("best_url") or j.get("zillow_homedetails") or j.get("zillow_deeplink")
        a = j.get("address") or {}
        human = " ".join([p for p in [a.get("street",""), a.get("city",""), a.get("state",""), a.get("zip","")] if p]).strip() or None
        return (upgrade_to_homedetails_if_needed(best) if best else None), human
    except Exception:
        return None, None

# ---------- Safe deeplink builder (blocks "listing-10127718" style) ----------
_BANNED_STREET_RX = re.compile(r"\b(listing|mls)\b", re.I)
_ONLY_NUM_RX      = re.compile(r"^\s*\d{6,}\s*$")

def _sanitize_street_for_slug(street: str) -> str:
    s = (street or "").strip()
    if not s:
        return ""
    if _BANNED_STREET_RX.search(s):
        return ""
    if _ONLY_NUM_RX.match(s):
        return ""
    return s

def _slugify_for_homes(*parts: str) -> str:
    raw = ", ".join([p for p in parts if p]).lower()
    raw = re.sub(r"[^\w\s,-]", "", raw).replace(",", "")
    return re.sub(r"\s+", "-", raw.strip())

def construct_deeplink_from_parts(street, city, state, zipc, defaults) -> Optional[str]:
    """
    Builds /homes/..._rb/ from address parts, avoiding 'listing/MLS' or pure-MLS street.
    Returns URL or None if unsafe to build.
    """
    c   = (city or defaults.get("city","")).strip()
    stt = (state or defaults.get("state","")).strip()
    z   = (zipc  or defaults.get("zip","")).strip()

    street_clean = _sanitize_street_for_slug(street)
    if not stt or not (street_clean or c):
        return None

    if street_clean:
        slug = _slugify_for_homes(street_clean, ", ".join([p for p in [c, stt] if p]) + (f" {z}" if z else ""))
    else:
        slug = _slugify_for_homes(", ".join([p for p in [c, stt] if p]) + (f" {z}" if z else ""))

    if not slug or slug.startswith("listing") or slug.startswith("mls"):
        return None

    return f"https://www.zillow.com/homes/{slug}_rb/"

# ---------- Minimal image/meta enrichment ----------
RE_PRICE  = re.compile(r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS   = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS  = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT   = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)

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
    for target_w in ("1152","960","1536","768"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html, re.I
        )
        if m:
            return m.group(1)
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None

def parse_listing_meta(html: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not html:
        return meta
    m = RE_PRICE.search(html);   meta["price"]  = m.group(1) if m else None
    m = RE_STATUS.search(html);  meta["status"] = m.group(1) if m else None
    m = RE_BEDS.search(html);    meta["beds"]   = m.group(1) if m else None
    m = RE_BATHS.search(html);   meta["baths"]  = m.group(1) if m else None
    m = RE_SQFT.search(html);    meta["sqft"]   = m.group(1) if m else None
    img = extract_zillow_first_image(html)
    if not img:
        m3 = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
        if m3: img = m3.group(1)
    meta["image_url"] = img
    return meta

async def enrich_results_async(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targets = [(i, r["zillow_url"]) for i, r in enumerate(results) if "/homedetails/" in (r.get("zillow_url") or "")]
    if not targets:
        return results
    limits = min(10, max(3, len(targets)))
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

def picture_for_result(query_address: str, zurl: str, csv_photo_url: Optional[str] = None) -> Optional[str]:
    # CSV-provided photo wins
    if csv_photo_url and (csv_photo_url.startswith("http://") or csv_photo_url.startswith("https://") or csv_photo_url.startswith("data:")):
        return csv_photo_url
    # Try Zillow page
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.ok:
                html = r.text
                im = extract_zillow_first_image(html)
                if im:
                    return im
                m = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # Fallback: Street View
    key = GOOGLE_MAPS_API_KEY
    if key and (query_address or zurl):
        from urllib.parse import quote_plus
        loc = quote_plus(query_address or "")
        return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={key}"
    return None

# ---------- Parsers for pasted input ----------
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}
MLS_ID_KEYS = {"mls","mls id","mls_id","mls #","mls#","mls number","mlsnumber","listing id","listing_id"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

def norm_key(k: str) -> str:
    return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row: Dict[str, Any], keys) -> str:
    for k, v in row.items():
        if norm_key(k) in keys:
            s = str(v).strip()
            if s:
                return s
    return ""

def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Accepts CSV with headers, or one item per line (URL or address).
    """
    text = (text or "").strip()
    if not text:
        return []
    # Try CSV
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
    # Fallback: line by line
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

# ---------- Core resolver ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    """
    1) If Homespotter-like → call microservice first. If it returns, normalize & return.
    2) Else open the page, parse address (JSON-LD/microdata), build safe deeplink.
    3) If deeplink -> try to upgrade to /homedetails/.
    4) If not enough address → city/state deeplink; else return original URL.
    """
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # Homespotter first
    try:
        host = urlparse(final_url).hostname or ""
    except Exception:
        host = ""
    is_hs = any(h in (host or "") for h in ("l.hms.pt", "homespotter", "idx.homespotter.com"))
    if is_hs and HS_RESOLVER:
        best, human = _call_hs_resolver(final_url, state_default=defaults.get("state","NC") or "NC")
        if best:
            return best, (human or "")

    # Parse address from HTML
    addr = extract_address_from_html(html)
    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = addr.get("state","") or (defaults.get("state") or "")
    zipc   = addr.get("zip","") or ""

    deeplink = construct_deeplink_from_parts(street, city, state, zipc, defaults)

    if deeplink:
        z = upgrade_to_homedetails_if_needed(deeplink) or deeplink
        return z, " ".join([p for p in [street, city, state, zipc] if p]).strip()

    # City + State only as gentle fallback
    if city and state:
        z2 = construct_deeplink_from_parts("", city, state, zipc, defaults)
        if z2:
            z2 = upgrade_to_homedetails_if_needed(z2) or z2
            return z2, " ".join([p for p in [city, state, zipc] if p]).strip()

    # Nothing safe to build
    return final_url, ""

def process_single_row(row: Dict[str, Any], defaults: Dict[str,str]) -> Dict[str, Any]:
    """
    For address-only rows: attempt to parse & build a safe deeplink.
    For url rows: call resolve_from_source_url().
    """
    csv_photo = get_first_by_keys(row, PHOTO_KEYS)
    src_url = _detect_source_url(row)
    if src_url:
        zurl, inferred_addr = resolve_from_source_url(src_url, defaults)
        return {
            "input_address": inferred_addr or row.get("address","") or "",
            "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
            "zillow_url": zurl,
            "status": "source_url",
            "csv_photo": csv_photo,
            "preview_url": make_preview_url(zurl),
            "display_url": zurl,
        }

    # Address-only line: try to parse with usaddress if present
    addr_text = (row.get("address") or row.get("full_address") or "").strip()
    street = city = state = zipc = ""
    if usaddress and addr_text:
        try:
            parts, _ = usaddress.tag(addr_text)
            street = (parts.get("AddressNumber","") + " " +
                      " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip()).strip()
            city  = parts.get("PlaceName","") or ""
            state = parts.get("StateName","") or (defaults.get("state") or "")
            zipc  = parts.get("ZipCode","") or ""
        except Exception:
            street = addr_text
    else:
        street = addr_text

    deeplink = construct_deeplink_from_parts(street, city, state, zipc, defaults)
    if not deeplink and (city or state):
        deeplink = construct_deeplink_from_parts("", city, state, zipc, defaults)

    zurl = (upgrade_to_homedetails_if_needed(deeplink) if deeplink else "") or (deeplink or "")
    return {
        "input_address": addr_text,
        "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
        "zillow_url": zurl,
        "status": "address_line",
        "csv_photo": csv_photo,
        "preview_url": make_preview_url(zurl),
        "display_url": zurl,
    }

# ---------- Output builders ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, include_notes: bool = False):
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft"]
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
            if not u: 
                continue
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    lines = []
    for r in rows:
        u = pick_url(r)
        if u:
            lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")

# ---------- Results list + thumbs ----------
def results_list_with_copy_all(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href:
            continue
        li_html.append(
            f'<li style="margin:0.2rem 0;"><a href="{escape(href)}" target="_blank" rel="noopener">{escape(href)}</a></li>'
        )
    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u:
            copy_lines.append(u.strip())
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

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
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

def thumbnails_grid(results: List[Dict[str, Any]], columns: int = 3):
    if not results:
        return
    cols = st.columns(columns)
    for i, r in enumerate(results):
        u = (r.get("preview_url") or r.get("zillow_url") or "")
        img = r.get("image_url") or picture_for_result(r.get("input_address",""), u, r.get("csv_photo"))
        label = r.get("input_address") or r.get("mls_id") or ""
        with cols[i % columns]:
            if img:
                st.image(img, use_container_width=True)
            st.caption(label or u)

# ---------- Main UI ----------
def render_run_tab(state):
    st.header("Run")

    # Controls
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area("Paste addresses or links", placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718", height=160, label_visibility="collapsed")
    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")
    st.markdown('</div>', unsafe_allow_html=True)

    # Options
    c1, c2, c3 = st.columns([1,1,1.2])
    with c1:
        enrich_details = st.checkbox("Enrich images", value=True, help="Fetch a hero photo when possible.")
    with c2:
        show_table = st.checkbox("Show table", value=False)
    with c3:
        st.caption(("Resolver: ✅ configured" if HS_RESOLVER else "Resolver: ⚠️ missing HS_ADDRESS_RESOLVER_URL"))

    # Collect rows
    rows_in: List[Dict[str, Any]] = []
    # from CSV
    csv_rows_count = 0
    if file is not None:
        try:
            content = file.getvalue().decode("utf-8-sig")
            reader = list(csv.DictReader(io.StringIO(content)))
            csv_rows_count = len(reader)
            rows_in.extend(reader)
        except Exception:
            st.warning("Could not read CSV; please ensure UTF-8 / CSV with header.")
    # from paste
    if paste and paste.strip():
        rows_in.extend(_rows_from_paste(paste))

    # Run
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown('</div>', unsafe_allow_html=True)

    if clicked:
        if not rows_in:
            st.warning("Nothing to process.")
            return

        defaults = {"city":"", "state":"NC", "zip":""}  # Force NC unless provided
        total = len(rows_in)
        results: List[Dict[str, Any]] = []
        prog = st.progress(0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            try:
                out = process_single_row(row, defaults)
                results.append(out)
            except Exception as e:
                results.append({"input_address": row.get("address",""), "zillow_url": "", "status": f"error:{e}"})
            prog.progress(i/total, text=f"Resolved {i}/{total}")
        prog.progress(1.0, text="Done")

        # Normalize & optional enrichment
        for r in results:
            base = r.get("zillow_url") or ""
            r["preview_url"] = make_preview_url(base) if base else ""
            r["display_url"] = r["preview_url"] or base

        if enrich_details:
            try:
                results = asyncio.run(enrich_results_async(results))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                results = loop.run_until_complete(enrich_results_async(results))
                loop.close()

        # Results list
        st.subheader("Results")
        results_list_with_copy_all(results)

        # Table (optional)
        if show_table:
            import pandas as pd
            cols = ["display_url","zillow_url","status","mls_id","input_address","price","beds","baths","sqft"]
            df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
            st.dataframe(df, use_container_width=True, hide_index=True)

        # Images
        st.subheader("Images")
        thumbnails_grid(results, columns=3)

        # Export
        st.subheader("Download")
        fmt = st.radio("Format", ["txt","csv","md","html"], horizontal=True, index=0)
        payload, mime = build_output(results, fmt, include_notes=False)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        st.download_button("Export", data=payload, file_name=f"results_{ts}.{fmt}", mime=mime, use_container_width=True)

        st.divider()

    # ---------- Fix properties ----------
    st.subheader("Fix properties")
    st.caption("Paste any Zillow/Homespotter links. I’ll output clean /homedetails/ if possible, else a safe /homes/..._rb/ deeplink (never 'listing-…').")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        defaults = {"state":"NC", "city":"", "zip":""}
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                # Resolve via full pipeline
                z, _addr = resolve_from_source_url(best, defaults)
                best = z or best
                if best:
                    best = upgrade_to_homedetails_if_needed(best) or best
            except Exception:
                pass
            fixed.append((canonicalize_zillow(best)[0] if best else u))
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")

# (optional) Allow running this module directly for local testing:
if __name__ == "__main__":
    render_run_tab(st.session_state)
