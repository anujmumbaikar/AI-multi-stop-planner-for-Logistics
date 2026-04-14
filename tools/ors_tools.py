"""
tools/ors_tools.py
OpenRouteService tools — geocoding (Pelias) + VROOM optimisation + distance matrix.

Endpoints used:
  geocode_address()   →  GET  /geocode/search
  elevation_point()   →  POST /elevation/point
  optimize_route()    →  POST /optimization          (VROOM shipments)
  distance_matrix()   →  POST /v2/matrix/{profile}  (before/after POV)
"""
from dotenv import load_dotenv
load_dotenv()

import os
import logging
from typing import List, Optional

log = logging.getLogger(__name__)

import requests
from langchain_core.tools import tool

ORS_BASE = "https://api.openrouteservice.org"
ORS_KEY  = os.getenv("ORS_API_KEY", "")


def _ors_headers() -> dict:
    return {
        "Authorization": ORS_KEY,
        "Content-Type":  "application/json",
        "Accept":        "application/json, application/geo+json",
    }


def _time_str_to_seconds(time_str: str) -> Optional[int]:
    """
    Convert 'HH:MM' string to seconds since midnight.
    Returns None if parsing fails.

    VROOM time windows are in seconds since midnight:
        '08:00' → 28800,  '10:15' → 36900,  '13:30' → 48600
    """
    try:
        parts = time_str.strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        return h * 3600 + m * 60
    except (ValueError, AttributeError, IndexError):
        return None



@tool
def geocode_address(address: str) -> dict:
    """
    Convert a human-readable address to GPS coordinates (ORS Pelias).

    Endpoint: GET /geocode/search
    Args:
        address: Full street address to geocode.
    Returns:
        { address, latitude, longitude, confidence }
    """
    resp = requests.get(
        f"{ORS_BASE}/geocode/search",
        params={
            "api_key":          ORS_KEY,
            "text":             address,
            "size":             1,
            "boundary.country": "IND",    # restrict results to India
            "focus.point.lat":  19.0,     # bias toward Mumbai/Maharashtra region
            "focus.point.lon":  73.0,
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("features"):
        raise ValueError(f"No geocoding result for: {address}")

    feat       = data["features"][0]
    lon, lat   = feat["geometry"]["coordinates"]
    label      = feat["properties"].get("label", address)
    confidence = feat["properties"].get("confidence", 0.0)

    # Reject coordinates that fall outside India's bounding box
    INDIA = {"lat_min": 6.0, "lat_max": 37.0, "lon_min": 67.0, "lon_max": 98.0}
    if not (INDIA["lat_min"] <= lat <= INDIA["lat_max"] and
            INDIA["lon_min"] <= lon <= INDIA["lon_max"]):
        raise ValueError(
            f"Geocoded address outside India (lat={lat:.5f}, lon={lon:.5f}, "
            f"confidence={confidence:.2f}) for input: '{address}'. "
            f"Check address spelling or use a more specific address."
        )

    if confidence < 0.3:
        log.warning("Low-confidence geocoding (%.2f) for '%s' → %s (%.5f, %.5f)",
                    confidence, address, label, lat, lon)

    log.info("Geocoded '%s' → (%.5f, %.5f) confidence=%.2f", address, lat, lon, confidence)
    return {"address": label, "latitude": lat, "longitude": lon, "confidence": confidence}

@tool
def elevation_point(latitude: float, longitude: float) -> dict:
    """
    Get elevation (metres above sea level) for a GPS coordinate.

    Endpoint: POST /elevation/point
    Args:
        latitude, longitude: GPS coordinate.
    Returns:
        { elevation, latitude, longitude }
    """
    resp = requests.post(
        f"{ORS_BASE}/elevation/point",
        headers=_ors_headers(),
        json={"format_in": "geojson",
              "geometry": {"type": "Point", "coordinates": [longitude, latitude]}},
        timeout=10,
    )
    resp.raise_for_status()
    data   = resp.json()
    coords = data.get("geometry", {}).get("coordinates", [])
    return {
        "elevation": coords[2] if len(coords) >= 3 else 0.0,
        "latitude":  latitude,
        "longitude": longitude,
    }


@tool
def optimize_route(
    stops: List[dict],
    depot_lon: float,
    depot_lat: float,
    max_vehicles: int   = 5,
    vehicle_capacity: int = 100,
    use_pd_pairs: bool  = True,
) -> dict:
    """
    Send geocoded stops to ORS /optimization (VROOM).

    Supports two modes:
      use_pd_pairs=True  → VROOM `shipments` — pickup ALWAYS precedes its delivery.
      use_pd_pairs=False → VROOM `jobs`       — pickup-only (no PD pairing).

    Request body format (matching ORS VROOM API spec):
      {
        "jobs": [  # or "shipments" for PD pairs
          {
            "id": int,
            "service": int,           # service time in seconds (default: 300)
            "location": [lon, lat],
            "skills": [int],          # optional skill requirements
            "time_windows": [[start_s, end_s]]  # optional
          }
        ],
        "vehicles": [
          {
            "id": int,
            "profile": str,           # e.g., "driving-hgv"
            "start": [lon, lat],
            "end": [lon, lat],
            "capacity": [int],        # array of capacity dimensions
            "skills": [int],          # optional skill requirements
            "time_window": [start_s, end_s]  # vehicle availability window
          }
        ]
      }

    Response format from ORS /optimization:
      {
        "code": 0,
        "summary": {
          "cost": int,                # total route cost
          "routes": int,              # number of routes used
          "unassigned": int,          # count of unassigned jobs
          "delivery": [int],          # total delivery amounts per dimension
          "amount": [int],            # total amount delivered
          "pickup": [int],            # total pickup amounts per dimension
          "setup": int,               # total setup time
          "service": int,             # total service time
          "duration": int,            # total duration including service/waiting
          "waiting_time": int,        # total waiting time
          "priority": int,            # priority score
          "violations": [],           # constraint violations
          "computing_times": {        # performance metrics
            "loading": int,           # loading time in ms
            "solving": int,           # solving time in ms
            "routing": int            # routing time in ms
          }
        },
        "unassigned": [
          { "id": int, "location": [lon,lat], "type": "job|pickup|delivery" }
        ],
        "routes": [
          {
            "vehicle": int,
            "cost": int,
            "delivery": [int],
            "amount": [int],
            "pickup": [int],
            "setup": int,
            "service": int,
            "duration": int,
            "waiting_time": int,
            "priority": int,
            "steps": [
              {
                "type": "start|job|pickup|delivery|end",
                "location": [lon, lat],
                "setup": int,
                "service": int,
                "waiting_time": int,
                "load": [int],          # remaining load after this step
                "arrival": int,         # arrival time in seconds since midnight
                "duration": int,        # cumulative duration to this point
                "violations": [],
                "id": int,              # job/pickup/delivery ID (if applicable)
                "job": int              # alternative ID field
              }
            ],
            "violations": []
          }
        ]
      }

    Args:
        stops: List of geocoded stop dicts (stop_index, store_name, pickup/delivery lat/lon, etc.)
        depot_lon / depot_lat: Depot start/end location.
        max_vehicles: Fleet size ceiling (VROOM decides actual usage).
        vehicle_capacity: Capacity per vehicle in units.
        use_pd_pairs: Enforce pickup-before-delivery ordering via VROOM shipments.

    Returns:
        {
          "summary": {
            # Full VROOM summary with all fields
            "cost": int,
            "routes_used": int,
            "unassigned_count": int,
            "total_delivery": [int],
            "total_amount": [int],
            "total_pickup": [int],
            "setup_time_sec": int,
            "service_time_sec": int,
            "total_duration_sec": int,
            "total_waiting_time_sec": int,
            "priority": int,
            "violations": [],
            "computing_times": { "loading_ms": int, "solving_ms": int, "routing_ms": int }
          },
          "routes": list,       # Per-vehicle routes with full step details
          "unassigned": list,   # Stops VROOM couldn't assign (with reasons if available)
          "ordered_stops": list # Flat optimised stop list with sequence, ETA, vehicle assignment
        }
    """
    if not stops:
        raise ValueError("No stops provided to optimize_route")

    TOLERANCE_SEC = 3600   # ±60-minute time-window tolerance
    vehicles = []
    for i in range(max_vehicles):
        
        start_offset = i * 7200  # 2 hours per vehicle
        vehicle_start = 28800 + start_offset  # 08:00 + offset
        vehicle_end = 79200  # All vehicles end at 22:00

        vehicle = {
            "id":       i + 1,
            "profile":  "driving-hgv",            # HGV/truck profile
            "start":    [depot_lon, depot_lat],   # Depot start location
            "end":      [depot_lon, depot_lat],   # Depot end location (circular route)
            "capacity": [vehicle_capacity],       # Capacity per dimension
            # Optional: skills can be added for constraint-based routing
            # "skills": [1, 14],                  # Skills this vehicle has
            "time_window": [vehicle_start, vehicle_end],
            "priority": 10 - i,
        }
        vehicles.append(vehicle)

    if use_pd_pairs:
        # Use shipments mode: pickup ALWAYS precedes its delivery
        shipments = []
        for s in stops:
            idx         = s["stop_index"]
            pickup_id   = idx * 10 + 1
            delivery_id = idx * 10 + 2

            pickup_leg = {
                "id":          pickup_id,
                "location":    [s["pickup_longitude"], s["pickup_latitude"]],
                "service":     300,  # 5 minutes service time
                "description": f"{s['store_name']} Pickup",
            }
            pu_str = s.get("expected_pickup_time")
            if pu_str:
                pu_sec = _time_str_to_seconds(pu_str)
                if pu_sec is not None:
                    pickup_leg["time_windows"] = [
                        [max(0, pu_sec - TOLERANCE_SEC), pu_sec + TOLERANCE_SEC]
                    ]

            # Build delivery leg
            delivery_leg = {
                "id":          delivery_id,
                "location":    [s["delivery_longitude"], s["delivery_latitude"]],
                "service":     300,  # 5 minutes service time
                "description": f"{s['store_name']} Delivery",
            }
            dl_str = s.get("expected_delivery_time")
            if dl_str:
                dl_sec = _time_str_to_seconds(dl_str)
                if dl_sec is not None:
                    delivery_leg["time_windows"] = [
                        [max(0, dl_sec - TOLERANCE_SEC), dl_sec + TOLERANCE_SEC]
                    ]

            shipment = {
                "pickup":   pickup_leg,
                "delivery": delivery_leg,
                "amount":   [1]  # 1 unit per shipment
            }
            # Priority based on time window presence — stops with time windows get higher priority
            if pu_str or dl_str:
                shipment["priority"] = 100  # High priority for time-sensitive stops
            shipments.append(shipment)

        payload = {"shipments": shipments, "vehicles": vehicles}

    else:
        # Format matches: openrouterservice_examples/optimization/body_object.json
        jobs = []
        for s in stops:
            idx = s["stop_index"]
            job = {
                "id":       idx * 10 + 1,
                "service":  300,  # 5 minutes service time
                "delivery": [1],  # Delivery amount (ORS VROOM format)
                "location": [s["pickup_longitude"], s["pickup_latitude"]],
                # Optional: skills can be added for constraint-based routing
                # "skills": [1],
            }
            # Add time window if specified
            pu_str = s.get("expected_pickup_time")
            if pu_str:
                pu_sec = _time_str_to_seconds(pu_str)
                if pu_sec is not None:
                    job["time_windows"] = [[max(0, pu_sec - TOLERANCE_SEC), pu_sec + TOLERANCE_SEC]]
                    job["priority"] = 100  # High priority for time-sensitive stops

            jobs.append(job)

        payload = {"jobs": jobs, "vehicles": vehicles}

    resp = requests.post(
        f"{ORS_BASE}/optimization",
        headers=_ors_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise ValueError(f"ORS /optimization error: {data['error']}")
    if not data.get("routes"):
        raise ValueError("ORS /optimization returned no routes — check vehicle/job config")

    job_lookup: dict = {}
    for s in stops:
        idx = s["stop_index"]
        job_lookup[idx * 10 + 1] = (s, "pickup")
        job_lookup[idx * 10 + 2] = (s, "delivery")

    ordered_stops: list = []
    seq = 1
    for route in data["routes"]:
        for step in route["steps"]:
            if step["type"] not in ("pickup", "delivery", "job"):
                continue

            job_id = int(step.get("id", step.get("job", 0)))
            entry  = job_lookup.get(job_id)
            if not entry:
                log.warning("Job %s not found in job_lookup", job_id)
                continue

            s, stop_type = entry
            is_delivery   = stop_type == "delivery"
            ordered_stops.append({
                "job_id":                   job_id,
                "store_id":                 s.get("store_id", ""),
                "store_name":               s["store_name"],
                "pickup_address":           s["pickup_address"],
                "delivery_address":         s.get("delivery_address", ""),
                "address":                  s["delivery_address"] if is_delivery else s["pickup_address"],
                "latitude":                 s["delivery_latitude"]  if is_delivery else s["pickup_latitude"],
                "longitude":                s["delivery_longitude"] if is_delivery else s["pickup_longitude"],
                "stop_type":                stop_type,
                "vehicle_id":               route["vehicle"],
                "arrival_time_seconds":     step.get("arrival", 0),
                "service_duration_seconds": step.get("service", 300),
                "waiting_time_seconds":     step.get("waiting_time", 0),
                "temperature_control":      s.get("temperature_control", False),
                "original_sequence":        s.get("original_sequence", s["stop_index"]),
                "optimized_sequence":       seq,
            })
            seq += 1

    summary = data["summary"]


    detailed_summary = {
        "cost":         summary.get("cost", 0),
        "routes":       summary.get("routes", 0),
        "unassigned":   summary.get("unassigned", 0),
        "delivery":     summary.get("delivery", [0]),
        "amount":       summary.get("amount", [0]),
        "pickup":       summary.get("pickup", [0]),
        "setup":        summary.get("setup", 0),
        "service":      summary.get("service", 0),
        "duration":     summary.get("duration", 0),
        "waiting_time": summary.get("waiting_time", 0),
        "priority":     summary.get("priority", 0),
        "violations":   summary.get("violations", []),
        "computing_times": summary.get("computing_times", {}),

        "routes_used":            summary.get("routes", 0),
        "unassigned_count":       summary.get("unassigned", 0),
        "total_delivery":         summary.get("delivery", [0]),
        "total_amount":           summary.get("amount", [0]),
        "total_pickup":           summary.get("pickup", [0]),
        "setup_time_sec":         summary.get("setup", 0),
        "service_time_sec":       summary.get("service", 0),
        "total_duration_sec":     summary.get("duration", 0),
        "total_waiting_time_sec": summary.get("waiting_time", 0),
    }

    return {
        "summary":       detailed_summary,           # Summary with original + detailed fields
        "routes":        data["routes"],             # per-vehicle routes with steps
        "unassigned":    data.get("unassigned", []), # stops VROOM couldn't assign
        "ordered_stops": ordered_stops,              # flat optimised stop list
    }


@tool
def distance_matrix(locations: List[dict], profile: str = "driving-car") -> dict:
    """
    Compute duration and distance between locations in sequence.

    Used for before/after POV comparison:
      • unoptimised: original email stop order
      • optimised:   VROOM-ordered stops

    Endpoint: POST /v2/matrix/{profile}

    Request body sent:
      {
        "locations": [[lon, lat], [lon, lat], ...],
        "metrics":   ["duration", "distance"]
      }

    Response shape from /v2/matrix/{profile}:
      {
        "durations": [[0, 291.6, ...], [839.4, 0, ...], ...],   # seconds NxN
        "distances": [[0, 1367.6, ...], [894.2, 0, ...], ...],  # metres  NxN
        "metadata":  { "attribution": "...", "service": "matrix", ... }
      }

    Sequential legs are extracted as matrix[i][i+1] for i in 0..N-2.

    Args:
        locations: List of { longitude, latitude, store_name? } dicts.
        profile: ORS routing profile (default: 'driving-car').

    Returns:
        {
          legs: [ { from, to, distance_km, duration_min } ],
          total_distance_km:  float,
          total_duration_min: float,
        }
    """
    if len(locations) < 2:
        return {"legs": [], "total_distance_km": 0.0, "total_duration_min": 0.0}

    loc_coords = [[loc["longitude"], loc["latitude"]] for loc in locations]

    resp = requests.post(
        f"{ORS_BASE}/v2/matrix/{profile}",
        headers=_ors_headers(),
        json={"locations": loc_coords, "metrics": ["duration", "distance"]},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    durations = data.get("durations", [])
    distances = data.get("distances", [])

    if not durations or not distances:
        raise ValueError(f"Matrix API returned empty data. Response: {data}")

    legs = []
    for i in range(len(locations) - 1):
        dist = distances[i][i + 1] if i < len(distances) and (i + 1) < len(distances[i]) else None
        dur  = durations[i][i + 1] if i < len(durations)  and (i + 1) < len(durations[i])  else None

        if dist is None or dur is None:
            log.warning("Matrix[%d][%d] is None — skipping leg", i, i + 1)
            continue

        legs.append({
            "from":         locations[i].get("store_name", f"Stop {i + 1}"),
            "to":           locations[i + 1].get("store_name", f"Stop {i + 2}"),
            "distance_km":  round(dist / 1000, 2),
            "duration_min": round(dur  / 60,   2),
        })

    if not legs:
        return {"legs": [], "total_distance_km": 0.0, "total_duration_min": 0.0}

    return {
        "legs":               legs,
        "total_distance_km":  round(sum(leg["distance_km"]  for leg in legs), 2),
        "total_duration_min": round(sum(leg["duration_min"] for leg in legs), 2),
    }