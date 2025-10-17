# utils/html_tools.py
# Lightweight HTML helpers shared by the app.
from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional, Tuple
import requests

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

def expand_url_and_fetch_html(url: str, timeout: float = 12.0) -> Tuple[str, str, int]:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=timeout, allow_redirects=True)
        return r.url, (r.text if r.ok else ""), r.status_code
    except Exception:
        return url, "", 0

def json_ld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html:
        return out
    try:
        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I|re.S):
            blob = m.group(1)
            try:
                data = json.loads(blob)
                if isinstance(data, list):
                    out.extend([d for d in data if isinstance(d, dict)])
                elif isinstance(data, dict):
                    out.append(data)
            except Exception:
                continue
    except Exception:
        pass
    return out

def extract_title_or_desc(html: str) -> str:
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

# Existing summarization utilities retained
def summarize_remarks(text: str, max_sent: int = 2) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    sents = re.split(r"(?<=[\.!?])\s+", text)
    if len(sents) <= max_sent:
        return text
    pref_kw = ["updated","renovated","new","roof","hvac","kitchen","bath","floor","windows","mechanicals","acres","acre","lot","school","zoned","hoa","no hoa"]
    scored = [(sum(1 for k in pref_kw if k in s.lower()), i, s) for i,s in enumerate(sents[:8])]
    scored.sort(key=lambda x:(-x[0], x[1]))
    return " ".join([s for _,_,s in scored[:max_sent]])

KEY_HL = [("new roof","roof"),("hvac","hvac"),("ac unit","ac"),("furnace","furnace"),("water heater","water heater"),
          ("renovated","renovated"),("updated","updated"),("remodeled","remodeled"),("open floor plan","open plan"),
          ("cul-de-sac","cul-de-sac"),("pool","pool"),("fenced","fenced"),("acre","acre"),("hoa","hoa"),
          ("primary on main","primary on main"),("finished basement","finished basement")]

def extract_highlights(text: str) -> List[str]:
    t = (text or "").lower()
    out: List[str] = []
    for pat,label in KEY_HL:
        if pat in t:
            out.append(label)
    # de-dupe, keep order
    seen = set()
    uniq: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return uniq[:6]
