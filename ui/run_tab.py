# ui/run_tab.py
# Address/link → Zillow resolver with robust Homespotter handling
# - Fetches BOTH initial short-link HTML (no-redirect) AND final page HTML
# - Extracts address from JSON-LD, meta tags, and Homespotter-specific fields
# - Returns /homedetails/... when possible; otherwise a Zillow address-search deeplink
# - Keeps "Fix properties" section
# - render_run_tab(state: dict)

import os, csv, io, re, json, time
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, quote

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional deps ----------
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Safe import of clients helpers ----------
def _safe_import_attr(module_name: str, attr: str, default=None):
    try:
        import importlib
        mod = importlib.import_module(module_name)
        return getattr(mod, attr, default)
    except Exception:
        return default

fetch_clients = _safe_import_attr("ui.clients_tab", "fetch_clients", default=lambda include_inactive=False: [])
upsert_client = _safe_import_attr("ui.clients_tab", "upsert_client", default=lambda name, active=True: (False, "Clients module not available"))

# ---------- Styles ----------
st.markdown("""
<style>
.center-box { padding:10px 12px; background:transparent; border-radius:12px; }
.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.new { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.dup { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
.run-zone .stButton>button { background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important; color:#fff !important; font-weight:800 !important; border:0 !important; border-radius:12px !important; box-shadow:0 8px 20px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important; }
</style>
""", unsafe_allow_html=True)

REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ---------- Generic utils ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

# NEW: fetch both initial (no-redirect) and final pages
def fetch_initial_and_final(url: str) -> Tuple[str, str, str, int]:
    """
    Returns: (final_url, initial_html, final_html, status_code_final)
    - initial_html: HTML from the initial request with allow_redirects=False (often contains OG tags on shorteners)
    - final_html: HTML after following redirects
    """
    initial_html = ""
    final_html = ""
    final_url = url
    status = 0

    try:
        r0 = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        initial_html = r0.text or ""
        # Follow redirects manually if present
        hops = 0
        cur = r0
        next_url = url
        while cur.is_redirect or cur.is_permanent_redirect:
            hops += 1
            if hops > 6:
                break
            loc = cur.headers.get("Location") or cur.headers.get("location")
            if not loc:
                break
            if loc.startswith("/"):
                parsed = urlparse(next_url)
                next_url = f"{parsed.scheme}://{parsed.netloc}{loc}"
            else:
                next_url = loc
            cur = requests.get(next_url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        final_url = cur.url
        status = cur.status_code
        if cur.ok:
            final_html = cur.text or ""
    except Exception:
        # Fallback: single fetch with redirects
        try:
            rr = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            final_url = rr.url
            status = rr.status_code
            final_html = rr.text if rr.ok else ""
        except Exception:
            pass

    return final_url, initial_html, final_html, status

# ---------- JSON-LD helper ----------
def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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

# ---------- Zillow helpers ----------
ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url or "zillow.com" not in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok: return url
        html = r.text
        for pat in [
            r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']',
            r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']'
        ]:
            m = re.search(pat, html, re.I)
            if m: return m.group(1)
        for pat in [
            r'"canonicalUrl"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
            r'"url"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m: return m.group(1)
    except Exception:
        pass
    return url

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ---------- Address extraction ----------
def extract_title_or_desc(html: str) -> str:
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m: return re.sub(r'\s+', ' ', m.group(1)).strip()
    return ""

def extract_address_from_jsonld(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    try:
        blocks = _jsonld_blocks(html)
        for b in blocks:
            if not isinstance(b, dict): 
                continue
            addr = b.get("address") or b.get("itemOffered", {}).get("address")
            if isinstance(addr, dict):
                out["street"] = out["street"] or (addr.get("streetAddress") or "")
                out["city"]   = out["city"]   or (addr.get("addressLocality") or "")
                stt = (addr.get("addressRegion") or addr.get("addressCountry") or "")
                out["state"] = out["state"] or (stt[:2] if isinstance(stt, str) else "")
                out["zip"]   = out["zip"]   or (addr.get("postalCode") or "")
                if out["street"]:
                    break
    except Exception:
        pass
    return out

def extract_address_from_meta_bits(html: str, seed: Dict[str,str]) -> Dict[str,str]:
    out = dict(seed)
    pats = [
        (r'"street(Address|1)?"\s*:\s*"([^"]+)"', "street", 2),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city", 1),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state", 1),
        (r'"postal(Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip", 2),
        (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street", 1),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city", 1),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', "state", 1),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip", 1),
        # Extra Homespotter-ish names
        (r'"fullAddress"\s*:\s*"([^"]+)"', "street", 1),
        (r'"displayAddress"\s*:\s*"([^"]+)"', "street", 1),
        (r'"formattedAddress"\s*:\s*"([^"]+)"', "street", 1),
    ]
    for pat, key, gi in pats:
        if not out.get(key):
            m = re.search(pat, html, re.I)
            if m:
                out[key] = m.group(gi).strip()

    if not out.get("street"):
        title = extract_title_or_desc(html)
        if title and re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
            out["street"] = title
    return out

def extract_address_from_html(html: str) -> Dict[str, str]:
    jl = extract_address_from_jsonld(html)
    return extract_address_from_meta_bits(html, jl)

# ---------- Homespotter detection + extract ----------
def _is_homespotter_like(u: str) -> bool:
    try:
        h = urlparse(u).hostname or ""
        return ("l.hms.pt" in h) or ("idx.homespotter.com" in h) or ("homespotter" in h)
    except Exception:
        return False

def _extract_addr_homespotter(html: str) -> Dict[str, str]:
    addr = extract_address_from_html(html)
    if addr.get("street"):
        return addr
    # More aggressive patterns commonly present in short-link HTML or preloaded state
    extra = [
        (r'"address1"\s*:\s*"([^"]+)"', "street"),
        (r'"street"\s*:\s*"([^"]+)"', "street"),
        (r'"city"\s*:\s*"([^"]+)"', "city"),
        (r'"state(?:OrProvince)?"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'"postal(?:Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
        (r'data-address=["\']([^"\']+)["\']', "street"),
        (r'data-city=["\']([^"\']+)["\']', "city"),
        (r'data-state=["\']([A-Za-z]{2})["\']', "state"),
        (r'data-zip=["\'](\d{5}(?:-\d{4})?)["\']', "zip"),
    ]
    for pat, key in extra:
        if not addr.get(key):
            m = re.search(pat, html, re.I)
            if m:
                addr[key] = m.group(1).strip()

    if not addr.get("street"):
        title = extract_title_or_desc(html)
        if title and re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
            addr["street"] = title
    return addr

# ---------- Zillow address-search deeplink ----------
def _compose_addr_line(street: str, city: str, state: str, zipc: str) -> str:
    parts = []
    if street: parts.append(street)
    loc = " ".join([p for p in [city, state] if p]).strip()
    if loc: parts.append(loc)
    if zipc and parts:
        parts[-1] = f"{parts[-1]} {zipc}"
    line = re.sub(r"[#&]", " ", ", ".join([p for p in parts if p]).strip())
    return re.sub(r"\s+", " ", line).strip()

def build_zillow_search_deeplink(street: str, city: str, state: str, zipc: str) -> str:
    term = _compose_addr_line(street or "", city or "", state or "", zipc or "")
    if not term:
        # Don't emit a blank /homes/ link — return the base search homepage only if nothing at all is known.
        return "https://www.zillow.com/homes/"
    q = {
        "pagination": {},
        "usersSearchTerm": term,
        "mapBounds": {"west": -180, "east": 180, "south": -90, "north": 90},
        "isMapVisible": False,
        "filterState": {},
        "isListVisible": True
    }
    return "https://www.zillow.com/homes/?searchQueryState=" + quote(json.dumps(q), safe="")

def ensure_address_based_zillow_link(zurl: str, street: str, city: str, state: str, zipc: str) -> str:
    z = (zurl or "").strip()
    if z.startswith("https://www.zillow.com/") and "/homedetails/" in z:
        return z
    return build_zillow_search_deeplink(street, city, state, zipc)

# ---------- Minimal deeplink from address words (used only for upgrades) ----------
def construct_deeplink_from_parts(street: str, city: str, state: str, zipc: str) -> str:
    slug_parts = []
    if street: slug_parts.append(street)
    loc_bits = " ".join([p for p in [city, state] if p]).strip()
    if loc_bits:
        slug_parts.append(loc_bits + ((" " + zipc) if zipc else ""))
    slug = ", ".join(slug_parts) if slug_parts else ""
    a = slug.lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ---------- Source URL resolver (short-link aware) ----------
def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    """
    1) Fetch initial (no-redirect) HTML and final HTML.
    2) If Homespotter, extract address from initial short-link HTML first; then final HTML.
    3) Try to upgrade any Zillow /homes/*_rb/ to /homedetails/ URL.
    4) If no homedetails found, return an address-based Zillow search deeplink.
    Returns (zillow_url, display_address)
    """
    preferred_state = (defaults.get("state") or "").strip()
    final_url, init_html, final_html, _ = fetch_initial_and_final(source_url)

    # Decide extraction order: the initial short page often has the address
    host0 = (urlparse(source_url).hostname or "").lower()
    hostF = (urlparse(final_url).hostname or "").lower()

    addr = {"street":"", "city":"", "state":"", "zip":""}

    # 1) If initial is Homespotter shortener, try it first
    if "l.hms.pt" in host0 or "homespotter" in host0:
        a0 = _extract_addr_homespotter(init_html)
        for k in addr.keys():
            if a0.get(k): addr[k] = a0[k]

    # 2) Then final page
    if not addr.get("street"):
        if "homespotter" in hostF:
            a1 = _extract_addr_homespotter(final_html)
        else:
            a1 = extract_address_from_html(final_html)
        for k in addr.keys():
            if not addr.get(k) and a1.get(k):
                addr[k] = a1[k]

    # 3) If still no street, try any meta/title on initial short page again
    if not addr.get("street"):
        a2 = extract_address_from_html(init_html)
        for k in addr.keys():
            if not addr.get(k) and a2.get(k):
                addr[k] = a2[k]

    street = addr.get("street","") or ""
    city   = addr.get("city","") or ""
    state  = (addr.get("state","") or preferred_state)
    zipc   = addr.get("zip","") or ""

    # If final is a Zillow /homes/ URL, try to upgrade it
    candidate = final_url
    if "zillow.com" in candidate and "/homes/" in candidate and "/homedetails/" not in candidate:
        candidate = upgrade_to_homedetails_if_needed(candidate)

    # Build the final, address-safe output
    final = ensure_address_based_zillow_link(candidate, street, city, state, zipc)

    display_addr = _compose_addr_line(street, city, state, zipc)
    return final, display_addr

# ---------- Parsers for pasted input ----------
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}

def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    # CSV first
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
    # Fallback: one per line
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

def _detect_source_url(row: Dict[str, Any]) -> Optional[str]:
    for k, v in row.items():
        k_norm = re.sub(r"\s+", " ", (str(k) or "").strip().lower())
        if k_norm in URL_KEYS and is_probable_url(str(v)):
            return str(v).strip()
    for k in ("url", "source", "href", "link"):
        if is_probable_url(str(row.get(k, ""))):
            return str(row.get(k)).strip()
    return None

# ---------- Build output ----------
def build_output(rows: List[Dict[str, Any]], fmt: str):
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","url","status"]
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
            if not u: continue
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    lines = []
    for r in rows:
        u = pick_url(r)
        if u: lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")

# ---------- Results list ----------
def results_list_with_copy_all(results: List[Dict[str, Any]]):
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

# ---------- Main renderer ----------
def render_run_tab(state: dict):
    st.header("Run")

    # Client (optional)
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"
    clients = []
    try:
        clients = fetch_clients(include_inactive=False) or []
    except Exception:
        clients = []
    client_names = [c.get("name", "") for c in clients if c.get("name")]
    options = [NO_CLIENT] + client_names + [ADD_SENTINEL]

    with st.container():
        c1, c2 = st.columns([2, 1])
        with c1:
            chosen = st.selectbox("Client", options, index=0)
        with c2:
            campaign = st.text_input("Campaign tag (optional)", value=state.get("campaign", ""))

        if chosen == ADD_SENTINEL:
            new_name = st.text_input("New client name")
            if st.button("Create client", type="primary"):
                ok, msg = upsert_client(new_name.strip(), active=True)
                if ok:
                    st.success(f"Added “{new_name}”.")
                    try: st.rerun()
                    except Exception: pass
                else:
                    st.error(msg or "Could not add client.")
            return

    # Paste area
    st.subheader("Paste rows")
    st.caption("Paste CSV with address fields or URLs, **or** paste one URL/address per line.")
    paste = st.text_area(
        "Input",
        height=180,
        placeholder="e.g. 407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718",
        key="__paste__"
    )

    with st.expander("Options"):
        default_state = st.text_input("Default state (2-letter)", value="NC")
        try_usaddr = st.checkbox("Normalize addresses with usaddress (if available)", value=True)

    run_btn = st.button("🚀 Run", type="primary", use_container_width=True)
    results: List[Dict[str, Any]] = []

    if run_btn:
        rows_in: List[Dict[str, Any]] = _rows_from_paste(paste)

        # Optional normalization for freeform address lines
        if try_usaddr and usaddress:
            normalized = []
            for row in rows_in:
                if "address" in row and row.get("address"):
                    try:
                        parts = usaddress.tag(row["address"])[0]
                        norm = (parts.get("AddressNumber","") + " " +
                                " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())
                        cityst = ((", " + parts.get("PlaceName","") + ", " + parts.get("StateName","") +
                                   (" " + parts.get("ZipCode","") if parts.get("ZipCode") else "")) if (parts.get("PlaceName") or parts.get("StateName")) else "")
                        row["address"] = re.sub(r"\s+"," ", (norm + cityst).strip())
                    except Exception:
                        pass
                normalized.append(row)
            rows_in = normalized

        if not rows_in:
            st.warning("Nothing to process.")
            return

        defaults = {"state": (default_state or "").strip().upper(), "city":"", "zip":""}
        total = len(rows_in)
        prog = st.progress(0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            src_url = _detect_source_url(row)
            if src_url:
                zurl, inferred_addr = resolve_from_source_url(src_url, defaults)
                # If we *still* somehow got only /homes/ (blank), keep the original link instead of useless Zillow root.
                if zurl.strip().rstrip("/") == "https://www.zillow.com/homes":
                    zurl = src_url
                results.append({
                    "input_address": inferred_addr or (row.get("address") or ""),
                    "zillow_url": zurl,
                    "status": "source_url",
                    "preview_url": make_preview_url(zurl),
                    "display_url": zurl,
                })
            else:
                # Plain address → direct Zillow search deeplink
                addr = (row.get("address") or "").strip()
                street = city = stt = zipc = ""
                if addr and usaddress and try_usaddr:
                    try:
                        parts = usaddress.tag(addr)[0]
                        street = re.sub(r"\s+", " ", (parts.get("AddressNumber","") + " " +
                                  " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())).strip()
                        city = parts.get("PlaceName","") or ""
                        stt  = (parts.get("StateName","") or defaults["state"] or "")
                        zipc = parts.get("ZipCode","") or ""
                    except Exception:
                        pass
                if not street:
                    street = addr
                    stt = defaults["state"]

                zsearch = build_zillow_search_deeplink(street, city, stt or defaults["state"], zipc)
                results.append({
                    "input_address": _compose_addr_line(street, city, stt or defaults["state"], zipc),
                    "zillow_url": zsearch,
                    "status": "address_search",
                    "preview_url": zsearch,
                    "display_url": zsearch,
                })

            prog.progress(i/total, text=f"Resolved {i}/{total}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results")
        results_list_with_copy_all(results)

        # Export
        fmt = st.radio("Export format", ["txt", "md", "html", "csv"], horizontal=True, index=0)
        payload, mime = build_output(results, fmt=fmt)
        st.download_button(
            "Download",
            data=payload.encode("utf-8"),
            file_name=f"results.{fmt}",
            mime=mime,
            use_container_width=True,
        )

        st.divider()

    # ---------- Fix properties ----------
    st.subheader("Fix properties")
    st.caption("Paste listing links (Homespotter, MLS, Zillow /homes/*_rb/). I’ll output clean **Zillow** links. If /homedetails/ can’t be found, you’ll get a **Zillow address-search** link — not a generic state page.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")

    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")

        defaults = {"state": "NC", "city": "", "zip": ""}

        for i, u in enumerate(lines, start=1):
            best = u
            try:
                final_url, init_html, final_html, _ = fetch_initial_and_final(u)

                # Try to upgrade Zillow /homes/ → /homedetails/
                if "zillow.com" in final_url and "/homes/" in final_url and "/homedetails/" not in final_url:
                    final_url = upgrade_to_homedetails_if_needed(final_url)

                if "/homedetails/" not in (final_url or ""):
                    # Extract address from either initial or final HTML
                    if _is_homespotter_like(u) or _is_homespotter_like(final_url):
                        adr = _extract_addr_homespotter(init_html)  # short page first
                        if not adr.get("street"):
                            a2 = _extract_addr_homespotter(final_html)
                            for k in ("street","city","state","zip"):
                                if not adr.get(k) and a2.get(k): adr[k] = a2[k]
                    else:
                        adr = extract_address_from_html(final_html or init_html)

                    street = adr.get("street","")
                    city   = adr.get("city","")
                    stt    = adr.get("state","") or defaults["state"]
                    zipc   = adr.get("zip","")

                    final_url = ensure_address_based_zillow_link(final_url, street, city, stt, zipc)

                best = final_url or best
            except Exception:
                pass

            if "/homedetails/" in (best or ""):
                best, _ = canonicalize_zillow(best)

            # Never return blank /homes/ root if we started with a specific link
            if best.strip().rstrip("/") == "https://www.zillow.com/homes":
                best = u

            fixed.append(best)
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")

        prog.progress(1.0, text="Done")

        items = "\n".join([f"- [{escape(x)}]({escape(x)})" for x in fixed])
        st.markdown("**Fixed links**")
        st.markdown(items, unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# (optional) run directly
if __name__ == "__main__":
    render_run_tab({})
