# ui/run_tab.py
# Streamlit "Run" tab — paste addresses and listing links (incl. Homespotter) → Zillow links.
# CSV upload supported. Microservice-first resolver for Homespotter. Lightweight, mobile-friendly.
# This version avoids bad outputs like .../homes/nc_rb/ or nc-nc_rb by requiring a real address.

import os, io, re, csv, json
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, quote_plus

import requests
import streamlit as st
import streamlit.components.v1 as components

# =========================
# Config & constants
# =========================

def _get_secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets and st.secrets[name]:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)

HS_ADDRESS_RESOLVER_URL = _get_secret("HS_ADDRESS_RESOLVER_URL", "").rstrip("/")
GOOGLE_MAPS_API_KEY     = _get_secret("GOOGLE_MAPS_API_KEY", "")
DEFAULT_STATE           = _get_secret("DEFAULT_STATE", "NC").strip() or "NC"

REQUEST_TIMEOUT = 15
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# =========================
# Small helpers
# =========================

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

def _is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return ("l.hms.pt" in h) or ("homespotter" in h) or ("idx.homespotter.com" in h)
    except Exception:
        return False

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def _jsonloads_silent(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None

JSONLD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)

def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html:
        return out
    for m in JSONLD_RE.finditer(html):
        data = _jsonloads_silent(m.group(1))
        if isinstance(data, list):
            out.extend([d for d in data if isinstance(d, dict)])
        elif isinstance(data, dict):
            out.append(data)
    return out

def _clean(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Heuristically extract address from arbitrary HTML (JSON-LD first, then inline json/meta).
    Returns dict with keys: street, city, state, zip (any may be empty).
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}

    # 1) JSON-LD
    for b in _jsonld_blocks(html):
        addr = None
        if isinstance(b, dict):
            addr = b.get("address")
            if not isinstance(addr, dict):
                io = b.get("itemOffered") if isinstance(b.get("itemOffered"), dict) else None
                if io:
                    addr = io.get("address") if isinstance(io.get("address"), dict) else None
        if isinstance(addr, dict):
            out["street"] = out["street"] or _clean(addr.get("streetAddress"))
            out["city"]   = out["city"]   or _clean(addr.get("addressLocality"))
            reg = _clean(addr.get("addressRegion") or addr.get("addressCountry"))
            if reg and len(reg) >= 2:
                out["state"] = out["state"] or reg[:2]
            out["zip"]    = out["zip"]    or _clean(addr.get("postalCode"))
            if out["street"] and out["city"] and out["state"]:
                return out

    # 2) Inline JSON common patterns
    pats = [
        (r'"streetAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city"),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
        (r'"street"\s*:\s*"([^"]+)"', "street"),
        (r'"city"\s*:\s*"([^"]+)"', "city"),
        (r'"state(?:OrProvince)?"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'"postal(?:Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
    ]
    for rx, key in pats:
        m = re.search(rx, html, re.I)
        if m and not out.get(key):
            out[key] = _clean(m.group(1))[:2] if key == "state" else _clean(m.group(1))

    # 3) Meta/title with full-line address
    if not out.get("street"):
        for rx in [
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<title>\s*([^<]+?)\s*</title>",
            r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
        ]:
            m = re.search(rx, html, re.I)
            if m:
                t = _clean(m.group(1))
                if re.search(r"\b[A-Za-z]{2}\b", t) and re.search(r"\d{5}(?:-\d{4})?", t):
                    out["street"] = t.split(",")[0]
                    if not out["city"]:
                        after = _clean(re.sub(r".*?,\s*", "", t))
                        out["city"] = after.split(",")[0]
                    if not out["state"]:
                        sm = re.search(r"\b([A-Za-z]{2})\b", t)
                        if sm:
                            out["state"] = sm.group(1)
                    if not out["zip"]:
                        zm = re.search(r"\b(\d{5}(?:-\d{4})?)\b", t)
                        if zm:
                            out["zip"] = zm.group(1)
                    break

    return out

ADDR_TAIL_ZIP = re.compile(r"(,?\s*)\b(\d{5})(-\d{4})?\b\s*$")
ADDR_TAIL_STATE = re.compile(r"(,?\s*)\b([A-Za-z]{2})\b\s*$")

def parse_freeform_address_components(s: str, default_state: str = "NC") -> Tuple[str, str, str, str]:
    """
    Very light freeform parser. Input like:
      '407 E Woodall St, Smithfield, NC 27577'
      '4 beds 2 baths for $265000 in Erwin, NC'
    Returns (street, city, state, zip) — any may be "".
    """
    s0 = _clean(s)
    if not s0:
        return "", "", "", ""

    # remove ZIP at end
    zipc = ""
    m = ADDR_TAIL_ZIP.search(s0)
    if m:
        zipc = m.group(2)
        s0 = s0[:m.start()].strip(", ")

    # remove state at end
    state = ""
    m = ADDR_TAIL_STATE.search(s0)
    if m:
        state = m.group(2).upper()
        s0 = s0[:m.start()].strip(", ")

    # split by commas for street/city guess
    parts = [p.strip() for p in s0.split(",") if p.strip()]

    street = ""
    city = ""

    if len(parts) >= 2:
        street = parts[0]
        city = parts[1]
    elif len(parts) == 1:
        # Heuristic: if it starts with number, assume street
        if re.match(r"^\d+\b", parts[0]):
            street = parts[0]
        else:
            city = parts[0]

    # final clean-ups
    if not state:
        state = default_state
    # If the "street" looks like just the state token, drop it
    if street.upper() == state:
        street = ""
    # If the "city" is the same as state, drop it
    if city.upper() == state:
        city = ""

    return street, city, state, zipc

def build_zillow_deeplink(street: str, city: str, state: str, zipc: str, default_state: str = "NC") -> Optional[str]:
    """
    Compose a Zillow /homes/..._rb/ URL from address components.
    IMPORTANT: If we only know the state (no street/city/zip), return None to avoid '.../homes/nc_rb/'.
    Also dedupe cases like street 'NC' + state 'NC' (which yielded nc-nc).
    """
    street = _clean(street)
    city   = _clean(city)
    state  = (_clean(state) or default_state).upper()
    zipc   = _clean(zipc)

    # Nothing but state? don't build.
    if not street and not city and not zipc:
        return None

    # Avoid duplicate 'NC' pieces
    tokens: List[str] = []
    if street and street.upper() != state:
        tokens.append(street)
    loc = " ".join([p for p in [city, state] if p]).strip()
    if loc and loc.upper() != state.upper():
        tokens.append(loc)
    elif city or state:
        tokens.append(loc)  # at least state present with city possibly empty

    if zipc:
        tokens.append(zipc)

    # As a final guard: if tokens collapsed to just the state, abort
    joined = ", ".join([t for t in tokens if t])
    if not joined or joined.strip().upper() == state:
        return None

    s = joined.lower()
    s = re.sub(r"[^\w\s,-/]", "", s).replace(",", "")
    s = re.sub(r"\s+", "-", s.strip())
    return f"https://www.zillow.com/homes/{s}_rb/"

def is_bad_nc_only(u: Optional[str]) -> bool:
    if not u:
        return True
    return bool(re.search(r"/homes/(nc(?:-nc){0,3})_rb/?$", u, re.I))

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    Try to turn an /homes/..._rb/ link into /homedetails/.../_zpid/ by scanning the RB page for a homedetails href.
    """
    if not url or "/homedetails/" in url or "/homes/" not in url:
        return url or ""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        html = r.text
        # direct anchors to /homedetails/
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        # canonical link
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

ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)
def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url:
        return "", None
    base = re.sub(r'[?#].*$', '', url.strip())
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

def picture_for_result(query_address: str, zurl: str, csv_photo_url: Optional[str] = None) -> Optional[str]:
    """
    Try CSV photo -> Zillow og:image -> Google Street View (requires key) -> None
    (We don't fetch Zillow hero sizes here to keep it lightweight.)
    """
    def _ok(u: Optional[str]) -> bool:
        return isinstance(u, str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))

    if _ok(csv_photo_url):
        return csv_photo_url

    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.ok:
                html = r.text
                m = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass

    if GOOGLE_MAPS_API_KEY and query_address:
        loc = quote_plus(query_address)
        return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={GOOGLE_MAPS_API_KEY}"

    return None

# =========================
# Resolver integrations
# =========================

def resolve_hs_to_zillow(url: str, default_state: str = "NC") -> Tuple[str, str]:
    """
    Calls the microservice to render a Homespotter page, pull the address (and components),
    and build a Zillow link. Returns (best_zillow_url, address_text).
    """
    if not HS_ADDRESS_RESOLVER_URL:
        return "", ""

    try:
        r = requests.get(HS_ADDRESS_RESOLVER_URL, params={"u": url}, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return "", ""
        data = r.json()

        # Accept flexible field names from the microservice
        street = _clean(data.get("street") or data.get("address1") or data.get("line1"))
        city   = _clean(data.get("city") or data.get("locality"))
        state  = _clean(data.get("state") or data.get("region") or default_state)
        zipc   = _clean(data.get("zip") or data.get("postal") or data.get("postal_code"))

        addr_text = _clean(data.get("address_text") or " ".join([p for p in [street, city, state, zipc] if p]))

        z_canon = _clean(data.get("zillow_canonical"))
        z_rb    = _clean(data.get("zillow_deeplink"))

        best = z_canon or z_rb

        # If service didn't give a Zillow link, build from address components/text
        if not best:
            if not (street or city or zipc):
                # parse any freeform address_text
                p_street, p_city, p_state, p_zip = parse_freeform_address_components(addr_text, default_state=default_state)
                street, city, state, zipc = street or p_street, city or p_city, state or p_state, zipc or p_zip
            built = build_zillow_deeplink(street, city, state or default_state, zipc, default_state=default_state)
            best = built or ""

        # Upgrade / sanitize
        if best:
            best = upgrade_to_homedetails_if_needed(best)
            canon, _ = canonicalize_zillow(best)
            best = canon or best
            if is_bad_nc_only(best):
                best = ""

        return best, addr_text
    except Exception:
        return "", ""

def resolve_generic_url(source_url: str, default_state: str = "NC") -> Tuple[str, str]:
    """
    Expand an arbitrary URL, try to scrape an address, build Zillow RB deeplink, then try to upgrade to homedetails.
    Returns (zillow_url, address_text). If we only have state → returns ("","") to avoid .../homes/nc_rb/.
    """
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    addr = extract_address_from_html(html)
    street = addr.get("street", "")
    city   = addr.get("city", "")
    state  = addr.get("state", "") or default_state
    zipc   = addr.get("zip", "")

    addr_text = " ".join([p for p in [street, city, state, zipc] if p]).strip()

    if not (street or city or zipc):
        # parse any title-ish address again (best-effort)
        p_street, p_city, p_state, p_zip = parse_freeform_address_components(addr_text, default_state=default_state)
        street, city, state, zipc = street or p_street, city or p_city, state or p_state, zipc or p_zip

    deeplink = build_zillow_deeplink(street, city, state, zipc, default_state=default_state) or ""
    if deeplink:
        deeplink = upgrade_to_homedetails_if_needed(deeplink)
        canon, _ = canonicalize_zillow(deeplink)
        deeplink = canon or deeplink
        if is_bad_nc_only(deeplink):
            deeplink = ""

    return (deeplink or ""), addr_text

# =========================
# CSV & paste parsing
# =========================

URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}
ADDR_KEYS  = {"address","full_address","site address","site_address","street address","street_address","listing address","listing_address","location"}

def norm_key(k: str) -> str:
    return re.sub(r"\s+", " ", (k or "").strip().lower())

def get_first_by_keys(row: Dict[str, Any], keys) -> str:
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v:
                return v
    return ""

def rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Accept CSV with headers or loose lines (one per line: URL or address).
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV
    try:
        lines = text.splitlines()
        if len(lines) >= 2 and ("," in lines[0] or "\t" in lines[0] or ";" in lines[0]):
            # auto-detect delimiter
            dialect = csv.Sniffer().sniff(lines[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            rows = [dict(r) for r in reader]
            if rows:
                return rows
    except Exception:
        pass

    # Simple lines
    out: List[Dict[str, Any]] = []
    for ln in text.splitlines():
        s = (ln or "").strip()
        if not s:
            continue
        if is_probable_url(s):
            out.append({"url": s})
        else:
            out.append({"address": s})
    return out

# =========================
# UI — Run Tab
# =========================

def _results_list_with_copy_all(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href:
            continue
        safe_href = escape(href)
        link_txt = href
        li_html.append(
            f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a></li>'
        )
    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u:
            copy_lines.append(u.strip())
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
    est_h = max(60, min(34 * max(1, len(li_html)) + 20, 700))
    components.html(html, height=est_h, scrolling=False)

def _thumbnails_grid(results: List[Dict[str, Any]], columns: int = 2):
    if not results:
        return
    cols = st.columns(columns)
    for i, r in enumerate(results):
        u = r.get("preview_url") or r.get("zillow_url") or ""
        addr = r.get("input_address") or ""
        img = r.get("image_url")
        if not img:
            img = picture_for_result(addr, u, r.get("csv_photo"))
        with cols[i % columns]:
            if img:
                st.image(img, use_container_width=True)
            st.caption(addr or u)

def render_run_tab(state: Optional[dict] = None):
    st.header("Run")
    if not HS_ADDRESS_RESOLVER_URL:
        st.warning("Homespotter resolver: **HS_ADDRESS_RESOLVER_URL** not set. Homespotter links will not be auto-resolved.", icon="⚠️")

    st.markdown("Paste **addresses** or **listing links** (Homespotter, etc.). Upload CSVs too.")
    paste = st.text_area("Paste here (one per line)", height=160,
                         placeholder="e.g.\n407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718")

    upcol1, upcol2 = st.columns([1,1])
    with upcol1:
        csv_file = st.file_uploader("Upload CSV (address or url column)", type=["csv"])
    with upcol2:
        try_homedetails = st.checkbox("Try to upgrade to /homedetails/", value=True)

    # Parse rows from paste
    rows_in: List[Dict[str, Any]] = rows_from_paste(paste)

    # Append CSV rows
    if csv_file is not None:
        try:
            content = csv_file.getvalue().decode("utf-8-sig")
            # Detect delimiter safely
            try:
                sample = content.splitlines()[0]
                dialect = csv.Sniffer().sniff(sample)
                reader = csv.DictReader(io.StringIO(content), dialect=dialect)
            except Exception:
                reader = csv.DictReader(io.StringIO(content))
            rows_in.extend(list(reader))
        except Exception as e:
            st.error(f"CSV parse error: {e}")

    # Run
    if st.button("🚀 Run", type="primary", use_container_width=True):
        if not rows_in:
            st.info("Nothing to process.")
            return

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")
        total = len(rows_in)

        for i, row in enumerate(rows_in, start=1):
            src_url = ""
            for k, v in row.items():
                if norm_key(k) in URL_KEYS and is_probable_url(str(v)):
                    src_url = str(v).strip()
                    break
            if not src_url and "url" in row and is_probable_url(str(row.get("url", ""))):
                src_url = str(row["url"]).strip()

            csv_photo = get_first_by_keys(row, PHOTO_KEYS)
            input_address = ""
            best = ""

            if src_url:
                if _is_homespotter_like(src_url):
                    z, addr = resolve_hs_to_zillow(src_url, default_state=DEFAULT_STATE)
                    # If service returned no Zillow but we did get an address, build from that address
                    if not z and addr:
                        stt, cty, sta, zc = parse_freeform_address_components(addr, default_state=DEFAULT_STATE)
                        z = build_zillow_deeplink(stt, cty, sta or DEFAULT_STATE, zc, default_state=DEFAULT_STATE) or ""
                    # If still nothing, generic scrape attempt
                    if not z:
                        z2, addr2 = resolve_generic_url(src_url, default_state=DEFAULT_STATE)
                        if z2:
                            z, addr = z2, (addr or addr2)
                    input_address = addr
                    best = z or ""
                else:
                    z, addr = resolve_generic_url(src_url, default_state=DEFAULT_STATE)
                    input_address = addr
                    best = z or ""
            else:
                # pure address row
                raw_addr = get_first_by_keys(row, ADDR_KEYS) or (row.get("address") or "")
                input_address = raw_addr
                stt, cty, sta, zc = parse_freeform_address_components(raw_addr, default_state=DEFAULT_STATE)
                z = build_zillow_deeplink(stt, cty, sta or DEFAULT_STATE, zc, default_state=DEFAULT_STATE) or ""
                best = z

            # Upgrade / sanitize
            if try_homedetails and best:
                best = upgrade_to_homedetails_if_needed(best)
            if best:
                canon, _ = canonicalize_zillow(best)
                best = canon or best
            if is_bad_nc_only(best):
                best = ""  # drop useless /homes/nc... links

            results.append({
                "input_address": input_address,
                "zillow_url": best,
                "preview_url": make_preview_url(best) if best else "",
                "display_url": best,
                "csv_photo": csv_photo,
            })

            prog.progress(i / total, text=f"Resolved {i}/{total}")

        prog.progress(1.0, text="Done")

        st.subheader("Results")
        if not any(r.get("zillow_url") for r in results):
            st.warning("No solid Zillow links could be built from the provided inputs.")
        _results_list_with_copy_all(results)

        st.subheader("Images (confirmation)")
        _thumbnails_grid(results, columns=2)

        # Export
        st.subheader("Export")
        fmt = st.radio("Format", ["txt", "md", "html", "csv"], horizontal=True, index=0)

        def _build_output(rows: List[Dict[str, Any]], fmt: str):
            def pick_url(r):
                return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

            if fmt == "csv":
                fields = ["input_address", "url"]
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=fields)
                w.writeheader()
                for r in rows:
                    w.writerow({"input_address": r.get("input_address") or "", "url": pick_url(r)})
                return buf.getvalue(), "text/csv", "results.csv"

            if fmt == "html":
                items = []
                for r in rows:
                    u = pick_url(r)
                    if u:
                        items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
                html = "<ul>\n" + "\n".join(items) + "\n</ul>\n"
                return html, "text/html", "results.html"

            if fmt == "md":
                lines = []
                for r in rows:
                    u = pick_url(r)
                    if u:
                        lines.append(f"- {u}")
                md = "\n".join(lines) + ("\n" if lines else "")
                return md, "text/markdown", "results.md"

            # txt
            lines = []
            for r in rows:
                u = pick_url(r)
                if u:
                    lines.append(u)
            txt = "\n".join(lines) + ("\n" if lines else "")
            return txt, "text/plain", "results.txt"

        payload, mime, fname = _build_output(results, fmt)
        st.download_button("Download", data=payload.encode("utf-8"), file_name=fname, mime=mime, use_container_width=True)

        st.divider()

    # --------------- Fix properties (Link fixer) ---------------
    st.subheader("Fix properties")
    st.caption("Paste Homespotter or Zillow /homes/*_rb/ links. I’ll output clean canonical /homedetails/ URLs if possible.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")

    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0.0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            best = ""
            try:
                if _is_homespotter_like(u) and HS_ADDRESS_RESOLVER_URL:
                    z, addr = resolve_hs_to_zillow(u, default_state=DEFAULT_STATE)
                    if not z and addr:
                        stt, cty, sta, zc = parse_freeform_address_components(addr, default_state=DEFAULT_STATE)
                        z = build_zillow_deeplink(stt, cty, sta or DEFAULT_STATE, zc, default_state=DEFAULT_STATE) or ""
                    if not z:
                        z, _ = resolve_generic_url(u, default_state=DEFAULT_STATE)
                    best = z or ""
                else:
                    z, _ = resolve_generic_url(u, default_state=DEFAULT_STATE)
                    best = z or ""

                if best:
                    best = upgrade_to_homedetails_if_needed(best)
                    canon, _ = canonicalize_zillow(best)
                    best = canon or best
                if is_bad_nc_only(best):
                    best = u  # keep original if we couldn't build a valid Zillow link
            except Exception:
                best = u
            fixed.append(best)
            prog.progress(i / len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# Allow running this module directly for local testing:
if __name__ == "__main__":
    render_run_tab({})
