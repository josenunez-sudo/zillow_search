# hs_resolver_service.py
# FastAPI + Playwright service to resolve Homespotter links → Zillow link by extracting the address after JS loads.

import asyncio, json, re
from typing import Dict, Any, List, Tuple, Iterable, Optional
from urllib.parse import quote

from fastapi import FastAPI, Query
from pydantic import BaseModel
from playwright.async_api import async_playwright

APP = FastAPI(title="Homespotter Resolver", version="1.0")

# --------- Helpers (JSON/HTML address extraction) ---------
def _json_walk(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _json_walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _json_walk(x)

_ADDR_KEYS = {
    "street": {"street", "street1", "streetaddress", "address1", "line1", "address", "displayaddress", "formattedaddress", "fulladdress"},
    "city":   {"city", "locality", "addresslocality"},
    "state":  {"state", "region", "addressregion", "statecode"},
    "zip":    {"zip", "zipcode", "postalcode"},
}
def _nk(s: str) -> str: return re.sub(r"[^a-z]", "", (s or "").lower())

def extract_address_from_json_any(text: str) -> Dict[str, str]:
    out = {"street":"", "city":"", "state":"", "zip":""}
    if not text: return out

    # Direct JSON
    candidates: List[Any] = []
    try:
        candidates.append(json.loads(text))
    except Exception:
        pass
    # var foo = {...};
    for m in re.finditer(r'=\s*({.*?})\s*[,;]\s*$', text, re.S | re.M):
        try:
            candidates.append(json.loads(m.group(1)))
        except Exception:
            continue
    # Heuristic single object
    if not candidates:
        for m in re.finditer(r'(\{[^{}]{30,}\})', text, re.S):
            try:
                candidates.append(json.loads(m.group(1)))
            except Exception:
                continue

    for data in candidates:
        try:
            for node in _json_walk(data):
                got: Dict[str, str] = {}
                for want, pool in _ADDR_KEYS.items():
                    for k, v in node.items():
                        if _nk(k) in pool and isinstance(v, str) and v.strip():
                            got[want] = v.strip()
                            break
                for k in ("street","city","state","zip"):
                    if got.get(k) and not out.get(k):
                        out[k] = got[k]
                if out["street"] and (out["city"] or out["state"]):
                    return out
        except Exception:
            continue
    return out

def _jsonld_blocks(html: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not html: return out
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
    return out

def _title_or_desc(html: str) -> str:
    if not html: return ""
    for pat in [
        r"<meta[^>]+property=['\"]og:title['\"][^>]+content=['\"]([^'\"]+)['\"]",
        r"<title>\s*([^<]+)</title>",
        r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]",
    ]:
        m = re.search(pat, html, re.I)
        if m: return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def extract_address_from_html(html: str) -> Dict[str,str]:
    out = {"street":"", "city":"", "state":"", "zip":""}
    if not html: return out
    # JSON-LD first
    for blk in _jsonld_blocks(html):
        addr = blk.get("address") or (blk.get("itemOffered",{}) or {}).get("address")
        if isinstance(addr, dict):
            out["street"] = out["street"] or (addr.get("streetAddress") or "")
            out["city"]   = out["city"]   or (addr.get("addressLocality") or "")
            rg = addr.get("addressRegion") or addr.get("addressCountry") or ""
            out["state"]  = out["state"]  or (rg[:2] if isinstance(rg, str) else "")
            out["zip"]    = out["zip"]    or (addr.get("postalCode") or "")
            if out["street"]: break
    # Meta bits / microdata / loose JSON
    if not out["street"]:
        pats = [
            (r'"street(Address|1)?"\s*:\s*"([^"]+)"', "street", 2),
            (r'itemprop=["\']streetAddress["\'][^>]*>\s*([^<]+)', "street", 1),
        ]
        for pat, key, gi in pats:
            m = re.search(pat, html, re.I)
            if m and not out[key]: out[key] = m.group(gi).strip()
    for pat, key in [
        (r'"addressLocality"\s*:\s*"([^"]+)"', "city"),
        (r'itemprop=["\']addressLocality["\'][^>]*>\s*([^<]+)', "city"),
        (r'"addressRegion"\s*:\s*"([A-Za-z]{2})"', "state"),
        (r'itemprop=["\']addressRegion["\'][^>]*>\s*([A-Za-z]{2})', "state"),
        (r'"postal(Code)?"\s*:\s*"(\d{5}(?:-\d{4})?)"', "zip"),
        (r'itemprop=["\']postalCode["\'][^>]*>\s*(\d{5}(?:-\d{4})?)', "zip"),
        (r'"displayAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"formattedAddress"\s*:\s*"([^"]+)"', "street"),
        (r'"fullAddress"\s*:\s*"([^"]+)"', "street"),
    ]:
        m = re.search(pat, html, re.I)
        if m and not out.get(key):
            out[key] = m.group(m.lastindex or 1).strip()
    if not out["street"]:
        t = _title_or_desc(html)
        if t and re.search(r"\b[A-Za-z]{2}\b", t) and re.search(r"\d{5}", t):
            out["street"] = t
    return out

def _compose_addr(street: str, city: str, state: str, zipc: str) -> str:
    parts = []
    if street: parts.append(street)
    loc = " ".join([p for p in [city, state] if p])
    if loc: parts.append(loc)
    if zipc and parts:
        parts[-1] = f"{parts[-1]} {zipc}"
    return re.sub(r"\s+", " ", ", ".join(parts)).strip()

def build_zillow_search_deeplink(street: str, city: str, state: str, zipc: str) -> str:
    term = _compose_addr(street, city, state, zipc)
    if not term:
        return "https://www.zillow.com/homes/"
    q = {
        "pagination": {},
        "usersSearchTerm": term,
        "mapBounds": {"west": -180, "east": 180, "south": -90, "north": 90},
        "isMapVisible": False,
        "filterState": {},
        "isListVisible": True,
    }
    return "https://www.zillow.com/homes/?searchQueryState=" + quote(json.dumps(q), safe="")

def find_homedetails_in_html(html: str) -> Optional[str]:
    if not html: return None
    m = re.search(r'https://www\.zillow\.com/homedetails/[^"\']+?_zpid/', html, re.I)
    return m.group(0) if m else None

# --------- Playwright resolve ----------
async def _resolve_with_browser(url: str, state_hint: str) -> Dict[str, Any]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 412, "height": 823},  # mobile-ish
            user_agent=("Mozilla/5.0 (Linux; Android 12; Pixel 5) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")
        )
        page = await ctx.new_page()

        captured_texts: List[str] = []

        async def grab_response(resp):
            try:
                ct = resp.headers.get("content-type","").lower()
                if "application/json" in ct or ct.endswith("+json"):
                    txt = await resp.text()
                    if txt and len(txt) < 1_500_000:
                        captured_texts.append(txt)
                # sometimes HTML endpoints return JSON (no correct header)
                if "text/html" in ct or "text/plain" in ct:
                    txt = await resp.text()
                    if txt and any(k in txt for k in ["street","address","postal","city","region"]):
                        if len(txt) < 1_500_000:
                            captured_texts.append(txt)
            except Exception:
                pass

        page.on("response", grab_response)
        await page.goto(url, wait_until="networkidle", timeout=45000)
        html = await page.content()
        cur_url = page.url

        await browser.close()

    # 1) Try DOM
    addr = extract_address_from_html(html)

    # 2) If incomplete, scour captured JSON
    if not addr.get("street") or not (addr.get("city") or addr.get("state")):
        for txt in captured_texts:
            aug = extract_address_from_json_any(txt)
            for k in ("street","city","state","zip"):
                if not addr.get(k) and aug.get(k):
                    addr[k] = aug[k]
            if addr.get("street") and (addr.get("city") or addr.get("state")):
                break

    street, city = addr.get("street",""), addr.get("city","")
    state = addr.get("state","") or (state_hint or "")
    zipc = addr.get("zip","")

    # Try to grab a homedetails link outright
    zurl = find_homedetails_in_html(html)
    if not zurl:
        zurl = build_zillow_search_deeplink(street, city, state, zipc)

    # Never return a blank homes root if we had a specific URL
    if zurl.strip().rstrip("/") == "https://www.zillow.com/homes":
        zurl = url

    return {
        "address": _compose_addr(street, city, state, zipc),
        "zillow_url": zurl,
        "final_url": cur_url,
    }

class ResolveOut(BaseModel):
    address: str
    zillow_url: str
    final_url: str

@APP.get("/resolve", response_model=ResolveOut)
async def resolve(url: str = Query(...), state: str = Query(default="NC")):
    return await _resolve_with_browser(url, state_hint=state)

# Run:
#   pip install fastapi uvicorn playwright
#   playwright install
#   uvicorn hs_resolver_service:APP --host 0.0.0.0 --port 8001
