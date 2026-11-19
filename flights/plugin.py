from bdh_sdk import PluginSpec, SettingsPage, Setting, Tile
from bdh_sdk.alerts import CHIMES
from bdh_sdk.plugins import BOOL, CHOICE, LIFECYCLE_ALWAYS, NETWORK_OUTBOUND, SECRET, STR

CHIME_CHOICES = (("none", "No chime"),) + tuple(
    (c, c.replace("-", " ").capitalize()) for c in CHIMES)
LIMIT_CHOICES = ((0, "Off — no paid lookups"), (5, "$5"), (10, "$10"), (20, "$20"), (50, "$50"))

spec = PluginSpec(
    id="flights",
    app="flights",
    urls="flights.urls",
    tiles=(
        Tile("Flights", "flights_index", "flights/hub/icon.svg", section="lodge",
             subtitle="Track flights, delays and gates", order=70),
    ),
    status="flights.homepage.status",
    run_action="flights.homepage.run_action",
    settings=(
        Setting("aeroapi_key", "FlightAware AeroAPI key", type=SECRET,
                help="From flightaware.com/aeroapi/portal (Personal tier)."),
        Setting("monthly_budget", "Monthly limit", type=CHOICE, default=5, choices=LIMIT_CHOICES,
                help="Lookups stop once FlightAware's bill for the calendar month (UTC) reaches this. "
                     "The Personal tier includes $5 of free usage a month."),
        Setting("chime", "Chime", type=CHOICE, default="soft-bell", choices=CHIME_CHOICES,
                help="Played on the screens with a flight's popup."),
        Setting("notify_targets", "Phone notifications", type=STR, default="",
                help="Home Assistant notify services, comma separated (e.g. mobile_app_pixel_8). "
                     "Leave empty for no phone notifications."),
        Setting("notify_delay", "Notify on delays", type=BOOL, default=True),
        Setting("notify_gate", "Notify on gate changes", type=BOOL, default=True),
        Setting("notify_progress", "Notify on takeoff and landing", type=BOOL, default=True),
        Setting("notify_cancel", "Notify on cancellations and diversions", type=BOOL, default=True),
    ),
    settings_pages=(
        SettingsPage("FlightAware usage", "flights_usage",
                     subtitle="This month's FlightAware bill against the limit", icon="fas fa-chart-simple"),
    ),
    api_version=2,
    # Popups on the screens, and phone notifications through Home Assistant.
    permissions=("alerts", "ha.call:notify.*"),
    # FlightAware AeroAPI.
    network=NETWORK_OUTBOUND,
    # The poller has to run to catch delays and landings.
    lifecycle=LIFECYCLE_ALWAYS,
    memory_mb=96,
)
