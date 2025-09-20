# services/tours.py
from typing import List, Dict, Any, Optional, Tuple
from datetime import date
import os, re
import streamlit as st
from supabase import create_client, Client

# We reuse helpers from the Run tab to keep logic identical
from ui.run_tab import canonicalize_zillow, make_preview_url

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

def sb_ok() -> bool:
    try: return bool(SUPABASE)
    except Exception: return False

def fetch_tours(client_norm: str) -> List[Dict[str, Any]]:
    if not sb_ok() or not client_norm.strip():
        return []
    try:
        resp = SUPABASE.table("tours")\
            .select("id,client,url,canonical,zpid,address,notes,status,tour_date,created_at")\
            .eq("client", client_norm.strip())\
            .order("created_at", desc=True)\
            .execute()
        return resp.data or []
    except Exception:
        return []

def upsert_tours(client_norm: str, items: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """
    items: [{url, address?, notes?, status?, tour_date?}, ...]
    Canonical & zpid are derived here for de-dup logic.
    """
    if not sb_ok() or not client_norm.strip() or not items:
        return False, "Not configured or no items"

    rows = []
    for it in items:
        raw = (it.get("url") or "").strip()
        if not raw:
            continue
        canon, zpid = canonicalize_zillow(raw)
        rows.append({
            "client": client_norm.strip(),
            "url": raw,
            "canonical": canon or None,
            "zpid": zpid or None,
            "address": (it.get("address") or "").strip() or None,
            "notes": (it.get("notes") or "").strip() or None,
            "status": (it.get("status") or "requested"),
            "tour_date": it.get("tour_date") or None
        })
    if not rows:
        return False, "No valid rows"

    try:
        # upsert using (client, canonical) uniqueness; if canonical missing, zpid will cover
        SUPABASE.table("tours").upsert(rows).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def update_tour_status(tour_id: int, status: str) -> Tuple[bool, str]:
    if not sb_ok() or not tour_id:
        return False, "Not configured or bad id"
    try:
        SUPABASE.table("tours").update({"status": status}).eq("id", tour_id).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def update_tour_meta(tour_id: int, *, notes: Optional[str]=None, tour_date: Optional[date]=None) -> Tuple[bool, str]:
    if not sb_ok() or not tour_id:
        return False, "Not configured or bad id"
    patch = {}
    if notes is not None: patch["notes"] = notes
    if tour_date is not None: patch["tour_date"] = tour_date
    if not patch:
        return True, "noop"
    try:
        SUPABASE.table("tours").update(patch).eq("id", tour_id).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_tour(tour_id: int) -> Tuple[bool, str]:
    if not sb_ok() or not tour_id:
        return False, "Not configured or bad id"
    try:
        SUPABASE.table("tours").delete().eq("id", tour_id).execute()
        return True, "ok"
    except Exception as e:
        return False, str(e)
