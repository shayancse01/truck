from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import TripViewSet, plan_trip

router = DefaultRouter()
router.register("trips", TripViewSet, basename="trip")

urlpatterns = [
    path("plan/", plan_trip, name="plan-trip"),
    path("", include(router.urls)),
]
