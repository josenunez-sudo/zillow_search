# ui/run_tab.py
# Mobile-friendly link resolver with thumbnails:
# - Paste addresses or listing links (incl. Homespotter)
# - Upload MLS CSV (URL or address columns auto-detected; optional photo column)
# - Uses HS_ADDRESS_RESOLVER_URL microservice for Homespotter when set
# - Builds Zillow /homes/<slug>_rb/ and upgrades to /homedetails/ when possible
# - Shows an Images section for quick visual confirmation
# - Includes "Fix properties" tool

import os, re, io, csv, json
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, quote_plus, unquote

import requests
import streamlit as st
import streamlit.components.v1 as components

# Optional address normalization
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Config / Secrets ----------
def _get_secret(key: str, default: str = "") -> str:
    try:
        if key in st.secrets and st.secrets[key]:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return str(os.getenv(key, default)).strip()

HS_RESOLVER = _get_secret("HS_ADDRESS_RESOLVER_URL", "").rstrip("/")
GOOGLE_MAPS_API_KEY = _get_secret("GOOGLE_MAPS_API_KEY", "")

REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ---------- Small helpers ----------
def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

def _is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return any(k in h for k in ("l.hms.pt", "idx.homespotter.com", "homespotter"))
    except Exception:
        return False

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return (r.url or url), (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

# ---------- HTML → address ----------
def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html:
        return out
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            blob = (m.group(1) or "").strip()
            if not blob:
                continue
            try:
                data = json.loads(blob)
                if isinstance(data, list):
                    for d in data:
                        if isinstance(d, dict):
                            out.append(d)
                elif isinstance(data, dict):
                    out.append(data)
            except Exception:
                continue
    except Exception:
        pass
    return out

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Try to pull street/city/state/zip from JSON-LD, microdata, or meta/title fallbacks.
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}

    if not html:
        return out

    # JSON-LD first
    try:
        for blk in _jsonld_blocks(html):
            if not isinstance(blk, dict):
                continue
            addr = blk.get("address")
            if not addr and isinstance(blk.get("itemOffered"), dict):
                addr = blk["itemOffered"].get("address")
            if isinstance(addr, dict):
                out["street"] = out["street"] or (addr.get("streetAddress") or "").strip()
                out["city"]   = out["city"]   or (addr.get("addressLocality") or "").strip()
                st_or_cty = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()
                if st_or_cty and not out["state"]:
                    out["state"] = st_or_cty[:2].upper()
                out["zip"]    = out["zip"]    or (addr.get("postalCode") or "").strip()
                if out["street"] and (out["city"] or out["state"]):
                    break
    except Exception:
        pass

    # Direct JSON keys
    if not out["street"]:
        m = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I)
        if m: out["street"] = m.group(1).strip()
    if not out["city"]:
        m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I)
        if m: out["city"] = m.group(1).strip()
    if not out["state"]:
        m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I)
        if m: out["state"] = m.group(1).strip().upper()
    if not out["zip"]:
        m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I)
        if m: out["zip"] = m.group(1).strip()

    # Microdata itemprops
    if not out["street"]:
        m = re.search(r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', html, re.I)
        if m: out["street"] = m.group(1).strip()
    if not out["city"]:
        m = re.search(r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', html, re.I)
        if m: out["city"] = m.group(1).strip()
    if not out["state"]:
        m = re.search(r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', html, re.I)
        if m: out["state"] = m.group(1).strip().upper()
    if not out["zip"]:
        m = re.search(r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', html, re.I)
        if m: out["zip"] = m.group(1).strip()

    # Fallback from <title> / og:title when it looks like an address
    if not out["street"]:
        for pat in [
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<title>\s*([^<]+?)\s*</title>",
        ]:
            m = re.search(pat, html, re.I)
            if not m:
                continue
            title = (m.group(1) or "").strip()
            if re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\b\d{5}\b", title):
                out["street"] = title
                break

    return out

# ---------- Zillow helpers ----------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r'[?#].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', base, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(base)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base

def zillow_slugify(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\s,-]", "", t).replace(",", "")
    t = re.sub(r"\s+", "-", t.strip())
    return t

def zillow_deeplink_from_addr(street: str, city: str, state: str, zipc: str) -> str:
    parts: List[str] = []
    if street:
        parts.append(street)
    loc = ", ".join([p for p in [city, state] if p])
    if loc:
        parts.append(loc)
    if zipc:
        if parts:
            parts[-1] = (parts[-1] + f" {zipc}").strip()
        else:
            parts.append(zipc)
    slug = zillow_slugify(", ".join(parts))
    return f"https://www.zillow.com/homes/{slug}_rb/" if slug else "https://www.zillow.com/homes/"

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    For Zillow /homes/*_rb/ pages, try to find the canonical /homedetails/*/_zpid/ link in-page.
    If not found, return the original URL.
    """
    if not url or "/homedetails/" in url or "zillow.com" not in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if not r.ok:
            return url
        html = r.text or ""
        # Direct homedetails link
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        # Canonical
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return url

# ---------- Image helpers ----------
def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html:
        return None
    for target_w in ("1152","960","768","1536"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html, re.I
        )
        if m: return m.group(1)
    m = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
    if m: return m.group(1)
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None

def picture_for_result_with_log(query_address: str, zurl: str, csv_photo_url: Optional[str] = None):
    """
    Returns (image_url_or_None, log_dict)
    Priority: CSV-provided photo > Zillow hero/meta > Google Street View (if key) > None
    """
    log = {
        "url": zurl, "csv_provided": bool(csv_photo_url), "stage": None,
        "status_code": None, "html_len": None, "selected": None, "errors": []
    }

    def _ok(u: Optional[str]) -> bool:
        return isinstance(u, str) and u.startswith(("http://","https://","data:"))

    # 1) CSV photo (if provided)
    if csv_photo_url and _ok(csv_photo_url):
        log["stage"] = "csv_photo"; log["selected"] = csv_photo_url
        return csv_photo_url, log

    # 2) Zillow hero/meta if homedetails
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            log["status_code"] = r.status_code
            if r.ok:
                html = r.text
                log["html_len"] = len(html)
                zfirst = extract_zillow_first_image(html)
                if zfirst:
                    log["stage"] = "zillow_hero"; log["selected"] = zfirst
                    return zfirst, log
        except Exception as e:
            log["errors"].append(f"fetch_err:{e!r}")

    # 3) Google Street View fallback
    try:
        key = GOOGLE_MAPS_API_KEY
        if key and query_address:
            from urllib.parse import quote_plus
            loc = quote_plus(query_address)
            sv = f"https://maps.googleapis.com/maps/api/streetview?size=640x400&location={loc}&key={key}"
            log["stage"] = "street_view"; log["selected"] = sv
            return sv, log
        else:
            if not key: log["errors"].append("no_google_maps_key")
    except Exception as e:
        log["errors"].append(f"sv_err:{e!r}")

    log["stage"] = "none"
    return None, log

@st.cache_data(ttl=900, show_spinner=False)
def get_thumbnail_and_log(query_address: str, zurl: str, csv_photo_url: Optional[str]):
    return picture_for_result_with_log(query_address, zurl, csv_photo_url)

# ---------- Homespotter microservice ----------
def resolve_hs_address_via_service(hs_url: str) -> Optional[Dict[str, Any]]:
    """
    Call your Worker/Tunnel that returns:
    { ok: true, address: {street,city,state,zip}, zillow_candidate?: "https://www.zillow.com/homes/<slug>_rb/" }
    """
    if not (HS_RESOLVER and hs_url):
        return None
    try:
        resp = requests.get(f"{HS_RESOLVER}?u={quote_plus(hs_url)}", timeout=REQUEST_TIMEOUT)
        if resp.ok:
            return resp.json()
    except Exception:
        return None
    return None

# ---------- Resolution core ----------
def resolve_from_source_url(source_url: str, state_default: str = "NC") -> Tuple[str, str]:
    """
    Returns (zillow_url, inferred_address_text)
    - If Homespotter link and resolver is configured, use it first
    - Else, expand and parse HTML for address; build a Zillow /homes/<slug>_rb/ deeplink
    - Then try to upgrade to /homedetails/ if the page exposes canonical
    """
    if not source_url:
        return "", ""

    # Homespotter → service first
    if _is_homespotter_like(source_url) and HS_RESOLVER:
        data = resolve_hs_address_via_service(source_url)
        if data and data.get("ok"):
            a = data.get("address") or {}
            z = data.get("zillow_candidate") or ""
            street = (a.get("street") or "").strip()
            city   = (a.get("city") or "").strip()
            state  = (a.get("state") or "").strip() or state_default
            zipc   = (a.get("zip") or "").strip()
            zurl = z or zillow_deeplink_from_addr(street, city, state, zipc)
            zurl = upgrade_to_homedetails_if_needed(zurl)
            inferred = " ".join([x for x in [street, city, state, zipc] if x])
            return zurl, inferred

    # Generic page parse
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    addr = extract_address_from_html(html)
    street = (addr.get("street") or "").strip()
    city   = (addr.get("city") or "").strip()
    state  = (addr.get("state") or "").strip() or state_default
    zipc   = (addr.get("zip") or "").strip()

    if street or city or state or zipc:
        z = zillow_deeplink_from_addr(street, city, state, zipc)
        z = upgrade_to_homedetails_if_needed(z)
        inferred = " ".join([x for x in [street, city, state, zipc] if x])
        return z, inferred

    # Fallback
    return final_url, ""

# ---------- Input parsing ----------
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}

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

# Allow CSV-provided photo columns to drive the image
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

def norm_key(k: str) -> str:
    return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row: Dict[str, Any], keys_set) -> str:
    for k, v in row.items():
        if norm_key(k) in keys_set:
            s = str(v).strip() if v is not None else ""
            if s:
                return s
    return ""

def compose_address_from_row(row: Dict[str, Any], default_state: str = "NC") -> str:
    """
    Build a single-line address from possible CSV columns.
    """
    n = { norm_key(k): (str(v).strip() if v is not None else "") for k, v in row.items() }

    # Full-address style field?
    for k in list(n.keys()):
        if k in ADDR_PRIMARY and n[k]:
            return n[k]

    # Components
    num   = get_first_by_keys(n, NUM_KEYS)
    name  = get_first_by_keys(n, NAME_KEYS)
    suf   = get_first_by_keys(n, SUF_KEYS)
    city  = get_first_by_keys(n, CITY_KEYS)
    state = get_first_by_keys(n, STATE_KEYS) or default_state
    zipc  = get_first_by_keys(n, ZIP_KEYS)

    street = " ".join([x for x in [num, name, suf] if x]).strip()
    parts = [street]
    if city or state:
        parts.append(", ".join([p for p in [city, state] if p]))
    if zipc:
        if parts:
            parts[-1] = (parts[-1] + f" {zipc}").strip()
        else:
            parts.append(zipc)
    return re.sub(r"\s+"," ", ", ".join([p for p in parts if p]).strip())

def parse_csv_uploaded(file, default_state: str = "NC") -> List[Dict[str, str]]:
    """
    Returns list of dicts with either {"url": "..."} OR {"address": "..."} (+ optional "photo")
    """
    rows: List[Dict[str, str]] = []
    try:
        text = file.getvalue().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        for raw in reader:
            row = {k: ("" if v is None else str(v)) for k, v in raw.items()}
            # URL first (any of URL_KEYS)
            url = ""
            for k, v in row.items():
                if norm_key(k) in URL_KEYS and is_probable_url(v):
                    url = v.strip()
                    break
            photo = get_first_by_keys(row, PHOTO_KEYS)

            if url:
                item = {"url": url}
                if photo:
                    item["photo"] = photo
                rows.append(item)
                continue

            # Else: compose address from columns
            addr = compose_address_from_row(row, default_state)
            if addr:
                item = {"address": addr}
                if photo:
                    item["photo"] = photo
                rows.append(item)
    except Exception:
        pass
    return rows

def _list_to_rows(text: str, default_state: str = "NC") -> List[Dict[str, str]]:
    """
    Accept CSV-like text with header (url/address), or plain text (one URL/address per line).
    """
    text = (text or "").strip()
    if not text:
        return []

    # CSV path (quick sniff)
    lines = text.splitlines()
    if len(lines) >= 2 and ("," in lines[0] or "\t" in lines[0]):
        try:
            dialect = csv.Sniffer().sniff(lines[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            rows = []
            for r in reader:
                row = {k: ("" if v is None else str(v)) for k, v in r.items()}
                url = ""
                for k, v in row.items():
                    if norm_key(k) in URL_KEYS and is_probable_url(v):
                        url = v.strip()
                        break
                photo = get_first_by_keys(row, PHOTO_KEYS)

                if url:
                    item = {"url": url}
                    if photo: item["photo"] = photo
                    rows.append(item)
                    continue

                addr = compose_address_from_row(row, default_state)
                if addr:
                    item = {"address": addr}
                    if photo: item["photo"] = photo
                    rows.append(item)
            if rows:
                return rows
        except Exception:
            pass

    # Plain list
    out: List[Dict[str, str]] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if is_probable_url(s):
            out.append({"url": s})
        else:
            # optional normalize with usaddress
            if usaddress:
                try:
                    parts, _ = usaddress.tag(s)
                    street = " ".join([
                        parts.get("AddressNumber",""),
                        parts.get("StreetNamePreDirectional",""),
                        parts.get("StreetName",""),
                        parts.get("StreetNamePostType",""),
                        parts.get("OccupancyType",""),
                        parts.get("OccupancyIdentifier",""),
                    ]).strip()
                    city  = parts.get("PlaceName","")
                    state = parts.get("StateName","") or default_state
                    zipc  = parts.get("ZipCode","")
                    s = ", ".join([p for p in [street, ", ".join([p for p in [city, state] if p])] if p])
                    if zipc:
                        s = (s + f" {zipc}").strip()
                except Exception:
                    pass
            out.append({"address": s})
    return out

# ---------- Output helpers ----------
def _result_item_html(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    esc = re.sub(r'"', "&quot;", u)
    return f'<li style="margin:0.25rem 0;"><a href="{esc}" target="_blank" rel="noopener">{esc}</a></li>'

def results_list_with_copy_all(urls: List[str]):
    items_html = "\n".join(_result_item_html(u) for u in urls if u)
    if not items_html:
        items_html = "<li>(no results)</li>"
    copy_text = "\n".join([u for u in urls if u]) + ("\n" if urls else "")
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
        <ul class="link-list">{items_html}</ul>
      </div>
      <script>
        (function(){{
          const btn = document.getElementById('copyAll');
          const text = `{copy_text}`;
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
    </body></html>
    """
    components.html(html, height=min(700, 40 * max(1, len(urls)) + 30), scrolling=False)

def build_export_payload(urls: List[str], fmt: str) -> Tuple[bytes, str, str]:
    """
    Returns (data_bytes, mime, default_ext)
    """
    fmt = (fmt or "txt").lower()
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["url"])
        for u in urls:
            if u:
                w.writerow([u])
        return buf.getvalue().encode("utf-8"), "text/csv", "csv"
    elif fmt == "md":
        body = "\n".join([f"- {u}" for u in urls if u]) + ("\n" if urls else "")
        return body.encode("utf-8"), "text/markdown", "md"
    elif fmt == "html":
        body = "<ul>\n" + "\n".join([f'<li><a href="{u}" target="_blank" rel="noopener">{u}</a></li>' for u in urls if u]) + "\n</ul>\n"
        return body.encode("utf-8"), "text/html", "html"
    else:
        body = "\n".join([u for u in urls if u]) + ("\n" if urls else "")
        return body.encode("utf-8"), "text/plain", "txt"

# ---------- Thumbnails UI ----------
def _thumb_cell(url: str, addr: str, csv_photo: Optional[str]) -> str:
    img, _log = get_thumbnail_and_log(addr or "", url or "", csv_photo)
    safe_u = re.sub(r'"', "&quot;", (url or ""))
    if img:
        safe_img = re.sub(r'"', "&quot;", img)
        return f'''
        <div style="border:1px solid rgba(0,0,0,.08);border-radius:14px;padding:8px;margin-bottom:10px">
          <a href="{safe_u}" target="_blank" rel="noopener">
            <img src="{safe_img}" alt="thumbnail" style="width:100%;height:auto;border-radius:12px"/>
          </a>
          <div style="font-size:12px;opacity:.8;margin-top:6px">{(addr or "")}</div>
        </div>'''
    return f'''
    <div style="border:1px solid rgba(0,0,0,.08);border-radius:14px;padding:8px;margin-bottom:10px">
      <a href="{safe_u}" target="_blank" rel="noopener">{safe_u}</a>
      <div style="font-size:12px;opacity:.8;margin-top:6px">{(addr or "")}</div>
    </div>'''

def _thumbnails_grid(results: List[Dict[str, Any]], columns: int = 3):
    if not results:
        return
    cols = st.columns(columns)
    for i, r in enumerate(results):
        url = r.get("zurl") or ""
        addr = r.get("addr") or ""
        csv_photo = r.get("csv_photo") or None
        html = _thumb_cell(url, addr, csv_photo)
        with cols[i % columns]:
            st.markdown(html, unsafe_allow_html=True)

# ---------- Main render ----------
def render_run_tab(state):  # keep this signature for your app loader
    st.header("Run")

    colA, colB, colC = st.columns([1,1,1])
    with colA:
        default_state = st.text_input("Default state (if missing)", value="NC")
    with colB:
        show_preview = st.checkbox("Show preview of paste", value=True)
    with colC:
        show_images = st.checkbox("Show images", value=True)

    # Paste + CSV
    st.caption("Paste *addresses or listing links* and/or upload an **MLS CSV**.")
    paste = st.text_area("Paste", height=160, placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718", label_visibility="collapsed")
    file = st.file_uploader("Upload CSV", type=["csv"], accept_multiple_files=False)

    pasted_rows = _list_to_rows(paste, default_state=default_state) if paste.strip() else []
    csv_rows = parse_csv_uploaded(file, default_state=default_state) if file else []

    if show_preview:
        cnt_paste = len(pasted_rows)
        cnt_csv = len(csv_rows)
        bits = []
        if cnt_paste: bits.append(f"**{cnt_paste}** pasted")
        if cnt_csv:   bits.append(f"**{cnt_csv}** from CSV")
        if bits:
            st.caption(" • ".join(bits))

    if st.button("🚀 Resolve", type="primary", use_container_width=True):
        if not pasted_rows and not csv_rows:
            st.warning("Nothing to process.")
            return

        if not HS_RESOLVER:
            st.info("Homespotter resolver not configured (HS_ADDRESS_RESOLVER_URL). Homespotter links will be handled via basic HTML parsing only.")

        all_rows = pasted_rows + csv_rows
        results: List[Dict[str, Any]] = []  # each: {"zurl","addr","csv_photo"}
        urls_out: List[str] = []

        prog = st.progress(0, text="Resolving…")
        total = len(all_rows) if all_rows else 1

        for i, r in enumerate(all_rows, start=1):
            csv_photo = r.get("photo") or None
            if r.get("url"):
                z, addr = resolve_from_source_url(r["url"], state_default=(default_state or "NC"))
                z = z or r["url"]
                results.append({"zurl": z, "addr": addr, "csv_photo": csv_photo})
                urls_out.append(z)
            else:
                addr = (r.get("address") or "").strip()
                if addr:
                    z = "https://www.zillow.com/homes/" + zillow_slugify(addr) + "_rb/"
                    z = upgrade_to_homedetails_if_needed(z)
                    results.append({"zurl": z, "addr": addr, "csv_photo": csv_photo})
                    urls_out.append(z)
            prog.progress(i/total, text=f"Resolved {i}/{total}")
        prog.progress(1.0, text="Done")

        st.subheader("Results")
        results_list_with_copy_all(urls_out)

        if show_images:
            st.markdown("#### Images")
            _thumbnails_grid(results, columns=3)

        st.markdown("**Download**")
        fmt = st.selectbox("Format", ["txt","csv","md","html"], index=0, key="dl_fmt")
        data, mime, ext = build_export_payload(urls_out, fmt)
        st.download_button(
            "Download",
            data=data,
            file_name=f"resolved_links.{ext}",
            mime=mime,
            use_container_width=True,
        )

    st.divider()

    # ---------- Fix links ----------
    st.subheader("Fix properties")
    st.caption("Paste any listing links. I’ll output clean canonical **/homedetails/** Zillow URLs when possible.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")
    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        total = max(1, len(lines))
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                if "zillow.com" not in (best or ""):
                    z, _addr = resolve_from_source_url(best, state_default="NC")
                    best = z or best
                best = upgrade_to_homedetails_if_needed(best)
            except Exception:
                pass
            fixed.append(best)
            prog.progress(i / total, text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{u}]({u})" for u in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")

# Allow running this module directly for local checks
if __name__ == "__main__":
    render_run_tab({})
