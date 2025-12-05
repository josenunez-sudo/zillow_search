# ui/run_tab.py
#
# Safe, simple version to restore:
#  - Single search box
#  - Bulk paste
#  - CSV upload
#  - Always outputs *Zillow* links
# For addresses: builds https://www.zillow.com/homes/<slug>_rb/
# For existing Zillow URLs: cleans them (drops ?query and #fragment).

import csv
import io
import re
from html import escape
from typing import Any, Dict, List

import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Address Alchemist — Zillow simple", layout="centered")

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

# ---------- Core helpers ----------

def slugify_for_zillow(addr: str) -> str:
    """Turn an address into a Zillow-friendly slug."""
    s = (addr or "").lower()
    # keep letters, numbers, underscore, space, comma, dash
    s = re.sub(r"[^\w\s,-]", "", s)
    s = s.replace(",", " ")
    return re.sub(r"\s+", "-", s.strip())

def address_to_zillow_homes(addr: str) -> str:
    """Build /homes/<slug>_rb/ from a freeform address."""
    slug = slugify_for_zillow(addr)
    if not slug:
        return ""
    return f"https://www.zillow.com/homes/{slug}_rb/"

def normalize_zillow_or_build(addr_or_url: str) -> str:
    """
    If it's already a Zillow URL -> clean it (remove query/fragment).
    Otherwise -> treat as address and build /homes/<slug>_rb/.
    """
    s = (addr_or_url or "").strip()
    if not s:
        return ""
    if "zillow.com" in s:
        # just clean the URL
        return re.sub(r"[?#].*$", "", s)
    # assume it's an address
    return address_to_zillow_homes(s)

# ---------- Input parsing ----------

def parse_pasted_rows(text: str) -> List[Dict[str, Any]]:
    """
    Accept either:
      - CSV pasted in (with a header line), OR
      - one value per line (address or URL).
    Returns rows of {"value": <string>}.
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
                # Wrap into "value"
                out: List[Dict[str, Any]] = []
                for row in rows:
                    # try common column names; you can tweak this list
                    v = (
                        row.get("zillow_url")
                        or row.get("url")
                        or row.get("address")
                        or row.get("property_address")
                        or row.get("full_address")
                        or ""
                    ).strip()
                    if v:
                        out.append({"value": v})
                return out
    except Exception:
        pass

    # Fallback: one item per line
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        rows.append({"value": s})
    return rows

# ---------- Results rendering ----------

def render_results_list(results: List[Dict[str, Any]]):
    """
    Show clickable Zillow links, plus a "Copy" button that copies raw URLs.
    """
    items = []
    urls_for_copy: List[str] = []

    for r in results:
        z = (r.get("zillow_url") or "").strip()
        if not z:
            continue
        urls_for_copy.append(z)
        label = r.get("label") or z
        note = r.get("note", "")
        if note == "zillow":
            badge = '<span class="badge ok">zillow</span>'
        elif note == "generated":
            badge = '<span class="badge ok">from address</span>'
        else:
            badge = ""
        items.append(
            f'<li style="margin:0.2rem 0;"><a href="{escape(z)}" target="_blank" '
            f'rel="noopener">{escape(label)}</a>{badge}</li>'
        )

    if not items:
        items.append("<li>(no Zillow links)</li>")

    list_html = "\n".join(items)
    raw = "\n".join(urls_for_copy) + ("\n" if urls_for_copy else "")
    js = raw.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

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
        const text = "{js}";
        const btn = document.getElementById('copyAll');
        btn.addEventListener('click', async () => {{
          try {{
            await navigator.clipboard.writeText(text.replace(/\\\\n/g, "\\n"));
            btn.textContent = "✓";
            setTimeout(() => btn.textContent = "Copy", 900);
          }} catch(e) {{
            btn.textContent = "×";
            setTimeout(() => btn.textContent = "Copy", 900);
          }}
        }});
      }})();
    </script>
  </body>
</html>
""",
        height=200 + 24 * len(items),
        scrolling=False,
    )

# ---------- Main tab ----------

def render_run_tab(state=None):
    st.header("Address Alchemist — Simple Zillow Link Builder")
    st.caption(
        "Enter addresses or Zillow links.\n\n"
        "• For plain addresses, I’ll build a Zillow `/homes/<slug>_rb/` search link.\n"
        "• For existing Zillow links, I’ll just clean them (no Google, no other domains)."
    )

    # --- Single search box ---
    st.subheader("Single search")
    single = st.text_input("Address or Zillow URL", key="single_search")
    if st.button("Create Zillow link", key="single_btn"):
        z = normalize_zillow_or_build(single)
        if z:
            st.success(z)
            st.markdown(f"[Open in Zillow]({z})")
        else:
            st.warning("I couldn't build a Zillow link from that input.")

    st.divider()

    # --- Bulk section ---
    st.subheader("Bulk: paste or CSV")

    col1, col2 = st.columns([1.4, 1])
    with col1:
        bulk_text = st.text_area(
            "Paste addresses or Zillow links (one per line)",
            height=180,
            key="bulk_text",
        )
    with col2:
        csv_file = st.file_uploader("...or upload CSV", type=["csv"], key="bulk_csv")

    rows: List[Dict[str, Any]] = []
    rows.extend(parse_pasted_rows(bulk_text))

    # If CSV uploaded, merge rows from file
    if csv_file is not None:
        try:
            content = csv_file.getvalue().decode("utf-8-sig", errors="ignore")
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                v = (
                    row.get("zillow_url")
                    or row.get("url")
                    or row.get("address")
                    or row.get("property_address")
                    or row.get("full_address")
                    or ""
                ).strip()
                if v:
                    rows.append({"value": v})
        except Exception as e:
            st.warning(f"Could not read CSV: {e}")

    st.write(f"Parsed **{len(rows)}** bulk row(s).")

    if st.button("Build Zillow links for bulk", key="bulk_btn"):
        results: List[Dict[str, Any]] = []
        for r in rows:
            raw = (r.get("value") or "").strip()
            if not raw:
                continue
            z = normalize_zillow_or_build(raw)
            if not z:
                continue
            note = "zillow" if "zillow.com" in raw else "generated"
            results.append(
                {
                    "original": raw,
                    "zillow_url": z,
                    "label": raw,
                    "note": note,
                }
            )

        st.subheader("Results")
        render_results_list(results)

        # Download .txt of just the Zillow URLs
        z_urls = [r["zillow_url"] for r in results]
        txt_data = "\n".join(z_urls) + ("\n" if z_urls else "")
        st.download_button(
            "Download as .txt",
            data=txt_data.encode("utf-8"),
            file_name="zillow_links.txt",
            mime="text/plain",
            use_container_width=True,
        )

if __name__ == "__main__":
    render_run_tab()
