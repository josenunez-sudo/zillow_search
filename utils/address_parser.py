# utils/address_parser.py
# Minimal deps, import-safe. Public API: address_as_markdown_link(url, parse_html=True, timeout=12)

import re, json
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse, unquote

def _clean(s: Optional[str]) -> Optional[str]:
    if not s: return s
    return re.sub(r"\s+", " ", s).strip()

def _titleish(s: str) -> str:
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    out = []
    for p in parts:
        if re.fullmatch(r"[A-Z]{2}", p):
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:].lower())
    return " ".join(out)

def _normalize(parts: Dict[str, str]) -> Optional[str]:
    line1 = _clean(parts.get("streetAddress") or parts.get("addressLine1") or parts.get("address") or parts.get("line1"))
    city  = _clean(parts.get("addressLocality") or parts.get("city"))
    reg   = _clean(parts.get("addressRegion") or parts.get("region") or parts.get("state"))
    zipc  = _clean(parts.get("postalCode") or parts.get("zip"))
    if line1 and city and reg:
        return f"{line1}, {city}, {reg}{(' ' + zipc) if zipc else ''}"
    return None

def _addr_from_url_slug(url: str) -> Optional[Dict[str, str]]:
    p = urlparse(url)
    host = (p.netloc or "").lower()
    path = unquote(p.path or "")

    # Zillow
    m = re.search(r"/homedetails/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, re.I)
    if "zillow.com" in host and m:
        return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    # Redfin: /NC/Raleigh/721-Currituck-Dr-27609/...
    if "redfin.com" in host:
        m = re.search(r"^/([A-Z]{2})/([^/]+)/([\w\-]+)-(\d{5})(?:/|$)", path, re.I)
        if m:
            return {
                "streetAddress": _titleish(m.group(3)),
                "addressLocality": _titleish(m.group(2)),
                "addressRegion": m.group(1).upper(),
                "postalCode": m.group(4),
            }

    # Trulia
    if "trulia.com" in host:
        m = re.search(r"/home/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, re.I)
        if m:
            return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    # Homes.com
    if "homes.com" in host:
        m = re.search(r"/([a-z\-]+)-([a-z]{2})/([\w\-]+)-(\d{5})", path, re.I)
        if m:
            return {
                "streetAddress": _titleish(m.group(3)),
                "addressLocality": _titleish(m.group(1).replace("-", " ")),
                "addressRegion": m.group(2).upper(),
                "postalCode": m.group(4),
            }

    # Realtor.com usually needs HTML JSON; slug rarely has full address
    return None

def _mk_parts(street_slug: str, city_slug: str, st: str, zipc: str) -> Dict[str, str]:
    return {
        "streetAddress": _titleish(street_slug),
        "addressLocality": _titleish(city_slug),
        "addressRegion": st.upper(),
        "postalCode": zipc,
    }

def _addr_from_ld_json(soup) -> Optional[Dict[str, str]]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.text or "{}")
        except Exception:
            continue

        def find_postal(obj) -> Optional[Dict[str, str]]:
            if isinstance(obj, dict):
                if obj.get("@type") in ("PostalAddress", "schema:PostalAddress"):
                    return {
                        "streetAddress": obj.get("streetAddress"),
                        "addressLocality": obj.get("addressLocality"),
                        "addressRegion": obj.get("addressRegion"),
                        "postalCode": obj.get("postalCode"),
                    }
                for v in obj.values():
                    got = find_postal(v)
                    if got: return got
            elif isinstance(obj, list):
                for it in obj:
                    got = find_postal(it)
                    if got: return got
            return None

        found = find_postal(data)
        if found and _normalize(found):
            return found
    return None

def _addr_from_meta_or_title(soup) -> Optional[Dict[str, str]]:
    for m in soup.find_all("meta"):
        k = (m.get("property") or m.get("name") or "").lower()
        v = _clean(m.get("content"))
        if not k or not v: 
            continue
        if k in ("og:title","twitter:title","og:description","twitter:description"):
            a = _pick_address_from_text(v)
            if a: return a
    t = _clean(soup.title.string if soup.title else None)
    if t:
        a = _pick_address_from_text(t)
        if a: return a
    return None

def _pick_address_from_text(txt: str) -> Optional[Dict[str, str]]:
    m = re.search(r"(\d{1,6}[^,]+),\s*([A-Za-z .'-]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", txt)
    if not m: 
        return None
    return {
        "streetAddress": _titleish(m.group(1)),
        "addressLocality": _titleish(m.group(2)),
        "addressRegion": m.group(3),
        "postalCode": (m.group(4) or "").strip(),
    }

def extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]:
    # Follow redirects with requests if available
    final_url = url
    html_text = None

    if parse_html:
        try:
            import requests
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            })
            r = sess.get(url, allow_redirects=True, timeout=timeout)
            r.raise_for_status()
            final_url = r.url
            html_text = r.text
        except Exception:
            pass

    # If we got HTML, try BeautifulSoup-based strategies
    if html_text:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_text, "html.parser")
            for strat in (_addr_from_ld_json, _addr_from_meta_or_title):
                a = strat(soup)
                if a and _normalize(a):
                    return {**a, "full": _normalize(a), "source": "html", "url_final": final_url}
        except Exception:
            # fall back to slug below
            pass

    # Slug fallback (works without extra deps)
    a = _addr_from_url_slug(final_url)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "url-slug", "url_final": final_url}

    return {
        "full": None, "streetAddress": None, "addressLocality": None,
        "addressRegion": None, "postalCode": None, "source": "unknown",
        "url_final": final_url
    }

def address_as_markdown_link(url: str, label: Optional[str] = None, parse_html: bool = True, timeout: float = 12.0) -> Tuple[str, Dict[str, Optional[str]]]:
    info = extract_address(url, parse_html=parse_html, timeout=timeout)
    text = label or info.get("full") or (urlparse(info.get("url_final") or url).netloc or "Listing")
    return f"[{text}]({info.get('url_final') or url})", info
