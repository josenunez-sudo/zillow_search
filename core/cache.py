import streamlit as st

def safe_rerun():
    try:
        st.rerun()
    except Exception:
        try:
            st.experimental_rerun()
        except Exception:
            pass

# Expose aliases so you can use from core.cache import cache_data / cache_resource
cache_data = st.cache_data
cache_resource = st.cache_resource
