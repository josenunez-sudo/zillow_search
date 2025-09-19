from typing import Any, Dict, List, Tuple
from supabase import create_client, Client
from core.cache import cache_resource, cache_data
from core.config import SUPABASE_URL, SUPABASE_KEY
import streamlit as st

@cache_resource
def get_supabase() -> Client | None:
    if not (SUPABASE_URL and SUPABASE_KEY): return None
    try:
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:
        return None

def sb_ok() -> bool:
    try: return bool(get_supabase())
    except Exception: return False

# ---- Clients
@cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False):
    supa = get_supabase()
    if not supa: return []
    try:
        rows = (supa.table("clients")
            .select("id,name,name_norm,active")
            .order("name", desc=False).execute().data) or []
        rows = [r for r in rows if r.get("name_norm") != "test test"]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def invalidate_clients_cache():
    try: fetch_clients.clear()  # type: ignore[attr-defined]
    except Exception: pass

def upsert_client(name: str, active: bool = True, notes: str | None = None):
    supa = get_supabase()
    if not supa or not (name or "").strip(): return False, "Not configured or empty name"
    try:
        payload = {"name": name.strip(), "name_norm": name.strip().lower(), "active": active}
        if notes is not None: payload["notes"] = notes
        supa.table("clients").upsert(payload, on_conflict="name_norm").execute()
        invalidate_clients_cache()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool):
    supa = get_supabase()
    if not supa or not client_id: return False, "Not configured"
    try:
        supa.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        invalidate_clients_cache(); return True, "ok"
    except Exception as e:
        return False, str(e)

def rename_client(client_id: int, new_name: str):
    supa = get_supabase()
    if not supa or not client_id or not (new_name or "").strip(): return False, "Bad input"
    try:
        new_norm = new_name.strip().lower()
        existing = supa.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that name already exists."
        supa.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        invalidate_clients_cache(); return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int):
    supa = get_supabase()
    if not supa or not client_id: return False, "Not configured"
    try:
        supa.table("clients").delete().eq("id", client_id).execute()
        invalidate_clients_cache(); return True, "ok"
    except Exception as e:
        return False, str(e)

# ---- Sent/report
@cache_data(ttl=120, show_spinner=False)
def fetch_sent_for_client(client_norm: str, limit: int = 5000):
    supa = get_supabase()
    if not supa or not client_norm.strip(): return []
    try:
        cols = "url,address,sent_at,campaign,mls_id,canonical,zpid"
        resp = (supa.table("sent")
            .select(cols)
            .eq("client", client_norm.strip())
            .order("sent_at", desc=True)
            .limit(limit).execute())
        return resp.data or []
    except Exception:
        return []
