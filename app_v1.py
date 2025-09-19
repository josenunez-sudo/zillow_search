import os, csv, io, re, time, json, streamlit as st
from datetime import datetime
import requests

# Read optional secrets -> env
for k in ["AZURE_SEARCH_ENDPOINT","AZURE_SEARCH_INDEX","AZURE_SEARCH_API_KEY","BING_API_KEY","BING_CUSTOM_CONFIG_ID"]:
    try:
        if k in st.secrets and st.secrets[k]: os.environ[k]=st.secrets[k]
    except Exception: pass

AZURE_SEARCH_ENDPOINT=os.getenv("AZURE_SEARCH_ENDPOINT","").rstrip("/")
AZURE_SEARCH_INDEX=os.getenv("AZURE_SEARCH_INDEX","")
AZURE_SEARCH_KEY=os.getenv("AZURE_SEARCH_API_KEY","")
BING_API_KEY=os.getenv("BING_API_KEY","")
BING_CUSTOM_ID=os.getenv("BING_CUSTOM_CONFIG_ID","")

BING_WEB="https://api.bing.microsoft.com/v7.0/search"
BING_CUSTOM="https://api.bing.microsoft.com/v7.0/custom/search"

ADDR_PRIMARY={"full_address","address","property address","property_address","site address","site_address","street address","street_address","listing address","listing_address","location"}
NUM_KEYS={"street #","street number","street_no","streetnum","house_number","number","streetnumber"}
NAME_KEYS={"street name","street","st name","st_name","road","rd","avenue","ave","blvd","boulevard","drive","dr","lane","ln","way","terrace","ter","court","ct","place","pl","parkway","pkwy","square","sq","circle","cir"}
SUF_KEYS={"suffix","st suffix","street suffix","suffix1","suffix2","street_type","street type"}
UNIT_KEYS={"unit","apt","apartment","suite","ste","lot","unit #","unit number","apt #","apt number"}
CITY_KEYS={"city","municipality","town"}
STATE_KEYS={"state","st","province","region"}
ZIP_KEYS={"zip","zip code","postal code","postalcode","zip_code","postal_code"}

def norm_key(k): return re.sub(r"\s+"," ",(k or "").strip().lower())
def get_first(row, keys):
    for k in row.keys():
        if norm_key(k) in keys:
            v=str(row[k]).strip()
            if v: return v
    return ""

def extract_address(row):
    n={norm_key(k):(str(v).strip() if v is not None else "") for k,v in row.items()}
    for k in list(n.keys()):
        if k in ADDR_PRIMARY and n[k]: return n[k]
    num=get_first(n,NUM_KEYS); name=get_first(n,NAME_KEYS); suf=get_first(n,SUF_KEYS)
    unit=get_first(n,UNIT_KEYS); city=get_first(n,CITY_KEYS); st=get_first(n,STATE_KEYS); zc=get_first(n,ZIP_KEYS)
    street=" ".join([x for x in [num,name,suf] if x]).strip()
    if unit:
        street=f"{street} Unit {unit}".strip() if (re.match(r'^[A-Za-z]?\d+$',unit) or unit.isdigit()) else f"{street} {unit}".strip()
    parts=[p for p in [street,city,st] if p]; addr=", ".join(parts)
    if zc: addr=(addr+" "+zc).strip()
    return addr

def construct_deeplink(addr):
    a=addr.lower(); a=re.sub(r"[^\w\s,-]","",a); a=a.replace(",",""); a=re.sub(r"\s+","-",a.strip())
    return f"https://www.zillow.com/homes/{a}_rb/"

def best_from_bing_items(items):
    if not items: return None, None, None
    def score(it):
        url=it.get("url") or it.get("link") or ""; s=0
        if "zillow.com" in url: s+=1
        if "/homedetails/" in url: s+=3
        if "/homes/" in url: s+=2
        if "zpid" in url: s+=1
        return s
    top=sorted(items,key=score,reverse=True)[0]
    return top.get("url") or top.get("link") or "", top.get("name") or top.get("title") or "", top.get("snippet") or ""

def resolve_with_bing(addr, delay=0.25):
    if not BING_API_KEY: return None, None, None
    h={"Ocp-Apim-Subscription-Key":BING_API_KEY}
    qs=[f"{addr} site:zillow.com/homedetails", f"{addr} site:zillow.com", f"\"{addr}\" site:zillow.com"]
    for q in qs:
        try:
            if BING_CUSTOM_ID:
                p={"q":q,"customconfig":BING_CUSTOM_ID,"mkt":"en-US","count":10}
                r=requests.get(BING_CUSTOM,headers=h,params=p,timeout=20)
            else:
                p={"q":q,"mkt":"en-US","count":10,"responseFilter":"Webpages"}
                r=requests.get(BING_WEB,headers=h,params=p,timeout=20)
            r.raise_for_status()
            data=r.json()
            items=data.get("webPages",{}).get("value") if "webPages" in data else data.get("items",[])
            url,title,snip=best_from_bing_items(items or [])
            if url: return url,title,snip
        except requests.HTTPError as e:
            if getattr(e.response,"status_code",None) in (429,500,502,503,504): time.sleep(1.0); continue
            break
        except requests.RequestException:
            time.sleep(0.8); continue
        finally:
            time.sleep(delay)
    return None, None, None

def resolve_with_azure_search(addr):
    if not (AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX and AZURE_SEARCH_KEY): return None, None, None
    url=f"{AZURE_SEARCH_ENDPOINT}/indexes/{AZURE_SEARCH_INDEX}/docs/search?api-version=2023-11-01"
    h={"Content-Type":"application/json","api-key":AZURE_SEARCH_KEY}
    try:
        r=requests.post(url,headers=h,data=json.dumps({"search":addr,"top":1}),timeout=20)
        r.raise_for_status()
        data=r.json() or {}; hits=data.get("value") or data.get("results") or []
        if not hits: return None, None, None
        doc=hits[0].get("document") or hits[0]
        for k in ("zillow_url","zillowLink","zillow","url","link"):
            v=doc.get(k) if isinstance(doc,dict) else None
            if isinstance(v,str) and "zillow.com" in v: return v,"From Azure AI Search",""
        parts=[]
        for k in ("street","address","street_address","city","state","zip","postalCode","postal_code"):
            v=doc.get(k) if isinstance(doc,dict) else None
            if v: parts.append(str(v))
        if parts:
            return construct_deeplink(", ".join(parts)), "Deeplink (from Azure fields)", ""
    except requests.RequestException:
        return None, None, None
    return None, None, None

def process_rows(rows, delay):
    out=[]
    for row in rows:
        addr=extract_address(row)
        addr=re.sub(r"\s+"," ",(addr or "")).strip()
        if not addr:
            addr=re.sub(r"\s+"," "," ".join([str(v).strip() for v in row.values() if isinstance(v,str)])).strip()
        url=title=snip=None
        u1,t1,s1=resolve_with_azure_search(addr)
        if u1: url,title,snip=u1,t1,s1
        if not url and BING_API_KEY:
            u2,t2,s2=resolve_with_bing(addr,delay=delay)
            if u2: url,title,snip=u2,t2,s2
        if not url:
            url=construct_deeplink(addr); title="Deeplink (constructed)"; snip=""
        out.append({"input_address":addr,"zillow_url":url,"title":title,"snippet":snip})
        time.sleep(min(delay,0.5))
    return out

def build_output(rows, fmt):
    if fmt=="csv":
        import io
        s=io.StringIO(); w=csv.DictWriter(s,fieldnames=["input_address","zillow_url","title","snippet"])
        w.writeheader(); w.writerows(rows); return s.getvalue(),"text/csv"
    if fmt=="md":
        text="\n".join([f"- [{r['input_address'] or r['zillow_url']}]({r['zillow_url']})" if r['zillow_url'] else f"- {r['input_address']} — _(no link found)_" for r in rows])+"\n"
        return text,"text/markdown"
    if fmt=="html":
        items=[]
        for r in rows:
            if r["zillow_url"]:
                label=r["input_address"] or r["zillow_url"]
                items.append(f'<li><a href="{r["zillow_url"]}" target="_blank" rel="noopener">{label}</a></li>')
            else:
                items.append(f'<li>{r["input_address"]} — <em>no link found</em></li>')
        return "<ul>\n"+"\n".join(items)+"\n</ul>\n","text/html"
    # txt
    text="\n".join([f"- {r['zillow_url']}" for r in rows if r['zillow_url']])+"\n"
    return text,"text/plain"

st.set_page_config(page_title="Zillow Link Finder", page_icon="🏠", layout="centered")
st.title("🏠 Zillow Link Finder")
st.caption("Upload a CSV → get Zillow links (CSV/Markdown/HTML/TXT).")

fmt=st.selectbox("Output format",["csv","md","html","txt"],index=0)
delay=st.slider("Delay between lookups (seconds)",0.0,2.0,0.3,0.1)
file=st.file_uploader("Upload CSV (must include a header row)",type=["csv"])

if file:
    try:
        content=file.read().decode("utf-8-sig")
        rows=list(csv.DictReader(io.StringIO(content)))
        results=process_rows(rows,delay=delay)
        st.success(f"Processed {len(results)} rows.")
        st.dataframe(results, use_container_width=True)
        ts=datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        name=f"zillow_links_{ts}.{fmt}"
        payload,mime=build_output(results,fmt)
        st.download_button("Download result", data=payload, file_name=name, mime=mime)
        if fmt in ("md","txt"):
            st.subheader("Preview"); st.code(payload, language="markdown" if fmt=="md" else "text")
    except Exception as e:
        st.error(f"Error: {e}")
else:
    st.info("Choose a CSV to begin.")
