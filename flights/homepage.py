from bdh_sdk import INFO, ON, WARN, Action, Status, StatusRow

from . import store, worker

ICONS = {"remove": ("fa-solid fa-times", "Stop tracking")}


def _title(f):
    o, d = f.get("origin") or {}, f.get("destination") or {}
    return f"{f.get('ident', '')} {o.get('code', '')} → {d.get('code', '')}"


def status():
    flights = store.flights()
    if not flights:
        return Status("No flights")
    rows = []
    for f in flights:
        pill, detail = store.label(f)
        rows.append(StatusRow(id=f["id"], title=_title(f), detail=f"{pill} · {detail}",
                              dimmed=store.phase(f) in (store.LANDED, store.CANCELLED),
                              actions=(Action("remove", *ICONS["remove"]),)))
    trouble = [f for f in flights if store.label(f)[0] in ("Delayed", "Cancelled", "Diverted")]
    flying = [f for f in flights if store.phase(f) == store.AIR]
    if trouble:
        f = trouble[0]
        text, level = f"{f['ident']} {store.label(f)[0].lower()}", WARN
    elif flying:
        f = flying[0]
        text, level = f"{f['ident']} {store.label(f)[1]}", ON
    else:
        f = min(flights, key=lambda x: store.dep_time(x) or float("inf"))
        text, level = f"{f['ident']} {store.label(f)[1]}", INFO
    b = store.budget()
    if b["limit"] and b["left"] <= 0:
        text, level = "FlightAware budget used up", WARN
    return Status(text, level=level, rows=tuple(rows), refresh=60)


def run_action(row_id, action_id, mode=None):
    if action_id not in ICONS:
        raise LookupError(f"unknown action {action_id!r}")
    f = store.get(row_id)
    remove(row_id)
    return f"Stopped tracking {f['ident']}"


def remove(flight_id):
    store.remove(flight_id)
    from bdh_sdk import alerts
    alerts.dismiss(store.APP, f"flight:{flight_id}")
    worker.poke()
