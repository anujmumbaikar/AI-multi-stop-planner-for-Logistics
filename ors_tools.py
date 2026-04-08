"""
tools/ors_tools.py
OpenRouteService tools — geocoding (Pelias) + VROOM route optimization.

n8n equivalents:
  geocode_address()   →  GPS Pickup / GPS Delivery nodes
  format_ors_jobs()   →  Format Jobs node
  optimize_route()    →  Request Open Route API → Extract Job → Merge Sequence
"""

from __future__ import annotations
import os
import logging
from typing import List

log = logging.getLogger(__name__)

import requests
from langchain_core.tools import tool

ORS_BASE = "https://api.openrouteservice.org"
ORS_KEY = os.getenv("ORS_API_KEY", "")


def _ors_headers() -> dict:
    return {
        "Authorization": ORS_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, application/geo+json",
    }


# ── Geocoding (Pelias) ────────────────────────────────────────────────────────

@tool
def geocode_address(address: str) -> dict:
    """
    Convert a human-readable address to GPS coordinates using ORS Geocoder (Pelias).

    Endpoint: GET https://api.openrouteservice.org/geocode/search
    Params:
        api_key  - your ORS key
        text     - address string
        size     - number of results (we use 1)

    Args:
        address: Full street address to geocode.

    Returns:
        dict with keys: address (str), latitude (float), longitude (float), confidence (float).
    """
    resp = requests.get(
        f"{ORS_BASE}/geocode/search",
        params={
            "api_key": ORS_KEY,
            "text": address,
            "size": 1,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("features"):
        raise ValueError(f"No geocoding result for address: {address!r}")

    feature = data["features"][0]
    lon, lat = feature["geometry"]["coordinates"]
    label = feature["properties"].get("label", address)
    confidence = feature["properties"].get("confidence", 0.0)

    return {
        "address": label,
        "latitude": lat,
        "longitude": lon,
        "confidence": confidence,
    }


# ── ORS Optimization (VROOM) ──────────────────────────────────────────────────

@tool
def optimize_route(stops: List[dict]) -> dict:
    """
    Send geocoded stops to ORS /optimization (VROOM) to get the optimal
    truck pickup sequence with driving duration and distance.

    Endpoint: POST https://api.openrouteservice.org/optimization
    Body schema (VROOM):
      {
        "jobs": [
          { "id": <int>, "location": [lon, lat], "service": 300 }
        ],
        "vehicles": [
          {
            "id": 1,
            "profile": "driving-hgv",
            "start": [depot_lon, depot_lat],
            "end":   [depot_lon, depot_lat],   ← circular return
            "time_window": [28800, 64800]       ← 08:00–18:00 in seconds from midnight
          }
        ]
      }

    Args:
        stops: List of dicts, each must have:
               { stop_index, store_name, address, longitude, latitude }

    Returns:
        dict matching RouteResult schema:
          {
            total_duration_seconds: int,
            total_distance_meters: int,
            ordered_stops: [ { job_id, store_name, address, longitude, latitude,
                               arrival_time_seconds, service_duration_seconds } ]
          }
    """
    if not stops:
        raise ValueError("No stops provided to optimize_route")

    # Depot = first stop for circular routing
    depot_lon = stops[0]["longitude"]
    depot_lat = stops[0]["latitude"]

    jobs = [
        {
            "id": s["stop_index"],
            "location": [s["longitude"], s["latitude"]],
            "service": 300,          # 5 min service time per stop
            "description": s["store_name"],
        }
        for s in stops
    ]

    vehicles = [
        {
            "id": 1,
            "profile": "driving-hgv",  # heavy goods vehicle (truck)
            "start": [depot_lon, depot_lat],
            "end": [depot_lon, depot_lat],   # circular — returns to depot
            "time_window": [28800, 64800],   # 08:00–18:00
            "capacity": [100],               # arbitrary capacity units
        }
    ]

    payload = {"jobs": jobs, "vehicles": vehicles}

    resp = requests.post(
        f"{ORS_BASE}/optimization",
        headers=_ors_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    log.info("ORS optimization response: %s", data)

    # Check for API errors
    if "error" in data:
        raise ValueError(f"ORS API error: {data['error']}")

    if not data.get("routes"):
        raise ValueError("ORS optimization returned no routes - check vehicle/job configuration")

    route = data["routes"][0]

    if "summary" not in route:
        # VROOM may return 'cost' instead of 'summary' for some responses
        # Try to extract from 'steps' if summary is missing
        if "cost" in route:
            summary = {"duration": route["cost"], "distance": route.get("distance", 0)}
        else:
            raise ValueError(f"Route missing summary. Full response: {data}")
    else:
        summary = route["summary"]

    # Build a lookup for stop metadata
    stop_lookup = {s["stop_index"]: s for s in stops}

    ordered_stops = []
    for step in route["steps"]:
        if step["type"] != "job":
            continue
        job_id = step["job"]
        s = stop_lookup[job_id]
        ordered_stops.append({
            "job_id": job_id,
            "store_name": s["store_name"],
            "address": s["address"],
            "longitude": s["longitude"],
            "latitude": s["latitude"],
            "arrival_time_seconds": step.get("arrival", 0),
            "service_duration_seconds": step.get("service", 300),
        })

    return {
        "total_duration_seconds": summary["duration"],
        "total_distance_meters": summary["distance"],
        "ordered_stops": ordered_stops,
    }
