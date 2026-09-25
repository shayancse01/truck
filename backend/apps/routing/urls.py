from django.urls import path

from .views import geocode_view, route_view

urlpatterns = [
    path("geocode/", geocode_view, name="geocode"),
    path("route/", route_view, name="route"),
]
