# Logistics Planner

AI-powered logistics route optimizer that turns inbound Gmail requests into a routed pickup schedule, stores an audit trail in Google Sheets, and sends a confirmation reply back to the sender.

## What It Does

The project ingests unread Gmail messages, extracts structured pickup data with GPT-4o, geocodes pickup and delivery locations with OpenRouteService, optimizes the pickup order for a truck route, compares original vs. optimized distance/time using ORS matrix calls, writes the results to Google Sheets, and sends an HTML reply to the original email thread.

## Workflow

The main workflow lives in [agent.ipynb](agent.ipynb). It is built as a LangGraph pipeline with these steps:

1. `gmail_trigger` - create a `REQ-YYYYMMDD-<6-char hash>` request id and check for duplicates.
2. `parser_agent` - use GPT-4o to extract sender info and pickup stops from the email body.
3. `save_email_logs_to_sheet` - save the raw email and parsed stops to Google Sheets.
4. `geocode_pickup_delivery_address` - geocode both pickup and delivery addresses and save the geocoded rows.
5. `route_optimization` - calculate original distance/time, run ORS VROOM optimization, then calculate optimized distance/time.
6. `ai_agent_reply` - generate the final HTML confirmation email with route table and savings summary.
7. `send_reply_to_gmail` - send the reply in the original Gmail thread.
8. `error_handler` - log failures to the error sheet.

### Route Optimization Deep Dive

The route optimization node now follows a three-step pattern:

- Measure the original email order with ORS `/matrix`.
- Optimize pickup order with ORS VROOM `/optimization`.
- Measure the optimized order again with ORS `/matrix`.

This means the README and the notebook both reflect the same design: real baseline distance/time, real optimized distance/time, and computed savings.

## Workflow in Action

### Step 1: Incoming Pickup Request Email

The pipeline starts by polling Gmail for unread emails matching the collection request query. A user sends a structured email with pickup stops:

![Incoming Pickup Schedule Email](screenshots/Screenshot%202026-04-12%20at%204.31.50%20AM.png)

The email contains stop details including store ID, pickup address, delivery address, expected times, and temperature control requirements.

### Step 2: Automated Route Optimization & Confirmation Reply

Once parsed and geocoded, the pipeline optimizes the route using ORS VROOM and sends back an HTML confirmation email with an interactive table showing the optimized sequence, ETAs, and route summary:

![Optimized Route Confirmation Email](screenshots/Screenshot%202026-04-12%20at%204.30.53%20AM.png)

The reply includes:
- Optimized stop sequence with ETAs
- Original vs. optimized distance/time savings
- Stop details (Store ID, Pickup/Delivery addresses, Cold Chain flags)

### Step 3: Audit Trail in Google Sheets

All request data, geocoding results, and optimized routes are logged to the `route_output` Google Sheets tab for auditing and analysis:

![Google Sheets Route Output](screenshots/Screenshot%202026-04-12%20at%204.35.43%20AM.png)

The sheet captures:
- Request ID and optimized sequence
- Store metadata (ID, name, addresses)
- GPS coordinates and ETAs
- Total distance and duration
- Temperature control flags

## Repository Layout

- [agent.ipynb](agent.ipynb) - notebook that contains the full LangGraph pipeline and runtime entry point.
- [tools/gmail_tools.py](tools/gmail_tools.py) - Gmail polling and threaded reply helpers.
- [tools/sheets_tools.py](tools/sheets_tools.py) - Google Sheets logging helpers and duplicate detection.
- [tools/ors_tools.py](tools/ors_tools.py) - ORS geocoding, elevation, optimization, and distance matrix helpers.
- [auth_setup.py](auth_setup.py) - one-time Google OAuth setup that creates `credentials/token.json`.
- [requirements.txt](requirements.txt) - Python dependencies.

## Requirements

- Python 3.13 or compatible virtual environment
- Google OAuth credentials in `credentials/credentials.json`
- Google token file in `credentials/token.json`
- `OPENAI_API_KEY`
- `ORS_API_KEY`
- `GOOGLE_SHEET_ID`

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Add a `.env` file with the required environment variables.
4. Download your Google OAuth client JSON and save it as `credentials/credentials.json`.
5. Run `python auth_setup.py` once to create `credentials/token.json`.

## Environment Variables

The notebook and helper modules read these variables:

- `OPENAI_API_KEY` - OpenAI API key used by GPT-4o.
- `ORS_API_KEY` - OpenRouteService API key.
- `GMAIL_POLL_INTERVAL` - polling interval in seconds, default `60`.
- `GMAIL_QUERY` - Gmail search query, default `is:unread subject:Pickup Schedule`.
- `GOOGLE_TOKEN_PATH` - OAuth token path, default `credentials/token.json`.
- `GOOGLE_SHEET_ID` - Google Sheet id used for logs and route output.

## Google Sheets Tabs

The Sheets integration writes to these tabs:

- `email_log` - raw email metadata per request
- `parsed_stops` - extracted stops from the LLM parser
- `geocoded` - pickup and delivery GPS coordinates with elevation
- `route_output` - optimized route with sequences, ETAs, totals, and savings context
- `error_log` - failed requests with error codes

## Running The App

Open [agent.ipynb](agent.ipynb) and execute the cells in order. The last notebook cell starts the polling loop and continuously checks Gmail for new requests.

The helper scripts can also be run directly:

- `python auth_setup.py` - create the Google OAuth token.
- `python tools/ors_tools.py` - run ORS helper examples.

## Implementation Notes

- The route optimization uses `driving-hgv`, not a passenger car profile.
- The pipeline is now linear, not fan-out based.
- Duplicate requests are skipped by checking the generated request id against the email log.
- Original and optimized route metrics are both calculated with ORS `/matrix`.
- Both pickup and delivery addresses are geocoded, but only pickup addresses are used for optimization.
- The reply email is built from an HTML route table plus a summary of original distance, optimized distance, and savings.

## Troubleshooting

- **Gmail access fails**: Verify `credentials/credentials.json` and regenerate `credentials/token.json` with `python auth_setup.py`.
- **ORS geocoding or optimization fails**: Confirm `ORS_API_KEY` has quota and is correctly set.
- **Sheets writes fail**: Verify `GOOGLE_SHEET_ID` is correct and the authenticated Google account has edit access to the spreadsheet.
- **No reply email sent**: Check the `error_log` sheet in Google Sheets for error codes and messages. Common causes include:
  - Email format doesn't match parser expectations (missing required fields)
  - Addresses cannot be geocoded (invalid or too vague)
  - ORS optimization times out or returns no valid route
- **Distance/time values are inaccurate**: ORS matrix results are more reliable than VROOM estimates alone, but road data freshness and routing profile can still change the result.
- **Duplicate requests detected**: If the same email is resent, the second attempt will be tagged as `DUPLICATE_REQUEST` and skipped to prevent duplicate work.

## Future Todo

Work in progress:

1. Support Gmail attachments such as `.csv` and `.xlsx` files.
2. For now, this is implemented in the notebook file; later it will be moved into a proper folder-based structure.
3. More routes optimization by priority , elevation .