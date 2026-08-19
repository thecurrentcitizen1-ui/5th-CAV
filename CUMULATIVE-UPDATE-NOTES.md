# Battalion Clerk cumulative update
- Approved voice-channel activity system added.
- `/activity-channel-add`, `/activity-channel-remove`, `/activity-channel-status`.
- Only 10+ minute sessions in explicitly approved voice channels update Soldier activity.
- Voice activity remains an activity/neglect signal only; it does not grant operation or training credit.

## Battalion Help Desk Expansion
- Added persistent interactive Help Desk panel with Personnel, Training, Supply, Leadership, and Technical routing.
- Added private ticket channels under `BATTALION HELP DESK` with requester + proper staff-section visibility only.
- Linked Soldiers automatically receive a website `personnel_actions` record when a ticket opens.
- Tickets include Claim and Close Ticket buttons.
- Closing a ticket marks the linked personnel action CLOSED, exports up to 500 messages as a transcript, archives it to the configured archive channel, then removes the private ticket channel.
- Added `/helpdesk-panel`, `/helpdesk-archive`, `/helpdesk-status`, and `/ticket` commands.
- Existing `/request-channel` routes are also notified when a private Help Desk ticket opens.


## Leadership Appointment Sync — 2026-08-17
- Added Team Leader to the Battalion Clerk appointment-role blueprint.
- Platoon Sergeant, Squad Leader, Assistant Squad Leader, and Team Leader are managed as appointments, not ranks.
- Battalion Clerk now mirrors these four leadership appointment roles from the authoritative website personnel record during canonical personnel synchronization.

## 2026-08-18 — Battalion Inactivity & Property Accountability
- Default inactivity stages aligned to 7-day WATCH, 14-day S-1 deficiency, 21-day property accountability, 30-day command review.
- Authorized leave pauses Discord inactivity escalation.
- Battalion Clerk opens S-1/property actions and command actions at the appropriate stages.
- Member reminders no longer tell Soldiers that merely opening their website record resets inactivity.

## 2026-08-18 — Personnel / Training / Replacement Expansion
- Added Replacement Depot Discord status role and synchronization for approved recruiting cases.
- New personnel conversion now accepts REPLACEMENT_DEPOT cases and waits for valid rank, MOS, company, platoon, and squad roles before opening the Soldier record.
- Recruiting DMs now explain Replacement Depot status and automatic Movement Orders.
- Discord /request and Help Desk actions receive automatic suspense dates (3 days for S-1, 5 days for other staff offices).
- Added /suspense-channel to choose the staff channel for action reminders.
- Added hourly Personnel Action Suspense Watch for 2-day, 1-day, due-today, and overdue reminders.


## 2026-08-18 — Verified Automation & Live Activity Credit
- Approved community activity credits live after 10 minutes without leaving the channel.
- Unified four-stage inactivity thresholds with website/M16 logic.
- Operation schedules require a linked website Operation ID.

## 2026-08-18 — Dedicated Operation Reminder System
- Added `/operation-reminder-channel`.
- Added `/operation-reminder-times`.
- Added `/operation-reminder-status`.
- Default reminders: 24 hours, 2 hours, and 30 minutes before OPERATION step-off.
- Scheduling an OPERATION immediately posts an Operation Scheduled notice to the reminder channel.
- Reminder sends are persisted in PostgreSQL to prevent duplicates after restart.


## 2026-08-18 — Prospective Replacements Live Intake (Battalion Clerk)
- Every non-bot Discord member is tracked in discord_members on join.
- Website now exposes active unlinked Discord arrivals on a Command-only Prospective Replacements board.
- Prospective arrivals do not receive a 201 File, rank, strength credit, or official personnel status.
- Board links to an existing Recruiting Case when one is linked; otherwise shows NO APPLICATION.
- Battalion Clerk now preserves the true Discord joined_at timestamp.
- Members automatically disappear from the prospective board once a website_member_links personnel link is created or they leave Discord.


## 2026-08-18 — Operation Round Reconciliation
- Added `/operation-rounds-reconcile operation_id:<UUID>` to repair missing M16 weapon-ledger rounds from filed operation participation.


## 2026-08-18 — Full System Stabilization Review
- Revalidated all slash commands and background source after cumulative website changes.
- Retained live Discord intake joined_at tracking and M16 operation-round reconciliation.
- Added shared active-member index used by both website and Battalion Clerk.
- No duplicate slash command names detected.
