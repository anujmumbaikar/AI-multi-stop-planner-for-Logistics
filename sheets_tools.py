"""
tools/sheets_tools.py
Google Sheets read/write via gspread.

n8n equivalents:
  save_stops_to_sheet()     →  Record Email Content node
  save_gps_to_sheet()       →  Save Pickup GPS / Save Delivery GPS nodes
  save_route_to_sheet()     →  Save Sequence & Duration node
  load_shipment_info()      →  Collect Shipment Information node (step 4)
"""

from __future__ import annotations
import os
from datetime import datetime
from typing import List, Optional

import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from langchain_core.tools import tool

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "credentials/token.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")


TAB_RAW_EMAIL = "RawEmail"
TAB_GPS = "GPS_Coordinates"
TAB_ROUTE = "OptimizedRoute"


def _get_gspread_client() -> gspread.Client:
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return gspread.authorize(creds)


def _open_sheet(tab_name: str) -> gspread.Worksheet:
    gc = _get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=1000, cols=20)

@tool
def save_stops_to_sheet(stops: List[dict], thread_id: str, sender_email: str) -> str:
    """
    Save the raw parsed email stops to the RawEmail sheet.
    Maps to n8n 'Record Email Content' node.

    Args:
        stops:        List of CollectionStop dicts (pre-geocoding).
        thread_id:    Gmail thread ID.
        sender_email: Sender's email address.

    Returns:
        Confirmation string.
    """
    ws = _open_sheet(TAB_RAW_EMAIL)

    if ws.row_count == 0 or ws.acell("A1").value is None:
        ws.append_row([
            "timestamp", "thread_id", "sender_email",
            "stop_index", "store_name", "address",
            "time_window", "temperature_control",
        ])

    ts = datetime.utcnow().isoformat()
    for s in stops:
        ws.append_row([
            ts,
            thread_id,
            sender_email,
            s.get("stop_index", ""),
            s.get("store_name", ""),
            s.get("address", ""),
            s.get("time_window", ""),
            str(s.get("temperature_control", "")),
        ])

    return f"Saved {len(stops)} stops to sheet '{TAB_RAW_EMAIL}'"


@tool
def save_gps_to_sheet(geocoded_stops: List[dict]) -> str:
    """
    Save geocoded GPS coordinates to the GPS_Coordinates sheet.
    Maps to n8n 'Save Pickup GPS' and 'Save Delivery GPS' nodes.

    Args:
        geocoded_stops: List of CollectionStop dicts with lat/lon filled in.

    Returns:
        Confirmation string.
    """
    ws = _open_sheet(TAB_GPS)

    if ws.row_count == 0 or ws.acell("A1").value is None:
        ws.append_row([
            "stop_index", "store_name", "address",
            "latitude", "longitude", "geocode_confidence",
        ])

    for s in geocoded_stops:
        ws.append_row([
            s.get("stop_index", ""),
            s.get("store_name", ""),
            s.get("address", ""),
            s.get("latitude", ""),
            s.get("longitude", ""),
            s.get("confidence", ""),
        ])

    return f"Saved {len(geocoded_stops)} GPS records to sheet '{TAB_GPS}'"


@tool
def save_route_to_sheet(route_result: dict) -> str:
    """
    Save the final optimised route sequence + duration to the OptimizedRoute sheet.
    Maps to n8n 'Save Sequence & Duration' node.

    Args:
        route_result: RouteResult dict with ordered_stops, total_duration_seconds,
                      total_distance_meters.

    Returns:
        Confirmation string.
    """
    ws = _open_sheet(TAB_ROUTE)

    if ws.row_count == 0 or ws.acell("A1").value is None:
        ws.append_row([
            "sequence", "job_id", "store_name", "address",
            "latitude", "longitude",
            "arrival_time", "service_duration_sec",
            "total_duration_sec", "total_distance_m",
        ])

    total_dur = route_result.get("total_duration_seconds", 0)
    total_dist = route_result.get("total_distance_meters", 0)

    for i, s in enumerate(route_result.get("ordered_stops", [])):
        arr_sec = s.get("arrival_time_seconds", 0)
        h, rem = divmod(arr_sec, 3600)
        m = rem // 60
        arrival_str = f"{h:02d}:{m:02d}"

        ws.append_row([
            i + 1,
            s.get("job_id", ""),
            s.get("store_name", ""),
            s.get("address", ""),
            s.get("latitude", ""),
            s.get("longitude", ""),
            arrival_str,
            s.get("service_duration_seconds", 300),
            total_dur,
            total_dist,
        ])

    return f"Route saved to sheet '{TAB_ROUTE}' with {len(route_result.get('ordered_stops', []))} stops"


@tool
def load_shipment_info(thread_id: str) -> dict:
    """
    Load the full route information from sheets for a given thread.
    Maps to n8n 'Collect Shipment Information' node (step 4 input).

    Args:
        thread_id: Gmail thread ID used as correlation key.

    Returns:
        Dict with route stops and metadata for the AI reply agent.
    """
    ws_route = _open_sheet(TAB_ROUTE)
    ws_raw = _open_sheet(TAB_RAW_EMAIL)

    all_route = ws_route.get_all_records()
    all_raw = ws_raw.get_all_records()

    # Filter by matching thread data (route sheet doesn't store thread_id,
    # so we return all records from the most recent batch)
    # In production, add thread_id column to sheets and filter by it.
    return {
        "route_stops": all_route[-20:],  # last 20 rows as proxy
        "raw_email_records": [r for r in all_raw if r.get("thread_id") == thread_id],
    }
