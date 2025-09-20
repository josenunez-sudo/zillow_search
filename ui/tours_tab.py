# ui/tours_tab.py
from __future__ import annotations
import os
import io
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components

# -------- Tour parsing (PDF / Print HTML) --------
from services.tour_parsers import (
    parse_showingtime_pdf,
    parse_showingtime_print_html,
)

# -------- Optional resolver (homedetails); we fall back to _rb if not present --------
HAVE_RESOLVER = False
try:
    # These names follow your original monolithic logic.
    from services.resolver import process_single_row, upgrade_to_homedetails_if_needed, make_preview_url  # type: ignore
    HAVE_RESOLVER = True
except Exception:
    HAVE_RESOLVER = False

# -------- Optional: Supabase access to fetch clients & log to "sent" --------
# (kept local to avoid import cycles)
try:
    from supabase import create_client, Client  # type: ignore
    SUPABASE_OK = True
except Exception:
    SUPABASE_OK = False
    Client = object  # type: ignore

@st.cache_resource
def _get_supabase() -> Optional[Client]:
    if not SUPABASE_OK:
        return None
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

SUPABASE = _get_supabase()

@st.cache_data(ttl=60, show_spinner=False)
def _fetch_clients(include_inactive: bool = False) -> List[Dict]:
    if not SUPABASE:
        return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _slug_addr_for_zillow_rb(address: str) -> str:
    """
    Fallback to a clean Zillow search deeplink that unfurls well in SMS:
    https://www.zillow.com/homes/<slug>_rb/
    We expect address like: '123 Main St, City, ST 12345'
    """
    base = (address or "").strip().lower()
    base = re.sub(r"[^\w\s,-]", "", base).replace(",", "")
    base = re.sub(r"\s+", "-", base)
    return f"https://www.zillow.com/homes/{base}_rb/"

def _resolve_address_to_zillow(address: str) -> str:
    """
    Try your resolver (process_single_row) to get a homedetails URL.
    If unavailable or it fails, return a clean _rb deeplink.
    """
    addr = (address or "").strip()
    if not addr:
        return ""
    # Try full resolver
    if HAVE_RESOLVER:
        try:
            row = {"address": addr}
            res = process_single_row(
                row,
                delay=0.35,
                land_mode=True,
                defaults={"city": "", "state": "", "zip": ""},
                require_state=True,
                mls_first=True,
                default_mls_name="",
                max_candidates=12,
            )
            z = res.get("zillow_url") or ""
            if z:
                z = upgrade_to_homedetails_if_needed(z)
                # Prefer canonical preview-style url to maximize unfurls
                prev = make_preview_url(z) or z
                return prev or z
        except Exception:
            pass
    # Fallback
    return _slug_addr_for_zillow_rb(addr)

def _tight_results_html(urls: List[str], badges: List[str]) -> str:
    """
    Render a compact list of hyperlinks (URL as anchor text) + a floating Copy button.
    badges: "" | '<span class="badge new">NEW</span>' | '<span class="badge dup">Duplicate</span>'
    """
    items = []
    for i, u in enumerate(urls):
        if not u:
            continue
        b = badges[i] if i < len(badges) else ""
        items.append(f'<li style="margin:0.2rem 0;"><a href="{st.html(u)}" target="_blank" rel="noopener">{st.html(u)}</a>{b}</li>')
    if not items:
        items = ["<li>(no links)</li>"]

    copy_text = "\\n".join([u for u in urls if u]) + ("\\n" if urls else "")
    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{
          position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px;
          border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95;
        }}
      </style>
    </head><body>
      <div class="results-wrap">
        <button id="copyAll" class="copyall-btn" title="Copy clean URLs" aria-label="Copy clean URLs">Copy</button>
        <ul class="link-list" id="resultsList">{''.join(items)}</ul>
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
    return html

def _log_tour_to_sent(urls: List[str], addresses: List[str], client_norm: str, campaign: str) -> Tuple[bool, str]:
    """Minimal insert to 'sent' table so they show up under the client report."""
    if not (SUPABASE and client_norm and urls):
        return False, "Supabase not configured or no data."
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    rows = []
    for i, u in enumerate(urls):
        if not u:
            continue
        addr = (addresses[i] if i < len(addresses) else "") or None
        rows.append({
            "client": client_norm,
            "campaign": campaign,
            "url": u,
            "address": addr,
            "sent_at": now_iso,
            # canonical/zpid left null; your deduper will compute when needed in Run tab
        })
    if not rows:
        return False, "No rows to log."
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def render_tours_tab():
    st.subheader("Tours")
    st.caption("Paste a **ShowingTime Print page URL** or upload the **Tour PDF**. I’ll extract the stops and (optionally) build clean Zillow links.")

    # ---------- Input controls ----------
    col_input, col_actions = st.columns([1.4, 1])
    with col_input:
        mode = st.radio("Source", ["Print page URL", "Upload PDF"], horizontal=True)
        tour_url: str = ""
        pdf_bytes: Optional[bytes] = None

        if mode == "Print page URL":
            tour_url = st.text_input("ShowingTime Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
        else:
            up = st.file_uploader("ShowingTime Tour PDF", type=["pdf"])
            if up is not None:
                pdf_bytes = up.getvalue()

    with col_actions:
        if st.button("Parse tour", use_container_width=True):
            st.session_state.pop("__tour_parsed__", None)
            try:
                if mode == "Print page URL":
                    if not tour_url.strip():
                        st.error("Please paste a ShowingTime Print page URL.")
                        st.stop()
                    parsed = parse_showingtime_print_html(tour_url, is_url=True)
                else:
                    if not pdf_bytes:
                        st.error("Please upload a tour PDF.")
                        st.stop()
                    parsed = parse_showingtime_pdf(pdf_bytes)

                st.session_state["__tour_parsed__"] = parsed
                st.success("Tour parsed.")
            except Exception as e:
                st.error("Could not parse this tour. Please ensure it is the ShowingTime **Print** page or the exported **Tour PDF**.")
                with st.expander("Details"):
                    st.exception(e)

    parsed = st.session_state.get("__tour_parsed__")
    if not parsed:
        st.info("Provide a Print page URL or a PDF and click **Parse tour**.")
        return

    buyer = parsed.get("buyer") or "(unknown buyer)"
    tour_date = parsed.get("date") or "(unknown date)"
    stops: List[Dict] = parsed.get("stops") or []

    # ---------- Summary ----------
    st.markdown(f"**Buyer:** {buyer} &nbsp; • &nbsp; **Tour:** {tour_date}")
    st.caption(f"{len(stops)} stop{'s' if len(stops)!=1 else ''} detected")

    # ---------- Show the extracted stops (address + time) ----------
    # Keep a simple compact list; we’ll render links later
    items = []
    raw_addresses = []
    for s in stops:
        seq = s.get("seq")
        addr = s.get("address", "")
        raw_addresses.append(addr)
        tt = s.get("raw_time") or ""
        items.append(f"<li style='margin:0.2rem 0;'>{seq}. {st.html(addr)}" + (f" — {st.html(tt)}" if tt else "") + "</li>")
    st.markdown("<ul>" + "\n".join(items) + "</ul>", unsafe_allow_html=True)

    # ---------- Resolution to Zillow links ----------
    st.markdown("#### Links")
    col_resolve, col_client = st.columns([1.1, 1])
    with col_resolve:
        do_resolve = st.checkbox("Resolve each address to a Zillow link (try homedetails; fallback to _rb)", value=True)
        if st.button("Build links", use_container_width=True, key="__build_links__"):
            urls: List[str] = []
            for addr in raw_addresses:
                u = _resolve_address_to_zillow(addr) if do_resolve else _slug_addr_for_zillow_rb(addr)
                urls.append(u)
            st.session_state["__tour_urls__"] = urls

    urls: List[str] = st.session_state.get("__tour_urls__") or []
    if urls:
        # Simple badges placeholder (no dup logic here)
        badges = [""] * len(urls)
        # Render tight list (URL as anchor text for clean SMS unfurls)
        html = _tight_results_html(urls, badges)
        est_h = max(60, min(34 * max(1, len(urls)) + 20, 700))
        components.html(html, height=est_h, scrolling=False)

        # Export
        txt_payload = "\n".join(urls) + ("\n" if urls else "")
        colD1, colD2 = st.columns([1, 1])
        with colD1:
            st.download_button("Download .txt", data=txt_payload, file_name="tour_links.txt", mime="text/plain", use_container_width=True)
        with colD2:
            # Simple HTML list for email drop-in
            li = "\n".join([f'<li><a href="{u}" target="_blank" rel="noopener">{u}</a></li>' for u in urls])
            html_ul = "<ul>\n" + li + "\n</ul>\n"
            st.download_button("Download .html", data=html_ul, file_name="tour_links.html", mime="text/html", use_container_width=True)

    # ---------- Save to client (optional) ----------
    st.markdown("#### Save to client (optional)")
    if not SUPABASE:
        st.info("Supabase not configured; saving to client is disabled.")
        return

    clients = _fetch_clients(include_inactive=False)
    client_names = [c["name"] for c in clients]
    sel = st.selectbox("Client", ["— Select —"] + client_names, index=0)
    chosen = None if sel == "— Select —" else next((c for c in clients if c["name"] == sel), None)

    # Default campaign from tour date
    def _campaign_from_date(s: str) -> str:
        # Try to parse like "Monday, September 22, 2025" -> 20250922
        try:
            # Remove weekday if present
            s2 = s
            if "," in s2:
                # heuristic: split by comma, keep last two segments
                parts = [p.strip() for p in s2.split(",")]
                if len(parts) >= 2:
                    s2 = ", ".join(parts[-2:])
            dt = datetime.strptime(s2, "%B %d, %Y")
            return "tour-" + dt.strftime("%Y%m%d")
        except Exception:
            return "tour-" + datetime.utcnow().strftime("%Y%m%d")

    default_campaign = _campaign_from_date(tour_date)
    campaign = st.text_input("Campaign tag", value=default_campaign)

    can_save = bool(chosen and (st.session_state.get("__tour_urls__")))
    if st.button("Save stops to client", use_container_width=True, disabled=not can_save):
        urls_to_save = st.session_state.get("__tour_urls__") or []
        ok, msg = _log_tour_to_sent(
            urls=urls_to_save,
            addresses=raw_addresses,
            client_norm=_norm_tag(chosen["name"]),
            campaign=_norm_tag(campaign),
        )
        if ok:
            st.success("Saved to Supabase. They’ll now appear under this client’s report.")
        else:
            st.error(f"Save failed: {msg}")
