import datetime as dt

from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.routing.services import geocode, route

from .hos import ScheduleError, generate_schedule
from .models import Trip
from .serializers import PlanRequestSerializer, TripSerializer


@api_view(["POST"])
@permission_classes([AllowAny])
def plan_trip(request):
    """Full pipeline: geocode 3 locations -> OSRM route -> HOS schedule.

    Inputs (per the assessment brief):
      current_location, pickup_location, dropoff_location, cycle_used (hrs)
    Outputs:
      route geometry + stops/rests for the map, and daily ELD log sheets.
    """
    ser = PlanRequestSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    d = ser.validated_data

    # ---- 1. geocode -----------------------------------------------------------
    resolved = {}
    for key in ("current_location", "pickup_location", "dropoff_location"):
        g = geocode(d[key])
        if g is None:
            return Response({"detail": f"Could not resolve location: {d[key]}"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        resolved[key] = g

    # ---- 2. route (current -> pickup -> dropoff) ------------------------------
    waypoints = [(resolved[k]["lat"], resolved[k]["lon"])
                 for k in ("current_location", "pickup_location", "dropoff_location")]
    r12 = route(waypoints[:2])   # repositioning leg to pickup
    r23 = route(waypoints[1:])   # loaded haul to drop-off

    # The HOS clock starts when duty starts at the pickup; the repositioning
    # leg is included in the trip plan as well (driver is on-duty then too).
    total_miles = r12["distance_miles"] + r23["distance_miles"]
    total_hours = r12["duration_hours"] + r23["duration_hours"]

    # ---- 3. schedule ------------------------------------------------------------
    # Departure time in the driver's local zone (naive local time drives the sim).
    start_local = d.get("start_time")
    if start_local:
        naive = timezone.make_naive(start_local, tz=dt.timezone.utc) \
            if timezone.is_aware(start_local) else start_local.replace(tzinfo=None)
    else:
        utc_now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        naive = utc_now + dt.timedelta(hours=d["utc_offset_hours"])

    try:
        plan = generate_schedule(
            distance_miles=total_miles,
            drive_hours_total=max(total_hours, 0.1),
            cycle_used_hours=d["cycle_used_hours"],
            start_time=naive,
            avg_speed_mph=d.get("avg_speed_mph"),
            allow_34hr_restart=d["allow_34hr_restart"],
        )
    except ScheduleError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    payload = {
        "inputs": {
            "current_location": resolved["current_location"],
            "pickup_location": resolved["pickup_location"],
            "dropoff_location": resolved["dropoff_location"],
            "cycle_used_hours": d["cycle_used_hours"],
            "start_time": naive.isoformat(),
        },
        "route": {
            "legs": {"repositioning": r12, "haul": r23},
            "distance_miles": round(total_miles, 2),
            "driving_hours_raw": round(total_hours, 2),
            # merged polyline for the map (concatenate, dedupe shared vertex)
            "geometry": r12["geometry"] + r23["geometry"][1:],
        },
        "plan": plan,
    }

    trip_id = None
    if d["save"]:
        trip = Trip.objects.create(
            current_location=d["current_location"],
            pickup_location=d["pickup_location"],
            dropoff_location=d["dropoff_location"],
            cycle_used_hours=d["cycle_used_hours"],
            start_time=timezone.now(),
            utc_offset_hours=d["utc_offset_hours"],
            route=payload["route"],
            plan=payload["plan"],
        )
        trip_id = str(trip.id)
        payload["trip_id"] = trip_id

    return Response(payload)


class TripViewSet(viewsets.ModelViewSet):
    queryset = Trip.objects.all()
    serializer_class = TripSerializer

    def get_permissions(self):
        return [AllowAny()]

    @action(detail=True, methods=["get"])
    def plan(self, request, pk=None):
        trip = self.get_object()
        return Response({"trip_id": str(trip.id), "route": trip.route, "plan": trip.plan})
