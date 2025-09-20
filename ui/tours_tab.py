# --- Import (at top) with persistent state ---
STATE_KEY = "__tour_parse__"

def _set_parsed(payload: Dict[str, Any]):
    st.session_state[STATE_KEY] = payload

def _get_parsed() -> Dict[str, Any]:
    return st.session_state.get(STATE_KEY, {})

st.subheader("Import Tour")
col_u, col_p = st.columns([1.2, 1])
with col_u:
    print_url = st.text_input("ShowingTime Print URL", placeholder="https://scheduling.showingtime.com/.../Tour/Print/30235965")
with col_p:
    pdf_file = st.file_uploader("or upload Tour PDF", type=["pdf"])

c_parse, c_clear = st.columns([1, 1])
parse_clicked = c_parse.button("Parse tour", use_container_width=True)
clear_clicked = c_clear.button("Clear parsed tour", use_container_width=True)

if clear_clicked:
    st.session_state.pop(STATE_KEY, None)
    st.rerun()

parsed = _get_parsed()

if parse_clicked:
    # Parse fresh, then store in state
    if print_url:
        d, b, s, err = parse_from_print_url(print_url.strip())
        source_str = print_url.strip()
    elif pdf_file is not None:
        d, b, s, err = parse_from_pdf(pdf_file)
        source_str = f"pdf:{pdf_file.name}"
    else:
        d, b, s, err = None, None, [], "Provide a Print URL or a Tour PDF."
        source_str = ""

    if err:
        st.error(err)
    else:
        # Initialize persistent checkbox flags once
        flags = [True] * len(s)
        _set_parsed({
            "date": d.isoformat() if d else None,
            "buyer": b,
            "stops": s,              # list of dicts: address, start, end, address_slug, deeplink
            "source": source_str,
            "flags": flags,          # persisted include/exclude flags
        })
        parsed = _get_parsed()
        st.success(f"Parsed {len(parsed['stops'])} stop(s)"
                   + (f" • {parsed['buyer']}" if parsed.get("buyer") else "")
                   + (f" • {parsed['date']}" if parsed.get("date") else ""))

# If we have a parsed tour in state, render the preview with persistent checkboxes
if parsed.get("stops"):
    st.markdown("#### Preview")
    # Show a row per stop with a persistent checkbox
    new_flags = []
    for i, s in enumerate(parsed["stops"]):
        # Persist the checkbox for each row. Use a stable key per index.
        default_val = bool(parsed["flags"][i]) if i < len(parsed["flags"]) else True
        cols = st.columns([0.08, 0.92])
        with cols[0]:
            chk = st.checkbox("", value=default_val, key=f"__tour_inc_{i}", help="Uncheck to exclude this stop before saving.")
            new_flags.append(chk)
        with cols[1]:
            start_end = f'{s["start"]} – {s["end"]}'
            st.markdown(
                f"""
                <div class="tour-card">
                  <a href="{escape(s["deeplink"])}" target="_blank" rel="noopener">{escape(s["address"])}</a>
                  <span class="tour-pill">{escape(start_end)}</span>
                </div>
                """,
                unsafe_allow_html=True
            )

    # Update flags in state after rendering
    parsed["flags"] = new_flags
    _set_parsed(parsed)

    # Choose client (sentinel LAST, per your preference)
    clients = fetch_clients(include_inactive=True)
    names = [c["name"] for c in clients]
    name_to_norm = {c["name"]: c.get("name_norm","") for c in clients}
    options = ["— Choose client —"] + names + ["➤ No client (show ALL, no logging)"]
    sel = st.selectbox("Add all included stops to client", options, index=0)

    add_clicked = st.button("Add all included stops", use_container_width=True)

    if add_clicked:
        if sel == "— Choose client —":
            st.warning("Pick a client to save these stops.")
            st.stop()
        if sel == "➤ No client (show ALL, no logging)":
            st.info("Preview only: no client selected, nothing will be saved.")
            st.stop()

        client_display = sel
        client_norm = name_to_norm.get(sel, _norm_tag(sel))
        tour_date = None
        if parsed.get("date"):
            try:
                tour_date = datetime.fromisoformat(parsed["date"]).date()
            except Exception:
                tour_date = datetime.utcnow().date()
        else:
            tour_date = datetime.utcnow().date()

        final_stops = [s for s, inc in zip(parsed["stops"], parsed["flags"]) if inc]
        if not final_stops:
            st.warning("No stops selected.")
            st.stop()

        ok_t, tour_id_or_err = create_tour(
            client_norm=client_norm,
            client_display=client_display,
            tour_date=tour_date,
            source_url=parsed.get("source","")
        )
        if not ok_t:
            st.error(f"Create tour failed: {tour_id_or_err}")
            st.stop()
        tour_id = tour_id_or_err

        ok_s, msg_s = insert_tour_stops(tour_id=int(tour_id), stops=final_stops)
        if not ok_s:
            st.error(f"Insert stops failed: {msg_s}")
            st.stop()

        # Also log into 'sent' so Clients tab can tag as TOURED
        ok_l, msg_l = log_sent_for_stops(client_norm=client_norm, stops=final_stops, tour_date=tour_date)
        if not ok_l:
            st.warning(f"Logged to 'sent' skipped/failed: {msg_l}")

        st.success(f"Saved {len(final_stops)} stop(s) to {client_display} for {tour_date}.")
        st.toast("Tour created.", icon="✅")

