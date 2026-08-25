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


## 2026-08-18 — Website-First S-3 Operations Support
- Adds Clerk health heartbeat for website status.
- Reloads website-selected OPERATION voice binding during announcement polling.
- Detects website-published operation events and posts scheduled notices/reminders.
- `/schedule` remains available as a backup and now accepts an optional custom credit-minutes threshold.


## 2026-08-19 — Authoritative Role Mirror Queue / Member Reminders
- Battalion Clerk now polls website personnel-state changes and mirrors rank, MOS, company, platoon, squad, and managed leadership roles in Discord.
- Added low-noise deduplicated DMs for approaching M16 inspection and qualification suspense supplied by the website.


## 2026-08-19 — Website-Authoritative Operations / Live M16 Accrual
- `/schedule` is now a guidance-only deprecated command; normal Operations must be scheduled from S-3 on the website.
- Existing 60-second website event/binding refresh remains the bridge that activates the selected Operation voice channel automatically.
- Inactivity watcher now triggers website-side issued-M16 condition refresh hourly.
- Existing 5-minute duty voice flush drives live time-proportional M16 ammunition expenditure.

## 2026-08-20 — Website-Authoritative Replacement Provisioning
- Approved recruits are provisioned through the website Replacement Detachment endpoint without waiting for Discord rank/MOS/formation roles.
- Approved Replacement role remains during S-1 processing and is cleared when the website Recruiting Case reaches ENLISTED after final release.
- Battalion Clerk is now the notification/role-mirroring layer for this workflow, not the personnel-creation authority.


2026-08-24 ORGANIZATION ROLE NORMALIZATION
- Case/spacing-insensitive managed Discord role lookup and duplicate managed-role reporting. See ORGANIZATION-DISCORD-ROLE-NORMALIZATION-2026-08-24.txt.

## 2026-08-24 — Website-Authoritative Discord Organization Cleanup
- Added `/organization-cleanup` preview and `/organization-cleanup confirm:True` execution flow.
- Cleanup reads the complete canonical linked personnel roster from the website before changing managed Discord roles.
- Discord-to-website role-change echo is temporarily suppressed for affected members during maintenance so transient migration states cannot overwrite Company/Platoon/Squad/Team assignments.
- Duplicate managed roles are consolidated case/spacing-insensitively; exact blueprint spelling is preferred.
- Members are migrated to the canonical role before a duplicate is removed.
- Duplicate-role channel/category overwrites are merged onto the surviving canonical role before deletion.
- Obsolete generic Platoon/Squad roles are removed only after canonical website assignments have been reapplied.
- Canonical personnel roles and approved channel permissions are reapplied after cleanup.
- `/structure-status` now directs administrators to the safe cleanup workflow when duplicate managed roles are found.

## 2026-08-24 — Welcome Delivery Preview
- Added `/welcome-preview` for Manage Server / Administrator users.
- Preview uses the same `WELCOME_MESSAGE` constant as live public join notices.
- Refactored the live approval credential DM through `build_recruit_credentials_message()` and reuses that exact builder for preview.
- Preview uses placeholder credentials only and sends nothing to a recruit.
- No roles, website records, credentials, or Welcome Packet milestones are changed by preview.

## 2026-08-24 — MOS Role Mapping Authority
- Confirmed HLL: Vietnam role IDs now seed their corresponding 1/5 Cavalry battlefield MOS codes where the mapping is unambiguous.
- Rifleman 0→11R, Medic 3→91M, Machine Gunner 6→11M, Grenadier 7→11G, Engineer 8→12E, Squad Leader 9→11L, Crewman 11→19K, Tank Commander 12→19C, Pilot 16→67P, Logistics Officer 17→67L.
- Specialist role 5 remains intentionally unmapped until its community MOS relationship is explicitly verified.
- Existing `role_seconds` remains the single authoritative clock; no duplicate MOS timer was added.
