# core/db.py
import os, re
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import streamlit as st
from supabase import create_client, Client

# ---------- Supabase ----------
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

def _sb_ok() -> bool:
    try: return bool(SUPABASE)
    except NameError: return False

def _norm_tag(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

# ---------- Clients helpers ----------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if _norm_tag(r.get("name_norm","")) != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def upsert_client(name: str, active: bool = True, notes: str = None):
    if not _sb_ok() or not (name or "").strip():
        return False, "Not configured or empty name"
    try:
        name_norm = _norm_tag(name)
        payload = {"name": name.strip(), "name_norm": name_norm, "active": active}
        if notes is not None: payload["notes"] = notes
        SUPABASE.table("clients").upsert(payload, on_conflict="name_norm").execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---------- Sent table lookups ----------
def _supabase_available():
    try: return bool(SUPABASE)
    except NameError: return False

@st.cache_data(ttl=300, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    if not (_supabase_available() and client_norm.strip()): return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = SUPABASE.table("sent")\
            .select(cols)\
            .eq("client", client_norm.strip())\
            .order("sent_at", desc=True)\
            .limit(limit)\
            .execute()
        return resp.data or []
    except Exception:
        return []

@st.cache_data(ttl=300, show_spinner=False)
def get_already_sent_maps(client_tag: str):
    if not (_supabase_available() and client_tag.strip()):
        return set(), set(), {}, {}
    try:
        rows = SUPABASE.table("sent").select("canonical,zpid,url,sent_at").eq("client", client_tag.strip()).limit(20000).execute().data or []
        canon_set = { (r.get("canonical") or "").strip() for r in rows if r.get("canonical") }
        zpid_set  = { (r.get("zpid") or "").strip() for r in rows if r.get("zpid") }
        canon_info, zpid_info = {}, {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info: canon_info[c] = info
            if z and z not in zpid_info:  zpid_info[z]  = info
        return canon_set, zpid_set, canon_info, zpid_info
    except Exception:
        return set(), set(), {}, {}

@st.cache_data(ttl=300, show_spinner=False)
def get_toured_sets(client_tag: str):
    """Return (canon_set, zpid_set) for rows where campaign starts with 'tour-'."""
    if not (_supabase_available() and (client_tag or "").strip()):
        return set(), set()
    try:
        rows = (
            SUPABASE.table("sent")
            .select("canonical,zpid,campaign")
            .eq("client", (client_tag or "").strip())
            .ilike("campaign", "tour-%")
            .limit(20000)
            .execute()
            .data
            or []
        )
        canon = {(r.get("canonical") or "").strip() for r in rows if r.get("canonical")}
        zpid  = {(r.get("zpid") or "").strip() for r in rows if r.get("zpid")}
        return canon, zpid
    except Exception:
        return set(), set()

def log_sent_rows(results: List[Dict[str, Any]], client_tag: str, campaign_tag: str):
    if not SUPABASE or not results:
        return False, "Supabase not configured or no results."
    rows = []
    now_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for r in results:
        raw_url = (r.get("preview_url") or r.get("zillow_url") or r.get("display_url") or "").strip()
        if not raw_url:
            continue
        canon = r.get("canonical"); zpid = r.get("zpid")
        if not (canon and zpid):
            canon2, zpid2 = _canonicalize_zillow(raw_url)
            canon = canon or canon2; zpid = zpid or zpid2
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
    if not rows: return False, "No valid rows to log."
    try:
        SUPABASE.table("sent").insert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

# Small local canonicalizer (used if Run tab hasn't populated canonical/zpid yet)
import re as _re
_ZPID_RE = _re.compile(r'(\d{6,})_zpid', _re.I)
def _canonicalize_zillow(url: str):
    if not url: return "", None
    base = _re.sub(r'[#?].*$', '', url)
    m_full = _re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, _re.I)
    canon = m_full.group(1) if m_full else base
    m_z = _ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)
