"""Tracked flights, the spend ledger and cached searches.

The hub runs one server process, so module-level state under a lock is the
source of truth; tracked flights and the ledger are written through to
``app_state`` so they survive a restart. Times are epoch seconds.

A flight record holds what FlightAware last told us (scheduled, estimated and
actual gate-out/takeoff/landing/gate-in times, gates, delays) plus what we
already notified about, so each change is announced once.
"""
import datetime as dt
import threading
import time
import uuid
import zoneinfo

from bdh_sdk.settings import app_setting
from bdh_sdk.storage import app_state

APP = "flights"
MAX_FLIGHTS = 20
SEARCH_TTL = 15 * 60
POSITION_TTL = 5 * 60
DELAY_STEP = 15 * 60  # announce a delay once it reaches, or moves by, this much
ON_TIME = 5 * 60  # a delayed flight is "back on time" under this
KEEP_LANDED = 10 * 60  # after touchdown
KEEP_CANCELLED = 12 * 3600
KEEP_STALE = 24 * 3600  # never seen to take off this long after it was due out
FINAL_APPROACH = 15 * 60  # poll every minute from this long before touchdown until it's at the gate
GATE_WAIT = 45 * 60  # stop waiting for a gate arrival this long after touchdown
LANDING_NOW = 2 * 60

SCHEDULED, TAXI, AIR, LANDED, CANCELLED, DIVERTED = "scheduled", "taxi", "air", "landed", "cancelled", "diverted"
TIMES = tuple(f"{k}_{e}" for e in ("out", "off", "on", "in") for k in ("sched", "est", "act"))
# What each event needs switched on in the settings.
EVENT_SETTING = {"delay": "notify_delay", "ontime": "notify_delay", "gate": "notify_gate",
                 "departed": "notify_progress", "landed": "notify_progress",
                 "cancelled": "notify_cancel", "diverted": "notify_cancel"}

_lock = threading.RLock()
_flights = None
_ledger = None
_searches = {}  # key -> (expires, [candidate, ...]); memory only
_positions = {}  # flight id -> (expires, position)


def _state():
    return app_state(APP)


def _load():
    global _flights, _ledger
    if _flights is None:
        st = _state()
        _flights = [f for f in (st.get("flights") or []) if isinstance(f, dict) and f.get("id")]
        _ledger = st.get("ledger") or {}
        if not isinstance(_ledger, dict):
            _ledger = {}


def _save():
    st = _state()
    st.set("flights", _flights)
    st.set("ledger", _ledger)


def reset_cache():
    """Forget the in-memory copy (tests, or after the state store changes)."""
    global _flights, _ledger
    with _lock:
        _flights = _ledger = None
        _searches.clear()
        _positions.clear()


# --- time helpers -------------------------------------------------------------

def parse_time(value):
    """Epoch seconds for an AeroAPI ISO 8601 time, or None."""
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def hub_tz():
    try:
        from bdh_sdk.location import tzinfo
        return tzinfo() or dt.timezone.utc
    except Exception:
        return dt.timezone.utc


def zone(name):
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except Exception:
            pass
    return hub_tz()


def clock(ts, tz_name=None, now=None):
    """``6:15 PM``, or ``Tue Nov 18 · 6:15 PM`` when it isn't today (in that zone)."""
    if ts is None:
        return ""
    tz = zone(tz_name)
    local = dt.datetime.fromtimestamp(ts, tz)
    text = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    today = dt.datetime.fromtimestamp(time.time() if now is None else now, tz).date()
    if local.date() != today:
        text = f"{local:%a %b} {local.day} · {text}"
    return text


def span(seconds):
    """``1h 10m`` / ``25m``."""
    m = max(0, int(round(seconds / 60)))
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def day_window(date_text, now=None):
    """UTC (start, end) epochs of a ``YYYY-MM-DD`` day in the hub's time zone."""
    if not date_text:
        day = dt.datetime.fromtimestamp(time.time() if now is None else now, hub_tz()).date()
    else:
        try:
            day = dt.date.fromisoformat(str(date_text))
        except ValueError:
            raise ValueError("Pick a date.") from None
    start = dt.datetime.combine(day, dt.time(), hub_tz())
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


# --- budget ledger ------------------------------------------------------------

def month(now=None):
    return dt.datetime.fromtimestamp(time.time() if now is None else now, dt.timezone.utc).strftime("%Y-%m")


def budget_limit():
    try:
        return max(0, int(app_setting(APP, "monthly_budget", 5, cast=int)))
    except (TypeError, ValueError):
        return 5


def budget(now=None):
    with _lock:
        _load()
        m = _ledger.get(month(now)) or {}
        spent = round(float(m.get("spent", 0)), 4)
        limit = budget_limit()
        return {"month": month(now), "spent": spent, "calls": int(m.get("calls", 0)),
                "limit": limit, "left": round(max(0.0, limit - spent), 4)}


def can_spend(cost, now=None):
    b = budget(now)
    return b["spent"] + cost <= b["limit"] + 1e-9


def spend(cost, now=None):
    """Record a lookup's cost. False (and nothing recorded) if it would go over budget."""
    with _lock:
        _load()
        if not can_spend(cost, now):
            return False
        key = month(now)
        m = _ledger.setdefault(key, {"spent": 0.0, "calls": 0})
        m["spent"] = round(float(m.get("spent", 0)) + cost, 4)
        m["calls"] = int(m.get("calls", 0)) + 1
        # Keep a year of history.
        for old in sorted(_ledger)[:-12]:
            _ledger.pop(old, None)
        _save()
        return True


# --- searches -----------------------------------------------------------------

def cache_search(candidates, now=None):
    """Remember search results; the client tracks one by ``<search>:<index>``."""
    now = time.time() if now is None else now
    with _lock:
        for k in [k for k, (exp, _) in _searches.items() if exp <= now]:
            del _searches[k]
        key = uuid.uuid4().hex[:12]
        _searches[key] = (now + SEARCH_TTL, list(candidates))
        return key


def candidate(ref, now=None):
    now = time.time() if now is None else now
    key, _, idx = str(ref or "").partition(":")
    with _lock:
        found = _searches.get(key)
        if not found or found[0] <= now:
            raise LookupError("That search has expired; search again.")
        try:
            return dict(found[1][int(idx)])
        except (ValueError, IndexError):
            raise LookupError("No such result.") from None


# --- flights ------------------------------------------------------------------

def flights():
    with _lock:
        _load()
        return [dict(f) for f in _flights]


def get(flight_id):
    with _lock:
        _load()
        for f in _flights:
            if f["id"] == flight_id:
                return dict(f)
    raise LookupError(flight_id)


def _put(f):
    for n, cur in enumerate(_flights):
        if cur["id"] == f["id"]:
            _flights[n] = f
            _save()
            return f
    raise LookupError(f["id"])


def dep_time(f):
    return f.get("act_out") or f.get("est_out") or f.get("sched_out")


def arr_time(f):
    return f.get("act_in") or f.get("est_in") or f.get("sched_in")


def phase(f):
    if f.get("cancelled"):
        return CANCELLED
    if f.get("diverted"):
        return DIVERTED
    if f.get("act_on") or f.get("act_in"):
        return LANDED
    if f.get("act_off"):
        return AIR
    if f.get("act_out"):
        return TAXI
    return SCHEDULED


def dep_delay(f):
    if f.get("dep_delay") is not None:
        return f["dep_delay"]
    if f.get("sched_out") and (f.get("act_out") or f.get("est_out")):
        return (f.get("act_out") or f["est_out"]) - f["sched_out"]
    return 0


def arr_delay(f):
    if f.get("arr_delay") is not None:
        return f["arr_delay"]
    if f.get("sched_in") and (f.get("act_in") or f.get("est_in")):
        return (f.get("act_in") or f["est_in"]) - f["sched_in"]
    return 0


def track(cand, popups=False, now=None):
    """Start tracking a search result. Its data is already fresh, so no lookup is needed."""
    now = time.time() if now is None else now
    with _lock:
        _load()
        if len(_flights) >= MAX_FLIGHTS:
            raise ValueError(f"You can track up to {MAX_FLIGHTS} flights.")
        for f in _flights:
            same_id = cand.get("fa_flight_id") and f.get("fa_flight_id") == cand["fa_flight_id"]
            same_sched = f["ident"] == cand.get("ident") and f.get("sched_out") == cand.get("sched_out")
            if same_id or same_sched:
                raise ValueError(f"Already tracking {f['ident']}.")
        f = {"id": uuid.uuid4().hex[:10], "popups": bool(popups), "added": now,
             "last_poll": now if cand.get("live") else None, "notified": {}, "error": ""}
        f.update({k: v for k, v in cand.items() if k != "live"})
        f["notified"] = _baseline(f)
        f["next_poll"] = next_poll(f, now)
        _flights.append(f)
        _flights.sort(key=lambda x: dep_time(x) or 0)
        _save()
        return dict(f)


def _baseline(f):
    """What a newly tracked flight has 'already' been announced, so we only report changes."""
    return {"delay": dep_delay(f) if dep_delay(f) >= DELAY_STEP else 0, "gate": f.get("gate_out") or "",
            "departed": bool(f.get("act_off")), "landed": bool(f.get("act_on") or f.get("act_in")),
            "cancelled": bool(f.get("cancelled")), "diverted": bool(f.get("diverted"))}


def remove(flight_id):
    with _lock:
        _load()
        before = len(_flights)
        _flights[:] = [f for f in _flights if f["id"] != flight_id]
        if len(_flights) == before:
            raise LookupError(flight_id)
        _positions.pop(flight_id, None)
        _save()


def set_popups(flight_id, on):
    with _lock:
        f = get(flight_id)
        f["popups"] = bool(on)
        return _put(f)


def set_error(flight_id, error, retry_at):
    with _lock:
        f = get(flight_id)
        f["error"], f["next_poll"] = error, retry_at
        return _put(f)


def update(flight_id, data, now=None):
    """Apply fresh FlightAware data; returns (flight, [event, ...]) for what changed."""
    now = time.time() if now is None else now
    with _lock:
        f = get(flight_id)
        old_gate = f.get("gate_out") or ""
        f.update(data)
        f.pop("live", None)
        f["last_poll"], f["error"] = now, ""
        events = detect(f, old_gate)
        f["next_poll"] = next_poll(f, now)
        return _put(f), events


def detect(f, old_gate=""):
    """Events not yet announced for ``f``; marks them announced in ``f['notified']``."""
    n = f.setdefault("notified", {})
    events = []
    for flag in ("cancelled", "diverted"):
        if f.get(flag) and not n.get(flag):
            n[flag] = True
            events.append(flag)
    if f.get("act_off") and not n.get("departed"):
        n["departed"] = True
        events.append("departed")
    if (f.get("act_on") or f.get("act_in")) and not n.get("landed"):
        n["landed"] = True
        events.append("landed")
    gate, prev = f.get("gate_out") or "", n.get("gate") or old_gate
    if gate and prev and gate != prev:
        events.append("gate")
    if gate:
        n["gate"] = gate
        n["gate_from"] = prev
    if not f.get("act_out") and not f.get("cancelled"):
        delay, last = dep_delay(f), n.get("delay", 0)
        if delay >= DELAY_STEP and abs(delay - last) >= DELAY_STEP:
            n["delay"] = delay
            events.append("delay")
        elif last >= DELAY_STEP and delay < ON_TIME:
            n["delay"] = 0
            events.append("ontime")
    return events


def next_poll(f, now=None):
    """When to look the flight up next, or None once there's nothing left to learn.

    More than a day out it waits until a day before; then hourly until two hours
    before, every 10 minutes after that (5 around departure) and every 10 minutes
    in the air (5 in the last half hour, every minute in the last 15). After
    touchdown it keeps checking every minute until the plane reaches the gate.
    """
    now = time.time() if now is None else now
    p = phase(f)
    if p == LANDED:
        if f.get("act_in") or not f.get("act_on") or now - f["act_on"] >= GATE_WAIT:
            return None
        return now + 60
    if p in (CANCELLED, DIVERTED):
        return None
    if p in (SCHEDULED, TAXI) and not f.get("act_off"):
        dep = f.get("est_out") or f.get("sched_out")
        if dep is None:
            return now + 3600
        until = dep - now
        if until > 24 * 3600:
            return dep - 24 * 3600
        if until > 2 * 3600:
            return min(now + 3600, dep - 2 * 3600)
        if abs(until) <= 20 * 60 or p == TAXI:
            return now + 300
        return now + 600
    arr = f.get("est_on") or f.get("est_in") or f.get("sched_in")
    if arr is not None and arr - now <= FINAL_APPROACH:
        return now + 60
    if arr is not None and arr - now <= 30 * 60:
        return now + 300
    return now + 600


def expires_at(f):
    """When a finished flight drops off the list, or None while it's live."""
    p = phase(f)
    if p == LANDED:
        return (f.get("act_on") or f.get("act_in")) + KEEP_LANDED
    if p in (CANCELLED, DIVERTED):
        return f["sched_out"] + KEEP_CANCELLED if f.get("sched_out") else None
    if p == SCHEDULED and f.get("sched_out"):
        return f["sched_out"] + KEEP_STALE
    return None


def expired(f, now=None):
    now = time.time() if now is None else now
    at = expires_at(f)
    return at is not None and now > at


def due(now=None):
    now = time.time() if now is None else now
    return [f for f in flights() if f.get("next_poll") is not None and f["next_poll"] <= now]


def prune(now=None):
    """Drop finished flights; returns the ones removed."""
    gone = [f for f in flights() if expired(f, now)]
    for f in gone:
        try:
            remove(f["id"])
        except LookupError:
            pass
    return gone


def next_wake(now=None):
    times = []
    for f in flights():
        if f.get("next_poll") is not None:
            times.append(f["next_poll"])
        at = expires_at(f)
        if at is not None:
            times.append(at + 1)  # expired() is strict
    return min(times) if times else None


def cached_position(flight_id, now=None):
    """(True, position-or-None) while a recent lookup is cached, else (False, None)."""
    now = time.time() if now is None else now
    with _lock:
        hit = _positions.get(flight_id)
        return (True, hit[1]) if hit and hit[0] > now else (False, None)


def save_position(flight_id, pos, now=None):
    now = time.time() if now is None else now
    with _lock:
        _positions[flight_id] = (now + POSITION_TTL, pos)


# --- display ------------------------------------------------------------------

def label(f, now=None):
    """(pill, detail) for a flight, e.g. ("Delayed", "Departs 6:15 PM (+25m)")."""
    now = time.time() if now is None else now
    p = phase(f)
    o, d = f.get("origin") or {}, f.get("destination") or {}
    if p == CANCELLED:
        return "Cancelled", f"Was due out {clock(f.get('sched_out'), o.get('tz'), now)}"
    if p == DIVERTED:
        return "Diverted", f"Diverted from {d.get('code', '')}"
    tz = d.get("tz")
    gate = f.get("gate_in") or ""
    if p == LANDED:
        if f.get("act_in"):
            where = f"At gate {gate}" if gate else "At the gate"
            return "Landed", f"{where} {clock(f['act_in'], tz, now)}"
        taxi = f"Taxiing to {gate}" if gate else "Taxiing to the gate"
        return "Landed", f"Landed {clock(f.get('act_on'), tz, now)} · {taxi}"
    if p == AIR:
        # Headline the gate arrival, as FlightAware and the airlines do; touchdown
        # is several minutes earlier.
        arr = f.get("est_in") or f.get("sched_in") or f.get("est_on")
        touchdown = f.get("est_on")
        if touchdown and touchdown - now <= LANDING_NOW:
            at = f" · at gate ~{clock(arr, tz, now)}" if arr else ""
            return "In air", f"Landing now{at}"
        left = f" · {span(arr - now)} left" if arr and arr > now else ""
        return "In air", f"Arrives {clock(arr, tz, now)}{left}"
    dep = f.get("act_out") or f.get("est_out") or f.get("sched_out")
    delay = dep_delay(f)
    late = f" (+{span(delay)})" if delay >= ON_TIME else ""
    if p == TAXI:
        return "Departing", f"Left the gate {clock(dep, o.get('tz'), now)}{late}"
    if delay >= DELAY_STEP:
        return "Delayed", f"Departs {clock(dep, o.get('tz'), now)}{late}"
    return "Scheduled", f"Departs {clock(dep, o.get('tz'), now)}{late}"


def snapshot(f, now=None):
    """JSON for the page: the record plus display labels."""
    now = time.time() if now is None else now
    pill, detail = label(f, now)
    o, d = f.get("origin") or {}, f.get("destination") or {}
    out = {k: v for k, v in f.items() if k != "notified"}
    out.update({
        "phase": phase(f), "pill": pill, "detail": detail,
        "dep_delay": dep_delay(f), "arr_delay": arr_delay(f),
        "next_label": clock(f.get("next_poll"), None, now) if f.get("next_poll") else "",
        "labels": {k: clock(f.get(k), (o if k.endswith(("out", "off")) else d).get("tz"), now) for k in TIMES},
    })
    return out
