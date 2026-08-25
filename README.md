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
  booking API it calls, and builds a standard GTFS schedule feed.
- `spm_fares.py` — builds a GTFS-Fares v2 fares module (adult + reduced fares, one-way/return, for the
  Saint-Pierre↔Miquelon, Saint-Pierre↔Langlade, and Saint-Pierre/Miquelon↔Fortune routes) from SPM's
  published tariff table, and can merge it into the schedule feed above.
- `latest/spm_gtfs.zip` — the most recently generated combined feed (refreshed weekly, see below).

## How the data is sourced

- **Schedules** come from a live JSON API (`api.ls-resa.fr`), the same one SPM's own website calls.
  There's no published API contract for this — it's the backend behind their public schedule widget —
  so treat it as best-effort and expect it may change or break without notice.
- **Fares** are hand-transcribed from SPM's static tariff page (not from an API — see comments in
  `spm_fares.py` for the source URL and transcription date) and only cover core adult/reduced fares.
  Group, school, bike, pet, multi-ride card, and PASS SPM fares are not yet included.

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
- The feed has been validated with [MobilityData's canonical `gtfs-validator`](https://github.com/MobilityData/gtfs-validator)
  with zero errors — only a handful of optional best-practice warnings (missing `bikes_allowed`,
  no feed contact email, a couple of naming-convention nits).

## Regenerating the feed

```bash
python3 spm_gtfs.py --days-ahead 180 --out spm_gtfs.zip
python3 spm_fares.py --merge-into spm_gtfs.zip --merged-out spm_gtfs_with_fares.zip
```

No dependencies beyond the Python standard library. A GitHub Actions workflow
(`.github/workflows/refresh.yml`) regenerates and commits the feed weekly automatically.

## Using the feed

The latest combined feed is always available at:

```
https://raw.githubusercontent.com/nizgoon/spm-ferries-gtfs/main/latest/spm_gtfs.zip
```

Point any GTFS-consuming tool (OpenTripPlanner, a GTFS validator, your own app) at that URL.

## License

Code in this repo is MIT licensed (see `LICENSE`). The underlying schedule and fare data belongs to
SPM Ferries; this repo only republishes it in a machine-readable format for personal/non-commercial use.