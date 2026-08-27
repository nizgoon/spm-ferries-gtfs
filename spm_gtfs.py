#!/usr/bin/env python3
"""
spm_gtfs.py — Scrape SPM Ferries (Saint-Pierre-et-Miquelon) schedules and
build a GTFS feed.

How it works
------------
SPM Ferries' public schedule widget:
    https://horaires.spm-ferries.fr/afficheur-web.php?cie=SPM&langue=en_EN

...is a thin HTML page that embeds a short-lived Bearer JWT (in a hidden
<input id="token">) and then calls a JSON API run by the ferry-booking
vendor LS-Résa:

    GET https://api.ls-resa.fr/LSRest2/SPM/LS_STE_DEPART_CRO/{portId}
        ?date=YYYY-MM-DD
        &dateFin=YYYY-MM-DD
        &heure=500
        &affichageAllerRetour=false
        &affichageTroncon=false
        &vendableInternet=true

Port ids: 1=Saint-Pierre, 2=Miquelon, 3=Fortune (Newfoundland), 4=Langlade.

Each returned record is a "cruise" (a bookable product, which can bundle a
multi-day round trip). The real, physical, single-sailing legs are in its
`etapes` array — each etape has its own boat name, departure port/time and
arrival port/time. This script builds GTFS trips at the ETAPE (leg) level,
which is the physically correct level: one etape == one sailing a rider can
board.

Because the token expires in ~20 minutes, the script re-fetches a fresh one
from the widget page before every batch of API calls (call it as often as
you like — it's the same, unauthenticated flow your browser uses).

Usage
-----
    python3 spm_gtfs.py --days-ahead 180 --out spm_gtfs.zip

Requires only the Python standard library (urllib, no external deps),
optionally add --pretty for pretty JSON dumps to inspect raw data.
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

WIDGET_URL = "https://horaires.spm-ferries.fr/afficheur-web.php?cie=SPM&langue=en_EN&ver=4445546"
API_BASE = "https://api.ls-resa.fr/LSRest2/SPM/LS_STE_DEPART_CRO/"
USER_AGENT = "Mozilla/5.0 (compatible; spm-gtfs-scraper/1.0)"

# Port id -> canonical info. Coordinates are approximate (harbor/quay
# locations); refine if you have surveyed coordinates.
#
# stop_name: feed language is French (agency_lang="fr"), so these are in
# French -- except Fortune, which stays in English since it's an
# anglophone Newfoundland town and "Fortune, NL" is how it's actually
# referred to (a French translation would look odd/invented).
#
# stop_timezone: Fortune (Newfoundland) is in America/St_Johns (UTC-3:30 /
# UTC-2:30 DST), while Saint-Pierre, Miquelon, and Langlade are all in
# America/Miquelon (UTC-3 / UTC-2 DST) — a 30-minute offset. The raw API's
# dateHeureDepart/dateHeureArrive are each recorded in *their own port's*
# local time (confirmed empirically: naive STP->FOR duration reads 60 min,
# FOR->STP reads 120 min for the same physical ~90 min crossing — the
# asymmetry is exactly the 30-minute tz gap applied in opposite
# directions; also cross-checked against SPM's own official Miquelon<->
# Fortune PDF calendar, which labels its times "HEURE LOCALE / LOCAL
# TIME" and matches our scraped data exactly). So the wall-clock values
# we write to stop_times.txt are already correct; they just need
# Fortune's stop_timezone set so GTFS consumers resolve elapsed/absolute
# time correctly instead of assuming everything is in agency_timezone.
PORTS = {
    1: {"name": "SAINT PIERRE", "stop_id": "STP", "stop_name": "Gare maritime de Saint-Pierre",
        "lat": 46.778612767967154, "lon": -56.17149092325049, "stop_timezone": None},
    2: {"name": "MIQUELON", "stop_id": "MIQ", "stop_name": "Gare maritime de Miquelon",
        "lat": 47.1026601518199, "lon":  -56.375324928010784, "stop_timezone": None},
    3: {"name": "FORTUNE", "stop_id": "FOR", "stop_name": "Fortune Ferry Terminal",
        "lat": 47.073578583687706, "lon": -55.83051862309114, "stop_timezone": "America/St_Johns"},
    4: {"name": "LANGLADE", "stop_id": "LAN", "stop_name": "Débarcadère de Langlade",
        "lat": 46.89787671605952, "lon": -56.300670760277356, "stop_timezone": None},
}

# Proper French display form of each raw API place name, used for
# route_long_name / trip_headsign. NAME_TO_STOP_ID's raw keys ("SAINT
# PIERRE" etc.) come from the API in all-caps with no hyphen, so a plain
# .title() call would produce "Saint Pierre" instead of "Saint-Pierre".
DISPLAY_NAME = {
    "SAINT PIERRE": "Saint-Pierre",
    "MIQUELON": "Miquelon",
    "FORTUNE": "Fortune",
    "LANGLADE": "Langlade",
}
NAME_TO_STOP_ID = {p["name"]: p["stop_id"] for p in PORTS.values()}
STOP_ID_TO_TIMEZONE = {p["stop_id"]: (p["stop_timezone"] or "America/Miquelon") for p in PORTS.values()}

TIMEZONE = "America/Miquelon"

# Booking rules. Fortune departures before 9:00 AM (Fortune/America/St_Johns
# local time) require booking by 16:00 the day before -- ALSO in Fortune
# time -- which booking_rules.txt's prior_notice_last_time must express in
# agency_timezone (America/Miquelon), so we convert it here rather than
# hardcoding a value that would silently go stale if the 30-minute
# Miquelon/Newfoundland offset ever changed.
FORTUNE_EARLY_CUTOFF = time(9, 0, 0)
_FOR_16H_MIQUELON = datetime(2026, 1, 1, 16, 0, 0).replace(
    tzinfo=ZoneInfo(PORTS[3]["stop_timezone"])).astimezone(ZoneInfo(TIMEZONE))
BR_FORTUNE_EARLY = "BR_FOR_AV09H00_16HVEILLE"
BR_STANDARD = "BR_1H_AVANT_DEPART"

_BOOKING_RULES_COLUMNS = [
    "booking_rule_id", "booking_type", "prior_notice_duration_min",
    "prior_notice_last_day", "prior_notice_last_time", "pickup_message", "booking_url",
]
BOOKING_RULES_ROWS = [
    {col: row.get(col, "") for col in _BOOKING_RULES_COLUMNS}
    for row in [
        {
            "booking_rule_id": BR_FORTUNE_EARLY,
            "booking_type": 2,
            "prior_notice_last_day": 1,
            "prior_notice_last_time": _FOR_16H_MIQUELON.strftime("%H:%M:%S"),
            "pickup_message": "La réservation est obligatoire. Pour les départs de Fortune avant "
                               "9h00, la réservation doit être effectuée avant 16h00 la veille.",
            "booking_url": "https://www.spm-ferries.fr/achetez-votre-billet/billetterie-en-ligne/",
        },
        {
            "booking_rule_id": BR_STANDARD,
            "booking_type": 1,
            "prior_notice_duration_min": 60,
            "pickup_message": "La réservation est obligatoire. Pour ce départ, la réservation en "
                               "ligne doit être effectuée au moins 1 heure avant le départ.",
            "booking_url": "https://www.spm-ferries.fr/achetez-votre-billet/billetterie-en-ligne/",
        },
    ]
]

# ---------------------------------------------------------------------------
# stops.txt / pathways.txt
#
# Each port's top-level station (location_type=1) has separate boarding
# platforms (location_type=0) per how long before departure that sailing's
# gate opens, plus one or more entrances (location_type=2). A trip planner
# that respects pathways.txt's traversal_time has to route the rider through
# the entrance and its pathway to the specific platform for their sailing,
# which forces it to surface the true lead time (e.g. Fortune's Canadian
# customs control needs 2h, so FOR_120 riders show up 120 min early) instead
# of the walk/wait time it would otherwise assume.
#
# Which platform a sailing boards from depends on BOTH its origin and
# destination (e.g. STP->FOR boards from STP_60, but STP->MIQ boards from
# STP_15) -- so this is keyed on the (origin, destination) port_id pair,
# not just the origin.
# ---------------------------------------------------------------------------

OD_BOARDING_STOPS = {
    ("FOR", "STP"): ("FOR_120", "STP_60"),
    ("FOR", "MIQ"): ("FOR_120", "MIQ_60"),
    ("LAN", "STP"): ("LAN_30", "STP_15"),
    ("MIQ", "STP"): ("MIQ_15", "STP_15"),
    ("MIQ", "FOR"): ("MIQ_60", "FOR_120"),
    ("STP", "MIQ"): ("STP_15", "MIQ_15"),
    ("STP", "LAN"): ("STP_15", "LAN_30"),
    ("STP", "FOR"): ("STP_60", "FOR_120"),
}

STOPS_ROWS = [
    {"stop_id": "FOR", "stop_name": "Fortune Ferry Terminal", "stop_lat": 47.07357858, "stop_lon": -55.83051862,
     "location_type": 1, "stop_timezone": "America/St_Johns", "parent_station": ""},
    {"stop_id": "FOR_120", "stop_name": "Fortune Ferry Terminal", "stop_lat": 47.07357858, "stop_lon": -55.83051862,
     "location_type": 0, "stop_timezone": "America/St_Johns", "parent_station": "FOR"},
    {"stop_id": "FOR_ENT", "stop_name": "Fortune Ferry Terminal", "stop_lat": 47.07400542, "stop_lon": -55.82937992,
     "location_type": 2, "stop_timezone": "America/St_Johns", "parent_station": "FOR"},
    {"stop_id": "LAN", "stop_name": "Débarcadère de Langlade", "stop_lat": 46.89787672, "stop_lon": -56.30067076,
     "location_type": 1, "stop_timezone": "", "parent_station": ""},
    {"stop_id": "LAN_30", "stop_name": "Débarcadère de Langlade", "stop_lat": 46.89787672, "stop_lon": -56.30067076,
     "location_type": 0, "stop_timezone": "", "parent_station": "LAN"},
    {"stop_id": "LAN_ENT", "stop_name": "Débarcadère de Langlade", "stop_lat": 46.89790803, "stop_lon": -56.30103062,
     "location_type": 2, "stop_timezone": "", "parent_station": "LAN"},
    {"stop_id": "MIQ", "stop_name": "Gare maritime de Miquelon", "stop_lat": 47.10266015, "stop_lon": -56.37532493,
     "location_type": 1, "stop_timezone": "", "parent_station": ""},
    {"stop_id": "MIQ_15", "stop_name": "Gare maritime de Miquelon", "stop_lat": 47.10266015, "stop_lon": -56.37532493,
     "location_type": 0, "stop_timezone": "", "parent_station": "MIQ"},
    {"stop_id": "MIQ_60", "stop_name": "Gare maritime de Miquelon", "stop_lat": 47.10266015, "stop_lon": -56.37532493,
     "location_type": 0, "stop_timezone": "", "parent_station": "MIQ"},
    {"stop_id": "MIQ_ENT", "stop_name": "Gare maritime de Miquelon", "stop_lat": 47.10091553, "stop_lon": -56.37638985,
     "location_type": 2, "stop_timezone": "", "parent_station": "MIQ"},
    {"stop_id": "STP", "stop_name": "Gare maritime de Saint-Pierre", "stop_lat": 46.77861277, "stop_lon": -56.17149092,
     "location_type": 1, "stop_timezone": "", "parent_station": ""},
    {"stop_id": "STP_15", "stop_name": "Gare maritime de Saint-Pierre", "stop_lat": 46.77861277, "stop_lon": -56.17149092,
     "location_type": 0, "stop_timezone": "", "parent_station": "STP"},
    {"stop_id": "STP_60", "stop_name": "Gare maritime de Saint-Pierre", "stop_lat": 46.77861277, "stop_lon": -56.17149092,
     "location_type": 0, "stop_timezone": "", "parent_station": "STP"},
    {"stop_id": "STP_ENT_ALL", "stop_name": "Gare maritime de Saint-Pierre", "stop_lat": 46.77869949, "stop_lon": -56.1720757,
     "location_type": 2, "stop_timezone": "", "parent_station": "STP"},
    {"stop_id": "STP_EXIT_INTL", "stop_name": "Gare maritime de Saint-Pierre", "stop_lat": 46.77869949, "stop_lon": -56.1720757,
     "location_type": 2, "stop_timezone": "", "parent_station": "STP"},
]

_PATHWAYS_COLUMNS = [
    "pathway_id", "from_stop_id", "to_stop_id", "pathway_mode",
    "is_bidirectional", "traversal_time", "signposted_as",
]
PATHWAYS_ROWS = [
    {col: row.get(col, "") for col in _PATHWAYS_COLUMNS}
    for row in [
        {"pathway_id": "PW_FOR_ENT-FOR_120", "from_stop_id": "FOR_ENT", "to_stop_id": "FOR_120",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 7200, "signposted_as": "Contrôle et embarquement"},
        {"pathway_id": "PW_FOR_120-FOR_ENT", "from_stop_id": "FOR_120", "to_stop_id": "FOR_ENT",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 3600, "signposted_as": "Sortie via douane canadienne"},
        {"pathway_id": "PW_LAN_ENT-LAN_30", "from_stop_id": "LAN_ENT", "to_stop_id": "LAN_30",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 1800, "signposted_as": "Embarquement via Jeune France"},
        {"pathway_id": "PW_LAN_30-LAN_ENT", "from_stop_id": "LAN_30", "to_stop_id": "LAN_ENT",
         "pathway_mode": 1, "is_bidirectional": 0, "signposted_as": "Sortie"},
        {"pathway_id": "PW_MIQ_ENT-MIQ_15", "from_stop_id": "MIQ_ENT", "to_stop_id": "MIQ_15",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 900, "signposted_as": "Embarquement"},
        {"pathway_id": "PW_MIQ_ENT-MIQ_60", "from_stop_id": "MIQ_ENT", "to_stop_id": "MIQ_60",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 3600, "signposted_as": "Contrôle et embarquement"},
        {"pathway_id": "PW_MIQ_15-MIQ_ENT", "from_stop_id": "MIQ_15", "to_stop_id": "MIQ_ENT",
         "pathway_mode": 1, "is_bidirectional": 0, "signposted_as": "Sortie"},
        {"pathway_id": "PW_MIQ_60-MIQ_ENT", "from_stop_id": "MIQ_60", "to_stop_id": "MIQ_ENT",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 900, "signposted_as": "Sortie via douane française"},
        {"pathway_id": "PW_STP_ENT_ALL-STP_60", "from_stop_id": "STP_ENT_ALL", "to_stop_id": "STP_60",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 3600, "signposted_as": "Contrôle et embarquement"},
        {"pathway_id": "PW_STP_ENT_ALL-STP_15", "from_stop_id": "STP_ENT_ALL", "to_stop_id": "STP_15",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 900, "signposted_as": "Embarquement"},
        {"pathway_id": "PW_STP_60-STP_EXIT_INTL", "from_stop_id": "STP_60", "to_stop_id": "STP_EXIT_INTL",
         "pathway_mode": 1, "is_bidirectional": 0, "traversal_time": 1800, "signposted_as": "Sortie via douane française"},
        {"pathway_id": "PW_STP_15-STP_ENT_ALL", "from_stop_id": "STP_15", "to_stop_id": "STP_ENT_ALL",
         "pathway_mode": 1, "is_bidirectional": 0, "signposted_as": "Sortie"},
    ]
]


def sanity_check_legs(legs, min_minutes=5, max_minutes=360, verbose=True):
    """Verify each leg's REAL elapsed travel time using proper IANA
    timezone conversion (zoneinfo) rather than assuming a fixed offset.

    dep_dt/arr_dt are naive local times, each already in *their own port's*
    timezone (see PORTS docstring). We localize each to its correct zone
    and convert to UTC before diffing, so this is automatically correct
    across DST transitions even if SPM and Newfoundland ever change clocks
    on different dates in the future (checked 2020-2030: they currently
    don't -- the gap is a constant 30 minutes -- but we don't want to bake
    that constant in anywhere; this check re-derives it from real tz data
    every time instead).

    This also serves as an independent check for bad data (e.g. would have
    caught the "MODIFS PAYANTES" administrative placeholder records on its
    own, since a real port-to-port sailing should always land between
    min_minutes and max_minutes)."""
    problems = []
    for leg in legs:
        from_id = NAME_TO_STOP_ID.get(leg["from_name"])
        to_id = NAME_TO_STOP_ID.get(leg["to_name"])
        if not from_id or not to_id:
            continue
        dep_aware = datetime.fromisoformat(leg["dep_dt"]).replace(
            tzinfo=ZoneInfo(STOP_ID_TO_TIMEZONE[from_id]))
        arr_aware = datetime.fromisoformat(leg["arr_dt"]).replace(
            tzinfo=ZoneInfo(STOP_ID_TO_TIMEZONE[to_id]))
        real_minutes = (arr_aware - dep_aware).total_seconds() / 60
        if not (min_minutes <= real_minutes <= max_minutes):
            problems.append((leg, real_minutes))
    if problems and verbose:
        print(f"Warning: {len(problems)} leg(s) have implausible real "
              f"(timezone-corrected) travel time:", file=sys.stderr)
        for leg, mins in problems[:10]:
            print(f"  {leg['from_name']} -> {leg['to_name']} at {leg['dep_dt']}: "
                  f"{mins:.0f} real minutes (boat={leg['boat']!r})", file=sys.stderr)
    return problems


def fetch_token():
    """Load the widget HTML page and pull out the embedded Bearer token."""
    req = urllib.request.Request(WIDGET_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = re.search(r'id="token"\s+type="hidden"\s+value="([^"]+)"', html)
    if not m:
        raise RuntimeError("Could not find token in widget HTML — page structure may have changed")
    return m.group(1)


def fetch_departures(port_id, date_start, date_end, token):
    """Call the LS-Résa API for one port and one date window. Returns a list
    of raw 'cruise' dicts (possibly empty)."""
    url = (
        f"{API_BASE}{port_id}"
        f"?date={date_start}&heure=500&dateFin={date_end}"
        f"&affichageAllerRetour=false&affichageTroncon=false&vendableInternet=true"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"message": body}
        if payload.get("message") == "No cruise found":
            return []
        raise RuntimeError(f"API error for port {port_id}: {payload}") from e


def collect_all_legs(date_start, date_end, refresh_every=15, verbose=True):
    """Query all 4 ports for the given window and flatten every trip's
    `etapes` into individual sailing legs. Refreshes the token periodically
    since it's only valid ~20 minutes."""
    legs = []
    token = fetch_token()
    calls_since_refresh = 0

    for port_id, info in PORTS.items():
        if calls_since_refresh >= refresh_every:
            token = fetch_token()
            calls_since_refresh = 0
        if verbose:
            print(f"Fetching departures from {info['name']} ({date_start} -> {date_end})...",
                  file=sys.stderr)
        cruises = fetch_departures(port_id, date_start, date_end, token)
        calls_since_refresh += 1

        for cruise in cruises:
            code = cruise.get("code", "")
            for etape in cruise.get("etapes", []):
                legs.append({
                    "cruise_code": code,
                    "cruise_name": cruise.get("nom", ""),
                    "boat": etape.get("nomBateau", ""),
                    "from_name": etape["portDepart"],
                    "to_name": etape["portArrive"],
                    "dep_dt": etape["dateHeureDepart"],
                    "arr_dt": etape["dateHeureArrive"],
                    "vendable": cruise.get("vendable"),
                    "seats_total": cruise.get("placeTotal"),
                    "seats_left": cruise.get("placeRestante"),
                    "status": cruise.get("etat"),
                })
    return legs


def dedupe_legs(legs):
    """Different port queries only ever return cruises departing from that
    port, so legs shouldn't repeat — but dedupe defensively on the natural
    key (boat, from, to, departure time)."""
    seen = set()
    out = []
    for leg in legs:
        key = (leg["boat"], leg["from_name"], leg["to_name"], leg["dep_dt"])
        if key in seen:
            continue
        seen.add(key)
        out.append(leg)
    return out


# Real sailings only ever show up with these two boats (188 and 120 seats
# respectively). The booking system also emits administrative placeholder
# "cruises" — observed boat name "MODIFS PAYANTES" ("paid modifications"),
# sentinel capacity of 10000 seats, no real departure/arrival (e.g. a
# 40km crossing in "1 minute") — that aren't real sailings and would
# otherwise show up as physically-impossible trips in a GTFS validator.
NON_SAILING_BOATS = {"MODIFS PAYANTES"}
MAX_PLAUSIBLE_CAPACITY = 500  # real boats are 120-188 seats; 10000 is a sentinel


def filter_administrative_legs(legs, verbose=True):
    """Drop non-sailing placeholder records the booking system emits."""
    kept, dropped = [], []
    for leg in legs:
        if leg["boat"] in NON_SAILING_BOATS or (
            leg["seats_total"] is not None and leg["seats_total"] > MAX_PLAUSIBLE_CAPACITY
        ):
            dropped.append(leg)
        else:
            kept.append(leg)
    if dropped and verbose:
        print(f"Filtered {len(dropped)} administrative/placeholder record(s) "
              f"(e.g. boat={dropped[0]['boat']!r}, seats_total={dropped[0]['seats_total']})",
              file=sys.stderr)
    return kept


# ---------------------------------------------------------------------------
# GTFS construction
# ---------------------------------------------------------------------------

def build_gtfs(legs, feed_start, feed_end):
    """Turn a flat list of sailing legs into a dict of GTFS table rows."""

    agency_rows = [{
        "agency_id": "SPM",
        "agency_name": "SPM Ferries",
        "agency_url": "https://www.spm-ferries.fr",
        "agency_timezone": TIMEZONE,
        "agency_lang": "fr",
    }]

    routes = {}       # route_id -> row
    trips_rows = []
    stop_times_rows = []
    service_dates = set()  # set of YYYYMMDD strings actually used

    skipped = 0
    skipped_od = 0
    for i, leg in enumerate(legs):
        from_id = NAME_TO_STOP_ID.get(leg["from_name"])
        to_id = NAME_TO_STOP_ID.get(leg["to_name"])
        if not from_id or not to_id:
            skipped += 1
            continue

        boarding_stops = OD_BOARDING_STOPS.get((from_id, to_id))
        if not boarding_stops:
            skipped_od += 1
            continue
        board_stop_id, alight_stop_id = boarding_stops

        # Undirected route: STP<->MIQ is one route regardless of which way
        # it's sailing; direction_id (0/1) captures the direction. The
        # canonical (alphabetical) order defines direction_id 0.
        stop_a, stop_b = sorted([from_id, to_id])
        route_id = f"{stop_a}-{stop_b}"
        direction_id = 0 if from_id == stop_a else 1

        if route_id not in routes:
            name_a = leg["from_name"] if from_id == stop_a else leg["to_name"]
            name_b = leg["to_name"] if from_id == stop_a else leg["from_name"]
            routes[route_id] = {
                "route_id": route_id,
                "agency_id": "SPM",
                "route_short_name": route_id,
                "route_long_name": f"{DISPLAY_NAME.get(name_a, name_a)} - {DISPLAY_NAME.get(name_b, name_b)}",
                "route_type": 4,  # ferry
            }

        dep_dt = datetime.fromisoformat(leg["dep_dt"])
        arr_dt = datetime.fromisoformat(leg["arr_dt"])
        service_id = dep_dt.strftime("%Y%m%d")
        service_dates.add(service_id)

        trip_id = f"{from_id}-{to_id}_{dep_dt.strftime('%Y%m%dT%H%M')}_{i}"
        trips_rows.append({
            "route_id": route_id,
            "service_id": service_id,
            "trip_id": trip_id,
            "direction_id": direction_id,
            "trip_short_name": leg["cruise_code"],
            "trip_headsign": DISPLAY_NAME.get(leg["to_name"], leg["to_name"]),
            "bikes_allowed": 1,  # SPM Ferries carries bikes (see tariff page bike fares)
        })

        if from_id == "FOR" and dep_dt.time() < FORTUNE_EARLY_CUTOFF:
            booking_rule_id = BR_FORTUNE_EARLY
        else:
            booking_rule_id = BR_STANDARD

        stop_times_rows.append({
            "trip_id": trip_id,
            "arrival_time": dep_dt.strftime("%H:%M:%S"),
            "departure_time": dep_dt.strftime("%H:%M:%S"),
            "stop_id": board_stop_id,
            "stop_sequence": 1,
            "pickup_type": 2,
            "pickup_booking_rule_id": booking_rule_id,
        })
        # if departure and arrival cross midnight, GTFS wants arrival_time
        # to keep counting past 24:00:00 rather than wrapping to 00:xx
        day_offset = (arr_dt.date() - dep_dt.date()).days
        arr_hms = arr_dt.strftime("%H:%M:%S")
        if day_offset > 0:
            h, m, s = arr_hms.split(":")
            arr_hms = f"{int(h) + 24 * day_offset}:{m}:{s}"
        stop_times_rows.append({
            "trip_id": trip_id,
            "arrival_time": arr_hms,
            "departure_time": arr_hms,
            "stop_id": alight_stop_id,
            "stop_sequence": 2,
            "pickup_type": 1,
            "pickup_booking_rule_id": "",
        })

    calendar_dates_rows = [
        {"service_id": d, "date": d, "exception_type": 1}
        for d in sorted(service_dates)
    ]

    feed_info_rows = [{
        "feed_publisher_name": "Transit app (Derek LEE & Brody FLANNIGAN)",
        "feed_publisher_url": "https://transit.app",
        "feed_lang": "fr",
        "feed_start_date": feed_start.replace("-", ""),
        "feed_end_date": feed_end.replace("-", ""),
        "feed_contact_email": "derek@transit.app",
    }]

    if skipped:
        print(f"Warning: skipped {skipped} leg(s) with unrecognized port names", file=sys.stderr)
    if skipped_od:
        print(f"Warning: skipped {skipped_od} leg(s) with no OD_BOARDING_STOPS entry "
              f"for their origin/destination pair", file=sys.stderr)

    return {
        "agency.txt": agency_rows,
        "stops.txt": STOPS_ROWS,
        "routes.txt": list(routes.values()),
        "trips.txt": trips_rows,
        "stop_times.txt": stop_times_rows,
        "calendar_dates.txt": calendar_dates_rows,
        "feed_info.txt": feed_info_rows,
        "booking_rules.txt": BOOKING_RULES_ROWS,
        "pathways.txt": PATHWAYS_ROWS,
    }


def write_gtfs_zip(tables, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, rows in tables.items():
            buf = io.StringIO()
            if rows:
                fieldnames = list(rows[0].keys())
                writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            zf.writestr(filename, buf.getvalue())
    print(f"Wrote {out_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-ahead", type=int, default=180,
                     help="How many days ahead to request (actual published horizon may be shorter; default 180)")
    ap.add_argument("--start-date", default=None,
                     help="YYYY-MM-DD to start from (default: today)")
    ap.add_argument("--out", default="spm_gtfs.zip", help="Output GTFS zip path")
    ap.add_argument("--raw-json", default=None, help="Optional path to also dump the raw flattened legs as JSON")
    args = ap.parse_args()

    start = datetime.strptime(args.start_date, "%Y-%m-%d") if args.start_date else datetime.now()
    end = start + timedelta(days=args.days_ahead)
    date_start = start.strftime("%Y-%m-%d")
    date_end = end.strftime("%Y-%m-%d")

    legs = collect_all_legs(date_start, date_end)
    legs = dedupe_legs(legs)
    legs = filter_administrative_legs(legs)
    sanity_check_legs(legs)  # diagnostic only; doesn't drop anything itself
    print(f"Collected {len(legs)} sailing legs", file=sys.stderr)

    if args.raw_json:
        with open(args.raw_json, "w") as f:
            json.dump(legs, f, indent=2)
        print(f"Wrote raw legs to {args.raw_json}", file=sys.stderr)

    if not legs:
        print("No legs found — nothing to write.", file=sys.stderr)
        sys.exit(1)

    tables = build_gtfs(legs, date_start, date_end)
    write_gtfs_zip(tables, args.out)


if __name__ == "__main__":
    main()
