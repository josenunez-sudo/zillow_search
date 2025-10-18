# ui/run_tab.py
# Minimal, mobile-friendly Run tab:
# - Paste addresses OR links (incl. Homespotter short links)
# - If Homespotter, call HS_ADDRESS_RESOLVER_URL /resolve?u=...
# - Build a Zillow /homes/<slug>_rb/ deeplink from the address
# - Try to upgrade rb → /homedetails/ if the page shows a canonical homedetails link
# - "Fix links" section to normalize any pasted Zillow links

import os, re, io, csv, json
from typing import List, Dict, Any, Optional, Tuple
import requests
import streamlit as st
import streamlit.components.v1 as components
from urllib.parse import quote_plus, urlparse, unquote

# ---------- Config / Secrets ----------
def _get_secret(key: str, default: str = "") -> str:
    try:
        if key in st.secrets and st.secrets[key]:
            return str(st.secrets[key]).strip()
    except Exception:
        pass
    return str(os.getenv(key, default)).strip()

HS_RESOLVER = _get_secret("HS_ADDRESS_RESOLVER_URL", "")
HS_RESOLVER = HS_RESOLVER.rstrip("/") if HS_RESOLVER else ""

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

# ---------- Address extractors ----------
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
            addr = blk.get("address") or blk.get("itemOffered", {}).get("address") if isinstance(blk.get("itemOffered"), dict) else blk.get("address")
            if isinstance(addr, dict):
                out["street"] = out["street"] or (addr.get("streetAddress") or "").strip()
                out["city"]   = out["city"]   or (addr.get("addressLocality") or "").strip()
                # Sometimes addressRegion contains "NC", or addressCountry mistakenly used
                st_or_cty = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()
                if st_or_cty and not out["state"]:
                    out["state"] = st_or_cty[:2].upper()
                out["zip"]    = out["zip"]    or (addr.get("postalCode") or "").strip()
                # If we already have a decent address, stop early
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

    # Fallback from <title> / og:title when it clearly looks like an address
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
        # Look for a direct homedetails link or canonical tag
        m = re.search(r'href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
        m = re.search(r'rel=["\']canonical["\'][^>]+href=["\'](https://www\.zillow\.com/homedetails/[^"\']+)["\']', html, re.I)
        if m:
            return m.group(1)
    except Exception:
        pass
    return url

# ---------- Homespotter microservice ----------
def resolve_hs_address_via_service(hs_url: str) -> Optional[Dict[str, Any]]:
    """
    Call your Cloudflare Worker (or any microservice) that returns:
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

# ---------- Core: resolve any input into a Zillow URL ----------
def resolve_from_source_url(source_url: str, state_default: str = "NC") -> Tuple[str, str]:
    """
    Returns (zillow_url, inferred_address_text)
    - If Homespotter link and your resolver is configured, use it first.
    - Else expand and parse HTML for address; build a Zillow /homes/<slug>_rb/ deeplink.
    - Try to upgrade to /homedetails/ if canonical exists.
    """
    if not source_url:
        return "", ""

    # 0) Homespotter → call microservice first
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

    # 1) Expand the URL and fetch the page
    final_url, html, _ = expand_url_and_fetch_html(source_url)

    # 2) Parse address off the page
    addr = extract_address_from_html(html)
    street = (addr.get("street") or "").strip()
    city   = (addr.get("city") or "").strip()
    state  = (addr.get("state") or "").strip() or state_default
    zipc   = (addr.get("zip") or "").strip()

    # 3) Build a Zillow deeplink (even if partial address)
    if street or city or state or zipc:
        z = zillow_deeplink_from_addr(street, city, state, zipc)
        z = upgrade_to_homedetails_if_needed(z)
        inferred = " ".join([x for x in [street, city, state, zipc] if x])
        return z, inferred

    # 4) Fallback: just return the expanded URL (no parsing)
    return final_url, ""

# ---------- UI helpers ----------
def _list_to_rows(text: str) -> List[Dict[str, str]]:
    """
    Accept CSV with header (url or address), OR plain text (one URL/address per line).
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV first if it looks like CSV
    lines = text.splitlines()
    if len(lines) >= 2 and ("," in lines[0] or "\t" in lines[0]):
        try:
            dialect = csv.Sniffer().sniff(lines[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            out = []
            for r in reader:
                # field name could be 'url' or 'address' or similar
                row = {k.lower(): (str(v).strip() if v is not None else "") for k, v in r.items()}
                url = row.get("url") or row.get("link") or row.get("source_url") or ""
                addr = row.get("address") or row.get("full_address") or ""
                if url:
                    out.append({"url": url})
                elif addr:
                    out.append({"address": addr})
            if out:
                return out
        except Exception:
            pass

    # Plain lines
    out: List[Dict[str, str]] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if is_probable_url(s):
            out.append({"url": s})
        else:
            out.append({"address": s})
    return out

def _result_item_html(url: str, badge: str = "") -> str:
    u = (url or "").strip()
    if not u:
        return ""
    esc = re.sub(r'"', "&quot;", u)
    return f'<li style="margin:0.25rem 0;"><a href="{esc}" target="_blank" rel="noopener">{esc}</a>{badge}</li>'

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

# ---------- Main render ----------
def render_run_tab(state):  # keep this signature for your app loader
    st.header("Run")

    # Paste area
    st.caption("Paste *addresses* or *listing links* (Homespotter, IDX, MLS, etc.). I’ll output clean Zillow links.")
    paste = st.text_area("Input", height=180, placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718")

    # Run
    if st.button("🚀 Resolve", type="primary", use_container_width=True):
        rows = _list_to_rows(paste)
        if not rows:
            st.warning("Nothing to process.")
            return

        urls_out: List[str] = []
        for r in rows:
            if r.get("url"):
                z, _addr = resolve_from_source_url(r["url"], state_default="NC")
                urls_out.append(z or r["url"])
            else:
                # Address line → Zillow deeplink
                addr = (r.get("address") or "").strip()
                if addr:
                    # very light guess: try to split pieces; you can hook usaddress here if you want
                    # but we’ll just produce a slug from the full line
                    z = "https://www.zillow.com/homes/" + zillow_slugify(addr) + "_rb/"
                    z = upgrade_to_homedetails_if_needed(z)
                    urls_out.append(z)

        st.subheader("Results")
        results_list_with_copy_all(urls_out)

    st.divider()

    # ---------- Fix links section ----------
    st.subheader("Fix properties")
    st.caption("Paste Zillow links (/homes/*_rb/ or anything). I’ll output clean canonical **/homedetails/** URLs.")
    fix_text = st.text_area("Links to fix", height=120, key="fix_area")

    if st.button("🔧 Fix / Re-run links"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[str] = []
        prog = st.progress(0, text="Fixing…")
        total = max(1, len(lines))
        for i, u in enumerate(lines, start=1):
            best = u
            try:
                # If it's a non-Zillow source, try to resolve to Zillow first
                if "zillow.com" not in (best or ""):
                    z, _addr = resolve_from_source_url(best, state_default="NC")
                    best = z or best
                # Then upgrade to homedetails if possible
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

# Allow running this module directly for quick local checks
if __name__ == "__main__":
    render_run_tab({})
