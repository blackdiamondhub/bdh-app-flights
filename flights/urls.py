from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="flights_index"),
    path("settings/usage/", views.usage_page, name="flights_usage"),
    path("popup/<str:flight_id>/", views.popup, name="flights_popup"),
    path("api/state/", views.state, name="flights_state"),
    path("api/search/", views.search, name="flights_search"),
    path("api/airports/", views.airport_search, name="flights_airports"),
    path("api/track/", views.track, name="flights_track"),
    path("api/flights/<str:flight_id>/position/", views.position, name="flights_position"),
    path("api/flights/<str:flight_id>/<str:act>/", views.control, name="flights_control"),
]
