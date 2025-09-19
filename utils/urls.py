import re, requests
from core.config import REQUEST_TIMEOUT
UA_HEADERS = {"User-Agent": "Mozilla/5.0 ..."}

ZPID_RE = re.compile(r'(\d{6,})_zpid', re.I)

def canonicalize_zillow(url: str):
    if not url: return "", None
    base = re.sub(r'[#?].*$', '', url)
    m_full = re.search(r'^(https?://[^?#]*/homedetails/[^/]+/\d{6,}_zpid/)', url, re.I)
    canon = m_full.group(1) if m_full else base
    m_z = ZPID_RE.search(url)
    return canon, (m_z.group(1) if m_z else None)

def make_preview_url(url: str) -> str:
    if not url: return ""
    import re as _re
    base = _re.sub(r'[?#].*$', '', url.strip())
    canon, _ = canonicalize_zillow(base)
    return canon if "/homedetails/" in canon else base

def upgrade_to_homedetails_if_needed(url: str) -> str:
    if not url or "/homedetails/" in url: return url
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if not r.ok: return url
        m = re.search(r'href="(https://www\.zillow\.com/homedetails/[^"]+)"', r.text)
        return m.group(1) if m else url
    except Exception:
        return url
