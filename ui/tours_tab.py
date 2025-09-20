# -*- coding: utf-8 -*-
# ui/tours_tab.py
#
# Standalone "Tours" tab:
# - Choose client
# - Paste Zillow links -> store under 'tours' table (client, url, canonical, zpid, notes, added_at)
# - Cross-check against 'sent' table (duplicate badge + sent_at)
# - List is clickable; per-row remove; "Copy all links" helper
#
# No imports from other ui modules (avoid circular imports).

from __future__ import annotations

import os
import io
import re
from datetime import datetime
from html import escape
from typing import Any, Dict, List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components
from supabase import create_client, Client
from urllib.parse import unquote

# ---------------- Supabase bootstrap ----------------

@st.cache_resource(show_spinner=False)
def _get_supabase() -> Optional[Client]:
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = _get_supabase()

def _sb_ok() -> bool:
    try:
        return bool(SUPABASE)
    except NameError:
        return False

# ---------------- Utils ----------------

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    """Return (canonical_url, zpid or None) for Zillow links; otherwise just trim #/?."""
    if not url:
        return "", None
    base = re.sub(r'[#?].*$', '', url.strip())
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', base, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(base)
    return canon, (m_z.group(1) if m_z else None)

def address_text_from_url(url: str) -> str:
    if not url:
        return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m:
        return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    return ""

# ---------------- Clients & Sent lookups ----------------

@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
    if not _sb_ok():
        return []
    try:
        rows = (
            SUPABASE.table("clients")
            .select("id,name,name_norm,active")
            .order("name", desc=False)
            .execute()
            .data
            or []
        )
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

@st.cache_data(ttl=300, show_spinner=False)
def _sent_maps(client_norm: str):
    """Return (canon_set, zpid_set, canon_info, zpid_info) for quick cross-checks."""
    if not (_sb_ok() and client_norm.strip()):
        return set(), set(), {}, {}
    try:
        rows = (
            SUPABASE.table("sent")
            .select("canonical,zpid,url,sent_at")
            .eq("client", client_norm.strip())
            .limit(20000)
            .execute()
            .data
            or []
        )
        canon_set = {(r.get("canonical") or "").strip() for r in rows if r.get("canonical")}
        zpid_set = {(r.get("zpid") or "").strip() for r in rows if r.get("zpid")}
        canon_info: Dict[str, Dict[str, str]] = {}
        zpid_info: Dict[str, Dict[str, str]] = {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info:
                canon_info[c] = info
            if z and z not in zpid_info:
                zpid_info[z] = info
        return canon_set, zpid_set, canon_info, zpid_info
    except Exception:
        return set(), set(), {}, {}

# ---------------- Tours CRUD ----------------

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tours(client_norm: str) -> List[Dict[str, Any]]:
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        rows = (
            SUPABASE.table("tours")
            .select("id,client,url,canonical,zpid,notes,added_at")
            .eq("client", client_norm.strip())
            .order("added_at", desc=True)
            .execute()
            .data
            or []
        )
        return rows
    except Exception:
        return []

def invalidate_tours_cache():
    try:
        fetch_tours.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

def _existing_tours_sets(client_norm: str):
    rows = fetch_tours(client_norm)
    cset = {(r.get("canonical") or "").strip() for r in rows if r.get("canonical")}
    zset = {(r.get("zpid") or "").strip() for r in rows if r.get("zpid")}
    return cset, zset

def add_tour_links(client_norm: str, urls: List[str], notes: str = "") -> Dict[str, int]:
    if not (_sb_ok() and client_norm.strip() and urls):
        return {"added": 0, "skipped": 0}

    existing_c, existing_z = _existing_tours_sets(client_norm)
    added = 0
    skipped = 0
    rows = []
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    for raw in urls:
        if not is_probable_url(raw):
            continue
        canon, zpid = canonicalize_zillow(raw)
        key_c = canon.strip()
        key_z = (zpid or "").strip()
        # De-dup within tours for this client
        if (key_c and key_c in existing_c) or (key_z and key_z in existing_z):
            skipped += 1
            continue
        rows.append(
            {
                "client": client_norm.strip(),
                "url": raw.strip(),
                "canonical": key_c or None,
                "zpid": key_z or None,
                "notes": (notes or "").strip() or None,
                "added_at": now_iso,
            }
        )
        existing_c.add(key_c) if key_c else None
        existing_z.add(key_z) if key_z else None

    if not rows:
        return {"added": 0, "skipped": skipped}

    try:
        SUPABASE.table("tours").insert(rows).execute()
        invalidate_tours_cache()
        added = len(rows)
        return {"added": added, "skipped": skipped}
    except Exception:
        return {"added": 0, "skipped": skipped}

def delete_tour(tour_id: Any):
    if not (_sb_ok() and tour_id):
        return False
    try:
        SUPABASE.table("tours").delete().eq("id", tour_id).execute()
        invalidate_tours_cache()
        return True
    except Exception:
        return False

# ---------------- UI helpers ----------------

def _copy_links_widget(urls: List[str]):
    # Compact copy-to-clipboard button (like your Results UI)
    copy_text = "\\n".join([u.strip() for u in urls if u.strip()]) + ("\\n" if urls else "")
    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .wrap {{ position:relative; box-sizing:border-box; padding:4px 80px 0 0; }}
        .copyall-btn {{
          position:absolute; top:0; right:0; z-index:5; padding:6px 10px; height:26px; border:0;
          border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95;
        }}
      </style>
    </head><body>
      <div class="wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
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
    </body></html>
    """
    components.html(html, height=40, scrolling=False)

# ---------------- Public entrypoint ----------------

def render_tours_tab():
    st.subheader("Tours")

    if not _sb_ok():
        st.info("Supabase is not configured. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE` in secrets/environment.")
        return

    clients = fetch_clients(include_inactive=False)
    if not clients:
        st.info("No active clients yet. Add one in the **Clients** tab.")
        return

    names = [c["name"] for c in clients]
    sel_idx = st.selectbox("Client", list(range(len(names))), format_func=lambda i: names[i], index=0)
    client = clients[sel_idx]
    client_norm = client.get("name_norm") or _norm_tag(client["name"])

    st.markdown("**Paste Zillow links for this tour** (one per line). Non-Zillow links will be stored as-is.")
    links_text = st.text_area(
        "Tour links",
        value="",
        height=120,
        placeholder="https://www.zillow.com/homedetails/...\nhttps://www.zillow.com/homedetails/...",
        label_visibility="collapsed",
    )
    notes = st.text_input("Notes (optional)", value="")

    colA, colB = st.columns([1, 1])
    with colA:
        if st.button("Add to Tours", use_container_width=True, key="__add_tours__"):
            raw_lines = [ln.strip() for ln in (links_text or "").splitlines() if ln.strip()]
            # Keep order, remove exact dupes
            seen = set()
            urls = []
            for ln in raw_lines:
                if ln not in seen:
                    seen.add(ln)
                    urls.append(ln)
            stats = add_tour_links(client_norm, urls, notes=notes)
            st.success(f"Added {stats['added']} • Skipped {stats['skipped']} (already in Tours).")
            _safe_rerun()

    st.markdown("---")
    st.markdown("### Current tour list")

    rows = fetch_tours(client_norm)
    canon_set_sent, zpid_set_sent, canon_info, zpid_info = _sent_maps(client_norm)

    if not rows:
        st.info("No tour items yet. Paste links above and click **Add to Tours**.")
        return

    # Copy all helper
    copy_urls = [r.get("canonical") or r.get("url") or "" for r in rows if (r.get("canonical") or r.get("url"))]
    _copy_links_widget(copy_urls)

    # Render compact list with badges and per-row delete
    for r in rows:
        url = (r.get("canonical") or r.get("url") or "").strip()
        if not url:
            continue
        addr = address_text_from_url(url) or url
        zpid = (r.get("zpid") or "").strip()
        already = False
        sent_when = ""
        if url in canon_set_sent:
            already = True
            sent_when = (canon_info.get(url, {}) or {}).get("sent_at", "")
        elif zpid and zpid in zpid_set_sent:
            already = True
            sent_when = (zpid_info.get(zpid, {}) or {}).get("sent_at", "")

        c1, c2 = st.columns([12, 1])
        with c1:
            badge = ""
            if already:
                tip = f"Duplicate; sent {sent_when or '-'}"
                badge = f' <span class="badge dup" title="{escape(tip)}">Duplicate</span>'
            st.markdown(
                f"<div style='margin:4px 0;'>"
                f"<a href='{escape(url)}' target='_blank' rel='noopener'>{escape(addr)}</a>{badge}"
                f"</div>",
                unsafe_allow_html=True,
            )
            if r.get("notes"):
                st.caption(r.get("notes"))

        with c2:
            if st.button("⌫", key=f"tour_del_{r.get('id')}", help="Remove from Tours"):
                delete_tour(r.get("id"))
                _safe_rerun()

    # Optional export
    with st.expander("Export tour links"):
        lines = "\n".join(copy_urls) + ("\n" if copy_urls else "")
        st.download_button(
            "Download TXT",
            data=lines,
            file_name=f"tour_links_{client_norm}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.txt",
            mime="text/plain",
            use_container_width=False,
        )

    # Helpful schema hint (only shows if table missing)
    # You can remove this block once your table exists.
    try:
        _ = rows  # use rows
    except Exception:
        st.info(
            "If you haven't created the `tours` table yet, here's a minimal schema:\n\n"
            "columns: id (uuid, pk, default gen_random_uuid()), client (text), url (text), canonical (text), "
            "zpid (text), notes (text), added_at (timestamptz)\n"
            "indexes: (client, canonical), (client, zpid)"
        )
