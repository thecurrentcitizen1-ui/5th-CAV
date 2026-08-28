# Automation / Reliability Audit — 2026-08-25

This pass audits the code paths and adds a runtime exception board. Static packaging cannot prove live Railway/PostgreSQL/Discord/RCON behavior without production credentials, so the new Staff > System Health page performs production-state checks after deployment.

## Audited lanes
- Application approval: approval -> replacement provisioning -> 201 File -> Welcome Packet -> login delivery path retained.
- Steam/Gamertag linking: approval queues/links identity with conflict checks; console claims remain pending until telemetry verifies them.
- Discord sync: rank, MOS, assignment, appointment, replacement activation, and lifecycle actions enqueue role synchronization.
- Assignment changes: authoritative Website action updates personnel/history/orders/notices then queues Discord mirror.
- Promotion: permanent rank history/order/member notice/sync is filed through one action path.
- Awards: manual, Command-approved, and automatic awards now all create/open a member Awards notice pointing to 201 File > Awards.
- Member actions: legacy destinations continue to normalize at read time; award notices target the canonical Awards tab.
- Welcome Packet: existing reconcile/repair/submit/Command review flow retained and surfaced in System Health.
- M16: legacy Discord voice round credit has been disabled. HLL server activity remains authoritative for field service/fouling evidence.
- MOS progression: existing 0/1/5/15/30-hour server-role progression retained; runtime audit flags legacy manual proficiency rows.
- Readiness/inactivity: runtime audit surfaces active Soldiers without enough activity evidence for reliable calculation.
- Operations credit: runtime audit flags published active Operations without Clerk linkage.
- Server telemetry: runtime audit flags stale RCON/telemetry heartbeat and Command Desk shows current seeding state.

## Staff consolidation changes
- System Health added as one staff work center.
- All staff search results open the same canonical 201 File; staff-only controls remain permission-gated.
- Global staff search expanded to name, BR/service number, Discord identity, Steam ID, console identity, assignment/MOS/duty, and personnel order number.
- Command Desk now shows live server seeding state and an automation reliability exception card.
- Recruiting Control now consumes the same authorized billet/MOS shortage data used by Command/S-1.
- Vacancy counts remain derived from the authoritative billet and personnel records; no duplicate vacancy ledger was introduced.

## Important correction
The bot's scheduled Operation notice and maintenance loop still contained an older Discord-voice M16 expenditure path. That path is now disabled. The compatibility endpoints return no-op responses so an older caller cannot reintroduce voice-derived M16 credit.
