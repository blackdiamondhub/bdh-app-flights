import json
import logging
import time

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from . import aeroapi, homepage, store, worker

log = logging.getLogger(__name__)
CONTROLS = ("remove", "refresh", "popups_on", "popups_off")
REFRESH_MIN = 120  # a manual refresh more often than this just returns what we have


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


def _error(msg, status=400):
    return JsonResponse({"error": str(msg)}, status=status)


def _state():
    now = time.time()
    return {"now": now, "max": store.MAX_FLIGHTS, "has_key": bool(aeroapi.api_key()),
            "budget": store.budget(now), "costs": aeroapi.COST,
            "flights": [store.snapshot(f, now) for f in store.flights()]}


def index(request):
    return render(request, "flights/index.html", {"state": _state()})


@xframe_options_sameorigin
def popup(request, flight_id):
    try:
        f = store.get(flight_id)
    except LookupError:
        raise Http404("no such flight") from None
    return render(request, "flights/popup.html", {"flight": store.snapshot(f, time.time())})


@require_GET
def state(request):
    return JsonResponse(_state())


@require_POST
def search(request):
    data = _body(request)
    now = time.time()
    try:
        found = aeroapi.search(data.get("mode") or "ident", ident=data.get("ident"), origin=data.get("origin"),
                               destination=data.get("destination"), date=data.get("date"), now=now)
    except ValueError as exc:
        return _error(exc)
    except aeroapi.ApiError as exc:
        return _error(exc, status=502)
    key = store.cache_search(found, now)
    results = []
    for n, cand in enumerate(found):
        snap = store.snapshot(cand, now)
        snap["ref"] = f"{key}:{n}"
        results.append(snap)
    return JsonResponse({"results": results, "budget": store.budget(now)})


@require_POST
def track(request):
    data = _body(request)
    try:
        cand = store.candidate(data.get("ref"))
        store.track(cand, popups=bool(data.get("popups")))
    except (LookupError, ValueError) as exc:
        return _error(exc)
    worker.poke()
    return JsonResponse(_state())


@require_POST
def control(request, flight_id, act):
    if act not in CONTROLS:
        raise Http404("unknown action")
    try:
        if act == "remove":
            homepage.remove(flight_id)
        elif act in ("popups_on", "popups_off"):
            store.set_popups(flight_id, act == "popups_on")
        else:
            f = store.get(flight_id)
            if time.time() - (f.get("last_poll") or 0) >= REFRESH_MIN:
                worker.refresh(f)
    except LookupError:
        raise Http404("no such flight") from None
    return JsonResponse(_state())


@require_GET
def position(request, flight_id):
    try:
        f = store.get(flight_id)
    except LookupError:
        raise Http404("no such flight") from None
    if store.phase(f) != store.AIR or not f.get("fa_flight_id"):
        return JsonResponse({"position": None})
    cached, pos = store.cached_position(flight_id)
    if not cached:
        try:
            pos = aeroapi.position(f["fa_flight_id"])
        except aeroapi.ApiError as exc:
            return _error(exc, status=502)
        store.save_position(flight_id, pos)
    return JsonResponse({"position": pos, "budget": store.budget()})
