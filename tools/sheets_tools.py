from __future__ import annotations
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import gspread
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from langchain_core.tools import tool
from email.utils import parseaddr

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "credentials/token.json")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")

# Tab names
TAB_EMAIL_LOG = "email_log"
TAB_PARSED_STOPS = "parsed_stops"
TAB_GEOCODED = "geocoded"
TAB_ROUTE = "route_output"
TAB_ERROR = "error_log"
TAB_REJECTION = "rejection_log"


# ── Core Helpers ─────────────────────────────────────────

def get_client():
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return gspread.authorize(creds)


def open_sheet(tab_name):
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        return sh.worksheet(tab_name)
    except:
        return sh.add_worksheet(title=tab_name, rows=1000, cols=30)


def ensure_headers(ws, headers):
    """Ensure headers exist in row 1. If headers exist but differ, update them."""
    data = ws.get_all_values()
    if not data:
        ws.append_row(headers)
    else:
        current_headers = ws.row_values(1)
        if current_headers != headers:
            ws.update([headers], 'A1')


# ── Email Log ────────────────────────────────────────────

@tool
def save_email_log(collection_request_id: str, sender_email: str, sender_company: str, raw_body: str):
    """Save email log to Google Sheets.

    Args:
        collection_request_id: Collection request ID
        sender_email: Sender email address
        sender_company: Sender company name
        raw_body: Raw email body content

    Returns:
        str: Confirmation message
    """
    ws = open_sheet(TAB_EMAIL_LOG)

    headers = [
        "request_id","sender_name", "sender_email", "sender_company", "email_body", "received_at"
    ]
    ensure_headers(ws, headers)

    sender_name, sender_email = parseaddr(sender_email)

    if not sender_name:
        sender_name = sender_email.split("@")[0]
    received_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")

    ws.append_row([
        collection_request_id,
        sender_name,
        sender_email,
        sender_company,
        raw_body,
        received_at
    ])

    return "email saved"


# ── Parsed Stops ─────────────────────────────────────────

@tool
def save_parsed_stops(collection_request_id: str, stops: list):
    """Save parsed stops to Google Sheets.

    Args:
        collection_request_id: Collection request ID
        stops: List of stop dictionaries with store info and addresses

    Returns:
        str: Confirmation message with count of stops saved
    """
    ws = open_sheet(TAB_PARSED_STOPS)

    headers = [
        "request_id",
        "stop_number",
        "store_id",
        "store_name",
        "pickup_address",
        "delivery_address",
        "expected_pickup_time",
        "expected_delivery_time",
        "temperature_control",
        "collection_date"
    ]
    ensure_headers(ws, headers)

    rows = []
    for i, stop in enumerate(stops, 1):
        rows.append([
            collection_request_id,
            i,
            stop.get("store_id", ""),
            stop.get("store_name", ""),
            stop.get("pickup_address", ""),
            stop.get("delivery_address", ""),
            stop.get("expected_pickup_time", ""),
            stop.get("expected_delivery_time", ""),
            "YES" if stop.get("temperature_control") else "NO",
            stop.get("collection_date", "")
        ])
    ws.append_rows(rows)


# ── Geocoded Data ────────────────────────────────────────

@tool
def save_geocoded(collection_request_id: str, geo_data: list):
    """Save geocoded coordinates to Google Sheets.

    Args:
        collection_request_id: Collection request ID
        geo_data: List of geocoded location data with addresses and coordinates

    Returns:
        str: Confirmation message
    """
    ws = open_sheet(TAB_GEOCODED)

    headers = [
        "request_id",
        "stop_number",
        "store_name",
        "address_type",
        "raw_address",
        "latitude",
        "longitude",
        "confidence",
        "elevation_m"
    ]
    ensure_headers(ws, headers)

    for g in geo_data:
        ws.append_row([
            collection_request_id,
            g.get("stop_number", ""),
            g.get("store_name", ""),
            g.get("address_type", ""),
            g.get("raw_address", ""),
            g.get("latitude", ""),
            g.get("longitude", ""),
            g.get("confidence", ""),
            g.get("elevation_m", "")
        ])

    return "geocoded saved"


# ── Route Output ─────────────────────────────────────────

@tool
def save_route(collection_request_id: str, route: dict):
    """Save optimized route output to Google Sheets.

    Args:
        collection_request_id: Collection request ID
        route: Route result dictionary with ordered stops, duration, and distance

    Returns:
        str: Confirmation message
    """
    ws = open_sheet(TAB_ROUTE)

    headers = [
        "request_id",
        "optimized_sequence",
        "original_sequence",
        "vehicle_id",
        "store_id",
        "store_name",
        "pickup_address",
        "pickup_latitude",
        "pickup_longitude",
        "delivery_address",
        "eta",
        "service_duration_min",
        "total_distance_km",
        "total_duration_min",
        "temperature_control"
    ]
    ensure_headers(ws, headers)

    total_km = round(route.get("total_distance_meters", 0) / 1000, 2)
    total_min = round(route.get("total_duration_seconds", 0) / 60, 1)

    for stop in route.get("ordered_stops", []):
        sec = stop.get("arrival_time_seconds", 0)
        h, r = divmod(sec, 3600)
        eta = f"{h:02d}:{r//60:02d}"

        ws.append_row([
            collection_request_id,
            stop.get("optimized_sequence", ""),
            stop.get("original_sequence", ""),
            stop.get("vehicle_id", 1),
            stop.get("store_id", ""),
            stop.get("store_name", ""),
            stop.get("pickup_address", ""),
            stop.get("latitude", ""),
            stop.get("longitude", ""),
            stop.get("delivery_address", ""),
            eta,
            round(stop.get("service_duration_seconds", 300)/60, 1),
            total_km,
            total_min,
            "YES" if stop.get("temperature_control") else "NO"
        ])

    return "route saved"


# ── Error Log ────────────────────────────────────────────

@tool
def save_error(collection_request_id: str, thread_id: str, email: str, code: str):
    """Save error log to Google Sheets.

    Args:
        collection_request_id: Collection request ID
        thread_id: Gmail thread ID
        email: Sender email address
        code: Error code string

    Returns:
        str: Confirmation message
    """
    ws = open_sheet(TAB_ERROR)

    headers = [
        "request_id", "thread_id", "sender_email", "error_code", "failed_at"
    ]
    ensure_headers(ws, headers)

    ws.append_row([
        collection_request_id,
        thread_id,
        email,
        code,
        datetime.now(timezone.utc).isoformat()
    ])

    return "error logged"


# ── Duplicate Check ─────────────────────────────────────

def check_duplicate(collection_request_id: str) -> bool:
    """Check if a collection request ID already exists in the email log.

    Args:
        collection_request_id: Collection request ID to check

    Returns:
        bool: True if duplicate found, False otherwise
    """
    ws = open_sheet(TAB_EMAIL_LOG)
    data = ws.get_all_values()

    if not data:
        return False

    for row in data[1:]:
        if row and row[0] == collection_request_id:
            return True

    return False


# ── Rejection Log ────────────────────────────────────────

@tool
def save_rejection(collection_request_id: str, thread_id: str, email: str, rejected_stops: list):
    """Save rejected stops to Google Sheets rejection_log tab.

    Args:
        collection_request_id: Collection request ID
        thread_id: Gmail thread ID
        email: Sender email address
        rejected_stops: List of rejected stop dicts with store info and rejection reason

    Returns:
        str: Confirmation message
    """
    ws = open_sheet(TAB_REJECTION)

    headers = [
        "request_id",
        "thread_id",
        "sender_email",
        "store_id",
        "store_name",
        "address",
        "stop_type",
        "rejection_reason",
        "rejected_at"
    ]
    ensure_headers(ws, headers)

    for stop in rejected_stops:
        ws.append_row([
            collection_request_id,
            thread_id,
            email,
            stop.get("store_id", ""),
            stop.get("store_name", ""),
            stop.get("address", ""),
            stop.get("stop_type", ""),
            stop.get("reason", ""),
            datetime.now(timezone.utc).isoformat()
        ])

    return "rejection logged"