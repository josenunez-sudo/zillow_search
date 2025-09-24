# ui/run_tab.py
# Run tab with: address-as-link parsing for Zillow/Redfin/Realtor/Trulia/Homes + short links (e.g., l.hms.pt),
# plus a simple UI. Exports: render_run_tab(st).

import os, re, json
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, unquote

import requests
import streamlit as st

# ---------- Optional deps ----------
try:
    from bs4 import BeautifulSoup  # type: ignore
except Exception:
    BeautifulSoup = None  # graceful degrade if bs4 isn't installed

# ---------- Supabase (kept lightweight; only if you already use it elsewhere) ----------
try:
    from supabase import create_client, Client
except Exception:
    create_client = None
    Client = None

@st.cache_resource
def get_supabase() -> Optional["Client"]:
    if not create_client:
        return None
    try:
        url = os.getenv("SUPABASE_URL", st.secrets.get("SUPABASE_URL", ""))
        key = os.getenv("SUPABASE_SERVICE_ROLE", st.secrets.get("SUPABASE_SERVICE_ROLE", ""))
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None


# =====================================================================================
#                           ADDRESS PARSER (self-contained)
# =====================================================================================

ENABLE_ADDRESS_FROM_LINK_DEFAULT = True

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": _UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

def _clean(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    return re.sub(r"\s+", " ", s).strip()

def _titleish(s: str) -> str:
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    parts = s.split()
    out = []
    for p in parts:
        if re.fullmatch(r"[A-Z]{2}", p):  # state codes
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
    m = re.search(r"(\d{1,6}[^,]+),\s*([A-Za-z .'-]+),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?", txt)
    if not m:
        return None
    return {
        "streetAddress": _titleish(m.group(1)),
        "addressLocality": _titleish(m.group(2)),
        "addressRegion": m.group(3),
        "postalCode": (m.group(4) or "").strip(),
    }

def _addr_from_ld_json(soup) -> Optional[Dict[str, str]]:
    if not BeautifulSoup:
        return None
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
    if not BeautifulSoup:
        return None
    # metas
    for m in soup.find_all("meta"):
        k = (m.get("property") or m.get("name") or "").lower()
        v = _clean(m.get("content"))
        if not k or not v:
            continue
        if k in ("og:title","twitter:title","og:description","twitter:description"):
            a = _pick_address_from_text(v)
            if a: return a
    # title
    t = _clean(soup.title.string if soup.title else None)
    if t:
        a = _pick_address_from_text(t)
        if a: return a
    return None

def _addr_from_redfin_json(soup) -> Optional[Dict[str, str]]:
    if not BeautifulSoup:
        return None
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if "__REDUX_STATE__" in txt:
            try:
                m = re.search(r"__REDUX_STATE__\s*=\s*({.*?})\s*;", txt, re.S)
                if not m:
                    continue
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
                if _normalize(parts):
                    return parts
    return None

def _addr_from_zillow_json(soup) -> Optional[Dict[str, str]]:
    if not BeautifulSoup:
        return None
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        if "streetAddress" in txt and "addressLocality" in txt and "addressRegion" in txt:
            m = re.search(
                r'"streetAddress"\s*:\s*"([^"]+)"[^}]*"addressLocality"\s*:\s*"([^"]+)"[^}]*"addressRegion"\s*:\s*"([A-Z]{2})"[^}]*"postalCode"\s*:\s*"([^"]+)"',
                txt, re.S
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
    if not BeautifulSoup:
        return None
    for tag in soup.find_all("script"):
        txt = tag.string or tag.text or ""
        m = re.search(
            r'"address"\s*:\s*{[^}]*"streetAddress"\s*:\s*"([^"]+)"[^}]*"addressLocality"\s*:\s*"([^"]+)"[^}]*"addressRegion"\s*:\s*"([A-Z]{2})"[^}]*("postalCode"\s*:\s*"([^"]+)")?',
            txt, re.S
        )
        if m:
            return {
                "streetAddress": m.group(1),
                "addressLocality": m.group(2),
                "addressRegion": m.group(3),
                "postalCode": (m.group(5) or "").strip(),
            }
    return None

def _addr_from_url_slug(url: str) -> Optional[Dict[str, str]]:
    p = urlparse(url)
    host = p.netloc.lower()
    path = unquote(p.path or "")

    if "zillow.com" in host:
        m = re.search(r"/homedetails/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, re.I)
        if m:
            return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    if "redfin.com" in host:
        m1 = re.search(r"^/([A-Z]{2})/([^/]+)/([\w\-]+)-(\d{5})(?:/|$)", path, re.I)
        if m1:
            st_ = m1.group(1).upper()
            city = _titleish(m1.group(2))
            street = _titleish(m1.group(3))
            zipc = m1.group(4)
            return {"streetAddress": street, "addressLocality": city, "addressRegion": st_, "postalCode": zipc}

    if "trulia.com" in host:
        m = re.search(r"/home/([\w\-]+)-([a-z ]+)-([a-z]{2})-(\d{5})", path, re.I)
        if m:
            return _mk_parts(m.group(1), m.group(2), m.group(3), m.group(4))

    if "homes.com" in host:
        m = re.search(r"/([a-z\-]+)-([a-z]{2})/([\w\-]+)-(\d{5})", path, re.I)
        if m:
            city = _titleish(m.group(1).replace("-", " "))
            st_ = m.group(2).upper()
            street = _titleish(m.group(3))
            zipc = m.group(4)
            return {"streetAddress": street, "addressLocality": city, "addressRegion": st_, "postalCode": zipc}

    # Realtor.com usually needs JSON.
    return None

def _mk_parts(street_slug: str, city_slug: str, st: str, zipc: str) -> Dict[str, str]:
    return {
        "streetAddress": _titleish(street_slug),
        "addressLocality": _titleish(city_slug),
        "addressRegion": st.upper(),
        "postalCode": zipc,
    }

def extract_address(url: str, parse_html: bool = True, timeout: float = 12.0) -> Dict[str, Optional[str]]:
    """
    Resolve a listing URL (incl. short links) to:
    full, streetAddress, addressLocality, addressRegion, postalCode, source, url_final
    """
    # Try HTTP fetch + redirects
    try:
        resp = _SESSION.get(url, allow_redirects=True, timeout=timeout)
        resp.raise_for_status()
    except Exception:
        # network error → fallback to slug parsing on original URL
        parts = _addr_from_url_slug(url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": url}

    final_url = resp.url

    # If no BeautifulSoup or parsing disabled, slug fallback only
    if not parse_html or not BeautifulSoup:
        parts = _addr_from_url_slug(final_url)
        if parts and _normalize(parts):
            return {**parts, "full": _normalize(parts), "source": "url-slug", "url_final": final_url}
        return {"full": None, "streetAddress": None, "addressLocality": None, "addressRegion": None, "postalCode": None, "source": "unknown", "url_final": final_url}

    soup = BeautifulSoup(resp.text, "html.parser")

    # 1) JSON-LD
    a = _addr_from_ld_json(soup)
    if a and _normalize(a):
        return {**a, "full": _normalize(a), "source": "json-ld", "url_final": final_url}

    host = urlparse(final_url).netloc.lower()

    # 2) site JSONs
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

    # 4) slug
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


# =====================================================================================
#                              EXPORTED TAB RENDERER
# =====================================================================================

def render_run_tab() -> None:
    """Entry point called by app.py"""
    st.title("Address Alchemist — Run")
    st.caption("Paste addresses *or* listing links. We’ll render clean address-as-links and log sends.")

    input_text = st.text_area("Paste addresses or listing URLs", height=140, placeholder="One per line…")

    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        do_parse = st.button("Run")
    with c2:
        show_markdown = st.checkbox("Show Markdown list", value=True)
    with c3:
        enable_html_parsing = st.checkbox(
            "Parse from page HTML (JSON-LD/site JSON)",
            value=ENABLE_ADDRESS_FROM_LINK_DEFAULT and bool(BeautifulSoup),
            help="If off (or bs4 not installed), falls back to URL slug only."
        )

    results: List[Dict[str, Any]] = []

    def _looks_like_url(s: str) -> bool:
        return bool(re.match(r"^https?://", s.strip(), re.I))

    def _clean_lines(block: str) -> List[str]:
        return [ln.strip() for ln in (block or "").splitlines() if ln.strip()]

    if do_parse:
        lines = _clean_lines(input_text)
        for raw in lines:
            if _looks_like_url(raw):
                link_md, info = address_as_markdown_link(raw, parse_html=enable_html_parsing)
                results.append({
                    "input": raw,
                    "type": "url",
                    "address": info.get("full"),
                    "source": info.get("source"),
                    "url_final": info.get("url_final"),
                    "link_md": link_md
                })
            else:
                # raw address passthrough (you can add your own normalization here)
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
                f"- {r['link_md'] if r['type']=='url' else r['address']}"
                for r in results
            ))
        else:
            import pandas as pd
            st.dataframe(pd.DataFrame(results)[["address","url_final","source","input"]])

    # (Optional) Use get_supabase() here if you want to insert logs into your `sent` table.
    # For example:
    # sb = get_supabase()
    # if sb and do_parse:
    #     for r in results:
    #         if r["type"] == "url" and r["address"]:
    #             try:
    #                 sb.table("sent").insert({
    #                     "client": "inline",  # replace with your client key
    #                     "campaign": None,
    #                     "url": r["url_final"],
    #                     "canonical": None,
    #                     "zpid": None,
    #                     "mls_id": None,
    #                     "address": r["address"],
    #                 }).execute()
    #             except Exception:
    #                 pass


# Optional explicit export list (helps certain import linters)
__all__ = ["render_run_tab", "extract_address", "address_as_markdown_link"]
