# services/images.py
from __future__ import annotations
import re, requests
from typing import Dict, Any, Optional, Tuple
import streamlit as st
from core.config import GOOGLE_MAPS_API_KEY, REQUEST_TIMEOUT
from services.enrich import extract_zillow_first_image

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def picture_for_result_with_log(query_address: str, zurl: str, csv_photo_url: Optional[str] = None):
    log = {
        "url": zurl, "csv_provided": bool(csv_photo_url), "stage": None,
        "status_code": None, "html_len": None, "selected": None, "errors": []
    }
    def _ok(u:str)->bool: return isinstance(u,str) and (u.startswith("http://") or u.startswith("https://") or u.startswith("data:"))
    if csv_photo_url and _ok(csv_photo_url):
        log["stage"]="csv_photo"; log["selected"]=csv_photo_url; return csv_photo_url, log
    if zurl and "/homedetails/" in zurl:
        try:
            r = requests.get(zurl, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT); log["status_code"]=r.status_code
            if r.ok:
                html=r.text; log["html_len"]=len(html)
                zfirst=extract_zillow_first_image(html)
                if zfirst: log["stage"]="zillow_hero"; log["selected"]=zfirst; return zfirst, log
                for pat in [
                    r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"<meta[^>]+property=['\"]og:image:secure_url['\"][^>]+content=['\"]([^'\"]+)['\"]",
                    r"\"image\"\s*:\s*\"(https?://[^\"]+)\"",
                    r"\"image\"\s*:\s*\[\s*\"(https?://[^\"]+)\"",
                ]:
                    m = re.search(pat, html, re.I)
                    if m: log["stage"]="og_image"; log["selected"]=m.group(1); return m.group(1), log
        except Exception as e:
            log["errors"].append(f"fetch_err:{e!r}")
    try:
        key = GOOGLE_MAPS_API_KEY
        if key and query_address:
            from urllib.parse import quote_plus
            loc = quote_plus(query_address)
            sv = f"https://maps.googleapis.com/maps/api/streetview?size=600x400&location={loc}&key={key}"
            log["stage"]="street_view"; log["selected"]=sv; return sv, log
        else:
            if not key: log["errors"].append("no_google_maps_key")
    except Exception as e:
        log["errors"].append(f"sv_err:{e!r}")
    log["stage"]="none"; return None, log

@st.cache_data(ttl=900, show_spinner=False)
def get_thumbnail_and_log(query_address: str, zurl: str, csv_photo_url: Optional[str]):
    return picture_for_result_with_log(query_address, zurl, csv_photo_url)
