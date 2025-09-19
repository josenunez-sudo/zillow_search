import os, re
import streamlit as st

def get_secret(key: str, default: str = "") -> str:
    try:
        if key in st.secrets and st.secrets[key]:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.getenv(key, default)

# Pull once on import
SUPABASE_URL  = get_secret("SUPABASE_URL", "")
SUPABASE_KEY  = get_secret("SUPABASE_SERVICE_ROLE", "")
AZURE_SEARCH_ENDPOINT = get_secret("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
AZURE_SEARCH_INDEX    = get_secret("AZURE_SEARCH_INDEX", "")
AZURE_SEARCH_KEY      = get_secret("AZURE_SEARCH_API_KEY", "")
BING_API_KEY          = get_secret("BING_API_KEY", "")
BING_CUSTOM_ID        = get_secret("BING_CUSTOM_CONFIG_ID", "")
GOOGLE_MAPS_API_KEY   = get_secret("GOOGLE_MAPS_API_KEY", "")
BITLY_TOKEN           = get_secret("BITLY_TOKEN", "")
REQUEST_TIMEOUT       = 12

def norm_tag(s: str) -> str:
    return re.sub(r"\s+"," ", (s or "").strip()).lower()
