# ui/run_tab.py

import os, io, re, csv, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import streamlit as st
import streamlit.components.v1 as components

# --- Optional deps (same behavior as your original) ---
try:
    import usaddress  # type: ignore
except Exception:
    usaddress = None  # graceful fallback

# --- Services / Utils you already have in the modular layout ---
# Resolver: turn addresses/links into Zillow URLs
from services.resolver import (
    process_single_row,
    resolve_from_source_url,
    upgrade_to_homedetails_if_needed,
    make_preview_url,
)

# Enrichment (async scrape of Zillow page → price/beds/etc.)
from services.enrich import enrich_results_async  # no-op if you keep value=False

# Images: hero image or Street View fallback + tiny log
from services.images import get_thumbnail_and_log

# Supabase helpers (clients, dedupe, logging “sent”)
from services.supabase_helpers import (
    fetch_clients,
    upsert_client,
    get_already_sent_maps,
    mark_duplicates,
    log_sent_rows,
)

# Utility key-sets and small helpers
from utils.address import (
    URL_KEYS,
    MLS_ID_KEYS,
    PHOTO_KEYS,
    is_probable_url,
    get_first_by_keys,
)


# ---------- Tracking / Bitly (kept here so Run tab is self-contained) ----------
BITLY_TOKEN = os.getenv("BITLY_TOKEN", st.secrets.get("BITLY_TOKEN", "")) if hasattr(st, "secrets") else os.getenv("BITLY_TOKEN", "")

def bitly_shorten(long_url: str) -> Optional[str]:
    if not BITLY_TOKEN or not long_url:
        return None
    try:
        r = requests.post(
            "https://api-ssl.bitly.com/v4/shorten",
            headers={"Authorization": f"Bearer {BITLY_TOKEN}", "Content-Type": "application/json"},
            json={"long_url": long_url},
            timeout=10,
        )
        if r.ok:
            return r.json().get("link")
    except Exception:
        return None
    return None

def make_trackable_url(url: str, client_tag: str, campaign_tag: str) -> str:
    """Append a lightweight fragment for tracking (matches your original behavior)."""
    if not url:
        return ""
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9\-]+", "", (s or "").lower().replace(" ", "-"))
    c = _slug(client_tag)
    k = _slug(campaign_tag)
    frag = f"#aa={c}.{k}" if (c or k) else ""
    return url + (frag if frag else "")


# ---------- Output builders (unchanged behavior) ----------
def build_output(rows: List[Dict[str, Any]], fmt: str, use_display: bool = True, include_notes: bool = False):
    """Return (payload, mime) for txt/csv/md/html. HTML shows <li><a>URL</a></li> cleanly."""
    def pick_url(r):
        return r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""

    if fmt == "csv":
        fields = ["input_address","mls_id","url","status","price","beds","baths","sqft","already_sent","dup_reason","dup_sent_at"]
        if include_notes:
            fields += ["summary","highlights","remarks"]
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=fields)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k) for k in fields if k != "url"}
            row["url"] = pick_url(r)
            w.writerow(row)
        return s.getvalue(), "text/csv"

    if fmt == "html":
        items = []
        for r in rows:
            u = pick_url(r)
            if not u:
                continue
            # Keep the anchor text as the URL for best SMS unfurls
            items.append(f'<li><a href="{escape(u)}" target="_blank" rel="noopener">{escape(u)}</a></li>')
        return "<ul>\n" + "\n".join(items) + "\n</ul>\n", "text/html"

    # txt / md (one URL per line)
    lines = []
    for r in rows:
        u = pick_url(r)
        if u:
            lines.append(u)
    payload = "\n".join(lines) + ("\n" if lines else "")
    return payload, ("text/markdown" if fmt == "md" else "text/plain")


# ---------- Tiny helpers ----------
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


# ---------- Main renderer ----------
def render_run_tab(state=None):
    # === Client + Campaign pickers ===
    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    colC, colK = st.columns([1.2, 1])
    with colC:
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]
        options = [NO_CLIENT] + names + [ADD_SENTINEL]
        sel_idx = st.selectbox("Client", list(range(len(options))), format_func=lambda i: options[i], index=0)
        selected_client = None if sel_idx in (0, len(options) - 1) else active_clients[sel_idx - 1]

        if options[sel_idx] == ADD_SENTINEL:
            new_cli = st.text_input("New client name", key="__add_client_name__")
            if st.button("Add client", use_container_width=True, key="__add_client_btn__"):
                ok, msg = upsert_client(new_cli, active=True)
                if ok:
                    st.success("Client added.")
                    st.rerun()
                else:
                    st.error(f"Add failed: {msg}")

        client_tag_raw = (selected_client["name"] if selected_client else "")

    with colK:
        campaign_tag_raw = st.text_input("Campaign tag", value=datetime.utcnow().strftime("%Y%m%d"))

    c1, c2, c3, c4 = st.columns([1, 1, 1.25, 1.45])
    with c1:
        use_shortlinks = st.checkbox(
            "Use short links (Bitly)",
            value=False,
            help="Optional tracking; sharing uses clean Zillow links."
        )
    with c2:
        # Default UNCHECKED per your request
        enrich_details = st.checkbox("Enrich details", value=False)
    with c3:
        show_details = st.checkbox("Show details under results", value=False)
    with c4:
        only_show_new = st.checkbox(
            "Only show NEW for this client",
            value=bool(selected_client),
            help="Hide duplicates. Disabled when 'No client' is selected."
        )
        if not selected_client:
            only_show_new = False

    # Default off to remove dead space; user can enable it
    table_view = st.checkbox("Show results as table", value=False, help="Easier to scan details")

    client_tag = _norm_tag(client_tag_raw)
    campaign_tag = _norm_tag(campaign_tag_raw)

    # === Paste + CSV ===
    st.markdown('<div class="center-box">', unsafe_allow_html=True)
    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area(
        "Paste addresses or links",
        placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524",
        height=160,
        label_visibility="collapsed",
    )

    opt1, opt2, opt3 = st.columns([1.15, 1, 1.2])
    with opt1:
        remove_dupes = st.checkbox("Remove duplicates (pasted)", value=True)
    with opt2:
        trim_spaces = st.checkbox("Auto-trim (pasted)", value=True)
    with opt3:
        show_preview = st.checkbox("Show preview (pasted)", value=True)

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")
    st.markdown("</div>", unsafe_allow_html=True)

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean: List[str] = []
    for ln in lines_raw:
        ln = ln.strip() if trim_spaces else ln
        if not ln:
            continue
        if remove_dupes and ln in lines_clean:
            continue
        if is_probable_url(ln):
            lines_clean.append(ln)
        else:
            # Optional "normalize" via usaddress (if installed)
            if usaddress:
                parts = usaddress.tag(ln)[0]
                norm = (
                    parts.get("AddressNumber", "")
                    + " "
                    + " ".join(
                        [
                            parts.get("StreetNamePreDirectional", ""),
                            parts.get("StreetName", ""),
                            parts.get("StreetNamePostType", ""),
                            parts.get("OccupancyType", ""),
                            parts.get("OccupancyIdentifier", ""),
                        ]
                    ).strip()
                )
                cityst = (
                    (", " + parts.get("PlaceName", "") + ", " + parts.get("StateName", "")
                     + (" " + parts.get("ZipCode", "") if parts.get("ZipCode") else ""))
                    if (parts.get("PlaceName") or parts.get("StateName"))
                    else ""
                )
                lines_clean.append(re.sub(r"\s+", " ", (norm + cityst).strip()))
            else:
                lines_clean.append(ln)

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
    if file is not None:
        bits.append(f"**{csv_count}** CSV")
    st.caption(" • ".join(bits) + "  •  Paste short links or MLS pages too; we’ll resolve them to Zillow.")

    if show_preview and count_pasted:
        st.markdown("**Preview (pasted)** (first 5):")
        st.markdown(
            "<ul class='link-list'>"
            + "\n".join([f"<li>{escape(p)}</li>" for p in lines_clean[:5]])
            + ("<li>…</li>" if count_pasted > 5 else "")
            + "</ul>",
            unsafe_allow_html=True,
        )

    # === RUN ===
    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown("</div>", unsafe_allow_html=True)

    # === Renderers ===
    def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
        """Always show as hyperlinks (URL anchor text) + tight spacing + Copy button."""
        li_html = []
        for r in results:
            href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
            if not href:
                continue
            safe_href = escape(href)
            link_txt = href  # show raw URL for clean SMS/Messages unfurls

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

        # Copy-all uses the same clean URL list (one per line)
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

    def _render_results_and_downloads(
        results: List[Dict[str, Any]],
        client_tag: str,
        campaign_tag: str,
        include_notes: bool,
        client_selected: bool,
    ):
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=client_selected)

        # Table OFF by default to remove dead space (user can enable from the checkbox)
        if table_view:
            import pandas as pd  # local import to avoid global dependency if not used
            cols = [
                "already_sent",
                "dup_reason",
                "dup_sent_at",
                "display_url",
                "zillow_url",
                "preview_url",
                "status",
                "price",
                "beds",
                "baths",
                "sqft",
                "mls_id",
                "input_address",
            ]
            df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
            st.dataframe(df, use_container_width=True, hide_index=True)

        fmt_options = ["txt", "csv", "md", "html"]
        prev_fmt = (st.session_state.get("__results__") or {}).get("fmt")
        default_idx = fmt_options.index(prev_fmt) if prev_fmt in fmt_options else 0
        fmt = st.selectbox("Download format", fmt_options, index=default_idx)
        payload, mime = build_output(results, fmt, use_display=True, include_notes=include_notes)
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        tag = ("_" + re.sub(r"[^a-z0-9\-]+", "", (client_tag or "").lower().replace(" ", "-"))) if client_tag else ""
        st.download_button(
            "Export",
            data=payload,
            file_name=f"address_alchemist{tag}_{ts}.{fmt}",
            mime=mime,
            use_container_width=True,
        )
        st.session_state["__results__"] = {"results": results, "fmt": fmt}

        # Images (clickable → link)
        thumbs = []
        for r in results:
            img = r.get("image_url")
            if not img:
                img, _ = get_thumbnail_and_log(
                    r.get("input_address", ""),
                    r.get("preview_url") or r.get("zillow_url") or "",
                    r.get("csv_photo"),
                )
            if img:
                thumbs.append((r, img))
        if thumbs:
            st.markdown("#### Images")
            cols = st.columns(3)
            for i, (r, img) in enumerate(thumbs):
                with cols[i % 3]:
                    url = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "#"
                    safe_url = escape(url)
                    safe_img = escape(img)
                    # Make the image itself clickable
                    st.markdown(
                        f"<a href='{safe_url}' target='_blank' rel='noopener'>"
                        f"<img src='{safe_img}' alt='Listing image' style='width:100%; border-radius:12px;'/>"
                        f"</a>",
                        unsafe_allow_html=True,
                    )
                    # Optional caption (kept minimal)
                    mls_id = (r.get("mls_id") or "").strip()
                    addr = (r.get("input_address") or "").strip()
                    link_text = escape(addr) if addr else "View listing"
                    st.markdown(
                        f"<div class='img-label' style='margin-top:6px;'>"
                        f"{('<strong>MLS#: '+escape(mls_id)+'</strong><br/>' if mls_id else '')}"
                        f"<a href='{safe_url}' target='_blank' rel='noopener'>{link_text}</a>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )

    # === Click handler ===
    if clicked:
        try:
            rows_in: List[Dict[str, Any]] = []
            csv_rows_count = 0

            # CSV
            if file is not None:
                content = file.getvalue().decode("utf-8-sig")
                reader = list(csv.DictReader(io.StringIO(content)))
                csv_rows_count = len(reader)
                rows_in.extend(reader)

            # Pasted
            for item in lines_clean:
                if is_probable_url(item):
                    rows_in.append({"source_url": item})
                else:
                    rows_in.append({"address": item})

            if not rows_in:
                st.error("Please paste at least one address or link and/or upload a CSV.")
                st.stop()

            defaults = {"city": "", "state": "", "zip": ""}
            total = len(rows_in)
            results: List[Dict[str, Any]] = []

            prog = st.progress(0, text="Resolving to Zillow…")
            for i, row in enumerate(rows_in, start=1):
                url_in = ""
                url_in = url_in or get_first_by_keys(row, URL_KEYS)
                url_in = url_in or row.get("source_url", "")
                if url_in and is_probable_url(url_in):
                    zurl, used_addr = resolve_from_source_url(url_in, defaults)
                    results.append(
                        {
                            "input_address": used_addr or row.get("address", "") or "",
                            "mls_id": get_first_by_keys(row, MLS_ID_KEYS),
                            "zillow_url": zurl,
                            "status": "",
                            "csv_photo": get_first_by_keys(row, PHOTO_KEYS),
                        }
                    )
                else:
                    res = process_single_row(
                        row,
                        delay=0.45,
                        land_mode=True,
                        defaults=defaults,
                        require_state=True,
                        mls_first=True,
                        default_mls_name="",
                        max_candidates=20,
                    )
                    results.append(res)

                prog.progress(i / total, text=f"Resolved {i}/{total}")
            prog.progress(1.0, text="Links resolved")

            # Canonicalize to homedetails where applicable
            for r in results:
                for key in ("zillow_url", "display_url"):
                    if r.get(key):
                        r[key] = upgrade_to_homedetails_if_needed(r[key])

            # Optional enrichment (async)
            if enrich_details:
                st.write("Enriching details (parallel)…")
                results = asyncio.run(enrich_results_async(results))

            # Build final URLs (preview + display/short)
            for r in results:
                base = r.get("zillow_url")
                r["preview_url"] = make_preview_url(base) if base else ""
                display = make_trackable_url(base, client_tag, campaign_tag) if base else base
                if use_shortlinks and display:
                    short = bitly_shorten(display)
                    r["display_url"] = short or display
                else:
                    r["display_url"] = display or base

            # Dedupe + logging (if client selected)
            client_selected = bool(client_tag.strip())
            if client_selected:
                canon_set, zpid_set, canon_info, zpid_info = get_already_sent_maps(client_tag)
                results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info)
                if only_show_new:
                    results = [r for r in results if not r.get("already_sent")]
                if results:
                    ok_log, info_log = log_sent_rows(results, client_tag, campaign_tag)
                    st.success("Logged to Supabase.") if ok_log else st.warning(f"Supabase log skipped/failed: {info_log}")
            else:
                for r in results:
                    r["already_sent"] = False

            st.success(
                f"Processed {len(results)} item(s)"
                + (f" — CSV rows read: {csv_rows_count}" if file is not None else "")
            )

            _render_results_and_downloads(
                results,
                client_tag,
                campaign_tag,
                include_notes=enrich_details,
                client_selected=client_selected,
            )

        except Exception as e:
            st.error("We hit an error while processing.")
            with st.expander("Details"):
                st.exception(e)

    # Re-render last results if present (and user didn’t click Run this time)
    data = st.session_state.get("__results__") or {}
    results = data.get("results") or []
    if results and not clicked:
        _render_results_and_downloads(
            results,
            client_tag,
            campaign_tag,
            include_notes=False,
            client_selected=bool(client_tag.strip()),
        )
    else:
        if not clicked:
            st.info("Paste addresses or links (or upload CSV), then click **Run**.")
