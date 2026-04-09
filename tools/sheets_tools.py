"""
tools/sheets_tools.py
Google Sheets read/write via gspread.

Tabs:
  email_log      — one row per collection request
  parsed_stops   — one row per stop extracted from email
  geocoded_coords — pickup + delivery GPS per stop
  route_output   — one row per stop in the optimised route
  error_log      — one row per failed request
"""

from __future__ import annotations
import os
from datetime import datetime
from typing import List

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

TAB_EMAIL_LOG      = "email_log"
TAB_PARSED_STOPS   = "parsed_stops"
TAB_GEOCODED_COORDS = "geocoded_coords"
TAB_ROUTE_OUTPUT   = "route_output"
TAB_ERROR_LOG      = "error_log"


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
        return sh.add_worksheet(title=tab_name, rows=1000, cols=26)


# ── Email Log (one row per request) ──────────────────────────────────────────

@tool
def save_email_log(
    collection_request_id: str,
    sender_email: str,
    sender_company: str,
    raw_body: str,
) -> str:
    """
    Save email metadata to email_log tab.

    Args:
        collection_request_id: System-generated request ID.
        sender_email:          Sender's Gmail address.
        sender_company:        Retailer company name.
        raw_body:              Full raw email body text.
    """
    ws = _open_sheet(TAB_EMAIL_LOG)
    if not ws.get_all_values():
        ws.append_row([
            "collection_request_id", "sender_email",
            "sender_company", "raw_body", "timestamp",
        ])
    ws.append_row([
        collection_request_id,
        sender_email,
        sender_company,
        raw_body,
        datetime.utcnow().isoformat(),
    ])
    return f"Saved email log for {collection_request_id}"


def check_duplicate(collection_request_id: str) -> bool:
    """
    Check whether a collection_request_id already exists in email_log.
    Returns True if duplicate, False if new.
    """
    try:
        ws = _open_sheet(TAB_EMAIL_LOG)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return False
        existing_ids = {row[0] for row in rows[1:] if row}
        return collection_request_id in existing_ids
    except Exception:
        return False


# ── Parsed Stops (one row per stop extracted from email) ─────────────────────

@tool
def save_parsed_stops(collection_request_id: str, stops: list) -> str:
    """
    Save each stop parsed from the email to the parsed_stops tab.

    Columns: collection_request_id, stop_number, store_id, store_name,
             pickup_address, delivery_address, expected_pickup_time,
             expected_delivery_time, temperature_control, collection_date

    Args:
        collection_request_id: Links to email_log.
        stops:                 List of stop dicts from LLM parser.
    """
    ws = _open_sheet(TAB_PARSED_STOPS)
    if not ws.get_all_values():
        ws.append_row([
            "collection_request_id", "stop_number", "store_id", "store_name",
            "pickup_address", "delivery_address",
            "expected_pickup_time", "expected_delivery_time",
            "temperature_control", "collection_date",
        ])
    for i, stop in enumerate(stops, 1):
        ws.append_row([
            collection_request_id,
            i,
            stop.get("store_id", ""),
            stop.get("store_name", ""),
            stop.get("pickup_address", ""),
            stop.get("delivery_address", ""),
            stop.get("expected_pickup_time", ""),
            stop.get("expected_delivery_time", ""),
            "YES" if stop.get("temperature_control") else "No",
            stop.get("collection_date", ""),
        ])
    return f"Saved {len(stops)} parsed stops for {collection_request_id}"


# ── Geocoded Coordinates (pickup + delivery per stop) ─────────────────────────

@tool
def save_geocoded_coords(collection_request_id: str, geocoded_stops: list) -> str:
    """
    Save geocoding results for pickup and delivery addresses to geocoded_coords tab.

    Columns: collection_request_id, stop_number, store_name, address_type,
             raw_address, resolved_address, latitude, longitude, confidence

    Args:
        collection_request_id: Links to email_log.
        geocoded_stops:        List of dicts with geocoding results.
                               Each dict must have: stop_number, store_name,
                               address_type ('pickup'/'delivery'), raw_address,
                               resolved_address, latitude, longitude, confidence.
    """
    ws = _open_sheet(TAB_GEOCODED_COORDS)
    if not ws.get_all_values():
        ws.append_row([
            "collection_request_id", "stop_number", "store_name",
            "address_type", "raw_address", "resolved_address",
            "latitude", "longitude", "confidence",
        ])
    for entry in geocoded_stops:
        ws.append_row([
            collection_request_id,
            entry.get("stop_number", ""),
            entry.get("store_name", ""),
            entry.get("address_type", ""),
            entry.get("raw_address", ""),
            entry.get("resolved_address", ""),
            entry.get("latitude", ""),
            entry.get("longitude", ""),
            entry.get("confidence", ""),
        ])
    return f"Saved {len(geocoded_stops)} geocoded addresses for {collection_request_id}"


# ── Route Output (one row per stop in optimised order) ────────────────────────

@tool
def save_route_output(collection_request_id: str, route_result: dict) -> str:
    """
    Save the optimised route to route_output tab.

    One row per stop. Columns:
      collection_request_id, optimized_sequence, original_sequence,
      store_id, store_name, pickup_address, pickup_lat, pickup_lon,
      delivery_address, eta, service_min,
      total_distance_km, total_duration_min, temperature_control

    Args:
        collection_request_id: Links to email_log.
        route_result:          RouteResult dict with ordered_stops list.
    """
    ws = _open_sheet(TAB_ROUTE_OUTPUT)
    if not ws.get_all_values():
        ws.append_row([
            "collection_request_id",
            "optimized_sequence",
            "original_sequence",
            "store_id",
            "store_name",
            "pickup_address",
            "pickup_lat",
            "pickup_lon",
            "delivery_address",
            "eta",
            "service_min",
            "total_distance_km",
            "total_duration_min",
            "temperature_control",
        ])

    total_dist_km = round(route_result.get("total_distance_meters", 0) / 1000, 2)
    total_dur_min = round(route_result.get("total_duration_seconds", 0) / 60, 1)

    for stop in route_result.get("ordered_stops", []):
        arr_sec = stop.get("arrival_time_seconds", 0)
        h, rem = divmod(arr_sec, 3600)
        eta = f"{h:02d}:{rem // 60:02d}"
        svc_min = round(stop.get("service_duration_seconds", 300) / 60, 1)
        temp = "YES ⚠️" if stop.get("temperature_control") else "No"

        ws.append_row([
            collection_request_id,
            stop.get("optimized_sequence", ""),
            stop.get("original_sequence", ""),
            stop.get("store_id", ""),
            stop.get("store_name", ""),
            stop.get("address", ""),
            stop.get("latitude", ""),
            stop.get("longitude", ""),
            stop.get("delivery_address", ""),
            eta,
            svc_min,
            total_dist_km,
            total_dur_min,
            temp,
        ])

    stops_count = len(route_result.get("ordered_stops", []))
    return f"Saved {stops_count} stops to route_output for {collection_request_id}"


# ── Error Log ─────────────────────────────────────────────────────────────────

@tool
def save_error_log(
    collection_request_id: str,
    thread_id: str,
    sender_email: str,
    error_code: str,
) -> str:
    """
    Log a failed request to the error_log tab.

    Error codes: PARSE_FAILED, DUPLICATE_REQUEST, GEOCODE_FAILED,
                 ORS_OPTIMIZATION_FAILED, UNKNOWN_ERROR

    Args:
        collection_request_id: Request ID (may be empty if failure was early).
        thread_id:             Gmail thread ID.
        sender_email:          Sender's email.
        error_code:            One of the defined error codes.
    """
    ws = _open_sheet(TAB_ERROR_LOG)
    if not ws.get_all_values():
        ws.append_row([
            "collection_request_id", "thread_id",
            "sender_email", "error_code", "timestamp",
        ])
    ws.append_row([
        collection_request_id,
        thread_id,
        sender_email,
        error_code,
        datetime.utcnow().isoformat(),
    ])
    return f"Logged {error_code} for {collection_request_id or thread_id}"
