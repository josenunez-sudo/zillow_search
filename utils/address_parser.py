# utils/address_parser.py
# Public API: extract_address(url, parse_html=True, timeout=12.0) and address_as_markdown_link(...)
# Robust against IDX pages (Homespotter), JSON-LD, microdata, generic JSON blobs, and URL slugs.

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

def _mk_parts(street_slug: str, city_slug: str, st: str, zipc: str) -> Dict[str, str]:
    return {
        "streetAddress": _titleish(street_slug),
        "addressLocality": _titleish(city_slug),
        "addressRegion": st.upper(),
        "postalCode": zipc,
    }

def _pick_address_from_text(txt: str) -> Optional[Dict[str, str]]:
    m = re.search(r"(\d{1,6}[^,]+),\s*([A-Za-z .'-]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", txt)
    if not m: return None
    return {
        "streetAddress": _titleish(m.group(1)),
        "addressLocality": _titleish(m.group(2)),
        "addressRegion": m.group(3),
        "postalCode": (m.group(4) or "").strip(),
    }

# -------------------- URL slug parsers (no HTML needed) --------------------

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
        if m: return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

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

# -------------------- HTML-based parsers --------------------

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

def _addr_from_microdata(soup) -> Optional[Dict[str, str]]:
    # itemprop-based microdata (common on IDX pages)
    addr_root = soup.select_one('[itemprop="address"]') or soup
    def _txt(sel):
        el = addr_root.select_one(sel)
        return _clean(el.get_text(" ", strip=True)) if el else None
    parts = {
        "streetAddress": _txt('[itemprop="streetAddress"]'),
        "addressLocality": _txt('[itemprop="addressLocality"]'),
        "addressRegion": _txt('[itemprop="addressRegion"]'),
        "postalCode": _txt('[itemprop="postalCode"]'),
    }
    if _normalize(parts): return parts
    return None

def _first_match(txt: str, keys) -> Optional[str]:
    for k in keys:
        m = re.search(rf'"{k}"\s*:\s*"([^"]+)"', txt, re.I)
        if m:
            val = _clean(m.group(1))
            if val: return val
    return None

def _addr_from_generic_json(soup) -> Optional[Dict[str, str]]:
    """
    Generic JSON hunter: scans ANY <script> for common address key patterns.
    Helps on idx.homespotter.com, MLS/IDX sites without JSON-LD.
    """
    street_keys = ["streetAddress","address1","line1","addr1","address_line1","street"]
    city_keys   = ["addressLocality","city","locality","municipality","town"]
    state_keys  = ["addressRegion","state","stateCode","region","province"]
    zip_keys    = ["postalCode","zip","zipCode","postal","postal_code"]

    best = None
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if not txt or ("http" in txt and len(txt) > 200000):  # skip huge blobs with tons of urls
            pass
        s = _first_match(txt, street_keys)
        c = _first_match(txt, city_keys)
        st = _first_match(txt, state_keys)
        zp = _first_match(txt, zip_keys)
        # prefer 2-letter state if multiple
        if st and re.fullmatch(r"[A-Za-z]{2}", st):
            st = st.upper()
        if s and c and st:
            cand = {"streetAddress": s, "addressLocality": c, "addressRegion": st, "postalCode": (zp or "")}
            if _normalize(cand):
                # choose the "best" (longest street) if many
                if not best or len(cand["streetAddress"]) > len(best["streetAddress"]):
                    best = cand
    return best

def _addr_from_meta_or_title(soup) -> Optional[Dict[str, str]]:
    for m in soup.find_all("meta"):
        k = (m.get("property") or m.get("name") or "").lower()
        v = _clean(m.get("content"))
        if not k or not v: continue
        if k in ("og:title","twitter:title","og:description","twitter:description"):
            a = _pick_address_from_text(v)
            if a: return a
    t = _clean(soup.title.string if soup.title else None)
    if t:
        a = _pick_address_from_text(t)
        if a: return a
    return None

# Site-hinted JSON (kept from earlier versions for higher precision on big portals)
def _addr_from_redfin_json(soup) -> Optional[Dict[str, str]]:
    import re as _re
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if "__REDUX_STATE__" in txt or "propertyInfoStore" in txt:
            try:
                m = _re.search(r"__REDUX_STATE__\s*=\s*({.*?})\s*;", txt, _re.S)
                data = json.loads(m.group(1)) if m else None
            except Exception:
                data = None
            if not data: 
                continue
            candidates = [
                data.get("propertyInfo") or {},
                data.get("listingDetailsStore",{}).get("fullAddress") or {},
                data.get("propertyInfoStore",{}).get("addressInfo") or {},
            ]
            for obj in candidates:
                parts = {
                    "streetAddress": obj.get("streetAddress") or obj.get("line1"),
                    "addressLocality": obj.get("city") or obj.get("addressLocality"),
                    "addressRegion": obj.get("state") or obj.get("addressRegion"),
                    "postalCode": obj.get("zip") or obj.get("postalCode"),
                }
                if _normalize(parts): return parts
    return None

def _addr_from_zillow_json(soup) -> Optional[Dict[str, str]]:
    import re as _re
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if "streetAddress" in txt and "addressLocality" in txt and "addressRegion" in txt:
            m = _re.search(
                r'"streetAddress"\s*:\s*"([^"]+)"[^}]*"addressLocality"\s*:\s*"([^"]+)"[^}]*"addressRegion"\s*:\s*"([A-Z]{2})"[^}]*"postalCode"\s*:\s*"([^"]+)"',
                txt, _re.S
            )
            if m:
                return {
                    "streetAddress": m.group(1),
                    "addressLocality": m.group(2),
                    "addressRegion": m.group(3),
                    "postalCode": m.group(4),
                }
    return None

def _addr_from_realtor_json(soup) -> Optional[Dict[str, str]]:
    import re as _re
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        m = _re.search(
            r'"address"\s*:\s*{[^}]*"streetAddress"\s*:\s*"([^"]+)"[^}]*"addressLocality"\s*:\s*"([^"]+)"[^}]*"addressRegion"\s*:\s*"([A-Z]{2})"[^}]*("postalCode"\s*:\s*"([^"]+)")?',
            txt, _re.S
        )
        if m:
            return {
                "streetAddress": m.group(1),
                "addressLocality": m.group(2),
                "addressRegion": m.group(3),
                "postalCode": (m.group(5) or "").strip(),
            }
    return None

# -------------------- Public API --------------------

def extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]:
    """
    Resolve a listing URL (incl. short links) to:
    full, streetAddress, addressLocality, addressRegion, postalCode, source, url_final
    """
    # Lazy HTTP import
    try:
        import requests
    except Exception:
        parts = _addr_from_url_slug(url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": url}

    # Follow redirects
    try:
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
        parts = _addr_from_url_slug(url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": url}

    if not parse_html:
        parts = _addr_from_url_slug(final_url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": final_url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

    # BeautifulSoup (lazy)
    try:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(html_text, "html.parser")
    except Exception:
        parts = _addr_from_url_slug(final_url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": final_url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

    # 1) JSON-LD
    a = _addr_from_ld_json(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "json-ld", "url_final": final_url}

    # 2) Microdata (itemprop)
    a = _addr_from_microdata(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "microdata", "url_final": final_url}

    # 3) Site-hinted JSON for majors
    host = (urlparse(final_url).netloc or "").lower()
    if "redfin.com" in host:
        a = _addr_from_redfin_json(soup)
        if a and _normalize(a):
            return {**a, "full": _normalize(a), "source": "site-json", "url_final": final_url}
    if "zillow.com" in host:
        a = _addr_from_zillow_json(soup)
        if a and _normalize(a):
            return {**a, "full": _normalize(a), "source": "site-json", "url_final": final_url}
    if "realtor.com" in host:
        a = _addr_from_realtor_json(soup)
        if a and _normalize(a):
            return {**a, "full": _normalize(a), "source": "site-json", "url_final": final_url}

    # 4) Generic JSON hunter (works for many IDX pages incl. Homespotter)
    a = _addr_from_generic_json(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "generic-json", "url_final": final_url}

    # 5) Meta/title heuristics
    a = _addr_from_meta_or_title(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "meta/title", "url_final": final_url}

    # 6) URL slug fallback
    a = _addr_from_url_slug(final_url)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "url-slug", "url_final": final_url}

    return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

def address_as_markdown_link(url: str, label: Optional[str] = None, parse_html: bool = True, timeout: float = 12.0) -> Tuple[str, Dict[str, Optional[str]]]:
    info = extract_address(url, parse_html=parse_html, timeout=timeout)
    text = label or info.get("full")
    if not text:
        text = urlparse(info.get("url_final") or url).netloc or "Listing"
    return f"[{text}]({info.get('url_final') or url})", info
