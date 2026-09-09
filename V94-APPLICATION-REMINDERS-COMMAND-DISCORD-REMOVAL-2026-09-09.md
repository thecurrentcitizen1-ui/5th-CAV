# V94 — Application Reminders + Command Discord Removal
Date: 2026-09-09
Baseline: V93 Discord Intake Reliability

## Changes
- Added recurring application reminder for Discord-only prospects with no Recruiting Case.
- Default cadence: every 3 days after the member has been in Discord for at least 3 days.
- Reminder stops automatically as soon as any Recruiting Case exists or the member has an official linked personnel record.
- Reminder supports both /apply and the website application and resumes existing Discord drafts.
- Added Battalion Clerk poller for explicit Command Discord-member removal requests from the website.
- Removal uses Discord kick, not ban, and reports permission/hierarchy failures back to the website action ledger.
- Added APPLICATION_REMINDER_DAYS=3 environment setting.
