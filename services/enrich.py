# services/enrich.py
from __future__ import annotations
import re, httpx, asyncio, requests
from typing import Dict, Any, List, Optional, Tuple
from core.config import REQUEST_TIMEOUT
from services.resolver import UA_HEADERS
from utils.html_tools import summarize_remarks, extract_highlights

RE_PRICE  = re.compile(r'"(?:price|unformattedPrice|priceZestimate)"\s*:\s*"?\$?([\d,]+)"?', re.I)
RE_STATUS = re.compile(r'"(?:homeStatus|statusText)"\s*:\s*"([^"]+)"', re.I)
RE_BEDS   = re.compile(r'"(?:bedrooms|beds)"\s*:\s*(\d+)', re.I)
RE_BATHS  = re.compile(r'"(?:bathrooms|baths)"\s*:\s*([0-9.]+)', re.I)
RE_SQFT   = re.compile(r'"(?:livingArea|livingAreaValue|area)"\s*:\s*([0-9,]+)', re.I)
RE_DESC   = re.compile(r'"(?:description|homeDescription|marketingDescription)"\s*:\s*"([^"]+)"', re.I)

def _fetch_html_sync(url: str) -> str:
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200: return r.text
    except Exception:
        pass
    return ""

async def _fetch_html_async(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url, headers=UA_HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200: return r.text
    except Exception:
        pass
    return ""

def extract_zillow_first_image(html: str) -> Optional[str]:
    if not html: return None
    for target_w in ("960","1152","768","1536"):
        m = re.search(
            rf"<img[^>]+src=['\"](https://photos\.zillowstatic\.com/fp/[^'\" ]+-cc_ft_{target_w}\.(?:jpg|webp))['\"]",
            html, re.I
        )
        if m: return m.group(1)
    m = re.search(r"srcset=['\"]([^'\"]*photos\.zillowstatic\.com[^'\"]+)['\"]", html, re.I)
    if m:
        cand=[]
        for part in m.group(1).split(","):
            part=part.strip(); m2=re.match(r"(https://photos\.zillowstatic\.com/\S+)\s+(\d+)w", part, re.I)
            if m2: cand.append((int(m2.group(2)), m2.group(1)))
        if cand:
            up=[u for (w,u) in cand if w<=1152]
            return (sorted(((w,u) for (w,u) in cand if w<=1152), key=lambda x:x[0])[-1][1] if up
                    else sorted(cand, key=lambda x:x[0])[-1][1])
    m = re.search(r"(https://photos\.zillowstatic\.com/fp/\S+-cc_ft_\d+\.(jpg|webp))", html, re.I)
    return m.group(1) if m else None

def parse_listing_meta(html: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if not html: return meta
    m = RE_PRICE.search(html);   meta["price"]  = m.group(1) if m else None
    m = RE_STATUS.search(html);  meta["status"] = m.group(1) if m else None
    m = RE_BEDS.search(html);    meta["beds"]   = m.group(1) if m else None
    m = RE_BATHS.search(html);   meta["baths"]  = m.group(1) if m else None
    m = RE_SQFT.search(html);    meta["sqft"]   = m.group(1) if m else None
    m = RE_DESC.search(html);    remark = m.group(1) if m else None
    if not remark:
        m2 = re.search(r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
        if m2: remark = m2.group(1)
    meta["remarks"] = remark
    img = extract_zillow_first_image(html)
    if not img:
        m3 = re.search(r"<meta[^>]+property=['\"]og:image['\"][^>]+content=['\"]([^'\"]+)['\"]", html, re.I)
        if m3: img = m3.group(1)
    meta["image_url"] = img
    meta["summary"] = summarize_remarks(remark or "")
    meta["highlights"] = extract_highlights(remark or "")
    return meta

async def enrich_results_async(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    targets = [(i, r["zillow_url"]) for i, r in enumerate(results) if "/homedetails/" in (r.get("zillow_url") or "")]
    if not targets: return results
    limits = min(12, max(4, len(targets)))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        sem = asyncio.Semaphore(limits)
        async def task(i, url):
            async with sem:
                html = await _fetch_html_async(client, url)
                return i, parse_listing_meta(html)
        coros = [task(i, url) for i, url in targets]
        for fut in asyncio.as_completed(coros):
            i, meta = await fut
            if meta: results[i].update(meta)
    return results
