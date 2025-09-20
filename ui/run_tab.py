# ui/run_tab.py
import io, re, csv, asyncio, time
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components

# ---- services & utils imports (aligned with your modular split) ----
from services.resolver import (
    resolve_from_source_url,
    process_single_row,
    upgrade_to_homedetails_if_needed,
    make_preview_url,
)
from services.enrich import enrich_results_async
from services.images import get_thumbnail_and_log
from services.clients import (
    fetch_clients,
    upsert_client,
    get_already_sent_maps,
    mark_duplicates,
    log_sent_rows,
)
from services.tracking import make_trackable_url, bitly_shorten

from utils.address import (
    is_probable_url,
    get_first_by_keys,
    URL_KEYS,
    MLS_ID_KEYS,
    PHOTO_KEYS,
    # optional usaddress re-export
    usaddress,
)


# ---------- small helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass


# ---------- output builders (kept local to Run tab) ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    """
    When sharing links by SMS, we keep anchors as the raw URL (best unfurl).
    """
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft","already_sent","dup_reason","dup_sent_at"]
        if include_notes:
            fields += ["summary","highlights","remarks"]
        s = io.StringIO(); w = csv.DictWriter(s, fieldnames=fields); w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields if k != "url"}
            row["url"] = pick_url(r)
            w.writerow(row)
        return s.getvalue(), "text/csv"

    if fmt == "html":
        items = []
        for r in rows:
            u = pick_url(r)
            if not u: continue
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    # txt / md default to clean URL-per-line
    lines = []
    for r in rows:
        u = pick_url(r)
        if u: lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")


# ---------- Results list (hyperlinks + copy all) ----------
def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href:
            continue
        safe_href = escape(href)
        link_txt = href  # raw URL as anchor text for best SMS unfurls

        badge_html = ""
        if client_selected:
            if r.get("already_sent"):
                tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
                badge_html = f' <span class="badge dup" title="{tip}">Duplicate</span>'
            else:
                badge_html = ' <span class="badge new" title="New for this client">NEW</span>'

        li_html.append(
            f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a>{badge_html}</li>'
        )

    items_html = "\n".join(li_html) if li_html else "<li>(no results)</li>"

    copy_lines = []
    for r in results:
        u = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if u:
            copy_lines.append(u.strip())
    copy_text = "\\n".join(copy_lines) + ("\\n" if copy_lines else "")

    html = f"""
    <html><head><meta charset="utf-8" />
      <style>
        html,body {{ margin:0; font-family:-apple-system, Segoe UI, Roboto, Arial, sans-serif; }}
        .results-wrap {{ position:relative; box-sizing:border-box; padding:8px 120px 4px 0; }}
        ul.link-list {{ margin:0 0 0.2rem 1.2rem; padding:0; list-style:disc; }}
        ul.link-list li {{ margin:0.2rem 0; }}
        .copyall-btn {{ position:absolute; top:0; right:8px; z-index:5; padding:6px 10px; height:26px; border:0; border-radius:10px; color:#fff; font-weight:700; background:#1d4ed8; cursor:pointer; opacity:.95; }}
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


# ---------- render_run_tab ----------
def render_run_tab(state=None):
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    colC, colK = st.columns([1.2, 1])
    with colC:
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]
        options = [NO_CLIENT] + names + [ADD_SENTINEL]
        sel_idx = st.selectbox("Client", list(range(len(options))), format_func=lambda i: options[i], index=0, key="__client_select__")
        selected_client = None if sel_idx in (0, len(options)-1) else active_clients[sel_idx-1]

        if options[sel_idx] == ADD_SENTINEL:
            new_cli = st.text_input("New client name", key="__add_client_name__")
            if st.button("Add client", use_container_width=True, key="__add_client_btn__"):
                ok, msg = upsert_client(new_cli, active=True)
                if ok:
                    st.success("Client added.")
                    _safe_rerun()
                else:
                    st.error(f"Add failed: {msg}")

        client_tag_raw = (selected_client["name"] if selected_client else "")
    with colK:
        campaign_tag_raw = st.text_input("Campaign tag", value=datetime.utcnow().strftime("%Y%m%d"))

    c1, c2, c3, c4 = st.columns([1,1,1.25,1.45])
    with c1:
        use_shortlinks = st.checkbox("Use short links (Bitly)", value=False, help="Optional tracking; sharing uses clean Zillow links.")
    with c2:
        enrich_details = st.checkbox("Enrich details", value=False)  # default unchecked per your spec
    with c3:
        show_details = st.checkbox("Show details under results", value=False)
    with c4:
        only_show_new = st.checkbox(
            "Only show NEW for this client",
            value=bool(selected_client),
            help="Hide duplicates. Disabled when 'No client' is selected.",
            key="__only_new__"
        )
        if not selected_client:
            only_show_new = False

    # Default: hide table (remove dead space)
    table_view = st.checkbox("Show results as table", value=False, help="Easier to scan details", key="__table_view__")

    client_tag = _norm_tag(client_tag_raw)
    campaign_tag = _norm_tag(campaign_tag_raw)

    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area(
        "Paste addresses or links",
        placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524",
        height=160,
        label_visibility="collapsed",
        key="__paste__",
    )
    opt1, opt2, opt3 = st.columns([1.15, 1, 1.2])
    with opt1:
        remove_dupes = st.checkbox("Remove duplicates (pasted)", value=True, key="__rm_dupes__")
    with opt2:
        trim_spaces = st.checkbox("Auto-trim (pasted)", value=True, key="__trim__")
    with opt3:
        show_preview = st.checkbox("Show preview (pasted)", value=True, key="__preview__")

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed", key="__uploader__")
    st.markdown('</div>', unsafe_allow_html=True)

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean = []
    for ln in lines_raw:
        ln0 = ln.strip() if trim_spaces else ln
        if not ln0:
            continue
        if remove_dupes and ln0 in lines_clean:
            continue
        if is_probable_url(ln0):
            lines_clean.append(ln0)
        else:
            if usaddress:
                try:
                    parts = usaddress.tag(ln0)[0]
                    norm = (parts.get("AddressNumber","") + " " +
                            " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())
                    cityst = ((", " + parts.get("PlaceName","") + ", " + parts.get("StateName","") +
                              (" " + parts.get("ZipCode","") if parts.get("ZipCode") else "")) if (parts.get("PlaceName") or parts.get("StateName")) else "")
                    lines_clean.append(re.sub(r"\s+"," ", (norm + cityst).strip()))
                except Exception:
                    lines_clean.append(ln0)
            else:
                lines_clean.append(ln0)

    count_pasted = len(lines_clean)
    csv_count = 0
    if file is not None:
        try:
            content_peek = file.getvalue().decode("utf-8-sig")
            csv_reader = csv.DictReader(io.StringIO(content_peek))
            csv_count = sum(1 for _ in csv_reader)
        except Exception:
            csv_count = 0

    bits = [f"**{count_pasted}** pasted"]
    if file is not None: bits.append(f"**{csv_count}** CSV")
    st.caption(" • ".join(bits) + "  •  Paste short links or MLS pages too; we’ll resolve them to Zillow.")

    if show_preview and count_pasted:
        st.markdown("**Preview (pasted)** (first 5):")
        st.markdown(
            "<ul class='link-list'>" +
            "\n".join([f"<li>{escape(p)}</li>" for p in lines_clean[:5]]) +
            ("<li>…</li>" if count_pasted > 5 else "") +
            "</ul>",
            unsafe_allow_html=True
        )

    # POPPY RUN BUTTON
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown('</div>', unsafe_allow_html=True)

    def _render_results_and_downloads(results: List[Dict[str, Any]], client_tag: str, campaign_tag: str, include_notes: bool, client_selected: bool):
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=client_selected)

        # Table removed by default (no extra vertical space); toggle if needed.
        if table_view:
            import pandas as pd
            cols = ["already_sent","dup_reason","dup_sent_at","display_url","zillow_url","preview_url","status","price","beds","baths","sqft","mls_id","input_address"]
            df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
            st.dataframe(df, use_container_width=True, hide_index=True)

        fmt_options = ["txt","csv","md","html"]
        prev_fmt = (st.session_state.get("__results__") or {}).get("fmt")
        default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
        fmt = st.selectbox("Download format", fmt_options, index=default_idx, key="__fmt__")
        payload, mime = build_output(results, fmt, use_display=True, include_notes=include_notes)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        tag = ("_" + re.sub(r'[^a-z0-9\-]+','', (client_tag or "").lower().replace(" ","-"))) if client_tag else ""
        st.download_button("Export", data=payload, file_name=f"address_alchemist{tag}_{ts}.{fmt}", mime=mime, use_container_width=True, key="__export__")
        st.session_state["__results__"] = {"results": results, "fmt": fmt}

        # Thumbs (kept)
        thumbs=[]
        for r in results:
            img = r.get("image_url")
            if not img:
                img, _ = get_thumbnail_and_log(r.get("input_address",""), r.get("preview_url") or r.get("zillow_url") or "", r.get("csv_photo"))
            if img: thumbs.append((r,img))
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i,(r,img) in enumerate(thumbs):
                with cols[i%3]:
                    st.image(img, use_container_width=True)
                    mls_id = (r.get("mls_id") or "").strip()
                    addr = (r.get("input_address") or "").strip()
                    url = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "#"
                    link_text = escape(addr) if addr else "View listing"
                    st.markdown(
                        f"<div class='img-label'>{('<strong>MLS#: '+escape(mls_id)+'</strong><br/>' if mls_id else '')}<a href='{escape(url)}' target='_blank' rel='noopener'>{link_text}</a></div>",
                        unsafe_allow_html=True
                    )

    if clicked:
        try:
            rows_in: List[Dict[str, Any]] = []
            csv_rows_count = 0
            if file is not None:
                content = file.getvalue().decode("utf-8-sig")
                reader = list(csv.DictReader(io.StringIO(content)))
                csv_rows_count = len(reader)
                rows_in.extend(reader)
            for item in lines_clean:
                if is_probable_url(item):
                    rows_in.append({"source_url": item})
                else:
                    rows_in.append({"address": item})

            if not rows_in:
                st.error("Please paste at least one address or link and/or upload a CSV.")
                st.stop()

            defaults = {"city":"", "state":"", "zip":""}
            total = len(rows_in)
            results: List[Dict[str, Any]] = []

            prog = st.progress(0, text="Resolving to Zillow…")
            for i, row in enumerate(rows_in, start=1):
                url_in = ""
                url_in = url_in or get_first_by_keys(row, URL_KEYS)
                url_in = url_in or row.get("source_url","")
                if url_in and is_probable_url(url_in):
                    zurl, used_addr = resolve_from_source_url(url_in, defaults)
                    results.append({
                        "input_address": used_addr or row.get("address","") or "",
                        "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
                        "zillow_url": zurl,
                        "status": "",
                        "csv_photo": get_first_by_keys(row, PHOTO_KEYS)
                    })
                else:
                    res = process_single_row(row, delay=0.45, land_mode=True, defaults=defaults,
                                             require_state=True, mls_first=True, default_mls_name="", max_candidates=20)
                    # also carry possible CSV photo col
                    res["csv_photo"] = get_first_by_keys(row, PHOTO_KEYS)
                    results.append(res)
                prog.progress(i/total, text=f"Resolved {i}/{total}")
            prog.progress(1.0, text="Links resolved")

            # Normalize to /homedetails/ URLs where possible
            for r in results:
                for key in ("zillow_url","display_url"):
                    if r.get(key):
                        r[key] = upgrade_to_homedetails_if_needed(r[key])

            if enrich_details:
                st.write("Enriching details (parallel)…")
                results = asyncio.run(enrich_results_async(results))

            # Build display/preview URLs + optional short links
            client_tag_norm = _norm_tag(client_tag)
            campaign_tag_norm = _norm_tag(campaign_tag)
            for r in results:
                base = r.get("zillow_url")
                r["preview_url"] = make_preview_url(base) if base else ""
                display = make_trackable_url(base, client_tag_norm, campaign_tag_norm) if base else base
                if use_shortlinks and display:
                    short = bitly_shorten(display)
                    r["display_url"] = short or display
                else:
                    r["display_url"] = display or base

            client_selected = bool(client_tag_norm.strip())
            if client_selected:
                canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(client_tag_norm)
                results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info)
                if only_show_new:
                    results = [r for r in results if not r.get("already_sent")]
                if results:
                    ok_log, info_log = log_sent_rows(results, client_tag_norm, campaign_tag_norm)
                    st.success("Logged to Supabase.") if ok_log else st.warning(f"Supabase log skipped/failed: {info_log}")
            else:
                for r in results:
                    r["already_sent"] = False

            st.success(f"Processed {len(results)} item(s)" + (f" — CSV rows read: {csv_count}" if file is not None else ""))

            _render_results_and_downloads(results, client_tag_norm, campaign_tag_norm, include_notes=enrich_details, client_selected=client_selected)

        except Exception as e:
            st.error("We hit an error while processing.")
            with st.expander("Details"): st.exception(e)

    # Restore previous results on first paint (if present)
    data = st.session_state.get("__results__") or {}
    results = data.get("results") or []
    if results and not clicked:
        _render_results_and_downloads(results, client_tag, campaign_tag, include_notes=False, client_selected=bool(client_tag.strip()))
    else:
        if not clicked:
            st.info("Paste addresses or links (or upload CSV), then click **Run**.")
