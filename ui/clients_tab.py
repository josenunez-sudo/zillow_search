# ui/clients_tab.py
# -*- coding: utf-8 -*-
import os
import re
import io
from datetime import datetime
from html import escape
from typing import List, Dict, Any, Optional, Tuple

import streamlit as st

# --- Safe supabase import (module loads even if supabase is not installed) ---
try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = object  # type: ignore

DEBUG_REPORT = False  # set True if you want to see per-row debug info

# ================= Basics =================
def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def _fmt_ts_date_tag(ts: Any) -> str:
    """Return YYYYMMDD from epoch/ISO-ish strings; '' if cannot parse."""
    if ts is None or str(ts).strip() == "":
        return ""
    raw = str(ts).strip()
    try:
        if raw.isdigit():
            val = int(raw)
            if val > 10_000_000_000:  # ms
                d = datetime.utcfromtimestamp(val / 1000.0)
            else:
                d = datetime.utcfromtimestamp(val)
            return d.strftime("%Y%m%d")
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d.strftime("%Y%m%d")
    except Exception:
        return ""

def _pick_timestamp_date_tag(row: Dict[str, Any]) -> str:
    """
    Find first timestamp-like field and convert to YYYYMMDD.
    If none found, try campaign if it looks like YYYYMMDD.
    """
    candidates = [
        "sent_at", "sentAt",
        "created_at", "createdAt",
        "inserted_at", "insertedAt",
        "updated_at", "updatedAt",
        "ts", "timestamp"
    ]
    for k in candidates:
        tag = _fmt_ts_date_tag(row.get(k))
        if tag:
            return tag
    camp = (row.get("campaign") or "").strip()
    if re.fullmatch(r"\d{8}", camp):
        return camp
    return ""

# ------------- Property/address normalization -------------
_STTYPE = {
    "street":"st","st":"st","st.":"st",
    "avenue":"ave","ave":"ave","ave.":"ave","av":"ave","av.":"ave",
    "road":"rd","rd":"rd","rd.":"rd",
    "drive":"dr","dr":"dr","dr.":"dr",
    "lane":"ln","ln":"ln","ln.":"ln",
    "boulevard":"blvd","blvd":"blvd","blvd.":"blvd",
    "court":"ct","ct":"ct","ct.":"ct",
    "place":"pl","pl":"pl","pl.":"pl",
    "terrace":"ter","ter":"ter","ter.":"ter",
    "highway":"hwy","hwy":"hwy","hwy.":"hwy",
    "parkway":"pkwy","pkwy":"pkwy","pkwy.":"pkwy",
    "circle":"cir","cir":"cir","cir.":"cir",
    "square":"sq","sq":"sq","sq.":"sq",
    "driveway":"dr","way":"wy","wy":"wy","wy.":"wy"
}
_DIR = {
    "north":"n","n":"n","south":"s","s":"s","east":"e","e":"e","west":"w","w":"w",
    "n.":"n","s.":"s","e.":"e","w.":"w"
}

# Normalize frequent city variants (extend as needed)
_CITY_ALIASES = {
    "fuquay-varina": "fuquay varina",
    "fuquay  varina": "fuquay varina",
    "fuquay  - varina": "fuquay varina",
}

_SMALL_WORDS = {"of", "and", "the", "at", "in", "on"}

def _title_keep_small_words(words: List[str]) -> str:
    out = []
    for i, w in enumerate(words):
        wl = w.lower()
        if i > 0 and wl in _SMALL_WORDS:
            out.append(wl)
        else:
            out.append(w[:1].upper() + w[1:].lower())
    return " ".join(out)

def _token_norm(tok: str) -> str:
    t = tok.lower().strip(" .,#")
    if t in _STTYPE: return _STTYPE[t]
    if t in _DIR:    return _DIR[t]
    if t in {"apt","unit","ste","suite","lot","#"}: return ""
    return re.sub(r"[^a-z0-9-]", "", t)

def _norm_slug_from_text(text: str) -> str:
    s = (text or "").lower().replace("&", " and ")
    toks = re.split(r"[^a-z0-9]+", s)
    norm = [t for t in (_token_norm(t) for t in toks) if t]
    return "-".join(norm)

_RE_HD = re.compile(r"/homedetails/([^/]+)/\d{6,}_zpid/?", re.I)
_RE_HM = re.compile(r"/homes/([^/_]+)_rb/?", re.I)

def _norm_slug_from_url(url: str) -> str:
    u = (url or "").strip()
    m = _RE_HD.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    m = _RE_HM.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    return ""

def _address_text_from_url(url: str) -> str:
    u = (url or "").strip()
    m = _RE_HD.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    m = _RE_HM.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    return ""

# ====== Address parsing (handles comma/no-comma) ======
STATE_2 = r"(?:A[LKZR]|C[AOT]|D[EC]|F[LM]|G[AU]|H[IW]|I[ADLN]|K[SY]|L[A]|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[A]|R[IL]|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])"

def _normalize_city_name(raw: str) -> str:
    c = (raw or "").strip().lower()
    c = re.sub(r"\s+", " ", c.replace("-", " "))
    c = _CITY_ALIASES.get(c, c)
    return _title_keep_small_words(c.split())

def _split_addr(addr: str) -> Tuple[str, str, str, str]:
    """
    Split into (street, city, state, zip) for lines with or without commas.
    """
    a = (addr or "").strip()
    if not a:
        return "", "", "", ""
    a = re.sub(r"\s+", " ", a)

    if "," in a:
        parts = [p.strip() for p in a.split(",")]
        street = parts[0] if parts else ""
        rest = " ".join(parts[1:]).strip()
        m = re.search(rf"\b({STATE_2})\b(?:\s+(\d{{5}}(?:-\d{{4})?))?\s*$", rest, re.I)
        state = (m.group(1) if m else "").upper()
        zipc  = (m.group(2) if (m and m.lastindex and m.lastindex >= 2) else "")
        city  = rest[:m.start()].strip() if m else rest
        return street, _normalize_city_name(city), state, zipc

    m2 = re.search(rf"\b({STATE_2})\b(?:\s+(\d{{5}}(?:-\d{{4})?))?\s*$", a, re.I)
    state = (m2.group(1) if m2 else "").upper()
    zipc  = (m2.group(2) if (m2 and m2.lastindex and m2.lastindex >= 2) else "")
    head = a[:m2.start()].strip() if m2 else a

    m3 = re.match(r"^\s*\d+\s+.*$", head)
    if m3:
        mtype = re.search(
            r"\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|pl|place|ter|terrace|hwy|highway|pkwy|parkway|cir|circle|sq|square|way|wy|driveway)\b\.?",
            head, re.I
        )
        street = head[:mtype.end()] if mtype else head
        city = head[len(street):].strip()
        return street.strip(), _normalize_city_name(city), state, zipc

    return head, _normalize_city_name(""), state, zipc

def _strip_unit_tail(street: str) -> str:
    return re.sub(r"\b(?:apt|unit|suite|ste|lot|#)\s*[A-Za-z0-9\-]*\s*$", "", street, flags=re.I).strip()

def _canonicalize_street(street: str) -> str:
    """
    Return canonical display street like: 'E Jackson St' or 'W Martin St'
    """
    s = _strip_unit_tail(street or "")
    s = re.sub(r"\s+", " ", s).strip().strip(",")
    if not s:
        return ""

    parts = re.split(r"\s+", s)
    # street number (keep as-is)
    num = parts[0] if parts and parts[0].isdigit() else ""

    # detect leading dir
    idx = 1 if num else 0
    lead_dir = ""
    if idx < len(parts) and _DIR.get(parts[idx].lower().strip(".,"), ""):
        lead_dir = _DIR[parts[idx].lower().strip(".,")]
        idx += 1

    # find street type at end
    tail_dir = ""
    st_type = ""
    j = len(parts) - 1
    if j >= idx:
        # check last token is type or dir
        last = parts[j].lower().strip(".,")
        if last in _DIR:
            tail_dir = _DIR[last]
            j -= 1
            last = parts[j].lower().strip(".,") if j >= idx else ""
        if last in _STTYPE:
            st_type = _STTYPE[last]
            j -= 1

    # middle name tokens (street name core)
    name_tokens = [re.sub(r"[^\w-]", "", t) for t in parts[idx:j+1] if t]

    # build display
    disp: List[str] = []
    if num:
        disp.append(num)
    if lead_dir:
        disp.append(lead_dir.upper())
    if name_tokens:
        disp.append(_title_keep_small_words(name_tokens))
    if st_type:
        disp.append(st_type.upper())
    elif tail_dir:  # rare cases like 'East Street' w/ dir at end but no type
        disp.append(tail_dir.upper())
    return " ".join([p for p in disp if p]).strip()

def _street_only(addr: str) -> str:
    s, _, _, _ = _split_addr(addr)
    return _canonicalize_street(s)

def _street_slug(addr: str) -> str:
    # Slug derived from the canonicalized street only (stable across variants)
    return _norm_slug_from_text(_street_only(addr))

def _city_slug(addr: str) -> str:
    _, c, _, _ = _split_addr(addr)
    return _norm_slug_from_text(c)

def _state2(addr: str) -> str:
    _, _, stt, _ = _split_addr(addr)
    return (stt or "").upper()

def _zip5(addr: str) -> str:
    _, _, _, z = _split_addr(addr)
    m = re.match(r"^(\d{5})", z or "")
    return m.group(1) if m else ""

def _canonical_display_address(addr: str, url: str = "") -> str:
    """
    Build a uniform display address:
      '<CanonStreet>, <City>, <ST> <ZIP>' where available.
    Fallback to url-derived address text if needed.
    """
    raw = (addr or "").strip()
    if not raw and url:
        raw = _address_text_from_url(url)

    st_txt, city, st2, z = _split_addr(raw)
    c_street = _canonicalize_street(st_txt)

    # Prefer ZIP if present, else omit
    parts = []
    if c_street:
        parts.append(c_street)
    if city:
        parts.append(city)
    tail = []
    if st2:
        tail.append(st2)
    if z:
        tail.append(re.match(r"^\d{5}", z).group(0) if re.match(r"^\d{5}", z or "") else "")
    if tail:
        parts.append(" ".join([p for p in tail if p]))
    # Insert commas appropriately
    if len(parts) >= 3:
        return f"{parts[0]}, {parts[1]}, {parts[2]}"
    if len(parts) == 2:
        return f"{parts[0]}, {parts[1]}"
    if len(parts) == 1:
        return parts[0]
    # Final fallback
    return (raw or "").strip() or (_address_text_from_url(url) or "Listing")

# ---- Candidate keys per row (most specific -> least) ----
def _candidate_keys(row: Dict[str, Any]) -> List[str]:
    url = (row.get("url") or "").strip()
    addr_raw = (row.get("address") or "").strip() or _address_text_from_url(url)

    # Always compute against canonicalized forms
    display_addr = _canonical_display_address(addr_raw, url)

    sslug = _street_slug(display_addr)
    cslug = _city_slug(display_addr)
    st2   = _state2(display_addr)
    z5    = _zip5(display_addr)

    keys: List[str] = []

    canon = (row.get("canonical") or "").strip().lower()
    if canon: keys.append("canon::" + canon)

    zpid = (row.get("zpid") or "").strip()
    if zpid: keys.append("zpid::" + zpid)

    zslug = _norm_slug_from_url(url)
    if zslug: keys.append("zslug::" + zslug)

    # Most precise geographic keys first
    if sslug and z5:
        keys.append("addrzip::{}::{}".format(sslug, z5))
    if sslug and cslug and st2:
        keys.append("addrcs::{}::{}::{}".format(sslug, cslug, st2))
    if sslug and cslug and not st2:
        keys.append("addrc::{}::{}".format(sslug, cslug))
    if sslug:
        keys.append("addr::{}".format(sslug))

    if url:
        keys.append("url::" + url.lower())

    out: List[str] = []
    seen = set()
    for kk in keys:
        if kk and kk not in seen:
            seen.add(kk)
            out.append(kk)
    return out

def _best_ts(row: Dict[str, Any]) -> datetime:
    tag = _pick_timestamp_date_tag(row)
    if tag:
        try:
            return datetime.strptime(tag, "%Y%m%d")
        except Exception:
            pass
    try:
        return datetime.fromisoformat((row.get("sent_at") or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.min

def _dedupe_by_property(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Group rows by ANY overlapping candidate key.
    For each group, keep the row with the newest timestamp.
    """
    key_to_gid: Dict[str, str] = {}
    gid_best: Dict[str, Dict[str, Any]] = {}

    def pick_gid(cands: List[str]) -> Optional[str]:
        for c in cands:
            if c in key_to_gid:
                return key_to_gid[c]
        return None

    def make_gid(cands: List[str]) -> str:
        for c in cands:
            if c:
                return c
        return "gid::{}".format(len(gid_best) + 1)

    for r in rows:
        cands = _candidate_keys(r)
        gid = pick_gid(cands)
        if not gid:
            gid = make_gid(cands)
        for c in cands:
            if c and c not in key_to_gid:
                key_to_gid[c] = gid
        cur = gid_best.get(gid)
        if (not cur) or (_best_ts(r) > _best_ts(cur)):
            gid_best[gid] = r

    return list(gid_best.values())

# ================= Query params helpers =================
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

def _qp_set(**kwargs):
    try:
        if kwargs:
            st.query_params.update(kwargs)
        else:
            st.query_params.clear()
    except Exception:
        if kwargs:
            st.experimental_set_query_params(**kwargs)
        else:
            st.experimental_set_query_params()

# ================= Lazy Streamlit bits (run-time only) =================
def _inject_css_once():
    if st.session_state.get("__clients_css_injected__"):
        return
    st.session_state["__clients_css_injected__"] = True
    st.markdown(
        """
        <style>
        :root { --row-border:#e2e8f0; --ink:#0f172a; --muted:#475569; }
        html[data-theme="dark"], .stApp [data-theme="dark"] {
          --row-border:#0b1220; --ink:#f8fafc; --muted:#cbd5e1;
        }
        .client-row { display:flex; align-items:center; justify-content:space-between; padding:10px 8px; border-bottom:1px solid var(--row-border); }
        .client-left { display:flex; align-items:center; gap:8px; min-width:0; }
        .client-name { font-weight:700; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

        .pill {
          font-size:11px; font-weight:800; padding:2px 10px; border-radius:999px;
          background:#f1f5f9; color:#0f172a; border:1px solid #cbd5e1; display:inline-block;
        }
        html[data-theme="dark"] .pill { background:#111827; color:#e5e7eb; border-color:#374151; }
        .pill.active {
          background: linear-gradient(180deg, #dcfce7 0%, #bbf7d0 100%);
          color:#166534; border:1px solid rgba(5,150,105,.35);
        }
        html[data-theme="dark"] .pill.active {
          background: linear-gradient(180deg, #064e3b 0%, #065f46 100%);
          color:#a7f3d0; border-color:rgba(167,243,208,.35);
        }
        .pill.inactive { opacity: 0.95; }

        .section-rule { border-bottom:1px solid var(--row-border); margin:8px 0 6px 0; }
        .meta-chip { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:8px; background:#eef2ff; color:#1e3a8a; border:1px solid #c7d2fe; }
        .date-badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:8px; background:#e0f2fe; color:#075985; border:1px solid #7dd3fc; }
        html[data-theme="dark"] .date-badge { background:#0b1220; color:#7dd3fc; border-color:#164e63; }
        .toured-badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:8px; background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }
        html[data-theme="dark"] .toured-badge { background:#7f1d1d; color:#fecaca; border-color:#ef4444; }
        </style>
        """,
        unsafe_allow_html=True,
    )

def _safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# ============== Supabase (lazy + safe) ==============
@st.cache_resource(show_spinner=False)
def get_supabase() -> Optional["Client"]:
    if create_client is None:
        return None
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None

def _sb_ok(SUPABASE) -> bool:
    try:
        return bool(SUPABASE)
    except Exception:
        return False

# ============== DB helpers (lazy supabase handle) ==============
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE):
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
        return [r for r in rows if r.get("active")] if not include_inactive else rows
    except Exception:
        return []

def _invalidate_clients_cache():
    try:
        fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

def toggle_client_active(client_id: int, new_active: bool):
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = (
            SUPABASE.table("clients")
            .select("id")
            .eq("name_norm", new_norm)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE) or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def upsert_client(name: str, active: bool = True):
    """Insert-or-update a client by normalized name (used by Run tab Add new client)."""
    SUPABASE = get_supabase()
    if not _sb_ok(SUPABASE):
        return False, "Not configured"
    name = (name or "").strip()
    if not name:
        return False, "Name required"
    try:
        norm = _norm_tag(name)
        existing = (
            SUPABASE.table("clients")
            .select("id")
            .eq("name_norm", norm)
            .limit(1)
            .execute()
            .data
            or []
        )
        if existing:
            cid = existing[0]["id"]
            SUPABASE.table("clients").update({"name": name, "active": active}).eq("id", cid).execute()
        else:
            SUPABASE.table("clients").insert({"name": name, "name_norm": norm, "active": active}).execute()
        _invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

@st.cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    SUPABASE = get_supabase()
    if not (_sb_ok(SUPABASE) and client_norm.strip()):
        return []
    try:
        cols = "id,url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = (
            SUPABASE.table("sent")
            .select(cols)
            .eq("client", client_norm.strip())
            .order("sent_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tour_norm_slugs_for_client(client_norm: str) -> set:
    """
    Return a set of street-only keys so toured tags align with dedupe.
    """
    SUPABASE = get_supabase()
    if not (_sb_ok(SUPABASE) and client_norm.strip()):
        return set()
    try:
        tq = SUPABASE.table("tours").select("id").eq("client", client_norm).limit(5000).execute()
        ids = [t["id"] for t in (tq.data or [])]
        if not ids:
            return set()
        sq = (
            SUPABASE.table("tour_stops")
            .select("address,address_slug")
            .in_("tour_id", ids)
            .limit(50000)
            .execute()
        )
        stops = (sq.data or [])
        out: set = set()
        for s in stops:
            raw = s.get("address_slug") or s.get("address") or ""
            if raw:
                # Normalize with the same canonical rules used in the report
                sslug = _street_slug(_canonical_display_address(raw))
                if sslug:
                    out.add("addr::" + sslug)
        return out
    except Exception:
        return set()

# ---- Sent-delete helpers ----
def _invalidate_sent_cache():
    try:
        fetch_sent_for_client.clear()  # type: ignore[attr-defined]
    except Exception:
        pass

def _collect_ids_for_property(client_norm: str, all_sent_rows: List[Dict[str, Any]], gid: str) -> List[int]:
    """Return all 'sent.id' that match the same-property group id for this client."""
    gid_map: Dict[str, str] = {}

    def pick_gid(cands: List[str]) -> Optional[str]:
        for c in cands:
            if c in gid_map:
                return gid_map[c]
        return None

    for r in all_sent_rows:
        cands = _candidate_keys(r)
        g = pick_gid(cands) or (cands[0] if cands else None)
        if not g:
            continue
        for c in cands:
            if c not in gid_map:
                gid_map[c] = g

    out_ids: List[int] = []
    for r in all_sent_rows:
        try:
            cands = _candidate_keys(r)
            g = None
            for c in cands:
                if c in gid_map:
                    g = gid_map[c]
                    break
            if g == gid and r.get("id"):
                out_ids.append(int(r["id"]))
        except Exception:
            continue
    return out_ids

# ================= UI bits =================
def _client_row_icons(name: str, norm: str, cid: int, active: bool):
    col_name, col_rep, col_ren, col_tog, col_del, col_sp = st.columns([8, 1, 1, 1, 1, 2])

    with col_name:
        status_class = "active" if active else "inactive"
        status_text  = "active" if active else "inactive"
        st.markdown(
            "<span class='client-name'>{nm}</span> <span class='pill {cls}'>{txt}</span>".format(
                nm=escape(name), cls=status_class, txt=status_text
            ),
            unsafe_allow_html=True,
        )

    with col_rep:
        if st.button("Open", key="rep_{0}".format(cid), help="Open report"):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set(report=norm, scroll="1")
            _safe_rerun()

    with col_ren:
        if st.button("Rename", key="rn_btn_{0}".format(cid), help="Rename"):
            st.session_state["__edit_{0}"] = True

    with col_tog:
        if st.button("Toggle", key="tg_{0}".format(cid), help=("Deactivate" if active else "Activate")):
            try:
                SUPABASE = get_supabase()
                rows = (
                    []
                    if not _sb_ok(SUPABASE)
                    else SUPABASE.table("clients").select("active").eq("id", cid).limit(1).execute().data
                    or []
                )
                cur = rows[0]["active"] if rows else active
                toggle_client_active(cid, (not cur))
            except Exception:
                toggle_client_active(cid, (not active))
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()

    with col_del:
        if st.button("Delete", key="del_{0}".format(cid), help="Delete"):
            st.session_state["__del_{0}"] = True

    if st.session_state.get("__edit_{0}".format(cid)):
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        new_name = st.text_input("New name", value=name, key="rn_val_{0}".format(cid))
        cc1, cc2 = st.columns([0.2, 0.2])
        if cc1.button("Save", key="rn_save_{0}".format(cid)):
            ok, msg = rename_client(cid, new_name)
            if not ok:
                st.warning(msg)
            st.session_state["__edit_{0}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if cc2.button("Cancel", key="rn_cancel_{0}".format(cid)):
            st.session_state["__edit_{0}"] = False

    if st.session_state.get("__del_{0}".format(cid)):
        st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)
        dc1, dc2 = st.columns([0.25, 0.25])
        if dc1.button("Confirm delete", key="del_yes_{0}".format(cid)):
            delete_client(cid)
            st.session_state["__del_{0}"] = False
            st.session_state["__active_tab__"] = "Clients"
            _safe_rerun()
        if dc2.button("Cancel", key="del_no_{0}".format(cid)):
            st.session_state["__del_{0}"] = False

    st.markdown("<div class='section-rule'></div>", unsafe_allow_html=True)

def _render_client_report_view(client_display_name: str, client_norm: str):
    st.markdown("### Report for {nm}".format(nm=escape(client_display_name)), unsafe_allow_html=True)

    colX, _ = st.columns([1, 3])
    with colX:
        if st.button("Close report", key="__close_report_{0}".format(client_norm)):
            st.session_state["__active_tab__"] = "Clients"
            _qp_set()
            _safe_rerun()

    sent_rows = fetch_sent_for_client(client_norm)
    if not sent_rows:
        st.info("No listings have been sent to this client yet.")
        return

    tour_street_keys = fetch_tour_norm_slugs_for_client(client_norm)

    # Filters
    seen_camps: List[str] = []
    for r in sent_rows:
        c = (r.get("campaign") or "").strip()
        if c not in seen_camps:
            seen_camps.append(c)
    labels = ["All campaigns"] + [("no campaign" if c == "" else c) for c in seen_camps]
    keys = [None] + seen_camps

    colF1, colF2, colF3 = st.columns([1.2, 1.8, 1])
    with colF1:
        i = st.selectbox(
            "Filter by campaign",
            list(range(len(labels))),
            format_func=lambda j: labels[j],
            index=0,
            key="__camp_{0}".format(client_norm),
        )
        sel_camp = keys[i]
    with colF2:
        q = st.text_input(
            "Search address / MLS / URL",
            value="",
            placeholder="e.g. 407 Woodall, 2501234, /homedetails/",
            key="__q_{0}".format(client_norm),
        )
        qn = (q or "").strip().lower()
    with colF3:
        st.caption("{n} total logged".format(n=len(sent_rows)))

    def _match(row) -> bool:
        if sel_camp is not None and (row.get("campaign") or "").strip() != sel_camp:
            return False
        if not qn:
            return True
        return (
            qn in (row.get("address", "").lower())
            or qn in (row.get("mls_id", "").lower())
            or qn in (row.get("url", "").lower())
        )

    filtered = [r for r in sent_rows if _match(r)]
    deduped = _dedupe_by_property(filtered)
    st.caption(
        "{n} unique listing{pl} (deduped by property)".format(
            n=len(deduped), pl=("s" if len(deduped) != 1 else "")
        )
    )

    md_lines: List[str] = []
    for r in deduped:
        url = (r.get("url") or "").strip()
        # Uniform display address
        addr_display = _canonical_display_address((r.get("address") or "").strip(), url)

        # DATE tag (YYYYMMDD)
        date_tag = _pick_timestamp_date_tag(r)

        # Toured badge: compare on street-only key (canonical rules)
        sslug = _street_slug(addr_display)
        street_key = "addr::" + sslug if sslug else ""
        toured = street_key in tour_street_keys

        debug_html = ""
        if DEBUG_REPORT:
            debug_html = (
                " <span style='font-size:10px;opacity:.7'>(dbg date_tag={dt}, key={k}, toured={t})</span>".format(
                    dt=escape(date_tag or "-"),
                    k=escape(street_key or "-"),
                    t=("yes" if toured else "no"),
                )
            )

        meta: List[str] = []
        if date_tag:
            meta.append("<span class='date-badge'>{}</span>".format(escape(date_tag)))
        if toured:
            meta.append("<span class='toured-badge'>Toured</span>")

        line = "- <a href=\"{u}\" target=\"_blank\" rel=\"noopener\">{a}</a> {meta}{dbg}".format(
            u=escape(url) if url else "#",
            a=escape(addr_display),
            meta=" ".join(meta),
            dbg=debug_html,
        )
        md_lines.append(line)

    if not md_lines:
        st.warning("No results returned.")
        return

    st.markdown("\n".join(md_lines), unsafe_allow_html=True)

    # ---- Manage sent listings (delete as groups)
    groups: Dict[str, str] = {}
    gid_rows: Dict[str, List[Dict[str, Any]]] = {}

    def _pick_gid(cands: List[str]) -> Optional[str]:
        for c in cands:
            if c in groups:
                return groups[c]
        return None

    for r in filtered:
        cands = _candidate_keys(r)
        gid = _pick_gid(cands) or (cands[0] if cands else None)
        if not gid:
            continue
        for c in cands:
            groups.setdefault(c, gid)
        gid_rows.setdefault(gid, []).append(r)

    gid_label: Dict[str, str] = {}
    for gid, rows_for_gid in gid_rows.items():
        best = max(rows_for_gid, key=_best_ts)
        url = (best.get("url") or "").strip()
        addr_display = _canonical_display_address((best.get("address") or "").strip(), url)
        gid_label[gid] = addr_display

    with st.expander("Manage sent listings (delete)"):
        label_to_gid: Dict[str, str] = {}
        counts: Dict[str, int] = {}
        for gid, lbl in gid_label.items():
            c = counts.get(lbl, 0)
            counts[lbl] = c + 1
            if c == 0:
                label_to_gid[lbl] = gid
            else:
                dis_lbl = "{}  · {}".format(lbl, c + 1)
                label_to_gid[dis_lbl] = gid

        choices = list(label_to_gid.keys())
        to_delete = st.multiselect(
            "Select properties to delete (removes all 'sent' rows for those properties for this client):",
            options=choices,
        )

        if st.button("Delete selected properties", type="primary", use_container_width=False):
            ids: List[int] = []
            for lbl in to_delete:
                gid = label_to_gid[lbl]
                ids.extend(_collect_ids_for_property(client_norm, sent_rows, gid))
            ids = sorted(set(ids))
            ok, msg = _delete_sent_rows_by_ids(ids)
            if ok:
                st.success(
                    "Deleted {n} sent row(s) across {m} propert{y}.".format(
                        n=len(ids), m=len(to_delete), y=("y" if len(to_delete) == 1 else "ies")
                    )
                )
                st.session_state["__active_tab__"] = "Clients"
                _safe_rerun()
            else:
                st.error("Delete failed: {0}".format(msg))

    with st.expander("Export filtered (deduped)"):
        import pandas as pd
        buf = io.StringIO()
        pd.DataFrame(deduped).to_csv(buf, index=False)
        st.download_button(
            "Download CSV",
            data=buf.getvalue(),
            file_name="client_report_{nm}_{ts}.csv".format(
                nm=client_norm, ts=datetime.utcnow().strftime("%Y%m%d%H%M%S")
            ),
            mime="text/csv",
            use_container_width=False,
        )

# ============== Public entry (call this from app.py) ==============
def render_clients_tab():
    _inject_css_once()

    st.subheader("Clients")
    st.caption("Use Open to open an inline report; Rename; Toggle; Delete.")

    report_norm_qp = _qp_get("report", "")
    want_scroll = _qp_get("scroll", "") in ("1", "true", "yes")

    all_clients = fetch_clients(include_inactive=True)
    active = [c for c in all_clients if c.get("active")]
    inactive = [c for c in all_clients if not c.get("active")]

    colA, colB = st.columns(2)
    with colA:
        st.markdown("### Active", unsafe_allow_html=True)
        if not active:
            st.write("_No active clients_")
        for c in active:
            _client_row_icons(c["name"], c.get("name_norm", ""), c["id"], active=True)
    with colB:
        st.markdown("### Inactive", unsafe_allow_html=True)
        if not inactive:
            st.write("_No inactive clients_")
        for c in inactive:
            _client_row_icons(c["name"], c.get("name_norm", ""), c["id"], active=False)

    st.markdown('<div id="report_anchor"></div>', unsafe_allow_html=True)
    if report_norm_qp:
        display_name = next(
            (c["name"] for c in all_clients if c.get("name_norm") == report_norm_qp),
            report_norm_qp,
        )
        st.markdown("---")
        _render_client_report_view(display_name, report_norm_qp)
        if want_scroll:
            st.components.v1.html(
                """
                <script>
                  const el = parent.document.getElementById("report_anchor");
                  if (el) { el.scrollIntoView({behavior: "smooth", block: "start"}); }
                </script>
                """,
                height=0,
            )
        _qp_set(report=report_norm_qp)
