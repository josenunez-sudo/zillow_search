# core/styles.py
import io, os
from PIL import Image
import streamlit as st

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

def apply_page_base():
    st.set_page_config(
        page_title="Address Alchemist",
        page_icon=_page_icon_from_avif("/mnt/data/link.avif"),
        layout="centered",
    )
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

/* Badges */
.badge { display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; }
.badge.dup { background:#fee2e2; color:#991b1b; }
.badge.new {
  background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
  color:#065f46;
  border:1px solid rgba(5,150,105,.35);
  box-shadow: 0 6px 16px rgba(16,185,129,.25), 0 1px 3px rgba(0,0,0,.08);
  text-transform: uppercase;
}

/***** TOURED badge *****/
.badge.toured {
  background: linear-gradient(180deg, #dbeafe 0%, #bfdbfe 100%);
  color:#1e3a8a;
  border:1px solid rgba(59,130,246,.35);
  box-shadow: 0 4px 12px rgba(59,130,246,.20);
}
html[data-theme="dark"] .badge.toured,
.stApp [data-theme="dark"] .badge.toured {
  background: linear-gradient(180deg, #0b1220 0%, #0b1a35 100%);
  color:#93c5fd;
  border-color: rgba(147,197,253,.35);
  box-shadow: 0 4px 12px rgba(29,78,216,.30);
}

html[data-theme="dark"] .badge.new,
.stApp [data-theme="dark"] .badge.new {
  background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
  color:#a7f3d0;
  border-color: rgba(167,243,208,.35);
  box-shadow: 0 6px 16px rgba(6,95,70,.45), 0 1px 3px rgba(0,0,0,.35);
}

/* Theme variables */
:root {
  --text-strong: #0f172a;
  --text-muted:  #475569;
  --row-border: #e2e8f0;
  --row-hover:  #f8fafc;
  --ok-bg:#dcfce7; --ok-fg:#166534;
  --bad-bg:#fee2e2; --bad-fg:#991b1b;
}
html[data-theme="dark"], .stApp [data-theme="dark"] {
  --text-strong: #f8fafc;
  --text-muted:  #cbd5e1;
  --row-border: #0b1220;
  --row-hover:  #0f172a;
  --ok-bg:#064e3b; --ok-fg:#a7f3d0;
  --bad-bg:#7f1d1d; --bad-fg:#fecaca;
}

/* Status pill */
.pill { font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px; }
.pill.active {
  background: linear-gradient(180deg, var(--ok-bg) 0%, #bbf7d0 100%);
  color: var(--ok-fg);
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

/* Run button pop */
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
.run-zone .stButton > button:active { transform: translateY(0) scale(.99) !important; }

/* ===== Clients row: inline icon buttons (▦ ✎ ⟳ ⌫) ===== */
.client-name { font-weight:700; color:#ffffff !important; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.inline-panel {
  margin-top:6px; padding:6px; border:1px dashed var(--row-border); border-radius:8px; background:rgba(148,163,184,.08);
}
</style>
""", unsafe_allow_html=True)
