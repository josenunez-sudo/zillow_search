# ====== DROP-IN: helpers & core routines for Tours rules ======
import re
from datetime import date
from typing import List, Dict, Any, Tuple

def _slug_addr(addr: str) -> str:
    a = (addr or "").strip().lower()
    a = re.sub(r"[^\w\s,-]", "", a)
    a = a.replace(",", "")
    a = re.sub(r"\s+", "-", a).strip("-")
    return a

def _ordinal(n: int) -> str:
    # 1 -> 1st, 2 -> 2nd, 3 -> 3rd, 4 -> 4th, etc.
    if 10 <= (n % 100) <= 20: suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def _address_to_deeplink(addr: str) -> str:
    slug = _slug_addr(addr)
    return f"https://www.zillow.com/homes/{slug}_rb/"

def _create_or_get_tour(client_norm: str, client_display: str, tour_url: str, tour_date: date) -> Tuple[int, bool]:
    """
    Return (tour_id, created_flag).
    - Enforces no duplicate tour dates per client.
    - If found, we reuse that tour (and can update URL if previously missing).
    """
    # Look for an existing tour on the same date
    q = SUPABASE.table("tours")\
        .select("id,url,status")\
        .eq("client", client_norm)\
        .eq("tour_date", tour_date.isoformat())\
        .limit(1)\
        .execute()
    rows = q.data or []
    if rows:
        tour_id = rows[0]["id"]
        old_url = (rows[0].get("url") or "").strip()
        if tour_url and not old_url:
            # update missing URL, don't change status here
            SUPABASE.table("tours").update({"url": tour_url}).eq("id", tour_id).execute()
        return tour_id, False

    # Create fresh tour (unique(client,tour_date) protects us)
    payload = {
        "client": client_norm,
        "client_display": client_display,
        "url": tour_url or None,
        "tour_date": tour_date.isoformat(),
        "status": "saved",  # saved/finalized
    }
    ins = SUPABASE.table("tours").insert(payload).execute()
    if not ins.data:
        raise RuntimeError(f"Create tour failed: {ins.dict() if hasattr(ins, 'dict') else ins}")
    return ins.data[0]["id"], True

def _insert_stops_for_tour(tour_id: int, stops: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Insert a batch of stops for a tour.
    - Skips duplicates inside the same tour (unique index catches; we filter proactively).
    """
    # Fetch existing slugs for this tour to pre-dedupe
    existing = SUPABASE.table("tour_stops").select("address_slug").eq("tour_id", tour_id).execute().data or []
    existing_slugs = { (r.get("address_slug") or "") for r in existing }

    rows = []
    for s in stops:
        addr = (s.get("address") or "").strip()
        if not addr:
            continue
        slug = _slug_addr(addr)
        if slug in existing_slugs:
            continue  # already present for this tour date
        start = (s.get("start") or "").strip()
        end   = (s.get("end") or "").strip()
        href  = (s.get("deeplink") or "").strip() or _address_to_deeplink(addr)
        rows.append({
            "tour_id": tour_id,
            "address": addr,
            "address_slug": slug,
            "start": start or None,
            "end": end or None,
            "deeplink": href,
        })
        existing_slugs.add(slug)

    if not rows:
        return {"inserted": 0, "skipped": 0}

    ins = SUPABASE.table("tour_stops").insert(rows).execute()
    if ins.data is None:
        # If PostgREST returns [] on conflict skip (depending on policies), treat as success on the ones allowed
        return {"inserted": 0, "skipped": len(rows)}
    return {"inserted": len(ins.data or []), "skipped": len(rows) - len(ins.data or [])}

def _build_repeat_map(client_norm: str) -> Dict[Tuple[str, str], int]:
    """
    Compute visit number per (address_slug, tour_date) for a client.
    Returns a dict keyed by (address_slug, iso_date) -> visit_num (1,2,3…).
    """
    # Get all tours for client in chronological order
    tq = SUPABASE.table("tours")\
        .select("id,tour_date")\
        .eq("client", client_norm)\
        .order("tour_date", desc=False)\
        .limit(5000)\
        .execute()
    tours = tq.data or []
    if not tours:
        return {}
    tour_ids = [t["id"] for t in tours]

    # Fetch all stops for those tours
    sq = SUPABASE.table("tour_stops")\
        .select("tour_id,address_slug")\
        .in_("tour_id", tour_ids)\
        .limit(50000)\
        .execute()
    stops = sq.data or []

    # Map: tour_id -> tour_date
    id_to_date = {t["id"]: t["tour_date"] for t in tours}

    # Build counts per slug as we advance by tour_date
    repeat_map: Dict[Tuple[str, str], int] = {}
    seen_counts: Dict[str, int] = {}
    # Group stops by date, but we already have tours sorted by date
    tour_to_stops: Dict[int, List[str]] = {}
    for s in stops:
        tour_to_stops.setdefault(s["tour_id"], []).append(s["address_slug"])

    for t in tours:
        td = t["tour_date"]
        for slug in tour_to_stops.get(t["id"], []):
            seen_counts[slug] = seen_counts.get(slug, 0) + 1
            repeat_map[(slug, td)] = seen_counts[slug]

    return repeat_map

def _render_client_tours_report(client_display: str, client_norm: str):
    """
    Show the client's tours as:
      [9:00–9:30]  123 Main St, City, ST  (2nd showing)
    Each address is a clickable hyperlink (deeplink/Zillow).
    """
    # Fetch tours (latest first)
    tq = SUPABASE.table("tours")\
        .select("id,tour_date")\
        .eq("client", client_norm)\
        .order("tour_date", desc=True)\
        .limit(2000)\
        .execute()
    tours = tq.data or []
    if not tours:
        st.info("No tours logged for this client yet.")
        return

    # Build repeat map (address_slug, date) -> visit_num
    repeat_map = _build_repeat_map(client_norm)

    # Render grouped by date
    for t in tours:
        td = t["tour_date"]  # ISO date
        st.markdown(f"##### {td}")

        sq = SUPABASE.table("tour_stops")\
            .select("address,address_slug,start,end,deeplink")\
            .eq("tour_id", t["id"])\
            .order("start", desc=False)\
            .limit(500)\
            .execute()
        stops = sq.data or []

        if not stops:
            st.caption("_(No stops logged for this date.)_")
            continue

        li = []
        for s in stops:
            addr = s.get("address") or ""
            slug = s.get("address_slug") or _slug_addr(addr)
            start = (s.get("start") or "").strip()
            end   = (s.get("end") or "").strip()
            href  = (s.get("deeplink") or "").strip() or _address_to_deeplink(addr)

            time_txt = f"{start}–{end}" if (start and end) else (start or end or "")
            visit_num = repeat_map.get((slug, td), 1)

            repeat_badge = f" <span class='tag repeat'> {_ordinal(visit_num)} showing</span>" if visit_num >= 2 else ""
            li.append(
                f"<li style='margin:0.25rem 0;'>"
                f"{(f'<span class=\"time\">{escape(time_txt)}</span> ' if time_txt else '')}"
                f"<a href='{escape(href)}' target='_blank' rel='noopener'>{escape(addr)}</a>"
                f"{repeat_badge}"
                f"</li>"
            )

        html = (
            "<style>"
            ".time{font-weight:700;margin-right:.35rem;}"
            ".tag.repeat{display:inline-block;margin-left:.5rem;padding:2px 6px;border-radius:8px;"
            "font-size:11px;font-weight:800;background:#fef3c7;color:#92400e;border:1px solid #f59e0b;}"
            "</style>"
            "<ul class='link-list'>"
            + "\n".join(li) +
            "</ul>"
        )
        st.markdown(html, unsafe_allow_html=True)
