"""FlightAware AeroAPI v4 client.

Every request is billed per result set (up to 15 records), so each call asks
for one page only. Before a call is made, this month's spend is checked
against the admin's monthly limit: FlightAware's own usage total (the free
``/account/usage``, cached for a few minutes) plus what we've spent since it
was fetched. If FlightAware's usage can't be read, the local ledger stands in.
A local limiter keeps us under the Personal tier's 10 result sets a minute.
"""
import collections
import datetime as dt
import logging
import re
import threading
import time

import requests

from . import airports, store

log = logging.getLogger(__name__)
BASE = "https://aeroapi.flightaware.com/aeroapi"
TIMEOUT = 15
# Dollars per result set, as FlightAware bills them (route searches are dearest).
COST = {"flights": 0.005, "schedules": 0.02, "route": 0.05, "position": 0.01}
USAGE_TTL = 5 * 60
USAGE_TIMEOUT = 8
WARN_AT = 0.95
OK, WARN, OUT, OFF = "ok", "warn", "out", "off"
RATE = 9  # calls per RATE_S
RATE_S = 60
LIVE_AHEAD = 2 * 86400  # /flights and /airports only see two days ahead...
LIVE_BACK = 10 * 86400  # ...and ten days back
MAX_RESULTS = 15

IDENT = re.compile(r"^([A-Z]{3}|[A-Z][A-Z0-9]|[0-9][A-Z])\s*-?\s*0*(\d{1,4})[A-Z]?$")
AIRPORT = re.compile(r"^[A-Z0-9]{3,4}$")


class ApiError(Exception):
    pass


class NoKey(ApiError):
    def __init__(self):
        super().__init__("Add your FlightAware AeroAPI key in Settings.")


class BudgetExhausted(ApiError):
    def __init__(self):
        super().__init__("This month's FlightAware limit is reached. An admin can raise it in Settings.")


class RateLimited(ApiError):
    def __init__(self):
        super().__init__("Too many FlightAware lookups; try again in a minute.")


_calls = collections.deque()
_rate_lock = threading.Lock()


def api_key():
    from bdh_sdk import secrets
    try:
        return (secrets.get("aeroapi_key", "", app=store.APP) or "").strip()
    except Exception:
        return ""


def _reserve(now):
    with _rate_lock:
        while _calls and _calls[0] <= now - RATE_S:
            _calls.popleft()
        if len(_calls) >= RATE:
            raise RateLimited()
        _calls.append(now)


def reset_rate():
    with _rate_lock:
        _calls.clear()


# --- monthly usage --------------------------------------------------------------

_usage = {}
_usage_lock = threading.Lock()
# (label, test on the operation, e.g. "GET /flights/{ident}"), first match wins.
KINDS = (
    ("Route search", lambda op: "/flights/to/" in op),
    ("Date search", lambda op: "/schedules" in op),
    ("Live position", lambda op: op.rstrip("/").endswith("/position")),
    ("Flight status", lambda op: op.split(" ", 1)[-1].startswith("/flights/")),
    ("Airport lookup", lambda op: op.split(" ", 1)[-1].startswith("/airports/")),
)


def _kind(op):
    for label, test in KINDS:
        if test(op or ""):
            return label
    return "Other"


def month_start(now):
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def _fetch_usage(now):
    """This month's usage from FlightAware (a free call), or None if it can't be read."""
    key = api_key()
    if not key:
        return None
    try:
        r = requests.get(BASE + "/account/usage", params={"start": iso(month_start(now))}, timeout=USAGE_TIMEOUT,
                         headers={"x-apikey": key, "Accept": "application/json"})
        if r.status_code != 200:
            log.warning("flights: FlightAware usage returned %s", r.status_code)
            return None
        data = r.json()
        total = float(data["total_cost"])
        parts = {}
        for item in data.get("resource_details") or []:
            label = _kind(item.get("operation"))
            p = parts.setdefault(label, {"label": label, "calls": 0, "cost": 0.0})
            p["calls"] += int(item.get("total_resource_calls") or 0)
            p["cost"] = round(p["cost"] + float(item.get("resource_cost") or 0), 4)
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("flights: couldn't read FlightAware usage (%s)", exc.__class__.__name__)
        return None
    order = [k for k, _ in KINDS] + ["Other"]
    return {"total": round(total, 4), "calls": int(data.get("total_calls") or 0),
            "parts": sorted(parts.values(), key=lambda p: order.index(p["label"]))}


def usage(now=None, force=False):
    """This month's usage: FlightAware's figures, refreshed every few minutes.

    ``ok`` is False when FlightAware has never answered this month; ``stale``
    when the last refresh failed and older figures are shown. ``ledger_at`` is
    our own ledger's spend when the figures were fetched.
    """
    now = time.time() if now is None else now
    m = store.month(now)
    with _usage_lock:
        if force or _usage.get("month") != m or now - _usage.get("at", 0) >= USAGE_TTL:
            got = _fetch_usage(now)
            ledger = store.budget(now)["spent"]
            if got is not None:
                _usage.clear()
                _usage.update(got, month=m, at=now, fetched_at=now, ok=True, stale=False, ledger_at=ledger)
            elif _usage.get("month") == m and _usage.get("ok"):
                _usage.update(at=now, stale=True)  # keep the last figures; retry after USAGE_TTL
            else:
                _usage.clear()
                _usage.update(month=m, at=now, fetched_at=None, ok=False, stale=False, ledger_at=ledger)
        return dict(_usage)


def reset_usage():
    with _usage_lock:
        _usage.clear()


def spent(now=None):
    """(dollars spent this month, from FlightAware?) including our calls since the last fetch."""
    now = time.time() if now is None else now
    u = usage(now)
    local = store.budget(now)["spent"]
    if u["ok"]:
        return round(u["total"] + max(0.0, local - u["ledger_at"]), 4), True
    return local, False


def level(now=None):
    """OK, WARN (95% of the limit), OUT (limit reached) or OFF (a $0 limit)."""
    limit = store.budget_limit()
    if limit <= 0:
        return OFF
    if not api_key():
        return OK
    s, _ = spent(now)
    if s + min(COST.values()) > limit + 1e-9:
        return OUT
    return WARN if s >= WARN_AT * limit else OK


def _get(path, params, kind, now=None):
    key = api_key()
    if not key:
        raise NoKey()
    now = time.time() if now is None else now
    limit = store.budget_limit()
    if limit <= 0 or spent(now)[0] + COST[kind] > limit + 1e-9:
        raise BudgetExhausted()
    _reserve(now)
    store.spend(COST[kind], now)
    try:
        r = requests.get(BASE + path, params={**params, "max_pages": 1}, timeout=TIMEOUT,
                         headers={"x-apikey": key, "Accept": "application/json"})
    except requests.RequestException as exc:
        raise ApiError(f"Couldn't reach FlightAware ({exc.__class__.__name__}).") from None
    if r.status_code == 404:
        return {}
    if r.status_code in (401, 403):
        raise ApiError("FlightAware rejected the API key.")
    if r.status_code == 429:
        raise RateLimited()
    if r.status_code >= 400:
        try:
            body = r.json()
            detail = body.get("detail") or body.get("title") or ""
        except ValueError:
            detail = ""
        raise ApiError(f"FlightAware error {r.status_code}{': ' + detail if detail else ''}")
    try:
        return r.json()
    except ValueError:
        raise ApiError("FlightAware sent a bad response.") from None


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ident(text):
    """``("AS", "342")`` from "AS342", "as 342", "ASA342" or "AS0342"."""
    m = IDENT.match(" ".join(str(text or "").upper().split()))
    if not m:
        raise ValueError("Enter a flight number like AS342.")
    return m.group(1), m.group(2)


def parse_airport(text, what):
    """An airport code from a code, city or airport name (see ``airports``)."""
    try:
        return airports.resolve(text, what)
    except ValueError:
        code = str(text or "").strip().upper()
        if AIRPORT.match(code):  # a code our list doesn't know: let FlightAware decide
            return code
        raise


# --- normalising --------------------------------------------------------------

def _airport(a):
    if isinstance(a, dict):
        return {"code": a.get("code_iata") or a.get("code_lid") or a.get("code") or "",
                "icao": a.get("code_icao") or a.get("code") or "", "name": a.get("name") or "",
                "city": a.get("city") or "", "tz": a.get("timezone")}
    return {"code": a or "", "icao": "", "name": "", "city": "", "tz": None}


def from_flight(f):
    """A tracked-flight dict from an AeroAPI flight object."""
    d = {
        "ident": f.get("ident_iata") or f.get("ident") or "",
        "ident_icao": f.get("ident_icao") or f.get("ident") or "",
        "fa_flight_id": f.get("fa_flight_id"),
        "operator": f.get("operator_iata") or f.get("operator") or "",
        "flight_number": f.get("flight_number") or "",
        "registration": f.get("registration") or "",
        "aircraft_type": f.get("aircraft_type") or "",
        "origin": _airport(f.get("origin")),
        "destination": _airport(f.get("destination")),
        "cancelled": bool(f.get("cancelled")),
        "diverted": bool(f.get("diverted")),
        "status": f.get("status") or "",
        "progress": f.get("progress_percent"),
        "dep_delay": f.get("departure_delay"),
        "arr_delay": f.get("arrival_delay"),
        "gate_out": f.get("gate_origin") or "",
        "gate_in": f.get("gate_destination") or "",
        "terminal_out": f.get("terminal_origin") or "",
        "terminal_in": f.get("terminal_destination") or "",
        "baggage": f.get("baggage_claim") or "",
        "live": True,
    }
    for event in ("out", "off", "on", "in"):
        for short, long in (("sched", "scheduled"), ("est", "estimated"), ("act", "actual")):
            d[f"{short}_{event}"] = store.parse_time(f.get(f"{long}_{event}"))
    return d


def from_schedule(s):
    """A tracked-flight dict from a /schedules entry (times only; no live status)."""
    d = {
        "ident": s.get("ident_iata") or s.get("ident") or "",
        "ident_icao": s.get("ident_icao") or s.get("ident") or "",
        "fa_flight_id": s.get("fa_flight_id"),
        "aircraft_type": s.get("aircraft_type") or "",
        "origin": _airport(s.get("origin_iata") or s.get("origin")),
        "destination": _airport(s.get("destination_iata") or s.get("destination")),
        "cancelled": False, "diverted": False, "status": "Scheduled",
        "live": False,
    }
    for k in store.TIMES:
        d[k] = None
    d["sched_out"] = store.parse_time(s.get("scheduled_out"))
    d["sched_in"] = store.parse_time(s.get("scheduled_in"))
    return d


def _sorted(found):
    found = [f for f in found if f.get("ident")]
    return sorted(found, key=lambda f: f.get("sched_out") or 0)[:MAX_RESULTS]


# --- calls --------------------------------------------------------------------

def search(mode, ident="", origin="", destination="", date="", now=None):
    """Flights on ``date`` (hub-local day) by flight number or by route.

    Within FlightAware's live window this returns live flights; further out it
    falls back to airline schedules, which are resolved to a live flight once
    the day comes into range.
    """
    now = time.time() if now is None else now
    start, end = store.day_window(date, now)
    if start < now - LIVE_BACK:
        raise ValueError("FlightAware only goes back 10 days.")
    live = end <= now + LIVE_AHEAD
    if not live and start > now + 365 * 86400:
        raise ValueError("Schedules only go a year ahead.")
    window = {"start": iso(start), "end": iso(end)}
    if mode == "route":
        o, d = parse_airport(origin, "departure"), parse_airport(destination, "arrival")
        if o == d:
            raise ValueError("Pick two different airports.")
        if live:
            data = _get(f"/airports/{o}/flights/to/{d}", {**window, "connection": "nonstop"}, "route", now)
            return _sorted(from_flight(item["segments"][0]) for item in data.get("flights") or []
                           if len(item.get("segments") or []) == 1)
        data = _get(f"/schedules/{window['start']}/{window['end']}",
                    {"origin": o, "destination": d, "include_codeshares": "false"}, "schedules", now)
        return _sorted(from_schedule(s) for s in data.get("scheduled") or [])
    airline, number = parse_ident(ident)
    if live:
        data = _get(f"/flights/{airline}{number}", {**window, "ident_type": "designator"}, "flights", now)
        return _sorted(from_flight(f) for f in data.get("flights") or [] if not f.get("blocked"))
    data = _get(f"/schedules/{window['start']}/{window['end']}",
                {"airline": airline, "flight_number": number, "include_codeshares": "false"}, "schedules", now)
    return _sorted(from_schedule(s) for s in data.get("scheduled") or [])


def lookup(f, now=None):
    """Fresh data for a tracked flight, or None if FlightAware doesn't have it (yet)."""
    if f.get("fa_flight_id"):
        data = _get(f"/flights/{f['fa_flight_id']}", {"ident_type": "fa_flight_id"}, "flights", now)
        found = data.get("flights") or []
        return from_flight(found[0]) if found else None
    # Tracked from a schedule: find the live flight closest to the scheduled time.
    sched = f.get("sched_out")
    if not sched:
        return None
    data = _get(f"/flights/{f.get('ident_icao') or f['ident']}",
                {"ident_type": "designator", "start": iso(sched - 6 * 3600), "end": iso(sched + 6 * 3600)},
                "flights", now)
    want = (f.get("origin") or {}).get("code")
    found = [from_flight(x) for x in data.get("flights") or [] if not x.get("blocked")]
    found = [x for x in found if not want or want in (x["origin"]["code"], x["origin"]["icao"])] or found
    found = [x for x in found if x.get("sched_out")]
    if not found:
        return None
    return min(found, key=lambda x: abs(x["sched_out"] - sched))


def position(fa_flight_id, now=None):
    data = _get(f"/flights/{fa_flight_id}/position", {}, "position", now)
    p = data.get("last_position") or {}
    if p.get("latitude") is None:
        return None
    return {"lat": p["latitude"], "lon": p["longitude"], "alt_ft": (p.get("altitude") or 0) * 100,
            "speed_kt": p.get("groundspeed"), "heading": p.get("heading"),
            "at": store.parse_time(p.get("timestamp"))}
