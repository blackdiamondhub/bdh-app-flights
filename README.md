# bdh-app-flights

Flight tracker app for [BlackDiamondHub](https://github.com/blackdiamondlodge/BlackDiamondHub).

Find a flight by number (`AS342`) or by route and date (`SEA → SFO`). Then track it to see:

- gates, delays and progress;
- the aircraft's live position while it's in the air.

Changes can be announced in two ways:

- **Screen popups with a chime.** Turn these on or off per flight.
- **Phone notifications.** These go through Home Assistant `notify.*` services.

Data comes from [FlightAware AeroAPI](https://www.flightaware.com/aeroapi/). The Personal tier includes $5 of free usage a month.

## Settings

| Setting | Purpose |
|---|---|
| `aeroapi_key` | AeroAPI key (secret). Without it, searches are disabled. |
| `monthly_budget` | Lookups stop once this many dollars have been spent in a UTC calendar month. Default `5`. |
| `chime` | Chime played with flight popups. |
| `notify_targets` | Comma-separated HA notify services, e.g. `mobile_app_pixel_8`. |
| `notify_delay` | Toggle phone notifications for delays. |
| `notify_gate` | Toggle phone notifications for gate changes. |
| `notify_progress` | Toggle phone notifications for takeoff and landing. |
| `notify_cancel` | Toggle phone notifications for cancellations and diversions. |

## Polling and cost

Each tracked flight is polled on a schedule that tightens as departure approaches:

| When | Poll interval |
|---|---|
| More than a day out | Once, 24 h before departure |
| Up to 2 h before departure | Hourly |
| From 2 h before until 20 min before | Every 10 min |
| Within 20 min of departure, and while taxiing | Every 5 min |
| In the air | Every 10 min |
| Within 30 min of touchdown | Every 5 min |
| Within 15 min of touchdown, and after landing until the plane is at the gate (max 45 min) | Every minute |

Polling stops once the flight is at the gate or is cancelled. Arrival times on the card are gate arrival times, as on FlightAware.

The app keeps a spend ledger and stays under AeroAPI's rate limit. Every call is counted against `monthly_budget`.

## Development

Tests run in the hub checkout:

```
python manage.py test flights --settings=BlackDiamondHub.settings_test --exclude-tag=selenium
```
