# spm-ferries-gtfs

Unofficial [GTFS](https://gtfs.org) + [GTFS-Fares v2](https://gtfs.org/documentation/schedule/reference/#fares-v2) feed
for [SPM Ferries](https://www.spm-ferries.fr) (Saint-Pierre-et-Miquelon), covering sailings between
Saint-Pierre, Miquelon, Fortune (Newfoundland), and Langlade.

**This is not an official feed.** It is not produced, endorsed, or maintained by SPM Ferries or the
Collectivité territoriale de Saint-Pierre-et-Miquelon. Schedules and fares are derived from SPM Ferries'
public website and may be inaccurate, incomplete, or out of date. **Always confirm sailing times and
prices directly with SPM Ferries before travel.**

## What's here

- `spm_gtfs.py` — scrapes SPM's public schedule widget (`horaires.spm-ferries.fr`) and the LS-Résa
  booking API it calls, and builds a standard GTFS schedule feed, including `booking_rules.txt` and a
  `stops.txt`/`pathways.txt` station hierarchy (see below).
- `spm_fares.py` — builds a GTFS-Fares v2 fares module (adult + reduced fares, one-way/return, for the
  Saint-Pierre↔Miquelon, Saint-Pierre↔Langlade, and Saint-Pierre/Miquelon↔Fortune routes) from SPM's
  published tariff table, and can merge it into the schedule feed above.
- `latest/spm_gtfs.zip` — the most recently generated combined feed (refreshed weekly, see below).
- `latest/gtfs/` — the same feed, unzipped into its individual GTFS `.txt` files, so changes to a
  single file are visible in a normal diff instead of being buried in a binary zip.

## How the data is sourced

- **Schedules** come from a live JSON API (`api.ls-resa.fr`), the same one SPM's own website calls.
  There's no published API contract for this — it's the backend behind their public schedule widget —
  so treat it as best-effort and expect it may change or break without notice.
- **Fares** are hand-transcribed from SPM's static tariff page (not from an API — see comments in
  `spm_fares.py` for the source URL and transcription date) and only cover core adult/reduced fares.
  Group, school, bike, pet, multi-ride card, and PASS SPM fares are not yet included.
- All rider-facing text (`stop_name`, `route_long_name`, fare product/rider category names) is in
  French, matching `agency_lang`/`feed_lang: fr` — except Fortune, which stays in English (`Fortune
  Ferry Terminal`) since it's an anglophone Newfoundland town.

## Data quality notes

- **Fortune (Newfoundland) is a different timezone from the rest of the network.** Saint-Pierre,
  Miquelon, and Langlade are all `America/Miquelon`; Fortune is `America/St_Johns`, 30 minutes behind.
  `stops.txt` sets `stop_timezone` accordingly so any standards-compliant GTFS consumer computes
  correct crossing times automatically. This was cross-checked against SPM's own official
  Miquelon↔Fortune PDF calendar (which labels its times "HEURE LOCALE / LOCAL TIME") and matched
  exactly. `spm_gtfs.py` also runs an independent `zoneinfo`-based sanity check (not a hardcoded
  30-minute assumption — it re-derives real elapsed time from IANA tz data on every run, so it stays
  correct even if either region's DST rules ever change) that flags any leg whose real travel time
  looks physically implausible.
- **The booking system emits administrative placeholder records** (observed boat name
  `MODIFS PAYANTES`, a sentinel capacity of 10,000 seats) that aren't real sailings. `spm_gtfs.py`
  filters these out before building the feed.
- **Bikes are carried** on all SPM Ferries sailings, so every trip sets `bikes_allowed: 1`.
- `feed_info.txt`'s `feed_start_date`/`feed_end_date` are derived automatically from the actual
  min/max dates in `calendar_dates.txt` at merge time — not hardcoded — so they stay accurate as the
  published schedule window rolls forward each week. `feed_version` is stamped with the UTC timestamp
  of the generation run, so consumers can tell which weekly refresh they're looking at.
- Every sailing requires a reservation, so `stop_times.txt` sets `pickup_type: 1` (no boarding without
  booking) with a `booking_rules.txt` reference. Most sailings require booking at least 1 hour ahead;
  Fortune departures before 9:00 AM require booking by 16:00 the day before, which `spm_gtfs.py`
  converts from Fortune (`America/St_Johns`) into `agency_timezone` (`America/Miquelon`) via `zoneinfo`
  rather than hardcoding a fixed offset, so it stays correct if the 30-minute gap ever changes.
- Each port's station in `stops.txt` is split into boarding platforms (`location_type: 0`) and
  entrances (`location_type: 2`) connected by `pathways.txt`, with `traversal_time` set to the real
  lead time a rider needs — e.g. Fortune's Canadian customs control needs 2 hours, so a Fortune-bound
  sailing's platform is 2 hours' pathway-walk from its entrance. This lets a pathways-aware trip
  planner surface the true show-up time instead of assuming an ordinary walk/wait.
- The feed has been validated with [MobilityData's canonical `gtfs-validator`](https://github.com/MobilityData/gtfs-validator)
  with **zero errors and zero warnings**, except one left in deliberately:
  `mixed_case_recommended_field` on `route_short_name` (e.g. `STP-MIQ`). These are short, all-caps
  route/station-code style identifiers — the same convention used by rail and ferry systems generally.
  (`trip_short_name` was previously populated with SPM's internal cruise code but has since been
  dropped, since it wasn't a real per-sailing identifier the way a flight or train number is.)

## Regenerating the feed

```bash
python3 spm_gtfs.py --days-ahead 180 --out spm_gtfs.zip
python3 spm_fares.py --merge-into spm_gtfs.zip --merged-out spm_gtfs_with_fares.zip \
  --unzipped-out latest/gtfs
```

No dependencies beyond the Python standard library. A GitHub Actions workflow
(`.github/workflows/refresh.yml`) regenerates and commits the feed weekly automatically.

## Using the feed

The latest combined feed is always available at:

```
https://raw.githubusercontent.com/nizgoon/spm-ferries-gtfs/main/latest/spm_gtfs.zip
```

Point any GTFS-consuming tool (OpenTripPlanner, a GTFS validator, your own app) at that URL.

Questions or issues with the feed: derek@transit.app (also set as `feed_contact_email` in `feed_info.txt`).

## License

Code in this repo is MIT licensed (see `LICENSE`). The underlying schedule and fare data belongs to
SPM Ferries; this repo only republishes it in a machine-readable format.
