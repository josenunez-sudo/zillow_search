import io, csv, asyncio, time, re
from html import escape
import streamlit as st
import streamlit.components.v1 as components

from core.cache import safe_rerun
from core.config import norm_tag
from services.links import make_trackable_url, bitly_shorten
from services.images import get_thumbnail_and_log   # keep your existing implementation
from services.resolver import (                      # your existing resolver helpers
    is_probable_url, get_first_by_keys, process_single_row,
    resolve_from_source_url, upgrade_to_homedetails_if_needed
)
from utils.urls import make_preview_url

def build_output(rows, fmt, use_display=True, include_notes=False):
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft","already_sent","dup_reason","dup_sent_at"]
        if include_notes: fields += ["summary","highlights","remarks"]
        s = io.StringIO(); w = csv.DictWriter(s, fieldnames=fields); w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields if k != "url"}
            row["url"] = pick_url(r); w.writerow(row)
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

def results_list_with_copy_all(results, client_selected: bool):
    # Always show as hyperlinks, compact spacing
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href: continue
        safe_href = escape(href)
        link_txt = href  # clean URL as text (best for SMS unfurl)
        badge_html = ""
        if client_selected:
            if r.get("already_sent"):
                tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
                badge_html = f' <span class="badge dup" title="{tip}">Duplicate</span>'
            else:
                badge_html = ' <span class="badge new" title="New for this client">NEW</span>'
        li_html.append(f'<li style="margin-bottom:0.3em;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a>{badge_html}</li>')

    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u: copy_lines.append(u.strip())
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 96px 0 0; }}
        ul.link-list {{ margin:0; padding:0 0 0 1.2rem; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.92; }}
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

def render_run_tab(state):
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    # … keep your existing controls & processing flow …
    # Ensure “Enrich details” default is UNCHECKED:
    enrich_details = st.checkbox("Enrich details", value=False)

    # When rendering results:
    # st.markdown("#### Results")
    # results_list_with_copy_all(results, client_selected=client_selected)
    # (do not render the dataframe unless you expose a toggle; you asked to remove dead space)
