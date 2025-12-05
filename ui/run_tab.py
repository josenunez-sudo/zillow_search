# ui/run_tab.py
# Homespotter → full address (with number) via aggressive coord & text extraction + reverse/forward geocode.
# Outputs clean Zillow /homes/<full-address>_rb/ deeplinks. CSV upload preserved. No Bing/Azure.

import os, csv, io, re, json, time, html
from typing import List, Dict, Any, Optional, Tuple
from html import escape
from urllib.parse import urlparse, urlunparse, parse_qs, quote_plus

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------------- Page ----------------
st.set_page_config(page_title="Address Alchemist — HS Resolver", layout="centered")
st.markdown("""
<style>
.block-container { max-width: 980px; }
.center-box { padding:12px; border-radius:12px; }
ul.link-list { margin:0.25rem 0 0 1.1rem; padding:0; }
.run-zone .stButton>button { background: linear-gradient(180deg,#2563eb 0%,#1d4ed8 100%)!important;color:#fff!important;font-weight:800!important;border:0!important;border-radius:12px!important;box-shadow:0 10px 22px rgba(29,78,216,.35),0 2px 6px rgba(0,0,0,.18)!important; }
.img-label { font-size:13px; margin-top:6px; }
.small { color:#64748b; font-size:12px; }
.badge { display:inline-block; font-size:11px; font-weight:800; padding:2px 6px; border-radius:999px; margin-left:6px; border:1px solid rgba(0,0,0,.15); }
.badge.ok { background:#dcfce7; color:#065f46; border-color:#86efac; }
.badge.warn { background:#fee2e2; color:#7f1d1d; border-color:#fecaca; }
</style>
""", unsafe_allow_html=True)

# ---------------- Config ----------------
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", st.secrets.get("GOOGLE_MAPS_API_KEY", ""))
REQUEST_TIMEOUT = 18

DEFAULT_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SOCIAL_UAS = [
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Twitterbot/1.0",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
]

# ---------------- HTTP helpers ----------------
def _get(url: str, ua: str = DEFAULT_UA, allow_redirects: bool = True) -> Tuple[str, str, int, Dict[str,str]]:
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=allow_redirects,
        )
        return r.url, (r.text if r.ok else ""), r.status_code, dict(r.headers or {})
    except Exception:
        return url, "", 0, {}

def _readable(url: str) -> str:
    """
    Text-only readability proxy for stubborn JS pages (r.jina.ai).
    """
    try:
        u = urlparse(url)
        inner = "http://" + u.netloc + u.path + (("?" + u.query) if u.query else "")
        prox = "https://r.jina.ai/" + inner
        _, text, code, _ = _get(prox, ua=DEFAULT_UA, allow_redirects=True)
        return text if code == 200 and text else ""
    except Exception:
        return ""

# ---------------- Address patterns ----------------
RE_STREET_CITY_ST_ZIP = re.compile(
    r'(\d{1,6}\s+[A-Za-z0-9\.\'\-\s]+(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Circle|Cir|Highway|Hwy|Route|Parkway|Pkwy)\b[^\n,]*)\s*,\s*([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)',
    re.I
)
RE_CITY_ST_ZIP = re.compile(r'([A-Za-z\.\'\-\s]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)', re.I)

# ---------------- Extractors ----------------
def _jsonld_blocks(html_txt: str) -> List[Dict[str, Any]]:
    out = []
    if not html_txt: return out
    # unescape then parse
    un = html.unescape(html_txt)
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', un, re.I|re.S):
        blob = (m.group(1) or "").strip()
        try:
            data = json.loads(blob)
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                out.extend([d for d in data if isinstance(d, dict)])
        except Exception:
            continue
