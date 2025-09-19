import re, requests
from core.config import BITLY_TOKEN

def make_trackable_url(url: str, client_tag: str, campaign_tag: str) -> str:
    client_tag = re.sub(r'[^a-z0-9\-]+','', (client_tag or "").lower().replace(" ","-"))
    campaign_tag = re.sub(r'[^a-z0-9\-]+','', (campaign_tag or "").lower().replace(" ","-"))
    frag = f"#aa={client_tag}.{campaign_tag}" if (client_tag or campaign_tag) else ""
    return (url or "") + (frag if url and frag else "")

def bitly_shorten(long_url: str) -> str | None:
    if not BITLY_TOKEN: return None
    try:
        r = requests.post("https://api-ssl.bitly.com/v4/shorten",
                          headers={"Authorization": f"Bearer {BITLY_TOKEN}", "Content-Type":"application/json"},
                          json={"long_url": long_url}, timeout=10)
        if r.ok: return r.json().get("link")
    except Exception:
        return None
    return None
