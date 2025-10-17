# ui/run_tab.py
# Robust Homespotter → Zillow resolver
# - Reads initial short-link HTML (no redirects) + final HTML
# - Tries alternative Homespotter endpoints to scrape JSON/HTML for the address
# - Builds Zillow address-search deeplink from the actual address (never blank /homes/)
# - Keeps "Fix properties" section
# - render_run_tab(state: dict)

import os, csv, io, re, json, time
from typing import List, Dict, Any, Optional, Tuple, Iterable
from html import escape
from urllib.parse import urlparse, urljoin, quote

import requests
import streamlit as st
import streamlit.components.v1 as components

# ----------------------------- Basic config -----------------------------
REQUEST_TIMEOUT = 12
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ----------------------------- Tiny helpers -----------------------------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

# Read BOTH the initial (no-redirect) response and the final page after following redirects
def fetch_initial_and_final(url: str) -> Tuple[str, str, str, int]:
    """
    Returns: (final_url, initial_html, final_html, final_status)
    """
    initial_html, final_html, final_url, status = "", "", url, 0
    try:
        r0 = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        initial_html = r0.text or ""
        hops = 0
        cur = r0
        next_url = url
        while (cur.is_redirect or cur.is_permanent_redirect) and hops < 7:
            hops += 1
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
        try:
            rr = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            final_url = rr.url
            status = rr.status_code
            final_html = rr.text if rr.ok else ""
        except Exception:
            pass
    return final_url, initial_html, final_html, status

# ----------------------------- JSON-LD + meta extraction -----------------------------
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

def extract_title_or_desc(html: str) -> str:
    if not html:
        return ""
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def extract_address_from_jsonld(html: str) -> Dict[str, str]:
    out = {"street": "", "city": "", "state": "", "zip": ""}
    for b in _jsonld_blocks(html):
        if not isinstance(b, dict):
            continue
        addr = b.get("address") or (b.get("itemOffered", {}) or {}).get("address")
        if isinstance(addr, dict):
            out["street"] = out["street"] or (addr.get("streetAddress") or "")
            out["city"]   = out["city"]   or (addr.get("addressLocality") or "")
            stt = addr.get("addressRegion") or addr.get("addressCountry") or ""
            out["state"]  = out["state"]  or (stt[:2] if isinstance(stt, str) else "")
            out["zip"]    = out["zip"]    or (addr.get("postalCode") or "")
            if out["street"]:
                break
    return out

def extract_address_from_meta_bits(html: str, seed: Dict[str, str]) -> Dict[str, str]:
    out = dict(seed)
    if not html:
        return out
    pats = [
        (r'"street(Address|1)?"\s*:\s*"([^"]+)"', "street", 2),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city", 1),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state", 1),
        (r'"postal(Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip", 2),
        (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street", 1),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city", 1),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', "state", 1),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip", 1),
        # Homespotter-ish keys:
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

# ----------------------------- JSON walker (any payload) -----------------------------
def _walk(obj: Any) -> Iterable[Any]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk(x)

ADDR_KEYS = {
    "street": {"street", "street1", "streetaddress", "address1", "line1", "address", "displayaddress", "formattedaddress", "fulladdress"},
    "city":   {"city", "locality", "addresslocality"},
    "state":  {"state", "region", "addressregion", "statecode"},
    "zip":    {"zip", "zipcode", "postalcode"},
}

def _normkey(k: str) -> str:
    return re.sub(r"[^a-z]", "", (k or "").lower())

def extract_address_from_json_any(text: str) -> Dict[str, str]:
    """
    Attempt to parse arbitrary JSON/JS payloads for address fields (recursively).
    """
    out = {"street": "", "city": "", "state": "", "zip": ""}
    if not text:
        return out
    # Try to locate a large JSON blob in text
    candidates = []
    # Straight JSON in the whole text
    try:
        data = json.loads(text)
        candidates.append(data)
    except Exception:
        pass
    # JSON inside <script> var = {...} ;
    for m in re.finditer(r'=\s*({.*?})\s*[,;]\s*$', text, re.S | re.M):
        blob = m.group(1)
        try:
            data = json.loads(blob)
            candidates.append(data)
        except Exception:
            continue
    # Fallback: look for {"...":"..."} minimum braces
    if not candidates:
        for m in re.finditer(r'(\{[^{}]{20,}\})', text, re.S):
            blob = m.group(1)
            try:
                data = json.loads(blob)
                candidates.append(data)
            except Exception:
                continue

    for data in candidates:
        try:
            for node in _walk(data):
                if not isinstance(node, dict):
                    continue
                keys = set(_normkey(k) for k in node.keys())
                # collect best guess
                got: Dict[str, str] = {}
                for want, pool in ADDR_KEYS.items():
                    for k in node.keys():
                        if _normkey(k) in pool:
                            v = node.get(k)
                            if isinstance(v, str) and v.strip():
                                got[want] = v.strip()
                                break
                # merge
                for k in ("street", "city", "state", "zip"):
                    if got.get(k) and not out.get(k):
                        out[k] = got[k]
                # Early exit if we already have enough
                if out["street"] and (out["city"] or out["state"]):
                    return out
        except Exception:
            continue
    return out

# ----------------------------- Homespotter helpers -----------------------------
def _is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return ("l.hms.pt" in h) or ("idx.homespotter.com" in h) or ("homespotter" in h)
    except Exception:
        return False

def _parse_hs_path(u: str) -> Tuple[str, str, str]:
    """
    Extract (site_slug, board, listing_id) from a Homespotter IDX URL if possible.
    E.g. https://idx.homespotter.com/hs_triangle/tmlspar/10127718 -> ("hs_triangle","tmlspar","10127718")
    """
    try:
        p = urlparse(u).path.strip("/")
        parts = p.split("/")
        if len(parts) >= 3 and parts[0].startswith("hs_"):
            return parts[0], parts[1], parts[2]
    except Exception:
        pass
    return "", "", ""

def _try_get(url: str) -> Optional[str]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.ok and (r.text or "").strip():
            return r.text
    except Exception:
        return None
    return None

def _probe_homespotter_variants(final_url: str) -> List[str]:
    """
    For a given Homespotter IDX URL, try a handful of nearby endpoints that often expose
    address JSON/HTML (embed/share/api). Returns a list of payload texts to parse.
    """
    texts: List[str] = []
    parsed = urlparse(final_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    site, board, lid = _parse_hs_path(final_url)
    # Variant guesses (best-effort; harmless if 404)
    candidates = []
    if site and board and lid:
        # share & embed-ish
        candidates += [
            f"{base}/{site}/{board}/{lid}?output=embed",
            f"{base}/{site}/share/{board}/{lid}",
            f"{base}/{site}/listing/{board}/{lid}",
            f"{base}/{site}/{board}/{lid}?format=json",
        ]
        # api-ish
        candidates += [
            f"{base}/{site}/api/listing/{board}/{lid}",
            f"{base}/{site}/api/listings/{board}/{lid}",
            f"{base}/api/listing/{board}/{lid}",
            f"{base}/api/listings/{board}/{lid}",
            f"{base}/api/{board}/{lid}",
        ]
        # oEmbed-ish
        candidates += [
            f"{base}/{site}/oembed?url=/{site}/{board}/{lid}",
        ]
    # Always include original URL (in case of different query strings)
    candidates.append(final_url)

    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        t = _try_get(c)
        if t:
            texts.append(t)
    return texts

# ----------------------------- Zillow helpers -----------------------------
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
    if not url or "/homedetails/" in url or "zillow.com" not in url:
        return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok:
            return url
        html = r.text
        # anchors / canonical / json hints
        for pat in [
            r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']',
            r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']',
            r'"canonicalUrl"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
            r'"url"\s*:\s*"(https://www\.zillow\.com/homedetails/[^"]+)"',
        ]:
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return url

def _compose_addr_line(street: str, city: str, state: str, zipc: str) -> str:
    parts = []
    if street:
        parts.append(street)
    loc = " ".join([p for p in [city, state] if p]).strip()
    if loc:
        parts.append(loc)
    if zipc and parts:
        parts[-1] = f"{parts[-1]} {zipc}"
    line = re.sub(r"[#&]", " ", ", ".join([p for p in parts if p]).strip())
    return re.sub(r"\s+", " ", line).strip()

def build_zillow_search_deeplink(street: str, city: str, state: str, zipc: str) -> str:
    term = _compose_addr_line(street or "", city or "", state or "", zipc or "")
    if not term:
        return "https://www.zillow.com/homes/"  # last resort
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
    # Otherwise always return an address search deeplink
    return build_zillow_search_deeplink(street, city, state, zipc)

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    upgraded = upgrade_to_homedetails_if_needed(base)
    canon, _ = canonicalize_zillow(upgraded)
    return canon or upgraded or base

# ----------------------------- Source URL resolver -----------------------------
def resolve_from_source_url(source_url: str, defaults: Dict[str, str]) -> Tuple[str, str]:
    """
    Main resolver for arbitrary links (esp. Homespotter):
      1) Fetch initial+final HTML
      2) If HS, parse address from initial, final, and nearby HS endpoints (embed/share/api)
      3) Build Zillow address-search deeplink (or upgraded homedetails if already a Zillow link)
    Returns (zillow_url, display_address)
    """
    pref_state = (defaults.get("state") or "").strip().upper()
    final_url, init_html, final_html, _ = fetch_initial_and_final(source_url)

    # 1) Try initial HTML first (helps for l.hms.pt)
    addr = extract_address_from_html(init_html)

    # 2) Then final page
    if not addr.get("street"):
        a2 = extract_address_from_html(final_html)
        for k in ("street", "city", "state", "zip"):
            if not addr.get(k) and a2.get(k):
                addr[k] = a2[k]

    # 3) If Homespotter, probe variants & scan JSON as well
    if _is_homespotter_like(source_url) or _is_homespotter_like(final_url):
        payloads = _probe_homespotter_variants(final_url or source_url)
        for t in payloads:
            if addr.get("street") and (addr.get("city") or addr.get("state")):
                break
            # HTML path
            a_html = extract_address_from_html(t)
            for k in ("street", "city", "state", "zip"):
                if not addr.get(k) and a_html.get(k):
                    addr[k] = a_html[k]
            # JSON path (any)
            a_json = extract_address_from_json_any(t)
            for k in ("street", "city", "state", "zip"):
                if not addr.get(k) and a_json.get(k):
                    addr[k] = a_json[k]

    street = addr.get("street", "") or ""
    city   = addr.get("city", "") or ""
    state  = addr.get("state", "") or pref_state
    zipc   = addr.get("zip", "") or ""

    # If final is a Zillow /homes/ URL, try to upgrade to /homedetails/
    candidate = final_url
    if "zillow.com" in candidate and "/homes/" in candidate and "/homedetails/" not in candidate:
        candidate = upgrade_to_homedetails_if_needed(candidate)

    # Produce address-based Zillow link (or upgraded homedetails)
    final = ensure_address_based_zillow_link(candidate, street, city, state, zipc)

    # Never return a blank /homes/ if we started with a specific link — keep original as last resort
    if final.strip().rstrip("/") == "https://www.zillow.com/homes":
        final = source_url

    display_addr = _compose_addr_line(street, city, state, zipc)
    return final, display_addr

# ----------------------------- Paste parsers -----------------------------
URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}

def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
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
    # One per line
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

# ----------------------------- Output / UI helpers -----------------------------
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

# ----------------------------- Main renderer -----------------------------
def render_run_tab(state: dict):
    st.header("Run")

    st.subheader("Paste rows")
    st.caption("Paste **Homespotter/MLS/Zillow** links or addresses, one per line. I’ll return a **Zillow link**. If homedetails can’t be found, you’ll get a **Zillow address-search** link (never a blank /homes/).")
    paste = st.text_area(
        "Input",
        height=170,
        placeholder="e.g. https://l.hms.pt/403/340/10127718/74461375/1091612/GI\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718\n407 E Woodall St, Smithfield, NC 27577",
        key="__paste__"
    )

    default_state = st.text_input("Default 2-letter state (used if none is found)", value="NC")

    run_btn = st.button("🚀 Run", type="primary", use_container_width=True)
    results: List[Dict[str, Any]] = []

    if run_btn:
        rows_in: List[Dict[str, Any]] = _rows_from_paste(paste)
        if not rows_in:
            st.warning("Nothing to process.")
            return

        defaults = {"state": (default_state or "").strip().upper(), "city": "", "zip": ""}
        total = len(rows_in)
        prog = st.progress(0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            src_url = _detect_source_url(row)
            if src_url:
                zurl, inferred_addr = resolve_from_source_url(src_url, defaults)
                # guard: never return a bare /homes/ root
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
                # Pure address line → address-search deeplink
                addr = (row.get("address") or "").strip()
                if addr:
                    # naive split; we don’t rely on usaddress here
                    street = addr
                    zsearch = build_zillow_search_deeplink(street, "", defaults["state"], "")
                    results.append({
                        "input_address": street,
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

    # ----------------------------- Fix properties -----------------------------
    st.subheader("Fix properties")
    st.caption("Paste listing links (Homespotter, MLS, Zillow /homes/*_rb/). I’ll output clean **Zillow** links. If /homedetails/ can’t be found, you’ll get a **Zillow address-search** link, not a blank homepage.")
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
                    # Extract address from either initial/final OR HS variants
                    addr = extract_address_from_html(init_html)
                    if not addr.get("street"):
                        a2 = extract_address_from_html(final_html)
                        for k in ("street","city","state","zip"):
                            if not addr.get(k) and a2.get(k): addr[k] = a2[k]

                    if _is_homespotter_like(u) or _is_homespotter_like(final_url):
                        for t in _probe_homespotter_variants(final_url or u):
                            if addr.get("street") and (addr.get("city") or addr.get("state")):
                                break
                            a_html = extract_address_from_html(t)
                            for k in ("street","city","state","zip"):
                                if not addr.get(k) and a_html.get(k): addr[k] = a_html[k]
                            a_json = extract_address_from_json_any(t)
                            for k in ("street","city","state","zip"):
                                if not addr.get(k) and a_json.get(k): addr[k] = a_json[k]

                    street = addr.get("street","")
                    city   = addr.get("city","")
                    stt    = addr.get("state","") or defaults["state"]
                    zipc   = addr.get("zip","")

                    final_url = ensure_address_based_zillow_link(final_url, street, city, stt, zipc)

                best = final_url or best
            except Exception:
                pass

            if "/homedetails/" in (best or ""):
                best, _ = canonicalize_zillow(best)

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
