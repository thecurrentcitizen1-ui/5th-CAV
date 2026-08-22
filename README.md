# 5th Cavalry Regiment — 1966 Digital Battalion Headquarters

A ground-up presentation rebuild for the Hell Let Loose: Vietnam community representing **1st Battalion, 5th Cavalry Regiment, 1st Cavalry Division (Airmobile)**.

## Design intent

The site is treated as a digital interpretation of a 1966 U.S. Army battalion headquarters: arrival/in-processing, S-1 personnel records, 201 Files, S-3 operations, training records, S-4 property, Morning Reports, battalion orders, company orderly rooms, command staff, and recruiting/replacement processing.

The approved community logo and two approved user-supplied field images are used as the source visual world. Several web crops are derived from those images so page environments share consistent lighting, materials, and historical character rather than relying on unrelated stock textures.

## Protected backend

This package preserves the existing PostgreSQL schema identifiers, authentication session fields, role identifiers, Battalion Clerk integration tables, personnel/equipment/qualification/operation identifiers, and Railway configuration. Additions are presentation-only except for the non-destructive `/company/<unit_code>` view route and derived tour-display fields calculated in Python without changing stored records.

## Railway

Keep the existing variables:

- `DATABASE_URL`
- `SECRET_KEY`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`

The Procfile remains the Railway/Gunicorn entry point.

## Repository

Upload this package to **`5th-Cavalry-Website` only**. Do not upload it to the `5th-CAV` Battalion Clerk repository.


## Phase 4 — Ranks & Appointments

Adds the 1965-era rank catalog, appointment catalog, rank history, appointment history,
acting/temporary appointments, relief actions, automatic service-history entries, and
appointment-derived duty access. Rank and appointment remain separate concepts.

Historical note: Command Sergeant Major is intentionally not in the 1965 catalog because
the CSM program/insignia postdates the site's primary 1965 setting.


## Phase 5 — Battalion Organization & Chain of Command

Adds the structured battalion hierarchy, active rifle company/platoon/squad nodes,
company orderly rooms, platoon rosters, S-1 assignment orders, automatic assignment
history, current organizational assignment, chain-of-command resolution from current
appointments, and the NCO/leader "My Soldiers" working file.

Legacy unit_code/platoon/squad values remain in place for compatibility. Structured
unit_node_id relationships are additive and become the hierarchy source for new actions.


## Phase 6 — Morning Report & Battalion Readiness

Adds the living battalion condition layer:
- Army-style Battalion Morning Report generated from personnel records.
- Duty status history and personnel status actions.
- Individual, leader, company and battalion readiness calculations.
- Activity classification without public bot/Discord terminology.
- M16 condition integrated into readiness.
- DEROS 30/60/90-day forecasting and short-timer/tour phase logic.
- Key command vacancy detection from Phase 4 appointments + Phase 5 hierarchy.
- Historical Morning Report snapshots.
- Critical deficiency presentation.
- 201 File readiness/tour status section.

Later Supply, Training and Operations phases can feed additional deficiencies into this
same readiness engine without replacing it.


## Phase 7 — S-4 Supply, Arms Room & Persistent Equipment

Adds:
- S-4 Supply / Arms Room working environment.
- Persistent individual M16 records with serial and rack numbers.
- Round expenditure history prepared for later HLL game-data input.
- Rounds since cleaning / last fired / last cleaned / last inspected.
- Weapon fouling, cleaning, inspection, maintenance and unserviceable states.
- Weapon maintenance journal.
- Persistent field-equipment catalog and issue/turn-in history.
- Individual Equipment Record in the 201 File.
- Company-level supply stock and readiness.
- Supply requisitions with Army-style request numbers.
- Property accountability and current issue roster.
- Uses the approved M16 master image for the Soldier's weapon record.


## Phase 8 — S-3 Operations Center & Battalion Combat History

Adds:
- S-3 Operations Center with five-paragraph OPORDs.
- Operation numbering, status, H-hour, area of operations, commander and tasking.
- Participating-unit records.
- Soldier participation records written into the 201 File.
- Personal Combat Operations Journal.
- Operation ammunition expenditure tied to the assigned M16.
- Casualty/WIA/KIA records tied to operations.
- After Action Reports.
- Battalion Operations Journal.
- Post-operation personnel recommendations.
- Foundation for operation photographs/screenshots to become part of battalion history.


## Phase 9 — Training Office / HLL: Vietnam Duty Qualifications
- Adds the 17 officially listed HLL: Vietnam playable roles as Duty Qualifications.
- Keeps historical Army MOS, rank, appointments, weapons, and issued equipment separate.
- Adds qualification dates, instructors, optional expiration/requalification dates, requests, and deficiency tracking.
- Writes earned duty qualifications into the Soldier's permanent service history and 201 File.

## Battalion Clerk scheduled-duty credit bridge

The Phase 9 build now contains the website side of scheduled attendance credit. It is deliberately presented to members as Army duty/service history; technical communications-platform language is confined to the backend.

Recognized duty channels are exactly: `Training`, `Operation`, and `Meeting`.

Credit rule: a Soldier must accumulate at least **45 minutes / 2700 seconds** of qualifying presence during the scheduled event window. Multiple voice sessions during the same event accumulate. Time before the scheduled start or after the scheduled end does not count. Unscheduled voice activity does not award event credit. A Soldier can receive only one activity credit per scheduled event.

Battalion Clerk integration:

1. When an event is scheduled, POST it to `/internal/clerk/events` with `event_type`, `title`, `starts_at`, `ends_at`, `channel_name`, optional `channel_id`, optional `external_event_id`, and optional website `operation_id`.
2. When a member leaves a tracked duty voice channel (or the event closes), POST the member's interval to `/internal/clerk/attendance` with `guild_id`, `member_id`, `channel_name`, optional `channel_id`, `joined_at`, `left_at`, and optional `session_id`.
3. Both requests require header `X-Battalion-Clerk-Key` matching Railway environment variable `CLERK_SYNC_KEY` on the website.
4. The website resolves the member through `website_member_links`, accumulates qualifying time, and at 45 minutes writes `personnel_activity_credit` plus a permanent `personnel_service_history` entry. Operation events linked to an `operation_id` also file `operation_participation` automatically.

This means the website remains the system of record for service credit while Battalion Clerk only reports scheduled duty and presence.
