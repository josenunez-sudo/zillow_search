# ui/run_tab.py
# Convert addresses or Zillow /homes/..._rb/ links into Zillow URLs.
# Preference:
#   1) /homedetails/..._zpid/ (canonical)
#   2) fallback: /homes/<slug>_rb/
#
# Only Zillow links ever leave this file.

import csv
import html
import io
import json
import re
import time
from html import escape
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------------- Page ----------------
st.set_page_config(page_title="Address Alchemist — Zillow homedetails", layout="centered")
st.markdown(
    """
<style>
.block-container { max-width: 980px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.run-zone .stButton>button {
  background: linear-gradient(180deg,#2563eb 0%,#1d4ed8 100%)!important;
  color:#fff!important;font-weight:800!important;border:0!important;
  border-radius:12px!important;
  box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important;
}
.badge {
  display:inline-block; font-size:11px; font-weight:800; padding:2px 6px;
  border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15);
}
.badge.ok { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.warn { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
</style>
""",
    unsafe_allow_html=True,
)

# ---------------- HTTP ----------------
REQUEST_TIMEOUT = 12
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _get(
    url: str, ua: str = DEFAULT_UA, allow_redirects: bool = True
) -> Tuple[str, str, int, Dict[str, str]]:
    """Wrapper around requests.get that returns (final_url, text, status_code, headers)."""
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


# ---------------- Small helpers ----------------
def _slugify_for_zillow(s: str) -> str:
    """
    Convert '1008 Joe Collins Road, Lillington NC 27546' ->
    '1008-joe-collins-road-lillington-nc-27546'
    """
    s = (s or "").lower()
    s = re.sub(r"[^\w\s,-]", "", s)
    s = s.replace(",", " ")
    return re.sub(r"\s+", "-", s.strip())


def _extract_homedetails_from_html(html_txt: str) -> str:
    """
    Find the first homedetails ... _zpid/ URL inside the HTML.
    """
    if not html_txt:
        return ""
    m = re.search(
        r'https://www\.zillow\.com/homedetails/[^"\']+?_zpid/',
        html_txt,
    )
    return m.group(0).strip() if m else ""


def _zillow_homedetails_from_search(search_url: str) -> str:
    """
    Given a /homes/<slug>_rb/ search URL, try to resolve to /homedetails/..._zpid/.
    Fallback: the original search_url.
    """
    final, html_txt, code, _ = _get(search_url, ua=DEFAULT_UA, allow_redirects=True)

    # If Zillow redirects us straight to homedetails
    if (
        "zillow.com" in final
        and "/homedetails/" in final
        and "_zpid" in final
    ):
        return re.sub(r"[?#].*$", "", final)

    # Otherwise, look inside HTML for a homedetails link
    if code == 200 and html_txt:
        hd = _extract_homedetails_from_html(html_txt)
        if hd:
            return re.sub(r"[?#].*$", "", hd)

    # Fallback: still a valid Zillow URL (search).
    return re.sub(r"[?#].*$", "", search_url)


def zillow_from_freeform_address(addr_str: str) -> str:
    """
    Take any address-ish string and produce a Zillow URL.
    Preference: homedetails; fallback: /homes/<slug>_rb/.
    """
    addr_str = (addr_str or "").strip()
    if not addr_str:
        return ""

    slug = _slugify_for_zillow(addr_str)
    if not slug:
        return ""

    search_url = f"https://www.zillow.com/homes/{slug}_rb/"
    return _zillow_homedetails_from_search(search_url)


def resolve_any_zillow_url(z_url: str) -> Tuple[str, str]:
    """
    Handle existing Zillow URLs:
      - /homedetails/..._zpid/ => keep as is
      - /homes/..._rb/         => attempt to upgrade to homedetails
      - others (zillow profile etc.) => returned unchanged
    Returns (resolved_url, note).
    """
    if "zillow.com" not in z_url:
        return "", "not_zillow"

    clean = re.sub(r"[?#].*$", "", z_url)

    # Already a homedetails URL
    if "/homedetails/" in clean and "_zpid" in clean:
        return clean, "already_homedetails"

    # /homes/..._rb/ search URL => try to upgrade
    if "/homes/" in clean and "_rb" in clean:
        hd = _zillow_homedetails_from_search(clean)
        if "/homedetails/" in hd and "_zpid" in hd:
            return hd, "from_search"
        else:
            return hd, "search_fallback"

    # Some other Zillow URL (rentals, agents, etc.)
    return clean, "zillow_other"


# ---------------- Input parsing ----------------
def _rows_from_paste(text: str) -> List[Dict[str, Any]]:
    """
    Parse pasted text as either CSV or line-by-line URLs/addresses.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Attempt CSV first
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
        if s.startswith(("http://", "https://")):
            rows.append({"url": s})
        else:
            rows.append({"address": s})
    return rows


# ---------------- Results rendering ----------------
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
        label = r.get("display_label") or u
        note = r.get("note", "")
        if note in ("already_homedetails", "from_search"):
            badge_html = '<span class="badge ok">homedetails</span>'
        elif note == "search_fallback":
            badge_html = '<span class="badge warn">search</span>'
        else:
            badge_html = ""

        items.append(
            f'<li style="margin:0.2rem 0;"><a href="{escape(u)}" target="_blank" '
            f'rel="noopener">{escape(label)}</a>{badge_html}</li>'
        )

    html_list = "\n".join(items) if items else "<li>(no Zillow results)</li>"
    raw_lines = "\n".join(urls_for_copy) + ("\n" if urls_for_copy else "")
    js_lines = json.dumps(raw_lines)

    components.html(
        f"""
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
    """,
        height=min(600, 40 * max(1, len(items)) + 60),
        scrolling=False,
    )


# ---------------- Main UI ----------------
def render_run_tab(state: dict = None):
    st.header("Address Alchemist — Zillow homedetails resolver")
    st.caption(
        "Paste **addresses** or **Zillow /homes/..._rb/** links below.\n\n"
        "I’ll try to output **/homedetails/..._zpid/** (canonical Zillow links). "
        "If Zillow doesn’t expose the zpid, I’ll fall back to the /homes/<slug>_rb/ URL."
    )

    paste = st.text_area("Paste addresses or Zillow links (one per line)", height=180)

    rows_in: List[Dict[str, Any]] = _rows_from_paste(paste)
    st.write(f"Parsed **{len(rows_in)}** row(s).")

    if st.button("🚀 Resolve to Zillow"):
        if not rows_in:
            st.warning("Nothing to process.")
            st.stop()

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")

        for i, row in enumerate(rows_in, start=1):
            raw = (
                row.get("url")
                or row.get("source_url")
                or row.get("href")
                or row.get("address")
                or ""
            ).strip()
            if not raw:
                continue

            looks_like_url = raw.startswith(("http://", "https://")) or re.match(
                r"^[\w.-]+\.[a-z]{2,10}(/|$)", raw, re.I
            )

            if looks_like_url and "zillow.com" in raw:
                # Existing Zillow link
                z, note = resolve_any_zillow_url(raw)
                results.append(
                    {
                        "original": raw,
                        "zillow_url": z,
                        "display_label": raw,
                        "note": note,
                    }
                )
            elif looks_like_url:
                # Non-zillow URL: leave blank or treat as address in the future if you want
                results.append(
                    {
                        "original": raw,
                        "zillow_url": "",
                        "display_label": raw,
                        "note": "not_zillow",
                    }
                )
            else:
                # Plain address string → Zillow builder
                z = zillow_from_freeform_address(raw)
                note = "from_search" if z else "failed"
                results.append(
                    {
                        "original": raw,
                        "zillow_url": z,
                        "display_label": raw,
                        "note": note,
                    }
                )

            prog.progress(i / len(rows_in), text=f"Resolved {i}/{len(rows_in)}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results (Zillow homedetails preferred)")
        _results_list(results)

        zillow_results = [
            r
            for r in results
            if r.get("zillow_url") and "zillow.com" in r.get("zillow_url", "")
        ]

        st.markdown("#### Export (Zillow links only)")
        fmt = st.radio("Format", ["txt", "csv", "md", "html"], horizontal=True)
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(
                buf, fieldnames=["original", "zillow_url", "display_label", "note"]
            )
            w.writeheader()
            for r in zillow_results:
                w.writerow(
                    {
                        "original": r.get("original", ""),
                        "zillow_url": r.get("zillow_url", ""),
                        "display_label": r.get("display_label", ""),
                        "note": r.get("note", ""),
                    }
                )
            payload, mime, fname = buf.getvalue(), "text/csv", "zillow_resolved.csv"
        elif fmt == "html":
            items = "\n".join(
                [
                    f'<li><a href="{escape(r.get("zillow_url",""))}" target="_blank" '
                    f'rel="noopener">{escape(r.get("zillow_url",""))}</a></li>'
                    for r in zillow_results
                ]
            )
            payload, mime, fname = "<ul>\n" + items + "\n</ul>\n", "text/html", "zillow_resolved.html"
        elif fmt == "md":
            payload = "\n".join([r.get("zillow_url", "") for r in zillow_results]) + "\n"
            mime, fname = "text/markdown", "zillow_resolved.md"
        else:
            payload = "\n".join([r.get("zillow_url", "") for r in zillow_results]) + "\n"
            mime, fname = "text/plain", "zillow_resolved.txt"

        st.download_button(
            "Download",
            data=payload.encode("utf-8"),
            file_name=fname,
            mime=mime,
            use_container_width=True,
        )


if __name__ == "__main__":
    render_run_tab(st.session_state)
