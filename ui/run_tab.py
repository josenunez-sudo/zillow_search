# ui/run_tab.py
# Exports: render_run_tab(**kwargs)
# Import-safe: only stdlib at top level. Non-stdlib imports happen inside the function.

import re, json
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, unquote

# -------------------- lightweight, no-fail helpers (stdlib only) --------------------

def _clean(s: Optional[str]) -> Optional[str]:
    if not s: return s
    import re as _re
    return _re.sub(r"\s+", " ", s).strip()

def _titleish(s: str) -> str:
    import re as _re
    s = s.replace("-", " ")
    s = _re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    out = []
    for p in parts:
        if _re.fullmatch(r"[A-Z]{2}", p):
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

def _pick_address_from_text(txt: str) -> Optional[Dict[str, str]]:
    import re as _re
    m = _re.search(r"(\d{1,6}[^,]+),\s*([A-Za-z .'-]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", txt)
    if not m: return None
    return {
        "streetAddress": _titleish(m.group(1)),
        "addressLocality": _titleish(m.group(2)),
        "addressRegion": m.group(3),
        "postalCode": (m.group(4) or "").strip(),
    }

def _addr_from_url_slug(url: str) -> Optional[Dict[str, str]]:
    import re as _re
    p = urlparse(url)
    host = (p.netloc or "").lower()
    path = unquote(p.path or "")

    if "zillow.com" in host:
        m = _re.search(r"/homedetails/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, _re.I)
        if m: return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    if "redfin.com" in host:
        m = _re.search(r"^/([A-Z]{2})/([^/]+)/([\w\-]+)-(\d{5})(?:/|$)", path, _re.I)
        if m:
            st_ = m.group(1).upper()
            city = _titleish(m.group(2))
            street = _titleish(m.group(3))
            zipc = m.group(4)
            return {"streetAddress": street, "addressLocality": city, "addressRegion": st_, "postalCode": zipc}

    if "trulia.com" in host:
        m = _re.search(r"/home/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, _re.I)
        if m: return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    if "homes.com" in host:
        m = _re.search(r"/([a-z\-]+)-([a-z]{2})/([\w\-]+)-(\d{5})", path, _re.I)
        if m:
            city = _titleish(m.group(1).replace("-", " "))
            st_ = m.group(2).upper()
            street = _titleish(m.group(3))
            zipc = m.group(4)
            return {"streetAddress": street, "addressLocality": city, "addressRegion": st_, "postalCode": zipc}

    return None

def _mk_parts(street_slug: str, city_slug: str, st: str, zipc: str) -> Dict[str, str]:
    return {
        "streetAddress": _titleish(street_slug),
        "addressLocality": _titleish(city_slug),
        "addressRegion": st.upper(),
        "postalCode": zipc,
    }

# -------------------- HTML-based parsers (loaded lazily inside function) --------------------

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
        if not k or not v: continue
        if k in ("og:title","twitter:title","og:description","twitter:description"):
            a = _pick_address_from_text(v)
            if a: return a
    t = _clean(soup.title.string if soup.title else None)
    if t:
        a = _pick_address_from_text(t)
        if a: return a
    return None

def _addr_from_redfin_json(soup) -> Optional[Dict[str, str]]:
    import re as _re
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if "__REDUX_STATE__" in txt:
            try:
                m = _re.search(r"__REDUX_STATE__\s*=\s*({.*?})\s*;", txt, _re.S)
                if not m: continue
                data = json.loads(m.group(1))
            except Exception:
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

# -------------------- PUBLIC API: safe to import anywhere --------------------

def extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]:
    """
    Resolve a listing URL (incl. short links) to:
    full, streetAddress, addressLocality, addressRegion, postalCode, source, url_final
    """
    # Lazy imports to avoid ImportError at module import time
    try:
        import requests
    except Exception:
        parts = _addr_from_url_slug(url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": url}

    try:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })
        r = sess.get(url, allow_redirects=True, timeout=timeout)
        r.raise_for_status()
        final_url = r.url
    except Exception:
        parts = _addr_from_url_slug(url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": url}

    # If HTML parsing disabled, fall back to slug
    if not parse_html:
        parts = _addr_from_url_slug(final_url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": final_url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

    # Try BeautifulSoup; if missing, fall back to slug
    try:
        from bs4 import BeautifulSoup as _BS
        soup = _BS(r.text, "html.parser")
    except Exception:
        parts = _addr_from_url_slug(final_url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": final_url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

    # 1) JSON-LD
    a = _addr_from_ld_json(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "json-ld", "url_final": final_url}

    host = (urlparse(final_url).netloc or "").lower()

    # 2) site JSON blobs
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

    # 3) meta/title
    a = _addr_from_meta_or_title(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "meta/title", "url_final": final_url}

    # 4) URL slug
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

# -------------------- EXPORTED TAB (lazy-imports all heavy deps) --------------------

def render_run_tab(**kwargs) -> None:
    """
    Entry point called by app.py. Accepts arbitrary kwargs (e.g., state=st.session_state).
    """
    import streamlit as st

    # Accept but don't require external state
    state = kwargs.get("state", st.session_state)

    st.title("Address Alchemist — Run")
    st.caption("Paste addresses or listing URLs. We’ll render clean address-as-links and (optionally) log sends.")

    input_text = st.text_area("Paste addresses or listing URLs", height=140, placeholder="One per line…")

    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        do_parse = st.button("Run")
    with c2:
        show_markdown = st.checkbox("Show Markdown list", value=True)
    with c3:
        parse_html = st.checkbox(
            "Parse from page HTML (JSON-LD/site JSON)",
            value=True,
            help="If off, falls back to URL slug only (works for many Redfin/Trulia/Zillow/Homes URLs)."
        )

    results: List[Dict[str, Any]] = []

    def _looks_like_url(s: str) -> bool:
        import re as _re
        return bool(_re.match(r"^https?://", s.strip(), _re.I))

    def _clean_lines(block: str) -> List[str]:
        return [ln.strip() for ln in (block or "").splitlines() if ln.strip()]

    if do_parse:
        lines = _clean_lines(input_text)
        for raw in lines:
            if _looks_like_url(raw):
                link_md, info = address_as_markdown_link(raw, parse_html=parse_html)
                results.append({
                    "input": raw,
                    "type": "url",
                    "address": info.get("full"),
                    "source": info.get("source"),
                    "url_final": info.get("url_final"),
                    "link_md": link_md
                })
            else:
                results.append({
                    "input": raw,
                    "type": "address",
                    "address": raw,
                    "source": "manual",
                    "url_final": None,
                    "link_md": raw
                })

        st.subheader("Results")
        if show_markdown:
            st.markdown("\n".join(
                f"- {r['link_md'] if r['type']=='url' else r['address']}" for r in results
            ))
        else:
            try:
                import pandas as pd
                st.dataframe(pd.DataFrame(results)[["address","url_final","source","input"]])
            except Exception:
                for r in results:
                    st.write(r)

# Optional explicit export
__all__ = ["render_run_tab", "extract_address", "address_as_markdown_link"]
