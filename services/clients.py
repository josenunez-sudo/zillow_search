# services/clients.py
import os
import re
from typing import List, Dict, Any, Optional, Tuple
import streamlit as st
from supabase import create_client, Client

# ---- Supabase
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
    try:
        return bool(SUPABASE)
    except Exception:
        return False

def _norm_tag(s: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", (s or "").strip()).lower()

# ---- Clients registry
@st.cache_data(ttl=60, show_spinner=False)
def fetch_clients(include_inactive: bool = False) -> List[Dict[str, Any]]:
    if not _sb_ok(): return []
    try:
        rows = SUPABASE.table("clients").select("id,name,name_norm,active").order("name", desc=False).execute().data or []
        rows = [r for r in rows if r.get("name_norm") != _norm_tag("test test")]
        return rows if include_inactive else [r for r in rows if r.get("active")]
    except Exception:
        return []

def rename_client(client_id: int, new_name: str) -> Tuple[bool, str]:
    if not _sb_ok() or not client_id or not (new_name or "").strip():
        return False, "Bad input"
    try:
        new_norm = _norm_tag(new_name)
        existing = SUPABASE.table("clients").select("id").eq("name_norm", new_norm).limit(1).execute().data or []
        if existing and existing[0]["id"] != client_id:
            return False, "A client with that (normalized) name already exists."
        SUPABASE.table("clients").update({"name": new_name.strip(), "name_norm": new_norm}).eq("id", client_id).execute()
        try: fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception: pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def toggle_client_active(client_id: int, new_active: bool) -> Tuple[bool, str]:
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").update({"active": new_active}).eq("id", client_id).execute()
        try: fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception: pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

def delete_client(client_id: int) -> Tuple[bool, str]:
    if not _sb_ok() or not client_id:
        return False, "Not configured"
    try:
        SUPABASE.table("clients").delete().eq("id", client_id).execute()
        try: fetch_clients.clear()  # type: ignore[attr-defined]
        except Exception: pass
        return True, "ok"
    except Exception as e:
        return False, str(e)

# ---- “Already sent” lookups used by cross-check
@st.cache_data(ttl=300, show_spinner=False)
def get_already_sent_maps(client_tag: str):
    if not (_sb_ok() and client_tag.strip()):
        return set(), set(), {}, {}
    try:
        rows = SUPABASE.table("sent").select("canonical,zpid,url,sent_at").eq("client", client_tag.strip()).limit(20000).execute().data or []
        canon_set = { (r.get("canonical") or "").strip() for r in rows if r.get("canonical") }
        zpid_set  = { (r.get("zpid") or "").strip() for r in rows if r.get("zpid") }
        canon_info: Dict[str, Dict[str,str]] = {}
        zpid_info:  Dict[str, Dict[str,str]] = {}
        for r in rows:
            c = (r.get("canonical") or "").strip()
            z = (r.get("zpid") or "").strip()
            info = {"sent_at": r.get("sent_at") or "", "url": r.get("url") or ""}
            if c and c not in canon_info: canon_info[c] = info
            if z and z not in zpid_info:  zpid_info[z]  = info
        return canon_set, zpid_set, canon_info, zpid_info
    except Exception:
        return set(), set(), {}, {}
