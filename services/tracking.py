# services/tracking.py
import os, re, requests
from typing import Optional

BITLY_TOKEN = os.getenv("BITLY_TOKEN", "")

def make_trackable_url(url: str, client_tag: str, campaign_tag: str) -> str:
    client_tag = re.sub(r'[^a-z0-9\-]+','', (client_tag or "").lower().replace(" ","-"))
    campaign_tag = re.sub(r'[^a-z0-9\-]+','', (campaign_tag or "").lower().replace(" ","-"))
    frag = f"#aa={client_tag}.{campaign_tag}" if (client_tag or campaign_tag) else ""
    return (url or "") + (frag if url and frag else "")

def bitly_shorten(long_url: str) -> Optional[str]:
    token = BITLY_TOKEN
    if not token: return None
    try:
        r = requests.post(
            "https://api-ssl.bitly.com/v4/shorten",
            headers={"Authorization": f"Bearer {token}", "Content-Type":"application/json"},
            json={"long_url": long_url},
            timeout=10
        )
        if r.ok: return r.json().get("link")
    except Exception:
        return None
    return None
