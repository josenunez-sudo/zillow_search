# utils/html_tools.py
from __future__ import annotations
import re
from typing import List

def _tidy_txt(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip()

def summarize_remarks(text: str, max_sent: int = 2) -> str:
    text = _tidy_txt(text)
    if not text: return ""
    sents = re.split(r'(?<=[\.\!\?])\s+', text)
    if len(sents) <= max_sent: return text
    pref_kw = [
        "updated","renovated","new","roof","hvac","kitchen","bath","floor","windows",
        "mechanicals","acres","acre","lot","school","zoned","hoa","no hoa"
    ]
    scored = [(sum(1 for k in pref_kw if k in s.lower()), i, s) for i,s in enumerate(sents[:8])]
    scored.sort(key=lambda x:(-x[0], x[1]))
    return " ".join([s for _,_,s in scored[:max_sent]])

KEY_HL = [
    ("new roof","roof"),("hvac","hvac"),("ac unit","ac"),("furnace","furnace"),("water heater","water heater"),
    ("renovated","renovated"),("updated","updated"),("remodeled","remodeled"),("open floor plan","open plan"),
    ("cul-de-sac","cul-de-sac"),("pool","pool"),("fenced","fenced"),("acre","acre"),("hoa","hoa"),
    ("primary on main","primary on main"),("finished basement","finished basement")
]

def extract_highlights(text: str) -> List[str]:
    t = (text or "").lower(); out=[]
    for pat,label in KEY_HL:
        if pat in t: out.append(label)
    # de-dupe, max 6
    return list(dict.fromkeys(out))[:6]
