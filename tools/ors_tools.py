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

DEFAULT_WINDOW_HALF_SEC = 1800
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
 
 
def _time_window(time_str: Optional[str]) -> Optional[List[int]]:
    """
    Build a VROOM [[start, end]] time window from a single 'HH:MM' string.
    Returns None if no time string is given — VROOM then has no time constraint.
    """
    if not time_str:
        return None
    sec = _time_str_to_seconds(time_str)
    if sec is None:
        return None
    return [[max(0, sec - DEFAULT_WINDOW_HALF_SEC), sec + DEFAULT_WINDOW_HALF_SEC]]



@tool
def geocode_address(address: str) -> dict:
    """
    Convert a human-readable address to GPS coordinates.

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

# print(geocode_address("1600 Amphitheatre Parkway, Mountain View, CA"))
# output:
#  {                                                                                      
#     "address": "1600 Amphitheatre Parkway, Mountain View, CA, USA",                      
#     "latitude": 37.422288,                                                               
#     "longitude": -122.085652,                                                            
#     "confidence": 1                                                                      
#   }

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
# print(elevation_point(37.422288, -122.085652))
# output :{'elevation': 7, 'latitude': 37.422288, 'longitude': -122.085652}

@tool
def optimize_route(
    stops: List[dict],
    depot_lon: float,
    depot_lat: float,
    max_vehicles: int = 5,
    vehicle_capacity: int = 100,
    use_pd_pairs: bool = True,
    vehicle_time_window: Optional[List[int]] = None,
) -> dict:
    """
    Send geocoded stops to ORS /optimization (VROOM).
 
    What VROOM handles 
      - Pickup-before-delivery ordering          → use shipments mode
      - Capacity enforcement per vehicle         → capacity + amount fields
      - Time-window feasibility                  → stops that can't be served go to unassigned[]
      - violations[] on each step               → delay / lead_time / precedence causes
      - Multi-vehicle dispatch optimisation      → pass max_vehicles, VROOM picks how many to use
 
    Modes:
      use_pd_pairs=True  → VROOM `shipments` — pickup ALWAYS precedes its delivery.
      use_pd_pairs=False → VROOM `jobs`       — single-location delivery tasks only.
 
    Each stop dict must contain:
      stop_index, store_name, pickup_address,
      pickup_latitude, pickup_longitude
      (and for shipments: delivery_address, delivery_latitude, delivery_longitude)
 
    Optional stop fields:
      expected_pickup_time   ('HH:MM') → time window on the pickup leg
      expected_delivery_time ('HH:MM') → time window on the delivery leg
      priority               (int)     → passed through to VROOM; higher = preferred
      temperature_control    (bool)    → preserved in ordered_stops for downstream use
 
    Args:
        stops:                Geocoded stop dicts.
        depot_lon/depot_lat:  Depot coordinates (start + end for all vehicles).
        max_vehicles:         Fleet ceiling — VROOM decides how many to actually use.
        vehicle_capacity:     Capacity per vehicle (single dimension, in units).
        use_pd_pairs:         True → shipments, False → delivery jobs.
        vehicle_time_window:  [start_sec, end_sec] for all vehicles.
                              Defaults to [28800, 79200] (08:00–22:00).
 
    Returns:
        {
          "summary":          dict,   # VROOM summary as-is (cost, routes, unassigned, ...)
          "routes":           list,   # per-vehicle routes with full step details
          "unassigned_stops": list,   # stops VROOM couldn't assign (mapped back to stop info)
          "ordered_stops":    list,   # flat optimised stop list with ETA + vehicle assignment
        }
    """
    if not stops:
        raise ValueError("No stops provided to optimize_route")
 
    tw = vehicle_time_window or [28800, 79200]  # default 08:00–22:00
 
    vehicles = [
        {
            "id":          i + 1,
            "profile":     "driving-hgv",
            "start":       [depot_lon, depot_lat],
            "end":         [depot_lon, depot_lat],
            "capacity":    [vehicle_capacity],
            "time_window": tw,
        }
        for i in range(max_vehicles)
    ]
 
    job_lookup: dict = {}
 
    if use_pd_pairs:
        # Shipments mode: VROOM guarantees pickup precedes delivery
        shipments = []
        for s in stops:
            idx         = s["stop_index"]
            pickup_id   = idx * 2 + 1
            delivery_id = idx * 2 + 2
            job_lookup[pickup_id]   = (s, "pickup")
            job_lookup[delivery_id] = (s, "delivery")
 
            pickup_leg = {
                "id":       pickup_id,
                "location": [s["pickup_longitude"], s["pickup_latitude"]],
                "service":  300,  # 5 min service time
            }
            pu_tw = _time_window(s.get("expected_pickup_time"))
            if pu_tw:
                pickup_leg["time_windows"] = pu_tw
 
            delivery_leg = {
                "id":       delivery_id,
                "location": [s["delivery_longitude"], s["delivery_latitude"]],
                "service":  300,
            }
            dl_tw = _time_window(s.get("expected_delivery_time"))
            if dl_tw:
                delivery_leg["time_windows"] = dl_tw
 
            shipment: dict = {
                "pickup":   pickup_leg,
                "delivery": delivery_leg,
                "amount":   [1],  # 1 unit per shipment; VROOM tracks load automatically
            }
            if s.get("priority"):
                shipment["priority"] = int(s["priority"])
 
            shipments.append(shipment)
 
        payload = {"shipments": shipments, "vehicles": vehicles}
 
    else:
        # ── Jobs mode: simple single-location delivery tasks ──────────────────
        jobs = []
        for s in stops:
            idx    = s["stop_index"]
            job_id = idx * 2 + 1
            job_lookup[job_id] = (s, "job")
 
            job: dict = {
                "id":       job_id,
                "service":  300,
                "delivery": [1],  # reduces vehicle load by 1 (pre-loaded at depot)
                "location": [s["pickup_longitude"], s["pickup_latitude"]],
            }
            pu_tw = _time_window(s.get("expected_pickup_time"))
            if pu_tw:
                job["time_windows"] = pu_tw
 
            if s.get("priority"):
                job["priority"] = int(s["priority"])
 
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
 

    ordered_stops: list = []
    seq = 1
    for route in data.get("routes", []):
        for step in route["steps"]:
            if step["type"] not in ("pickup", "delivery", "job"):
                continue
            job_id = int(step.get("id", step.get("job", 0)))
            entry  = job_lookup.get(job_id)
            if not entry:
                log.warning("Job %s not found in job_lookup", job_id)
                continue
            s, stop_type = entry
            is_delivery  = stop_type == "delivery"
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
                "violations":               step.get("violations", []),
                "temperature_control":      s.get("temperature_control", False),
                "original_sequence":        s.get("original_sequence", s["stop_index"]),
                "optimized_sequence":       seq,
            })
            seq += 1
 
    # ── Map unassigned VROOM items back to stop info
    # VROOM's unassigned[] gives {id, location, type} — no reason field.
    # Violation causes (delay / lead_time / capacity) live in route step violations[],
    unassigned_stops: list = []
    seen_indices: set = set()

    # rejection reason
    rejection_reasons: dict[int, str] = {}
    for route in data.get("routes", []):
        for step in route.get("steps", []):
            for v in step.get("violations", []):
                job_id = int(v.get("id", 0))
                if job_id:
                    violation_type = v.get("violation", "unknown")
                    rejection_reasons[job_id] = violation_type

    for item in data.get("unassigned", []):
        job_id = int(item.get("id", 0))
        entry  = job_lookup.get(job_id)
        if not entry:
            continue
        s, stop_type = entry
        idx = s["stop_index"]
        if idx in seen_indices:
            continue  # deduplicate: one entry per shipment (pickup+delivery pair)
        seen_indices.add(idx)

        # Get reason from violations map, or infer from item type
        reason = rejection_reasons.get(job_id)
        if not reason:
            reason = "CAPACITY_EXCEEDED" if item.get("type") == "delivery" else "TIME_WINDOW_CONFLICT"

        unassigned_stops.append({
            "stop_index": idx,
            "store_id":   s.get("store_id", ""),
            "store_name": s["store_name"],
            "address":    s.get("pickup_address", ""),
            "stop_type":  "shipment",
            "reason":     reason,
        })
 
    return {
        "summary":          data["summary"],           # raw VROOM summary, no duplication
        "routes":           data.get("routes", []),    # per-vehicle routes with step detail
        "unassigned_stops": unassigned_stops,          # stops VROOM couldn't schedule
        "ordered_stops":    ordered_stops,             # flat optimised stop sequence
    }
 

@tool
def distance_matrix(locations: List[dict], profile: str = "driving-car") -> dict:
    """
    Compute duration and distance between locations in sequence.

    Endpoint: POST /v2/matrix/{profile}

    Sequential legs are extracted as matrix[i][i+1] for i in 0.....N-2.

    Args:
        locations: List of { longitude, latitude, store_name } dicts.
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

# print(distance_matrix(
#     [
#         {'store_name': 'Store A', 'address': '1600 Amphitheatre Parkway, Mountain View, CA', 'latitude': 37.422288, 'longitude': -122.085652},
#         {'store_name': 'Store B', 'address': '1 Hacker Way, Menlo Park, CA', 'latitude': 37.4847, 'longitude': -122.1477},
#         {'store_name': 'Store C', 'address': '2300 Traverwood Dr, Ann Arbor, MI', 'latitude': 42.3037, 'longitude': -83.7108},
#     ]
# ))
# output:
# {'legs': [{'from': 'Store A', 'to': 'Store B', 'distance_km': 11.37, 'duration_min': 15.75}, {'from': 'Store B', 'to': 'Store C', 'distance_km': 3838.34, 'duration_min': 3244.26}], 'total_distance_km': 3849.71, 'total_duration_min': 3260.01}
