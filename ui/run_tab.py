# ui/run_tab.py
# Always output ONLY Zillow /homes/<slug>_rb/ links.
# - If you paste URLs (Homespotter, HMS, etc.), we try to extract an address from the HTML
#   and build a Zillow search link.
# - If you paste plain addresses, we slugify them directly into Zillow /homes/..._rb/.
# - NO Google links leave this file.

import os, csv, io, re, json, time, html
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------------- Page ----------------
st.set_page_config(page_title="Address Alchemist — Zillow-only Resolver", layout="centered")
st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { padding:12px; border-radius:12px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.run-zone .stButton>button {
  background: linear-gradient(180deg,#2563eb 0%,#1d4ed8 100%)!important;
  color:#fff!important;font-weight:800!important;border:0!important;
  border-radius:12px!important;
  box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important;
}
.img-label { font-size:13px; margin-top:6px; }
.small { color:#64748b; font-size:12px; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px;
         border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.ok { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.warn { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
</style>
""", unsafe_allow_html=True)

# ---------------- Config ----------------
REQUEST_TIMEOUT = 12
DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOCIAL_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

# ---------------- HTTP helpers (only for parsing, never for output URLs) ----------------
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

# ---------------- Address patterns & extractors ----------------
RE_STREET_CITY_ST_ZIP = re.compile(
    r'(\d{1,6}\s+[A-Za-z0-9\.\'\-\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Cir|Highway|Hwy|Route|Parkway|Pkwy)\b[^\n,]*)\s*,\s*([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
    re.I
)
RE_CITY_ST_ZIP = re.compile(r'([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', re.I)

def _jsonld_blocks(html_txt: str) -> List[Dict[str, Any]]:
    out = []
    if not html_txt: return out
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

def _extract_address_from_jsonld(html_txt: str) -> str:
    """
    Return a single best address string like '123 Main St, City ST 12345'
    using JSON-LD blocks.
    """
    for blk in _jsonld_blocks(html_txt):
        addr = (blk.get("address")
                or blk.get("itemOffered", {}).get("address")
                or blk.get("item", {}).get("address"))
        if isinstance(addr, dict):
            street = (addr.get("streetAddress") or "").strip()
            city   = (addr.get("addressLocality") or "").strip()
            state  = (addr.get("addressRegion") or addr.get("addressCountry") or "").strip()
            zipc   = (addr.get("postalCode") or "").strip()
            parts = [street, f"{city} {state}".strip(), zipc]
            s = ", ".join([p for p in parts if p])
            if s:
                return s
    return ""

def _extract_address_from_meta(html_txt: str) -> str:
    """
    Try og:title, twitter:title, meta description, <title>, og:street-address.
    """
    un = html.unescape(html_txt or "")
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]twitter:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)\s*</title>",
        r"<meta[^>]+property=['\"]og:street-address['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, un, re.I)
        if not m: 
            continue
        text = re.sub(r'\s+', ' ', m.group(1)).strip()
        s1 = RE_STREET_CITY_ST_ZIP.search(text)
        if s1:
            return f"{s1.group(1)}, {s1.group(2)} {s1.group(3)} {s1.group(4)}".strip()
        s2 = RE_CITY_ST_ZIP.search(text)
        if s2:
            return f"{s2.group(1)}, {s2.group(2)} {s2.group(3)}".strip()
        # Sometimes the meta text is just full address
        if re.search(r'^\d+\s', text) and "," in text:
            return text
    return ""

def _extract_address_from_text(txt: str) -> str:
    if not txt:
        return ""
    s1 = RE_STREET_CITY_ST_ZIP.search(txt)
    if s1:
        return f"{s1.group(1)}, {s1.group(2)} {s1.group(3)} {s1.group(4)}".strip()
    # last-resort: first line with a street-number pattern
    for line in txt.splitlines():
        l = line.strip()
        if re.match(r"^\d+\s+[A-Za-z0-9]", l):
            return l
    return ""

def extract_best_address_string(html_txt: str) -> str:
    """
    Try JSON-LD first, then meta tags, then raw text.
    """
    if not html_txt:
        return ""
    for fn in (_extract_address_from_jsonld,
               _extract_address_from_meta,
               _extract_address_from_text):
        s = fn(html_txt)
        if s:
            return s.strip()
    return ""

# ---------------- Zillow slug builder ----------------
def _slugify_for_zillow(s: str) -> str:
    """
    Turn any address-ish string into something Zillow will parse as a search term.
    """
    s = (s or "").lower()
    # strip weird stuff
    s = re.sub(r"[^\w\s,-]", "", s)
    s = s.replace(",", " ")
    return re.sub(r"\s+", "-", s.strip())

def zillow_from_freeform_address(addr_str: str) -> str:
    """
    Core rule: ALWAYS return a Zillow /homes/..._rb/ URL for any non-empty addr_str.
    """
    addr_str = (addr_str or "").strip()
    if not addr_str:
        return ""
    slug = _slugify_for_zillow(addr_str)
    if not slug:
        return ""
    return f"https://www.zillow.com/homes/{slug}_rb/"

# ---------------- Resolver: any link -> Zillow search link ----------------
def _try_variants(url: str) -> List[Tuple[str,str,int]]:
    variants = [url]
    try:
        u = urlparse(url)
        variants.append(url)  # original
        variants.append(url.rstrip("/") + "/")
        variants.append(url.rstrip("/") + "/amp")
        variants.append(url.rstrip("/") + "/share")
        variants.append(f"{u.scheme}://{u.netloc}{u.path}")
    except Exception:
        pass

    out = []
    # main hit
    f1, h1, c1, _ = _get(url, ua=DEFAULT_UA, allow_redirects=True)
    out.append((f1, h1, c1))
    # social bots
    for ua in SOCIAL_UAS:
        f2, h2, c2, _ = _get(url, ua=ua, allow_redirects=True)
        out.append((f2, h2, c2))
    # a couple permutations
    for v in variants[1:]:
        f3, h3, c3, _ = _get(v, ua=DEFAULT_UA, allow_redirects=True)
        out.append((f3, h3, c3))
    return out

def resolve_any_link_to_zillow_rb(source_url: str) -> Tuple[str, str, str]:
    """
    Returns: (zillow_url_or_empty, display_address, note)
    NOTE: This will NEVER return a non-zillow URL.
    """
    if not source_url:
        return "", "", "empty"

    # If it's already Zillow, trust it (and try to derive display address from the page).
    if "zillow.com" in source_url:
        clean = re.sub(r"[?#].*$", "", source_url)
        if "zillow.com" not in clean:
            return "", "", "failed"
        # Optionally, fetch title for display
        final, html_txt, code, _ = _get(clean, ua=DEFAULT_UA, allow_redirects=True)
        addr_str = ""
        if code == 200 and html_txt:
            addr_str = extract_best_address_string(html_txt)
        return clean, addr_str, "already_zillow"

    # For non-zillow URLs (Homespotter, HMS, etc.) → pull HTML & find address text.
    tries = _try_variants(source_url)
    addr_str = ""
    for final, html_txt, code in tries:
        if code != 200 or not html_txt:
            continue
        addr_str = extract_best_address_string(html_txt)
        if addr_str:
            break

    # If we still didn't find anything, just use the URL path as a "search hint"
    if not addr_str:
        parsed = urlparse(source_url)
        guess = (parsed.path or "").replace("-", " ").replace("/", " ").strip()
        addr_str = guess or source_url

    zurl = zillow_from_freeform_address(addr_str)
    if not zurl or "zillow.com" not in zurl:
        return "", addr_str, "failed"

    return zurl, addr_str, "from_html"

# ---------------- UI helpers ----------------
def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Parse pasted text as either CSV or line-by-line URLs/addresses.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV first
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

    # Fallback: one item per line
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
    """
    Render only Zillow URLs as clickable links + a "Copy" button with pure Zillow URL list.
    """
    items = []
    urls_for_copy: List[str] = []

    for r in results:
        u = (r.get("zillow_url") or "").strip()
        if not u or "zillow.com" not in u:
            continue

        urls_for_copy.append(u)
        addr = r.get("display_address") or ""
        badge = r.get("note","")
        bh = ""
        if badge == "ok":
            bh = ' <span class="badge ok">street # found</span>'
        elif badge == "no_number":
            bh = ' <span class="badge warn">no street #</span>'
        label = (addr + " — " + u) if addr else u
        items.append(
            f'<li style="margin:0.2rem 0;"><a href="{escape(u)}" target="_blank" rel="noopener">'
            f'{escape(label)}</a>{bh}</li>'
        )

    html_list = "\n".join(items) if items else "<li>(no Zillow results)</li>"
    raw_lines = "\n".join(urls_for_copy) + ("\n" if urls_for_copy else "")
    js_lines = json.dumps(raw_lines)

    components.html(f"""
      <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{
          position:absolute; top:0; right:8px; z-index:5;
          padding:6px 10px; height:26px; border:0; border-radius:10px;
          color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95;
        }}
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
              try {{
                await navigator.clipboard.writeText(lines);
                btn.textContent='✓';
                setTimeout(()=>btn.textContent='Copy',900);
              }} catch(e) {{
                btn.textContent='×';
                setTimeout(()=>btn.textContent='Copy',900);
              }}
            }});
          }})();
        </script>
      </body></html>
    """, height=min(600, 40 * max(1, len(items)) + 60), scrolling=False)

# ---------------- Main UI ----------------
def render_run_tab(state: dict = None):
    st.header("Address Alchemist — Zillow-only Resolver")
    st.caption(
        "Paste listing links (Homespotter / HMS / MLS) or plain addresses. "
        "This will output **ONLY** Zillow `/homes/..._rb/` links. "
        "Any row that can’t be turned into a Zillow link is skipped."
    )

    colA, colB = st.columns([1.4, 1])
    with colA:
        paste = st.text_area("Paste links or addresses", height=140, label_visibility="collapsed")
    with colB:
        up = st.file_uploader("Upload CSV", type=["csv"], label_visibility="visible")

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

                row_lc = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}

                u = (
                    row_lc.get("url")
                    or row_lc.get("link")
                    or row_lc.get("source_url")
                    or row_lc.get("listing_url")
                    or row_lc.get("property_url")
                    or row_lc.get("hs_link")
                )

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

    # ---------- MAIN BUTTON ----------
    if st.button("🚀 Resolve to Zillow"):
        if not rows_in:
            st.warning("Nothing to process.")
            st.stop()

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            raw = (row.get("url") or row.get("source_url") or row.get("href") or row.get("address") or "").strip()
            if not raw:
                continue

            looks_like_url = (
                raw.startswith(("http://", "https://"))
                or re.match(r'^[\w.-]+\.[a-z]{2,10}(/|$)', raw, re.I)
            )

            if looks_like_url:
                # Make sure it has a scheme if it's a domain-only thing
                if not raw.startswith(("http://", "https://")):
                    u = "https://" + raw
                else:
                    u = raw

                z, addr_str, note = resolve_any_link_to_zillow_rb(u)
                results.append({
                    "original": raw,
                    "zillow_url": z,           # may be ""
                    "display_address": addr_str or "",
                    "note": note,
                })
            else:
                # Plain address string → direct Zillow search link
                z = zillow_from_freeform_address(raw)
                results.append({
                    "original": raw,
                    "zillow_url": z,           # may be ""
                    "display_address": raw,
                    "note": "manual" if z else "failed",
                })

            prog.progress(i/len(rows_in), text=f"Resolved {i}/{len(rows_in)}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results (Zillow-only)")
        _results_list(results)

        zillow_results = [
            r for r in results
            if r.get("zillow_url") and "zillow.com" in r.get("zillow_url","")
        ]

        st.markdown("#### Export (Zillow links only)")
        fmt = st.radio("Format", ["txt","csv","md","html"], horizontal=True)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["original","zillow_url","display_address","note"])
            w.writeheader()
            for r in zillow_results:
                w.writerow({k: r.get(k,"") for k in ["original","zillow_url","display_address","note"]})
            payload, mime, fname = buf.getvalue(), "text/csv", "resolved.csv"
        elif fmt == "html":
            items = "\n".join([
                f'<li><a href="{escape(r.get("zillow_url",""))}" target="_blank" rel="noopener">{escape(r.get("zillow_url",""))}</a></li>'
                for r in zillow_results
            ])
            payload, mime, fname = "<ul>\n"+items+"\n</ul>\n", "text/html", "resolved.html"
        elif fmt == "md":
            payload, mime, fname = "\n".join([r.get("zillow_url","") for r in zillow_results]) + "\n", "text/markdown", "resolved.md"
        else:
            payload, mime, fname = "\n".join([r.get("zillow_url","") for r in zillow_results]) + "\n", "text/plain", "resolved.txt"

        st.download_button("Download", data=payload.encode("utf-8"), file_name=fname, mime=mime, use_container_width=True)

    # ---------- FIX / RE-RUN ----------
    st.divider()
    st.subheader("Fix / Re-run links")
    st.caption("Paste any listing links; I’ll convert them to Zillow `/homes/..._rb/` links.")
    fix_text = st.text_area("Links to fix", height=140, key="fix_area")
    if st.button("🔧 Fix / Re-run"):
        lines = [l.strip() for l in (fix_text or "").splitlines() if l.strip()]
        fixed, shown = [], []
        prog = st.progress(0, text="Fixing…")
        for i, u in enumerate(lines, start=1):
            if not u:
                continue
            if u.startswith(("http://","https://")) or re.match(r'^[\w.-]+\.[a-z]{2,10}(/|$)', u, re.I):
                z, addr_str, note = resolve_any_link_to_zillow_rb(u)
            else:
                z = zillow_from_freeform_address(u)
                addr_str, note = u, "manual" if z else "failed"

            if not z or "zillow.com" not in z:
                prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)} (skipped)")
                continue

            best = z
            badge = "✅" if note in ("ok","already_zillow","from_html","manual") else "⚠️"
            fixed.append(best)
            label = (addr_str or best)
            shown.append(f"- {badge} [{escape(label)}]({escape(best)})")
            prog.progress(i/len(lines), text=f"Fixed {i}/{len(lines)}")

        prog.progress(1.0, text="Done")
        st.markdown("**Fixed Zillow links**")
        st.markdown("\n".join(shown) if shown else "_(No Zillow links could be resolved.)_", unsafe_allow_html=True)
        st.text_area("Copy clean list", value=("\n".join(fixed) + "\n") if fixed else "", height=140, label_visibility="collapsed")

if __name__ == "__main__":
    render_run_tab(st.session_state)
