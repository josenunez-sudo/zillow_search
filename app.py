import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image

# ---------- Optional deps ----------
try:
    import pillow_avif  # noqa: F401
except Exception:
    pass
try:
    import usaddress
except Exception:
    usaddress = None

# ---------- Rerun helper ----------
def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ---------- Supabase ----------
from supabase import create_client, Client

@st.cache_resource
def get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = get_supabase()

# ---------- Page & global styles ----------
def _page_icon_from_avif(path: str):
    if not os.path.exists(path):
        return "⚗️"
    try:
        im = Image.open(path); im.load()
        if im.mode not in ("RGB", "RGBA"): im = im.convert("RGBA")
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return "⚗️"

st.set_page_config(
    page_title="Address Alchemist",
    page_icon=_page_icon_from_avif("/mnt/data/link.avif"),
    layout="centered",
)

# ---------- Base styles ----------
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;800&display=swap');
.block-container { max-width: 980px; }
.app-title { font-family: 'Barlow Condensed', system-ui; font-weight: 800; font-size: 2.1rem; margin: 0 0 8px; }
.app-sub { color:#6b7280; margin:0 0 12px; }
.center-box { border:1px solid rgba(0,0,0,.08); border-radius:12px; padding:16px; }
.small { color:#6b7280; font-size:12.5px; margin-top:6px; }
ul.link-list { margin:0 0 .5rem 1.2rem; padding:0; }
textarea { border-radius:10px !important; }
textarea:focus { outline:3px solid #93c5fd !important; outline-offset:2px; }
[data-testid="stFileUploadClearButton"] { display:none !important; }
.detail { font-size:14.5px; margin:8px 0 0 0; line-height:1.35; }
.hl { display:inline-block; background:#f1f5f9; border-radius:8px; padding:2px 6px; margin-right:6px; font-size:12px; }

/* Badges baseline + DUP */
.badge {
  display:inline-block; font-size:12px; font-weight:800;
  padding:3px 10px; border-radius:999px; margin-left:8px;
  letter-spacing:.2px;
}
.badge.dup { background:#fee2e2; color:#991b1b; }

/* Poppier NEW badge */
.badge.new {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#065f46;
  border:1px solid rgba(5,150,105,.35);
  box-shadow: 0 6px 16px rgba(16,185,129,.25), 0 1px 3px rgba(0,0,0,.08);
  text-transform: uppercase;
}
html[data-theme="dark"] .badge.new,
.stApp [data-theme="dark"] .badge.new {
  background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
  color:#a7f3d0;
  border-color: rgba(167,243,208,.35);
  box-shadow: 0 6px 16px rgba(6,95,70,.45), 0 1px 3px rgba(0,0,0,.35);
}

/* Pills for client active/inactive (match NEW pop) */
.pill { font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px; }
.pill.active {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#065f46;
  border:1px solid rgba(5,150,105,.35);
  box-shadow: 0 4px 12px rgba(16,185,129,.25);
}
html[data-theme="dark"] .pill.active,
.stApp [data-theme="dark"] .pill.active {
  background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
  color:#a7f3d0;
  border-color: rgba(167,243,208,.35);
  box-shadow: 0 4px 12px rgba(6,95,70,.45);
}

/* Client name always white */
.client-name { color: #ffffff !important; font-weight: 700; }

/* Run button glow / pop (scoped to .run-zone) */
.run-zone .stButton > button {
  background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important;
  color: #fff !important;
  font-weight: 800 !important;
  letter-spacing: .2px !important;
  border: 0 !important;
  border-radius: 12px !important;
  box-shadow: 0 8px 20px rgba(29,78,216,.35), 0 2px 6px rgba(0,0,0,.15) !important;
  transform: translateY(0) !important;
  transition: transform .08s ease, box-shadow .12s ease, filter .08s ease !important;
}
.run-zone .stButton > button:hover {
  transform: translateY(-1px) !important;
  box-shadow: 0 12px 28px rgba(29,78,216,.40), 0 3px 10px rgba(0,0,0,.18) !important;
  filter: brightness(1.06) !important;
}
.run-zone .stButton > button:active {
  transform: translateY(0) scale(.99) !important;
}
</style>
""", unsafe_allow_html=True)

# ---------- Debug toggle ----------
def _get_debug_mode() -> bool:
    try:
        qp = st.query_params if hasattr(st, "query_params") else st.experimental_get_query_params()
        raw = qp.get("debug", "")
        val = raw[0] if isinstance(raw, list) and raw else raw
        token = (str(val) or os.getenv("AA_DEBUG", ""))
    except Exception:
        token = os.getenv("AA_DEBUG", "")
    return str(token).lower() in ("1","true","yes","on")
DEBUG_MODE = _get_debug_mode()

# ---------- Query param helpers ----------
def _qp_get(name, default=None):
    try:
        qp = st.query_params
        val = qp.get(name, default)
        if isinstance(val, list) and val:
            return val[0]
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        return (qp.get(name, [default]) or [default])[0]

# (UPDATED) _qp_set supports clear-all when called with no kwargs
def _qp_set(**kwargs):
    try:
        if not kwargs:
            # New: clear all query params (modern API)
            if hasattr(st, "query_params"):
                st.query_params.clear()
            else:
                st.experimental_set_query_params()
        else:
            if hasattr(st, "query_params"):
                st.query_params.update(kwargs)
            else:
                st.experimental_set_query_params(**kwargs)
    except Exception:
        # Fallback for very old versions
        if kwargs:
            st.experimental_set_query_params(**kwargs)
        else:
            st.experimental_set_query_params()

# ---------- Header ----------
st.markdown('<h2 class="app-title">Address Alchemist</h2>', unsafe_allow_html=True)
st.markdown('<p class="app-sub">Paste addresses or <em>any listing links</em> → verified Zillow links</p>', unsafe_allow_html=True)

# ---------- Tabs ----------
tab_run, tab_clients = st.tabs(["Run", "Clients"])

# --------------------------------------------------------------------------------------
# RUN TAB (trimmed logic; hooks preserved for your resolver + enrichment + rendering)
# --------------------------------------------------------------------------------------
with tab_run:
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area(
        "Paste addresses or links",
        placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524",
        height=160,
        label_visibility="collapsed"
    )
    st.markdown('</div>', unsafe_allow_html=True)

    # Your real code populates these; providing safe defaults so this trimmed file runs.
    results: List[Dict[str, Any]] = []
    client_tag = ""
    campaign_tag = ""

    # Pop Run button (scoped)
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown('</div>', unsafe_allow_html=True)

    # Enrichment is opt-in (default False)
    enrich_details = st.checkbox("Enrich details", value=False)

    # --- PLACEHOLDER: your actual pipeline goes here on click ---
    # For demonstration, we simulate a single result to showcase badges/styles.
    if clicked:
        addr = (paste.splitlines()[0].strip() if paste.strip() else "123 Demo St, Raleigh, NC 27601")
        demo_url = "https://www.zillow.com/homedetails/123-Demo-St-Raleigh-NC-27601/1234567_zpid/"
        results = [{
            "input_address": addr,
            "zillow_url": demo_url,
            "preview_url": demo_url,
            "already_sent": False,   # so it shows NEW
            "dup_reason": "",
            "dup_sent_at": "",
        }]

        if enrich_details:
            st.write("Enriching details (parallel)…")
            # results = asyncio.run(enrich_results_async(results))  # keep your real call in your full app

    # Results HTML with badges
    def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
        li_html = []
        for r in results:
            href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
            if not href:
                continue
            safe_href = escape(href)

            badge_html = ""
            if client_selected:
                if r.get("already_sent"):
                    tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
                    badge_html = f' <span class="badge dup" title="{tip}">Duplicate</span>'
                else:
                    # NEW badge (poppier)
                    badge_html = ' <span class="badge new" title="New for this client">NEW</span>'

            li_html.append(f'<li><a href="{safe_href}" target="_blank" rel="noopener">{safe_href}</a>{badge_html}</li>')

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
            .results-wrap {{ position:relative; box-sizing:border-box; padding:12px 132px 8px 0; }}
            ul.link-list {{ margin:0 0 .5rem 1.2rem; padding:0; list-style:disc; }}
            ul.link-list li {{ margin:0.45rem 0; }}
            .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:8px 12px; height:28px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:0; transform:translateY(-2px); transition:opacity .18s ease, transform .06s ease; }}
            .results-wrap:hover .copyall-btn {{ opacity:1; transform:translateY(0); }}
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
        est_h = max(120, min(52 * max(1, len(li_html)) + 24, 900))
        components.html(html, height=est_h, scrolling=False)

    # Render results section (client_selected=True to show NEW sample)
    if results:
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=True)
    else:
        st.info("Paste addresses or links (or upload CSV), then click **Run**.")

# --------------------------------------------------------------------------------------
# CLIENTS TAB (trimmed; shows pills and working Close Report using _qp_set())
# --------------------------------------------------------------------------------------
def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    # Close report button – clears all query params
    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _qp_set()  # clear report + scroll flags
            _safe_rerun()

    # Minimal body so the view renders
    st.caption("Showing latest sent links (trimmed demo).")
    st.markdown("<ul class='link-list'><li><a href='https://www.zillow.com' target='_blank' rel='noopener'>Example address → Zillow</a></li></ul>", unsafe_allow_html=True)

def _client_row_native(name: str, norm: str, cid: int, active: bool):
    status = "active" if active else "inactive"
    with st.container():
        col_left, col_right = st.columns([0.72, 0.28], vertical_alignment="center")
        with col_left:
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;gap:10px;">
                    <span class="client-name">{escape(name)}</span>
                    <span class="pill {status}">{status}</span>
                </div>
                """,
                unsafe_allow_html=True
            )
        with col_right:
            c1, c2 = st.columns(2)
            with c1:
                if st.button("▦", key=f"rep_{cid}", help="Open report"):
                    _qp_set(report=norm, scroll="1")
                    _safe_rerun()
            with c2:
                st.button("✎", key=f"ren_{cid}", help="Rename (demo)")

with tab_clients:
    st.subheader("Clients")
    st.caption("Manage active and inactive clients. (trimmed demo list)")

    # Trimmed demo list to showcase pill style + report open/close
    demo_clients = [
        {"id": 1, "name": "Keelie Mason", "name_norm": "keelie mason", "active": True},
        {"id": 2, "name": "Alex Rivera",   "name_norm": "alex rivera",   "active": False},
    ]

    colA, colB = st.columns(2)
    with colA:
        st.markdown("### Active")
        for c in [d for d in demo_clients if d["active"]]:
            _client_row_native(c["name"], c["name_norm"], c["id"], active=True)
    with colB:
        st.markdown("### Inactive")
        for c in [d for d in demo_clients if not d["active"]]:
            _client_row_native(c["name"], c["name_norm"], c["id"], active=False)

    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)

    # If report param is present, render report and keep scroll param
    try:
        report_norm_qp = st.query_params.get("report", "")
        if isinstance(report_norm_qp, list): report_norm_qp = report_norm_qp[0] if report_norm_qp else ""
    except Exception:
        report_norm_qp = (st.experimental_get_query_params().get("report", [""]) or [""])[0]

    if report_norm_qp:
        display_name = next((c["name"] for c in demo_clients if c.get("name_norm")==report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        # keep only the report param (drop scroll) after scrolling
        _qp_set(report=report_norm_qp)
