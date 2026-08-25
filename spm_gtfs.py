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
from datetime import datetime, timedelta

WIDGET_URL = "https://horaires.spm-ferries.fr/afficheur-web.php?cie=SPM&langue=en_EN&ver=4445546"
API_BASE = "https://api.ls-resa.fr/LSRest2/SPM/LS_STE_DEPART_CRO/"
USER_AGENT = "Mozilla/5.0 (compatible; spm-gtfs-scraper/1.0)"

# Port id -> canonical info. Coordinates are approximate (harbor/quay
# locations); refine if you have surveyed coordinates.
PORTS = {
    1: {"name": "SAINT PIERRE", "stop_id": "STP", "stop_name": "Saint-Pierre Ferry Terminal",
        "lat": 46.7778, "lon": -56.1719},
    2: {"name": "MIQUELON", "stop_id": "MIQ", "stop_name": "Miquelon Ferry Terminal",
        "lat": 47.0958, "lon": -56.3789},
    3: {"name": "FORTUNE", "stop_id": "FOR", "stop_name": "Fortune Ferry Terminal (Newfoundland)",
        "lat": 47.0736, "lon": -55.8330},
    4: {"name": "LANGLADE", "stop_id": "LAN", "stop_name": "Langlade Ferry Landing",
        "lat": 46.9500, "lon": -56.2900},
}
NAME_TO_STOP_ID = {p["name"]: p["stop_id"] for p in PORTS.values()}

TIMEZONE = "America/Miquelon"


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

    stops_rows = [{
        "stop_id": p["stop_id"],
        "stop_name": p["stop_name"],
        "stop_lat": p["lat"],
        "stop_lon": p["lon"],
        "location_type": 0,
    } for p in PORTS.values()]

    routes = {}       # route_id -> row
    trips_rows = []
    stop_times_rows = []
    service_dates = set()  # set of YYYYMMDD strings actually used

    skipped = 0
    for i, leg in enumerate(legs):
        from_id = NAME_TO_STOP_ID.get(leg["from_name"])
        to_id = NAME_TO_STOP_ID.get(leg["to_name"])
        if not from_id or not to_id:
            skipped += 1
            continue

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
                "route_long_name": f"{name_a.title()} - {name_b.title()}",
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
            "trip_headsign": leg["to_name"].title(),
        })

        stop_times_rows.append({
            "trip_id": trip_id,
            "arrival_time": dep_dt.strftime("%H:%M:%S"),
            "departure_time": dep_dt.strftime("%H:%M:%S"),
            "stop_id": from_id,
            "stop_sequence": 1,
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
            "stop_id": to_id,
            "stop_sequence": 2,
        })

    calendar_dates_rows = [
        {"service_id": d, "date": d, "exception_type": 1}
        for d in sorted(service_dates)
    ]

    feed_info_rows = [{
        "feed_publisher_name": "SPM Ferries (unofficial scrape)",
        "feed_publisher_url": "https://www.spm-ferries.fr",
        "feed_lang": "fr",
        "feed_start_date": feed_start.replace("-", ""),
        "feed_end_date": feed_end.replace("-", ""),
    }]

    if skipped:
        print(f"Warning: skipped {skipped} leg(s) with unrecognized port names", file=sys.stderr)

    return {
        "agency.txt": agency_rows,
        "stops.txt": stops_rows,
        "routes.txt": list(routes.values()),
        "trips.txt": trips_rows,
        "stop_times.txt": stop_times_rows,
        "calendar_dates.txt": calendar_dates_rows,
        "feed_info.txt": feed_info_rows,
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
