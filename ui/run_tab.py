# ui/run_tab.py
# Homespotter → Address (no Bing), then compose clean Zillow /homes/*_rb/ deeplinks.
# Also supports CSV upload, "Fix links" section, and thumbnail previews.

import os, csv, io, re, time, json, asyncio
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from html import escape
from urllib.parse import urlparse, urlunparse, quote_plus

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ==========================
# ---- Page + Styles -------
# ==========================
st.set_page_config(page_title="Address Alchemist (no-Bing resolver)", layout="centered")

st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { padding:10px 12px; background:transparent; border-radius:12px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.new { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.dup { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
.run-zone .stButton>button { background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important; color:#fff !important; font-weight:800 !important; border:0 !important; border-radius:12px !important; box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important; }
</style>
""", unsafe_allow_html=True)

# ==========================
# ---- Config / Secrets ----
# ==========================
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
BITLY_TOKEN         = os.getenv("BITLY_TOKEN", "")

# ==========================
# ---- HTTP helpers --------
# ==========================
REQUEST_TIMEOUT = 15

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOCIAL_UAS = [
    # These often trigger server-side share markup with address/title/JSON-LD
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
    """
    Free plain-text readability proxy. Great for stubborn JS sites.
    Example: https://r.jina.ai/http://idx.homespotter.com/hs_triangle/tmlspar/10127718
    """
    try:
        u = urlparse(url)
        # Build: https://r.jina.ai/http://HOST/PATH?QUERY
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
        addr = blk.get("address") or blk.get("itemOffered", {}).get("address") if isinstance(blk, dict) else None
        if isinstance(addr, dict):
            street = (addr.get("streetAddress") or "").strip()
            city   = (addr.get("addressLocality") or "").strip()
            state  = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()[:2]
            zipc   = (addr.get("postalCode") or "").strip()
            if street or (city and state):
                return {"street": street, "city": city, "state": state, "zip": zipc}
    return {"street":"", "city":"", "state":"", "zip":""}

def _extract_address_from_meta(html: str) -> Dict[str,str]:
    # Try og:title, twitter:title, description
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
        # direct "123 Main St, City, NC 27577"?
        s1 = RE_STREET_CITY_ST_ZIP.search(text)
        if s1:
            return {"street": s1.group(1), "city": s1.group(2), "state": s1.group(3).upper(), "zip": s1.group(4)}
        # maybe "City, NC 27577" only
        s2 = RE_CITY_ST_ZIP.search(text)
        if s2:
            return {"street": "", "city": s2.group(1), "state": s2.group(2).upper(), "zip": s2.group(3)}
    return {"street":"", "city":"", "state":"", "zip":""}

def _extract_address_from_text(txt: str) -> Dict[str,str]:
    # Scan the plain text body (Jina readability) for the first addressy thing
    if not txt:
        return {"street":"", "city":"", "state":"", "zip":""}
    s1 = RE_STREET_CITY_ST_ZIP.search(txt)
    if s1:
        return {"street": s1.group(1).strip(), "city": s1.group(2).strip(), "state": s1.group(3).upper(), "zip": s1.group(4).strip()}
    s2 = RE_CITY_ST_ZIP.search(txt)
    if s2:
        return {"street":"", "city": s2.group(1).strip(), "state": s2.group(2).upper(), "zip": s2.group(3).strip()}
    return {"street":"", "city":"", "state":"", "zip":""}

def best_effort_address_from_hs(url: str) -> Dict[str,str]:
    """
    Try very hard, without Bing, to get (street, city, state, zip) from a Homespotter (or l.hms.pt) link.
    Order:
      1) default UA
      2) social UAs (FB/Twitter/Slack/Googlebot)
      3) AMP variants (?amp=1, /amp)
      4) Jina readability proxy (plaintext)
    """
    # 1) default UA
    final, html, code, _ = _get(url, ua=DEFAULT_UA, allow_redirects=True)
    if code == 200 and html:
        a = _extract_address_from_jsonld(html)
        if a.get("street") or (a.get("city") and a.get("state")):
            return a
        b = _extract_address_from_meta(html)
        if b.get("street") or (b.get("city") and b.get("state")):
            return b

    # 2) social UAs
    for ua in SOCIAL_UAS:
        _, html2, code2, _ = _get(final or url, ua=ua, allow_redirects=True)
        if code2 == 200 and html2:
            a2 = _extract_address_from_jsonld(html2)
            if a2.get("street") or (a2.get("city") and a2.get("state")):
                return a2
            b2 = _extract_address_from_meta(html2)
            if b2.get("street") or (b2.get("city") and b2.get("state")):
                return b2

    # 3) AMP variants
    # try ?amp=1
    amp_q = (final or url)
    amp_join = amp_q + ("&amp=1" if ("?" in amp_q) else "?amp=1")
    _, html3, code3, _ = _get(amp_join, ua=DEFAULT_UA, allow_redirects=True)
    if code3 == 200 and html3:
        a3 = _extract_address_from_jsonld(html3)
        if a3.get("street") or (a3.get("city") and a3.get("state")):
            return a3
        b3 = _extract_address_from_meta(html3)
        if b3.get("street") or (b3.get("city") and b3.get("state")):
            return b3
    # try /amp
    try:
        u = urlparse(final or url)
        amp_path = (u.path.rstrip("/") + "/amp")
        amp_url = urlunparse((u.scheme, u.netloc, amp_path, "", "", ""))
        _, html4, code4, _ = _get(amp_url, ua=DEFAULT_UA, allow_redirects=True)
        if code4 == 200 and html4:
            a4 = _extract_address_from_jsonld(html4)
            if a4.get("street") or (a4.get("city") and a4.get("state")):
                return a4
            b4 = _extract_address_from_meta(html4)
            if b4.get("street") or (b4.get("city") and b4.get("state")):
                return b4
    except Exception:
        pass

    # 4) Jina readability proxy
    text = _jina_readable(final or url)
    if text:
        a5 = _extract_address_from_text(text)
        if a5.get("street") or (a5.get("city") and a5.get("state")):
            return a5

    return {"street":"", "city":"", "state":"", "zip":""}

# ==========================
# ---- Zillow builders -----
# ==========================
def _slugify(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s,-]", "", s)
    s = s.replace(",", "")
    return re.sub(r"\s+", "-", s.strip())

def zillow_rb_from_address(street: str, city: str, state: str, zipc: str) -> Optional[str]:
    # Need at least a street + city + state to make a meaningful deeplink
    city = (city or "").strip()
    state = (state or "").strip()
    street = (street or "").strip()
    zipc = (zipc or "").strip()

    if not (city and state):
        # last resort: fail safe
        return None

    # Build slug like: "123 Main St, City NC 27577" → /homes/123-main-st-city-nc-27577_rb/
    bits = [street] if street else []
    loc = city + " " + state
    if loc.strip():
        bits.append(loc.strip())
    if zipc:
        bits[-1] = (bits[-1] + " " + zipc) if bits else zipc
    base = ", ".join(bits)
    slug = _slugify(base)
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ==========================
# ---- Thumbnails ----------
# ==========================
def _streetview_or_none(query_addr: str) -> Optional[str]:
    if not GOOGLE_MAPS_API_KEY or not query_addr:
        return None
    loc = quote_plus(query_addr)
    return f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={GOOGLE_MAPS_API_KEY}"

# ==========================
# ---- Core Resolve --------
# ==========================
def resolve_any_link_to_zillow_rb(source_url: str) -> Tuple[str, str]:
    """
    Return (zillow_deeplink, human_readable_address) or (original_url, "") if we couldn't form a deeplink.
    No Bing/Azure dependency.
    """
    if not source_url:
        return "", ""

    # Expand once (in case of l.hms.pt shortlink)
    final, html, code, _ = _get(source_url, ua=DEFAULT_UA, allow_redirects=True)
    target = final or source_url

    # If this is already a Zillow homedetails or homes deeplink, pass through
    if "zillow.com" in (target or ""):
        if "/homedetails/" in target or "/homes/" in target:
            return re.sub(r"[?#].*$", "", target), ""

    # If Homespotter-ish, extract address via robust path
    host = (urlparse(target).hostname or "").lower()
    if any(k in host for k in ["homespotter", "hms.pt", "idx."]):
        addr = best_effort_address_from_hs(target)
    else:
        # generic page – try JSON-LD/meta first, then Jina
        addr = _extract_address_from_jsonld(html or "") if (code == 200 and html) else {"street":"","city":"","state":"","zip":""}
        if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
            if code == 200 and html:
                tmp = _extract_address_from_meta(html)
                if tmp.get("street") or (tmp.get("city") and tmp.get("state")):
                    addr = tmp
        if not (addr.get("street") or (addr.get("city") and addr.get("state"))):
            text = _jina_readable(target)
            addr = _extract_address_from_text(text)

    street = addr.get("street","")
    city   = addr.get("city","")
    state  = addr.get("state","")
    zipc   = addr.get("zip","")

    deeplink = zillow_rb_from_address(street, city, state, zipc)
    # If absolutely nothing – return original URL, address blank
    if not deeplink:
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
    # Try CSV (header present)
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
    # Fallback: 1 item per line
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

def _thumbnail_for(deeplink: str, fallback_addr: str) -> Optional[str]:
    # We don't scrape Zillow images here; just Street View if you have a key
    return _streetview_or_none(fallback_addr)

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

# ==========================
# ---- Main UI -------------
# ==========================
def render_run_tab(state: dict):
    st.header("Address Alchemist — no-Bing Homespotter resolver")

    # Paste OR CSV
    st.subheader("Input")
    st.caption("Paste Homespotter links (or any listing links) and/or upload a CSV with a column named `url`.")
    colA, colB = st.columns([1.4, 1])
    with colA:
        paste = st.text_area("Paste links or addresses", height=140, label_visibility="collapsed")
    with colB:
        file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="visible")

    use_streetview = st.checkbox("Try Street View thumbs (needs GOOGLE_MAPS_API_KEY)", value=False)

    # Parse pasted
    rows_in: List[Dict[str, Any]] = []
    for r in _rows_from_paste(paste):
        rows_in.append(r)

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
                    thumb = _thumbnail_for(z or "", addr)
                    if thumb:
                        out["image_url"] = thumb
                results.append(out)
            else:
                # Treat as address string → direct rb deeplink
                parts = re.split(r"\s*,\s*", u)
                city = state = zipc = ""
                street = u
                if len(parts) >= 2:
                    street = parts[0]
                    tail = ", ".join(parts[1:])
                    # crude parse city, state, zip from tail
                    m = RE_CITY_ST_ZIP.search(tail)
                    if m:
                        city, state, zipc = m.group(1), m.group(2), m.group(3)
                z = zillow_rb_from_address(street, city, state, zipc) or ""
                results.append({"original": u, "zillow_url": z or u, "display_address": u})

            prog.progress(i/len(rows_in), text=f"Resolved {i}/{len(rows_in)}")
            time.sleep(0.05)

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
                    st.image(img, use_container_width=True, caption=addr or "")
                    st.markdown(f'<a href="{escape(link)}" target="_blank" rel="noopener">{escape(link)}</a>', unsafe_allow_html=True)

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
    st.caption("Paste any Homespotter or other listing links; I’ll try to pull the address and build a Zillow **/_rb/** deeplink.")
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
            shown.append(f"- [{escape(addr or best)}]({escape(best)})")
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")
        prog.progress(1.0, text="Done")

        st.markdown("**Fixed links**")
        st.markdown("\n".join(shown), unsafe_allow_html=True)
        st.text_area("Copy clean list", value="\n".join(fixed) + "\n", height=140, label_visibility="collapsed")


# Allow `streamlit run ui/run_tab.py`
if __name__ == "__main__":
    render_run_tab(st.session_state)
