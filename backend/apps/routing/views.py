from rest_framework.decorators import api_view
from rest_framework.response import Response

from .services import geocode, route


@api_view(["GET"])
def geocode_view(request):
    q = request.query_params.get("q", "")
    result = geocode(q)
    if result is None:
        return Response({"detail": "Could not resolve location."}, status=404)
    return Response(result)


@api_view(["POST"])
def route_view(request):
    """Body: {"points": [{"lat": .., "lon": ..}, ...]}  (>= 2 points)."""
    pts = request.data.get("points") or []
    try:
        waypoints = [(float(p["lat"]), float(p["lon"])) for p in pts]
        assert len(waypoints) >= 2
    except (KeyError, TypeError, ValueError, AssertionError):
        return Response(
            {"detail": "Provide at least two points as {lat, lon} objects."}, status=400
        )
    return Response(route(waypoints))
