# Battalion Clerk V70 — Weekly Battalion Accountability Automation

- Added `/weekly-battalion-report-channel` for Command to select the Discord destination.
- Added automatic Sunday 1900 battalion-time weekly leadership report.
- Report pulls authoritative strength/readiness/activity/action figures from the Website instead of maintaining parallel bot state.
- Includes battalion strength, 7-day HLL activity, 14+/30+ inactivity, average readiness, missing game IDs, ready-to-assign count, staff actions/overdue count, sync errors, 30-day recruiting volume and Company health.
- Uses a weekly notice key to prevent duplicate reports after restarts or repeated hourly checks.
- Existing inactivity, personnel suspense, training, recruiting, routing, publication and live-match formation automations remain intact.
