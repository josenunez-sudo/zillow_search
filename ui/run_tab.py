# ui/run_tab.py
# Robust Homespotter → Address (no Bing). If full address is hidden, extract lat/lon and reverse-geocode
# to recover house number + street. Then compose clean Zillow /homes/*_rb/ deeplinks.
# Keeps CSV upload, "Fix links" section, and Street View thumbs.

import os, csv, io, re, time, json
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from html import escape
from urllib.parse import urlparse, urlunparse, quote_plus

import requests
import streamlit as st
import streamlit.components.v1 as components

# ==========================
# ---- Page + Styles -------
# ==========================
st.set_page_config(page_title="Address Alchemist (no-Bing resolver + reverse geocode)", layout="centered")

st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { padding:10px 12px; background:transparent; border-radius:12px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.new { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.dup { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
.run-zone .stButton>button { background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important; color:#fff !important; font-weight:800 !important; border:0 !important; border-radius:12px !important; box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important; }
.img-label { font-size:13px; margin-top:6px; }
</style>
""", unsafe_allow_html=True)

# ==========================
# ---- Config / Secrets ----
# ==========================
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", st.secrets.get("GOOGLE_MAPS_API_KEY", ""))
REQUEST_TIMEOUT = 18

# ==========================
# ---- HTTP helpers --------
# ==========================
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOCIAL_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

def _get(url: str, ua: str = DEFAULT_UA, allow_redirects: bool = True) -> Tuple[str, str, int, Dict[str,str]]:
    """Return (final_url, text, status_code, headers)"""
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=allow_redirects,
        )
        return r.url, (r.text if r.ok else ""), r.status_code, dict(r.headers or {})
    except Exception:
        return url, "", 0, {}

def _jina_readable(url: str) -> str:
    """Plain-text readability proxy for stubborn JS pages (free)."""
    try:
        u = urlparse(url)
        inner = "http://" + u.netloc + u.path
        if u.query:
            inner += "?" + u.query
        prox = "https://r.jina.ai/" + inner
        _, text, code, _ = _get(prox, ua=DEFAULT_UA, allow_redirects=True)
        return text if code == 200 and text else ""
    except Exception:
        return ""

# ==========================
# ---- Address extractors --
# ==========================
RE_STREET_CITY_ST_ZIP = re.compile(
    r'(\d{1,6}\s+[A-Za-z0-9\.\'\-\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Cir|Highway|Hwy|Route|Pkwy|Parkway)\b[^\n,]*)\s*,\s*([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
    re.I
)
RE_CITY_ST_ZIP = re.compile(r'([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', re.I)

def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out = []
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I|re.S):
            blob = m.group(1).strip()
            try:
                data = json.loads(blob)
                if isinstance(data, dict):
                    out.append(data)
                elif isinstance(data, list):
                    out.extend([d for d in data if isinstance(d, dict)])
            except Exception:
                continue
    except Exception:
        pass
    return out

def _extract_address_from_jsonld(html: str) -> Dict[str,str]:
    for blk in _jsonld_blocks(html):
        if not isinstance(blk, dict):
            continue
        addr = blk.get("address") or blk.get("itemOffered", {}).get("address")
        if isinstance(addr, dict):
            street = (addr.get("streetAddress") or "").strip()
            city   = (addr.get("addressLocality") or "").strip()
            state  = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()[:2]
            zipc   = (addr.get("postalCode") or "").strip()
            if street or (city and state):
                return {"street": street, "city": city, "state": state, "zip": zipc}
    return {"street":"", "city":"", "state":"", "zip":""}

def _extract_address_from_meta(html: str) -> Dict[str,str]:
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]twitter:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)\s*</title>",
    ]:
        m = re.search(pat, html, re.I)
        if not m:
            continue
        text = re.sub(r'\s+', ' ', m.group(1)).strip()
        s1 = RE_STREET_CITY_ST_ZIP.search(text)
        if s1:
            return {"street": s1.group(1), "city": s1.group(2), "state": s1.group(3).upper(), "zip": s1.group(4)}
        s2 = RE_CITY_ST_ZIP.search(text)
        if s2:
            return {"street": "", "city": s2.group(1), "state": s2.group(2).upper(), "zip": s2.group(3)}
    return {"street":"", "city":"", "state":"", "zip":""}

def _extract_address_from_text(txt: str) -> Dict[str,str]:
    if not txt:
        return {"street":"", "city":"", "state":"", "zip":""}
    s1 = RE_STREET_CITY_ST_ZIP.search(txt)
    if s1:
        return {"street": s1.group(1).strip(), "city": s1.group(2).strip(), "state": s1.group(3).upper(), "zip": s1.group(4).strip()}
    s2 = RE_CITY_ST_ZIP.search(txt)
    if s2:
        return {"street":"", "city": s2.group(1).strip(), "state": s2.group(2).upper(), "zip": s2.group(3).strip()}
    return {"street":"", "city":"", "state":"", "zip":""}

# ---- Deep-scan inline JSON for address & LAT/LON ----
STREET_KEYS = ["streetAddress","street","street1","address1","addressLine1","line1","line","unparsedAddress","displayAddress","route"]
CITY_KEYS   = ["addressLocality","locality","city","town"]
STATE_KEYS  = ["addressRegion","region","state","stateOrProvince","province"]
ZIP_KEYS    = ["postalCode","zip","zipCode","postcode"]

LAT_KEYS = ["latitude","lat","latDeg","y"]
LON_KEYS = ["longitude","lng","lon","long","x"]

def _find_first(html: str, keys: List[str]) -> str:
    for k in keys:
        pat = rf'["\']{re.escape(k)}["\']\s*:\s*["\']([^"\']+)["\']'
        m = re.search(pat, html, re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""

def _find_first_number(html: str, keys: List[str]) -> Optional[float]:
    for k in keys:
        # allow either number or quoted number
        pat = rf'["\']{re.escape(k)}["\']\s*:\s*(?:["\']([\-0-9\.]+)["\']|([\-0-9\.]+))'
        m = re.search(pat, html, re.I)
        if m:
            val = m.group(1) or m.group(2)
            try:
                return float(val)
            except Exception:
                continue
    return None

def _extract_address_inline_json(html: str) -> Dict[str,str]:
    if not html:
        return {"street":"","city":"","state":"","zip":""}
    street = _find_first(html, STREET_KEYS)
    city   = _find_first(html, CITY_KEYS)
    state  = _find_first(html, STATE_KEYS)
    zipc   = _find_first(html, ZIP_KEYS)
    # Single blob (fallback)
    if not (street or (city and state)):
        m = re.search(r'["\'](?:fullAddress|address|line|location)["\']\s*:\s*["\']([^"\']+?)["\']', html, re.I)
        if m:
            return _extract_address_from_text(m.group(1))
    return {"street":street, "city":city, "state":(state[:2] if state else ""), "zip":zipc}

def _extract_latlon_inline(html: str) -> Tuple[Optional[float], Optional[float]]:
    lat = _find_first_number(html, LAT_KEYS)
    lon = _find_first_number(html, LON_KEYS)
    # Also try common patterns like "latLng":{"lat":..,"lng":..}
    if lat is None or lon is None:
        m = re.search(r'"latLng"\s*:\s*\{\s*"lat"\s*:\s*([\-0-9\.]+)\s*,\s*"lng"\s*:\s*([\-0-9\.]+)\s*\}', html, re.I)
        if m:
            try:
                return float(m.group(1)), float(m.group(2))
            except Exception:
                pass
    return lat, lon

# ==========================
# ---- Geocoding helpers ---
# ==========================
def reverse_geocode(lat: float, lon: float) -> Dict[str,str]:
    """
    Use Google Geocoding API to get a precise address from coordinates.
    Returns dict with street (number+route), city, state (2-letter), zip.
    """
    key = GOOGLE_MAPS_API_KEY
    if not key:
        return {"street":"","city":"","state":"","zip":""}
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": key},
            timeout=REQUEST_TIMEOUT
        )
        if not r.ok:
            return {"street":"","city":"","state":"","zip":""}
        data = r.json() or {}
        results = data.get("results") or []
        if not results:
            return {"street":"","city":"","state":"","zip":""}
        # pick the first result (usually rooftop)
        comp = results[0].get("address_components") or []
        def _get(types):
            for c in comp:
                t = c.get("types") or []
                if any(tt in t for tt in types):
                    return c.get("long_name") or c.get("short_name") or ""
            return ""
        num   = _get(["street_number"])
        route = _get(["route"])
        city  = _get(["locality","postal_town","sublocality","administrative_area_level_3"])
        state = _get(["administrative_area_level_1"])
        zipc  = _get(["postal_code"])
        street = (" ".join([p for p in [num, route] if p])).strip()
        state2 = _get(["administrative_area_level_1"]) or ""
        if len(state2) > 2:
            # if long form, also try short_name
            for c in comp:
                if "administrative_area_level_1" in (c.get("types") or []):
                    state2 = c.get("short_name") or state2
                    break
        return {"street":street, "city":city, "state":state2[:2], "zip":zipc}
    except Exception:
        return {"street":"","city":"","state":"","zip":""}

# ==========================
# ---- Zillow builders -----
# ==========================
def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s,-]", "", s)
    s = s.replace(",", "")
    return re.sub(r"\s+", "-", s.strip())

def zillow_rb_from_address(street: str, city: str, state: str, zipc: str) -> Optional[str]:
    city  = (city or "").strip()
    state = (state or "").strip()
    street = (street or "").strip()
    zipc  = (zipc or "").strip()

    # Require city+state to avoid garbage like /homes/nc_rb/
    if not (city and state):
        return None

    # Prevent “nc-nc”
    if street.lower() == state.lower():
        street = ""

    parts = []
    if street: parts.append(street)
    loc = (city + " " + state).strip()
    if loc: parts.append(loc)
    if zipc:
        if parts:
            parts[-1] = parts[-1] + " " + zipc
        else:
            parts.append(zipc)

    slug = _slugify(", ".join(parts))
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ==========================
# ---- Core Resolve --------
# ==========================
def _try_variants(url: str) -> List[Tuple[str,str,int]]:
    """Try multiple UA + URL variants and return list of (final_url, html, status)."""
    variants = [url]
    # AMP-ish toggles
    variants.append(url + ("&amp=1" if "?" in url else "?amp=1"))
    try:
        u = urlparse(url); variants.append(urlunparse((u.scheme,u.netloc,u.path.rstrip('/')+'/amp',"","","")))
    except Exception:
        pass

    out = []
    # default first
    f1, h1, c1, _ = _get(url, ua=DEFAULT_UA, allow_redirects=True)
    out.append((f1, h1, c1))
    # social UAs on base
    for ua in SOCIAL_UAS:
        f2, h2, c2, _ = _get(url, ua=ua, allow_redirects=True); out.append((f2, h2, c2))
    # default UA on variants
    for v in variants[1:]:
        f3, h3, c3, _ = _get(v, ua=DEFAULT_UA, allow_redirects=True); out.append((f3, h3, c3))
    # social UAs on variants
    for v in variants[1:]:
        for ua in SOCIAL_UAS:
            f4, h4, c4, _ = _get(v, ua=ua, allow_redirects=True); out.append((f4, h4, c4))
    return out

def best_effort_address_from_hs(url: str) -> Dict[str,str]:
    """
    Try very hard (no Bing) to get (street, city, state, zip) from Homespotter (or l.hms.pt) link.
    Order: JSON-LD → meta → inline JSON deep-scan → lat/lon reverse-geocode → readability text.
    """
    tries = _try_variants(url)

    # 1) structured first (json-ld/meta)
    for _, html, code in tries:
        if code == 200 and html:
            a = _extract_address_from_jsonld(html)
            if a.get("street") or (a.get("city") and a.get("state")):
                return a
            b = _extract_address_from_meta(html)
            if b.get("street") or (b.get("city") and b.get("state")):
                return b

    # 2) deep-scan inline JSON keys (very permissive)
    for _, html, code in tries:
        if code == 200 and html:
            c = _extract_address_inline_json(html)
            if c.get("street") or (c.get("city") and c.get("state")):
                # if street missing number, see if lat/lon exists to refine
                lat, lon = _extract_latlon_inline(html)
                if (not c.get("street") or not re.match(r'^\d+\s', c["street"])) and lat is not None and lon is not None:
                    geo = reverse_geocode(lat, lon)
                    # prefer reverse-geocoded street if it has a number
                    if geo.get("street") and re.match(r'^\d+\s', geo["street"]):
                        c = {
                            "street": geo["street"],
                            "city":   c.get("city") or geo.get("city",""),
                            "state":  c.get("state") or geo.get("state",""),
                            "zip":    c.get("zip")   or geo.get("zip",""),
                        }
                return c

    # 3) lat/lon reverse-geocode even if no address keys matched
    #    (Some pages show only a map JSON.)
    for _, html, code in tries:
        if code == 200 and html:
            lat, lon = _extract_latlon_inline(html)
            if lat is not None and lon is not None:
                g = reverse_geocode(lat, lon)
                if g.get("city") and g.get("state"):
                    return g

    # 4) readability plaintext (last resort)
    f0, _, _, _ = _get(url, ua=DEFAULT_UA, allow_redirects=True)
    txt = _jina_readable(f0 or url)
    if txt:
        d = _extract_address_from_text(txt)
        if d.get("street") or (d.get("city") and d.get("state")):
            return d

    return {"street":"", "city":"", "state":"", "zip":""}

def resolve_any_link_to_zillow_rb(source_url: str) -> Tuple[str, str]:
    """
    Return (zillow_deeplink, human_readable_address) or (original_url, "") if we couldn't form a deeplink.
    No Bing/Azure dependency. Uses reverse geocoding if needed to get full street number.
    """
    if not source_url:
        return "", ""

    # Expand once (in case of l.hms.pt shortlink)
    final, html, code, _ = _get(source_url, ua=DEFAULT_UA, allow_redirects=True)
    target = final or source_url

    # If already Zillow homedetails/homes, keep it (strip query/fragment)
    if "zillow.com" in (target or "") and ("/homedetails/" in target or "/homes/" in target):
        return re.sub(r"[?#].*$", "", target), ""

    # Homespotter-ish?
    host = (urlparse(target).hostname or "").lower()
    if any(k in host for k in ["homespotter", "hms.pt", "idx."]):
        addr = best_effort_address_from_hs(target)
    else:
        # generic: try json-ld/meta/inline + reverse geocode if lat/lon
        addr = {"street":"","city":"","state":"","zip":""}
        if code == 200 and html:
            addr = _extract_address_from_jsonld(html)
            if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
                tmp = _extract_address_from_meta(html)
                if tmp.get("street") or (tmp.get("city") and tmp.get("state")):
                    addr = tmp
        if code == 200 and html and not (addr.get("street") or (addr.get("city") and addr.get("state"))):
            addr = _extract_address_inline_json(html)
            if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
                lat, lon = _extract_latlon_inline(html)
                if lat is not None and lon is not None:
                    addr = reverse_geocode(lat, lon)
        if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
            txt = _jina_readable(target)
            addr = _extract_address_from_text(txt)

    street = addr.get("street","")
    city   = addr.get("city","")
    state  = addr.get("state","")
    zipc   = addr.get("zip","")

    # If street lacks a number but we have lat/lon on the original page, try once more to refine using reverse geocode.
    if (not street or not re.match(r'^\d+\s', street)) and code == 200 and html:
        lat, lon = _extract_latlon_inline(html)
        if lat is not None and lon is not None:
            geo = reverse_geocode(lat, lon)
            if geo.get("street") and re.match(r'^\d+\s', geo["street"]):
                street = geo["street"]
                city   = city or geo.get("city","")
                state  = state or geo.get("state","")
                zipc   = zipc or geo.get("zip","")

    deeplink = zillow_rb_from_address(street, city, state, zipc)
    if not deeplink:
        # Could not form a meaningful rb; return original link and blank address
        return target, ""

    display_addr = ", ".join([p for p in [street, f"{city} {state}".strip(), zipc] if p])
    return deeplink, display_addr

# ==========================
# ---- UI Helpers ----------
# ==========================
def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    # Try CSV if header present
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
    # Fallback: 1 per line
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("http://") or s.startswith("https://"):
            rows.append({"url": s})
        else:
            rows.append({"address": s})
    return rows

def _results_list(results: List[Dict[str, Any]]):
    li_html = []
    for r in results:
        u = (r.get("zillow_url") or r.get("original") or "").strip()
        if not u:
            continue
        addr = r.get("display_address") or ""
        label = u if not addr else f"{addr} — {u}"
        li_html.append(f'<li style="margin:0.2rem 0;"><a href="{escape(u)}" target="_blank" rel="noopener">{escape(label)}</a></li>')
    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"
    components.html(
        f"""
        <html><head><meta charset="utf-8" />
        <style>
          html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
          .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
          ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
          ul.link-list li {{ margin:0.2rem 0; }}
          .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }}
        </style></head><body>
          <div class="results-wrap">
            <button id="copyAll" class="copyall-btn">Copy</button>
            <ul class="link-list">{items_html}</ul>
          </div>
          <script>
            (function(){{
              const text = `{escape("\\n".join([(r.get("zillow_url") or r.get("original") or "").strip() for r in results if (r.get("zillow_url") or r.get("original"))]))}\\n`;
              const btn = document.getElementById('copyAll');
              btn.addEventListener('click', async () => {{
                try {{ await navigator.clipboard.writeText(text); btn.textContent='✓'; setTimeout(()=>btn.textContent='Copy',900); }}
                catch(e){{ btn.textContent='×'; setTimeout(()=>btn.textContent='Copy',900); }}
              }});
            }})();
          </script>
        </body></html>
        """,
        height=min(600, 40 * max(1, len(results)) + 60),
        scrolling=False
    )

def _streetview_thumb(query_addr: str) -> Optional[str]:
    if not GOOGLE_MAPS_API_KEY or not query_addr:
        return None
    loc = quote_plus(query_addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={GOOGLE_MAPS_API_KEY}"

# ==========================
# ---- Main UI -------------
# ==========================
def render_run_tab(state: dict):
    st.header("Address Alchemist — Homespotter resolver (reverse geocode enabled)")

    # Paste OR CSV
    st.subheader("Input")
    st.caption("Paste Homespotter links (or any listing links) and/or upload a CSV with a column named `url`.")
    colA, colB = st.columns([1.4, 1])
    with colA:
        paste = st.text_area("Paste links or addresses", height=140, label_visibility="collapsed")
    with colB:
        file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="visible")

    use_streetview = st.checkbox("Show Street View thumbnails (needs GOOGLE_MAPS_API_KEY)", value=False)

    # Parse pasted
    rows_in: List[Dict[str, Any]] = []
    rows_in.extend(_rows_from_paste(paste))

    # CSV
    if file is not None:
        try:
            content = file.getvalue().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                u = (row.get("url") or row.get("link") or "").strip()
                if u:
                    rows_in.append({"url": u})
        except Exception as e:
            st.warning(f"Could not read CSV: {e}")

    st.write(f"Parsed **{len(rows_in)}** row(s).")

    if st.button("🚀 Resolve to Zillow"):
        if not rows_in:
            st.warning("Nothing to process.")
            st.stop()

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            u = (row.get("url") or row.get("source_url") or row.get("href") or row.get("address") or "").strip()
            if not u:
                continue

            if u.startswith("http"):
                z, addr = resolve_any_link_to_zillow_rb(u)
                out = {
                    "original": u,
                    "zillow_url": z or u,
                    "display_address": addr or "",
                }
                if use_streetview and addr:
                    thumb = _streetview_thumb(addr)
                    if thumb:
                        out["image_url"] = thumb
                results.append(out)
            else:
                # Treat as address string → direct rb deeplink (best effort)
                parts = re.split(r"\s*,\s*", u)
                city = state = zipc = ""
                street = u
                if len(parts) >= 2:
                    street = parts[0]
                    tail = ", ".join(parts[1:])
                    m = RE_CITY_ST_ZIP.search(tail)
                    if m:
                        city, state, zipc = m.group(1), m.group(2), m.group(3)
                z = zillow_rb_from_address(street, city, state, zipc) or ""
                results.append({"original": u, "zillow_url": z or u, "display_address": u})

            prog.progress(i/len(rows_in), text=f"Resolved {i}/{len(rows_in)}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results")
        _results_list(results)

        # Thumbs grid
        thumbs = [(r.get("zillow_url",""), r.get("image_url",""), r.get("display_address","")) for r in results if r.get("image_url")]
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i, (link, img, addr) in enumerate(thumbs):
                with cols[i % 3]:
                    st.image(img, use_container_width=True)
                    st.markdown(f'<div class="img-label">{escape(addr or "")}</div>', unsafe_allow_html=True)

        # Download
        st.markdown("#### Export")
        fmt = st.radio("Format", ["txt","csv","md","html"], horizontal=True)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["original","zillow_url","display_address"])
            w.writeheader()
            for r in results:
                w.writerow({k: r.get(k,"") for k in ["original","zillow_url","display_address"]})
            payload = buf.getvalue(); mime="text/csv"; fname="resolved.csv"
        elif fmt == "html":
            items = "\n".join([f'<li><a href="{escape(r.get("zillow_url",""))}" target="_blank" rel="noopener">{escape(r.get("zillow_url",""))}</a></li>' for r in results])
            payload = "<ul>\n" + items + "\n</ul>\n"; mime="text/html"; fname="resolved.html"
        elif fmt == "md":
            lines = "\n".join([r.get("zillow_url","") for r in results]) + "\n"
            payload = lines; mime="text/markdown"; fname="resolved.md"
        else:
            lines = "\n".join([r.get("zillow_url","") for r in results]) + "\n"
            payload = lines; mime="text/plain"; fname="resolved.txt"

        st.download_button("Download", data=payload.encode("utf-8"), file_name=fname, mime=mime, use_container_width=True)

    st.divider()

    # ==========================
    # ---- Fix links ----------
    # ==========================
    st.subheader("Fix / Re-run links")
    st.caption("Paste any Homespotter or other listing links; I’ll try to pull the address and build a Zillow **/_rb/** deeplink. Reverse-geocodes when needed.")
    fix_text = st.text_area("Links to fix", height=140, key="fix_area")
    if st.button("🔧 Fix / Re-run"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        shown: List[str] = []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            z, addr = resolve_any_link_to_zillow_rb(u)
            best = z or u
            fixed.append(best)
            label = addr or best
            shown.append(f"- [{escape(label)}]({escape(best)})")
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        st.markdown("**Fixed links**")
        st.markdown("\n".join(shown), unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# Allow `streamlit run ui/run_tab.py`
if __name__ == "__main__":
    render_run_tab(st.session_state)
