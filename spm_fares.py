#!/usr/bin/env python3
"""
spm_fares.py — Build a GTFS-Fares v2 fares module for SPM Ferries, meant to
sit alongside the schedule feed produced by spm_gtfs.py (same stop_ids:
STP, MIQ, FOR, LAN).

IMPORTANT — this is NOT scraped from a live API
--------------------------------------------------
Unlike the schedule widget, SPM Ferries' fares page is static WordPress
content, not JSON from a booking API:

    https://www.spm-ferries.fr/horaires-et-tarifs/tarifs-2019/
    (English: .../en/schedules-and-fares/fares-2019/)

There's no fares endpoint on the LS-Résa API (checked: LS_TARIF, LS_TARIFS,
LS_GRILLE_TARIF, LS_PRODUIT, and similar candidate names all 404). So this
script encodes the published tariff table as data below, with the source
URL and the date it was transcribed. Prices don't seem to change often
(the page has been called "tarifs-2019" for years), but there is no way to
auto-detect an update — re-check the page periodically and edit FARES
below by hand.

Scope (v1, "core" per user choice)
-----------------------------------
Adult + "Réduit 1-4" fares, one-way and round-trip, for the three route
groups SPM Ferries actually prices independently:
  - Saint-Pierre <-> Miquelon
  - Saint-Pierre <-> Langlade
  - Saint-Pierre <-> Fortune  AND  Miquelon <-> Fortune (same tariff table)

Explicitly NOT included in this pass (ask for a v2 if you want these):
  group/school fares, bikes, pets, multi-ride cards (Cartes d'abonnement),
  and the triangular "PASS SPM" through-fare.

Modeling note on round trips
-----------------------------
GTFS-Fares v2's fare_leg_rules formally prices a single leg. SPM's
"aller retour" price isn't a rigorously composed 2-leg transfer fare in the
source data — it's just a second flat number in the same table row. Rather
than fake a transfer_rule that wouldn't be true to the source, this script
models "one-way" and "return" as two separate fare_products both attached
to the same (directional) leg via fare_leg_rules. A consumer just sees two
line-item prices for that leg, same as the human-readable price table.
"""

import csv
import io
import zipfile

SOURCE_URL = "https://www.spm-ferries.fr/horaires-et-tarifs/tarifs-2019/"
TRANSCRIBED_ON = "2026-08-25"
CURRENCY = "EUR"

# (route_group_id, area_a, area_b, rider_category_id, one_way, return)
FARES = [
    # Saint-Pierre <-> Miquelon
    ("STP_MIQ", "STP", "MIQ", "adult",    16.00, 24.00),
    ("STP_MIQ", "STP", "MIQ", "reduit_1", 10.00, 13.00),

    # Saint-Pierre <-> Langlade
    ("STP_LAN", "STP", "LAN", "adult",    10.00, 17.00),
    ("STP_LAN", "STP", "LAN", "reduit_1",  5.00,  8.00),

    # Saint-Pierre <-> Fortune  (same tariff as Miquelon <-> Fortune)
    ("STP_FOR", "STP", "FOR", "adult",    45.00, 73.00),
    ("STP_FOR", "STP", "FOR", "reduit_2", 35.00, 49.00),
    ("STP_FOR", "STP", "FOR", "reduit_3", 35.00, 49.00),
    ("STP_FOR", "STP", "FOR", "reduit_4", 40.00, 68.00),

    # Miquelon <-> Fortune (same tariff table as Saint-Pierre <-> Fortune)
    ("MIQ_FOR", "MIQ", "FOR", "adult",    45.00, 73.00),
    ("MIQ_FOR", "MIQ", "FOR", "reduit_2", 35.00, 49.00),
    ("MIQ_FOR", "MIQ", "FOR", "reduit_3", 35.00, 49.00),
    ("MIQ_FOR", "MIQ", "FOR", "reduit_4", 40.00, 68.00),
]

# Feed language is French (matches agency_lang="fr" in spm_gtfs.py and
# feed_lang below -- these two must agree or gtfs-validator flags
# feed_info_lang_and_agency_lang_mismatch).
RIDER_CATEGORIES = {
    "adult":    ("Adulte", 1),
    "reduit_1": ("Tarif réduit 1 (enfant 2-11 ans, personne en situation de handicap, "
                 "60 ans et plus) — lignes Saint-Pierre/Miquelon et Saint-Pierre/Langlade", 0),
    "reduit_2": ("Tarif réduit 2 (enfant 2-11 ans) — ligne Fortune", 0),
    "reduit_3": ("Tarif réduit 3 (personne en situation de handicap) — ligne Fortune", 0),
    "reduit_4": ("Tarif réduit 4 (60 ans et plus) — ligne Fortune", 0),
}

# Short French labels for fare_product_name (the RIDER_CATEGORIES names
# above are the full accessibility-oriented descriptions used in
# rider_categories.txt; product names want something shorter).
RIDER_SHORT_NAME = {
    "adult": "Adulte",
    "reduit_1": "Tarif réduit 1",
    "reduit_2": "Tarif réduit 2",
    "reduit_3": "Tarif réduit 3",
    "reduit_4": "Tarif réduit 4",
}

# Full French place names, used for fare_product_name. Fortune stays in
# English (see spm_gtfs.py's DISPLAY_NAME / stop_name for the same
# reasoning): it's an anglophone Newfoundland town, and "Fortune" is
# spelled the same either way, so no translation needed there anyway.
AREAS = {
    "STP": "Saint-Pierre",
    "MIQ": "Miquelon",
    "FOR": "Fortune",
    "LAN": "Langlade",
}
# 1:1 mapping onto the stop_ids used in spm_gtfs.py's stops.txt
STOP_TO_AREA = {"STP": "STP", "MIQ": "MIQ", "FOR": "FOR", "LAN": "LAN"}


def build_tables():
    rider_categories_rows = [
        {"rider_category_id": rid, "rider_category_name": name, "is_default_fare_category": is_default}
        for rid, (name, is_default) in RIDER_CATEGORIES.items()
    ]

    areas_rows = [{"area_id": aid, "area_name": name} for aid, name in AREAS.items()]

    stop_areas_rows = [{"stop_id": sid, "area_id": aid} for sid, aid in STOP_TO_AREA.items()]

    fare_products_rows = []
    fare_leg_rules_rows = []

    for route_group, area_a, area_b, rider_id, one_way, return_fare in FARES:
        ow_id = f"{route_group}_{rider_id}_OW"
        rt_id = f"{route_group}_{rider_id}_RT"
        rider_short = RIDER_SHORT_NAME[rider_id]
        place_a, place_b = AREAS[area_a], AREAS[area_b]

        fare_products_rows.append({
            "fare_product_id": ow_id,
            "fare_product_name": f"{place_a} \u2013 {place_b} aller simple ({rider_short})",
            "rider_category_id": rider_id,
            "amount": f"{one_way:.2f}",
            "currency": CURRENCY,
        })
        fare_products_rows.append({
            "fare_product_id": rt_id,
            "fare_product_name": f"{place_a} \u2013 {place_b} aller-retour ({rider_short})",
            "rider_category_id": rider_id,
            "amount": f"{return_fare:.2f}",
            "currency": CURRENCY,
        })

        # One row per direction, each referencing both the one-way and
        # return products for that leg (see module docstring).
        for from_area, to_area in [(area_a, area_b), (area_b, area_a)]:
            for prod_id in (ow_id, rt_id):
                fare_leg_rules_rows.append({
                    "leg_group_id": f"{route_group}_{from_area}_{to_area}",
                    "from_area_id": from_area,
                    "to_area_id": to_area,
                    "fare_product_id": prod_id,
                })

    feed_info_note = [{
        "feed_publisher_name": "SPM Ferries (unofficial, manually transcribed)",
        "feed_publisher_url": SOURCE_URL,
        "feed_lang": "fr",
        "feed_version": f"fares-transcribed-{TRANSCRIBED_ON}",
    }]

    return {
        "rider_categories.txt": rider_categories_rows,
        "areas.txt": areas_rows,
        "stop_areas.txt": stop_areas_rows,
        "fare_products.txt": fare_products_rows,
        "fare_leg_rules.txt": fare_leg_rules_rows,
        "feed_info.txt": feed_info_note,
    }


def write_zip(tables, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, rows in tables.items():
            buf = io.StringIO()
            if rows:
                fieldnames = list(rows[0].keys())
                writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            zf.writestr(filename, buf.getvalue())
    print(f"Wrote {out_path}")


def merge_into_gtfs_zip(fares_tables, existing_gtfs_zip, out_path):
    """Convenience: merge these fares tables into an existing schedule GTFS
    zip (e.g. the one produced by spm_gtfs.py) so you end up with one feed."""
    with zipfile.ZipFile(existing_gtfs_zip, "r") as zin:
        existing = {name: zin.read(name) for name in zin.namelist()}
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in existing.items():
            if name == "feed_info.txt":
                continue  # we'll write a merged one below
            zout.writestr(name, data)
        for filename, rows in fares_tables.items():
            buf = io.StringIO()
            if rows:
                fieldnames = list(rows[0].keys())
                writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            zout.writestr(filename, buf.getvalue())
    print(f"Wrote merged feed to {out_path}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="spm_fares.zip", help="Output path for the standalone fares-only zip")
    ap.add_argument("--merge-into", default=None,
                     help="Path to an existing schedule GTFS zip (e.g. from spm_gtfs.py) to merge these tables into")
    ap.add_argument("--merged-out", default="spm_gtfs_with_fares.zip",
                     help="Output path for the merged feed (used with --merge-into)")
    args = ap.parse_args()

    tables = build_tables()
    write_zip(tables, args.out)

    if args.merge_into:
        merge_into_gtfs_zip(tables, args.merge_into, args.merged_out)
