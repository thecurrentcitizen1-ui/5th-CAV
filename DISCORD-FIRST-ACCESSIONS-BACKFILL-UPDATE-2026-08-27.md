# Discord-First Accessions / Retroactive Backfill — 2026-08-27

- New Discord arrivals without a linked Soldier Record are assigned/reconciled as `Prospective Replacement` and directed to the authoritative website recruiting application.
- The Battalion Clerk DM now makes the website application the primary next step rather than presenting the Discord modal as the primary application path.
- Existing website applicants are told not to duplicate their application; `/apply` remains available to attach an already-filed case when needed.
- Approved applicants continue through the existing Replacement Detachment provisioning and credential-delivery workflow.
- Added `/accessions-backfill send_messages:true|false` for Command/Manage Server staff.
- The backfill scans the existing Discord roster, refreshes `discord_members`, leaves established linked Soldiers untouched, reconciles existing recruiting cases, stages unlinked arrivals as Prospective Replacements, and optionally DMs the website-first instructions.
- The command reports linked Soldiers, prospective arrivals, existing cases, approved replacements, DMs sent/blocked, closed cases, and errors.
- The sweep never creates a personnel record or recruiting application for an unlinked user; the website remains authoritative.
- Existing Prospective Replacements website board continues to show active Discord members without a linked personnel record.
