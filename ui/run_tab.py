# ui/run_tab.py
# Minimal, mobile-friendly "paste → Zillow deeplinks" with rock-solid Homespotter handling.
# It ALWAYS calls HS_ADDRESS_RESOLVER_URL for Homespotter/IDX links to extract a real address,
# then builds a clean Zillow /homes/{address}_rb/ deeplink. If no address is found, it leaves
# the original link and marks it UNRESOLVED (no more wrong Zillow URLs).

import os, re, io, csv, json, time
from typing import Dict, Any, List, Optional, Tuple
from html import escape
from urllib.parse import urlparse, quote_plus, unquote

import requests
import streamlit as st
import streamlit.components.v1 as components

# ----------------------------- Config / Secrets -----------------------------

# Let secrets populate env for local + cloud
for k in [
    "HS_ADDRESS_RESOLVER_URL",  # your microservice
    "BING_API_KEY",             # optional: not required here
]:
    try:
        if k in st.secrets and st.secrets[k]:
            os.environ[k] = st.secrets[k]
    except Exception:
        pass

HS_SERVICE = os.getenv("HS_ADDRESS_RESOLVER_URL", "").strip()
REQUEST_TIMEOUT = 12

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

# ----------------------------- Tiny helpers ---------------------------------

def is_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

def is_homespotter_like(u: str) -> bool:
    try:
        h = (urlparse(u).hostname or "").lower()
        return ("homespotter" in h) or ("hms.pt" in h) or ("idx." in h and "homespotter" in u.lower())
    except Exception:
        return False

def clean_str(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def slugify_for_zillow_homes(street: str, city: str, state: str, zipc: str) -> str:
    """
    Build a Zillow /homes/{slug}_rb/ search URL from address parts.
    Requires at least (street + city + state) OR (city + state + zip).
    """
    street = clean_str(street); city = clean_str(city); state = clean_str(state); zipc = clean_str(zipc)
    if not ((street and city and state) or (city and state and zipc)):
        return ""  # not enough to make a trustworthy slug

    parts = []
    if street: parts.append(street)
    loc = ", ".join([p for p in [city, state] if p])
    if loc: parts.append(loc)
    if zipc:
        if parts:
            parts[-1] = f"{parts[-1]} {zipc}"
        else:
            parts.append(zipc)

    slug = ", ".join(parts)
    a = slug.lower()
    a = re.sub(r"[^\w\s,-]", "", a).replace(",", "")
    a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

# ----------------------- Microservice-first resolver ------------------------

def _call_hs_service(url: str) -> Optional[Dict[str,str]]:
    """
    POST { url } to your HS microservice.
    Expected JSON: { "street": "...", "city": "...", "state": "NC", "zip": "...." }
    Returns dict or None.
    """
    if not (HS_SERVICE and url):
        return None
    try:
        r = requests.post(
            HS_SERVICE,
            headers={"Content-Type": "application/json"},
            json={"url": url},
            timeout=REQUEST_TIMEOUT,
        )
        if not r.ok:
            return None
        data = r.json() if r.headers.get("content-type","").startswith("application/json") else None
        if not isinstance(data, dict):
            return None
        # Accept common keys, case-insensitive
        def pick(d, *keys):
            for k in keys:
                v = d.get(k) or d.get(k.lower()) or d.get(k.upper())
                if v: return str(v)
            return ""
        addr = {
            "street": pick(data, "street", "streetAddress", "address"),
            "city":   pick(data, "city", "locality", "addressLocality"),
            "state":  pick(data, "state", "region", "addressRegion"),
            "zip":    pick(data, "zip", "postalCode"),
        }
        if any(addr.values()):
            return addr
    except Exception:
        return None
    return None

def _extract_address_from_html(html: str) -> Dict[str,str]:
    """
    Looser HTML extractor (JSON-LD + micro patterns). Avoid marketing titles like “4 beds 2 baths…”.
    """
    out = {"street":"", "city":"", "state":"", "zip":""}
    if not html:
        return out

    # JSON-LD blocks
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I|re.S):
            try:
                data = json.loads(m.group(1))
            except Exception:
                continue
            blocks = data if isinstance(data, list) else [data]
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                addr = b.get("address") or b.get("itemOffered", {}).get("address") if isinstance(b.get("itemOffered"), dict) else None
                if isinstance(addr, dict):
                    out["street"] = out["street"] or clean_str(addr.get("streetAddress",""))
                    out["city"]   = out["city"]   or clean_str(addr.get("addressLocality",""))
                    # state might be "US-NC" etc; keep 2 letters if so
                    st_raw = addr.get("addressRegion") or addr.get("addressCountry") or ""
                    st_raw = clean_str(st_raw)
                    if st_raw and not out["state"]:
                        m2 = re.search(r"\b([A-Za-z]{2})\b", st_raw)
                        out["state"] = m2.group(1) if m2 else st_raw[:2]
                    out["zip"]    = out["zip"]    or clean_str(addr.get("postalCode",""))
    except Exception:
        pass

    # Meta fallbacks (but ignore marketing-y titles)
    if not out["street"]:
        for pat in [
            r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
            r"<title>\s*([^<]+?)\s*</title>",
        ]:
            m = re.search(pat, html, re.I)
            if m:
                title = clean_str(m.group(1))
                # Reject marketing strings like “4 beds 2 baths ... homespotter”
                if re.search(r"\b(beds?|baths?|homespotter|for sale|mls)\b", title, re.I):
                    continue
                # Only accept if it *looks* like an address: has state abbrevi., maybe ZIP
                if re.search(r"\b[A-Za-z]{2}\b", title) and re.search(r"\d{5}", title):
                    out["street"] = title
                    break

    # Meta itemprops (street/city/state/zip)
    patterns = [
        (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street"),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city"),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', "state"),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip"),
        (r'"streetAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city"),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'"postalCode"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
    ]
    for pat, key in patterns:
        if out[key]: continue
        m = re.search(pat, html, re.I)
        if m: out[key] = clean_str(m.group(1))

    return out

def _fetch(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

# ------------------------ Core: resolve any single item ----------------------

def resolve_single_item_to_zillow(item: str) -> Tuple[str, str]:
    """
    Returns (best_url, note)
    - If it's a Homespotter/IDX link:
        -> call HS service. If address -> clean Zillow /homes/{address}_rb/
        -> else try page HTML (rarely helps; gated)
        -> else UNRESOLVED (return original link)
    - If it's any other URL:
        -> return canonical (strip query/fragment)
    - If it's a plain address:
        -> build Zillow /homes/{address}_rb/
    """
    s = (item or "").strip()
    if not s:
        return "", "empty"

    # 1) URL path
    if is_url(s):
        # Always try microservice first for Homespotter/IDX style links
        if is_homespotter_like(s):
            addr = _call_hs_service(s)
            if addr and (addr.get("street") or (addr.get("city") and addr.get("state"))):
                z = slugify_for_zillow_homes(addr.get("street",""), addr.get("city",""), addr.get("state",""), addr.get("zip",""))
                if z: return z, "hs:service"
            # Gated or service returned nothing -> last-chance: try HTML (often blocked)
            final_url, html, _ = _fetch(s)
            addr2 = _extract_address_from_html(html)
            if addr2 and (addr2.get("street") or (addr2.get("city") and addr2.get("state"))):
                z = slugify_for_zillow_homes(addr2.get("street",""), addr2.get("city",""), addr2.get("state",""), addr2.get("zip",""))
                if z: return z, "hs:html"
            return s, "UNRESOLVED (hs gated or service empty)"

        # Zillow? strip query/fragment; keep as-is
        if "zillow.com" in s.lower():
            base = re.sub(r"[?#].*$", "", s)
            return base, "zillow"

        # Other site: try to read an address from the page; if found, build a /homes/ deeplink
        final_url, html, _ = _fetch(s)
        addr = _extract_address_from_html(html)
        if addr and (addr.get("street") or (addr.get("city") and addr.get("state"))):
            z = slugify_for_zillow_homes(addr.get("street",""), addr.get("city",""), addr.get("state",""), addr.get("zip",""))
            if z: return z, "addr:html"
        # No good address: pass original through (don’t invent)
        return s, "pass-through"

    # 2) Plain address line
    # Normalize trivial commas/spacing, then build a clean /homes/ deeplink
    addr_text = clean_str(s)
    # Rudimentary split; we just need a solid homes slug
    # The homes slug works best with "street, City, ST ZIP"
    slug_in = addr_text
    # If it looks like just city/state, still okay
    z = slugify_for_zillow_homes(addr_text, "", "", "")
    if not z:
        # Try to salvage: “Street, City, ST ZIP” pattern from the string
        m = re.search(r"^(.*?),\s*([A-Za-z .'-]+),\s*([A-Za-z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$", addr_text)
        if m:
            z = slugify_for_zillow_homes(m.group(1), m.group(2), m.group(3), m.group(4) or "")
    if z:
        return z, "addr:text"
    return addr_text, "addr:insufficient"

# ----------------------------- UI Components --------------------------------

def _render_results(items: List[Tuple[str, str, str]]):
    """
    items: [(input, output_url, note), ...]
    Show as a small list with a Copy button, tuned for mobile.
    """
    li = []
    for _inp, out_url, note in items:
        badge = "" if not note else f"<span style='font-size:11px;color:#64748b;margin-left:8px'>{escape(note)}</span>"
        li.append(f"<li style='margin:6px 0'><a href='{escape(out_url)}' target='_blank' rel='noopener'>{escape(out_url)}</a>{badge}</li>")
    html = "<ul style='margin:0 0 0 1.1rem; padding:0'>" + "\n".join(li or ["<li>(no results)</li>"]) + "</ul>"

    copy_text = "\n".join([o for _, o, _ in items if o]) + ("\n" if items else "")
    components.html(
        f"""
        <div style="position:relative;padding-right:84px">
          <button id="__copy" style="position:absolute;right:0;top:0;border:0;border-radius:10px;background:#1d4ed8;color:#fff;font-weight:800;padding:6px 10px">Copy</button>
          {html}
        </div>
        <script>
          (function(){{
            const btn=document.getElementById("__copy");
            const text={json.dumps(copy_text)};
            btn.addEventListener("click", async()=>{{
              try{{ await navigator.clipboard.writeText(text); const p=btn.textContent; btn.textContent="✓"; setTimeout(()=>btn.textContent=p, 900); }}
              catch(e){{ const p=btn.textContent; btn.textContent="×"; setTimeout(()=>btn.textContent=p, 900); }}
            }});
          }})();
        </script>
        """,
        height=min(500, 38 * max(1, len(li)) + 16),
        scrolling=False
    )

# ------------------------------ Main entry ----------------------------------

def render_run_tab(state: dict):
    st.header("Paste → Zillow")
    st.caption("Works great on mobile. Homespotter links are converted using your private resolver first.")

    # Health check row (tiny)
    ok_hs = bool(HS_SERVICE)
    st.write(f"Resolver: {'✅' if ok_hs else '⚠️ missing HS_ADDRESS_RESOLVER_URL'}")

    st.subheader("Paste addresses or links")
    text = st.text_area("One per line", height=140, label_visibility="collapsed",
                        placeholder="e.g.\nhttps://idx.homespotter.com/hs_triangle/tmlspar/10127718\n407 E Woodall St, Smithfield, NC 27577")

    colA, colB = st.columns(2)
    with colA:
        run = st.button("🚀 Resolve", use_container_width=True)
    with colB:
        clear = st.button("Clear", use_container_width=True)
    if clear:
        st.session_state.pop("__last__", None)
        st.experimental_rerun()

    results: List[Tuple[str, str, str]] = []
    if run:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        prog = st.progress(0.0, text="Resolving…")
        for i, line in enumerate(lines, start=1):
            try:
                out, note = resolve_single_item_to_zillow(line)
            except Exception as e:
                out, note = line, f"error: {e.__class__.__name__}"
            results.append((line, out, note))
            prog.progress(i/len(lines), text=f"Resolved {i}/{len(lines)}")
            time.sleep(0.02)
        prog.progress(1.0, text="Done")
        st.session_state["__last__"] = results

    # Show last results if not just ran
    results = st.session_state.get("__last__", results)
    if results:
        st.subheader("Results")
        _render_results(results)

    st.divider()
    st.subheader("Fix links")
    st.caption("Paste raw Zillow/Homespotter links; I’ll output clean Zillow links only when I have a **real address**.")
    fix_text = st.text_area("Links to fix", height=120, key="__fix__", label_visibility="collapsed")
    if st.button("🔧 Clean / Convert", use_container_width=True):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed: List[Tuple[str,str,str]] = []
        for l in lines:
            try:
                out, note = resolve_single_item_to_zillow(l)
            except Exception as e:
                out, note = l, f"error: {e.__class__.__name__}"
            fixed.append((l, out, note))
        st.markdown("**Fixed links**")
        _render_results(fixed)

# Allow running this module directly for local testing:
if __name__ == "__main__":
    render_run_tab({})
