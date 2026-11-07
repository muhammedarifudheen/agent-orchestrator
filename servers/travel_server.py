"""Travel MCP server: flight search, booking, confirmation (mock data).

Run standalone: python servers/travel_server.py   (stdio transport)

Set TRAVEL_FAIL_MODE to simulate outages (for testing the exception routing layer):
  search_down  -> search_flights fails with SERVICE_UNAVAILABLE
  booking_down -> book_flight fails with SERVICE_UNAVAILABLE
  all_down     -> every tool fails
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import date as _date

from mcp.server.mcpserver import MCPServer

from errors import ToolFailure, guarded

mcp = MCPServer("travel")

# TODO(real-api): replace with the airline/GDS inventory (Amadeus, Sabre, NDC, ...).
_MOCK_ROUTES: dict[tuple[str, str], list[dict]] = {
    ("DEL", "BOM"): [
        {"airline": "IndiGo", "flight_number": "6E-2041", "depart": "06:15", "arrive": "08:25",
         "duration_min": 130, "price_inr": 5890, "seats_available": 9, "fare_class": "Economy",
         "refundable": False, "baggage_kg": 15, "stops": 0},
        {"airline": "Air India", "flight_number": "AI-865", "depart": "09:40", "arrive": "11:55",
         "duration_min": 135, "price_inr": 7420, "seats_available": 4, "fare_class": "Economy Flex",
         "refundable": True, "baggage_kg": 25, "stops": 0},
        {"airline": "Vistara", "flight_number": "UK-995", "depart": "18:05", "arrive": "20:20",
         "duration_min": 135, "price_inr": 9150, "seats_available": 0, "fare_class": "Premium Economy",
         "refundable": True, "baggage_kg": 25, "stops": 0},
        {"airline": "SpiceJet", "flight_number": "SG-8169", "depart": "13:30", "arrive": "18:10",
         "duration_min": 280, "price_inr": 4650, "seats_available": 12, "fare_class": "Economy",
         "refundable": False, "baggage_kg": 15, "stops": 1},
    ],
}
_BOOKINGS: dict[str, dict] = {}
_IATA = re.compile(r"^[A-Z]{3}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_outage(tool: str) -> None:
    mode = os.environ.get("TRAVEL_FAIL_MODE", "")
    if mode == "all_down" or (mode == "search_down" and tool == "search_flights") or (
        mode == "booking_down" and tool == "book_flight"
    ):
        raise ToolFailure("SERVICE_UNAVAILABLE", f"Upstream travel provider is down (simulated: {mode}).",
                          retryable=True)


@mcp.tool()
@guarded
def search_flights(origin: str, destination: str, date: str) -> list[dict]:
    """Search available flights between two airports on a date. Read-only and idempotent.

    When to call: first step of any flight booking; call it to get the flight_id needed by
    book_flight. Re-run with another date or airports if the result is empty or sold out.

    Args:
        origin: 3-letter IATA airport code of departure (e.g. "DEL" for Delhi).
        destination: 3-letter IATA airport code of arrival (e.g. "BOM" for Mumbai).
        date: Travel date as YYYY-MM-DD. Must not be in the past.

    Returns:
        List of flights with flight_id, airline, depart/arrive times, duration_min, price_inr,
        seats_available (0 = sold out, cannot be booked), fare_class, refundable, baggage_kg,
        stops. Empty list if no flights operate on that route/date.

    Errors: INVALID_AIRPORT (retryable=false), INVALID_DATE (retryable=false; malformed or past
    date), SERVICE_UNAVAILABLE (retryable=true; the same call may succeed later).
    """
    _check_outage("search_flights")
    origin, destination = origin.strip().upper(), destination.strip().upper()
    if not (_IATA.match(origin) and _IATA.match(destination)):
        raise ToolFailure("INVALID_AIRPORT", "Use 3-letter IATA codes, e.g. DEL, BOM.", retryable=False)
    try:
        day = _date.fromisoformat(date)
    except ValueError:
        raise ToolFailure("INVALID_DATE", f"'{date}' is not YYYY-MM-DD.", retryable=False) from None
    if day < _date.today():
        raise ToolFailure("INVALID_DATE", f"{date} is in the past.", retryable=False)

    # TODO(real-api): call the flight search API here; map its response to the same fields.
    flights = _MOCK_ROUTES.get((origin, destination), [])
    return [
        {"flight_id": f"{f['flight_number']}_{origin}{destination}_{date}",
         "origin": origin, "destination": destination, "date": date, **f}
        for f in flights
    ]


@mcp.tool()
@guarded
def book_flight(flight_id: str, passenger_name: str, passenger_email: str) -> dict:
    """Book one seat on a flight returned by search_flights. Creates a real reservation.

    Not idempotent: calling twice with the same arguments creates two bookings, so only retry
    after a failure whose error says no booking was created.

    When to call: only after search_flights returned a flight with seats_available > 0 and the
    traveler has confirmed the choice and given their name and email. Call send_confirmation
    afterwards with the returned booking_ref.

    Args:
        flight_id: The exact flight_id from search_flights.
        passenger_name: Full name as on the passenger's government ID.
        passenger_email: Passenger email (used for confirmation).

    Returns:
        Dict with booking_ref, status ("CONFIRMED"), flight_id, airline, depart, arrive,
        passenger_name, passenger_email, total_price_inr, payment_status, and booked_at.

    Errors: FLIGHT_NOT_FOUND (retryable=false; unknown, malformed or past-dated flight_id),
    SOLD_OUT (retryable=false; search for another flight), INVALID_PASSENGER (retryable=false),
    SERVICE_UNAVAILABLE (retryable=true; no booking was created).
    """
    _check_outage("book_flight")
    if not passenger_name.strip() or not _EMAIL.match(passenger_email):
        raise ToolFailure("INVALID_PASSENGER", "A full passenger name and a valid email are required.", retryable=False)
    try:
        flight_number, route, day = flight_id.split("_")
    except ValueError:
        raise ToolFailure("FLIGHT_NOT_FOUND", f"Malformed flight_id '{flight_id}'.", retryable=False) from None

    try:  # the date embedded in flight_id was never validated: past/garbage dates were bookable
        if _date.fromisoformat(day) < _date.today():
            raise ValueError(day)
    except ValueError:
        raise ToolFailure("FLIGHT_NOT_FOUND", f"flight_id '{flight_id}' has an invalid or past date.",
                          retryable=False) from None

    # TODO(real-api): re-check live availability, create the PNR, take payment.
    flight = next((f for (o, d), fl in _MOCK_ROUTES.items() if o + d == route
                   for f in fl if f["flight_number"] == flight_number), None)
    if flight is None:
        raise ToolFailure("FLIGHT_NOT_FOUND", f"No flight matches '{flight_id}'.", retryable=False)
    if flight["seats_available"] <= 0:
        raise ToolFailure("SOLD_OUT", f"{flight_number} on {day} has no seats left.", retryable=False)

    ref = "BK" + uuid.uuid4().hex[:6].upper()
    _BOOKINGS[ref] = {
        "booking_ref": ref, "status": "CONFIRMED", "flight_id": flight_id,
        "airline": flight["airline"], "depart": flight["depart"], "arrive": flight["arrive"],
        "passenger_name": passenger_name.strip(), "passenger_email": passenger_email,
        "total_price_inr": flight["price_inr"], "payment_status": "PAID",
        "booked_at": _date.today().isoformat(),
    }
    return _BOOKINGS[ref]


@mcp.tool()
@guarded
def send_confirmation(email: str, booking_ref: str, details: str) -> bool:
    """Email a booking confirmation to the passenger. Not idempotent: each call sends one email.

    When to call: right after book_flight succeeds, using its booking_ref. Do not call it for
    a booking that failed.

    Args:
        email: Recipient email address.
        booking_ref: Reference returned by book_flight.
        details: Human-readable itinerary summary to include in the email body.

    Returns:
        True if the email was accepted for delivery.

    Errors: INVALID_EMAIL (retryable=false), BOOKING_NOT_FOUND (retryable=false),
    SERVICE_UNAVAILABLE (retryable=true; the booking itself is unaffected).
    """
    _check_outage("send_confirmation")
    if not _EMAIL.match(email):
        raise ToolFailure("INVALID_EMAIL", f"'{email}' is not a valid email address.", retryable=False)
    if booking_ref not in _BOOKINGS:
        raise ToolFailure("BOOKING_NOT_FOUND", f"Unknown booking_ref '{booking_ref}'.", retryable=False)
    # TODO(real-api): send via the email/SMS provider (SES, SendGrid, ...).
    return True


if __name__ == "__main__":
    mcp.run()
