# ui/run_tab.py
# Address Alchemist (simplified, mobile friendly)
# - Paste addresses AND arbitrary listing links → clean Zillow links
# - Prefers HS resolver microservice (HS_ADDRESS_RESOLVER_URL) for Homespotter links
# - No Bing/Azure dependency
# - CSV upload preserved
# - Image preview preserved
# - "Fix properties" section restored
# - Entry point: render_run_tab(state=None)

import os, io, re, csv, json, time
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, quote_plus, unquote

import requests
import streamlit as st
import streamlit.components.v1 as components

# =========================
# Config & constants
# =========================

REQUEST_TIMEOUT = 12

def _get_secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets and st.secrets[name]:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)

HS_ADDRESS_RESOLVER_URL = _get_secret("HS_ADDRESS_RESOLVER_URL", "").strip()
GOOGLE_MAPS_API_KEY     = _get_secret("GOOGLE_MAPS_API_KEY", "").strip()

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

URL_KEYS = {
    "url","link","source url","source_url","listing url","listing_url",
    "property url","property_url","href"
}

PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

# =========================
# HTTP + HTML helpers
# =========================

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    """Follow redirects and return (final_url, html, status_code)."""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html:
        return out
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

def _is_homespotter_like(u: str) -> bool:
    try:
        host = urlparse(u).hostname or ""
        host = host.lower()
        return ("l.hms.pt" in host) or ("homespotter" in host) or ("idx.homespotter.com" in host)
    except Exception:
        return False

# =========================
# HS microservice resolver
# =========================

def resolve_hs_address_via_service(source_url: str) -> Optional[Dict[str, Any]]:
    """
    Call your Cloudflare/Worker (or other) resolver to get { ok, address{street,city,state,zip}, zillow_candidate? }.
    """
    if not (HS_ADDRESS_RESOLVER_URL and source_url):
        return None
    try:
        resp = requests.get(
            f"{HS_ADDRESS_RESOLVER_URL}?u={quote_plus(source_url)}",
            headers={"Accept":"application/json"},
            timeout=min(REQUEST_TIMEOUT, 10),
        )
        if resp.ok:
            return resp.json()
    except Exception:
        return None
    return None

# =========================
# Address extraction (robust + HS-specific)
# =========================

STREET_TYPES_RX = r"(st|street|rd|road|ave|avenue|blvd|boulevard|ln|lane|dr|drive|ct|court|ter|terrace|way|pkwy|parkway|hwy|highway|cir|circle|pl|place)\b"

def _looks_like_real_street(s: str) -> bool:
    s2 = (s or "").lower()
    # must contain a number AND a typical street-type token
    return bool(re.search(r"\d", s2) and re.search(STREET_TYPES_RX, s2))

def _parse_us_address_loose(text: str) -> Optional[Dict[str, str]]:
    """
    Pull "123 Main St, City, NC 27577" out of arbitrary text.
    """
    if not text:
        return None
    m = re.search(
        r'(?P<street>\d{1,6}[^,\n]+?)\s*,\s*(?P<city>[A-Za-z .\'-]+?)\s*,\s*(?P<state>[A-Z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)?',
        text
    )
    if not m:
        return None
    d = m.groupdict()
    street = (d.get("street") or "").strip()
    if not _looks_like_real_street(street):
        return None
    return {
        "street": street,
        "city": (d.get("city") or "").strip(),
        "state": (d.get("state") or "").strip(),
        "zip": (d.get("zip") or "").strip(),
    }

def _extract_hs_address_html(html: str) -> Dict[str, str]:
    """
    Homespotter/IDX: check JSON keys they often embed.
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not html:
        return out

    # fullAddress: "123 Main St, City, NC 27577"
    m = re.search(r'"fullAddress"\s*:\s*"([^"]+)"', html, re.I)
    if m:
        cand = _parse_us_address_loose(m.group(1))
        if cand:
            return cand

    # Common quartet
    field_map = {
        "street": r'"(?:address1|street|streetAddress)"\s*:\s*"([^"]+)"',
        "city":   r'"(?:city|addressLocality)"\s*:\s*"([^"]+)"',
        "state":  r'"(?:stateOrProvince|addressRegion)"\s*:\s*"([A-Za-z]{2})"',
        "zip":    r'"(?:postalCode|zip|postal_code)"\s*:\s*"(\d{5}(?:-\d{4})?)"',
    }
    for key, pat in field_map.items():
        mm = re.search(pat, html, re.I)
        if mm:
            out[key] = mm.group(1).strip()

    # Validate quickly
    if _looks_like_real_street(out.get("street", "")) and (out.get("city") or out.get("state")):
        return out

    # Microdata
    if not out["street"]:
        m = re.search(r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', html, re.I)
        if m: out["street"] = m.group(1).strip()
    if not out["city"]:
        m = re.search(r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', html, re.I)
        if m: out["city"] = m.group(1).strip()
    if not out["state"]:
        m = re.search(r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', html, re.I)
        if m: out["state"] = m.group(1).strip()
    if not out["zip"]:
        m = re.search(r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', html, re.I)
        if m: out["zip"] = m.group(1).strip()

    # If still not real, try description/title blobs
    if not _looks_like_real_street(out.get("street")):
        for pat in [
            r"<meta[^>]+property=['\"]og:description['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<meta[^>]+name=['\"]twitter:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        ]:
            m = re.search(pat, html, re.I)
            if m:
                cand = _parse_us_address_loose(m.group(1))
                if cand:
                    return cand

    return out

def extract_address_from_html(html: str) -> Dict[str, str]:
    """
    Robust address extractor. Avoids treating "Listing 10127718" or "MLS ####" as a street.
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}

    if not html:
        return out

    # Homespotter pass
    hs_guess = _extract_hs_address_html(html)
    if _looks_like_real_street(hs_guess.get("street", "")):
        return hs_guess

    # JSON-LD
    for blk in _jsonld_blocks(html):
        try:
            if not isinstance(blk, dict):
                continue
            addr = blk.get("address") or (blk.get("itemOffered") or {}).get("address")
            if isinstance(addr, dict):
                street = (addr.get("streetAddress") or "").strip()
                city   = (addr.get("addressLocality") or "").strip()
                state  = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()
                zipc   = (addr.get("postalCode") or "").strip()
                if _looks_like_real_street(street):
                    return {"street": street, "city": city, "state": state[:2].upper(), "zip": zipc}
        except Exception:
            pass

    # Direct JSON
    mstreet = re.search(r'"streetAddress"\s*:\s*"([^"]+)"', html, re.I)
    street = (mstreet.group(1).strip() if mstreet else "")
    if not _looks_like_real_street(street):
        street = ""
    m = re.search(r'"addressLocality"\s*:\s*"([^"]+)"', html, re.I); city = (m.group(1).strip() if m else "")
    m = re.search(r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', html, re.I); state = (m.group(1).strip() if m else "")
    m = re.search(r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', html, re.I); zipc = (m.group(1).strip() if m else "")
    if street and (city or state):
        return {"street": street, "city": city, "state": state, "zip": zipc}

    # Title/meta (ignore "listing" / "mls")
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+?)\s*</title>",
    ]:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        title = (m.group(1) or "").strip()
        if re.search(r"\b(listing|mls)\b", title, re.I):
            continue
        cand = _parse_us_address_loose(title)
        if cand:
            return cand

    # Description fallback
    m = re.search(r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
    if m:
        cand = _parse_us_address_loose(m.group(1))
        if cand:
            return cand

    return out

# =========================
# Zillow URL utilities
# =========================

def zillow_slugify(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\s,-]", "", t)
    t = t.replace(",", "")
    t = re.sub(r"\s+", "-", t.strip())
    return t

def zillow_deeplink_from_addr(street: str, city: str, state: str, zipc: str) -> Optional[str]:
    """
    Build /homes/<slug>_rb/ only when we truly have an address:
    require (street+state) or (city+state).
    """
    street = (street or "").strip()
    city   = (city or "").strip()
    state  = (state or "").strip()
    zipc   = (zipc or "").strip()

    # junk guard
    if re.search(r"\b(listing|mls)\b", street, re.I):
        street = ""

    if not ((street and state) or (city and state)):
        return None

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
    if not slug:
        return None
    return f"https://www.zillow.com/homes/{slug}_rb/"

ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    """Return canonical homedetails url (if present) and zpid (if present)."""
    if not url:
        return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def upgrade_to_homedetails_if_needed(url: str) -> str:
    """
    If we have a /homes/..._rb/ page, try to upgrade to the canonical /homedetails/.../_zpid/.
    Otherwise return original.
    """
    if not url or "/homedetails/" in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=min(REQUEST_TIMEOUT, 10))
        if not r.ok:
            return url
        html = r.text

        # direct anchors
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)

        # rel=canonical
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
        pass
    return url

def make_preview_url(url: str) -> str:
    """Canonical clean preview URL (strip query/fragment, prefer homedetails)."""
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# =========================
# Image helpers
# =========================

def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html:
        return None
    for target_w in ("960","1152","768","1536"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html, re.I
        )
        if m:
            return m.group(1)
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None

def picture_for_result(query_address: str, zurl: str, csv_photo_url: Optional[str] = None) -> Optional[str]:
    def _ok(u:str)->bool:
        return isinstance(u,str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))
    if csv_photo_url and _ok(csv_photo_url):
        return csv_photo_url
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.ok:
                html = r.text
                img = extract_zillow_first_image(html)
                if img:
                    return img
                m = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # StreetView fallback
    if GOOGLE_MAPS_API_KEY and query_address:
        return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={quote_plus(query_address)}&key={GOOGLE_MAPS_API_KEY}"
    return None

# =========================
# Parsing pasted input
# =========================

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Accepts:
      - CSV with headers (address fields OR url field)
      - Plain list (one URL or one address per line)
    Returns a list of dict rows.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV first (requires a header line with a delimiter)
    try:
        sample = text.splitlines()
        if len(sample) >= 2 and ("," in sample[0] or "\t" in sample[0] or ";" in sample[0]):
            # auto-detect delimiter
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
            rows.append({"url": s})
        else:
            rows.append({"address": s})
    return rows

def norm_key(k:str) -> str:
    return re.sub(r"\s+"," ", (k or "").strip().lower())

def get_first_by_keys(row: Dict[str, Any], keys) -> str:
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v:
                return v
    return ""

def detect_source_url(row: Dict[str, Any]) -> Optional[str]:
    # honor explicit URL columns first
    for k, v in row.items():
        if norm_key(k) in URL_KEYS and is_probable_url(str(v)):
            return str(v).strip()
    # fallback shortcuts
    for k in ("url","source","href","link"):
        if is_probable_url(str(row.get(k, ""))):
            return str(row.get(k)).strip()
    return None

# =========================
# Resolution pipeline
# =========================

def resolve_from_source_url(source_url: str, state_default: str = "NC") -> Tuple[str, str]:
    """
    1) If Homespotter-like and HS resolver is set → call it.
    2) Else open the page, extract address.
    3) Build a safe Zillow deeplink (only with real address), then try to upgrade to /homedetails/.
    Returns (zillow_url_or_expanded, inferred_address_text).
    """
    if not source_url:
        return "", ""

    # 1) HS resolver microservice
    if _is_homespotter_like(source_url) and HS_ADDRESS_RESOLVER_URL:
        data = resolve_hs_address_via_service(source_url)
        if data and data.get("ok"):
            a = data.get("address") or {}
            zcand = data.get("zillow_candidate") or None
            street = (a.get("street") or "").strip()
            city   = (a.get("city") or "").strip()
            state  = (a.get("state") or "").strip() or state_default
            zipc   = (a.get("zip") or "").strip()

            zurl = zcand or zillow_deeplink_from_addr(street, city, state, zipc)
            if zurl:
                zurl = upgrade_to_homedetails_if_needed(zurl)
                inferred = " ".join([x for x in [street, city, state, zipc] if x])
                return zurl, inferred

    # 2) Generic HTML parse
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    addr = extract_address_from_html(html)
    street = (addr.get("street") or "").strip()
    city   = (addr.get("city") or "").strip()
    state  = (addr.get("state") or "").strip() or state_default
    zipc   = (addr.get("zip") or "").strip()

    # 3) Build Zillow link when we have enough
    z = zillow_deeplink_from_addr(street, city, state, zipc)
    if z:
        z = upgrade_to_homedetails_if_needed(z)
        inferred = " ".join([x for x in [street, city, state, zipc] if x])
        return z, inferred

    # Fallback: give expanded URL back (don't return root /homes/)
    return final_url, ""

def process_row(row: Dict[str, Any], state_default: str = "NC") -> Dict[str, Any]:
    """
    If the row contains a URL → resolve_from_source_url.
    If it's an address → build a safe Zillow deeplink & upgrade if possible.
    """
    src_url = detect_source_url(row)
    photo   = get_first_by_keys(row, PHOTO_KEYS)

    # URL path
    if src_url:
        zurl, inferred = resolve_from_source_url(src_url, state_default=state_default)
        return {
            "input_address": inferred or (row.get("address") or "").strip(),
            "zillow_url": zurl,
            "status": ("ok" if zurl else "no_address_found"),
            "csv_photo": photo,
        }

    # Address path
    addr = (row.get("address") or row.get("full_address") or "").strip()
    # Try to pull city/state/zip if present in the same string
    parsed = _parse_us_address_loose(addr) or {"street": addr, "city": "", "state": state_default, "zip": ""}
    z = zillow_deeplink_from_addr(parsed.get("street",""), parsed.get("city",""), parsed.get("state",""), parsed.get("zip",""))
    if z:
        z = upgrade_to_homedetails_if_needed(z)
    return {
        "input_address": " ".join([x for x in [parsed.get("street",""), parsed.get("city",""), parsed.get("state",""), parsed.get("zip","")] if x]),
        "zillow_url": z or "",
        "status": ("ok" if z else "no_address_found"),
        "csv_photo": photo,
    }

# =========================
# UI helpers (list, images, export)
# =========================

def results_list_with_copy_all(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        href = r.get("zillow_url") or ""
        if not href:
            continue
        safe_href = escape(make_preview_url(href))
        li_html.append(f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{safe_href}</a></li>')

    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("zillow_url") or ""
        if u:
            copy_lines.append(make_preview_url(u).strip())
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
          const text = {json.dumps(copy_text)};
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

def build_output(rows: List[Dict[str, Any]], fmt: str) -> Tuple[str, str]:
    def pick_url(r):
        return make_preview_url(r.get("zillow_url") or "")
    if fmt == "csv":
        fields = ["input_address","zillow_url","status"]
        s = io.StringIO(); w = csv.DictWriter(s, fieldnames=fields); w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields}
            row["zillow_url"] = pick_url(r)
            w.writerow(row)
        return s.getvalue(), "text/csv"
    if fmt == "html":
        items = []
        for r in rows:
            u = pick_url(r)
            if not u: continue
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"
    # txt / md (same list shape)
    lines = []
    for r in rows:
        u = pick_url(r)
        if u: lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")

def thumbnails_grid(results: List[Dict[str, Any]], columns: int = 3):
    if not results:
        return
    cols = st.columns(columns)
    for i, r in enumerate(results):
        zurl = r.get("zillow_url") or ""
        addr = r.get("input_address") or ""
        img  = picture_for_result(addr, zurl, r.get("csv_photo"))
        with cols[i % columns]:
            if img:
                st.image(img, use_container_width=True)
            link = make_preview_url(zurl) if zurl else ""
            if link:
                st.markdown(f"[{escape(addr) if addr else 'View listing'}]({link})", unsafe_allow_html=True)

# =========================
# Main UI
# =========================

def render_run_tab(state: dict | None = None):
    st.header("Address Alchemist")

    # Mobile-friendly, minimal controls
    st.write("Paste addresses or **any listing links** (Homespotter, IDX, etc.). I’ll return clean Zillow links.")

    with st.container():
        paste = st.text_area("Paste addresses or links (one per line)", height=160, placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718")

    csv_file = st.file_uploader("Upload CSV (optional)", type=["csv"])

    run_col, export_col = st.columns([1, 1])
    with run_col:
        run_btn = st.button("🚀 Run", type="primary", use_container_width=True)

    results: List[Dict[str, Any]] = []

    if run_btn:
        rows_in: List[Dict[str, Any]] = []

        # Parse CSV first (if provided)
        if csv_file is not None:
            try:
                content = csv_file.getvalue().decode("utf-8-sig", errors="replace")
                reader = list(csv.DictReader(io.StringIO(content)))
                rows_in.extend(reader)
            except Exception as e:
                st.warning(f"CSV read error: {e}")

        # Parse pasted
        rows_in.extend(_rows_from_paste(paste or ""))

        if not rows_in:
            st.warning("Nothing to process.")
            return

        total = len(rows_in)
        prog = st.progress(0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            res = process_row(row, state_default="NC")  # default NC if state missing
            results.append(res)
            prog.progress(i/total, text=f"Resolved {i}/{total}")
            # light politeness for rate limits
            time.sleep(0.05)

        prog.progress(1.0, text="Done")

        # Show results list with copy-all
        st.subheader("Results")
        results_list_with_copy_all(results)

        # Thumbnails section
        st.subheader("Images")
        thumbnails_grid(results, columns=3)

        with export_col:
            fmt = st.radio("Export format", ["txt","md","html","csv"], horizontal=True, index=0)
            payload, mime = build_output(results, fmt=fmt)
            st.download_button("Download", data=payload.encode("utf-8"), file_name=f"results.{fmt}", mime=mime, use_container_width=True)

        st.divider()

    # -------- Fix properties section --------
    st.subheader("Fix properties")
    st.caption("Paste any listing links (including short Homespotter links). I’ll return clean Zillow **/homedetails/** URLs when possible.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")

    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                # If it's homespotter-like, try full resolve first
                if _is_homespotter_like(best):
                    z, _addr = resolve_from_source_url(best, state_default="NC")
                    best = z or best
                # Always try to upgrade to homedetails if it's already a Zillow link
                if "zillow.com" in (best or ""):
                    best = upgrade_to_homedetails_if_needed(best)
            except Exception:
                pass
            # Avoid returning root /homes/; keep original if no address
            if best and re.match(r"^https?://(www\.)?zillow\.com/homes/?$", best.strip("/"), re.I):
                fixed.append(u)  # keep original if we couldn't resolve
            else:
                fixed.append(make_preview_url(best))
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# Allow running directly for local testing:
if __name__ == "__main__":
    render_run_tab({})
