# ui/run_tab.py
#
# Goal:
#   Take addresses or Zillow /homes/..._rb/ URLs and output
#   Zillow links, preferring:
#     1) /homedetails/..._zpid/  (canonical)
#     2) fallback: /homes/<slug>_rb/
#
# NOTE:
#   This relies on Zillow redirecting the /homes/..._rb/ URL
#   to /homedetails/..._zpid/ or exposing it via HTTP redirects.
#   If no redirect happens, you will still see /homes/..._rb/ URLs.

import csv
import io
import re
import time
from html import escape
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

import requests
import streamlit as st
import streamlit.components.v1 as components


# ---------- Streamlit page styling ----------
st.set_page_config(page_title="Address Alchemist — Zillow homedetails", layout="centered")
st.markdown(
    """
<style>
.block-container { max-width: 980px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
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

# ---------- HTTP helper ----------
REQUEST_TIMEOUT = 10
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _http_get(url: str) -> Tuple[str, int]:
    """
    Simple wrapper for requests.get that:
      - allows redirects
      - returns (final_url, status_code)
    """
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return r.url, r.status_code
    except Exception:
        return url, 0


# ---------- Zillow helpers ----------
def _slugify_for_zillow(addr: str) -> str:
    """
    Convert a freeform address to a Zillow slug:
      '1008 Joe Collins Road, Lillington NC 27546'
        -> '1008-joe-collins-road-lillington-nc-27546'
    """
    s = addr.lower()
    s = re.sub(r"[^\w\s,-]", "", s)   # letters, digits, underscore, space, comma, dash
    s = s.replace(",", " ")
    return re.sub(r"\s+", "-", s.strip())


def build_zillow_homes_url(addr: str) -> str:
    """
    From a plain address, build the /homes/<slug>_rb/ URL.
    """
    slug = _slugify_for_zillow(addr)
    if not slug:
        return ""
    return f"https://www.zillow.com/homes/{slug}_rb/"


def upgrade_to_homedetails(z_url: str) -> Tuple[str, str]:
    """
    If possible, upgrade a Zillow URL to /homedetails/..._zpid/.
    Logic:
      - If it's already homedetails/_zpid/, just clean query/fragment and return.
      - If it's /homes/..._rb/, hit it once with redirects allowed:
          * if final URL contains /homedetails/ and _zpid -> use final
          * else -> use cleaned original /homes/..._rb/.
      - If it's some other Zillow URL, return it cleaned.

    Returns: (resolved_url, note)
    """
    if "zillow.com" not in z_url:
        return "", "not_zillow"

    # Normalize: drop query params and fragment for display
    clean = re.sub(r"[?#].*$", "", z_url.strip())

    # Case 1: already canonical homedetails
    if "/homedetails/" in clean and "_zpid" in clean:
        return clean, "already_homedetails"

    # Case 2: /homes/..._rb/ URL – try redirect
    if "/homes/" in clean and "_rb" in clean:
        final_url, status = _http_get(clean)
        final_clean = re.sub(r"[?#].*$", "", final_url.strip())

        # If redirected to /homedetails/..._zpid/, use that
        if (
            "zillow.com" in final_clean
            and "/homedetails/" in final_clean
            and "_zpid" in final_clean
            and status in (200, 301, 302, 303, 307, 308)
        ):
            return final_clean, "from_redirect"

        # Fallback: we stay with /homes/..._rb/
        return clean, "fallback_homes"

    # Case 3: any other Zillow URL – just clean it
    return clean, "other_zillow"


# ---------- Input parsing ----------
def parse_pasted_rows(text: str) -> List[Dict[str, Any]]:
    """
    Input can be:
      - one item per line (address or URL)
      - or CSV (with columns like 'address' or 'url')
    """
    text = (text or "").strip()
    if not text:
        return []

    # Try CSV first
    try:
        lines = text.splitlines()
        if len(lines) >= 2 and ("," in lines[0] or "\t" in lines[0]):
            dialect = csv.Sniffer().sniff(lines[0])
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            rows = [dict(r) for r in reader]
            if rows:
                return rows
    except Exception:
        pass

    # Fallback: treat each non-empty line as a record
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


# ---------- Results renderer ----------
def render_results_list(results: List[Dict[str, Any]]):
    """
    Show clickable Zillow links plus a "Copy" button with the raw URLs.
    """
    items_html = []
    urls_for_copy: List[str] = []

    for r in results:
        url = (r.get("zillow_url") or "").strip()
        if not url or "zillow.com" not in url:
            continue

        urls_for_copy.append(url)
        label = r.get("label") or url
        note = r.get("note", "")

        if note in ("already_homedetails", "from_redirect"):
            badge = '<span class="badge ok">homedetails</span>'
        elif note == "fallback_homes":
            badge = '<span class="badge warn">search</span>'
        else:
            badge = ""

        items_html.append(
            f'<li style="margin:0.2rem 0;"><a href="{escape(url)}" '
            f'target="_blank" rel="noopener">{escape(label)}</a>{badge}</li>'
        )

    if not items_html:
        items_html.append("<li>(no Zillow URLs resolved)</li>")

    list_html = "\n".join(items_html)
    copy_text = "\n".join(urls_for_copy) + ("\n" if urls_for_copy else "")
    js_copy = copy_text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    components.html(
        f"""
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      html,body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif; }}
      .wrap {{ position:relative; padding:8px 120px 4px 0; }}
      .copy-btn {{
        position:absolute; top:0; right:8px;
        padding:6px 10px; height:26px;
        border:0; border-radius:10px;
        color:#fff; font-weight:700;
        background:#1d4ed8; cursor:pointer; opacity:.95;
      }}
      ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
      ul.link-list li {{ margin:0.2rem 0; }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <button id="copyAll" class="copy-btn">Copy</button>
      <ul class="link-list">{list_html}</ul>
    </div>
    <script>
      (function(){{
        const text = "{js_copy}";
        const btn = document.getElementById('copyAll');
        btn.addEventListener('click', async () => {{
          try {{
            await navigator.clipboard.writeText(text.replace(/\\\\n/g, "\\n"));
            btn.textContent = "✓";
            setTimeout(() => btn.textContent = "Copy", 900);
          }} catch (e) {{
            btn.textContent = "×";
            setTimeout(() => btn.textContent = "Copy", 900);
          }}
        }});
      }})();
    </script>
  </body>
</html>
""",
        height=min(600, 40 * max(1, len(items_html)) + 60),
        scrolling=False,
    )


# ---------- Main UI ----------
def render_run_tab(state: dict = None):
    st.header("Address Alchemist — Zillow homedetails resolver")
    st.caption(
        "Paste plain **addresses** or Zillow **/homes/..._rb/** links.\n\n"
        "I’ll try to upgrade them to **/homedetails/..._zpid/** links using Zillow’s redirects. "
        "If Zillow doesn’t redirect, you’ll still get the /homes/<slug>_rb/ URL."
    )

    paste = st.text_area("Paste addresses or Zillow links (one per line)", height=180)

    rows = parse_pasted_rows(paste)
    st.write(f"Parsed **{len(rows)}** row(s).")

    if st.button("🚀 Resolve to Zillow"):
        if not rows:
            st.warning("Nothing to process.")
            st.stop()

        results: List[Dict[str, Any]] = []
        prog = st.progress(0.0, text="Resolving…")

        for i, row in enumerate(rows, start=1):
            raw = (row.get("url") or row.get("address") or "").strip()
            if not raw:
                continue

            # Decide: URL vs address
            looks_like_url = raw.startswith(("http://", "https://")) or re.match(
                r"^[\w.-]+\.[a-z]{2,10}(/|$)", raw, re.I
            )

            if looks_like_url and "zillow.com" in raw:
                # Already a Zillow link – try to upgrade
                z_url, note = upgrade_to_homedetails(raw)
                results.append(
                    {
                        "original": raw,
                        "zillow_url": z_url,
                        "label": raw,
                        "note": note,
                    }
                )
            elif looks_like_url:
                # Non-Zillow URL – we don't touch it
                results.append(
                    {
                        "original": raw,
                        "zillow_url": "",
                        "label": raw,
                        "note": "not_zillow",
                    }
                )
            else:
                # Plain address: build /homes/<slug>_rb/ then try upgrade
                homes_url = build_zillow_homes_url(raw)
                if homes_url:
                    z_url, note = upgrade_to_homedetails(homes_url)
                else:
                    z_url, note = "", "bad_address"

                results.append(
                    {
                        "original": raw,
                        "zillow_url": z_url,
                        "label": raw,
                        "note": note,
                    }
                )

            prog.progress(i / len(rows), text=f"Resolved {i}/{len(rows)}")
            time.sleep(0.02)

        prog.progress(1.0, text="Done")

        st.subheader("Results (homedetails preferred)")
        render_results_list(results)

        # Export only Zillow URLs
        zillow_rows = [
            r
            for r in results
            if r.get("zillow_url") and "zillow.com" in r.get("zillow_url", "")
        ]

        st.markdown("#### Export (Zillow links only)")
        fmt = st.radio("Format", ["txt", "csv", "md"], horizontal=True)

        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf, fieldnames=["original", "zillow_url", "label", "note"]
            )
            writer.writeheader()
            for r in zillow_rows:
                writer.writerow(
                    {
                        "original": r.get("original", ""),
                        "zillow_url": r.get("zillow_url", ""),
                        "label": r.get("label", ""),
                        "note": r.get("note", ""),
                    }
                )
            data = buf.getvalue()
            mime = "text/csv"
            fname = "zillow_resolved.csv"
        elif fmt == "md":
            data = "\n".join([r.get("zillow_url", "") for r in zillow_rows]) + "\n"
            mime = "text/markdown"
            fname = "zillow_resolved.md"
        else:
            data = "\n".join([r.get("zillow_url", "") for r in zillow_rows]) + "\n"
            mime = "text/plain"
            fname = "zillow_resolved.txt"

        st.download_button(
            "Download",
            data=data.encode("utf-8"),
            file_name=fname,
            mime=mime,
            use_container_width=True,
        )


if __name__ == "__main__":
    render_run_tab(st.session_state)
