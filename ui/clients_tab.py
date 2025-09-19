import streamlit as st
import streamlit.components.v1 as components
from html import escape
from core.cache import safe_rerun
from services.supabase_client import (
    fetch_clients, toggle_client_active, rename_client, delete_client, get_supabase
)

def _qp_get(name, default=None):
    try:
        qp = st.query_params; val = qp.get(name, default)
        if isinstance(val, list) and val: return val[0]
        return val
    except Exception:
        qp = st.experimental_get_query_params()
        return (qp.get(name, [default]) or [default])[0]

def _qp_set(**kwargs):
    try:
        if kwargs: st.query_params.update(kwargs)
        else: st.query_params.clear()
    except Exception:
        if kwargs: st.experimental_set_query_params(**kwargs)
        else: st.experimental_set_query_params()

def address_text_from_url(url: str) -> str:
    from urllib.parse import unquote
    import re
    if not url: return ""
    u = unquote(url)
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m: return re.sub(r"[-+]", " ", m.group(1)).strip().title()
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    return (re.sub(r"[-+]", " ", m.group(1)).strip().title() if m else "")

def _render_client_report_view(client_display_name: str, client_norm: str):
    from services.supabase_client import fetch_sent_for_client
    st.markdown(f"### Report for {escape(client_display_name)}", unsafe_allow_html=True)

    colX, _ = st.columns([1,3])
    with colX:
        if st.button("Close report", key=f"__close_report_{client_norm}"):
            _qp_set(); safe_rerun()

    rows = fetch_sent_for_client(client_norm)
    total = len(rows)

    # campaign filter + search
    seen = []
    for r in rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen: seen.append(c)
    labels = ["All campaigns"] + [("— no campaign —" if c == "" else c) for c in seen]
    keys   = [None] + seen

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        sel_idx = st.selectbox("Filter by campaign", list(range(len(labels))),
                               format_func=lambda i: labels[i], index=0, key=f"__camp_{client_norm}")
        sel_campaign = keys[sel_idx]
    with colF2:
        q = st.text_input("Search address / MLS / URL", value="", placeholder="e.g. 407 Woodall, 2501234, /homedetails/", key=f"__q_{client_norm}")
        q_norm = q.strip().lower()
    with colF3:
        st.caption(f"{total} total logged")

    def _match(row) -> bool:
        if sel_campaign is not None and (row.get("campaign") or "").strip() != sel_campaign:
            return False
        if not q_norm: return True
        addr = (row.get("address") or "").lower()
        mls  = (row.get("mls_id") or "").lower()
        url  = (row.get("url") or "").lower()
        return (q_norm in addr) or (q_norm in mls) or (q_norm in url)

    rows_f = [r for r in rows if _match(r)]
    st.caption(f"{len(rows_f)} matching listing{'s' if len(rows_f)!=1 else ''}")

    if not rows_f:
        st.info("No results match the current filters."); return

    items_html = []
    from html import escape as _esc
    for r in rows_f:
        url = (r.get("url") or "").strip()
        addr = (r.get("address") or "").strip() or address_text_from_url(url) or "Listing"
        sent_at = r.get("sent_at") or ""
        camp = (r.get("campaign") or "").strip()
        chip = f"<span style='font-size:11px;font-weight:700;padding:2px 6px;border-radius:999px;background:#e2e8f0;margin-left:6px;'>{_esc(camp)}</span>" if camp and sel_campaign is None else ""
        items_html.append(
            f"""<li>
                  <a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(addr)}</a>
                  <span style="color:#64748b;font-size:12px;margin-left:6px;">{_esc(sent_at)}</span>
                  {chip}
                </li>"""
        )
    st.markdown("<ul class='link-list'>" + "\n".join(items_html) + "</ul>", unsafe_allow_html=True)

def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    # Single HTML block for perfect inline alignment (name + pill + icons)
    components.html(f"""
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
.row {{display:flex;align-items:center;justify-content:space-between;padding:10px 8px;border-bottom:1px solid rgba(226,232,240,1);}}
.left {{display:flex;align-items:center;gap:8px;min-width:0;}}
.name {{font-weight:700;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}}
.pill {{font-size:11px;font-weight:800;padding:2px 10px;border-radius:999px;{('background:linear-gradient(180deg,#dcfce7, #bbf7d0);color:#166534;border:1px solid rgba(5,150,105,.35);' if active else 'background:#fee2e2;color:#991b1b;')}}}}
.icons {{display:flex;align-items:center;gap:8px;}}
.ic {{min-width: 28px;height:28px;padding:0 8px;border-radius:8px;border:1px solid rgba(0,0,0,.08);font-weight:700;line-height:1;cursor:pointer;background:#f8fafc;color:#64748b;text-decoration:none;display:flex;align-items:center;justify-content:center;}}
.ic:hover {{transform: translateY(-1px);}}
</style></head><body>
  <div class="row">
    <div class="left">
      <span class="name">{escape(name)}</span>
      <span class="pill">{'ACTIVE' if active else 'INACTIVE'}</span>
    </div>
    <div class="icons">
      <a class="ic" href="?report={escape(norm)}&scroll=1" target="_parent" title="Open report">▦</a>
      <a class="ic" href="#" onclick="
        const n=prompt('Rename client:', '{escape(name)}');
        if(n && n.trim()){{
          const u=new URL(parent.location.href);
          u.searchParams.set('act','rename');
          u.searchParams.set('id','{cid}');
          u.searchParams.set('arg', n.trim());
          parent.location.search=u.search;
        }}
        return false;
      " title="Rename">✎</a>
      <a class="ic" href="?act=toggle&id={cid}" target="_parent" title="{'Deactivate' if active else 'Activate'}">⟳</a>
      <a class="ic" href="#" onclick="
        if(confirm('Delete {escape(name)}? This cannot be undone.')){{
          const u=new URL(parent.location.href);
          u.searchParams.set('act','delete');
          u.searchParams.set('id','{cid}');
          parent.location.search=u.search;
        }}
        return false;
      " title="Delete">⌫</a>
    </div>
  </div>
</body></html>""", height=56, scrolling=False)

def render_clients_tab():
    st.subheader("Clients")
    st.caption("Manage active and inactive clients. “test test” is always hidden.")

    # Handle query param actions (toggle/rename/delete) *before* rendering
    act = _qp_get("act",""); cid = _qp_get("id",""); arg = _qp_get("arg",""); report_norm = _qp_get("report","")
    if act and cid:
        try: cid_int = int(cid)
        except Exception: cid_int = 0
        if cid_int:
            if act == "toggle":
                supa = get_supabase()
                cur = (supa.table("clients").select("active").eq("id", cid_int).limit(1).execute().data or [{"active": True}])[0]["active"]
                toggle_client_active(cid_int, (not cur))
            elif act == "rename" and arg:
                rename_client(cid_int, arg)
            elif act == "delete":
                delete_client(cid_int)
        _qp_set(); safe_rerun()

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)
    with colA:
        st.markdown("### Active", unsafe_allow_html=True)
        if not active: st.write("_No active clients_")
        else:
            for c in active: _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=True)

    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive: st.write("_No inactive clients_")
        else:
            for c in inactive: _client_row_icons(c["name"], c.get("name_norm",""), c["id"], active=False)

    # Report render
    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    report_norm_qp = _qp_get("report",""); want_scroll = _qp_get("scroll","") in ("1","true","yes")
    if report_norm_qp:
        display_name = next((c["name"] for c in all_clients if c.get("name_norm")==report_norm_qp), report_norm_qp)
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        if want_scroll:
            components.html("""<script>
              const el = parent.document.getElementById("report_anchor");
              if(el){ el.scrollIntoView({behavior:"smooth", block:"start"}); }
            </script>""", height=0)
