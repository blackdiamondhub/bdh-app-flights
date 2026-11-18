"""Background poller: refreshes tracked flights from FlightAware and announces changes.

Each flight carries its own ``next_poll`` (see ``store.next_poll``), so a
flight days away costs nothing until the day before. Changes (delay, gate,
departed, landed, cancelled, diverted) become a hub popup when the flight has
popups on, and a phone notification through Home Assistant for the event
kinds switched on in the settings.
"""
import logging
import os
import sys
import threading
import time

from bdh_sdk import alerts
from bdh_sdk.settings import app_setting

from . import aeroapi, store

log = logging.getLogger(__name__)

RETRY_ERROR = 10 * 60
RETRY_BUDGET = 3600
POPUP_TTL = 120

_wake = threading.Event()
_started = False
_start_lock = threading.Lock()


def _is_runtime(path):
    """``python -m bdh_sdk.runtime`` (the app's container) runs ``bdh_sdk/runtime/__main__.py``."""
    parts = path.replace("\\", "/").split("/")
    return parts[-3:] == ["bdh_sdk", "runtime", "__main__.py"]


def should_start(argv=None):
    """Start only in the server process (the app's container, Daphne, or runserver's reloaded child)."""
    if os.environ.get("BDH_FLIGHTS_WORKER", "").lower() in ("0", "false", "off", "no"):
        return False
    argv = sys.argv if argv is None else argv
    if argv and _is_runtime(argv[0]):
        return True
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py"):
        if argv[1] == "runserver":
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
        return False
    return False


def chime():
    value = app_setting(store.APP, "chime", "soft-bell")
    return value if value in alerts.CHIMES or value == alerts.SILENT else "soft-bell"


def notify_targets():
    from bdh_sdk.plugins import as_list
    raw = app_setting(store.APP, "notify_targets", "") or ""
    items = as_list(raw) if not isinstance(raw, str) else raw.replace(";", ",").split(",")
    out = []
    for t in items:
        t = str(t).strip()
        if t.startswith("notify."):
            t = t[len("notify."):]
        if t and t not in out:
            out.append(t)
    return out


def enabled(event):
    key = store.EVENT_SETTING.get(event)
    if not key:
        return False
    value = app_setting(store.APP, key, True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "off", "no", "")
    return bool(value)


def _spoken(ident):
    """"AS342" -> "A S 342" so speech reads the airline letter by letter."""
    m = aeroapi.IDENT.match(ident or "")
    return f"{' '.join(m.group(1))} {m.group(2)}" if m else ident


def describe(event, f, now=None):
    """(title, body) for an event."""
    now = time.time() if now is None else now
    o, d = f.get("origin") or {}, f.get("destination") or {}
    route = f"{o.get('code', '')} → {d.get('code', '')}"
    title = f"{f.get('ident', '')} {route}"
    if event == "delay":
        dep = f.get("est_out") or f.get("sched_out")
        body = f"Delayed {store.span(store.dep_delay(f))} · now departs {store.clock(dep, o.get('tz'), now)}"
    elif event == "ontime":
        body = f"Back on time · departs {store.clock(f.get('est_out') or f.get('sched_out'), o.get('tz'), now)}"
    elif event == "gate":
        was = (f.get("notified") or {}).get("gate_from")
        body = f"Gate changed to {f.get('gate_out')}" + (f" (was {was})" if was else "")
    elif event == "departed":
        arr = f.get("est_in") or f.get("sched_in") or f.get("est_on")
        body = f"Took off · arrives {store.clock(arr, d.get('tz'), now)}"
    elif event == "landed":
        body = f"Landed at {d.get('code', '')}" + (f" · bags at {f['baggage']}" if f.get("baggage") else "")
    elif event == "cancelled":
        body = "Cancelled"
    elif event == "diverted":
        body = "Diverted"
    else:
        body = event
    return title, body


def announce(event, f, now=None):
    title, body = describe(event, f, now)
    if f.get("popups"):
        speak = f"{_spoken(f.get('ident', ''))}. {body}".replace("→", "to").replace("·", ",")
        alerts.push(alerts.Alert(
            app=store.APP, key=f"flight:{f['id']}", title=title, body=body,
            embed_url=f"/flights/popup/{f['id']}/", ttl=POPUP_TTL, sound=chime(),
            speak=speak[:alerts.MAX_SPEAK]))
    if enabled(event):
        targets = notify_targets()
        if targets:
            threading.Thread(target=_notify, args=(title, body, targets), daemon=True,
                             name="flights-notify").start()


def _notify(title, body, targets):
    from bdh_sdk import ha
    for target in targets:
        try:
            ok, err = ha.call_service("notify", target, None, {"title": title, "message": body})
            if not ok:
                log.warning("flights: notify.%s failed: %s", target, err)
        except Exception:
            log.exception("flights: notify.%s failed", target)


def refresh(f, now=None):
    """Look one flight up and announce what changed. Returns the updated flight."""
    now = time.time() if now is None else now
    try:
        data = aeroapi.lookup(f, now)
    except aeroapi.BudgetExhausted as exc:
        return store.set_error(f["id"], str(exc), now + RETRY_BUDGET)
    except aeroapi.RateLimited as exc:
        return store.set_error(f["id"], str(exc), now + 60)
    except aeroapi.ApiError as exc:
        return store.set_error(f["id"], str(exc), now + RETRY_ERROR)
    if data is None:
        # Not in FlightAware's live window yet (schedule) or gone; try again later.
        sched = f.get("sched_out") or now
        retry = sched - 24 * 3600 if sched - now > 26 * 3600 else now + 3600
        return store.set_error(f["id"], "", retry)
    updated, events = store.update(f["id"], data, now)
    for event in events:
        try:
            announce(event, updated, now)
        except Exception:
            log.exception("flights: announcing %s for %s failed", event, f["id"])
    return updated


def tick(now=None):
    now = time.time() if now is None else now
    for f in store.prune(now):
        alerts.dismiss(store.APP, f"flight:{f['id']}")
    for f in store.due(now):
        try:
            refresh(f, now)
        except LookupError:
            pass  # removed meanwhile


def poke():
    """Wake the poller after a change so a new schedule is picked up."""
    _wake.set()


def _run():
    while True:
        try:
            tick()
            nxt = store.next_wake()
            wait = 300 if nxt is None else max(1.0, min(300.0, nxt - time.time()))
        except Exception:
            log.exception("flights: tick failed")
            wait = 60
        _wake.wait(wait)
        _wake.clear()


def start():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run, daemon=True, name="flights-worker").start()
