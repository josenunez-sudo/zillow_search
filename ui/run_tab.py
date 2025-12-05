# ui/run_tab.py
# Homespotter → full address (with number) via aggressive coord & text extraction + reverse/forward geocode.
# Outputs clean Zillow /homes/<full-address>_rb/ deeplinks. CSV upload preserved. No Bing/Azure.

import os, csv, io, re, json, time, html
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, urlunparse, parse_qs, quote_plus

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------------- Page ----------------
st.set_page_config(page_title="Address Alchemist — HS Resolver", layout="centered")
st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { padding:12px; border-radius:12px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.run-zone .stButton>button { background: linear-gradient(180deg,#2563eb 0%,#1d4ed8 100%)!important;color:#fff!important;font-weight:800!important;border:0!important;border-radius:12px!important;box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important; }
.img-label { font-size:13px; margin-top:6px; }
.small { color:#64748b; font-size:12px; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.ok { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.warn { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
</style>
""", unsafe_allow_html=True)

# ---------------- Config ----------------
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", st.secrets.get("GOOGLE_MAPS_API_KEY", ""))
REQUEST_TIMEOUT = 18

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOCIAL_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

# ---------------- HTTP helpers ----------------
def _get(url: str, ua: str = DEFAULT_UA, allow_redirects: bool = True) -> Tuple[str, str, int, Dict[str,str]]:
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

def _readable(url: str) -> str:
    """
    Text-only readability proxy for stubborn JS pages (r.jina.ai).
    """
    try:
        u = urlparse(url)
        inner = "http://" + u.netloc + u.path + (("?" + u.query) if u.query else "")
        prox = "https://r.jina.ai/" + inner
        _, text, code, _ = _get(prox, ua=DEFAULT_UA, allow_redirects=True)
        return text if code == 200 and text else ""
    except Exception:
        return ""

# ---------------- Address patterns ----------------
RE_STREET_CITY_ST_ZIP = re.compile(
    r'(\d{1,6}\s+[A-Za-z0-9\.\'\-\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Cir|Highway|Hwy|Route|Parkway|Pkwy)\b[^\n,]*)\s*,\s*([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
    re.I
)
RE_CITY_ST_ZIP = re.compile(r'([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', re.I)

# ---------------- Extractors ----------------
def _jsonld_blocks(html_txt: str) -> List[Dict[str, Any]]:
    out = []
    if not html_txt: return out
    # unescape then parse
    un = html.unescape(html_txt)
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', un, re.I|re.S):
        blob = (m.group(1) or "").strip()
        try:
            data = json.loads(blob)
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                out.extend([d for d in data if isinstance(d, dict)])
        except Exception:
            continue
    return out

def _extract_address_from_jsonld(html_txt: str) -> Dict[str,str]:
    for blk in _jsonld_blocks(html_txt):
        addr = (blk.get("address")
                or blk.get("itemOffered", {}).get("address")
                or blk.get("item", {}).get("address"))
        if isinstance(addr, dict):
            street = (addr.get("streetAddress") or "").strip()
            city   = (addr.get("addressLocality") or "").strip()
            state  = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()
            zipc   = (addr.get("postalCode") or "").strip()
            return {"street": street, "city": city, "state": state[:2], "zip": zipc}
    return {"street":"","city":"","state":"","zip":""}

def _extract_address_from_meta(html_txt: str) -> Dict[str,str]:
    un = html.unescape(html_txt or "")
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]twitter:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)\s*</title>",
        r"<meta[^>]+property=['\"]og:street-address['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, un, re.I)
        if not m: continue
        text = re.sub(r'\s+', ' ', m.group(1)).strip()
        s1 = RE_STREET_CITY_ST_ZIP.search(text)
        if s1:
            return {"street": s1.group(1), "city": s1.group(2), "state": s1.group(3).upper(), "zip": s1.group(4)}
        s2 = RE_CITY_ST_ZIP.search(text)
        if s2:
            return {"street":"", "city": s2.group(1), "state": s2.group(2).upper(), "zip": s2.group(3)}
        # Sometimes og:street-address alone returns a full string
        if re.search(r'^\d+\s', text) and ("," in text):
            a = _extract_address_from_text(text)
            if a.get("city") and a.get("state"): return a
    return {"street":"","city":"","state":"","zip":""}

def _extract_address_from_text(txt: str) -> Dict[str,str]:
    if not txt: return {"street":"","city":"","state":"","zip":""}
    s1 = RE_STREET_CITY_ST_ZIP.search(txt)
    if s1:
        return {"street": s1.group(1).strip(), "city": s1.group(2).strip(), "state": s1.group(3).upper(), "zip": s1.group(4).strip()}
    s2 = RE_CITY_ST_ZIP.search(txt)
    if s2:
        return {"street":"", "city": s2.group(1).strip(), "state": s2.group(2).upper(), "zip": s2.group(3).strip()}
    return {"street":"","city":"","state":"","zip":""}

# ----- Inline JSON address + coordinates -----
STREET_KEYS = ["streetAddress","street","street1","address1","addressLine1","line1","line","unparsedAddress","displayAddress","route"]
CITY_KEYS   = ["addressLocality","locality","city","town"]
STATE_KEYS  = ["addressRegion","region","state","stateOrProvince","province"]
ZIP_KEYS    = ["postalCode","zip","zipCode","postcode"]

def _find_first(html_txt: str, keys: List[str]) -> str:
    for k in keys:
        m = re.search(rf'["\']{re.escape(k)}["\']\s*:\s*["\']([^"\']+)["\']', html_txt, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return ""

def _extract_address_inline_json(html_txt: str) -> Dict[str,str]:
    if not html_txt: return {"street":"","city":"","state":"","zip":""}
    un = html.unescape(html_txt)
    street = _find_first(un, STREET_KEYS)
    city   = _find_first(un, CITY_KEYS)
    state  = _find_first(un, STATE_KEYS)
    zipc   = _find_first(un, ZIP_KEYS)
    if not (street or (city and state)):
        # fallback: fullAddress / addressText (grab then parse)
        m = re.search(r'["\'](?:fullAddress|addressText|displayAddress|address)["\']\s*:\s*["\']([^"\']{10,})["\']', un, re.I)
        if m:
            return _extract_address_from_text(m.group(1))
    return {"street":street, "city":city, "state":(state[:2] if state else ""), "zip":zipc}

def _float_ok(x: Optional[str]) -> Optional[float]:
    if x is None: return None
    try: return float(x)
    except Exception: return None

def _extract_latlon_inline(html_txt: str) -> Tuple[Optional[float], Optional[float]]:
    if not html_txt: return None, None
    un = html.unescape(html_txt)

    # Common "lat", "lng"
    m = re.search(r'"lat"\s*:\s*([\-0-9\.]+)\s*,\s*"lng"\s*:\s*([\-0-9\.]+)', un, re.I)
    if m: return _float_ok(m.group(1)), _float_ok(m.group(2))

    # "latitude", "longitude" (allow quoted)
    m = re.search(r'"latitude"\s*:\s*(?:["\']([\-0-9\.]+)["\']|([\-0-9\.]+))[^}]*?"longitude"\s*:\s*(?:["\']([\-0-9\.]+)["\']|([\-0-9\.]+))', un, re.I|re.S)
    if m:
        la = _float_ok(m.group(1) or m.group(2))
        lo = _float_ok(m.group(3) or m.group(4))
        return la, lo

    # latLng object
    m = re.search(r'"latLng"\s*:\s*\{\s*"lat"\s*:\s*([\-0-9\.]+)\s*,\s*"lng"\s*:\s*([\-0-9\.]+)\s*\}', un, re.I)
    if m: return _float_ok(m.group(1)), _float_ok(m.group(2))

    # data-lat / data-lng
    m = re.search(r'data-lat=["\']([\-0-9\.]+)["\'][^>]+data-lng=["\']([\-0-9\.]+)["\']', un, re.I)
    if m: return _float_ok(m.group(1)), _float_ok(m.group(2))

    # Meta tags for coords
    la = re.search(r'<meta[^>]+(latitude|place:location:latitude|itemprop=["\']latitude["\'])[^>]+content=["\']([\-0-9\.]+)["\']', un, re.I)
    lo = re.search(r'<meta[^>]+(longitude|place:location:longitude|itemprop=["\']longitude["\'])[^>]+content=["\']([\-0-9\.]+)["\']', un, re.I)
    if la and lo:
        return _float_ok(la.group(2)), _float_ok(lo.group(2))

    # Mapbox center: "center":[lon,lat]
    m = re.search(r'"center"\s*:\s*\[\s*([\-0-9\.]+)\s*,\s*([\-0-9\.]+)\s*\]', un, re.I)
    if m:
        a = _float_ok(m.group(1)); b = _float_ok(m.group(2))
        if a is not None and b is not None:
            lon, lat = a, b
            return lat, lon

    # Generic coordinates array: "coordinates":[lon,lat]  or [lat,lon]
    m = re.search(r'"coordinates"\s*:\s*\[\s*([\-0-9\.]+)\s*,\s*([\-0-9\.]+)\s*\]', un, re.I)
    if m:
        a = _float_ok(m.group(1)); b = _float_ok(m.group(2))
        if a is not None and b is not None:
            first_is_lat = abs(a) <= 90 and abs(b) <= 180
            return (a, b) if first_is_lat else (b, a)

    # Embedded Google Maps URLs (q=lat,lng or ll=lat,lng or q=address)
    for m in re.finditer(r'https?://(?:www\.)?google\.[^/"\']+/maps[^\s"\'<>()]+', un, re.I):
        try:
            qs = parse_qs(urlparse(m.group(0)).query)
            if "q" in qs:
                val = qs["q"][0]
                # q can be "lat,lng" OR "500 Denim Dr, Erwin NC"
                mm = re.match(r'\s*([\-0-9\.]+)\s*,\s*([\-0-9\.]+)\s*$', val)
                if mm:
                    return _float_ok(mm.group(1)), _float_ok(mm.group(2))
            if "ll" in qs:
                val = qs["ll"][0]
                mm = re.match(r'\s*([\-0-9\.]+)\s*,\s*([\-0-9\.]+)\s*$', val)
                if mm:
                    return _float_ok(mm.group(1)), _float_ok(mm.group(2))
        except Exception:
            pass

    # Mapbox URL fragments like ...#<zoom>/<lat>/<lng>
    m = re.search(r'#\d{2}\.?\d*/([\-0-9\.]+)/([\-0-9\.]+)', un)
    if m:
        return _float_ok(m.group(1)), _float_ok(m.group(2))

    return None, None

# ---------------- Geocoding ----------------
def _pick_best_geocode_result(results: List[Dict[str,Any]]) -> Optional[Dict[str,Any]]:
    if not results: return None
    def has_num(r):
        for c in r.get("address_components", []):
            if "street_number" in (c.get("types") or []):
                return True
        return False
    scored = []
    for r in results:
        types = r.get("types", [])
        lt = (r.get("geometry", {}).get("location_type") or "")
        score = 0
        if "street_address" in types: score += 4
        if "premise" in types or "subpremise" in types: score += 3
        if lt == "ROOFTOP": score += 3
        if lt == "RANGE_INTERPOLATED": score += 1
        if has_num(r): score += 2
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else results[0]

def reverse_geocode(lat: float, lon: float) -> Dict[str,str]:
    key = GOOGLE_MAPS_API_KEY
    if not key: return {"street":"","city":"","state":"","zip":""}
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"latlng": f"{lat},{lon}", "key": key},
            timeout=REQUEST_TIMEOUT
        )
        if not r.ok: return {"street":"","city":"","state":"","zip":""}
        data = r.json() or {}
        res = _pick_best_geocode_result(data.get("results") or [])
        if not res: return {"street":"","city":"","state":"","zip":""}
        comps = res.get("address_components") or []
        def get(types, short=False):
            for c in comps:
                t = c.get("types") or []
                if any(tt in t for tt in types):
                    return c.get("short_name") if short and c.get("short_name") else (c.get("long_name") or "")
            return ""
        num   = get(["street_number"])
        route = get(["route"])
        city  = get(["locality","postal_town","sublocality","administrative_area_level_3"])
        state = get(["administrative_area_level_1"], short=True)
        zipc  = get(["postal_code"])
        return {"street":(" ".join([p for p in [num,route] if p])).strip(), "city":city, "state":state, "zip":zipc}
    except Exception:
        return {"street":"","city":"","state":"","zip":""}

def forward_geocode(query: str) -> Dict[str, str]:
    """
    Geocode a freeform address string using Google Maps.
    Used when we only know the street (e.g., '1008 Joe Collins Road').
    """
    key = GOOGLE_MAPS_API_KEY
    if not key or not query:
        return {"street": "", "city": "", "state": "", "zip": ""}

    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": key},
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            return {"street": "", "city": "", "state": "", "zip": ""}

        data = r.json() or {}
        res = _pick_best_geocode_result(data.get("results") or [])
        if not res:
            return {"street": "", "city": "", "state": "", "zip": ""}

        comps = res.get("address_components") or []

        def get(types, short=False):
            for c in comps:
                t = c.get("types") or []
                if any(tt in t for tt in types):
                    return c.get("short_name") if short and c.get("short_name") else (c.get("long_name") or "")
            return ""

        num   = get(["street_number"])
        route = get(["route"])
        city  = get(["locality", "postal_town", "sublocality", "administrative_area_level_3"])
        state = get(["administrative_area_level_1"], short=True)
        zipc  = get(["postal_code"])

        street = (" ".join([p for p in [num, route] if p])).strip()
        return {"street": street, "city": city, "state": state, "zip": zipc}
    except Exception:
        return {"street": "", "city": "", "state": "", "zip": ""}

# ---------------- Zillow deeplink ----------------
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
    if not (city and state): return None
    parts = []
    if street: parts.append(street)
    loc = (city + " " + state).strip()
    if loc: parts.append(loc)
    if zipc:
        if parts: parts[-1] = parts[-1] + " " + zipc
        else: parts.append(zipc)
    slug = _slugify(", ".join(parts))
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ---------------- Core resolve ----------------
def _try_variants(url: str) -> List[Tuple[str,str,int]]:
    variants = [url]
    # common mobile/amp/share toggles
    variants.append(url + ("&amp=1" if "?" in url else "?amp=1"))
    variants.append(url + ("&m=1" if "?" in url else "?m=1"))
    variants.append(url + ("&noapp=1" if "?" in url else "?noapp=1"))
    try:
        u = urlparse(url)
        variants.append(urlunparse((u.scheme, u.netloc, u.path.rstrip('/') + "/amp", "", "", "")))
        variants.append(urlunparse((u.scheme, u.netloc, u.path.rstrip('/') + "/share", "", "", "")))
    except Exception:
        pass
    out = []
    f1, h1, c1, _ = _get(url, ua=DEFAULT_UA, allow_redirects=True)
    out.append((f1, h1, c1))
    for ua in SOCIAL_UAS:
        f2, h2, c2, _ = _get(url, ua=ua, allow_redirects=True); out.append((f2, h2, c2))
    for v in variants[1:]:
        f3, h3, c3, _ = _get(v, ua=DEFAULT_UA, allow_redirects=True); out.append((f3, h3, c3))
    for v in variants[1:]:
        for ua in SOCIAL_UAS:
            f4, h4, c4, _ = _get(v, ua=ua, allow_redirects=True); out.append((f4, h4, c4))
    return out

def _merge_addr(a: Dict[str,str], b: Dict[str,str]) -> Dict[str,str]:
    """Prefer street with a leading number; otherwise fill missing parts from b."""
    def has_num(s): return bool(s and re.match(r"^\d+\s", s))
    street = a.get("street") or ""
    if not has_num(street) and has_num(b.get("street","")):
        street = b.get("street","")
    elif not street:
        street = b.get("street","") or ""
    city  = a.get("city")  or b.get("city")  or ""
    state = a.get("state") or b.get("state") or ""
    zipc  = a.get("zip")   or b.get("zip")   or ""
    return {"street":street, "city":city, "state":state[:2], "zip":zipc}

def _full_with_city_anchor(html_txt: str, city: str, state: str) -> Dict[str,str]:
    """Find '123 Something Rd, <city>, <state> <zip>' anywhere if we know city/state."""
    un = html.unescape(html_txt or "")
    if not (city and state): return {"street":"","city":"","state":"","zip":""}
    # Very permissive: any 'number + street + , city, ST zzzzz'
    pat = re.compile(
        rf'(\d{{1,6}}\s+[A-Za-z0-9\.\'\-\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Cir|Highway|Hwy|Route|Parkway|Pkwy)\b[^\n,]*)\s*,\s*{re.escape(city)}\s*,\s*{re.escape(state)}\s+(\d{{5}}(?:-\d{{4}})?)',
        re.I
    )
    m = pat.search(un)
    if m:
        return {"street": m.group(1).strip(), "city": city, "state": state.upper(), "zip": m.group(2)}
    return {"street":"","city":"","state":"","zip":""}

def best_effort_address_from_hs(url: str) -> Dict[str,str]:
    tries = _try_variants(url)

    addr = {"street":"","city":"","state":"","zip":""}
    lat, lon = None, None

    # Pass 1: JSON-LD/meta for city/state (and maybe full)
    for _, html_txt, code in tries:
        if code != 200 or not html_txt: continue
        a = _extract_address_from_jsonld(html_txt)
        addr = _merge_addr(addr, a)
        b = _extract_address_from_meta(html_txt)
        addr = _merge_addr(addr, b)

    # Pass 2: Inline JSON keys + coords
    for _, html_txt, code in tries:
        if code != 200 or not html_txt: continue
        c = _extract_address_inline_json(html_txt)
        addr = _merge_addr(addr, c)
        if lat is None or lon is None:
            la, lo = _extract_latlon_inline(html_txt)
            lat = lat if lat is not None else la
            lon = lon if lon is not None else lo

    # Pass 3: If still no number, try readability + anchored full address
    if not re.match(r"^\d+\s", addr.get("street","")):
        final_url = tries[0][0] or url
        txt = _readable(final_url)
        if txt:
            # Try anchored full address using known city/state
            a2 = _full_with_city_anchor(txt, addr.get("city",""), addr.get("state",""))
            addr = _merge_addr(addr, a2)
            # Generic scan as well
            addr = _merge_addr(addr, _extract_address_from_text(txt))

    # Pass 4: Reverse geocode if we captured any coords
    if not re.match(r"^\d+\s", addr.get("street","")) and (lat is not None and lon is not None):
        geo = reverse_geocode(lat, lon)
        addr = _merge_addr(addr, geo)

    # Pass 5: if we have a street but no city/state, try forward geocoding it
    if addr.get("street") and not (addr.get("city") and addr.get("state")):
        geo2 = forward_geocode(addr["street"])
        addr = _merge_addr(addr, geo2)

    return addr

def resolve_any_link_to_zillow_rb(source_url: str) -> Tuple[str, str, str]:
    """
    Returns: (deeplink_or_fallback, display_address, note)
    """
    if not source_url: return "", "", "empty"
    final, html_txt, code, _ = _get(source_url, ua=DEFAULT_UA, allow_redirects=True)
    target = final or source_url

    # If already a zillow homes/homedetails link
    if "zillow.com" in target and ("/homedetails/" in target or "/homes/" in target):
        clean = re.sub(r"[?#].*$", "", target)
        return clean, "", "already_zillow"

    host = (urlparse(target).hostname or "").lower()
    if any(k in host for k in ["homespotter", "hms.pt", "idx."]):
        addr = best_effort_address_from_hs(target)
    else:
        addr = {"street":"","city":"","state":"","zip":""}
        if code == 200 and html_txt:
            addr = _merge_addr(addr, _extract_address_from_jsonld(html_txt))
            addr = _merge_addr(addr, _extract_address_from_meta(html_txt))
            if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
                addr = _merge_addr(addr, _extract_address_inline_json(html_txt))
                la, lo = _extract_latlon_inline(html_txt)
                if (not re.match(r"^\d+\s", addr.get("street",""))) and (la is not None and lo is not None):
                    addr = _merge_addr(addr, reverse_geocode(la, lo))
            if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
                txt = _readable(target)
                addr = _merge_addr(addr, _extract_address_from_text(txt))

    street, city, state, zipc = addr.get("street",""), addr.get("city",""), addr.get("state",""), addr.get("zip","")

    # One more shot: anchored match on original HTML if we know city/state but still no number
    if (not re.match(r"^\d+\s", street)) and html_txt and (city and state):
        addr2 = _full_with_city_anchor(html_txt, city, state)
        if addr2.get("street"):
            street, city, state, zipc = addr2["street"], addr2["city"], addr2["state"], addr2["zip"]

    deeplink = zillow_rb_from_address(street, city, state, zipc)
    note = "ok" if re.match(r"^\d+\s", street) else "no_number"
    display_addr = ", ".join([p for p in [street, f"{city} {state}".strip(), zipc] if p])

    if not deeplink:
        # Fallback: always return a real http(s) URL so Streamlit doesn't treat it as a local path
        if source_url.startswith(("http://", "https://")):
            fallback_url = source_url
        else:
            query = display_addr or source_url
            fallback_url = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)

        return fallback_url, display_addr, "fallback"

    return deeplink, display_addr, note

# ---------------- UI helpers ----------------
def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text: return []
    # Try CSV first
    try:
        sample = text.splitlines()
        if len(sample) >= 2 and ("," in sample[0] or "\t" in sample[0]):
            dialect = csv.Sniffer().sniff(sample[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            rows = [dict(r) for r in reader]
            if rows: return rows
    except Exception:
        pass
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        if s.startswith("http://") or s.startswith("https://"):
            rows.append({"url": s})
        else:
            rows.append({"address": s})
    return rows

def _results_list(results: List[Dict[str, Any]]):
    items = []
    for r in results:
        u = (r.get("zillow_url") or r.get("original") or "").strip()
        if not u:
            continue

        # Safety: if somehow we still get a non-URL, make it a Google search
        if not (u.startswith("http://") or u.startswith("https://")):
            u = "https://www.google.com/search?q=" + quote_plus(u)

        addr = r.get("display_address") or ""
        badge = r.get("note","")
        bh = ""
        if badge == "ok":
            bh = ' <span class="badge ok">street # found</span>'
        elif badge == "no_number":
            bh = ' <span class="badge warn">no street #</span>'
        label = (addr + " — " + u) if addr else u
        items.append(f'<li style="margin:0.2rem 0;"><a href="{escape(u)}" target="_blank" rel="noopener">{escape(label)}</a>{bh}</li>')
    html_list = "\n".join(items) if items else "<li>(no results)</li>"

    raw_lines = "\n".join([(r.get("zillow_url") or r.get("original") or "").strip() for r in results if (r.get("zillow_url") or r.get("original"))]) + "\n"
    js_lines = json.dumps(raw_lines)

    components.html(f"""
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
          <ul class="link-list">{html_list}</ul>
        </div>
        <script>
          (function(){{
            const lines = {js_lines};
            const btn = document.getElementById('copyAll');
            btn.addEventListener('click', async () => {{
              try {{ await navigator.clipboard.writeText(lines); btn.textContent='✓'; setTimeout(()=>btn.textContent='Copy',900); }}
              catch(e) {{ btn.textContent='×'; setTimeout(()=>btn.textContent='Copy',900); }}
            }});
          }})();
        </script>
      </body></html>
    """, height=min(600, 40 * max(1, len(items)) + 60), scrolling=False)

def _streetview_thumb(query_addr: str) -> Optional[str]:
    if not GOOGLE_MAPS_API_KEY or not query_addr: return None
    loc = quote_plus(query_addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={GOOGLE_MAPS_API_KEY}"

# ---------------- Main UI ----------------
def render_run_tab(state: dict = None):
    st.header("Address Alchemist — Homespotter Resolver")
    st.caption("Paste Homespotter / HMS links or upload CSV with a `url` column. We’ll pull the full address (with number) and build a Zillow **/_rb/** deeplink.")

    colA, colB = st.columns([1.4, 1])
    with colA:
        paste = st.text_area("Paste links or addresses", height=140, label_visibility="collapsed")
    with colB:
        up = st.file_uploader("Upload CSV", type=["csv"], label_visibility="visible")
        show_sv = st.checkbox("Show Street View thumbnails", value=False, help="Requires GOOGLE_MAPS_API_KEY")

    rows_in: List[Dict[str, Any]] = []
    rows_in.extend(_rows_from_paste(paste))

    # ---- CSV upload: case-insensitive headers, multiple URL/address column names ----
    if up is not None:
        try:
            content = up.getvalue().decode("utf-8-sig", errors="ignore")
            reader = csv.DictReader(io.StringIO(content))

            for row in reader:
                if not row:
                    continue

                # Normalize header names to lowercase once
                row_lc = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

                # Try a bunch of common URL column names
                u = (
                    row_lc.get("url")
                    or row_lc.get("link")
                    or row_lc.get("source_url")
                    or row_lc.get("listing_url")
                    or row_lc.get("property_url")
                    or row_lc.get("hs_link")
                )

                # Optionally support an address column from CSV
                addr = (
                    row_lc.get("address")
                    or row_lc.get("full_address")
                    or row_lc.get("property_address")
                )

                if u:
                    rows_in.append({"url": u})
                elif addr:
                    rows_in.append({"address": addr})

        except Exception as e:
            st.warning(f"Could not read CSV: {e}")

    st.write(f"Parsed **{len(rows_in)}** row(s).")

    if st.button("🚀 Resolve to Zillow"):
        if not rows_in:
            st.warning("Nothing to process."); st.stop()

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            raw = (row.get("url") or row.get("source_url") or row.get("href") or row.get("address") or "").strip()
            if not raw:
                continue

            # Guess if this is a URL even if scheme is missing
            looks_like_url = (
                raw.startswith(("http://", "https://"))
                or re.match(r'^[\w.-]+\.[a-z]{2,10}(/|$)', raw, re.I)  # e.g. homespotter.com/..., hms.pt/...
            )

            if looks_like_url:
                # Ensure we have a scheme
                if not raw.startswith(("http://", "https://")):
                    u = "https://" + raw
                else:
                    u = raw

                z, addr, note = resolve_any_link_to_zillow_rb(u)
                out: Dict[str, Any] = {
                    "original": raw,
                    "zillow_url": z or u,
                    "display_address": addr or "",
                    "note": note,
                }
                if show_sv and addr:
                    thumb = _streetview_thumb(addr)
                    if thumb:
                        out["image_url"] = thumb
                results.append(out)

            else:
                # Treat as address → rb best-effort
                parts = re.split(r"\s*,\s*", raw)
                street, city, state, zipc = raw, "", "", ""
                if len(parts) >= 2:
                    street = parts[0]
                    m = RE_CITY_ST_ZIP.search(", ".join(parts[1:]))
                    if m:
                        city, state, zipc = m.group(1), m.group(2), m.group(3)
                z = zillow_rb_from_address(street, city, state, zipc) or ""
                results.append({
                    "original": raw,
                    "zillow_url": z or raw,
                    "display_address": raw,
                    "note": "manual"
                })

            prog.progress(i/len(rows_in), text=f"Resolved {i}/{len(rows_in)}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results")
        _results_list(results)

        thumbs = [(r.get("zillow_url",""), r.get("image_url",""), r.get("display_address","")) for r in results if r.get("image_url")]
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i, (link, img, addr) in enumerate(thumbs):
                with cols[i % 3]:
                    st.image(img, use_container_width=True)
                    st.markdown(f'<div class="img-label">{escape(addr or "")}</div>', unsafe_allow_html=True)

        st.markdown("#### Export")
        fmt = st.radio("Format", ["txt","csv","md","html"], horizontal=True)
        if fmt == "csv":
            buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=["original","zillow_url","display_address","note"]); w.writeheader()
            for r in results: w.writerow({k: r.get(k,"") for k in ["original","zillow_url","display_address","note"]})
            payload, mime, fname = buf.getvalue(), "text/csv", "resolved.csv"
        elif fmt == "html":
            items = "\n".join([f'<li><a href="{escape(r.get("zillow_url",""))}" target="_blank" rel="noopener">{escape(r.get("zillow_url",""))}</a></li>' for r in results])
            payload, mime, fname = "<ul>\n"+items+"\n</ul>\n", "text/html", "resolved.html"
        elif fmt == "md":
            payload, mime, fname = "\n".join([r.get("zillow_url","") for r in results]) + "\n", "text/markdown", "resolved.md"
        else:
            payload, mime, fname = "\n".join([r.get("zillow_url","") for r in results]) + "\n", "text/plain", "resolved.txt"
        st.download_button("Download", data=payload.encode("utf-8"), file_name=fname, mime=mime, use_container_width=True)

    st.divider()
    st.subheader("Fix / Re-run links")
    st.caption("Paste any listing links; I’ll parse + reverse-geocode if needed to build clean Zillow **/_rb/** links.")
    fix_text = st.text_area("Links to fix", height=140, key="fix_area")
    if st.button("🔧 Fix / Re-run"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed, shown = [], []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            z, addr, note = resolve_any_link_to_zillow_rb(u)
            best = z or u
            badge = "✅" if note == "ok" else "⚠️"
            fixed.append(best)
            label = (addr or best)
            shown.append(f"- {badge} [{escape(label)}]({escape(best)})")
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")
        st.markdown("**Fixed links**")
        st.markdown("\n".join(shown), unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")

if __name__ == "__main__":
    render_run_tab(st.session_state)
