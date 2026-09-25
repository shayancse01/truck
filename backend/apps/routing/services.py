"""Free, key-less external API helpers: Nominatim geocoding + OSRM routing.

Both services are used with graceful degradation: if the public instance is
unreachable we fall back to a straight-line (haversine * road factor) estimate
so the planner never hard-fails during a demo.
"""
from __future__ import annotations

import math
from typing import Iterable

import requests
from django.conf import settings

CFG = settings.ROUTING_CONFIG

ROAD_FACTOR = 1.16  # great-circle -> road distance multiplier (US interstates)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": CFG["USER_AGENT"]})
    return s


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def geocode(query: str) -> dict | None:
    """Forward-geocode an address/city through Nominatim (free)."""
    q = (query or "").strip()
    if not q:
        return None
    # Already coordinates? ("40.7484,-73.9857")
    try:
        lat_s, lon_s = q.split(",")
        lat, lon = float(lat_s), float(lon_s)
        if abs(lat) <= 90 and abs(lon) <= 180:
            return {"lat": lat, "lon": lon, "display_name": q, "source": "coordinates"}
    except (ValueError, AttributeError):
        pass

    try:
        r = _session().get(
            CFG["NOMINATIM_URL"],
            params={
                "q": q,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "us",
                "addressdetails": 0,
            },
            timeout=CFG["HTTP_TIMEOUT"],
        )
        r.raise_for_status()
        data = r.json()
        if data:
            return {
                "lat": float(data[0]["lat"]),
                "lon": float(data[0]["lon"]),
                "display_name": data[0]["display_name"],
                "source": "nominatim",
            }
    except requests.RequestException:
        pass
    return None


def _fallback_geometry(points: Iterable[tuple[float, float]]) -> list[list[float]]:
    """Densify waypoints into a plausible polyline when OSRM is unavailable."""
    pts = list(points)
    out: list[list[float]] = []
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        steps = max(2, int(haversine_miles((la1, lo1), (la2, lo2)) // 60) + 1)
        for i in range(steps):
            t = i / steps
            out.append([round(lo1 + (lo2 - lo1) * t, 6), round(la1 + (la2 - la1) * t, 6)])
    out.append([pts[-1][1], pts[-1][0]])
    return out


def route(waypoints: list[tuple[float, float]]) -> dict:
    """Drive-routing between ordered (lat, lon) waypoints via public OSRM."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in waypoints)
    url = f"{CFG['OSRM_BASE_URL']}/route/v1/driving/{coords}"
    try:
        r = _session().get(
            url,
            params={"overview": "full", "geometries": "geojson", "alternatives": "false"},
            timeout=CFG["HTTP_TIMEOUT"],
        )
        r.raise_for_status()
        j = r.json()
        if j.get("code") == "Ok" and j["routes"]:
            rt = j["routes"][0]
            return {
                "distance_miles": round(rt["distance"] / 1609.344, 2),
                "duration_hours": round(rt["duration"] / 3600.0, 3),
                "geometry": rt["geometry"]["coordinates"],  # [lon, lat][]
                "source": "osrm",
            }
    except requests.RequestException:
        pass

    # ---- graceful fallback -------------------------------------------------
    dist = sum(haversine_miles(a, b) for a, b in zip(waypoints, waypoints[1:])) * ROAD_FACTOR
    hours = dist / CFG["FALLBACK_SPEED_MPH"]
    return {
        "distance_miles": round(dist, 2),
        "duration_hours": round(hours, 3),
        "geometry": _fallback_geometry(waypoints),
        "source": "estimated",
    }
