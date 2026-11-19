import datetime as dt
import time
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from bdh_sdk import INFO, ON, WARN
from bdh_sdk.storage import set_state_backend
from bdh_sdk.testing import assert_plugin_importable
from flights import aeroapi, airports, homepage, store, worker

T0 = 1_800_000_000.0  # 2027-01-15 08:00 UTC
H = 3600


def airport(iata, icao, city):
    return {"code": icao, "code_iata": iata, "code_icao": icao, "city": city, "name": city, "timezone": "UTC"}


def fa(base=T0, **kw):
    """An AeroAPI flight object departing three hours after ``base``."""
    f = {"ident": "ASA342", "ident_iata": "AS342", "ident_icao": "ASA342", "fa_flight_id": "ASA342-1-x",
         "registration": "N123AS", "aircraft_type": "B39M",
         "origin": airport("SEA", "KSEA", "Seattle"), "destination": airport("SFO", "KSFO", "San Francisco"),
         "scheduled_out": aeroapi.iso(base + 3 * H), "estimated_out": aeroapi.iso(base + 3 * H),
         "scheduled_in": aeroapi.iso(base + 5 * H), "estimated_in": aeroapi.iso(base + 5 * H)}
    f.update(kw)
    return f


def response(status=200, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = {} if body is None else body
    return r


class Fresh:
    def setUp(self):
        set_state_backend(None)
        store.reset_cache()
        aeroapi.reset_rate()
        aeroapi.reset_usage()
        self.addCleanup(store.reset_cache)
        self.addCleanup(aeroapi.reset_rate)
        self.addCleanup(aeroapi.reset_usage)
        patch.object(store, "hub_tz", return_value=dt.timezone.utc).start()
        # FlightAware's usage figures; None = unreachable, so spend falls back to our ledger.
        self.fetch_patch = patch.object(aeroapi, "_fetch_usage", return_value=None)
        self.fetch = self.fetch_patch.start()
        self.addCleanup(patch.stopall)

    def tracked(self, now=T0, popups=False, **kw):
        cand = aeroapi.from_flight(fa(now, **kw))
        cand["dep_delay"] = cand["arr_delay"] = None
        return store.track(cand, popups=popups, now=now)


class AeroApiTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.key = patch("flights.aeroapi.api_key", return_value="k").start()
        self.get = patch("flights.aeroapi.requests.get", return_value=response(body={"flights": [fa()]})).start()

    def test_parse_ident(self):
        for text in ("AS342", "as 342", "AS-0342", "ASA342"):
            self.assertEqual(aeroapi.parse_ident(text)[1], "342", text)
        self.assertEqual(aeroapi.parse_ident("asa 342"), ("ASA", "342"))
        self.assertEqual(aeroapi.parse_ident("B6 1"), ("B6", "1"))
        self.assertEqual(aeroapi.parse_ident("9w12"), ("9W", "12"))
        for bad in ("", "342", "AS", "AS12345", "Seattle"):
            with self.assertRaises(ValueError):
                aeroapi.parse_ident(bad)

    def test_search_live_flight_number(self):
        found = aeroapi.search("ident", ident="as342", date="2027-01-15", now=T0)
        url, = self.get.call_args.args
        params = self.get.call_args.kwargs["params"]
        self.assertTrue(url.endswith("/flights/AS342"))
        self.assertEqual((params["start"], params["end"], params["max_pages"]),
                         ("2027-01-15T00:00:00Z", "2027-01-16T00:00:00Z", 1))
        self.assertEqual(self.get.call_args.kwargs["headers"]["x-apikey"], "k")
        (f,) = found
        self.assertEqual((f["ident"], f["origin"]["code"], f["destination"]["code"], f["live"]),
                         ("AS342", "SEA", "SFO", True))
        self.assertEqual(f["sched_out"], T0 + 3 * H)
        self.assertEqual(store.budget(T0)["spent"], aeroapi.COST["flights"])

    def test_search_far_ahead_uses_schedules(self):
        self.get.return_value = response(body={"scheduled": [
            {"ident_iata": "AS342", "ident_icao": "ASA342", "origin_iata": "SEA", "destination_iata": "SFO",
             "scheduled_out": "2027-03-01T17:00:00Z", "scheduled_in": "2027-03-01T19:10:00Z"}]})
        (f,) = aeroapi.search("ident", ident="AS342", date="2027-03-01", now=T0)
        self.assertIn("/schedules/2027-03-01T00:00:00Z/2027-03-02T00:00:00Z", self.get.call_args.args[0])
        self.assertEqual(self.get.call_args.kwargs["params"]["flight_number"], "342")
        self.assertEqual((f["live"], f["origin"]["code"], f["est_out"]), (False, "SEA", None))

    def test_search_by_route_keeps_nonstop_only(self):
        self.get.return_value = response(body={"flights": [
            {"segments": [fa()]}, {"segments": [fa(), fa(ident_iata="AS9")]}]})
        found = aeroapi.search("route", origin="sea", destination="SFO", date="2027-01-15", now=T0)
        self.assertTrue(self.get.call_args.args[0].endswith("/airports/SEA/flights/to/SFO"))
        self.assertEqual([f["ident"] for f in found], ["AS342"])
        for o, d in (("SEA", "SEA"), ("", "SFO"), ("SEA", "zzzz qqq")):
            with self.assertRaises(ValueError):
                aeroapi.search("route", origin=o, destination=d, date="2027-01-15", now=T0)

    def test_search_by_route_accepts_city_and_airport_names(self):
        self.get.return_value = response(body={"flights": [{"segments": [fa()]}]})
        for o, d in (("Seattle", "san francisco"), ("Seattle (SEA)", "San Francisco (SFO)"),
                     ("seattle-tacoma", "KSFO")):
            aeroapi.search("route", origin=o, destination=d, date="2027-01-15", now=T0)
            self.assertTrue(self.get.call_args.args[0].endswith("/airports/SEA/flights/to/SFO"), (o, d))

    def test_search_date_limits(self):
        with self.assertRaises(ValueError):
            aeroapi.search("ident", ident="AS342", date="2026-12-01", now=T0)
        with self.assertRaises(ValueError):
            aeroapi.search("ident", ident="AS342", date="2028-06-01", now=T0)
        with self.assertRaises(ValueError):
            aeroapi.search("ident", ident="AS342", date="soon", now=T0)
        self.get.assert_not_called()

    def test_no_key_budget_and_rate_limits(self):
        self.key.return_value = ""
        with self.assertRaises(aeroapi.NoKey):
            aeroapi._get("/x", {}, "flights", T0)
        self.key.return_value = "k"
        with patch.object(store, "budget_limit", return_value=0):
            with self.assertRaises(aeroapi.BudgetExhausted):
                aeroapi._get("/x", {}, "flights", T0)
        self.get.assert_not_called()
        aeroapi.reset_rate()
        for n in range(aeroapi.RATE):
            aeroapi._get("/x", {}, "flights", T0 + n)
        with self.assertRaises(aeroapi.RateLimited):
            aeroapi._get("/x", {}, "flights", T0 + 30)
        aeroapi._get("/x", {}, "flights", T0 + 61)  # the first call has aged out

    def usage_body(self):
        return {"total_calls": 17, "total_cost": 0.21, "resource_details": [
            {"operation": "GET /flights/{ident}", "total_resource_calls": 6, "resource_cost": 0.03},
            {"operation": "GET /flights/{id}/position", "total_resource_calls": 3, "resource_cost": 0.03},
            {"operation": "GET /airports/{id}/flights/to/{dest_id}", "total_resource_calls": 1, "resource_cost": 0.05},
            {"operation": "GET /schedules/{date_start}/{date_end}", "total_resource_calls": 4, "resource_cost": 0.08},
            {"operation": "GET /operators/{id}", "total_resource_calls": 3, "resource_cost": 0.02}]}

    def test_reads_usage_from_flightaware(self):
        self.fetch_patch.stop()
        self.get.return_value = response(body=self.usage_body())
        u = aeroapi.usage(T0)
        url, = self.get.call_args.args
        self.assertTrue(url.endswith("/account/usage"))
        self.assertEqual(self.get.call_args.kwargs["params"], {"start": "2027-01-01T00:00:00Z"})
        self.assertEqual((u["ok"], u["total"], u["calls"]), (True, 0.21, 17))
        self.assertEqual([(p["label"], p["calls"], p["cost"]) for p in u["parts"]],
                         [("Route search", 1, 0.05), ("Date search", 4, 0.08), ("Live position", 3, 0.03),
                          ("Flight status", 6, 0.03), ("Other", 3, 0.02)])
        aeroapi.usage(T0 + aeroapi.USAGE_TTL - 1)
        self.assertEqual(self.get.call_count, 1)  # cached
        aeroapi.usage(T0 + aeroapi.USAGE_TTL)
        self.assertEqual(self.get.call_count, 2)
        for bad in (response(500), response(body={"nope": 1})):
            self.get.return_value = bad
            self.assertIsNone(aeroapi._fetch_usage(T0))

    def test_limit_uses_flightaware_total(self):
        self.fetch.return_value = {"total": 4.9, "calls": 99, "parts": []}
        self.assertEqual(aeroapi.spent(T0), (4.9, True))
        self.assertEqual(aeroapi.level(T0), aeroapi.WARN)
        aeroapi._get("/x", {}, "flights", T0)  # 4.905 so far: still under $5
        self.assertEqual(aeroapi.spent(T0), (4.905, True))  # our calls since the fetch are added on
        self.fetch.return_value = {"total": 4.996, "calls": 99, "parts": []}
        aeroapi.usage(T0, force=True)
        with self.assertRaises(aeroapi.BudgetExhausted):
            aeroapi._get("/x", {}, "schedules", T0)
        self.assertEqual(aeroapi.level(T0), aeroapi.OUT)
        self.assertEqual(self.get.call_count, 1)
        with patch.object(store, "budget_limit", return_value=10):
            self.assertEqual(aeroapi.level(T0), aeroapi.OK)
        with patch.object(store, "budget_limit", return_value=0):
            self.assertEqual(aeroapi.level(T0), aeroapi.OFF)

    def test_unreachable_usage(self):
        # Never reached this month: our own ledger.
        store.spend(1.0, T0)
        self.assertEqual(aeroapi.spent(T0), (1.0, False))
        # Reached once, then unreachable: the last figures, marked stale.
        self.fetch.return_value = {"total": 2.0, "calls": 9, "parts": []}
        aeroapi.usage(T0, force=True)
        self.fetch.return_value = None
        u = aeroapi.usage(T0 + aeroapi.USAGE_TTL)
        self.assertEqual((u["ok"], u["stale"], u["total"], u["fetched_at"]), (True, True, 2.0, T0))
        self.assertEqual(aeroapi.spent(T0 + aeroapi.USAGE_TTL), (2.0, True))
        # A new month starts over.
        self.assertFalse(aeroapi.usage(T0 + 40 * 86400)["ok"])

    def test_http_errors(self):
        self.get.return_value = response(404)
        self.assertEqual(aeroapi._get("/x", {}, "flights", T0), {})
        self.get.return_value = response(401)
        with self.assertRaisesMessage(aeroapi.ApiError, "rejected the API key"):
            aeroapi._get("/x", {}, "flights", T0)
        self.get.return_value = response(429)
        with self.assertRaises(aeroapi.RateLimited):
            aeroapi._get("/x", {}, "flights", T0)
        self.get.return_value = response(400, {"detail": "bad start"})
        with self.assertRaisesMessage(aeroapi.ApiError, "400: bad start"):
            aeroapi._get("/x", {}, "flights", T0)

    def test_lookup_from_schedule_picks_closest_departure(self):
        f = {"ident": "AS342", "ident_icao": "ASA342", "sched_out": T0 + 3 * H, "origin": {"code": "SEA"}}
        self.get.return_value = response(body={"flights": [
            fa(T0 + 4 * H, fa_flight_id="later"), fa(fa_flight_id="right"),
            fa(fa_flight_id="elsewhere", origin=airport("PDX", "KPDX", "Portland"))]})
        self.assertEqual(aeroapi.lookup(f, T0)["fa_flight_id"], "right")
        self.assertIn("/flights/ASA342", self.get.call_args.args[0])
        self.get.return_value = response(body={"flights": []})
        self.assertIsNone(aeroapi.lookup(f, T0))

    def test_position(self):
        self.get.return_value = response(body={"last_position": {
            "latitude": 45.1, "longitude": -122.3, "altitude": 350, "groundspeed": 460, "heading": 180,
            "timestamp": aeroapi.iso(T0)}})
        pos = aeroapi.position("ASA342-1-x", T0)
        self.assertEqual((pos["alt_ft"], pos["speed_kt"], pos["at"]), (35000, 460, T0))
        self.get.return_value = response(body={})
        self.assertIsNone(aeroapi.position("ASA342-1-x", T0))


class StoreTests(Fresh, TestCase):
    def test_track_limits(self):
        f = self.tracked()
        self.assertEqual(store.get(f["id"])["notified"]["delay"], 0)
        self.assertNotIn("live", f)
        with self.assertRaisesMessage(ValueError, "Already tracking AS342"):
            self.tracked()
        for n in range(store.MAX_FLIGHTS - 1):
            self.tracked(fa_flight_id=f"x{n}", ident_iata=f"AS{n}")
        with self.assertRaisesMessage(ValueError, "up to 20"):
            self.tracked(fa_flight_id="one-more", ident_iata="AS999")

    def test_search_cache(self):
        key = store.cache_search([{"ident": "AS342"}], now=T0)
        self.assertEqual(store.candidate(f"{key}:0", now=T0)["ident"], "AS342")
        for ref in (f"{key}:1", f"{key}:x", "nope:0"):
            with self.assertRaises(LookupError):
                store.candidate(ref, now=T0)
        with self.assertRaisesMessage(LookupError, "expired"):
            store.candidate(f"{key}:0", now=T0 + store.SEARCH_TTL)

    def test_ledger_survives_restart(self):
        self.assertTrue(store.spend(4.99, T0))
        self.assertTrue(store.spend(0.02, T0))  # the limit is enforced in aeroapi, not the ledger
        store.reset_cache()
        b = store.budget(T0)
        self.assertEqual((b["spent"], b["calls"], b["month"]), (5.01, 2, "2027-01"))
        self.assertEqual(store.budget(T0 + 40 * 86400)["spent"], 0)

    def test_events(self):
        f = self.tracked()
        sched = f["sched_out"]

        def update(**data):
            return store.update(f["id"], data, T0)[1]

        self.assertEqual(update(est_out=sched + 20 * 60), ["delay"])
        self.assertEqual(update(est_out=sched + 25 * 60), [])
        self.assertEqual(update(est_out=sched + 40 * 60), ["delay"])
        self.assertEqual(update(est_out=sched), ["ontime"])
        self.assertEqual(update(gate_out="C1"), [])
        self.assertEqual(update(gate_out="D4"), ["gate"])
        self.assertEqual(store.get(f["id"])["notified"]["gate_from"], "C1")
        self.assertEqual(update(gate_out="D4"), [])
        self.assertEqual(update(act_out=sched + 600, act_off=sched + 1200), ["departed"])
        self.assertEqual(update(act_on=T0 + 5 * H), ["landed"])
        self.assertEqual(update(act_in=T0 + 5 * H + 300), [])
        self.assertIsNone(store.get(f["id"])["next_poll"])
        g = self.tracked(fa_flight_id="b", ident_iata="AS2")
        self.assertEqual(store.update(g["id"], {"cancelled": True}, T0)[1], ["cancelled"])

    def test_next_poll(self):
        def at(**kw):
            return store.next_poll(kw, T0) - T0

        self.assertEqual(at(sched_out=T0 + 72 * H), 48 * H)
        self.assertEqual(at(sched_out=T0 + 10 * H), H)
        self.assertEqual(at(sched_out=T0 + 2.5 * H), 0.5 * H)
        self.assertEqual(at(sched_out=T0 + H), 600)
        self.assertEqual(at(sched_out=T0 + 600), 300)
        self.assertEqual(at(sched_out=T0, act_out=T0), 300)
        self.assertEqual(at(act_off=T0 - H, est_on=T0 + H), 600)
        self.assertEqual(at(act_off=T0 - H, est_on=T0 + 1200), 300)
        self.assertEqual(at(act_off=T0 - H, est_on=T0 + store.FINAL_APPROACH, est_in=T0 + H), 120)
        self.assertEqual(at(act_off=T0 - H, est_on=T0 - 60), 120)
        # Touchdown is the end: no polling for the gate.
        self.assertIsNone(store.next_poll({"act_on": T0}, T0))
        self.assertIsNone(store.next_poll({"act_on": T0 - 300, "act_in": T0}, T0))
        self.assertIsNone(store.next_poll({"act_in": T0}, T0))
        self.assertIsNone(store.next_poll({"cancelled": True}, T0))

    def test_stops_polling_at_touchdown(self):
        f = self.tracked()
        f = store.update(f["id"], {"act_off": T0 + 3 * H, "est_on": T0 + 5 * H}, T0 + 5 * H - 300)[0]
        self.assertEqual(f["next_poll"], T0 + 5 * H - 300 + 120)
        f = store.update(f["id"], {"act_on": T0 + 5 * H}, T0 + 5 * H)[0]
        self.assertIsNone(f["next_poll"])

    def test_prune(self):
        landed = self.tracked(fa_flight_id="a", ident_iata="AS1")
        store.update(landed["id"], {"act_off": T0, "act_on": T0 + H}, T0 + H)
        self.tracked(fa_flight_id="b", ident_iata="AS2")
        self.assertEqual(store.prune(T0 + H + 60), [])
        gone = store.prune(T0 + H + store.KEEP_LANDED + 1)
        self.assertEqual([f["ident"] for f in gone], ["AS1"])
        self.assertEqual([f["ident"] for f in store.flights()], ["AS2"])

    def test_landed_drops_off_ten_minutes_after_touchdown(self):
        # Timed from touchdown even while it's still taxiing to the gate.
        f = self.tracked()
        store.update(f["id"], {"act_off": T0 + 3 * H, "act_on": T0 + 5 * H}, T0 + 5 * H)
        self.assertEqual(store.prune(T0 + 5 * H + 600), [])
        self.assertEqual(len(store.prune(T0 + 5 * H + 601)), 1)

    def test_poller_wakes_when_a_flight_expires(self):
        # A flight with nothing left to poll must still be pruned on time,
        # not up to five minutes late on the idle wake.
        f = self.tracked()
        store.update(f["id"], {"act_off": T0 + 3 * H, "act_on": T0 + 5 * H,
                               "act_in": T0 + 5 * H + 300}, T0 + 5 * H + 300)
        self.assertIsNone(store.flights()[0]["next_poll"])
        self.assertEqual(store.next_wake(), T0 + 5 * H + store.KEEP_LANDED + 1)

    def test_labels(self):
        f = self.tracked()
        self.assertEqual(store.label(f, T0), ("Scheduled", "Departs 11:00 AM"))
        f["est_out"] += 30 * 60
        self.assertEqual(store.label(f, T0), ("Delayed", "Departs 11:30 AM (+30m)"))
        f.update(act_out=T0 + 3.5 * H, act_off=T0 + 3.6 * H, est_on=T0 + 4.9 * H)
        # Headline is the gate time (est_in), not touchdown (est_on).
        self.assertEqual(store.label(f, T0 + 4 * H), ("In air", "Arrives 1:00 PM · 1h 0m left"))
        self.assertEqual(store.label(f, T0 + 4.9 * H - 60), ("In air", "Landing now · at gate ~1:00 PM"))
        self.assertEqual(store.label(f, T0 + 4.9 * H + 120), ("In air", "Landing now · at gate ~1:00 PM"))
        snap = store.snapshot(f, T0)
        self.assertEqual((snap["phase"], snap["labels"]["sched_out"]), ("air", "11:00 AM"))
        self.assertNotIn("notified", snap)
        f.update(act_on=T0 + 4.9 * H)
        self.assertEqual(store.label(f, T0 + 4.95 * H), ("Landed", "Landed 12:54 PM · Taxiing to the gate"))
        f["gate_in"] = "C10"
        self.assertEqual(store.label(f, T0 + 4.95 * H), ("Landed", "Landed 12:54 PM · Taxiing to C10"))
        f["act_in"] = T0 + 5 * H + 120
        self.assertEqual(store.label(f, T0 + 5.1 * H), ("Landed", "At gate C10 1:02 PM"))
        del f["gate_in"]
        self.assertEqual(store.label(f, T0 + 5.1 * H), ("Landed", "At the gate 1:02 PM"))
        self.assertEqual(store.snapshot(f, T0)["phase"], "landed")


class WorkerTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        self.push = patch("flights.worker.alerts.push").start()
        self.settings = {"notify_targets": "notify.mobile_app_a; mobile_app_b"}
        patch("flights.worker.app_setting",
              side_effect=lambda app, key, default=None, **kw: self.settings.get(key, default)).start()
        patch("flights.worker.threading.Thread", side_effect=lambda target, args, **kw: type(
            "T", (), {"start": lambda self: target(*args)})()).start()
        self.notify = patch("bdh_sdk.ha.call_service", return_value=(True, None)).start()

    def test_should_start(self):
        self.assertTrue(worker.should_start(["/x/bdh_sdk/runtime/__main__.py"]))
        self.assertTrue(worker.should_start(["/venv/bin/daphne", "app"]))
        self.assertFalse(worker.should_start(["manage.py", "test"]))
        with patch.dict("os.environ", {"BDH_FLIGHTS_WORKER": "0"}):
            self.assertFalse(worker.should_start(["/venv/bin/daphne"]))

    def test_delay_pops_up_and_notifies(self):
        f = self.tracked(popups=True)
        late = dict(f, est_out=f["sched_out"] + 45 * 60, gate_out="")
        with patch("flights.worker.aeroapi.lookup", return_value=late):
            worker.refresh(f, T0)
        (call,) = self.push.call_args_list
        alert = call.args[0]
        self.assertEqual((alert.key, alert.embed_url, alert.title),
                         (f"flight:{f['id']}", f"/flights/popup/{f['id']}/", "AS342 SEA → SFO"))
        self.assertEqual(alert.body, "Delayed 45m · now departs 11:45 AM")
        self.assertTrue(alert.speak.startswith("A S 342. Delayed"))
        self.assertEqual([c.args[1] for c in self.notify.call_args_list], ["mobile_app_a", "mobile_app_b"])
        self.assertEqual(self.notify.call_args.args[3]["message"], alert.body)

    def test_popups_off_and_disabled_events(self):
        f = self.tracked(popups=False)
        self.settings["notify_progress"] = False
        with patch("flights.worker.aeroapi.lookup", return_value=dict(f, act_out=T0, act_off=T0 + 60)):
            worker.refresh(f, T0)
        self.push.assert_not_called()
        self.notify.assert_not_called()
        self.assertEqual(store.get(f["id"])["notified"]["departed"], True)

    def test_refresh_errors_back_off(self):
        f = self.tracked()
        cases = ((aeroapi.BudgetExhausted(), worker.RETRY_BUDGET), (aeroapi.RateLimited(), 60),
                 (aeroapi.ApiError("boom"), worker.RETRY_ERROR))
        for exc, wait in cases:
            with patch("flights.worker.aeroapi.lookup", side_effect=exc):
                g = worker.refresh(f, T0)
            self.assertEqual((g["error"], g["next_poll"]), (str(exc), T0 + wait))
        far = self.tracked(T0 + 4 * 86400, fa_flight_id="far", ident_iata="AS9")
        with patch("flights.worker.aeroapi.lookup", return_value=None):
            self.assertEqual(worker.refresh(far, T0)["next_poll"], far["sched_out"] - 24 * H)
            self.assertEqual(worker.refresh(f, T0)["next_poll"], T0 + H)

    def test_tick_refreshes_due_and_prunes(self):
        f = self.tracked()
        with patch("flights.worker.aeroapi.lookup", return_value=dict(f)) as lookup, \
                patch("flights.worker.alerts.dismiss") as dismiss:
            worker.tick(T0 + 10)
            lookup.assert_not_called()
            worker.tick(f["next_poll"])
            lookup.assert_called_once()
            store.update(f["id"], {"act_off": T0, "act_on": T0 + H}, T0 + H)
            worker.tick(T0 + H + store.KEEP_LANDED + 1)
            dismiss.assert_called_once_with("flights", f"flight:{f['id']}")
        self.assertEqual(store.flights(), [])


class HomepageTests(Fresh, TestCase):
    def test_levels(self):
        self.assertEqual(homepage.status().text, "No flights")
        f = self.tracked(time.time())
        s = homepage.status()
        self.assertEqual((s.level, s.rows[0].title), (INFO, "AS342 SEA → SFO"))
        store.update(f["id"], {"act_out": time.time(), "act_off": time.time()})
        self.assertEqual(homepage.status().level, ON)
        store.update(f["id"], {"cancelled": True})
        s = homepage.status()
        self.assertEqual((s.level, s.text, s.rows[0].dimmed), (WARN, "AS342 cancelled", True))
        patch.object(aeroapi, "api_key", return_value="k").start()
        store.spend(store.budget_limit())
        self.assertEqual(homepage.status().text, "FlightAware limit reached")
        aeroapi.reset_usage()
        with patch.object(store, "budget_limit", return_value=0):
            self.assertEqual(homepage.status().text, "FlightAware lookups off")
        with patch.object(aeroapi, "level", side_effect=RuntimeError):
            self.assertEqual(homepage.status().text, "AS342 cancelled")  # status never fails on usage
        with patch("bdh_sdk.alerts.dismiss"):
            self.assertEqual(homepage.run_action(f["id"], "remove"), "Stopped tracking AS342")
        self.assertEqual(store.flights(), [])


class ViewTests(Fresh, TestCase):
    def setUp(self):
        super().setUp()
        patch("flights.aeroapi.api_key", return_value="k").start()
        patch("flights.worker.poke").start()
        self.now = time.time()
        self.get = patch("flights.aeroapi.requests.get",
                         return_value=response(body={"flights": [fa(self.now)]})).start()

    def post(self, name, body=None, *args):
        return self.client.post(reverse(name, args=args), body or {}, content_type="application/json")

    def test_airport_lookup_endpoint_and_home(self):
        r = self.client.get(reverse("flights_airports"), {"q": "seattle"})
        self.assertEqual(r.json()["airports"][0]["code"], "SEA")
        self.assertEqual(self.client.get(reverse("flights_airports")).json(), {"airports": []})
        self.assertContains(self.client.get(reverse("flights_index")), "data-airports-url")
        with patch("flights.airports.home", return_value={"code": "SEA", "city": "Seattle"}):
            self.assertEqual(self.client.get(reverse("flights_state")).json()["home"]["code"], "SEA")

    def test_search_track_and_control(self):
        page = self.client.get(reverse("flights_index"))
        self.assertContains(page, "fl-initial")
        today = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).date().isoformat()
        r = self.post("flights_search", {"mode": "ident", "ident": "AS342", "date": today})
        self.assertEqual(r.status_code, 200)
        (res,) = r.json()["results"]
        self.assertEqual((res["ident"], res["pill"]), ("AS342", "Scheduled"))
        self.assertEqual(self.post("flights_search", {"mode": "ident", "ident": "?"}).status_code, 400)
        state = self.post("flights_track", {"ref": res["ref"], "popups": True}).json()
        (f,) = state["flights"]
        self.assertTrue(f["popups"] and state["has_key"])
        self.assertEqual(state["usage"], aeroapi.OK)
        self.assertNotIn("budget", state)  # no money figures in the app itself
        self.assertNotIn("budget", r.json())
        self.assertEqual(self.post("flights_track", {"ref": res["ref"]}).status_code, 400)
        self.assertFalse(self.post("flights_control", {}, f["id"], "popups_off").json()["flights"][0]["popups"])
        self.post("flights_control", {}, f["id"], "refresh")  # just looked up: no new call
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(self.client.get(reverse("flights_popup", args=[f["id"]])).status_code, 200)
        with patch("bdh_sdk.alerts.dismiss"):
            self.assertEqual(self.post("flights_control", {}, f["id"], "remove").json()["flights"], [])
        self.assertEqual(self.client.get(reverse("flights_popup", args=[f["id"]])).status_code, 404)
        self.assertEqual(self.post("flights_control", {}, f["id"], "bogus").status_code, 404)

    def test_search_api_error(self):
        self.get.return_value = response(500)
        today = dt.datetime.fromtimestamp(self.now, dt.timezone.utc).date().isoformat()
        r = self.post("flights_search", {"mode": "ident", "ident": "AS342", "date": today})
        self.assertEqual(r.status_code, 502)
        self.assertIn("500", r.json()["error"])

    def test_position_is_cached(self):
        f = self.tracked(self.now)
        url = reverse("flights_position", args=[f["id"]])
        self.assertIsNone(self.client.get(url).json()["position"])
        store.update(f["id"], {"act_out": self.now, "act_off": self.now})
        self.get.return_value = response(body={"last_position": {"latitude": 1, "longitude": 2, "altitude": 300}})
        self.assertEqual(self.client.get(url).json()["position"]["alt_ft"], 30000)
        self.client.get(url)
        self.assertEqual(self.get.call_count, 1)

    def test_usage_page_is_admin_only(self):
        url = reverse("flights_usage")
        with patch("flights.views.is_admin", return_value=False):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url).status_code, 404)
        self.fetch.return_value = {"total": 4.8, "calls": 17, "parts": [
            {"label": "Flight status", "calls": 6, "cost": 0.03}]}
        with patch("flights.views.is_admin", return_value=True):
            page = self.client.get(url)
            self.assertContains(page, "$4.80")
            self.assertContains(page, "Flight status")
            self.assertContains(page, "Nearly at this month")
            self.assertEqual(self.client.get(reverse("flights_state")).json()["usage"], aeroapi.WARN)
            self.assertRedirects(self.client.post(url), url, fetch_redirect_response=False)
            self.assertEqual(self.fetch.call_count, 2)  # Refresh skips the cache
            self.fetch.return_value = None
            aeroapi.reset_usage()
            self.assertContains(self.client.get(url), "this hub's own count")
    """Route search takes what people know (a city or airport name), not just codes."""

    def codes(self, q):
        return [a["code"] for a in airports.search(q)]

    def test_every_airport_is_found_by_its_own_name(self):
        # Catches folding bugs like the en dash in "Seattle–Tacoma" gluing two words together.
        self.assertEqual(airports.fold("Seattle–Tacoma Montréal"), ["seattle", "tacoma", "montreal"])
        missing = [a["code"] for a in airports._airports()
                   if a["code"] not in {r["code"] for r in airports.search(f'{a["name"]} {a["city"]}', limit=50)}]
        self.assertEqual(missing, [])

    def test_search_by_city_name_code_and_icao(self):
        for q, code in (("seattle", "SEA"), ("Seattle", "SEA"), ("san francisco", "SFO"), ("kona", "KOA"),
                        ("SEA", "SEA"), ("sea", "SEA"), ("KSEA", "SEA"), ("vancouver", "YVR"),
                        ("kamloops", "YKA"), ("montreal", "YUL"), ("montréal", "YUL"), ("heathrow", "LHR")):
            self.assertEqual(self.codes(q)[:1], [code], q)
        self.assertEqual(self.codes(""), [])
        self.assertEqual(self.codes("zzzz qqq"), [])
        self.assertLessEqual(len(airports.search("san")), 8)
        a = airports.search("seattle")[0]
        self.assertEqual(set(a), {"code", "name", "city", "place"})

    def test_resolve(self):
        for text in ("SEA", "seattle", "Seattle (SEA)", " Seattle (sea) ", "KSEA"):
            self.assertEqual(airports.resolve(text, "origin"), "SEA", text)
        for bad in ("", "  ", "zzzz qqq"):
            with self.assertRaises(ValueError):
                airports.resolve(bad, "origin")

    def test_nearest_is_the_hub_home_airport(self):
        self.assertEqual(airports.nearest(47.57, -122.22)["code"], "SEA")   # Mercer Island
        self.assertEqual(airports.nearest(50.88, -119.89)["code"], "YKA")   # Sun Peaks
        self.assertIsNone(airports.nearest(None, None))
        with patch("bdh_sdk.location.location", return_value=None):
            self.assertIsNone(airports.home())
        with patch("bdh_sdk.location.location", side_effect=RuntimeError):
            self.assertIsNone(airports.home())
        with patch("bdh_sdk.location.location", return_value=MagicMock(latitude=47.57, longitude=-122.22)):
            self.assertEqual(airports.home()["code"], "SEA")


class PluginTests(SimpleTestCase):
    def test_plugin_importable(self):
        assert_plugin_importable("flights.plugin")

    def test_manifest(self):
        from flights.plugin import spec
        m = spec.manifest()
        self.assertEqual((m["api_version"], m["network"], m["lifecycle"]), (2, "outbound", "always"))
        self.assertEqual(m["permissions"], ["alerts", "ha.call:notify.*"])


class HubFrameLayoutTests(SimpleTestCase):
    """The app renders inside the hub's iframe, already below the hub navbar, so
    its CSS must not reserve space for that navbar (top:62px / 100vh-62px /
    phone 118px) nor scope rules under the hub's `.navbar`, which doesn't
    exist in the iframe."""

    def test_no_hub_navbar_offsets(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertTrue((root / "templates").exists())
        pattern = re.compile(r"top\s*:\s*(6[0-9]|118)px|100vh\s*-\s*(6[0-9]|118)px|\.navbar(?![-\w])")
        hits = []
        for path in list(root.glob("templates/**/*.html")) + list(root.glob("static/**/*.css")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])
