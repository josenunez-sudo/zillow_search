import io, os
from PIL import Image
import streamlit as st

def page_icon_from_avif(path: str):
    if not os.path.exists(path):
        return "⚗️"
    try:
        im = Image.open(path); im.load()
        if im.mode not in ("RGB","RGBA"): im = im.convert("RGBA")
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return "⚗️"

BASE_CSS = """<style>/* your big CSS block exactly as-is */</style>"""

def apply_page_base():
    st.set_page_config(
        page_title="Address Alchemist",
        page_icon=page_icon_from_avif("/mnt/data/link.avif"),
        layout="centered",
    )
    st.markdown(BASE_CSS, unsafe_allow_html=True)
