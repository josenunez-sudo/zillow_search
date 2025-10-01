# maintenance_dedupe_sent.py
# -*- coding: utf-8 -*-

import os, re, csv
from datetime import datetime
from collections import defaultdict

try:
    from supabase import create_client
except Exception as e:
    raise SystemExit("pip install supabase==2.*  (or @latest)\n" + str(e))

# ----------------- address normalization (street-only slug) -----------------
_STTYPE = {
    "street":"st","st":"st","st.":"st",
    "avenue":"ave","ave":"ave","ave.":"ave","av":"ave","av.":"ave",
    "road":"rd","rd":"rd","rd.":"rd",
    "drive":"dr","dr":"dr","dr.":"dr",
    "lane":"ln","ln":"ln","ln.":"ln",
    "boulevard":"blvd","blvd":"blvd","blvd.":"blvd",
    "court":"ct","ct":"ct","ct.":"ct",
    "place":"pl","pl":"pl","pl.":"pl",
    "terrace":"ter","ter":"ter","ter.":"ter",
    "highway":"hwy","hwy":"hwy","hwy.":"hwy",
    "parkway":"pkwy","pkwy":"pkwy","pkwy.":"pkwy",
    "circle":"cir","cir":"cir","cir.":"cir",
    "square":"sq","sq":"sq","sq.":"sq",
    "way":"wy","wy":"wy","wy.":"wy"
}
_DIR = {"north":"n","n":"n","south":"s","s":"s","east":"e","e":"e","west":"w","w":"w",
        "n.":"n","s.":"s","e.":"e","w.":"w"}

STATE_2 = r"(?:A[LKZR]|C[AOT]|D[EC]|F[LM]|G[AU]|H[IW]|I[ADLN]|K[SY]|L[A]|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|P[A]|R[IL]|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])"

def _token_norm(tok):
    t = (tok or "").lower().strip(" .,#")
    if t in _STTYPE: return _STTYPE[t]
    if t in _DIR:    return _DIR[t]
    if t in {"apt","unit","ste","suite","lot","#"}: return ""
    return re.sub(r"[^a-z0-9-]", "", t)

def _norm_slug_from_text(text):
    s = (text or "").lower().replace("&", " and ")
    toks = re.split(r"[^a-z0-9]+", s)
    norm = [t for t in (_token_norm(t) for t in toks) if t]
    return "-".join(norm)

_HD = re.compile(r"/homedetails/([^/]+)/\d{6,}_zpid/?", re.I)
_HM = re.compile(r"/homes/([^/_]+)_rb/?", re.I)

def _address_text_from_url(url):
    u = (url or "").strip()
    m = _HD.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    m = _HM.search(u)
    if m: return re.sub(r"[-+]", " ", m.group(1)).title()
    return ""

def _split_addr(addr):
    a = (addr or "").strip()
    if not a: return "", "", "", ""
    a = re.sub(r"\s+", " ", a)
    if "," in a:
        parts = [p.strip() for p in a.split(",")]
        street = parts[0] if parts else ""
        rest = " ".join(parts[1:]).strip()
        m = re.search(rf"\b({STATE_2})\b(?:\s+(\d{{5}}(?:-\d{{4})?))?\s*$", rest, re.I)
        state = (m.group(1) if m else "").upper()
        zipc  = (m.group(2) if (m and m.lastindex and m.lastindex >= 2) else "")
        city  = rest[:m.start()].strip() if m else rest
        return street, city, state, zipc
    m2 = re.search(rf"\b({STATE_2})\b(?:\s+(\d{{5}}(?:-\d{{4})?))?\s*$", a, re.I)
    head = a[:m2.start()].strip() if m2 else a
    return head, "", "", ""

def _strip_unit_tail(street):
    return re.sub(r"\b(?:apt|unit|suite|ste|lot|#)\s*[A-Za-z0-9\-]*\s*$", "", street or "", flags=re.I).strip()

def _canonicalize_street(street):
    s = _strip_unit_tail(street or "")
    s = re.sub(r"\s+", " ", s).strip().strip(",")
    if not s: return ""
    parts = s.split()
    num = parts[0] if parts and parts[0].isdigit() else ""
    idx = 1 if num else 0
    lead_dir = ""
    if idx < len(parts) and parts[idx].lower().strip(".,") in _DIR:
        lead_dir = _DIR[parts[idx].lower().strip(".,")]
        idx += 1
    st_type = ""
    tail_dir = ""
    j = len(parts) - 1
    if j >= idx:
        last = parts[j].lower().strip(".,")
        if last in _DIR:
            tail_dir = _DIR[last]; j -= 1
            last = parts[j].lower().strip(".,") if j >= idx else ""
        if last in _STTYPE:
            st_type = _STTYPE[last]; j -= 1
    core = [re.sub(r"[^\w-]", "", t) for t in parts[idx:j+1] if t]
    small = {"of","and","the","at","in","on"}
    core_disp = []
    for k, tok in enumerate(core):
        core_disp.append(tok.lower() if (k>0 and tok.lower() in small) else (tok[:1].upper()+tok[1:].lower()))
    out = []
    if num: out.append(num)
    if lead_dir: out.append(lead_dir.upper())
    if core_disp: out.append(" ".join(core_disp))
    if st_type: out.append(st_type.upper())
    elif tail_dir: out.append(tail_dir.upper())
    return " ".join(out).strip()

def _street_only(addr):
    street, _, _, _ = _split_addr(addr)
    return _canonicalize_street(street)

def _norm_slug_from_url(url):
    u = (url or "").strip()
    m = _HD.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    m = _HM.search(u)
    if m: return _norm_slug_from_text(m.group(1))
    return ""

def property_key(row):
    """Stable key per (client, property). Street-only → slug; fallback to url/canonical/zpid."""
    url = (row.get("url") or "").strip()
    addr = (row.get("address") or "").strip() or _address_text_from_url(url)
    street = _street_only(addr)
    slug = _norm_slug_from_url(url) or _norm_slug_from_text(street)
    if slug:
        return "normslug::" + slug
    canon = (row.get("canonical") or "").strip().lower()
    if canon:
        return "canon::" + canon
    zpid = (row.get("zpid") or "").strip()
    if zpid:
        return "zpid::" + zpid
    return "url::" + url.lower()

def best_ts(row):
    raw = (row.get("sent_at") or "").strip()
    if not raw:
        return datetime.min
    try:
        return datetime.fromisoformat(raw.replace("Z","+00:00"))
    except Exception:
        return datetime.min

# ----------------- main -----------------
def main():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE", "")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE in your environment.")

    sb = create_client(url, key)

    # 1) Pull all sent rows (adjust LIMIT if you have a huge table)
    cols = "id,client,url,address,sent_at,campaign,canonical,zpid"
    rows = sb.table("sent").select(cols).order("client").order("sent_at", desc=True).limit(250000).execute().data or []
    if not rows:
        print("No rows in sent.")
        return

    # 2) Group by (client, property_key) and keep the latest (by sent_at; tie-breaker highest id)
    by_group = defaultdict(list)
    for r in rows:
        k = (r.get("client") or "").strip().lower()
        pk = property_key(r)
        by_group[(k, pk)].append(r)

    keep_ids = set()
    delete_ids = []

    for (client_norm, pk), items in by_group.items():
        items.sort(key=lambda r: (best_ts(r), r.get("id") or 0))
        keeper = items[-1]
        keep_ids.add(keeper["id"])
        for r in items[:-1]:
            if r.get("id") not in keep_ids:
                delete_ids.append(r["id"])

    # 3) Backup to CSV before deleting
    with open("sent_dupes_to_delete.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id","client","address","url","sent_at","campaign","canonical","zpid","group_key"])
        for (client_norm, pk), items in by_group.items():
            items.sort(key=lambda r: (best_ts(r), r.get("id") or 0))
            for r in items[:-1]:
                w.writerow([r.get("id"), r.get("client"), r.get("address"), r.get("url"),
                            r.get("sent_at"), r.get("campaign"), r.get("canonical"), r.get("zpid"), pk])

    with open("sent_kept.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id","client","address","url","sent_at","campaign","canonical","zpid","group_key"])
        for (client_norm, pk), items in by_group.items():
            items.sort(key=lambda r: (best_ts(r), r.get("id") or 0))
            r = items[-1]
            w.writerow([r.get("id"), r.get("client"), r.get("address"), r.get("url"),
                        r.get("sent_at"), r.get("campaign"), r.get("canonical"), r.get("zpid"), pk])

    print("Groups:", len(by_group), "to delete:", len(delete_ids), "to keep:", len(keep_ids))

    if not delete_ids:
        print("Nothing to delete.")
        return

    # 4) Delete in batches
    BATCH = 1000
    for i in range(0, len(delete_ids), BATCH):
        chunk = delete_ids[i:i+BATCH]
        sb.table("sent").delete().in_("id", chunk).execute()
        print("Deleted", len(chunk), "rows")

    print("Done. CSV backups written: sent_dupes_to_delete.csv, sent_kept.csv")

if __name__ == "__main__":
    main()
