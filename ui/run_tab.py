# ui/run_tab.py
# Address Alchemist — Run tab
# - Shows "Toured" badge when a result matches any toured address for the selected client
# - Prevents duplicate logging (case-insensitive canonical) for the selected client
# - Removed the "NEW" badge per request
# - If there are no results to render, shows a visible "No results returned." message

import os, csv, io, re, time, json, asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from html import escape

import requests
import httpx
import streamlit as st
import streamlit.components.v1 as components

# ---------- Optional deps ----------
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
    try: return bool(SUPABASE)
    except NameError: return False

# ---------- Helpers ----------
REQUEST_TIMEOUT = 12
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

URL_KEYS = {"url","link","source url","source_url","listing url","listing_url","property url","property_url","href"}
def is_probable_url(s: str) -> bool:
    s = (s or "").strip()
    return s.startswith("http://") or s.startswith("https://") or re.match(r"^[a-z]+://", s) is not None

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def canonicalize_zillow(url: str) -> Tuple[str, Optional[str]]:
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = re.search(r'(\d{6,})_zpid', url, re.I)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    if not url:
        return ""
    base = re.sub(r'[?#].*$', '', url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base

def expand_url_and_fetch_html(url: str) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url: return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok: return url
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', r.text)
        return m.group(1) if m else url
    except Exception:
        return url

# ---------- Address parsing keys ----------
ADDR_PRIMARY = {"full_address","address","property address","property_address","site address","site_address",
                "street address","street_address","listing address","listing_address","location"}
NUM_KEYS   = {"street #","street number","street_no","streetnum","house_number","number","streetnumber"}
NAME_KEYS  = {"street name","street","st name","st_name","road","rd","avenue","ave","blvd","boulevard",
              "drive","dr","lane","ln","way","terrace","ter","court","ct","place","pl","parkway","pkwy",
              "square","sq","circle","cir","highway","hwy","route","rt"}
SUF_KEYS   = {"suffix","st suffix","street suffix","suffix1","suffix2","street_type","street type"}
CITY_KEYS  = {"city","municipality","town"}
STATE_KEYS = {"state","st","province","region"}
ZIP_KEYS   = {"zip","zip code","postal code","postalcode","zip_code","postal_code"}
MLS_ID_KEYS   = {"mls","mls id","mls_id","mls #","mls#","mls number","mlsnumber","listing id","listing_id"}
MLS_NAME_KEYS = {"mls name","mls board","mls provider","source","source mls","mls source"}
PHOTO_KEYS = {"photo","image","photo url","image url","picture","thumbnail","thumb","img","img url","img_url"}

def norm_key(k:str) -> str: return re.sub(r"\s+"," ", (k or "").strip().lower())
def get_first_by_keys(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v = str(row[k]).strip()
            if v: return v
    return ""

def extract_components(row):
    n = { norm_key(k): (str(v).strip() if v is not None else "") for k,v in row.items() }
    for k in n.keys():
        if k in ADDR_PRIMARY and n[k]:
            return {"street_raw": n[k], "city":"", "state":"", "zip":"",
                    "mls_id": get_first_by_keys(n, MLS_ID_KEYS),
                    "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)}
    num   = get_first_by_keys(n, NUM_KEYS)
    name  = get_first_by_keys(n, NAME_KEYS)
    suf   = get_first_by_keys(n, SUF_KEYS)
    city  = get_first_by_keys(n, CITY_KEYS)
    state = get_first_by_keys(n, STATE_KEYS)
    zipc  = get_first_by_keys(n, ZIP_KEYS)
    street_raw = " ".join([x for x in [num,name,suf] if x]).strip()
    return {"street_raw": street_raw, "city": city, "state": state, "zip": zipc,
            "mls_id": get_first_by_keys(n, MLS_ID_KEYS), "mls_name": get_first_by_keys(n, MLS_NAME_KEYS)}

# ---------- Strong address normalization ----------
SUFFIX_MAP = {
    "st":"street","street":"street","rd":"road","road":"road","ave":"avenue","av":"avenue","avenue":"avenue",
    "blvd":"boulevard","boulevard":"boulevard","dr":"drive","drive":"drive","ln":"lane","lane":"lane",
    "ct":"court","court":"court","pl":"place","place":"place","ter":"terrace","terrace":"terrace",
    "hwy":"highway","highway":"highway","cir":"circle","circle":"circle","pkwy":"parkway","parkway":"parkway",
    "sq":"square","square":"square",
}
DIR_MAP = {"n":"north","s":"south","e":"east","w":"west","north":"north","south":"south","east":"east","west":"west"}

def _slug_addr_strong(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^\w\s,/-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[\s,/]+", "-", s)

def _normalize_address_strong(addr: str) -> str:
    s = (addr or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[^\w\s,/-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.replace(",", " , ").split()
    out = []
    for t in toks:
        if t == ",":
            out.append(","); continue
        if t in DIR_MAP:
            out.append(DIR_MAP[t]); continue
        if t in SUFFIX_MAP:
            out.append(SUFFIX_MAP[t]); continue
        out.append(t)
    s2 = " ".join(out)
    s2 = re.sub(r"\s*,\s*", ", ", s2)
    s2 = re.sub(r"\s+", " ", s2).strip()
    return _slug_addr_strong(s2)

def _address_from_zillow_url(url: str) -> str:
    u = (url or "")
    m = re.search(r"/homedetails/([^/]+)/\d{6,}_zpid/", u, re.I)
    if m: return m.group(1).replace("-", " ")
    m = re.search(r"/homes/([^/_]+)_rb/?", u, re.I)
    if m: return m.group(1).replace("-", " ")
    return ""

def _property_key_from_result(r: Dict[str, Any]) -> str:
    addr = (r.get("input_address") or r.get("address") or "").strip()
    if not addr:
        addr = _address_from_zillow_url(r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "")
    return _normalize_address_strong(addr)

# ---------- Resolve helpers (minimal) ----------
def compose_query_address(street, city, state, zipc, defaults):
    parts = [street]
    c  = (city  or defaults.get("city","")).strip()
    stt = (state or defaults.get("state","")).strip()
    z  = (zipc  or defaults.get("zip","")).strip()
    if c: parts.append(c)
    if stt: parts.append(stt)
    if z: parts.append(z)
    return " ".join([p for p in parts if p]).strip()

def construct_deeplink_from_parts(street, city, state, zipc, defaults):
    c = (city or defaults.get("city","")).strip()
    st_abbr = (state or defaults.get("state","")).strip()
    z = (zipc  or defaults.get("zip","")).strip()
    slug_parts = [street]; loc_parts = [p for p in [c, st_abbr] if p]
    if loc_parts: slug_parts.append(", ".join(loc_parts))
    if z:
        if slug_parts: slug_parts[-1] = f"{slug_parts[-1]} {z}"
        else: slug_parts.append(z)
    slug = ", ".join(slug_parts)
    a = slug.lower(); a = re.sub(r"[^\w\s,-]", "", a).replace(",", ""); a = re.sub(r"\s+", "-", a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

def resolve_from_source_url(source_url: str, defaults: Dict[str,str]) -> Tuple[str, str]:
    final_url, html, _ = expand_url_and_fetch_html(source_url)
    if "/homedetails/" in final_url:
        return final_url, ""
    title = ""
    m = re.search(r"<title>\s*([^<]+?)\s*</title>", html, re.I)
    if m: title = re.sub(r"\s+", " ", m.group(1)).strip()
    return construct_deeplink_from_parts(title or "", "", "", "", defaults), title

def process_single_row(row, *, delay=0.45, defaults=None):
    defaults = defaults or {"city":"", "state":"", "zip":""}
    comp = extract_components(row)
    street_raw = comp["street_raw"]
    query_address = compose_query_address(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    deeplink = construct_deeplink_from_parts(street_raw, comp["city"], comp["state"], comp["zip"], defaults)
    time.sleep(min(delay, 0.4))
    return {"input_address": query_address, "mls_id": comp.get("mls_id") or "", "zillow_url": deeplink, "status": "", "csv_photo": get_first_by_keys(row, PHOTO_KEYS)}

# ---------- Images (minimal) ----------
def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html: return None
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None

def picture_for_result_with_log(query_address: str, zurl: str, csv_photo_url: Optional[str] = None):
    def _ok(u:str)->bool: return isinstance(u,str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))
    if csv_photo_url and _ok(csv_photo_url):
        return csv_photo_url, {}
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
            if r.ok:
                zfirst=extract_zillow_first_image(r.text)
                if zfirst: return zfirst, {}
        except Exception:
            pass
    return None, {}

@st.cache_data(ttl=900, show_spinner=False)
def get_thumbnail_and_log(query_address: str, zurl: str, csv_photo_url: Optional[str]):
    return picture_for_result_with_log(query_address, zurl, csv_photo_url)

# ---------- Clients registry ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def upsert_client(name: str, active: bool = True, notes: str = None):
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        name_norm = _norm_tag(name)
        payload = {"name": name.strip(), "name_norm": name_norm, "active": active}
        if notes is not None: payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        try: fetch_clients.clear()
        except Exception: pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Tours cross-check ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_all_tour_addresses_for_client(client_norm: str) -> List[str]:
    if not (_sb_ok() and client_norm.strip()):
        return []
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm.strip()).limit(5000).execute()
        tours = tq.data or []
        if not tours: return []
        ids = [t["id"] for t in tours]
        if not ids: return []
        sq = SUPABASE.table("tour_stops").select("address").in_("tour_id", ids).limit(50000).execute()
        stops = sq.data or []
        return [s.get("address") or "" for s in stops if (s.get("address") or "").strip()]
    except Exception:
        return []

def build_toured_keyset(client_norm: str) -> set:
    addrs = fetch_all_tour_addresses_for_client(client_norm)
    return { _normalize_address_strong(a) for a in addrs if a.strip() }

# ---------- Sent lookups ----------
@st.cache_data(ttl=300, show_spinner=False)
def get_already_sent_maps(client_tag: str):
    """
    Returns:
      canon_set (canonical urls),
      zpid_set,
      canon_info,
      zpid_info,
      canon_lower_set (lowercase canonicals for case-insensitive dedupe),
    """
    if not (_sb_ok() and client_tag.strip()):
        return set(), set(), {}, {}, set()
    try:
        rows = SUPABASE.table("sent").select("canonical,zpid,url,sent_at").eq("client", client_tag.strip()).limit(20000).execute().data or []
        canon_set = { (r.get("canonical") or "").strip() for r in rows if r.get("canonical") }
        zpid_set  = { (r.get("zpid") or "").strip() for r in rows if r.get("zpid") }
        canon_lower_set = { c.lower() for c in canon_set if c }
        canon_info: Dict[str, Dict[str,str]] = {}
        zpid_info:  Dict[str, Dict[str,str]] = {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info: canon_info[c] = info
            if z and z not in zpid_info:  zpid_info[z]  = info
        return canon_set, zpid_set, canon_info, zpid_info, canon_lower_set
    except Exception:
        return set(), set(), {}, {}, set()

def mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info, toured_keys: set):
    for r in results:
        url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not url:
            r["already_sent"] = False
        else:
            canon, zpid = canonicalize_zillow(url)
            reason = None; sent_when = ""; sent_url = ""
            if canon and canon in canon_set:
                reason = "canonical"; meta = canon_info.get(canon, {})
                sent_when, sent_url = meta.get("sent_at",""), meta.get("url","")
            elif zpid and zpid in zpid_set:
                reason = "zpid"; meta = zpid_info.get(zpid, {})
                sent_when, sent_url = meta.get("sent_at",""), meta.get("url","")
            r["canonical"] = canon
            r["zpid"] = zpid
            r["already_sent"] = bool(reason)
            r["dup_reason"] = reason
            r["dup_sent_at"] = sent_when
            r["dup_original_url"] = sent_url

        # Cross-check vs Tours
        pkey = _property_key_from_result(r)
        r["was_toured"] = bool(pkey and pkey in toured_keys)
    return results

def log_sent_rows(results: List[Dict[str, Any]], client_tag: str, campaign_tag: str, canon_lower_already: set):
    """
    Insert only NEW canonicals (case-insensitive) for this client.
    Skips anything whose canonical (lower) is already present.
    """
    if not SUPABASE or not results:
        return False, "Supabase not configured or no results."

    rows = []
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    seen_lower = set(canon_lower_already)

    for r in results:
        raw_url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not raw_url:
            continue
        canon = (r.get("canonical") or "").strip()
        zpid  = (r.get("zpid") or "").strip()
        if not (canon and zpid):
            canon2, zpid2 = canonicalize_zillow(raw_url)
            canon = canon or canon2
            zpid  = zpid  or zpid2

        canon_l = (canon or "").lower()
        if not canon_l or canon_l in seen_lower:
            continue  # skip duplicates

        rows.append({
            "client":     (client_tag or "").strip(),
            "campaign":   (campaign_tag or "").strip(),
            "url":        raw_url,
            "canonical":  canon,
            "zpid":       zpid,
            "mls_id":     (r.get("mls_id") or "").strip() or None,
            "address":    (r.get("input_address") or "").strip() or None,
            "sent_at":    now_iso,
        })
        seen_lower.add(canon_l)

    if not rows:
        return False, "No NEW rows to log."

    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, f"Inserted {len(rows)} new row(s)."
    except Exception as e:
        return False, str(e)

# ---------- UI: results list with Copy + badges (Toured + Duplicate only) ----------
def results_list_with_copy_all(results: List[Dict[str, Any]], client_selected: bool):
    # Build list items
    li_html = []
    for r in results:
        href = r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or ""
        if not href:
            continue
        safe_href = escape(href)
        link_txt = href  # keep raw URL text for clean SMS unfurls

        badge_parts = []
        if client_selected and r.get("already_sent"):
            tip = f"Duplicate ({escape(r.get('dup_reason','') or '-')}); sent {escape(r.get('dup_sent_at') or '-')}"
            badge_parts.append(f'<span class="badge dup" title="{tip}">Duplicate</span>')

        if r.get("was_toured"):
            badge_parts.append('<span class="badge" style="background:#0b7f48;color:#ecfdf5;border:1px solid #16a34a;">Toured</span>')

        badges_html = ("".join(badge_parts)) if badge_parts else ""
        li_html.append(f'<li style="margin:0.2rem 0;"><a href="{safe_href}" target="_blank" rel="noopener">{escape(link_txt)}</a>{badges_html}</li>')

    # If nothing to render, show a visible message and stop
    has_any = any(r.get("preview_url") or r.get("zillow_url") or r.get("display_url") for r in results)
    if not has_any:
        st.warning("No results returned.")
        return

    items_html = "\n".join(li_html) if li_html else "<li>(no rows)</li>"

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
        .badge {{ display:inline-block; font-size:12px; font-weight:800; padding:2px 8px; border-radius:999px; margin-left:8px; }}
        .badge.dup {{ background:#fee2e2; color:#991b1b; }}
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

# ---------- Main renderer ----------
def render_run_tab(state: dict):
    st.markdown("### Run")

    NO_CLIENT = "➤ No client (show ALL, no logging)"
    ADD_SENTINEL = "➕ Add new client…"

    colC, colK = st.columns([1.2, 1])
    with colC:
        active_clients = fetch_clients(include_inactive=False)
        names = [c["name"] for c in active_clients]
        options = [NO_CLIENT] + names + [ADD_SENTINEL]
        sel_idx = st.selectbox("Client", list(range(len(options))), format_func=lambda i: options[i], index=0, key="__run_client_pick__")
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
        use_shortlinks = st.checkbox("Use short links (Bitly)", value=False)
    with c2:
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

    table_view = st.checkbox("Show results as table", value=False, help="Easier to scan details")

    client_tag = _norm_tag(client_tag_raw)
    campaign_tag = _norm_tag(campaign_tag_raw)

    st.markdown("**Paste addresses or links** (one per line) _and/or_ **drop a CSV**")
    paste = st.text_area(
        "Paste addresses or links",
        placeholder="407 E Woodall St, Smithfield, NC 27577\nhttps://l.hms.pt/...\n123 US-301 S, Four Oaks, NC 27524",
        height=160, label_visibility="collapsed"
    )
    opt1, opt2, opt3 = st.columns([1.15, 1, 1.2])
    with opt1:
        remove_dupes = st.checkbox("Remove duplicates (pasted)", value=True)
    with opt2:
        trim_spaces = st.checkbox("Auto-trim (pasted)", value=True)
    with opt3:
        show_preview = st.checkbox("Show preview (pasted)", value=True)

    file = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")

    # Parse pasted
    lines_raw = (paste or "").splitlines()
    lines_clean = []
    for ln in lines_raw:
        ln = ln.strip() if trim_spaces else ln
        if not ln: continue
        if remove_dupes and ln in lines_clean: continue
        if is_probable_url(ln):
            lines_clean.append(ln)
        else:
            if usaddress:
                try:
                    parts = usaddress.tag(ln)[0]
                    norm = (parts.get("AddressNumber","") + " " +
                            " ".join([parts.get(k,"") for k in ["StreetNamePreDirectional","StreetName","StreetNamePostType","OccupancyType","OccupancyIdentifier"]]).strip())
                    cityst = ((", " + parts.get("PlaceName","") + ", " + parts.get("StateName","") +
                               (" " + parts.get("ZipCode","") if parts.get("ZipCode") else "")) if (parts.get("PlaceName") or parts.get("StateName")) else "")
                    lines_clean.append(re.sub(r"\s+"," ", (norm + cityst).strip()))
                except Exception:
                    lines_clean.append(ln)
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
    if file is not None: bits.append(f"**{csv_count}** CSV")
    st.caption(" • ".join(bits) + "  •  Paste short links or MLS pages too; we’ll resolve them to Zillow.")

    if show_preview and count_pasted:
        st.markdown("**Preview (pasted)** (first 5):")
        st.markdown("<ul class='link-list'>" + "\n".join([f"<li>{escape(p)}</li>" for p in lines_clean[:5]]) + ("<li>…</li>" if count_pasted > 5 else "") + "</ul>", unsafe_allow_html=True)

    st.markdown('<div class="run-zone">', unsafe_allow_html=True)
    clicked = st.button("🚀 Run", use_container_width=True, key="__run_btn__")
    st.markdown('</div>', unsafe_allow_html=True)

    # ---------- On click ----------
    if clicked:
        try:
            rows_in: List[Dict[str, Any]] = []
            if file is not None:
                content = file.getvalue().decode("utf-8-sig")
                reader = list(csv.DictReader(io.StringIO(content)))
                rows_in.extend(reader)
            for item in lines_clean:
                if is_probable_url(item):
                    rows_in.append({"source_url": item})
                else:
                    rows_in.append({"address": item})

            if not rows_in:
                st.warning("No results returned.")
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
                    res = process_single_row(row, delay=0.45, defaults=defaults)
                    results.append(res)
                prog.progress(i/total, text=f"Resolved {i}/{total}")
            prog.progress(1.0, text="Links resolved")

            # Upgrade to homedetails when possible
            for r in results:
                for key in ("zillow_url","display_url"):
                    if r.get(key):
                        r[key] = upgrade_to_homedetails_if_needed(r[key])

            # Prepare display / preview URLs
            for r in results:
                base = r.get("zillow_url")
                r["preview_url"] = make_preview_url(base) if base else ""
                r["display_url"] = base

            client_selected = bool(client_tag.strip())
            toured_keys = build_toured_keyset(client_tag) if client_selected else set()

            if client_selected:
                canon_set, zpid_set, canon_info, zpid_info, canon_lower_set = get_already_sent_maps(client_tag)
                results = mark_duplicates(results, canon_set, zpid_set, canon_info, zpid_info, toured_keys)

                # DB safety: do not attempt to insert dupes
                results_for_db = [r for r in results if not r.get("already_sent")]

                if only_show_new:
                    results = results_for_db

                if SUPABASE and results_for_db:
                    ok_log, info_log = log_sent_rows(results_for_db, client_tag, campaign_tag, canon_lower_set)
                    st.success(info_log) if ok_log else st.warning(f"Supabase log skipped/failed: {info_log}")
                else:
                    if not results_for_db:
                        st.info("Nothing new to log for this client.")
            else:
                for r in results:
                    r["already_sent"] = False
                    r["was_toured"] = False

            # If nothing to show after filtering, clearly say so
            if not any(r.get("preview_url") or r.get("zillow_url") or r.get("display_url") for r in results):
                st.warning("No results returned.")
                st.session_state["__results__"] = {"results": [], "fmt": "txt"}
                return

            st.success(f"Processed {len(results)} item(s)")

            # ---------- Render results ----------
            st.markdown("#### Results")
            results_list_with_copy_all(results, client_selected=client_selected)

            if table_view:
                import pandas as pd
                cols = ["already_sent","was_toured","dup_reason","dup_sent_at","display_url","zillow_url","preview_url","status","mls_id","input_address"]
                df = pd.DataFrame([{c: r.get(c) for c in cols} for r in results])
                st.dataframe(df, use_container_width=True, hide_index=True)

            # Persist
            st.session_state["__results__"] = {"results": results, "fmt": "txt"}

        except Exception as e:
            st.error("We hit an error while processing.")
            with st.expander("Details"): st.exception(e)

    # Re-show last results if present
    data = st.session_state.get("__results__") or {}
    results = data.get("results") or []
    if results and not clicked:
        st.markdown("#### Results")
        results_list_with_copy_all(results, client_selected=False)
    elif not clicked:
        st.warning("No results returned.")
