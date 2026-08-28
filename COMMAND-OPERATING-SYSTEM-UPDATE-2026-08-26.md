# Command Operating System Update — 2026-08-26

This cumulative build adds:
- self-healing Personnel Sync Control with stale-job retry
- Command Accession Pipeline dashboard and per-case repair
- Discord delivery of award and promotion notices
- Command Promotion Board with published requirement enforcement
- Operation lifecycle AAR review automation
- System Health repair center for sync, progression, accession and operations

Authority remains:
Website = personnel / progression / awards / orders authority.
Battalion Clerk = Discord delivery and synchronization executor.
Discord = access and presentation mirror.
HLL telemetry = verified field-service authority.

## Member Action Center auto-clear repair
- `WATCH` server activity is no longer misclassified as ACTION REQUIRED. Only AT RISK / INACTIVE / ADMIN REVIEW inactivity states generate a live S-1 action.
- M16 inspections that are merely coming due are no longer shown as member-clearable actions. Only an actually overdue inspection generates ACTION REQUIRED; recording the S-4 inspection immediately clears it on refresh by moving the next-due date.
- Passive promotion gates (especially time in grade) are no longer shown as ACTION REQUIRED. They remain live in the 201 File promotion/career panel and advance automatically.
- Legacy Duty Desk notices for Server Activity WATCH, future M16 inspections, and promotion-requirement reminders are filtered against the current authoritative state so stale cards do not survive after the condition changes.
